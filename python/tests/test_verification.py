"""Checks run in order and can be repeated after a fix."""

from pathlib import Path

from mclaude.agent import run_agent
from mclaude.config import ModelConfig
from mclaude.permissions import PermissionGate
from mclaude.provider import ModelResponse, TextBlock, ToolUseBlock
from mclaude.tools import ToolResult
from mclaude.verification import VerificationWorkflow
from mclaude.workspace_version import workspace_version


def test_passed_checks_bind_to_contents_and_retest(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "mclaude.verification.run_command", lambda *_: ToolResult("passed")
    )
    target = tmp_path / "module.py"
    target.write_text("value = 1\n")
    workflow = VerificationWorkflow()
    workflow.run({"checks": [{"command": "pytest"}]}, tmp_path)
    version = workflow.version
    workflow.refresh(tmp_path)
    assert workflow.state == "passed"
    target.write_text("value = 2\n")
    workflow.refresh(tmp_path)
    assert workflow.state == "stale"
    workflow.run({}, tmp_path, retest=True)
    assert workflow.state == "passed"
    assert workflow.version != version


def test_version_tracks_untracked_files_but_ignores_artifacts(tmp_path):
    (tmp_path / ".gitignore").write_text("dist/\n")
    before = workspace_version(tmp_path)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "build.py").write_text("artifact")
    assert workspace_version(tmp_path) == before
    (tmp_path / "new.py").write_text("source")
    assert workspace_version(tmp_path) != before
    (tmp_path / "new.py").unlink()
    assert workspace_version(tmp_path) == before


def test_check_source_mutation_is_stale_even_when_command_succeeds(
    tmp_path, monkeypatch
):
    def command(*_):
        (tmp_path / "module.py").write_text("changed")
        return ToolResult("passed")

    monkeypatch.setattr("mclaude.verification.run_command", command)
    workflow = VerificationWorkflow()
    result = workflow.run({"checks": [{"command": "format-and-test"}]}, tmp_path)
    assert result.is_error
    assert workflow.state == "stale"
    assert "changed during verification" in result.content


def test_unreadable_ignore_rules_fail_closed(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "mclaude.verification.run_command",
        lambda *_: calls.append(1) or ToolResult("passed"),
    )
    workflow = VerificationWorkflow()
    workflow.run({"checks": [{"command": "pytest"}]}, tmp_path)
    (tmp_path / ".gitignore").write_bytes(b"\xff")
    workflow.refresh(tmp_path)
    assert workflow.state == "stale"
    assert workflow.run({"checks": [{"command": "pytest"}]}, tmp_path).is_error
    assert len(calls) == 1


def test_agent_detects_command_edits_before_next_request(tmp_path, monkeypatch):
    target = tmp_path / "module.py"
    target.write_text("before")
    monkeypatch.setattr(
        "mclaude.verification.run_command", lambda *_: ToolResult("passed")
    )

    def command(*_):
        target.write_text("after")
        return ToolResult("edited")

    monkeypatch.setattr("mclaude.agent.run_command", command)
    responses = iter(
        [
            ModelResponse(
                (
                    ToolUseBlock(
                        "check", "run_checks", {"checks": [{"command": "pytest"}]}
                    ),
                    ToolUseBlock("edit", "run_command", {"command": "format"}),
                ),
                "tool_use",
            ),
            ModelResponse((TextBlock("done"),), "end_turn"),
        ]
    )
    systems = []

    def request(*_, **kwargs):
        systems.append(kwargs.get("system", ""))
        return next(responses)

    workflow = VerificationWorkflow()
    run_agent(
        "check then edit",
        ModelConfig(api_key="test", model="test"),
        workspace=tmp_path,
        request=request,
        permission_gate=PermissionGate(prompt=lambda *_: True),
        allow_host_commands=True,
        verification=workflow,
    )
    assert workflow.state == "stale"
    assert "Development checks: stale" in systems[-1]


def test_source_changes_during_model_response_require_new_response(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "mclaude.verification.run_command", lambda *_: ToolResult("passed")
    )
    target = tmp_path / "module.py"
    target.write_text("before")
    workflow = VerificationWorkflow()
    workflow.run({"checks": [{"command": "pytest"}]}, tmp_path)
    requests = []

    def request(*_, **kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            target.write_text("after")
            return ModelResponse((TextBlock("All checks passed"),), "end_turn")
        assert "Development checks: stale" in kwargs["system"]
        return ModelResponse((TextBlock("Code changed; retest required"),), "end_turn")

    result = run_agent(
        "Report verification",
        ModelConfig(api_key="test", model="test"),
        workspace=tmp_path,
        request=request,
        allow_host_commands=True,
        verification=workflow,
    )
    assert len(requests) == 2
    assert "retest required" in result.text


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
