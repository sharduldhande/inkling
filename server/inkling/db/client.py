"""Async connection pool with per-transaction tenant context.

Neon pooled connections are PgBouncer transaction mode: session state does not
persist across transactions, so the tenant id is set with a transaction-local
`set_config(..., true)` inside every transaction — never with plain SET, and
never assumed to carry over (§3). Advisory locks and LISTEN/NOTIFY are banned
for correctness anywhere in this codebase for the same reason.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


class Database:
    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 5):
        self.pool = AsyncConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            open=False,
            kwargs={"row_factory": dict_row},
        )

    async def open(self) -> None:
        await self.pool.open(wait=True)

    async def close(self) -> None:
        await self.pool.close()

    @asynccontextmanager
    async def with_tenant(self, user_id: str) -> AsyncIterator[AsyncConnection]:
        """One transaction scoped to `user_id`; RLS filters every statement.

        The whole block is a single short transaction — long work (model calls,
        HTTP) must never happen inside it (§5.3).
        """
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.user_id', %s, true)", (user_id,)
                )
                yield conn

    @asynccontextmanager
    async def system(self) -> AsyncIterator[AsyncConnection]:
        """Transaction with no tenant set: RLS yields zero rows on user tables.

        For system tables (audit_log, schema_migrations) and cross-tenant
        scans that use explicit non-RLS queries via table owners — not for
        user data access.
        """
        async with self.pool.connection() as conn:
            async with conn.transaction():
                yield conn
