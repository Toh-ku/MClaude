"""Exercise real newline-delimited stdio JSON-RPC with a local fake server."""

import json
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


@contextmanager
def http_mcp_server(mode="json"):
    events = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            size = int(self.headers["Content-Length"])
            message = json.loads(self.rfile.read(size))
            events.append((message, dict(self.headers)))
            assert self.path == "/mcp"
            assert "application/json" in self.headers["Accept"]
            assert "text/event-stream" in self.headers["Accept"]
            if message.get("method") == "initialize":
                assert "Mcp-Session-Id" not in self.headers
                result = {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "remote", "version": "1"},
                }
            else:
                assert self.headers["Mcp-Session-Id"] == "test-session"
                assert self.headers["MCP-Protocol-Version"] == "2025-06-18"
                if message.get("method") == "tools/list":
                    result = {
                        "tools": [{"name": "echo", "inputSchema": {"type": "object"}}]
                    }
                elif message.get("method") == "tools/call":
                    if mode == "failure":
                        self.send_response(503)
                        self.end_headers()
                        return
                    result = {
                        "content": [
                            {
                                "type": "text",
                                "text": message["params"]["arguments"]["text"],
                            }
                        ]
                    }
                else:
                    self.send_response(202)
                    self.end_headers()
                    return
            payload = json.dumps(
                {"jsonrpc": "2.0", "id": message["id"], "result": result}
            ).encode()
            if mode in {"sse", "sse_ping"}:
                ping = b""
                if mode == "sse_ping" and message["method"] == "tools/call":
                    ping = (
                        b"data: "
                        + json.dumps(
                            {"jsonrpc": "2.0", "id": "server-ping", "method": "ping"}
                        ).encode()
                        + b"\n\n"
                    )
                payload = (
                    b": heartbeat\n\n"
                    + b"data: "
                    + json.dumps(
                        {"jsonrpc": "2.0", "method": "notifications/message"}
                    ).encode()
                    + b"\n\n"
                    + ping
                    + b"data: "
                    + payload
                    + b"\n\n"
                )
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/event-stream"
                if mode in {"sse", "sse_ping"}
                else "application/json",
            )
            if message["method"] == "initialize":
                self.send_header("Mcp-Session-Id", "test-session")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_DELETE(self):
            events.append(("DELETE", dict(self.headers)))
            self.send_response(405)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp", events
    finally:
        server.shutdown()
        worker.join()
        server.server_close()


@pytest.mark.parametrize("mode", ["json", "sse", "sse_ping"])
def test_streamable_http_discovery_call_and_session(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("MCP_TEST_TOKEN", "test-secret")
    with http_mcp_server(mode) as (url, events):
        path = tmp_path / "mcp.json"
        path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "remote": {
                            "url": url,
                            "headers": {"Authorization": "Bearer ${MCP_TEST_TOKEN}"},
                        }
                    }
                }
            )
        )
        mcp = MCPRegistry.from_file(path, tmp_path)
        try:
            mcp.connect()
            assert [tool["name"] for tool in mcp.definitions] == ["mcp_remote_echo"]
            assert mcp.execute("mcp_remote_echo", {"text": "中文"}).content == "中文"
        finally:
            mcp.close()
    assert [entry[0].get("method") for entry in events[:-1]] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ] + ([None] if mode == "sse_ping" else [])
    assert all(entry[1]["Authorization"] == "Bearer test-secret" for entry in events)
    assert events[-1][0] == "DELETE"


def test_http_tool_failure_is_not_retried(tmp_path):
    with http_mcp_server("failure") as (url, events):
        mcp = MCPRegistry((ServerConfig("remote", url=url),), tmp_path)
        try:
            mcp.connect()
            result = mcp.execute("mcp_remote_echo", {"text": "once"})
            assert result.is_error and "status 503" in result.content
        finally:
            mcp.close()
    assert (
        sum(
            message.get("method") == "tools/call"
            for message, _ in events
            if isinstance(message, dict)
        )
        == 1
    )


@pytest.mark.parametrize(
    "entry",
    [
        {"url": "ftp://example.com/mcp"},
        {"url": "https://example.com:abc/mcp"},
        {"url": "https://user:secret@example.com/mcp"},
        {"url": "https://example.com/mcp", "command": "python"},
        {"url": "https://example.com/mcp", "headers": {"Mcp-Session-Id": "x"}},
        {
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer ${MISSING_MCP_TOKEN}"},
        },
    ],
)
def test_invalid_http_config(tmp_path, monkeypatch, entry):
    monkeypatch.delenv("MISSING_MCP_TOKEN", raising=False)
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"bad": entry}}))
    with pytest.raises(MCPError):
        MCPRegistry.from_file(path, tmp_path)
