"""SQL for typed semantic facts (§4.10).

Read-only from the turn path: memory_facts are written only by the
consolidation job (never mid-turn), so the runtime just surfaces the live ones.
"""

from typing import Any

from psycopg import AsyncConnection

_ACTIVE = """
SELECT * FROM memory_facts
WHERE invalidated_at IS NULL
ORDER BY confidence DESC, COALESCE(last_confirmed, first_seen) DESC
LIMIT %s
"""


async def active(conn: AsyncConnection, limit: int) -> list[dict[str, Any]]:
    cur = await conn.execute(_ACTIVE, (limit,))
    return await cur.fetchall()
