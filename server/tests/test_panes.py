"""Pane projections are tested together because they share one read contract.

The fixtures seed through the migration owner with an explicit tenant context:
FORCE RLS applies there too. API assertions then use the runtime pool so shape,
authentication, and tenant isolation are exercised exactly as in production.
"""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from inkling.api.auth import Roster
from inkling.api.server import create_app
from inkling.db.client import Database

from tests.conftest import USER_A, USER_B

TOKEN_A = "token-for-a"
TOKEN_B = "token-for-b"
LOCAL_TIMEZONE = ZoneInfo("America/New_York")


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture(autouse=True)
async def seeded(migrated: str, dsn: str):
    """Reset pane tables and seed every edge through owner-scoped RLS."""
    now = datetime.now(UTC).replace(microsecond=0)
    local_today = now.astimezone(LOCAL_TIMEZONE).date()
    utc_bucket_instant = datetime.combine(
        local_today - timedelta(days=1), time(hour=0, minute=30), UTC
    )

    with psycopg.connect(dsn) as conn:
        conn.execute(
            "TRUNCATE commitment_occurrences, commitments, "
            "calendar_events_cache, intake, workouts, body_metrics "
            "RESTART IDENTITY CASCADE"
        )
        conn.commit()

        conn.execute("BEGIN")
        conn.execute("SELECT set_config('app.user_id', %s, true)", (USER_A,))
        conn.execute(
            "UPDATE users SET targets = "
            "'{\"kcal_quota\": 2200, \"protein_quota_g\": 140}' "
            "WHERE id = %s",
            (USER_A,),
        )

        overdue_id = conn.execute(
            """
            INSERT INTO commitments
              (user_id, title, body, due_at, status, source)
            VALUES (%s, 'Pay electricity bill', NULL, %s, 'open', 'conversation')
            RETURNING id
            """,
            (USER_A, now - timedelta(hours=4)),
        ).fetchone()[0]
        done_id = conn.execute(
            """
            INSERT INTO commitments
              (user_id, title, due_at, status, source)
            VALUES (%s, 'Already done', %s, 'done', 'conversation')
            RETURNING id
            """,
            (USER_A, now - timedelta(hours=3)),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO commitments
              (user_id, title, due_at, status, source, updated_at)
            VALUES (%s, 'Done long ago', %s, 'done', 'conversation', %s)
            """,
            (USER_A, now - timedelta(days=2), now - timedelta(hours=25)),
        )
        conn.execute(
            """
            INSERT INTO commitments
              (user_id, title, due_at, status, source)
            VALUES (%s, 'Beyond the window', %s, 'open', 'conversation')
            """,
            (USER_A, now + timedelta(hours=49)),
        )
        recurring_id = conn.execute(
            """
            INSERT INTO commitments
              (user_id, title, body, recurrence, status, source)
            VALUES (
              %s, 'Evening review', NULL, 'FREQ=DAILY', 'open', 'conversation'
            )
            RETURNING id
            """,
            (USER_A,),
        ).fetchone()[0]
        occurrence_id = conn.execute(
            """
            INSERT INTO commitment_occurrences
              (user_id, commitment_id, fire_at, status)
            VALUES (%s, %s, %s, 'pending')
            RETURNING id
            """,
            (USER_A, recurring_id, now + timedelta(hours=1)),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO calendar_events_cache
              (user_id, provider_event_id, calendar_id, starts_at, ends_at,
               summary, raw)
            VALUES (%s, 'evt-abc', 'primary', %s, %s, 'Dentist', '{}')
            """,
            (USER_A, now + timedelta(hours=2), now + timedelta(hours=3)),
        )

        original_meal_id = conn.execute(
            """
            INSERT INTO intake
              (user_id, occurred_at, note, kcal, protein_g, carb_g, fat_g)
            VALUES (%s, %s, 'wrong meal', 900, 10, 140, 20)
            RETURNING id
            """,
            (USER_A, now - timedelta(hours=2)),
        ).fetchone()[0]
        corrected_meal_id = conn.execute(
            """
            INSERT INTO intake
              (user_id, occurred_at, note, kcal, protein_g, carb_g, fat_g,
               supersedes_id)
            VALUES (%s, %s, 'dal + rice', 700, 24, 110, 12, %s)
            RETURNING id
            """,
            (USER_A, now - timedelta(hours=1), original_meal_id),
        ).fetchone()[0]
        bucket_meal_id = conn.execute(
            """
            INSERT INTO intake
              (user_id, occurred_at, note, kcal, protein_g)
            VALUES (%s, %s, 'midnight UTC meal', 540, 30)
            RETURNING id
            """,
            (USER_A, utc_bucket_instant),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO intake (user_id, occurred_at, note, kcal, protein_g)
            VALUES (%s, %s, 'old meal', 300, 12)
            """,
            (USER_A, now - timedelta(days=8, hours=1)),
        )

        workout_at = now - timedelta(hours=2)
        workout_id = conn.execute(
            """
            INSERT INTO workouts
              (user_id, occurred_at, activity_type, duration_min, kcal_burned,
               distance_km, note)
            VALUES (%s, %s, 'running', 30, 260, 5, 'easy pace')
            RETURNING id
            """,
            (USER_A, workout_at),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO workouts
              (user_id, occurred_at, activity_type, duration_min)
            VALUES (%s, %s, 'walking', 20)
            """,
            (USER_A, now - timedelta(days=8, hours=1)),
        )

        weight_at = now - timedelta(hours=3)
        weight_id = conn.execute(
            """
            INSERT INTO body_metrics
              (user_id, occurred_at, kind, value, unit)
            VALUES (%s, %s, 'weight', 82.4, 'kg')
            RETURNING id
            """,
            (USER_A, weight_at),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO body_metrics
              (user_id, occurred_at, kind, value, unit)
            VALUES (%s, %s, 'weight', 83.1, 'kg')
            """,
            (USER_A, now - timedelta(days=8, hours=1)),
        )
        conn.execute("COMMIT")

        conn.execute("BEGIN")
        conn.execute("SELECT set_config('app.user_id', %s, true)", (USER_B,))
        conn.execute("UPDATE users SET targets = '{}' WHERE id = %s", (USER_B,))
        conn.execute("COMMIT")

    yield {
        "now": now,
        "local_today": local_today,
        "bucket_date": utc_bucket_instant.astimezone(LOCAL_TIMEZONE).date(),
        "workout_date": workout_at.astimezone(LOCAL_TIMEZONE).date(),
        "weight_at": weight_at,
        "overdue_id": overdue_id,
        "done_id": done_id,
        "occurrence_id": occurrence_id,
        "corrected_meal_id": corrected_meal_id,
        "bucket_meal_id": bucket_meal_id,
        "workout_id": workout_id,
        "weight_id": weight_id,
    }


@pytest.fixture
def roster() -> Roster:
    return Roster({USER_A: TOKEN_A, USER_B: TOKEN_B})


@pytest_asyncio.fixture
async def client(db: Database, roster: Roster):
    app = create_app(db, roster)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.mark.parametrize("path", ["/agenda", "/measurements", "/days"])
async def test_panes_without_token_are_bare_401(client, path):
    response = await client.get(path)

    assert response.status_code == 401
    assert response.text == "unauthorized"


async def test_agenda_merges_typed_items_in_time_order(client, seeded):
    response = await client.get("/agenda", headers=auth(TOKEN_A))

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["kind"] for item in items] == [
        "commitment",
        "recurring",
        "calendar",
        "commitment",
    ]
    assert items[0] == {
        "id": f"c:{seeded['overdue_id']}",
        "kind": "commitment",
        "title": "Pay electricity bill",
        "body": None,
        "due": int((seeded["now"] - timedelta(hours=4)).timestamp() * 1000),
        "overdue": True,
    }
    assert items[1]["id"] == f"o:{seeded['occurrence_id']}"
    assert set(items[1]) == {"id", "kind", "title", "body", "due", "overdue"}
    assert items[1]["overdue"] is False
    assert items[2]["id"] == "g:evt-abc"
    assert set(items[2]) == {"id", "kind", "title", "starts", "ends"}
    live = items[:3]
    assert [item.get("due", item.get("starts")) for item in live] == sorted(
        item.get("due", item.get("starts")) for item in live
    )
    done = items[3]
    assert set(done) == {"id", "kind", "title", "body", "due", "done", "done_ts"}
    assert done["id"] == f"c:{seeded['done_id']}"
    assert done["title"] == "Already done"
    assert done["done"] is True
    assert done["due"] == int(
        (seeded["now"] - timedelta(hours=3)).timestamp() * 1000
    )
    assert isinstance(done["done_ts"], int)
    assert not any(item["title"] == "Done long ago" for item in items)
    assert not any(item["title"] == "Beyond the window" for item in items)


async def test_measurements_are_typed_recent_and_newest_first(client, seeded):
    response = await client.get("/measurements", headers=auth(TOKEN_A))

    assert response.status_code == 200
    entries = response.json()["entries"]
    assert [entry["ts"] for entry in entries] == sorted(
        (entry["ts"] for entry in entries), reverse=True
    )
    assert all(
        entry["ts"] > int((seeded["now"] - timedelta(days=7)).timestamp() * 1000)
        for entry in entries
    )
    assert not any(entry.get("note") == "wrong meal" for entry in entries)
    assert not any(entry.get("note") == "old meal" for entry in entries)
    assert not any(entry.get("activity") == "walking" for entry in entries)

    meal = next(
        entry
        for entry in entries
        if entry["id"] == f"i:{seeded['corrected_meal_id']}"
    )
    assert meal == {
        "id": f"i:{seeded['corrected_meal_id']}",
        "type": "meal",
        "note": "dal + rice",
        "kcal": 700.0,
        "protein_g": 24.0,
        "carb_g": 110.0,
        "fat_g": 12.0,
        "ts": int((seeded["now"] - timedelta(hours=1)).timestamp() * 1000),
    }
    workout = next(
        entry for entry in entries if entry["id"] == f"w:{seeded['workout_id']}"
    )
    assert set(workout) == {
        "id",
        "type",
        "activity",
        "duration_min",
        "kcal_burned",
        "distance_km",
        "note",
        "ts",
    }
    assert workout["duration_min"] == 30.0
    assert workout["distance_km"] == 5.0
    weight = next(
        entry for entry in entries if entry["id"] == f"b:{seeded['weight_id']}"
    )
    assert set(weight) == {"id", "type", "kind", "value", "unit", "ts"}
    assert weight["value"] == 82.4


async def test_days_bucket_locally_fill_gaps_and_echo_summary(client, seeded):
    response = await client.get("/days", headers=auth(TOKEN_A))

    assert response.status_code == 200
    body = response.json()
    days = body["days"]
    assert len(days) == 84
    assert [day["date"] for day in days] == sorted(day["date"] for day in days)
    assert days[-1]["date"] == seeded["local_today"].isoformat()

    bucket_day = next(
        day for day in days if day["date"] == seeded["bucket_date"].isoformat()
    )
    assert bucket_day["kcal"] == 540.0
    assert bucket_day["protein_g"] == 30.0
    assert bucket_day["logged"] is True
    assert seeded["bucket_date"] != (
        seeded["local_today"] - timedelta(days=1)
    ), "the fixture must cross a UTC/local date boundary"

    gap_date = seeded["local_today"] - timedelta(days=30)
    gap = next(day for day in days if day["date"] == gap_date.isoformat())
    assert gap == {
        "date": gap_date.isoformat(),
        "kcal": 0.0,
        "protein_g": 0.0,
        "exercised": False,
        "logged": False,
    }
    workout_day = next(
        day for day in days if day["date"] == seeded["workout_date"].isoformat()
    )
    assert workout_day["exercised"] is True
    assert body["targets"] == {"kcal_quota": 2200, "protein_quota_g": 140}
    assert body["weight"] == {
        "value": 82.4,
        "unit": "kg",
        "ts": int(seeded["weight_at"].timestamp() * 1000),
    }


async def test_empty_tenant_gets_only_empty_projections(client):
    headers = auth(TOKEN_B)

    agenda = (await client.get("/agenda", headers=headers)).json()
    measurements = (await client.get("/measurements", headers=headers)).json()
    summary = (await client.get("/days", headers=headers)).json()

    assert agenda == {"items": []}
    assert measurements == {"entries": []}
    assert len(summary["days"]) == 84
    assert all(
        day["kcal"] == 0.0
        and day["protein_g"] == 0.0
        and day["exercised"] is False
        and day["logged"] is False
        for day in summary["days"]
    )
    assert summary["targets"] == {}
    assert summary["weight"] is None
