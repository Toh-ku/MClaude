"""Model/tool loop with optional in-memory conversation history."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mclaude.cancellation import TurnCancelled, protect_cleanup
from mclaude.config import ModelConfig
from mclaude.context import (
    DEFAULT_CONTEXT_BUDGET_TOKENS,
    ContextBudget,
    ContextBudgetExceeded,
    ProjectInstructions,
    load_project_instructions,
)
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
HistoryEvent = Callable[[str, dict[str, Any]], None]


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
    on_history_event: HistoryEvent | None = None,
    project_instructions: ProjectInstructions | None = None,
    context_budget_tokens: int = DEFAULT_CONTEXT_BUDGET_TOKENS,
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
    project_instructions = project_instructions or load_project_instructions(workspace)
    system_prompt = project_instructions.system_prompt()
    try:
        context_budget = ContextBudget(context_budget_tokens, config.max_tokens)
    except ValueError as exc:
        raise ModelError(str(exc)) from exc
    request = request or create_message
    permission_gate = permission_gate or PermissionGate()
    messages = history if history is not None else []
    user_message = {"role": "user", "content": prompt}
    messages.append(user_message)
    if on_history_event is not None:
        on_history_event("message", user_message)

    for _ in range(max_iterations):
        chunks: list[str] = []

        def emit_text(chunk: str, buffer: list[str] = chunks) -> None:
            buffer.append(chunk)
            if on_text is not None:
                on_text(chunk)

        try:
            try:
                context_budget.ensure_fits(
                    messages, tools=TOOL_DEFINITIONS, system=system_prompt
                )
            except ContextBudgetExceeded as exc:
                raise ModelError(str(exc)) from exc
            response = request(
                messages,
                config,
                tools=TOOL_DEFINITIONS,
                **({"system": system_prompt} if system_prompt else {}),
                **({"on_text": emit_text} if on_text is not None else {}),
            )
        except (ModelError, KeyboardInterrupt):
            with protect_cleanup():
                partial_text = "".join(chunks)
                if partial_text.strip():
                    partial_message = {
                        "role": "assistant",
                        "content": [{"type": "text", "text": partial_text}],
                    }
                    messages.append(partial_message)
                    if on_history_event is not None:
                        on_history_event("message", partial_message)
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
            truncated_message = {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": block.text}
                    for block in response.content
                    if isinstance(block, TextBlock)
                ],
            }
            messages.append(truncated_message)
            if on_history_event is not None:
                on_history_event("message", truncated_message)
            return AgentResponse(text=text, truncated=True)

        if response.stop_reason == "end_turn":
            if any(isinstance(block, ToolUseBlock) for block in response.content):
                raise ModelError("The model ended while requesting a tool.")
            if not text.strip():
                raise ModelError("The model returned no text.")
            final_message = {
                "role": "assistant",
                "content": _assistant_content(response),
            }
            messages.append(final_message)
            if on_history_event is not None:
                on_history_event("message", final_message)
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
            if on_history_event is not None:
                on_history_event("message", assistant_message)
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
                original_content = result.content

                def build_messages(
                    candidate: str,
                    *,
                    call_id: str = call.id,
                    error: bool = result.is_error,
                    prior_results: list[dict[str, Any]] = results,
                ) -> list[dict[str, Any]]:
                    candidate_result = {
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "content": candidate,
                        "is_error": error,
                    }
                    return [
                        *messages,
                        {
                            "role": "user",
                            "content": [*prior_results, candidate_result],
                        },
                    ]

                try:
                    fitted_content, budget_truncated = context_budget.fit_tool_result(
                        original_content,
                        build_messages,
                        tools=TOOL_DEFINITIONS,
                        system=system_prompt,
                    )
                except ContextBudgetExceeded as exc:
                    raise ModelError(str(exc)) from exc
                result_block = {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": fitted_content,
                    "is_error": result.is_error or budget_truncated,
                }
                results.append(result_block)
                if on_history_event is not None:
                    on_history_event("tool_result", result_block)
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
                    result_block = {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": detail,
                        "is_error": True,
                    }
                    results.append(result_block)
                    if on_history_event is not None:
                        on_history_event("tool_result", result_block)
                if messages[-1] is assistant_message:
                    messages.append(result_message)
            raise

    raise ModelError(f"Agent exceeded the maximum of {max_iterations} model requests.")
