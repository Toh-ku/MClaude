"""Independent contexts, enforced tool boundaries and bounded delegation."""

import copy

from mclaude.agent import run_agent
from mclaude.config import ModelConfig
from mclaude.permissions import PermissionAction, PermissionDecision, PermissionGate
from mclaude.provider import ModelResponse, TextBlock, ToolUseBlock
from mclaude.subagents import SubagentRunner

CONFIG = ModelConfig(api_key="test", model="test")


def test_child_context_is_independent_and_forbidden_tools_do_not_run(tmp_path):
    (tmp_path / "note").write_text("evidence")
    responses = iter(
        [
            ModelResponse(
                (
                    ToolUseBlock(
                        "delegate", "delegate_readonly", {"prompt": "investigate"}
                    ),
                ),
                "tool_use",
            ),
            ModelResponse(
                (
                    ToolUseBlock("read", "read_file", {"path": "note"}),
                    ToolUseBlock(
                        "edit", "create_file", {"path": "bad", "content": "bad"}
                    ),
                    ToolUseBlock("nest", "delegate_readonly", {"prompt": "recurse"}),
                    ToolUseBlock("cmd", "run_command", {"command": "echo bad"}),
                ),
                "tool_use",
            ),
            ModelResponse((TextBlock("child report"),), "end_turn"),
            ModelResponse((TextBlock("parent answer"),), "end_turn"),
        ]
    )
    requests = []

    def request(messages, config, **kwargs):
        requests.append((copy.deepcopy(messages), kwargs))
        return next(responses)

    parent_history = []
    result = run_agent(
        "PARENT_PRIVATE_CONTEXT",
        CONFIG,
        workspace=tmp_path,
        history=parent_history,
        request=request,
        permission_gate=PermissionGate(
            policy=lambda req: PermissionDecision(PermissionAction.ALLOW, "yes")
        ),
    )
    assert result.text == "parent answer"
    assert "PARENT_PRIVATE_CONTEXT" not in repr(requests[1][0])
    assert requests[1][0] == [{"role": "user", "content": "investigate"}]
    assert {t["name"] for t in requests[1][1]["tools"]} == {
        "read_file",
        "find_files",
        "search_text",
    }
    child_results = requests[2][0][2]["content"]
    assert child_results[0]["content"] == "evidence"
    assert all(item["is_error"] for item in child_results[1:])
    assert not (tmp_path / "bad").exists()
    assert "child report" in repr(parent_history)
    assert "evidence" not in repr(parent_history)


def test_delegations_share_budget_and_do_not_recurse(tmp_path):
    runner = SubagentRunner(1)
    calls = []

    def request(*args, **kwargs):
        calls.append(1)
        return ModelResponse((TextBlock("done"),), "end_turn")

    kwargs = dict(
        config=CONFIG,
        workspace=tmp_path,
        request=request,
        permission_gate=PermissionGate(),
        context_budget_tokens=100_000,
        max_file_chars=100_000,
    )
    assert not runner.execute({"prompt": "first"}, **kwargs).is_error
    assert runner.execute({"prompt": "second"}, **kwargs).is_error
    assert len(calls) == 1
    assert SubagentRunner(5, depth=1).execute({"prompt": "nested"}, **kwargs).is_error


def test_child_iteration_limit_and_validation(tmp_path):
    runner = SubagentRunner(8)
    calls = []

    def request(*args, **kwargs):
        calls.append(1)
        return ModelResponse(
            (ToolUseBlock(str(len(calls)), "find_files", {"pattern": "*"}),), "tool_use"
        )

    kwargs = dict(
        config=CONFIG,
        workspace=tmp_path,
        request=request,
        permission_gate=PermissionGate(),
        context_budget_tokens=100_000,
        max_file_chars=100_000,
    )
    result = runner.execute({"prompt": "loop", "max_iterations": 2}, **kwargs)
    assert result.is_error and "maximum of 2" in result.content
    assert len(calls) == 2 and runner.remaining_requests == 6
    for value in [0, 5, True]:
        assert runner.execute(
            {"prompt": "bad", "max_iterations": value}, **kwargs
        ).is_error
