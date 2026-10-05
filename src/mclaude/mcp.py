"""MCP clients for stdio and Streamable HTTP tool servers."""

import json
import math
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2

from mclaude.cancellation import protect_cleanup
from mclaude.tools import ToolResult, _terminate_process_tree

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = {PROTOCOL_VERSION, "2025-03-26", "2024-11-05"}
MAX_MESSAGE_BYTES = 1_000_000


class MCPError(RuntimeError):
    pass


@dataclass(frozen=True)
class ServerConfig:
    name: str
    command: tuple[str, ...] = ()
    timeout: float = 30.0
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict, repr=False)


class StdioClient:
    """One sequential connection. Reader/writer workers keep pipe I/O cancellable."""

    def __init__(self, config: ServerConfig, workspace: Path):
        self.config = config
        self.sequence = 0
        self.closed = False
        self.stopped = threading.Event()
        self.incoming: queue.Queue = queue.Queue(maxsize=64)
        self.outgoing: queue.Queue = queue.Queue(maxsize=64)
        self.process = subprocess.Popen(
            config.command,
            cwd=workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.writer = threading.Thread(target=self._write, daemon=True)
        self.reader.start()
        self.writer.start()
        try:
            result = self.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "mclaude", "version": "0.1.0"},
                },
            )
            if result.get("protocolVersion") not in SUPPORTED_VERSIONS:
                raise MCPError("Unsupported MCP protocol version.")
            if (
                not isinstance(result.get("capabilities"), dict)
                or "tools" not in result["capabilities"]
            ):
                raise MCPError("MCP server does not support tools.")
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except BaseException:
            self.close()
            raise

    def _offer(self, item: object) -> None:
        while not self.stopped.is_set():
            try:
                self.incoming.put(item, timeout=0.05)
                return
            except queue.Full:
                continue

    def _read(self) -> None:
        try:
            while not self.stopped.is_set():
                raw = self.process.stdout.readline(MAX_MESSAGE_BYTES + 1)
                if not raw:
                    raise MCPError("MCP server disconnected.")
                if len(raw) > MAX_MESSAGE_BYTES or not raw.endswith(b"\n"):
                    raise MCPError(
                        "MCP message exceeds the size limit or lacks newline."
                    )
                value = json.loads(raw)
                if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
                    raise MCPError("Invalid MCP JSON-RPC message.")
                self._offer(value)
        except (OSError, ValueError, MCPError):
            self._offer(
                MCPError("MCP stream closed or invalid; call will not be retried.")
            )

    def _write(self) -> None:
        try:
            while not self.stopped.is_set():
                try:
                    raw = self.outgoing.get(timeout=0.05)
                except queue.Empty:
                    continue
                self.process.stdin.write(raw)
                self.process.stdin.flush()
        except (OSError, ValueError):
            self._offer(MCPError("Could not write to MCP server."))

    def _send(self, message: dict) -> None:
        if self.closed:
            raise MCPError("MCP connection is closed.")
        raw = (
            json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
            + b"\n"
        )
        if len(raw) > MAX_MESSAGE_BYTES:
            raise MCPError("MCP request exceeds the size limit.")
        try:
            self.outgoing.put_nowait(raw)
        except queue.Full as exc:
            raise MCPError("MCP write queue is full.") from exc

    def request(self, method: str, params: dict) -> dict:
        self.sequence += 1
        request_id = self.sequence
        deadline = time.monotonic() + self.config.timeout
        try:
            self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MCPError(
                        "MCP request timed out; side effects may be partial."
                    )
                try:
                    message = self.incoming.get(timeout=min(0.05, remaining))
                except queue.Empty:
                    continue
                if isinstance(message, Exception):
                    raise message
                if "method" in message:
                    if "id" in message:
                        response = {"jsonrpc": "2.0", "id": message["id"]}
                        if message["method"] == "ping":
                            response["result"] = {}
                        else:
                            response["error"] = {
                                "code": -32601,
                                "message": "Unsupported client method",
                            }
                        self._send(response)
                    continue
                if message.get("id") != request_id or isinstance(
                    message.get("id"), bool
                ):
                    raise MCPError("MCP response ID does not match the request.")
                if "error" in message:
                    raise MCPError("MCP server returned a JSON-RPC error.")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise MCPError("Invalid MCP response result.")
                return result
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.stopped.set()
        with protect_cleanup():
            _terminate_process_tree(self.process)
            self.process.wait()
            self.reader.join(timeout=2)
            self.writer.join(timeout=2)
            for stream in (self.process.stdin, self.process.stdout):
                if stream is not None:
                    stream.close()


