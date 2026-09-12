"""FastAPI app factory and process lifespan (§6).

Roles must be independently runnable (§1). `create_app` builds the API; when
`supervisor` is passed it also owns the worker tasks, which is the `all` role
running everything in one asyncio process. Run role=api without one and the
API serves reads and records triggers while some other process turns them.

v1 answered 404 with an empty body for every unknown path; FastAPI's default
is a JSON detail object, so that is overridden below.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import Response

from inkling.api import routes_calendar, routes_chat, routes_health, routes_history
from inkling.api import routes_devices, routes_documents, routes_panes
from inkling.api.auth import Roster
from inkling.api.broker import Broker
from inkling.db.client import Database
from inkling.turns.supervisor import Supervisor

log = logging.getLogger(__name__)


class _NoSupervisor:
    """Stand-in for role=api: triggers are recorded, someone else turns them."""

    def nudge(self, user_id: str) -> None:
        return None


def create_app(
    db: Database,
    roster: Roster,
    *,
    broker: Broker | None = None,
    supervisor: Supervisor | None = None,
    calendar_sync=None,
    uploads_dir: str = "",  # EXPORT_GIT_DIR; §14 originals land under uploads/
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if supervisor is not None:
            await supervisor.start()
        try:
            yield
        finally:
            if supervisor is not None:
                await supervisor.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db = db
    app.state.roster = roster
    app.state.broker = broker or Broker()
    app.state.supervisor = supervisor or _NoSupervisor()
    app.state.calendar_sync = calendar_sync  # None when calendar is off (§12)
    app.state.uploads_dir = uploads_dir

    app.include_router(routes_health.router)
    app.include_router(routes_history.router)
    app.include_router(routes_chat.router)
    app.include_router(routes_calendar.router)
    app.include_router(routes_panes.router)
    app.include_router(routes_documents.router)
    app.include_router(routes_devices.router)

    @app.exception_handler(404)
    async def not_found(request: Request, exc) -> Response:
        return Response(status_code=404)

    return app
