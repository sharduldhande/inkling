"""SQL for turns and turn_events (§4.3)."""

import json
from typing import Any

from psycopg import AsyncConnection

_CREATE = """
INSERT INTO turns (user_id, trigger_id, status, leased_by, lease_until)
VALUES (%s, %s, 'running', %s, now() + make_interval(secs => %s))
RETURNING *
"""

# Heartbeat extends the lease only while this worker still holds it. The
# leased_by guard means a worker whose lease was already reaped cannot revive
# it and start double-writing alongside the replacement (§5.2).
_HEARTBEAT = """
UPDATE turns SET lease_until = now() + make_interval(secs => %s)
WHERE id = %s AND leased_by = %s AND status = 'running'
RETURNING id
"""

_FINISH = """
UPDATE turns
SET status = %s, finished_at = now(), error = %s,
    model = COALESCE(%s, model),
    input_tokens = COALESCE(%s, input_tokens),
    output_tokens = COALESCE(%s, output_tokens)
WHERE id = %s
RETURNING id
"""

_EXPIRED = """
SELECT t.*, g.source AS trigger_source
FROM turns t JOIN triggers g ON g.id = t.trigger_id
WHERE t.status = 'running' AND t.lease_until < now()
"""

_RUNNING_FOR_USER = """
SELECT id FROM turns
WHERE user_id = %s AND status = 'running' AND lease_until >= now()
LIMIT 1
"""

# The /chat busy check (§5.1). Excludes the caller's own trigger: the worker
# can claim a just-submitted trigger before the route gets to this check, and
# a request must not be told "busy" because of the very turn it started.
_RUNNING_FOR_OTHER_TRIGGER = """
SELECT id FROM turns
WHERE user_id = %s AND status = 'running' AND lease_until >= now()
  AND trigger_id <> %s
LIMIT 1
"""

# Attempts are counted from turns rows rather than a triggers.attempts column:
# every claim creates exactly one turn, so COUNT(*) is the attempt number and
# §4.2's DDL stays as written.
_ATTEMPTS = """
SELECT count(*) AS n FROM turns WHERE trigger_id = %s
"""

# Presence pacing (§10 presence): the clock resets at the end of any turn,
# whatever its trigger — a chat exchange, a fired reminder, or a presence
# check-in all count as contact.
_LAST_FINISHED = """
SELECT max(finished_at) AS ts FROM turns
WHERE user_id = %s AND finished_at IS NOT NULL
"""

_APPEND_EVENT = """
INSERT INTO turn_events (user_id, turn_id, seq, kind, data)
VALUES (%s, %s, %s, %s, %s)
RETURNING id, seq
"""

_EVENTS_SINCE = """
SELECT seq, kind, data FROM turn_events
WHERE turn_id = %s AND seq > %s
ORDER BY seq
"""


async def create(
    conn: AsyncConnection,
    *,
    user_id: str,
    trigger_id: int,
    leased_by: str,
    lease_seconds: int,
) -> dict[str, Any]:
    cur = await conn.execute(_CREATE, (user_id, trigger_id, leased_by, lease_seconds))
    row = await cur.fetchone()
    assert row is not None  # INSERT ... RETURNING always yields a row
    return row


async def heartbeat(
    conn: AsyncConnection, turn_id: int, leased_by: str, lease_seconds: int
) -> bool:
    """Extend the lease. False means we no longer hold it — stop working."""
    cur = await conn.execute(_HEARTBEAT, (lease_seconds, turn_id, leased_by))
    return await cur.fetchone() is not None


async def finish(
    conn: AsyncConnection,
    turn_id: int,
    *,
    status: str,
    error: str | None = None,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    await conn.execute(
        _FINISH, (status, error, model, input_tokens, output_tokens, turn_id)
    )


async def expired(conn: AsyncConnection) -> list[dict[str, Any]]:
    """Running turns whose lease has lapsed — RLS scopes this to the tenant."""
    cur = await conn.execute(_EXPIRED)
    return await cur.fetchall()


async def running_for_user(conn: AsyncConnection, user_id: str) -> int | None:
    cur = await conn.execute(_RUNNING_FOR_USER, (user_id,))
    row = await cur.fetchone()
    return int(row["id"]) if row else None


async def running_for_other_trigger(
    conn: AsyncConnection, user_id: str, trigger_id: int
) -> int | None:
    cur = await conn.execute(_RUNNING_FOR_OTHER_TRIGGER, (user_id, trigger_id))
    row = await cur.fetchone()
    return int(row["id"]) if row else None


async def attempts(conn: AsyncConnection, trigger_id: int) -> int:
    cur = await conn.execute(_ATTEMPTS, (trigger_id,))
    row = await cur.fetchone()
    return int(row["n"]) if row else 0


async def last_finished_at(conn: AsyncConnection, user_id: str) -> Any | None:
    """When this user's most recent turn finished, or None if none ever has."""
    cur = await conn.execute(_LAST_FINISHED, (user_id,))
    row = await cur.fetchone()
    return row["ts"] if row else None


async def append_event(
    conn: AsyncConnection,
    *,
    user_id: str,
    turn_id: int,
    seq: int,
    kind: str,
    data: dict[str, Any],
) -> None:
    await conn.execute(_APPEND_EVENT, (user_id, turn_id, seq, kind, json.dumps(data)))


async def events_since(
    conn: AsyncConnection, turn_id: int, seq: int
) -> list[dict[str, Any]]:
    cur = await conn.execute(_EVENTS_SINCE, (turn_id, seq))
    return await cur.fetchall()
