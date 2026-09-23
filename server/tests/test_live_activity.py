"""The "next up" Live Activity (§11): token route, card derivation, push logic.

APNs is a fake recording (token, payload) and answering scripted statuses;
the tables are real (Neon test branch, runtime_role).
"""

from datetime import datetime, timedelta, timezone

import psycopg
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from inkling.api.auth import Roster
from inkling.api.server import create_app
from inkling.db.client import Database
from inkling.db.queries import live_activity as la_q
from inkling.delivery.live_activity import LiveActivitySync, next_up

from tests.conftest import USER_A, USER_B

TOKEN_A = "token-for-a"
START = "a" * 64
UPDATE = "b" * 64


@pytest_asyncio.fixture(autouse=True)
async def clean(migrated: str, dsn: str):
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "TRUNCATE live_activity, commitment_occurrences, commitments, "
            "calendar_events_cache, docs, turn_events, turns, triggers, "
            "transcript, audit_log, events RESTART IDENTITY CASCADE"
        )
        conn.commit()
    yield


@pytest_asyncio.fixture
async def client(db: Database):
    app = create_app(db, Roster({USER_A: TOKEN_A}))
    woken = []
    app.state.live_activity_wake = lambda: woken.append(1)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        c.woken = woken
        yield c


class FakeApns:
    def __init__(self, *statuses):
        self.statuses = list(statuses)
        self.sent: list[tuple[str, dict]] = []

    async def push_live_activity(self, *, token, environment, payload, priority=10):
        self.sent.append((token, payload))
        status = self.statuses.pop(0) if self.statuses else 200
        return status, "" if status == 200 else "Unregistered"


class Clock:
    def __init__(self):
        self.now = datetime.now(timezone.utc).timestamp()

    def __call__(self):
        return self.now


async def _commit(db, title, due_in: timedelta, user_id=USER_A):
    async with db.with_tenant(user_id) as conn:
        await conn.execute(
            "INSERT INTO commitments (user_id, title, due_at, source) "
            "VALUES (%s, %s, now() + %s, 'conversation')",
            (user_id, title, due_in),
        )


async def _event(db, summary, starts_in, ends_in=None, *, all_day=False,
                 calendar="canvas", user_id=USER_A):
    async with db.with_tenant(user_id) as conn:
        await conn.execute(
            "INSERT INTO calendar_events_cache (user_id, provider_event_id, "
            "calendar_id, raw, summary, starts_at, ends_at, all_day) "
            "VALUES (%s, %s, %s, '{}', %s, now() + %s, now() + %s, %s)",
            (user_id, f"evt-{summary}", calendar, summary, starts_in,
             starts_in if ends_in is None else ends_in, all_day),
        )


async def _row(db, user_id=USER_A):
    async with db.with_tenant(user_id) as conn:
        return await la_q.get(conn, user_id)


async def _register(db, *, start=START, enabled=True, user_id=USER_A):
    async with db.with_tenant(user_id) as conn:
        await la_q.register(
            conn, user_id=user_id, environment="development",
            start_token=start, enabled=enabled,
        )


# --- next_up -----------------------------------------------------------------

async def test_next_up_is_earliest_due_with_24h_more_count(db):
    await _commit(db, "later", timedelta(hours=5))
    await _commit(db, "overdue thing", timedelta(hours=-2))
    await _commit(db, "tomorrow night", timedelta(hours=40))
    await _commit(db, "other user's", timedelta(hours=-9), user_id=USER_B)
    async with db.with_tenant(USER_A) as conn:
        card = await next_up(conn, USER_A)
    assert card["title"] == "overdue thing"
    assert card["overdue"] is True
    assert card["kind"] == "commitment"
    assert card["more"] == 2
    assert abs(card["due"] - (datetime.now(timezone.utc).timestamp() - 7200)) < 60


