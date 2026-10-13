"""Successful tool observations survive repeated bounded history compaction."""

import json
from pathlib import Path

from mclaude.context import ContextBudget, compact_history
from mclaude.evidence import EVIDENCE_PREFIX, collect_tool_evidence, render_evidence
from mclaude.session import _validate_complete_history

VERSION = "a" * 64


def test_shared_compacted_evidence_fixture_survives_recompression():
    fixture = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "contracts/fixtures/compacted-evidence.json"
        ).read_text(encoding="utf-8")
    )
    _validate_complete_history(fixture["history"])
    assert collect_tool_evidence(fixture["history"]) == fixture["evidence"]
    assert (
        collect_tool_evidence(force_compaction(fixture["history"]))
        == fixture["evidence"]
    )


def exchange(call_id, tool, arguments, content, is_error=False):
    return [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": call_id, "name": tool, "input": arguments}
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": content,
                    "is_error": is_error,
                }
            ],
        },
    ]


def force_compaction(history, budget=None):
    budget = budget or ContextBudget(1800, 100)
    compacted = compact_history(
        [
            *history,
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "verbose update " * 1500}],
            },
            {"role": "user", "content": "Continue; preserve Windows support"},
        ],
        budget,
    )
    assert compacted is not None
    budget.ensure_fits(compacted)
    return compacted


def test_compaction_preserves_successful_evidence_with_sources():
    history = [
        {"role": "user", "content": "Fix cache races while keeping the API stable"},
        *exchange("read", "read_file", {"path": "src/cache.py"}, "def lookup(): pass"),
        *exchange(
            "search",
            "search_text",
            {"query": "lookup", "path": "src"},
            "src/cache.py:37:lookup() acquires the lock",
        ),
        *exchange(
            "edit",
            "apply_edits",
            {"edits": [{"path": "src/cache.py"}, {"path": "src/lock.py"}]},
            "Updated src/cache.py\nCheckpoint: checkpoint-123\n+ acquire_lock()",
        ),
        *exchange(
            "checks",
            "run_checks",
            {"checks": [{"command": "pytest"}]},
            f"Verification attempt 1 passed. Workspace version: {VERSION}.\n"
            "12 tests passed",
        ),
        *exchange(
            "delegate",
            "delegate_readonly",
            {"prompt": "Investigate races"},
            "src/cache.py:37 needs a lock; src/lock.py provides it",
        ),
        *exchange(
            "error", "read_file", {"path": "missing.py"}, "ENOENT: missing.py", True
        ),
    ]
    evidence = {
        record["call_id"]: record
        for record in collect_tool_evidence(force_compaction(history))
    }
    assert len(evidence) == 6
    assert evidence["read"]["targets"] == ["src/cache.py"]
    assert "src/cache.py:37" in evidence["search"]["excerpt"]
    assert evidence["edit"]["targets"] == ["src/cache.py", "src/lock.py"]
    assert "checkpoint-123" in evidence["edit"]["excerpt"]
    assert evidence["checks"]["workspace_version"] == VERSION
    assert "pytest" in evidence["checks"]["excerpt"]
    assert "provides it" in evidence["delegate"]["excerpt"]
    assert evidence["error"]["outcome"] == "error_or_incomplete"


def test_evidence_survives_repeated_compaction_without_nesting():
    history = force_compaction(
        [
            {"role": "user", "content": "Keep the public API stable"},
            *exchange(
                "check",
                "run_checks",
                {"checks": [{"command": "pytest"}]},
                f"Passed. Workspace version: {VERSION}.\nTests passed",
            ),
            *exchange(
                "search",
                "search_text",
                {"query": "race", "path": "src"},
                "src/cache.py:37: race fixed",
            ),
        ]
    )
    for _ in range(5):
        history = force_compaction(history)
        evidence = {
            record["call_id"]: record for record in collect_tool_evidence(history)
        }
        assert len(evidence) == 2
        assert evidence["check"]["workspace_version"] == VERSION
        assert "src/cache.py:37" in evidence["search"]["excerpt"]
        assert "Earlier context: Tool evidence" not in str(history)
        assert "public API stable" in str(history)
        assert "preserve Windows support" in str(history)


def test_failed_retest_replaces_older_passed_evidence():
    history = [
        {"role": "user", "content": "Fix failure"},
        *exchange(
            "passed",
            "run_checks",
            {"checks": [{"command": "pytest"}]},
            f"Passed. Workspace version: {VERSION}.",
        ),
    ]
    for index in range(20):
        history.extend(
            exchange(
                f"read-{index}", "read_file", {"path": f"src/{index}.py"}, "source"
            )
        )
    history.extend(
        exchange(
            "failed",
            "retest_checks",
            {},
            "Verification attempt 2 failed. AssertionError: missing lock",
            True,
        )
    )
    evidence = collect_tool_evidence(force_compaction(history))
    assert any(
        record["call_id"] == "failed" and record["outcome"] == "error_or_incomplete"
        for record in evidence
    )
    assert not any(record["call_id"] == "passed" for record in evidence)


def test_long_evidence_is_explicitly_truncated_and_unicode_bounded():
    records = collect_tool_evidence(
        exchange(
            "read",
            "read_file",
            {"path": "中文.py"},
            "HEAD_IMPORT\n" + "🙂中" * 2000 + "\nTAIL_EXPORT",
        )
    )
    assert records[0]["truncated"]
    assert "HEAD_IMPORT" in records[0]["excerpt"]
    assert "TAIL_EXPORT" in records[0]["excerpt"]
    for limit in [200, 400, 800, 2000]:
        lines = render_evidence(records, limit)
        assert len("\n".join(lines)) <= limit
        for line in lines:
            if line.startswith(EVIDENCE_PREFIX):
                assert json.loads(line[len(EVIDENCE_PREFIX) :])["truncated"]


def test_compaction_refuses_to_erase_all_tool_evidence():
    history = [
        {"role": "user", "content": "Goal"},
        *exchange(
            "check",
            "run_checks",
            {},
            f"Passed. Workspace version: {VERSION}.\n" + "output" * 1000,
        ),
        {"role": "user", "content": "Continue"},
    ]
    assert compact_history(history, ContextBudget(120, 20)) is None


def test_malformed_legacy_evidence_is_ignored():
    history = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "Earlier conversation summary (generated locally):\n"
                    "Earlier goal: Goal\nTool evidence: {invalid-json",
                }
            ],
        }
    ]
    assert collect_tool_evidence(history) == []
    assert "Goal" in str(force_compaction(history))
