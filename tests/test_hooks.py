"""Hook ordering, denial, timeouts and read-only boundaries."""

import json
import sys

import pytest

from mclaude.agent import run_agent
from mclaude.config import ModelConfig
from mclaude.hooks import Hook, HookError, HookRunner
from mclaude.provider import ModelResponse, TextBlock, ToolUseBlock
from mclaude.tools import ToolResult


def hook(script, timeout=5):
    return Hook((sys.executable, "-c", script), timeout=timeout)


def test_before_can_block_without_executing(tmp_path):
    runner = HookRunner(before=(hook('print(\'{"block":true,"reason":"policy"}\')'),))
    result = runner.execute(
        "create_file", {}, tmp_path, lambda: pytest.fail("must not execute")
    )
    assert result.is_error and "policy" in result.content


def test_after_receives_result_and_appends(tmp_path):
    script = (
        "import json,sys; p=json.load(sys.stdin); "
        'assert p["event"]=="after_tool"; '
        'print(json.dumps({"append":p["result"]["content"]+" reviewed"}))'
    )
    runner = HookRunner(after=(hook(script),))
    result = runner.execute("read_file", {}, tmp_path, lambda: ToolResult("original"))
    assert not result.is_error
    assert result.content == "original\n[Hook]\noriginal reviewed"


@pytest.mark.parametrize(
    "script", ["import time; time.sleep(5)", "print('not JSON')", "raise SystemExit(2)"]
)
def test_before_failure_is_closed(tmp_path, script):
    runner = HookRunner(before=(hook(script, 0.1),))
    assert runner.execute(
        "read_file", {}, tmp_path, lambda: pytest.fail("must not execute")
    ).is_error


def test_config_validation(tmp_path):
    path = tmp_path / "hooks.json"
    path.write_text(
        json.dumps(
            {"before_tool": [{"command": [sys.executable, "hook.py"], "timeout": 1}]}
        )
    )
    assert HookRunner.from_file(path).before[0].timeout == 1
    path.write_text('{"before_tool":[{"command":"shell string"}]}')
    with pytest.raises(HookError):
        HookRunner.from_file(path)


def test_planning_never_executes_hooks(tmp_path):
    (tmp_path / "note").write_text("ok")
    responses = iter(
        [
            ModelResponse(
                (ToolUseBlock("t", "read_file", {"path": "note"}),), "tool_use"
            ),
            ModelResponse((TextBlock("done"),), "end_turn"),
        ]
    )
    runner = HookRunner(before=(hook('from pathlib import Path; Path("bad").touch()'),))
    run_agent(
        "plan",
        ModelConfig(api_key="test", model="test"),
        workspace=tmp_path,
        request=lambda *a, **k: next(responses),
        planning=True,
        hooks=runner,
    )
    assert not (tmp_path / "bad").exists()
