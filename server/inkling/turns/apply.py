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

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg import AsyncConnection

from inkling.db.queries import commitments as commitments_q
from inkling.db.queries import deliveries as deliveries_q
from inkling.db.queries import docs as docs_q
from inkling.db.queries import documents as documents_q
from inkling.db.queries import events as events_q
from inkling.db.queries import facts as facts_q
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
        source=(
            f"ingestion:{cmd.document_id}"
            if cmd.document_id is not None else "conversation"
        ),
        turn_id=turn_id,
    )
    detail: dict[str, Any] = {"event_id": int(row["id"])}
    if cmd.document_id is not None:
        # §14: the ingestion marker doubles as the confirmation record.
        # Validation checked unconfirmed inside this same transaction, so a
        # False here is a caught race, not a silent double-confirm.
        detail["confirmed"] = await documents_q.confirm(conn, cmd.document_id)
        detail["document_id"] = cmd.document_id
    return detail


async def _enqueue_healthkit(
    conn: AsyncConnection,
    *,
    user_id: str,
    turn_id: int,
    index: int,
    sample: dict[str, Any],
) -> None:
    """Mirror a chat-logged health row to Apple Health via the outbox.

    channel='healthkit' rows are PULLED by the iOS app (GET /healthkit/pending
    + ack), never pushed: the §11 drainer's due-scan excludes the channel,
    because a phone has no endpoint to push to. Same idempotency shape as
    Notify, so a replayed turn mirrors each sample at most once. Until the app
    ships its drain (Phase C) these rows simply accumulate as 'queued', which
    is the honest representation of "not yet in Apple Health".
    """
    await deliveries_q.enqueue(
        conn,
        user_id=user_id,
        channel="healthkit",
        destination="healthkit",
        body=json.dumps(sample, default=str),
        idempotency_key=f"turn:{turn_id}:healthkit:{index}",
        turn_id=turn_id,
    )


async def _apply_log_meal(
    conn: AsyncConnection, cmd: LogMeal, *, user_id: str, turn_id: int, index: int
) -> dict[str, Any]:
    row = await health_q.insert_intake(
        conn,
        user_id=user_id,
        occurred_at=cmd.occurred_at,
        note=cmd.note,
        kcal=cmd.kcal,
        protein_g=cmd.protein_g,
        carb_g=cmd.carb_g,
        fat_g=cmd.fat_g,
        supersedes_id=cmd.supersedes_id,
        turn_id=turn_id,
    )
    await _enqueue_healthkit(
        conn,
        user_id=user_id,
        turn_id=turn_id,
        index=index,
        sample={
            "kind": "meal",
            "intake_id": int(row["id"]),
            "occurred_at": row["occurred_at"].isoformat(),
            "note": cmd.note,
            "kcal": cmd.kcal,
            "protein_g": cmd.protein_g,
            "carb_g": cmd.carb_g,
            "fat_g": cmd.fat_g,
            "supersedes_id": cmd.supersedes_id,
        },
    )
    return {"intake_id": int(row["id"]), "superseded": cmd.supersedes_id}


async def _apply_log_workout(
    conn: AsyncConnection, cmd: LogWorkout, *, user_id: str, turn_id: int, index: int
) -> dict[str, Any]:
    row = await health_q.insert_workout(
        conn,
        user_id=user_id,
        occurred_at=cmd.occurred_at,
        activity_type=cmd.activity_type,
        duration_min=cmd.duration_min,
        kcal_burned=cmd.kcal_burned,
        distance_km=cmd.distance_km,
        note=cmd.note,
        turn_id=turn_id,
    )
    assert row is not None  # chat rows carry no source_ref, so no conflict path
    await _enqueue_healthkit(
        conn,
        user_id=user_id,
        turn_id=turn_id,
        index=index,
        sample={
            "kind": "workout",
            "workout_id": int(row["id"]),
            "occurred_at": row["occurred_at"].isoformat(),
            "activity_type": cmd.activity_type,
            "duration_min": cmd.duration_min,
            "kcal_burned": cmd.kcal_burned,
            "distance_km": cmd.distance_km,
        },
    )
    return {"workout_id": int(row["id"])}


