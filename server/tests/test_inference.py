"""Inference gateway machinery (§7) and command schema (§9.1) — no network.

Adapters' provider calls are exercised in production, not here; what these
tests pin is everything deterministic around them: schema validation, the
emit_commands tool shape, error classification, retry policy, and profile
routing.
"""

import anthropic
import httpx
import pytest

from inkling.inference.anthropic_api import _classify
from inkling.inference.gateway import (
    InferenceError,
    StubGateway,
    TurnResult,
    emit_commands_tool,
    with_one_retry,
)
from inkling.inference.profile import ProfileRoutingGateway
from inkling.turns.commands import (
    AppendEvent,
    UpsertCommitment,
    commands_json_schema,
    parse_commands,
)
from tests.conftest import USER_A


async def _sink(text: str) -> None:
    pass


# --- §9.1 command schema ------------------------------------------------------


def test_valid_commands_parse_into_typed_objects():
    parsed = parse_commands(
        [
            {"type": "AppendEvent", "kind": "habit_done", "habit_id": "run", "note": "5k"},
            {"type": "UpsertCommitment", "title": "call dentist", "due_at": "2026-07-18T15:00:00Z"},
        ]
    )

    assert isinstance(parsed[0], AppendEvent)
    assert parsed[0].kind == "habit_done"
    assert isinstance(parsed[1], UpsertCommitment)
    assert parsed[1].due_at is not None


def test_invalid_commands_are_dropped_not_fatal():
    parsed = parse_commands(
        [
            {"type": "DeleteCalendarEvent", "id": 1},  # deliberately not a command
            {"type": "AppendEvent", "kind": "note", "note": "kept"},
            {"type": "AppendEvent", "kind": "not_a_kind", "note": "bad enum"},
            "not even an object",
        ]
    )

    assert len(parsed) == 1
    assert parsed[0].note == "kept"


def test_garbage_payloads_mean_no_commands():
    assert parse_commands(None) == []
    assert parse_commands("not json{") == []
    assert parse_commands({"commands": "nested wrong"}) == []
    assert parse_commands('[{"type": "Notify", "body": "hi"}]') != []  # JSON string ok


def test_schema_has_no_forbidden_commands():
    schema = commands_json_schema()

    rendered = str(schema)
    assert "DeleteCalendarEvent" not in rendered  # §9.1: deliberately absent
    assert "emit" not in schema["properties"]["commands"]["items"].get("title", "")
    assert schema["required"] == ["commands"]


def test_emit_commands_tool_is_the_only_tool():
    tool = emit_commands_tool()

    assert tool.name == "emit_commands"
    assert tool.input_schema["properties"]["commands"]["type"] == "array"


# --- §7 error classification and retry ---------------------------------------


def _status_error(status: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request, json={"error": {"message": "x"}})
    return anthropic.APIStatusError("boom", response=response, body=None)


def test_5xx_is_retryable_4xx_is_fatal():
    assert _classify(_status_error(500)).retryable is True
    assert _classify(_status_error(529)).retryable is True
    assert _classify(_status_error(400)).retryable is False
    assert _classify(_status_error(401)).retryable is False
    assert _classify(_status_error(404)).retryable is False


def test_connection_errors_are_retryable():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    exc = anthropic.APIConnectionError(request=request)
    assert _classify(exc).retryable is True


async def test_retryable_error_gets_exactly_one_retry():
    attempts = 0

    async def flaky() -> TurnResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise InferenceError("overloaded", retryable=True)
        return TurnResult(text="ok", commands=[], model="m", input_tokens=1, output_tokens=1)

    import inkling.inference.gateway as gw

    original = gw.RETRY_BACKOFF_SECONDS
    gw.RETRY_BACKOFF_SECONDS = 0.0
    try:
        result = await with_one_retry(flaky)
    finally:
        gw.RETRY_BACKOFF_SECONDS = original

    assert result.text == "ok"
    assert attempts == 2


async def test_fatal_error_does_not_retry():
    attempts = 0

    async def broken() -> TurnResult:
        nonlocal attempts
        attempts += 1
        raise InferenceError("bad auth", retryable=False)

    with pytest.raises(InferenceError):
        await with_one_retry(broken)
    assert attempts == 1


