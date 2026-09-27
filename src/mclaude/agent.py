"""Model/tool loop with optional in-memory conversation history."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mclaude.cancellation import TurnCancelled, protect_cleanup
from mclaude.checkpoints import CheckpointStore
from mclaude.config import ModelConfig
from mclaude.context import (
    DEFAULT_CONTEXT_BUDGET_TOKENS,
    ContextBudget,
    ContextBudgetExceeded,
    ProjectInstructions,
    compact_history,
    load_project_instructions,
)
from mclaude.hooks import HookRunner
from mclaude.mcp import MCPError, MCPRegistry
from mclaude.permissions import (
    READ_ONLY_TOOLS,
    PermissionAction,
    PermissionDecision,
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
from mclaude.scheduler import ToolScheduler
from mclaude.skills import LOAD_SKILL_DEFINITION, SkillCatalog
from mclaude.subagents import DELEGATE_DEFINITION, SubagentRunner
from mclaude.tasks import TASK_DEFINITIONS, TaskBoard
from mclaude.tools import (
    TOOL_DEFINITIONS,
    ToolResult,
    create_file,
    find_files,
    list_edit_checkpoints,
    read_file,
    replace_text,
    restore_edit_checkpoint,
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
    block: ToolUseBlock,
    workspace: Path,
    *,
    max_file_chars: int,
    checkpoints: CheckpointStore,
) -> ToolResult:
    if block.name != "read_file":
        if block.name == "find_files":
            return find_files(block.input, workspace)
        if block.name == "search_text":
            return search_text(block.input, workspace)
        if block.name == "create_file":
            return create_file(block.input, workspace, checkpoints=checkpoints)
        if block.name == "replace_text":
            return replace_text(block.input, workspace, checkpoints=checkpoints)
        if block.name == "run_command":
            return run_command(block.input, workspace)
        if block.name == "list_edit_checkpoints":
            return list_edit_checkpoints(block.input, checkpoints)
        if block.name == "restore_edit_checkpoint":
            return restore_edit_checkpoint(block.input, checkpoints)
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
    checkpoint_store: CheckpointStore | None = None,
    task_board: TaskBoard | None = None,
    planning: bool = False,
    hooks: HookRunner | None = None,
    mcp: MCPRegistry | None = None,
    allowed_tools: frozenset[str] | None = None,
    subagent_budget: int = 8,
    subagent_depth: int = 0,
    read_workers: int = 4,
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
    if type(subagent_budget) is not int or not 0 <= subagent_budget <= 32:
        raise ValueError("subagent_budget must be between 0 and 32.")

    ToolScheduler(read_workers)  # Validate before any model or external process.
    workspace = (workspace or Path.cwd()).resolve()
    checkpoint_store = checkpoint_store or CheckpointStore(workspace)
    project_instructions = project_instructions or load_project_instructions(workspace)
    system_prompt = project_instructions.system_prompt()
    base_system_prompt = system_prompt or ""
    task_board = task_board if task_board is not None else TaskBoard()
    subagents = SubagentRunner(subagent_budget, subagent_depth)
    skills = SkillCatalog(workspace)
    tool_definitions = [*TOOL_DEFINITIONS, *TASK_DEFINITIONS, LOAD_SKILL_DEFINITION]
    if mcp is not None and not planning:
        try:
            mcp.connect()
        except (OSError, MCPError) as exc:
            raise ModelError(f"MCP discovery failed: {exc}") from exc
        tool_definitions.extend(mcp.definitions)
    if skills.skills:
        base_system_prompt += (
            "\nAvailable skills (load on demand):\n" + skills.summary()
        )
    if not planning and subagent_depth == 0 and subagent_budget > 0:
        tool_definitions.append(DELEGATE_DEFINITION)
    if allowed_tools is not None:
        tool_definitions = [t for t in tool_definitions if t["name"] in allowed_tools]
    if planning:
        tool_definitions = [
            tool for tool in tool_definitions if tool["name"] in READ_ONLY_TOOLS
        ]
        base_system_prompt += (
            "\nPlanning mode: analyze with read-only tools and return a plan. "
            "File modifications, commands and task updates are prohibited."
        )
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
        system_prompt = base_system_prompt
        if task_board.tasks:
            system_prompt += "\nCurrent task state:\n" + task_board.render()
        chunks: list[str] = []

        def emit_text(chunk: str, buffer: list[str] = chunks) -> None:
            buffer.append(chunk)
            if on_text is not None:
                on_text(chunk)

        try:
            try:
                context_budget.ensure_fits(
                    messages, tools=tool_definitions, system=system_prompt
                )
            except ContextBudgetExceeded as exc:
                compacted = compact_history(
                    messages,
                    context_budget,
                    tools=tool_definitions,
                    system=system_prompt,
                )
                if compacted is None:
                    raise ModelError(str(exc)) from exc
                messages[:] = compacted
                if on_history_event is not None:
                    on_history_event("compaction", {"history": messages.copy()})
            response = request(
                messages,
                config,
                tools=tool_definitions,
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
        if len({call.id for call in tool_calls}) != len(tool_calls):
            raise ModelError("The model returned duplicate tool call IDs.")
        results: list[dict[str, Any]] = []
        assistant_message = {
            "role": "assistant",
            "content": _assistant_content(response),
        }
        result_message = {"role": "user", "content": results}
        scheduler = ToolScheduler(read_workers, enabled=hooks is None or planning)
        call_order = {call.id: index for index, call in enumerate(tool_calls)}

        def prepare(call: ToolUseBlock) -> ToolResult | None:
            if allowed_tools is not None and call.name not in allowed_tools:
                permission = PermissionDecision(
                    PermissionAction.DENY,
                    "Tool is outside this agent's allowed set.",
                )
            elif planning and call.name not in READ_ONLY_TOOLS:
                permission = PermissionDecision(
                    PermissionAction.DENY, "Tool is blocked in planning mode."
                )
            else:
                permission = permission_gate.check(
                    PermissionRequest(
                        tool_name=call.name,
                        tool_input=call.input,
                        external=mcp is not None and call.name in mcp.routes,
                    )
                )
            if permission.action is PermissionAction.ALLOW:
                return None
            return ToolResult(
                f"Permission denied for tool '{call.name}': {permission.reason}",
                is_error=True,
            )

        def execute_call(call: ToolUseBlock) -> ToolResult:
            def execute() -> ToolResult:
                if call.name in {"list_tasks", "update_tasks"}:
                    return task_board.execute(call.name, call.input, on_history_event)
                if call.name == "load_skill":
                    return skills.load(call.input)
                if mcp is not None and call.name in mcp.routes:
                    return mcp.execute(call.name, call.input)
                if call.name == "delegate_readonly":
                    return subagents.execute(
                        call.input,
                        config=config,
                        workspace=workspace,
                        request=request,
                        permission_gate=permission_gate,
                        context_budget_tokens=context_budget_tokens,
                        max_file_chars=max_file_chars,
                    )
                return _execute_tool(
                    call,
                    workspace,
                    max_file_chars=max_file_chars,
                    checkpoints=checkpoint_store,
                )

            return (
                hooks.execute(call.name, call.input, workspace, execute)
                if hooks is not None and not planning
                else execute()
            )

        def record_result(
            call: ToolUseBlock,
            result: ToolResult,
            results: list[dict[str, Any]] = results,
            system_prompt: str = system_prompt,
            call_order: dict[str, int] = call_order,
        ) -> None:
            if any(item["tool_use_id"] == call.id for item in results):
                return
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

            fitted_content, budget_truncated = context_budget.fit_tool_result(
                original_content,
                build_messages,
                tools=tool_definitions,
                system=system_prompt,
            )
            result_block = {
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": fitted_content,
                "is_error": result.is_error or budget_truncated,
            }
            with protect_cleanup():
                if on_history_event is not None:
                    on_history_event("tool_result", result_block)
                results.append(result_block)
                results.sort(key=lambda item: call_order[item["tool_use_id"]])

        try:
            messages.append(assistant_message)
            if on_history_event is not None:
                on_history_event("message", assistant_message)
            scheduler.run(tool_calls, prepare, execute_call, record_result)
            messages.append(result_message)
        except KeyboardInterrupt as exc:
            with protect_cleanup():
                completed = {result["tool_use_id"] for result in results}
                for call in tool_calls:
                    if call.id in completed:
                        continue
                    detail = "Not executed: the user cancelled this turn."
                    if call.id in scheduler.started:
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
                results.sort(key=lambda item: call_order[item["tool_use_id"]])
                if messages[-1] is assistant_message:
                    messages.append(result_message)
            raise

    raise ModelError(f"Agent exceeded the maximum of {max_iterations} model requests.")
