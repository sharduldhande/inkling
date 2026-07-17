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
    ToolSchema,
    TurnResult,
    with_one_retry,
)
from inkling.turns.commands import parse_commands

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-4-8"
MAX_TOKENS = 16000


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
    ) -> TurnResult:
        return await with_one_retry(
            lambda: self._run(system=system, context=context, tools=tools, on_text_delta=on_text_delta)
        )

    async def _run(
        self, *, system: str, context: str, tools: list[ToolSchema], on_text_delta: OnTextDelta
    ) -> TurnResult:
        api_key = Path(self.key_file).read_text().strip()
        if not api_key:
            raise InferenceError(f"empty api key file {self.key_file}", retryable=False)

        # The tool schemas carry additionalProperties via Pydantic, but the
        # strict guarantee needs it explicit at every object level; Pydantic's
        # json_schema already sets it on models. `strict` gives schema-valid
        # tool input; parse_commands still validates because strict tool use
        # doesn't cover our discriminated-union semantics end to end.
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

        try:
            async with anthropic.AsyncAnthropic(api_key=api_key) as client:
                async with client.messages.stream(
                    model=self.model,
                    max_tokens=MAX_TOKENS,
                    thinking={"type": "adaptive"},
                    # Stable prefix cached; context (§8, stable→volatile) follows.
                    system=[
                        {
                            "type": "text",
                            "text": f"{system}\n\n{EMIT_COMMANDS_INSTRUCTION}",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    tools=wire_tools,
                    messages=[{"role": "user", "content": context}],
                ) as stream:
                    async for event in stream:
                        if (
                            event.type == "content_block_delta"
                            and event.delta.type == "text_delta"
                        ):
                            text_parts.append(event.delta.text)
                            await on_text_delta(event.delta.text)
                    final = await stream.get_final_message()
        except anthropic.AnthropicError as exc:
            raise _classify(exc) from exc

        for block in final.content:
            if block.type == "tool_use" and block.name == "emit_commands":
                raw_commands = block.input.get("commands")

        if final.stop_reason == "refusal":
            log.warning("model refused the turn (stop_details=%s)", final.stop_details)

        return TurnResult(
            text="".join(text_parts),
            commands=parse_commands(raw_commands),
            model=final.model,
            input_tokens=final.usage.input_tokens,
            output_tokens=final.usage.output_tokens,
        )
