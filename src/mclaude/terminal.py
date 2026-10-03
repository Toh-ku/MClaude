"""ANSI terminal dashboard for interactive MClaude sessions."""

from __future__ import annotations

import os
import platform
import re
import shutil
import sys
import threading
import time
import unicodedata
from collections import deque
from pathlib import Path
from typing import Any, TextIO

from mclaude.context import estimate_tokens

CLEAR_SCREEN = "\x1b[2J\x1b[H"
ENTER_ALT_SCREEN = "\x1b[?1049h"
LEAVE_ALT_SCREEN = "\x1b[?1049l"
RESET_TERMINAL = "\x1b[0m"
SAVE_CURSOR = "\x1b[s"
RESTORE_CURSOR = "\x1b[u"

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _tokens(value: int) -> str:
    return f"{value:,}"


def _context(value: int, budget: int) -> str:
    percent = min(999.9, value / budget * 100) if budget else 0.0
    return f"{_tokens(value)} / {_tokens(budget)} tokens ({percent:.1f}%)"


def _task_summary(tasks: list[dict[str, str]]) -> str:
    if not tasks:
        return "none"
    counts = {
        status: sum(task.get("status") == status for task in tasks)
        for status in ("pending", "in_progress", "completed", "blocked")
    }
    active = counts["in_progress"]
    return (
        f"{len(tasks)} total | {active} active | {counts['pending']} pending | "
        f"{counts['completed']} done | {counts['blocked']} blocked"
    )


def _one_line(value: object) -> str:
    """Keep terminal-owned content from moving the cursor or changing styles."""
    return _CONTROL_CHARACTERS.sub(" ", str(value)).strip()


def _character_width(character: str) -> int:
    if unicodedata.category(character) in {"Mn", "Me", "Cf"}:
        return 0
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def _fit(value: str, width: int) -> str:
    if width <= 0:
        return ""
    used = 0
    fitted: list[str] = []
    for character in value:
        character_width = _character_width(character)
        if used + character_width > width:
            break
        fitted.append(character)
        used += character_width
    if len(fitted) < len(value):
        while fitted and used >= width:
            removed = fitted.pop()
            used -= _character_width(removed)
        fitted.append("…")
        used += 1
    return "".join(fitted) + " " * max(0, width - used)


def _progress(value: int, budget: int, width: int = 14) -> str:
    ratio = min(1.0, value / budget) if budget else 0.0
    filled = round(ratio * width)
    return "█" * filled + "░" * (width - filled)


