"""Delivery drainer (§11): claim CAS, backoff, dead-letter, expiry, ack.

Runs as runtime_role against the Neon test branch. drain() takes an explicit
`now`, so retry/backoff tests drive time instead of sleeping.
"""

from datetime import datetime, timedelta, timezone

import psycopg
import pytest_asyncio

from inkling.db.client import Database
from inkling.db.queries import commitments as commitments_q
from inkling.db.queries import deliveries as deliveries_q
from inkling.delivery.ntfy import StubChannel
from inkling.delivery.outbox import BACKOFF, Drainer
from inkling.inference.gateway import StubGateway
from inkling.turns import ingress
from inkling.turns.commands import parse_commands
from inkling.turns.worker import Worker

from tests.conftest import USER_A, USER_B

T = datetime(2026, 7, 17, 18, 0, tzinfo=timezone.utc)


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


async def _enqueue(db: Database, user_id: str = USER_A, **kwargs) -> int:
    defaults = {
        "destination": f"inkling-{user_id}-test",
        "body": "reminder: file taxes",
        "idempotency_key": f"test:{datetime.now().timestamp()}",
        # Dueness must not depend on the wall clock (created_at is now()):
        # pin not_before to the tests' fixed T unless a test says otherwise.
        "not_before": T,
    }
    defaults.update(kwargs)
    async with db.with_tenant(user_id) as conn:
        row = await deliveries_q.enqueue(conn, user_id=user_id, **defaults)
        assert row is not None
        return int(row["id"])


def _drainer(db: Database, channel: StubChannel, **kwargs) -> Drainer:
    return Drainer(db, [USER_A], channel=channel, **kwargs)


# --- the happy path ---------------------------------------------------------


async def test_due_delivery_sends_and_records_provider_id(db: Database):
    delivery_id = await _enqueue(db)
    channel = StubChannel()

    assert await _drainer(db, channel).drain(now=T) == 1

    row = (await _rows(db, USER_A, "SELECT * FROM deliveries WHERE id=%s", (delivery_id,)))[0]
    assert row["status"] == "sent"
    assert row["provider_msg_id"] == "stub-1"
    assert row["sent_at"] is not None
    assert channel.sent == [{
        "topic": f"inkling-{USER_A}-test",
        "body": "reminder: file taxes",
        "title": "Inkling",
    }]


async def test_sent_delivery_is_not_resent(db: Database):
    await _enqueue(db)
    channel = StubChannel()
    drainer = _drainer(db, channel)

    assert await drainer.drain(now=T) == 1
    assert await drainer.drain(now=T) == 0
    assert len(channel.sent) == 1


async def test_not_before_defers_the_attempt(db: Database):
    await _enqueue(db, not_before=T + timedelta(hours=2))
    channel = StubChannel()
    drainer = _drainer(db, channel)

    assert await drainer.drain(now=T) == 0
    assert await drainer.drain(now=T + timedelta(hours=2)) == 1


async def test_drain_is_tenant_scoped_to_the_roster(db: Database):
    await _enqueue(db, user_id=USER_B)  # not on this drainer's roster
    channel = StubChannel()

    assert await _drainer(db, channel).drain(now=T) == 0
    assert channel.sent == []


# --- failure, backoff, dead-letter (§11) ------------------------------------


async def test_failure_backs_off_1m_then_retries(db: Database):
    delivery_id = await _enqueue(db)
    channel = StubChannel(fail_times=1)
    drainer = _drainer(db, channel)

    assert await drainer.drain(now=T) == 0

    row = (await _rows(db, USER_A, "SELECT * FROM deliveries WHERE id=%s", (delivery_id,)))[0]
    assert row["status"] == "retry_wait"
    assert row["attempt_count"] == 1
    assert "scripted failure" in row["last_error"]
    assert row["not_before"] == T + timedelta(minutes=1)

    # Not due again until the backoff passes; then the retry succeeds.
    assert await drainer.drain(now=T + timedelta(seconds=30)) == 0
    assert await drainer.drain(now=T + timedelta(minutes=1)) == 1


async def test_exhausted_backoff_dead_letters_and_alerts_operator(db: Database):
    delivery_id = await _enqueue(db)
    channel = StubChannel(fail_times=99)
    drainer = _drainer(db, channel, operator_topic="inkling-operator-test")

    now = T
    for step in BACKOFF:  # 4 failures land in retry_wait
        assert await drainer.drain(now=now) == 0
        now += step
    assert await drainer.drain(now=now) == 0  # 5th failure exhausts

    row = (await _rows(db, USER_A, "SELECT * FROM deliveries WHERE id=%s", (delivery_id,)))[0]
    assert row["status"] == "dead_letter"
    assert row["attempt_count"] == len(BACKOFF) + 1

    alerts = await _rows(
        db, USER_A,
        "SELECT * FROM deliveries WHERE destination=%s", ("inkling-operator-test",),
    )
    assert len(alerts) == 1
    assert f"delivery {delivery_id}" in alerts[0]["body"]
    assert alerts[0]["status"] == "queued"

    # The operator alert then goes out through the same outbox.
    channel.fail_times = 0
    assert await drainer.drain(now=now) == 1
    assert channel.sent[-1]["topic"] == "inkling-operator-test"


