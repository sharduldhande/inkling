"""Turn pipeline (§5): ingress, claim/lease, reaper, commit.

Runs as runtime_role against the Neon test branch, the shape production uses.
"""

import psycopg
import pytest
import pytest_asyncio

from inkling.db.client import Database
from inkling.db.queries import triggers as triggers_q
from inkling.db.queries import turns as turns_q
from inkling.inference.gateway import StubGateway
from inkling.turns import ingress
from inkling.turns.worker import Worker

from tests.conftest import USER_A, USER_B


@pytest_asyncio.fixture(autouse=True)
async def clean(migrated: str, dsn: str):
    """Empty the turn tables between tests.

    Via the owner connection: runtime_role has no DELETE/TRUNCATE grant, which
    is the point (D10) — the app can never erase this history.
    """
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "TRUNCATE turn_events, turns, triggers, transcript, audit_log "
            "RESTART IDENTITY CASCADE"
        )
        conn.commit()
    yield


async def _rows(db: Database, user_id: str, sql: str, args=()):
    async with db.with_tenant(user_id) as conn:
        cur = await conn.execute(sql, args)
        return await cur.fetchall()


# --- ingress (§5.1) ---------------------------------------------------------


async def test_submit_records_pending_trigger_with_source_priority(db: Database):
    sub = await ingress.submit(
        db, user_id=USER_A, source="chat", payload={"text": "hi"}
    )

    assert sub.status == "pending"
    assert sub.duplicate is False
    rows = await _rows(db, USER_A, "SELECT * FROM triggers WHERE id = %s", (sub.trigger_id,))
    assert rows[0]["priority"] == 1
    assert rows[0]["payload"] == {"text": "hi"}


async def test_priority_ordering_is_chat_first_maintenance_last(db: Database):
    for source in ("job", "chat", "occurrence", "wakeup"):
        await ingress.submit(db, user_id=USER_A, source=source, payload={})

    rows = await _rows(
        db, USER_A, "SELECT source, priority FROM triggers ORDER BY priority"
    )
    assert [r["source"] for r in rows] == ["chat", "occurrence", "wakeup", "job"]


async def test_duplicate_idempotency_key_is_a_noop(db: Database):
    first = await ingress.submit(
        db, user_id=USER_A, source="chat", payload={"text": "one"},
        idempotency_key="client-msg-1",
    )
    second = await ingress.submit(
        db, user_id=USER_A, source="chat", payload={"text": "retry"},
        idempotency_key="client-msg-1",
    )

    assert second.duplicate is True
    assert second.trigger_id == first.trigger_id
    rows = await _rows(db, USER_A, "SELECT payload FROM triggers")
    assert len(rows) == 1
    assert rows[0]["payload"] == {"text": "one"}  # retry did not overwrite


async def test_same_key_different_users_are_independent(db: Database):
    a = await ingress.submit(
        db, user_id=USER_A, source="chat", payload={}, idempotency_key="shared"
    )
    b = await ingress.submit(
        db, user_id=USER_B, source="chat", payload={}, idempotency_key="shared"
    )

    assert b.duplicate is False
    assert a.trigger_id != b.trigger_id


async def test_omitted_key_means_not_a_retry(db: Database):
    a = await ingress.submit(db, user_id=USER_A, source="chat", payload={"text": "hi"})
    b = await ingress.submit(db, user_id=USER_A, source="chat", payload={"text": "hi"})

    assert a.trigger_id != b.trigger_id
    assert b.duplicate is False


async def test_calendar_hint_coalesces_into_pending_one(db: Database):
    first = await ingress.submit(
        db, user_id=USER_A, source="calendar", payload={"rev": 1}, source_ref="cal-1"
    )
    second = await ingress.submit(
        db, user_id=USER_A, source="calendar", payload={"rev": 2}, source_ref="cal-1"
    )

    assert first.status == "pending"
    assert second.status == "coalesced"
    async with db.with_tenant(USER_A) as conn:
        assert await triggers_q.pending_count(conn, USER_A) == 1


async def test_calendar_hints_for_different_calendars_do_not_coalesce(db: Database):
    await ingress.submit(
        db, user_id=USER_A, source="calendar", payload={}, source_ref="cal-1"
    )
    second = await ingress.submit(
        db, user_id=USER_A, source="calendar", payload={}, source_ref="cal-2"
    )

    assert second.status == "pending"


