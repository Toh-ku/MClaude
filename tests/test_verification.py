"""Checks run in order and can be repeated after a fix."""

from pathlib import Path

from mclaude.agent import run_agent
from mclaude.config import ModelConfig
from mclaude.permissions import PermissionGate
from mclaude.provider import ModelResponse, TextBlock, ToolUseBlock
from mclaude.tools import ToolResult
from mclaude.verification import VerificationWorkflow


def test_verification_stops_at_failure_and_retests(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def command(arguments, workspace):
        calls.append(arguments["command"])
        if len(calls) == 1:
            return ToolResult("Exit code: 1\nstderr:\nassertion failed", True)
        return ToolResult("Exit code: 0\n[no output]")

    monkeypatch.setattr("mclaude.verification.run_command", command)
    workflow = VerificationWorkflow()
    checks = {"checks": [{"command": "pytest"}, {"command": "ruff check ."}]}

    failed = workflow.run(checks, tmp_path)
    assert failed.is_error is True
    assert calls == ["pytest"]
    assert "assertion failed" in failed.content
    assert workflow.state == "failed"

    passed = workflow.run({}, tmp_path, retest=True)
    assert passed.is_error is False
    assert calls == ["pytest", "pytest", "ruff check ."]
    assert workflow.state == "passed"
    assert workflow.attempts == 2

    workflow.mark_edited()
    assert workflow.state == "stale"


def test_retest_requires_prior_checks(tmp_path: Path) -> None:
    workflow = VerificationWorkflow()
    result = workflow.run({}, tmp_path, retest=True)

    assert result.is_error is True
    assert "No prior checks" in result.content


def test_agent_approves_retest_commands_and_reports_passed_state(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    responses = iter(
        [
            ModelResponse(
                (
                    ToolUseBlock(
                        "checks", "run_checks", {"checks": [{"command": "pytest"}]}
                    ),
                ),
                "tool_use",
            ),
            ModelResponse(
                (
                    ToolUseBlock(
                        "fix",
                        "replace_text",
                        {"path": "module.py", "old_text": "1", "new_text": "2"},
                    ),
                ),
                "tool_use",
            ),
            ModelResponse((ToolUseBlock("retest", "retest_checks", {}),), "tool_use"),
            ModelResponse((TextBlock("Fixed and verified."),), "end_turn"),
        ]
    )
    commands = []
    approvals = []
    systems = []

    def command(arguments, workspace):
        commands.append(arguments["command"])
        if len(commands) == 1:
            return ToolResult("Exit code: 1\nassertion failed", True)
        return ToolResult("Exit code: 0\nall passed")

    def request(messages, config, *, tools, **kwargs):
        systems.append(kwargs.get("system", ""))
        return next(responses)

    monkeypatch.setattr("mclaude.verification.run_command", command)
    workflow = VerificationWorkflow()
    result = run_agent(
        "Fix the test failure",
        ModelConfig(api_key="test", model="test"),
        workspace=tmp_path,
        request=request,
        permission_gate=PermissionGate(
            prompt=lambda item, reason: approvals.append(item) or True
        ),
        allow_host_commands=True,
        verification=workflow,
    )

    assert result.text == "Fixed and verified."
    assert commands == ["pytest", "pytest"]
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert [item.tool_name for item in approvals] == [
        "run_checks",
        "replace_text",
        "retest_checks",
    ]
    assert approvals[-1].tool_input == {"checks": [{"command": "pytest"}]}
    assert "Development checks: failed" in systems[1]
    assert "Development checks: passed" in systems[-1]
    assert workflow.state == "passed"
