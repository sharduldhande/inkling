"""SQL for the "next up" Live Activity row (§11), one per user."""

import json
from typing import Any

from psycopg import AsyncConnection

_GET = "SELECT * FROM live_activity WHERE user_id = %s"

# The app reports what it knows on every launch and whenever iOS hands it a
# token. start_token is only overwritten when sent (a launch that hasn't
# received it yet must not erase it); `enabled = false` (Live Activities off
# in iOS Settings) clears it so the server stops trying.
_UPSERT_START = """
INSERT INTO live_activity (user_id, environment, start_token)
VALUES (%(user_id)s, %(environment)s,
        CASE WHEN %(enabled)s THEN %(start_token)s END)
ON CONFLICT (user_id) DO UPDATE SET
  environment = excluded.environment,
  start_token = CASE
    WHEN NOT %(enabled)s THEN NULL
    ELSE COALESCE(%(start_token)s, live_activity.start_token)
  END,
  updated_at = now()
RETURNING *
"""

# A different activity than the one we last pushed to (the app kept an older
# duplicate): its screen may show stale content, so forget what we sent and
# let the next pass push the current card to it. The first report after a
# start (activity_id NULL) is the activity we just started — nothing stale.
_SET_ACTIVITY = """
UPDATE live_activity
SET content = CASE WHEN activity_id IS NOT NULL
                        AND activity_id <> %(activity_id)s
                   THEN NULL ELSE content END,
    activity_id = %(activity_id)s,
    update_token = %(update_token)s,
    updated_at = now()
WHERE user_id = %(user_id)s
RETURNING *
"""

# The app saw no activity on screen (ended by iOS, or swiped away). Ignored
# for two minutes after a start push: that report may predate the push.
_CLEAR_ACTIVITY = """
UPDATE live_activity
SET activity_id = NULL, update_token = NULL, started_at = NULL,
    updated_at = now()
WHERE user_id = %s
  AND (started_at IS NULL OR started_at < now() - interval '2 minutes')
RETURNING *
"""

# APNs said the activity's token is dead: unlike _CLEAR_ACTIVITY this is
# authoritative, so no grace window.
_FORGET_ACTIVITY = """
UPDATE live_activity
SET activity_id = NULL, update_token = NULL, started_at = NULL,
    updated_at = now()
WHERE user_id = %s
"""

_STARTED = """
UPDATE live_activity
SET started_at = now(), pushed_at = now(), content = %s::jsonb,
    activity_id = NULL, update_token = NULL, updated_at = now()
WHERE user_id = %s
"""

_UPDATED = """
UPDATE live_activity
SET pushed_at = now(), content = %s::jsonb, updated_at = now()
WHERE user_id = %s
"""

_DROP_START_TOKEN = """
UPDATE live_activity SET start_token = NULL, updated_at = now()
WHERE user_id = %s
"""


async def get(conn: AsyncConnection, user_id: str) -> dict[str, Any] | None:
    cur = await conn.execute(_GET, (user_id,))
    return await cur.fetchone()


async def register(
    conn: AsyncConnection,
    *,
    user_id: str,
    environment: str,
    start_token: str | None,
    enabled: bool,
) -> dict[str, Any]:
    cur = await conn.execute(
        _UPSERT_START,
        {
            "user_id": user_id,
            "environment": environment,
            "start_token": start_token,
            "enabled": enabled,
        },
    )
    row = await cur.fetchone()
    assert row is not None
    return row


async def set_activity(
    conn: AsyncConnection, user_id: str, *, activity_id: str, update_token: str
) -> None:
    await conn.execute(
        _SET_ACTIVITY,
        {"activity_id": activity_id, "update_token": update_token, "user_id": user_id},
    )


async def clear_activity(conn: AsyncConnection, user_id: str) -> bool:
    cur = await conn.execute(_CLEAR_ACTIVITY, (user_id,))
    return await cur.fetchone() is not None


async def mark_started(
    conn: AsyncConnection, user_id: str, content: dict[str, Any]
) -> None:
    await conn.execute(_STARTED, (json.dumps(content), user_id))


async def mark_updated(
    conn: AsyncConnection, user_id: str, content: dict[str, Any]
) -> None:
    await conn.execute(_UPDATED, (json.dumps(content), user_id))


async def drop_start_token(conn: AsyncConnection, user_id: str) -> None:
    await conn.execute(_DROP_START_TOKEN, (user_id,))


async def forget_activity(conn: AsyncConnection, user_id: str) -> None:
    await conn.execute(_FORGET_ACTIVITY, (user_id,))
