"""Exercise the real SDK against a simulated HTTP transport."""

import asyncio
import json
import signal

import httpx2
import pytest
from anthropic import Anthropic, AsyncAnthropic

from mclaude import cli, provider
from mclaude.config import ModelConfig


def message_events(body):
    """Encode the Messages SSE lifecycle, splitting text and tool JSON."""
    yield {
        "type": "message_start",
        "message": {
            **body,
            "content": [],
            "stop_reason": None,
        },
    }
    for index, block in enumerate(body["content"]):
        is_text = block["type"] == "text"
        yield {
            "type": "content_block_start",
            "index": index,
            "content_block": {
                **block,
                **({"text": ""} if is_text else {"input": {}}),
            },
        }
        value = block["text"] if is_text else json.dumps(block["input"])
        split = max(1, len(value) // 2)
        for piece in (value[:split], value[split:]):
            yield {
                "type": "content_block_delta",
                "index": index,
                "delta": {
                    "type": "text_delta" if is_text else "input_json_delta",
                    "text" if is_text else "partial_json": piece,
                },
            }
        yield {"type": "content_block_stop", "index": index}
    yield {
        "type": "message_delta",
        "delta": {
            "stop_reason": body["stop_reason"],
            "stop_sequence": None,
        },
        "usage": {"output_tokens": 5},
    }
    yield {"type": "message_stop"}


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch):
    state = {
        "requests": [],
        "options": {},
        "status": 200,
        "error": None,
        "events": None,
        "delivered": [],
        "stream_closed": False,
        "failures_remaining": 0,
        "body": {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "test-model",
            "content": [{"type": "text", "text": "Hello from the model"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 3, "output_tokens": 5},
        },
    }

    def handle(request):
        state["requests"].append(request)
        if state["failures_remaining"]:
            failure_status = state["status"]
            state["failures_remaining"] -= 1
            if not state["failures_remaining"]:
                state["status"] = 200
            return httpx2.Response(failure_status, json=state["body"])
        if state["error"]:
            raise state["error"]("simulated failure", request=request)
        if json.loads(request.content).get("stream") and state["status"] == 200:

            class Stream(httpx2.AsyncByteStream):
                async def __aiter__(self):
                    events = state["events"]
                    if events is None:
                        events = message_events(state["body"])
                    for event in events:
                        if isinstance(event, BaseException):
                            raise event
                        state["delivered"].append(event)
                        yield (
                            f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                        ).encode()

                async def aclose(self):
                    state["stream_closed"] = True

            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, stream=Stream()
            )
        return httpx2.Response(state["status"], json=state["body"])

    def factory(**kwargs):
        state["options"] = kwargs
        client = Anthropic(
            **kwargs, http_client=httpx2.Client(transport=httpx2.MockTransport(handle))
        )
        state["client"] = client
        return client

    monkeypatch.setattr(provider, "Anthropic", factory)

    def async_factory(**kwargs):
        state["options"] = kwargs
        client = AsyncAnthropic(
            **kwargs,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)),
        )
        state["client"] = client
        return client

    monkeypatch.setattr(provider, "AsyncAnthropic", async_factory)
    return state


def test_request_and_multiple_text_blocks(api, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.test")
    api["body"]["content"].append({"type": "text", "text": "Second block"})
    config = ModelConfig(api_key="test-secret", model="test-model", max_tokens=50)
    result = provider.complete("你好", config)
    assert result.text == "Hello from the model\nSecond block"
    assert result.truncated is False
    assert len(api["requests"]) == 1
    request = api["requests"][0]
    assert str(request.url) == "https://example.test/v1/messages"
    assert request.headers["x-api-key"] == "test-secret"
    assert json.loads(request.content) == {
        "model": "test-model",
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "你好"}],
    }
    assert api["options"]["timeout"] == 60.0
    assert api["options"]["max_retries"] == 0
    assert api["client"].is_closed()


