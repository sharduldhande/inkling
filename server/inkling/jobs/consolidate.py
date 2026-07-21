"""Nightly consolidation (§13.1): the job's prompt and context.

Consolidation is a *turn*, not a separate engine: the scheduler fires a
priority-4 job trigger at 02:30 local (or §8 enqueues one when working_state
blows its cap), the ordinary worker claims it, and this module supplies the
special system prompt and a day-scoped context in place of §8's chat assembly.
Output flows through the same parse→validate→apply pipeline; the job-only
commands (UpdateDoc, UpsertMemoryFact) are unlocked by commit_turn passing
consolidation=True to validate (see is_consolidation_trigger).

Input per spec: day's transcript + events + current docs + open occurrences.
The doc-diffs-into-nightly-export step belongs to §13.2 (backup/export).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from inkling.db.client import Database
from inkling.db.queries import commitments as commitments_q
from inkling.db.queries import docs as docs_q
from inkling.db.queries import events as events_q
from inkling.db.queries import facts as facts_q
from inkling.db.queries import transcript as transcript_q
from inkling.db.queries import users as users_q
from inkling.turns.context import AssembledContext
from inkling.turns.validate import (
    DOC_TOKEN_CAPS,
    WORKING_STATE_TOKEN_CAP,
    estimate_tokens,
)

log = logging.getLogger(__name__)

DAY = timedelta(hours=24)
TRANSCRIPT_LIMIT = 200
EVENTS_LIMIT = 200
FACTS_LIMIT = 50
OCCURRENCE_HORIZON = timedelta(days=7)
# Inputs may legitimately be oversized (an over-cap working_state is *why* the
# job runs), so input clipping is generous — twice the biggest output cap.
INPUT_DOC_CLIP = WORKING_STATE_TOKEN_CAP * 2

CONSOLIDATION_PROMPT = f"""\
You are Inkling's nightly consolidation pass. This is maintenance, not chat: \
no user is watching, and your reply text should be a one-line summary of what \
you changed. All real output happens through the emit_commands tool.

Your context carries only the last 24 hours; your memory is deeper. Use the \
recall tool to look further back whenever a doc you're writing spans more \
than today: pull the week's events before rewriting digest7, check whether a \
"pattern" you're about to assert actually recurs across weeks (query by \
habit or keyword; kinds=['measurement'] for food/exercise numbers), and \
verify a loop is truly stale before flagging it. Never assert a trend you \
did not recall evidence for.

Your jobs, in order of importance:
1. Audit the working_state doc against today's transcript and events. Close \
loops that finished, flag loops stale for more than 7 days (keep their \
"(as of YYYY-MM-DD)" stamps honest), archive what no longer matters as \
AppendEvent(kind: note) rows, and rewrite the doc under its \
{WORKING_STATE_TOKEN_CAP}-token cap via UpdateWorkingState with a reason \
starting with "consolidation".
2. Update the 7-day digest via UpdateDoc(kind: digest7, \
max {DOC_TOKEN_CAPS['digest7']} tokens): what actually happened this week, \
compressed.
3. Maintain the patterns doc via UpdateDoc(kind: patterns, \
max {DOC_TOKEN_CAPS['patterns']} tokens) when today's behavior confirms or \
contradicts it.
4. Extract durable facts as UpsertMemoryFact commands (subject, predicate, \
value, optional condition, confidence 0-1). Reconcile against the known facts \
in context: confirm, correct, or add — never duplicate.
5. If today's record contradicts itself or a doc in a way you cannot resolve, \
schedule a morning-question wakeup (ScheduleWakeup, purpose stating the exact \
question) rather than guessing.

