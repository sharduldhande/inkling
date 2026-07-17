"""SQL for typed semantic facts (§4.10).

Written only by the §13.1 consolidation job (validate.py enforces it); the
turn path just surfaces the live ones. "Upsert" here is history-preserving:
a changed value invalidates the old fact rather than editing it, so the table
records what we believed and when we stopped believing it.
"""

from typing import Any

from psycopg import AsyncConnection

_ACTIVE = """
SELECT * FROM memory_facts
WHERE invalidated_at IS NULL
ORDER BY confidence DESC, COALESCE(last_confirmed, first_seen) DESC
LIMIT %s
"""

_MATCH = """
SELECT * FROM memory_facts
WHERE invalidated_at IS NULL
  AND lower(subject) = lower(%s) AND lower(predicate) = lower(%s)
ORDER BY id
"""

_CONFIRM = """
UPDATE memory_facts
SET last_confirmed = now(), confidence = %s, condition = %s
WHERE id = %s
RETURNING *
"""

_INVALIDATE = "UPDATE memory_facts SET invalidated_at = now() WHERE id = %s"

_INSERT = """
INSERT INTO memory_facts (user_id, subject, predicate, value, condition, confidence)
VALUES (%s, %s, %s, %s, %s, %s)
RETURNING *
"""


async def active(conn: AsyncConnection, limit: int) -> list[dict[str, Any]]:
    cur = await conn.execute(_ACTIVE, (limit,))
    return await cur.fetchall()


async def upsert(
    conn: AsyncConnection,
    *,
    user_id: str,
    subject: str,
    predicate: str,
    value: str,
    condition: str | None,
    confidence: float,
) -> dict[str, Any]:
    """Upsert on (subject, predicate); returns {fact_id, action}.

    Same value → confirm (refresh last_confirmed/confidence). Different value
    → invalidate every live match and insert the new belief.
    """
    cur = await conn.execute(_MATCH, (subject, predicate))
    matches = await cur.fetchall()

    same = [m for m in matches if m["value"].strip().lower() == value.strip().lower()]
    if same:
        cur = await conn.execute(_CONFIRM, (confidence, condition, same[0]["id"]))
        row = await cur.fetchone()
        assert row is not None
        return {"fact_id": int(row["id"]), "action": "confirmed"}

    for stale in matches:
        await conn.execute(_INVALIDATE, (stale["id"],))
    cur = await conn.execute(
        _INSERT, (user_id, subject, predicate, value, condition, confidence)
    )
    row = await cur.fetchone()
    assert row is not None
    return {
        "fact_id": int(row["id"]),
        "action": "replaced" if matches else "created",
        "invalidated": [int(m["id"]) for m in matches],
    }
