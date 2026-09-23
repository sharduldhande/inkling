"""POST /mcp/<secret>: the remote MCP server other chats use to add reminders."""

from datetime import datetime, timedelta, timezone

import psycopg
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from inkling.api.auth import Roster
from inkling.api.server import create_app
from inkling.db.client import Database

from tests.conftest import USER_A

SECRET = "s" * 40
URL = f"/mcp/{SECRET}"


@pytest_asyncio.fixture(autouse=True)
async def clean(migrated: str, dsn: str):
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "TRUNCATE transcript, events, commitment_occurrences, commitments "
            "RESTART IDENTITY CASCADE"
        )
        conn.commit()
    yield


@pytest_asyncio.fixture
async def client(db: Database):
    app = create_app(db, Roster({USER_A: "tok"}), mcp_tokens={USER_A: SECRET})
    woken = []
    app.state.schedule_wake = lambda: woken.append("scheduler")
    app.state.live_activity_wake = lambda: woken.append("card")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        c.woken = woken
        yield c


def _rpc(method, params=None, msg_id=1):
    msg = {"jsonrpc": "2.0", "method": method, "id": msg_id}
    if params is not None:
        msg["params"] = params
    return msg


async def _call(client, **arguments):
    r = await client.post(URL, json=_rpc(
        "tools/call", {"name": "add_reminder", "arguments": arguments}
    ))
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    return result["content"][0]["text"], result["isError"]


async def _rows(db, sql):
    async with db.with_tenant(USER_A) as conn:
        cur = await conn.execute(sql)
        return await cur.fetchall()


async def test_wrong_secret_is_a_bare_404(client):
    r = await client.post("/mcp/" + "x" * 40, json=_rpc("initialize"))
    assert r.status_code == 404 and r.content == b""
    assert (await client.get("/mcp/" + "x" * 40)).status_code == 404


async def test_handshake_and_tool_list(client):
    r = await client.post(URL, json=_rpc("initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    }))
    body = r.json()["result"]
    assert body["protocolVersion"] == "2025-06-18"
    assert body["capabilities"] == {"tools": {"listChanged": False}}

    r = await client.post(URL, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202

    tools = (await client.post(URL, json=_rpc("tools/list"))).json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["add_reminder"]
    assert (await client.get(URL)).status_code == 405

    r = await client.post(URL, json=_rpc("resources/list"))
    assert r.json()["error"]["code"] == -32601


async def test_add_reminder_creates_commitment_occurrence_and_chat_line(client, db):
    text, is_error = await _call(
        client, title="Call the dentist", due="2027-03-04T15:00",
        notes="re: crown", source="ChatGPT",
    )
    assert not is_error, text
    assert text.startswith("Added to Inkling: Call the dentist (due Thu Mar 4, 3:00 PM")

    [c] = await _rows(db, "SELECT * FROM commitments")
    assert (c["title"], c["body"], c["source"], c["source_ref"]) == (
        "Call the dentist", "re: crown", "mcp", "ChatGPT",
    )
    # Naive = the user's wall clock (testa is America/New_York in conftest).
    assert c["due_at"] == datetime(2027, 3, 4, 20, 0, tzinfo=timezone.utc)
    assert len(await _rows(db, "SELECT id FROM commitment_occurrences")) == 1
    [line] = await _rows(db, "SELECT role, text FROM transcript")
    assert line["role"] == "user"
    assert line["text"].startswith("📌 Added from ChatGPT: Call the dentist")
    assert client.woken == ["scheduler", "card"]

    # A retried call is the same reminder.
    text, is_error = await _call(
        client, title="call the dentist", due="2027-03-04T15:00", source="ChatGPT",
    )
    assert text.startswith("Already on the Inkling list")
    assert len(await _rows(db, "SELECT id FROM commitments")) == 1


async def test_add_reminder_undated_and_bad_input(client, db):
    text, is_error = await _call(client, title="Renew passport")
    assert not is_error and "no due date" in text
    [c] = await _rows(db, "SELECT source_ref, due_at FROM commitments")
    assert c["source_ref"] == "another chat" and c["due_at"] is None

    past = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    for args, needle in (
        ({"title": " "}, "title is required"),
        ({"title": "x", "due": "next tuesday"}, "ISO 8601"),
        ({"title": "x", "due": past}, "in the past"),
        ({"title": "x" * 201}, "over 200"),
    ):
        text, is_error = await _call(client, **args)
        assert is_error and needle in text, (args, text)
