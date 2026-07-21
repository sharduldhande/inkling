"""Read-only pane projections keep chat as the sole user-facing write path.

Agenda and measurement windows are fixed server-side so every client sees the
same slice. Daily health totals are bucketed in the roster user's stored IANA
timezone; the phone clock never decides which day a row belongs to.
"""

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse

from inkling.api.v1_contract import UNAUTHORIZED_BODY

router = APIRouter()

_AGENDA_COMMITMENTS = """
SELECT
  'c:' || id::text AS id,
  title,
  body,
  (extract(epoch FROM due_at) * 1000)::bigint AS due,
  due_at < now() AS overdue
FROM commitments
WHERE user_id = %s
  AND status = 'open'
  AND due_at IS NOT NULL
  AND due_at <= now() + interval '48 hours'
ORDER BY due_at, id
LIMIT 100
"""

# Completions stay visible for a day instead of vanishing: the Today pane
# shows them checked off so "done" reads as an outcome, not a deletion.
# updated_at stands in for completed-at — CompleteCommitment is the only
# writer that moves a commitment to 'done', bumping updated_at as it does.
_AGENDA_DONE = """
SELECT
  'c:' || id::text AS id,
  title,
  body,
  (extract(epoch FROM due_at) * 1000)::bigint AS due,
  (extract(epoch FROM updated_at) * 1000)::bigint AS done_ts
FROM commitments
WHERE user_id = %s
  AND status = 'done'
  AND updated_at >= now() - interval '24 hours'
ORDER BY updated_at DESC, id DESC
LIMIT 50
"""

_AGENDA_OCCURRENCES = """
SELECT
  'o:' || o.id::text AS id,
  c.title,
  c.body,
  (extract(epoch FROM o.fire_at) * 1000)::bigint AS due,
  o.fire_at < now() AS overdue
FROM commitment_occurrences o
JOIN commitments c ON c.id = o.commitment_id
WHERE o.user_id = %s
  AND c.user_id = %s
  AND c.status = 'open'
  AND c.recurrence IS NOT NULL
  AND o.status IN ('pending', 'eligible')
  AND o.fire_at <= now() + interval '48 hours'
ORDER BY o.fire_at, o.id
LIMIT 100
"""

_AGENDA_CALENDAR = """
SELECT
  'g:' || provider_event_id AS id,
  coalesce(summary, '(untitled)') AS title,
  (extract(epoch FROM starts_at) * 1000)::bigint AS starts,
  CASE
    WHEN ends_at IS NULL THEN NULL
    ELSE (extract(epoch FROM ends_at) * 1000)::bigint
  END AS ends
FROM calendar_events_cache
WHERE user_id = %s
  AND starts_at IS NOT NULL
  AND coalesce(ends_at, starts_at) >= now()
  AND starts_at <= now() + interval '48 hours'
ORDER BY starts_at, provider_event_id
LIMIT 100
"""