class TerminalStatus:
    """Maintain a fixed dashboard above the terminal's scrolling output pane."""

    def __init__(
        self,
        *,
        enabled: bool,
        workspace: Path,
        model: str,
        session_id: str | None,
        session_kind: str,
        planning: bool,
        context_budget: int,
        max_tokens: int,
        history: list[dict[str, Any]],
        version: str,
        hooks_enabled: bool,
        mcp_enabled: bool,
        subagent_budget: int,
        read_workers: int,
        tasks: list[dict[str, str]] | None = None,
        stream: TextIO | None = None,
        terminal_size: tuple[int, int] | None = None,
    ) -> None:
        self.enabled = enabled
        self.workspace = workspace
        self.model = model
        self.session_id = session_id
        self.session_kind = session_kind
        self.planning = planning
        self.context_budget = context_budget
        self.max_tokens = max_tokens
        self.history = history
        self.version = version
        self.hooks_enabled = hooks_enabled
        self.mcp_enabled = mcp_enabled
        self.subagent_budget = subagent_budget
        self.read_workers = read_workers
        self.tasks = tasks or []
        self.stream = stream or sys.stderr
        self.started_at = time.monotonic()
        self._terminal_size = terminal_size
        self._events: deque[tuple[float, str, str]] = deque(maxlen=4)
        self._phase = "starting"
        self._context_tokens = estimate_tokens(history) if history else 0
        self._api_input_tokens: int | None = None
        self._api_output_tokens: int | None = None
        self._iteration: tuple[int, int] | None = None
        self._stop_reason: str | None = None
        self._opened = False
        self._lock = threading.RLock()

    def _size(self) -> tuple[int, int]:
        if self._terminal_size is not None:
            return self._terminal_size
        size = shutil.get_terminal_size(fallback=(100, 30))
        return size.columns, size.lines

    def _layout(self) -> tuple[int, int, int]:
        columns, rows = self._size()
        columns = max(10, columns)
        rows = max(4, rows)
        # Leave at least three rows for streamed output and the input prompt.
        dashboard_rows = max(1, min(14, rows - 3))
        return columns, rows, dashboard_rows

    @staticmethod
    def _paint(text: str, code: str) -> str:
        return f"\x1b[{code}m{text}{RESET_TERMINAL}"

    def _active_task(self) -> str | None:
        for task in self.tasks:
            if task.get("status") == "in_progress":
                return _one_line(task.get("description") or task.get("id") or "task")
        return None

    def _dashboard_lines(self, width: int, height: int) -> list[str]:
        inner = max(1, width - 2)
        elapsed = time.monotonic() - self.started_at
        mode = "PLAN" if self.planning else "EXECUTE"
        title = f" MClaude {self.version}  {mode}  {self._phase.upper()} "
        title = self._paint(_fit(title, inner), "1;97;44")
        workspace = f" workspace  {_one_line(self.workspace)}"
        session = self.session_id or "temporary"
        session_line = (
            f" session    {_one_line(session)} ({_one_line(self.session_kind)})"
            f"  |  model  {_one_line(self.model)}"
        )
        context_line = (
            f" context    [{_progress(self._context_tokens, self.context_budget)}] "
            f"{_context(self._context_tokens, self.context_budget)}"
        )
        usage = "waiting for response"
        if self._api_input_tokens is not None:
            usage = (
                f"input {_tokens(self._api_input_tokens)} | "
                f"output {_tokens(self._api_output_tokens or 0)}"
            )
        request = (
            f" request    {usage} | limit {_tokens(self.max_tokens)}"
            f" | elapsed {elapsed:.1f}s"
        )
        if self._iteration is not None:
            request += f" | iteration {self._iteration[0]}/{self._iteration[1]}"
        if self._stop_reason:
            request += f" | stop {self._stop_reason}"
        task_line = f" tasks      {_task_summary(self.tasks)}"
        active_task = self._active_task()
        if active_task:
            task_line += f" | current: {active_task}"
        features = (
            f" runtime    Python {platform.python_version()} | PID {os.getpid()}"
            f" | hooks {'on' if self.hooks_enabled else 'off'}"
            f" | MCP {'on' if self.mcp_enabled else 'off'}"
            f" | agents {self.subagent_budget} | readers {self.read_workers}"
        )
        divider = " " + "─" * max(0, inner - 2) + " "
        lines = [
            title,
            _fit(workspace, inner),
            _fit(session_line, inner),
            _fit(context_line, inner),
            _fit(request, inner),
            _fit(task_line, inner),
            _fit(features, inner),
            divider,
            self._paint(_fit(" recent activity", inner), "1;36"),
        ]
        event_slots = max(0, height - len(lines) - 1)
        events = list(self._events)[-event_slots:]
        lines.extend(_fit("", inner) for _ in range(event_slots - len(events)))
        for event_elapsed, name, detail in events:
            suffix = f"  {detail}" if detail else ""
            lines.append(
                _fit(f" {event_elapsed:7.1f}s  {name.upper():<10}{suffix}", inner)
            )
        lines.append(self._paint(_fit(" assistant output", inner), "1;36"))
        return lines[:height]

    def _render(self, *, initial: bool = False) -> None:
        if not self.enabled or not self._opened:
            return
        width, rows, dashboard_rows = self._layout()
        lines = self._dashboard_lines(width, dashboard_rows)
        chunks = ["" if initial else SAVE_CURSOR]
        for row, line in enumerate(lines, start=1):
            chunks.append(f"\x1b[{row};1H\x1b[2K{line}")
        output_top = dashboard_rows + 1
        chunks.append(f"\x1b[{output_top};{rows}r")
        if initial:
            chunks.append(f"\x1b[{output_top};1H")
        else:
            chunks.append(RESTORE_CURSOR)
        self.stream.write("".join(chunks))
        self.stream.flush()

    def open(self) -> None:
        """Enter the alternate screen and draw the initial dashboard."""
        if not self.enabled:
            return
        with self._lock:
            if self._opened:
                return
            self._opened = True
            self.stream.write(ENTER_ALT_SCREEN + CLEAR_SCREEN)
            self._render(initial=True)

    def close(self) -> None:
        """Restore the normal terminal screen. Safe to call more than once."""
        if not self.enabled:
            return
        with self._lock:
            if not self._opened:
                return
            self.stream.write(
                "\x1b[r" + RESET_TERMINAL + "\x1b[?25h" + LEAVE_ALT_SCREEN
            )
            self.stream.flush()
            self._opened = False

    def event(self, name: str, detail: str = "") -> None:
        """Record a lifecycle event and redraw the dashboard in place."""
        if not self.enabled:
            return
        with self._lock:
            elapsed = time.monotonic() - self.started_at
            clean_name = _one_line(name)
            clean_detail = _one_line(detail)
            self._phase = clean_name
            self._events.append((elapsed, clean_name, clean_detail))
            self._render()

    def ready(self) -> None:
        self._context_tokens = estimate_tokens(self.history) if self.history else 0
        detail = f"history est. {_context(self._context_tokens, self.context_budget)}"
        self.event("ready", detail)

    def set_mode(self, planning: bool) -> None:
        self.planning = planning
        self.event("mode", "planning" if planning else "execution")

    def update_tasks(self, tasks: list[dict[str, str]]) -> None:
        self.tasks = tasks
        self.event("tasks", _task_summary(tasks))

    def agent_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Update dashboard state from structured agent-loop events."""
        if event_type == "request.started":
            self._iteration = (payload["iteration"], payload["max_iterations"])
            self._context_tokens = payload["context_tokens"]
            self._stop_reason = None
            self.event(
                "request",
                f"context {_context(self._context_tokens, self.context_budget)} "
                f"| output reserve {_tokens(payload['output_tokens'])}",
            )
        elif event_type == "response.received":
            self._api_input_tokens = payload.get("input_tokens")
            self._api_output_tokens = payload.get("output_tokens")
            self._stop_reason = payload["stop_reason"]
            usage = "usage unavailable"
            if self._api_input_tokens is not None:
                usage = (
                    f"API input {_tokens(self._api_input_tokens)} | "
                    f"output {_tokens(self._api_output_tokens or 0)}"
                )
            self.event("response", f"{usage} | stop {self._stop_reason}")
        elif event_type == "context.compacted":
            self._context_tokens = payload["context_tokens"]
            self.event(
                "compact",
                f"context reduced to {_tokens(self._context_tokens)} est. tokens",
            )
        elif event_type == "tools.started":
            names = ", ".join(_one_line(name) for name in payload["names"])
            self.event("tools", f"{payload['count']} call(s): {names}")
        elif event_type == "tool.finished":
            outcome = "error" if payload["is_error"] else "ok"
            self.event("tool", f"{_one_line(payload['name'])} [{outcome}]")


def supports_status_view(
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> bool:
    """Return whether all conversation streams share an ANSI-capable terminal."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    terminal_supports_ansi = os.name == "nt" or os.environ.get("TERM") != "dumb"
    return bool(
        stdin.isatty()
        and stdout.isatty()
        and stderr.isatty()
        and terminal_supports_ansi
    )
