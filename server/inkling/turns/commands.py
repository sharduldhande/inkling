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

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

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


class NotifyPolicy(BaseModel):
    """§4.6 commitments.notify_policy shape."""

    repeat_after_min: int = 15
    escalate_after_min: int = 45
    stop_on_ack: bool = True
    quiet_hours: Literal["defer", "override"] = "defer"


class AppendEvent(BaseModel):
    type: Literal["AppendEvent"]
    kind: EventKind
    habit_id: str | None = None
    value_num: float | None = None
    unit: str | None = None
    note: str
    occurred_at: datetime | None = None


class UpsertCommitment(BaseModel):
    type: Literal["UpsertCommitment"]
    id: int | None = None
    expected_version: int | None = None
    title: str
    body: str | None = None
    due_at: datetime | None = None
    recurrence: str | None = None  # RRULE
    lead_time: timedelta | None = None
    notify_policy: NotifyPolicy | None = None


class CompleteCommitment(BaseModel):
    type: Literal["CompleteCommitment"]
    id: int
    expected_version: int
    reason: str


class SnoozeCommitment(BaseModel):
    type: Literal["SnoozeCommitment"]
    id: int
    expected_version: int
    until: datetime


class UpdateWorkingState(BaseModel):
    type: Literal["UpdateWorkingState"]
    new_content: str
    reason: str


class ScheduleWakeup(BaseModel):
    type: Literal["ScheduleWakeup"]
    fire_at: datetime | None = None
    offset_minutes: int | None = None
    purpose: str


class CancelWakeup(BaseModel):
    type: Literal["CancelWakeup"]
    id: int


class CreateCalendarEvent(BaseModel):
    type: Literal["CreateCalendarEvent"]
    calendar_id: str
    summary: str
    starts_at: datetime
    ends_at: datetime
    description: str | None = None


class Notify(BaseModel):  # rare: extra push beyond the reply
    type: Literal["Notify"]
    body: str


Command = Annotated[
    AppendEvent
    | UpsertCommitment
    | CompleteCommitment
    | SnoozeCommitment
    | UpdateWorkingState
    | ScheduleWakeup
    | CancelWakeup
    | CreateCalendarEvent
    | Notify,
    Field(discriminator="type"),
]

_command_adapter: TypeAdapter[Command] = TypeAdapter(Command)


def parse_commands(raw: Any) -> list[Command]:
    """Validate the model's raw `commands` array, dropping what doesn't parse.

    §7: missing/invalid JSON means "no commands", logged — a model that emits
    nonsense produces a turn with fewer commands, never a failed turn. Per-item
    rather than all-or-nothing, so one malformed command doesn't discard its
    valid siblings.
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
    return parsed


def commands_json_schema() -> dict[str, Any]:
    """JSON schema for the emit_commands tool input (§7)."""
    return {
        "type": "object",
        "properties": {
            "commands": {
                "type": "array",
                "description": "State-changing commands for this turn; empty if none.",
                "items": _command_adapter.json_schema(),
            }
        },
        "required": ["commands"],
    }
