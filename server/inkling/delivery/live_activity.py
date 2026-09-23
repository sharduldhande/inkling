"""The "next up" Live Activity (§11): a persistent lock-screen card.

The server owns the card. Every pass it derives what the card should say
(the earliest-due open one-shot commitment or deadline-calendar item, e.g. a
Canvas assignment) and pushes only when that changed. Recurring habits are
deliberately absent: they're pushable, deadlines aren't.

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

from inkling.db.client import Database
from inkling.db.queries import live_activity as la_q

log = logging.getLogger(__name__)

POLL_SECONDS = 60
RESTART_AFTER = timedelta(hours=7, minutes=45)  # iOS ends activities at 8h
ORPHAN_AFTER = timedelta(minutes=10)
MORE_WINDOW = timedelta(days=7)
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
    cur = await conn.execute(
        _CANDIDATES,
        {"user_id": user_id, "now": now, "calendars": DEADLINE_CALENDARS},
    )
    rows = await cur.fetchall()
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
        title = content["title"] or "All clear"
        aps["alert"] = {"title": "Next up", "body": title}
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
            content = await next_up(conn, user_id, now=now_dt)

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
