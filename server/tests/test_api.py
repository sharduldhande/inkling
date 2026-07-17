"""API layer (§6) and the v1 wire contract (D8).

The v1 assertions here are deliberately literal: they compare against bytes
copied from habit-enforcer-server/server.js, because the iOS app ships
unmodified across the cutover and a plausible-but-wrong string would pass any
shape-based test.
"""

import json
import time

import psycopg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from inkling.api.auth import Roster, load_roster
from inkling.api.broker import Broker
from inkling.api.routes_chat import POLL_SECONDS
from inkling.api.server import create_app
from inkling.db.client import Database
from inkling.inference.gateway import StubGateway
from inkling.turns.supervisor import Supervisor

from tests.conftest import USER_A, USER_B

TOKEN_A = "token-for-a"
TOKEN_B = "token-for-b"


@pytest_asyncio.fixture(autouse=True)
async def clean(migrated: str, dsn: str):
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "TRUNCATE turn_events, turns, triggers, transcript, audit_log "
            "RESTART IDENTITY CASCADE"
        )
        conn.commit()
    yield


@pytest.fixture
def roster() -> Roster:
    return Roster({USER_A: TOKEN_A, USER_B: TOKEN_B})


@pytest_asyncio.fixture
async def client(db: Database, roster: Roster):
    """App with live workers — the `all` role shape."""
    broker = Broker()
    supervisor = Supervisor(
        db, roster.user_ids, StubGateway("hello"), subscriber=broker.publish
    )
    app = create_app(db, roster, broker=broker, supervisor=supervisor)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        async with app.router.lifespan_context(app):
            yield c


@pytest_asyncio.fixture
async def quiet_client(db: Database, roster: Roster):
    """App with no workers: triggers are recorded but nothing turns them."""
    app = create_app(db, roster)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- auth -------------------------------------------------------------------


async def test_chat_without_token_is_v1_401(quiet_client):
    r = await quiet_client.post("/chat", json={"message": "hi"})

    assert r.status_code == 401
    assert r.text == "unauthorized"


async def test_history_without_token_is_v1_401(quiet_client):
    r = await quiet_client.get("/history")

    assert r.status_code == 401
    assert r.text == "unauthorized"


async def test_wrong_token_is_rejected(quiet_client):
    r = await quiet_client.post("/chat", json={"message": "hi"}, headers=auth("nope"))

    assert r.status_code == 401


async def test_lowercase_bearer_scheme_is_rejected(quiet_client):
    """v1 compares the header with `=== 'Bearer ' + TOKEN`, byte for byte."""
    r = await quiet_client.post(
        "/chat", json={"message": "hi"}, headers={"Authorization": f"bearer {TOKEN_A}"}
    )

    assert r.status_code == 401


async def test_token_identifies_the_user(client, db: Database):
    await client.post("/chat", json={"message": "from b"}, headers=auth(TOKEN_B))

    async with db.with_tenant(USER_B) as conn:
        cur = await conn.execute("SELECT user_id, payload FROM triggers")
        rows = await cur.fetchall()
    assert rows[0]["user_id"] == USER_B
    assert rows[0]["payload"] == {"text": "from b"}


# --- /health ----------------------------------------------------------------


async def test_health_is_v1_shape_and_needs_no_auth(quiet_client):
    r = await quiet_client.get("/health")

    assert r.status_code == 200
    assert r.json() == {"status": "ok", "busy": False}


async def test_health_busy_reflects_the_tokens_user(quiet_client, db: Database):
    from inkling.turns import ingress
    from inkling.turns.worker import Worker

    await ingress.submit(db, user_id=USER_A, source="chat", payload={"text": "hi"})
    assert await Worker(db, USER_A, StubGateway()).claim() is not None

    a = await quiet_client.get("/health", headers=auth(TOKEN_A))
    b = await quiet_client.get("/health", headers=auth(TOKEN_B))
    anon = await quiet_client.get("/health")

    assert a.json() == {"status": "ok", "busy": True}
    assert b.json() == {"status": "ok", "busy": False}  # A's turn is not B's
    assert anon.json() == {"status": "ok", "busy": False}


# --- /chat request validation -----------------------------------------------


async def test_missing_message_is_v1_400(quiet_client):
    r = await quiet_client.post("/chat", json={"nope": 1}, headers=auth(TOKEN_A))

    assert r.status_code == 400
    assert r.text == "bad request"


async def test_non_string_message_is_v1_400(quiet_client):
    r = await quiet_client.post("/chat", json={"message": 42}, headers=auth(TOKEN_A))

    assert r.status_code == 400
    assert r.text == "bad request"


async def test_unparseable_body_is_v1_400(quiet_client):
    r = await quiet_client.post(
        "/chat", content=b"not json", headers=auth(TOKEN_A)
    )

    assert r.status_code == 400


async def test_unknown_path_is_bare_404(quiet_client):
    r = await quiet_client.get("/nope")

    assert r.status_code == 404
    assert r.text == ""


# --- busy contract (§5.1, D8) -----------------------------------------------