async def test_second_failure_propagates():
    async def always_down() -> TurnResult:
        raise InferenceError("still down", retryable=True)

    import inkling.inference.gateway as gw

    original = gw.RETRY_BACKOFF_SECONDS
    gw.RETRY_BACKOFF_SECONDS = 0.0
    try:
        with pytest.raises(InferenceError, match="still down"):
            await with_one_retry(always_down)
    finally:
        gw.RETRY_BACKOFF_SECONDS = original


# --- codex_exec reply splitting (§7) ------------------------------------------


def test_split_reply_extracts_commands_and_strips_block():
    from inkling.inference.codex_exec import split_reply

    raw = (
        "Logged your run — nice pace.\n\n"
        "```emit_commands\n"
        '{"commands": [{"type": "AppendEvent", "kind": "habit_done", '
        '"habit_id": "run", "note": "5k"}]}\n'
        "```\n"
    )

    text, commands = split_reply(raw)

    assert text == "Logged your run — nice pace."
    assert len(commands) == 1
    assert commands[0].type == "AppendEvent"


def test_split_reply_without_block_is_no_commands():
    from inkling.inference.codex_exec import split_reply

    text, commands = split_reply("Just chatting, nothing to record.")

    assert text == "Just chatting, nothing to record."
    assert commands == []


def test_split_reply_invalid_json_keeps_text_drops_commands():
    from inkling.inference.codex_exec import split_reply

    text, commands = split_reply("Reply.\n\n```emit_commands\n{not json\n```")

    assert text == "Reply."
    assert commands == []


def test_split_reply_uses_only_the_last_block():
    from inkling.inference.codex_exec import split_reply

    raw = (
        "Here is how a block looks:\n"
        "```emit_commands\n"
        '{"commands": [{"type": "Notify", "body": "quoted example"}]}\n'
        "```\n"
        "But the real one is:\n"
        "```emit_commands\n"
        '{"commands": []}\n'
        "```\n"
    )

    text, commands = split_reply(raw)

    assert commands == []
    assert "quoted example" in text  # the earlier block stays in the prose


def test_codex_argv_is_sandboxed_and_non_interactive():
    from inkling.inference.codex_exec import CodexExecGateway

    argv = CodexExecGateway("/home/u/.inkling/codex", model="gpt-5.4")._argv(
        "/tmp/scratch", "/tmp/scratch/last.txt"
    )

    assert argv[:2] == ["codex", "exec"]
    assert ("--sandbox", "read-only") == tuple(argv[argv.index("--sandbox") : argv.index("--sandbox") + 2])
    assert ("--ask-for-approval", "never") == tuple(argv[argv.index("--ask-for-approval") : argv.index("--ask-for-approval") + 2])
    assert "--ephemeral" in argv
    assert "--skip-git-repo-check" in argv
    assert ("--model", "gpt-5.4") == tuple(argv[argv.index("--model") : argv.index("--model") + 2])
    assert argv[-1] == "-"  # prompt on stdin


# --- profile routing (D7) -----------------------------------------------------


async def test_unknown_profile_type_is_fatal(db):
    gateway = ProfileRoutingGateway(db)

    # conftest seeds USER_A with {"type": "api_key", "key_file": "/dev/null"};
    # an empty key file is a fatal InferenceError from the api adapter, which
    # proves routing reached the right adapter without any network call.
    with pytest.raises(InferenceError) as excinfo:
        await gateway.run_turn(
            user_id=USER_A,
            system="s",
            context="c",
            tools=[emit_commands_tool()],
            on_text_delta=_sink,
        )
    assert excinfo.value.retryable is False
    assert "empty api key" in str(excinfo.value)


async def test_stub_gateway_matches_the_protocol():
    stub = StubGateway("hello", chunk_size=2)
    seen: list[str] = []

    async def collect(text: str) -> None:
        seen.append(text)

    result = await stub.run_turn(
        user_id="u", system="s", context="c", tools=[emit_commands_tool()], on_text_delta=collect
    )

    assert "".join(seen) == "hello"
    assert result.text == "hello"
    assert result.model == "stub"
