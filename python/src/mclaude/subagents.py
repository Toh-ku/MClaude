"""Read-only investigations with fresh histories and a shared delegation budget."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mclaude.config import ModelConfig
from mclaude.permissions import PermissionGate
from mclaude.provider import ModelError
from mclaude.tools import ToolResult

INVESTIGATION_TOOLS = frozenset({"read_file", "find_files", "search_text"})
DELEGATE_DEFINITION = {
    "name": "delegate_readonly",
    "description": (
        "Delegate a focused read-only investigation with an independent context. "
        "Supply all relevant context in the prompt. Returns only the final report."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "maxLength": 8000},
            "max_iterations": {"type": "integer", "minimum": 1, "maximum": 4},
        },
        "required": ["prompt"],
        "additionalProperties": False,
    },
}


@dataclass
class SubagentRunner:
    remaining_requests: int = 8
    depth: int = 0

    def execute(
        self,
        arguments: object,
        *,
        config: ModelConfig,
        workspace: Path,
        request: Any,
        permission_gate: PermissionGate,
        context_budget_tokens: int,
        max_file_chars: int,
    ) -> ToolResult:
        from mclaude.agent import run_agent

        if self.depth >= 1:
            return ToolResult("Subagent recursion limit reached.", True)
        if not isinstance(arguments, dict) or set(arguments) - {
            "prompt",
            "max_iterations",
        }:
            return ToolResult("Invalid delegation arguments.", True)
        prompt = arguments.get("prompt")
        iterations = arguments.get("max_iterations", 4)
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8000:
            return ToolResult("Subagent prompt must be 1-8,000 characters.", True)
        if type(iterations) is not int or not 1 <= iterations <= 4:
            return ToolResult("Subagent max_iterations must be 1-4.", True)
        if self.remaining_requests <= 0:
            return ToolResult("Subagent request budget exhausted for this turn.", True)

        def child_request(*args, **kwargs):
            if self.remaining_requests <= 0:
                raise ModelError("Subagent request budget exhausted for this turn.")
            self.remaining_requests -= 1
            return request(*args, **kwargs)

        try:
            response = run_agent(
                prompt,
                config,
                workspace=workspace,
                max_iterations=min(iterations, self.remaining_requests),
                max_file_chars=max_file_chars,
                request=child_request,
                permission_gate=permission_gate,
                history=[],
                planning=True,
                allowed_tools=INVESTIGATION_TOOLS,
                subagent_budget=0,
                subagent_depth=self.depth + 1,
                context_budget_tokens=context_budget_tokens,
                on_text=lambda text: None,
            )
        except ModelError as exc:
            return ToolResult(f"Subagent failed: {exc}", True)
        if len(response.text) > 16_000:
            return ToolResult(
                response.text[:16_000] + "\n[Subagent report truncated]", True
            )
        return ToolResult(response.text, response.truncated)
