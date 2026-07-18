"""SQL for calendar sync state and the events cache (§4.10, driven by §12)."""

import json
from typing import Any

from psycopg import AsyncConnection

_STATES = "SELECT * FROM calendar_sync_state ORDER BY calendar_id"

_STATE_BY_CHANNEL = """
SELECT * FROM calendar_sync_state WHERE channel_id = %s
"""

_SET_CURSOR = """
UPDATE calendar_sync_state
SET sync_cursor = %s, last_synced_at = now()
WHERE user_id = %s AND provider = %s AND calendar_id = %s
"""

_SET_FULL_RESYNC = """
UPDATE calendar_sync_state
SET sync_cursor = %s, last_synced_at = now(), last_full_resync_at = now()
WHERE user_id = %s AND provider = %s AND calendar_id = %s
"""

_SET_CHANNEL = """
UPDATE calendar_sync_state
SET channel_id = %s, channel_expires_at = %s
WHERE user_id = %s AND provider = %s AND calendar_id = %s
"""

_CACHE_GET = """
SELECT * FROM calendar_events_cache
WHERE provider_event_id = %s AND calendar_id = %s
"""

_CACHE_UPSERT = """
INSERT INTO calendar_events_cache
  (user_id, provider_event_id, calendar_id, revision, starts_at, ends_at,
   summary, created_by_us, raw, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (user_id, provider_event_id) DO UPDATE SET
  revision = EXCLUDED.revision,
  starts_at = EXCLUDED.starts_at,
  ends_at = EXCLUDED.ends_at,
  summary = EXCLUDED.summary,
  raw = EXCLUDED.raw,
  updated_at = now()
RETURNING *
"""


async def states(conn: AsyncConnection) -> list[dict[str, Any]]:
    cur = await conn.execute(_STATES)
    return await cur.fetchall()


async def state_by_channel(
    conn: AsyncConnection, channel_id: str
) -> dict[str, Any] | None:
    cur = await conn.execute(_STATE_BY_CHANNEL, (channel_id,))
    return await cur.fetchone()


async def set_cursor(
    conn: AsyncConnection,
    *,
    user_id: str,
    provider: str,
    calendar_id: str,
    cursor: str | None,
    full_resync: bool = False,
) -> None:
    sql = _SET_FULL_RESYNC if full_resync else _SET_CURSOR
    await conn.execute(sql, (cursor, user_id, provider, calendar_id))


async def set_channel(
    conn: AsyncConnection,
    *,
    user_id: str,
    provider: str,
    calendar_id: str,
    channel_id: str | None,
    expires_at: Any,
) -> None:
    await conn.execute(
        _SET_CHANNEL, (channel_id, expires_at, user_id, provider, calendar_id)
    )


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
    created_by_us: bool,
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
            created_by_us,
            json.dumps(raw),
        ),
    )
    row = await cur.fetchone()
    assert row is not None
    return row
