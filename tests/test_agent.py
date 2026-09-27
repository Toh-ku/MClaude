"""Tests for message history and the minimal agent loop."""

from pathlib import Path

import pytest

from mclaude.agent import run_agent
from mclaude.cancellation import TurnCancelled
from mclaude.config import ModelConfig
from mclaude.context import load_project_instructions
from mclaude.permissions import PermissionAction, PermissionDecision, PermissionGate
from mclaude.provider import ModelError, ModelResponse, TextBlock, ToolUseBlock
from mclaude.tools import ToolResult


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
        "create_file",
        "replace_text",
        "run_command",
        "list_edit_checkpoints",
        "restore_edit_checkpoint",
        "list_tasks",
        "update_tasks",
        "load_skill",
        "delegate_readonly",
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


def test_agent_sends_scoped_project_instructions(
    tmp_path: Path, config: ModelConfig
) -> None:
    (tmp_path / "AGENTS.md").write_text("Always test changes.", encoding="utf-8")
    received = {}

    def request(messages, request_config, *, tools, system):
        received["system"] = system
        return ModelResponse(content=(TextBlock("Done"),), stop_reason="end_turn")

    instructions = load_project_instructions(tmp_path)
    result = run_agent(
        "Work",
        config,
        workspace=tmp_path,
        project_instructions=instructions,
        request=request,
    )

    assert result.text == "Done"
    assert "Always test changes." in received["system"]
    assert "source='AGENTS.md'" in received["system"]


def test_agent_truncates_tool_result_to_context_budget(
    tmp_path: Path, config: ModelConfig, monkeypatch
) -> None:
    responses = iter(
        [
            ModelResponse(
                content=(ToolUseBlock("large", "read_file", {"path": "x"}),),
                stop_reason="tool_use",
            ),
            ModelResponse(content=(TextBlock("Done"),), stop_reason="end_turn"),
        ]
    )
    seen = []

    def request(messages, request_config, *, tools):
        seen.append(messages.copy())
        return next(responses)

    monkeypatch.setattr(
        "mclaude.agent._execute_tool",
        lambda *args, **kwargs: ToolResult("x" * 20_000),
    )
    small_output_config = ModelConfig(
        api_key=config.api_key, model=config.model, max_tokens=50
    )
    result = run_agent(
        "Read",
        small_output_config,
        workspace=tmp_path,
        request=request,
        context_budget_tokens=2_000,
    )
    tool_result = seen[1][-1]["content"][0]
    assert result.text == "Done"
    assert tool_result["is_error"] is True
    assert "truncated to fit context budget" in tool_result["content"]
    assert len(tool_result["content"]) < 20_000


def test_agent_compacts_old_history_before_request(
    tmp_path: Path, config: ModelConfig
) -> None:
    history = [
        {"role": "user", "content": "Original objective " * 200},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "Earlier answer " * 200}],
        },
        {"role": "user", "content": "Recent constraint"},
        {"role": "assistant", "content": [{"type": "text", "text": "Noted"}]},
    ]
    events = []

    def request(messages, request_config, *, tools):
        assert messages[0]["content"].startswith("Use this compacted context")
        assert "Original objective" in messages[1]["content"][0]["text"]
        return ModelResponse(content=(TextBlock("Done"),), stop_reason="end_turn")

    response = run_agent(
        "Continue",
        ModelConfig(api_key=config.api_key, model=config.model, max_tokens=50),
        workspace=tmp_path,
        history=history,
        request=request,
        context_budget_tokens=1_500,
        on_history_event=lambda kind, payload: events.append((kind, payload)),
    )
    assert response.text == "Done"
    assert any(kind == "compaction" for kind, _ in events)


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


