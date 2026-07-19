"""Scheduler (§10): heap load, firing CAS, quiet hours, catch-up, invalidation.

Runs as runtime_role against the Neon test branch. The scheduler's refresh and
fire_due take an explicit `now`, so these tests drive time instead of sleeping.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
import pytest
import pytest_asyncio

from inkling.db.client import Database
from inkling.db.queries import commitments as commitments_q
from inkling.db.queries import users as users_q
from inkling.db.queries import wakeups as wakeups_q
from inkling.inference.gateway import StubGateway
from inkling.scheduler.occurrences import materialize_user, next_instance
from inkling.scheduler.scheduler import Scheduler, quiet_deferral
from inkling.turns.apply import apply_command
from inkling.turns.commands import parse_commands
from inkling.turns.worker import Worker

from tests.conftest import USER_A

UTC = timezone.utc
NY = ZoneInfo("America/New_York")

# 14:00 EDT — outside the seeded default quiet window (03:00–11:59 local).
T = datetime(2026, 7, 17, 18, 0, tzinfo=UTC)
# 09:00 EDT — inside it, and (unlike earlier hours) past the consolidation
# job's 6h catch-up window, so fire counts here are wakeups/occurrences only.
T_QUIET = datetime(2026, 7, 17, 13, 0, tzinfo=UTC)
# 11:59 EDT — the window's end, exclusive, so fires resume here.
T_QUIET_END = datetime(2026, 7, 17, 15, 59, tzinfo=UTC)


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


async def _seed_wakeup(
    db: Database,
    *,
    fire_at: datetime | None,
    recurrence: str | None = None,
    purpose: str = "check in",
    respect_quiet: bool = True,
) -> int:
    async with db.with_tenant(USER_A) as conn:
        cur = await conn.execute(
            "INSERT INTO scheduled_wakeups "
            "(user_id, fire_at, recurrence, purpose, created_by, respect_quiet) "
            "VALUES (%s, %s, %s, %s, 'seed', %s) RETURNING id",
            (USER_A, fire_at, recurrence, purpose, respect_quiet),
        )
        return int((await cur.fetchone())["id"])


async def _seed_occurrence(
    db: Database, *, fire_at: datetime, title: str = "file taxes"
) -> tuple[int, int]:
    async with db.with_tenant(USER_A) as conn:
        commitment = await commitments_q.insert(
            conn, user_id=USER_A, title=title, body=None,
            due_at=fire_at, recurrence=None, lead_time=None,
            source="conversation", source_ref=None, notify_policy=None,
        )
        occ_id = await commitments_q.insert_occurrence(
            conn, user_id=USER_A, commitment_id=int(commitment["id"]),
            fire_at=fire_at,
        )
        return int(commitment["id"]), int(occ_id)


def _scheduler(db: Database, **kwargs) -> Scheduler:
    return Scheduler(db, [USER_A], **kwargs)


# --- rrule helpers and materialization --------------------------------------


def test_next_instance_daily():
    start = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    nxt = next_instance("FREQ=DAILY", dtstart=start, after=T)
    assert nxt == datetime(2026, 7, 18, 9, 0, tzinfo=UTC)


async def test_materialize_creates_next_recurring_occurrence_idempotently(db: Database):
    async with db.with_tenant(USER_A) as conn:
        await commitments_q.insert(
            conn, user_id=USER_A, title="weekly review", body=None,
            due_at=None, recurrence="FREQ=WEEKLY", lead_time=None,
            source="conversation", source_ref=None, notify_policy=None,
        )
        assert await materialize_user(conn, user_id=USER_A, now=T, tz=NY) == 1
        assert await materialize_user(conn, user_id=USER_A, now=T, tz=NY) == 0

    occs = await _rows(db, USER_A, "SELECT * FROM commitment_occurrences")
    assert len(occs) == 1
    assert occs[0]["fire_at"] > T


async def test_materialize_survives_a_bad_rrule(db: Database):
    async with db.with_tenant(USER_A) as conn:
        await commitments_q.insert(
            conn, user_id=USER_A, title="broken", body=None,
            due_at=None, recurrence="EVERY OTHER THURSDAYISH", lead_time=None,
            source="conversation", source_ref=None, notify_policy=None,
        )
        assert await materialize_user(conn, user_id=USER_A, now=T, tz=NY) == 0


# --- wakeup firing (§10) ----------------------------------------------------


async def test_past_due_wakeup_fires_late_on_catch_up(db: Database):
    wakeup_id = await _seed_wakeup(db, fire_at=T - timedelta(hours=3))
    sched = _scheduler(db)

    await sched.refresh(now=T)  # boot scan: no lower bound, so past-due loads
    assert await sched.fire_due(now=T) == 1

    triggers = await _rows(db, USER_A, "SELECT * FROM triggers")
    assert len(triggers) == 1
    assert triggers[0]["source"] == "wakeup"
    assert triggers[0]["priority"] == 3
    assert triggers[0]["payload"]["late"] is True
    assert triggers[0]["payload"]["text"] == "check in"
    wk = await _rows(db, USER_A, "SELECT * FROM scheduled_wakeups WHERE id=%s", (wakeup_id,))
    assert wk[0]["status"] == "fired"

    # Re-refresh + fire: the row is fired, nothing re-heaps, nothing duplicates.
    await sched.refresh(now=T)
    assert await sched.fire_due(now=T) == 0
    assert len(await _rows(db, USER_A, "SELECT 1 FROM triggers")) == 1


async def test_future_wakeup_waits_for_its_time(db: Database):
    await _seed_wakeup(db, fire_at=T + timedelta(hours=1))
    sched = _scheduler(db)

    await sched.refresh(now=T)
    assert await sched.fire_due(now=T) == 0
    assert await sched.fire_due(now=T + timedelta(hours=1, seconds=1)) == 1

    triggers = await _rows(db, USER_A, "SELECT * FROM triggers")
    assert triggers[0]["payload"]["late"] is False


async def test_recurring_wakeup_advances_instead_of_dying(db: Database):
    fire_at = T - timedelta(minutes=10)
    wakeup_id = await _seed_wakeup(db, fire_at=fire_at, recurrence="FREQ=DAILY")
    sched = _scheduler(db)

    await sched.refresh(now=T)
    assert await sched.fire_due(now=T) == 1

    wk = (await _rows(db, USER_A, "SELECT * FROM scheduled_wakeups WHERE id=%s", (wakeup_id,)))[0]
    assert wk["status"] == "active"
    assert wk["fire_at"] == fire_at + timedelta(days=1)
    assert len(await _rows(db, USER_A, "SELECT 1 FROM triggers")) == 1


async def test_cancelled_wakeup_loses_the_fire_race(db: Database):
    wakeup_id = await _seed_wakeup(db, fire_at=T - timedelta(minutes=1))
    sched = _scheduler(db)
    await sched.refresh(now=T)

    async with db.with_tenant(USER_A) as conn:
        await wakeups_q.cancel(conn, wakeup_id)

    assert await sched.fire_due(now=T) == 0
    assert await _rows(db, USER_A, "SELECT 1 FROM triggers") == []


# --- occurrence firing (§10) ------------------------------------------------


async def test_due_occurrence_becomes_eligible_with_priority_2_trigger(db: Database):
    commitment_id, occ_id = await _seed_occurrence(db, fire_at=T - timedelta(minutes=30))
    sched = _scheduler(db)

    await sched.refresh(now=T)
    assert await sched.fire_due(now=T) == 1

    occ = (await _rows(db, USER_A, "SELECT * FROM commitment_occurrences WHERE id=%s", (occ_id,)))[0]
    assert occ["status"] == "eligible"
    trigger = (await _rows(db, USER_A, "SELECT * FROM triggers"))[0]
    assert trigger["source"] == "occurrence"
    assert trigger["priority"] == 2
    assert trigger["payload"]["commitment_id"] == commitment_id
    assert trigger["payload"]["occurrence_id"] == occ_id
    assert "file taxes" in trigger["payload"]["text"]

    # A rebuilt heap can't double-fire: eligible loses the pending CAS.
    await sched.refresh(now=T)
    assert await sched.fire_due(now=T) == 0
    assert len(await _rows(db, USER_A, "SELECT 1 FROM triggers")) == 1


async def test_occurrence_turn_success_marks_surfaced(db: Database):
    _, occ_id = await _seed_occurrence(db, fire_at=T - timedelta(minutes=1))
    sched = _scheduler(db)
    await sched.refresh(now=T)
    await sched.fire_due(now=T)

    worker = Worker(db, USER_A, StubGateway(reply="on it"))
    assert await worker.tick() is True

    occ = (await _rows(db, USER_A, "SELECT * FROM commitment_occurrences WHERE id=%s", (occ_id,)))[0]
    assert occ["status"] == "surfaced"
    assert occ["surfaced_turn"] is not None


async def test_completing_a_commitment_retires_its_pending_occurrences(db: Database):
    commitment_id, occ_id = await _seed_occurrence(db, fire_at=T + timedelta(hours=5))
    async with db.with_tenant(USER_A) as conn:
        user = await users_q.get(conn, USER_A)
        [cmd] = parse_commands([{
            "type": "CompleteCommitment", "id": commitment_id,
            "expected_version": 1, "reason": "did it early",
        }])
        detail = await apply_command(conn, cmd, user=user, turn_id=1, index=0)

    assert detail["occurrences_skipped"] == 1
    occ = (await _rows(db, USER_A, "SELECT * FROM commitment_occurrences WHERE id=%s", (occ_id,)))[0]
    assert occ["status"] == "skipped_done"


# --- quiet hours (§10) ------------------------------------------------------


def test_quiet_deferral_math():
    inside = quiet_deferral(
        T_QUIET, tz=NY,
        quiet_start=datetime(2000, 1, 1, 3, 0).time(),
        quiet_end=datetime(2000, 1, 1, 11, 59).time(),
    )
    assert inside == T_QUIET_END
    outside = quiet_deferral(
        T, tz=NY,
        quiet_start=datetime(2000, 1, 1, 3, 0).time(),
        quiet_end=datetime(2000, 1, 1, 11, 59).time(),
    )
    assert outside is None


async def test_respect_quiet_wakeup_defers_to_quiet_end(db: Database):
    await _seed_wakeup(db, fire_at=T_QUIET - timedelta(minutes=5))
    sched = _scheduler(db)

    await sched.refresh(now=T_QUIET)
    assert await sched.fire_due(now=T_QUIET) == 0  # deferred, not dropped
    assert await _rows(db, USER_A, "SELECT 1 FROM triggers") == []

    assert await sched.fire_due(now=T_QUIET_END) == 1
    triggers = await _rows(db, USER_A, "SELECT * FROM triggers")
    assert triggers[0]["source"] == "wakeup"


async def test_urgent_wakeup_ignores_quiet_hours(db: Database):
    await _seed_wakeup(
        db, fire_at=T_QUIET - timedelta(minutes=5), respect_quiet=False
    )
    sched = _scheduler(db)

    await sched.refresh(now=T_QUIET)
    assert await sched.fire_due(now=T_QUIET) == 1


async def test_occurrence_defers_per_notify_policy(db: Database):
    await _seed_occurrence(db, fire_at=T_QUIET - timedelta(minutes=5))
    sched = _scheduler(db)

    await sched.refresh(now=T_QUIET)
    assert await sched.fire_due(now=T_QUIET) == 0  # default policy is "defer"
    assert await sched.fire_due(now=T_QUIET_END) == 1


# --- presence (dead-man check-ins) ------------------------------------------


def _set_presence(dsn: str, max_gap: str | None, min_gap: str = "45 minutes"):
    """FORCE RLS applies to the owner too: the tenant must be set to write."""
    with psycopg.connect(dsn) as conn:
        conn.execute("BEGIN")
        conn.execute("SELECT set_config('app.user_id', %s, true)", (USER_A,))
        cur = conn.execute(
            "UPDATE users SET presence_max_gap = %s, presence_min_gap = %s "
            "WHERE id = %s RETURNING id",
            (max_gap, min_gap, USER_A),
        )
        assert cur.fetchone() is not None, "RLS silently swallowed the update"
        conn.execute("COMMIT")


@pytest.fixture
def presence_user(dsn: str):
    """Opt USER_A in for one test; users survives TRUNCATE, so always reset."""
    _set_presence(dsn, "60 minutes")
    yield
    _set_presence(dsn, None)


async def _seed_finished_turn(db: Database, *, finished_at: datetime) -> None:
    async with db.with_tenant(USER_A) as conn:
        cur = await conn.execute(
            "INSERT INTO triggers (user_id, source, idempotency_key, payload, "
            "priority, status) VALUES (%s, 'chat', %s, '{}', 1, 'done') "
            "RETURNING id",
            (USER_A, f"seed:{finished_at.isoformat()}"),
        )
        trigger_id = int((await cur.fetchone())["id"])
        await conn.execute(
            "INSERT INTO turns (user_id, trigger_id, status, finished_at) "
            "VALUES (%s, %s, 'ok', %s)",
            (USER_A, trigger_id, finished_at),
        )


def _presence_triggers(rows):
    return [r for r in rows if r["source"] == "presence"]


async def test_presence_fires_max_gap_after_the_last_turn(
    db: Database, presence_user
):
    await _seed_finished_turn(db, finished_at=T - timedelta(hours=2))
    sched = _scheduler(db)

    await sched.refresh(now=T)  # due at last turn + 60 min = T - 1h: past due
    assert await sched.fire_due(now=T) == 1

    [trig] = _presence_triggers(await _rows(db, USER_A, "SELECT * FROM triggers"))
    assert trig["priority"] == 3
    assert trig["payload"]["late"] is True
    assert trig["idempotency_key"].startswith(f"presence:{USER_A}:")


async def test_presence_min_gap_suppresses_at_fire_time(
    db: Database, presence_user
):
    await _seed_finished_turn(db, finished_at=T - timedelta(hours=2))
    sched = _scheduler(db)
    await sched.refresh(now=T)

    # A turn finishes between refresh and fire: within the 45-min floor.
    await _seed_finished_turn(db, finished_at=T - timedelta(minutes=10))

    assert await sched.fire_due(now=T) == 0
    assert _presence_triggers(await _rows(db, USER_A, "SELECT * FROM triggers")) == []


async def test_presence_yields_to_a_queued_trigger(db: Database, presence_user):
    from inkling.turns import ingress

    await _seed_finished_turn(db, finished_at=T - timedelta(hours=2))
    sched = _scheduler(db)
    await sched.refresh(now=T)
    await ingress.submit(db, user_id=USER_A, source="chat", payload={"text": "hi"})

    assert await sched.fire_due(now=T) == 0
    assert _presence_triggers(await _rows(db, USER_A, "SELECT * FROM triggers")) == []


async def test_null_max_gap_means_presence_off(db: Database):
    await _seed_finished_turn(db, finished_at=T - timedelta(hours=6))
    sched = _scheduler(db)
    await sched.refresh(now=T)

    assert not any(e.kind == "presence" for e in sched._heap)


async def test_presence_duplicate_fire_inserts_one_trigger(
    db: Database, presence_user
):
    import heapq as _heapq

    from inkling.scheduler.scheduler import Entry

    await _seed_finished_turn(db, finished_at=T - timedelta(hours=2))
    sched = _scheduler(db)
    await sched.refresh(now=T)
    [entry] = [e for e in sched._heap if e.kind == "presence"]
    assert await sched.fire_due(now=T) == 1

    # A second scheduler (or rebuilt heap) proposes the same instant. Settle
    # the first trigger so the busy check isn't what saves us — this is the
    # idempotency key's own test.
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "UPDATE triggers SET status = 'done' WHERE source = 'presence'"
        )
    _heapq.heappush(
        sched._heap,
        Entry(entry.fire_at, "presence", USER_A, 0, dict(entry.extra)),
    )
    assert await sched.fire_due(now=T) == 0
    assert len(_presence_triggers(await _rows(db, USER_A, "SELECT * FROM triggers"))) == 1


async def test_presence_defers_out_of_quiet_hours(db: Database, presence_user):
    await _seed_finished_turn(db, finished_at=T_QUIET - timedelta(hours=2))
    sched = _scheduler(db)

    await sched.refresh(now=T_QUIET)
    assert await sched.fire_due(now=T_QUIET) == 0  # deferred, not dropped
    assert await _rows(db, USER_A, "SELECT 1 FROM triggers WHERE source='presence'") == []

    assert await sched.fire_due(now=T_QUIET_END) == 1
    [trig] = _presence_triggers(await _rows(db, USER_A, "SELECT * FROM triggers"))
    assert trig["source"] == "presence"


async def test_presence_anchors_at_now_for_a_user_with_no_history(
    db: Database, presence_user
):
    sched = _scheduler(db)
    await sched.refresh(now=T)

    [entry] = [e for e in sched._heap if e.kind == "presence"]
    assert entry.fire_at == T + timedelta(minutes=60)
    assert await sched.fire_due(now=T) == 0


# --- invalidation (§10) -----------------------------------------------------


async def test_schedule_changing_command_pokes_the_scheduler(db: Database):
    from inkling.turns import ingress

    poked = []
    gateway = StubGateway(
        reply="scheduled",
        commands=parse_commands([{
            "type": "ScheduleWakeup", "offset_minutes": 30, "purpose": "follow up",
        }]),
    )
    worker = Worker(
        db, USER_A, gateway, on_schedule_change=lambda: poked.append(True)
    )
    await ingress.submit(db, user_id=USER_A, source="chat", payload={"text": "remind me"})

    assert await worker.tick() is True
    assert poked == [True]


async def test_plain_chat_turn_does_not_poke_the_scheduler(db: Database):
    from inkling.turns import ingress

    poked = []
    worker = Worker(
        db, USER_A, StubGateway(reply="hi"),
        on_schedule_change=lambda: poked.append(True),
    )
    await ingress.submit(db, user_id=USER_A, source="chat", payload={"text": "hello"})

    assert await worker.tick() is True
    assert poked == []


async def test_wake_forces_early_refresh_pickup(db: Database):
    """A wakeup inserted after refresh is invisible until wake() re-scans."""
    sched = _scheduler(db)
    await sched.refresh(now=T)
    await _seed_wakeup(db, fire_at=T + timedelta(minutes=1))

    assert await sched.fire_due(now=T + timedelta(minutes=2)) == 0  # stale heap
    sched.wake()  # what the worker's on_schedule_change calls
    assert sched._next_refresh is None
    await sched.refresh(now=T + timedelta(minutes=2))
    assert await sched.fire_due(now=T + timedelta(minutes=2)) == 1
