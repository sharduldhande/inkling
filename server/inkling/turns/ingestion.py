"""Document ingestion turns (§14): special prompt and context.

Like consolidation (§13.1), ingestion is a *turn*, not a separate engine:
POST /documents extracts text, stores an ingested_documents row, and enqueues
a trigger; the ordinary worker claims it and this module supplies the prompt
and a document-scoped context in place of §8's chat assembly.

The flow is two-phase by design: the ingestion turn PROPOSES (reply text
only — validate.py rejects its state commands), and the user's next chat
turn confirms. Until confirmed_at is set, §8's context carries a pending-
ingestion section telling the model how to emit the batch on a "yes".
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from inkling.db.client import Database
from inkling.db.queries import commitments as commitments_q
from inkling.db.queries import documents as documents_q
from inkling.db.queries import users as users_q
from inkling.turns.context import AssembledContext
from inkling.turns.validate import estimate_tokens

log = logging.getLogger(__name__)

COMMITMENTS_HORIZON = timedelta(days=60)
# The document is the point of this turn; give it most of the window.
DOC_TOKEN_BUDGET = 12_000

INGESTION_PROMPT = """\
You are Inkling, ingesting a document the user just uploaded (a syllabus, a \
study plan, a schedule page). This turn is a PROPOSAL only.

Do, in order:
1. Identify what the document is, in one line.
2. Extract its structure and EVERY dated obligation — assignments, exams, \
sessions, deadlines — with their dates and, where given, times.
3. If the material is self-paced (no inbuilt dates), do NOT invent a \
schedule: ask the user for a target finish date and stop there.
4. Reply with the proposed schedule, compact and scannable: one line per \
dated obligation (date — title), plus a pacing suggestion if the material \
needs day-by-day work. End by asking the user to confirm.

Do NOT emit state-changing commands this turn — they will be rejected. The \
user confirms in their next message; you will emit the full batch then \
(your context will carry the pending proposal). You MAY emit one Notify \
with a one-sentence push telling the user the extraction is ready, since \
this turn runs after they left the upload screen.

Check the existing commitments in context: if some obligations are already \
tracked, say so instead of proposing duplicates.

Keep the reply plain text — no markdown tables or headers; simple dash \
lists only.
"""


def is_ingestion_trigger(trigger: dict[str, Any]) -> bool:
    return trigger["source"] == "ingestion"


async def assemble_ingestion_context(
    db: Database, *, user_id: str, trigger: dict[str, Any]
) -> AssembledContext:
    """One snapshot transaction (§5.3): the document plus what's already tracked."""
    payload = trigger["payload"] or {}
    document_id = int(payload["document_id"])
    now = datetime.now(timezone.utc)

    async with db.with_tenant(user_id) as conn:
        user = await users_q.get(conn, user_id)
        assert user is not None
        doc = await documents_q.get(conn, document_id)
        upcoming = await commitments_q.upcoming(
            conn, horizon=now + COMMITMENTS_HORIZON, limit=40
        )

    try:
        tz = ZoneInfo(user["timezone"])
    except Exception:
        tz = ZoneInfo("UTC")
    local_now = now.astimezone(tz).strftime("%Y-%m-%d %H:%M (%A)")

    sections: list[tuple[str, str, str]] = []
    sections.append(("now", "## Current local time", local_now))

    lines = [
        f"- [commitment {r['id']} v{r['version']}] {r['title']}"
        + (
            f" — due {r['due_at'].astimezone(tz).strftime('%Y-%m-%d %H:%M')}"
            if r["due_at"] else ""
        )
        + (f" — recurs {r['recurrence']}" if r["recurrence"] else "")
        for r in upcoming
    ]
    sections.append((
        "commitments", "## Already-tracked commitments (next 60 days)",
        "\n".join(lines) or "(none)",
    ))

    if doc is None:
        # Deleted between enqueue and claim — degrade to an honest reply.
        body = f"(document {document_id} no longer exists; tell the user)"
        title = payload.get("title", f"document {document_id}")
    else:
        extracted = doc["extracted"] or {}
        text = (extracted.get("text") or "").strip() or "(no text extracted)"
        if estimate_tokens(text) > DOC_TOKEN_BUDGET:
            text = text[: DOC_TOKEN_BUDGET * 4] + "\n[document clipped to budget]"
        title = doc["title"]
        body = text
    sections.append((
        "document",
        f"## The document: '{title}' (ingested_documents id {document_id})",
        body,
    ))

    sections.append((
        "trigger", "## Task",
        payload.get("text")
        or "Identify this document, extract its dated obligations, and "
           "propose a schedule for the user to confirm.",
    ))

    context = "\n\n".join(f"{header}\n{body}" for _, header, body in sections)
    sizes = {name: estimate_tokens(body) for name, _, body in sections}
    log.info(
        "assembled ingestion context for user %s doc %s: %d tokens, %s",
        user_id, document_id, sum(sizes.values()), sizes,
    )
    return AssembledContext(system=INGESTION_PROMPT, context=context, sizes=sizes)
