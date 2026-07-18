"""Cross-tenant isolation tests (§4.11, mandatory).

For every user table: insert rows as user A, attempt reads/writes under a
different app.user_id, assert zero visibility and rejected writes. Coverage is
enforced by enumerating tables from information_schema — adding a table without
a factory here fails the suite. (API-layer checks with wrong bearer tokens are
added when the API milestone lands — tracked in test_api_isolation, TODO.)
"""

import uuid

import psycopg.errors
import pytest

from tests.conftest import USER_A, USER_B, runtime_tenant

# Tables with a user_id column that are deliberately NOT tenant-isolated.
SYSTEM_TABLES = {"audit_log"}


async def _mk_trigger(conn, row_user: str) -> int:
    row = await (
        await conn.execute(
            """
            INSERT INTO triggers (user_id, source, idempotency_key, payload)
            VALUES (%s, 'chat', %s, '{}') RETURNING id
            """,
            (row_user, str(uuid.uuid4())),
        )
    ).fetchone()
    return row["id"]


async def _mk_turn(conn, row_user: str) -> int:
    trigger_id = await _mk_trigger(conn, row_user)
    row = await (
        await conn.execute(
            "INSERT INTO turns (user_id, trigger_id) VALUES (%s, %s) RETURNING id",
            (row_user, trigger_id),
        )
    ).fetchone()
    return row["id"]


async def _mk_commitment(conn, row_user: str) -> int:
    row = await (
        await conn.execute(
            """
            INSERT INTO commitments (user_id, title, due_at, source)
            VALUES (%s, 'call the dentist', now() + interval '1 day', 'conversation')
            RETURNING id
            """,
            (row_user,),
        )
    ).fetchone()
    return row["id"]


async def triggers_factory(conn, row_user):
    await _mk_trigger(conn, row_user)


async def turns_factory(conn, row_user):
    await _mk_turn(conn, row_user)


async def turn_events_factory(conn, row_user):
    turn_id = await _mk_turn(conn, row_user)
    await conn.execute(
        """
        INSERT INTO turn_events (user_id, turn_id, seq, kind, data)
        VALUES (%s, %s, 1, 'text_delta', '{"text":"hi"}')
        """,
        (row_user, turn_id),
    )


async def events_factory(conn, row_user):
    await conn.execute(
        """
        INSERT INTO events (user_id, occurred_at, kind, note)
        VALUES (%s, now(), 'note', 'isolation probe')
        """,
        (row_user,),
    )


async def habits_factory(conn, row_user):
    await conn.execute(
        "INSERT INTO habits (user_id, id, title) VALUES (%s, %s, 'stretch')",
        (row_user, f"h-{uuid.uuid4().hex[:8]}"),
    )


async def commitments_factory(conn, row_user):
    await _mk_commitment(conn, row_user)


async def intake_factory(conn, row_user):
    await conn.execute(
        """
        INSERT INTO intake (user_id, occurred_at, note, kcal)
        VALUES (%s, now(), 'isolation probe meal', 500)
        """,
        (row_user,),
    )


async def workouts_factory(conn, row_user):
    await conn.execute(
        """
        INSERT INTO workouts (user_id, occurred_at, activity_type, duration_min)
        VALUES (%s, now(), 'running', 30)
        """,
        (row_user,),
    )


async def body_metrics_factory(conn, row_user):
    await conn.execute(
        """
        INSERT INTO body_metrics (user_id, occurred_at, kind, value, unit)
        VALUES (%s, now(), 'weight', 80, 'kg')
        """,
        (row_user,),
    )


async def commitment_occurrences_factory(conn, row_user):
    commitment_id = await _mk_commitment(conn, row_user)
    await conn.execute(
        """
        INSERT INTO commitment_occurrences (user_id, commitment_id, fire_at)
        VALUES (%s, %s, now() + interval '1 day')
        """,
        (row_user, commitment_id),
    )


