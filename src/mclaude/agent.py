"""Minimal model/tool loop for a single user task."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mclaude.config import ModelConfig
from mclaude.provider import (
    ModelError,
    ModelResponse,
    TextBlock,
    ToolUseBlock,
    create_message,
)
from mclaude.tools import (
    TOOL_DEFINITIONS,
    ToolResult,
    find_files,
    read_file,
    search_text,
)

DEFAULT_MAX_ITERATIONS = 8
DEFAULT_MAX_FILE_CHARS = 100_000

ModelRequest = Callable[..., ModelResponse]


@dataclass(frozen=True)
class AgentResponse:
    text: str
    truncated: bool = False


def _assistant_content(response: ModelResponse) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    for block in response.content:
        if isinstance(block, TextBlock):
            content.append({"type": "text", "text": block.text})
        else:
            content.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
            )
    return content


def _execute_tool(
    block: ToolUseBlock, workspace: Path, *, max_file_chars: int
) -> ToolResult:
    if block.name != "read_file":
        if block.name == "find_files":
            return find_files(block.input, workspace)
        if block.name == "search_text":
            return search_text(block.input, workspace)
        return ToolResult(f"Unknown tool: {block.name}", is_error=True)
    return read_file(block.input, workspace, max_chars=max_file_chars)


def run_agent(
    prompt: str,
    config: ModelConfig,
    *,
    workspace: Path | None = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    max_file_chars: int = DEFAULT_MAX_FILE_CHARS,
    request: ModelRequest | None = None,
) -> AgentResponse:
    """Run until the model answers, truncates, fails, or exhausts the loop limit."""
    if not prompt.strip():
        raise ModelError("The prompt must not be empty.")
    if max_iterations <= 0:
        raise ValueError("max_iterations must be positive.")
    if max_file_chars <= 0:
        raise ValueError("max_file_chars must be positive.")

    workspace = (workspace or Path.cwd()).resolve()
    request = request or create_message
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

    for _ in range(max_iterations):
        response = request(messages, config, tools=TOOL_DEFINITIONS)
        text = "\n".join(
            block.text for block in response.content if isinstance(block, TextBlock)
        )

        if response.stop_reason == "max_tokens":
            if not text.strip():
                raise ModelError(
                    "The model output was truncated before producing text."
                )
            return AgentResponse(text=text, truncated=True)

        if response.stop_reason == "end_turn":
            if any(isinstance(block, ToolUseBlock) for block in response.content):
                raise ModelError("The model ended while requesting a tool.")
            if not text.strip():
                raise ModelError("The model returned no text.")
            return AgentResponse(text=text)

        if response.stop_reason != "tool_use":
            raise ModelError(
                f"The model stopped unexpectedly ({response.stop_reason or 'unknown'})."
            )

        tool_calls = [
            block for block in response.content if isinstance(block, ToolUseBlock)
        ]
        if not tool_calls:
            raise ModelError("The model requested tool use without a tool call.")
        messages.append({"role": "assistant", "content": _assistant_content(response)})
        results: list[dict[str, Any]] = []
        for call in tool_calls:
            result = _execute_tool(call, workspace, max_file_chars=max_file_chars)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": result.content,
                    "is_error": result.is_error,
                }
            )
        messages.append({"role": "user", "content": results})

    raise ModelError(f"Agent exceeded the maximum of {max_iterations} model requests.")
