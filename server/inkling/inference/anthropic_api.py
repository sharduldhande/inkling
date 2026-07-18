"""Metered adapter: Anthropic Messages API with a per-user API key (§7, D7).

The key is read from the file named by the user's inference_profile
(`{"type": "api_key", "key_file": "/path"}`) at call time — the DB stores the
location, never the secret (§2).
"""

import logging
from pathlib import Path
from typing import Any

import anthropic

from inkling.inference.gateway import (
    EMIT_COMMANDS_INSTRUCTION,
    InferenceError,
    OnTextDelta,
    ToolHandler,
    ToolSchema,
    TurnResult,
    with_one_retry,
)
from inkling.turns.commands import parse_commands

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-4-8"
MAX_TOKENS = 16000
# Read-tool loop bound: recall's per-turn budget is 4, plus emit_commands and
# slack. A model still asking for tools here gets its results cut off rather
# than an unbounded conversation on someone's API key.
MAX_TOOL_ROUNDS = 8


def _classify(exc: Exception) -> InferenceError:
    """§7: retryable (5xx, overload, rate limit, timeout) vs fatal (auth, 4xx)."""
    if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return InferenceError(f"connection: {exc}", retryable=True)
    if isinstance(exc, anthropic.RateLimitError):
        return InferenceError(f"rate limited: {exc}", retryable=True)
    if isinstance(exc, anthropic.APIStatusError):
        return InferenceError(
            f"api {exc.status_code}: {exc.message}", retryable=exc.status_code >= 500
        )
    return InferenceError(f"{type(exc).__name__}: {exc}", retryable=False)


class AnthropicAPIGateway:
    def __init__(self, key_file: str, *, model: str = DEFAULT_MODEL):
        self.key_file = key_file
        self.model = model

    async def run_turn(
        self,
        *,
        user_id: str,
        system: str,
        context: str,
        tools: list[ToolSchema],
        on_text_delta: OnTextDelta,
        tool_handlers: dict[str, ToolHandler] | None = None,
    ) -> TurnResult:
        return await with_one_retry(
            lambda: self._run(
                system=system,
                context=context,
                tools=tools,
                on_text_delta=on_text_delta,
                tool_handlers=tool_handlers or {},
            )
        )

    async def _run(
        self,
        *,
        system: str,
        context: str,
        tools: list[ToolSchema],
        on_text_delta: OnTextDelta,
        tool_handlers: dict[str, ToolHandler],
    ) -> TurnResult:
        api_key = Path(self.key_file).read_text().strip()
        if not api_key:
            raise InferenceError(f"empty api key file {self.key_file}", retryable=False)

        wire_tools: list[dict[str, Any]] = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in tools
        ]

        text_parts: list[str] = []
        raw_commands: Any = None
        input_tokens = 0
        output_tokens = 0
        model_used = self.model

        # Tool loop: stream a response; if it stopped for tool_use, execute
        # the handled (read) tools, feed results back, continue. emit_commands
        # is capture-only — its "result" is an ack so the model can keep
        # composing after emitting (matching the SDK adapter's behavior).
        messages: list[dict[str, Any]] = [{"role": "user", "content": context}]

        try:
            async with anthropic.AsyncAnthropic(api_key=api_key) as client:
                for _round in range(MAX_TOOL_ROUNDS):
                    async with client.messages.stream(
                        model=self.model,
                        max_tokens=MAX_TOKENS,
                        thinking={"type": "adaptive"},
                        # Stable prefix cached; context (§8) follows.
                        system=[
                            {
                                "type": "text",
                                "text": f"{system}\n\n{EMIT_COMMANDS_INSTRUCTION}",
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                        tools=wire_tools,
                        messages=messages,
                    ) as stream:
                        async for event in stream:
                            if (
                                event.type == "content_block_delta"
                                and event.delta.type == "text_delta"
                            ):
                                text_parts.append(event.delta.text)
                                await on_text_delta(event.delta.text)
                        final = await stream.get_final_message()

                    input_tokens += final.usage.input_tokens
                    output_tokens += final.usage.output_tokens
                    model_used = final.model

                    if final.stop_reason == "refusal":
                        log.warning(
                            "model refused the turn (stop_details=%s)",
                            final.stop_details,
                        )

                    tool_uses = [b for b in final.content if b.type == "tool_use"]
                    for block in tool_uses:
                        if block.name == "emit_commands":
                            raw_commands = block.input.get("commands")
                    if final.stop_reason != "tool_use":
                        break

                    results = []
                    for block in tool_uses:
                        handler = tool_handlers.get(block.name)
                        if handler is not None:
                            result_text = await handler(dict(block.input or {}))
                        elif block.name == "emit_commands":
                            result_text = "recorded"
                        else:
                            result_text = f"unknown tool {block.name}"
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_text,
                        })
                    messages.append(
                        {"role": "assistant", "content": final.content}
                    )
                    messages.append({"role": "user", "content": results})
                else:
                    log.warning(
                        "tool loop hit MAX_TOOL_ROUNDS=%d; finishing with what we have",
                        MAX_TOOL_ROUNDS,
                    )
        except anthropic.AnthropicError as exc:
            raise _classify(exc) from exc

        notes: list[str] = []
        commands = parse_commands(raw_commands, notes=notes)
        text = "".join(text_parts)
        if notes:
            text = (text + "\n\n" + "\n".join(notes)).strip()
        return TurnResult(
            text=text,
            commands=commands,
            model=model_used,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