async def test_busy_returns_the_exact_v1_body(quiet_client, db: Database):
    from inkling.turns import ingress
    from inkling.turns.worker import Worker

    await ingress.submit(db, user_id=USER_A, source="chat", payload={"text": "first"})
    assert await Worker(db, USER_A, StubGateway()).claim() is not None

    r = await quiet_client.post(
        "/chat", json={"message": "second"}, headers=auth(TOKEN_A)
    )

    assert r.status_code == 429
    assert r.text == "brain is busy, try again in a minute"
    assert r.headers["content-type"].startswith("text/plain")


async def test_busy_still_persists_the_trigger(quiet_client, db: Database):
    """D8: the app keeps v1 behaviour, but the message must not be lost."""
    from inkling.turns import ingress
    from inkling.turns.worker import Worker

    await ingress.submit(db, user_id=USER_A, source="chat", payload={"text": "first"})
    assert await Worker(db, USER_A, StubGateway()).claim() is not None

    r = await quiet_client.post(
        "/chat", json={"message": "not lost"}, headers=auth(TOKEN_A)
    )
    assert r.status_code == 429

    async with db.with_tenant(USER_A) as conn:
        cur = await conn.execute(
            "SELECT payload, status FROM triggers WHERE status = 'pending'"
        )
        rows = await cur.fetchall()
    assert [r["payload"]["text"] for r in rows] == ["not lost"]


async def test_own_turn_does_not_count_as_busy(db: Database):
    """The race the route guards against: the worker claims a just-submitted
    trigger before the route's busy check runs. That turn belongs to this
    request and must not 429 it; any *other* trigger's turn must."""
    from inkling.db.queries import turns as turns_q
    from inkling.turns import ingress
    from inkling.turns.worker import Worker

    sub = await ingress.submit(db, user_id=USER_A, source="chat", payload={"text": "x"})
    assert await Worker(db, USER_A, StubGateway()).claim() is not None

    async with db.with_tenant(USER_A) as conn:
        own = await turns_q.running_for_other_trigger(conn, USER_A, sub.trigger_id)
        other = await turns_q.running_for_other_trigger(conn, USER_A, sub.trigger_id + 1)

    assert own is None, "a request 429ed by its own turn"
    assert other is not None


async def test_a_busy_user_does_not_block_another(db: Database, roster: Roster):
    """B streams normally while A has a turn in flight.

    A's worker is deliberately left out of the supervisor: with one running,
    it would race this test for A's trigger and finish the turn, so A would no
    longer be busy and the test would assert nothing.
    """
    from inkling.turns import ingress
    from inkling.turns.worker import Worker

    await ingress.submit(db, user_id=USER_A, source="chat", payload={"text": "x"})
    assert await Worker(db, USER_A, StubGateway()).claim() is not None

    broker = Broker()
    supervisor = Supervisor(db, [USER_B], StubGateway("hi"), subscriber=broker.publish)
    app = create_app(db, roster, broker=broker, supervisor=supervisor)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        async with app.router.lifespan_context(app):
            r = await c.post("/chat", json={"message": "hi"}, headers=auth(TOKEN_B))

    assert r.status_code == 200
    assert parse_sse(r.text)[-1] == {"exit": 0}


# --- /chat streaming (§5.5) -------------------------------------------------


def parse_sse(text: str) -> list[dict]:
    return [
        json.loads(line[len("data: ") :])
        for line in text.splitlines()
        if line.startswith("data: ")
    ]


async def test_chat_streams_v1_frames(client):
    r = await client.post("/chat", json={"message": "hi"}, headers=auth(TOKEN_A))

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"

    frames = parse_sse(r.text)
    assert "".join(f["text"] for f in frames if "text" in f) == "hello"
    assert frames[-2] == {"done": True}
    assert frames[-1] == {"exit": 0}


async def test_chat_records_the_exchange(client, db: Database):
    await client.post("/chat", json={"message": "ping"}, headers=auth(TOKEN_A))

    async with db.with_tenant(USER_A) as conn:
        cur = await conn.execute("SELECT role, text FROM transcript ORDER BY id")
        rows = await cur.fetchall()
    assert [(r["role"], r["text"]) for r in rows] == [
        ("user", "ping"),
        ("assistant", "hello"),
    ]


async def test_stream_is_driven_by_the_broker_not_the_poll(client):
    """Regression: the /chat subscription must outlive the route function.

    Returning StreamingResponse from inside `async with broker.subscribe(...)`
    unsubscribes the queue before the generator runs, so every event misses the
    live path and the stream limps along on the POLL_SECONDS fallback. Still
    correct, just needlessly slow — which only shows up as timing.
    """
    started = time.monotonic()
    r = await client.post("/chat", json={"message": "hi"}, headers=auth(TOKEN_A))
    elapsed = time.monotonic() - started

    assert r.status_code == 200
    assert elapsed < POLL_SECONDS, (
        f"stream took {elapsed:.1f}s: the broker is not waking it and it is "
        f"falling back to the {POLL_SECONDS}s poll"
    )


