"""Tests for tool permission policy resolution."""

from mclaude.permissions import (
    PermissionAction,
    PermissionDecision,
    PermissionGate,
    PermissionRequest,
    default_permission_policy,
)


def test_default_policy_allows_known_read_only_tools() -> None:
    for tool_name in (
        "read_file",
        "find_files",
        "search_text",
        "list_edit_checkpoints",
    ):
        decision = default_permission_policy(PermissionRequest(tool_name, {}))

        assert decision.action is PermissionAction.ALLOW


def test_default_policy_denies_unknown_tools() -> None:
    decision = default_permission_policy(PermissionRequest("unknown_tool", {}))

    assert decision.action is PermissionAction.DENY
    assert "not included" in decision.reason


def test_default_policy_asks_before_file_edits() -> None:
    for tool_name in ("create_file", "replace_text", "restore_edit_checkpoint"):
        decision = default_permission_policy(PermissionRequest(tool_name, {}))

        assert decision.action is PermissionAction.ASK
        assert "modifies workspace files" in decision.reason


def test_default_policy_asks_before_commands() -> None:
    decision = default_permission_policy(PermissionRequest("run_command", {}))

    assert decision.action is PermissionAction.ASK
    assert "shell command" in decision.reason


def test_gate_returns_direct_allow_and_deny_decisions() -> None:
    request = PermissionRequest("test_tool", {"value": 1})
    allow = PermissionDecision(PermissionAction.ALLOW, "safe")
    deny = PermissionDecision(PermissionAction.DENY, "blocked")

    assert PermissionGate(policy=lambda _: allow).check(request) == allow
    assert PermissionGate(policy=lambda _: deny).check(request) == deny


def test_gate_asks_and_resolves_user_approval() -> None:
    request = PermissionRequest("write_file", {"path": "notes.txt"})
    seen = []

    def prompt(prompt_request: PermissionRequest, reason: str) -> bool:
        seen.append((prompt_request, reason))
        return True

    gate = PermissionGate(
        policy=lambda _: PermissionDecision(PermissionAction.ASK, "Changes a file."),
        prompt=prompt,
    )

    decision = gate.check(request)

    assert decision.action is PermissionAction.ALLOW
    assert "Approved by the user" in decision.reason
    assert seen == [(request, "Changes a file.")]


def test_gate_asks_and_resolves_user_denial() -> None:
    gate = PermissionGate(
        policy=lambda _: PermissionDecision(PermissionAction.ASK, "Runs a command."),
        prompt=lambda request, reason: False,
    )

    decision = gate.check(PermissionRequest("run_command", {"command": "pytest"}))

    assert decision.action is PermissionAction.DENY
    assert "Denied by the user" in decision.reason


def test_gate_fails_closed_when_asking_is_unavailable() -> None:
    gate = PermissionGate(
        policy=lambda _: PermissionDecision(PermissionAction.ASK, "Needs approval.")
    )

    decision = gate.check(PermissionRequest("write_file", {}))

    assert decision.action is PermissionAction.DENY
    assert "No permission prompt" in decision.reason


def test_gate_fails_closed_when_policy_or_prompt_fails() -> None:
    def fail_policy(request: PermissionRequest) -> PermissionDecision:
        raise RuntimeError("policy details")

    policy_failure = PermissionGate(policy=fail_policy).check(
        PermissionRequest("tool", {})
    )

    def fail_prompt(request: PermissionRequest, reason: str) -> bool:
        raise EOFError

    prompt_failure = PermissionGate(
        policy=lambda _: PermissionDecision(PermissionAction.ASK, "Needs approval."),
        prompt=fail_prompt,
    ).check(PermissionRequest("tool", {}))

    assert policy_failure.action is PermissionAction.DENY
    assert policy_failure.reason == "Permission policy failed (RuntimeError)."
    assert prompt_failure.action is PermissionAction.DENY
    assert prompt_failure.reason == "Permission prompt failed (EOFError)."
