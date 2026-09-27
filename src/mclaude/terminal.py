"""Small terminal status view for interactive MClaude sessions."""

from __future__ import annotations

import os
import platform
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from mclaude.context import estimate_tokens

CLEAR_SCREEN = "\x1b[2J\x1b[H"


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


class TerminalStatus:
    """Render an append-only status stream without disturbing model stdout."""

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
        self._lock = threading.Lock()

    def _write(self, text: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            print(text, file=self.stream, flush=True)

    def open(self) -> None:
        """Clear the terminal and print the stable session metadata."""
        if not self.enabled:
            return
        history_tokens = estimate_tokens(self.history) if self.history else 0
        session = (
            f"{self.session_id} ({self.session_kind})"
            if self.session_id
            else "temporary (not persisted)"
        )
        features = [
            f"persistence: {'on' if self.session_id else 'off'}",
            f"hooks: {'on' if self.hooks_enabled else 'off'}",
            f"MCP: {'on' if self.mcp_enabled else 'off'}",
            f"subagents: {self.subagent_budget}",
            f"read workers: {self.read_workers}",
        ]
        lines = [
            CLEAR_SCREEN + f"MClaude {self.version}",
            "=" * 72,
            f"Workspace    {self.workspace}",
            f"Model        {self.model}",
            f"Session      {session}",
            f"Mode         {'planning' if self.planning else 'execution'}",
            "Context      "
            f"{_context(history_tokens, self.context_budget)} history est.",
            f"Output limit {_tokens(self.max_tokens)} tokens",
            f"Tasks        {_task_summary(self.tasks)}",
            f"Process      PID {os.getpid()} | Python {platform.python_version()}",
            f"Started      {datetime.now().astimezone().isoformat(timespec='seconds')}",
            f"Features     {' | '.join(features)}",
            "-" * 72,
        ]
        self._write("\n".join(lines))

    def event(self, name: str, detail: str = "") -> None:
        """Append a timestamped lifecycle event."""
        elapsed = time.monotonic() - self.started_at
        suffix = f" | {detail}" if detail else ""
        self._write(f"[{elapsed:7.1f}s] {name.upper():<10}{suffix}")

    def ready(self) -> None:
        history_tokens = estimate_tokens(self.history) if self.history else 0
        detail = f"history est. {_context(history_tokens, self.context_budget)}"
        self.event("ready", detail)

    def set_mode(self, planning: bool) -> None:
        self.planning = planning
        self.event("mode", "planning" if planning else "execution")

    def update_tasks(self, tasks: list[dict[str, str]]) -> None:
        self.tasks = tasks
        self.event("tasks", _task_summary(tasks))

    def agent_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Render structured events emitted by the agent loop."""
        if event_type == "request.started":
            self.event(
                "request",
                f"{payload['iteration']}/{payload['max_iterations']} | "
                "context est. "
                f"{_context(payload['context_tokens'], self.context_budget)} "
                f"| output reserve {_tokens(payload['output_tokens'])}",
            )
        elif event_type == "response.received":
            usage = "usage unavailable"
            if payload.get("input_tokens") is not None:
                usage = (
                    f"API input {_tokens(payload['input_tokens'])} | "
                    f"output {_tokens(payload.get('output_tokens') or 0)}"
                )
            self.event("response", f"{usage} | stop {payload['stop_reason']}")
        elif event_type == "context.compacted":
            self.event(
                "compact",
                f"context reduced to {_tokens(payload['context_tokens'])} est. tokens",
            )
        elif event_type == "tools.started":
            names = ", ".join(payload["names"])
            self.event("tools", f"{payload['count']} call(s): {names}")
        elif event_type == "tool.finished":
            outcome = "error" if payload["is_error"] else "ok"
            self.event("tool", f"{payload['name']} [{outcome}]")


def supports_status_view(
    *, stdin: TextIO | None = None, stderr: TextIO | None = None
) -> bool:
    """Return whether clearing and decorating the current terminal is safe."""
    stdin = stdin or sys.stdin
    stderr = stderr or sys.stderr
    terminal_supports_ansi = os.name == "nt" or os.environ.get("TERM") != "dumb"
    return bool(stdin.isatty() and stderr.isatty() and terminal_supports_ansi)
