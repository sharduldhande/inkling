"""SQL for the episodic event log and habits (§4.4, §4.5).

`events` is SELECT/INSERT only by grant: a correction is a new row pointing at
what it corrects via corrects_id, never an edit of the original.
"""

from typing import Any

from psycopg import AsyncConnection

_INSERT = """
INSERT INTO events
  (user_id, occurred_at, kind, habit_id, value_num, unit, note, corrects_id,
   source, turn_id)
VALUES (%s, COALESCE(%s::timestamptz, now()), %s, %s, %s, %s, %s, %s, %s, %s)
RETURNING *
"""

_HABIT = "SELECT * FROM habits WHERE id = %s"


async def insert(
    conn: AsyncConnection,
    *,
    user_id: str,
    occurred_at: Any,
    kind: str,
    habit_id: str | None,
    value_num: float | None,
    unit: str | None,
    note: str,
    corrects_id: int | None = None,
    source: str = "conversation",
    turn_id: int | None = None,
) -> dict[str, Any]:
    cur = await conn.execute(
        _INSERT,
        (
            user_id,
            occurred_at,
            kind,
            habit_id,
            value_num,
            unit,
            note,
            corrects_id,
            source,
            turn_id,
        ),
    )
    row = await cur.fetchone()
    assert row is not None  # INSERT ... RETURNING always yields a row
    return row


async def habit(conn: AsyncConnection, habit_id: str) -> dict[str, Any] | None:
    """RLS scopes this to the tenant: another user's habit returns None."""
    cur = await conn.execute(_HABIT, (habit_id,))
    return await cur.fetchone()
