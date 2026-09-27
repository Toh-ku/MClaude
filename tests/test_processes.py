"""Liveness probes must never signal or terminate a Windows process."""

import os
import subprocess
import sys

from mclaude.processes import pid_is_running


def test_current_process_liveness_does_not_exit():
    # A child isolates the regression: the old Windows os.kill(pid, 0) would
    # return exit code 0 without ever printing this marker.
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; from mclaude.processes import pid_is_running; "
            "assert pid_is_running(os.getpid()); print('still alive')",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert probe.returncode == 0
    assert probe.stdout.strip() == "still alive"


def test_live_child_and_exited_child():
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )
    try:
        assert pid_is_running(child.pid)
    finally:
        child.communicate(timeout=10)
    assert not pid_is_running(child.pid)
    assert not pid_is_running(0)
    assert pid_is_running(os.getpid())
