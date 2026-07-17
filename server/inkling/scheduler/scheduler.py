"""In-process timer scheduler (§10): DB-quiet, heap-driven, crash-safe.

State is an in-memory min-heap of (fire_at, kind, user_id, id) rebuilt from the
database on every horizon refresh: pending occurrences and active wakeups
within the next 48 h — with no lower bound, so past-due rows load on boot and
fire immediately, flagged `late` (catch-up; nothing is dropped). Occurrence
materialization piggybacks on the refresh.

Every fire is a database compare-and-set (pending→eligible, active→fired,
fire_at advance) plus a trigger insert with a natural idempotency key, in one
tenant transaction. That makes the heap disposable: rebuilding it can re-heap
an entry that already fired, and the CAS simply loses. Two schedulers racing
resolve the same way.

Invalidation is the in-process asyncio event (`wake()`): a worker that applied
a schedule-changing command pokes it and the loop refreshes early. When the
scheduler runs as its own process there is no event to poke, so the standalone
role runs with a 60 s refresh — the reconcile-scan the spec calls for (§10;
LISTEN/NOTIFY is banned by §3).

The roster comes from the caller (CHAT_TOKENS_FILE, like the supervisor): RLS
means runtime_role cannot enumerate users, so all scans are per-tenant loops.

Deferred to later milestones, deliberately: delivery `not_before` wakes and
channel renewals (§11/§12 own those tables' drains), job times (§13 — firing a
consolidation would need its prompt to exist), and the §10 occurrence *lease*:
in-process, the eligible→trigger insert is atomic and the trigger pipeline has
its own 90 s lease + reaper, so a second lease here would guard nothing yet.
The "silent_summary" quiet policy currently defers like "defer"; folding into
the day-start turn needs §13's day-start machinery.
"""

import asyncio
import heapq
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from inkling.db.client import Database
from inkling.db.queries import commitments as commitments_q
from inkling.db.queries import triggers as triggers_q
from inkling.db.queries import users as users_q
from inkling.db.queries import wakeups as wakeups_q
from inkling.scheduler.occurrences import materialize_user, next_instance
from inkling.turns.ingress import PRIORITY_BY_SOURCE

log = logging.getLogger(__name__)

HORIZON = timedelta(hours=48)
REFRESH_SECONDS = 3600
RECONCILE_SECONDS = 60  # standalone role: refresh is the invalidation channel
LATE_AFTER = timedelta(minutes=5)
MIN_SLEEP = 0.05

# §13.1: nightly consolidation per user at their local 02:30. There is no DB
# row behind a job entry — the trigger's per-local-date idempotency key is the
# CAS. A boot inside the catch-up window still runs the night's pass (late);
# past that, the night is skipped rather than consolidated at dinnertime.
CONSOLIDATION_LOCAL_TIME = time(2, 30)
JOB_CATCHUP = timedelta(hours=6)


@dataclass(order=True)
class Entry:
    fire_at: datetime
    kind: str  # "occurrence" | "wakeup" | "job"
    user_id: str
    ref_id: int  # 0 for jobs: nothing in the DB to point at
    # Everything a fire needs beyond the key, loaded at refresh. db_fire_at is
    # the row's own time — it diverges from fire_at when quiet hours defer an
    # entry, and CAS guards and idempotency keys must use the row's value.
    extra: dict[str, Any] = field(compare=False, default_factory=dict)


def _in_quiet(local: datetime, quiet_start: time, quiet_end: time) -> bool:
    t = local.time()
    if quiet_start <= quiet_end:
        return quiet_start <= t < quiet_end
    return t >= quiet_start or t < quiet_end  # window wraps midnight


def quiet_deferral(
    now: datetime, *, tz: ZoneInfo, quiet_start: time, quiet_end: time
) -> datetime | None:
    """The next quiet_end after `now` if `now` is inside quiet hours, else None."""
    local = now.astimezone(tz)
    if not _in_quiet(local, quiet_start, quiet_end):
        return None
    end = local.replace(
        hour=quiet_end.hour, minute=quiet_end.minute,
        second=quiet_end.second, microsecond=0,
    )
    if end <= local:
        end += timedelta(days=1)
    return end


