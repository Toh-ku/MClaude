"""Tests for message history and the minimal agent loop."""

from pathlib import Path

import pytest

from mclaude.agent import run_agent
from mclaude.config import ModelConfig
from mclaude.permissions import PermissionAction, PermissionDecision, PermissionGate
from mclaude.provider import ModelError, ModelResponse, TextBlock, ToolUseBlock


@pytest.fixture
def config() -> ModelConfig:
    return ModelConfig(api_key="test-secret", model="test-model")


def test_agent_reads_file_and_returns_final_answer(
    tmp_path: Path, config: ModelConfig
) -> None:
    (tmp_path / "notes.txt").write_text("important content", encoding="utf-8")
    calls = []
    responses = iter(
        [
            ModelResponse(
                content=(
                    TextBlock("I will inspect it."),
                    ToolUseBlock("tool-1", "read_file", {"path": "notes.txt"}),
                ),
                stop_reason="tool_use",
            ),
            ModelResponse(
                content=(TextBlock("The file contains important content."),),
                stop_reason="end_turn",
            ),
        ]
    )

    def request(messages, request_config, *, tools):
        calls.append((messages.copy(), request_config, tools))
        return next(responses)

    result = run_agent(
        "Summarize notes.txt", config, workspace=tmp_path, request=request
    )

    assert result.text == "The file contains important content."
    assert result.truncated is False
    assert len(calls) == 2
    assert calls[0][0] == [{"role": "user", "content": "Summarize notes.txt"}]
    assert [tool["name"] for tool in calls[0][2]] == [
        "read_file",
        "find_files",
        "search_text",
    ]
    assert calls[1][0][1] == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "I will inspect it."},
            {
                "type": "tool_use",
                "id": "tool-1",
                "name": "read_file",
                "input": {"path": "notes.txt"},
            },
        ],
    }
    assert calls[1][0][2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "tool-1",
                "content": "important content",
                "is_error": False,
            }
        ],
    }


def test_multiple_tool_calls_execute_in_order_and_return_failures(
    tmp_path: Path, config: ModelConfig
) -> None:
    (tmp_path / "first.txt").write_text("first", encoding="utf-8")
    seen_messages = []
    responses = iter(
        [
            ModelResponse(
                content=(
                    ToolUseBlock("a", "read_file", {"path": "first.txt"}),
                    ToolUseBlock("b", "read_file", {"path": "missing.txt"}),
                ),
                stop_reason="tool_use",
            ),
            ModelResponse(content=(TextBlock("Done"),), stop_reason="end_turn"),
        ]
    )

    def request(messages, request_config, *, tools):
        seen_messages.append(messages.copy())
        return next(responses)

    assert (
        run_agent("Read both", config, workspace=tmp_path, request=request).text
        == "Done"
    )
    results = seen_messages[1][-1]["content"]
    assert [result["tool_use_id"] for result in results] == ["a", "b"]
    assert results[0]["content"] == "first"
    assert results[0]["is_error"] is False
    assert "not found" in results[1]["content"]
    assert results[1]["is_error"] is True


def test_agent_executes_workspace_search_tools(
    tmp_path: Path, config: ModelConfig
) -> None:
    (tmp_path / "module.py").write_text("def target():\n    pass\n", encoding="utf-8")
    seen_messages = []
    responses = iter(
        [
            ModelResponse(
                content=(
                    ToolUseBlock("find", "find_files", {"pattern": "*.py"}),
                    ToolUseBlock("search", "search_text", {"query": "target"}),
                ),
                stop_reason="tool_use",
            ),
            ModelResponse(content=(TextBlock("Found it"),), stop_reason="end_turn"),
        ]
    )

    def request(messages, request_config, *, tools):
        seen_messages.append(messages.copy())
        return next(responses)

    result = run_agent("Locate target", config, workspace=tmp_path, request=request)

    assert result.text == "Found it"
    tool_results = seen_messages[1][-1]["content"]
    assert tool_results[0]["content"] == "module.py"
    assert tool_results[1]["content"] == "module.py:1:def target():"


def test_agent_does_not_execute_denied_tool_and_returns_reason(
    tmp_path: Path, config: ModelConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = iter(
        [
            ModelResponse(
                content=(ToolUseBlock("blocked", "read_file", {"path": "secret"}),),
                stop_reason="tool_use",
            ),
            ModelResponse(
                content=(TextBlock("Permission was denied."),), stop_reason="end_turn"
            ),
        ]
    )
    seen_messages = []

    def request(messages, request_config, *, tools):
        seen_messages.append(messages.copy())
        return next(responses)

    def unexpected_execution(*args, **kwargs):
        pytest.fail("Denied tool was executed")

    monkeypatch.setattr("mclaude.agent._execute_tool", unexpected_execution)
    gate = PermissionGate(
        policy=lambda _: PermissionDecision(
            PermissionAction.DENY, "This path is restricted."
        )
    )

    result = run_agent(
        "Read the secret",
        config,
        workspace=tmp_path,
        request=request,
        permission_gate=gate,
    )

    assert result.text == "Permission was denied."
    tool_result = seen_messages[1][-1]["content"][0]
    assert tool_result == {
        "type": "tool_result",
        "tool_use_id": "blocked",
        "content": ("Permission denied for tool 'read_file': This path is restricted."),
        "is_error": True,
    }


def test_agent_preserves_partial_text_when_output_is_truncated(
    config: ModelConfig,
) -> None:
    response = ModelResponse(
        content=(TextBlock("partial answer"),), stop_reason="max_tokens"
    )

    result = run_agent("Question", config, request=lambda *args, **kwargs: response)

    assert result.text == "partial answer"
    assert result.truncated is True


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (ModelResponse(content=(), stop_reason="end_turn"), "no text"),
        (ModelResponse(content=(), stop_reason="max_tokens"), "truncated"),
        (ModelResponse(content=(), stop_reason="tool_use"), "without a tool call"),
        (ModelResponse(content=(), stop_reason="refusal"), "unexpectedly"),
        (
            ModelResponse(
                content=(ToolUseBlock("a", "read_file", {"path": "x"}),),
                stop_reason="end_turn",
            ),
            "ended while requesting",
        ),
    ],
)
def test_agent_rejects_invalid_stops(
    config: ModelConfig, response: ModelResponse, error: str
) -> None:
    with pytest.raises(ModelError, match=error):
        run_agent("Question", config, request=lambda *args, **kwargs: response)


def test_agent_enforces_iteration_limit(config: ModelConfig) -> None:
    response = ModelResponse(
        content=(ToolUseBlock("a", "unknown", {}),), stop_reason="tool_use"
    )

    with pytest.raises(ModelError, match="maximum of 2"):
        run_agent(
            "Question",
            config,
            max_iterations=2,
            request=lambda *args, **kwargs: response,
        )
