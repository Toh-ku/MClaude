"""Project instructions and conversation context management."""

from dataclasses import dataclass
from pathlib import Path

INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
MAX_INSTRUCTION_FILE_CHARS = 100_000


class ContextError(RuntimeError):
    """Project context could not be loaded safely."""


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
