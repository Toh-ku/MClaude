"""Explicit run, fix, and retest workflow for development checks."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mclaude.tools import ToolResult, run_command

RUN_CHECKS_DEFINITION = {
    "name": "run_checks",
    "description": (
        "Run up to five approved development checks in order, stopping at the "
        "first failure. Inspect the failure, edit files, then call retest_checks "
        "to run the same checks again. Host commands must be enabled."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "checks": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "path": {"type": "string"},
                        "timeout_seconds": {
                            "type": "number",
                            "exclusiveMinimum": 0,
                            "maximum": 600,
                        },
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["checks"],
        "additionalProperties": False,
    },
}

RETEST_CHECKS_DEFINITION = {
    "name": "retest_checks",
    "description": (
        "Rerun the same development checks after inspecting a failure and "
        "making a fix. The user will be asked to approve the commands again."
    ),
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}

VERIFICATION_DEFINITIONS = [RUN_CHECKS_DEFINITION, RETEST_CHECKS_DEFINITION]
EDIT_TOOLS = frozenset(
    {"create_file", "replace_text", "apply_edits", "restore_edit_checkpoint"}
)


@dataclass
class VerificationWorkflow:
    checks: list[dict[str, Any]] = field(default_factory=list)
    attempts: int = 0
    state: str = "not_run"

    def current_checks(self) -> list[dict[str, Any]]:
        return [check.copy() for check in self.checks]

    def mark_edited(self) -> None:
        if self.state == "passed":
            self.state = "stale"

    def summary(self) -> str:
        if self.state == "not_run":
            return "No development checks have been run in this conversation."
        commands = "; ".join(check["command"] for check in self.checks)
        return (
            f"Development checks: {self.state} after {self.attempts} run(s). "
            f"Commands: {commands}. If failed or stale, inspect/fix and call "
            "retest_checks before reporting verification as passed."
        )

    def run(
        self, arguments: object, workspace: Path, *, retest: bool = False
    ) -> ToolResult:
        if retest:
            if not isinstance(arguments, dict) or arguments:
                return ToolResult("retest_checks input must be an empty object.", True)
            if not self.checks:
                return ToolResult("No prior checks are available to retest.", True)
            checks = self.current_checks()
        else:
            if not isinstance(arguments, dict) or set(arguments) != {"checks"}:
                return ToolResult("run_checks requires only a checks array.", True)
            checks = arguments["checks"]
            if not isinstance(checks, list) or not 1 <= len(checks) <= 5:
                return ToolResult("run_checks requires 1-5 checks.", True)
            for index, check in enumerate(checks, 1):
                if not isinstance(check, dict) or set(check) - {
                    "command",
                    "path",
                    "timeout_seconds",
                }:
                    return ToolResult(f"Check {index} has invalid fields.", True)
                if (
                    not isinstance(check.get("command"), str)
                    or not check["command"].strip()
                ):
                    return ToolResult(f"Check {index} needs a command.", True)
            checks = [check.copy() for check in checks]

        self.checks = checks
        self.attempts += 1
        outputs = []
        for index, check in enumerate(checks, 1):
            result = run_command(check, workspace)
            outputs.append(
                f"Check {index}/{len(checks)}: {check['command']}\n{result.content}"
            )
            if result.is_error:
                self.state = "failed"
                return ToolResult(
                    f"Verification attempt {self.attempts} failed. "
                    "Inspect the output, fix the issue, then call retest_checks.\n\n"
                    + "\n\n".join(outputs),
                    True,
                )
        self.state = "passed"
        return ToolResult(
            f"Verification attempt {self.attempts} passed.\n\n" + "\n\n".join(outputs)
        )
