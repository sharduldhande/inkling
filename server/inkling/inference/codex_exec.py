"""Codex CLI adapter: `codex exec` with per-user ChatGPT-subscription auth (§7, D7).

CODEX_HOME points at the user's profile dir, so credentials (from `codex
login`) resolve per user — never shared. The subprocess is non-interactive
and sandboxed: `--sandbox read-only` (no writes anywhere), approvals `never`,
working directory pinned to a per-call scratch dir, `--ephemeral` so no
session files persist.

Codex has no tool-call channel we control, so commands arrive as a fenced
``emit_commands`` JSON block in the reply text (instructed via the prompt),
extracted here, stripped from the user-facing text, and Pydantic-validated by
parse_commands exactly like the tool-call adapters — missing or invalid JSON
means "no commands" plus a log line (§7).

Streaming granularity is the coarsest allowed by §7: the whole reply arrives
as one on_text_delta call. The final text comes from `--output-last-message`
(a documented, stable contract) rather than parsing the --json event stream,
whose schema is experimental. Token counts are not exposed by the CLI; they
are recorded as 0.
"""

import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from inkling.inference.gateway import (
    InferenceError,
    OnTextDelta,
    ToolSchema,
    TurnResult,
    with_one_retry,
)
from inkling.turns.commands import Command, commands_json_schema, parse_commands

log = logging.getLogger(__name__)

TURN_TIMEOUT_SECONDS = 300

EMIT_BLOCK_INSTRUCTION = (
    "At the very end of your reply, append a fenced code block tagged "
    "emit_commands containing a JSON object {\"commands\": [...]} with every "
    "state-changing command this turn requires — an empty list if none. "
    "Commands are the only way anything is recorded; never claim an action "
    "you did not emit a command for. Everything before that block is shown "
    "to the user; the block itself is not. Commands must match this JSON "
    "Schema exactly — invalid commands are dropped, so invent no field "
    "names:\n"
)


def emit_block_instruction() -> str:
    """The prose instruction plus the real command schema.

    The tool-call adapters hand the model commands_json_schema() as the
    emit_commands tool definition; without the same schema in its prompt the
    codex model guesses field names (seen live: LogWeight{weight_kg}).
    """
    return EMIT_BLOCK_INSTRUCTION + json.dumps(commands_json_schema(), separators=(",", ":"))

_EMIT_BLOCK = re.compile(r"```emit_commands\s*\n(.*?)```\s*", re.DOTALL)


def split_reply(raw: str) -> tuple[str, list[Command]]:
    """Split codex output into user-facing text and validated commands.

    Only the LAST emit_commands block counts (the model was told to append it);
    earlier ones — e.g. quoted in prose — are left in the text untouched.
    """
    matches = list(_EMIT_BLOCK.finditer(raw))
    if not matches:
        log.warning("codex reply has no emit_commands block; treating as no commands")
        return raw.strip(), []

    last = matches[-1]
    text = (raw[: last.start()] + raw[last.end() :]).strip()
    try:
        payload: Any = json.loads(last.group(1))
    except json.JSONDecodeError:
        log.warning("emit_commands block is not valid JSON; treating as no commands")
        return text, []
    if isinstance(payload, dict):
        payload = payload.get("commands")
    notes: list[str] = []
    commands = parse_commands(payload, notes=notes)
    if notes:
        text = (text + "\n\n" + "\n".join(notes)).strip()
    return text, commands


class CodexExecGateway:
    def __init__(self, profile_dir: str, *, model: str | None = None):
        self.profile_dir = profile_dir
        self.model = model

    async def run_turn(
        self,
        *,
        user_id: str,
        system: str,
        context: str,
        tools: list[ToolSchema],
        on_text_delta: OnTextDelta,
        tool_handlers: dict[str, Any] | None = None,
        source: str = "chat",  # accepted for the protocol; no per-source tuning here
    ) -> TurnResult:
        # A one-shot subprocess has no mid-turn tool channel: recall is
        # unavailable on this adapter, and §8's deterministic retrieval gate
        # is the floor it falls back to (documented degradation, not an error).
        if tool_handlers:
            log.debug("codex adapter ignores tool handlers: %s", list(tool_handlers))
        return await with_one_retry(
            lambda: self._run(system=system, context=context, on_text_delta=on_text_delta)
        )

    def _argv(self, scratch: str, last_message_path: str) -> list[str]:
        argv = [
            "codex",
            "exec",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "--cd",
            scratch,
            "--color",
            "never",
            "--output-last-message",
            last_message_path,
        ]
        if self.model:
            argv += ["--model", self.model]
        argv.append("-")  # prompt on stdin
        return argv

    async def _run(
        self, *, system: str, context: str, on_text_delta: OnTextDelta
    ) -> TurnResult:
        prompt = f"{system}\n\n{emit_block_instruction()}\n\n{context}"

        with tempfile.TemporaryDirectory(prefix="inkling-codex-") as scratch:
            last_message_path = str(Path(scratch) / "last-message.txt")
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self._argv(scratch, last_message_path),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    env={**os.environ, "CODEX_HOME": self.profile_dir},
                )
            except FileNotFoundError as exc:
                raise InferenceError(
                    "codex CLI not installed on this host", retryable=False
                ) from exc

            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(prompt.encode()), timeout=TURN_TIMEOUT_SECONDS
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                raise InferenceError(
                    f"codex exec exceeded {TURN_TIMEOUT_SECONDS}s", retryable=True
                ) from None

            if proc.returncode != 0:
                tail = stderr.decode(errors="replace")[-500:]
                # Exit codes don't distinguish auth from transient failures;
                # one retry is cheap and a config error fails identically fast.
                raise InferenceError(
                    f"codex exec exited {proc.returncode}: {tail}", retryable=True
                )

            try:
                raw = Path(last_message_path).read_text()
            except FileNotFoundError:
                raise InferenceError(
                    "codex exec wrote no last message", retryable=True
                ) from None

        text, commands = split_reply(raw)
        if text:
            await on_text_delta(text)

        return TurnResult(
            text=text,
            commands=commands,
            model=self.model or "codex",
            input_tokens=0,  # not exposed by the CLI
            output_tokens=0,
        )
