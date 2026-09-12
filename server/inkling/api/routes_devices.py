"""POST /devices (§11): the app registers its APNs device token.

Called on every app launch (tokens can rotate; a revoked row comes back to
life). Auth is the routes_history shape: bearer token → roster → tenant.
"""

import re

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse
from psycopg.errors import InsufficientPrivilege, UniqueViolation

from inkling.api.v1_contract import UNAUTHORIZED_BODY
from inkling.db.queries import devices as devices_q

router = APIRouter()

# APNs tokens are 32 bytes today (64 hex); Apple reserves the right to grow
# them, so accept longer but still only hex.
_TOKEN = re.compile(r"[0-9a-f]{64,200}")
ENVIRONMENTS = {"development", "production"}


@router.post("/devices")
async def register_device(
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

    token = str(body.get("token", "")).strip().lower()
    environment = str(body.get("environment", "")).strip()
    platform = str(body.get("platform", "ios")).strip() or "ios"
    app_version = body.get("app_version")
    if not _TOKEN.fullmatch(token):
        return JSONResponse({"error": "token must be hex (64+ chars)"}, status_code=422)
    if environment not in ENVIRONMENTS:
        return JSONResponse(
            {"error": "environment must be 'development' or 'production'"},
            status_code=422,
        )
    if app_version is not None and not isinstance(app_version, str):
        return JSONResponse({"error": "app_version must be a string"}, status_code=422)

    try:
        async with app.state.db.with_tenant(user_id) as conn:
            row = await devices_q.upsert(
                conn,
                user_id=user_id,
                token=token,
                environment=environment,
                app_version=app_version,
                platform=platform[:32],
            )
    except (UniqueViolation, InsufficientPrivilege):
        # The token belongs to another user's row: RLS hides it from the ON
        # CONFLICT clause (UniqueViolation) or refuses the update (policy
        # violation) — either way, not this user's device to claim.
        return JSONResponse(
            {"error": "token registered to another user"}, status_code=409
        )

    return {
        "device_id": int(row["id"]),
        "environment": row["environment"],
        "registered_at": row["registered_at"].isoformat(),
    }
