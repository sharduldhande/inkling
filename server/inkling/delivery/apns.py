"""APNs channel adapter (§11): user pushes go straight to Apple.

Token-based provider auth: one ES256 JWT signed with the team's .p8 key,
cached and refreshed inside Apple's 20-60 minute window. Each push is one
HTTP/2 POST per registered device; the drainer calls send() outside any
database transaction, treats any DeliveryError as a failed attempt, and owns
retry/backoff — this module performs the POSTs and reports honestly.

Addressing: the deliveries row carries destination = user_id, and send()
fans out to every device row that is live at send time. A device registered
after the Notify was enqueued (say, during a quiet-hours deferral) still gets
the push, and a user with no devices fails at send time, where the outbox
alerts the operator, instead of enqueueing nothing.

Partial-failure semantics, deliberately simple: success if at least one
device accepted. Devices that 429/5xx'd in the same pass are NOT retried,
because a retry would repeat the push to the device that accepted. If no
device accepted and any error was transient → DeliveryError (backoff); if
every device was rejected as unregistered → PermanentDeliveryError
(dead-letter now, no point in an 80-minute backoff ladder).
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

import httpx
import jwt

from inkling.db.client import Database
from inkling.db.queries import devices as devices_q
from inkling.delivery.ntfy import (
    DEFAULT_TITLE,
    DeliveryError,
    PermanentDeliveryError,
)

log = logging.getLogger(__name__)

SANDBOX_HOST = "https://api.sandbox.push.apple.com"
PRODUCTION_HOST = "https://api.push.apple.com"
HOSTS = {"development": SANDBOX_HOST, "production": PRODUCTION_HOST}
TIMEOUT_SECONDS = 10.0
TOKEN_TTL_SECONDS = 50 * 60  # Apple: refresh no sooner than 20, no later than 60

# Apple's `reason` strings that mean "this token will never work again".
UNREGISTERED_REASONS = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}
PROVIDER_TOKEN_REASONS = {"ExpiredProviderToken", "InvalidProviderToken"}


class ApnsClient:
    def __init__(
        self,
        db: Database,
        *,
        key_file: str,
        key_id: str,
        team_id: str,
        topic: str,
        timeout: float = TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.db = db
        self.key_id = key_id
        self.team_id = team_id
        self.topic = topic
        self._key = Path(key_file).read_text()
        self._clock = clock
        self._token = ""
        self._token_issued = 0.0
        # http2=True negotiates h2 on the real hosts; a MockTransport in tests
        # ignores it. Both hosts share one pool: URLs are absolute.
        self._client = httpx.AsyncClient(
            http2=True, timeout=timeout, transport=transport
        )

    async def close(self) -> None:
        await self._client.aclose()

    # --- provider token ------------------------------------------------------

    def _provider_token(self, *, force: bool = False) -> str:
        now = self._clock()
        if force or not self._token or now - self._token_issued >= TOKEN_TTL_SECONDS:
            self._token = jwt.encode(
                {"iss": self.team_id, "iat": int(now)},
                self._key,
                algorithm="ES256",
                headers={"kid": self.key_id},
            )
            self._token_issued = now
        return self._token

    # --- one POST ------------------------------------------------------------

    async def _post(
        self, device: dict[str, Any], *, title: str, body: str
    ) -> httpx.Response:
        host = HOSTS[device["environment"]]
        payload = {"aps": {"alert": {"title": title, "body": body}, "sound": "default"}}
        return await self._client.post(
            f"{host}/3/device/{device['token']}",
            content=json.dumps(payload).encode(),
            headers={
                "authorization": f"bearer {self._provider_token()}",
                "apns-topic": self.topic,
                "apns-push-type": "alert",
                "apns-priority": "10",
                "apns-expiration": "0",
                "content-type": "application/json",
            },
        )

    @staticmethod
    def _reason(response: httpx.Response) -> str:
        try:
            return str(response.json().get("reason", ""))
        except ValueError:
            return ""

    # --- channel protocol ----------------------------------------------------

    async def send(
        self, *, topic: str, body: str, title: str = DEFAULT_TITLE, user_id: str = ""
    ) -> str:
        """Fan out to the user's live devices; returns the accepted apns-ids.

        `topic` (the row's destination) is the user id by construction and is
        ignored; `user_id` is the address.
        """
        async with self.db.with_tenant(user_id) as conn:
            devices = await devices_q.active(conn)
        if not devices:
            raise DeliveryError(f"no active devices for user {user_id!r}")

        accepted: list[str] = []
        transient: list[str] = []
        revoked: list[tuple[int, str]] = []
        for device in devices:
            outcome, detail = await self._send_one(device, title=title, body=body)
            if outcome == "accepted":
                accepted.append(detail)
            elif outcome == "revoked":
                revoked.append((int(device["id"]), detail))
            else:
                transient.append(detail)

        if revoked:
            async with self.db.with_tenant(user_id) as conn:
                for device_id, reason in revoked:
                    await devices_q.revoke(conn, device_id, reason=reason)
                    log.warning("device %s revoked: %s", device_id, reason)

        if accepted:
            if transient:
                log.warning(
                    "apns partial delivery for %s: %d accepted, %d failed (%s)",
                    user_id, len(accepted), len(transient), "; ".join(transient),
                )
            return ",".join(accepted)
        if transient:
            raise DeliveryError("apns: " + "; ".join(transient))
        raise PermanentDeliveryError(
            "apns: every device unregistered (" + "; ".join(r for _, r in revoked) + ")"
        )

    async def _send_one(
        self, device: dict[str, Any], *, title: str, body: str
    ) -> tuple[str, str]:
        """One device: ('accepted', apns-id) | ('revoked', reason) | ('transient', error)."""
        try:
            response = await self._post(device, title=title, body=body)
            if response.status_code == 403 and self._reason(response) in PROVIDER_TOKEN_REASONS:
                # Clock drift or a stale cache: mint a fresh JWT and try once.
                self._provider_token(force=True)
                response = await self._post(device, title=title, body=body)
        except httpx.HTTPError as exc:
            return "transient", f"POST failed: {exc}"

        status = response.status_code
        if status == 200:
            return "accepted", response.headers.get("apns-id", "")
        reason = self._reason(response)
        if status == 410 or (status == 400 and reason in UNREGISTERED_REASONS):
            return "revoked", reason or "Unregistered"
        return "transient", f"apns returned {status} {reason or response.text[:200]}"
