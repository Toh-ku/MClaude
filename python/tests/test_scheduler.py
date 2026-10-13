"""Concurrency proven by synchronization, with write barriers and safe cancellation."""

import threading
import time

import pytest

from mclaude.agent import run_agent
from mclaude.cancellation import TurnCancelled, check_read_cancelled
from mclaude.config import ModelConfig
from mclaude.hooks import HookRunner
from mclaude.provider import ModelError, ModelResponse, TextBlock, ToolUseBlock
from mclaude.scheduler import ToolScheduler
from mclaude.session import SessionStore
from mclaude.tools import ToolResult


def test_concurrent_reads_and_sequential_write_barrier():
    barrier = threading.Barrier(2)
    finished = set()
    outputs = {}
    main_thread = threading.get_ident()
    calls = [
        ToolUseBlock("a", "read_file", {}),
        ToolUseBlock("b", "search_text", {}),
        ToolUseBlock("write", "create_file", {}),
        ToolUseBlock("after", "read_file", {}),
    ]

    def prepare(call):
        assert threading.get_ident() == main_thread

    def execute(call):
        if call.id in {"a", "b"}:
            barrier.wait(timeout=3)
            finished.add(call.id)
        elif call.id == "write":
            assert finished == {"a", "b"}
            assert threading.get_ident() == main_thread
            finished.add("write")
        else:
            assert "write" in finished
        return ToolResult(call.id)

    ToolScheduler(2).run(
        calls,
        prepare,
        execute,
        lambda call, result: outputs.update({call.id: result.content}),
    )
    assert outputs == {call.id: call.id for call in calls}


def test_denied_read_never_starts():
    calls = [
        ToolUseBlock("deny", "read_file", {}),
        ToolUseBlock("ok", "find_files", {}),
    ]
    ran = []
    results = {}
    ToolScheduler().run(
        calls,
        lambda c: ToolResult("denied", True) if c.id == "deny" else None,
        lambda c: ran.append(c.id) or ToolResult("ok"),
        lambda c, r: results.update({c.id: r}),
    )
    assert ran == ["ok"] and results["deny"].is_error


def test_cancel_preserves_completed_read_and_cleans_workers():
    finished = threading.Event()
    calls = [
        ToolUseBlock("done", "read_file", {}),
        ToolUseBlock("cancel", "read_file", {}),
        ToolUseBlock("waiting", "search_text", {}),
        ToolUseBlock("write", "create_file", {}),
    ]
    results = {}

    def execute(call):
        if call.id == "done":
            finished.set()
            return ToolResult("saved")
        if call.id == "cancel":
            assert finished.wait(3)
            raise TurnCancelled("cancelled")
        if call.id == "waiting":
            while True:
                check_read_cancelled()
                time.sleep(0.01)
        pytest.fail("write must not start")

    with pytest.raises(TurnCancelled):
        ToolScheduler(3).run(
            calls,
            lambda c: None,
            execute,
            lambda c, r: results.update({c.id: r.content}),
        )
    assert results["done"] == "saved"
    assert not any(t.name.startswith("mclaude-read") for t in threading.enumerate())


def test_agent_associates_results_with_ids_and_preserves_order(tmp_path, monkeypatch):
    barrier = threading.Barrier(2)

    def execute(call, *args, **kwargs):
        barrier.wait(timeout=3)
        return ToolResult(call.input["path"])

    monkeypatch.setattr("mclaude.agent._execute_tool", execute)
    responses = iter(
        [
            ModelResponse(
                (
                    ToolUseBlock("a", "read_file", {"path": "first"}),
                    ToolUseBlock("b", "read_file", {"path": "second"}),
                ),
                "tool_use",
            ),
            ModelResponse((TextBlock("done"),), "end_turn"),
        ]
    )
    history = []
    events = []
    run_agent(
        "read",
        ModelConfig(api_key="test", model="test"),
        workspace=tmp_path,
        request=lambda *a, **k: next(responses),
        history=history,
        on_history_event=lambda kind, payload: events.append((kind, payload)),
    )
    results = history[2]["content"]
    assert [(r["tool_use_id"], r["content"]) for r in results] == [
        ("a", "first"),
        ("b", "second"),
    ]
    assert len([e for e in events if e[0] == "tool_result"]) == 2


def test_hooks_force_main_thread_and_duplicate_ids_fail(tmp_path, monkeypatch):
    main_thread = threading.get_ident()

    def execute(call, *args, **kwargs):
        assert threading.get_ident() == main_thread
        return ToolResult("ok")

    monkeypatch.setattr("mclaude.agent._execute_tool", execute)
    calls = (ToolUseBlock("a", "read_file", {}), ToolUseBlock("b", "read_file", {}))
    responses = iter(
        [
            ModelResponse(calls, "tool_use"),
            ModelResponse((TextBlock("done"),), "end_turn"),
        ]
    )
    run_agent(
        "read",
        ModelConfig(api_key="test", model="test"),
        workspace=tmp_path,
        request=lambda *a, **k: next(responses),
        hooks=HookRunner(),
    )
    with pytest.raises(ModelError, match="duplicate"):
        run_agent(
            "read",
            ModelConfig(api_key="test", model="test"),
            workspace=tmp_path,
            request=lambda *a, **k: ModelResponse((calls[0], calls[0]), "tool_use"),
        )


def test_cancelled_parallel_batch_remains_resumable(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "state")
    session = store.create(tmp_path, "test")
    barrier = threading.Barrier(2)

    def execute(call, *args, **kwargs):
        barrier.wait(timeout=3)
        if call.id == "cancel":
            raise TurnCancelled("cancelled read")
        return ToolResult("completed evidence")

    def record(kind, payload):
        if kind == "message":
            session.record_message(payload)
        elif kind == "tool_result":
            session.record_tool_result(payload)

    monkeypatch.setattr("mclaude.agent._execute_tool", execute)
    response = ModelResponse(
        (
            ToolUseBlock("done", "read_file", {}),
            ToolUseBlock("cancel", "read_file", {}),
            ToolUseBlock("write", "create_file", {}),
        ),
        "tool_use",
    )
    try:
        with pytest.raises(TurnCancelled):
            run_agent(
                "read",
                ModelConfig(api_key="test", model="test"),
                workspace=tmp_path,
                request=lambda *a, **k: response,
                history=session.history,
                on_history_event=record,
            )
        expected = {
            item["tool_use_id"]: item for item in session.history[-1]["content"]
        }
        assert expected["done"]["content"] == "completed evidence"
        assert "Not executed" in expected["write"]["content"]
    finally:
        session.close()
    resumed = store.resume(session.id, tmp_path)
    try:
        assert {
            item["tool_use_id"]: item for item in resumed.history[-1]["content"]
        } == expected
        assert not resumed.recovery_warning
    finally:
        resumed.close()
