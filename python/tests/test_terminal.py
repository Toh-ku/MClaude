"""Interactive terminal status rendering."""

import os
import unicodedata
from io import StringIO
from pathlib import Path

from mclaude.terminal import (
    CLEAR_SCREEN,
    ENTER_ALT_SCREEN,
    LEAVE_ALT_SCREEN,
    SAVE_CURSOR,
    TerminalStatus,
    supports_status_view,
)


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
        terminal_size=(120, 30),
    )


def test_status_page_clears_terminal_and_shows_session_metadata() -> None:
    stream = StringIO()
    view = status(stream)

    view.open()

    output = stream.getvalue()
    assert output.startswith(ENTER_ALT_SCREEN + CLEAR_SCREEN)
    assert "MClaude 0.1.0" in output
    assert str(Path("workspace").resolve()) in output
    assert "test-model" in output
    assert "a" * 32 in output
    assert "context" in output
    assert "PID" in output
    assert "tasks      none" in output
    assert "hooks on" in output
    assert "MCP off" in output
    assert "\x1b[15;30r" in output
    assert "assistant output" in output


def test_status_page_prints_request_usage_and_tool_lifecycle() -> None:
    stream = StringIO()
    view = status(stream)
    view.open()

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
    assert output.count(SAVE_CURSOR) == 5
    assert "25,000 / 100,000 tokens (25.0%)" in output
    assert "API input 24,500 | output 80" in output
    assert "2 call(s): read_file, search_text" in output
    assert "read_file [ok]" in output
    assert "2 total | 1 active | 0 pending | 0 done | 1 blocked" in output


def test_status_view_requires_real_input_and_error_terminals(monkeypatch) -> None:
    monkeypatch.delenv("TERM", raising=False)
    assert supports_status_view(stdin=TTY(), stdout=TTY(), stderr=TTY())
    assert not supports_status_view(stdin=StringIO(), stdout=TTY(), stderr=TTY())
    assert not supports_status_view(stdin=TTY(), stdout=StringIO(), stderr=TTY())
    assert not supports_status_view(stdin=TTY(), stdout=TTY(), stderr=StringIO())

    monkeypatch.setenv("TERM", "dumb")
    assert supports_status_view(stdin=TTY(), stdout=TTY(), stderr=TTY()) is (
        os.name == "nt"
    )


def test_close_restores_terminal_and_is_idempotent() -> None:
    stream = StringIO()
    view = status(stream)
    view.open()
    view.close()
    first_close = stream.getvalue()
    view.close()

    assert stream.getvalue() == first_close
    assert "\x1b[r" in first_close
    assert first_close.endswith(LEAVE_ALT_SCREEN)


def test_events_redraw_in_place_and_escape_terminal_control_characters() -> None:
    stream = StringIO()
    view = status(stream)
    view.open()
    view.event("tool\nname", "unsafe\x1b[2Jdetail")

    redraw = stream.getvalue().split(SAVE_CURSOR, maxsplit=1)[1]
    assert "TOOL NAME" in redraw
    assert "unsafe [2Jdetail" in redraw
    assert "\n" not in redraw


def test_dashboard_respects_terminal_width_for_wide_characters() -> None:
    stream = StringIO()
    view = status(stream)
    view._terminal_size = (40, 12)
    view.workspace = Path("中文工作区/一个很长的项目名称")

    lines = view._dashboard_lines(40, 9)

    # Every plain dashboard row fits without wrapping; ANSI-styled rows add escapes.
    plain_lines = [line for line in lines if "\x1b[" not in line]
    assert all(
        sum(
            0
            if unicodedata.category(character) in {"Mn", "Me", "Cf"}
            else 2
            if unicodedata.east_asian_width(character) in {"W", "F"}
            else 1
            for character in line
        )
        == 38
        for line in plain_lines
    )


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
