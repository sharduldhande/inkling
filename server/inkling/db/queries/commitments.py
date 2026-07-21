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

# §8.1 section 7: the lead-time window. A commitment is "due/upcoming" when its
# next live occurrence (fire_at = due_at - lead_time) or its due_at falls before
# the horizon — which includes everything overdue. Undated open commitments are
# open promises with nothing to fire, so they surface here too, after the dated
# ones; the horizon can't apply to them.
_UPCOMING = """
SELECT c.id, c.title, c.body, c.due_at, c.recurrence, c.status, c.version,
       o.fire_at AS next_fire_at, o.status AS occurrence_status
FROM commitments c
LEFT JOIN LATERAL (
  SELECT fire_at, status FROM commitment_occurrences
  WHERE commitment_id = c.id AND status IN ('pending', 'eligible', 'leased')
  ORDER BY fire_at
  LIMIT 1
) o ON true
WHERE c.status = 'open'
  AND (o.fire_at <= %s OR c.due_at <= %s
       OR (o.fire_at IS NULL AND c.due_at IS NULL))
ORDER BY COALESCE(o.fire_at, c.due_at) ASC NULLS LAST, c.id
LIMIT %s
"""

_INSERT_OCCURRENCE = """
INSERT INTO commitment_occurrences (user_id, commitment_id, fire_at)
VALUES (%s, %s, %s)
ON CONFLICT (commitment_id, fire_at) DO NOTHING
RETURNING id
"""

# §10 heap load: pending occurrences up to the horizon, which has no lower
# bound — past-due rows are exactly the catch-up-on-boot set. Closed
# commitments are excluded here as a belt (their occurrences are also marked
# skipped_done at completion).
_PENDING_OCCURRENCES = """
SELECT o.id, o.commitment_id, o.fire_at, c.title, c.due_at, c.notify_policy
FROM commitment_occurrences o
JOIN commitments c ON c.id = o.commitment_id
WHERE o.status = 'pending' AND o.fire_at <= %s AND c.status = 'open'
ORDER BY o.fire_at, o.id
"""

# §10 status machine, each transition a compare-and-set like wakeups.
_MARK_ELIGIBLE = """
UPDATE commitment_occurrences
SET status = 'eligible'
WHERE id = %s AND status = 'pending'
RETURNING *
"""

_MARK_SURFACED = """
UPDATE commitment_occurrences
SET status = 'surfaced', surfaced_turn = %s
WHERE id = %s AND status = 'eligible'
RETURNING *
"""

# Completing a commitment retires its un-surfaced occurrences: a reminder for
# a thing already done is noise, not diligence (§4.6 skipped_done).
_SKIP_UNSURFACED = """
UPDATE commitment_occurrences
SET status = 'skipped_done'
WHERE commitment_id = %s AND status IN ('pending', 'eligible')
RETURNING id
"""

# A habit_done/habit_skipped event naming a recurring commitment retires that
# LOCAL day's un-surfaced occurrence so the reminder for a thing already done
# never fires. Local-day, not a rolling window: "done" at 11pm must not eat
# tomorrow morning's occurrence.
_SKIP_SAME_DAY = """
UPDATE commitment_occurrences
SET status = 'skipped_done'
WHERE commitment_id = %s
  AND status IN ('pending', 'eligible')
  AND (fire_at AT TIME ZONE %s)::date
      = (%s::timestamptz AT TIME ZONE %s)::date
RETURNING id
"""

# §11 acknowledgement heuristic: any chat turn after a reminder surfaced marks
# that user's un-acknowledged surfaced occurrences acknowledged. Deliberately
# coarse — the v1 app has no ack channel, and the user talking to us at all is
# the signal escalation cares about.
_ACKNOWLEDGE_SURFACED = """
UPDATE commitment_occurrences
SET acknowledged_at = now()
WHERE status = 'surfaced' AND acknowledged_at IS NULL
RETURNING id
"""

# §10 materialization: every open commitment that can fire (dated or
# recurring); recurrence expansion happens in Python (dateutil).
_OPEN_SCHEDULED = """
SELECT * FROM commitments
WHERE status = 'open' AND (due_at IS NOT NULL OR recurrence IS NOT NULL)
ORDER BY id
"""

_OPEN_ALL = "SELECT * FROM commitments WHERE status = 'open' ORDER BY id"


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


async def upcoming(
    conn: AsyncConnection, *, horizon: Any, limit: int
) -> list[dict[str, Any]]:
    cur = await conn.execute(_UPCOMING, (horizon, horizon, limit))
    return await cur.fetchall()


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


async def pending_occurrences(
    conn: AsyncConnection, *, horizon: Any
) -> list[dict[str, Any]]:
    cur = await conn.execute(_PENDING_OCCURRENCES, (horizon,))
    return await cur.fetchall()


async def mark_occurrence_eligible(
    conn: AsyncConnection, occurrence_id: int
) -> dict[str, Any] | None:
    """None means it was no longer pending — another fire path won."""
    cur = await conn.execute(_MARK_ELIGIBLE, (occurrence_id,))
    return await cur.fetchone()


async def mark_occurrence_surfaced(
    conn: AsyncConnection, occurrence_id: int, *, turn_id: int
) -> dict[str, Any] | None:
    cur = await conn.execute(_MARK_SURFACED, (turn_id, occurrence_id))
    return await cur.fetchone()


async def skip_unsurfaced_occurrences(
    conn: AsyncConnection, commitment_id: int
) -> int:
    cur = await conn.execute(_SKIP_UNSURFACED, (commitment_id,))
    return len(await cur.fetchall())


async def skip_same_day_occurrences(
    conn: AsyncConnection, *, commitment_id: int, done_at: Any, timezone: str
) -> int:
    """Retire pending/eligible occurrences on done_at's local day."""
    cur = await conn.execute(
        _SKIP_SAME_DAY, (commitment_id, timezone, done_at, timezone)
    )
    return len(await cur.fetchall())


async def open_scheduled(conn: AsyncConnection) -> list[dict[str, Any]]:
    cur = await conn.execute(_OPEN_SCHEDULED)
    return await cur.fetchall()


async def open_all(conn: AsyncConnection) -> list[dict[str, Any]]:
    cur = await conn.execute(_OPEN_ALL)
    return await cur.fetchall()


async def acknowledge_surfaced(conn: AsyncConnection) -> int:
    cur = await conn.execute(_ACKNOWLEDGE_SURFACED)
    return len(await cur.fetchall())
