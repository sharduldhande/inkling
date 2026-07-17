"""InferenceGateway protocol (§7).

Only the interface and a test stub live here for now. The real adapters —
anthropic_sdk (subscription auth), anthropic_api (metered), codex_exec — land
with §7 proper; load the claude-api skill before writing them.

The protocol is a stream rather than a call-and-wait because §5.5 needs to
write `turn_events` and relay SSE as text arrives, and because a turn must be
resumable from persisted events if the socket drops mid-answer.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class TextDelta:
    """A fragment of user-facing text."""

    text: str


@dataclass(frozen=True)
class Completed:
    """Terminal chunk: the assembled answer plus everything the turn records.

    `commands` stays raw here — validation is the commit path's job (§9.2), so
    a model that emits nonsense produces rejected commands and an audit_log
    row, never a gateway crash.
    """

    text: str
    commands: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


Chunk = TextDelta | Completed


class InferenceGateway(Protocol):
    def stream(
        self, *, user_id: str, prompt: str, context: dict[str, Any]
    ) -> AsyncIterator[Chunk]:
        """Yield TextDelta chunks, then exactly one Completed as the last chunk."""
        ...


class StubGateway:
    """Deterministic gateway for tests: echoes a scripted reply, no network.

    Lets the §5 pipeline — claim, lease, heartbeat, commit, stream — be
    exercised end-to-end before any real provider exists.
    """

    def __init__(
        self,
        reply: str = "ok",
        commands: list[dict[str, Any]] | None = None,
        *,
        chunk_size: int = 4,
        delay: float = 0.0,
    ):
        self.reply = reply
        self.commands = commands or []
        self.chunk_size = chunk_size
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    async def stream(
        self, *, user_id: str, prompt: str, context: dict[str, Any]
    ) -> AsyncIterator[Chunk]:
        import asyncio

        self.calls.append({"user_id": user_id, "prompt": prompt, "context": context})
        for i in range(0, len(self.reply), self.chunk_size):
            if self.delay:
                await asyncio.sleep(self.delay)
            yield TextDelta(self.reply[i : i + self.chunk_size])
        yield Completed(
            text=self.reply,
            commands=list(self.commands),
            model="stub",
            input_tokens=len(prompt.split()),
            output_tokens=len(self.reply.split()),
        )
