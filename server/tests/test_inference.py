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


def test_dropped_commands_surface_honest_notes():
    """A schema-invalid command must not vanish silently: the model may have
    claimed success in prose (live bug: LogWeight emitted as weight_kg=90 was
    dropped while the reply said "Logged today's weight")."""
    notes: list[str] = []
    parsed = parse_commands(
        [
            {"type": "LogWeight", "weight_kg": 90},  # wrong field names
            {"type": "AppendEvent", "kind": "note", "note": "kept"},
        ],
        notes=notes,
    )

    assert len(parsed) == 1
    assert len(notes) == 1
    assert notes[0].startswith("(couldn't complete LogWeight:")
    assert "value" in notes[0] and "unit" in notes[0]

    # Without the out-param the behavior is unchanged (40+ call sites).
    assert len(parse_commands([{"type": "LogWeight", "weight_kg": 90}])) == 0


def test_garbage_payloads_mean_no_commands():
    assert parse_commands(None) == []
    assert parse_commands("not json{") == []
    assert parse_commands({"commands": "nested wrong"}) == []
    assert parse_commands('[{"type": "Notify", "body": "hi"}]') != []  # JSON string ok


def test_schema_has_no_forbidden_commands():
    schema = commands_json_schema()

    rendered = str(schema)
    assert "DeleteCalendarEvent" not in rendered  # §9.1: deliberately absent
    assert "CreateCalendarEvent" not in rendered  # §12: the calendar is read-only
    assert "emit" not in schema["properties"]["commands"]["items"].get("title", "")
    assert schema["required"] == ["commands"]


def test_emit_commands_tool_is_the_only_write_tool():
    """recall (read-only) joined it post-spec; writes still go one way."""
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
    # codex-cli >= 0.144 removed --ask-for-approval from exec (non-interactive by default)
    assert "--ask-for-approval" not in argv
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


def test_every_gateway_accepts_the_full_protocol_signature():
    """Signature-drift guard: a kwarg added to the protocol must land on EVERY
    implementation, wrappers included — ProfileRoutingGateway sits between the
    worker and the adapters, and missing `images` there produced a live
    TypeError in prod (2026-07-23)."""
    import inspect

    from inkling.inference.anthropic_api import AnthropicAPIGateway
    from inkling.inference.anthropic_sdk import AnthropicSDKGateway
    from inkling.inference.codex_exec import CodexExecGateway
    from inkling.inference.gateway import InferenceGateway

    protocol_params = set(
        inspect.signature(InferenceGateway.run_turn).parameters
    ) - {"self"}
    for cls in (
        AnthropicSDKGateway, AnthropicAPIGateway, CodexExecGateway,
        ProfileRoutingGateway, StubGateway,
    ):
        params = set(inspect.signature(cls.run_turn).parameters) - {"self"}
        missing = protocol_params - params
        assert not missing, f"{cls.__name__}.run_turn missing {sorted(missing)}"


# --- SDK adapter stream handling (fake query; no CLI, no network) ----------

def _sdk_script(monkeypatch, steps):
    """Run AnthropicSDKGateway against a scripted stream. Steps: ("text", s)
    yields a text_delta StreamEvent, ("emit", cmds) calls the emit_commands
    handler the adapter registered, ("assistant", s) yields an assembled
    AssistantMessage, ("result",) a successful ResultMessage."""
    import claude_agent_sdk as sdk

    captured = {}

    def fake_server(*, name, version, tools):
        captured["tools"] = {t.name: t for t in tools}
        return object()

    async def fake_query(*, prompt, options):
        for step in steps:
            kind = step[0]
            if kind == "text":
                yield sdk.StreamEvent(
                    uuid="u", session_id="s",
                    event={"type": "content_block_delta",
                           "delta": {"type": "text_delta", "text": step[1]}},
                )
            elif kind == "emit":
                await captured["tools"]["emit_commands"].handler({"commands": step[1]})
            elif kind == "assistant":
                yield sdk.AssistantMessage(content=[sdk.TextBlock(text=step[1])], model="m")
            elif kind == "result":
                yield sdk.ResultMessage(
                    subtype="success", duration_ms=1, duration_api_ms=1,
                    is_error=False, num_turns=1, session_id="s",
                )

    monkeypatch.setattr(sdk, "create_sdk_mcp_server", fake_server)
    monkeypatch.setattr(sdk, "query", fake_query)


async def _sdk_run():
    from inkling.inference.anthropic_sdk import AnthropicSDKGateway

    deltas: list[str] = []

    async def on_delta(chunk: str) -> None:
        deltas.append(chunk)

    result = await AnthropicSDKGateway("/nonexistent").run_turn(
        user_id=USER_A, system="sys", context="ctx", tools=[],
        on_text_delta=on_delta,
    )
    return result, "".join(deltas)


async def test_sdk_tool_first_reply_streams_in_full(monkeypatch):
    # Live regression: emit_commands first, then the reply — the reply was
    # cut to its first delta ("Got it") by the restatement guard.
    _sdk_script(monkeypatch, [
        ("emit", []),
        ("text", "Got it"), ("text", " — logged"), ("text", " your run."),
        ("assistant", "Got it — logged your run."),
        ("result",),
    ])
    result, streamed = await _sdk_run()
    assert streamed == "Got it — logged your run."
    assert result.text == "Got it — logged your run."


async def test_sdk_reply_first_drops_the_post_emit_restatement(monkeypatch):
    _sdk_script(monkeypatch, [
        ("text", "Done,"), ("text", " marked."),
        ("emit", []),
        ("text", "Marked it done for you."),
        ("result",),
    ])
    result, streamed = await _sdk_run()
    assert streamed == "Done, marked."
    assert result.text == "Done, marked."
