"""Exercise the real SDK against a simulated HTTP transport."""

import json

import httpx2
import pytest
from anthropic import Anthropic

from mclaude import cli, provider
from mclaude.config import ModelConfig


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch):
    state = {
        "requests": [],
        "options": {},
        "status": 200,
        "error": None,
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
        if state["error"]:
            raise state["error"]("simulated failure", request=request)
        return httpx2.Response(state["status"], json=state["body"])

    def factory(**kwargs):
        state["options"] = kwargs
        client = Anthropic(
            **kwargs, http_client=httpx2.Client(transport=httpx2.MockTransport(handle))
        )
        state["client"] = client
        return client

    monkeypatch.setattr(provider, "Anthropic", factory)
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
    )
    assert json.loads(api["requests"][0].content)["tools"] == tools


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
def test_http_errors_are_clear_and_not_retried(api, status, expected):
    api["status"] = status
    api["body"] = {
        "type": "error",
        "error": {"type": "api_error", "message": "echoed-secret"},
    }
    with pytest.raises(provider.ModelError, match=expected) as error:
        provider.complete("Hello", ModelConfig(api_key="test-secret", model="test"))
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
        provider.complete("Hello", ModelConfig(api_key="test-secret", model="test"))
    assert len(api["requests"]) == 1


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
