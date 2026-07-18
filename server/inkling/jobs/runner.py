"""System-jobs runner (§13.2/§13.3 timing): backup 03:10, drill monthly.

These jobs are server-scoped, not per-user, so they don't ride the §10
scheduler (whose entries become per-user turn triggers) — this is its own
small loop with the same shape: compute the next fire time, sleep to it, run,
repeat. 03:10 *server* time per spec (not per-user local). The drill runs on
the 1st after that night's backup, at 04:10.

Missed times behave like §13.1's catch-up: on boot, a backup due within the
last 6 h still runs (better a late backup than none); anything older waits
for tonight.
"""

import asyncio
import logging
import tempfile
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

BACKUP_AT = time(3, 10)
DRILL_AT = time(4, 10)  # on the 1st, after the backup
CATCHUP = timedelta(hours=6)
MIN_SLEEP = 1.0

Job = Callable[[Path], Awaitable[None]]


def next_run(
    now: datetime, at: time, *, monthly: bool = False, catchup: timedelta = CATCHUP
) -> datetime:
    """Next fire time for a daily (or monthly, day-1) job, with boot catch-up."""
    candidate = now.replace(
        hour=at.hour, minute=at.minute, second=0, microsecond=0
    )
    if monthly:
        candidate = candidate.replace(day=1)
        if candidate > now or now - candidate <= catchup:
            return candidate
        # First of next month.
        return (candidate + timedelta(days=32)).replace(day=1)
    if candidate > now or now - candidate <= catchup:
        return candidate
    return candidate + timedelta(days=1)


class JobsRunner:
    def __init__(
        self,
        *,
        backup: Job,
        drill: Job | None = None,
    ):
        self.backup = backup
        self.drill = drill
        self._stop = asyncio.Event()
        self.last_backup_date: str = ""
        self.last_drill_month: str = ""

    def stop(self) -> None:
        self._stop.set()

    async def tick(self, now: datetime) -> None:
        """Run whatever is due at `now`; date guards make re-ticks no-ops."""
        backup_at = next_run(now, BACKUP_AT)
        if backup_at <= now and self.last_backup_date != backup_at.date().isoformat():
            self.last_backup_date = backup_at.date().isoformat()
            with tempfile.TemporaryDirectory(prefix="inkling-backup-") as tmp:
                await self.backup(Path(tmp))
        drill_at = next_run(now, DRILL_AT, monthly=True)
        month = drill_at.strftime("%Y-%m")
        if (
            self.drill is not None
            and drill_at <= now
            and self.last_drill_month != month
        ):
            self.last_drill_month = month
            with tempfile.TemporaryDirectory(prefix="inkling-drill-") as tmp:
                await self.drill(Path(tmp))

    async def run_forever(self, *, tz=None) -> None:
        while not self._stop.is_set():
            now = datetime.now(tz).astimezone() if tz is None else datetime.now(tz)
            try:
                await self.tick(now)
            except Exception:
                log.exception("jobs tick failed; retrying at the next window")
            nxt = min(
                next_run(now, BACKUP_AT),
                next_run(now, DRILL_AT, monthly=True),
            )
            delay = max((nxt - now).total_seconds(), MIN_SLEEP)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=min(delay, 3600))
            except TimeoutError:
                pass
