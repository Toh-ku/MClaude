"""Interactive terminal status rendering."""

import os
from io import StringIO
from pathlib import Path

from mclaude.terminal import CLEAR_SCREEN, TerminalStatus, supports_status_view


class TTY(StringIO):
    def isatty(self) -> bool:
        return True


def status(stream: StringIO, *, enabled: bool = True) -> TerminalStatus:
    return TerminalStatus(
        enabled=enabled,
        workspace=Path("workspace").resolve(),
        model="test-model",
        session_id="a" * 32,
        session_kind="new",
        planning=False,
        context_budget=100_000,
        max_tokens=2_000,
        history=[{"role": "user", "content": "hello"}],
        version="0.1.0",
        hooks_enabled=True,
        mcp_enabled=False,
        subagent_budget=8,
        read_workers=4,
        stream=stream,
    )


def test_status_page_clears_terminal_and_shows_session_metadata() -> None:
    stream = StringIO()
    view = status(stream)

    view.open()

    output = stream.getvalue()
    assert output.startswith(CLEAR_SCREEN + "MClaude 0.1.0")
    assert str(Path("workspace").resolve()) in output
    assert "test-model" in output
    assert "a" * 32 in output
    assert "Context" in output and "history est." in output
    assert "PID" in output
    assert "Tasks        none" in output
    assert "hooks: on" in output
    assert "MCP: off" in output


def test_status_page_prints_request_usage_and_tool_lifecycle() -> None:
    stream = StringIO()
    view = status(stream)

    view.agent_event(
        "request.started",
        {
            "iteration": 1,
            "max_iterations": 8,
            "context_tokens": 25_000,
            "output_tokens": 2_000,
        },
    )
    view.agent_event(
        "response.received",
        {"stop_reason": "tool_use", "input_tokens": 24_500, "output_tokens": 80},
    )
    view.agent_event(
        "tools.started", {"count": 2, "names": ["read_file", "search_text"]}
    )
    view.agent_event("tool.finished", {"name": "read_file", "is_error": False})
    view.update_tasks(
        [
            {"id": "one", "description": "work", "status": "in_progress"},
            {"id": "two", "description": "wait", "status": "blocked"},
        ]
    )

    output = stream.getvalue()
    assert "25,000 / 100,000 tokens (25.0%)" in output
    assert "API input 24,500 | output 80" in output
    assert "2 call(s): read_file, search_text" in output
    assert "read_file [ok]" in output
    assert "2 total | 1 active | 0 pending | 0 done | 1 blocked" in output


def test_status_view_requires_real_input_and_error_terminals(monkeypatch) -> None:
    monkeypatch.delenv("TERM", raising=False)
    assert supports_status_view(stdin=TTY(), stderr=TTY())
    assert not supports_status_view(stdin=StringIO(), stderr=TTY())
    assert not supports_status_view(stdin=TTY(), stderr=StringIO())

    monkeypatch.setenv("TERM", "dumb")
    assert supports_status_view(stdin=TTY(), stderr=TTY()) is (os.name == "nt")


def test_disabled_status_is_silent() -> None:
    stream = StringIO()
    view = status(stream, enabled=False)
    view.open()
    view.ready()
    view.agent_event(
        "response.received",
        {"stop_reason": "end_turn", "input_tokens": 1, "output_tokens": 1},
    )
    assert stream.getvalue() == ""