async def deliveries_factory(conn, row_user):
    await conn.execute(
        """
        INSERT INTO deliveries (user_id, destination, body, idempotency_key)
        VALUES (%s, 'topic', 'ping', %s)
        """,
        (row_user, str(uuid.uuid4())),
    )


async def docs_factory(conn, row_user):
    await conn.execute(
        """
        INSERT INTO docs (user_id, kind, version, content, written_by)
        VALUES (%s, 'working_state', (
            SELECT coalesce(max(version), 0) + 1 FROM docs
            WHERE user_id = %s AND kind = 'working_state'
          ), '# state', 'test')
        """,
        (row_user, row_user),
    )


async def transcript_factory(conn, row_user):
    await conn.execute(
        "INSERT INTO transcript (user_id, role, text) VALUES (%s, 'user', 'hello')",
        (row_user,),
    )


async def scheduled_wakeups_factory(conn, row_user):
    await conn.execute(
        """
        INSERT INTO scheduled_wakeups (user_id, fire_at, purpose, created_by)
        VALUES (%s, now() + interval '1 hour', 'probe', 'seed')
        """,
        (row_user,),
    )


async def calendar_sync_state_factory(conn, row_user):
    await conn.execute(
        """
        INSERT INTO calendar_sync_state (user_id, calendar_id, oauth_token_file)
        VALUES (%s, %s, '/dev/null')
        """,
        (row_user, f"cal-{uuid.uuid4().hex[:8]}"),
    )


async def calendar_events_cache_factory(conn, row_user):
    await conn.execute(
        """
        INSERT INTO calendar_events_cache (user_id, provider_event_id, calendar_id, raw)
        VALUES (%s, %s, 'primary', '{}')
        """,
        (row_user, f"evt-{uuid.uuid4().hex[:8]}"),
    )


async def ingested_documents_factory(conn, row_user):
    await conn.execute(
        """
        INSERT INTO ingested_documents (user_id, title, storage_path, extracted)
        VALUES (%s, 'syllabus', '/uploads/x.pdf', '{}')
        """,
        (row_user,),
    )


async def memory_facts_factory(conn, row_user):
    await conn.execute(
        """
        INSERT INTO memory_facts (user_id, subject, predicate, value)
        VALUES (%s, 'user', 'prefers', 'mornings')
        """,
        (row_user,),
    )


TABLE_FACTORIES = {
    "triggers": triggers_factory,
    "turns": turns_factory,
    "turn_events": turn_events_factory,
    "events": events_factory,
    "habits": habits_factory,
    "commitments": commitments_factory,
    "commitment_occurrences": commitment_occurrences_factory,
    "intake": intake_factory,
    "workouts": workouts_factory,
    "body_metrics": body_metrics_factory,
    "deliveries": deliveries_factory,
    "docs": docs_factory,
    "transcript": transcript_factory,
    "scheduled_wakeups": scheduled_wakeups_factory,
    "calendar_sync_state": calendar_sync_state_factory,
    "calendar_events_cache": calendar_events_cache_factory,
    "ingested_documents": ingested_documents_factory,
    "memory_facts": memory_facts_factory,
}


async def test_every_user_table_is_covered(db):
    """CI fails if any table with user_id lacks an isolation test (§4.11)."""
    async with db.system() as conn:
        rows = await (
            await conn.execute(
                """
                SELECT table_name FROM information_schema.columns
                WHERE table_schema = 'public' AND column_name = 'user_id'
                """
            )
        ).fetchall()
    user_tables = {r["table_name"] for r in rows} - SYSTEM_TABLES
    assert user_tables == set(TABLE_FACTORIES), (
        "user tables without an isolation factory: "
        f"{user_tables - set(TABLE_FACTORIES)}; "
        f"stale factories: {set(TABLE_FACTORIES) - user_tables}"
    )


