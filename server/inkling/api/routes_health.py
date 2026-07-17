"""GET /health (§6).

v1 shape `{"status":"ok","busy":bool}` kept exactly. v1's `busy` was one global
boolean because v1 had one user; here it means "a turn is running for the
token's user". Unauthenticated callers get busy:false rather than a 401 — v1
never authenticated /health and the app polls it.
"""

from fastapi import APIRouter, Header, Request

from inkling.db.queries import turns as turns_q

router = APIRouter()


@router.get("/health")
async def health(request: Request, authorization: str | None = Header(default=None)):
    user_id = request.app.state.roster.user_for_header(authorization)
    busy = False
    if user_id is not None:
        async with request.app.state.db.with_tenant(user_id) as conn:
            busy = await turns_q.running_for_user(conn, user_id) is not None
    return {"status": "ok", "busy": busy}
