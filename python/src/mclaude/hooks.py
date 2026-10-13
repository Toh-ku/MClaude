"""Opt-in subprocess lifecycle hooks with bounded output and fail-closed prechecks."""

import json
import math
import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mclaude.cancellation import TurnCancelled, protect_cleanup
from mclaude.tools import ToolResult, _terminate_process_tree


class HookError(ValueError):
    pass


@dataclass(frozen=True)
class Hook:
    command: tuple[str, ...]
    tools: tuple[str, ...] = ("*",)
    timeout: float = 10.0


class HookRunner:
    def __init__(self, before: tuple[Hook, ...] = (), after: tuple[Hook, ...] = ()):
        self.before = before
        self.after = after

    @classmethod
    def from_file(cls, path: Path) -> "HookRunner":
        try:
            with path.open(encoding="utf-8") as stream:
                raw = stream.read(100_001)
            if len(raw) > 100_000:
                raise HookError("Hook configuration exceeds 100,000 characters.")
            config = json.loads(raw)
            if not isinstance(config, dict) or set(config) - {
                "before_tool",
                "after_tool",
            }:
                raise HookError("Expected before_tool and/or after_tool arrays.")
            groups = []
            for key in ("before_tool", "after_tool"):
                entries = config.get(key, [])
                if not isinstance(entries, list) or len(entries) > 20:
                    raise HookError("Each hook phase supports at most 20 hooks.")
                hooks = []
                for entry in entries:
                    if not isinstance(entry, dict) or set(entry) - {
                        "command",
                        "tools",
                        "timeout",
                    }:
                        raise HookError("Invalid hook entry.")
                    command = entry.get("command")
                    names = entry.get("tools", ["*"])
                    timeout = entry.get("timeout", 10.0)
                    if (
                        not isinstance(command, list)
                        or not command
                        or not all(isinstance(arg, str) and arg for arg in command)
                    ):
                        raise HookError("Hook command must be a nonempty argv array.")
                    if (
                        not isinstance(names, list)
                        or not names
                        or not all(isinstance(name, str) and name for name in names)
                    ):
                        raise HookError("Hook tools must be a nonempty name array.")
                    if (
                        isinstance(timeout, bool)
                        or not isinstance(timeout, (int, float))
                        or not math.isfinite(timeout)
                        or not 0 < timeout <= 60
                    ):
                        raise HookError("Hook timeout must be in (0, 60] seconds.")
                    hooks.append(Hook(tuple(command), tuple(names), float(timeout)))
                groups.append(tuple(hooks))
            return cls(*groups)
        except (OSError, ValueError) as exc:
            raise HookError(f"Cannot load hooks: {exc}") from exc

    def _invoke(self, hook: Hook, payload: dict, workspace: Path) -> dict:
        # Regular files avoid pipe deadlocks and unbounded communicate() buffers.
        with (
            tempfile.TemporaryFile() as stdin,
            tempfile.TemporaryFile() as stdout,
            tempfile.TemporaryFile() as stderr,
        ):
            stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            stdin.seek(0)
            process = subprocess.Popen(
                hook.command,
                cwd=workspace,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                shell=False,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                if os.name == "nt"
                else 0,
            )
            deadline = time.monotonic() + hook.timeout
            try:
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        raise HookError("Hook timed out; tool execution stopped.")
                    if (
                        os.fstat(stdout.fileno()).st_size > 16_384
                        or os.fstat(stderr.fileno()).st_size > 16_384
                    ):
                        raise HookError("Hook output exceeded 16 KiB.")
                    time.sleep(0.01)
                if process.returncode != 0:
                    raise HookError(f"Hook exited with code {process.returncode}.")
                stdout.seek(0)
                output = stdout.read(16_385)
                if len(output) > 16_384:
                    raise HookError("Hook output exceeded 16 KiB.")
                value = json.loads(output or b"{}")
                if not isinstance(value, dict) or set(value) - {
                    "block",
                    "reason",
                    "append",
                }:
                    raise HookError("Invalid hook JSON response.")
                if not isinstance(value.get("block", False), bool) or not all(
                    isinstance(value.get(key, ""), str) for key in ("reason", "append")
                ):
                    raise HookError("Invalid hook response fields.")
                return value
            except KeyboardInterrupt as exc:
                raise TurnCancelled(
                    "Hook cancelled; inspect any hook side effects."
                ) from exc
            finally:
                with protect_cleanup():
                    _terminate_process_tree(process)
                    process.wait()

    def execute(
        self,
        name: str,
        arguments: object,
        workspace: Path,
        execute: Callable[[], ToolResult],
    ) -> ToolResult:
        payload = {"tool_name": name, "tool_input": arguments}
        try:
            for hook in self.before:
                if "*" not in hook.tools and name not in hook.tools:
                    continue
                response = self._invoke(
                    hook, {**payload, "event": "before_tool"}, workspace
                )
                if response.get("block"):
                    return ToolResult(
                        "Hook blocked tool: "
                        + response.get("reason", "No reason supplied."),
                        True,
                    )
        except (OSError, ValueError) as exc:
            return ToolResult(f"Before-tool hook failed: {exc}", True)
        result = execute()
        try:
            for hook in self.after:
                if "*" not in hook.tools and name not in hook.tools:
                    continue
                response = self._invoke(
                    hook,
                    {
                        **payload,
                        "event": "after_tool",
                        "result": {
                            "content": result.content,
                            "is_error": result.is_error,
                        },
                    },
                    workspace,
                )
                addition = response.get("append", "")
                if response.get("block"):
                    addition += "\nAfter-tool hook flagged result: " + response.get(
                        "reason", ""
                    )
                result = ToolResult(
                    result.content + ("\n[Hook]\n" + addition if addition else ""),
                    result.is_error or response.get("block", False),
                )
        except (OSError, ValueError) as exc:
            return ToolResult(result.content + f"\nAfter-tool hook failed: {exc}", True)
        return result
