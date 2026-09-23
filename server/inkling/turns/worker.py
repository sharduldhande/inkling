"""Per-user claim/lease worker loop (§5.2).

One asyncio task per user; one in-flight turn per user, ever. Within a process
the single task per user gives that; across processes the claim query's
NOT EXISTS guard gives it up to a narrow race (see db/queries/triggers.py).

No advisory locks and no LISTEN/NOTIFY anywhere (§3): the loop polls, because
PgBouncer transaction mode makes session-scoped primitives unreliable.
"""

import asyncio
import base64
import json
import logging
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from inkling.db.client import Database
from inkling.db.queries import triggers as triggers_q
from inkling.db.queries import turns as turns_q
from inkling.db.queries import users as users_q
from inkling.inference.gateway import InferenceGateway, emit_commands_tool
from inkling.lib import barcode
from inkling.jobs.consolidate import (
    assemble_consolidation_context,
    is_consolidation_trigger,
)
from inkling.turns.commands import (
    AppendEvent,
    CancelWakeup,
    CompleteCommitment,
    MarkCalendarDone,
    Notify,
    ScheduleWakeup,
    SnoozeCommitment,
    UpsertCommitment,
)
from inkling.turns.commands import parse_commands
from inkling.turns.commit import commit_turn
from inkling.turns.context import AssembledContext, assemble_context
from inkling.turns.ingestion import assemble_ingestion_context, is_ingestion_trigger
from inkling.turns.recall import make_recall_handler, recall_tool
from inkling.turns.stream import StreamEvent
from inkling.turns.validate import validate_commands

log = logging.getLogger(__name__)

# Commands whose application creates or moves a future fire_at (§10
# invalidation). CancelWakeup is included so the scheduler drops the dead
# entry at the next scan instead of firing into a CAS loss.
SCHEDULE_CHANGING = (UpsertCommitment, SnoozeCommitment, ScheduleWakeup, CancelWakeup)
# What the "next up" Live Activity shows: open commitments and their live
# occurrences. AppendEvent is here for habit_done, which retires a day's
# occurrence.
AGENDA_CHANGING = (
    UpsertCommitment, SnoozeCommitment, CompleteCommitment, AppendEvent,
    MarkCalendarDone,
)

LEASE_SECONDS = 90
HEARTBEAT_SECONDS = 30
MAX_ATTEMPTS_NON_CHAT = 2
IDLE_POLL_SECONDS = 1.0

# Subscriber for live SSE relay (§5.5). turn_events is the durable record; this
# is the best-effort live path, so a slow or dead socket must never stall a turn.
Subscriber = Callable[[StreamEvent], Awaitable[None]]


def new_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def make_barcode_handlers(images: tuple | list) -> dict[str, Any]:
    """Photo-turn tools (§14): scan the attached image, look a code up.

    Only wired when the turn actually carries images — on every other turn
    the tools simply don't exist, so the model can't call them. Both return
    model-facing text and never raise (a failed scan is an honest sentence).
    """
    if not images:
        return {}

    async def scan_barcode(args: dict[str, Any]) -> str:
        found: list[str] = []
        for image in images:
            try:
                data = base64.b64decode(image.data_base64)
            except Exception:
                continue
            found += [f"{fmt}: {text}" for fmt, text in barcode.decode_all(data)]
        if not found:
            return (
                "no barcode could be machine-decoded from the photo. If you "
                "can read the printed digits under the bars yourself, those "
                "ARE the product code — use them."
            )
        return "\n".join(found)

    async def lookup_product(args: dict[str, Any]) -> str:
        code = str(args.get("barcode") or "").strip()
        if not code:
            return "pass the barcode digits as {'barcode': '...'}"
        return await barcode.lookup(code)

    return {"scan_barcode": scan_barcode, "lookup_product": lookup_product}


