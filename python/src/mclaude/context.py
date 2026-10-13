"""Project instructions and conversation context management."""

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
MAX_INSTRUCTION_FILE_CHARS = 100_000
DEFAULT_CONTEXT_BUDGET_TOKENS = 100_000
TOOL_TRUNCATION_MARKER = "\n...[tool result truncated to fit context budget]"
SUMMARY_PREFIX = "Earlier conversation summary (generated locally):"


class ContextError(RuntimeError):
    """Project context could not be loaded safely."""


class ContextBudgetExceeded(ContextError):
    """A request cannot fit within the configured context budget."""


@dataclass(frozen=True)
class InstructionSource:
    """One instruction file and the workspace subtree where it applies."""

    path: Path
    scope: Path
    content: str


@dataclass(frozen=True)
class ProjectInstructions:
    """Ordered project instructions for one working directory."""

    workspace: Path
    working_directory: Path
    sources: tuple[InstructionSource, ...]

    def system_prompt(self) -> str | None:
        if not self.sources:
            return None
        sections = [
            "Follow the project instructions below. Later, more narrowly scoped "
            "files take precedence when instructions conflict. Each section states "
            "its source and scope."
        ]
        for source in self.sources:
            relative_source = source.path.relative_to(self.workspace).as_posix()
            relative_scope = source.scope.relative_to(self.workspace).as_posix() or "."
            sections.append(
                f"\n<project-instructions source={relative_source!r} "
                f"scope={relative_scope!r}>\n{source.content}\n"
                "</project-instructions>"
            )
        return "\n".join(sections)


def estimate_tokens(value: Any) -> int:
    """Return a conservative deterministic token estimate for request data.

    This avoids binding the core loop to one model tokenizer. UTF-8 bytes are
    estimated at four bytes per token with a small structural allowance.
    """
    if isinstance(value, str):
        encoded = value.encode("utf-8")
    else:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    return max(1, math.ceil(len(encoded) / 4) + 1)


