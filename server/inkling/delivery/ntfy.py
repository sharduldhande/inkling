"""ntfy channel adapter (§11) — operator alerts only since the APNs cutover.

User-facing pushes route to the 'apns' channel (delivery/apns.py); ntfy now
carries dead-letter, backup and restore-drill alerts to OPERATOR_NTFY, where
it must keep working precisely when the APNs path is broken.

One of the two places in the delivery path that talk to the network. The drainer
calls send() strictly outside any database transaction (§5.4), treats any
exception as a failed attempt, and owns retry/backoff — this module just
performs one POST and reports honestly.

ntfy has no server-side idempotency; the outbox's idempotency_key prevents
double-*enqueue*, and at-least-once is the delivery contract (§11) — a crash
between POST and status write may repeat a reminder, which is acceptable.
"""

import logging
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)

DEFAULT_TITLE = "Inkling"
CLICK_URL = "inkling://"  # §11: tapping the push opens the app
TIMEOUT_SECONDS = 10.0


class DeliveryError(Exception):
    """One attempt failed; the drainer decides whether to retry."""


class PermanentDeliveryError(DeliveryError):
    """No retry will help (every device token revoked); dead-letter now."""


class Channel(Protocol):
    async def send(
        self, *, topic: str, body: str, title: str = DEFAULT_TITLE, user_id: str = ""
    ) -> str:
        """Deliver one message; returns the provider's message id.

        `user_id` exists for channels whose auth is per-user (calendar);
        ntfy ignores it — the topic is the whole address.
        """
        ...


class NtfyClient:
    def __init__(self, base_url: str, *, timeout: float = TIMEOUT_SECONDS):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def send(
        self, *, topic: str, body: str, title: str = DEFAULT_TITLE, user_id: str = ""
    ) -> str:
        try:
            response = await self._client.post(
                f"{self.base_url}/{topic}",
                content=body.encode(),
                headers={"Title": title, "Click": CLICK_URL},
            )
        except httpx.HTTPError as exc:
            raise DeliveryError(f"ntfy POST failed: {exc}") from exc
        if response.status_code // 100 != 2:
            raise DeliveryError(
                f"ntfy returned {response.status_code}: {response.text[:200]}"
            )
        try:
            provider_id = response.json().get("id", "")
        except ValueError:
            provider_id = ""  # a 2xx with a weird body is still sent
        return provider_id


class StubChannel:
    """Deterministic channel for tests: records sends, scriptable failures."""

    def __init__(self, *, fail_times: int = 0):
        self.sent: list[dict[str, Any]] = []
        self.fail_times = fail_times

    async def send(
        self, *, topic: str, body: str, title: str = DEFAULT_TITLE, user_id: str = ""
    ) -> str:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise DeliveryError("stub: scripted failure")
        self.sent.append({"topic": topic, "body": body, "title": title})
        return f"stub-{len(self.sent)}"