def make_emit_validator(
    db: Database, user_id: str, trigger: dict[str, Any]
) -> Callable[[dict[str, Any]], Awaitable[str]]:
    """Mid-turn §9.2 validation for emit_commands (adapter-optional).

    Adapters that can talk back mid-turn (anthropic_sdk) call this from their
    emit_commands handler: the rejection reasons become the tool result, so the
    model corrects and re-emits *during* the turn instead of the user reading a
    "(couldn't complete …)" note after it. Same short-tenant-transaction shape
    as recall (§5.3 forbids a tx held ACROSS inference, not reads within it),
    same validate_commands gates commit_turn applies — which stays the
    authoritative backstop, since state can move between this check and commit.

    Returns JSON {"accepted": [command dicts], "rejections": [reasons]}: the
    adapter records only the accepted commands, so a rejected-then-corrected
    command is never double-validated into a user-facing note at commit.
    """
    consolidation = is_consolidation_trigger(trigger)
    ingestion = is_ingestion_trigger(trigger)

    async def validate(args: dict[str, Any]) -> str:
        notes: list[str] = []
        commands = parse_commands(args.get("commands") or [], notes=notes)
        async with db.with_tenant(user_id) as conn:
            user = await users_q.get(conn, user_id)
            assert user is not None  # a turn is running, so its user exists
            verdict = await validate_commands(
                conn,
                user=user,
                commands=commands,
                consolidation=consolidation,
                ingestion=ingestion,
            )
        return json.dumps(
            {
                "accepted": [
                    c.model_dump(mode="json") for c in verdict.accepted
                ],
                "rejections": notes + [r.note() for r in verdict.rejected],
            }
        )

    return validate


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
        on_schedule_change: Callable[[], None] | None = None,
        on_delivery_queued: Callable[[], None] | None = None,
        on_agenda_change: Callable[[], None] | None = None,
    ):
        self.db = db
        self.user_id = user_id
        self.gateway = gateway
        self.id = worker_id or new_worker_id()
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.idle_poll_seconds = idle_poll_seconds
        self.subscriber = subscriber
        self.on_schedule_change = on_schedule_change
        self.on_delivery_queued = on_delivery_queued
        self.on_agenda_change = on_agenda_change
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def nudge(self) -> None:
        """Wake the loop now instead of at the next poll.

        Ingress calls this in-process (role=all) so a chat turn starts on
        arrival rather than up to idle_poll_seconds later. It is an
        optimisation only — the poll is what makes the worker correct, and a
        worker in another process simply never gets nudged.
        """
        self._wake.set()

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            worked = False
            try:
                worked = await self.tick()
            except Exception:
                # A turn must never kill the loop: the next tick reaps and retries.
                log.exception("worker %s tick failed for user %s", self.id, self.user_id)
            if not worked:
                await self._idle()

    async def _idle(self) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=self.idle_poll_seconds)
        except TimeoutError:
            pass
        finally:
            self._wake.clear()

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
        trigger_id = int(trigger["id"])
        started = asyncio.get_running_loop().time()
        lease_lost = asyncio.Event()
        heart = asyncio.create_task(self._heartbeat(turn_id, lease_lost))
        seq = 0
        first_text_at: float | None = None
        try:
            assembled = await self._assemble_context(trigger)
            system, context = assembled.system, assembled.context

            async def on_text_delta(text: str) -> None:
                nonlocal seq, first_text_at
                if first_text_at is None:
                    first_text_at = asyncio.get_running_loop().time() - started
                seq += 1
                await self._emit(
                    trigger_id, turn_id, seq, "text_delta", {"text": text}
                )

            # The turn races lease loss: if the reaper takes the lease (another
            # worker now owns this turn), the in-flight model call is cancelled
            # rather than allowed to keep writing events (§5.2). Adapter-
            # agnostic — cancellation interrupts at the next await.
            turn_task = asyncio.create_task(
                self.gateway.run_turn(
                    user_id=self.user_id,
                    system=system,
                    context=context,
                    tools=[emit_commands_tool(), recall_tool()],
                    on_text_delta=on_text_delta,
                    # Fresh handler per turn: the recall budget is per-turn.
                    tool_handlers={
                        "recall": make_recall_handler(self.db, self.user_id),
                        "validate_commands": make_emit_validator(
                            self.db, self.user_id, trigger
                        ),
                        # Photo turns only: the model decides whether the
                        # image warrants a scan/lookup (§14 photo tools).
                        **make_barcode_handlers(assembled.images),
                    },
                    source=trigger["source"],
                    images=list(assembled.images),
                )
            )
            lease_waiter = asyncio.create_task(lease_lost.wait())
            done, _ = await asyncio.wait(
                {turn_task, lease_waiter}, return_when=asyncio.FIRST_COMPLETED
            )
            lease_waiter.cancel()
            if turn_task not in done:
                turn_task.cancel()
                try:
                    await turn_task
                except asyncio.CancelledError:
                    pass
                raise LeaseLost(f"lease on turn {turn_id} expired mid-stream")
            result = turn_task.result()  # raises if the gateway failed

            committed = await commit_turn(
                self.db,
                user_id=self.user_id,
                trigger=trigger,
                turn_id=turn_id,
                result=result,
                next_seq=seq + 1,
                actor=self.id,
            )
            # Republish what commit_turn persisted so live sockets see the end
            # of the stream without waiting to poll turn_events. It returns the
            # events rather than us assuming the shape: a turn with rejected
            # commands (§9.2) prepends a correction delta before done/exit.
            for event in committed:
                await self._publish(
                    trigger_id, turn_id, event.seq, event.kind, event.data
                )

            # §10 invalidation: a command that created or moved a future
            # fire_at pokes the in-process scheduler so it re-scans now
            # rather than at the next hourly refresh. In-process only — a
            # scheduler in another process relies on its reconcile scan.
            if self.on_schedule_change is not None and any(
                isinstance(c, SCHEDULE_CHANGING) for c in result.commands
            ):
                self.on_schedule_change()

            # §11: a Notify just became a deliveries row; wake the drainer so
            # the push leaves now, not at the next reconcile scan.
            if self.on_delivery_queued is not None and any(
                isinstance(c, Notify) for c in result.commands
            ):
                self.on_delivery_queued()

            # §11: the Live Activity card mirrors the agenda; refresh it now.
            if self.on_agenda_change is not None and any(
                isinstance(c, AGENDA_CHANGING) for c in result.commands
            ):
                self.on_agenda_change()

            # §15 per-turn summary line (context.py logs the token counts,
            # commit.py the accept/reject split; this is the roll-up).
            log.info(
                "turn %s done: user=%s trigger=%s source=%s model=%s "
                "commands=%d duration=%.1fs first_text=%s",
                turn_id, self.user_id, trigger_id, trigger["source"],
                result.model, len(result.commands),
                asyncio.get_running_loop().time() - started,
                f"{first_text_at:.1f}s" if first_text_at is not None else "-",
            )
        except Exception as exc:
            log.exception("turn %s failed for user %s", turn_id, self.user_id)
            await self._fail_turn(trigger, turn_id, seq + 1, exc)
        finally:
            heart.cancel()

    async def _assemble_context(self, trigger: dict[str, Any]) -> AssembledContext:
        """One snapshot transaction, closed before inference (§5.3).

        Consolidation triggers (§13.1) get their own prompt and day-scoped
        context; everything else gets the §8 assembly.
        """
        if is_consolidation_trigger(trigger):
            return await assemble_consolidation_context(
                self.db, user_id=self.user_id, trigger=trigger
            )
        if is_ingestion_trigger(trigger):
            return await assemble_ingestion_context(
                self.db, user_id=self.user_id, trigger=trigger
            )
        return await assemble_context(
            self.db, user_id=self.user_id, trigger=trigger
        )

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
        self, trigger_id: int, turn_id: int, seq: int, kind: str, data: dict[str, Any]
    ) -> None:
        """Persist a stream event, then relay it to any live socket (§5.5).

        Persist-then-relay, so a client that reconnects and replays from
        turn_events can never be missing text it already saw live.

        One transaction per delta: correct, and fine for sentence-sized deltas,
        but it makes stream cost linear in delta count. Adapters that stream
        token-level (anthropic_sdk) batch deltas by time window before calling
        this, so the row rate stays bounded regardless of token rate.
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
        await self._publish(trigger_id, turn_id, seq, kind, data)

    async def _publish(
        self, trigger_id: int, turn_id: int, seq: int, kind: str, data: dict[str, Any]
    ) -> None:
        if self.subscriber is None:
            return
        try:
            await self.subscriber(
                StreamEvent(
                    user_id=self.user_id,
                    trigger_id=trigger_id,
                    turn_id=turn_id,
                    seq=seq,
                    kind=kind,
                    data=data,
                )
            )
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
        # Published after the transaction commits: a live socket must never be
        # told the turn failed while the rollback is still possible.
        await self._publish(
            int(trigger["id"]), turn_id, seq, "error", {"error": error}
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
