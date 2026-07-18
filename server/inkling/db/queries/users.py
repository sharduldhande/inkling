"""SQL for users (§4.1). SELECT only by grant, with one exception: SetTargets
merges the `targets` column via a column-scoped UPDATE grant (see
queries/health.py, migration 005)."""

from typing import Any

from psycopg import AsyncConnection

_GET = "SELECT * FROM users WHERE id = %s"


async def get(conn: AsyncConnection, user_id: str) -> dict[str, Any] | None:
    cur = await conn.execute(_GET, (user_id,))
    return await cur.fetchone()
