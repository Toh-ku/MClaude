"""Bounded, attributed historical observations retained through compaction."""

import json
import math
import re
from typing import Any

EVIDENCE_PREFIX = "Tool evidence: "
NOTE = "Evidence note: Historical excerpts; omitted evidence must be re-inspected."
CHECKS = {"run_checks", "retest_checks"}
CHANGES = {"create_file", "replace_text", "apply_edits", "restore_edit_checkpoint"}


def _excerpt(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n...[evidence excerpt]...\n"
    if limit <= len(marker) + 20:
        return text[: max(0, limit - 3)] + ("..." if limit >= 3 else "")
    available = limit - len(marker)
    head = math.ceil(available * 0.65)
    return text[:head] + marker + text[-(available - head) :]


def _parse_evidence(text: str) -> dict[str, Any] | None:
    try:
        record = json.loads(text)
    except (ValueError, TypeError):
        return None
    if (
        isinstance(record, dict)
        and isinstance(record.get("call_id"), str)
        and isinstance(record.get("tool"), str)
        and record.get("outcome") in ("ok", "error_or_incomplete")
        and isinstance(record.get("targets"), list)
        and all(isinstance(target, str) for target in record["targets"])
        and isinstance(record.get("excerpt"), str)
        and isinstance(record.get("truncated"), bool)
    ):
        return record
    return None


def collect_tool_evidence(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls: dict[str, dict[str, Any]] = {}
    records: dict[str, dict[str, Any]] = {}
    for message in messages:
        if not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if not isinstance(block, dict):
                continue
            if message.get("role") == "assistant":
                if block.get("type") == "tool_use":
                    calls[block["id"]] = block
                elif block.get("type") == "text" and str(
                    block.get("text", "")
                ).startswith("Earlier conversation summary (generated locally):"):
                    for line in block["text"].splitlines():
                        if line.startswith(EVIDENCE_PREFIX):
                            record = _parse_evidence(line[len(EVIDENCE_PREFIX) :])
                            if record is not None:
                                records[record["call_id"]] = record
            elif block.get("type") == "tool_result":
                call_id = block["tool_use_id"]
                call = calls.get(call_id, {})
                arguments = call.get("input", {})
                if not isinstance(arguments, dict):
                    arguments = {}
                targets = []
                if isinstance(arguments.get("path"), str):
                    targets = [arguments["path"]]
                elif isinstance(arguments.get("edits"), list):
                    targets = [
                        edit["path"]
                        for edit in arguments["edits"]
                        if isinstance(edit, dict) and isinstance(edit.get("path"), str)
                    ]
                text = str(block.get("content", ""))
                context = arguments.get(
                    "query", arguments.get("pattern", arguments.get("checkpoint_id"))
                )
                if isinstance(context, str):
                    text = f"Request reference: {context[:200]}\n{text}"
                commands = arguments.get("checks")
                if isinstance(commands, list):
                    command_text = "; ".join(
                        str(check.get("command"))
                        for check in commands
                        if isinstance(check, dict)
                    )
                    text = f"Checks requested: {command_text}\n{text}"
                records[call_id] = {
                    "call_id": call_id,
                    "tool": call.get("name", "unknown"),
                    "outcome": "error_or_incomplete" if block.get("is_error") else "ok",
                    "targets": targets,
                    "excerpt": _excerpt(text, 600),
                    "truncated": len(text) > 600
                    or (isinstance(context, str) and len(context) > 200)
                    or bool(
                        re.search(
                            r"\[(?:Output|Subagent report|Diff|Results|Command output)"
                            r".*truncated|tool result truncated to fit context budget"
                            r"|Tool result omitted:",
                            str(block.get("content", "")),
                            re.IGNORECASE,
                        )
                    ),
                }
                version = re.search(
                    r"Workspace version: ([a-f0-9]{64})", str(block.get("content", ""))
                )
                if version and call.get("name") in CHECKS:
                    records[call_id]["workspace_version"] = version.group(1)
    return list(records.values())


def render_evidence(records: list[dict[str, Any]], limit: int) -> list[str]:
    if not records or limit < len(NOTE):
        return []
    if limit <= len(NOTE) + 100:
        return [NOTE]
    check_indices = [
        index for index, record in enumerate(records) if record["tool"] in CHECKS
    ]
    last_check = check_indices[-1] if check_indices else -1
    eligible = [
        (index, record)
        for index, record in enumerate(records)
        if record["tool"] not in CHECKS or index == last_check
    ]

    def priority(item):
        index, record = item
        tool = record["tool"]
        weight = (
            4
            if tool in CHECKS
            else 3
            if tool in CHANGES or record["outcome"] != "ok"
            else 2
            if tool == "delegate_readonly"
            else 1
        )
        return weight, index

    ranked = sorted(eligible, key=priority, reverse=True)
    selected = ranked[: min(8, max(1, (limit - len(NOTE)) // 320))]
    remaining = limit - len(NOTE) - 1
    lines = []
    for position, (index, record) in enumerate(selected):
        allowance = remaining // (len(selected) - position)

        def line(length, record=record):
            fitted = {
                **record,
                "excerpt": _excerpt(record["excerpt"], length),
                "truncated": record["truncated"] or length < len(record["excerpt"]),
            }
            return EVIDENCE_PREFIX + json.dumps(
                fitted, ensure_ascii=False, separators=(",", ":")
            )

        if len(line(0)) > allowance:
            continue
        low, high = 0, len(record["excerpt"])
        while low < high:
            middle = (low + high + 1) // 2
            if len(line(middle)) <= allowance:
                low = middle
            else:
                high = middle - 1
        text = line(low)
        lines.append((index, text))
        remaining -= len(text) + 1
    return [text for _, text in sorted(lines)] + [NOTE]
