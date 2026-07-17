"""POST /chat and GET /turns/{id}/events (§5.1, §5.5, §6).

The API never runs a turn. It records a trigger and streams what the worker
persists, which is what lets a dropped socket or an API restart lose nothing:
the turn keeps running and the client replays turn_events.

Streaming reads from the database, with the broker used only as a wakeup so
the read happens immediately instead of at the next poll. The database is the
single source of truth for what the client has seen — the live path can drop
events (bounded queues) without the client ever noticing.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import StreamingResponse

from inkling.api.wire import EXIT_ERROR, frame, is_terminal, render
from inkling.api.v1_contract import (
    BAD_REQUEST_BODY,
    BUSY_BODY,
    BUSY_MEDIA_TYPE,
    BUSY_STATUS,
    MAX_BODY_BYTES,
    SSE_HEADERS,
    UNAUTHORIZED_BODY,
)
from inkling.db.queries import triggers as triggers_q
from inkling.db.queries import turns as turns_q
from inkling.turns import ingress

log = logging.getLogger(__name__)

router = APIRouter()

# The broker wakes us on every event in-process, so this is only a safety net
# for a split deployment where the worker publishes into a different process.
POLL_SECONDS = 2.0

# Lease (90s) plus grace: past this the reaper has failed the turn, so an error
# event is coming and a socket still waiting is a bug, not patience.
STREAM_TIMEOUT_SECONDS = 150.0


@router.post("/chat")
async def chat(request: Request, authorization: str | None = Header(default=None)):
    app = request.app
    user_id = app.state.roster.user_for_header(authorization)
    if user_id is None:
        return Response(content=UNAUTHORIZED_BODY, status_code=401)

    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        # v1 called req.destroy() and never answered. ASGI has no clean socket
        # reset, so this answers instead of hanging. Unreachable for the app,
        # whose messages are far below 64KB.
        return Response(status_code=413)

    try:
        message = json.loads(body).get("message")
    except (json.JSONDecodeError, AttributeError):
        message = None
    if not isinstance(message, str) or not message:
        return Response(content=BAD_REQUEST_BODY, status_code=400)

    # Subscribe before submitting: the worker can claim and start emitting the
    # moment the trigger lands, and those events must not be missed. The queue
    # outlives this function — _stream closes it.
    queue = app.state.broker.open(user_id)
    try:
        submitted = await ingress.submit(
            app.state.db, user_id=user_id, source="chat", payload={"text": message}
        )

        # Busy means a turn for a *different* trigger is running. The worker
        # can legitimately claim our own trigger between submit and this check
        # (it polls every second); that turn is the answer to this request and
        # must be streamed, not 429ed.
        async with app.state.db.with_tenant(user_id) as conn:
            busy = (
                await turns_q.running_for_other_trigger(
                    conn, user_id, submitted.trigger_id
                )
                is not None
            )
    except Exception:
        app.state.broker.close(user_id, queue)
        raise

    if busy:
        # §5.1/D8: the trigger stays pending and runs when the worker frees,
        # so the message is not lost even though the socket gets a 429.
        app.state.broker.close(user_id, queue)
        return Response(
            content=BUSY_BODY,
            status_code=BUSY_STATUS,
            media_type=BUSY_MEDIA_TYPE,
        )

    app.state.supervisor.nudge(user_id)
    return StreamingResponse(
        _stream(app, user_id, submitted.trigger_id, queue),
        headers=dict(SSE_HEADERS),
    )


async def _stream(app, user_id: str, trigger_id: int, queue) -> AsyncIterator[str]:
    turn_id: int | None = None
    last_seq = 0
    loop = asyncio.get_running_loop()
    deadline = loop.time() + STREAM_TIMEOUT_SECONDS

    try:
        while True:
            if loop.time() > deadline:
                log.warning("stream for trigger %s timed out", trigger_id)
                yield frame({"text": "the assistant timed out"})
                yield frame({"done": True})
                yield frame({"exit": EXIT_ERROR})
                return

            try:
                await asyncio.wait_for(queue.get(), timeout=POLL_SECONDS)
            except TimeoutError:
                pass  # fall through and poll; the broker is a hint, not the truth

            async with app.state.db.with_tenant(user_id) as conn:
                if turn_id is None:
                    turn_id = await triggers_q.turn_id_for(conn, trigger_id)
                    if turn_id is None:
                        continue  # not claimed yet
                rows = await turns_q.events_since(conn, turn_id, last_seq)

            for row in rows:
                last_seq = int(row["seq"])
                for chunk in render(row["kind"], row["data"]):
                    yield chunk
                if is_terminal(row["kind"]):
                    return
    finally:
        app.state.broker.close(user_id, queue)


@router.get("/turns/{turn_id}/events")
async def turn_events(
    turn_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    """Replay a turn's persisted stream (§5.5).

    Last-Event-ID is `<turn_id>:<seq>`; anything unparseable replays from the
    start, which is safe because replay is idempotent.
    """
    user_id = request.app.state.roster.user_for_header(authorization)
    if user_id is None:
        return Response(content=UNAUTHORIZED_BODY, status_code=401)

    since = 0
    if last_event_id and ":" in last_event_id:
        _, _, raw_seq = last_event_id.partition(":")
        if raw_seq.isdigit():
            since = int(raw_seq)

    async with request.app.state.db.with_tenant(user_id) as conn:
        rows = await turns_q.events_since(conn, turn_id, since)

    async def replay() -> AsyncIterator[str]:
        # RLS scopes the read, so another tenant's turn id simply yields nothing.
        for row in rows:
            for chunk in render(row["kind"], row["data"]):
                yield chunk

    return StreamingResponse(replay(), headers=dict(SSE_HEADERS))
