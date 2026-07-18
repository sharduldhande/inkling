"""Health commands (LogMeal / LogWorkout / LogWeight / SetTargets).

Runs as runtime_role against the Neon test branch, so these tests also prove
migration 005's grants permit exactly what apply.py does: INSERT on the typed
tables, the column-scoped UPDATE on users.targets, and nothing more.
"""

from datetime import datetime, timedelta, timezone

import psycopg
import pytest
import pytest_asyncio

from inkling.db.client import Database
from inkling.db.queries import deliveries as deliveries_q
from inkling.db.queries import users as users_q
from inkling.turns.commands import parse_commands
from inkling.turns.validate import validate_commands
from inkling.turns.apply import apply_command

from tests.conftest import USER_A, USER_B


@pytest_asyncio.fixture(autouse=True)
async def clean(migrated: str, dsn: str):
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "TRUNCATE turn_events, turns, triggers, transcript, audit_log, "
            "events, deliveries, intake, workouts, body_metrics "
            "RESTART IDENTITY CASCADE"
        )
        conn.commit()
    with psycopg.connect(dsn) as conn:
        for uid in (USER_A, USER_B):
            conn.execute("BEGIN")
            conn.execute("SELECT set_config('app.user_id', %s, true)", (uid,))
            cur = conn.execute(
                "UPDATE users SET targets = '{}' WHERE id = %s RETURNING id", (uid,)
            )
            assert cur.fetchone() is not None
            conn.execute("COMMIT")
    yield


async def _validate(db: Database, user_id: str, raw: list[dict]):
    commands = parse_commands(raw)
    assert len(commands) == len(raw), "fixture didn't parse under §9.1"
    async with db.with_tenant(user_id) as conn:
        user = await users_q.get(conn, user_id)
        return await validate_commands(conn, user=user, commands=commands)


async def _apply_one(db: Database, user_id: str, raw: dict, *, turn_id: int = 1):
    """Validate then apply a single command inside one tenant transaction."""
    async with db.with_tenant(user_id) as conn:
        user = await users_q.get(conn, user_id)
        validated = await validate_commands(
            conn, user=user, commands=parse_commands([raw])
        )
        assert not validated.rejected, validated.rejected
        return await apply_command(
            conn, validated.accepted[0], user=user, turn_id=turn_id, index=0
        )


async def _rows(db: Database, user_id: str, sql: str, args=()):
    async with db.with_tenant(user_id) as conn:
        cur = await conn.execute(sql, args)
        return await cur.fetchall()


# --- LogMeal ----------------------------------------------------------------


async def test_log_meal_writes_intake_and_mirrors_to_healthkit(db: Database):
    detail = await _apply_one(
        db,
        USER_A,
        {
            "type": "LogMeal",
            "note": "dal + rice",
            "kcal": 650,
            "protein_g": 24,
            "carb_g": 110,
            "fat_g": 12,
        },
        turn_id=7,
    )
    rows = await _rows(db, USER_A, "SELECT * FROM intake")
    assert len(rows) == 1
    row = rows[0]
    assert row["note"] == "dal + rice"
    assert float(row["kcal"]) == 650.0 and float(row["protein_g"]) == 24.0
    assert row["source"] == "chat" and row["turn_id"] == 7
    assert detail["intake_id"] == int(row["id"])

    mirrored = await _rows(
        db, USER_A, "SELECT * FROM deliveries WHERE channel = 'healthkit'"
    )
    assert len(mirrored) == 1
    assert mirrored[0]["status"] == "queued"
    assert mirrored[0]["idempotency_key"] == "turn:7:healthkit:0"
    assert '"kind": "meal"' in mirrored[0]["body"]


async def test_log_meal_bounds(db: Database):
    validated = await _validate(
        db, USER_A, [{"type": "LogMeal", "note": "feast", "kcal": 6500}]
    )
    assert len(validated.rejected) == 1
    assert "kcal" in validated.rejected[0].reason

    validated = await _validate(
        db, USER_A, [{"type": "LogMeal", "note": "shake", "protein_g": 900}]
    )
    assert "protein_g" in validated.rejected[0].reason

    validated = await _validate(db, USER_A, [{"type": "LogMeal", "note": "   "}])
    assert "note is required" in validated.rejected[0].reason


async def test_log_meal_future_is_rejected(db: Database):
    when = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    validated = await _validate(
        db, USER_A, [{"type": "LogMeal", "note": "dinner", "occurred_at": when}]
    )
    assert "future" in validated.rejected[0].reason


async def test_log_meal_supersede_chain(db: Database):
    first = await _apply_one(
        db, USER_A, {"type": "LogMeal", "note": "lunch, first guess", "kcal": 650}
    )
    old_id = first["intake_id"]

    await _apply_one(
        db,
        USER_A,
        {
            "type": "LogMeal",
            "note": "lunch, corrected",
            "kcal": 700,
            "supersedes_id": old_id,
        },
        turn_id=2,
    )

    # Superseding the already-corrected row again is rejected with direction.
    validated = await _validate(
        db,
        USER_A,
        [{"type": "LogMeal", "note": "third try", "kcal": 710, "supersedes_id": old_id}],
    )
    assert "already corrected" in validated.rejected[0].reason

    # Another tenant's meal id reads as not found (RLS).
    validated = await _validate(
        db,
        USER_B,
        [{"type": "LogMeal", "note": "not mine", "kcal": 500, "supersedes_id": old_id}],
    )
    assert "not found" in validated.rejected[0].reason


