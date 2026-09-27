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
            if not fits(candidate):
                raise ContextBudgetExceeded(
                    "The tool call and its minimal result exceed the configured "
                    "context budget."
                )
        return candidate, True


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
