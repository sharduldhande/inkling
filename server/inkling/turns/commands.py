"""Model command schema (§9.1) — the ONLY way the model changes anything (D4).

The model proposes commands; the §9.2 validation layer accepts or rejects them;
nothing here touches the database. There is deliberately NO DeleteCalendarEvent,
NO doc command targeting `goals`, NO raw SQL, NO file access.

Commands arrive as the JSON input of the `emit_commands` tool call (§7) and are
validated here with a discriminated union — never parsed from prose.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

log = logging.getLogger(__name__)

# §4.4 events.kind.
EventKind = Literal[
    "habit_done",
    "habit_skipped",
    "measurement",
    "checkin",
    "note",
    "correction",
    "ingestion",
    "system",
]


class _CommandBase(BaseModel):
    """Reject unknown fields outright: with all-optional commands (SetTargets)
    a wrong field name would otherwise parse as an EMPTY command — seen live
    from two different models. A hard ValidationError routes through the
    parse_commands notes path instead, telling the user what failed."""

    model_config = ConfigDict(extra="forbid")


class NotifyPolicy(_CommandBase):
    """§4.6 commitments.notify_policy shape."""

    repeat_after_min: int = 15
    escalate_after_min: int = 45
    stop_on_ack: bool = True
    quiet_hours: Literal["defer", "override"] = "defer"


class AppendEvent(_CommandBase):
    type: Literal["AppendEvent"]
    kind: EventKind
    habit_id: str | None = None
    value_num: float | None = None
    unit: str | None = None
    note: str
    occurred_at: datetime | None = None
    # §14: on kind='ingestion' only — records the user's confirmation of a
    # proposed document schedule and sets ingested_documents.confirmed_at.
    document_id: int | None = None
    # On kind='habit_done'/'habit_skipped' only — names the open recurring
    # commitment this fulfills, checking off today's occurrence (apply.py
    # retires it) and surfacing done-today in /agenda. The commitment stays
    # open; CompleteCommitment is how a loop ENDS, this is how a day counts.
    commitment_id: int | None = None


# Health commands write the typed tables (intake / workouts / body_metrics),
# never events: the schema carries the meaning, so there are no unit-string
# conventions for a prompt to drift away from.

# HKWorkoutActivityType-compatible names: the app writes chat-logged workouts
# to Apple Health verbatim from this string, so additions must map to a real
# HealthKit case.
WorkoutActivity = Literal[
    "running",
    "walking",
    "cycling",
    "swimming",
    "hiking",
    "rowing",
    "elliptical",
    "stairClimbing",
    "jumpRope",
    "yoga",
    "pilates",
    "coreTraining",
    "flexibility",
    "traditionalStrengthTraining",
    "functionalStrengthTraining",
    "highIntensityIntervalTraining",
    "dance",
    "martialArts",
    "soccer",
    "basketball",
    "tennis",
    "badminton",
    "cricket",
    "other",
]


class LogMeal(_CommandBase):
    type: Literal["LogMeal"]
    note: str  # what was eaten, the user's words or the model's identification
    kcal: float | None = None
    protein_g: float | None = None
    carb_g: float | None = None
    fat_g: float | None = None
    occurred_at: datetime | None = None
    supersedes_id: int | None = None  # correction: this row replaces that one


class LogWorkout(_CommandBase):
    type: Literal["LogWorkout"]
    activity_type: WorkoutActivity
    duration_min: float
    kcal_burned: float | None = None  # model-derived (MET × weight) when possible
    distance_km: float | None = None
    note: str | None = None
    occurred_at: datetime | None = None


class LogWeight(_CommandBase):
    type: Literal["LogWeight"]
    value: float
    unit: Literal["kg", "lb"]
    occurred_at: datetime | None = None


class SetTargets(_CommandBase):
    type: Literal["SetTargets"]
    kcal_quota: int | None = None
    protein_quota_g: int | None = None


class UpsertCommitment(_CommandBase):
    type: Literal["UpsertCommitment"]
    id: int | None = None
    expected_version: int | None = None
    title: str
    body: str | None = None
    due_at: datetime | None = None
    recurrence: str | None = None  # RRULE
    lead_time: timedelta | None = None
    notify_policy: NotifyPolicy | None = None


class CompleteCommitment(_CommandBase):
    type: Literal["CompleteCommitment"]
    id: int
    expected_version: int
    # done = the USER said it happened; dropped = the USER explicitly
    # abandoned it. Required — closing is always a deliberate verdict, and a
    # passed deadline is neither: overdue work stays open (turn 68 closed a
    # lapsed extra-credit window as 'done' and the app showed a false ✓).
    outcome: Literal["done", "dropped"]
    reason: str


class SnoozeCommitment(_CommandBase):
    type: Literal["SnoozeCommitment"]
    id: int
    expected_version: int
    until: datetime


class UpdateWorkingState(_CommandBase):
    type: Literal["UpdateWorkingState"]
    new_content: str
    reason: str


class ScheduleWakeup(_CommandBase):
    type: Literal["ScheduleWakeup"]
    fire_at: datetime | None = None
    offset_minutes: int | None = None
    purpose: str


class CancelWakeup(_CommandBase):
    type: Literal["CancelWakeup"]
    id: int


class CreateCalendarEvent(_CommandBase):
    type: Literal["CreateCalendarEvent"]
    calendar_id: str
    summary: str
    starts_at: datetime
    ends_at: datetime
    description: str | None = None


class Notify(_CommandBase):  # rare: extra push beyond the reply
    type: Literal["Notify"]
    body: str


# --- consolidation-only commands (§13.1) ------------------------------------
# §9.1's union deliberately has no doc command beyond working_state and no
# memory_facts writes; §13.1 requires both, "validated like any turn". These
# exist in the schema so they flow through the same parse→validate→apply
# pipeline, and validate.py rejects them for any turn that is not the
# consolidation job. goals stays command-less: only a human edits goals.


class UpdateDoc(_CommandBase):
    # daily_plan is writable from any turn (the day-start/presence machinery
    # owns today's plan); patterns and digest7 stay consolidation-only —
    # validate.py enforces the split.
    type: Literal["UpdateDoc"]
    kind: Literal["patterns", "digest7", "daily_plan"]
    new_content: str
    reason: str


class UpsertMemoryFact(_CommandBase):
    type: Literal["UpsertMemoryFact"]
    subject: str
    predicate: str
    value: str
    condition: str | None = None
    confidence: float = 0.5


Command = Annotated[
    AppendEvent
    | LogMeal
    | LogWorkout
    | LogWeight
    | SetTargets
    | UpsertCommitment
    | CompleteCommitment
    | SnoozeCommitment
    | UpdateWorkingState
    | ScheduleWakeup
    | CancelWakeup
    | CreateCalendarEvent
    | Notify
    | UpdateDoc
    | UpsertMemoryFact,
    Field(discriminator="type"),
]

_command_adapter: TypeAdapter[Command] = TypeAdapter(Command)


def parse_commands(raw: Any, *, notes: list[str] | None = None) -> list[Command]:
    """Validate the model's raw `commands` array, dropping what doesn't parse.

    §7: missing/invalid JSON means "no commands", logged — a model that emits
    nonsense produces a turn with fewer commands, never a failed turn. Per-item
    rather than all-or-nothing, so one malformed command doesn't discard its
    valid siblings.

    A dropped command must not be silent to the user — the model may have
    claimed success in its prose (seen live: LogWeight with wrong field names
    dropped here while the reply said "Logged today's weight"). Callers that
    pass `notes` get one §9.2-style honest line per drop to append to the
    reply text; the audit log keeps the full validation error either way.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("emit_commands payload is not JSON; treating as no commands")
            return []
    if not isinstance(raw, list):
        log.warning(
            "emit_commands payload is %s, not a list; treating as no commands",
            type(raw).__name__,
        )
        return []

    parsed: list[Command] = []
    for i, item in enumerate(raw):
        try:
            parsed.append(_command_adapter.validate_python(item))
        except ValidationError as exc:
            log.warning("dropping invalid command [%d]: %s", i, exc)
            if notes is not None:
                kind = item.get("type", "command") if isinstance(item, dict) else "command"
                fields = ", ".join(
                    dict.fromkeys(
                        str(e["loc"][-1]) for e in exc.errors() if e.get("loc")
                    )
                ) or "arguments"
                notes.append(f"(couldn't complete {kind}: invalid {fields})")
    return parsed


def commands_json_schema() -> dict[str, Any]:
    """JSON schema for the emit_commands tool input (§7).

    The union schema's $defs must be hoisted to the ROOT of the wrapper:
    its "#/$defs/..." refs resolve from the document root, and a strict
    validator (the SDK's in-process MCP server) rejects every tool call
    when they dangle. Seen live: the model reported "the command channel
    is rejecting every command" on the first schema-advertised prod turn.
    """
    items = _command_adapter.json_schema()
    defs = items.pop("$defs", {})
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "commands": {
                "type": "array",
                "description": "State-changing commands for this turn; empty if none.",
                "items": items,
            }
        },
        "required": ["commands"],
    }
    if defs:
        schema["$defs"] = defs
    return schema
