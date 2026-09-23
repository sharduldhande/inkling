"""The Now / Next Live Activity (§11): a persistent lock-screen card.

The server owns the card. Every pass it derives what the card should say and
pushes only when that changed:

- NOW: the class or meeting in progress (any timed calendar event with a
  duration); otherwise the model's SetFocus line for today — the one thing to
  chip away at; otherwise the most urgent deadline (earliest-due open one-shot
  commitment or deadline-calendar item, e.g. a Canvas assignment).
- NEXT: the next timed event within 24 hours; otherwise the next deadline.

Recurring habits are deliberately absent: they're pushable, deadlines aren't.

- no activity on screen, or the current one is close to iOS's 8-hour limit
  → push-to-start a fresh one (ending the old one first, if we can reach it);
- otherwise → an update to the activity's own token.

The app's side is reporting tokens (routes_live_activity): the push-to-start
token on launch, and each activity's update token when iOS wakes it for a
server-started activity. If that report never arrives, the card can't be
updated in place, so a content change after ORPHAN_AFTER starts a new one
(the app ends the older duplicate the next time it runs).

The countdown itself needs no pushes: the widget renders the due time with a
self-updating relative Text. `overdue` is in the content so the label flips
("due in" → "overdue") with one push at the due minute.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from inkling.db.client import Database
from inkling.db.queries import live_activity as la_q

log = logging.getLogger(__name__)

POLL_SECONDS = 60
RESTART_AFTER = timedelta(hours=7, minutes=45)  # iOS ends activities at 8h
ORPHAN_AFTER = timedelta(minutes=10)
MORE_WINDOW = timedelta(days=7)
# NEXT shows an event only when it's genuinely next: back-to-back after the
# current class (<= 30 min gap), or, when free, starting within 3 hours today.
BACK_TO_BACK_GAP = timedelta(minutes=30)
NEXT_EVENT_WINDOW = timedelta(hours=3)
# Calendar feeds whose items are deadlines, not meetings (feed ids from
# calendars-<user>.json). Within them, a deadline is an all-day item (Canvas
# exports 11:59 PM due dates that way; due = end of that day) or a timed
# item with no duration (start == end, the due moment). Anything with a real
# duration (office hours, coffee chats) is a meeting and stays off the card.
DEADLINE_CALENDARS = ["canvas"]

_CANDIDATES = """
SELECT * FROM (
  SELECT 'commitment' AS kind, 'c:' || id::text AS id, title, due_at
  FROM commitments
  WHERE user_id = %(user_id)s
    AND status = 'open'
    AND recurrence IS NULL
    AND due_at IS NOT NULL
    AND due_at <= %(now)s + interval '14 days'
  UNION ALL
  SELECT 'deadline', 'g:' || c.provider_event_id,
         coalesce(c.summary, '(untitled)'),
         CASE WHEN c.all_day
              THEN coalesce(c.ends_at, c.starts_at + interval '1 day')
                   - interval '1 minute'
              ELSE c.starts_at END
  FROM calendar_events_cache c
  LEFT JOIN calendar_sync_state s
    ON s.user_id = c.user_id AND s.calendar_id = c.calendar_id
  WHERE c.user_id = %(user_id)s
    AND c.calendar_id = ANY(%(calendars)s)
    AND c.status <> 'cancelled'
    AND c.done_at IS NULL
    AND coalesce(s.enabled, true)
    AND c.starts_at IS NOT NULL
    AND (c.all_day OR c.ends_at IS NULL OR c.ends_at = c.starts_at)
) items
-- A passed calendar deadline drops off (we can't know it was submitted);
-- an overdue commitment stays until the user closes it.
WHERE due_at <= %(now)s + interval '14 days'
  AND (kind = 'commitment' OR due_at > %(now)s)
ORDER BY due_at, id
LIMIT 50
"""
# Timed events with a real duration from every enabled feed: classes and
# meetings. The zero-duration and all-day items are deadlines (above).
_EVENTS = """
SELECT coalesce(c.summary, '(untitled)') AS title, c.starts_at, c.ends_at,
       nullif(trim(c.raw->>'location'), '') AS location
FROM calendar_events_cache c
LEFT JOIN calendar_sync_state s
  ON s.user_id = c.user_id AND s.calendar_id = c.calendar_id
WHERE c.user_id = %(user_id)s
  AND c.status <> 'cancelled'
  AND c.done_at IS NULL
  AND coalesce(s.enabled, true)
  AND NOT c.all_day
  AND c.starts_at IS NOT NULL
  AND c.ends_at > c.starts_at
  AND c.ends_at > %(now)s
  AND c.starts_at < %(now)s + interval '24 hours'
ORDER BY c.starts_at, c.ends_at
LIMIT 20
"""
_FOCUS = """
SELECT content, created_at FROM docs
WHERE user_id = %s AND kind = 'focus'
ORDER BY version DESC LIMIT 1
"""
_TIMEZONE = "SELECT timezone FROM users WHERE id = %s"

ATTRIBUTES_TYPE = "NextUpAttributes"  # the Swift ActivityAttributes type name
UNREGISTERED = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}


async def next_up(conn, user_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """The content-state the card should show right now.

    Keys match the Swift `NextUpAttributes.ContentState` (default Codable):
    title ("" = all clear), due (unix seconds or null), overdue, kind
    ('commitment' | 'deadline'), more (other items due within 7 days,
    overdue included).
    """
    now = now or datetime.now(timezone.utc)
    return _deadline_card(await _deadlines(conn, user_id, now), now)


async def _deadlines(conn, user_id: str, now: datetime) -> list[dict[str, Any]]:
    cur = await conn.execute(
        _CANDIDATES,
        {"user_id": user_id, "now": now, "calendars": DEADLINE_CALENDARS},
    )
    return await cur.fetchall()


def _deadline_card(rows: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    if not rows:
        return {"title": "", "due": None, "overdue": False, "kind": None, "more": 0}
    first, rest = rows[0], rows[1:]
    horizon = now + MORE_WINDOW
    return {
        "title": first["title"],
        "due": first["due_at"].timestamp(),
        "overdue": first["due_at"] < now,
        "kind": first["kind"],
        "more": sum(1 for r in rest if r["due_at"] <= horizon),
    }


async def now_next(conn, user_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """The full content-state: NOW / NEXT, plus the legacy deadline fields.

    NOW is what to do this minute: the class/meeting in progress, else the
    model's focus for today, else the most pressing deadline. NEXT is what to
    do after it: an event only if it's genuinely next (back-to-back after the
    current one, or later today within NEXT_EVENT_WINDOW when free), else the
    focus / deadline to work on. Tomorrow's lecture is never NEXT (user
    decision 2026-09-23: it serves no purpose on the lock screen).

    Deadlines are ranked upcoming-first: an old overdue chore stays on the
    Today tab and in the model's nags, but it doesn't lead the card while
    something is still winnable.

    The legacy keys (title/due/overdue/kind/more) stay so an older app build
    keeps decoding. Keys match Swift `NextUpAttributes.ContentState`.
    """
    now = now or datetime.now(timezone.utc)
    deadlines = await _deadlines(conn, user_id, now)
    state = _deadline_card(deadlines, now)
    ranked = [d for d in deadlines if d["due_at"] >= now] + [
        d for d in deadlines if d["due_at"] < now
    ]

    cur = await conn.execute(_EVENTS, {"user_id": user_id, "now": now})
    events = await cur.fetchall()
    current = next((e for e in events if e["starts_at"] <= now), None)

    cur = await conn.execute(_TIMEZONE, (user_id,))
    tz = ZoneInfo((await cur.fetchone())["timezone"])
    today = now.astimezone(tz).date()
    cur = await conn.execute(_FOCUS, (user_id,))
    focus = await cur.fetchone()
    if focus and focus["created_at"].astimezone(tz).date() != today:
        focus = None  # lapses at the user's midnight

    # Work items in order: the focus, then deadlines. NOW takes the first when
    # no event is on; NEXT takes the first one NOW didn't.
    work: list[dict[str, Any]] = []
    if focus is not None:
        work.append({"kind": "focus", "title": focus["content"], "due": None})
    work.extend(
        {"kind": "deadline", "title": d["title"], "due": d["due_at"]} for d in ranked
    )

    if current is not None:
        state.update(
            nowKind="event", nowTitle=current["title"],
            nowDetail=current["location"], nowUntil=current["ends_at"].timestamp(),
            nowDue=None, nowOverdue=False,
        )
        anchor, window = current["ends_at"], BACK_TO_BACK_GAP
    else:
        item = work.pop(0) if work else None
        if item is None:
            state.update(nowKind="clear", nowTitle="", nowDue=None, nowOverdue=False)
        else:
            state.update(
                nowKind=item["kind"], nowTitle=item["title"],
                nowDue=item["due"].timestamp() if item["due"] else None,
                nowOverdue=bool(item["due"] and item["due"] < now),
            )
        state.update(nowDetail=None, nowUntil=None)
        anchor, window = now, NEXT_EVENT_WINDOW

    upcoming = next(
        (
            e for e in events
            if e["starts_at"] > now
            and e["starts_at"] <= anchor + window
            and e["starts_at"].astimezone(tz).date() == today
        ),
        None,
    )
    if upcoming is not None:
        state.update(
            nextKind="event", nextTitle=upcoming["title"],
            nextAt=upcoming["starts_at"].timestamp(), nextDetail=upcoming["location"],
        )
    elif work:
        item = work[0]
        state.update(
            nextKind=item["kind"], nextTitle=item["title"],
            nextAt=item["due"].timestamp() if item["due"] else None, nextDetail=None,
        )
    else:
        state.update(nextKind=None, nextTitle=None, nextAt=None, nextDetail=None)
    return state


def _payload(event: str, content: dict[str, Any], *, now: float) -> dict[str, Any]:
    aps: dict[str, Any] = {
        "timestamp": int(now),
        "event": event,
        "content-state": content,
    }
    if event == "start":
        aps["attributes-type"] = ATTRIBUTES_TYPE
        aps["attributes"] = {}
        # iOS 18+: have the system mint an update token for the new activity
        # and wake the app to report it.
        aps["input-push-token"] = 1
        # A soundless alert: starts are the only pushes that light the screen.
        title = content.get("nowTitle") or content["title"] or "All clear"
        aps["alert"] = {"title": "Now", "body": title}
    elif event == "end":
        aps["dismissal-date"] = int(now)
    return {"aps": aps}


class LiveActivitySync:
    def __init__(
        self,
        db: Database,
        user_ids: list[str],
        apns: Any,
        *,
        poll_seconds: float = POLL_SECONDS,
        clock=time.time,
    ):
        self.db = db
        self.user_ids = list(user_ids)
        self.apns = apns
        self.poll_seconds = poll_seconds
        self._clock = clock
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            for user_id in self.user_ids:
                try:
                    await self.sync_user(user_id)
                except Exception:
                    log.exception("live activity pass failed for %s", user_id)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass
            finally:
                self._wake.clear()

    async def sync_user(self, user_id: str) -> str:
        """One pass for one user; returns what it did (for logs and tests)."""
        now = self._clock()
        now_dt = datetime.fromtimestamp(now, timezone.utc)
        async with self.db.with_tenant(user_id) as conn:
            row = await la_q.get(conn, user_id)
            if row is None or not row["start_token"]:
                return "disabled"
            content = await now_next(conn, user_id, now=now_dt)

        changed = content != row["content"]
        started_at = row["started_at"]
        age = now_dt - started_at if started_at else None
        need_start = (
            age is None
            or age >= RESTART_AFTER
            or (changed and not row["update_token"] and age >= ORPHAN_AFTER)
        )

        if need_start:
            if row["update_token"]:
                await self._push(user_id, row, "end", content, now=now)
            status = await self._push(user_id, row, "start", content, now=now)
            if status != 200:
                return f"start failed ({status})"
            async with self.db.with_tenant(user_id) as conn:
                await la_q.mark_started(conn, user_id, content)
            log.info("live activity started for %s: %r", user_id, content["title"])
            return "started"

        if not changed:
            return "unchanged"
        if not row["update_token"]:
            return "waiting for token"
        status = await self._push(user_id, row, "update", content, now=now)
        if status != 200:
            return f"update failed ({status})"
        async with self.db.with_tenant(user_id) as conn:
            await la_q.mark_updated(conn, user_id, content)
        return "updated"

    async def _push(
        self, user_id: str, row: dict[str, Any], event: str,
        content: dict[str, Any], *, now: float,
    ) -> int:
        token = row["start_token"] if event == "start" else row["update_token"]
        status, reason = await self.apns.push_live_activity(
            token=token,
            environment=row["environment"],
            payload=_payload(event, content, now=now),
        )
        if status == 200:
            return status
        log.warning("live activity %s for %s: %s %s", event, user_id, status, reason)
        if status == 410 or (status == 400 and reason in UNREGISTERED):
            async with self.db.with_tenant(user_id) as conn:
                if event == "start":
                    await la_q.drop_start_token(conn, user_id)
                else:
                    # The activity is gone; forget it so the next pass restarts.
                    await la_q.forget_activity(conn, user_id)
        return status
