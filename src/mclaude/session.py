"""Append-only persistent conversation sessions."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from mclaude.processes import pid_is_running
from mclaude.tasks import TaskBoard, validate_tasks

SESSION_VERSION = 1
MAX_EVENT_BYTES = 2_000_000
_SESSION_ID = re.compile(r"[0-9a-f]{32}")


class SessionError(RuntimeError):
    """A session could not be created, loaded, or updated safely."""


@dataclass(frozen=True)
class SessionInfo:
    id: str
    updated_at: str
    model: str
    first_request: str
    name: str | None = None


@dataclass(frozen=True)
class SessionMatch:
    id: str
    timestamp: str
    role: str
    excerpt: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _workspace_key(workspace: Path) -> str:
    normalized = os.path.normcase(str(workspace.resolve()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def _same_workspace(left: str, right: Path) -> bool:
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(
        str(right.resolve())
    )


def _state_root() -> Path:
    override = os.environ.get("MCLAUDE_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        return (
            Path(base) / "MClaude"
            if base
            else Path.home() / "AppData" / "Local" / "MClaude"
        )
    xdg_state = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local/state"
    return base / "mclaude"


def _validate_session_id(session_id: str) -> str:
    value = session_id.strip().casefold()
    if not _SESSION_ID.fullmatch(value):
        raise SessionError("Session ID must be 32 hexadecimal characters.")
    return value


def _pid_is_running(pid: int) -> bool:
    return pid_is_running(pid)


def _acquire_lock(path: Path) -> str:
    token = f"{os.getpid()} {uuid4().hex}"
    for _ in range(2):
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                existing = path.read_text(encoding="utf-8").split(maxsplit=1)
                pid = int(existing[0])
            except (OSError, ValueError, IndexError):
                pid = -1
            if _pid_is_running(pid):
                raise SessionError(
                    "This session is already open in another MClaude process."
                ) from None
            try:
                path.unlink()
            except OSError as exc:
                raise SessionError(
                    f"Could not remove stale session lock: {exc}"
                ) from exc
            continue
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                file.write(token + "\n")
                file.flush()
                os.fsync(file.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return token
    raise SessionError("Could not acquire the session lock.")


def _validate_message(message: Any) -> dict[str, Any]:
    if not isinstance(message, dict) or set(message) != {"role", "content"}:
        raise SessionError("Session contains an invalid message object.")
    role = message["role"]
    content = message["content"]
    if role not in {"user", "assistant"}:
        raise SessionError("Session contains an invalid message role.")
    if isinstance(content, str):
        if role != "user":
            raise SessionError("Session contains invalid assistant text content.")
        return message
    if not isinstance(content, list):
        raise SessionError("Session contains invalid message content.")
    for block in content:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise SessionError("Session contains an invalid content block.")
        block_type = block["type"]
        if role == "assistant" and block_type == "text":
            if set(block) != {"type", "text"} or not isinstance(block["text"], str):
                raise SessionError("Session contains an invalid text block.")
        elif role == "assistant" and block_type == "tool_use":
            if set(block) != {"type", "id", "name", "input"}:
                raise SessionError("Session contains an invalid tool call.")
            if not all(isinstance(block[key], str) for key in ("id", "name")):
                raise SessionError("Session contains an invalid tool call.")
            if not isinstance(block["input"], dict):
                raise SessionError("Session contains invalid tool input.")
        elif role == "user" and block_type == "tool_result":
            _validate_tool_result(block)
        else:
            raise SessionError("Session contains an unsupported content block.")
    return message


def _validate_tool_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict) or set(result) != {
        "type",
        "tool_use_id",
        "content",
        "is_error",
    }:
        raise SessionError("Session contains an invalid tool result.")
    if result.get("type") != "tool_result":
        raise SessionError("Session contains an invalid tool result type.")
    if not isinstance(result["tool_use_id"], str) or not isinstance(
        result["content"], str
    ):
        raise SessionError("Session contains an invalid tool result.")
    if not isinstance(result["is_error"], bool):
        raise SessionError("Session contains an invalid tool result status.")
    return result


def _validate_complete_history(history: Any) -> list[dict[str, Any]]:
    if not isinstance(history, list):
        raise SessionError("Session contains invalid compacted history.")
    validated = [_validate_message(message) for message in history]
    pending: set[str] = set()
    for message in validated:
        content = message["content"]
        if message["role"] == "assistant" and isinstance(content, list):
            if pending:
                raise SessionError("Compacted history has unfinished tool calls.")
            pending = {block["id"] for block in content if block["type"] == "tool_use"}
        elif message["role"] == "user" and isinstance(content, list):
            result_ids = {
                block["tool_use_id"]
                for block in content
                if block["type"] == "tool_result"
            }
            if result_ids != pending:
                raise SessionError("Compacted history has mismatched tool results.")
            pending.clear()
    if pending:
        raise SessionError("Compacted history has unfinished tool calls.")
    return validated


@dataclass
class Session:
    """One locked JSONL session and its reconstructed model history."""

    id: str
    workspace: Path
    path: Path
    history: list[dict[str, Any]]
    _lock_path: Path
    _lock_token: str
    _sequence: int
    recovery_warning: str | None = None
    task_board: TaskBoard = field(default_factory=TaskBoard)
    name: str | None = None
    _closed: bool = field(default=False, init=False)

    def record_message(self, message: dict[str, Any]) -> None:
        _validate_message(message)
        self._append({"type": "message.appended", "message": message})

    def record_tool_result(self, result: dict[str, Any]) -> None:
        _validate_tool_result(result)
        self._append({"type": "tool.result", "result": result})

    def record_turn(self, status: str) -> None:
        if status not in {"ok", "error", "cancelled", "truncated"}:
            raise ValueError(f"Unsupported turn status: {status}")
        self._append({"type": "turn.finished", "status": status})

    def record_tasks(self, tasks: list[dict[str, str]]) -> None:
        updated = validate_tasks(tasks)
        self._append({"type": "tasks.updated", "tasks": updated})
        self.task_board.tasks = updated

    def record_compaction(self, history: list[dict[str, Any]]) -> None:
        validated = _validate_complete_history(history)
        self._append({"type": "context.compacted", "history": validated})

    def record_name(self, name: str) -> None:
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ValueError("Session name must be 1-120 characters.")
        self._append({"type": "session.named", "name": name.strip()})
        self.name = name.strip()

    def _append(self, event: dict[str, Any]) -> None:
        if self._closed:
            raise SessionError("Cannot update a closed session.")
        self._sequence += 1
        record = {"seq": self._sequence, "timestamp": _now(), **event}
        try:
            encoded = json.dumps(
                record, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            self._sequence -= 1
            raise SessionError(f"Could not serialize session event: {exc}") from exc
        if len(encoded) > MAX_EVENT_BYTES:
            self._sequence -= 1
            raise SessionError("Session event exceeds the 2 MB safety limit.")
        try:
            with self.path.open("ab") as file:
                file.write(encoded + b"\n")
                file.flush()
                os.fsync(file.fileno())
        except OSError as exc:
            self._sequence -= 1
            raise SessionError(f"Could not update session: {exc}") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            current = self._lock_path.read_text(encoding="utf-8").strip()
        except OSError:
            return
        if current == self._lock_token:
            try:
                self._lock_path.unlink()
            except OSError:
                pass


class SessionStore:
    """Create and locate sessions stored outside the workspace."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or _state_root()).resolve() / "sessions"

    def _workspace_logs(self, workspace: Path) -> list[Path]:
        directory = self.root / _workspace_key(workspace)
        try:
            return sorted(
                directory.glob("*.jsonl"),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
        except OSError as exc:
            raise SessionError(f"Could not inspect saved sessions: {exc}") from exc

    def _inspect_records(self, path: Path, workspace: Path) -> list[dict[str, Any]]:
        """Read a possibly active journal without taking a writer lock."""
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise SessionError(f"Could not read session: {exc}") from exc
        lines = data.split(b"\n")[:-1]
        records: list[dict[str, Any]] = []
        for raw in lines:
            if len(raw) > MAX_EVENT_BYTES:
                raise SessionError(f"Session record is too large: {path.name}")
            try:
                record = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SessionError(f"Session record is corrupt: {path.name}") from exc
            if not isinstance(record, dict):
                raise SessionError(f"Session record is invalid: {path.name}")
            records.append(record)
        header = records[0] if records else None
        if (
            not isinstance(header, dict)
            or header.get("type") != "session.created"
            or header.get("version") != SESSION_VERSION
            or header.get("id") != path.stem
            or not isinstance(header.get("workspace"), str)
            or not _same_workspace(header["workspace"], workspace)
        ):
            raise SessionError(f"Session header is invalid: {path.name}")
        return records

    def list_sessions(self, workspace: Path) -> list[SessionInfo]:
        workspace = workspace.resolve()
        sessions = []
        for path in self._workspace_logs(workspace):
            records = self._inspect_records(path, workspace)
            first_request = ""
            name = None
            for record in records[1:]:
                message = record.get("message")
                if (
                    record.get("type") == "message.appended"
                    and isinstance(message, dict)
                    and message.get("role") == "user"
                    and isinstance(message.get("content"), str)
                    and not first_request
                ):
                    first_request = " ".join(message["content"].split())[:120]
                if record.get("type") == "session.named":
                    name = record.get("name")
            sessions.append(
                SessionInfo(
                    id=path.stem,
                    updated_at=str(records[-1].get("timestamp", "")),
                    model=str(records[0].get("model", "")),
                    first_request=first_request,
                    name=name,
                )
            )
        return sessions

    def rename(self, session_id: str, workspace: Path, name: str) -> None:
        session = self.resume(session_id, workspace)
        try:
            session.record_name(name)
        finally:
            session.close()

    def search_sessions(
        self, workspace: Path, query: str, *, max_results: int = 20
    ) -> list[SessionMatch]:
        if not query.strip():
            raise ValueError("Search query must not be empty.")
        if max_results <= 0:
            raise ValueError("max_results must be positive.")
        workspace = workspace.resolve()
        needle = query.casefold()
        matches: list[SessionMatch] = []
        for path in self._workspace_logs(workspace):
            for record in reversed(self._inspect_records(path, workspace)[1:]):
                if record.get("type") != "message.appended":
                    continue
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                role = message.get("role")
                if role not in {"user", "assistant"}:
                    continue
                content = message.get("content")
                texts = (
                    [content]
                    if isinstance(content, str)
                    else [
                        block["text"]
                        for block in content
                        if isinstance(block, dict)
                        and block.get("type") == "text"
                        and isinstance(block.get("text"), str)
                    ]
                    if isinstance(content, list)
                    else []
                )
                for value in texts:
                    offset = value.casefold().find(needle)
                    if offset < 0:
                        continue
                    start = max(0, offset - 80)
                    excerpt = " ".join(value[start : offset + len(query) + 120].split())
                    matches.append(
                        SessionMatch(
                            id=path.stem,
                            timestamp=str(record.get("timestamp", "")),
                            role=role,
                            excerpt=excerpt,
                        )
                    )
                    if len(matches) >= max_results:
                        return matches
        return matches

    def create(self, workspace: Path, model: str) -> Session:
        workspace = workspace.resolve()
        directory = self.root / _workspace_key(workspace)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SessionError(
                f"Could not create the session directory: {exc}"
            ) from exc
        session_id = uuid4().hex
        path = directory / f"{session_id}.jsonl"
        lock_path = directory / f"{session_id}.lock"
        lock_token = _acquire_lock(lock_path)
        header = {
            "seq": 0,
            "timestamp": _now(),
            "type": "session.created",
            "version": SESSION_VERSION,
            "id": session_id,
            "workspace": str(workspace),
            "model": model,
        }
        try:
            encoded = json.dumps(
                header, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            with path.open("xb") as file:
                file.write(encoded + b"\n")
                file.flush()
                os.fsync(file.fileno())
            try:
                path.chmod(0o600)
            except OSError:
                pass
        except (OSError, TypeError, ValueError) as exc:
            lock_path.unlink(missing_ok=True)
            raise SessionError(f"Could not create session: {exc}") from exc
        return Session(
            id=session_id,
            workspace=workspace,
            path=path,
            history=[],
            _lock_path=lock_path,
            _lock_token=lock_token,
            _sequence=0,
        )

    def resume(self, session_id: str, workspace: Path) -> Session:
        session_id = _validate_session_id(session_id)
        workspace = workspace.resolve()
        directory = self.root / _workspace_key(workspace)
        path = directory / f"{session_id}.jsonl"
        if not path.is_file():
            matches = list(self.root.glob(f"*/{session_id}.jsonl"))
            if matches:
                raise SessionError(
                    "That session belongs to a different workspace and cannot be "
                    "resumed here."
                )
            raise SessionError(f"Session not found: {session_id}")
        return self._load(path, workspace)

    def continue_latest(self, workspace: Path) -> Session:
        workspace = workspace.resolve()
        candidates = self._workspace_logs(workspace)
        if not candidates:
            raise SessionError("No saved session exists for this workspace.")
        return self._load(candidates[0], workspace)

    def _load(self, path: Path, workspace: Path) -> Session:
        lock_path = path.with_suffix(".lock")
        lock_token = _acquire_lock(lock_path)
        try:
            records, tail_repaired = self._read_records(path)
            header = records[0] if records else None
            if not isinstance(header, dict) or header.get("type") != "session.created":
                raise SessionError("Session header is missing or invalid.")
            if header.get("version") != SESSION_VERSION:
                raise SessionError(
                    f"Unsupported session version: {header.get('version')!r}."
                )
            session_id = _validate_session_id(str(header.get("id", "")))
            if session_id != path.stem:
                raise SessionError("Session ID does not match its file name.")
            saved_workspace = header.get("workspace")
            if not isinstance(saved_workspace, str) or not _same_workspace(
                saved_workspace, workspace
            ):
                raise SessionError(
                    "That session belongs to a different workspace and cannot be "
                    "resumed here."
                )
            history, pending, sequence, task_board, name = self._project(records[1:])
            session = Session(
                id=session_id,
                workspace=workspace,
                path=path,
                history=history,
                _lock_path=lock_path,
                _lock_token=lock_token,
                _sequence=sequence,
                task_board=task_board,
                name=name,
            )
            warnings = []
            if tail_repaired:
                warnings.append("a partial final log record was discarded")
            if pending:
                if (
                    history
                    and history[-1]["role"] == "user"
                    and isinstance(history[-1]["content"], list)
                ):
                    result_message = history[-1]
                else:
                    result_message = {"role": "user", "content": []}
                    history.append(result_message)
                for tool_id in pending:
                    result = {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": (
                            "The previous process ended before this tool result was "
                            "saved. The tool may have partially executed; inspect the "
                            "workspace before retrying."
                        ),
                        "is_error": True,
                    }
                    result_message["content"].append(result)
                    session.record_tool_result(result)
                warnings.append(
                    "unfinished tool calls were marked as uncertain and were not "
                    "replayed"
                )
            if warnings:
                session.recovery_warning = "; ".join(warnings) + "."
            return session
        except BaseException:
            try:
                if lock_path.read_text(encoding="utf-8").strip() == lock_token:
                    lock_path.unlink()
            except OSError:
                pass
            raise

    def _read_records(self, path: Path) -> tuple[list[dict[str, Any]], bool]:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise SessionError(f"Could not read session: {exc}") from exc
        repaired = False
        if data and not data.endswith(b"\n"):
            boundary = data.rfind(b"\n") + 1
            data = data[:boundary]
            try:
                with path.open("r+b") as file:
                    file.truncate(boundary)
                    file.flush()
                    os.fsync(file.fileno())
            except OSError as exc:
                raise SessionError(
                    f"Could not repair partial session tail: {exc}"
                ) from exc
            repaired = True
        records = []
        for line_number, raw_line in enumerate(data.splitlines(), start=1):
            if len(raw_line) > MAX_EVENT_BYTES:
                raise SessionError(
                    f"Session record {line_number} exceeds the 2 MB safety limit."
                )
            try:
                record = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SessionError(
                    f"Session record {line_number} is corrupt: {exc}"
                ) from exc
            if not isinstance(record, dict):
                raise SessionError(f"Session record {line_number} is not an object.")
            records.append(record)
        return records, repaired

    def _project(
        self, records: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[str], int, TaskBoard, str | None]:
        history: list[dict[str, Any]] = []
        task_board = TaskBoard()
        name = None
        pending: list[str] = []
        result_message: dict[str, Any] | None = None
        sequence = 0
        for record in records:
            event_sequence = record.get("seq")
            if not isinstance(event_sequence, int) or event_sequence != sequence + 1:
                raise SessionError("Session event sequence is invalid.")
            sequence = event_sequence
            event_type = record.get("type")
            if event_type == "message.appended":
                if pending:
                    raise SessionError(
                        "Session contains a message before its tool results are "
                        "complete."
                    )
                message = _validate_message(record.get("message"))
                history.append(message)
                result_message = None
                if message["role"] == "assistant":
                    pending = [
                        block["id"]
                        for block in message["content"]
                        if block["type"] == "tool_use"
                    ]
            elif event_type == "tool.result":
                result = _validate_tool_result(record.get("result"))
                tool_id = result["tool_use_id"]
                if tool_id not in pending:
                    raise SessionError(
                        "Session contains an unexpected or duplicate tool result."
                    )
                if result_message is None:
                    result_message = {"role": "user", "content": []}
                    history.append(result_message)
                result_message["content"].append(result)
                pending.remove(tool_id)
            elif event_type == "turn.finished":
                if record.get("status") not in {
                    "ok",
                    "error",
                    "cancelled",
                    "truncated",
                }:
                    raise SessionError("Session contains an invalid turn status.")
            elif event_type == "tasks.updated":
                try:
                    task_board.tasks = validate_tasks(record.get("tasks"))
                except ValueError as exc:
                    raise SessionError(str(exc)) from exc
            elif event_type == "context.compacted":
                if pending:
                    raise SessionError(
                        "Session compaction occurred before tool results were complete."
                    )
                history = _validate_complete_history(record.get("history"))
                result_message = None
            elif event_type == "session.named":
                value = record.get("name")
                if not isinstance(value, str) or not value.strip() or len(value) > 120:
                    raise SessionError("Session contains an invalid name.")
                name = value
            else:
                raise SessionError(f"Unsupported session event: {event_type!r}.")
        return history, pending, sequence, task_board, name