async def test_non_calendar_sources_do_not_coalesce(db: Database):
    await ingress.submit(db, user_id=USER_A, source="wakeup", payload={}, source_ref="w")
    second = await ingress.submit(
        db, user_id=USER_A, source="wakeup", payload={}, source_ref="w"
    )

    assert second.status == "pending"


async def test_unknown_source_is_rejected(db: Database):
    with pytest.raises(ValueError, match="unknown trigger source"):
        await ingress.submit(db, user_id=USER_A, source="telepathy", payload={})


# --- claim and lease (§5.2) -------------------------------------------------


async def test_claim_takes_highest_priority_then_oldest(db: Database):
    await ingress.submit(db, user_id=USER_A, source="job", payload={"n": 1})
    await ingress.submit(db, user_id=USER_A, source="chat", payload={"n": 2})
    await ingress.submit(db, user_id=USER_A, source="chat", payload={"n": 3})

    worker = Worker(db, USER_A, StubGateway())
    claimed = await worker.claim()

    assert claimed is not None
    trigger, turn = claimed
    assert trigger["payload"] == {"n": 2}  # chat before job, older chat first
    assert turn["leased_by"] == worker.id
    assert turn["status"] == "running"


async def test_claim_returns_none_on_empty_inbox(db: Database):
    worker = Worker(db, USER_A, StubGateway())
    assert await worker.claim() is None


async def test_claim_marks_trigger_running_and_links_turn(db: Database):
    sub = await ingress.submit(db, user_id=USER_A, source="chat", payload={})
    worker = Worker(db, USER_A, StubGateway())

    claimed = await worker.claim()
    assert claimed is not None

    rows = await _rows(db, USER_A, "SELECT * FROM triggers WHERE id = %s", (sub.trigger_id,))
    assert rows[0]["status"] == "running"
    assert rows[0]["turn_id"] == int(claimed[1]["id"])


async def test_only_one_turn_per_user_across_workers(db: Database):
    """Two workers, two pending triggers: the second must not open a turn.

    This is the §5.2 invariant. SKIP LOCKED keeps the workers off the same
    trigger row; the claim query's NOT EXISTS guard is what stops a second
    concurrent turn. Sequential here — the guard is not a mutex and does not
    hold under two genuinely simultaneous claim transactions.
    """
    await ingress.submit(db, user_id=USER_A, source="chat", payload={"n": 1})
    await ingress.submit(db, user_id=USER_A, source="chat", payload={"n": 2})

    first = Worker(db, USER_A, StubGateway())
    second = Worker(db, USER_A, StubGateway())

    assert await first.claim() is not None
    assert await second.claim() is None

    # The loser's claim rolled back whole: its trigger is still pending.
    async with db.with_tenant(USER_A) as conn:
        assert await triggers_q.pending_count(conn, USER_A) == 1
    running = await _rows(db, USER_A, "SELECT id FROM turns WHERE status = 'running'")
    assert len(running) == 1


async def test_a_users_turn_does_not_block_another_user(db: Database):
    await ingress.submit(db, user_id=USER_A, source="chat", payload={})
    await ingress.submit(db, user_id=USER_B, source="chat", payload={})

    assert await Worker(db, USER_A, StubGateway()).claim() is not None
    assert await Worker(db, USER_B, StubGateway()).claim() is not None


async def test_heartbeat_extends_lease_only_for_the_holder(db: Database):
    await ingress.submit(db, user_id=USER_A, source="chat", payload={})
    worker = Worker(db, USER_A, StubGateway())
    claimed = await worker.claim()
    assert claimed is not None
    turn_id = int(claimed[1]["id"])

    async with db.with_tenant(USER_A) as conn:
        assert await turns_q.heartbeat(conn, turn_id, worker.id, 90) is True
        assert await turns_q.heartbeat(conn, turn_id, "some-other-worker", 90) is False


# --- reaper (§5.2) ----------------------------------------------------------


