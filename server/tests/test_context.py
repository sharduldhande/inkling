"""Context assembly (§8): section order, budgets, truncation, retrieval gate.

Runs as runtime_role against the Neon test branch, the shape production uses.
"""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
import pytest_asyncio

from inkling.db.client import Database
from inkling.inference.gateway import StubGateway
from inkling.turns import ingress
from inkling.turns.context import (
    BUDGETS,
    _render_tail,
    assemble_context,
    references_past,
)
from inkling.turns.validate import estimate_tokens
from inkling.turns.worker import Worker

from tests.conftest import USER_A, USER_B

NOW = datetime.now(timezone.utc)


@pytest_asyncio.fixture(autouse=True)
async def clean(migrated: str, dsn: str):
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "TRUNCATE turn_events, turns, triggers, transcript, audit_log, "
            "events, docs, commitment_occurrences, commitments, habits, "
            "memory_facts, scheduled_wakeups, ingested_documents "
            "RESTART IDENTITY CASCADE"
        )
        conn.commit()
    yield


def _trigger(source: str = "chat", text: str = "hi", **overrides) -> dict:
    row = {
        "id": 1,
        "source": source,
        "source_ref": None,
        "payload": {"text": text},
    }
    row.update(overrides)
    return row


async def _seed_doc(db: Database, user_id: str, kind: str, content: str) -> None:
    async with db.with_tenant(user_id) as conn:
        await conn.execute(
            "INSERT INTO docs (user_id, kind, version, content, written_by) "
            "SELECT %s, %s, COALESCE(MAX(version), 0) + 1, %s, 'human' "
            "FROM docs WHERE user_id = %s AND kind = %s",
            (user_id, kind, content, user_id, kind),
        )


async def _seed_transcript(
    db: Database, user_id: str, role: str, text: str, *, ts: datetime | None = None
) -> None:
    async with db.with_tenant(user_id) as conn:
        await conn.execute(
            "INSERT INTO transcript (user_id, role, text, ts) "
            "VALUES (%s, %s, %s, COALESCE(%s::timestamptz, now()))",
            (user_id, role, text, ts),
        )


async def _rows(db: Database, user_id: str, sql: str, args=()):
    async with db.with_tenant(user_id) as conn:
        cur = await conn.execute(sql, args)
        return await cur.fetchall()


# --- section order and presence (§8.1) --------------------------------------


async def test_sections_appear_in_stable_to_volatile_order(db: Database):
    await _seed_doc(db, USER_A, "goals", "Run a marathon.")
    await _seed_doc(db, USER_A, "patterns", "Skips workouts when travelling.")
    await _seed_doc(db, USER_A, "digest7", "Solid week.")
    await _seed_doc(db, USER_A, "working_state", "# School\n- essay draft")
    await _seed_doc(db, USER_A, "daily_plan", "Morning: gym.")
    await _seed_transcript(db, USER_A, "user", "the essay is due Friday")

    got = await assemble_context(
        db, user_id=USER_A,
        trigger=_trigger(text="what did I say about the essay yesterday?"),
    )

    headers = [
        "## Goals",
        "## Patterns & facts",
        "## Last 7 days (digest)",
        "## Working state",
        "## Today's plan",
        "## Commitments (due / upcoming)",
        "## Recent conversation",
        "## Possibly relevant past",
        "## Trigger",
    ]
    positions = [got.context.index(h) for h in headers]
    assert positions == sorted(positions)
    # The trigger body ends the context (bar the first_contact_of_day flag,
    # which depends on the wall clock and gets its own tests below).
    assert (
        "User message: what did I say about the essay yesterday?"
        in got.context[positions[-1]:]
    )