class HttpClient:
    """Sequential MCP Streamable HTTP client; tool calls are never replayed."""

    def __init__(self, config: ServerConfig):
        self.config = config
        self.sequence = 0
        self.closed = False
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self.http = httpx2.Client(timeout=config.timeout, follow_redirects=False)
        try:
            result = self.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "mclaude", "version": "0.1.0"},
                },
            )
            version = result.get("protocolVersion")
            if version not in SUPPORTED_VERSIONS or version == "2024-11-05":
                raise MCPError("Unsupported MCP Streamable HTTP protocol version.")
            if (
                not isinstance(result.get("capabilities"), dict)
                or "tools" not in result["capabilities"]
            ):
                raise MCPError("MCP server does not support tools.")
            self.protocol_version = version
            try:
                self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            except (httpx2.HTTPError, ValueError, UnicodeError) as exc:
                raise MCPError(
                    "MCP HTTP stream failed or contained invalid data."
                ) from exc
        except BaseException:
            self.close()
            raise

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **self.config.headers,
        }
        if self.session_id is not None:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version is not None:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    def _message(self, raw: bytes) -> dict:
        if len(raw) > MAX_MESSAGE_BYTES:
            raise MCPError("MCP response exceeds the size limit.")
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
            raise MCPError("Invalid MCP JSON-RPC message.")
        return value

    def _server_request(self, message: dict) -> None:
        if "method" not in message or "id" not in message:
            return
        response = {"jsonrpc": "2.0", "id": message["id"]}
        if message["method"] == "ping":
            response["result"] = {}
        else:
            response["error"] = {
                "code": -32601,
                "message": "Unsupported client method",
            }
        self._post(response)

    def _post(self, message: dict) -> dict | None:
        raw = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > MAX_MESSAGE_BYTES:
            raise MCPError("MCP request exceeds the size limit.")
        deadline = time.monotonic() + self.config.timeout
        with self.http.stream(
            "POST", self.config.url, content=raw, headers=self._headers()
        ) as response:
            if response.status_code >= 400 or response.is_redirect:
                raise MCPError(
                    f"MCP HTTP request failed (status {response.status_code})."
                )
            if message.get("method") == "initialize":
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id is not None:
                    if not session_id or any(
                        not 33 <= ord(c) <= 126 for c in session_id
                    ):
                        raise MCPError("Invalid MCP session ID.")
                    self.session_id = session_id
            if "id" not in message or "method" not in message:
                if response.status_code != 202:
                    raise MCPError("MCP notification was not accepted.")
                return None
            content_type = (
                response.headers.get("content-type", "").split(";", 1)[0].lower()
            )
            if response.status_code != 200 or content_type not in {
                "application/json",
                "text/event-stream",
            }:
                raise MCPError("Invalid MCP HTTP response type or status.")
            if content_type == "application/json":
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=8192):
                    body.extend(chunk)
                    if len(body) > MAX_MESSAGE_BYTES:
                        raise MCPError("MCP response exceeds the size limit.")
                    if time.monotonic() >= deadline:
                        raise MCPError(
                            "MCP request timed out; side effects may be partial."
                        )
                result = self._response(self._message(body), message["id"])
                if result is None:
                    raise MCPError("MCP HTTP response did not answer the request.")
                return result
            return self._stream_response(response, message["id"], deadline)

    def _response(self, value: dict, request_id: int) -> dict | None:
        if "method" in value:
            self._server_request(value)
            return None
        if value.get("id") != request_id or isinstance(value.get("id"), bool):
            raise MCPError("MCP response ID does not match the request.")
        if "error" in value:
            raise MCPError("MCP server returned a JSON-RPC error.")
        result = value.get("result")
        if not isinstance(result, dict):
            raise MCPError("Invalid MCP response result.")
        return result

    def _stream_response(
        self, response: httpx2.Response, request_id: int, deadline: float
    ) -> dict:
        pending = bytearray()
        data: list[bytes] = []
        for chunk in response.iter_bytes(chunk_size=8192):
            if time.monotonic() >= deadline:
                raise MCPError("MCP request timed out; side effects may be partial.")
            pending.extend(chunk)
            if len(pending) + sum(map(len, data)) > MAX_MESSAGE_BYTES:
                raise MCPError("MCP SSE event exceeds the size limit.")
            while b"\n" in pending:
                line, _, remainder = pending.partition(b"\n")
                pending = bytearray(remainder)
                line = line.rstrip(b"\r")
                if not line:
                    if data:
                        value = self._message(b"\n".join(data))
                        data.clear()
                        result = self._response(value, request_id)
                        if result is not None:
                            return result
                elif line.startswith(b"data:"):
                    data.append(line[5:].lstrip(b" "))
        if data:
            result = self._response(self._message(b"\n".join(data)), request_id)
            if result is not None:
                return result
        raise MCPError("MCP SSE stream ended before the response.")

    def request(self, method: str, params: dict) -> dict:
        if self.closed:
            raise MCPError("MCP connection is closed.")
        self.sequence += 1
        try:
            result = self._post(
                {
                    "jsonrpc": "2.0",
                    "id": self.sequence,
                    "method": method,
                    "params": params,
                }
            )
            if result is None:
                raise MCPError("MCP HTTP response did not answer the request.")
            return result
        except (httpx2.HTTPError, ValueError, UnicodeError) as exc:
            self.close()
            raise MCPError("MCP HTTP stream failed or contained invalid data.") from exc
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        with protect_cleanup():
            if self.session_id is not None:
                try:
                    self.http.delete(self.config.url, headers=self._headers())
                except httpx2.HTTPError:
                    pass
            self.http.close()