async def test_connected_role_cannot_bypass_rls(db):
    """The app's connection role must never carry BYPASSRLS or SUPERUSER —
    Neon's owner role does, which would skip every policy silently."""
    async with db.system() as conn:
        row = await (
            await conn.execute(
                "SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname = current_user"
            )
        ).fetchone()
    assert not row["rolbypassrls"]
    assert not row["rolsuper"]


async def test_rls_enabled_and_forced_everywhere(db):
    async with db.system() as conn:
        rows = await (
            await conn.execute(
                """
                SELECT relname, relrowsecurity, relforcerowsecurity
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind = 'r' AND relname = ANY(%s)
                """,
                (list(TABLE_FACTORIES) + ["users"],),
            )
        ).fetchall()
    assert len(rows) == len(TABLE_FACTORIES) + 1
    for r in rows:
        assert r["relrowsecurity"], f"{r['relname']}: RLS not enabled"
        assert r["relforcerowsecurity"], f"{r['relname']}: RLS not FORCEd"


@pytest.mark.parametrize("table", sorted(TABLE_FACTORIES))
async def test_cross_tenant_isolation(db, table):
    factory = TABLE_FACTORIES[table]

    # A inserts and sees the row.
    async with runtime_tenant(db, USER_A) as conn:
        await factory(conn, USER_A)
        count = (await (await conn.execute(f"SELECT count(*) AS n FROM {table}")).fetchone())["n"]
        assert count >= 1

    # B sees nothing of A's, and blanket UPDATE/DELETE touch zero rows
    # (or are denied outright where the grant doesn't exist).
    async with runtime_tenant(db, USER_B) as conn:
        count = (await (await conn.execute(f"SELECT count(*) AS n FROM {table}")).fetchone())["n"]
        assert count == 0, f"{table}: tenant B can see tenant A rows"

        for stmt in (f"UPDATE {table} SET user_id = user_id", f"DELETE FROM {table}"):
            try:
                async with conn.transaction():  # savepoint: a denial must not kill the tx
                    cur = await conn.execute(stmt)
                    assert cur.rowcount == 0, f"{table}: tenant B mutated A rows via {stmt!r}"
            except psycopg.errors.InsufficientPrivilege:
                pass  # no grant at all is even stricter — fine

    # B cannot forge rows claiming to be A (WITH CHECK).
    async with runtime_tenant(db, USER_B) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with conn.transaction():
                await factory(conn, USER_A)


async def test_users_table_isolation(db):
    async with runtime_tenant(db, USER_A) as conn:
        rows = await (await conn.execute("SELECT id FROM users")).fetchall()
        assert [r["id"] for r in rows] == [USER_A]


async def test_no_tenant_means_no_rows(db):
    """A transaction that forgot set_config sees nothing, on every table."""
    async with runtime_tenant(db, None) as conn:
        for table in list(TABLE_FACTORIES) + ["users"]:
            count = (await (await conn.execute(f"SELECT count(*) AS n FROM {table}")).fetchone())["n"]
            assert count == 0, f"{table}: visible without tenant context"


async def test_events_are_append_only_for_runtime(db):
    """D10 at the grant level: runtime_role cannot UPDATE or DELETE events."""
    async with runtime_tenant(db, USER_A) as conn:
        await events_factory(conn, USER_A)
        for stmt in ("UPDATE events SET note = 'rewritten'", "DELETE FROM events"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                async with conn.transaction():
                    await conn.execute(stmt)


async def test_runtime_role_cannot_ddl(db):
    async with runtime_tenant(db, USER_A) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with conn.transaction():
                await conn.execute("CREATE TABLE smuggled (id int)")


async def test_with_tenant_helper_scopes_rows(db):
    """The production helper (not just raw SQL) enforces the same scoping."""
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "INSERT INTO transcript (user_id, role, text) VALUES (%s, 'user', 'via helper')",
            (USER_A,),
        )
    async with db.with_tenant(USER_B) as conn:
        rows = await (
            await conn.execute("SELECT * FROM transcript WHERE text = 'via helper'")
        ).fetchall()
        assert rows == []
