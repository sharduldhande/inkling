"""Calendar (§12): relevance gate, feed registration, snapshot sync, read-only.

Runs as runtime_role against the Neon test branch; the provider is always
StubProvider (the ICS parser has its own pure tests in test_ics.py).
"""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
import pytest_asyncio

from inkling.calendar_ import adapter as adapter_mod
from inkling.calendar_ import ics as ics_mod
from inkling.calendar_.adapter import NormalizedEvent, Snapshot, StubProvider
from inkling.calendar_.config import FeedSpec
from inkling.calendar_.sync import CalendarSync, passes_gate, plan_blocks
from inkling.db.client import Database
from inkling.turns.commands import commands_json_schema

from tests.conftest import USER_A

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 7, 17, 18, 0, tzinfo=UTC)  # 14:00 EDT

FEEDS = [FeedSpec(id="primary", name="Personal", url="https://example.test/a.ics")]


@pytest_asyncio.fixture(autouse=True)
async def clean(migrated: str, dsn: str):
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "TRUNCATE turn_events, turns, triggers, transcript, audit_log, "
            "events, docs, commitment_occurrences, commitments, "
            "scheduled_wakeups, deliveries, calendar_events_cache, "
            "calendar_sync_state RESTART IDENTITY CASCADE"
        )
        conn.commit()
    yield


async def _rows(db: Database, user_id: str, sql: str, args=()):
    async with db.with_tenant(user_id) as conn:
        cur = await conn.execute(sql, args)
        return await cur.fetchall()


def _evt(
    event_id: str,
    *,
    starts_in: timedelta,
    duration: timedelta = timedelta(hours=1),
    summary: str = "Dentist",
    revision: str = "r1",
    status: str = "confirmed",
    all_day: bool = False,
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
        all_day=all_day,
        raw={"uid": event_id},
    )


def _cached(event: NormalizedEvent, **overrides):
    row = {
        "revision": event.revision, "starts_at": event.starts_at,
        "ends_at": event.ends_at, "summary": event.summary,
        "status": "confirmed", "all_day": event.all_day,
    }
    row.update(overrides)
    return row


def _sync(db: Database, provider: StubProvider, **kwargs) -> CalendarSync:
    return CalendarSync(db, provider, {USER_A: FEEDS}, **kwargs)


async def _registered(db: Database, provider: StubProvider | None = None) -> CalendarSync:
    sync = _sync(db, provider or StubProvider())
    await sync.register_feeds()
    return sync


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
    assert not passes_gate(
        event, cached=_cached(event, revision="r1"), now=NOW, tz=NY,
        plan_text="", commitment_windows=[],
    )


def test_gate_cancellation_of_a_known_event_passes():
    event = _evt("e1", starts_in=timedelta(hours=2), status="cancelled")
    assert passes_gate(
        event, cached=_cached(event), now=NOW, tz=NY,
        plan_text="", commitment_windows=[],
    )
    # ...but a cancellation we never knew about isn't news.
    assert not passes_gate(
        event, cached=None, now=NOW, tz=NY, plan_text="", commitment_windows=[],
    )


# --- feed registration -------------------------------------------------------


async def test_register_feeds_mirrors_config_and_disables_missing(db: Database):
    provider = StubProvider()
    two = CalendarSync(db, provider, {USER_A: FEEDS + [
        FeedSpec(id="work", name="Work", url="https://example.test/w.ics")
    ]})
    await two.register_feeds()
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "UPDATE calendar_sync_state SET sync_cursor = 'etag:v1' "
            "WHERE calendar_id = 'primary'"
        )
    rows = await _rows(db, USER_A, "SELECT * FROM calendar_sync_state ORDER BY calendar_id")
    assert [(r["calendar_id"], r["summary"], r["enabled"]) for r in rows] == [
        ("primary", "Personal", True), ("work", "Work", True),
    ]
    assert all(r["provider"] == "ics" for r in rows)

    # Drop 'work' from the file: disabled, not deleted; the other cursor survives.
    await _registered(db, provider)
    rows = await _rows(db, USER_A, "SELECT * FROM calendar_sync_state ORDER BY calendar_id")
    assert [(r["calendar_id"], r["enabled"], r["sync_cursor"]) for r in rows] == [
        ("primary", True, "etag:v1"), ("work", False, None),
    ]


# --- snapshot sync (§12) -----------------------------------------------------


