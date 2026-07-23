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

emit_commands validates mid-turn when the worker provides its
"validate_commands" handler: rejection reasons become the tool result, the
model corrects and re-emits during the turn, and only accepted commands reach
the TurnResult — so the user sees the fixed outcome, not a "(couldn't
complete …)" §9.2 note after the fact. TTFT is untouched (the reply streamed
before the emit); the correction round costs one extra model request only on
turns that actually rejected something.
"""

import asyncio
import json
import logging
from typing import Any

from inkling.inference.gateway import (
    EMIT_COMMANDS_INSTRUCTION,
    ImageAttachment,
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
# bounds it so a confused model can't loop the subscription forever. Photo
# turns get headroom for scan_barcode → lookup_product before the reply.
MAX_TURNS = 4
MAX_TURNS_WITH_IMAGES = 6

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
        images: list[ImageAttachment] | None = None,
    ) -> TurnResult:
        return await with_one_retry(
            lambda: self._run(
                system=system,
                context=context,
                on_text_delta=on_text_delta,
                tool_handlers=tool_handlers or {},
                source=source,
                images=images or [],
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
        images: list[ImageAttachment],
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
        # False after an emit whose commands were (partly) rejected: the early
        # exit below must not fire until the model has re-emitted clean —
        # cutting the turn there would strand the correction round.
        emit_clean = False
        validator = tool_handlers.get("validate_commands")

        # Full JSON schema, not {"commands": list}: with a bare untyped list
        # the model guesses field names, and (pre extra=forbid) a wrong guess
        # on an all-optional command parsed as an EMPTY command. Seen live:
        # SetTargets arriving as {} on the first prod turn.
        @tool(
            "emit_commands",
            "Record this turn's state-changing commands. Call exactly once per "
            "turn; pass an empty list if the turn changes nothing. Text you "
            "write after this call is discarded unless you wrote none before "
            "it — write your reply first, then call this. If the result lists "
            "rejections, call it once more with corrected replacements for "
            "only the rejected commands — everything recorded stays recorded.",
            commands_json_schema(),
        )
        async def emit_commands(args: dict[str, Any]) -> dict[str, Any]:
            nonlocal emitted, emit_clean
            emitted = True
            raw = args.get("commands") or []
            if validator is None:
                raw_commands.extend(raw)
                emit_clean = True
                return {"content": [{"type": "text", "text": "recorded"}]}
            try:
                verdict = json.loads(await validator({"commands": raw}))
            except Exception:
                # Validation is an optimisation; commit_turn re-validates
                # authoritatively. Degrade to record-and-ack, never fail the
                # tool call over it.
                log.exception("mid-turn command validation failed; recording raw")
                raw_commands.extend(raw)
                emit_clean = True
                return {"content": [{"type": "text", "text": "recorded"}]}
            raw_commands.extend(verdict["accepted"])
            rejections = verdict["rejections"]
            if not rejections:
                emit_clean = True
                return {"content": [{"type": "text", "text": "recorded"}]}
            emit_clean = False
            text = (
                f"{len(verdict['accepted'])} recorded; "
                f"{len(rejections)} rejected:\n" + "\n".join(rejections) +
                "\nCall emit_commands again with corrected replacements for "
                "only the rejected commands — everything recorded stays "
                "recorded. Do not mention this correction to the user."
            )
            return {"content": [{"type": "text", "text": text}]}

        sdk_tools = [emit_commands]
        allowed = ["mcp__inkling__emit_commands"]

        scan_handler = tool_handlers.get("scan_barcode")
        if scan_handler is not None:
            @tool(
                "scan_barcode",
                "Machine-decode any barcode in the attached photo. Returns "
                "each code found with its symbology (the UPC/EAN is the "
                "product code; DataMatrix/QR are usually production codes).",
                {},
            )
            async def scan_barcode(args: dict[str, Any]) -> dict[str, Any]:
                result = await scan_handler(args)
                return {"content": [{"type": "text", "text": result}]}

            sdk_tools.append(scan_barcode)
            allowed.append("mcp__inkling__scan_barcode")

        lookup_handler = tool_handlers.get("lookup_product")
        if lookup_handler is not None:
            @tool(
                "lookup_product",
                "Look up a barcode in the food product database. Returns "
                "product name and nutrition (per serving AND per 100g).",
                {"barcode": str},
            )
            async def lookup_product(args: dict[str, Any]) -> dict[str, Any]:
                result = await lookup_handler(args)
                return {"content": [{"type": "text", "text": result}]}

            sdk_tools.append(lookup_product)
            allowed.append("mcp__inkling__lookup_product")

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
            max_turns=MAX_TURNS_WITH_IMAGES if images else MAX_TURNS,
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

        # A plain string prompt has nowhere to carry image content blocks, so
        # photo turns switch to the SDK's streaming input mode: one user
        # message whose content is the images followed by the assembled
        # context. Text-only turns keep the string path untouched.
        if images:
            async def _prompt_messages():
                yield {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            *[
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": image.media_type,
                                        "data": image.data_base64,
                                    },
                                }
                                for image in images
                            ],
                            {"type": "text", "text": context},
                        ],
                    },
                    "parent_tool_use_id": None,
                    "session_id": "default",
                }

            prompt: Any = _prompt_messages()
        else:
            prompt = context

        # Break-out needs an explicit aclose(): abandoning an async generator
        # only schedules cleanup at GC time, which would leave the CLI
        # subprocess running (and generating the restatement we broke out to
        # avoid) for an unbounded while.
        stream = query(prompt=prompt, options=options)
        try:
            async for message in stream:
                # Early exit: the reply exists and emit_commands has run CLEAN,
                # so the turn has everything it will ever contribute. What
                # remains is the model restating the reply post-tool (seen on
                # every live SDK turn, discarded on arrival before this
                # existed) — end the turn instead of paying a model request
                # for it. A rejected emit holds the exit open for the
                # correction round. Costs the ResultMessage: usage stays 0 on
                # early-exit turns.
                if emitted and emit_clean and (text_parts or buffer):
                    await flush()
                    break
                if isinstance(message, StreamEvent):
                    event = message.event
                    if (
                        message.parent_tool_use_id is None
                        and event.get("type") == "content_block_delta"
                        and event.get("delta", {}).get("type") == "text_delta"
                    ):
                        # One reply phase only: during a correction round the
                        # model may narrate ("fixed, re-emitting") — none of
                        # that is for the user, whose reply already streamed.
                        # Post-emit text counts only when there was none before.
                        if emitted and text_parts:
                            continue
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