class MCPRegistry:
    def __init__(self, configs: tuple[ServerConfig, ...], workspace: Path):
        self.configs = configs
        self.workspace = workspace
        self.clients: list[StdioClient | HttpClient] = []
        self.definitions: list[dict[str, Any]] = []
        self.routes: dict[str, tuple[StdioClient | HttpClient, str]] = {}
        self.connected = False

    @classmethod
    def from_file(cls, path: Path, workspace: Path) -> "MCPRegistry":
        try:
            with path.open(encoding="utf-8") as stream:
                raw = stream.read(100_001)
            if len(raw) > 100_000:
                raise ValueError("MCP config exceeds 100,000 characters.")
            config = json.loads(raw)
            if not isinstance(config, dict) or set(config) != {"mcpServers"}:
                raise ValueError("Expected mcpServers object.")
            servers = config["mcpServers"]
            if not isinstance(servers, dict) or len(servers) > 10:
                raise ValueError("At most 10 MCP servers are supported.")
            configs = []
            for name, entry in servers.items():
                if not re.fullmatch(r"[a-zA-Z0-9_-]{1,20}", name):
                    raise ValueError("MCP server names must be 1-20 simple characters.")
                if not isinstance(entry, dict) or set(entry) - {
                    "command",
                    "args",
                    "url",
                    "headers",
                    "timeout",
                }:
                    raise ValueError("Invalid MCP server entry.")
                timeout = entry.get("timeout", 30)
                if (
                    isinstance(timeout, bool)
                    or not isinstance(timeout, (int, float))
                    or not math.isfinite(timeout)
                    or not 0 < timeout <= 600
                ):
                    raise ValueError("MCP timeout must be in (0, 600] seconds.")
                if ("command" in entry) == ("url" in entry):
                    raise ValueError("MCP server needs exactly one of command or url.")
                if "command" in entry:
                    command, args = entry["command"], entry.get("args", [])
                    if (
                        not isinstance(command, str)
                        or not command
                        or not isinstance(args, list)
                        or not all(isinstance(a, str) for a in args)
                        or "headers" in entry
                    ):
                        raise ValueError("Invalid stdio MCP command or args.")
                    configs.append(ServerConfig(name, (command, *args), float(timeout)))
                else:
                    url, headers = entry["url"], entry.get("headers", {})
                    if "args" in entry or not isinstance(url, str):
                        raise ValueError("Invalid HTTP MCP server entry.")
                    parsed = urlsplit(url)
                    # Accessing port also rejects malformed authorities such as :abc.
                    port = parsed.port
                    if (
                        parsed.scheme not in {"http", "https"}
                        or not parsed.hostname
                        or (port is not None and port == 0)
                        or parsed.username is not None
                        or parsed.password is not None
                        or parsed.fragment
                    ):
                        raise ValueError(
                            "MCP URL must be HTTP(S) without credentials or fragment."
                        )
                    if not isinstance(headers, dict):
                        raise ValueError("MCP headers must be an object.")
                    clean_headers = {}
                    reserved = {
                        "accept",
                        "content-type",
                        "host",
                        "origin",
                        "mcp-session-id",
                        "mcp-protocol-version",
                    }
                    for key, value in headers.items():
                        if (
                            not isinstance(key, str)
                            or not re.fullmatch(r"[A-Za-z0-9-]+", key)
                            or key.lower() in reserved
                            or not isinstance(value, str)
                        ):
                            raise ValueError("Invalid MCP HTTP header.")

                        def substitute(match: re.Match) -> str:
                            variable = match.group(1)
                            if variable not in os.environ:
                                raise ValueError(
                                    f"MCP header variable {variable} is not set."
                                )
                            return os.environ[variable]

                        resolved = re.sub(
                            r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", substitute, value
                        )
                        if len(resolved) > 8192 or any(
                            ord(c) < 32 or ord(c) == 127 for c in resolved
                        ):
                            raise ValueError("Invalid MCP HTTP header value.")
                        clean_headers[key] = resolved
                    configs.append(
                        ServerConfig(
                            name, timeout=float(timeout), url=url, headers=clean_headers
                        )
                    )
            return cls(tuple(configs), workspace)
        except (OSError, ValueError) as exc:
            raise MCPError(f"Cannot load MCP configuration: {exc}") from exc

    def connect(self) -> None:
        if self.connected:
            return
        try:
            for config in self.configs:
                client = (
                    HttpClient(config)
                    if config.url
                    else StdioClient(config, self.workspace)
                )
                self.clients.append(client)
                cursor = None
                cursors = set()
                for _ in range(100):
                    listing = client.request(
                        "tools/list", {"cursor": cursor} if cursor else {}
                    )
                    entries = listing.get("tools")
                    if not isinstance(entries, list):
                        raise MCPError("Invalid MCP tool listing.")
                    for tool in entries:
                        if len(self.definitions) >= 100:
                            raise MCPError("MCP catalog exceeds 100 tools.")
                        if not isinstance(tool, dict) or not isinstance(
                            tool.get("name"), str
                        ):
                            raise MCPError("Invalid MCP tool name.")
                        remote_name = tool["name"]
                        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,35}", remote_name):
                            raise MCPError(
                                "MCP tool names must be 1-35 simple characters."
                            )
                        name = f"mcp_{config.name}_{remote_name}"
                        schema = tool.get("inputSchema")
                        description = tool.get("description", "External MCP tool")
                        if (
                            name in self.routes
                            or not isinstance(schema, dict)
                            or schema.get("type") != "object"
                            or not isinstance(description, str)
                        ):
                            raise MCPError("Invalid or duplicate MCP tool definition.")
                        if len(json.dumps(schema)) > 20_000:
                            raise MCPError("MCP tool schema exceeds 20,000 characters.")
                        self.definitions.append(
                            {
                                "name": name,
                                "description": description[:2000],
                                "input_schema": schema,
                            }
                        )
                        self.routes[name] = (client, remote_name)
                    cursor = listing.get("nextCursor")
                    if cursor is None:
                        break
                    if not isinstance(cursor, str) or not cursor or cursor in cursors:
                        raise MCPError("Invalid MCP pagination cursor.")
                    cursors.add(cursor)
                else:
                    raise MCPError("MCP discovery page limit exceeded.")
            self.connected = True
        except BaseException:
            self.close()
            raise

    def execute(self, name: str, arguments: object) -> ToolResult:
        if name not in self.routes or not isinstance(arguments, dict):
            return ToolResult("Unknown MCP tool or invalid arguments.", True)
        client, remote_name = self.routes[name]
        try:
            result = client.request(
                "tools/call", {"name": remote_name, "arguments": arguments}
            )
            content = result.get("content", [])
            error = result.get("isError", False)
            if not isinstance(content, list) or not isinstance(error, bool):
                raise MCPError("Invalid MCP tool result.")
            parts = []
            for block in content:
                if not isinstance(block, dict):
                    raise MCPError("Invalid MCP content block.")
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                else:
                    parts.append("[Non-text MCP content omitted]")
            if "structuredContent" in result:
                parts.append(
                    json.dumps(result["structuredContent"], ensure_ascii=False)
                )
            text = "\n".join(parts)
            if len(text) > 100_000:
                return ToolResult(text[:100_000] + "\n[MCP result truncated]", True)
            return ToolResult(text or "MCP tool returned no text.", error)
        except (OSError, ValueError, MCPError) as exc:
            return ToolResult(f"MCP tool failed (not retried): {exc}", True)

    def close(self) -> None:
        for client in self.clients:
            client.close()
        self.clients.clear()
        self.definitions.clear()
        self.routes.clear()
        self.connected = False
