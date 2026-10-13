"""Conflict-aware edit checkpoint tests."""

from pathlib import Path

import pytest

from mclaude.checkpoints import CheckpointError, CheckpointStore
from mclaude.tools import (
    create_file,
    list_edit_checkpoints,
    replace_text,
    restore_edit_checkpoint,
)


def test_replace_checkpoint_restores_original_bytes_and_mode(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("value = 1\n", encoding="utf-8", newline="")
    original_mode = target.stat().st_mode
    store = CheckpointStore(workspace, state)

    result = replace_text(
        {"path": "app.py", "old_text": "1", "new_text": "2"},
        workspace,
        checkpoints=store,
    )
    checkpoint_id = result.content.split("Checkpoint: ", 1)[1].splitlines()[0]
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert store.restore(checkpoint_id) == "Restored app.py"
    assert target.read_text(encoding="utf-8") == "value = 1\n"
    assert target.stat().st_mode == original_mode


def test_created_file_checkpoint_removes_unchanged_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CheckpointStore(workspace, tmp_path / "state")
    result = create_file(
        {"path": "new.txt", "content": "created"},
        workspace,
        checkpoints=store,
    )
    checkpoint_id = result.content.split("Checkpoint: ", 1)[1].splitlines()[0]
    assert store.restore(checkpoint_id) == "Removed new.txt"
    assert not (workspace / "new.txt").exists()


def test_restore_refuses_external_change(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("before", encoding="utf-8")
    store = CheckpointStore(workspace, tmp_path / "state")
    result = replace_text(
        {"path": "app.py", "old_text": "before", "new_text": "agent edit"},
        workspace,
        checkpoints=store,
    )
    checkpoint_id = result.content.split("Checkpoint: ", 1)[1].splitlines()[0]
    target.write_text("external edit", encoding="utf-8")

    with pytest.raises(CheckpointError, match="conflict"):
        store.restore(checkpoint_id)
    assert target.read_text(encoding="utf-8") == "external edit"
    assert store.list()[0].restored is False


def test_checkpoint_cannot_be_restored_twice(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CheckpointStore(workspace, tmp_path / "state")
    result = create_file(
        {"path": "new.txt", "content": "created"},
        workspace,
        checkpoints=store,
    )
    checkpoint_id = result.content.split("Checkpoint: ", 1)[1].splitlines()[0]
    store.restore(checkpoint_id)
    with pytest.raises(CheckpointError, match="already been restored"):
        store.restore(checkpoint_id)


def test_checkpoint_tools_list_and_restore(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CheckpointStore(workspace, tmp_path / "state")
    result = create_file(
        {"path": "new.txt", "content": "created"},
        workspace,
        checkpoints=store,
    )
    checkpoint_id = result.content.split("Checkpoint: ", 1)[1].splitlines()[0]
    listing = list_edit_checkpoints({}, store)
    assert checkpoint_id in listing.content
    assert "new.txt" in listing.content
    restored = restore_edit_checkpoint({"checkpoint_id": checkpoint_id}, store)
    assert restored.is_error is False
    assert restored.content == "Removed new.txt"
