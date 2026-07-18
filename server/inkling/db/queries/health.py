"""SQL for the typed health tables (intake, workouts, body_metrics) and the
users.targets quota blob.

These tables exist so that health data is typed at the schema, not conventions
in a prompt: a meal is one row with macro columns, a workout is one row with an
activity type, and nothing anywhere matches on magic unit strings. All three
are append-only under runtime grants — a wrong meal is superseded by a new row
(intake.supersedes_id), mirroring events.corrects_id.

occurred_at defaults to now() at the call sites, not in SQL, so a replayed
apply writes the same instant it validated.
"""

from datetime import datetime, timezone
from typing import Any

from psycopg import AsyncConnection

_INSERT_INTAKE = """
INSERT INTO intake
  (user_id, occurred_at, note, kcal, protein_g, carb_g, fat_g,
   supersedes_id, source, source_ref, turn_id)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
RETURNING *
"""

_INSERT_WORKOUT = """
INSERT INTO workouts
  (user_id, occurred_at, activity_type, duration_min, kcal_burned,
   distance_km, note, source, source_ref, turn_id)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (user_id, source_ref) WHERE source_ref IS NOT NULL DO NOTHING
RETURNING *
"""

_INSERT_BODY_METRIC = """
INSERT INTO body_metrics
  (user_id, occurred_at, kind, value, unit, source, source_ref, turn_id)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (user_id, source_ref) WHERE source_ref IS NOT NULL DO NOTHING
RETURNING *
"""

_INTAKE_GET = "SELECT * FROM intake WHERE id = %s"

_INTAKE_IS_SUPERSEDED = """
SELECT 1 FROM intake WHERE supersedes_id = %s LIMIT 1
"""

# jsonb || merges keys, so an absent field in the command leaves the stored
# quota untouched — SetTargets(protein only) must not clear the kcal quota.
_MERGE_TARGETS = """
UPDATE users SET targets = targets || %s::jsonb WHERE id = %s
RETURNING targets
"""


def _default_now(occurred_at: datetime | None) -> datetime:
    return occurred_at if occurred_at is not None else datetime.now(timezone.utc)


async def insert_intake(
    conn: AsyncConnection,
    *,
    user_id: str,
    occurred_at: datetime | None,
    note: str,
    kcal: float | None,
    protein_g: float | None,
    carb_g: float | None,
    fat_g: float | None,
    supersedes_id: int | None = None,
    source: str = "chat",
    source_ref: str | None = None,
    turn_id: int | None = None,
) -> dict[str, Any]:
    cur = await conn.execute(
        _INSERT_INTAKE,
        (
            user_id,
            _default_now(occurred_at),
            note,
            kcal,
            protein_g,
            carb_g,
            fat_g,
            supersedes_id,
            source,
            source_ref,
            turn_id,
        ),
    )
    return await cur.fetchone()


async def insert_workout(
    conn: AsyncConnection,
    *,
    user_id: str,
    occurred_at: datetime | None,
    activity_type: str,
    duration_min: float,
    kcal_burned: float | None,
    distance_km: float | None,
    note: str | None,
    source: str = "chat",
    source_ref: str | None = None,
    turn_id: int | None = None,
) -> dict[str, Any] | None:
    """None means source_ref already ingested (HealthKit replay) — a no-op."""
    cur = await conn.execute(
        _INSERT_WORKOUT,
        (
            user_id,
            _default_now(occurred_at),
            activity_type,
            duration_min,
            kcal_burned,
            distance_km,
            note,
            source,
            source_ref,
            turn_id,
        ),
    )
    return await cur.fetchone()


async def insert_body_metric(
    conn: AsyncConnection,
    *,
    user_id: str,
    occurred_at: datetime | None,
    kind: str,
    value: float,
    unit: str,
    source: str = "chat",
    source_ref: str | None = None,
    turn_id: int | None = None,
) -> dict[str, Any] | None:
    """None means source_ref already ingested — same contract as workouts."""
    cur = await conn.execute(
        _INSERT_BODY_METRIC,
        (user_id, _default_now(occurred_at), kind, value, unit, source, source_ref, turn_id),
    )
    return await cur.fetchone()


async def intake_get(conn: AsyncConnection, intake_id: int) -> dict[str, Any] | None:
    cur = await conn.execute(_INTAKE_GET, (intake_id,))
    return await cur.fetchone()


async def intake_is_superseded(conn: AsyncConnection, intake_id: int) -> bool:
    cur = await conn.execute(_INTAKE_IS_SUPERSEDED, (intake_id,))
    return await cur.fetchone() is not None


async def merge_targets(
    conn: AsyncConnection, *, user_id: str, patch: str
) -> dict[str, Any] | None:
    """patch is a JSON object string of only the keys being set."""
    cur = await conn.execute(_MERGE_TARGETS, (patch, user_id))
    return await cur.fetchone()


_TODAY_SUMMARY = """
SELECT
  (SELECT count(*) FROM intake i
    WHERE i.user_id = %(u)s AND i.occurred_at >= %(start)s
      AND NOT EXISTS (SELECT 1 FROM intake s WHERE s.supersedes_id = i.id))
    AS meals,
  (SELECT coalesce(sum(i.kcal), 0)::float8 FROM intake i
    WHERE i.user_id = %(u)s AND i.occurred_at >= %(start)s
      AND NOT EXISTS (SELECT 1 FROM intake s WHERE s.supersedes_id = i.id))
    AS kcal,
  (SELECT coalesce(sum(i.protein_g), 0)::float8 FROM intake i
    WHERE i.user_id = %(u)s AND i.occurred_at >= %(start)s
      AND NOT EXISTS (SELECT 1 FROM intake s WHERE s.supersedes_id = i.id))
    AS protein_g,
  (SELECT coalesce(sum(w.duration_min), 0)::float8 FROM workouts w
    WHERE w.user_id = %(u)s AND w.occurred_at >= %(start)s)
    AS exercise_min
"""


async def today_summary(
    conn: AsyncConnection, *, user_id: str, start: datetime
) -> dict[str, Any]:
    """One aggregate row for §8's 'today so far' line (detail stays in recall)."""
    cur = await conn.execute(_TODAY_SUMMARY, {"u": user_id, "start": start})
    return await cur.fetchone()


# Recall search: no stored tsvector on these tables (personal-scale row counts);
# computing it per query keeps the migration surface minimal.
_FTS_HEALTH = """
SELECT * FROM (
  SELECT 'meal' AS entry, occurred_at, note,
         kcal AS n1, protein_g AS n2, NULL::text AS label
  FROM intake i
  WHERE i.user_id = %(u)s
    AND NOT EXISTS (SELECT 1 FROM intake s WHERE s.supersedes_id = i.id)
    AND to_tsvector('english', note) @@ websearch_to_tsquery('english', %(q)s)
  UNION ALL
  SELECT 'workout', occurred_at, coalesce(note, ''),
         duration_min, kcal_burned, activity_type
  FROM workouts
  WHERE user_id = %(u)s
    AND to_tsvector('english', coalesce(note, '') || ' ' || activity_type)
        @@ websearch_to_tsquery('english', %(q)s)
) health
ORDER BY occurred_at DESC
LIMIT %(limit)s
"""


async def fts(
    conn: AsyncConnection, *, user_id: str, query: str, limit: int
) -> list[dict[str, Any]]:
    cur = await conn.execute(_FTS_HEALTH, {"u": user_id, "q": query, "limit": limit})
    return await cur.fetchall()
