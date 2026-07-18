"""SQL for the triggers inbox (§4.2)."""

from typing import Any

from psycopg import AsyncConnection

# Insert a trigger, deduplicating on (user_id, idempotency_key) and coalescing
# calendar hints in one statement.
#
# The status CASE implements §4.2's coalescing rule: when a pending calendar
# trigger already exists for the same calendar, the newcomer is still recorded
# but lands as 'coalesced' so the worker never picks it up. Known benign race:
# under READ COMMITTED two concurrent inserts can each miss the other's
# uncommitted row and both land 'pending', costing one redundant turn. Closing
# it would need a partial unique index on (user_id, source_ref) WHERE
# source='calendar' AND status='pending'; not worth the write-path contention
# for a duplicate sync that is itself idempotent.
_INSERT = """
INSERT INTO triggers (
  user_id, source, source_ref, idempotency_key, payload, priority, status
)
VALUES (
  %(user_id)s, %(source)s, %(source_ref)s, %(idempotency_key)s, %(payload)s,
  %(priority)s,
  CASE
    WHEN %(source)s = 'calendar' AND EXISTS (
      SELECT 1 FROM triggers
      WHERE user_id = %(user_id)s
        AND source = 'calendar'
        AND source_ref IS NOT DISTINCT FROM %(source_ref)s
        AND status = 'pending'
    ) THEN 'coalesced'
    ELSE 'pending'
  END
)
ON CONFLICT (user_id, idempotency_key) DO NOTHING
RETURNING id, status
"""

_BY_IDEMPOTENCY_KEY = """
SELECT id, status FROM triggers
WHERE user_id = %s AND idempotency_key = %s
"""

# §5.2, normative. SKIP LOCKED lets concurrent workers race without blocking:
# losers skip the locked row rather than queue behind it. ORDER BY priority,
# received_at makes chat (priority 1) jump maintenance (4), FIFO within a tier.
#
# The NOT EXISTS clause is §5.2's "one in-flight turn per user, ever": no claim
# while a turn row is still 'running'. It deliberately ignores lease_until, so
# an expired-but-unreaped turn keeps holding the slot and the worker must reap
# before it can claim — that ordering is what makes the handoff explicit rather
# than racy.
#
# LIMIT: this is a guard, not a mutex. Two workers in separate transactions can
# both observe NOT EXISTS as true and both open a turn. Closing that needs a
# lock we cannot take — advisory locks are banned (§3) and row locks on `users`
# need an UPDATE grant runtime_role does not have. In the deployed shape (one
# systemd unit, one worker task per user) the race needs two overlapping
# processes to occur.
_CLAIM = """
UPDATE triggers SET status = 'running'
WHERE id = (
  SELECT id FROM triggers
  WHERE user_id = %(user_id)s AND status = 'pending'
    AND NOT (source = 'calendar' AND payload->>'kind' = 'hint')
  ORDER BY priority, received_at
  FOR UPDATE SKIP LOCKED LIMIT 1)
  AND NOT EXISTS (
    SELECT 1 FROM turns WHERE user_id = %(user_id)s AND status = 'running')
RETURNING *
"""

# §12: webhook hints are sync work, not turns — the calendar engine claims
# them (excluded from the worker's _CLAIM above); everything else about the
# trigger machinery (coalescing, idempotency, audit trail) is shared.
_CLAIM_HINT = """
UPDATE triggers SET status = 'running'
WHERE id = (
  SELECT id FROM triggers
  WHERE status = 'pending'
    AND source = 'calendar' AND payload->>'kind' = 'hint'
  ORDER BY received_at
  FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING *
"""

_SET_STATUS = """
UPDATE triggers SET status = %s, turn_id = COALESCE(%s, turn_id)
WHERE id = %s
"""

_REQUEUE = """
UPDATE triggers SET status = 'pending'
WHERE id = %s
RETURNING id
"""


async def insert(
    conn: AsyncConnection,
    *,
    user_id: str,
    source: str,
    source_ref: str | None,
    idempotency_key: str,
    payload: str,
    priority: int,
) -> dict[str, Any] | None:
    """Insert a trigger; None means the idempotency key already existed."""
    cur = await conn.execute(
        _INSERT,
        {
            "user_id": user_id,
            "source": source,
            "source_ref": source_ref,
            "idempotency_key": idempotency_key,
            "payload": payload,
            "priority": priority,
        },
    )
    return await cur.fetchone()


async def by_idempotency_key(
    conn: AsyncConnection, user_id: str, idempotency_key: str
) -> dict[str, Any] | None:
    cur = await conn.execute(_BY_IDEMPOTENCY_KEY, (user_id, idempotency_key))
    return await cur.fetchone()


async def claim_next(conn: AsyncConnection, user_id: str) -> dict[str, Any] | None:
    """Claim the highest-priority pending trigger.

    None means either an empty inbox or a turn already in flight for this user.
    """
    cur = await conn.execute(_CLAIM, {"user_id": user_id})
    return await cur.fetchone()


async def claim_calendar_hint(conn: AsyncConnection) -> dict[str, Any] | None:
    """Claim the oldest pending webhook hint (tenant-scoped by RLS)."""
    cur = await conn.execute(_CLAIM_HINT)
    return await cur.fetchone()


async def set_status(
    conn: AsyncConnection, trigger_id: int, status: str, turn_id: int | None = None
) -> None:
    await conn.execute(_SET_STATUS, (status, turn_id, trigger_id))


async def requeue(conn: AsyncConnection, trigger_id: int) -> None:
    await conn.execute(_REQUEUE, (trigger_id,))


async def turn_id_for(conn: AsyncConnection, trigger_id: int) -> int | None:
    """The turn this trigger was claimed into, or None if still pending."""
    cur = await conn.execute("SELECT turn_id FROM triggers WHERE id = %s", (trigger_id,))
    row = await cur.fetchone()
    return int(row["turn_id"]) if row and row["turn_id"] is not None else None


async def pending_count(conn: AsyncConnection, user_id: str) -> int:
    cur = await conn.execute(
        "SELECT count(*) AS n FROM triggers WHERE user_id = %s AND status = 'pending'",
        (user_id,),
    )
    row = await cur.fetchone()
    return int(row["n"]) if row else 0
