"""POST /agenda/done: the Today tab's "Mark done" (the one pane write).

Chat stays the main write path; this is the single shortcut, for the
commonest and least ambiguous action. Every tap is the USER's verdict (the
same standing as saying it in chat) and leaves the same trail a chat
completion would: the state change, a system event, and a visible user line
in the transcript, so the chat shows it and the model reads it next turn.

Body: {"id": "c:<commitment>" | "o:<occurrence>" | "g:<calendar event>"},
the ids /agenda hands out. Recurring ("o:") checks off today, exactly like a
habit_done in chat; the commitment stays open for tomorrow. Dropping,
rescheduling and anything ambiguous stay in chat.
"""

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse

from inkling.api.v1_contract import UNAUTHORIZED_BODY
from inkling.db.queries import calendar as calendar_q
from inkling.db.queries import commitments as commitments_q
from inkling.db.queries import events as events_q

router = APIRouter()

_OCCURRENCE = """
SELECT o.id, o.commitment_id, c.title, c.status
FROM commitment_occurrences o
JOIN commitments c ON c.id = o.commitment_id
WHERE o.id = %s
"""
_TIMEZONE = "SELECT timezone FROM users WHERE id = %s"
_TRANSCRIPT = (
    "INSERT INTO transcript (user_id, turn_id, role, text) "
    "VALUES (%s, NULL, 'user', %s)"
)


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _event(conn, user_id: str, note: str, **kwargs) -> None:
    await events_q.insert(
        conn,
        user_id=user_id,
        occurred_at=None,
        kind=kwargs.pop("kind", "system"),
        habit_id=None,
        value_num=None,
        unit=None,
        note=note,
        source="app",
        **kwargs,
    )


async def _done_commitment(conn, user_id: str, ref: int) -> str | JSONResponse:
    row = await commitments_q.get(conn, ref)
    if row is None:
        return _error("no such commitment", 404)
    if row["status"] != "open":
        return _error(f"already {row['status']}", 409)
    if row["recurrence"] is not None:
        return _error("recurring commitments are checked off per day (o: ids)", 422)
    updated = await commitments_q.set_status(
        conn, commitment_id=ref, version=row["version"], status="done"
    )
    if updated is None:
        return _error("changed while you tapped; refresh and retry", 409)
    await commitments_q.skip_unsurfaced_occurrences(conn, ref)
    await _event(
        conn, user_id,
        f"completed commitment {ref} ({row['title']}): user marked done in Today",
    )
    return row["title"]


async def _done_occurrence(conn, user_id: str, ref: int) -> str | JSONResponse:
    cur = await conn.execute(_OCCURRENCE, (ref,))
    row = await cur.fetchone()
    if row is None:
        return _error("no such occurrence", 404)
    if row["status"] != "open":
        return _error(f"commitment already {row['status']}", 409)
    cur = await conn.execute(_TIMEZONE, (user_id,))
    timezone = (await cur.fetchone())["timezone"]
    event = await events_q.insert(
        conn,
        user_id=user_id,
        occurred_at=None,
        kind="habit_done",
        habit_id=None,
        value_num=None,
        unit=None,
        note=f"{row['title']}: user marked done in Today",
        source="app",
        commitment_id=row["commitment_id"],
    )
    await commitments_q.skip_same_day_occurrences(
        conn,
        commitment_id=row["commitment_id"],
        done_at=event["occurred_at"],
        timezone=timezone,
    )
    return row["title"]


async def _done_calendar(conn, user_id: str, ref: str) -> str | JSONResponse:
    row = await calendar_q.get_live(conn, ref)
    if row is None:
        return _error("no such calendar item", 404)
    if row["done_at"] is not None:
        return _error("already marked done", 409)
    await calendar_q.set_done(conn, ref, done=True)
    title = row["summary"] or "(untitled)"
    await _event(conn, user_id, f"marked calendar item {title!r} as done in Today")
    return title


@router.post("/agenda/done")
async def mark_done(request: Request, authorization: str | None = Header(default=None)):
    app = request.app
    user_id = app.state.roster.user_for_header(authorization)
    if user_id is None:
        return Response(content=UNAUTHORIZED_BODY, status_code=401)
    try:
        body = await request.json()
    except ValueError:
        body = None
    item_id = body.get("id") if isinstance(body, dict) else None
    if not isinstance(item_id, str) or item_id[:2] not in ("c:", "o:", "g:"):
        return _error("expected {\"id\": \"c:…\" | \"o:…\" | \"g:…\"}", 422)
    prefix, ref = item_id[:2], item_id[2:]
    if prefix in ("c:", "o:") and not ref.isdigit():
        return _error("malformed id", 422)

    async with app.state.db.with_tenant(user_id) as conn:
        if prefix == "c:":
            result = await _done_commitment(conn, user_id, int(ref))
        elif prefix == "o:":
            result = await _done_occurrence(conn, user_id, int(ref))
        else:
            result = await _done_calendar(conn, user_id, ref)
        if isinstance(result, JSONResponse):
            return result  # the transaction rolls back nothing: no writes yet
        await conn.execute(_TRANSCRIPT, (user_id, f"✓ Marked done in Today: {result}"))

    wake = getattr(app.state, "live_activity_wake", None)
    if wake is not None:
        wake()
    return JSONResponse({"ok": True, "title": result})
