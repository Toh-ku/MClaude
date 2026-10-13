import {
  Client,
  StreamableHTTPClientTransport,
  type Transport,
} from "@modelcontextprotocol/client";
import { StdioClientTransport } from "@modelcontextprotocol/client/stdio";
import {
  MClaudeError,
  TurnCancelled,
  checkCancelled,
  isObject,
  codePoints,
  clip,
  fail,
  type ToolResult,
  type ToolSpec,
} from "../core/types.js";
import { validateUrl } from "../core/config.js";
import { readBounded, utf8 } from "../security/workspace.js";
import { terminateTree } from "../runtime/process-platform.js";
export interface ServerConfig {
  name: string;
  timeout: number;
  command?: string;
  args?: string[];
  url?: string;
  headers?: Record<string, string>;
}
interface Connection {
  client: Client;
  transport: Transport;
  config: ServerConfig;
}
const MAX_BYTES = 1000000;
export function parseMCPConfig(
  value: unknown,
  env: NodeJS.ProcessEnv = process.env,
): ServerConfig[] {
  if (
    !isObject(value) ||
    Object.keys(value).length !== 1 ||
    !isObject(value["mcpServers"]) ||
    Object.keys(value["mcpServers"]).length > 10
  )
    throw new MClaudeError("Expected mcpServers with at most 10 servers.");
  return Object.entries(value["mcpServers"]).map(([name, entry]) => {
    if (
      !/^[a-zA-Z0-9_-]{1,20}$/.test(name) ||
      !isObject(entry) ||
      Object.keys(entry).some(
        (k) => !["command", "args", "url", "headers", "timeout"].includes(k),
      )
    )
      throw new MClaudeError("Invalid MCP server entry.");
    const timeout = entry["timeout"] ?? 30;
    if (
      typeof timeout !== "number" ||
      !Number.isFinite(timeout) ||
      timeout <= 0 ||
      timeout > 600 ||
      "command" in entry === "url" in entry
    )
      throw new MClaudeError(
        "MCP server needs exactly one of command or url and a valid timeout.",
      );
    if ("command" in entry) {
      const args = entry["args"] ?? [];
      if (
        typeof entry["command"] !== "string" ||
        !entry["command"] ||
        "headers" in entry ||
        !Array.isArray(args) ||
        !args.every((v: unknown) => typeof v === "string")
      )
        throw new MClaudeError("Invalid stdio MCP command or args.");
      return {
        name,
        timeout,
        command: entry["command"],
        args: args as string[],
      };
    }
    if (
      typeof entry["url"] !== "string" ||
      !validateUrl(entry["url"], true) ||
      "args" in entry ||
      !isObject(entry["headers"] ?? {})
    )
      throw new MClaudeError("Invalid MCP HTTP configuration.");
    const reserved = new Set([
      "accept",
      "content-type",
      "host",
      "origin",
      "mcp-session-id",
      "mcp-protocol-version",
    ]);
    const headers: Record<string, string> = {};
    for (const [key, value] of Object.entries(
      (entry["headers"] as Record<string, unknown>) ?? {},
    )) {
      if (
        !/^[A-Za-z0-9-]+$/.test(key) ||
        reserved.has(key.toLowerCase()) ||
        typeof value !== "string"
      )
        throw new MClaudeError("Invalid MCP HTTP header.");
      const resolved = value.replace(
        /\$\{([A-Za-z_][A-Za-z0-9_]*)\}/g,
        (_, variable: string) => {
          const value = env[variable];
          if (value === undefined)
            throw new MClaudeError(
              `MCP header variable ${variable} is not set.`,
            );
          return value;
        },
      );
      if (codePoints(resolved) > 8192 || /[\x00-\x1f\x7f]/.test(resolved))
        throw new MClaudeError("Invalid MCP HTTP header value.");
      headers[key] = resolved;
    }
    return { name, timeout, url: entry["url"], headers };
  });
}
function limitedFetch(timeout: number): typeof fetch {
  return async (input, init) => {
    const timeoutSignal = AbortSignal.timeout(Math.ceil(timeout * 1000));
    const signal = init?.signal
      ? AbortSignal.any([init.signal, timeoutSignal])
      : timeoutSignal;
    const response = await fetch(input, { ...init, signal, redirect: "error" });
    if (Number(response.headers.get("content-length") ?? 0) > MAX_BYTES) {
      await response.body?.cancel();
      throw new MClaudeError("MCP HTTP response exceeds the 1 MB limit.");
    }
    if (!response.body) return response;
    let total = 0;
    const reader = response.body.getReader();
    const body = new ReadableStream<Uint8Array>({
      async pull(controller) {
        try {
          const next = await reader.read();
          if (next.done) {
            controller.close();
            return;
          }
          total += next.value.length;
          if (total > MAX_BYTES) {
            await reader.cancel();
            controller.error(
              new MClaudeError("MCP response exceeds the 1 MB limit."),
            );
            return;
          }
          controller.enqueue(next.value);
        } catch (e) {
          controller.error(e);
        }
      },
      async cancel(reason) {
        await reader.cancel(reason);
      },
    });
    return new Response(body, {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
  };
}
export class MCPRegistry {
  readonly definitions: ToolSpec[] = [];
  readonly routes = new Map<
    string,
    { connection: Connection; remoteName: string }
  >();
  private connections: Connection[] = [];
  private connected = false;
  constructor(
    readonly configs: ServerConfig[],
    readonly workspace: string,
  ) {}
  static async fromFile(file: string, workspace: string): Promise<MCPRegistry> {
    let raw: unknown;
    try {
      raw = JSON.parse(utf8(await readBounded(file, 400004)));
    } catch {
      throw new MClaudeError("Cannot load MCP configuration.");
    }
    return new MCPRegistry(parseMCPConfig(raw), workspace);
  }
  async connect(signal: AbortSignal): Promise<void> {
    if (this.connected) return;
    checkCancelled(signal);
    try {
      for (const config of this.configs) {
        const client = new Client(
          { name: "mclaude", version: "0.1.0" },
          {
            capabilities: {},
            versionNegotiation: { mode: "legacy" },
            listMaxPages: 100,
          },
        );
        const transport = config.url
          ? new StreamableHTTPClientTransport(new URL(config.url), {
              requestInit: { headers: config.headers ?? {} },
              fetch: limitedFetch(config.timeout),
              reconnectionOptions: {
                maxRetries: 0,
                maxReconnectionDelay: 1000,
                initialReconnectionDelay: 1000,
                reconnectionDelayGrowFactor: 1,
              },
            })
          : new StdioClientTransport({
              command: config.command!,
              args: config.args ?? [],
              cwd: this.workspace,
              stderr: "ignore",
              maxBufferSize: MAX_BYTES,
            });
        const connection = { client, transport, config };
        this.connections.push(connection);
        await client.connect(transport, {
          signal,
          timeout: config.timeout * 1000,
        });
        let cursor: string | undefined;
        const cursors = new Set<string>();
        let finished = false;
        for (let page = 0; page < 100; page++) {
          checkCancelled(signal);
          const listing = await client.listTools(
            { cursor },
            { signal, timeout: config.timeout * 1000 },
          );
          for (const remote of listing.tools) {
            const name = `mcp_${config.name}_${remote.name}`;
            if (
              this.definitions.length >= 100 ||
              !/^[a-zA-Z0-9_-]{1,35}$/.test(remote.name) ||
              this.routes.has(name) ||
              remote.inputSchema.type !== "object" ||
              JSON.stringify(remote.inputSchema).length > 20000 ||
              (remote.outputSchema &&
                JSON.stringify(remote.outputSchema).length > 20000)
            )
              throw new MClaudeError(
                "Invalid or oversized MCP tool definition.",
              );
            this.definitions.push({
              name,
              description: clip(
                remote.description ?? "External MCP tool",
                2000,
              ),
              input_schema: remote.inputSchema,
            });
            this.routes.set(name, { connection, remoteName: remote.name });
          }
          cursor = listing.nextCursor;
          if (cursor === undefined) {
            finished = true;
            break;
          }
          if (!cursor || cursors.has(cursor))
            throw new MClaudeError("Invalid MCP pagination cursor.");
          cursors.add(cursor);
        }
        if (!finished)
          throw new MClaudeError("MCP discovery page limit exceeded.");
      }
      this.connected = true;
    } catch (e) {
      await this.close();
      checkCancelled(signal);
      throw e instanceof MClaudeError
        ? e
        : new MClaudeError("MCP discovery failed.");
    }
  }
  async execute(
    name: string,
    input: Record<string, unknown>,
    signal: AbortSignal,
  ): Promise<ToolResult> {
    const route = this.routes.get(name);
    if (!route) return fail("Unknown MCP tool.");
    try {
      const result = await route.connection.client.callTool(
        { name: route.remoteName, arguments: input },
        { signal, timeout: route.connection.config.timeout * 1000 },
      );
      const text = [
        ...result.content.map((b) =>
          b.type === "text" ? b.text : "[Non-text MCP content omitted]",
        ),
        ...(result.structuredContent
          ? [JSON.stringify(result.structuredContent)]
          : []),
      ].join("\n");
      if (codePoints(text) > 100000)
        return fail(clip(text, 100000) + "\n[MCP result truncated]");
      return {
        content: text || "MCP tool returned no text.",
        is_error: result.isError ?? false,
      };
    } catch (e) {
      if (e instanceof TurnCancelled) throw e;
      checkCancelled(signal);
      return fail("MCP tool failed (not retried).");
    }
  }
  async close(): Promise<void> {
    const connections = this.connections;
    this.connections = [];
    this.connected = false;
    this.definitions.length = 0;
    this.routes.clear();
    await Promise.all(
      connections.map(async (c) => {
        if (c.transport instanceof StdioClientTransport && c.transport.pid)
          await terminateTree(c.transport.pid);
        await c.client.close().catch(() => {});
      }),
    );
  }
}
