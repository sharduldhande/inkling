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
from inkling.db.queries import triggers as triggers_q
from inkling.db.queries import turns as turns_q
from inkling.inference.gateway import Completed

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
    result: Completed,
    next_seq: int,
    actor: str,
) -> None:
    """Apply a completed turn's effects atomically and close it out.

    `next_seq` continues the turn_events numbering the streaming loop left off
    at, so Last-Event-ID resume sees one contiguous sequence (§5.5).

    Command application is not wired yet: §9 defines the Pydantic command
    schema and the validation layer, and until it exists every command is
    recorded to audit_log and applied to nothing. No real gateway emits
    commands yet, so nothing is silently dropped today — but §9 must land
    before any adapter that can.
    """
    async with db.with_tenant(user_id) as conn:
        for command in result.commands:
            await _audit(
                conn,
                user_id=user_id,
                actor=actor,
                action="command_not_implemented",
                detail={"command": command, "turn_id": turn_id},
            )
        if result.commands:
            log.warning(
                "turn %s emitted %d command(s) with §9 unimplemented; recorded to "
                "audit_log and applied to nothing",
                turn_id,
                len(result.commands),
            )

        payload = trigger["payload"] or {}
        if trigger["source"] == "chat" and payload.get("text"):
            await _transcript(
                conn,
                user_id=user_id,
                turn_id=turn_id,
                role="user",
                text=payload["text"],
            )
        if result.text:
            await _transcript(
                conn,
                user_id=user_id,
                turn_id=turn_id,
                role="assistant",
                text=result.text,
            )

        # v1 stream shapes, byte-compatible: text deltas, then done, then exit.
        await turns_q.append_event(
            conn,
            user_id=user_id,
            turn_id=turn_id,
            seq=next_seq,
            kind="done",
            data={"done": True},
        )
        await turns_q.append_event(
            conn,
            user_id=user_id,
            turn_id=turn_id,
            seq=next_seq + 1,
            kind="exit",
            data={"exit": 0},
        )

        await turns_q.finish(
            conn,
            turn_id,
            status="ok",
            model=result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
        await triggers_q.set_status(conn, int(trigger["id"]), "done", turn_id=turn_id)