_MEASUREMENTS = """
SELECT *
FROM (
  SELECT
    'i:' || i.id::text AS id,
    'meal'::text AS type,
    i.note,
    i.kcal::float8 AS kcal,
    i.protein_g::float8 AS protein_g,
    i.carb_g::float8 AS carb_g,
    i.fat_g::float8 AS fat_g,
    NULL::text AS activity,
    NULL::float8 AS duration_min,
    NULL::float8 AS kcal_burned,
    NULL::float8 AS distance_km,
    NULL::text AS kind,
    NULL::float8 AS value,
    NULL::text AS unit,
    (extract(epoch FROM i.occurred_at) * 1000)::bigint AS ts
  FROM intake i
  WHERE i.user_id = %s
    AND i.occurred_at >= now() - interval '7 days'
    AND NOT EXISTS (
      SELECT 1
      FROM intake s
      WHERE s.user_id = i.user_id AND s.supersedes_id = i.id
    )

  UNION ALL

  SELECT
    'w:' || w.id::text,
    'workout'::text,
    w.note,
    NULL::float8,
    NULL::float8,
    NULL::float8,
    NULL::float8,
    w.activity_type,
    w.duration_min::float8,
    w.kcal_burned::float8,
    w.distance_km::float8,
    NULL::text,
    NULL::float8,
    NULL::text,
    (extract(epoch FROM w.occurred_at) * 1000)::bigint
  FROM workouts w
  WHERE w.user_id = %s
    AND w.occurred_at >= now() - interval '7 days'

  UNION ALL

  SELECT
    'b:' || b.id::text,
    'metric'::text,
    NULL::text,
    NULL::float8,
    NULL::float8,
    NULL::float8,
    NULL::float8,
    NULL::text,
    NULL::float8,
    NULL::float8,
    NULL::float8,
    b.kind,
    b.value::float8,
    b.unit,
    (extract(epoch FROM b.occurred_at) * 1000)::bigint
  FROM body_metrics b
  WHERE b.user_id = %s
    AND b.occurred_at >= now() - interval '7 days'
) entries
ORDER BY ts DESC, id DESC
LIMIT 500
"""

_TIMEZONE = "SELECT timezone FROM users WHERE id = %s"
_TARGETS = "SELECT targets FROM users WHERE id = %s"

_DAYS = """
WITH dates AS (
  SELECT generate_series(
    (now() AT TIME ZONE %s)::date - 83,
    (now() AT TIME ZONE %s)::date,
    interval '1 day'
  )::date AS local_date
), intake_days AS (
  SELECT
    (i.occurred_at AT TIME ZONE %s)::date AS local_date,
    coalesce(sum(i.kcal), 0)::float8 AS kcal,
    coalesce(sum(i.protein_g), 0)::float8 AS protein_g
  FROM intake i
  WHERE i.user_id = %s
    AND NOT EXISTS (
      SELECT 1
      FROM intake s
      WHERE s.user_id = i.user_id AND s.supersedes_id = i.id
    )
  GROUP BY 1
), workout_days AS (
  SELECT DISTINCT (w.occurred_at AT TIME ZONE %s)::date AS local_date
  FROM workouts w
  WHERE w.user_id = %s
)
SELECT
  d.local_date::text AS date,
  coalesce(i.kcal, 0)::float8 AS kcal,
  coalesce(i.protein_g, 0)::float8 AS protein_g,
  w.local_date IS NOT NULL AS exercised,
  i.local_date IS NOT NULL AS logged
FROM dates d
LEFT JOIN intake_days i USING (local_date)
LEFT JOIN workout_days w USING (local_date)
ORDER BY d.local_date
"""

_WEIGHT = """
SELECT
  value::float8 AS value,
  unit,
  (extract(epoch FROM occurred_at) * 1000)::bigint AS ts
FROM body_metrics
WHERE user_id = %s AND kind = 'weight'
ORDER BY occurred_at DESC, id DESC
LIMIT 1
"""


