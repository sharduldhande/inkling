"""POST /live-activity (§11): the app reports its Live Activity tokens.

Body: {"environment": "development"|"production", "enabled": bool,
       "start_token": hex|null, "activity": {"id": str, "token": hex}|null,
       "start_here": bool (optional)}

Sent on launch and whenever iOS hands the app a token (including the
background wake right after a server push-to-start). `activity: null` means
no Inkling activity is on screen, which lets the syncer start one.

`start_here: true` (with `activity: null`) is the foregrounded app saying it
will start the card itself: a local `Activity.request` isn't subject to iOS's
push-to-start budget, which a swiped-away card plus repeated server starts can
exhaust (Sep 28 – Oct 1 2026: ~14 accepted starts a day, none shown). The
reply carries the content to start with, and the row is marked started with
it, so the syncer waits for the new activity's token instead of pushing a
start of its own. Auth is the routes_history shape: bearer token → roster →
tenant.
"""

import re

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse

from inkling.api.v1_contract import UNAUTHORIZED_BODY
from inkling.db.queries import live_activity as la_q
from inkling.delivery.live_activity import now_next

router = APIRouter()

_TOKEN = re.compile(r"[0-9a-f]{32,400}")
ENVIRONMENTS = {"development", "production"}


def _token(value) -> str | None:
    if value is None:
        return None
    token = str(value).strip().lower()
    return token if _TOKEN.fullmatch(token) else ""


@router.post("/live-activity")
async def register_live_activity(
    request: Request, authorization: str | None = Header(default=None)
):
    app = request.app
    user_id = app.state.roster.user_for_header(authorization)
    if user_id is None:
        return Response(content=UNAUTHORIZED_BODY, status_code=401)

    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected a JSON object"}, status_code=422)

    environment = str(body.get("environment", "")).strip()
    if environment not in ENVIRONMENTS:
        return JSONResponse(
            {"error": "environment must be 'development' or 'production'"},
            status_code=422,
        )
    enabled = body.get("enabled", True)
    if not isinstance(enabled, bool):
        return JSONResponse({"error": "enabled must be a boolean"}, status_code=422)
    start_token = _token(body.get("start_token"))
    if start_token == "":
        return JSONResponse({"error": "start_token must be hex"}, status_code=422)

    start_here = body.get("start_here", False)
    if not isinstance(start_here, bool):
        return JSONResponse({"error": "start_here must be a boolean"}, status_code=422)

    activity = body.get("activity")
    activity_id = activity_token = None
    if activity is not None:
        if not isinstance(activity, dict):
            return JSONResponse({"error": "activity must be an object"}, status_code=422)
        activity_id = str(activity.get("id", "")).strip()[:100]
        activity_token = _token(activity.get("token"))
        if not activity_id or not activity_token:
            return JSONResponse(
                {"error": "activity needs an id and a hex token"}, status_code=422
            )

    content = None
    async with app.state.db.with_tenant(user_id) as conn:
        await la_q.register(
            conn,
            user_id=user_id,
            environment=environment,
            start_token=start_token,
            enabled=enabled,
        )
        if activity_id:
            await la_q.set_activity(
                conn, user_id, activity_id=activity_id, update_token=activity_token
            )
        elif start_here and enabled:
            content = await now_next(conn, user_id)
            await la_q.mark_started(conn, user_id, content)
        else:
            await la_q.clear_activity(conn, user_id)

    if content is not None:
        return JSONResponse({"ok": True, "content": content})
    wake = getattr(app.state, "live_activity_wake", None)
    if wake is not None:
        wake()
    return JSONResponse({"ok": True})