class Scheduler:
    def __init__(
        self,
        db: Database,
        user_ids: list[str],
        *,
        horizon: timedelta = HORIZON,
        refresh_seconds: float = REFRESH_SECONDS,
        late_after: timedelta = LATE_AFTER,
        nudge: Callable[[str], None] | None = None,
    ):
        self.db = db
        self.user_ids = user_ids
        self.horizon = horizon
        self.refresh_seconds = refresh_seconds
        self.late_after = late_after
        self.nudge = nudge
        self._heap: list[Entry] = []
        self._users: dict[str, dict[str, Any]] = {}
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._next_refresh: datetime | None = None  # None forces a refresh

    def wake(self) -> None:
        """In-process invalidation (§10): a future fire_at changed; re-scan."""
        self._next_refresh = None
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            now = datetime.now(timezone.utc)
            try:
                if self._next_refresh is None or now >= self._next_refresh:
                    await self.refresh(now=now)
                await self.fire_due(now=now)
            except Exception:
                # A bad row or a dropped connection must not kill the loop;
                # the next refresh rebuilds everything from the database.
                log.exception("scheduler pass failed; retrying after backoff")
                self._next_refresh = None
                await asyncio.sleep(2.0)
                continue
            await self._sleep(now)

    async def _sleep(self, now: datetime) -> None:
        assert self._next_refresh is not None  # refresh() just ran
        until = self._next_refresh
        if self._heap:
            until = min(until, self._heap[0].fire_at)
        delay = max((until - now).total_seconds(), MIN_SLEEP)
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=delay)
        except TimeoutError:
            pass
        finally:
            self._wake.clear()

    async def refresh(self, *, now: datetime | None = None) -> None:
        """Rebuild the heap from the database and materialize occurrences.

        A full rebuild every time: the fire path's compare-and-sets make
        re-heaped duplicates harmless, which is simpler than proving an
        incremental heap correct.
        """
        now = now or datetime.now(timezone.utc)
        horizon = now + self.horizon
        heap: list[Entry] = []
        for user_id in self.user_ids:
            try:
                async with self.db.with_tenant(user_id) as conn:
                    user = await users_q.get(conn, user_id)
                    if user is None:
                        log.warning("roster user %s not in users table; skipping", user_id)
                        continue
                    self._users[user_id] = user
                    tz = self._tz(user)
                    await materialize_user(conn, user_id=user_id, now=now, tz=tz)

                    for occ in await commitments_q.pending_occurrences(
                        conn, horizon=horizon
                    ):
                        heap.append(Entry(
                            fire_at=occ["fire_at"],
                            kind="occurrence",
                            user_id=user_id,
                            ref_id=int(occ["id"]),
                            extra={
                                "db_fire_at": occ["fire_at"],
                                "title": occ["title"],
                                "commitment_id": int(occ["commitment_id"]),
                                "quiet_policy": (occ["notify_policy"] or {}).get(
                                    "quiet_hours", "defer"
                                ),
                            },
                        ))

                    for wk in await wakeups_q.active(conn):
                        fire_at = wk["fire_at"]
                        if fire_at is None:
                            # A recurrence with no materialized instance yet:
                            # anchor at now and write the instance back so the
                            # row always says when it will next fire.
                            fire_at = await self._materialize_wakeup(conn, wk, now)
                            if fire_at is None:
                                continue
                        if fire_at > horizon:
                            continue
                        heap.append(Entry(
                            fire_at=fire_at,
                            kind="wakeup",
                            user_id=user_id,
                            ref_id=int(wk["id"]),
                            extra={
                                "db_fire_at": fire_at,
                                "purpose": wk["purpose"],
                                "recurrence": wk["recurrence"],
                                "respect_quiet": wk["respect_quiet"],
                            },
                        ))
                    job_at = self._next_consolidation(now, tz)
                    if job_at is not None:
                        heap.append(Entry(
                            fire_at=job_at,
                            kind="job",
                            user_id=user_id,
                            ref_id=0,
                            extra={
                                "job": "consolidate",
                                "local_date": job_at.astimezone(tz).date().isoformat(),
                            },
                        ))
            except Exception:
                log.exception("refresh failed for user %s; their entries retry next pass", user_id)
        heapq.heapify(heap)
        self._heap = heap
        self._next_refresh = now + timedelta(seconds=self.refresh_seconds)
        log.info("scheduler refreshed: %d entr(ies) in heap", len(heap))

    def _next_consolidation(self, now: datetime, tz: ZoneInfo) -> datetime | None:
        """Today's 02:30 local while it's due or within catch-up, else tomorrow's.

        Re-proposing an already-fired instance is harmless: the per-local-date
        idempotency key makes the trigger insert a no-op.
        """
        local = now.astimezone(tz)
        candidate = local.replace(
            hour=CONSOLIDATION_LOCAL_TIME.hour,
            minute=CONSOLIDATION_LOCAL_TIME.minute,
            second=0, microsecond=0,
        )
        if candidate > local:
            return candidate
        if local - candidate <= JOB_CATCHUP:
            return candidate
        return candidate + timedelta(days=1)

    async def _materialize_wakeup(
        self, conn: Any, wakeup: dict[str, Any], now: datetime
    ) -> datetime | None:
        try:
            instance = next_instance(wakeup["recurrence"], dtstart=now, after=now)
        except Exception:
            log.exception(
                "wakeup %s has unparseable recurrence %r; skipping",
                wakeup["id"], wakeup["recurrence"],
            )
            return None
        if instance is None:
            return None
        row = await wakeups_q.advance(
            conn, int(wakeup["id"]), old_fire_at=None, new_fire_at=instance
        )
        return row["fire_at"] if row else None

    async def fire_due(self, *, now: datetime | None = None) -> int:
        """Fire every heap entry at or past `now`. Returns how many fired."""
        now = now or datetime.now(timezone.utc)
        fired = 0
        while self._heap and self._heap[0].fire_at <= now:
            entry = heapq.heappop(self._heap)
            deferred = self._quiet_deferral_for(entry, now)
            if deferred is not None:
                heapq.heappush(
                    self._heap,
                    Entry(deferred, entry.kind, entry.user_id, entry.ref_id,
                          {**entry.extra, "quiet_deferred": True}),
                )
                continue
            try:
                if entry.kind == "wakeup":
                    ok = await self._fire_wakeup(entry, now)
                elif entry.kind == "job":
                    ok = await self._fire_job(entry, now)
                else:
                    ok = await self._fire_occurrence(entry, now)
            except Exception:
                # Leave it out of the heap; the next refresh re-proposes it if
                # the row still says it should fire.
                log.exception("firing %s %s for user %s failed",
                              entry.kind, entry.ref_id, entry.user_id)
                continue
            if ok:
                fired += 1
                if self.nudge is not None:
                    self.nudge(entry.user_id)
        return fired

    def _quiet_deferral_for(self, entry: Entry, now: datetime) -> datetime | None:
        """Quiet-hours deferral (§10), computed in the user's timezone.

        Wakeups defer per their respect_quiet flag; occurrences per notify
        policy ("defer" and, for now, "silent_summary" — see module docstring).
        Jobs never defer: consolidation at 02:30 is silent maintenance — quiet
        hours exist to protect sleep from pushes, not the DB from writes.
        Landing exactly at quiet_end can't re-defer: the window is [start, end).
        """
        if entry.kind == "job":
            return None
        if entry.kind == "wakeup" and not entry.extra["respect_quiet"]:
            return None
        if entry.kind == "occurrence" and entry.extra["quiet_policy"] not in (
            "defer", "silent_summary"
        ):
            return None
        user = self._users[entry.user_id]
        return quiet_deferral(
            now,
            tz=self._tz(user),
            quiet_start=user["quiet_start"],
            quiet_end=user["quiet_end"],
        )

    def _tz(self, user: dict[str, Any]) -> ZoneInfo:
        try:
            return ZoneInfo(user["timezone"])
        except Exception:
            return ZoneInfo("UTC")

    def _late(self, entry: Entry, now: datetime) -> bool:
        return now - entry.fire_at > self.late_after

    async def _fire_wakeup(self, entry: Entry, now: datetime) -> bool:
        """CAS the wakeup row and insert its trigger in one tenant transaction."""
        db_fire_at = entry.extra["db_fire_at"]
        async with self.db.with_tenant(entry.user_id) as conn:
            if entry.extra["recurrence"]:
                try:
                    nxt = next_instance(
                        entry.extra["recurrence"], dtstart=db_fire_at, after=db_fire_at
                    )
                except Exception:
                    log.exception("wakeup %s recurrence went unparseable", entry.ref_id)
                    return False
                row = await wakeups_q.advance(
                    conn, entry.ref_id, old_fire_at=db_fire_at, new_fire_at=nxt
                )
                # Idempotency per instance: the same (id, fire_at) can only
                # ever insert one trigger, however often it's re-heaped.
                key = f"wakeup:{entry.ref_id}:{db_fire_at.isoformat()}"
            else:
                row = await wakeups_q.mark_fired(conn, entry.ref_id)
                key = f"wakeup:{entry.ref_id}"
            if row is None:
                return False  # cancelled, already fired, or advanced elsewhere
            await triggers_q.insert(
                conn,
                user_id=entry.user_id,
                source="wakeup",
                source_ref=f"wakeup:{entry.ref_id}",
                idempotency_key=key,
                payload=json.dumps({
                    "text": entry.extra["purpose"],
                    "wakeup_id": entry.ref_id,
                    "late": self._late(entry, now),
                }),
                priority=PRIORITY_BY_SOURCE["wakeup"],
            )
        return True

    async def _fire_job(self, entry: Entry, now: datetime) -> bool:
        """Insert the consolidation trigger; its idempotency key is the CAS.

        False (key already present) means this local date's pass already ran
        or is queued — the normal outcome for every refresh after the first.
        """
        local_date = entry.extra["local_date"]
        async with self.db.with_tenant(entry.user_id) as conn:
            row = await triggers_q.insert(
                conn,
                user_id=entry.user_id,
                source="job",
                source_ref=f"consolidate:{local_date}",
                idempotency_key=f"consolidate:{local_date}",
                payload=json.dumps({
                    "job": "consolidate",
                    "text": f"Scheduled nightly consolidation for {local_date}.",
                    "late": self._late(entry, now),
                }),
                priority=PRIORITY_BY_SOURCE["job"],
            )
        return row is not None

    async def _fire_occurrence(self, entry: Entry, now: datetime) -> bool:
        """pending→eligible and the surfacing trigger, atomically (§10).

        The turn pipeline owns the rest: commit marks eligible→surfaced on
        success, and trigger retry semantics (§5.2) cover failures.
        """
        async with self.db.with_tenant(entry.user_id) as conn:
            row = await commitments_q.mark_occurrence_eligible(conn, entry.ref_id)
            if row is None:
                return False  # already eligible/surfaced/skipped
            await triggers_q.insert(
                conn,
                user_id=entry.user_id,
                source="occurrence",
                source_ref=f"occurrence:{entry.ref_id}",
                idempotency_key=f"occurrence:{entry.ref_id}",
                payload=json.dumps({
                    "text": (
                        f"Commitment '{entry.extra['title']}' is due — "
                        "surface it to the user."
                    ),
                    "commitment_id": entry.extra["commitment_id"],
                    "occurrence_id": entry.ref_id,
                    "late": self._late(entry, now),
                }),
                priority=PRIORITY_BY_SOURCE["occurrence"],
            )
        return True
