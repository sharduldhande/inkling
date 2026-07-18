"""Calendar sync engine (§12): cursor sync, webhook→hint, relevance gate.

Flow: the webhook route records a coalescible hint trigger (source='calendar',
payload kind='hint'). This engine — not the turn worker; the worker's claim
skips hints — claims hints, runs incremental_sync with the stored cursor
(410/CursorGone → bounded full resync, ±60 days), diffs each event against
calendar_events_cache by revision, skips events we created, and pushes the
survivors through the deterministic relevance gate. Only passers become
priority-3 turn triggers carrying the normalized delta; their source_ref
includes event id + revision so they never coalesce with pending hints (the
coalescing CASE in triggers keys on (source='calendar', source_ref)).

Relevance gate (§12, rules only — no model involved): material change AND
starts within 72 h AND (overlaps a daily_plan time block OR falls inside an
open commitment's [fire_at, due_at] window OR is new today).

Channel renewal: handled by this engine's own reconcile pass (each loop it
renews channels expiring within a day) rather than §10's heap — same
behavior, and it keeps the scheduler ignorant of provider auth. If calendar
is not configured (no calendar_sync_state rows), every pass is a no-op and
the feature is simply absent (§12).
"""

import asyncio
import json
import logging
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from inkling.calendar_.adapter import CalendarProvider, CursorGone, NormalizedEvent
from inkling.db.client import Database
from inkling.db.queries import calendar as calendar_q
from inkling.db.queries import commitments as commitments_q
from inkling.db.queries import docs as docs_q
from inkling.db.queries import triggers as triggers_q
from inkling.db.queries import users as users_q
from inkling.turns.ingress import PRIORITY_BY_SOURCE

log = logging.getLogger(__name__)

