"""Subscription adapter: Claude Agent SDK with a per-user config dir (§7, D7).

Each user's turns run on their own subscription credentials: CLAUDE_CONFIG_DIR
points at the profile's config_dir (`{"type": "subscription", "config_dir":
"/home/user/.inkling/claude-<user>"}`), so one user is never routed through
another's subscription.

The SDK ships Claude Code's whole toolset; this adapter's job is mostly to
turn that off. Tools are restricted to the single in-process emit_commands
tool (an SDK MCP server), and permission_mode="dontAsk" denies anything not
explicitly allowed — the model gets no filesystem, no bash, no network.

Streaming is token-level (include_partial_messages), batched into ~quarter-
second chunks so turn_events stays at a handful of rows per second rather than
one per token (§7's turn_events contract is unchanged). The turn also ends as
soon as the reply text and the emit_commands call both exist — the model's
habitual post-tool restatement (discarded anyway) is never generated, saving a
whole model request per turn. Chat-source turns run at effort="low": the reply
is conversational, and think time is pure added latency in front of the first
visible token.
"""

import asyncio
import logging
from typing import Any

from inkling.inference.gateway import (
    EMIT_COMMANDS_INSTRUCTION,
    InferenceError,
    OnTextDelta,
    ToolHandler,
    ToolSchema,
    TurnResult,
    with_one_retry,
)
from inkling.turns.commands import commands_json_schema, parse_commands

log = logging.getLogger(__name__)

# The model runs its inner agent loop (reply → emit_commands → wrap up); this
# bounds it so a confused model can't loop the subscription forever.
MAX_TURNS = 4

# Live text is batched by time window: one on_text_delta call (= one
# turn_events row, one DB transaction in the worker) per flush, not per token.
FLUSH_SECONDS = 0.25