def test_agent_asks_before_creating_file(tmp_path: Path, config: ModelConfig) -> None:
    responses = iter(
        [
            ModelResponse(
                content=(
                    ToolUseBlock(
                        "create",
                        "create_file",
                        {"path": "created.txt", "content": "new content\n"},
                    ),
                ),
                stop_reason="tool_use",
            ),
            ModelResponse(content=(TextBlock("Created it."),), stop_reason="end_turn"),
        ]
    )
    prompts = []

    def request(messages, request_config, *, tools):
        return next(responses)

    def approve(permission_request, reason):
        prompts.append((permission_request.tool_name, reason))
        return True

    result = run_agent(
        "Create a file",
        config,
        workspace=tmp_path,
        request=request,
        permission_gate=PermissionGate(prompt=approve),
    )

    assert result.text == "Created it."
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "new content\n"
    assert prompts == [("create_file", "This tool modifies workspace files.")]


def test_agent_asks_before_running_command(
    tmp_path: Path,
    config: ModelConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [
            ModelResponse(
                content=(
                    ToolUseBlock("command", "run_command", {"command": "pytest"}),
                ),
                stop_reason="tool_use",
            ),
            ModelResponse(
                content=(TextBlock("Tests passed."),), stop_reason="end_turn"
            ),
        ]
    )
    prompts = []
    executions = []

    def request(messages, request_config, *, tools):
        return next(responses)

    def approve(permission_request, reason):
        prompts.append((permission_request.tool_name, reason))
        return True

    def execute(tool_input, workspace):
        executions.append((tool_input, workspace))
        return ToolResult("Exit code: 0\n[no output]")

    monkeypatch.setattr("mclaude.agent.run_command", execute)
    result = run_agent(
        "Run tests",
        config,
        workspace=tmp_path,
        request=request,
        permission_gate=PermissionGate(prompt=approve),
    )

    assert result.text == "Tests passed."
    assert prompts == [("run_command", "This tool executes a shell command.")]
    assert executions == [({"command": "pytest"}, tmp_path.resolve())]


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


@pytest.mark.parametrize("failure", ["request", "limit"])
def test_followup_retains_executed_tools_after_failure(
    tmp_path: Path, config: ModelConfig, failure: str
) -> None:
    history = []
    calls = []
    approvals = []

    def request(messages, request_config, *, tools):
        calls.append(messages.copy())
        if len(calls) == 1:
            return ModelResponse(
                (
                    ToolUseBlock(
                        "write", "create_file", {"path": "new.txt", "content": "hi"}
                    ),
                ),
                "tool_use",
            )
        if failure == "request" and len(calls) == 2:
            raise ModelError("Request failed")
        return ModelResponse((TextBlock("Done"),), "end_turn")

    def approve(permission_request, reason):
        approvals.append(permission_request)
        return True

    with pytest.raises(ModelError, match="Request failed|maximum of 1"):
        run_agent(
            "Create a file",
            config,
            history=history,
            workspace=tmp_path,
            request=request,
            max_iterations=1 if failure == "limit" else 2,
            permission_gate=PermissionGate(prompt=approve),
        )

    assert (tmp_path / "new.txt").read_text() == "hi"
    assert len(history) == 3
    assert history[1]["content"][0]["id"] == "write"
    assert history[2]["content"][0]["tool_use_id"] == "write"
    assert history[2]["content"][0]["is_error"] is False
    assert (
        run_agent(
            "Summarize what happened",
            config,
            history=history,
            workspace=tmp_path,
            request=request,
            max_iterations=1,
            permission_gate=PermissionGate(prompt=approve),
        ).text
        == "Done"
    )
    assert len(approvals) == 1
    assert len(history) == 5


def test_truncated_tool_call_is_not_saved_or_executed(config: ModelConfig, monkeypatch):
    history = []

    def unexpected_execution(*args, **kwargs):
        pytest.fail("Truncated tool call was executed")

    monkeypatch.setattr("mclaude.agent._execute_tool", unexpected_execution)
    response = ModelResponse(
        (TextBlock("Partial"), ToolUseBlock("incomplete", "read_file", {})),
        "max_tokens",
    )
    assert run_agent(
        "Question", config, history=history, request=lambda *a, **kw: response
    ).truncated
    assert history[-1] == {
        "role": "assistant",
        "content": [{"type": "text", "text": "Partial"}],
    }


