"""Per-user claim/lease worker loop (§5.2).

One asyncio task per user; one in-flight turn per user, ever. Within a process
the single task per user gives that; across processes the claim query's
NOT EXISTS guard gives it up to a narrow race (see db/queries/triggers.py).

No advisory locks and no LISTEN/NOTIFY anywhere (§3): the loop polls, because
PgBouncer transaction mode makes session-scoped primitives unreliable.
"""

import asyncio
import logging
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from inkling.db.client import Database
from inkling.db.queries import triggers as triggers_q
from inkling.db.queries import turns as turns_q
from inkling.inference.gateway import Completed, InferenceGateway, TextDelta
from inkling.turns.commit import commit_turn

log = logging.getLogger(__name__)

LEASE_SECONDS = 90
HEARTBEAT_SECONDS = 30
MAX_ATTEMPTS_NON_CHAT = 2
IDLE_POLL_SECONDS = 1.0

# Subscriber for live SSE relay (§5.5). turn_events is the durable record; this
# is the best-effort live path, so a slow or dead socket must never stall a turn.
Subscriber = Callable[[int, int, str, dict[str, Any]], Awaitable[None]]


def new_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class LeaseLost(Exception):
    """The lease was reaped out from under us; another worker owns this turn."""


class Worker:
    def __init__(
        self,
        db: Database,
        user_id: str,
        gateway: InferenceGateway,
        *,
        worker_id: str | None = None,
        lease_seconds: int = LEASE_SECONDS,
        heartbeat_seconds: int = HEARTBEAT_SECONDS,
        idle_poll_seconds: float = IDLE_POLL_SECONDS,
        subscriber: Subscriber | None = None,
    ):
        self.db = db
        self.user_id = user_id
        self.gateway = gateway
        self.id = worker_id or new_worker_id()
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.idle_poll_seconds = idle_poll_seconds
        self.subscriber = subscriber
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            worked = False
            try:
                worked = await self.tick()
            except Exception:
                # A turn must never kill the loop: the next tick reaps and retries.
                log.exception("worker %s tick failed for user %s", self.id, self.user_id)
            if not worked:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.idle_poll_seconds
                    )
                except TimeoutError:
                    pass

    async def tick(self) -> bool:
        """Reap, then run at most one turn. True if a turn ran."""
        await self.reap()
        claimed = await self.claim()
        if claimed is None:
            return False
        trigger, turn = claimed
        await self.run_turn(trigger, int(turn["id"]))
        return True

    async def reap(self) -> int:
        """Fail turns whose lease lapsed and settle their triggers (§5.2).

        Scoped by RLS to this worker's user, which is also the only scope that
        matters: the slot it frees is this user's.
        """
        reaped = 0
        async with self.db.with_tenant(self.user_id) as conn:
            for turn in await turns_q.expired(conn):
                turn_id = int(turn["id"])
                await turns_q.finish(
                    conn, turn_id, status="error", error="lease expired"
                )
                await self._settle_failed(
                    conn,
                    trigger_id=int(turn["trigger_id"]),
                    source=turn["trigger_source"],
                )
                log.warning(
                    "reaped turn %s (user %s, lease held by %s)",
                    turn_id,
                    self.user_id,
                    turn["leased_by"],
                )
                reaped += 1
        return reaped

    async def claim(self) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Claim the next trigger and open its turn in one transaction.

        None means the inbox is empty or this user already has a turn in
        flight. Claim and turn creation share a transaction, so a failure
        anywhere leaves the trigger pending rather than consumed.
        """
        async with self.db.with_tenant(self.user_id) as conn:
            trigger = await triggers_q.claim_next(conn, self.user_id)
            if trigger is None:
                return None
            turn = await turns_q.create(
                conn,
                user_id=self.user_id,
                trigger_id=int(trigger["id"]),
                leased_by=self.id,
                lease_seconds=self.lease_seconds,
            )
            await triggers_q.set_status(
                conn, int(trigger["id"]), "running", turn_id=int(turn["id"])
            )
            return trigger, turn

    async def run_turn(self, trigger: dict[str, Any], turn_id: int) -> None:
        lease_lost = asyncio.Event()
        heart = asyncio.create_task(self._heartbeat(turn_id, lease_lost))
        seq = 0
        try:
            context = await self._assemble_context(trigger)
            prompt = (trigger["payload"] or {}).get("text", "")
            completed: Completed | None = None

            async for chunk in self.gateway.stream(
                user_id=self.user_id, prompt=prompt, context=context
            ):
                if lease_lost.is_set():
                    raise LeaseLost(f"lease on turn {turn_id} expired mid-stream")
                if isinstance(chunk, TextDelta):
                    seq += 1
                    await self._emit(turn_id, seq, "text_delta", {"text": chunk.text})
                else:
                    completed = chunk

            if completed is None:
                raise RuntimeError("gateway stream ended without a Completed chunk")

            await commit_turn(
                self.db,
                user_id=self.user_id,
                trigger=trigger,
                turn_id=turn_id,
                result=completed,
                next_seq=seq + 1,
                actor=self.id,
            )
        except Exception as exc:
            log.exception("turn %s failed for user %s", turn_id, self.user_id)
            await self._fail_turn(trigger, turn_id, seq + 1, exc)
        finally:
            heart.cancel()

    async def _assemble_context(self, trigger: dict[str, Any]) -> dict[str, Any]:
        """Placeholder for §8.

        Context assembly reads a consistent snapshot and closes the transaction
        before inference (§5.3). Until §8 lands the worker passes the trigger
        through, which is enough to exercise the pipeline but is not a context.
        """
        return {"trigger": {k: trigger[k] for k in ("id", "source", "source_ref")}}

    async def _heartbeat(self, turn_id: int, lease_lost: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            async with self.db.with_tenant(self.user_id) as conn:
                held = await turns_q.heartbeat(
                    conn, turn_id, self.id, self.lease_seconds
                )
            if not held:
                log.warning("worker %s lost lease on turn %s", self.id, turn_id)
                lease_lost.set()
                return

    async def _emit(
        self, turn_id: int, seq: int, kind: str, data: dict[str, Any]
    ) -> None:
        """Persist a stream event, then relay it to any live socket (§5.5).

        Persist-then-relay, so a client that reconnects and replays from
        turn_events can never be missing text it already saw live.

        One transaction per delta: correct, and fine for sentence-sized deltas,
        but it makes stream cost linear in delta count. If a real adapter emits
        token-sized deltas this wants batching by time window.
        """
        async with self.db.with_tenant(self.user_id) as conn:
            await turns_q.append_event(
                conn,
                user_id=self.user_id,
                turn_id=turn_id,
                seq=seq,
                kind=kind,
                data=data,
            )
        if self.subscriber is not None:
            try:
                await self.subscriber(turn_id, seq, kind, data)
            except Exception:
                log.exception("subscriber raised on turn %s seq %s", turn_id, seq)

    async def _fail_turn(
        self, trigger: dict[str, Any], turn_id: int, seq: int, exc: Exception
    ) -> None:
        error = f"{type(exc).__name__}: {exc}"[:2000]
        async with self.db.with_tenant(self.user_id) as conn:
            await turns_q.finish(conn, turn_id, status="error", error=error)
            await turns_q.append_event(
                conn,
                user_id=self.user_id,
                turn_id=turn_id,
                seq=seq,
                kind="error",
                data={"error": error},
            )
            await self._settle_failed(
                conn, trigger_id=int(trigger["id"]), source=trigger["source"]
            )

    async def _settle_failed(
        self, conn: Any, *, trigger_id: int, source: str
    ) -> None:
        """Requeue or give up, per §5.2.

        Non-chat triggers get up to MAX_ATTEMPTS_NON_CHAT attempts; the attempt
        count is COUNT(turns) for the trigger, since every claim creates exactly
        one turn. Chat triggers never retry — the user is waiting on a stream,
        so the error goes to them instead of silently running again.
        """
        if source == "chat":
            await triggers_q.set_status(conn, trigger_id, "error")
            return
        tries = await turns_q.attempts(conn, trigger_id)
        if tries < MAX_ATTEMPTS_NON_CHAT:
            await triggers_q.requeue(conn, trigger_id)
        else:
            await triggers_q.set_status(conn, trigger_id, "error")