async def test_reaper_fails_expired_turn_and_requeues_non_chat(db: Database):
    sub = await ingress.submit(db, user_id=USER_A, source="wakeup", payload={})
    dead = Worker(db, USER_A, StubGateway(), lease_seconds=0)  # expires immediately
    assert await dead.claim() is not None

    reaped = await Worker(db, USER_A, StubGateway()).reap()

    assert reaped == 1
    turns = await _rows(db, USER_A, "SELECT status, error FROM turns")
    assert turns[0]["status"] == "error"
    assert turns[0]["error"] == "lease expired"
    triggers = await _rows(db, USER_A, "SELECT status FROM triggers WHERE id = %s", (sub.trigger_id,))
    assert triggers[0]["status"] == "pending"  # requeued for attempt 2


async def test_non_chat_trigger_gives_up_after_two_attempts(db: Database):
    sub = await ingress.submit(db, user_id=USER_A, source="wakeup", payload={})
    reaper = Worker(db, USER_A, StubGateway())

    for _ in range(2):
        dead = Worker(db, USER_A, StubGateway(), lease_seconds=0)
        assert await dead.claim() is not None
        await reaper.reap()

    triggers = await _rows(db, USER_A, "SELECT status FROM triggers WHERE id = %s", (sub.trigger_id,))
    assert triggers[0]["status"] == "error"
    assert await reaper.claim() is None  # not retried a third time


async def test_chat_trigger_is_not_retried_after_reap(db: Database):
    sub = await ingress.submit(db, user_id=USER_A, source="chat", payload={"text": "hi"})
    dead = Worker(db, USER_A, StubGateway(), lease_seconds=0)
    assert await dead.claim() is not None

    await Worker(db, USER_A, StubGateway()).reap()

    triggers = await _rows(db, USER_A, "SELECT status FROM triggers WHERE id = %s", (sub.trigger_id,))
    assert triggers[0]["status"] == "error"  # the user is waiting; no silent rerun


async def test_reaping_frees_the_slot_for_the_next_turn(db: Database):
    await ingress.submit(db, user_id=USER_A, source="chat", payload={})
    await ingress.submit(db, user_id=USER_A, source="chat", payload={})
    dead = Worker(db, USER_A, StubGateway(), lease_seconds=0)
    assert await dead.claim() is not None

    live = Worker(db, USER_A, StubGateway())
    assert await live.claim() is None  # slot still held by the expired turn
    await live.reap()
    assert await live.claim() is not None


async def test_reaper_leaves_live_leases_alone(db: Database):
    await ingress.submit(db, user_id=USER_A, source="chat", payload={})
    worker = Worker(db, USER_A, StubGateway())
    assert await worker.claim() is not None

    assert await worker.reap() == 0


# --- full turn + commit (§5.4) ----------------------------------------------


async def test_tick_runs_a_turn_end_to_end(db: Database):
    sub = await ingress.submit(
        db, user_id=USER_A, source="chat", payload={"text": "how are you"}
    )
    gateway = StubGateway("doing fine", chunk_size=5)
    worker = Worker(db, USER_A, gateway)

    assert await worker.tick() is True

    turns = await _rows(db, USER_A, "SELECT * FROM turns")
    assert turns[0]["status"] == "ok"
    assert turns[0]["model"] == "stub"
    assert turns[0]["finished_at"] is not None
    assert turns[0]["output_tokens"] == 2

    triggers = await _rows(db, USER_A, "SELECT status FROM triggers WHERE id = %s", (sub.trigger_id,))
    assert triggers[0]["status"] == "done"

    # The gateway saw the user's text as the prompt.
    assert gateway.calls[0]["prompt"] == "how are you"


async def test_turn_events_carry_v1_stream_shape(db: Database):
    await ingress.submit(db, user_id=USER_A, source="chat", payload={"text": "hi"})
    await Worker(db, USER_A, StubGateway("abcdef", chunk_size=2)).tick()

    events = await _rows(db, USER_A, "SELECT seq, kind, data FROM turn_events ORDER BY seq")

    assert [e["kind"] for e in events] == ["text_delta"] * 3 + ["done", "exit"]
    assert [e["seq"] for e in events] == [1, 2, 3, 4, 5]  # contiguous for resume
    assert "".join(e["data"]["text"] for e in events[:3]) == "abcdef"
    assert events[3]["data"] == {"done": True}
    assert events[4]["data"] == {"exit": 0}


