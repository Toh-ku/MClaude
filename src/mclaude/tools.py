"""Tools exposed to the model by the minimal agent."""

import difflib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pathspec import PathSpec

DEFAULT_MAX_SEARCH_RESULTS = 200
DEFAULT_MAX_SEARCH_FILE_BYTES = 1_000_000
DEFAULT_MAX_SEARCH_LINE_CHARS = 500
DEFAULT_MAX_EDIT_FILE_BYTES = 1_000_000
DEFAULT_MAX_DIFF_CHARS = 20_000

READ_FILE_DEFINITION = {
    "name": "read_file",
    "description": (
        "Read a UTF-8 text file inside the current workspace. "
        "Use a path relative to the workspace when possible."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path of the file to read"}
        },
        "required": ["path"],
        "additionalProperties": False,
    },
}

FIND_FILES_DEFINITION = {
    "name": "find_files",
    "description": "Find files in the workspace by a git-style glob pattern.",
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob such as '*.py', 'src/**', or '**/test_*.py'",
            }
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
}

SEARCH_TEXT_DEFINITION = {
    "name": "search_text",
    "description": (
        "Search UTF-8 workspace files line by line and return paths and line numbers."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Text or regex to find"},
            "path": {
                "type": "string",
                "description": (
                    "Optional file or directory to search (default: workspace)"
                ),
            },
            "is_regex": {
                "type": "boolean",
                "description": (
                    "Interpret query as a regular expression (default: false)"
                ),
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "Use case-sensitive matching (default: true)",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

CREATE_FILE_DEFINITION = {
    "name": "create_file",
    "description": (
        "Create a new UTF-8 text file in the workspace. "
        "The call fails instead of overwriting an existing path."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path of the new file"},
            "content": {"type": "string", "description": "Complete file content"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
}

REPLACE_TEXT_DEFINITION = {
    "name": "replace_text",
    "description": (
        "Replace one exact, unique text occurrence in an existing UTF-8 workspace "
        "file. The call fails if the expected text is missing or ambiguous."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path of the file to edit"},
            "old_text": {
                "type": "string",
                "description": "Exact existing text, including whitespace",
            },
            "new_text": {"type": "string", "description": "Replacement text"},
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    },
}

TOOL_DEFINITIONS = [
    READ_FILE_DEFINITION,
    FIND_FILES_DEFINITION,
    SEARCH_TEXT_DEFINITION,
    CREATE_FILE_DEFINITION,
    REPLACE_TEXT_DEFINITION,
]


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False


def _resolve_in_workspace(path_value: str, workspace: Path) -> Path | None:
    requested = Path(path_value)
    resolved = (
        (workspace / requested).resolve()
        if not requested.is_absolute()
        else requested.resolve()
    )
    return resolved if resolved.is_relative_to(workspace) else None


def _workspace_files(workspace: Path):
    """Yield safe workspace files in stable path order."""

    def walk(directory: Path):
        try:
            entries = sorted(
                directory.iterdir(), key=lambda entry: entry.name.casefold()
            )
        except OSError:
            return
        for entry in entries:
            if entry.name == ".git":
                continue
            try:
                is_dir = entry.is_dir()
                resolved = entry.resolve()
            except OSError:
                continue
            if not resolved.is_relative_to(workspace):
                continue
            if is_dir:
                if not entry.is_symlink():
                    yield from walk(entry)
            elif entry.is_file():
                yield entry

    yield from walk(workspace)


def _display_path(path: Path, workspace: Path) -> str:
    return path.relative_to(workspace).as_posix()


def _format_diff(
    path: Path,
    workspace: Path,
    before: str,
    after: str,
    *,
    created: bool = False,
    max_chars: int = DEFAULT_MAX_DIFF_CHARS,
) -> str:
    relative = _display_path(path, workspace)
    diff = "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile="/dev/null" if created else f"a/{relative}",
            tofile=f"b/{relative}",
            lineterm="",
        )
    )
    if len(diff) > max_chars:
        return f"{diff[:max_chars]}\n[Diff truncated after {max_chars} characters.]"
    return diff


def _atomic_replace(path: Path, original: bytes, updated: bytes) -> bool:
    """Replace path atomically if its bytes still equal the version that was read."""
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(updated)
            file.flush()
            os.fsync(file.fileno())
        if path.read_bytes() != original:
            return False
        os.chmod(temporary, path.stat().st_mode)
        os.replace(temporary, path)
        return True
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def read_file(tool_input: object, workspace: Path, *, max_chars: int) -> ToolResult:
    """Read a bounded amount of text without escaping the workspace."""
    if not isinstance(tool_input, dict):
        return ToolResult("read_file input must be an object.", is_error=True)
    path_value = tool_input.get("path")
    if not isinstance(path_value, str) or not path_value.strip():
        return ToolResult("read_file requires a non-empty string path.", is_error=True)

    workspace = workspace.resolve()
    resolved = _resolve_in_workspace(path_value, workspace)
    if resolved is None:
        return ToolResult("Path is outside the workspace.", is_error=True)
    if not resolved.exists():
        return ToolResult(f"File not found: {path_value}", is_error=True)
    if not resolved.is_file():
        return ToolResult(f"Path is not a file: {path_value}", is_error=True)

    try:
        with resolved.open(encoding="utf-8") as file:
            content = file.read(max_chars + 1)
    except (OSError, UnicodeError) as exc:
        return ToolResult(
            f"Could not read {path_value}: {type(exc).__name__}", is_error=True
        )

    if len(content) > max_chars:
        content = content[:max_chars]
        content += f"\n\n[Output truncated after {max_chars} characters.]"
    return ToolResult(content)


def find_files(
    tool_input: object,
    workspace: Path,
    *,
    max_results: int = DEFAULT_MAX_SEARCH_RESULTS,
) -> ToolResult:
    """Find workspace files with a bounded git-style glob."""
    if not isinstance(tool_input, dict):
        return ToolResult("find_files input must be an object.", is_error=True)
    pattern = tool_input.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        return ToolResult("find_files requires a non-empty pattern.", is_error=True)

    workspace = workspace.resolve()
    matcher = PathSpec.from_lines("gitignore", [pattern])
    matches: list[str] = []
    truncated = False
    for path in _workspace_files(workspace):
        relative = path.relative_to(workspace).as_posix()
        if matcher.match_file(relative):
            if len(matches) == max_results:
                truncated = True
                break
            matches.append(relative)

    if not matches:
        return ToolResult(f"No files matched pattern: {pattern}")
    if truncated:
        matches.append(f"[Results truncated after {max_results} files.]")
    return ToolResult("\n".join(matches))


def search_text(
    tool_input: object,
    workspace: Path,
    *,
    max_results: int = DEFAULT_MAX_SEARCH_RESULTS,
    max_file_bytes: int = DEFAULT_MAX_SEARCH_FILE_BYTES,
    max_line_chars: int = DEFAULT_MAX_SEARCH_LINE_CHARS,
) -> ToolResult:
    """Search UTF-8 files and return bounded line-oriented matches."""
    if not isinstance(tool_input, dict):
        return ToolResult("search_text input must be an object.", is_error=True)
    query = tool_input.get("query")
    if not isinstance(query, str) or not query:
        return ToolResult("search_text requires a non-empty query.", is_error=True)
    path_value = tool_input.get("path", ".")
    if not isinstance(path_value, str) or not path_value.strip():
        return ToolResult("search_text path must be a non-empty string.", is_error=True)
    is_regex = tool_input.get("is_regex", False)
    case_sensitive = tool_input.get("case_sensitive", True)
    if not isinstance(is_regex, bool) or not isinstance(case_sensitive, bool):
        return ToolResult(
            "search_text is_regex and case_sensitive must be booleans.",
            is_error=True,
        )

    workspace = workspace.resolve()
    scope = _resolve_in_workspace(path_value, workspace)
    if scope is None:
        return ToolResult("Path is outside the workspace.", is_error=True)
    if not scope.exists():
        return ToolResult(f"Path not found: {path_value}", is_error=True)
    if not scope.is_dir() and not scope.is_file():
        return ToolResult(
            f"Path is not a file or directory: {path_value}", is_error=True
        )

    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(query if is_regex else re.escape(query), flags)
    except re.error as exc:
        return ToolResult(f"Invalid regular expression: {exc}", is_error=True)

    matches: list[str] = []
    truncated = False
    for path in _workspace_files(workspace):
        resolved = path.resolve()
        if scope.is_file():
            if resolved != scope:
                continue
        elif not resolved.is_relative_to(scope):
            continue
        try:
            if path.stat().st_size > max_file_bytes:
                continue
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        relative = path.relative_to(workspace).as_posix()
        for line_number, line in enumerate(content.splitlines(), start=1):
            if not regex.search(line):
                continue
            if len(matches) == max_results:
                truncated = True
                break
            display = line
            if len(display) > max_line_chars:
                display = f"{display[:max_line_chars]}…"
            matches.append(f"{relative}:{line_number}:{display}")
        if truncated:
            break

    if not matches:
        return ToolResult(f"No matches found for: {query}")
    if truncated:
        matches.append(f"[Results truncated after {max_results} matches.]")
    return ToolResult("\n".join(matches))


def create_file(
    tool_input: object,
    workspace: Path,
    *,
    max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS,
) -> ToolResult:
    """Create a UTF-8 file without overwriting an existing path."""
    if not isinstance(tool_input, dict):
        return ToolResult("create_file input must be an object.", is_error=True)
    path_value = tool_input.get("path")
    content = tool_input.get("content")
    if not isinstance(path_value, str) or not path_value.strip():
        return ToolResult(
            "create_file requires a non-empty string path.", is_error=True
        )
    if not isinstance(content, str):
        return ToolResult("create_file requires string content.", is_error=True)

    workspace = workspace.resolve()
    resolved = _resolve_in_workspace(path_value, workspace)
    if resolved is None:
        return ToolResult("Path is outside the workspace.", is_error=True)
    if resolved.exists():
        return ToolResult(
            f"Path already exists; refusing to overwrite: {path_value}", is_error=True
        )

    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        if not resolved.parent.resolve().is_relative_to(workspace):
            return ToolResult("Path is outside the workspace.", is_error=True)
        with resolved.open("x", encoding="utf-8", newline="") as file:
            file.write(content)
    except FileExistsError:
        return ToolResult(
            f"Path already exists; refusing to overwrite: {path_value}", is_error=True
        )
    except (OSError, UnicodeError) as exc:
        return ToolResult(
            f"Could not create {path_value}: {type(exc).__name__}", is_error=True
        )

    diff = _format_diff(
        resolved,
        workspace,
        "",
        content,
        created=True,
        max_chars=max_diff_chars,
    )
    return ToolResult(f"Created {resolved.relative_to(workspace).as_posix()}\n\n{diff}")


def replace_text(
    tool_input: object,
    workspace: Path,
    *,
    max_file_bytes: int = DEFAULT_MAX_EDIT_FILE_BYTES,
    max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS,
) -> ToolResult:
    """Replace one exact text occurrence and return a bounded unified diff."""
    if not isinstance(tool_input, dict):
        return ToolResult("replace_text input must be an object.", is_error=True)
    path_value = tool_input.get("path")
    old_text = tool_input.get("old_text")
    new_text = tool_input.get("new_text")
    if not isinstance(path_value, str) or not path_value.strip():
        return ToolResult(
            "replace_text requires a non-empty string path.", is_error=True
        )
    if not isinstance(old_text, str) or not old_text:
        return ToolResult("replace_text requires non-empty old_text.", is_error=True)
    if not isinstance(new_text, str):
        return ToolResult("replace_text requires string new_text.", is_error=True)
    if old_text == new_text:
        return ToolResult("old_text and new_text must differ.", is_error=True)

    workspace = workspace.resolve()
    resolved = _resolve_in_workspace(path_value, workspace)
    if resolved is None:
        return ToolResult("Path is outside the workspace.", is_error=True)
    if not resolved.exists():
        return ToolResult(f"File not found: {path_value}", is_error=True)
    if not resolved.is_file():
        return ToolResult(f"Path is not a file: {path_value}", is_error=True)

    try:
        original_bytes = resolved.read_bytes()
        if len(original_bytes) > max_file_bytes:
            return ToolResult(
                f"File exceeds the edit limit of {max_file_bytes} bytes.",
                is_error=True,
            )
        original = original_bytes.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        return ToolResult(
            f"Could not read {path_value}: {type(exc).__name__}", is_error=True
        )

    occurrences = original.count(old_text)
    if occurrences == 0:
        return ToolResult(
            "Expected text was not found; the file may have changed.", is_error=True
        )
    if occurrences > 1:
        return ToolResult(
            f"Expected text matched {occurrences} times; provide a unique match.",
            is_error=True,
        )

    updated = original.replace(old_text, new_text, 1)
    try:
        replaced = _atomic_replace(resolved, original_bytes, updated.encode("utf-8"))
    except OSError as exc:
        return ToolResult(
            f"Could not update {path_value}: {type(exc).__name__}", is_error=True
        )
    if not replaced:
        return ToolResult(
            "File changed during the edit; refusing to overwrite it.", is_error=True
        )

    diff = _format_diff(
        resolved,
        workspace,
        original,
        updated,
        max_chars=max_diff_chars,
    )
    return ToolResult(f"Updated {resolved.relative_to(workspace).as_posix()}\n\n{diff}")
