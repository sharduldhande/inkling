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

_ACTIVE = "SELECT * FROM scheduled_wakeups WHERE status = 'active' ORDER BY id"

# The status guard makes firing a compare-and-set: two schedulers (or a fire
# racing a cancel) resolve to exactly one winner, and the loser sees None.
_MARK_FIRED = """
UPDATE scheduled_wakeups
SET status = 'fired'
WHERE id = %s AND status = 'active'
RETURNING *
"""

# Advancing a recurring wakeup guards on the fire_at it advanced *from*
# (IS NOT DISTINCT FROM, so materializing a NULL fire_at uses the same path):
# a concurrent advance already moved it, and this one is a no-op.
_ADVANCE = """
UPDATE scheduled_wakeups
SET fire_at = %s
WHERE id = %s AND status = 'active' AND fire_at IS NOT DISTINCT FROM %s
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


async def active(conn: AsyncConnection) -> list[dict[str, Any]]:
    cur = await conn.execute(_ACTIVE)
    return await cur.fetchall()


async def mark_fired(conn: AsyncConnection, wakeup_id: int) -> dict[str, Any] | None:
    """None means someone else already fired or cancelled it."""
    cur = await conn.execute(_MARK_FIRED, (wakeup_id,))
    return await cur.fetchone()


async def advance(
    conn: AsyncConnection, wakeup_id: int, *, old_fire_at: Any, new_fire_at: Any
) -> dict[str, Any] | None:
    """None means fire_at already moved (or the wakeup died) under us."""
    cur = await conn.execute(_ADVANCE, (new_fire_at, wakeup_id, old_fire_at))
    return await cur.fetchone()
