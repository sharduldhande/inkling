"""§18 acceptance scenarios, end to end through the production pipeline.

Each test here is one Definition-of-Done bullet made executable. The
operational bullets (CU soak, iOS manual pass, rebuild-from-zero) live in
ops/ACCEPTANCE.md instead — they need hardware, not pytest.
"""

from datetime import datetime, timedelta, timezone

import psycopg
import pytest_asyncio

from inkling.db.client import Database
from inkling.inference.gateway import StubGateway
from inkling.scheduler.scheduler import Scheduler
from inkling.turns import ingress
from inkling.turns.commands import parse_commands
from inkling.turns.worker import Worker

from tests.conftest import USER_A

UTC = timezone.utc


@pytest_asyncio.fixture(autouse=True)
async def clean(migrated: str, dsn: str):
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "TRUNCATE turn_events, turns, triggers, transcript, audit_log, "
            "events, docs, commitment_occurrences, commitments, "
            "scheduled_wakeups, deliveries RESTART IDENTITY CASCADE"
        )
        conn.commit()
    yield


async def _rows(db: Database, user_id: str, sql: str, args=()):
    async with db.with_tenant(user_id) as conn:
        cur = await conn.execute(sql, args)
        return await cur.fetchall()


async def test_module1_hw2_closure_yields_a_question_not_closure(db: Database):
    """§18: completing "module 1" with open child hw2 must not close it.

    Scripted sequence: working_state holds module 1 with hw2 open beneath it;
    a commitment for module 1 exists; the model tries CompleteCommitment.
    The closure guard rejects, the reply is annotated, the commitment stays
    open — the model's next move (per prompt) is to ask, not to close.
    """
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "INSERT INTO docs (user_id, kind, version, content, written_by) "
            "VALUES (%s, 'working_state', 1, %s, 'human')",
            (USER_A, "# School\n- module 1 exam prep\n    - hw2 draft\n"),
        )
        await conn.execute(
            "INSERT INTO commitments (user_id, title, source) "
            "VALUES (%s, 'module 1 exam prep', 'conversation')",
            (USER_A,),
        )

    gateway = StubGateway(
        reply="Done! Closed module 1.",
        commands=parse_commands([{
            "type": "CompleteCommitment", "id": 1, "outcome": "done",
            "expected_version": 1, "reason": "user said did module 1",
        }]),
    )
    await ingress.submit(
        db, user_id=USER_A, source="chat", payload={"text": "did module 1"}
    )
    assert await Worker(db, USER_A, gateway).tick() is True

    commitment = (await _rows(db, USER_A, "SELECT * FROM commitments"))[0]
    assert commitment["status"] == "open"  # NOT closed
    rejections = await _rows(
        db, USER_A,
        "SELECT detail FROM audit_log WHERE action = 'command_rejected'",
    )
    assert rejections[0]["detail"]["reason"] == "children unresolved — confirm with user"
    # The user-visible reply was corrected on the same stream (D4).
    transcript = await _rows(
        db, USER_A, "SELECT text FROM transcript WHERE role = 'assistant'"
    )
    assert "couldn't complete CompleteCommitment" in transcript[0]["text"]
    assert "children unresolved" in transcript[0]["text"]


async def test_remind_me_in_10_weeks_survives_and_fires(db: Database):
    """§18: "remind me in 10 weeks" → correct fire_at; time-travel fires it."""
    # 20:00 UTC = afternoon ET: the fire window can't land in quiet hours or
    # the consolidation job's catch-up window, whatever hour this test runs.
    due = (datetime.now(UTC) + timedelta(weeks=10)).replace(
        hour=20, minute=0, second=0, microsecond=0
    )
    gateway = StubGateway(
        reply="I'll remind you.",
        commands=parse_commands([{
            "type": "UpsertCommitment", "title": "renew the lease",
            "due_at": due.isoformat(), "lead_time": "PT1H",
        }]),
    )
    await ingress.submit(
        db, user_id=USER_A, source="chat",
        payload={"text": "remind me in 10 weeks to renew the lease"},
    )
    assert await Worker(db, USER_A, gateway).tick() is True

    occ = (await _rows(db, USER_A, "SELECT * FROM commitment_occurrences"))[0]
    assert occ["fire_at"] == due - timedelta(hours=1)  # due_at - lead_time
    assert occ["status"] == "pending"

    # Time-travel to the fire window: the scheduler surfaces it.
    then = due - timedelta(hours=1)
    sched = Scheduler(db, [USER_A])
    await sched.refresh(now=then)
    assert await sched.fire_due(now=then) == 1
    trigger = (await _rows(
        db, USER_A, "SELECT * FROM triggers WHERE source = 'occurrence'"
    ))[0]
    assert trigger["payload"]["late"] is False
    assert "renew the lease" in trigger["payload"]["text"]


async def test_dead_worker_mid_turn_loses_nothing_and_duplicates_nothing(db: Database):
    """§18 kill -9 equivalent: a claim whose holder dies is reaped per policy.

    Chat triggers error out to the stream (never silently rerun — the user is
    watching); the message itself is preserved in the trigger row. A second
    identical send (client retry with the same idempotency key) is a no-op.
    """
    sub = await ingress.submit(
        db, user_id=USER_A, source="chat", payload={"text": "log my run"},
        idempotency_key="client-msg-77",
    )
    dead = Worker(db, USER_A, StubGateway(), lease_seconds=0)
    assert await dead.claim() is not None  # then the process "dies" here

    live = Worker(db, USER_A, StubGateway(reply="ok"))
    assert await live.reap() == 1

    trigger = (await _rows(db, USER_A, "SELECT * FROM triggers"))[0]
    assert trigger["status"] == "error"  # chat: surfaced as error, not rerun
    assert trigger["payload"]["text"] == "log my run"  # the message survives
    turn = (await _rows(db, USER_A, "SELECT * FROM turns"))[0]
    assert turn["status"] == "error" and "lease expired" in turn["error"]

    retry = await ingress.submit(
        db, user_id=USER_A, source="chat", payload={"text": "log my run"},
        idempotency_key="client-msg-77",
    )
    assert retry.duplicate is True
    assert retry.trigger_id == sub.trigger_id
    assert len(await _rows(db, USER_A, "SELECT 1 FROM triggers")) == 1
