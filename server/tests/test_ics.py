"""ICS feed provider (§12): parsing, recurrence expansion, conditional fetch.

Pure tests — no database. The feed fixture covers a timed event, an all-day
event, a weekly recurrence with an EXDATE and a RECURRENCE-ID override, a
cancelled event, and a floating-time event.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from inkling.calendar_.config import FeedConfigError, parse_feeds
from inkling.calendar_.ics import IcsProvider, parse_feed

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
WINDOW_START = datetime(2026, 9, 7, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 7, tzinfo=UTC)

FEED = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:timed-1
DTSTART:20260911T140000Z
DTEND:20260911T150000Z
SUMMARY:Dentist
LOCATION:Main St
END:VEVENT
BEGIN:VEVENT
UID:allday-1
DTSTART;VALUE=DATE:20260912
DTEND;VALUE=DATE:20260913
SUMMARY:Labor Day
END:VEVENT
BEGIN:VEVENT
UID:weekly-1
DTSTART;TZID=America/New_York:20260908T090000
DTEND;TZID=America/New_York:20260908T093000
RRULE:FREQ=WEEKLY;COUNT=4
EXDATE;TZID=America/New_York:20260922T090000
SUMMARY:Standup
END:VEVENT
BEGIN:VEVENT
UID:weekly-1
RECURRENCE-ID;TZID=America/New_York:20260915T090000
DTSTART;TZID=America/New_York:20260915T100000
DTEND;TZID=America/New_York:20260915T103000
SUMMARY:Standup (moved)
END:VEVENT
BEGIN:VEVENT
UID:cancelled-1
DTSTART:20260913T120000Z
DTEND:20260913T130000Z
STATUS:CANCELLED
SUMMARY:Old lunch
END:VEVENT
BEGIN:VEVENT
UID:floating-1
DTSTART:20260914T083000
DTEND:20260914T090000
SUMMARY:Floating breakfast
END:VEVENT
BEGIN:VEVENT
UID:outside-1
DTSTART:20261201T120000Z
DTEND:20261201T130000Z
SUMMARY:Far future
END:VEVENT
END:VCALENDAR
"""


def _parse():
    return {
        e.provider_event_id: e
        for e in parse_feed(
            FEED, calendar_id="personal", tz=NY,
            window_start=WINDOW_START, window_end=WINDOW_END,
        )
    }


def test_timed_event_keeps_instant_and_carries_location():
    event = _parse()["personal:timed-1"]
    assert event.starts_at == datetime(2026, 9, 11, 14, tzinfo=UTC)
    assert event.ends_at == datetime(2026, 9, 11, 15, tzinfo=UTC)
    assert event.all_day is False
    assert event.status == "confirmed"
    assert event.summary == "Dentist"
    assert event.raw["location"] == "Main St"


def test_all_day_event_is_local_midnight_in_user_tz():
    event = _parse()["personal:allday-1"]
    assert event.all_day is True
    assert event.starts_at == datetime(2026, 9, 12, 0, 0, tzinfo=NY)
    assert event.ends_at == datetime(2026, 9, 13, 0, 0, tzinfo=NY)
    # NOT 8 PM the previous evening, which is what midnight-UTC would give.
    assert event.starts_at.astimezone(UTC).hour == 4


def test_weekly_recurrence_expands_with_exdate_and_override():
    events = _parse()
    standups = sorted(
        (e for e in events.values() if e.raw["uid"] == "weekly-1"),
        key=lambda e: e.starts_at,
    )
    starts = [e.starts_at.astimezone(NY).strftime("%m-%d %H:%M") for e in standups]
    # COUNT=4 → 09-08, 09-15, 09-22, 09-29; 09-22 is EXDATEd; 09-15 is moved.
    assert starts == ["09-08 09:00", "09-15 10:00", "09-29 09:00"]
    moved = standups[1]
    assert moved.summary == "Standup (moved)"
    assert moved.raw["recurring"] is True
    # Instance ids embed the occurrence start so refetches upsert, not duplicate.
    assert len({e.provider_event_id for e in standups}) == 3
    assert all(":" in e.provider_event_id[len("personal:weekly-1"):] for e in standups)


