"""Test harness: runs against a dedicated Neon TEST branch, nothing local.

DESTRUCTIVE: every session drops and recreates the public schema. The guard
below refuses any TEST_DATABASE_URL that doesn't look like a test database,
so a misconfigured env can never point the suite at production.
"""

import os
import secrets
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
import pytest_asyncio

from inkling.db.client import Database
from inkling.db.migrate import apply_all

USER_A = "testa"
USER_B = "testb"


def _test_dsn() -> str:
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL not set (needs a Neon test-branch URL)")
    # Guard: host or database name must contain 'test' — points at the Neon
    # test branch endpoint or a database literally named for testing.
    hostdb = dsn.split("@")[-1].lower()
    if "test" not in hostdb:
        raise RuntimeError(
            "Refusing to run: TEST_DATABASE_URL host/db contains no 'test' "
            "marker; this suite DROPS the public schema."
        )
    return dsn


@pytest.fixture(scope="session")
def dsn() -> str:
    return _test_dsn()


@pytest.fixture(scope="session")
def migrated(dsn: str) -> str:
    """Fresh schema per session: drop, re-migrate, seed two users."""
    with psycopg.connect(dsn) as conn:
        conn.execute("DROP EXTENSION IF EXISTS vector CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()

    apply_all(dsn)

    # Production shape: the app connects AS runtime_role (DATABASE_URL, §2).
    # The Neon owner role carries BYPASSRLS, so testing through it would skip
    # RLS entirely — attach a throwaway login to runtime_role instead.
    password = secrets.token_urlsafe(16)
    with psycopg.connect(dsn) as conn:
        conn.execute(f"ALTER ROLE runtime_role WITH LOGIN PASSWORD '{password}'")
        conn.commit()

    with psycopg.connect(dsn) as conn:
        for uid in (USER_A, USER_B):
            # FORCE RLS applies to the owner too: tenant must be set to insert.
            conn.execute("BEGIN")
            conn.execute("SELECT set_config('app.user_id', %s, true)", (uid,))
            conn.execute(
                """
                INSERT INTO users (id, display_name, timezone, ntfy_topic, inference_profile)
                VALUES (%s, %s, 'America/New_York', %s, '{"type":"api_key","key_file":"/dev/null"}')
                """,
                (uid, uid.capitalize(), f"inkling-{uid}-test"),
            )
            conn.execute("COMMIT")

    parts = urlsplit(dsn)
    netloc = f"runtime_role:{password}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@pytest_asyncio.fixture(scope="session")
async def db(migrated: str) -> Database:
    """Pool connected as runtime_role — the shape production runs with."""
    database = Database(migrated, min_size=1, max_size=3)
    await database.open()
    yield database
    await database.close()


@asynccontextmanager
async def runtime_tenant(db: Database, user_id: str | None):
    """Transaction as runtime_role (narrow grants + RLS), optionally tenanted.

    SET LOCAL ROLE keeps the demotion transaction-scoped, so pooled
    connections come back privileged.
    """
    async with db.pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE runtime_role")
            if user_id is not None:
                await conn.execute(
                    "SELECT set_config('app.user_id', %s, true)", (user_id,)
                )
            yield conn
