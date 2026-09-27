"""Persistent session journal tests."""

import json
from pathlib import Path

import pytest

from mclaude.session import SessionError, SessionStore


def test_session_round_trip_preserves_unicode_and_tool_results(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(tmp_path / "state")
    session = store.create(workspace, "test-model")
    session_id = session.id
    session.record_message({"role": "user", "content": "读取 文件"})
    session.record_message(
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "read-1",
                    "name": "read_file",
                    "input": {"path": "文件.txt"},
                }
            ],
        }
    )
    session.record_tool_result(
        {
            "type": "tool_result",
            "tool_use_id": "read-1",
            "content": "内容",
            "is_error": False,
        }
    )
    session.record_message(
        {"role": "assistant", "content": [{"type": "text", "text": "完成"}]}
    )
    session.record_turn("ok")
    session.close()

    resumed = store.resume(session_id, workspace)
    assert resumed.history == [
        {"role": "user", "content": "读取 文件"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "read-1",
                    "name": "read_file",
                    "input": {"path": "文件.txt"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "read-1",
                    "content": "内容",
                    "is_error": False,
                }
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "完成"}]},
    ]
    resumed.close()


def test_resume_repairs_unfinished_tool_without_replaying_it(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(tmp_path / "state")
    session = store.create(workspace, "test-model")
    session_id = session.id
    session.record_message({"role": "user", "content": "Run it"})
    session.record_message(
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "command-1",
                    "name": "run_command",
                    "input": {"command": "do something"},
                }
            ],
        }
    )
    session.close()

    resumed = store.resume(session_id, workspace)
    result = resumed.history[-1]["content"][0]
    assert result["tool_use_id"] == "command-1"
    assert result["is_error"] is True
    assert "may have partially executed" in result["content"]
    assert "not replayed" in resumed.recovery_warning
    resumed.close()

    loaded_again = store.resume(session_id, workspace)
    assert len(loaded_again.history[-1]["content"]) == 1
    loaded_again.close()


def test_resume_keeps_completed_results_when_repairing_a_tool_batch(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(tmp_path / "state")
    session = store.create(workspace, "test-model")
    session_id = session.id
    session.record_message(
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "first", "name": "one", "input": {}},
                {"type": "tool_use", "id": "second", "name": "two", "input": {}},
            ],
        }
    )
    session.record_tool_result(
        {
            "type": "tool_result",
            "tool_use_id": "first",
            "content": "completed",
            "is_error": False,
        }
    )
    session.close()

    resumed = store.resume(session_id, workspace)
    results = resumed.history[-1]["content"]
    assert [result["tool_use_id"] for result in results] == ["first", "second"]
    assert results[0]["content"] == "completed"
    assert results[1]["is_error"] is True
    resumed.close()


def test_continue_is_scoped_to_workspace(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    store = SessionStore(tmp_path / "state")
    session = store.create(first, "test-model")
    session_id = session.id
    session.close()

    with pytest.raises(SessionError, match="different workspace"):
        store.resume(session_id, second)
    with pytest.raises(SessionError, match="No saved session"):
        store.continue_latest(second)


def test_session_rejects_a_concurrent_writer(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(tmp_path / "state")
    session = store.create(workspace, "test-model")
    with pytest.raises(SessionError, match="already open"):
        store.resume(session.id, workspace)
    session.close()


def test_session_discards_only_a_partial_final_record(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(tmp_path / "state")
    session = store.create(workspace, "test-model")
    session_id = session.id
    path = session.path
    session.record_message({"role": "user", "content": "Kept"})
    session.close()
    with path.open("ab") as file:
        file.write(b'{"seq":2,"type":"message')

    resumed = store.resume(session_id, workspace)
    assert resumed.history == [{"role": "user", "content": "Kept"}]
    assert "partial final log record" in resumed.recovery_warning
    assert path.read_bytes().endswith(b"\n")
    resumed.close()


def test_session_rejects_corrupt_complete_record(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(tmp_path / "state")
    session = store.create(workspace, "test-model")
    path = session.path
    session.close()
    with path.open("ab") as file:
        file.write(b"not-json\n")

    with pytest.raises(SessionError, match="corrupt"):
        store.continue_latest(workspace)


def test_log_does_not_store_credentials(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(tmp_path / "state")
    session = store.create(workspace, "test-model")
    records = [json.loads(line) for line in session.path.read_text().splitlines()]
    assert records[0]["model"] == "test-model"
    assert "api_key" not in records[0]
    session.close()