async def _apply_log_weight(
    conn: AsyncConnection, cmd: LogWeight, *, user_id: str, turn_id: int, index: int
) -> dict[str, Any]:
    row = await health_q.insert_body_metric(
        conn,
        user_id=user_id,
        occurred_at=cmd.occurred_at,
        kind="weight",
        value=cmd.value,
        unit=cmd.unit,
        turn_id=turn_id,
    )
    assert row is not None  # chat rows carry no source_ref
    await _enqueue_healthkit(
        conn,
        user_id=user_id,
        turn_id=turn_id,
        index=index,
        sample={
            "kind": "weight",
            "metric_id": int(row["id"]),
            "occurred_at": row["occurred_at"].isoformat(),
            "value": cmd.value,
            "unit": cmd.unit,
        },
    )
    return {"metric_id": int(row["id"])}


async def _apply_set_targets(
    conn: AsyncConnection, cmd: SetTargets, *, user_id: str, turn_id: int
) -> dict[str, Any]:
    patch = {
        k: v
        for k, v in (
            ("kcal_quota", cmd.kcal_quota),
            ("protein_quota_g", cmd.protein_quota_g),
        )
        if v is not None
    }
    row = await health_q.merge_targets(conn, user_id=user_id, patch=json.dumps(patch))
    if row is None:
        raise ConcurrentModification("users row invisible mid-turn")  # unreachable
    return {"targets": row["targets"]}


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
    # A reminder for a thing already done is noise: retire any occurrence that
    # hasn't surfaced yet (§4.6 skipped_done) so §10 never fires it.
    skipped = await commitments_q.skip_unsurfaced_occurrences(conn, cmd.id)
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
    return {
        "commitment_id": cmd.id,
        "version": int(row["version"]),
        "occurrences_skipped": skipped,
    }


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


async def _apply_update_doc(
    conn: AsyncConnection, cmd: UpdateDoc, *, user_id: str, turn_id: int
) -> dict[str, Any]:
    """§13.1: patterns/digest7 version, consolidation-only (validate gates)."""
    row = await docs_q.insert_next_version(
        conn,
        user_id=user_id,
        kind=cmd.kind,
        content=cmd.new_content,
        written_by=_authorship(turn_id),
    )
    return {"doc_id": int(row["id"]), "kind": cmd.kind, "version": int(row["version"])}


async def _apply_upsert_fact(
    conn: AsyncConnection, cmd: UpsertMemoryFact, *, user_id: str, turn_id: int
) -> dict[str, Any]:
    return await facts_q.upsert(
        conn,
        user_id=user_id,
        subject=cmd.subject,
        predicate=cmd.predicate,
        value=cmd.value,
        condition=cmd.condition,
        confidence=cmd.confidence,
    )


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


async def _apply_create_calendar_event(
    conn: AsyncConnection,
    cmd: CreateCalendarEvent,
    *,
    user_id: str,
    turn_id: int,
    index: int,
) -> dict[str, Any]:
    """§12 write path: a deliveries row on the 'calendar' channel.

    External side effects happen only via outbox drainers after commit (§5.4),
    and the calendar insert is exactly such an effect — same idempotency-key
    shape as Notify, so a replayed turn can't double-book.
    """
    key = f"turn:{turn_id}:calendar:{index}"
    row = await deliveries_q.enqueue(
        conn,
        user_id=user_id,
        channel="calendar",
        destination=cmd.calendar_id,
        body=json.dumps({
            "summary": cmd.summary,
            "starts_at": cmd.starts_at.isoformat(),
            "ends_at": cmd.ends_at.isoformat(),
            "description": cmd.description,
        }),
        idempotency_key=key,
        turn_id=turn_id,
    )
    return {"delivery_id": int(row["id"]) if row else None, "duplicate": row is None}


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
    if isinstance(cmd, LogMeal):
        return await _apply_log_meal(
            conn, cmd, user_id=user_id, turn_id=turn_id, index=index
        )
    if isinstance(cmd, LogWorkout):
        return await _apply_log_workout(
            conn, cmd, user_id=user_id, turn_id=turn_id, index=index
        )
    if isinstance(cmd, LogWeight):
        return await _apply_log_weight(
            conn, cmd, user_id=user_id, turn_id=turn_id, index=index
        )
    if isinstance(cmd, SetTargets):
        return await _apply_set_targets(conn, cmd, user_id=user_id, turn_id=turn_id)
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
    if isinstance(cmd, CreateCalendarEvent):
        return await _apply_create_calendar_event(
            conn, cmd, user_id=user_id, turn_id=turn_id, index=index
        )
    if isinstance(cmd, UpdateDoc):
        return await _apply_update_doc(conn, cmd, user_id=user_id, turn_id=turn_id)
    if isinstance(cmd, UpsertMemoryFact):
        return await _apply_upsert_fact(conn, cmd, user_id=user_id, turn_id=turn_id)
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
