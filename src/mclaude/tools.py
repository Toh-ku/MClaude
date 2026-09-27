"""Tools exposed to the model by the minimal agent."""

from dataclasses import dataclass
from pathlib import Path

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


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False


def read_file(tool_input: object, workspace: Path, *, max_chars: int) -> ToolResult:
    """Read a bounded amount of text without escaping the workspace."""
    if not isinstance(tool_input, dict):
        return ToolResult("read_file input must be an object.", is_error=True)
    path_value = tool_input.get("path")
    if not isinstance(path_value, str) or not path_value.strip():
        return ToolResult("read_file requires a non-empty string path.", is_error=True)

    workspace = workspace.resolve()
    requested = Path(path_value)
    resolved = (
        (workspace / requested).resolve()
        if not requested.is_absolute()
        else requested.resolve()
    )
    if not resolved.is_relative_to(workspace):
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
