"""GET /history (§6) — v1 shape.

v1 read a JSONL file of {id, role, text, ts} and returned
`{"messages": [...]}` filtered to `ts > since` and capped at the most recent
200. v2 reads the transcript table and reproduces that shape.

Two fidelity details that matter to the unmodified app:
- `ts` is epoch milliseconds (v1 wrote Date.now()), not an ISO timestamp.
- `id` is a string. v1's ids were crypto.randomUUID(); ours are bigserial, so
  they are stringified. A Swift `String` field will not decode a JSON number.
"""

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse

from inkling.api.v1_contract import HISTORY_MAX, UNAUTHORIZED_BODY

router = APIRouter()

_HISTORY = """
SELECT id, role, text, (extract(epoch from ts) * 1000)::bigint AS ts
FROM transcript
WHERE user_id = %s AND (extract(epoch from ts) * 1000)::bigint > %s
ORDER BY ts DESC, id DESC
LIMIT %s
"""


@router.get("/history")
async def history(
    request: Request,
    since: int = 0,
    authorization: str | None = Header(default=None),
):
    user_id = request.app.state.roster.user_for_header(authorization)
    if user_id is None:
        return Response(content=UNAUTHORIZED_BODY, status_code=401)

    async with request.app.state.db.with_tenant(user_id) as conn:
        cur = await conn.execute(_HISTORY, (user_id, since, HISTORY_MAX))
        rows = await cur.fetchall()

    # Newest-first above so LIMIT keeps the *most recent* 200 (v1 sliced the
    # tail); flip back to chronological, which is the order v1 emitted.
    messages = [
        {"id": str(r["id"]), "role": r["role"], "text": r["text"], "ts": int(r["ts"])}
        for r in reversed(rows)
    ]
    return JSONResponse({"messages": messages})
