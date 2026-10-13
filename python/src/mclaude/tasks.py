"""Explicit, bounded task state independent of compactable chat history."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mclaude.tools import ToolResult

TASK_SCHEMA = {
    "type": "array",
    "maxItems": 100,
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "description": {"type": "string"},
            "status": {"enum": ["pending", "in_progress", "completed", "blocked"]},
        },
        "required": ["id", "description", "status"],
        "additionalProperties": False,
    },
}
TASK_DEFINITIONS = [
    {
        "name": "list_tasks",
        "description": "Show the current task steps and their status.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "update_tasks",
        "description": "Replace the task list. Preserve IDs for existing steps.",
        "input_schema": {
            "type": "object",
            "properties": {"tasks": TASK_SCHEMA},
            "required": ["tasks"],
            "additionalProperties": False,
        },
    },
]


def validate_tasks(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("Tasks must be a list with at most 100 steps.")
    ids = set()
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"id", "description", "status"}:
            raise ValueError("Each task needs id, description and status.")
        for key, limit in (("id", 64), ("description", 1000), ("status", 20)):
            if (
                not isinstance(item[key], str)
                or not item[key].strip()
                or len(item[key]) > limit
            ):
                raise ValueError(f"Invalid task {key}.")
        if item["status"] not in {"pending", "in_progress", "completed", "blocked"}:
            raise ValueError("Invalid task status.")
        if item["id"] in ids:
            raise ValueError("Task IDs must be unique.")
        ids.add(item["id"])
        result.append(item.copy())
    return result


@dataclass
class TaskBoard:
    tasks: list[dict[str, str]] = field(default_factory=list)

    def render(self) -> str:
        return json.dumps(self.tasks, ensure_ascii=False)

    def execute(
        self,
        name: str,
        arguments: object,
        on_change: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> "ToolResult":
        from mclaude.tools import ToolResult

        if not isinstance(arguments, dict):
            return ToolResult("Task input must be an object.", is_error=True)
        if name == "list_tasks":
            if arguments:
                return ToolResult("list_tasks accepts no arguments.", is_error=True)
            return ToolResult(self.render())
        try:
            if set(arguments) != {"tasks"}:
                raise ValueError("update_tasks requires only tasks.")
            updated = validate_tasks(arguments["tasks"])
        except ValueError as exc:
            return ToolResult(str(exc), is_error=True)
        if on_change is not None:
            on_change("tasks", {"tasks": updated})
        self.tasks = updated
        return ToolResult(self.render())
