"""SQL for the delivery outbox (§4.6, drained by §11).

Nothing here sends anything. A row is a promise to attempt delivery after the
turn's transaction commits, so a rollback can never leave a push already sent.
"""

from typing import Any

from psycopg import AsyncConnection

# ON CONFLICT on the idempotency key makes enqueueing safe under retry: a
# replayed turn re-proposes the same key and adds no second notification.
_ENQUEUE = """
INSERT INTO deliveries
  (user_id, occurrence_id, turn_id, channel, destination, body,
   idempotency_key, not_before, expires_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz)
ON CONFLICT (idempotency_key) DO NOTHING
RETURNING *
"""


# §11 drain scan: rows ready to attempt, oldest promise first. No lower bound —
# rows that came due while the process was down are the boot catch-up set.
# channel='healthkit' is excluded the same way calendar hints are excluded from
# the worker's trigger claim: those rows are PULLED by the iOS app, and a
# drainer that claimed one would only dead-letter it (no push channel exists
# for a phone) and page the operator about its own misunderstanding.
_DUE = """
SELECT * FROM deliveries
WHERE status IN ('queued', 'retry_wait')
  AND channel <> 'healthkit'
  AND COALESCE(not_before, created_at) <= %s
ORDER BY COALESCE(not_before, created_at), id
"""

_NEXT_DUE = """
SELECT min(COALESCE(not_before, created_at)) AS next_due
FROM deliveries
WHERE status IN ('queued', 'retry_wait')
  AND channel <> 'healthkit'
"""

# Claiming is a compare-and-set on status, like every §10 fire: two drainers
# (or a drain racing a retry) resolve to one winner.
_CLAIM = """
UPDATE deliveries
SET status = 'leased'
WHERE id = %s AND status IN ('queued', 'retry_wait')
RETURNING *
"""

_MARK_SENT = """
UPDATE deliveries
SET status = 'sent', sent_at = now(), provider_msg_id = %s
WHERE id = %s AND status = 'leased'
RETURNING *
"""

_MARK_RETRY = """
UPDATE deliveries
SET status = 'retry_wait', attempt_count = %s, last_error = %s, not_before = %s
WHERE id = %s AND status = 'leased'
RETURNING *
"""

# Dead-lettering accepts any live status: retries exhaust from 'leased', but
# expiry kills a row before it is ever claimed.
_MARK_DEAD = """
UPDATE deliveries
SET status = 'dead_letter', attempt_count = %s, last_error = %s
WHERE id = %s AND status IN ('queued', 'retry_wait', 'leased')
RETURNING *
"""

# Boot recovery: the schema carries no lease deadline for deliveries, so a
# 'leased' row at drainer start is an orphan of a crash mid-attempt (single
# drainer per deployment — §11). Requeueing risks a duplicate push, which
# at-least-once explicitly accepts.
_RESET_LEASED = """
UPDATE deliveries
SET status = 'queued'
WHERE status = 'leased'
RETURNING id
"""


async def enqueue(
    conn: AsyncConnection,
    *,
    user_id: str,
    destination: str,
    body: str,
    idempotency_key: str,
    occurrence_id: int | None = None,
    turn_id: int | None = None,
    channel: str = "ntfy",  # the column default; user pushes pass "apns"
    not_before: Any = None,
    expires_at: Any = None,
) -> dict[str, Any] | None:
    """None means this key was already queued — the caller already promised it."""
    cur = await conn.execute(
        _ENQUEUE,
        (
            user_id,
            occurrence_id,
            turn_id,
            channel,
            destination,
            body,
            idempotency_key,
            not_before,
            expires_at,
        ),
    )
    return await cur.fetchone()


async def due(conn: AsyncConnection, *, now: Any) -> list[dict[str, Any]]:
    cur = await conn.execute(_DUE, (now,))
    return await cur.fetchall()


async def next_due(conn: AsyncConnection) -> Any | None:
    cur = await conn.execute(_NEXT_DUE)
    row = await cur.fetchone()
    return row["next_due"] if row else None


async def claim(conn: AsyncConnection, delivery_id: int) -> dict[str, Any] | None:
    """None means another drain path already owns or finished it."""
    cur = await conn.execute(_CLAIM, (delivery_id,))
    return await cur.fetchone()


async def mark_sent(
    conn: AsyncConnection, delivery_id: int, *, provider_msg_id: str
) -> dict[str, Any] | None:
    cur = await conn.execute(_MARK_SENT, (provider_msg_id, delivery_id))
    return await cur.fetchone()


async def mark_retry(
    conn: AsyncConnection,
    delivery_id: int,
    *,
    attempt_count: int,
    error: str,
    not_before: Any,
) -> dict[str, Any] | None:
    cur = await conn.execute(
        _MARK_RETRY, (attempt_count, error, not_before, delivery_id)
    )
    return await cur.fetchone()


async def mark_dead(
    conn: AsyncConnection, delivery_id: int, *, attempt_count: int, error: str
) -> dict[str, Any] | None:
    cur = await conn.execute(_MARK_DEAD, (attempt_count, error, delivery_id))
    return await cur.fetchone()


async def reset_leased(conn: AsyncConnection) -> int:
    cur = await conn.execute(_RESET_LEASED)
    return len(await cur.fetchall())