async def test_context_carries_an_authoritative_clock(db: Database):
    """The model has no clock: without this it reconstructs 'now' from
    transcript markers and its own notes — live it drifted a full day ahead
    and sent 'Morning' pushes at 00:39."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    got = await assemble_context(db, user_id=USER_A, trigger=_trigger())
    local = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
    assert "## Now\n" + local.strftime("%A %Y-%m-%d, %H:") in got.context
    assert "Trust this over any other time reference" in got.context


async def test_commitments_and_working_state_present_even_when_empty(db: Database):
    got = await assemble_context(db, user_id=USER_A, trigger=_trigger())

    assert "## Commitments (due / upcoming)\n(none)" in got.context
    assert "## Working state\n(none yet)" in got.context
    # Empty optional sections are skipped, not rendered hollow.
    assert "## Goals" not in got.context


async def test_commitment_lines_carry_id_and_version_for_the_command_flow(db: Database):
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "INSERT INTO commitments (user_id, title, due_at, source) "
            "VALUES (%s, 'file taxes', %s, 'conversation')",
            (USER_A, NOW - timedelta(hours=2)),
        )

    got = await assemble_context(db, user_id=USER_A, trigger=_trigger())

    assert "[commitment 1 v1] file taxes" in got.context
    assert "OVERDUE" in got.context


async def test_undated_open_commitments_surface_after_dated_ones(db: Database):
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "INSERT INTO commitments (user_id, title, source) "
            "VALUES (%s, 'someday: learn piano', 'conversation')",
            (USER_A,),
        )
        await conn.execute(
            "INSERT INTO commitments (user_id, title, due_at, source) "
            "VALUES (%s, 'renew passport', %s, 'conversation')",
            (USER_A, NOW + timedelta(hours=5)),
        )

    got = await assemble_context(db, user_id=USER_A, trigger=_trigger())

    assert got.context.index("renew passport") < got.context.index("learn piano")


async def test_commitment_beyond_horizon_is_not_surfaced(db: Database):
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "INSERT INTO commitments (user_id, title, due_at, source) "
            "VALUES (%s, 'far future thing', %s, 'conversation')",
            (USER_A, NOW + timedelta(days=30)),
        )

    got = await assemble_context(db, user_id=USER_A, trigger=_trigger())

    assert "far future thing" not in got.context


# --- budgets and truncation (§8.1) ------------------------------------------


async def test_oversized_goals_doc_is_truncated_with_marker(db: Database):
    await _seed_doc(db, USER_A, "goals", "word " * 2000)  # ~2500 tokens > 400

    got = await assemble_context(db, user_id=USER_A, trigger=_trigger())

    assert "[goals truncated to its context budget]" in got.context
    assert got.sizes["goals"] <= BUDGETS["goals"] + 40  # marker overhead only


async def test_transcript_tail_keeps_newest_rows(db: Database):
    for i in range(60):
        await _seed_transcript(
            db, USER_A, "user", f"message number {i}",
            ts=NOW - timedelta(minutes=60 - i),
        )

    got = await assemble_context(db, user_id=USER_A, trigger=_trigger())

    assert "message number 59" in got.context
    assert "message number 19" not in got.context  # beyond the 40-message tail
    assert got.sizes["transcript"] <= BUDGETS["transcript"] + 40


async def test_over_budget_tail_drops_oldest_and_stays_time_anchored(db: Database):
    for i in range(40):
        await _seed_transcript(
            db, USER_A, "user", f"long message {i} " + "filler " * 150,
            ts=NOW - timedelta(minutes=40 - i),
        )

    got = await assemble_context(db, user_id=USER_A, trigger=_trigger())

    section = got.context.split("## Recent conversation", 1)[1]
    lines = section.splitlines()
    assert lines[1] == "[earlier messages omitted]"
    # The surviving head re-grows its absolute-time anchor: either an opening
    # (HH:MM) on the first message or a dated day marker above it.
    assert re.match(r"(User \(\d\d:\d\d\): |— )", lines[2])
    assert "long message 39" in section
    assert got.sizes["transcript"] <= BUDGETS["transcript"] + 40


async def test_over_cap_working_state_gets_marker_and_queues_consolidation(db: Database):
    await _seed_doc(db, USER_A, "working_state", "- open loop x\n" * 500)  # >1200 tokens

    got = await assemble_context(db, user_id=USER_A, trigger=_trigger())

    assert "a consolidation pass has been queued" in got.context
    jobs = await _rows(
        db, USER_A, "SELECT * FROM triggers WHERE source = 'job'"
    )
    assert len(jobs) == 1
    assert jobs[0]["payload"]["job"] == "consolidate"
    assert jobs[0]["source_ref"] == "working_state:v1"

    # Same doc version → same idempotency key → no duplicate maintenance work.
    await assemble_context(db, user_id=USER_A, trigger=_trigger())
    jobs = await _rows(db, USER_A, "SELECT * FROM triggers WHERE source = 'job'")
    assert len(jobs) == 1


async def test_within_cap_working_state_queues_nothing(db: Database):
    await _seed_doc(db, USER_A, "working_state", "# Life\n- one open loop")

    got = await assemble_context(db, user_id=USER_A, trigger=_trigger())

    assert "consolidation" not in got.context
    assert await _rows(db, USER_A, "SELECT 1 FROM triggers WHERE source = 'job'") == []


# --- tail dialogue rendering (unit, no DB) -----------------------------------

_TAIL_TZ = ZoneInfo("America/New_York")
_TAIL_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=_TAIL_TZ)


def _tail_row(ts: datetime, role: str, text: str) -> dict:
    return {"ts": ts, "role": role, "text": text}


def _at(day: int, hour: int, minute: int) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=_TAIL_TZ)


def test_tail_same_day_burst_renders_as_anchored_dialogue():
    rows = [
        _tail_row(_at(22, 9, 14), "user", "how's my day looking"),
        _tail_row(_at(22, 9, 14), "assistant", "Gym at 6, report due."),
        _tail_row(_at(22, 9, 15), "user", "done with the report"),
    ]
    assert _render_tail(rows, _TAIL_TZ, _TAIL_NOW) == [
        "User (09:14): how's my day looking",
        "You: Gym at 6, report due.",
        "User: done with the report",
    ]


def test_tail_marks_long_silences_with_elapsed_time():
    rows = [
        _tail_row(_at(22, 9, 0), "user", "morning"),
        _tail_row(_at(22, 9, 45), "user", "back"),
        _tail_row(_at(22, 11, 45), "assistant", "How did it go?"),
    ]
    assert _render_tail(rows, _TAIL_TZ, _TAIL_NOW) == [
        "User (09:00): morning",
        "— 45m pass, now 09:45 —",
        "User: back",
        "— 2h pass, now 11:45 —",
        "You: How did it go?",
    ]


def test_tail_day_boundaries_get_dated_markers_not_anchors():
    rows = [
        _tail_row(_at(21, 22, 15), "user", "good night"),
        _tail_row(_at(22, 8, 5), "user", "morning"),
    ]
    yesterday = _at(21, 22, 15).strftime("%A %Y-%m-%d, %H:%M")
    today = _at(22, 8, 5).strftime("%A %Y-%m-%d, %H:%M")
    assert _render_tail(rows, _TAIL_TZ, _TAIL_NOW) == [
        f"— {yesterday} —",
        "User: good night",
        f"— {today} —",
        "User: morning",
    ]


# --- memory facts and deterministic queries ---------------------------------


async def test_active_facts_render_and_invalidated_ones_do_not(db: Database):
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "INSERT INTO memory_facts (user_id, subject, predicate, value, confidence) "
            "VALUES (%s, 'user', 'prefers', 'morning workouts', 0.9)",
            (USER_A,),
        )
        await conn.execute(
            "INSERT INTO memory_facts (user_id, subject, predicate, value, invalidated_at) "
            "VALUES (%s, 'user', 'lives_in', 'Boston', now())",
            (USER_A,),
        )

    got = await assemble_context(db, user_id=USER_A, trigger=_trigger())

    assert "user prefers morning workouts" in got.context
    assert "Boston" not in got.context


async def test_todays_events_and_streaks_render(db: Database):
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "INSERT INTO habits (user_id, id, title) VALUES (%s, 'gym', 'Gym')",
            (USER_A,),
        )
        for days_ago in (0, 1, 3):
            await conn.execute(
                "INSERT INTO events (user_id, occurred_at, kind, habit_id, note) "
                "VALUES (%s, %s, 'habit_done', 'gym', 'lifted')",
                (USER_A, NOW - timedelta(days=days_ago)),
            )

    got = await assemble_context(db, user_id=USER_A, trigger=_trigger())

    assert "## Today so far" in got.context
    assert "habit_done gym" in got.context
    assert "3 of last 7 days" in got.context


# --- episodic retrieval gate (§8.1 section 10) ------------------------------


def test_past_reference_heuristic():
    assert references_past("when did I last run the Peachtree?")
    assert references_past("what did we talk about yesterday")
    assert references_past("remember that thing about rent?")
    assert not references_past("log 20 pushups")
    assert not references_past("remind me to call mom tomorrow")


async def test_past_referencing_trigger_retrieves_matching_episodes(db: Database):
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "INSERT INTO events (user_id, occurred_at, kind, note) "
            "VALUES (%s, %s, 'note', 'ran the Peachtree race in 55 minutes')",
            (USER_A, NOW - timedelta(days=40)),
        )

    got = await assemble_context(
        db, user_id=USER_A,
        trigger=_trigger(text="when did I last run the Peachtree race?"),
    )

    assert "## Possibly relevant past" in got.context
    assert "Peachtree race in 55 minutes" in got.context


async def test_present_tense_trigger_skips_retrieval(db: Database):
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "INSERT INTO events (user_id, occurred_at, kind, note) "
            "VALUES (%s, %s, 'note', 'ran the Peachtree race')",
            (USER_A, NOW - timedelta(days=40)),
        )

    got = await assemble_context(db, user_id=USER_A, trigger=_trigger(text="log 20 pushups"))

    assert "## Possibly relevant past" not in got.context


async def test_retrieval_is_tenant_scoped(db: Database):
    async with db.with_tenant(USER_B) as conn:
        await conn.execute(
            "INSERT INTO events (user_id, occurred_at, kind, note) "
            "VALUES (%s, %s, 'note', 'ran the Peachtree race')",
            (USER_B, NOW - timedelta(days=40)),
        )

    got = await assemble_context(
        db, user_id=USER_A,
        trigger=_trigger(text="when did I last run the Peachtree race?"),
    )

    # The trigger echo itself mentions Peachtree; the point is that USER_B's
    # event never surfaces as a retrieved episode.
    assert "## Possibly relevant past" not in got.context


# --- non-chat trigger shapes and worker integration -------------------------


async def test_wakeup_trigger_renders_source_ref_and_late_flag(db: Database):
    trigger = _trigger(
        source="wakeup", source_ref="wakeup:7",
        payload={"text": "check on essay progress", "late": True},
    )

    got = await assemble_context(db, user_id=USER_A, trigger=trigger)

    assert (
        "[wakeup trigger wakeup:7] (late — fired past its time) "
        "check on essay progress" in got.context
    )


# --- first contact of the day -----------------------------------------------


def _set_quiet_end(dsn: str, user_id: str, quiet_end: str) -> None:
    """FORCE RLS applies to the owner too: the tenant must be set to write."""
    with psycopg.connect(dsn) as conn:
        conn.execute("BEGIN")
        conn.execute("SELECT set_config('app.user_id', %s, true)", (user_id,))
        cur = conn.execute(
            "UPDATE users SET quiet_end = %s WHERE id = %s RETURNING id",
            (quiet_end, user_id),
        )
        assert cur.fetchone() is not None, "RLS silently swallowed the update"
        conn.execute("COMMIT")


@pytest_asyncio.fixture
async def midnight_quiet_end(dsn: str):
    """quiet_end 00:00 makes the day-open boundary local midnight, so 'today'
    vs 'before today' is deterministic whatever wall-clock time the suite runs."""
    _set_quiet_end(dsn, USER_A, "00:00")
    yield
    _set_quiet_end(dsn, USER_A, "11:59")


async def test_first_contact_flag_set_when_no_conversation_today(
    db: Database, midnight_quiet_end
):
    await _seed_transcript(
        db, USER_A, "assistant", "good night", ts=NOW - timedelta(days=2)
    )
    got = await assemble_context(
        db, user_id=USER_A,
        trigger=_trigger(source="presence", text="Scheduled check-in."),
    )
    assert "first_contact_of_day" in got.context
    assert "[presence trigger] Scheduled check-in." in got.context


async def test_first_contact_flag_set_on_an_empty_transcript(
    db: Database, midnight_quiet_end
):
    got = await assemble_context(db, user_id=USER_A, trigger=_trigger())
    assert "first_contact_of_day" in got.context


async def test_first_contact_flag_clears_after_todays_first_exchange(
    db: Database, midnight_quiet_end
):
    await _seed_transcript(db, USER_A, "user", "morning")  # ts = now: today
    got = await assemble_context(db, user_id=USER_A, trigger=_trigger())
    assert "first_contact_of_day" not in got.context


@pytest_asyncio.fixture
async def late_quiet_end(dsn: str):
    """quiet_end just before midnight puts 'now' BEFORE today's boundary all
    day, so the most-recent-boundary rule is what keeps the flag honest."""
    _set_quiet_end(dsn, USER_A, "23:59:59")
    yield
    _set_quiet_end(dsn, USER_A, "11:59")


async def test_midnight_is_not_a_new_day(db: Database, late_quiet_end):
    """Regression: a conversation minutes ago, with today's quiet_end still
    ahead, must NOT read as first contact — the boundary is the most recent
    quiet_end, not today's. (Seen live: three 'Morning' pushes at 00:14.)"""
    await _seed_transcript(db, USER_A, "user", "the tank is low")
    got = await assemble_context(db, user_id=USER_A, trigger=_trigger())
    assert "first_contact_of_day" not in got.context


