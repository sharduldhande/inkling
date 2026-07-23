"""Context assembly (§8): the snapshot of state a turn reasons over.

Section order is stable→volatile per §8.1, each with a token budget. Budget
enforcement is per-section: the cheap ~4-chars/token estimate gates every
section, and the sections the spec says to truncate first (9 transcript,
10 episodes, 8 deterministic results) shed granular items — oldest messages,
lowest-ranked episodes — rather than being cut mid-line. Sections 5
(working_state) and 7 (commitments) are never truncated *silently*: an
over-cap working_state proceeds with an explicit marker AND a maintenance
trigger is enqueued for consolidation; the commitments section instead bounds
itself with an explicit "more not shown" line.

Per §5.3 the whole read is one consistent snapshot in one tenant transaction,
closed before inference begins. The only write this module performs — the
consolidation trigger — happens after that transaction, keyed by doc version
so repeated over-cap turns don't queue duplicate maintenance work.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from inkling.db.client import Database
from inkling.db.queries import commitments as commitments_q
from inkling.db.queries import docs as docs_q
from inkling.db.queries import documents as documents_q
from inkling.db.queries import events as events_q
from inkling.db.queries import facts as facts_q
from inkling.db.queries import health as health_q
from inkling.db.queries import transcript as transcript_q
from inkling.db.queries import users as users_q
from inkling.db.queries import wakeups as wakeups_q
from inkling.prompts.system import SYSTEM_PROMPT
from inkling.turns import ingress

# estimate_tokens lives in validate.py because it also gates doc writes (§9.2);
# both callers must agree on the estimate or the cap means two different things.
from inkling.turns.validate import WORKING_STATE_TOKEN_CAP, estimate_tokens

log = logging.getLogger(__name__)

# §8.1 budgets (tokens). The system prompt is deliberately unbudgeted — it is
# a stable, prompt-cached prefix reviewed for sharpness, not size (see
# prompts/system.py); its estimated size rides the assembly log line below.
BUDGETS = {
    "goals": 400,
    "patterns": 600,
    "digest7": 500,
    "working_state": WORKING_STATE_TOKEN_CAP,  # 1200 hard cap, marker on excess
    "daily_plan": 400,
    "commitments": 400,  # never silently truncated; bounded by row limit below
    "wakeups": 250,
    "pending_ingestion": 300,
    "today": 300,
    "transcript": 3500,
    "episodes": 400,
    "trigger": 300,
}

# The tail is deliberately deep (user decision 2026-07-22): it is the model's
# conversational voice-continuity — its own recent phrasing, verbatim — and the
# stable section ordering keeps the prompt-cache hit despite the size.
TRANSCRIPT_TAIL = 40
# Silences shorter than this read as one sitting; longer ones get an explicit
# "time passed" marker so the model never has to subtract timestamps.
TAIL_GAP_MARKER = timedelta(minutes=30)
COMMITMENTS_LIMIT = 20
COMMITMENTS_HORIZON = timedelta(hours=48)
FACTS_LIMIT = 12
EPISODES_LIMIT = 6
TODAY_EVENTS_LIMIT = 40

# §8.1 section 10 gate: retrieval runs only if the trigger references the past
# — or names a domain whose data deliberately never rides in context
# (food/exercise measurements live in memory, surfaced on demand; see the
# "today so far" aggregation below and the recall tool). A heuristic, tuned
# for recall over precision — a spurious retrieval costs a few hundred
# budgeted tokens, a missed one loses the memory the user asked for. The
# model-directed `recall` tool (turns/recall.py) is the ceiling above this
# floor: it covers every case no regex anticipates.
_PAST_RE = re.compile(
    r"\b(remember|recall|last (?:time|week|month|year|night)|yesterday"
    r"|earlier|previously|before|ago|used to|did i|have i|when did"
    r"|what did|what was|history"
    r"|calories?|macros?|protein|ate|eat(?:en|s|ing)?|food|meals?"
    r"|workouts?|exercised?|training|trained|weight|steps)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AssembledContext:
    system: str
    context: str
    sizes: dict[str, int]  # estimated tokens per included section


def references_past(text: str) -> bool:
    return bool(_PAST_RE.search(text))


def _fts_query(text: str) -> str:
    """OR-join the trigger's words for websearch_to_tsquery.

    Retrieval, not filtering: any overlapping content word should surface a
    candidate, and ts_rank orders by how many overlap. Stopwords vanish inside
    to_tsvector, so ORing them in costs nothing.
    """
    return " OR ".join(re.findall(r"\w+", text))


def _tz(user: dict[str, Any]) -> ZoneInfo:
    try:
        return ZoneInfo(user["timezone"])
    except Exception:
        log.warning("user %s has invalid timezone %r; using UTC",
                    user["id"], user["timezone"])
        return ZoneInfo("UTC")


def _local(ts: datetime, tz: ZoneInfo) -> str:
    return ts.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def _clip(text: str, budget: int, marker: str) -> str:
    """Char-level cut for prose sections; the marker states what happened."""
    if estimate_tokens(text) <= budget:
        return text
    return text[: budget * 4].rstrip() + f"\n{marker}"


def _clip_lines(lines: list[str], budget: int, marker: str, *, drop_head: bool) -> str:
    """Item-level cut: shed whole lines (oldest first when drop_head) to budget."""
    kept = list(lines)
    dropped = False
    while kept and estimate_tokens("\n".join(kept)) > budget:
        kept.pop(0 if drop_head else -1)
        dropped = True
    if dropped:
        kept.insert(0 if drop_head else len(kept), marker)
    return "\n".join(kept)


def _commitment_line(row: dict[str, Any], tz: ZoneInfo, now: datetime) -> str:
    ref = f"[commitment {row['id']} v{row['version']}] {row['title']}"
    parts = []
    if row["due_at"] is not None:
        overdue = row["due_at"] <= now
        parts.append(
            f"{'OVERDUE — was ' if overdue else ''}due {_local(row['due_at'], tz)}"
        )
    if row["recurrence"]:
        parts.append(f"recurs {row['recurrence']}")
    if row["next_fire_at"] is not None:
        parts.append(f"next reminder {_local(row['next_fire_at'], tz)}")
    if not parts:
        parts.append("no date")
    return f"- {ref} — {', '.join(parts)}"


def _event_line(row: dict[str, Any], tz: ZoneInfo) -> str:
    bits = [row["kind"]]
    if row["habit_id"]:
        bits.append(row["habit_id"])
    if row["value_num"] is not None:
        bits.append(f"{row['value_num']}{' ' + row['unit'] if row['unit'] else ''}")
    line = f"- {row['occurred_at'].astimezone(tz).strftime('%H:%M')} {' '.join(bits)}"
    return f"{line}: {row['note']}" if row["note"] else line


_TAIL_ROLES = {"user": "User", "assistant": "You"}


def _tail_day_marker(ts: datetime) -> str:
    return f"— {ts.strftime('%A %Y-%m-%d, %H:%M')} —"


def _tail_gap_marker(gap: timedelta, ts: datetime) -> str:
    minutes = round(gap.total_seconds() / 60)
    dur = f"{minutes}m" if minutes < 120 else f"{round(minutes / 60)}h"
    return f"— {dur} pass, now {ts.strftime('%H:%M')} —"


def _render_tail(
    rows: list[dict[str, Any]], tz: ZoneInfo, local_now: datetime
) -> list[str]:
    """The transcript tail as dialogue the model is mid-way through, not a log.

    Prior assistant turns render as "You:" — the model inherits voice from
    verbatim first-person history far more than from a timestamped record.
    Time survives where it carries signal: an opening anchor, a marker per
    ≥TAIL_GAP_MARKER silence, a dated marker per local-day boundary.
    """
    lines: list[str] = []
    prev: datetime | None = None
    for row in rows:
        ts = row["ts"].astimezone(tz)
        if prev is None:
            if ts.date() != local_now.date():
                lines.append(_tail_day_marker(ts))
        elif ts.date() != prev.date():
            lines.append(_tail_day_marker(ts))
        elif ts - prev >= TAIL_GAP_MARKER:
            lines.append(_tail_gap_marker(ts - prev, ts))
        role = _TAIL_ROLES.get(row["role"], row["role"])
        # Opening anchor — except when a day marker above already dated it.
        anchor = (
            f" ({ts.strftime('%H:%M')})"
            if prev is None and ts.date() == local_now.date()
            else ""
        )
        lines.append(f"{role}{anchor}: {row['text']}")
        prev = ts
    return lines


def format_trigger(trigger: dict[str, Any]) -> str:
    """The §8.1 section-11 body — same shapes the pre-§8 worker streamed."""
    payload = trigger["payload"] or {}
    text = payload.get("text", "")
    if trigger["source"] == "chat":
        return f"User message: {text}"
    ref = f" {trigger['source_ref']}" if trigger["source_ref"] else ""
    late = " (late — fired past its time)" if payload.get("late") else ""
    return f"[{trigger['source']} trigger{ref}]{late} {text}"


async def assemble_context(
    db: Database, *, user_id: str, trigger: dict[str, Any]
) -> AssembledContext:
    """Read one consistent snapshot and render the §8.1 context string."""
    trigger_text = (trigger["payload"] or {}).get("text", "")
    retrieve = bool(trigger_text) and references_past(trigger_text)
    now = datetime.now(timezone.utc)

    async with db.with_tenant(user_id) as conn:
        user = await users_q.get(conn, user_id)
        assert user is not None  # the caller holds a turn for this user
        tz = _tz(user)
        local_now = now.astimezone(tz)
        day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)

        docs = await docs_q.current_many(
            conn, ["goals", "patterns", "digest7", "working_state", "daily_plan"]
        )
        facts = await facts_q.active(conn, FACTS_LIMIT)
        upcoming = await commitments_q.upcoming(
            conn, horizon=now + COMMITMENTS_HORIZON, limit=COMMITMENTS_LIMIT
        )
        today = await events_q.between(
            conn, start=day_start, end=day_start + timedelta(days=1),
            limit=TODAY_EVENTS_LIMIT,
        )
        health_today = await health_q.today_summary(
            conn, user_id=user_id, start=day_start
        )
        targets = user.get("targets") or {}
        streaks = await events_q.streaks(
            conn, timezone=user["timezone"], week_start=now - timedelta(days=7)
        )
        active_wakeups = await wakeups_q.active(conn)
        pending_docs = await documents_q.unconfirmed(conn)
        tail = await transcript_q.tail(conn, TRANSCRIPT_TAIL)
        episodes: list[tuple[str, dict[str, Any]]] = []
        if retrieve:
            query = _fts_query(trigger_text)
            episodes = [
                ("event", r) for r in await events_q.fts(conn, query, EPISODES_LIMIT)
            ] + [
                ("said", r) for r in await transcript_q.fts(conn, query, EPISODES_LIMIT)
            ]
            episodes.sort(key=lambda pair: pair[1]["rank"], reverse=True)

    sections: list[tuple[str, str, str]] = []  # (name, header, body)

    def doc_section(name: str, kind: str, header: str) -> None:
        row = docs.get(kind)
        if row and row["content"].strip():
            body = _clip(row["content"].strip(), BUDGETS[name],
                         f"[{kind} truncated to its context budget]")
            sections.append((name, header, body))

    doc_section("goals", "goals", "## Goals")

    patterns_parts = []
    patterns_row = docs.get("patterns")
    if patterns_row and patterns_row["content"].strip():
        patterns_parts.append(patterns_row["content"].strip())
    if facts:
        patterns_parts.append("Known facts:\n" + "\n".join(
            f"- {f['subject']} {f['predicate']} {f['value']}"
            + (f" (when {f['condition']})" if f["condition"] else "")
            + f" [confidence {f['confidence']:.1f}]"
            for f in facts
        ))
    if patterns_parts:
        sections.append((
            "patterns", "## Patterns & facts",
            _clip("\n\n".join(patterns_parts), BUDGETS["patterns"],
                  "[patterns truncated to its context budget]"),
        ))

    doc_section("digest7", "digest7", "## Last 7 days (digest)")

    # §8.1 section 5: never truncated silently. Over-cap content is cut with an
    # explicit marker and consolidation is queued below, after the snapshot.
    ws = docs.get("working_state")
    ws_over_cap = False
    if ws and ws["content"].strip():
        body = ws["content"].strip()
        if estimate_tokens(body) > BUDGETS["working_state"]:
            ws_over_cap = True
            body = (
                body[: BUDGETS["working_state"] * 4].rstrip()
                + "\n[working_state exceeds its cap and was truncated here; "
                "a consolidation pass has been queued]"
            )
        sections.append(("working_state", "## Working state", body))
    else:
        sections.append(("working_state", "## Working state", "(none yet)"))

    doc_section("daily_plan", "daily_plan", "## Today's plan")

    # §8.1 section 7: ALWAYS present, bounded explicitly rather than clipped.
    if upcoming:
        lines = [_commitment_line(r, tz, now) for r in upcoming]
        if len(upcoming) == COMMITMENTS_LIMIT:
            lines.append(f"(only the first {COMMITMENTS_LIMIT} shown)")
        commitments_body = "\n".join(lines)
    else:
        commitments_body = "(none)"
    sections.append(("commitments", "## Commitments (due / upcoming)", commitments_body))

    # Pending wakeups: without this the model re-schedules follow-ups blind —
    # seen live: 4 duplicate "check the block landed" wakeups in one evening,
    # firing as a burst of near-identical pushes. Visibility + the prompt's
    # one-follow-up-per-thread rule is the fix.
    if active_wakeups:
        lines = [
            f"- [wakeup {w['id']}] "
            + (f"fires {_local(w['fire_at'], tz)}" if w["fire_at"]
               else f"recurs {w['recurrence']}")
            + f" — {w['purpose']}"
            for w in active_wakeups
        ]
        sections.append((
            "wakeups", "## Your scheduled wakeups (reconcile — don't duplicate)",
            _clip_lines(lines, BUDGETS["wakeups"],
                        "[more not shown]", drop_head=False),
        ))

    # §14: an uploaded document whose proposed schedule the user hasn't
    # confirmed yet. The section carries the how-to so the system prompt
    # doesn't have to spend its budget on a flow that's usually absent.
    if pending_docs:
        lines = [
            f"- '{d['title']}' (document_id {d['id']}, uploaded "
            f"{_local(d['created_at'], tz)})"
            for d in pending_docs
        ]
        lines.append(
            "A schedule was proposed for these in conversation. If the user "
            "confirms, emit the full batch NOW in one turn: UpsertCommitment "
            "per dated obligation (lead_time where time-of-day matters), "
            "day-keyed pacing notes if proposed (AppendEvent kind='note', "
            "occurred_at that day 06:00 local, under 240 chars each), and one "
            "AppendEvent kind='ingestion' with the document_id — that marker "
            "records the confirmation. If they ask for changes, adjust and "
            "re-propose; if they decline, no marker."
        )
        sections.append((
            "pending_ingestion", "## Pending ingestion (awaiting confirmation)",
            _clip_lines(lines, BUDGETS["pending_ingestion"],
                        "[trimmed to budget]", drop_head=False),
        ))

    # Measurements (food, weight, exercise numbers) are memory, not ambient
    # context: they'd crowd every turn with breakfasts. One aggregate line
    # keeps the model aware they exist; recall/the gate fetch detail on demand.
    measurements = [r for r in today if r["kind"] == "measurement"]
    today_lines = [_event_line(r, tz) for r in today if r["kind"] != "measurement"]
    if measurements:
        today_lines.append(
            f"- measurements: {len(measurements)} logged today (use recall for detail)"
        )
    # Health lives in its own typed tables; one aggregate line keeps the model
    # aware without crowding every turn with breakfasts (detail via recall).
    if health_today["meals"] or health_today["exercise_min"]:
        bits = []
        if health_today["meals"]:
            bits.append(
                f"{health_today['meals']} meals, "
                f"{round(health_today['kcal'])} kcal, "
                f"{round(health_today['protein_g'])} g protein"
            )
            if targets.get("kcal_quota"):
                bits.append(f"kcal quota {targets['kcal_quota']}")
            if targets.get("protein_quota_g"):
                bits.append(f"protein quota {targets['protein_quota_g']} g")
        if health_today["exercise_min"]:
            bits.append(f"{round(health_today['exercise_min'])} min exercise")
        today_lines.append(f"- food/exercise: {'; '.join(bits)} (recall for detail)")
    streak_lines = [
        f"- {s['id']} ({s['title']}): "
        + (f"last done {_local(s['last_done'], tz)}" if s["last_done"] else "never done")
        + f", {s['days_last7']} of last 7 days"
        for s in streaks
    ]
    if today_lines or streak_lines:
        parts = []
        if today_lines:
            parts.append("Events today:\n" + "\n".join(today_lines))
        if streak_lines:
            parts.append("Habit streaks:\n" + "\n".join(streak_lines))
        sections.append((
            "today", "## Today so far",
            _clip_lines("\n".join(parts).splitlines(), BUDGETS["today"],
                        "[trimmed to budget]", drop_head=False),
        ))

    if tail:
        # Clip by dropping whole ROWS (oldest first) and re-rendering, not by
        # cutting rendered lines: the surviving head must re-grow its opening
        # time anchor, and a marker must never outlive the message it dated.
        rows = list(tail)
        lines = _render_tail(rows, tz, local_now)
        dropped = False
        while len(rows) > 1 and estimate_tokens("\n".join(lines)) > BUDGETS["transcript"]:
            rows.pop(0)
            dropped = True
            lines = _render_tail(rows, tz, local_now)
        if dropped:
            lines.insert(0, "[earlier messages omitted]")
        newest = tail[-1]["ts"].astimezone(tz)
        header = (
            "## Recent conversation (today)"
            if newest.date() == local_now.date()
            else f"## Recent conversation (ending {newest.date().isoformat()})"
        )
        sections.append(("transcript", header, "\n".join(lines)))

    if episodes:
        lines = []
        for source, row in episodes:
            when = _local(row.get("occurred_at") or row["ts"], tz)
            what = row.get("note") or row.get("text") or ""
            role = row.get("role") or row.get("kind") or source
            lines.append(f"- [{when}] {role}: {what}")
        sections.append((
            "episodes", "## Possibly relevant past",
            _clip_lines(lines, BUDGETS["episodes"],
                        "[lower-ranked matches omitted]", drop_head=False),
        ))

    # First contact of the day: no conversation since the MOST RECENT
    # quiet_end — yesterday's when now is past midnight but before today's
    # (the day does not flip at midnight; seen live: three "Morning" pushes
    # at 00:14). Judged from the transcript, not turns, so a presence turn
    # that chose silence doesn't count as having opened the day. The flag
    # tells the model this turn establishes the day's shape (see prompt).
    quiet_end = user["quiet_end"]
    day_open = local_now.replace(
        hour=quiet_end.hour, minute=quiet_end.minute,
        second=quiet_end.second, microsecond=0,
    )
    if day_open > local_now:
        day_open -= timedelta(days=1)
    first_contact = not tail or tail[-1]["ts"].astimezone(tz) < day_open

    trigger_body = _clip(format_trigger(trigger), BUDGETS["trigger"],
                         "[trigger text truncated to budget]")
    if first_contact:
        trigger_body += (
            "\n[first_contact_of_day: no conversation yet since this "
            "morning's quiet hours ended — open the day]"
        )
    sections.append(("trigger", "## Trigger", trigger_body))

    context = "\n\n".join(f"{header}\n{body}" for _, header, body in sections)
    sizes = {name: estimate_tokens(body) for name, _, body in sections}
    log.info(
        "assembled context for user %s trigger %s: %d tokens total "
        "(+ ~%d system prompt), sections %s",
        user_id, trigger["id"], sum(sizes.values()),
        estimate_tokens(SYSTEM_PROMPT), sizes,
    )

    if ws_over_cap:
        assert ws is not None
        await ingress.submit(
            db,
            user_id=user_id,
            source="job",
            payload={
                "job": "consolidate",
                "text": "working_state exceeds its 1200-token cap; "
                "consolidate by closing or archiving loops",
            },
            source_ref=f"working_state:v{ws['version']}",
            # One consolidation request per over-cap doc version: retriggers
            # from every turn until the doc shrinks would be pure noise.
            idempotency_key=f"consolidate-ws:v{ws['version']}",
        )

    return AssembledContext(system=SYSTEM_PROMPT, context=context, sizes=sizes)
