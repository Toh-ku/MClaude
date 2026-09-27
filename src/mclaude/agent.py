"""Model/tool loop with optional in-memory conversation history."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mclaude.cancellation import TurnCancelled, protect_cleanup
from mclaude.config import ModelConfig
from mclaude.permissions import (
    PermissionAction,
    PermissionGate,
    PermissionRequest,
)
from mclaude.provider import (
    ModelError,
    ModelResponse,
    TextBlock,
    TextCallback,
    ToolUseBlock,
    create_message,
)
from mclaude.tools import (
    TOOL_DEFINITIONS,
    ToolResult,
    create_file,
    find_files,
    read_file,
    replace_text,
    run_command,
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
        if block.name == "create_file":
            return create_file(block.input, workspace)
        if block.name == "replace_text":
            return replace_text(block.input, workspace)
        if block.name == "run_command":
            return run_command(block.input, workspace)
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
    permission_gate: PermissionGate | None = None,
    history: list[dict[str, Any]] | None = None,
    on_text: TextCallback | None = None,
) -> AgentResponse:
    """Run one turn, appending messages to history when supplied.

    Completed tool exchanges remain in history even if a later request fails.
    Each turn receives a fresh request budget; previous tools are never replayed.
    """
    if not prompt.strip():
        raise ModelError("The prompt must not be empty.")
    if max_iterations <= 0:
        raise ValueError("max_iterations must be positive.")
    if max_file_chars <= 0:
        raise ValueError("max_file_chars must be positive.")

    workspace = (workspace or Path.cwd()).resolve()
    request = request or create_message
    permission_gate = permission_gate or PermissionGate()
    messages = history if history is not None else []
    messages.append({"role": "user", "content": prompt})

    for _ in range(max_iterations):
        chunks: list[str] = []

        def emit_text(chunk: str, buffer: list[str] = chunks) -> None:
            buffer.append(chunk)
            if on_text is not None:
                on_text(chunk)

        try:
            response = request(
                messages,
                config,
                tools=TOOL_DEFINITIONS,
                **({"on_text": emit_text} if on_text is not None else {}),
            )
        except (ModelError, KeyboardInterrupt):
            with protect_cleanup():
                partial_text = "".join(chunks)
                if partial_text.strip():
                    messages.append(
                        {
                            "role": "assistant",
                            "content": [{"type": "text", "text": partial_text}],
                        }
                    )
            raise
        finally:
            if on_text is not None and chunks and not "".join(chunks).endswith("\n"):
                on_text("\n")
        text = "\n".join(
            block.text for block in response.content if isinstance(block, TextBlock)
        )

        if response.stop_reason == "max_tokens":
            if not text.strip():
                raise ModelError(
                    "The model output was truncated before producing text."
                )
            # Incomplete tool calls must not enter the next request's history.
            messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": block.text}
                        for block in response.content
                        if isinstance(block, TextBlock)
                    ],
                }
            )
            return AgentResponse(text=text, truncated=True)

        if response.stop_reason == "end_turn":
            if any(isinstance(block, ToolUseBlock) for block in response.content):
                raise ModelError("The model ended while requesting a tool.")
            if not text.strip():
                raise ModelError("The model returned no text.")
            messages.append(
                {"role": "assistant", "content": _assistant_content(response)}
            )
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
        results: list[dict[str, Any]] = []
        assistant_message = {
            "role": "assistant",
            "content": _assistant_content(response),
        }
        result_message = {"role": "user", "content": results}
        active_call: str | None = None
        executing = False
        try:
            messages.append(assistant_message)
            for call in tool_calls:
                active_call = call.id
                executing = False
                permission = permission_gate.check(
                    PermissionRequest(tool_name=call.name, tool_input=call.input)
                )
                if permission.action is PermissionAction.ALLOW:
                    executing = True
                    result = _execute_tool(
                        call, workspace, max_file_chars=max_file_chars
                    )
                else:
                    result = ToolResult(
                        f"Permission denied for tool '{call.name}': "
                        f"{permission.reason}",
                        is_error=True,
                    )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": result.content,
                        "is_error": result.is_error,
                    }
                )
            messages.append(result_message)
        except KeyboardInterrupt as exc:
            with protect_cleanup():
                completed = {result["tool_use_id"] for result in results}
                for call in tool_calls:
                    if call.id in completed:
                        continue
                    detail = "Not executed: the user cancelled this turn."
                    if call.id == active_call and executing:
                        detail = (
                            str(exc)
                            if isinstance(exc, TurnCancelled)
                            else "Tool interrupted by the user. It may have partially "
                            "executed; inspect the workspace before retrying."
                        )
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call.id,
                            "content": detail,
                            "is_error": True,
                        }
                    )
                if messages[-1] is assistant_message:
                    messages.append(result_message)
            raise

    raise ModelError(f"Agent exceeded the maximum of {max_iterations} model requests.")
