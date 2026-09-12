"""APNs channel (§11): provider JWT, per-device fan-out, revocation.

The network is an httpx.MockTransport scripted per test; the devices table
is real (Neon test branch, runtime_role) because revocation is a DB write.
"""

import json

import httpx
import jwt
import psycopg
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from inkling.db.client import Database
from inkling.db.queries import devices as devices_q
from inkling.delivery.apns import (
    PRODUCTION_HOST,
    SANDBOX_HOST,
    TOKEN_TTL_SECONDS,
    ApnsClient,
)
from inkling.delivery.ntfy import DeliveryError, PermanentDeliveryError

from tests.conftest import USER_A

TOKEN_1 = "a" * 64
TOKEN_2 = "b" * 64


@pytest_asyncio.fixture(autouse=True)
async def clean(migrated: str, dsn: str):
    with psycopg.connect(dsn) as conn:
        conn.execute("TRUNCATE devices RESTART IDENTITY CASCADE")
        conn.commit()
    yield


@pytest.fixture
def keypair(tmp_path):
    private = ec.generate_private_key(ec.SECP256R1())
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path = tmp_path / "AuthKey_TESTKEY01.p8"
    path.write_bytes(pem)
    public = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return str(path), public


class Apple:
    """Scripted APNs: a list of (status, headers, body) per request, in order."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, headers, body = self.responses.pop(0)
        return httpx.Response(status, headers=headers, json=body)


def _client(db, keypair, apple, *, clock=None) -> ApnsClient:
    kwargs = {}
    if clock is not None:
        kwargs["clock"] = clock
    return ApnsClient(
        db,
        key_file=keypair[0],
        key_id="TESTKEY01",
        team_id="TEAM123456",
        topic="com.example.inkling",
        transport=httpx.MockTransport(apple.handler),
        **kwargs,
    )


async def _register(db, token, environment="development", user_id=USER_A):
    async with db.with_tenant(user_id) as conn:
        return await devices_q.upsert(
            conn, user_id=user_id, token=token, environment=environment,
            app_version="1.0",
        )


async def _devices(db, user_id=USER_A):
    async with db.with_tenant(user_id) as conn:
        cur = await conn.execute("SELECT * FROM devices ORDER BY id")
        return await cur.fetchall()


OK = (200, {"apns-id": "11111111-2222-3333-4444-555555555555"}, None)


async def test_send_posts_signed_alert_to_sandbox(db: Database, keypair):
    await _register(db, TOKEN_1)
    apple = Apple(OK)
    client = _client(db, keypair, apple)
    try:
        provider_id = await client.send(
            topic=USER_A, body="stretch", title="Inkling", user_id=USER_A
        )
    finally:
        await client.close()

    assert provider_id == "11111111-2222-3333-4444-555555555555"
    [request] = apple.requests
    assert str(request.url) == f"{SANDBOX_HOST}/3/device/{TOKEN_1}"
    assert request.headers["apns-topic"] == "com.example.inkling"
    assert request.headers["apns-push-type"] == "alert"
    assert request.headers["apns-priority"] == "10"
    assert json.loads(request.content) == {
        "aps": {"alert": {"title": "Inkling", "body": "stretch"}, "sound": "default"}
    }
    scheme, token = request.headers["authorization"].split(" ")
    assert scheme == "bearer"
    assert jwt.get_unverified_header(token)["kid"] == "TESTKEY01"
    claims = jwt.decode(token, keypair[1], algorithms=["ES256"])
    assert claims["iss"] == "TEAM123456"


async def test_production_device_goes_to_production_host(db: Database, keypair):
    await _register(db, TOKEN_1, environment="production")
    apple = Apple(OK)
    client = _client(db, keypair, apple)
    try:
        await client.send(topic=USER_A, body="x", user_id=USER_A)
    finally:
        await client.close()
    assert str(apple.requests[0].url).startswith(PRODUCTION_HOST)


async def test_provider_token_is_cached_then_rotated(db: Database, keypair):
    await _register(db, TOKEN_1)
    now = [1_000_000.0]
    apple = Apple(OK, OK, OK)
    client = _client(db, keypair, apple, clock=lambda: now[0])
    try:
        await client.send(topic=USER_A, body="1", user_id=USER_A)
        now[0] += 60
        await client.send(topic=USER_A, body="2", user_id=USER_A)
        now[0] += TOKEN_TTL_SECONDS
        await client.send(topic=USER_A, body="3", user_id=USER_A)
    finally:
        await client.close()
    tokens = [r.headers["authorization"] for r in apple.requests]
    assert tokens[0] == tokens[1], "within the TTL the JWT is reused"
    assert tokens[2] != tokens[1], "past the TTL a fresh JWT is minted"


async def test_expired_provider_token_is_refreshed_and_retried_once(
    db: Database, keypair
):
    await _register(db, TOKEN_1)
    apple = Apple((403, {}, {"reason": "ExpiredProviderToken"}), OK)
    client = _client(db, keypair, apple)
    try:
        assert await client.send(topic=USER_A, body="x", user_id=USER_A)
    finally:
        await client.close()
    assert len(apple.requests) == 2
    assert (
        apple.requests[0].headers["authorization"]
        != apple.requests[1].headers["authorization"]
    )


async def test_unregistered_device_is_revoked_and_permanent_when_alone(
    db: Database, keypair
):
    await _register(db, TOKEN_1)
    apple = Apple((410, {}, {"reason": "Unregistered", "timestamp": 1}))
    client = _client(db, keypair, apple)
    try:
        with pytest.raises(PermanentDeliveryError):
            await client.send(topic=USER_A, body="x", user_id=USER_A)
    finally:
        await client.close()
    [row] = await _devices(db)
    assert row["revoked_at"] is not None
    assert row["revoke_reason"] == "Unregistered"


async def test_bad_device_token_400_is_revoked_too(db: Database, keypair):
    await _register(db, TOKEN_1)
    apple = Apple((400, {}, {"reason": "BadDeviceToken"}))
    client = _client(db, keypair, apple)
    try:
        with pytest.raises(PermanentDeliveryError):
            await client.send(topic=USER_A, body="x", user_id=USER_A)
    finally:
        await client.close()
    assert (await _devices(db))[0]["revoke_reason"] == "BadDeviceToken"


async def test_throttled_is_transient(db: Database, keypair):
    await _register(db, TOKEN_1)
    apple = Apple((429, {}, {"reason": "TooManyRequests"}))
    client = _client(db, keypair, apple)
    try:
        with pytest.raises(DeliveryError) as info:
            await client.send(topic=USER_A, body="x", user_id=USER_A)
    finally:
        await client.close()
    assert not isinstance(info.value, PermanentDeliveryError)
    assert "429" in str(info.value)
    assert (await _devices(db))[0]["revoked_at"] is None


async def test_no_devices_is_transient(db: Database, keypair):
    apple = Apple()
    client = _client(db, keypair, apple)
    try:
        with pytest.raises(DeliveryError, match="no active devices"):
            await client.send(topic=USER_A, body="x", user_id=USER_A)
    finally:
        await client.close()
    assert apple.requests == []


async def test_one_of_two_devices_accepting_is_success(db: Database, keypair):
    await _register(db, TOKEN_1)
    await _register(db, TOKEN_2)
    apple = Apple(OK, (503, {}, {"reason": "ServiceUnavailable"}))
    client = _client(db, keypair, apple)
    try:
        provider_id = await client.send(topic=USER_A, body="x", user_id=USER_A)
    finally:
        await client.close()
    assert provider_id == "11111111-2222-3333-4444-555555555555"
    assert len(apple.requests) == 2


async def test_revoked_devices_are_skipped_and_re_registration_revives(
    db: Database, keypair
):
    await _register(db, TOKEN_1)
    async with db.with_tenant(USER_A) as conn:
        assert await devices_q.revoke(conn, 1, reason="test")
        assert not await devices_q.revoke(conn, 1, reason="twice"), "revoke is idempotent"
    apple = Apple()
    client = _client(db, keypair, apple)
    try:
        with pytest.raises(DeliveryError, match="no active devices"):
            await client.send(topic=USER_A, body="x", user_id=USER_A)
    finally:
        await client.close()

    row = await _register(db, TOKEN_1)  # the app relaunches and re-POSTs
    assert row["id"] == 1
    assert row["revoked_at"] is None
