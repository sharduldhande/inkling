"""POST /devices (§11): token registration, idempotent re-registration, RLS."""

import psycopg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from inkling.api.auth import Roster
from inkling.api.server import create_app
from inkling.db.client import Database
from inkling.db.queries import devices as devices_q

from tests.conftest import USER_A, USER_B

TOKEN_A = "token-for-a"
TOKEN_B = "token-for-b"
DEVICE = "c" * 64


@pytest_asyncio.fixture(autouse=True)
async def clean(migrated: str, dsn: str):
    with psycopg.connect(dsn) as conn:
        conn.execute("TRUNCATE devices RESTART IDENTITY CASCADE")
        conn.commit()
    yield


@pytest_asyncio.fixture
async def client(db: Database):
    app = create_app(db, Roster({USER_A: TOKEN_A, USER_B: TOKEN_B}))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _rows(db, user_id):
    async with db.with_tenant(user_id) as conn:
        cur = await conn.execute("SELECT * FROM devices ORDER BY id")
        return await cur.fetchall()


async def test_register_creates_a_row(client, db):
    r = await client.post(
        "/devices",
        json={"token": DEVICE.upper(), "environment": "development", "app_version": "1.2"},
        headers=_auth(TOKEN_A),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["device_id"] == 1
    assert body["environment"] == "development"
    [row] = await _rows(db, USER_A)
    assert row["token"] == DEVICE, "stored lowercase"
    assert row["app_version"] == "1.2"
    assert row["platform"] == "ios"
    assert row["revoked_at"] is None


async def test_re_register_is_a_touch_and_revives(client, db):
    first = (await client.post(
        "/devices", json={"token": DEVICE, "environment": "development"},
        headers=_auth(TOKEN_A),
    )).json()
    async with db.with_tenant(USER_A) as conn:
        await devices_q.revoke(conn, first["device_id"], reason="Unregistered")
        await conn.execute(
            "UPDATE devices SET last_seen_at = now() - interval '1 day'"
        )

    second = (await client.post(
        "/devices", json={"token": DEVICE, "environment": "production"},
        headers=_auth(TOKEN_A),
    )).json()
    assert second["device_id"] == first["device_id"]
    [row] = await _rows(db, USER_A)
    assert row["revoked_at"] is None
    assert row["environment"] == "production"
    assert row["last_seen_at"] > row["registered_at"]


async def test_another_users_token_is_409(client, db):
    assert (await client.post(
        "/devices", json={"token": DEVICE, "environment": "development"},
        headers=_auth(TOKEN_A),
    )).status_code == 200
    r = await client.post(
        "/devices", json={"token": DEVICE, "environment": "development"},
        headers=_auth(TOKEN_B),
    )
    assert r.status_code == 409
    assert await _rows(db, USER_B) == []
    assert len(await _rows(db, USER_A)) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"token": "zz" * 32, "environment": "development"},
        {"token": "a" * 10, "environment": "development"},
        {"token": DEVICE, "environment": "staging"},
        {"token": DEVICE},
        {"token": DEVICE, "environment": "development", "app_version": 3},
    ],
)
async def test_malformed_registration_is_422(client, db, body):
    r = await client.post("/devices", json=body, headers=_auth(TOKEN_A))
    assert r.status_code == 422, r.text
    assert "error" in r.json()
    assert await _rows(db, USER_A) == []


async def test_non_json_body_is_422(client):
    r = await client.post("/devices", content=b"nope", headers=_auth(TOKEN_A))
    assert r.status_code == 422


async def test_unauthenticated_is_401(client):
    r = await client.post(
        "/devices", json={"token": DEVICE, "environment": "development"}
    )
    assert r.status_code == 401