@dataclass(frozen=True)
class ContextBudget:
    """Estimate and enforce the input space available to one model request."""

    max_tokens: int = DEFAULT_CONTEXT_BUDGET_TOKENS
    output_tokens: int = 1024

    def __post_init__(self) -> None:
        if self.max_tokens <= 0:
            raise ValueError("The context budget must be positive.")
        if self.output_tokens <= 0:
            raise ValueError("The output token reserve must be positive.")
        if self.output_tokens >= self.max_tokens:
            raise ValueError("The context budget must exceed the output token limit.")

    def request_tokens(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> int:
        total = estimate_tokens(messages)
        if tools:
            total += estimate_tokens(tools)
        if system:
            total += estimate_tokens(system)
        return total

    def ensure_fits(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> int:
        used = self.request_tokens(messages, tools=tools, system=system)
        if used + self.output_tokens > self.max_tokens:
            raise ContextBudgetExceeded(
                "Conversation context exceeds the configured budget "
                f"({used:,} estimated input + {self.output_tokens:,} reserved output "
                f"> {self.max_tokens:,} tokens)."
            )
        return used

    def fit_tool_result(
        self,
        content: str,
        build_messages: Any,
        *,
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> tuple[str, bool]:
        """Fit a tool result using a callback that builds candidate messages."""

        def fits(candidate: str) -> bool:
            messages = build_messages(candidate)
            return (
                self.request_tokens(messages, tools=tools, system=system)
                + self.output_tokens
                <= self.max_tokens
            )

        if fits(content):
            return content, False
        low = 0
        high = len(content)
        while low < high:
            middle = (low + high + 1) // 2
            if fits(content[:middle] + TOOL_TRUNCATION_MARKER):
                low = middle
            else:
                high = middle - 1
        candidate = content[:low] + TOOL_TRUNCATION_MARKER
        if not fits(candidate):
            candidate = "Tool result omitted: context budget exhausted."
        return candidate, True


def _message_groups(
    messages: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Group tool calls with the immediately following result message."""
    groups: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        group = [message]
        content = message.get("content")
        tool_ids = {
            block.get("id")
            for block in content
            if isinstance(content, list)
            and isinstance(block, dict)
            and block.get("type") == "tool_use"
        }
        if tool_ids and index + 1 < len(messages):
            following = messages[index + 1]
            following_content = following.get("content")
            result_ids = {
                block.get("tool_use_id")
                for block in following_content
                if isinstance(following_content, list)
                and isinstance(block, dict)
                and block.get("type") == "tool_result"
            }
            if result_ids == tool_ids:
                group.append(following)
                index += 1
        groups.append(group)
        index += 1
    return groups


def _clip(value: str, limit: int = 500) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _summarize_messages(messages: list[dict[str, Any]], limit: int) -> str:
    previous: list[str] = []
    user_requests: list[str] = []
    assistant_updates: list[str] = []
    tool_errors: list[str] = []
    for message in messages:
        role = message.get("role", "unknown")
        content = message.get("content")
        if isinstance(content, str):
            if (
                role == "user"
                and content
                != "Use this compacted context for the earlier conversation."
            ):
                user_requests.append(content)
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                value = str(block.get("text", ""))
                if value.startswith(SUMMARY_PREFIX):
                    previous.extend(
                        value.removeprefix(SUMMARY_PREFIX).strip().splitlines()
                    )
                elif role == "assistant":
                    assistant_updates.append(value)
            elif block.get("type") == "tool_result" and block.get("is_error"):
                tool_errors.append(str(block.get("content", "")))

    lines: list[str] = []
    used = 0

    def add(label: str, value: str, cap: int) -> None:
        nonlocal used
        remaining = limit - used - len(label) - 2
        if remaining <= 12 or not value.strip():
            return
        line = f"{label}: {_clip(value, min(cap, remaining))}"
        lines.append(line)
        used += len(line) + 1

    goal = ""
    prior_requests: list[str] = []
    earlier_context: list[str] = []
    for line in previous:
        if line.startswith(("Earlier goal: ", "Original request: ")) and not goal:
            goal = line.split(": ", 1)[1]
        elif line.startswith("Recent user request: "):
            prior_requests.append(line.removeprefix("Recent user request: "))
        else:
            earlier_context.append(line)
    if not goal and previous:
        goal = previous[0]
    if not goal and user_requests:
        goal = user_requests[0]
    add("Earlier goal", goal, min(350, limit // 3))
    recent_requests = [*reversed(prior_requests), *user_requests]
    for value in reversed(recent_requests[-5:]):
        if value == goal:
            continue
        add("Recent user request", value, min(500, limit // 3))
    for value in reversed(earlier_context):
        add("Earlier context", value, min(300, limit // 4))
    for value in reversed(assistant_updates[-4:]):
        add("Assistant update", value, min(350, limit // 4))
    for value in reversed(tool_errors[-2:]):
        add("Tool error", value, min(250, limit // 5))
    return "\n".join(lines)


def compact_history(
    messages: list[dict[str, Any]],
    budget: ContextBudget,
    *,
    tools: list[dict[str, Any]] | None = None,
    system: str | None = None,
) -> list[dict[str, Any]] | None:
    """Replace the oldest complete groups with a bounded local summary."""
    groups = _message_groups(messages)
    if len(groups) < 2:
        return None
    for summary_limit in (4_000, 2_000, 1_000, 500, 200):
        for cut in range(1, len(groups)):
            removed = [message for group in groups[:cut] for message in group]
            retained = [message for group in groups[cut:] for message in group]
            summary = _summarize_messages(removed, summary_limit)
            candidate = [
                {
                    "role": "user",
                    "content": (
                        "Use this compacted context for the earlier conversation."
                    ),
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": f"{SUMMARY_PREFIX}\n{summary}"}
                    ],
                },
                *retained,
            ]
            try:
                budget.ensure_fits(candidate, tools=tools, system=system)
            except ContextBudgetExceeded:
                continue
            return candidate
    return None


def load_project_instructions(
    workspace: Path,
    working_directory: Path | None = None,
) -> ProjectInstructions:
    """Load instruction files from workspace root down to the active directory.

    Files outside that ancestor chain are deliberately ignored: an instruction file
    only governs its own directory and descendants. Within one directory AGENTS.md
    is loaded before CLAUDE.md, making the latter the later instruction source.
    """
    workspace = workspace.resolve()
    working_directory = (working_directory or workspace).resolve()
    try:
        relative = working_directory.relative_to(workspace)
    except ValueError as exc:
        raise ContextError("The working directory is outside the workspace.") from exc
    if not working_directory.is_dir():
        raise ContextError("The instruction working directory is not a directory.")

    directories = [workspace]
    current = workspace
    for part in relative.parts:
        current = current / part
        directories.append(current)

    sources: list[InstructionSource] = []
    for directory in directories:
        for name in INSTRUCTION_FILES:
            path = directory / name
            if not path.is_file() or path.is_symlink():
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise ContextError(
                    f"Could not read project instructions {path}: {exc}"
                ) from exc
            if len(content) > MAX_INSTRUCTION_FILE_CHARS:
                raise ContextError(
                    f"Project instructions {path} exceed the "
                    f"{MAX_INSTRUCTION_FILE_CHARS:,}-character limit."
                )
            if content.strip():
                sources.append(
                    InstructionSource(path=path, scope=directory, content=content)
                )
    return ProjectInstructions(
        workspace=workspace,
        working_directory=working_directory,
        sources=tuple(sources),
    )