async def test_next_up_takes_canvas_deadlines_not_meetings_or_habits(db):
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "INSERT INTO commitments (user_id, title, recurrence, source) "
            "VALUES (%s, 'stretch', 'FREQ=DAILY', 'conversation')",
            (USER_A,),
        )
    await _event(db, "Coffee Hour", timedelta(minutes=30), timedelta(minutes=90))
    await _event(db, "Team sync", timedelta(minutes=5), calendar="google-personal")
    await _event(db, "Quiz 3", timedelta(hours=-1))  # passed: drops off
    await _event(db, "Homework 2", timedelta(hours=3))
    await _event(db, "HW2 - Programming", timedelta(hours=1), timedelta(hours=25),
                 all_day=True)
    async with db.with_tenant(USER_A) as conn:
        card = await next_up(conn, USER_A)
    assert card["title"] == "Homework 2"
    assert card["kind"] == "deadline"
    assert card["overdue"] is False
    assert card["more"] == 1, "the all-day deadline, due at 11:59 PM that day"
    now = datetime.now(timezone.utc).timestamp()
    assert abs(card["due"] - (now + 3 * 3600)) < 60


async def test_next_up_all_clear(db):
    async with db.with_tenant(USER_A) as conn:
        assert await next_up(conn, USER_A) == {
            "title": "", "due": None, "overdue": False, "kind": None, "more": 0,
        }


# --- route ---------------------------------------------------------------------

async def test_route_stores_tokens_and_wakes(client, db):
    r = await client.post(
        "/live-activity",
        json={"environment": "development", "enabled": True,
              "start_token": START.upper(),
              "activity": {"id": "act-1", "token": UPDATE}},
        headers={"Authorization": f"Bearer {TOKEN_A}"},
    )
    assert r.status_code == 200, r.text
    row = await _row(db)
    assert (row["start_token"], row["update_token"], row["activity_id"]) == (
        START, UPDATE, "act-1",
    )
    assert client.woken

    # A launch before iOS re-issues the start token keeps the stored one.
    r = await client.post(
        "/live-activity",
        json={"environment": "development", "enabled": True, "activity": None},
        headers={"Authorization": f"Bearer {TOKEN_A}"},
    )
    assert r.status_code == 200
    row = await _row(db)
    assert row["start_token"] == START
    assert row["update_token"] is None, "no activity on screen → forget it"


async def test_route_disabled_clears_start_token(client, db):
    await _register(db)
    r = await client.post(
        "/live-activity",
        json={"environment": "development", "enabled": False, "start_token": START},
        headers={"Authorization": f"Bearer {TOKEN_A}"},
    )
    assert r.status_code == 200
    assert (await _row(db))["start_token"] is None


async def test_route_rejects_bad_input(client):
    headers = {"Authorization": f"Bearer {TOKEN_A}"}
    assert (await client.post("/live-activity", json={})).status_code == 401
    for body in (
        {"environment": "staging"},
        {"environment": "development", "start_token": "not hex"},
        {"environment": "development", "activity": {"id": "x"}},
        {"environment": "development", "enabled": "yes"},
    ):
        r = await client.post("/live-activity", json=body, headers=headers)
        assert r.status_code == 422, body


# --- sync ----------------------------------------------------------------------

async def test_sync_starts_then_updates_only_on_change(db):
    await _register(db)
    await _commit(db, "stretch", timedelta(hours=1))
    apns, clock = FakeApns(), Clock()
    sync = LiveActivitySync(db, [USER_A], apns, clock=clock)

    assert await sync.sync_user(USER_A) == "started"
    token, payload = apns.sent[-1]
    assert token == START
    aps = payload["aps"]
    assert aps["event"] == "start"
    assert aps["attributes-type"] == "NextUpAttributes"
    assert aps["content-state"]["title"] == "stretch"
    assert "sound" not in aps

    # The app reports the new activity's token (the post-start wake).
    async with db.with_tenant(USER_A) as conn:
        await la_q.set_activity(conn, USER_A, activity_id="act", update_token=UPDATE)
    assert await sync.sync_user(USER_A) == "unchanged"

    await _commit(db, "sooner", timedelta(minutes=10))
    assert await sync.sync_user(USER_A) == "updated"
    token, payload = apns.sent[-1]
    assert token == UPDATE
    assert payload["aps"]["event"] == "update"
    assert payload["aps"]["content-state"]["title"] == "sooner"
    assert payload["aps"]["content-state"]["more"] == 1
    assert len(apns.sent) == 2


