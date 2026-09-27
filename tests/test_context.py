"""Project instruction loading tests."""

from pathlib import Path

import pytest

from mclaude.context import ContextError, load_project_instructions


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