async def test_snapshot_caches_events_and_gates_deltas(db: Database):
    provider = StubProvider()
    provider.snapshots = [Snapshot(
        events=[
            _evt("today", starts_in=timedelta(hours=2)),      # passes: new same-day
            _evt("nextweek", starts_in=timedelta(days=40)),   # fails: beyond 72h
            _evt("holiday", starts_in=timedelta(days=3), all_day=True,
                 duration=timedelta(days=1), summary="Labor Day"),
        ],
        cursor="etag:1",
    )]
    sync = await _registered(db, provider)

    passed = await sync.sync_calendar(USER_A, "primary", now=NOW)

    assert passed == 1
    cache = await _rows(db, USER_A, "SELECT * FROM calendar_events_cache ORDER BY provider_event_id")
    assert [r["provider_event_id"] for r in cache] == ["holiday", "nextweek", "today"]
    assert cache[0]["all_day"] is True and cache[2]["all_day"] is False
    assert all(r["status"] == "confirmed" for r in cache)
    triggers = await _rows(db, USER_A, "SELECT * FROM triggers")
    assert len(triggers) == 1
    assert triggers[0]["source"] == "calendar"
    assert triggers[0]["priority"] == 3
    assert triggers[0]["payload"]["kind"] == "delta"
    assert "new event 'Dentist'" in triggers[0]["payload"]["text"]
    assert triggers[0]["payload"]["event"]["all_day"] is False
    state = (await _rows(db, USER_A, "SELECT * FROM calendar_sync_state"))[0]
    assert state["sync_cursor"] == "etag:1"
    assert state["last_synced_at"] is not None
    assert provider.calls[0]["cursor"] is None
    assert provider.calls[0]["window_start"] == NOW - timedelta(days=7)


async def test_unchanged_snapshot_touches_last_synced_only(db: Database):
    provider = StubProvider()
    provider.snapshots = [
        Snapshot(events=[_evt("today", starts_in=timedelta(hours=2))], cursor="etag:1"),
        Snapshot(events=[], cursor="etag:1", unchanged=True),
    ]
    sync = await _registered(db, provider)
    await sync.sync_calendar(USER_A, "primary", now=NOW)
    before = (await _rows(db, USER_A, "SELECT * FROM calendar_sync_state"))[0]

    passed = await sync.sync_calendar(USER_A, "primary", now=NOW + timedelta(minutes=5))

    assert passed == 0
    assert provider.calls[1]["cursor"] == "etag:1", "the ETag went back out"
    after = (await _rows(db, USER_A, "SELECT * FROM calendar_sync_state"))[0]
    assert after["sync_cursor"] == "etag:1"
    assert after["last_synced_at"] >= before["last_synced_at"]
    cache = await _rows(db, USER_A, "SELECT * FROM calendar_events_cache")
    assert len(cache) == 1 and cache[0]["status"] == "confirmed"
    assert len(await _rows(db, USER_A, "SELECT * FROM triggers")) == 1


async def test_resync_of_same_revision_is_idempotent(db: Database):
    provider = StubProvider()
    event = _evt("today", starts_in=timedelta(hours=2))
    provider.snapshots = [
        Snapshot(events=[event], cursor="etag:1"),
        Snapshot(events=[event], cursor="etag:2"),  # feed changed elsewhere
    ]
    sync = await _registered(db, provider)
    assert await sync.sync_calendar(USER_A, "primary", now=NOW) == 1
    assert await sync.sync_calendar(USER_A, "primary", now=NOW) == 0
    assert len(await _rows(db, USER_A, "SELECT * FROM triggers")) == 1


async def test_moved_event_fires_once_per_revision(db: Database):
    provider = StubProvider()
    provider.snapshots = [
        Snapshot(events=[_evt("today", starts_in=timedelta(hours=2))], cursor="etag:1"),
        Snapshot(events=[_evt("today", starts_in=timedelta(hours=4), revision="r2")], cursor="etag:2"),
    ]
    sync = await _registered(db, provider)
    await sync.sync_calendar(USER_A, "primary", now=NOW)
    assert await sync.sync_calendar(USER_A, "primary", now=NOW) == 1
    triggers = await _rows(db, USER_A, "SELECT * FROM triggers ORDER BY id")
    assert "moved to" in triggers[1]["payload"]["text"]
    cache = (await _rows(db, USER_A, "SELECT * FROM calendar_events_cache"))[0]
    assert cache["starts_at"] == NOW + timedelta(hours=4)


