"""The event the worker publishes and the API relays (§5.5).

Lives here rather than in api/ so the worker never imports the API: the worker
must run headless (role=worker) with nothing listening.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StreamEvent:
    """One persisted turn_events row, handed to live subscribers.

    Carries `trigger_id` so a /chat request can recognise the turn spawned by
    *its* trigger without racing the worker to read triggers.turn_id.
    """

    user_id: str
    trigger_id: int
    turn_id: int
    seq: int
    kind: str  # text_delta | done | exit | error
    data: dict[str, Any]
