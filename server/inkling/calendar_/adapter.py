"""Calendar provider interface (§12) — read-only by construction.

A provider can do exactly one thing: return a snapshot of a calendar's events
inside a time window. There is no create, update, or delete method, and the
interface cannot express one: whatever the model or a bug asks for, an adapter
has no vocabulary for writing to a calendar. tests/test_calendar.py asserts
this surface stays that way.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class NormalizedEvent:
    """A feed event reduced to what the gate and the model care about."""

    provider_event_id: str
    calendar_id: str
    revision: str | None
    summary: str | None
    starts_at: datetime | None
    ends_at: datetime | None
    status: str  # confirmed | tentative | cancelled
    all_day: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Snapshot:
    """Everything the feed lists inside the requested window.

    `unchanged=True` means the source reported no change since `cursor`
    (HTTP 304); `events` is then empty and the cache is left untouched.
    """

    events: list[NormalizedEvent]
    cursor: str | None
    unchanged: bool = False


class CalendarProvider(Protocol):
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
        """Fetch the calendar's events in [window_start, window_end)."""
        ...


class StubProvider:
    """Deterministic provider for tests: scripted snapshots, recorded calls."""

    def __init__(self):
        self.snapshots: list[Snapshot] = []
        self.calls: list[dict[str, Any]] = []

    async def snapshot(
        self, *, user_id, calendar_id, cursor, window_start, window_end, tz
    ) -> Snapshot:
        self.calls.append({
            "op": "snapshot", "calendar_id": calendar_id, "cursor": cursor,
            "window_start": window_start, "window_end": window_end,
        })
        if self.snapshots:
            return self.snapshots.pop(0)
        return Snapshot(events=[], cursor=cursor, unchanged=True)
