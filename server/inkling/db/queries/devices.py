"""SQL for the APNs device registry (§11).

The app registers its token on every launch; the same token re-registered by
the same user is a touch (last_seen_at, un-revoke). Under RLS a token already
owned by ANOTHER user is not this tenant's to update, so the statement fails
(UniqueViolation or an RLS policy violation, depending on the planner) — the
route turns either into a 409 rather than silently stealing the device.
"""

from typing import Any

from psycopg import AsyncConnection

_UPSERT = """
INSERT INTO devices (user_id, platform, token, environment, app_version)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (token) DO UPDATE SET
  environment = excluded.environment,
  app_version = excluded.app_version,
  last_seen_at = now(),
  revoked_at = NULL,
  revoke_reason = NULL
RETURNING *
"""

_ACTIVE = """
SELECT * FROM devices
WHERE revoked_at IS NULL
ORDER BY id
"""

_REVOKE = """
UPDATE devices
SET revoked_at = now(), revoke_reason = %s
WHERE id = %s AND revoked_at IS NULL
RETURNING id
"""


async def upsert(
    conn: AsyncConnection,
    *,
    user_id: str,
    token: str,
    environment: str,
    app_version: str | None,
    platform: str = "ios",
) -> dict[str, Any]:
    cur = await conn.execute(
        _UPSERT, (user_id, platform, token, environment, app_version)
    )
    row = await cur.fetchone()
    assert row is not None  # RETURNING on an insert-or-update always yields
    return row


async def active(conn: AsyncConnection) -> list[dict[str, Any]]:
    """Every live device for the tenant (RLS scopes the scan)."""
    cur = await conn.execute(_ACTIVE)
    return await cur.fetchall()


async def revoke(conn: AsyncConnection, device_id: int, *, reason: str) -> bool:
    cur = await conn.execute(_REVOKE, (reason[:200], device_id))
    return await cur.fetchone() is not None
