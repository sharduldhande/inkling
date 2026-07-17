"""Command application (§9.2) — the only code that turns a model command into
a row.

Everything here runs inside the turn's single tenant transaction (§5.4) and
assumes validate.py already accepted the command: an id exists, a version
matched, a transition is legal. The version guards are still written into the
UPDATEs, so a row that changes between validation and application aborts the
turn rather than silently applying to a stale row — a caught race, not a
corrupted commitment.

No external side effects. A Notify becomes a `deliveries` row that the §11
drainer sends after this transaction commits.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg import AsyncConnection

from inkling.db.queries import commitments as commitments_q
from inkling.db.queries import deliveries as deliveries_q
from inkling.db.queries import docs as docs_q
from inkling.db.queries import events as events_q
from inkling.db.queries import wakeups as wakeups_q
from inkling.turns.commands import (
    AppendEvent,
    CancelWakeup,
    Command,
    CompleteCommitment,
    Notify,
    ScheduleWakeup,
    SnoozeCommitment,
    UpdateWorkingState,
    UpsertCommitment,
)

log = logging.getLogger(__name__)


class ConcurrentModification(RuntimeError):
    """A version guard failed between validation and application."""


def _authorship(turn_id: int) -> str:
    """§4.7/§4.9 provenance: every agent write says which turn made it."""
    return f"agent:turn:{turn_id}"


async def _apply_append_event(
    conn: AsyncConnection, cmd: AppendEvent, *, user_id: str, turn_id: int
) -> dict[str, Any]:
    row = await events_q.insert(
        conn,
        user_id=user_id,
        occurred_at=cmd.occurred_at,
        kind=cmd.kind,
        habit_id=cmd.habit_id,
        value_num=cmd.value_num,
        unit=cmd.unit,
        note=cmd.note,
        source="conversation",
        turn_id=turn_id,
    )
    return {"event_id": int(row["id"])}


async def _materialize_first_occurrence(
    conn: AsyncConnection, commitment: dict[str, Any], *, user_id: str
) -> int | None:
    """Give a dated commitment its first occurrence so it can't be forgotten.

    §4.6's guarantee starts here: an open commitment with a due date has a
    pending occurrence from the moment it exists, rather than from whenever the
    scheduler next scans. Recurrence expansion stays with §10 — this is the
    one-shot case only, and it's idempotent on (commitment_id, fire_at).
    """
    if commitment["due_at"] is None:
        return None
    fire_at = commitment["due_at"] - (commitment["lead_time"] or timedelta(0))
    return await commitments_q.insert_occurrence(
        conn,
        user_id=user_id,
        commitment_id=int(commitment["id"]),
        fire_at=fire_at,
    )


async def _apply_upsert_commitment(
    conn: AsyncConnection, cmd: UpsertCommitment, *, user_id: str, turn_id: int
) -> dict[str, Any]:
    policy = cmd.notify_policy.model_dump() if cmd.notify_policy else None
    if cmd.id is None:
        row = await commitments_q.insert(
            conn,
            user_id=user_id,
            title=cmd.title,
            body=cmd.body,
            due_at=cmd.due_at,
            recurrence=cmd.recurrence,
            lead_time=cmd.lead_time,
            source="conversation",
            source_ref=_authorship(turn_id),
            notify_policy=policy,
        )
        created = True
    else:
        assert cmd.expected_version is not None  # validate.py requires it for updates
        updated = await commitments_q.update(
            conn,
            commitment_id=cmd.id,
            version=cmd.expected_version,
            title=cmd.title,
            body=cmd.body,
            due_at=cmd.due_at,
            recurrence=cmd.recurrence,
            lead_time=cmd.lead_time,
            notify_policy=policy,
        )
        if updated is None:
            raise ConcurrentModification(f"commitment {cmd.id} changed mid-turn")
        row = updated
        created = False

    occurrence_id = await _materialize_first_occurrence(conn, row, user_id=user_id)
    return {
        "commitment_id": int(row["id"]),
        "version": int(row["version"]),
        "created": created,
        "occurrence_id": occurrence_id,
    }


async def _apply_complete_commitment(
    conn: AsyncConnection, cmd: CompleteCommitment, *, user_id: str, turn_id: int
) -> dict[str, Any]:
    row = await commitments_q.set_status(
        conn, commitment_id=cmd.id, version=cmd.expected_version, status="done"
    )
    if row is None:
        raise ConcurrentModification(f"commitment {cmd.id} changed mid-turn")
    # The reason is the durable record of *why* this closed: the commitment row
    # only remembers that it did.
    await events_q.insert(
        conn,
        user_id=user_id,
        occurred_at=None,
        kind="system",
        habit_id=None,
        value_num=None,
        unit=None,
        note=f"completed commitment {cmd.id} ({row['title']}): {cmd.reason}",
        source="agent",
        turn_id=turn_id,
    )
    return {"commitment_id": cmd.id, "version": int(row["version"])}


async def _apply_snooze_commitment(
    conn: AsyncConnection, cmd: SnoozeCommitment, *, user_id: str, turn_id: int
) -> dict[str, Any]:
    row = await commitments_q.set_status(
        conn, commitment_id=cmd.id, version=cmd.expected_version, status="open"
    )
    if row is None:
        raise ConcurrentModification(f"commitment {cmd.id} changed mid-turn")

    # Snoozing moves the reminder, not the commitment. If nothing is pending
    # yet (the scheduler hasn't materialized it, or it's undated), create the
    # occurrence — otherwise "remind me later" would quietly mean "never".
    moved = await commitments_q.reschedule_pending(
        conn, commitment_id=cmd.id, fire_at=cmd.until
    )
    occurrence_id = None
    if moved == 0:
        occurrence_id = await commitments_q.insert_occurrence(
            conn, user_id=user_id, commitment_id=cmd.id, fire_at=cmd.until
        )
    return {
        "commitment_id": cmd.id,
        "version": int(row["version"]),
        "rescheduled": moved,
        "occurrence_id": occurrence_id,
    }


async def _apply_update_working_state(
    conn: AsyncConnection, cmd: UpdateWorkingState, *, user_id: str, turn_id: int
) -> dict[str, Any]:
    row = await docs_q.insert_next_version(
        conn,
        user_id=user_id,
        kind="working_state",
        content=cmd.new_content,
        written_by=_authorship(turn_id),
    )
    return {"doc_id": int(row["id"]), "version": int(row["version"])}


async def _apply_schedule_wakeup(
    conn: AsyncConnection, cmd: ScheduleWakeup, *, user_id: str, turn_id: int
) -> dict[str, Any]:
    fire_at = cmd.fire_at
    if fire_at is None:
        assert cmd.offset_minutes is not None  # validate.py requires one or the other
        fire_at = datetime.now(timezone.utc) + timedelta(minutes=cmd.offset_minutes)
    row = await wakeups_q.insert(
        conn,
        user_id=user_id,
        fire_at=fire_at,
        recurrence=None,  # §9.1 gives the model one-shot wakeups only
        purpose=cmd.purpose,
        created_by=_authorship(turn_id),
    )
    return {"wakeup_id": int(row["id"]), "fire_at": fire_at.isoformat()}


async def _apply_cancel_wakeup(
    conn: AsyncConnection, cmd: CancelWakeup, *, user_id: str, turn_id: int
) -> dict[str, Any]:
    row = await wakeups_q.cancel(conn, cmd.id)
    if row is None:
        raise ConcurrentModification(f"wakeup {cmd.id} changed mid-turn")
    return {"wakeup_id": cmd.id}


async def _apply_notify(
    conn: AsyncConnection,
    cmd: Notify,
    *,
    user_id: str,
    turn_id: int,
    ntfy_topic: str,
    index: int,
) -> dict[str, Any]:
    # The key is derived from turn and position, so a replayed turn re-proposes
    # the same key and the ON CONFLICT drops it: at most one push per Notify.
    key = f"turn:{turn_id}:notify:{index}"
    row = await deliveries_q.enqueue(
        conn,
        user_id=user_id,
        destination=ntfy_topic,
        body=cmd.body,
        idempotency_key=key,
        turn_id=turn_id,
    )
    return {"delivery_id": int(row["id"]) if row else None, "duplicate": row is None}


async def apply_command(
    conn: AsyncConnection,
    cmd: Command,
    *,
    user: dict[str, Any],
    turn_id: int,
    index: int,
) -> dict[str, Any]:
    """Apply one accepted command; returns the detail recorded to audit_log."""
    user_id = user["id"]
    if isinstance(cmd, AppendEvent):
        return await _apply_append_event(conn, cmd, user_id=user_id, turn_id=turn_id)
    if isinstance(cmd, UpsertCommitment):
        return await _apply_upsert_commitment(
            conn, cmd, user_id=user_id, turn_id=turn_id
        )
    if isinstance(cmd, CompleteCommitment):
        return await _apply_complete_commitment(
            conn, cmd, user_id=user_id, turn_id=turn_id
        )
    if isinstance(cmd, SnoozeCommitment):
        return await _apply_snooze_commitment(
            conn, cmd, user_id=user_id, turn_id=turn_id
        )
    if isinstance(cmd, UpdateWorkingState):
        return await _apply_update_working_state(
            conn, cmd, user_id=user_id, turn_id=turn_id
        )
    if isinstance(cmd, ScheduleWakeup):
        return await _apply_schedule_wakeup(conn, cmd, user_id=user_id, turn_id=turn_id)
    if isinstance(cmd, CancelWakeup):
        return await _apply_cancel_wakeup(conn, cmd, user_id=user_id, turn_id=turn_id)
    if isinstance(cmd, Notify):
        return await _apply_notify(
            conn,
            cmd,
            user_id=user_id,
            turn_id=turn_id,
            ntfy_topic=user["ntfy_topic"],
            index=index,
        )
    # CreateCalendarEvent is rejected in validate.py until §12 lands; anything
    # else is a §9.1 command someone added without wiring application.
    raise NotImplementedError(f"no application for command {cmd.type}")
