"""Calendar provider interface (§12).

The Protocol is the spec's, verbatim in shape: connect / initial_sync /
incremental_sync / renew_channel / create_event / normalize. There is
deliberately NO delete method — Google's scopes can't split delete from write,
so create-only is enforced structurally: an adapter simply cannot express a
deletion, whatever the model or a bug asks for.

`CursorGone` is the §12 `410 Gone` signal: the stored cursor expired and the
caller must run a bounded full resync (±60 days).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


class CursorGone(Exception):
    """The sync cursor expired (HTTP 410); run a bounded full resync."""


@dataclass(frozen=True)
class NormalizedEvent:
    """A provider event reduced to what the gate and the model care about."""

    provider_event_id: str
    calendar_id: str
    revision: str | None
    summary: str | None
    starts_at: datetime | None
    ends_at: datetime | None
    status: str  # confirmed | tentative | cancelled
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SyncPage:
    events: list[NormalizedEvent]
    next_cursor: str | None


class CalendarProvider(Protocol):
    async def connect(self, *, user_id: str, calendar_id: str) -> None:
        """Load/refresh credentials for this user+calendar; raise if unusable."""
        ...

    async def initial_sync(
        self, *, user_id: str, calendar_id: str,
        window_start: datetime, window_end: datetime,
    ) -> SyncPage:
        """Bounded full window scan; returns events plus a fresh cursor."""
        ...

    async def incremental_sync(
        self, *, user_id: str, calendar_id: str, cursor: str
    ) -> SyncPage:
        """Changes since cursor; raises CursorGone on an expired cursor."""
        ...

    async def renew_channel(
        self, *, user_id: str, calendar_id: str, webhook_url: str
    ) -> tuple[str, datetime]:
        """(Re)register the push channel; returns (channel_id, expires_at)."""
        ...

    async def create_event(
        self,
        *,
        user_id: str,
        calendar_id: str,
        summary: str,
        starts_at: datetime,
        ends_at: datetime,
        description: str | None,
    ) -> NormalizedEvent:
        """Insert one event; the only write this interface can express."""
        ...

    def normalize(self, raw: dict[str, Any], *, calendar_id: str) -> NormalizedEvent:
        ...


class StubProvider:
    """Deterministic provider for tests: scripted pages, recorded calls."""

    def __init__(self):
        self.pages: list[SyncPage] = []
        self.raise_cursor_gone = False
        self.calls: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []

    async def connect(self, *, user_id: str, calendar_id: str) -> None:
        self.calls.append({"op": "connect", "calendar_id": calendar_id})

    async def initial_sync(self, *, user_id, calendar_id, window_start, window_end):
        self.calls.append({
            "op": "initial_sync", "calendar_id": calendar_id,
            "window_start": window_start, "window_end": window_end,
        })
        return self.pages.pop(0) if self.pages else SyncPage([], "cursor-initial")

    async def incremental_sync(self, *, user_id, calendar_id, cursor):
        self.calls.append({
            "op": "incremental_sync", "calendar_id": calendar_id, "cursor": cursor,
        })
        if self.raise_cursor_gone:
            self.raise_cursor_gone = False
            raise CursorGone(cursor)
        return self.pages.pop(0) if self.pages else SyncPage([], cursor)

    async def renew_channel(self, *, user_id, calendar_id, webhook_url):
        self.calls.append({"op": "renew_channel", "calendar_id": calendar_id})
        from datetime import timedelta, timezone
        return (
            f"chan-{len(self.calls)}",
            datetime.now(timezone.utc) + timedelta(days=7),
        )

    async def create_event(
        self, *, user_id, calendar_id, summary, starts_at, ends_at, description
    ) -> NormalizedEvent:
        self.created.append({
            "calendar_id": calendar_id, "summary": summary,
            "starts_at": starts_at, "ends_at": ends_at,
        })
        return NormalizedEvent(
            provider_event_id=f"created-{len(self.created)}",
            calendar_id=calendar_id,
            revision="1",
            summary=summary,
            starts_at=starts_at,
            ends_at=ends_at,
            status="confirmed",
        )

    def normalize(self, raw: dict[str, Any], *, calendar_id: str) -> NormalizedEvent:
        return NormalizedEvent(
            provider_event_id=raw["id"],
            calendar_id=calendar_id,
            revision=raw.get("etag"),
            summary=raw.get("summary"),
            starts_at=raw.get("starts_at"),
            ends_at=raw.get("ends_at"),
            status=raw.get("status", "confirmed"),
            raw=raw,
        )
