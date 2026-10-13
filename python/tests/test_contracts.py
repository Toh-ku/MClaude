"""Shared wire fixtures remain readable by the Python implementation."""

import json
from pathlib import Path

from mclaude.session import _validate_complete_history
from mclaude.skills import LOAD_SKILL_DEFINITION
from mclaude.subagents import DELEGATE_DEFINITION
from mclaude.tasks import TASK_DEFINITIONS, validate_tasks
from mclaude.tools import TOOL_DEFINITIONS
from mclaude.verification import VERIFICATION_DEFINITIONS

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"


def test_shared_history_and_tasks():
    fixture = json.loads(
        (CONTRACTS / "fixtures/conversation.json").read_text(encoding="utf-8")
    )
    assert _validate_complete_history(fixture["history"]) == fixture["history"]
    assert validate_tasks(fixture["tasks"]) == fixture["tasks"]


def test_public_tool_contracts_match_exported_definitions():
    definitions = json.loads(
        (CONTRACTS / "schemas/tools.json").read_text(encoding="utf-8")
    )
    assert definitions == [
        *TOOL_DEFINITIONS,
        *VERIFICATION_DEFINITIONS,
        *TASK_DEFINITIONS,
        LOAD_SKILL_DEFINITION,
        DELEGATE_DEFINITION,
    ]