async def test_removed_event_is_tombstoned_and_gated_as_cancellation(db: Database):
    provider = StubProvider()
    provider.snapshots = [
        Snapshot(events=[
            _evt("today", starts_in=timedelta(hours=2)),
            _evt("later", starts_in=timedelta(days=20)),
        ], cursor="etag:1"),
        Snapshot(events=[], cursor="etag:2"),  # both vanished from the feed
    ]
    sync = await _registered(db, provider)
    await sync.sync_calendar(USER_A, "primary", now=NOW)

    passed = await sync.sync_calendar(USER_A, "primary", now=NOW)

    assert passed == 1, "today's cancellation is news; the far one isn't"
    cache = await _rows(db, USER_A, "SELECT * FROM calendar_events_cache ORDER BY provider_event_id")
    assert [(r["provider_event_id"], r["status"]) for r in cache] == [
        ("later", "cancelled"), ("today", "cancelled"),
    ]
    triggers = await _rows(db, USER_A, "SELECT * FROM triggers ORDER BY id")
    assert "'Dentist'" in triggers[1]["payload"]["text"]
    assert "was cancelled" in triggers[1]["payload"]["text"]
    assert triggers[1]["payload"]["event"]["status"] == "cancelled"

    # A tombstoned event that reappears is new again.
    provider.snapshots = [Snapshot(
        events=[_evt("today", starts_in=timedelta(hours=2))], cursor="etag:3",
    )]
    assert await sync.sync_calendar(USER_A, "primary", now=NOW) == 1
    row = (await _rows(db, USER_A,
        "SELECT * FROM calendar_events_cache WHERE provider_event_id = 'today'"))[0]
    assert row["status"] == "confirmed"


async def test_excluded_events_are_never_cached_and_retire_silently(db: Database):
    provider = StubProvider()
    provider.snapshots = [
        Snapshot(events=[
            _evt("coffee", starts_in=timedelta(hours=2),
                 summary="Coffee Hour [OMSCS Student Center]"),
        ], cursor="etag:1"),
    ]
    sync = await _registered(db, provider)
    await sync.sync_calendar(USER_A, "primary", now=NOW)  # cached pre-exclude

    feeds = [FeedSpec(id="primary", name="Personal", url="https://example.test/a.ics",
                      exclude=("[omscs student center]",))]
    sync = CalendarSync(db, provider, {USER_A: feeds})
    await sync.register_feeds()
    state = (await _rows(db, USER_A, "SELECT * FROM calendar_sync_state"))[0]
    assert state["sync_cursor"] is None, "excluding feeds get a full fetch"

    provider.snapshots = [Snapshot(events=[
        _evt("coffee", starts_in=timedelta(hours=2),
             summary="Coffee Hour [OMSCS Student Center]"),
        _evt("new", starts_in=timedelta(hours=3),
             summary="Office Hours [OMSCS Student Center]"),
    ], cursor="etag:2")]
    triggers_before = len(await _rows(db, USER_A, "SELECT id FROM triggers"))
    assert await sync.sync_calendar(USER_A, "primary", now=NOW) == 0
    assert len(await _rows(db, USER_A, "SELECT id FROM triggers")) == triggers_before
    cache = await _rows(db, USER_A, "SELECT provider_event_id, status FROM calendar_events_cache")
    assert [(r["provider_event_id"], r["status"]) for r in cache] == [("coffee", "cancelled")]


async def test_sync_all_covers_enabled_feeds_and_survives_a_bad_one(db: Database):
    class Flaky(StubProvider):
        async def snapshot(self, *, calendar_id, **kw):
            if calendar_id == "broken":
                raise RuntimeError("503 from the feed host")
            return await super().snapshot(calendar_id=calendar_id, **kw)

    provider = Flaky()
    provider.snapshots = [Snapshot(
        events=[_evt("today", starts_in=timedelta(hours=2))], cursor="etag:1",
    )]
    sync = CalendarSync(db, provider, {USER_A: FEEDS + [
        FeedSpec(id="broken", name="Broken", url="https://example.test/x.ics"),
    ]})
    await sync.register_feeds()

    assert await sync.sync_all(now=NOW) == 1
    states = await _rows(db, USER_A, "SELECT * FROM calendar_sync_state ORDER BY calendar_id")
    assert states[0]["calendar_id"] == "broken" and states[0]["last_synced_at"] is None
    assert states[1]["calendar_id"] == "primary" and states[1]["last_synced_at"] is not None


# --- read-only, structurally ---------------------------------------------------


def test_no_calendar_write_surface_exists():
    """§0 non-goal: no code path can create, change, or delete a calendar event."""
    forbidden = re.compile(r"create|update|delete|insert|patch|put|post|write", re.I)
    for cls in (adapter_mod.CalendarProvider, StubProvider, ics_mod.IcsProvider):
        offenders = [n for n in dir(cls) if not n.startswith("_") and forbidden.search(n)]
        assert offenders == [], f"{cls.__name__} exposes {offenders}"
    assert not hasattr(adapter_mod, "SyncPage")
    rendered = str(commands_json_schema())
    assert "Calendar" not in rendered, "no calendar command of any kind in the model's schema"
