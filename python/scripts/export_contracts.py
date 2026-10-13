"""Export the existing public wire contracts for both implementations."""

import json
from pathlib import Path

from mclaude.skills import LOAD_SKILL_DEFINITION
from mclaude.subagents import DELEGATE_DEFINITION
from mclaude.tasks import TASK_DEFINITIONS, TASK_SCHEMA
from mclaude.tools import TOOL_DEFINITIONS
from mclaude.verification import VERIFICATION_DEFINITIONS

destination = Path(__file__).resolve().parents[2] / "contracts" / "schemas"
destination.mkdir(parents=True, exist_ok=True)
contracts = {
    "tools.json": [
        *TOOL_DEFINITIONS,
        *VERIFICATION_DEFINITIONS,
        *TASK_DEFINITIONS,
        LOAD_SKILL_DEFINITION,
        DELEGATE_DEFINITION,
    ],
    "tasks.json": TASK_SCHEMA,
    "config.json": {
        "type": "object",
        "properties": {
            "version": {"const": 1},
            "api_key": {"type": "string"},
            "model": {"type": "string"},
            "base_url": {"type": "string"},
        },
        "required": ["version", "api_key", "model", "base_url"],
        "additionalProperties": False,
    },
}
for name, value in contracts.items():
    (destination / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
