"""Marking things done outside a commitment close: the Today tab's POST
/agenda/done, and the model's MarkCalendarDone for calendar deadlines
(Canvas items submitted via Gradescope)."""

from datetime import datetime, timedelta, timezone

import psycopg
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from inkling.api.auth import Roster
from inkling.api.server import create_app
from inkling.db.client import Database
from inkling.delivery.live_activity import next_up

from tests.conftest import USER_A, USER_B
from tests.test_commands import _run_turn, _validate

TOKEN_A = "token-for-a"
AUTH = {"Authorization": f"Bearer {TOKEN_A}"}


@pytest_asyncio.fixture(autouse=True)
async def clean(migrated: str, dsn: str):
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "TRUNCATE turn_events, turns, triggers, transcript, audit_log, "
            "events, deliveries, commitment_occurrences, commitments, "
            "calendar_events_cache RESTART IDENTITY CASCADE"
        )
        conn.commit()
    yield


@pytest_asyncio.fixture
async def client(db: Database):
    app = create_app(db, Roster({USER_A: TOKEN_A}))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _rows(db, sql, args=(), user_id=USER_A):
    async with db.with_tenant(user_id) as conn:
        cur = await conn.execute(sql, args)
        return await cur.fetchall()


async def _event(db, pid, summary, starts_in, *, user_id=USER_A):
    async with db.with_tenant(user_id) as conn:
        await conn.execute(
            "INSERT INTO calendar_events_cache (user_id, provider_event_id, "
            "calendar_id, raw, summary, starts_at, ends_at) "
            "VALUES (%s, %s, 'canvas', '{}', %s, now() + %s, now() + %s)",
            (user_id, pid, summary, starts_in, starts_in),
        )


# --- POST /agenda/done ----------------------------------------------------------

async def test_done_commitment_closes_it_and_logs_a_chat_line(client, db):
    await _run_turn(db, USER_A, [{"type": "UpsertCommitment", "title": "essay",
                                  "due_at": "2030-01-01T12:00:00Z"}])
    r = await client.post("/agenda/done", json={"id": "c:1"}, headers=AUTH)
    assert r.status_code == 200, r.text
    [row] = await _rows(db, "SELECT status, version FROM commitments")
    assert (row["status"], row["version"]) == ("done", 2)
    [line] = await _rows(db, "SELECT role, text, turn_id FROM transcript "
                             "WHERE text LIKE '✓%%'")
    assert line["role"] == "user" and line["turn_id"] is None
    assert line["text"] == "✓ Marked done in Today: essay"
    notes = await _rows(db, "SELECT source, note FROM events WHERE kind = 'system'")
    assert notes[-1]["source"] == "app"

    r = await client.post("/agenda/done", json={"id": "c:1"}, headers=AUTH)
    assert r.status_code == 409


async def test_done_recurring_checks_off_today_only(client, db):
    await _run_turn(db, USER_A, [{"type": "UpsertCommitment", "title": "stretch",
                                  "recurrence": "FREQ=DAILY"}])
    [occ] = await _rows(
        db,
        "INSERT INTO commitment_occurrences (user_id, commitment_id, fire_at) "
        "VALUES (%s, 1, now() + interval '1 minute') RETURNING id",
        (USER_A,),
    )
    r = await client.post("/agenda/done", json={"id": f"o:{occ['id']}"}, headers=AUTH)
    assert r.status_code == 200, r.text
    [c] = await _rows(db, "SELECT status FROM commitments")
    assert c["status"] == "open", "a habit stays open for tomorrow"
    [e] = await _rows(db, "SELECT commitment_id FROM events WHERE kind = 'habit_done'")
    assert e["commitment_id"] == 1


async def test_done_calendar_item_hides_it_from_card_and_agenda(client, db):
    await _event(db, "hw2", "HW2 - Programming [CS-7650-A]", timedelta(hours=5))
    await _event(db, "pa3", "Programming Assignment 3 [CS-8803-MCI]", timedelta(days=2))
    r = await client.post("/agenda/done", json={"id": "g:hw2"}, headers=AUTH)
    assert r.status_code == 200, r.text

    async with db.with_tenant(USER_A) as conn:
        card = await next_up(conn, USER_A)
    assert card["title"] == "Programming Assignment 3 [CS-8803-MCI]"

    items = (await client.get("/agenda", headers=AUTH)).json()["items"]
    hw2 = [i for i in items if i["id"] == "g:hw2"]
    assert len(hw2) == 1 and hw2[0]["done"] is True, "shown once, under Done"


async def test_done_rejects_bad_ids(client, db):
    await _event(db, "b-only", "theirs", timedelta(hours=1), user_id=USER_B)
    for body, status in (
        ({"id": "x:1"}, 422),
        ({"id": "c:abc"}, 422),
        ({"id": "c:99"}, 404),
        ({"id": "g:b-only"}, 404),  # RLS: another user's event isn't found
        ({}, 422),
    ):
        r = await client.post("/agenda/done", json=body, headers=AUTH)
        assert r.status_code == status, (body, r.text)
    assert (await client.post("/agenda/done", json={"id": "c:1"})).status_code == 401


# --- MarkCalendarDone (chat) ---------------------------------------------------

async def test_mark_calendar_done_by_exact_title(db):
    await _event(db, "hw2", "HW2 - Programming [CS-7650-A]", timedelta(days=3))
    await _run_turn(db, USER_A, [{"type": "MarkCalendarDone",
                                  "title": "hw2 - programming [cs-7650-a]"}])
    [row] = await _rows(db, "SELECT done_at FROM calendar_events_cache")
    assert row["done_at"] is not None

    await _run_turn(db, USER_A, [{"type": "MarkCalendarDone",
                                  "title": "HW2 - Programming [CS-7650-A]",
                                  "done": False}])
    [row] = await _rows(db, "SELECT done_at FROM calendar_events_cache")
    assert row["done_at"] is None, "undo clears the mark"


async def test_mark_calendar_done_rejects_unknown_and_ambiguous(db):
    await _event(db, "q1", "Quiz", timedelta(days=1))
    await _event(db, "q2", "Quiz", timedelta(days=8))
    verdict = await _validate(db, USER_A, [
        {"type": "MarkCalendarDone", "title": "HW9"},
        {"type": "MarkCalendarDone", "title": "Quiz"},
    ])
    assert not verdict.accepted
    assert "no calendar item titled 'HW9'" in verdict.rejected[0].reason
    assert "ask the user which one" in verdict.rejected[1].reason


async def test_sync_upsert_keeps_the_done_mark(db):
    from inkling.db.queries import calendar as calendar_q

    await _event(db, "hw2", "HW2", timedelta(days=1))
    async with db.with_tenant(USER_A) as conn:
        await calendar_q.set_done(conn, "hw2", done=True)
        await calendar_q.cache_upsert(
            conn, user_id=USER_A, provider_event_id="hw2", calendar_id="canvas",
            revision="r2", starts_at=datetime.now(timezone.utc) + timedelta(days=1),
            ends_at=None, summary="HW2 (edited)", status="confirmed",
            all_day=False, raw={},
        )
    [row] = await _rows(db, "SELECT summary, done_at FROM calendar_events_cache")
    assert row["summary"] == "HW2 (edited)" and row["done_at"] is not None
