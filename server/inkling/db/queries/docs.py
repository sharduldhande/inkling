"""SQL for the versioned prose docs (§4.7).

Whole-version writes only; current = max(version). There is no UPDATE grant on
this table (§4 004_grants) — history is the point.
"""

from typing import Any

from psycopg import AsyncConnection

_CURRENT = """
SELECT * FROM docs
WHERE kind = %s
ORDER BY version DESC
LIMIT 1
"""

# The version is computed inside the INSERT rather than read-then-written, so
# two writers racing collide on UNIQUE (user_id, kind, version) instead of
# silently overwriting one another's version number.
_INSERT_NEXT = """
INSERT INTO docs (user_id, kind, version, content, written_by)
SELECT %s, %s, COALESCE(MAX(version), 0) + 1, %s, %s
FROM docs WHERE user_id = %s AND kind = %s
RETURNING *
"""


async def current(conn: AsyncConnection, kind: str) -> dict[str, Any] | None:
    cur = await conn.execute(_CURRENT, (kind,))
    return await cur.fetchone()


async def insert_next_version(
    conn: AsyncConnection, *, user_id: str, kind: str, content: str, written_by: str
) -> dict[str, Any]:
    cur = await conn.execute(
        _INSERT_NEXT, (user_id, kind, content, written_by, user_id, kind)
    )
    row = await cur.fetchone()
    assert row is not None  # INSERT ... RETURNING always yields a row
    return row
