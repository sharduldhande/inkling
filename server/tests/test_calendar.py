"""Calendar (§12): relevance gate, cursor sync, hints, create-via-outbox.

Runs as runtime_role against the Neon test branch; the provider is always
StubProvider — google.py is HTTP shape only and needs real credentials (§19.2).
"""

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from inkling.api.auth import Roster
from inkling.api.server import create_app
from inkling.calendar_.adapter import NormalizedEvent, StubProvider, SyncPage
from inkling.calendar_.channel import CalendarChannel
from inkling.calendar_.sync import CalendarSync, passes_gate, plan_blocks
from inkling.db.client import Database
from inkling.db.queries import users as users_q
from inkling.delivery.ntfy import StubChannel
from inkling.delivery.outbox import Drainer
from inkling.inference.gateway import StubGateway, TurnResult
from inkling.turns import ingress
from inkling.turns.commands import parse_commands
from inkling.turns.commit import commit_turn
from inkling.turns.validate import validate_commands
from inkling.turns.worker import Worker

from tests.conftest import USER_A

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 7, 17, 18, 0, tzinfo=UTC)  # 14:00 EDT


@pytest_asyncio.fixture(autouse=True)
async def clean(migrated: str, dsn: str):
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "TRUNCATE turn_events, turns, triggers, transcript, audit_log, "
            "events, docs, commitment_occurrences, commitments, "
            "scheduled_wakeups, deliveries, calendar_events_cache, "
            "calendar_sync_state RESTART IDENTITY CASCADE"
        )
        conn.execute("UPDATE users SET calendar_enabled = false")
        conn.commit()
    yield


async def _rows(db: Database, user_id: str, sql: str, args=()):
    async with db.with_tenant(user_id) as conn:
        cur = await conn.execute(sql, args)
        return await cur.fetchall()


def _enable_calendar(dsn: str, user_id: str = USER_A) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute("UPDATE users SET calendar_enabled = true WHERE id = %s", (user_id,))
        conn.commit()


async def _seed_state(
    db: Database, *, calendar_id: str = "primary", channel_id: str | None = None
) -> None:
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "INSERT INTO calendar_sync_state "
            "(user_id, provider, calendar_id, channel_id, oauth_token_file) "
            "VALUES (%s, 'google', %s, %s, '/dev/null') "
            "ON CONFLICT (user_id, provider, calendar_id) DO NOTHING",
            (USER_A, calendar_id, channel_id),
        )


def _evt(
    event_id: str,
    *,
    starts_in: timedelta,
    duration: timedelta = timedelta(hours=1),
    summary: str = "Dentist",
    revision: str = "r1",
    status: str = "confirmed",
) -> NormalizedEvent:
    start = NOW + starts_in
    return NormalizedEvent(
        provider_event_id=event_id,
        calendar_id="primary",
        revision=revision,
        summary=summary,
        starts_at=start,
        ends_at=start + duration,
        status=status,
        raw={"id": event_id},
    )


def _sync(db: Database, provider: StubProvider, **kwargs) -> CalendarSync:
    return CalendarSync(db, [USER_A], provider, **kwargs)


# --- the deterministic relevance gate (§12) ---------------------------------


def test_plan_blocks_parses_time_ranges_and_ignores_prose():
    blocks = plan_blocks(
        "Morning: 09:00-10:30 deep work\nLunch at noon\n14:00 – 15:00 gym\n25:99-26:00 nonsense",
        day=NOW, tz=NY,
    )
    assert [(b[0].hour, b[0].minute, b[1].hour) for b in blocks] == [
        (9, 0, 10), (14, 0, 15)
    ]


def test_gate_new_same_day_event_passes():
    assert passes_gate(
        _evt("e1", starts_in=timedelta(hours=3)), cached=None, now=NOW,
        tz=NY, plan_text="", commitment_windows=[],
    )


def test_gate_new_event_in_two_days_without_overlap_fails():
    assert not passes_gate(
        _evt("e1", starts_in=timedelta(hours=50)), cached=None, now=NOW,
        tz=NY, plan_text="", commitment_windows=[],
    )


