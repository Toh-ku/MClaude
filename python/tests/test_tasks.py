"""Task state survives compaction and process restarts without tool replay."""

import pytest

from mclaude.agent import run_agent
from mclaude.config import ModelConfig
from mclaude.provider import ModelResponse, TextBlock, ToolUseBlock
from mclaude.session import SessionError, SessionStore
from mclaude.tasks import TaskBoard

TASKS = [{"id": "1", "description": "调查故障", "status": "in_progress"}]


def test_tasks_validate_before_mutation():
    board = TaskBoard()
    assert not board.execute("update_tasks", {"tasks": TASKS}).is_error
    for value in [TASKS * 2, [{**TASKS[0], "status": "invalid"}], {}, [None]]:
        assert board.execute("update_tasks", {"tasks": value}).is_error
        assert board.tasks == TASKS
    assert "调查" in board.execute("list_tasks", {}).content


def test_tasks_resume_after_compaction(tmp_path):
    store = SessionStore(tmp_path / "state")
    session = store.create(tmp_path, "model")
    session.record_tasks(TASKS)
    session.record_compaction([])
    session.close()
    resumed = store.resume(session.id, tmp_path)
    try:
        assert resumed.history == []
        assert resumed.task_board.tasks == TASKS
    finally:
        resumed.close()


def test_task_event_failure_does_not_change_board():
    board = TaskBoard()

    def fail(*args):
        raise SessionError("disk full")

    with pytest.raises(SessionError):
        board.execute("update_tasks", {"tasks": TASKS}, fail)
    assert board.tasks == []


def test_agent_exposes_updated_tasks_on_next_request(tmp_path):
    events = []
    requests = []
    responses = iter(
        [
            ModelResponse(
                (ToolUseBlock("t", "update_tasks", {"tasks": TASKS}),), "tool_use"
            ),
            ModelResponse((TextBlock("done"),), "end_turn"),
        ]
    )

    def request(messages, config, **kwargs):
        requests.append(kwargs)
        return next(responses)

    board = TaskBoard()
    run_agent(
        "plan",
        ModelConfig(api_key="test", model="test"),
        workspace=tmp_path,
        request=request,
        task_board=board,
        on_history_event=lambda event, payload: events.append((event, payload)),
    )
    assert "调查故障" in requests[1]["system"]
    assert any(event == "tasks" for event, _ in events)
    assert board.tasks == TASKS