async def test_active_wakeups_render_for_reconciliation(db: Database):
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "INSERT INTO scheduled_wakeups (user_id, fire_at, purpose, created_by) "
            "VALUES (%s, %s, 'check French block landed', 'agent:turn:1')",
            (USER_A, NOW + timedelta(hours=2)),
        )
    got = await assemble_context(db, user_id=USER_A, trigger=_trigger())
    assert "## Your scheduled wakeups" in got.context
    assert "check French block landed" in got.context
    assert "[wakeup 1]" in got.context  # the id CancelWakeup needs


async def test_no_wakeups_no_section(db: Database):
    got = await assemble_context(db, user_id=USER_A, trigger=_trigger())
    assert "## Your scheduled wakeups" not in got.context


async def test_worker_passes_assembled_context_to_the_gateway(db: Database):
    await _seed_doc(db, USER_A, "goals", "Ship Inkling.")
    await ingress.submit(db, user_id=USER_A, source="chat", payload={"text": "hi"})
    gateway = StubGateway(reply="hello")
    worker = Worker(db, USER_A, gateway)

    assert await worker.tick() is True

    call = gateway.calls[0]
    assert "## Goals\nShip Inkling." in call["context"]
    assert "User message: hi" in call["context"]
    assert "## Commitments (due / upcoming)" in call["context"]


async def test_assembly_totals_stay_within_the_overall_budget_sum(db: Database):
    """Every section is at or under its budget, so the sum is bounded (§8.1)."""
    for kind in ("goals", "patterns", "digest7", "working_state", "daily_plan"):
        await _seed_doc(db, USER_A, kind, "filler " * 3000)
    for i in range(30):
        await _seed_transcript(db, USER_A, "user", f"padding message {i} " * 40)

    got = await assemble_context(db, user_id=USER_A, trigger=_trigger())

    for name, tokens in got.sizes.items():
        assert tokens <= BUDGETS[name] + 40, f"{name} over budget: {tokens}"
    assert estimate_tokens(got.context) <= sum(BUDGETS.values()) + 200