def test_gate_beyond_72h_fails_even_with_overlap():
    event = _evt("e1", starts_in=timedelta(hours=100))
    window = (event.starts_at, event.ends_at)
    assert not passes_gate(
        event, cached=None, now=NOW, tz=NY, plan_text="",
        commitment_windows=[window],
    )


def test_gate_plan_block_overlap_passes_on_a_later_day():
    # 10:00-11:00 EDT tomorrow; plan block 09:30-10:30.
    event = _evt("e1", starts_in=timedelta(hours=24))  # 14:00 EDT tomorrow
    assert not passes_gate(
        event, cached=None, now=NOW, tz=NY,
        plan_text="09:30-10:30 review", commitment_windows=[],
    )
    assert passes_gate(
        event, cached=None, now=NOW, tz=NY,
        plan_text="13:30-14:30 review", commitment_windows=[],
    )


def test_gate_commitment_window_overlap_passes():
    event = _evt("e1", starts_in=timedelta(hours=30))
    window = (event.starts_at - timedelta(minutes=30), event.starts_at + timedelta(minutes=30))
    assert passes_gate(
        event, cached=None, now=NOW, tz=NY, plan_text="",
        commitment_windows=[window],
    )


def test_gate_immaterial_revision_bump_fails():
    event = _evt("e1", starts_in=timedelta(hours=2), revision="r2")
    cached = {
        "created_by_us": False, "revision": "r1",
        "starts_at": event.starts_at, "ends_at": event.ends_at,
        "summary": event.summary,
    }
    assert not passes_gate(
        event, cached=cached, now=NOW, tz=NY, plan_text="", commitment_windows=[],
    )


def test_gate_skips_events_we_created():
    event = _evt("e1", starts_in=timedelta(hours=2))
    cached = {
        "created_by_us": True, "revision": "r0",
        "starts_at": event.starts_at - timedelta(hours=1),
        "ends_at": event.ends_at, "summary": "moved",
    }
    assert not passes_gate(
        event, cached=cached, now=NOW, tz=NY, plan_text="", commitment_windows=[],
    )


# --- cursor sync (§12) ------------------------------------------------------


async def test_initial_sync_caches_events_and_gates_deltas(db: Database):
    await _seed_state(db)
    provider = StubProvider()
    provider.pages = [SyncPage(
        events=[
            _evt("today", starts_in=timedelta(hours=2)),      # passes: new same-day
            _evt("nextweek", starts_in=timedelta(days=40)),   # fails: beyond 72h
        ],
        next_cursor="cursor-1",
    )]

    passed = await _sync(db, provider).sync_calendar(USER_A, "primary", now=NOW)

    assert passed == 1
    cache = await _rows(db, USER_A, "SELECT * FROM calendar_events_cache ORDER BY provider_event_id")
    assert len(cache) == 2  # everything cached, relevant or not
    triggers = await _rows(db, USER_A, "SELECT * FROM triggers")
    assert len(triggers) == 1
    assert triggers[0]["source"] == "calendar"
    assert triggers[0]["priority"] == 3
    assert triggers[0]["payload"]["kind"] == "delta"
    assert "Dentist" in triggers[0]["payload"]["text"]
    state = (await _rows(db, USER_A, "SELECT * FROM calendar_sync_state"))[0]
    assert state["sync_cursor"] == "cursor-1"


async def test_resync_of_same_revision_is_idempotent(db: Database):
    await _seed_state(db)
    provider = StubProvider()
    event = _evt("today", starts_in=timedelta(hours=2))
    provider.pages = [
        SyncPage([event], "cursor-1"),
        SyncPage([event], "cursor-2"),  # same revision arrives again
    ]
    sync = _sync(db, provider)

    await sync.sync_calendar(USER_A, "primary", now=NOW)
    passed = await sync.sync_calendar(USER_A, "primary", now=NOW)

    assert passed == 0  # not material against the cache; key would block anyway
    assert len(await _rows(db, USER_A, "SELECT 1 FROM triggers")) == 1