def test_tool_request_is_sent_and_normalized(api):
    api["body"]["content"] = [
        {"type": "text", "text": "Reading"},
        {
            "type": "tool_use",
            "id": "tool-1",
            "name": "read_file",
            "input": {"path": "README.md"},
        },
    ]
    api["body"]["stop_reason"] = "tool_use"
    tools = [
        {
            "name": "read_file",
            "description": "Read a file",
            "input_schema": {"type": "object"},
        }
    ]

    response = provider.create_message(
        [{"role": "user", "content": "Read the README"}],
        ModelConfig(api_key="test-secret", model="test-model"),
        tools=tools,
    )

    assert response == provider.ModelResponse(
        content=(
            provider.TextBlock("Reading"),
            provider.ToolUseBlock("tool-1", "read_file", {"path": "README.md"}),
        ),
        stop_reason="tool_use",
        input_tokens=3,
        output_tokens=5,
    )
    assert json.loads(api["requests"][0].content)["tools"] == tools


@pytest.mark.parametrize("streaming", [False, True])
def test_saved_endpoint_reaches_both_sdk_clients(api, monkeypatch, streaming):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://environment.test")
    config = ModelConfig(
        api_key="saved-secret", model="saved-model", base_url="https://saved.test/api"
    )
    provider.create_message(
        [{"role": "user", "content": "Hello"}],
        config,
        on_text=(lambda text: None) if streaming else None,
    )
    request = api["requests"][0]
    assert str(request.url) == "https://saved.test/api/v1/messages"
    assert request.headers["x-api-key"] == "saved-secret"


def test_system_prompt_is_sent(api):
    provider.create_message(
        [{"role": "user", "content": "Hello"}],
        ModelConfig(api_key="test-secret", model="test-model"),
        system="Follow AGENTS.md",
    )
    assert json.loads(api["requests"][0].content)["system"] == "Follow AGENTS.md"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, "Invalid request"),
        (401, "Authentication failed"),
        (403, "Access denied"),
        (404, "not found"),
        (429, "rate limit"),
        (500, "unavailable"),
        (529, "unavailable"),
    ],
)
def test_http_errors_are_clear(api, status, expected):
    api["status"] = status
    api["body"] = {
        "type": "error",
        "error": {"type": "api_error", "message": "echoed-secret"},
    }
    with pytest.raises(provider.ModelError, match=expected) as error:
        provider.complete(
            "Hello",
            ModelConfig(api_key="test-secret", model="test", request_retries=0),
        )
    assert f"HTTP {status}" in str(error.value)
    assert "echoed-secret" not in str(error.value)
    assert len(api["requests"]) == 1
    assert api["client"].is_closed()


@pytest.mark.parametrize(
    ("error", "expected"),
    [(httpx2.ReadTimeout, "timed out"), (httpx2.ConnectError, "Cannot connect")],
)
def test_network_errors(api, error, expected):
    api["error"] = error
    with pytest.raises(provider.ModelError, match=expected):
        provider.complete(
            "Hello",
            ModelConfig(api_key="test-secret", model="test", request_retries=0),
        )
    assert len(api["requests"]) == 1


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 529])
def test_transient_status_is_retried_with_backoff(api, monkeypatch, status):
    api["status"] = status
    api["failures_remaining"] = 2
    delays = []
    monkeypatch.setattr(provider.time, "sleep", delays.append)
    response = provider.complete(
        "Hello",
        ModelConfig(
            api_key="test-secret",
            model="test",
            request_retries=2,
            retry_delay=0.25,
        ),
    )
    assert response.text == "Hello from the model"
    assert len(api["requests"]) == 3
    assert delays == [0.25, 0.5]


def test_non_retryable_status_fails_once(api, monkeypatch):
    api["status"] = 401
    monkeypatch.setattr(
        provider.time,
        "sleep",
        lambda delay: pytest.fail("Authentication errors must not be retried"),
    )
    with pytest.raises(provider.ModelError, match="Authentication failed"):
        provider.complete("Hello", ModelConfig(api_key="secret", model="test"))
    assert len(api["requests"]) == 1


def test_stream_retries_before_output_begins(api, monkeypatch):
    api["status"] = 503
    api["failures_remaining"] = 1
    monkeypatch.setattr(provider.time, "sleep", lambda _: None)
    chunks = []
    response = provider.create_message(
        [{"role": "user", "content": "Hello"}],
        ModelConfig(api_key="secret", model="test", request_retries=1),
        on_text=chunks.append,
    )
    assert response.content == (provider.TextBlock("Hello from the model"),)
    assert "".join(chunks) == "Hello from the model"
    assert len(api["requests"]) == 2


