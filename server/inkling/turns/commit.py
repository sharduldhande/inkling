"""Transactional commit of a finished turn (§5.4).

Everything a turn changed lands in ONE short transaction with the tenant set.
The model call already happened — by contract there is no open transaction
during inference (§5.3), so this opens the first and only one.

External side effects (ntfy, calendar writes) are never performed here. They
become `deliveries` outbox rows and happen after commit via the drainer (§11),
so a rollback can never leave a notification already sent.
"""

import json
import logging
from typing import Any

from psycopg import AsyncConnection

from inkling.db.client import Database
from inkling.db.queries import commitments as commitments_q
from inkling.db.queries import triggers as triggers_q
from inkling.db.queries import turns as turns_q
from inkling.db.queries import users as users_q
from inkling.inference.gateway import TurnResult
from inkling.turns.apply import apply_command
from inkling.turns.stream import StreamEvent
from inkling.turns.validate import validate_commands

log = logging.getLogger(__name__)


async def _audit(
    conn: AsyncConnection,
    *,
    user_id: str,
    actor: str,
    action: str,
    detail: dict[str, Any],
) -> None:
    await conn.execute(
        "INSERT INTO audit_log (user_id, actor, action, detail) VALUES (%s, %s, %s, %s)",
        (user_id, actor, action, json.dumps(detail)),
    )


async def _transcript(
    conn: AsyncConnection, *, user_id: str, turn_id: int, role: str, text: str
) -> None:
    await conn.execute(
        "INSERT INTO transcript (user_id, turn_id, role, text) VALUES (%s, %s, %s, %s)",
        (user_id, turn_id, role, text),
    )


async def commit_turn(
    db: Database,
    *,
    user_id: str,
    trigger: dict[str, Any],
    turn_id: int,
    result: TurnResult,
    next_seq: int,
    actor: str,
) -> list[StreamEvent]:
    """Apply a completed turn's effects atomically and close it out.

    `next_seq` continues the turn_events numbering the streaming loop left off
    at, so Last-Event-ID resume sees one contiguous sequence (§5.5). Returns the
    events this function persisted, in order, so the caller can republish them
    to live sockets without having to predict the numbering.

    Commands go through §9.2 here: validated against current state, applied if
    legal, and every outcome — accepted or rejected — written to audit_log.
    Rejections never fail the turn; they become a note appended to the assistant
    text so the model's claim and reality can't diverge (D4).
    """
    events: list[StreamEvent] = []
    trigger_id = int(trigger["id"])

    async with db.with_tenant(user_id) as conn:
        user = await users_q.get(conn, user_id)
        assert user is not None  # the turn exists, so its user does

        # Validation and application share this transaction: a check against a
        # row a concurrent turn could move between check and write would prove
        # nothing (§9.2).
        verdict = await validate_commands(conn, user=user, commands=result.commands)

        for index, command in enumerate(verdict.accepted):
            detail = await apply_command(
                conn, command, user=user, turn_id=turn_id, index=index
            )
            await _audit(
                conn,
                user_id=user_id,
                actor=actor,
                action="command_applied",
                detail={
                    "command": command.model_dump(mode="json"),
                    "turn_id": turn_id,
                    "result": detail,
                },
            )
        for rejection in verdict.rejected:
            await _audit(
                conn,
                user_id=user_id,
                actor=actor,
                action="command_rejected",
                detail={
                    "command": rejection.command.model_dump(mode="json"),
                    "turn_id": turn_id,
                    "reason": rejection.reason,
                },
            )

        text = result.text
        if verdict.rejected:
            log.info(
                "turn %s: applied %d command(s), rejected %d",
                turn_id,
                len(verdict.accepted),
                len(verdict.rejected),
            )
            notes = verdict.notes()
            text = f"{text}\n\n{notes}" if text else notes
            # The deltas already streamed are the model's claim; this correction
            # has to reach the same socket, not just the transcript.
            await turns_q.append_event(
                conn,
                user_id=user_id,
                turn_id=turn_id,
                seq=next_seq,
                kind="text_delta",
                data={"text": f"\n\n{notes}"},
            )
            events.append(
                StreamEvent(
                    user_id=user_id,
                    trigger_id=trigger_id,
                    turn_id=turn_id,
                    seq=next_seq,
                    kind="text_delta",
                    data={"text": f"\n\n{notes}"},
                )
            )
            next_seq += 1

        payload = trigger["payload"] or {}
        if trigger["source"] == "chat" and payload.get("text"):
            await _transcript(
                conn,
                user_id=user_id,
                turn_id=turn_id,
                role="user",
                text=payload["text"],
            )
        if text:
            # The transcript stores the text *with* the rejection notes: it is
            # what the user saw, and it's what the next turn's context replays.
            await _transcript(
                conn,
                user_id=user_id,
                turn_id=turn_id,
                role="assistant",
                text=text,
            )

        # v1 stream shapes, byte-compatible: text deltas, then done, then exit.
        for offset, (kind, data) in enumerate(
            (("done", {"done": True}), ("exit", {"exit": 0}))
        ):
            await turns_q.append_event(
                conn,
                user_id=user_id,
                turn_id=turn_id,
                seq=next_seq + offset,
                kind=kind,
                data=data,
            )
            events.append(
                StreamEvent(
                    user_id=user_id,
                    trigger_id=trigger_id,
                    turn_id=turn_id,
                    seq=next_seq + offset,
                    kind=kind,
                    data=data,
                )
            )

        await turns_q.finish(
            conn,
            turn_id,
            status="ok",
            model=result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
        await triggers_q.set_status(conn, trigger_id, "done", turn_id=turn_id)

        # §10: an occurrence trigger's successful turn is what "surfaced"
        # means. Same transaction as the turn's own completion, so a crash
        # can't record one without the other.
        if trigger["source"] == "occurrence" and trigger["source_ref"]:
            occurrence_id = int(trigger["source_ref"].split(":", 1)[1])
            await commitments_q.mark_occurrence_surfaced(
                conn, occurrence_id, turn_id=turn_id
            )

    return events