async def test_cursor_gone_runs_bounded_full_resync(db: Database):
    await _seed_state(db)
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "UPDATE calendar_sync_state SET sync_cursor = 'stale' WHERE user_id = %s",
            (USER_A,),
        )
    provider = StubProvider()
    provider.raise_cursor_gone = True
    provider.pages = [SyncPage([], "cursor-fresh")]

    await _sync(db, provider).sync_calendar(USER_A, "primary", now=NOW)

    full = [c for c in provider.calls if c["op"] == "initial_sync"]
    assert len(full) == 1
    assert full[0]["window_start"] == NOW - timedelta(days=60)
    assert full[0]["window_end"] == NOW + timedelta(days=60)
    state = (await _rows(db, USER_A, "SELECT * FROM calendar_sync_state"))[0]
    assert state["sync_cursor"] == "cursor-fresh"
    assert state["last_full_resync_at"] is not None


# --- webhook hints (§12) ----------------------------------------------------


async def test_worker_never_claims_hint_triggers(db: Database):
    await ingress.submit(
        db, user_id=USER_A, source="calendar",
        source_ref="primary", payload={"kind": "hint"},
    )
    worker = Worker(db, USER_A, StubGateway())
    assert await worker.claim() is None  # hint invisible to the turn path

    await ingress.submit(db, user_id=USER_A, source="chat", payload={"text": "hi"})
    claimed = await worker.claim()
    assert claimed is not None
    assert claimed[0]["source"] == "chat"  # hint doesn't block real work


async def test_process_hints_claims_syncs_and_settles(db: Database):
    await _seed_state(db)
    await ingress.submit(
        db, user_id=USER_A, source="calendar",
        source_ref="primary", payload={"kind": "hint"},
    )
    provider = StubProvider()

    assert await _sync(db, provider).process_hints() == 1

    assert any(c["op"] == "initial_sync" for c in provider.calls)  # no cursor yet
    hint = (await _rows(db, USER_A, "SELECT * FROM triggers"))[0]
    assert hint["status"] == "done"


