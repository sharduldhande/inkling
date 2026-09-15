"""ICS feed provider (§12): fetch a calendar's iCalendar feed and normalize it.

This is the only calendar provider. It issues HTTP GETs and nothing else — an
ICS feed has no write side, so read-only is structural, not a policy. Google's
"secret address in iCal format", iCloud "Public Calendar" links, and any
subscribed-from-URL feed all serve the same format.

Fetching is conditional: the sync cursor stores the feed's ETag (or
Last-Modified), and a 304 comes back as `Snapshot(unchanged=True)` so an
unchanged feed costs one cheap request and zero database writes.

Parsing: `icalendar` reads the feed; `recurring_ical_events` expands RRULE /
RDATE / EXDATE and applies RECURRENCE-ID overrides inside the window. Each
occurrence gets a stable id (`<calendar>:<UID>` plus the instance start for
recurring ones) so refetches upsert rather than duplicate. All-day events
(date-only DTSTART) become local midnight in the user's timezone and carry
`all_day=True`; floating times are read in the user's timezone too.
"""

import hashlib
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events

from inkling.calendar_.adapter import NormalizedEvent, Snapshot

log = logging.getLogger(__name__)

MAX_FEED_BYTES = 8 * 1024 * 1024
_CURSOR_ETAG = "etag:"
_CURSOR_MODIFIED = "modified:"


def _as_datetime(value: Any, tz: ZoneInfo) -> tuple[datetime | None, bool]:
    """(aware datetime, all_day). Dates → local midnight; naive → user tz."""
    if value is None:
        return None, False
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=tz), False
        return value, False
    if isinstance(value, date):
        return datetime.combine(value, time(0), tzinfo=tz), True
    return None, False


def _prop(component: icalendar.Component, name: str) -> Any:
    value = component.get(name)
    return getattr(value, "dt", value)


def _text(component: icalendar.Component, name: str) -> str | None:
    value = component.get(name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_component(
    component: icalendar.Component,
    *,
    calendar_id: str,
    tz: ZoneInfo,
    recurring: bool,
) -> NormalizedEvent | None:
    """One VEVENT occurrence → NormalizedEvent; None if it has no usable UID.

    `recurring` comes from the source feed (the expander strips RRULE and
    stamps RECURRENCE-ID on every occurrence, so it can't be read here).
    """
    uid = _text(component, "UID")
    if not uid:
        return None
    starts_at, all_day = _as_datetime(_prop(component, "DTSTART"), tz)
    ends_at, _ = _as_datetime(_prop(component, "DTEND"), tz)
    if ends_at is None and starts_at is not None:
        duration = _prop(component, "DURATION")
        if isinstance(duration, timedelta):
            ends_at = starts_at + duration
        elif all_day:
            ends_at = starts_at + timedelta(days=1)
        else:
            ends_at = starts_at
    summary = _text(component, "SUMMARY")
    status = (_text(component, "STATUS") or "confirmed").lower()
    if status not in ("confirmed", "tentative", "cancelled"):
        status = "confirmed"
    location = _text(component, "LOCATION")

    event_id = f"{calendar_id}:{uid}"
    if recurring and starts_at is not None:
        event_id += ":" + starts_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    fingerprint = "|".join([
        summary or "",
        starts_at.isoformat() if starts_at else "",
        ends_at.isoformat() if ends_at else "",
        status,
        location or "",
        "all-day" if all_day else "",
    ])
    revision = hashlib.sha1(fingerprint.encode()).hexdigest()[:16]

    return NormalizedEvent(
        provider_event_id=event_id,
        calendar_id=calendar_id,
        revision=revision,
        summary=summary,
        starts_at=starts_at,
        ends_at=ends_at,
        status=status,
        all_day=all_day,
        raw={
            "uid": uid,
            "summary": summary,
            "location": location,
            "description": (_text(component, "DESCRIPTION") or "")[:2000] or None,
            "recurring": recurring,
        },
    )


def parse_feed(
    text: str | bytes,
    *,
    calendar_id: str,
    tz: ZoneInfo,
    window_start: datetime,
    window_end: datetime,
) -> list[NormalizedEvent]:
    """Expand a feed into the occurrences that fall inside the window."""
    calendar = icalendar.Calendar.from_ical(text)
    recurring_uids = {
        str(c.get("UID"))
        for c in calendar.walk("VEVENT")
        if any(c.get(k) is not None for k in ("RRULE", "RDATE", "RECURRENCE-ID"))
    }
    events: dict[str, NormalizedEvent] = {}
    query = recurring_ical_events.of(calendar, components=["VEVENT"])
    for component in query.between(window_start, window_end):
        event = normalize_component(
            component, calendar_id=calendar_id, tz=tz,
            recurring=str(component.get("UID")) in recurring_uids,
        )
        if event is None:
            continue
        events[event.provider_event_id] = event  # last write wins on dup ids
    return sorted(events.values(), key=lambda e: (e.starts_at or window_start, e.provider_event_id))


class IcsProvider:
    def __init__(
        self,
        feeds_for: dict[str, dict[str, str]],  # user_id -> {calendar_id: url}
        *,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.feeds_for = feeds_for
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "Inkling calendar sync (read-only)"},
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def snapshot(
        self,
        *,
        user_id: str,
        calendar_id: str,
        cursor: str | None,
        window_start: datetime,
        window_end: datetime,
        tz: ZoneInfo,
    ) -> Snapshot:
        url = self.feeds_for.get(user_id, {}).get(calendar_id)
        if not url:
            raise RuntimeError(f"no feed url configured for {user_id}/{calendar_id}")
        headers: dict[str, str] = {}
        if cursor and cursor.startswith(_CURSOR_ETAG):
            headers["If-None-Match"] = cursor[len(_CURSOR_ETAG):]
        elif cursor and cursor.startswith(_CURSOR_MODIFIED):
            headers["If-Modified-Since"] = cursor[len(_CURSOR_MODIFIED):]

        response = await self._client.get(url, headers=headers)
        if response.status_code == 304:
            return Snapshot(events=[], cursor=cursor, unchanged=True)
        response.raise_for_status()
        body = response.content
        if len(body) > MAX_FEED_BYTES:
            raise RuntimeError(f"feed {calendar_id} is {len(body)} bytes; refusing")

        new_cursor: str | None = None
        if response.headers.get("etag"):
            new_cursor = _CURSOR_ETAG + response.headers["etag"]
        elif response.headers.get("last-modified"):
            new_cursor = _CURSOR_MODIFIED + response.headers["last-modified"]

        events = parse_feed(
            body, calendar_id=calendar_id, tz=tz,
            window_start=window_start, window_end=window_end,
        )
        return Snapshot(events=events, cursor=new_cursor, unchanged=False)