async def test_switching_activity_resends_the_card(db):
    await _register(db)
    await _commit(db, "stretch", timedelta(hours=1))
    apns = FakeApns()
    sync = LiveActivitySync(db, [USER_A], apns, clock=Clock())
    await sync.sync_user(USER_A)
    async with db.with_tenant(USER_A) as conn:
        await la_q.set_activity(conn, USER_A, activity_id="act", update_token=UPDATE)
    assert await sync.sync_user(USER_A) == "unchanged"
    async with db.with_tenant(USER_A) as conn:
        await la_q.set_activity(conn, USER_A, activity_id="older", update_token="c" * 64)
    assert await sync.sync_user(USER_A) == "updated"
    assert apns.sent[-1][0] == "c" * 64


async def test_sync_restarts_before_ios_8h_limit(db):
    await _register(db)
    apns, clock = FakeApns(), Clock()
    sync = LiveActivitySync(db, [USER_A], apns, clock=clock)
    assert await sync.sync_user(USER_A) == "started"
    async with db.with_tenant(USER_A) as conn:
        await la_q.set_activity(conn, USER_A, activity_id="act", update_token=UPDATE)
        await conn.execute(
            "UPDATE live_activity SET started_at = now() - interval '7 hours 50 minutes'"
        )
    assert await sync.sync_user(USER_A) == "started"
    events = [(t, p["aps"]["event"]) for t, p in apns.sent]
    assert events == [(START, "start"), (UPDATE, "end"), (START, "start")]


async def test_sync_orphaned_start_restarts_on_change(db):
    """No update token ever arrived: a change can't update in place."""
    await _register(db)
    apns, clock = FakeApns(), Clock()
    sync = LiveActivitySync(db, [USER_A], apns, clock=clock)
    assert await sync.sync_user(USER_A) == "started"
    await _commit(db, "new thing", timedelta(hours=2))
    assert await sync.sync_user(USER_A) == "waiting for token"
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "UPDATE live_activity SET started_at = now() - interval '11 minutes'"
        )
    assert await sync.sync_user(USER_A) == "started"


async def test_dead_start_token_is_dropped(db):
    await _register(db)
    sync = LiveActivitySync(db, [USER_A], FakeApns(410), clock=Clock())
    assert await sync.sync_user(USER_A) == "start failed (410)"
    assert (await _row(db))["start_token"] is None
    assert await sync.sync_user(USER_A) == "disabled"


async def test_empty_report_right_after_start_is_ignored(db):
    await _register(db)
    sync = LiveActivitySync(db, [USER_A], FakeApns(), clock=Clock())
    await sync.sync_user(USER_A)
    async with db.with_tenant(USER_A) as conn:
        assert not await la_q.clear_activity(conn, USER_A)
    assert (await _row(db))["started_at"] is not None


# --- now / next ------------------------------------------------------------------

async def _class(db, summary, starts_in, minutes=75, *, location="Klaus 1447"):
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "INSERT INTO calendar_events_cache (user_id, provider_event_id, "
            "calendar_id, raw, summary, starts_at, ends_at) "
            "VALUES (%s, %s, 'icloud-gt', %s::jsonb, %s, now() + %s, now() + %s)",
            (USER_A, f"cls-{summary}", f'{{"location": "{location}"}}', summary,
             starts_in, starts_in + timedelta(minutes=minutes)),
        )


