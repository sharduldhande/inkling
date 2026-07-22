"""InferenceGateway protocol and shared adapter machinery (§7).

The interface is the spec's, verbatim in shape: adapters take the assembled
system + context strings, stream user-facing text through `on_text_delta`, and
return a TurnResult whose commands are already Pydantic-validated §9.1 objects.
Commands are emitted via the `emit_commands` tool so output is structured JSON
— never parsed from prose (D4).

One deliberate divergence from §7's wording, made once here so every adapter
inherits it: the spec says commands arrive via a *forced* tool call, but a
forced `tool_choice` suppresses the normal assistant text stream and is
incompatible with adaptive thinking — it would satisfy the letter of the spec
by destroying its other requirement ("the user-facing text is the normal
assistant text stream"). Instead the tool is offered with auto tool choice and
the system prompt instructs the model to always call it exactly once, with an
empty list when there is nothing to change. The property the spec actually
wants — commands validated from structured JSON, never prose — is unchanged.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from pydantic import BaseModel

from inkling.turns.commands import Command, commands_json_schema

log = logging.getLogger(__name__)

OnTextDelta = Callable[[str], Awaitable[None]]

# A read tool's executor: takes the model's arguments, returns the tool_result
# text. Handlers open their own short tenant transactions — §5.3 forbids a
# transaction *held across* inference, not reads between tokens. emit_commands
# deliberately has no handler: it is capture-only, applied at commit (§5.4).
ToolHandler = Callable[[dict[str, Any]], Awaitable[str]]

EMIT_COMMANDS_INSTRUCTION = (
    "After your reply, call the emit_commands tool exactly once with every "
    "state-changing command this turn requires; pass an empty commands list "
    "if there are none. Commands are the only way anything is recorded — "
    "never claim an action you did not emit a command for."
)


class ToolSchema(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]


def emit_commands_tool() -> ToolSchema:
    """The single tool every adapter exposes (§7, §9)."""
    return ToolSchema(
        name="emit_commands",
        description=(
            "Record this turn's state-changing commands. Call exactly once per "
            "turn, after composing your reply; pass an empty list if the turn "
            "changes nothing."
        ),
        input_schema=commands_json_schema(),
    )


class TurnResult(BaseModel):
    text: str
    commands: list[Command]
    model: str
    input_tokens: int
    output_tokens: int


class InferenceGateway(Protocol):
    async def run_turn(
        self,
        *,
        user_id: str,
        system: str,
        context: str,  # assembled per §8
        tools: list[ToolSchema],  # emit_commands (§9) + read tools (recall)
        on_text_delta: OnTextDelta,
        tool_handlers: dict[str, ToolHandler] | None = None,
        # Trigger source ("chat", "wakeup", …): a latency hint, not a contract
        # change — chat has a human watching the stream, so adapters may trade
        # reasoning depth for time-to-first-token on it.
        source: str = "chat",
    ) -> TurnResult: ...


class InferenceError(Exception):
    """Adapter failure with the §7 retryable/fatal classification attached."""

    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


RETRY_BACKOFF_SECONDS = 2.0


async def with_one_retry(call: Callable[[], Awaitable[TurnResult]]) -> TurnResult:
    """§7 error policy: retryable errors get exactly one retry with backoff.

    Fatal errors (auth, 4xx) propagate immediately — retrying a bad request
    burns tokens to fail identically. TODO(§11): on repeated rate limits,
    insert a "brain is rate-limited until ~HH:MM" delivery once the outbox
    drainer exists (honest-degradation decision).
    """
    try:
        return await call()
    except InferenceError as exc:
        if not exc.retryable:
            raise
        log.warning("retryable inference error, retrying once: %s", exc)
        await asyncio.sleep(RETRY_BACKOFF_SECONDS)
        return await call()


class StubGateway:
    """Deterministic gateway for tests: scripted reply, no network.

    `recalls` scripts mid-turn read-tool calls: each dict is invoked against
    the worker-provided recall handler before the reply streams, and the
    results land in the recorded call as "recall_results" — so tests can
    assert what the model *would have seen*.
    """

    def __init__(
        self,
        reply: str = "ok",
        commands: list[Command] | None = None,
        *,
        chunk_size: int = 4,
        delay: float = 0.0,
        recalls: list[dict[str, Any]] | None = None,
    ):
        self.reply = reply
        self.commands = commands or []
        self.chunk_size = chunk_size
        self.delay = delay
        self.recalls = recalls or []
        self.calls: list[dict[str, Any]] = []

    async def run_turn(
        self,
        *,
        user_id: str,
        system: str,
        context: str,
        tools: list[ToolSchema],
        on_text_delta: OnTextDelta,
        tool_handlers: dict[str, ToolHandler] | None = None,
        source: str = "chat",
    ) -> TurnResult:
        recall_results = []
        handler = (tool_handlers or {}).get("recall")
        for args in self.recalls:
            assert handler is not None, "scripted recall but no handler wired"
            recall_results.append(await handler(args))
        self.calls.append(
            {
                "user_id": user_id,
                "system": system,
                "context": context,
                "tools": tools,
                "recall_results": recall_results,
                "source": source,
            }
        )
        for i in range(0, len(self.reply), self.chunk_size):
            if self.delay:
                await asyncio.sleep(self.delay)
            await on_text_delta(self.reply[i : i + self.chunk_size])
        return TurnResult(
            text=self.reply,
            commands=list(self.commands),
            model="stub",
            input_tokens=len(context.split()),
            output_tokens=len(self.reply.split()),
        )