class AnthropicSDKGateway:
    def __init__(self, config_dir: str, *, model: str | None = None):
        self.config_dir = config_dir
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
        source: str = "chat",
    ) -> TurnResult:
        return await with_one_retry(
            lambda: self._run(
                system=system,
                context=context,
                on_text_delta=on_text_delta,
                tool_handlers=tool_handlers or {},
                source=source,
            )
        )

    async def _run(
        self,
        *,
        system: str,
        context: str,
        on_text_delta: OnTextDelta,
        tool_handlers: dict[str, ToolHandler],
        source: str,
    ) -> TurnResult:
        # Imported here so the api_key/stub paths never require the package.
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            StreamEvent,
            TextBlock,
            ToolUseBlock,
            create_sdk_mcp_server,
            query,
            tool,
        )

        # Captured by the tool handler; created per turn so concurrent turns
        # (different users, different Worker tasks) never share state.
        raw_commands: list[Any] = []
        emitted = False

        # Full JSON schema, not {"commands": list}: with a bare untyped list
        # the model guesses field names, and (pre extra=forbid) a wrong guess
        # on an all-optional command parsed as an EMPTY command. Seen live:
        # SetTargets arriving as {} on the first prod turn.
        @tool(
            "emit_commands",
            "Record this turn's state-changing commands. Call exactly once per "
            "turn; pass an empty list if the turn changes nothing. Text you "
            "write after this call is discarded unless you wrote none before "
            "it — write your reply first, then call this.",
            commands_json_schema(),
        )
        async def emit_commands(args: dict[str, Any]) -> dict[str, Any]:
            nonlocal emitted
            emitted = True
            raw_commands.extend(args.get("commands") or [])
            return {"content": [{"type": "text", "text": "recorded"}]}

        sdk_tools = [emit_commands]
        allowed = ["mcp__inkling__emit_commands"]

        recall_handler = tool_handlers.get("recall")
        if recall_handler is not None:
            @tool(
                "recall",
                "Search your memory (past events and conversation) when the "
                "current context is not enough. Read-only.",
                {"query": str},
            )
            async def recall(args: dict[str, Any]) -> dict[str, Any]:
                result = await recall_handler(args)
                return {"content": [{"type": "text", "text": result}]}

            sdk_tools.append(recall)
            allowed.append("mcp__inkling__recall")

        server = create_sdk_mcp_server(name="inkling", version="1.0.0", tools=sdk_tools)

        options = ClaudeAgentOptions(
            system_prompt=f"{system}\n\n{EMIT_COMMANDS_INSTRUCTION}",
            mcp_servers={"inkling": server},
            allowed_tools=allowed,
            # Deny-by-default: file/bash/web tools are never approved (§7).
            permission_mode="dontAsk",
            env={"CLAUDE_CONFIG_DIR": self.config_dir},
            max_turns=MAX_TURNS,
            model=self.model,
            include_partial_messages=True,
            # Chat is latency-sensitive: the user is watching a stream, and
            # think time lands entirely before the first visible token. Other
            # sources (wakeup, occurrence, consolidation) keep the model's
            # default adaptive thinking — nobody is waiting on those.
            effort="low" if source == "chat" else None,
        )

        text_parts: list[str] = []
        model_used = self.model or "claude-agent-sdk"
        input_tokens = 0
        output_tokens = 0

        loop = asyncio.get_running_loop()
        buffer: list[str] = []
        last_flush = loop.time()
        streamed = False  # any live text_delta seen (partial-message path)

        async def flush() -> None:
            nonlocal last_flush
            if buffer:
                chunk = "".join(buffer)
                buffer.clear()
                text_parts.append(chunk)
                await on_text_delta(chunk)
            last_flush = loop.time()

        # Break-out needs an explicit aclose(): abandoning an async generator
        # only schedules cleanup at GC time, which would leave the CLI
        # subprocess running (and generating the restatement we broke out to
        # avoid) for an unbounded while.
        stream = query(prompt=context, options=options)
        try:
            async for message in stream:
                # Early exit: the reply exists and emit_commands has run, so
                # the turn has everything it will ever contribute. What remains
                # is the model restating the reply post-tool (seen on every
                # live SDK turn, discarded on arrival before this existed) —
                # end the turn instead of paying a model request for it. Costs
                # the ResultMessage: usage stays 0 on early-exit turns.
                if emitted and (text_parts or buffer):
                    await flush()
                    break
                if isinstance(message, StreamEvent):
                    event = message.event
                    if (
                        message.parent_tool_use_id is None
                        and event.get("type") == "content_block_delta"
                        and event.get("delta", {}).get("type") == "text_delta"
                    ):
                        streamed = True
                        buffer.append(event["delta"]["text"])
                        if loop.time() - last_flush >= FLUSH_SECONDS:
                            await flush()
                elif isinstance(message, AssistantMessage):
                    # ResultMessage carries no model attr — AssistantMessage is
                    # where the SDK names the actual model (turn 68 logged the
                    # bare gateway label; the answer was a live prod query away).
                    model_used = message.model or model_used
                    await flush()
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text:
                            # Already delivered token-by-token; the assembled
                            # block would double every reply.
                            if streamed:
                                continue
                            # Fallback path (no partial events, e.g. an older
                            # CLI): block-level streaming, one reply phase only
                            # — the model restates the reply post-tool no
                            # matter what the prompt says. Keep the first
                            # non-empty phase.
                            if emitted and text_parts:
                                log.debug("dropping post-emit restatement")
                                continue
                            text_parts.append(block.text)
                            await on_text_delta(block.text)
                        elif isinstance(block, ToolUseBlock):
                            log.debug("sdk tool call: %s", block.name)
                elif isinstance(message, ResultMessage):
                    if message.subtype != "success":
                        # Execution errors are transient (crashed CLI, dropped
                        # stream); config errors would fail again identically,
                        # but the SDK folds both into non-success subtypes —
                        # one retry is cheap and the second failure is fatal.
                        raise InferenceError(
                            f"sdk turn ended with {message.subtype}", retryable=True
                        )
                    usage = message.usage
                    if usage is not None:
                        get = (
                            usage.get
                            if isinstance(usage, dict)
                            else lambda k, d=0: getattr(usage, k, d)
                        )
                        input_tokens = int(get("input_tokens", 0) or 0)
                        output_tokens = int(get("output_tokens", 0) or 0)
        except InferenceError:
            raise
        except Exception as exc:
            # SDK/transport failures (CLI missing, connection lost). Retryable:
            # the CLI is respawned per call, so transient failures can clear.
            raise InferenceError(f"{type(exc).__name__}: {exc}", retryable=True) from exc
        finally:
            try:
                await stream.aclose()
            except Exception:
                log.debug("sdk stream close raised", exc_info=True)

        if not emitted:
            log.warning("model never called emit_commands; treating as no commands")

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