# --- LogWorkout -------------------------------------------------------------


async def test_log_workout_applies_and_mirrors(db: Database):
    detail = await _apply_one(
        db,
        USER_A,
        {
            "type": "LogWorkout",
            "activity_type": "running",
            "duration_min": 30,
            "kcal_burned": 260,
            "distance_km": 5,
            "note": "easy pace",
        },
        turn_id=3,
    )
    rows = await _rows(db, USER_A, "SELECT * FROM workouts")
    assert len(rows) == 1
    assert rows[0]["activity_type"] == "running"
    assert rows[0]["source"] == "chat" and rows[0]["source_ref"] is None
    assert detail["workout_id"] == int(rows[0]["id"])

    mirrored = await _rows(
        db, USER_A, "SELECT body FROM deliveries WHERE channel = 'healthkit'"
    )
    assert len(mirrored) == 1 and '"kind": "workout"' in mirrored[0]["body"]


async def test_log_workout_bounds_and_activity_list(db: Database):
    validated = await _validate(
        db,
        USER_A,
        [{"type": "LogWorkout", "activity_type": "running", "duration_min": 900}],
    )
    assert "duration_min" in validated.rejected[0].reason

    # An activity outside the pinned HealthKit-compatible list fails §9.1
    # parsing (dropped), not §9.2 validation.
    parsed = parse_commands(
        [{"type": "LogWorkout", "activity_type": "levitation", "duration_min": 30}]
    )
    assert parsed == []


# --- LogWeight --------------------------------------------------------------


async def test_log_weight_applies_and_mirrors(db: Database):
    await _apply_one(
        db, USER_A, {"type": "LogWeight", "value": 82.4, "unit": "kg"}, turn_id=4
    )
    rows = await _rows(db, USER_A, "SELECT * FROM body_metrics")
    assert len(rows) == 1
    assert rows[0]["kind"] == "weight" and float(rows[0]["value"]) == 82.4

    mirrored = await _rows(
        db, USER_A, "SELECT body FROM deliveries WHERE channel = 'healthkit'"
    )
    assert len(mirrored) == 1 and '"kind": "weight"' in mirrored[0]["body"]


async def test_log_weight_pounds_convert_for_bounds(db: Database):
    validated = await _validate(
        db, USER_A, [{"type": "LogWeight", "value": 180, "unit": "lb"}]
    )
    assert not validated.rejected  # ~81.6 kg: fine

    validated = await _validate(
        db, USER_A, [{"type": "LogWeight", "value": 900, "unit": "lb"}]
    )
    assert "kg" in validated.rejected[0].reason


# --- SetTargets -------------------------------------------------------------


async def test_set_targets_merges_not_replaces(db: Database):
    await _apply_one(db, USER_A, {"type": "SetTargets", "kcal_quota": 2200})
    detail = await _apply_one(db, USER_A, {"type": "SetTargets", "protein_quota_g": 140})
    assert detail["targets"] == {"kcal_quota": 2200, "protein_quota_g": 140}

    # And the other tenant's targets are untouched.
    async with db.with_tenant(USER_B) as conn:
        other = await users_q.get(conn, USER_B)
    assert other["targets"] == {}


async def test_set_targets_bounds_and_emptiness(db: Database):
    validated = await _validate(db, USER_A, [{"type": "SetTargets"}])
    assert "give kcal_quota or protein_quota_g" in validated.rejected[0].reason

    validated = await _validate(db, USER_A, [{"type": "SetTargets", "kcal_quota": 200}])
    assert "kcal_quota" in validated.rejected[0].reason


async def test_set_targets_blocked_for_consolidation(db: Database):
    commands = parse_commands([{"type": "SetTargets", "kcal_quota": 2000}])
    async with db.with_tenant(USER_A) as conn:
        user = await users_q.get(conn, USER_A)
        validated = await validate_commands(
            conn, user=user, commands=commands, consolidation=True
        )
    assert "conversation with the user" in validated.rejected[0].reason


# --- outbox exclusion -------------------------------------------------------


async def test_drainer_never_sees_healthkit_rows(db: Database):
    """The §11 due-scan must skip channel='healthkit' — those rows are pulled
    by the app; a drainer claim would dead-letter them and page the operator."""
    await _apply_one(
        db, USER_A, {"type": "LogMeal", "note": "invisible to ntfy", "kcal": 400}
    )
    far_future = datetime.now(timezone.utc) + timedelta(days=1)
    async with db.with_tenant(USER_A) as conn:
        due = await deliveries_q.due(conn, now=far_future)
        next_due = await deliveries_q.next_due(conn)
    assert due == []
    assert next_due is None