async def test_stream_frames_are_byte_exact(client):
    """v1 wrote `data: ` + compact JSON + a blank line. No SSE id/event fields."""
    r = await client.post("/chat", json={"message": "hi"}, headers=auth(TOKEN_A))

    assert r.text.endswith('data: {"exit":0}\n\n')
    assert 'data: {"done":true}\n\n' in r.text
    assert "event:" not in r.text
    assert "id:" not in r.text


# --- /history (§6) ----------------------------------------------------------


async def test_history_is_v1_shape(client, db: Database):
    await client.post("/chat", json={"message": "ping"}, headers=auth(TOKEN_A))

    r = await client.get("/history", headers=auth(TOKEN_A))

    body = r.json()
    assert list(body) == ["messages"]
    msg = body["messages"][0]
    assert set(msg) == {"id", "role", "text", "ts"}
    assert msg["role"] == "user"
    assert msg["text"] == "ping"
    assert isinstance(msg["id"], str), "v1 ids are strings; a number breaks decoding"
    assert isinstance(msg["ts"], int), "v1 ts is epoch milliseconds"


async def test_history_ts_is_epoch_millis(client):
    await client.post("/chat", json={"message": "ping"}, headers=auth(TOKEN_A))

    r = await client.get("/history", headers=auth(TOKEN_A))

    ts = r.json()["messages"][0]["ts"]
    assert 1_700_000_000_000 < ts < 2_000_000_000_000  # ms, not seconds


async def test_history_since_filters_strictly_greater(client):
    await client.post("/chat", json={"message": "one"}, headers=auth(TOKEN_A))
    first = (await client.get("/history", headers=auth(TOKEN_A))).json()["messages"]
    cutoff = first[-1]["ts"]

    r = await client.get(f"/history?since={cutoff}", headers=auth(TOKEN_A))

    assert all(m["ts"] > cutoff for m in r.json()["messages"])


async def test_history_is_tenant_scoped(client):
    await client.post("/chat", json={"message": "a-secret"}, headers=auth(TOKEN_A))

    r = await client.get("/history", headers=auth(TOKEN_B))

    assert r.json() == {"messages": []}


async def test_history_caps_at_200(client, db: Database):
    async with db.with_tenant(USER_A) as conn:
        for i in range(205):
            await conn.execute(
                "INSERT INTO transcript (user_id, role, text) VALUES (%s, 'user', %s)",
                (USER_A, f"m{i}"),
            )

    r = await client.get("/history", headers=auth(TOKEN_A))

    messages = r.json()["messages"]
    assert len(messages) == 200
    assert messages[-1]["text"] == "m204"  # the *most recent* 200, v1's slice(-200)


# --- resume (§5.5) ----------------------------------------------------------


async def test_turn_events_replays_persisted_stream(client, db: Database):
    await client.post("/chat", json={"message": "hi"}, headers=auth(TOKEN_A))
    async with db.with_tenant(USER_A) as conn:
        cur = await conn.execute("SELECT id FROM turns")
        turn_id = (await cur.fetchone())["id"]

    r = await client.get(f"/turns/{turn_id}/events", headers=auth(TOKEN_A))

    frames = parse_sse(r.text)
    assert "".join(f["text"] for f in frames if "text" in f) == "hello"
    assert frames[-1] == {"exit": 0}


async def test_last_event_id_resumes_after_seq(client, db: Database):
    await client.post("/chat", json={"message": "hi"}, headers=auth(TOKEN_A))
    async with db.with_tenant(USER_A) as conn:
        cur = await conn.execute("SELECT id FROM turns")
        turn_id = (await cur.fetchone())["id"]

    r = await client.get(
        f"/turns/{turn_id}/events",
        headers={**auth(TOKEN_A), "Last-Event-ID": f"{turn_id}:2"},
    )

    frames = parse_sse(r.text)
    assert not any(f.get("text") == "hell" for f in frames)  # seq 1-2 skipped
    assert frames[-1] == {"exit": 0}


async def test_replay_cannot_cross_tenants(client, db: Database):
    await client.post("/chat", json={"message": "hi"}, headers=auth(TOKEN_A))
    async with db.with_tenant(USER_A) as conn:
        cur = await conn.execute("SELECT id FROM turns")
        turn_id = (await cur.fetchone())["id"]

    r = await client.get(f"/turns/{turn_id}/events", headers=auth(TOKEN_B))

    assert parse_sse(r.text) == []  # RLS: B sees none of A's events


# --- roster -----------------------------------------------------------------


def test_load_roster_reads_user_token_map(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"alice": "tok-a", "bob": "tok-b"}))

    loaded = load_roster(str(path))

    assert loaded.user_ids == ["alice", "bob"]
    assert loaded.user_for_header("Bearer tok-b") == "bob"


def test_load_roster_rejects_empty(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text("{}")

    with pytest.raises(ValueError, match="no users configured"):
        load_roster(str(path))


def test_load_roster_rejects_wrong_shape(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"alice": {"token": "x"}}))

    with pytest.raises(ValueError, match="user_id: token"):
        load_roster(str(path))


def test_roster_resolves_nothing_for_missing_header():
    assert Roster({"a": "t"}).user_for_header(None) is None
    assert Roster({"a": "t"}).user_for_header("") is None