Doc caps are HARD and measured by a crude chars/4 estimator, not your \
tokenizer: an over-cap UpdateDoc is rejected WHOLE and that night's update \
is lost (seen live: a 623-token patterns draft against the 600 cap). Draft \
each doc to roughly 15% UNDER its cap, pruning the weakest or oldest \
material to make room — these docs are distillations, not logs.
Only emit commands for what actually needs to change; an uneventful day may \
need nothing but the digest. Never invent events that are not in the record.
"""


def is_consolidation_trigger(trigger: dict[str, Any]) -> bool:
    payload = trigger["payload"] or {}
    return trigger["source"] == "job" and payload.get("job") == "consolidate"


def _fmt(ts: datetime, tz: ZoneInfo) -> str:
    return ts.astimezone(tz).strftime("%Y-%m-%d %H:%M")


@dataclass(frozen=True)
class _Section:
    name: str
    header: str
    body: str


async def assemble_consolidation_context(
    db: Database, *, user_id: str, trigger: dict[str, Any]
) -> AssembledContext:
    """One snapshot transaction (§5.3), like §8, but scoped to the day."""
    now = datetime.now(timezone.utc)
    async with db.with_tenant(user_id) as conn:
        user = await users_q.get(conn, user_id)
        assert user is not None
        try:
            tz = ZoneInfo(user["timezone"])
        except Exception:
            tz = ZoneInfo("UTC")

        docs = await docs_q.current_many(
            conn, ["goals", "patterns", "digest7", "working_state", "daily_plan"]
        )
        facts = await facts_q.active(conn, FACTS_LIMIT)
        open_commitments = await commitments_q.open_all(conn)
        occurrences = await commitments_q.pending_occurrences(
            conn, horizon=now + OCCURRENCE_HORIZON
        )
        day_events = await events_q.between(
            conn, start=now - DAY, end=now, limit=EVENTS_LIMIT
        )
        day_transcript = await transcript_q.since(
            conn, start=now - DAY, limit=TRANSCRIPT_LIMIT
        )

    sections: list[_Section] = []

    for kind, header in (
        ("goals", "## Goals (read-only: no command may change this)"),
        ("patterns", "## Patterns doc (current)"),
        ("digest7", "## 7-day digest (current)"),
        ("working_state", "## Working state (current)"),
        ("daily_plan", "## Today's plan (current)"),
    ):
        row = docs.get(kind)
        content = row["content"].strip() if row and row["content"].strip() else "(none yet)"
        if estimate_tokens(content) > INPUT_DOC_CLIP:
            content = content[: INPUT_DOC_CLIP * 4] + "\n[input clipped]"
        sections.append(_Section(kind, header, content))

    if facts:
        sections.append(_Section(
            "facts", "## Known facts (reconcile against these)",
            "\n".join(
                f"- [{f['id']}] {f['subject']} {f['predicate']} {f['value']}"
                + (f" (when {f['condition']})" if f["condition"] else "")
                + f" [confidence {f['confidence']:.1f}]"
                for f in facts
            ),
        ))

    commitment_lines = [
        f"- [commitment {c['id']} v{c['version']}] {c['title']}"
        + (f" — due {_fmt(c['due_at'], tz)}" if c["due_at"] else "")
        + (f" — recurs {c['recurrence']}" if c["recurrence"] else "")
        for c in open_commitments
    ]
    sections.append(_Section(
        "commitments", "## Open commitments",
        "\n".join(commitment_lines) or "(none)",
    ))

    if occurrences:
        sections.append(_Section(
            "occurrences", "## Pending reminder occurrences (next 7 days)",
            "\n".join(
                f"- occurrence {o['id']}: '{o['title']}' fires {_fmt(o['fire_at'], tz)}"
                for o in occurrences
            ),
        ))

    sections.append(_Section(
        "events", "## Events (last 24h)",
        "\n".join(
            f"- {_fmt(e['occurred_at'], tz)} {e['kind']}"
            + (f" {e['habit_id']}" if e["habit_id"] else "")
            + (f": {e['note']}" if e["note"] else "")
            for e in day_events
        ) or "(none)",
    ))

    sections.append(_Section(
        "transcript", "## Transcript (last 24h)",
        "\n".join(
            f"[{_fmt(r['ts'], tz)}] {r['role']}: {r['text']}"
            for r in day_transcript
        ) or "(none)",
    ))

    payload = trigger["payload"] or {}
    sections.append(_Section(
        "trigger", "## Why this run",
        payload.get("text") or "Scheduled nightly consolidation.",
    ))

    context = "\n\n".join(f"{s.header}\n{s.body}" for s in sections)
    sizes = {s.name: estimate_tokens(s.body) for s in sections}
    log.info(
        "assembled consolidation context for user %s trigger %s: %d tokens, %s",
        user_id, trigger["id"], sum(sizes.values()), sizes,
    )
    return AssembledContext(
        system=CONSOLIDATION_PROMPT, context=context, sizes=sizes
    )
