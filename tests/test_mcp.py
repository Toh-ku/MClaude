"""Exercise real newline-delimited stdio JSON-RPC with a local fake server."""

import json
import sys

import pytest

from mclaude.agent import run_agent
from mclaude.config import ModelConfig
from mclaude.mcp import MCPError, MCPRegistry, ServerConfig, StdioClient
from mclaude.permissions import PermissionGate
from mclaude.provider import ModelResponse, TextBlock, ToolUseBlock

SERVER = """
import json, sys, time
from pathlib import Path
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
mode = sys.argv[1]
Path("started").touch()
initialized = False
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "test", "version": "1"}}
    elif method == "notifications/initialized":
        initialized = True
        continue
    elif method == "tools/list":
        assert initialized
        second = msg["params"].get("cursor") == "page2"
        result = {"tools": [{"name": "second" if second else "echo",
                   "description": "echo text", "inputSchema": {"type": "object"},
                   "annotations": {"readOnlyHint": True}}]}
        if not second:
            result["nextCursor"] = "page2"
    elif method == "tools/call":
        Path("called").touch()
        if mode == "timeout":
            time.sleep(10)
        if mode == "disconnect":
            break
        if mode == "invalid":
            print("not json", flush=True)
            continue
        text = msg["params"]["arguments"].get("text", "ok")
        result = {"content": [{"type": "text", "text": text}],
                  "isError": mode == "error"}
    else:
        continue
    notification = {"jsonrpc":"2.0", "method":"notifications/message", "params":{}}
    print(json.dumps(notification), flush=True)
    print(json.dumps({"jsonrpc":"2.0", "id":msg["id"], "result":result}), flush=True)
"""


def registry(tmp_path, mode="normal", timeout=3):
    path = tmp_path / "server.py"
    path.write_text(SERVER, encoding="utf-8")
    config = ServerConfig("demo", (sys.executable, "-u", str(path), mode), timeout)
    return MCPRegistry((config,), tmp_path)


def test_initialize_pagination_and_call(tmp_path):
    mcp = registry(tmp_path)
    try:
        mcp.connect()
        client = mcp.clients[0]
        assert [t["name"] for t in mcp.definitions] == [
            "mcp_demo_echo",
            "mcp_demo_second",
        ]
        result = mcp.execute("mcp_demo_echo", {"text": "中文"})
        assert result.content == "中文" and not result.is_error
    finally:
        mcp.close()
    assert client.process.poll() is not None
    assert not client.reader.is_alive() and not client.writer.is_alive()


@pytest.mark.parametrize("mode", ["timeout", "disconnect", "invalid", "error"])
def test_call_failures_return_error_without_retry(tmp_path, mode):
    mcp = registry(tmp_path, mode, timeout=0.5)
    try:
        mcp.connect()
        assert mcp.execute("mcp_demo_echo", {}).is_error
        if mode != "error":
            assert mcp.clients[0].closed
    finally:
        mcp.close()


@pytest.mark.parametrize("allow", [False, True])
def test_external_tool_uses_permission_gate_despite_readonly_hint(tmp_path, allow):
    mcp = registry(tmp_path)
    prompts = []
    responses = iter(
        [
            ModelResponse((ToolUseBlock("call", "mcp_demo_echo", {}),), "tool_use"),
            ModelResponse((TextBlock("done"),), "end_turn"),
        ]
    )

    def prompt(request, reason):
        prompts.append(request)
        return allow

    try:
        run_agent(
            "echo",
            ModelConfig(api_key="test", model="test"),
            workspace=tmp_path,
            mcp=mcp,
            permission_gate=PermissionGate(prompt=prompt),
            request=lambda *a, **k: next(responses),
        )
    finally:
        mcp.close()
    assert len(prompts) == 1 and prompts[0].external
    assert (tmp_path / "called").exists() == allow


def test_planning_does_not_launch_server(tmp_path):
    mcp = registry(tmp_path)
    run_agent(
        "plan",
        ModelConfig(api_key="test", model="test"),
        workspace=tmp_path,
        mcp=mcp,
        planning=True,
        request=lambda *a, **k: ModelResponse((TextBlock("plan"),), "end_turn"),
    )
    assert not (tmp_path / "started").exists()


def test_invalid_config_and_protocol(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"mcpServers": {"test": {"command": sys.executable, "timeout": -1}}})
    )
    with pytest.raises(MCPError, match="timeout"):
        MCPRegistry.from_file(path, tmp_path)
    script = (
        "import sys,json; p=json.loads(sys.stdin.readline()); "
        'print(json.dumps({"jsonrpc":"2.0","id":p["id"],"result":'
        '{"protocolVersion":"unsupported"}}),flush=True)'
    )
    with pytest.raises(MCPError, match="version"):
        StdioClient(ServerConfig("bad", (sys.executable, "-c", script)), tmp_path)
