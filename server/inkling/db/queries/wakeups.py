"""SQL for scheduled_wakeups (§4.9)."""

from typing import Any

from psycopg import AsyncConnection

_GET = "SELECT * FROM scheduled_wakeups WHERE id = %s"

_INSERT = """
INSERT INTO scheduled_wakeups (user_id, fire_at, recurrence, purpose, created_by)
VALUES (%s, %s, %s, %s, %s)
RETURNING *
"""

# Only an active wakeup can be cancelled; cancelling a fired one is a no-op
# rather than a rewrite of history.
_CANCEL = """
UPDATE scheduled_wakeups
SET status = 'cancelled'
WHERE id = %s AND status = 'active'
RETURNING *
"""


async def get(conn: AsyncConnection, wakeup_id: int) -> dict[str, Any] | None:
    """RLS scopes this to the tenant: another user's id returns None."""
    cur = await conn.execute(_GET, (wakeup_id,))
    return await cur.fetchone()


async def insert(
    conn: AsyncConnection,
    *,
    user_id: str,
    fire_at: Any,
    recurrence: str | None,
    purpose: str,
    created_by: str,
) -> dict[str, Any]:
    cur = await conn.execute(_INSERT, (user_id, fire_at, recurrence, purpose, created_by))
    row = await cur.fetchone()
    assert row is not None  # INSERT ... RETURNING always yields a row
    return row


async def cancel(conn: AsyncConnection, wakeup_id: int) -> dict[str, Any] | None:
    cur = await conn.execute(_CANCEL, (wakeup_id,))
    return await cur.fetchone()
