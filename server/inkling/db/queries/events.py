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

_BETWEEN = """
SELECT id, occurred_at, kind, habit_id, value_num, unit, note
FROM events
WHERE occurred_at >= %s AND occurred_at < %s
ORDER BY occurred_at, id
LIMIT %s
"""

# §8.1 section 8: streaks for active habits. "Streak" here is the deterministic
# summary the model reasons over — last done + distinct local days done in the
# past 7 — not a strict consecutive-day count.
_STREAKS = """
SELECT h.id, h.title,
       max(e.occurred_at) AS last_done,
       count(DISTINCT (e.occurred_at AT TIME ZONE %s)::date)
         FILTER (WHERE e.occurred_at >= %s) AS days_last7
FROM habits h
LEFT JOIN events e
  ON e.habit_id = h.id AND e.kind = 'habit_done'
WHERE h.active
GROUP BY h.id, h.title
ORDER BY h.id
"""

# See transcript.py on websearch_to_tsquery + OR-joined terms.
_FTS = """
SELECT id, occurred_at, kind, habit_id, value_num, note,
       ts_rank(search, query) AS rank
FROM events, websearch_to_tsquery('english', %s) query
WHERE search @@ query
ORDER BY rank DESC, occurred_at DESC
LIMIT %s
"""


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


async def between(
    conn: AsyncConnection, *, start: Any, end: Any, limit: int
) -> list[dict[str, Any]]:
    cur = await conn.execute(_BETWEEN, (start, end, limit))
    return await cur.fetchall()


async def streaks(
    conn: AsyncConnection, *, timezone: str, week_start: Any
) -> list[dict[str, Any]]:
    cur = await conn.execute(_STREAKS, (timezone, week_start))
    return await cur.fetchall()


async def fts(conn: AsyncConnection, text: str, limit: int) -> list[dict[str, Any]]:
    cur = await conn.execute(_FTS, (text, limit))
    return await cur.fetchall()
