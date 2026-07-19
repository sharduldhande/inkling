"""SQL for ingested_documents (§14, §4.10)."""

import json
from typing import Any

from psycopg import AsyncConnection

_INSERT = """
INSERT INTO ingested_documents (user_id, title, storage_path, extracted)
VALUES (%s, %s, %s, %s)
RETURNING *
"""

_GET = "SELECT * FROM ingested_documents WHERE id = %s"

# Recent unconfirmed docs surface in chat context until confirmed or stale;
# 14 days keeps a proposal the user never answered from nagging forever.
_UNCONFIRMED = """
SELECT id, title, created_at FROM ingested_documents
WHERE confirmed_at IS NULL AND created_at > now() - interval '14 days'
ORDER BY created_at DESC
LIMIT %s
"""

# The WHERE guards double-confirmation: the CAS shape everything here uses.
_CONFIRM = """
UPDATE ingested_documents SET confirmed_at = now()
WHERE id = %s AND confirmed_at IS NULL
RETURNING id
"""


async def insert(
    conn: AsyncConnection,
    *,
    user_id: str,
    title: str,
    storage_path: str,
    extracted: dict[str, Any],
) -> dict[str, Any]:
    cur = await conn.execute(
        _INSERT, (user_id, title, storage_path, json.dumps(extracted))
    )
    row = await cur.fetchone()
    assert row is not None
    return row


async def get(conn: AsyncConnection, document_id: int) -> dict[str, Any] | None:
    cur = await conn.execute(_GET, (document_id,))
    return await cur.fetchone()


async def unconfirmed(conn: AsyncConnection, limit: int = 3) -> list[dict[str, Any]]:
    cur = await conn.execute(_UNCONFIRMED, (limit,))
    return await cur.fetchall()


async def confirm(conn: AsyncConnection, document_id: int) -> bool:
    """True if this call confirmed it; False if missing or already confirmed."""
    cur = await conn.execute(_CONFIRM, (document_id,))
    return await cur.fetchone() is not None