@pytest.mark.parametrize("reason", ["tool_use", "refusal", "pause_turn", None])
def test_unexpected_stop_is_not_success(api, reason):
    api["body"]["stop_reason"] = reason
    with pytest.raises(provider.ModelError, match="did not finish"):
        provider.complete("Hello", ModelConfig(api_key="test-secret", model="test"))


def test_empty_response_is_not_success(api):
    api["body"]["content"] = []
    with pytest.raises(provider.ModelError, match="no text"):
        provider.complete("Hello", ModelConfig(api_key="test-secret", model="test"))


def test_cli_request_options(api, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-secret")
    monkeypatch.setenv("ANTHROPIC_MODEL", "env-model")
    assert (
        cli.main(
            ["Hello", "--model", "override", "--max-tokens", "25", "--timeout", "10"]
        )
        == 0
    )
    payload = json.loads(api["requests"][0].content)
    assert payload["model"] == "override"
    assert payload["max_tokens"] == 25
    assert api["options"]["timeout"] == 10.0
    captured = capsys.readouterr()
    assert captured.out == "Hello from the model\n"
    assert captured.err == ""


def test_cli_truncation_keeps_partial_text_and_fails(api, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-secret")
    monkeypatch.setenv("ANTHROPIC_MODEL", "test-model")
    api["body"]["stop_reason"] = "max_tokens"
    assert cli.main(["Hello"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "Hello from the model\n"
    assert "truncated" in captured.err


def test_cli_api_error_has_no_traceback_or_secrets(api, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-secret")
    monkeypatch.setenv("ANTHROPIC_MODEL", "test-model")
    api["status"] = 401
    api["body"] = {"error": {"message": "test-secret"}}
    assert cli.main(["Hello"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Authentication failed" in captured.err
    assert "test-secret" not in captured.err
    assert "Traceback" not in captured.err


def test_interactive_history_reaches_sdk(api, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-secret")
    monkeypatch.setenv("ANTHROPIC_MODEL", "test-model")
    lines = iter(["你好", "请用中文回答", "/exit"])
    monkeypatch.setattr("builtins.input", lambda: next(lines))

    assert cli.main(["--interactive", "--max-iterations", "1"]) == 0
    assert len(api["requests"]) == 2
    assert json.loads(api["requests"][1].content)["messages"] == [
        {"role": "user", "content": "你好"},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello from the model"}],
        },
        {"role": "user", "content": "请用中文回答"},
    ]
    assert capsys.readouterr().out == "Hello from the model\n" * 2


def test_stream_emits_text_before_message_finishes(api):
    api["body"]["content"] = [
        {"type": "text", "text": "你好世界"},
        {"type": "text", "text": "Second block"},
    ]
    chunks = []

    def display(text):
        assert api["delivered"][-1]["type"] != "message_stop"
        chunks.append(text)

    response = provider.create_message(
        [{"role": "user", "content": "Hello"}],
        ModelConfig(api_key="test-secret", model="test"),
        on_text=display,
    )
    assert chunks[:2] == ["你好", "世界"]
    assert "".join(chunks) == "你好世界\nSecond block"
    assert response.content == (
        provider.TextBlock("你好世界"),
        provider.TextBlock("Second block"),
    )
    assert json.loads(api["requests"][0].content)["stream"] is True
    assert api["stream_closed"] and api["client"].is_closed()


def test_stream_tool_arguments_execute_only_after_message_stop(
    api, tmp_path, monkeypatch
):
    from mclaude.agent import run_agent
    from mclaude.tools import ToolResult

    api["body"]["content"] = [
        {"type": "text", "text": "Reading"},
        {
            "type": "tool_use",
            "id": "read",
            "name": "read_file",
            "input": {"path": "notes.txt"},
        },
    ]
    api["body"]["stop_reason"] = "tool_use"
    executions = []

    def execute(block, workspace, **kwargs):
        assert api["delivered"][-1]["type"] == "message_stop"
        assert api["stream_closed"]
        executions.append(block.input)
        api["body"]["content"] = [{"type": "text", "text": "Done"}]
        api["body"]["stop_reason"] = "end_turn"
        return ToolResult("File contents")

    monkeypatch.setattr("mclaude.agent._execute_tool", execute)
    chunks = []
    history = []
    response = run_agent(
        "Read notes",
        ModelConfig(api_key="test-secret", model="test"),
        workspace=tmp_path,
        history=history,
        on_text=chunks.append,
    )
    assert executions == [{"path": "notes.txt"}]
    assert "".join(chunks) == "Reading\nDone\n"
    assert response.text == "Done"
    assert history[2]["content"][0]["tool_use_id"] == "read"


@pytest.mark.parametrize("failure", ["eof", "network", "error", "json"])
def test_failed_stream_preserves_text_without_executing_tools(
    api, failure, monkeypatch
):
    from mclaude.agent import run_agent

    api["body"]["content"] = [
        {"type": "text", "text": "Partial answer"},
        {"type": "tool_use", "id": "read", "name": "read_file", "input": {"path": "x"}},
    ]
    api["body"]["stop_reason"] = "tool_use"
    events = list(message_events(api["body"]))
    if failure == "eof":
        events.pop()
    elif failure == "network":
        events[-1] = httpx2.ReadError("secret raw body")
    elif failure == "error":
        events[-1] = {
            "type": "error",
            "error": {"type": "overloaded_error", "message": "secret"},
        }
    else:
        # The SDK accepts this via its partial JSON parser; the runtime must not.
        for event in events:
            delta = event.get("delta", {})
            if delta.get("partial_json", "").endswith("}"):
                delta["partial_json"] = delta["partial_json"][:-1]
    api["events"] = events

    def unexpected_tool(*args, **kwargs):
        pytest.fail("An incomplete stream must not execute a tool")

    monkeypatch.setattr("mclaude.agent._execute_tool", unexpected_tool)
    chunks = []
    history = []
    with pytest.raises(provider.ModelError) as error:
        run_agent(
            "Read",
            ModelConfig(api_key="test-secret", model="test"),
            history=history,
            on_text=chunks.append,
        )
    assert "secret" not in str(error.value)
    assert len(api["requests"]) == 1
    assert "".join(chunks) == "Partial answer\n"
    assert history[-1] == {
        "role": "assistant",
        "content": [{"type": "text", "text": "Partial answer"}],
    }
    assert api["stream_closed"] and api["client"].is_closed()


def test_cancelled_sdk_stream_closes_connection_and_preserves_partial_text(api):
    from mclaude.agent import run_agent

    events = list(message_events(api["body"]))
    api["events"] = [*events[:3], KeyboardInterrupt()]
    history = []
    chunks = []
    with pytest.raises(KeyboardInterrupt):
        run_agent(
            "Hello",
            ModelConfig(api_key="test-secret", model="test"),
            history=history,
            on_text=chunks.append,
        )
    partial = events[2]["delta"]["text"]
    assert "".join(chunks) == partial + "\n"
    assert history[-1]["content"] == [{"type": "text", "text": partial}]
    assert api["stream_closed"] and api["client"].is_closed()


@pytest.mark.parametrize("waiting_for_headers", [False, True])
def test_ctrl_c_cancels_pending_network_wait(monkeypatch, waiting_for_headers):
    """Deliver a real SIGINT while awaiting an otherwise unbounded response."""
    clients = []
    closed = []
    interrupted_wait = []

    async def stall():
        asyncio.get_running_loop().call_soon(signal.raise_signal, signal.SIGINT)
        try:
            await asyncio.sleep(30)
            pytest.fail("Ctrl+C did not interrupt the network wait")
        except asyncio.CancelledError:
            interrupted_wait.append(True)
            raise

    class Stream(httpx2.AsyncByteStream):
        async def __aiter__(self):
            await stall()
            yield b""

        async def aclose(self):
            closed.append(True)

    async def handle(request):
        if waiting_for_headers:
            await stall()
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=Stream(),
        )

    def factory(**kwargs):
        client = AsyncAnthropic(
            **kwargs,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)),
        )
        clients.append(client)
        return client

    monkeypatch.setattr(provider, "AsyncAnthropic", factory)
    original_handler = signal.getsignal(signal.SIGINT)
    # pytest may install its own interrupt hook; emulate the standalone CLI.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            provider.create_message(
                [{"role": "user", "content": "Hello"}],
                ModelConfig(api_key="test-secret", model="test"),
                on_text=lambda _: None,
            )
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    finally:
        signal.signal(signal.SIGINT, original_handler)
    assert interrupted_wait == [True]
    assert clients[0].is_closed()
    if not waiting_for_headers:
        assert closed == [True]
