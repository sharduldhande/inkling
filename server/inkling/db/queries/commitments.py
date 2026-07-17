"""SQL for commitments and their occurrences (§4.6).

Optimistic concurrency lives in the WHERE clauses here: every mutating
statement carries `version = %s` and RETURNs the row. No row back means the
version moved under us, and the caller rejects the command with the current
version rather than clobbering a concurrent write (§9.2).
"""

import json
from typing import Any

from psycopg import AsyncConnection

_GET = "SELECT * FROM commitments WHERE id = %s"

# The casts are load-bearing: an omitted optional field arrives as an untyped
# NULL, which COALESCE can't resolve a type for on its own.
_INSERT = """
INSERT INTO commitments
  (user_id, title, body, due_at, recurrence, lead_time, source, source_ref,
   notify_policy)
VALUES (%s, %s, %s, %s::timestamptz, %s, COALESCE(%s::interval, '0'::interval),
        %s, %s,
        COALESCE(%s::jsonb,
                 '{"repeat_after_min":15,"escalate_after_min":45,
                   "stop_on_ack":true,"quiet_hours":"defer"}'::jsonb))
RETURNING *
"""

# COALESCE(%s, col) leaves an omitted field untouched: the model sends the
# fields it means to change, not a full row it may have half-remembered.
_UPDATE = """
UPDATE commitments
SET title = %s,
    body = COALESCE(%s, body),
    due_at = COALESCE(%s::timestamptz, due_at),
    recurrence = COALESCE(%s, recurrence),
    lead_time = COALESCE(%s::interval, lead_time),
    notify_policy = COALESCE(%s::jsonb, notify_policy),
    version = version + 1,
    updated_at = now()
WHERE id = %s AND version = %s
RETURNING *
"""

_SET_STATUS = """
UPDATE commitments
SET status = %s, version = version + 1, updated_at = now()
WHERE id = %s AND version = %s
RETURNING *
"""

_OPEN_WITH_TITLE = """
SELECT * FROM commitments
WHERE status = 'open' AND lower(title) = lower(%s)
LIMIT 1
"""

# Snooze moves the reminder, not the commitment: pending occurrences are the
# thing that fires (§4.6), so pushing their fire_at is what "later" means.
_RESCHEDULE_PENDING = """
UPDATE commitment_occurrences
SET fire_at = %s
WHERE commitment_id = %s AND status = 'pending'
RETURNING id
"""

_INSERT_OCCURRENCE = """
INSERT INTO commitment_occurrences (user_id, commitment_id, fire_at)
VALUES (%s, %s, %s)
ON CONFLICT (commitment_id, fire_at) DO NOTHING
RETURNING id
"""


async def get(conn: AsyncConnection, commitment_id: int) -> dict[str, Any] | None:
    """RLS scopes this to the tenant: another user's id simply returns None."""
    cur = await conn.execute(_GET, (commitment_id,))
    return await cur.fetchone()


async def open_with_title(conn: AsyncConnection, title: str) -> dict[str, Any] | None:
    cur = await conn.execute(_OPEN_WITH_TITLE, (title,))
    return await cur.fetchone()


async def insert(
    conn: AsyncConnection,
    *,
    user_id: str,
    title: str,
    body: str | None,
    due_at: Any,
    recurrence: str | None,
    lead_time: Any,
    source: str,
    source_ref: str | None,
    notify_policy: dict[str, Any] | None,
) -> dict[str, Any]:
    cur = await conn.execute(
        _INSERT,
        (
            user_id,
            title,
            body,
            due_at,
            recurrence,
            lead_time,
            source,
            source_ref,
            json.dumps(notify_policy) if notify_policy is not None else None,
        ),
    )
    row = await cur.fetchone()
    assert row is not None  # INSERT ... RETURNING always yields a row
    return row


async def update(
    conn: AsyncConnection,
    *,
    commitment_id: int,
    version: int,
    title: str,
    body: str | None,
    due_at: Any,
    recurrence: str | None,
    lead_time: Any,
    notify_policy: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """None means the version guard failed — the row changed under us."""
    cur = await conn.execute(
        _UPDATE,
        (
            title,
            body,
            due_at,
            recurrence,
            lead_time,
            json.dumps(notify_policy) if notify_policy is not None else None,
            commitment_id,
            version,
        ),
    )
    return await cur.fetchone()


async def set_status(
    conn: AsyncConnection, *, commitment_id: int, version: int, status: str
) -> dict[str, Any] | None:
    cur = await conn.execute(_SET_STATUS, (status, commitment_id, version))
    return await cur.fetchone()


async def reschedule_pending(
    conn: AsyncConnection, *, commitment_id: int, fire_at: Any
) -> int:
    cur = await conn.execute(_RESCHEDULE_PENDING, (fire_at, commitment_id))
    return len(await cur.fetchall())


async def insert_occurrence(
    conn: AsyncConnection, *, user_id: str, commitment_id: int, fire_at: Any
) -> int | None:
    """Idempotent per (commitment, fire_at); None means it already existed."""
    cur = await conn.execute(_INSERT_OCCURRENCE, (user_id, commitment_id, fire_at))
    row = await cur.fetchone()
    return int(row["id"]) if row else None