@router.get("/agenda")
async def agenda(
    request: Request,
    authorization: str | None = Header(default=None),
):
    user_id = request.app.state.roster.user_for_header(authorization)
    if user_id is None:
        return Response(content=UNAUTHORIZED_BODY, status_code=401)

    async with request.app.state.db.with_tenant(user_id) as conn:
        cur = await conn.execute(_AGENDA_COMMITMENTS, (user_id,))
        commitment_rows = await cur.fetchall()
        cur = await conn.execute(_AGENDA_OCCURRENCES, (user_id, user_id))
        occurrence_rows = await cur.fetchall()
        cur = await conn.execute(_AGENDA_CALENDAR, (user_id,))
        calendar_rows = await cur.fetchall()
        cur = await conn.execute(_AGENDA_DONE, (user_id,))
        done_rows = await cur.fetchall()

    items = [
        {
            "id": r["id"],
            "kind": "commitment",
            "title": r["title"],
            "body": r["body"],
            "due": int(r["due"]),
            "overdue": r["overdue"],
        }
        for r in commitment_rows
    ]
    items.extend(
        {
            "id": r["id"],
            "kind": "recurring",
            "title": r["title"],
            "body": r["body"],
            "due": int(r["due"]),
            "overdue": r["overdue"],
        }
        for r in occurrence_rows
    )
    items.extend(
        {
            "id": r["id"],
            "kind": "calendar",
            "title": r["title"],
            "starts": int(r["starts"]),
            "ends": int(r["ends"]) if r["ends"] is not None else None,
        }
        for r in calendar_rows
    )
    items.sort(key=lambda item: item.get("due", item.get("starts")))
    # Done items ride after the live ones (newest completion first) rather
    # than interleaving: an undated commitment has no due to sort by, and the
    # client sections them separately anyway.
    items = items[:100]
    items.extend(
        {
            "id": r["id"],
            "kind": "commitment",
            "title": r["title"],
            "body": r["body"],
            "due": int(r["due"]) if r["due"] is not None else None,
            "done": True,
            "done_ts": int(r["done_ts"]),
        }
        for r in done_rows
    )
    return JSONResponse({"items": items})


@router.get("/measurements")
async def measurements(
    request: Request,
    authorization: str | None = Header(default=None),
):
    user_id = request.app.state.roster.user_for_header(authorization)
    if user_id is None:
        return Response(content=UNAUTHORIZED_BODY, status_code=401)

    async with request.app.state.db.with_tenant(user_id) as conn:
        cur = await conn.execute(_MEASUREMENTS, (user_id, user_id, user_id))
        rows = await cur.fetchall()

    entries = []
    for r in rows:
        if r["type"] == "meal":
            entries.append(
                {
                    "id": r["id"],
                    "type": "meal",
                    "note": r["note"],
                    "kcal": r["kcal"],
                    "protein_g": r["protein_g"],
                    "carb_g": r["carb_g"],
                    "fat_g": r["fat_g"],
                    "ts": int(r["ts"]),
                }
            )
        elif r["type"] == "workout":
            entries.append(
                {
                    "id": r["id"],
                    "type": "workout",
                    "activity": r["activity"],
                    "duration_min": r["duration_min"],
                    "kcal_burned": r["kcal_burned"],
                    "distance_km": r["distance_km"],
                    "note": r["note"],
                    "ts": int(r["ts"]),
                }
            )
        else:
            entries.append(
                {
                    "id": r["id"],
                    "type": "metric",
                    "kind": r["kind"],
                    "value": r["value"],
                    "unit": r["unit"],
                    "ts": int(r["ts"]),
                }
            )
    return JSONResponse({"entries": entries})


@router.get("/days")
async def days(
    request: Request,
    authorization: str | None = Header(default=None),
):
    user_id = request.app.state.roster.user_for_header(authorization)
    if user_id is None:
        return Response(content=UNAUTHORIZED_BODY, status_code=401)

    async with request.app.state.db.with_tenant(user_id) as conn:
        cur = await conn.execute(_TIMEZONE, (user_id,))
        timezone = (await cur.fetchone())["timezone"]

        cur = await conn.execute(
            _DAYS,
            (timezone, timezone, timezone, user_id, timezone, user_id),
        )
        rows = await cur.fetchall()
        cur = await conn.execute(_TARGETS, (user_id,))
        targets = (await cur.fetchone())["targets"]
        cur = await conn.execute(_WEIGHT, (user_id,))
        weight_row = await cur.fetchone()

    day_rows = [
        {
            "date": r["date"],
            "kcal": r["kcal"],
            "protein_g": r["protein_g"],
            "exercised": r["exercised"],
            "logged": r["logged"],
        }
        for r in rows
    ]
    weight = None
    if weight_row is not None:
        weight = {
            "value": weight_row["value"],
            "unit": weight_row["unit"],
            "ts": int(weight_row["ts"]),
        }
    return JSONResponse({"days": day_rows, "targets": targets, "weight": weight})