async def test_commit_writes_user_and_assistant_transcript(db: Database):
    await ingress.submit(db, user_id=USER_A, source="chat", payload={"text": "ping"})
    await Worker(db, USER_A, StubGateway("pong")).tick()

    rows = await _rows(db, USER_A, "SELECT role, text FROM transcript ORDER BY id")

    assert [(r["role"], r["text"]) for r in rows] == [
        ("user", "ping"),
        ("assistant", "pong"),
    ]


async def test_non_chat_turn_records_no_user_transcript(db: Database):
    await ingress.submit(db, user_id=USER_A, source="wakeup", payload={"note": "2pm"})
    await Worker(db, USER_A, StubGateway("reminder")).tick()

    rows = await _rows(db, USER_A, "SELECT role FROM transcript")

    assert [r["role"] for r in rows] == ["assistant"]


async def test_commands_are_audited_not_applied_until_section_9(db: Database):
    await ingress.submit(db, user_id=USER_A, source="chat", payload={"text": "hi"})
    gateway = StubGateway("ok", commands=[{"type": "add_habit", "name": "walk"}])

    await Worker(db, USER_A, gateway).tick()

    async with db.with_tenant(USER_A) as conn:
        cur = await conn.execute("SELECT action, detail FROM audit_log")
        audits = await cur.fetchall()
    assert [a["action"] for a in audits] == ["command_not_implemented"]
    assert audits[0]["detail"]["command"] == {"type": "add_habit", "name": "walk"}


async def test_subscriber_sees_deltas_live(db: Database):
    await ingress.submit(db, user_id=USER_A, source="chat", payload={"text": "hi"})
    seen: list[tuple[int, str, dict]] = []

    async def subscriber(turn_id, seq, kind, data):
        seen.append((seq, kind, data))

    await Worker(db, USER_A, StubGateway("xy", chunk_size=1), subscriber=subscriber).tick()

    assert seen == [(1, "text_delta", {"text": "x"}), (2, "text_delta", {"text": "y"})]


async def test_a_broken_subscriber_does_not_fail_the_turn(db: Database):
    await ingress.submit(db, user_id=USER_A, source="chat", payload={"text": "hi"})

    async def subscriber(turn_id, seq, kind, data):
        raise RuntimeError("socket died")

    await Worker(db, USER_A, StubGateway("ok"), subscriber=subscriber).tick()

    turns = await _rows(db, USER_A, "SELECT status FROM turns")
    assert turns[0]["status"] == "ok"


# --- failure paths ----------------------------------------------------------


class ExplodingGateway:
    async def stream(self, *, user_id, prompt, context):
        raise RuntimeError("provider exploded")
        yield  # pragma: no cover — makes this an async generator


async def test_gateway_failure_errors_the_turn_and_streams_the_error(db: Database):
    sub = await ingress.submit(db, user_id=USER_A, source="chat", payload={"text": "hi"})

    await Worker(db, USER_A, ExplodingGateway()).tick()

    turns = await _rows(db, USER_A, "SELECT status, error FROM turns")
    assert turns[0]["status"] == "error"
    assert "provider exploded" in turns[0]["error"]
    events = await _rows(db, USER_A, "SELECT kind, data FROM turn_events ORDER BY seq")
    assert events[-1]["kind"] == "error"
    triggers = await _rows(db, USER_A, "SELECT status FROM triggers WHERE id = %s", (sub.trigger_id,))
    assert triggers[0]["status"] == "error"


async def test_failed_turn_frees_the_slot(db: Database):
    await ingress.submit(db, user_id=USER_A, source="chat", payload={"text": "hi"})
    await ingress.submit(db, user_id=USER_A, source="chat", payload={"text": "hi again"})

    await Worker(db, USER_A, ExplodingGateway()).tick()

    assert await Worker(db, USER_A, StubGateway()).claim() is not None


async def test_loop_survives_a_failing_turn_and_drains_the_inbox(db: Database):
    """A poisoned turn must not wedge the worker (§5.2 reaper intent)."""
    await ingress.submit(db, user_id=USER_A, source="wakeup", payload={})
    await Worker(db, USER_A, ExplodingGateway()).tick()

    # Attempt 2 with a healthy gateway succeeds and the inbox drains.
    worker = Worker(db, USER_A, StubGateway("recovered"))
    assert await worker.tick() is True
    assert await worker.tick() is False

    turns = await _rows(db, USER_A, "SELECT status FROM turns ORDER BY id")
    assert [t["status"] for t in turns] == ["error", "ok"]