RELEVANCE_HORIZON = timedelta(hours=72)
FULL_RESYNC_WINDOW = timedelta(days=60)  # ±60 days (§12)
RENEW_BEFORE = timedelta(days=1)
RECONCILE_SECONDS = 60.0

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
    """New, moved, retitled, or cancelled — not a reminder-blob revision bump."""
    if cached is None:
        return event.status != "cancelled"  # a deletion we never saw isn't news
    if event.status == "cancelled":
        return True
    return (
        event.starts_at != cached["starts_at"]
        or event.ends_at != cached["ends_at"]
        or (event.summary or "") != (cached["summary"] or "")
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
    if cached is not None and cached["created_by_us"]:
        return False
    if not is_material(event, cached):
        return False
    if event.starts_at is None or event.ends_at is None:
        return False
    if not (now - timedelta(hours=1) <= event.starts_at <= now + RELEVANCE_HORIZON):
        return False

    is_new_today = (
        cached is None
        and event.starts_at.astimezone(tz).date() == now.astimezone(tz).date()
    )
    if is_new_today:
        return True
    for start, end in plan_blocks(plan_text, day=event.starts_at, tz=tz):
        if _overlaps(event.starts_at, event.ends_at, start, end):
            return True
    for start, end in commitment_windows:
        if _overlaps(event.starts_at, event.ends_at, start, end):
            return True
    return False


def _delta_summary(event: NormalizedEvent, cached: dict[str, Any] | None) -> str:
    when = event.starts_at.isoformat() if event.starts_at else "?"
    if event.status == "cancelled":
        return f"Calendar: '{cached['summary'] if cached else event.summary}' was cancelled."
    if cached is None:
        return f"Calendar: new event '{event.summary}' at {when}."
    if event.starts_at != cached["starts_at"]:
        return f"Calendar: '{event.summary}' moved to {when}."
    return f"Calendar: '{event.summary}' changed."


class CalendarSync:
    def __init__(
        self,
        db: Database,
        user_ids: list[str],
        provider: CalendarProvider,
        *,
        webhook_url: str = "",
        reconcile_seconds: float = RECONCILE_SECONDS,
        nudge: Callable[[str], None] | None = None,
    ):
        self.db = db
        self.user_ids = user_ids
        self.provider = provider
        self.webhook_url = webhook_url
        self.reconcile_seconds = reconcile_seconds
        self.nudge = nudge
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self.process_hints()
                await self.renew_channels()
            except Exception:
                log.exception("calendar pass failed; retrying next reconcile")
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self.reconcile_seconds
                )
            except TimeoutError:
                pass
            finally:
                self._wake.clear()

    async def process_hints(self) -> int:
        """Claim pending hint triggers and sync their calendars."""
        processed = 0
        for user_id in self.user_ids:
            while True:
                async with self.db.with_tenant(user_id) as conn:
                    hint = await triggers_q.claim_calendar_hint(conn)
                if hint is None:
                    break
                calendar_id = hint["source_ref"] or ""
                try:
                    await self.sync_calendar(user_id, calendar_id)
                    status = "done"
                except Exception:
                    log.exception(
                        "sync failed for user %s calendar %s", user_id, calendar_id
                    )
                    status = "error"
                async with self.db.with_tenant(user_id) as conn:
                    await triggers_q.set_status(conn, int(hint["id"]), status)
                processed += 1
        return processed

    async def sync_calendar(
        self, user_id: str, calendar_id: str, *, now: datetime | None = None
    ) -> int:
        """One sync pass; returns how many deltas passed the gate."""
        now = now or datetime.now(timezone.utc)
        async with self.db.with_tenant(user_id) as conn:
            states = await calendar_q.states(conn)
        state = next(
            (s for s in states if s["calendar_id"] == calendar_id), None
        )
        if state is None:
            log.warning("hint for unconfigured calendar %s; ignoring", calendar_id)
            return 0

        full_resync = False
        if state["sync_cursor"]:
            try:
                page = await self.provider.incremental_sync(
                    user_id=user_id, calendar_id=calendar_id,
                    cursor=state["sync_cursor"],
                )
            except CursorGone:
                full_resync = True
        else:
            full_resync = True
        if full_resync:
            page = await self.provider.initial_sync(
                user_id=user_id, calendar_id=calendar_id,
                window_start=now - FULL_RESYNC_WINDOW,
                window_end=now + FULL_RESYNC_WINDOW,
            )

        passed = 0
        async with self.db.with_tenant(user_id) as conn:
            user = await users_q.get(conn, user_id)
            assert user is not None
            try:
                tz = ZoneInfo(user["timezone"])
            except Exception:
                tz = ZoneInfo("UTC")
            plan = await docs_q.current(conn, "daily_plan")
            plan_text = plan["content"] if plan else ""
            windows = []
            for c in await commitments_q.open_scheduled(conn):
                if c["due_at"] is not None:
                    lead = c["lead_time"] or timedelta(0)
                    windows.append((c["due_at"] - lead, c["due_at"]))

            for event in page.events:
                cached = await calendar_q.cache_get(
                    conn,
                    provider_event_id=event.provider_event_id,
                    calendar_id=calendar_id,
                )
                relevant = passes_gate(
                    event, cached=cached, now=now, tz=tz,
                    plan_text=plan_text, commitment_windows=windows,
                )
                await calendar_q.cache_upsert(
                    conn,
                    user_id=user_id,
                    provider_event_id=event.provider_event_id,
                    calendar_id=calendar_id,
                    revision=event.revision,
                    starts_at=event.starts_at,
                    ends_at=event.ends_at,
                    summary=event.summary,
                    created_by_us=bool(cached and cached["created_by_us"]),
                    raw=event.raw,
                )
                if not relevant:
                    continue
                # source_ref carries event id + revision: distinct from the
                # calendar_id refs that hints coalesce on, and a natural
                # idempotency key so a re-synced revision can't double-fire.
                await triggers_q.insert(
                    conn,
                    user_id=user_id,
                    source="calendar",
                    source_ref=f"{calendar_id}/{event.provider_event_id}@{event.revision}",
                    idempotency_key=(
                        f"caldelta:{calendar_id}:{event.provider_event_id}"
                        f":{event.revision}"
                    ),
                    payload=json.dumps({
                        "kind": "delta",
                        "text": _delta_summary(event, cached),
                        "event": {
                            "id": event.provider_event_id,
                            "summary": event.summary,
                            "starts_at": event.starts_at.isoformat()
                            if event.starts_at else None,
                            "ends_at": event.ends_at.isoformat()
                            if event.ends_at else None,
                            "status": event.status,
                        },
                    }),
                    priority=PRIORITY_BY_SOURCE["calendar"],
                )
                passed += 1

            await calendar_q.set_cursor(
                conn,
                user_id=user_id,
                provider="google",
                calendar_id=calendar_id,
                cursor=page.next_cursor,
                full_resync=full_resync,
            )
        if passed and self.nudge is not None:
            self.nudge(user_id)
        log.info(
            "synced calendar %s for %s: %d event(s), %d passed the gate%s",
            calendar_id, user_id, len(page.events), passed,
            " (full resync)" if full_resync else "",
        )
        return passed

    async def renew_channels(self, *, now: datetime | None = None) -> int:
        if not self.webhook_url:
            return 0
        now = now or datetime.now(timezone.utc)
        renewed = 0
        for user_id in self.user_ids:
            async with self.db.with_tenant(user_id) as conn:
                states = await calendar_q.states(conn)
            for state in states:
                expires = state["channel_expires_at"]
                if expires is not None and expires - now > RENEW_BEFORE:
                    continue
                try:
                    channel_id, new_expiry = await self.provider.renew_channel(
                        user_id=user_id,
                        calendar_id=state["calendar_id"],
                        webhook_url=self.webhook_url,
                    )
                except Exception:
                    log.exception(
                        "channel renewal failed for %s/%s",
                        user_id, state["calendar_id"],
                    )
                    continue
                async with self.db.with_tenant(user_id) as conn:
                    await calendar_q.set_channel(
                        conn,
                        user_id=user_id,
                        provider=state["provider"],
                        calendar_id=state["calendar_id"],
                        channel_id=channel_id,
                        expires_at=new_expiry,
                    )
                renewed += 1
        return renewed
