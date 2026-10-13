"""Content versions for discoverable workspace files, independent of Git."""

import hashlib
import json
import os
from pathlib import Path

from mclaude.auth import config_path
from mclaude.cancellation import check_read_cancelled
from mclaude.session import _state_root
from mclaude.tools import _workspace_files


def workspace_version(workspace: Path) -> str:
    workspace = workspace.resolve()
    excluded = (
        (_state_root() / "sessions").resolve(),
        (_state_root() / "checkpoints").resolve(),
    )
    version = hashlib.sha256()
    for file in sorted(_workspace_files(workspace, strict=True)):
        check_read_cancelled()
        resolved = file.resolve()
        if resolved == config_path().resolve() or any(
            resolved.is_relative_to(directory) for directory in excluded
        ):
            continue
        content = hashlib.sha256()
        with resolved.open("rb") as handle:
            before = os.fstat(handle.fileno())
            while chunk := handle.read(65536):
                check_read_cancelled()
                content.update(chunk)
            after = os.fstat(handle.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise OSError("Workspace changed while computing its version.")
        entry = [
            file.relative_to(workspace).as_posix(),
            before.st_mode & 0o7777,
            content.hexdigest(),
        ]
        version.update(
            (
                json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode("utf-8")
        )
    return version.hexdigest()