async def _focus(db, text, *, days_ago=0):
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "INSERT INTO docs (user_id, kind, version, content, written_by, created_at) "
            "VALUES (%s, 'focus', (SELECT coalesce(max(version), 0) + 1 FROM docs "
            "WHERE user_id = %s AND kind = 'focus'), %s, 'test', now() - %s)",
            (USER_A, USER_A, text, timedelta(days=days_ago)),
        )


async def _now_next(db):
    from inkling.delivery.live_activity import now_next

    async with db.with_tenant(USER_A) as conn:
        return await now_next(conn, USER_A)


async def test_in_class_next_is_the_focus_not_tomorrows_lecture(db):
    await _class(db, "Natural Language", timedelta(minutes=-40))
    await _class(db, "Digital Health", timedelta(hours=20), location="Tech Sq 118")
    await _focus(db, "Record the BCG video")
    await _event(db, "HW2 - Programming", timedelta(days=2))
    card = await _now_next(db)
    assert (card["nowKind"], card["nowTitle"], card["nowDetail"]) == (
        "event", "Natural Language", "Klaus 1447",
    )
    assert abs(card["nowUntil"] - (datetime.now(timezone.utc).timestamp() + 35 * 60)) < 60
    assert (card["nextKind"], card["nextTitle"], card["nextAt"]) == (
        "focus", "Record the BCG video", None,
    )


async def test_back_to_back_class_is_next(db):
    await _class(db, "Machine Learning Theory", timedelta(minutes=-60))
    await _class(db, "Data Science for Epidemiology", timedelta(minutes=30),
                 location="East Architecture 309")
    await _focus(db, "Record the BCG video")
    card = await _now_next(db)
    assert (card["nextKind"], card["nextTitle"], card["nextDetail"]) == (
        "event", "Data Science for Epidemiology", "East Architecture 309",
    )


async def test_between_classes_now_is_focus_and_next_a_class_only_if_soon(db):
    await _focus(db, "Stale focus", days_ago=2)
    await _focus(db, "Record the BCG video")
    await _event(db, "HW2 - Programming", timedelta(days=2))
    await _class(db, "Evening seminar", timedelta(hours=5))  # too far out
    card = await _now_next(db)
    assert (card["nowKind"], card["nowTitle"]) == ("focus", "Record the BCG video")
    assert (card["nextKind"], card["nextTitle"]) == ("deadline", "HW2 - Programming")


async def test_upcoming_deadlines_outrank_old_overdue_chores(db):
    await _focus(db, "Yesterday's focus", days_ago=1)
    await _commit(db, "Buy a dress shirt", timedelta(days=-4))
    await _commit(db, "BCG video", timedelta(hours=3))
    await _event(db, "HW2 - Programming", timedelta(days=2))
    card = await _now_next(db)
    assert (card["nowKind"], card["nowTitle"], card["nowOverdue"]) == (
        "deadline", "BCG video", False,
    )
    assert (card["nextKind"], card["nextTitle"]) == ("deadline", "HW2 - Programming")


async def test_only_overdue_left_still_shows(db):
    await _commit(db, "Buy a dress shirt", timedelta(days=-4))
    card = await _now_next(db)
    assert (card["nowTitle"], card["nowOverdue"]) == ("Buy a dress shirt", True)


async def test_all_clear(db):
    card = await _now_next(db)
    assert card["nowKind"] == "clear" and card["nextKind"] is None


async def test_set_focus_command_validates_and_lands_in_docs(db):
    from tests.test_commands import _run_turn, _validate

    verdict = await _validate(db, USER_A, [
        {"type": "SetFocus", "text": "x" * 61, "reason": "r"},
        {"type": "SetFocus", "text": "Draft PA3 writeup", "reason": ""},
    ])
    assert "under 60" in verdict.rejected[0].reason
    assert "reason is required" in verdict.rejected[1].reason
    await _run_turn(db, USER_A, [
        {"type": "SetFocus", "text": "  Draft   PA3 writeup ", "reason": "due Mon"},
    ])
    card = await _now_next(db)
    assert (card["nowKind"], card["nowTitle"]) == ("focus", "Draft PA3 writeup")