def test_conversations_are_independent_and_budget_resets(config: ModelConfig):
    histories = [[], []]
    calls = []

    def request(messages, request_config, *, tools):
        calls.append(messages.copy())
        return ModelResponse((TextBlock("Answer"),), "end_turn")

    for history, prompt in [
        (histories[0], "First"),
        (histories[0], "Followup"),
        (histories[1], "Separate"),
    ]:
        run_agent(prompt, config, history=history, request=request, max_iterations=1)
    assert len(calls[1]) == 3
    assert calls[2] == [{"role": "user", "content": "Separate"}]


def test_agent_reports_request_context_and_api_token_usage(config: ModelConfig):
    events = []

    response = ModelResponse(
        (TextBlock("Answer"),),
        "end_turn",
        input_tokens=321,
        output_tokens=12,
    )
    run_agent(
        "Question",
        config,
        request=lambda *args, **kwargs: response,
        on_status_event=lambda kind, payload: events.append((kind, payload)),
    )

    assert events[0][0] == "request.started"
    assert events[0][1]["iteration"] == 1
    assert events[0][1]["context_tokens"] > 0
    assert events[0][1]["output_tokens"] == config.max_tokens
    assert events[1] == (
        "response.received",
        {
            "stop_reason": "end_turn",
            "input_tokens": 321,
            "output_tokens": 12,
        },
    )


@pytest.mark.parametrize("during_permission", [False, True])
def test_cancelled_tool_batch_keeps_results_and_skips_remaining_calls(
    config, tmp_path, monkeypatch, during_permission
):
    history = []
    executions = []
    responses = iter(
        [
            ModelResponse(
                (
                    ToolUseBlock("first", "read_file", {"path": "a"}),
                    ToolUseBlock("active", "run_command", {"command": "test"}),
                    ToolUseBlock("remaining", "read_file", {"path": "b"}),
                ),
                "tool_use",
            ),
            ModelResponse((TextBlock("Continued"),), "end_turn"),
        ]
    )

    def request(messages, request_config, *, tools):
        return next(responses)

    def execute(call, workspace, **kwargs):
        executions.append(call.id)
        if call.id == "active":
            raise TurnCancelled(
                "Command cancelled; process tree terminated.\nstdout: partial"
            )
        assert call.id == "first"
        return ToolResult("Original contents")

    def approve(request, reason):
        if during_permission:
            raise KeyboardInterrupt
        return True

    monkeypatch.setattr("mclaude.agent._execute_tool", execute)
    with pytest.raises(KeyboardInterrupt):
        run_agent(
            "Run tools",
            config,
            history=history,
            workspace=tmp_path,
            request=request,
            permission_gate=PermissionGate(prompt=approve),
        )
    assert executions == (["first"] if during_permission else ["first", "active"])
    results = history[-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["first", "active", "remaining"]
    assert results[0]["content"] == "Original contents"
    assert [r["is_error"] for r in results] == [False, True, True]
    if during_permission:
        assert "Not executed" in results[1]["content"]
    else:
        assert "stdout: partial" in results[1]["content"]
    assert "Not executed" in results[2]["content"]
    assert (
        run_agent("Continue", config, history=history, request=request).text
        == "Continued"
    )
    assert len(history) == 5


def test_agent_emits_durable_history_events_before_and_after_tool(config, tmp_path):
    events = []
    responses = iter(
        [
            ModelResponse(
                (ToolUseBlock("read", "read_file", {"path": "notes.txt"}),),
                "tool_use",
            ),
            ModelResponse((TextBlock("Done"),), "end_turn"),
        ]
    )
    (tmp_path / "notes.txt").write_text("saved", encoding="utf-8")

    result = run_agent(
        "Read it",
        config,
        workspace=tmp_path,
        request=lambda *args, **kwargs: next(responses),
        on_history_event=lambda kind, payload: events.append((kind, payload)),
    )

    assert result.text == "Done"
    assert [kind for kind, _ in events] == [
        "message",
        "message",
        "tool_result",
        "message",
    ]
    assert events[1][1]["content"][0]["id"] == "read"
    assert events[2][1]["content"] == "saved"
