"""Domain validation for model commands (§9.2) — code, not prompt.

§9.1 already proved a command is *well-formed*. This layer decides whether it
is *legal*: does the tenant own what it references, is the state transition
allowed, does the optimistic-concurrency version still match, does closing this
commitment orphan unresolved children.

Two rules govern everything here:

1. A rejection is never a turn failure. Rejected commands are logged and the
   caller appends a short note to the assistant text, so the model's claim and
   reality can't silently diverge (D4). A turn with three good commands and one
   illegal one applies three.
2. Validation is advisory about tenancy, never the enforcement. RLS is the
   backstop; these checks exist to produce a clean reason string instead of a
   permission error. Every lookup below runs inside the tenant transaction, so
   another user's id is simply invisible and reads as "not found".

Validation runs inside the same transaction as application (§5.4) — a check
against a row that a concurrent turn could change between check and write would
be worth nothing otherwise.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from difflib import SequenceMatcher
from typing import Any

from psycopg import AsyncConnection

from inkling.db.queries import calendar as calendar_q
from inkling.db.queries import commitments as commitments_q
from inkling.db.queries import docs as docs_q
from inkling.db.queries import documents as documents_q
from inkling.db.queries import events as events_q
from inkling.db.queries import health as health_q
from inkling.db.queries import wakeups as wakeups_q
from inkling.turns.commands import (
    AppendEvent,
    CancelWakeup,
    Command,
    CompleteCommitment,
    CreateCalendarEvent,
    LogMeal,
    LogWeight,
    LogWorkout,
    Notify,
    ScheduleWakeup,
    SetTargets,
    SnoozeCommitment,
    UpdateDoc,
    UpdateWorkingState,
    UpsertCommitment,
    UpsertMemoryFact,
)

log = logging.getLogger(__name__)

# §8.1: working_state is a 1200-token hard cap; patterns and digest7 are
# capped at their context budgets so an accepted doc always fits its section.
WORKING_STATE_TOKEN_CAP = 1200
DOC_TOKEN_CAPS = {"patterns": 600, "digest7": 500, "daily_plan": 400}

# UpdateDoc kinds any turn may write. daily_plan is the day's shape — the
# first-contact and presence turns own it; the rest stay consolidation-only.
TURN_WRITABLE_DOC_KINDS = frozenset({"daily_plan"})

# §9.2: a rewrite that replaces more than this much of the doc is a red flag —
# the model is discarding state rather than editing it. Consolidation is the
# one job allowed to do that, and it must say so.
MAX_REPLACEMENT_FRACTION = 0.8

# §9.2: an event may be backdated freely (we often learn late) but must not be
# dated into the future — that's a prediction, not an observation.
FUTURE_EVENT_TOLERANCE = timedelta(minutes=5)

# §9.2: calendar events longer than this are a parse error, not a plan.
MAX_CALENDAR_DURATION = timedelta(hours=24)

# §9.2 health bounds: generous enough for real life, tight enough that a
# hallucinated number (a 65,000 kcal meal, a 40-hour run) is rejected with a
# reason instead of poisoning the quota math the app displays.
MEAL_KCAL_MAX = 5000
MACRO_G_MAX = 500
WORKOUT_MIN_RANGE = (1, 600)
WORKOUT_KCAL_MAX = 3000
WORKOUT_KM_MAX = 200
WEIGHT_KG_RANGE = (20, 400)
LB_PER_KG = 2.20462
TARGET_KCAL_RANGE = (800, 6000)
TARGET_PROTEIN_RANGE = (20, 400)

# §14 flood caps: a runaway extraction must not be able to bury the user. Per
# turn — no legitimate conversation creates this many; an ingestion confirm
# for a dense syllabus stays well inside them. Pacing notes are additionally
# size-capped so day-keyed rows fit the today-context budget.
MAX_COMMITMENTS_PER_TURN = 20
MAX_EVENTS_PER_TURN = 120
INGESTION_NOTE_TOKEN_CAP = 60
MAX_PACING_HORIZON = timedelta(days=180)

# Markers that say a working-state line is closed. Deliberately generous: a
# false "closed" only costs us a guard, a false "open" blocks a legitimate
# completion and annoys the user (§9.2 heuristic, not a parser).
_CLOSED_MARKERS = ("[x]", "[X]", "~~", "(done", "done)", "✓", "✔")
_BULLETS = ("-", "*", "+", "•")


@dataclass(frozen=True)
class Rejection:
    """A command that will not be applied, and the reason the model gets told."""

    command: Command
    reason: str

    def note(self) -> str:
        """The §9.2 note appended to the assistant text."""
        return f"(couldn't complete {self.command.type}: {self.reason})"


@dataclass(frozen=True)
class Validated:
    accepted: list[Command]
    rejected: list[Rejection]

    def notes(self) -> str:
        return "\n".join(r.note() for r in self.rejected)


def estimate_tokens(text: str) -> int:
    """Cheap tokenizer (§8.1): ~4 chars per token, no model dependency.

    Deliberately not tiktoken — this gates a doc write, and being within ~10%
    is worth more than a tokenizer dependency that must match whichever model
    the user's profile routes to today.
    """
    return (len(text) + 3) // 4


def _aware(value: datetime) -> datetime:
    """Treat a naive datetime as UTC — models omit the offset constantly.

    Backstop only: validate_commands localizes naive datetimes to the user's
    timezone up front, so by the time checks run, values should be aware.
    """
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _localize_naive_datetimes(commands: list[Command], user: dict[str, Any]) -> None:
    """Give every naive datetime field the user's timezone, in place.

    The model's context clock is user-local, so its bare timestamps are
    wall-clock times in that zone. One pass here means validators, apply,
    and storage all see the intended instant.
    """
    try:
        tz = ZoneInfo(user.get("timezone") or "UTC")
    except Exception:
        tz = timezone.utc
    for cmd in commands:
        for name in type(cmd).model_fields:
            value = getattr(cmd, name, None)
            if isinstance(value, datetime) and value.tzinfo is None:
                setattr(cmd, name, value.replace(tzinfo=tz))


# A proactive turn whose working_state RECORDS a push must actually carry
# the Notify: turn 138 (live) wrote "sent ONE plain push" with no Notify in
# the batch, and every later turn trusted the note and stayed silent.
_PUSH_CLAIM = re.compile(
    r"(sent|sending|delivered|pushed)\s+(?:[\w'-]+\s+){0,4}?"
    r"(push|ping|nudge|notification)|pinged",
    re.IGNORECASE,
)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _is_open_line(line: str) -> bool:
    """A list item with no closure marker is an open loop (§8.2)."""
    stripped = line.strip()
    if not stripped:
        return False
    is_item = stripped[0] in _BULLETS or (
        stripped[0].isdigit() and "." in stripped[:3]
    )
    if not is_item:
        return False
    return not any(marker in stripped for marker in _CLOSED_MARKERS)


def _has_unresolved_children(working_state: str, needles: list[str]) -> bool:
    """§9.2 closure guard.

    §8.2 makes indentation express containment, so the check is structural: find
    the line naming this commitment, then look at the block indented under it.
    Any open child means completing the parent would strand it.
    """
    lines = working_state.splitlines()
    for i, line in enumerate(lines):
        haystack = line.lower()
        if not any(n for n in needles if n and n.lower() in haystack):
            continue
        parent_indent = _indent(line)
        for child in lines[i + 1 :]:
            if not child.strip():
                continue
            if _indent(child) <= parent_indent:
                break  # dedent ends this parent's block
            if _is_open_line(child):
                return True
        return False  # matched, and every child was closed
    return False  # not mentioned in working_state at all


async def _validate_append_event(
    conn: AsyncConnection, cmd: AppendEvent
) -> str | None:
    if cmd.occurred_at is not None:
        drift = _aware(cmd.occurred_at) - datetime.now(timezone.utc)
        if drift > FUTURE_EVENT_TOLERANCE and cmd.kind != "note":
            return (
                f"occurred_at is {int(drift.total_seconds() // 60)} min in the "
                "future; events record what happened, not what will"
            )
        if drift > FUTURE_EVENT_TOLERANCE:
            # §14 pacing notes: kind='note' MAY be day-keyed into the future
            # ("plan day N/M: ...") so each day's plan surfaces in that day's
            # "today so far". Bounded in time and size so a runaway batch
            # can't crowd months of context.
            if drift > MAX_PACING_HORIZON:
                return (
                    "a future-dated note may pace at most "
                    f"{MAX_PACING_HORIZON.days} days ahead"
                )
            if (tokens := estimate_tokens(cmd.note)) > INGESTION_NOTE_TOKEN_CAP:
                return (
                    f"a future-dated pacing note must fit the day's context: "
                    f"~{tokens} tokens, cap {INGESTION_NOTE_TOKEN_CAP}"
                )
    if cmd.habit_id is not None and await events_q.habit(conn, cmd.habit_id) is None:
        return f"unknown habit '{cmd.habit_id}'"
    if cmd.document_id is not None:
        # §14 confirmation marker: kind=ingestion + document_id records the
        # user's "yes" and sets confirmed_at (apply.py).
        if cmd.kind != "ingestion":
            return "document_id only belongs on kind='ingestion' events"
        doc = await documents_q.get(conn, cmd.document_id)
        if doc is None:
            return f"document {cmd.document_id} not found"  # RLS: or not yours
        if doc["confirmed_at"] is not None:
            return f"document {cmd.document_id} was already confirmed"
    if cmd.commitment_id is not None:
        if cmd.kind not in ("habit_done", "habit_skipped"):
            return (
                "commitment_id only belongs on habit_done/habit_skipped events"
            )
        row = await commitments_q.get(conn, cmd.commitment_id)
        if row is None:
            return f"commitment {cmd.commitment_id} not found"  # RLS: or not yours
        if row["status"] != "open" or row["recurrence"] is None:
            return (
                f"commitment {cmd.commitment_id} is not an open recurring "
                "commitment — commitment_id marks a recurring day done; "
                "one-shots close via CompleteCommitment"
            )
    return None


def _future(occurred_at: datetime | None) -> str | None:
    """Same rule as events: backdating is fine, predicting is not."""
    if occurred_at is None:
        return None
    drift = _aware(occurred_at) - datetime.now(timezone.utc)
    if drift > FUTURE_EVENT_TOLERANCE:
        return (
            f"occurred_at is {int(drift.total_seconds() // 60)} min in the "
            "future; log what happened, not what will"
        )
    return None


async def _validate_log_meal(conn: AsyncConnection, cmd: LogMeal) -> str | None:
    if not cmd.note.strip():
        return "note is required — a meal with no description can't be recalled"
    if reason := _future(cmd.occurred_at):
        return reason
    if cmd.kcal is not None and not 0 <= cmd.kcal <= MEAL_KCAL_MAX:
        return f"kcal {cmd.kcal} is outside 0–{MEAL_KCAL_MAX} for one meal"
    for name in ("protein_g", "carb_g", "fat_g"):
        grams = getattr(cmd, name)
        if grams is not None and not 0 <= grams <= MACRO_G_MAX:
            return f"{name} {grams} is outside 0–{MACRO_G_MAX} for one meal"
    if cmd.supersedes_id is not None:
        old = await health_q.intake_get(conn, cmd.supersedes_id)
        if old is None:
            return f"meal {cmd.supersedes_id} not found"  # RLS: or not yours
        if await health_q.intake_is_superseded(conn, cmd.supersedes_id):
            return (
                f"meal {cmd.supersedes_id} was already corrected; "
                "supersede the newest row instead"
            )
    return None


async def _validate_log_workout(conn: AsyncConnection, cmd: LogWorkout) -> str | None:
    if reason := _future(cmd.occurred_at):
        return reason
    lo, hi = WORKOUT_MIN_RANGE
    if not lo <= cmd.duration_min <= hi:
        return f"duration_min {cmd.duration_min} is outside {lo}–{hi}"
    if cmd.kcal_burned is not None and not 0 <= cmd.kcal_burned <= WORKOUT_KCAL_MAX:
        return f"kcal_burned {cmd.kcal_burned} is outside 0–{WORKOUT_KCAL_MAX}"
    if cmd.distance_km is not None and not 0 <= cmd.distance_km <= WORKOUT_KM_MAX:
        return f"distance_km {cmd.distance_km} is outside 0–{WORKOUT_KM_MAX}"
    return None


async def _validate_log_weight(conn: AsyncConnection, cmd: LogWeight) -> str | None:
    if reason := _future(cmd.occurred_at):
        return reason
    kg = cmd.value / LB_PER_KG if cmd.unit == "lb" else cmd.value
    lo, hi = WEIGHT_KG_RANGE
    if not lo <= kg <= hi:
        return (
            f"{cmd.value} {cmd.unit} is outside the plausible {lo}–{hi} kg "
            "range — double-check with the user"
        )
    return None


async def _validate_set_targets(
    conn: AsyncConnection, cmd: SetTargets, *, consolidation: bool
) -> str | None:
    # Quotas are the user's stated intent; the consolidation job summarizes,
    # it does not set policy.
    if consolidation:
        return "targets can only change in a conversation with the user"
    if cmd.kcal_quota is None and cmd.protein_quota_g is None:
        return "give kcal_quota or protein_quota_g (or both)"
    if cmd.kcal_quota is not None:
        lo, hi = TARGET_KCAL_RANGE
        if not lo <= cmd.kcal_quota <= hi:
            return f"kcal_quota {cmd.kcal_quota} is outside {lo}–{hi}"
    if cmd.protein_quota_g is not None:
        lo, hi = TARGET_PROTEIN_RANGE
        if not lo <= cmd.protein_quota_g <= hi:
            return f"protein_quota_g {cmd.protein_quota_g} is outside {lo}–{hi}"
    return None


async def _load_open_commitment(
    conn: AsyncConnection, commitment_id: int, expected_version: int
) -> tuple[dict[str, Any] | None, str | None]:
    row = await commitments_q.get(conn, commitment_id)
    if row is None:
        return None, f"commitment {commitment_id} not found"
    if row["status"] != "open":
        return None, f"commitment {commitment_id} is already {row['status']}"
    if row["version"] != expected_version:
        return None, (
            f"commitment {commitment_id} changed since you read it "
            f"(expected version {expected_version}, current {row['version']}); "
            "re-read it and retry"
        )
    return row, None


async def _validate_complete(
    conn: AsyncConnection, cmd: CompleteCommitment
) -> str | None:
    row, reason = await _load_open_commitment(conn, cmd.id, cmd.expected_version)
    if reason is not None:
        return reason
    assert row is not None

    doc = await docs_q.current(conn, "working_state")
    if doc and _has_unresolved_children(
        doc["content"], [row.get("source_ref"), row["title"]]
    ):
        return "children unresolved — confirm with user"
    return None


async def _validate_snooze(conn: AsyncConnection, cmd: SnoozeCommitment) -> str | None:
    _, reason = await _load_open_commitment(conn, cmd.id, cmd.expected_version)
    return reason


async def _validate_upsert(conn: AsyncConnection, cmd: Any) -> str | None:
    if cmd.due_at is not None and cmd.recurrence is not None:
        return "a commitment is one-shot (due_at) or recurring (recurrence), not both"
    if cmd.id is None:
        return None  # a create: nothing to reference, nothing to race
    if cmd.expected_version is None:
        return f"updating commitment {cmd.id} requires expected_version"
    _, reason = await _load_open_commitment(conn, cmd.id, cmd.expected_version)
    return reason


async def _validate_update_working_state(
    conn: AsyncConnection, cmd: UpdateWorkingState
) -> str | None:
    if not cmd.reason.strip():
        return "reason is required"

    tokens = estimate_tokens(cmd.new_content)
    if tokens > WORKING_STATE_TOKEN_CAP:
        return (
            f"working_state would be ~{tokens} tokens, over the "
            f"{WORKING_STATE_TOKEN_CAP} cap; close or archive loops first"
        )

    current = await docs_q.current(conn, "working_state")
    if current is None or not current["content"].strip():
        return None  # first write: nothing to preserve, nothing to check

    is_consolidation = cmd.reason.strip().lower().startswith("consolidation")
    if is_consolidation:
        return None
    similarity = SequenceMatcher(
        None, current["content"], cmd.new_content, autojunk=False
    ).ratio()
    if (1 - similarity) > MAX_REPLACEMENT_FRACTION:
        return (
            f"this rewrites {round((1 - similarity) * 100)}% of working_state; "
            "edit it incrementally, or say why under a 'consolidation' reason"
        )
    return None


async def _validate_schedule_wakeup(
    conn: AsyncConnection, cmd: ScheduleWakeup
) -> str | None:
    if cmd.fire_at is None and cmd.offset_minutes is None:
        return "give either fire_at or offset_minutes"
    if cmd.fire_at is not None and cmd.offset_minutes is not None:
        return "give either fire_at or offset_minutes, not both"
    if cmd.offset_minutes is not None and cmd.offset_minutes <= 0:
        return "offset_minutes must be positive — wakeups fire in the future"
    if cmd.fire_at is not None and _aware(cmd.fire_at) <= datetime.now(timezone.utc):
        return "fire_at is in the past"
    if not cmd.purpose.strip():
        return "purpose is required — a wakeup with no purpose can't be acted on"
    return None


async def _validate_cancel_wakeup(
    conn: AsyncConnection, cmd: CancelWakeup
) -> str | None:
    row = await wakeups_q.get(conn, cmd.id)
    if row is None:
        return f"wakeup {cmd.id} not found"
    if row["status"] != "active":
        return f"wakeup {cmd.id} is already {row['status']}"
    return None


async def _validate_calendar(
    conn: AsyncConnection, cmd: CreateCalendarEvent, *, user: dict[str, Any]
) -> str | None:
    if not user["calendar_enabled"]:
        return "calendar is not enabled for this user"
    duration = _aware(cmd.ends_at) - _aware(cmd.starts_at)
    if duration <= timedelta(0):
        return "ends_at must be after starts_at"
    if duration >= MAX_CALENDAR_DURATION:
        return f"event spans {duration} — longer than a day is almost certainly a mistake"
    # "Never on someone else's calendar" (§9.2): the target must be one of the
    # user's own configured calendars. RLS scopes the lookup, so another
    # user's calendar id simply reads as unknown.
    states = await calendar_q.states(conn)
    if not any(s["calendar_id"] == cmd.calendar_id for s in states):
        return f"'{cmd.calendar_id}' is not one of your configured calendars"
    return None


async def _validate_notify(conn: AsyncConnection, cmd: Notify) -> str | None:
    if not cmd.body.strip():
        return "body is required"
    return None


async def _validate_update_doc(
    conn: AsyncConnection, cmd: UpdateDoc, *, consolidation: bool
) -> str | None:
    if not consolidation and cmd.kind not in TURN_WRITABLE_DOC_KINDS:
        return f"only the consolidation job may rewrite {cmd.kind}"
    if not cmd.reason.strip():
        return "reason is required"
    tokens = estimate_tokens(cmd.new_content)
    cap = DOC_TOKEN_CAPS[cmd.kind]
    if tokens > cap:
        return f"{cmd.kind} would be ~{tokens} tokens, over its {cap} cap"
    return None


async def _validate_upsert_fact(
    conn: AsyncConnection, cmd: UpsertMemoryFact, *, consolidation: bool
) -> str | None:
    if not consolidation:
        return "only the consolidation job may write memory_facts"
    if not (cmd.subject.strip() and cmd.predicate.strip() and cmd.value.strip()):
        return "subject, predicate, and value are all required"
    if not 0.0 <= cmd.confidence <= 1.0:
        return f"confidence {cmd.confidence} is not in [0, 1]"
    return None


async def validate_commands(
    conn: AsyncConnection,
    *,
    user: dict[str, Any],
    commands: list[Command],
    consolidation: bool = False,
    ingestion: bool = False,
) -> Validated:
    """Split the model's commands into what may be applied and what may not.

    `conn` must already be inside the tenant transaction that will apply the
    accepted commands. `consolidation` is true only for the §13.1 job's turns;
    it unlocks the job-only commands (UpdateDoc, UpsertMemoryFact).
    `ingestion` is true only for §14 document-ingestion turns, which PROPOSE:
    every state command is rejected there (Notify excepted — the turn is
    asynchronous and may ping that the extraction is ready); the batch is
    emitted from the chat turn where the user confirms.
    """
    accepted: list[Command] = []
    rejected: list[Rejection] = []
    commitment_count = 0
    event_count = 0

    # Naive datetimes mean the USER's wall clock, not UTC: the model reads a
    # user-local context clock, so "20:00" naive is 8 PM for them. Treating
    # it as UTC fired an evening wakeup at 4 PM (live, 2026-07-23).
    _localize_naive_datetimes(commands, user)
    has_notify = any(isinstance(cmd, Notify) for cmd in commands)

    for cmd in commands:
        if ingestion and not isinstance(cmd, Notify):
            rejected.append(Rejection(
                command=cmd,
                reason=(
                    "ingestion turns only propose; emit this after the user "
                    "confirms in chat"
                ),
            ))
            continue
        if isinstance(cmd, UpsertCommitment):
            commitment_count += 1
            if commitment_count > MAX_COMMITMENTS_PER_TURN:
                rejected.append(Rejection(
                    command=cmd,
                    reason=(
                        f"over the {MAX_COMMITMENTS_PER_TURN}-commitment "
                        "per-turn cap; split the batch across turns"
                    ),
                ))
                continue
        if isinstance(cmd, AppendEvent):
            event_count += 1
            if event_count > MAX_EVENTS_PER_TURN:
                rejected.append(Rejection(
                    command=cmd,
                    reason=(
                        f"over the {MAX_EVENTS_PER_TURN}-event per-turn cap; "
                        "split the batch across turns"
                    ),
                ))
                continue

        if isinstance(cmd, AppendEvent):
            reason = await _validate_append_event(conn, cmd)
        elif isinstance(cmd, LogMeal):
            reason = await _validate_log_meal(conn, cmd)
        elif isinstance(cmd, LogWorkout):
            reason = await _validate_log_workout(conn, cmd)
        elif isinstance(cmd, LogWeight):
            reason = await _validate_log_weight(conn, cmd)
        elif isinstance(cmd, SetTargets):
            reason = await _validate_set_targets(
                conn, cmd, consolidation=consolidation
            )
        elif isinstance(cmd, CompleteCommitment):
            reason = await _validate_complete(conn, cmd)
        elif isinstance(cmd, SnoozeCommitment):
            reason = await _validate_snooze(conn, cmd)
        elif isinstance(cmd, UpdateWorkingState):
            reason = await _validate_update_working_state(conn, cmd)
            if reason is None and not has_notify and _PUSH_CLAIM.search(cmd.new_content):
                # References to OLDER pushes are fine — those left delivery
                # rows. Only a claim with no Notify in-batch AND no recent
                # delivery is the poison case.
                cur = await conn.execute(
                    "SELECT 1 FROM deliveries WHERE channel = 'apns' "
                    "AND created_at > now() - interval '20 minutes' LIMIT 1"
                )
                if await cur.fetchone() is None:
                    reason = (
                        "this working_state records a push being sent, but "
                        "no push exists (none in this batch, none recent) — "
                        "emit the Notify alongside it, or correct the note "
                        "(a recorded-but-unsent push silences every later "
                        "turn)"
                    )
        elif isinstance(cmd, ScheduleWakeup):
            reason = await _validate_schedule_wakeup(conn, cmd)
        elif isinstance(cmd, CancelWakeup):
            reason = await _validate_cancel_wakeup(conn, cmd)
        elif isinstance(cmd, CreateCalendarEvent):
            reason = await _validate_calendar(conn, cmd, user=user)
        elif isinstance(cmd, Notify):
            reason = await _validate_notify(conn, cmd)
        elif isinstance(cmd, UpdateDoc):
            reason = await _validate_update_doc(
                conn, cmd, consolidation=consolidation
            )
        elif isinstance(cmd, UpsertMemoryFact):
            reason = await _validate_upsert_fact(
                conn, cmd, consolidation=consolidation
            )
        else:  # UpsertCommitment
            reason = await _validate_upsert(conn, cmd)

        if reason is None:
            accepted.append(cmd)
        else:
            log.info("rejecting %s: %s", cmd.type, reason)
            rejected.append(Rejection(command=cmd, reason=reason))

    return Validated(accepted=accepted, rejected=rejected)
