"""SQL for calendar feed state and the events cache (§4.10, driven by §12).

Read-only feature: every statement here is SELECT/INSERT/UPDATE on our own
mirror tables. Nothing in this module (or anywhere) talks back to a calendar.
"""

import json
from typing import Any

from psycopg import AsyncConnection

_STATES = "SELECT * FROM calendar_sync_state ORDER BY calendar_id"

_ENABLED_STATES = """
SELECT * FROM calendar_sync_state WHERE enabled ORDER BY calendar_id
"""

_UPSERT_STATE = """
INSERT INTO calendar_sync_state (user_id, provider, calendar_id, summary, enabled)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (user_id, provider, calendar_id) DO UPDATE SET
  summary = EXCLUDED.summary,
  enabled = EXCLUDED.enabled
"""

_DISABLE_MISSING = """
UPDATE calendar_sync_state SET enabled = false
WHERE user_id = %s AND provider = %s AND enabled
  AND NOT (calendar_id = ANY(%s))
RETURNING calendar_id
"""

_SET_CURSOR = """
UPDATE calendar_sync_state
SET sync_cursor = %s, last_synced_at = now()
WHERE user_id = %s AND provider = %s AND calendar_id = %s
"""

_CACHE_GET = """
SELECT * FROM calendar_events_cache
WHERE provider_event_id = %s AND calendar_id = %s
"""

_CACHE_UPSERT = """
INSERT INTO calendar_events_cache
  (user_id, provider_event_id, calendar_id, revision, starts_at, ends_at,
   summary, status, all_day, raw, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (user_id, provider_event_id) DO UPDATE SET
  revision = EXCLUDED.revision,
  starts_at = EXCLUDED.starts_at,
  ends_at = EXCLUDED.ends_at,
  summary = EXCLUDED.summary,
  status = EXCLUDED.status,
  all_day = EXCLUDED.all_day,
  raw = EXCLUDED.raw,
  updated_at = now()
RETURNING *
"""

# Snapshot semantics: an event inside the fetched window that the feed no
# longer lists is gone. Tombstone it (status), never delete it (D10).
_CACHE_CANCEL_MISSING = """
UPDATE calendar_events_cache
SET status = 'cancelled', updated_at = now()
WHERE calendar_id = %s
  AND status <> 'cancelled'
  AND starts_at IS NOT NULL
  AND starts_at >= %s AND starts_at < %s
  AND NOT (provider_event_id = ANY(%s))
RETURNING *
"""

_UPCOMING = """
SELECT
  c.provider_event_id, c.calendar_id, c.summary, c.starts_at, c.ends_at,
  c.all_day, s.summary AS calendar_summary
FROM calendar_events_cache c
LEFT JOIN calendar_sync_state s
  ON s.user_id = c.user_id AND s.calendar_id = c.calendar_id
WHERE c.status <> 'cancelled'
  AND coalesce(s.enabled, true)
  AND c.starts_at IS NOT NULL
  AND coalesce(c.ends_at, c.starts_at) >= %s
  AND c.starts_at <= %s
ORDER BY c.starts_at, c.provider_event_id
LIMIT %s
"""


async def states(conn: AsyncConnection) -> list[dict[str, Any]]:
    cur = await conn.execute(_STATES)
    return await cur.fetchall()


async def enabled_states(conn: AsyncConnection) -> list[dict[str, Any]]:
    cur = await conn.execute(_ENABLED_STATES)
    return await cur.fetchall()


async def upsert_state(
    conn: AsyncConnection,
    *,
    user_id: str,
    provider: str,
    calendar_id: str,
    summary: str | None,
    enabled: bool = True,
) -> None:
    """Register a feed; never touches the cursor of an existing row."""
    await conn.execute(
        _UPSERT_STATE, (user_id, provider, calendar_id, summary, enabled)
    )


async def disable_missing(
    conn: AsyncConnection, *, user_id: str, provider: str, keep_ids: list[str]
) -> list[str]:
    cur = await conn.execute(_DISABLE_MISSING, (user_id, provider, keep_ids))
    return [row["calendar_id"] for row in await cur.fetchall()]


async def set_cursor(
    conn: AsyncConnection,
    *,
    user_id: str,
    provider: str,
    calendar_id: str,
    cursor: str | None,
) -> None:
    await conn.execute(_SET_CURSOR, (cursor, user_id, provider, calendar_id))


async def cache_get(
    conn: AsyncConnection, *, provider_event_id: str, calendar_id: str
) -> dict[str, Any] | None:
    cur = await conn.execute(_CACHE_GET, (provider_event_id, calendar_id))
    return await cur.fetchone()


async def cache_upsert(
    conn: AsyncConnection,
    *,
    user_id: str,
    provider_event_id: str,
    calendar_id: str,
    revision: str | None,
    starts_at: Any,
    ends_at: Any,
    summary: str | None,
    status: str,
    all_day: bool,
    raw: dict[str, Any],
) -> dict[str, Any]:
    cur = await conn.execute(
        _CACHE_UPSERT,
        (
            user_id,
            provider_event_id,
            calendar_id,
            revision,
            starts_at,
            ends_at,
            summary,
            status,
            all_day,
            json.dumps(raw),
        ),
    )
    row = await cur.fetchone()
    assert row is not None
    return row


async def cache_cancel_missing(
    conn: AsyncConnection,
    *,
    calendar_id: str,
    window_start: Any,
    window_end: Any,
    keep_ids: list[str],
) -> list[dict[str, Any]]:
    """Tombstone cached events the snapshot no longer lists; returns them."""
    cur = await conn.execute(
        _CACHE_CANCEL_MISSING, (calendar_id, window_start, window_end, keep_ids)
    )
    return await cur.fetchall()


async def upcoming(
    conn: AsyncConnection, *, start: Any, end: Any, limit: int
) -> list[dict[str, Any]]:
    """Live (non-cancelled) events overlapping [start, end], soonest first."""
    cur = await conn.execute(_UPCOMING, (start, end, limit))
    return await cur.fetchall()
