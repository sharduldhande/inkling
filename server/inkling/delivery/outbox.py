"""Outbox drainer (§11): the only path from a deliveries row to a real push.

Shape mirrors the §10 scheduler: per-tenant scans over the roster (RLS means
runtime_role cannot enumerate users), every state change a compare-and-set, and
an in-process wake() so a turn that just enqueued a Notify sends within a
second instead of a poll interval.

One attempt is three steps: claim (CAS to 'leased', one tenant tx), POST
(strictly outside any transaction), record (sent / retry_wait / dead_letter,
a second tx). A crash between POST and record leaves a 'leased' orphan that
boot recovery requeues — a possible duplicate push, which is exactly the
at-least-once contract; a lost reminder is the failure mode we refuse.

Backoff is 1m/5m/15m/1h, then dead_letter plus an operator alert — itself a
deliveries row for the same user, destination OPERATOR_NTFY (§11 allows that
topic to be user-agnostic; the row still lives under the user's RLS). Alerts
are suppressed for rows already destined to the operator topic, or the alert
for a dead alert would loop forever.

Expiry (`expires_at` passed) dead-letters without an attempt or an alert; §11's
"fold into next day-start context" waits on §13's day-start machinery.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from inkling.db.client import Database
from inkling.db.queries import deliveries as deliveries_q
from inkling.delivery.ntfy import Channel

log = logging.getLogger(__name__)

BACKOFF = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(hours=1),
)
RECONCILE_SECONDS = 60.0  # cap on sleep: also the catch-up cadence
MIN_SLEEP = 0.05


class Drainer:
    def __init__(
        self,
        db: Database,
        user_ids: list[str],
        *,
        channel: Channel,
        operator_topic: str = "",
        reconcile_seconds: float = RECONCILE_SECONDS,
    ):
        self.db = db
        self.user_ids = user_ids
        self.channel = channel
        self.operator_topic = operator_topic
        self.reconcile_seconds = reconcile_seconds
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()

    def wake(self) -> None:
        """In-process nudge: a turn just enqueued a delivery; drain now."""
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    async def run_forever(self) -> None:
        await self.recover_orphans()
        while not self._stop.is_set():
            try:
                await self.drain()
                delay = await self._seconds_until_next_due()
            except Exception:
                log.exception("drain pass failed; retrying after backoff")
                delay = 2.0
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except TimeoutError:
                pass
            finally:
                self._wake.clear()

    async def recover_orphans(self) -> int:
        """Requeue rows a crash left 'leased' (boot only; see module docstring)."""
        requeued = 0
        for user_id in self.user_ids:
            async with self.db.with_tenant(user_id) as conn:
                requeued += await deliveries_q.reset_leased(conn)
        if requeued:
            log.warning("requeued %d orphaned leased deliver(ies) at boot", requeued)
        return requeued

    async def drain(self, *, now: datetime | None = None) -> int:
        """Attempt every due delivery once. Returns how many sent."""
        now = now or datetime.now(timezone.utc)
        sent = 0
        for user_id in self.user_ids:
            try:
                async with self.db.with_tenant(user_id) as conn:
                    rows = await deliveries_q.due(conn, now=now)
            except Exception:
                log.exception("due-scan failed for user %s", user_id)
                continue
            for row in rows:
                try:
                    sent += await self._attempt(user_id, row, now)
                except Exception:
                    # _attempt handles send failures itself; this catches
                    # infrastructure errors so one row can't stall the rest.
                    log.exception("delivery %s attempt crashed", row["id"])
        return sent

    async def _seconds_until_next_due(self) -> float:
        soonest: datetime | None = None
        for user_id in self.user_ids:
            async with self.db.with_tenant(user_id) as conn:
                nxt = await deliveries_q.next_due(conn)
            if nxt is not None and (soonest is None or nxt < soonest):
                soonest = nxt
        if soonest is None:
            return self.reconcile_seconds
        delta = (soonest - datetime.now(timezone.utc)).total_seconds()
        return min(max(delta, MIN_SLEEP), self.reconcile_seconds)

    async def _attempt(
        self, user_id: str, row: dict[str, Any], now: datetime
    ) -> int:
        delivery_id = int(row["id"])

        if row["expires_at"] is not None and row["expires_at"] <= now:
            async with self.db.with_tenant(user_id) as conn:
                await deliveries_q.mark_dead(
                    conn,
                    delivery_id,
                    attempt_count=int(row["attempt_count"]),
                    error="expired",
                )
            log.info("delivery %s expired before sending", delivery_id)
            return 0

        async with self.db.with_tenant(user_id) as conn:
            claimed = await deliveries_q.claim(conn, delivery_id)
        if claimed is None:
            return 0  # another path already owns or finished it

        try:
            provider_id = await self.channel.send(
                topic=claimed["destination"], body=claimed["body"]
            )
        except Exception as exc:
            await self._record_failure(user_id, claimed, now, exc)
            return 0

        async with self.db.with_tenant(user_id) as conn:
            await deliveries_q.mark_sent(
                conn, delivery_id, provider_msg_id=provider_id
            )
        log.info("delivery %s sent to %s", delivery_id, claimed["destination"])
        return 1

    async def _record_failure(
        self, user_id: str, row: dict[str, Any], now: datetime, exc: Exception
    ) -> None:
        delivery_id = int(row["id"])
        attempts = int(row["attempt_count"]) + 1
        error = f"{type(exc).__name__}: {exc}"[:500]

        async with self.db.with_tenant(user_id) as conn:
            if attempts <= len(BACKOFF):
                await deliveries_q.mark_retry(
                    conn,
                    delivery_id,
                    attempt_count=attempts,
                    error=error,
                    not_before=now + BACKOFF[attempts - 1],
                )
                log.warning(
                    "delivery %s failed (attempt %d/%d): %s",
                    delivery_id, attempts, len(BACKOFF) + 1, error,
                )
                return

            await deliveries_q.mark_dead(
                conn, delivery_id, attempt_count=attempts, error=error
            )
            if self.operator_topic and row["destination"] != self.operator_topic:
                await deliveries_q.enqueue(
                    conn,
                    user_id=user_id,
                    destination=self.operator_topic,
                    body=(
                        f"delivery {delivery_id} to '{row['destination']}' "
                        f"dead-lettered after {attempts} attempts: {error}"
                    ),
                    idempotency_key=f"operator:dead:{delivery_id}",
                    # Due in the drain's own timeline (`now` is injectable),
                    # not created_at's wall clock.
                    not_before=now,
                )
        log.error(
            "delivery %s dead-lettered after %d attempts: %s",
            delivery_id, attempts, error,
        )
