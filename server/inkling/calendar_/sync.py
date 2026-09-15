"""Calendar sync engine (§12): feed polling, snapshot diff, relevance gate.

Read-only, by construction: the engine's only inputs are ICS feed snapshots
and its only outputs are rows in our own mirror tables plus turn triggers.
No code path here (or anywhere) can write to a calendar.

Flow: every `poll_seconds` each enabled feed is fetched (conditionally — an
unchanged feed is a 304 and a `last_synced_at` touch). A changed feed is a
full snapshot of the window [now - 7 d, now + 60 d): each occurrence is
diffed against `calendar_events_cache` by revision and upserted; cached
events inside the window that the feed no longer lists are tombstoned
(`status = 'cancelled'`) and treated as cancellations. Material changes then
pass through the deterministic relevance gate; only passers become
priority-3 turn triggers carrying the normalized delta, keyed by event id +
revision so a re-fetched revision can't double-fire.

Relevance gate (§12, rules only — no model involved): material change AND
starts within 72 h AND (starts today OR overlaps a daily_plan time block OR
falls inside an open commitment's [fire_at, due_at] window).

If no feeds are configured, the engine is never started and the feature is
simply absent (§12).
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from inkling.calendar_.adapter import CalendarProvider, NormalizedEvent
from inkling.calendar_.config import FeedSpec
from inkling.db.client import Database
from inkling.db.queries import calendar as calendar_q
from inkling.db.queries import commitments as commitments_q
from inkling.db.queries import docs as docs_q
from inkling.db.queries import triggers as triggers_q
from inkling.db.queries import users as users_q
from inkling.turns.ingress import PRIORITY_BY_SOURCE

log = logging.getLogger(__name__)

PROVIDER = "ics"
RELEVANCE_HORIZON = timedelta(hours=72)
WINDOW_PAST = timedelta(days=7)
WINDOW_FUTURE = timedelta(days=60)
POLL_SECONDS = 300.0

# Time blocks in a daily_plan doc: "09:00-10:30", "9:00 – 10:30", "14:00—15:00".
_BLOCK_RE = re.compile(
    r"(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})"
)


def plan_blocks(
    plan_text: str, *, day: datetime, tz: ZoneInfo
) -> list[tuple[datetime, datetime]]:
    """Concrete time ranges for `day` parsed from daily_plan prose."""
    blocks = []
    local_day = day.astimezone(tz)
    for m in _BLOCK_RE.finditer(plan_text):
        try:
            start = local_day.replace(
                hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0
            )
            end = local_day.replace(
                hour=int(m[3]), minute=int(m[4]), second=0, microsecond=0
            )
        except ValueError:
            continue  # "25:99" is prose, not a block
        if end > start:
            blocks.append((start, end))
    return blocks


def _overlaps(
    a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime
) -> bool:
    return a_start < b_end and b_start < a_end


def is_material(event: NormalizedEvent, cached: dict[str, Any] | None) -> bool:
    """New, moved, retitled, or cancelled — not a description-only edit."""
    if cached is None:
        return event.status != "cancelled"  # a deletion we never saw isn't news
    if event.status == "cancelled":
        return cached["status"] != "cancelled"
    return (
        event.starts_at != cached["starts_at"]
        or event.ends_at != cached["ends_at"]
        or (event.summary or "") != (cached["summary"] or "")
        or cached["status"] == "cancelled"  # un-cancelled: back on the books
    )


def passes_gate(
    event: NormalizedEvent,
    *,
    cached: dict[str, Any] | None,
    now: datetime,
    tz: ZoneInfo,
    plan_text: str,
    commitment_windows: list[tuple[datetime, datetime]],
) -> bool:
    if not is_material(event, cached):
        return False
    if event.starts_at is None or event.ends_at is None:
        return False
    if not (now - timedelta(hours=1) <= event.starts_at <= now + RELEVANCE_HORIZON):
        return False

    # Anything material about today — a new event, a move, a cancellation —
    # changes the shape of the day the model is currently working in.
    if event.starts_at.astimezone(tz).date() == now.astimezone(tz).date():
        return True
    for start, end in plan_blocks(plan_text, day=event.starts_at, tz=tz):
        if _overlaps(event.starts_at, event.ends_at, start, end):
            return True
    for start, end in commitment_windows:
        if _overlaps(event.starts_at, event.ends_at, start, end):
            return True
    return False


def _when(event: NormalizedEvent, tz: ZoneInfo) -> str:
    if event.starts_at is None:
        return "?"
    local = event.starts_at.astimezone(tz)
    if event.all_day:
        return local.strftime("%a %Y-%m-%d (all day)")
    return local.strftime("%a %Y-%m-%d %H:%M")


def _delta_summary(
    event: NormalizedEvent, cached: dict[str, Any] | None, tz: ZoneInfo
) -> str:
    title = event.summary or (cached["summary"] if cached else None) or "(untitled)"
    when = _when(event, tz)
    if event.status == "cancelled":
        return f"Calendar: '{title}' ({when}) was cancelled."
    if cached is None or cached["status"] == "cancelled":
        return f"Calendar: new event '{title}' at {when}."
    if event.starts_at != cached["starts_at"]:
        return f"Calendar: '{title}' moved to {when}."
    return f"Calendar: '{title}' ({when}) changed."


def _cancelled_from_row(row: dict[str, Any]) -> NormalizedEvent:
    """A cache row the feed dropped, expressed as a cancellation delta."""
    return NormalizedEvent(
        provider_event_id=row["provider_event_id"],
        calendar_id=row["calendar_id"],
        revision=f"gone:{row['revision'] or ''}",
        summary=row["summary"],
        starts_at=row["starts_at"],
        ends_at=row["ends_at"],
        status="cancelled",
        all_day=bool(row["all_day"]),
        raw=row["raw"] or {},
    )


class CalendarSync:
    def __init__(
        self,
        db: Database,
        provider: CalendarProvider,
        feeds_for: dict[str, list[FeedSpec]],  # user_id -> configured feeds
        *,
        poll_seconds: float = POLL_SECONDS,
        nudge: Callable[[str], None] | None = None,
    ):
        self.db = db
        self.provider = provider
        self.feeds_for = feeds_for
        self.poll_seconds = poll_seconds
        self.nudge = nudge
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()

    @property
    def user_ids(self) -> list[str]:
        return list(self.feeds_for)

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    async def run_forever(self) -> None:
        try:
            await self.register_feeds()
        except Exception:
            log.exception("calendar feed registration failed; will retry each pass")
        while not self._stop.is_set():
            try:
                await self.sync_all()
            except Exception:
                log.exception("calendar pass failed; retrying next poll")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass
            finally:
                self._wake.clear()

    async def register_feeds(self) -> None:
        """Mirror the config files into calendar_sync_state (cursors kept)."""
        for user_id, feeds in self.feeds_for.items():
            async with self.db.with_tenant(user_id) as conn:
                for feed in feeds:
                    await calendar_q.upsert_state(
                        conn,
                        user_id=user_id,
                        provider=PROVIDER,
                        calendar_id=feed.id,
                        summary=feed.name,
                        enabled=True,
                    )
                disabled = await calendar_q.disable_missing(
                    conn,
                    user_id=user_id,
                    provider=PROVIDER,
                    keep_ids=[f.id for f in feeds],
                )
            log.info(
                "registered %d calendar feed(s) for %s: %s%s",
                len(feeds), user_id, ", ".join(f.name for f in feeds),
                f" (disabled: {', '.join(disabled)})" if disabled else "",
            )

    async def sync_all(self, *, now: datetime | None = None) -> int:
        """One pass over every enabled feed; returns deltas that passed."""
        now = now or datetime.now(timezone.utc)
        passed = 0
        for user_id in self.user_ids:
            async with self.db.with_tenant(user_id) as conn:
                states = await calendar_q.enabled_states(conn)
            for state in states:
                try:
                    passed += await self.sync_calendar(
                        user_id, state["calendar_id"], now=now
                    )
                except Exception:
                    log.exception(
                        "sync failed for user %s calendar %s",
                        user_id, state["calendar_id"],
                    )
        return passed

    async def sync_calendar(
        self, user_id: str, calendar_id: str, *, now: datetime | None = None
    ) -> int:
        """Fetch one feed, diff it into the cache; returns gate passers."""
        now = now or datetime.now(timezone.utc)
        async with self.db.with_tenant(user_id) as conn:
            states = await calendar_q.enabled_states(conn)
            user = await users_q.get(conn, user_id)
        state = next((s for s in states if s["calendar_id"] == calendar_id), None)
        if state is None:
            log.warning("sync requested for unconfigured/disabled calendar %s", calendar_id)
            return 0
        assert user is not None
        try:
            tz = ZoneInfo(user["timezone"])
        except Exception:
            tz = ZoneInfo("UTC")

        window_start = now - WINDOW_PAST
        window_end = now + WINDOW_FUTURE
        snapshot = await self.provider.snapshot(
            user_id=user_id,
            calendar_id=calendar_id,
            cursor=state["sync_cursor"],
            window_start=window_start,
            window_end=window_end,
            tz=tz,
        )
        if snapshot.unchanged:
            async with self.db.with_tenant(user_id) as conn:
                await calendar_q.set_cursor(
                    conn, user_id=user_id, provider=PROVIDER,
                    calendar_id=calendar_id, cursor=state["sync_cursor"],
                )
            return 0

        passed = 0
        async with self.db.with_tenant(user_id) as conn:
            plan = await docs_q.current(conn, "daily_plan")
            plan_text = plan["content"] if plan else ""
            windows = []
            for c in await commitments_q.open_scheduled(conn):
                if c["due_at"] is not None:
                    lead = c["lead_time"] or timedelta(0)
                    windows.append((c["due_at"] - lead, c["due_at"]))

            async def consider(event: NormalizedEvent, cached: dict[str, Any] | None) -> None:
                nonlocal passed
                if not passes_gate(
                    event, cached=cached, now=now, tz=tz,
                    plan_text=plan_text, commitment_windows=windows,
                ):
                    return
                # source_ref carries event id + revision: a natural idempotency
                # key so a re-fetched revision can't double-fire, and distinct
                # per change so successive edits each get their own trigger.
                # A tombstoned event coming back at its old revision is a new
                # change, so it gets its own key too.
                rev = event.revision or ""
                if cached is not None and cached["status"] == "cancelled":
                    rev += ":revived"
                await triggers_q.insert(
                    conn,
                    user_id=user_id,
                    source="calendar",
                    source_ref=f"{calendar_id}/{event.provider_event_id}@{rev}",
                    idempotency_key=(
                        f"caldelta:{calendar_id}:{event.provider_event_id}:{rev}"
                    ),
                    payload=json.dumps({
                        "kind": "delta",
                        "text": _delta_summary(event, cached, tz),
                        "event": {
                            "id": event.provider_event_id,
                            "summary": event.summary,
                            "starts_at": event.starts_at.isoformat()
                            if event.starts_at else None,
                            "ends_at": event.ends_at.isoformat()
                            if event.ends_at else None,
                            "all_day": event.all_day,
                            "status": event.status,
                        },
                    }),
                    priority=PRIORITY_BY_SOURCE["calendar"],
                )
                passed += 1

            seen_ids: list[str] = []
            for event in snapshot.events:
                seen_ids.append(event.provider_event_id)
                cached = await calendar_q.cache_get(
                    conn,
                    provider_event_id=event.provider_event_id,
                    calendar_id=calendar_id,
                )
                if (
                    cached is not None
                    and cached["revision"] == event.revision
                    and cached["status"] != "cancelled"
                ):
                    continue  # byte-identical occurrence; nothing to write
                await consider(event, cached)
                await calendar_q.cache_upsert(
                    conn,
                    user_id=user_id,
                    provider_event_id=event.provider_event_id,
                    calendar_id=calendar_id,
                    revision=event.revision,
                    starts_at=event.starts_at,
                    ends_at=event.ends_at,
                    summary=event.summary,
                    status=event.status,
                    all_day=event.all_day,
                    raw=event.raw,
                )

            gone = await calendar_q.cache_cancel_missing(
                conn,
                calendar_id=calendar_id,
                window_start=window_start,
                window_end=window_end,
                keep_ids=seen_ids,
            )
            for row in gone:
                # `row` is the pre-tombstone state for the gate's purposes.
                before = dict(row, status="confirmed")
                await consider(_cancelled_from_row(row), before)

            await calendar_q.set_cursor(
                conn,
                user_id=user_id,
                provider=PROVIDER,
                calendar_id=calendar_id,
                cursor=snapshot.cursor,
            )
        if passed and self.nudge is not None:
            self.nudge(user_id)
        log.info(
            "synced calendar %s for %s: %d event(s), %d removed, %d passed the gate",
            calendar_id, user_id, len(snapshot.events), len(gone), passed,
        )
        return passed
