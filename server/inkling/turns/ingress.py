"""Trigger insertion (§5.1).

Every path into a turn — POST /chat, scheduler wakeups, occurrence surfacing,
calendar hints, jobs — lands here, inserts a `triggers` row, and returns
immediately. Nothing in this module runs a turn; the per-user worker picks the
row up (§5.2).
"""

import json
import uuid
from dataclasses import dataclass
from typing import Any

from inkling.db.client import Database
from inkling.db.queries import triggers as q

# §4.2: 1 chat · 2 occurrence · 3 calendar/wakeup · 4 maintenance.
PRIORITY_BY_SOURCE = {
    "chat": 1,
    "occurrence": 2,
    "calendar": 3,
    "wakeup": 3,
    "presence": 3,
    "job": 4,
}

SOURCES = frozenset(PRIORITY_BY_SOURCE)


@dataclass(frozen=True)
class Submitted:
    trigger_id: int
    status: str  # pending | coalesced
    duplicate: bool  # idempotency key already present; nothing was inserted


async def submit(
    db: Database,
    *,
    user_id: str,
    source: str,
    payload: dict[str, Any],
    source_ref: str | None = None,
    idempotency_key: str | None = None,
) -> Submitted:
    """Record a trigger and return without running it.

    `idempotency_key` makes retried submissions no-ops (§4.2): a duplicate key
    returns the original row rather than raising. Callers with a natural key
    (client message id, provider event id, wakeup id) should pass it. Omitting
    it generates a unique one, which means "this submission is not a retry" —
    two identical chat messages are two real turns, not a double-send.
    """
    if source not in SOURCES:
        raise ValueError(f"unknown trigger source {source!r}; expected one of {sorted(SOURCES)}")

    key = idempotency_key or str(uuid.uuid4())
    priority = PRIORITY_BY_SOURCE[source]

    async with db.with_tenant(user_id) as conn:
        row = await q.insert(
            conn,
            user_id=user_id,
            source=source,
            source_ref=source_ref,
            idempotency_key=key,
            payload=json.dumps(payload),
            priority=priority,
        )
        if row is not None:
            return Submitted(int(row["id"]), row["status"], duplicate=False)

        # ON CONFLICT DO NOTHING swallowed the insert: the key is already here.
        existing = await q.by_idempotency_key(conn, user_id, key)
        assert existing is not None  # the conflict proves the row exists
        return Submitted(int(existing["id"]), existing["status"], duplicate=True)