async def test_webhook_validates_channel_and_coalesces(db: Database, dsn: str):
    await _seed_state(db, channel_id="chan-123")
    app = create_app(db, Roster({USER_A: "tok"}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        for _ in range(2):
            response = await client.post(
                "/calendar/webhook/google",
                headers={"X-Goog-Channel-ID": "chan-123",
                         "X-Goog-Resource-State": "exists"},
            )
            assert response.status_code == 200
        unknown = await client.post(
            "/calendar/webhook/google",
            headers={"X-Goog-Channel-ID": "who-dis",
                     "X-Goog-Resource-State": "exists"},
        )
        assert unknown.status_code == 200  # uniform response, nothing enqueued
        handshake = await client.post(
            "/calendar/webhook/google",
            headers={"X-Goog-Channel-ID": "chan-123",
                     "X-Goog-Resource-State": "sync"},
        )
        assert handshake.status_code == 200

    triggers = await _rows(db, USER_A, "SELECT * FROM triggers ORDER BY id")
    assert len(triggers) == 2  # two real pings; second coalesced, none for others
    assert triggers[0]["status"] == "pending"
    assert triggers[1]["status"] == "coalesced"


async def test_channel_renewal_updates_expiring_channels(db: Database):
    await _seed_state(db, channel_id="chan-old")
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "UPDATE calendar_sync_state SET channel_expires_at = %s",
            (NOW + timedelta(hours=2),),  # inside the renew-before window
        )
    provider = StubProvider()

    renewed = await _sync(db, provider, webhook_url="https://x/hook").renew_channels(now=NOW)

    assert renewed == 1
    state = (await _rows(db, USER_A, "SELECT * FROM calendar_sync_state"))[0]
    assert state["channel_id"].startswith("chan-")
    assert state["channel_id"] != "chan-old"
    assert state["channel_expires_at"] > NOW + timedelta(days=1)


# --- CreateCalendarEvent via the outbox (§12 write path) --------------------


async def test_create_event_requires_enabled_and_owned_calendar(db: Database, dsn: str):
    raw = [{
        "type": "CreateCalendarEvent", "calendar_id": "primary",
        "summary": "Focus", "starts_at": (NOW + timedelta(hours=2)).isoformat(),
        "ends_at": (NOW + timedelta(hours=3)).isoformat(),
    }]
    async with db.with_tenant(USER_A) as conn:
        user = await users_q.get(conn, USER_A)
        verdict = await validate_commands(conn, user=user, commands=parse_commands(raw))
    assert verdict.rejected[0].reason == "calendar is not enabled for this user"

    _enable_calendar(dsn)
    async with db.with_tenant(USER_A) as conn:
        user = await users_q.get(conn, USER_A)
        verdict = await validate_commands(conn, user=user, commands=parse_commands(raw))
    assert "not one of your configured calendars" in verdict.rejected[0].reason

    await _seed_state(db)
    async with db.with_tenant(USER_A) as conn:
        user = await users_q.get(conn, USER_A)
        verdict = await validate_commands(conn, user=user, commands=parse_commands(raw))
    assert verdict.accepted and not verdict.rejected


async def test_create_event_flows_through_outbox_to_provider(db: Database, dsn: str):
    _enable_calendar(dsn)
    await _seed_state(db)
    sub = await ingress.submit(db, user_id=USER_A, source="chat", payload={"text": "book it"})
    async with db.with_tenant(USER_A) as conn:
        cur = await conn.execute("SELECT * FROM triggers WHERE id = %s", (sub.trigger_id,))
        trigger = await cur.fetchone()
        cur = await conn.execute(
            "INSERT INTO turns (user_id, trigger_id, status) "
            "VALUES (%s, %s, 'running') RETURNING id",
            (USER_A, sub.trigger_id),
        )
        turn_id = int((await cur.fetchone())["id"])
    await commit_turn(
        db, user_id=USER_A, trigger=trigger, turn_id=turn_id,
        result=TurnResult(
            text="booked",
            commands=parse_commands([{
                "type": "CreateCalendarEvent", "calendar_id": "primary",
                "summary": "Focus block",
                "starts_at": (NOW + timedelta(hours=2)).isoformat(),
                "ends_at": (NOW + timedelta(hours=3)).isoformat(),
            }]),
            model="stub", input_tokens=0, output_tokens=0,
        ),
        next_seq=1, actor="test",
    )

    row = (await _rows(db, USER_A, "SELECT * FROM deliveries"))[0]
    assert row["channel"] == "calendar"
    assert row["destination"] == "primary"

    provider = StubProvider()
    drainer = Drainer(
        db, [USER_A], channel=StubChannel(),
        extra_channels={"calendar": CalendarChannel(db, provider)},
    )
    assert await drainer.drain() == 1

    assert provider.created[0]["summary"] == "Focus block"
    cache = (await _rows(db, USER_A, "SELECT * FROM calendar_events_cache"))[0]
    assert cache["created_by_us"] is True
    sent = (await _rows(db, USER_A, "SELECT * FROM deliveries"))[0]
    assert sent["status"] == "sent"
    assert sent["provider_msg_id"] == "created-1"


async def test_unconfigured_calendar_channel_degrades_honestly(db: Database, dsn: str):
    """A calendar delivery with no channel dead-letters instead of vanishing."""
    _enable_calendar(dsn)
    await _seed_state(db)
    async with db.with_tenant(USER_A) as conn:
        from inkling.db.queries import deliveries as deliveries_q

        await deliveries_q.enqueue(
            conn, user_id=USER_A, channel="calendar", destination="primary",
            body="{}", idempotency_key="cal-test-1",
        )
    drainer = Drainer(db, [USER_A], channel=StubChannel())  # no calendar channel

    assert await drainer.drain() == 0
    row = (await _rows(db, USER_A, "SELECT * FROM deliveries"))[0]
    assert row["status"] == "retry_wait"
    assert "no 'calendar' channel configured" in row["last_error"]