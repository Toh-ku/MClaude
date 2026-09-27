"""Durable, conflict-aware checkpoints for Agent file edits."""

import base64
import binascii
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from mclaude.session import _state_root, _workspace_key

_CHECKPOINT_ID = re.compile(r"[0-9a-f]{32}")


class CheckpointError(RuntimeError):
    """A checkpoint could not be created or restored safely."""


@dataclass(frozen=True)
class EditCheckpoint:
    id: str
    path: str
    created_at: str
    restored: bool


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class CheckpointStore:
    """Store snapshots outside the workspace and restore with hash checks."""

    def __init__(self, workspace: Path, root: Path | None = None) -> None:
        self.workspace = workspace.resolve()
        base = (root or _state_root()).resolve()
        self.directory = base / "checkpoints" / _workspace_key(self.workspace)

    def save(self, path: Path, before: bytes | None, after: bytes) -> str:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(self.workspace).as_posix()
        except ValueError as exc:
            raise CheckpointError("Checkpoint path is outside the workspace.") from exc
        checkpoint_id = uuid4().hex
        record = {
            "version": 1,
            "id": checkpoint_id,
            "workspace": str(self.workspace),
            "path": relative,
            "created_at": datetime.now(UTC).isoformat(),
            "before": (
                base64.b64encode(before).decode("ascii") if before is not None else None
            ),
            "before_mode": path.stat().st_mode if before is not None else None,
            "after_sha256": _digest(after),
            "restored": False,
        }
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            destination = self.directory / f"{checkpoint_id}.json"
            self._write_record(destination, record, exclusive=True)
        except OSError as exc:
            raise CheckpointError(f"Could not save edit checkpoint: {exc}") from exc
        return checkpoint_id

    def list(self) -> list[EditCheckpoint]:
        if not self.directory.exists():
            return []
        checkpoints = []
        try:
            paths = sorted(
                self.directory.glob("*.json"),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
            for path in paths:
                record = self._read_record(path)
                checkpoints.append(
                    EditCheckpoint(
                        id=record["id"],
                        path=record["path"],
                        created_at=record["created_at"],
                        restored=record["restored"],
                    )
                )
        except OSError as exc:
            raise CheckpointError(f"Could not list edit checkpoints: {exc}") from exc
        return checkpoints

    def discard(self, checkpoint_id: str) -> None:
        if not _CHECKPOINT_ID.fullmatch(checkpoint_id):
            return
        try:
            (self.directory / f"{checkpoint_id}.json").unlink(missing_ok=True)
        except OSError:
            pass

    def restore(self, checkpoint_id: str) -> str:
        checkpoint_id = checkpoint_id.strip().casefold()
        if not _CHECKPOINT_ID.fullmatch(checkpoint_id):
            raise CheckpointError("Checkpoint ID must be 32 hexadecimal characters.")
        path = self.directory / f"{checkpoint_id}.json"
        if not path.is_file():
            raise CheckpointError(f"Edit checkpoint not found: {checkpoint_id}")
        record = self._read_record(path)
        if record["restored"]:
            raise CheckpointError("This edit checkpoint has already been restored.")
        target = (self.workspace / record["path"]).resolve()
        if not target.is_relative_to(self.workspace):
            raise CheckpointError("Checkpoint target is outside the workspace.")
        try:
            current = target.read_bytes()
        except FileNotFoundError as exc:
            raise CheckpointError(
                "Checkpoint conflict: the edited file no longer exists."
            ) from exc
        except OSError as exc:
            raise CheckpointError(f"Could not read checkpoint target: {exc}") from exc
        if _digest(current) != record["after_sha256"]:
            raise CheckpointError(
                "Checkpoint conflict: the file changed after the Agent edit; "
                "refusing to overwrite it."
            )

        encoded_before = record["before"]
        if encoded_before is None:
            try:
                if _digest(target.read_bytes()) != record["after_sha256"]:
                    raise CheckpointError(
                        "Checkpoint conflict: the file changed during restore."
                    )
                target.unlink()
            except OSError as exc:
                raise CheckpointError(f"Could not remove created file: {exc}") from exc
            action = f"Removed {record['path']}"
        else:
            try:
                before = base64.b64decode(encoded_before, validate=True)
            except (TypeError, ValueError, binascii.Error) as exc:
                raise CheckpointError("Edit checkpoint snapshot is invalid.") from exc
            self._restore_bytes(
                target,
                expected=current,
                restored=before,
                mode=record["before_mode"],
            )
            action = f"Restored {record['path']}"
        record["restored"] = True
        record["restored_at"] = datetime.now(UTC).isoformat()
        try:
            self._write_record(path, record, exclusive=False)
        except OSError as exc:
            raise CheckpointError(
                f"File was restored but checkpoint status could not be updated: {exc}"
            ) from exc
        return action

    def _read_record(self, path: Path) -> dict[str, Any]:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CheckpointError(f"Could not read edit checkpoint: {exc}") from exc
        required = {
            "version",
            "id",
            "workspace",
            "path",
            "created_at",
            "before",
            "before_mode",
            "after_sha256",
            "restored",
        }
        if not isinstance(record, dict) or not required.issubset(record):
            raise CheckpointError("Edit checkpoint is invalid.")
        if (
            record["version"] != 1
            or not isinstance(record["id"], str)
            or record["id"] != path.stem
        ):
            raise CheckpointError("Edit checkpoint identity is invalid.")
        if not isinstance(record["workspace"], str):
            raise CheckpointError("Edit checkpoint workspace is invalid.")
        if os.path.normcase(record["workspace"]) != os.path.normcase(
            str(self.workspace)
        ):
            raise CheckpointError("Edit checkpoint belongs to another workspace.")
        if (
            not isinstance(record["path"], str)
            or not record["path"]
            or not isinstance(record["created_at"], str)
            or not isinstance(record["restored"], bool)
            or record["before"] is not None
            and not isinstance(record["before"], str)
            or record["before_mode"] is not None
            and not isinstance(record["before_mode"], int)
            or not isinstance(record["after_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", record["after_sha256"])
        ):
            raise CheckpointError("Edit checkpoint fields are invalid.")
        return record

    @staticmethod
    def _write_record(path: Path, record: dict[str, Any], *, exclusive: bool) -> None:
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        if exclusive:
            with path.open("x", encoding="utf-8", newline="\n") as file:
                file.write(encoded + "\n")
                file.flush()
                os.fsync(file.fileno())
            return
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                file.write(encoded + "\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _restore_bytes(
        path: Path, *, expected: bytes, restored: bytes, mode: int | None
    ) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".restore", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(restored)
                file.flush()
                os.fsync(file.fileno())
            if path.read_bytes() != expected:
                raise CheckpointError(
                    "Checkpoint conflict: the file changed during restore."
                )
            if mode is not None:
                os.chmod(temporary, mode)
            os.replace(temporary, path)
        except OSError as exc:
            raise CheckpointError(f"Could not restore checkpoint: {exc}") from exc
        finally:
            temporary.unlink(missing_ok=True)