def test_cancelled_status_is_preserved():
    assert _parse()["personal:cancelled-1"].status == "cancelled"


def test_floating_time_is_read_in_user_tz():
    event = _parse()["personal:floating-1"]
    assert event.starts_at == datetime(2026, 9, 14, 8, 30, tzinfo=NY)
    assert event.all_day is False


def test_events_outside_window_are_dropped():
    assert "personal:outside-1" not in _parse()


def test_revision_is_stable_across_parses_and_changes_with_content():
    first = _parse()["personal:timed-1"]
    second = _parse()["personal:timed-1"]
    assert first.revision == second.revision
    moved = parse_feed(
        FEED.replace("DTEND:20260911T150000Z", "DTEND:20260911T160000Z"),
        calendar_id="personal", tz=NY,
        window_start=WINDOW_START, window_end=WINDOW_END,
    )
    changed = {e.provider_event_id: e for e in moved}["personal:timed-1"]
    assert changed.revision != first.revision


# --- conditional fetch -------------------------------------------------------


async def test_snapshot_uses_etag_and_reports_304_as_unchanged():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(
            200, content=FEED.encode(),
            headers={"ETag": '"v1"', "Content-Type": "text/calendar"},
        )

    provider = IcsProvider(
        {"testa": {"personal": "https://example.test/basic.ics"}},
        transport=httpx.MockTransport(handler),
    )
    try:
        first = await provider.snapshot(
            user_id="testa", calendar_id="personal", cursor=None,
            window_start=WINDOW_START, window_end=WINDOW_END, tz=NY,
        )
        assert first.unchanged is False
        assert first.cursor == 'etag:"v1"'
        assert {e.provider_event_id for e in first.events} >= {
            "personal:timed-1", "personal:allday-1"
        }

        second = await provider.snapshot(
            user_id="testa", calendar_id="personal", cursor=first.cursor,
            window_start=WINDOW_START, window_end=WINDOW_END, tz=NY,
        )
        assert second.unchanged is True
        assert second.events == []
        assert second.cursor == first.cursor
    finally:
        await provider.close()
    assert [r.method for r in seen] == ["GET", "GET"], "reads only, ever"


async def test_unknown_feed_is_an_error_not_a_silent_empty():
    provider = IcsProvider({}, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(RuntimeError):
        await provider.snapshot(
            user_id="testa", calendar_id="nope", cursor=None,
            window_start=WINDOW_START, window_end=WINDOW_END, tz=NY,
        )
    await provider.close()


# --- feed config file --------------------------------------------------------


def test_parse_feeds_normalizes_webcal_and_validates_ids():
    feeds = parse_feeds(
        '{"calendars": [{"id": "family", "name": "Family", '
        '"url": "webcal://p12.icloud.com/published/2/abc"}]}'
    )
    assert feeds[0].url == "https://p12.icloud.com/published/2/abc"
    assert feeds[0].exclude == ()
    for bad in (
        '{"calendars": [{"id": "Bad Id", "name": "x", "url": "https://a/b"}]}',
        '{"calendars": [{"id": "a", "name": "", "url": "https://a/b"}]}',
        '{"calendars": [{"id": "a", "name": "x", "url": "http://a/b"}]}',
        '{"calendars": [{"id": "a", "name": "x", "url": "https://a/b"},'
        ' {"id": "a", "name": "y", "url": "https://a/c"}]}',
        '{"nope": []}',
        'not json',
        '{"calendars": [{"id": "a", "name": "x", "url": "https://a/b", "exclude": "OMSCS"}]}',
        '{"calendars": [{"id": "a", "name": "x", "url": "https://a/b", "exclude": [""]}]}',
    ):
        with pytest.raises(FeedConfigError):
            parse_feeds(bad)


def test_parse_feeds_exclude_matches_titles_case_insensitively():
    [feed] = parse_feeds(
        '{"calendars": [{"id": "canvas", "name": "Canvas", "url": "https://a/b",'
        ' "exclude": ["[OMSCS Student Center]"]}]}'
    )
    assert feed.excludes("Coffee Hour with David Joyner [OMSCS Student Center]")
    assert feed.excludes("x [omscs student center]")
    assert not feed.excludes("HW2 - Programming [CS-7650-A]")
    assert not feed.excludes(None)
