"""In-process fan-out from workers to live SSE sockets (§5.5).

Best-effort by design. `turn_events` is the durable record and the client can
always replay it with Last-Event-ID, so this path is allowed to drop: a slow or
dead socket must never stall a turn. Queues are bounded and full queues drop
rather than block.

In-process only — no LISTEN/NOTIFY (§3). If the API and worker are ever split
across processes, SSE falls back to replaying turn_events, which is why the
route polls as well as subscribes.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from inkling.turns.stream import StreamEvent

log = logging.getLogger(__name__)

QUEUE_MAX = 256


class Broker:
    def __init__(self, queue_max: int = QUEUE_MAX):
        self._subscribers: dict[str, set[asyncio.Queue[StreamEvent]]] = {}
        self._queue_max = queue_max

    def open(self, user_id: str) -> asyncio.Queue[StreamEvent]:
        """Register a queue and start buffering events for `user_id`.

        Paired with close() rather than only offered as a context manager
        because /chat must stay subscribed across a `return
        StreamingResponse(...)`: a `with` block there unsubscribes before the
        generator ever runs, and every event silently misses.
        """
        queue: asyncio.Queue[StreamEvent] = asyncio.Queue(maxsize=self._queue_max)
        self._subscribers.setdefault(user_id, set()).add(queue)
        return queue

    def close(self, user_id: str, queue: asyncio.Queue[StreamEvent]) -> None:
        subs = self._subscribers.get(user_id)
        if subs is not None:
            subs.discard(queue)
            if not subs:
                del self._subscribers[user_id]

    @asynccontextmanager
    async def subscribe(self, user_id: str) -> AsyncIterator[asyncio.Queue[StreamEvent]]:
        queue = self.open(user_id)
        try:
            yield queue
        finally:
            self.close(user_id, queue)

    async def publish(self, event: StreamEvent) -> None:
        for queue in self._subscribers.get(event.user_id, set()):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning(
                    "dropping live event turn=%s seq=%s: subscriber queue full "
                    "(client can resume from turn_events)",
                    event.turn_id,
                    event.seq,
                )
