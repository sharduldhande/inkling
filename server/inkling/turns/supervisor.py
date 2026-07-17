"""Owns one Worker task per user (§5.2).

The roster comes from CHAT_TOKENS_FILE, not the database: RLS scopes `users` to
the current tenant, so runtime_role cannot list them (see api/auth.py).

A worker task that dies takes only its user down, so the supervisor restarts it
rather than letting one user's bad turn silently stop their whole pipeline.
"""

import asyncio
import logging

from inkling.db.client import Database
from inkling.inference.gateway import InferenceGateway
from inkling.turns.worker import Subscriber, Worker

log = logging.getLogger(__name__)

RESTART_BACKOFF_SECONDS = 2.0


class Supervisor:
    def __init__(
        self,
        db: Database,
        user_ids: list[str],
        gateway: InferenceGateway,
        *,
        subscriber: Subscriber | None = None,
        restart_backoff: float = RESTART_BACKOFF_SECONDS,
    ):
        self.db = db
        self.gateway = gateway
        self.subscriber = subscriber
        self.restart_backoff = restart_backoff
        self.workers = {
            user_id: Worker(db, user_id, gateway, subscriber=subscriber)
            for user_id in user_ids
        }
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stopping = False

    def nudge(self, user_id: str) -> None:
        """Wake a user's worker; no-op for users without one."""
        worker = self.workers.get(user_id)
        if worker is not None:
            worker.nudge()

    async def start(self) -> None:
        for user_id in self.workers:
            self._tasks[user_id] = asyncio.create_task(
                self._supervise(user_id), name=f"worker:{user_id}"
            )
        log.info("supervisor started %d worker(s)", len(self._tasks))

    async def _supervise(self, user_id: str) -> None:
        while not self._stopping:
            try:
                await self.workers[user_id].run_forever()
                return  # clean stop
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "worker for user %s crashed; restarting in %.1fs",
                    user_id,
                    self.restart_backoff,
                )
                await asyncio.sleep(self.restart_backoff)

    async def stop(self) -> None:
        self._stopping = True
        for worker in self.workers.values():
            worker.stop()
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        log.info("supervisor stopped")
