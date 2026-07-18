"""The `recall` read tool: model-directed memory retrieval, mid-turn.

Post-spec amendment (2026-07-18, user decision): §8's deterministic assembly
decides what every turn gets *by default*, but only the model can judge what a
particular situation needs — especially proactive turns, where there is no
user phrasing for a rule to gate on. So alongside the write-only
emit_commands tool, the model gets exactly one read tool. The write path is
unchanged: recall returns text, mutates nothing, and every query runs inside
its own short tenant transaction (§5.3 forbids transactions *held across*
inference, not reads between tokens; RLS scopes everything to the turn's
user).

Budgeted like every §8 section: results are clipped to RESULT_TOKEN_BUDGET,
and the worker's handler enforces MAX_RECALLS_PER_TURN so a curious model
can't loop the database. Failures and empty results return honest text —
a recall that errors must degrade into "nothing found", never a failed turn.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any

from inkling.db.client import Database
from inkling.db.queries import events as events_q
from inkling.db.queries import transcript as transcript_q
from inkling.inference.gateway import ToolHandler, ToolSchema
from inkling.turns.validate import estimate_tokens

log = logging.getLogger(__name__)

MAX_RECALLS_PER_TURN = 4
RESULT_LIMIT = 12
RESULT_TOKEN_BUDGET = 500

RECALL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Free-text search over past events and conversation "
                "(word-match, OR semantics). Empty string with a date range "
                "lists that period chronologically."
            ),
        },
        "since": {
            "type": "string",
            "description": "ISO date/datetime lower bound (optional).",
        },
        "until": {
            "type": "string",
            "description": "ISO date/datetime upper bound (optional).",
        },
        "kinds": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Restrict event kinds, e.g. ['measurement'] for food/exercise "
                "numbers, ['habit_done'] for habit history (optional)."
            ),
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}


def recall_tool() -> ToolSchema:
    return ToolSchema(
        name="recall",
        description=(
            "Search your memory (past events and conversation) when the "
            "current context is not enough — e.g. history the user refers "
            "to, food/exercise numbers, how something went last time. "
            "Read-only; results are excerpts, not guarantees of absence."
        ),
        input_schema=RECALL_SCHEMA,
    )


def _fts_query(text: str) -> str:
    """OR-joined words for websearch_to_tsquery (same rationale as §8)."""
    return " OR ".join(re.findall(r"\w+", text))


def _parse_when(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    # Models omit offsets constantly; naive means UTC here (same rule as §9.2).
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _clip(lines: list[str]) -> str:
    kept: list[str] = []
    for line in lines:
        if estimate_tokens("\n".join(kept + [line])) > RESULT_TOKEN_BUDGET:
            kept.append("[more results omitted — narrow the query]")
            break
        kept.append(line)
    return "\n".join(kept)


async def run_recall(db: Database, user_id: str, args: dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    since = _parse_when(args.get("since"))
    until = _parse_when(args.get("until"))
    kinds = args.get("kinds") or None

    async with db.with_tenant(user_id) as conn:
        if query:
            events = await events_q.fts(conn, _fts_query(query), RESULT_LIMIT)
            said = await transcript_q.fts(conn, _fts_query(query), RESULT_LIMIT)
        elif since or until:
            events = await events_q.between(
                conn,
                start=since or datetime.min.replace(tzinfo=timezone.utc),
                end=until or datetime.max.replace(tzinfo=timezone.utc),
                limit=RESULT_LIMIT,
            )
            said = []
        else:
            return "recall needs a query or a date range"

    def _within(ts: datetime) -> bool:
        if since and ts < since:
            return False
        if until and ts > until:
            return False
        return True

    lines: list[str] = []
    for row in events:
        if kinds and row.get("kind") not in kinds:
            continue
        ts = row["occurred_at"]
        if not _within(ts):
            continue
        bits = [row["kind"]]
        if row.get("habit_id"):
            bits.append(row["habit_id"])
        if row.get("value_num") is not None:
            bits.append(str(row["value_num"]))
        note = f": {row['note']}" if row.get("note") else ""
        lines.append(f"- [{ts.date()}] {' '.join(bits)}{note}")
    for row in said:
        if kinds:
            continue  # kinds filters to events only
        if not _within(row["ts"]):
            continue
        lines.append(f"- [{row['ts'].date()}] {row['role']} said: {row['text']}")

    if not lines:
        return "nothing recalled for that query"
    return _clip(lines)


def make_recall_handler(db: Database, user_id: str) -> ToolHandler:
    """Per-turn handler with the call budget; errors degrade to honest text."""
    calls = 0

    async def handler(args: dict[str, Any]) -> str:
        nonlocal calls
        calls += 1
        if calls > MAX_RECALLS_PER_TURN:
            return (
                f"recall budget exhausted ({MAX_RECALLS_PER_TURN} per turn); "
                "work with what you have"
            )
        try:
            return await run_recall(db, user_id, args)
        except Exception:
            log.exception("recall failed for user %s (args=%s)", user_id, args)
            return "recall failed; work with what you have"

    return handler
