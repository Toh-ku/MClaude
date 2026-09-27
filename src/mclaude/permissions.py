"""Permission decisions applied before every tool execution."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class PermissionAction(StrEnum):
    """The action selected by a tool permission policy."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class PermissionRequest:
    """The tool call being considered for execution."""

    tool_name: str
    tool_input: object


@dataclass(frozen=True)
class PermissionDecision:
    """A policy decision and the reason shown on denial or confirmation."""

    action: PermissionAction
    reason: str


PermissionPolicy = Callable[[PermissionRequest], PermissionDecision]
PermissionPrompt = Callable[[PermissionRequest, str], bool]

READ_ONLY_TOOLS = frozenset({"read_file", "find_files", "search_text"})


def default_permission_policy(request: PermissionRequest) -> PermissionDecision:
    """Allow known read-only tools and reject everything else."""
    if request.tool_name in READ_ONLY_TOOLS:
        return PermissionDecision(
            PermissionAction.ALLOW, "Known read-only workspace tool."
        )
    return PermissionDecision(
        PermissionAction.DENY,
        "The tool is not included in the configured permission policy.",
    )


@dataclass
class PermissionGate:
    """Resolve allow, ask, and deny policies to a final execution decision."""

    policy: PermissionPolicy = default_permission_policy
    prompt: PermissionPrompt | None = None

    def check(self, request: PermissionRequest) -> PermissionDecision:
        """Return ALLOW or DENY, failing closed if policy evaluation cannot finish."""
        try:
            decision = self.policy(request)
        except Exception as exc:
            return PermissionDecision(
                PermissionAction.DENY,
                f"Permission policy failed ({type(exc).__name__}).",
            )

        if not isinstance(decision, PermissionDecision):
            return PermissionDecision(
                PermissionAction.DENY,
                "Permission policy returned an invalid decision.",
            )
        if decision.action is PermissionAction.ALLOW:
            return decision
        if decision.action is PermissionAction.DENY:
            return decision
        if decision.action is not PermissionAction.ASK:
            return PermissionDecision(
                PermissionAction.DENY,
                "Permission policy returned an invalid action.",
            )
        if self.prompt is None:
            return PermissionDecision(
                PermissionAction.DENY,
                f"{decision.reason} No permission prompt is available.",
            )

        try:
            approved = self.prompt(request, decision.reason)
        except Exception as exc:
            return PermissionDecision(
                PermissionAction.DENY,
                f"Permission prompt failed ({type(exc).__name__}).",
            )
        if not isinstance(approved, bool):
            return PermissionDecision(
                PermissionAction.DENY,
                "Permission prompt returned an invalid response.",
            )
        if approved:
            return PermissionDecision(
                PermissionAction.ALLOW, f"{decision.reason} Approved by the user."
            )
        return PermissionDecision(
            PermissionAction.DENY, f"{decision.reason} Denied by the user."
        )
