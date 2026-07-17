"""SQL for the transcript (§4.8). SELECT/INSERT only by grant — append-only."""

from typing import Any

from psycopg import AsyncConnection

# Newest N, returned oldest-first so the caller can render chronologically.
_TAIL = """
SELECT * FROM (
  SELECT id, turn_id, ts, role, text
  FROM transcript
  ORDER BY ts DESC, id DESC
  LIMIT %s
) newest
ORDER BY ts, id
"""

# websearch_to_tsquery: raw-ish input can never raise a syntax error, and it
# honors the OR the caller joins terms with. Retrieval wants OR semantics —
# AND-of-all-trigger-words almost never matches a months-old note — and
# ts_rank still puts rows matching more terms first. All-stopword input yields
# an empty tsquery, which matches nothing: also the right answer.
_FTS = """
SELECT id, ts, role, text, ts_rank(search, query) AS rank
FROM transcript, websearch_to_tsquery('english', %s) query
WHERE search @@ query
ORDER BY rank DESC, ts DESC
LIMIT %s
"""


async def tail(conn: AsyncConnection, limit: int) -> list[dict[str, Any]]:
    cur = await conn.execute(_TAIL, (limit,))
    return await cur.fetchall()


async def fts(conn: AsyncConnection, text: str, limit: int) -> list[dict[str, Any]]:
    cur = await conn.execute(_FTS, (text, limit))
    return await cur.fetchall()
