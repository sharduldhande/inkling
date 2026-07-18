"""POST /calendar/webhook/google (§6, §12): webhook → coalescible hint.

Google's push notification carries no body worth parsing — it is purely a
"something changed" doorbell. Validation is the channel id: it must match a
channel we registered (calendar_sync_state.channel_id). RLS means the lookup
walks the roster per tenant; the roster is small by design.

Always 200: Google retries non-2xx aggressively and backs off channels that
keep failing, and an attacker probing with made-up channel ids learns nothing
from a uniform response. Unmatched pings are logged and dropped.
"""

import logging

from fastapi import APIRouter, Request, Response

from inkling.db.queries import calendar as calendar_q
from inkling.turns import ingress

log = logging.getLogger(__name__)

router = APIRouter()


@router.post("/calendar/webhook/google")
async def google_webhook(request: Request) -> Response:
    channel_id = request.headers.get("x-goog-channel-id", "")
    state = request.headers.get("x-goog-resource-state", "")
    if not channel_id:
        return Response(status_code=200)
    if state == "sync":  # channel-created handshake ping, not a change
        return Response(status_code=200)

    db = request.app.state.db
    roster = request.app.state.roster
    for user_id in roster.user_ids:
        async with db.with_tenant(user_id) as conn:
            match = await calendar_q.state_by_channel(conn, channel_id)
        if match is None:
            continue
        await ingress.submit(
            db,
            user_id=user_id,
            source="calendar",
            source_ref=match["calendar_id"],  # what hints coalesce on (§4.2)
            payload={"kind": "hint"},
        )
        sync = getattr(request.app.state, "calendar_sync", None)
        if sync is not None:
            sync.wake()
        return Response(status_code=200)

    log.warning("calendar webhook with unknown channel id %s", channel_id)
    return Response(status_code=200)
