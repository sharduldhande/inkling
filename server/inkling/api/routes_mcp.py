"""POST /mcp/<secret>: a remote MCP server so other chats can add reminders.

Claude (claude.ai, Claude Code), ChatGPT (developer-mode connectors) and
Codex all speak MCP's Streamable HTTP transport. This is the minimal
stateless form of it: one JSON-RPC request per POST, answered with a plain
application/json body (no SSE stream, no sessions), which the spec allows.

Auth is the URL itself: MCP_TOKENS_FILE holds {"<user_id>": "<secret>"} and
the path segment must match one (constant-time compare). Any other path is a
bare 404, so a wrong URL learns nothing. The only tool is write-only
(add_reminder): whoever holds the URL can add to the list, never read it.

A reminder is created DIRECTLY (user decision) as an open commitment with its
first occurrence, exactly like a chat UpsertCommitment, and a visible line
lands in the Inkling chat saying which assistant added it, so the model sees
it on its next turn.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from secrets import compare_digest
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from inkling.db.queries import commitments as commitments_q
from inkling.db.queries import events as events_q
from inkling.turns.apply import _materialize_first_occurrence

log = logging.getLogger(__name__)

router = APIRouter()

SERVER_INFO = {"name": "inkling", "version": "1.0"}
PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
MAX_TITLE = 200
MAX_NOTES = 2000
MAX_SOURCE = 40

INSTRUCTIONS = (
    "Inkling is the user's personal accountability assistant. Use add_reminder "
    "when the user asks you to remind them of something, or to put a task or "
    "deadline on their Inkling list. It is added immediately and Inkling "
    "follows up with push notifications."
)

ADD_REMINDER = {
    "name": "add_reminder",
    "title": "Add reminder to Inkling",
    "description": (
        "Add a reminder or deadline to the user's Inkling list. Inkling pushes "
        "a notification at the due time and keeps following up until the user "
        "says it's done. Only call this when the user asked for a reminder or "
        "task; state what you added in your reply."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short imperative title, e.g. 'Call the dentist'.",
            },
            "due": {
                "type": "string",
                "description": (
                    "When it's due, ISO 8601 (e.g. 2026-09-25T15:00). Without a "
                    "UTC offset it is read as the user's local time. Omit for "
                    "an undated task."
                ),
            },
            "notes": {
                "type": "string",
                "description": "Optional context: links, details, why it matters.",
            },
            "source": {
                "type": "string",
                "description": "Your assistant's name, e.g. 'ChatGPT', 'Claude', 'Codex'.",
            },
        },
        "required": ["title"],
        "additionalProperties": False,
    },
    "annotations": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
}

_TIMEZONE = "SELECT timezone FROM users WHERE id = %s"
_TRANSCRIPT = (
    "INSERT INTO transcript (user_id, turn_id, role, text) "
    "VALUES (%s, NULL, 'user', %s)"
)


def load_mcp_tokens(path: str) -> dict[str, str]:
    """{user_id: secret}; an unset path disables the endpoint (no users)."""
    if not path:
        return {}
    raw = json.loads(Path(path).expanduser().read_text())
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and len(v) >= 32
        for k, v in raw.items()
    ):
        raise ValueError(f"{path}: expected {{user_id: secret}} with secrets of 32+ chars")
    return dict(raw)


def _user_for_secret(tokens: dict[str, str], secret: str) -> str | None:
    match = None
    for user_id, expected in tokens.items():
        if compare_digest(secret.encode(), expected.encode()):
            match = user_id
    return match


# --- JSON-RPC plumbing --------------------------------------------------------

def _result(msg_id: Any, result: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _error(msg_id: Any, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}},
        status_code=status,
    )


def _tool_text(msg_id: Any, text: str, *, is_error: bool = False) -> JSONResponse:
    return _result(
        msg_id, {"content": [{"type": "text", "text": text}], "isError": is_error}
    )


# --- the tool -----------------------------------------------------------------

def _parse_due(value: Any, tz: ZoneInfo, now: datetime) -> datetime | str | None:
    """A datetime, None (undated), or an error string for the calling model."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        return "due must be an ISO 8601 string"
    try:
        due = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return f"couldn't read due {value!r}; use ISO 8601 like 2026-09-25T15:00"
    if due.tzinfo is None:
        due = due.replace(tzinfo=tz)  # naive = the user's wall clock
    if due < now - timedelta(days=1):
        return "that due time is in the past; check the date"
    if due > now + timedelta(days=730):
        return "that due time is more than two years out; check the date"
    return due


