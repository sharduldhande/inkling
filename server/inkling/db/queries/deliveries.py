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


async def enqueue(
    conn: AsyncConnection,
    *,
    user_id: str,
    destination: str,
    body: str,
    idempotency_key: str,
    occurrence_id: int | None = None,
    turn_id: int | None = None,
    channel: str = "ntfy",
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