async def test_dead_operator_alert_does_not_alert_about_itself(db: Database):
    await _enqueue(db, destination="inkling-operator-test")
    channel = StubChannel(fail_times=99)
    drainer = _drainer(db, channel, operator_topic="inkling-operator-test")

    now = T
    for _ in range(len(BACKOFF) + 1):
        await drainer.drain(now=now)
        now += timedelta(hours=2)

    rows = await _rows(db, USER_A, "SELECT * FROM deliveries")
    assert len(rows) == 1  # dead-lettered, and no alert row was spawned
    assert rows[0]["status"] == "dead_letter"


async def test_no_operator_topic_means_no_alert_row(db: Database):
    await _enqueue(db)
    channel = StubChannel(fail_times=99)
    drainer = _drainer(db, channel)  # operator_topic unset

    now = T
    for _ in range(len(BACKOFF) + 1):
        await drainer.drain(now=now)
        now += timedelta(hours=2)

    rows = await _rows(db, USER_A, "SELECT * FROM deliveries")
    assert len(rows) == 1
    assert rows[0]["status"] == "dead_letter"


# --- expiry and boot recovery (§11) -----------------------------------------


async def test_expired_delivery_dead_letters_without_an_attempt(db: Database):
    delivery_id = await _enqueue(db, expires_at=T - timedelta(minutes=1))
    channel = StubChannel()

    assert await _drainer(db, channel).drain(now=T) == 0

    row = (await _rows(db, USER_A, "SELECT * FROM deliveries WHERE id=%s", (delivery_id,)))[0]
    assert row["status"] == "dead_letter"
    assert row["last_error"] == "expired"
    assert channel.sent == []


async def test_boot_requeues_orphaned_leased_rows(db: Database):
    delivery_id = await _enqueue(db)
    async with db.with_tenant(USER_A) as conn:
        assert await deliveries_q.claim(conn, delivery_id) is not None
    # A crash here would strand the row 'leased' forever without recovery.

    channel = StubChannel()
    drainer = _drainer(db, channel)
    assert await drainer.drain(now=T) == 0  # leased rows are not due

    assert await drainer.recover_orphans() == 1
    assert await drainer.drain(now=T) == 1  # at-least-once: requeued and sent


# --- acknowledgement heuristic (§11) ----------------------------------------


async def _surfaced_occurrence(db: Database) -> int:
    async with db.with_tenant(USER_A) as conn:
        commitment = await commitments_q.insert(
            conn, user_id=USER_A, title="file taxes", body=None,
            due_at=T, recurrence=None, lead_time=None,
            source="conversation", source_ref=None, notify_policy=None,
        )
        occ_id = await commitments_q.insert_occurrence(
            conn, user_id=USER_A, commitment_id=int(commitment["id"]), fire_at=T
        )
        await commitments_q.mark_occurrence_eligible(conn, occ_id)
        await commitments_q.mark_occurrence_surfaced(conn, occ_id, turn_id=None)
        return int(occ_id)


async def test_chat_turn_acknowledges_surfaced_occurrences(db: Database):
    occ_id = await _surfaced_occurrence(db)
    await ingress.submit(db, user_id=USER_A, source="chat", payload={"text": "on it"})

    assert await Worker(db, USER_A, StubGateway(reply="noted")).tick() is True

    occ = (await _rows(db, USER_A, "SELECT * FROM commitment_occurrences WHERE id=%s", (occ_id,)))[0]
    assert occ["acknowledged_at"] is not None


async def test_non_chat_turn_does_not_acknowledge(db: Database):
    occ_id = await _surfaced_occurrence(db)
    await ingress.submit(
        db, user_id=USER_A, source="wakeup", payload={"text": "check in"}
    )

    assert await Worker(db, USER_A, StubGateway(reply="checking")).tick() is True

    occ = (await _rows(db, USER_A, "SELECT * FROM commitment_occurrences WHERE id=%s", (occ_id,)))[0]
    assert occ["acknowledged_at"] is None


# --- end-to-end: Notify command → outbox → push -----------------------------


async def test_notify_command_flows_from_turn_to_push(db: Database):
    channel = StubChannel()
    drainer = _drainer(db, channel)
    woken = []
    gateway = StubGateway(
        reply="I'll ping you",
        commands=parse_commands([{"type": "Notify", "body": "drink water"}]),
    )
    worker = Worker(db, USER_A, gateway, on_delivery_queued=lambda: woken.append(True))
    await ingress.submit(db, user_id=USER_A, source="chat", payload={"text": "nudge me"})

    assert await worker.tick() is True
    assert woken == [True]  # the worker poked the drainer

    assert await drainer.drain() == 1
    assert channel.sent[0]["body"] == "drink water"
    assert channel.sent[0]["topic"] == f"inkling-{USER_A}-test"