def _when(due: datetime | None, tz: ZoneInfo) -> str:
    if due is None:
        return "no due date"
    local = due.astimezone(tz)
    return "due " + local.strftime("%a %b %-d, %-I:%M %p")


async def _add_reminder(
    request: Request, user_id: str, args: dict[str, Any]
) -> tuple[str, bool]:
    """(message for the calling model, is_error)."""
    title = args.get("title")
    if not isinstance(title, str) or not title.strip():
        return "title is required", True
    title = " ".join(title.split())
    if len(title) > MAX_TITLE:
        return f"title is over {MAX_TITLE} characters; shorten it", True
    notes = args.get("notes")
    if notes is not None and not isinstance(notes, str):
        return "notes must be a string", True
    notes = (notes or "").strip()[:MAX_NOTES] or None
    source = args.get("source")
    source = " ".join(source.split())[:MAX_SOURCE] if isinstance(source, str) else ""
    source = source or "another chat"

    db = request.app.state.db
    now = datetime.now(timezone.utc)
    async with db.with_tenant(user_id) as conn:
        cur = await conn.execute(_TIMEZONE, (user_id,))
        tz = ZoneInfo((await cur.fetchone())["timezone"])
        due = _parse_due(args.get("due"), tz, now)
        if isinstance(due, str):
            return due, True

        # Models retry tool calls; the same open title + due is the same
        # reminder, not a second one.
        existing = await commitments_q.open_with_title(conn, title)
        if existing is not None and existing["due_at"] == due:
            return f"Already on the Inkling list: {title} ({_when(due, tz)}).", False

        row = await commitments_q.insert(
            conn,
            user_id=user_id,
            title=title,
            body=notes,
            due_at=due,
            recurrence=None,
            lead_time=None,
            source="mcp",
            source_ref=source,
            notify_policy=None,
        )
        await _materialize_first_occurrence(conn, row, user_id=user_id)
        await events_q.insert(
            conn,
            user_id=user_id,
            occurred_at=None,
            kind="system",
            habit_id=None,
            value_num=None,
            unit=None,
            note=f"{source} added commitment {row['id']} ({title}) via MCP",
            source="mcp",
        )
        line = f"📌 Added from {source}: {title} ({_when(due, tz)})"
        if notes:
            line += f"\n{notes}"
        await conn.execute(_TRANSCRIPT, (user_id, line))

    for hook in ("schedule_wake", "live_activity_wake"):
        wake = getattr(request.app.state, hook, None)
        if wake is not None:
            wake()
    log.info("mcp: %s added commitment %s for %s", source, row["id"], user_id)
    return f"Added to Inkling: {title} ({_when(due, tz)}).", False


# --- the endpoint -------------------------------------------------------------

@router.api_route("/mcp/{secret}", methods=["GET", "DELETE"])
async def mcp_other(secret: str, request: Request) -> Response:
    if _user_for_secret(request.app.state.mcp_tokens, secret) is None:
        return Response(status_code=404)
    # Stateless: no server-initiated stream to open, no session to end.
    return Response(status_code=405, headers={"Allow": "POST"})


@router.post("/mcp/{secret}")
async def mcp(secret: str, request: Request) -> Response:
    user_id = _user_for_secret(request.app.state.mcp_tokens, secret)
    if user_id is None:
        return Response(status_code=404)

    try:
        msg = await request.json()
    except ValueError:
        return _error(None, -32700, "parse error", status=400)
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _error(None, -32600, "expected one JSON-RPC 2.0 message", status=400)

    method = msg.get("method")
    msg_id = msg.get("id")
    if msg_id is None:
        # A notification (initialized, cancelled, ...): acknowledge, no body.
        return Response(status_code=202)
    params = msg.get("params") or {}

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _result(msg_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": [ADD_REMINDER]})
    if method == "tools/call":
        if params.get("name") != "add_reminder":
            return _error(msg_id, -32602, f"unknown tool {params.get('name')!r}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _error(msg_id, -32602, "arguments must be an object")
        text, is_error = await _add_reminder(request, user_id, args)
        return _tool_text(msg_id, text, is_error=is_error)
    return _error(msg_id, -32601, f"method {method!r} not supported")
