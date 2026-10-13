"""Verify bidirectional persisted data and locks using temporary directories."""

import json
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from mclaude.auth import ConfigStore
from mclaude.checkpoints import CheckpointStore
from mclaude.session import SessionError, SessionStore

repository = Path(__file__).resolve().parents[2]
fixture = json.loads(
    (repository / "contracts/fixtures/conversation.json").read_text(encoding="utf-8")
)
node = shutil.which("node")
if node is None:
    raise RuntimeError("Node.js is required for compatibility verification.")
bridge = repository / "ts/scripts/interop.mjs"

with TemporaryDirectory(prefix="mclaude-interop-") as temporary:
    workspace = Path(temporary) / "workspace"
    workspace.mkdir()
    state = Path(temporary) / "state"

    def invoke(operation: str, *arguments: str) -> dict:
        result = subprocess.run(
            [node, str(bridge), operation, str(workspace), str(state), *arguments],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        return json.loads(result.stdout)

    config = ConfigStore(state / "config.json")
    config.save(fixture["config"])
    sessions = SessionStore(root=state)
    session = sessions.create(workspace, fixture["config"]["model"])
    for message in fixture["history"]:
        if message["role"] == "user" and isinstance(message["content"], list):
            for result in message["content"]:
                session.record_tool_result(result)
        else:
            session.record_message(message)
    session.record_tasks(fixture["tasks"])
    session.record_name("Python 会话")
    session.record_turn("ok")
    assert invoke("check-lock", session.id)["locked"] is True
    session.close()
    target = workspace / "example.txt"
    target.write_bytes(fixture["before"].encode())
    checkpoints = CheckpointStore(workspace, root=state)
    checkpoint = checkpoints.save(
        target, fixture["before"].encode(), fixture["after"].encode()
    )
    target.write_bytes(fixture["after"].encode())
    report = invoke("read-python", session.id, checkpoint)
    assert report["name"] == "Python 会话"
    assert report["model"] == fixture["config"]["model"]
    assert target.read_bytes() == fixture["before"].encode()
    assert next(item for item in checkpoints.list() if item.id == checkpoint).restored

    report = invoke("create-ts")
    assert config.load()["api_key"] == fixture["config"]["api_key"]
    resumed = sessions.resume(report["id"], workspace)
    assert resumed.history == fixture["history"]
    assert resumed.task_board.tasks == fixture["tasks"]
    assert resumed.name == "TS 会话"
    resumed.close()
    checkpoints.restore(report["checkpoint"])
    assert target.read_bytes() == fixture["before"].encode()
    with subprocess.Popen(
        [node, str(bridge), "hold-lock", str(workspace), str(state), report["id"]],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    ) as holder:
        try:
            assert holder.stdout is not None
            assert json.loads(holder.stdout.readline())["locked"] is True
            try:
                sessions.resume(report["id"], workspace)
            except SessionError as error:
                assert "already open" in str(error)
            else:
                raise AssertionError("Python accepted an active TS writer lock.")
        finally:
            holder.communicate(input="\n", timeout=10)
    assert holder.returncode == 0

print("Python <-> TS config, history, tasks, checkpoints and locks verified.")
