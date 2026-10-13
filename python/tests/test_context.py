"""Project instruction loading tests."""

from pathlib import Path

import pytest

from mclaude.context import (
    SUMMARY_PREFIX,
    TOOL_TRUNCATION_MARKER,
    ContextBudget,
    ContextBudgetExceeded,
    ContextError,
    compact_history,
    load_project_instructions,
)


def test_instructions_follow_ancestor_scope_and_order(tmp_path: Path) -> None:
    nested = tmp_path / "src" / "feature"
    sibling = tmp_path / "other"
    nested.mkdir(parents=True)
    sibling.mkdir()
    (tmp_path / "AGENTS.md").write_text("root rule", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("root later", encoding="utf-8")
    (tmp_path / "src" / "AGENTS.md").write_text("src rule", encoding="utf-8")
    (sibling / "AGENTS.md").write_text("sibling rule", encoding="utf-8")

    instructions = load_project_instructions(tmp_path, nested)

    assert [
        source.path.relative_to(tmp_path).as_posix() for source in instructions.sources
    ] == [
        "AGENTS.md",
        "CLAUDE.md",
        "src/AGENTS.md",
    ]
    prompt = instructions.system_prompt()
    assert prompt is not None
    assert "sibling rule" not in prompt
    assert prompt.index("root rule") < prompt.index("src rule")
    assert "scope='src'" in prompt


def test_instruction_scope_must_stay_in_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ContextError, match="outside"):
        load_project_instructions(workspace, tmp_path)


def test_symlinked_instruction_file_is_ignored(tmp_path: Path) -> None:
    target = tmp_path / "outside.md"
    target.write_text("not trusted", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        (workspace / "AGENTS.md").symlink_to(target)
    except OSError:
        pytest.skip("File symlinks are unavailable")
    assert load_project_instructions(workspace).sources == ()


def test_context_budget_counts_system_tools_and_output_reserve() -> None:
    budget = ContextBudget(max_tokens=100, output_tokens=20)
    messages = [{"role": "user", "content": "x" * 100}]
    baseline = budget.request_tokens(messages)
    assert budget.request_tokens(messages, system="rules") > baseline
    assert budget.request_tokens(messages, tools=[{"name": "read"}]) > baseline
    with pytest.raises(ContextBudgetExceeded, match="reserved output"):
        budget.ensure_fits([{"role": "user", "content": "x" * 400}])


def test_context_budget_truncates_tool_result_to_fit() -> None:
    budget = ContextBudget(max_tokens=120, output_tokens=20)

    def build(content: str):
        return [
            {"role": "user", "content": "inspect"},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "read-1",
                        "content": content,
                        "is_error": False,
                    }
                ],
            },
        ]

    content, truncated = budget.fit_tool_result("x" * 1000, build)
    assert truncated is True
    assert content.endswith(TOOL_TRUNCATION_MARKER)
    assert len(content) < 1000
    budget.ensure_fits(build(content))


def test_compaction_preserves_recent_tool_pair_and_summarizes_goal() -> None:
    history = [
        {"role": "user", "content": "Implement the important objective " * 80},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "I will inspect the code."}],
        },
        {"role": "user", "content": "Keep compatibility." * 80},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "read-1",
                    "name": "read_file",
                    "input": {"path": "src/app.py"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "read-1",
                    "content": "recent result",
                    "is_error": False,
                }
            ],
        },
        {"role": "user", "content": "Continue now"},
    ]
    budget = ContextBudget(max_tokens=850, output_tokens=50)
    compacted = compact_history(history, budget)
    assert compacted is not None
    assert "important objective" in compacted[1]["content"][0]["text"]
    call_index = next(
        index
        for index, message in enumerate(compacted)
        if isinstance(message["content"], list)
        and any(block.get("type") == "tool_use" for block in message["content"])
    )
    assert compacted[call_index + 1]["content"][0]["tool_use_id"] == "read-1"
    budget.ensure_fits(compacted)


def test_compaction_keeps_recent_requirements_and_prior_summary() -> None:
    from mclaude.context import _summarize_messages

    history = [
        {"role": "user", "content": "Build the parser"},
        {"role": "assistant", "content": [{"type": "text", "text": "x" * 3000}]},
        {"role": "user", "content": "Keep the JSON format stable"},
        {"role": "user", "content": "Do not remove the legacy option"},
    ]
    first = _summarize_messages(history, 500)
    repeated = _summarize_messages(
        [
            {
                "role": "user",
                "content": "Use this compacted context for the earlier conversation.",
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": f"{SUMMARY_PREFIX}\n{first}",
                    }
                ],
            },
            {"role": "user", "content": "Also keep Windows support"},
        ],
        500,
    )

    assert "Build the parser" in repeated
    assert "Do not remove the legacy option" in repeated
    assert "Also keep Windows support" in repeated
    assert len(repeated) <= 500

    for _ in range(10):
        repeated = _summarize_messages(
            [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": f"{SUMMARY_PREFIX}\n{repeated}"}
                    ],
                }
            ],
            500,
        )
    assert "Earlier goal: Build the parser" in repeated
    assert "Also keep Windows support" in repeated
