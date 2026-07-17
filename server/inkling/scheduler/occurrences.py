"""Commitment → occurrence materialization (§10).

For every open commitment, ensure the next occurrence row exists. The
`UNIQUE (commitment_id, fire_at)` constraint makes this idempotent, so it runs
on every horizon refresh without bookkeeping: re-proposing an occurrence that
exists (or already fired — same key) is a no-op.

RRULE expansion uses dateutil. Instances are computed in the user's timezone
so `BYHOUR=9` means 9am where they live, not 9am UTC. A recurrence string the
model produced that dateutil can't parse is logged and skipped — a broken rule
must never take down the scheduler; the commitment still shows in §8's
always-present commitments section, so it degrades to visible-but-unscheduled.
"""

import logging
from datetime import datetime, timedelta, tzinfo
from typing import Any

from dateutil.rrule import rrulestr
from psycopg import AsyncConnection

from inkling.db.queries import commitments as commitments_q

log = logging.getLogger(__name__)


def next_instance(
    recurrence: str, *, dtstart: datetime, after: datetime
) -> datetime | None:
    """The first RRULE instance strictly after `after`, or None if exhausted."""
    rule = rrulestr(recurrence, dtstart=dtstart)
    return rule.after(after)


async def materialize_user(
    conn: AsyncConnection, *, user_id: str, now: datetime, tz: tzinfo
) -> int:
    """Ensure the next occurrence exists for each schedulable open commitment.

    Returns how many occurrence rows were actually created.
    """
    created = 0
    for c in await commitments_q.open_scheduled(conn):
        lead = c["lead_time"] or timedelta(0)
        if c["due_at"] is not None:
            # One-shot: apply.py materializes this at creation; re-proposing it
            # here covers rows seeded outside the command path.
            fire_at = c["due_at"] - lead
        else:
            try:
                instance = next_instance(
                    c["recurrence"],
                    dtstart=c["created_at"].astimezone(tz),
                    after=now.astimezone(tz),
                )
            except Exception:
                log.exception(
                    "commitment %s has unparseable recurrence %r; skipping",
                    c["id"], c["recurrence"],
                )
                continue
            if instance is None:
                continue  # the rule has run out; nothing left to schedule
            fire_at = instance - lead
        if await commitments_q.insert_occurrence(
            conn, user_id=user_id, commitment_id=int(c["id"]), fire_at=fire_at
        ):
            created += 1
    return created
