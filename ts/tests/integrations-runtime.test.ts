import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs/promises";
import path from "node:path";
import http from "node:http";
import { EventEmitter } from "node:events";
import stringWidth from "string-width";
import { fixture, scripted, tools, call, final } from "./helpers.js";
import { runProcess } from "../src/runtime/process-runner.js";
import { pidIsRunning } from "../src/runtime/process-platform.js";
import { MCPRegistry, parseMCPConfig } from "../src/integrations/mcp.js";
import { HookRunner } from "../src/integrations/hooks.js";
import { runAgent } from "../src/core/agent.js";
import { PermissionGate } from "../src/security/permissions.js";
import { gitReview } from "../src/tools/command.js";
import { TerminalStatus, fit, safeLine } from "../src/terminal/status.js";
import { TurnCancelled, type Message } from "../src/core/types.js";

test("process captures exit, UTF-8 and bounded output without pipe deadlock", async (t) => {
  const { root } = await fixture(t);
  const result = await runProcess({
    command: process.execPath,
    args: [
      "-e",
      'process.stdout.write("你好😀".repeat(10000));process.stderr.write("error");process.exitCode=7;',
    ],
    cwd: root,
    maxOutputChars: 100,
  });
  assert.equal(result.exitCode, 7);
  assert.equal(result.stderr, "error");
  assert.equal(result.truncated, true);
  assert.equal(Array.from(result.stdout).length, 100);
});
test("process timeout preserves output and terminates a running child", async (t) => {
  const { root } = await fixture(t);
  const result = await runProcess({
    command: process.execPath,
    args: [
      "-e",
      'console.log("started "+process.pid);setInterval(()=>{},1000)',
    ],
    cwd: root,
    timeoutSeconds: 0.4,
  });
  assert.equal(result.timedOut, true);
  assert.match(result.stdout, /started/);
  const pid = Number(/started (\d+)/.exec(result.stdout)?.[1]);
  assert.equal(pidIsRunning(pid), false);
});
test("process cancellation completes cleanup and errors remain typed", async (t) => {
  const { root } = await fixture(t);
  const controller = new AbortController();
  const run = runProcess({
    command: process.execPath,
    args: ["-e", "setInterval(()=>{},1000)"],
    cwd: root,
    signal: controller.signal,
  });
  setTimeout(() => controller.abort(), 100);
  await assert.rejects(run, TurnCancelled);
  await assert.rejects(
    runProcess({ command: "definitely-no-such-mclaude-command", cwd: root }),
    /Could not start/,
  );
  assert.equal(pidIsRunning(process.pid), true);
  assert.equal(pidIsRunning(-1), false);
});
test("Windows timeout terminates descendants, not just the parent", async (t) => {
  if (process.platform !== "win32") {
    t.skip("Windows taskkill process-tree coverage.");
    return;
  }
  const { root } = await fixture(t);
  const program =
    'const {spawn}=require("node:child_process");const child=spawn(process.execPath,["-e","setInterval(()=>{},1000)"],{stdio:"ignore"});console.log("descendant "+child.pid);setInterval(()=>{},1000);';
  const result = await runProcess({
    command: process.execPath,
    args: ["-e", program],
    cwd: root,
    timeoutSeconds: 0.4,
  });
  const pid = Number(/descendant (\d+)/.exec(result.stdout)?.[1]);
  assert.ok(pid > 0);
  assert.equal(pidIsRunning(pid), false);
});
test("git review reports staged/unstaged changes and whitespace failures without committing", async (t) => {
  const { root, workspace } = await fixture(t);
  const git = async (args: string[]) =>
    runProcess({ command: "git", args, cwd: root });
  assert.equal((await git(["init"])).exitCode, 0);
  await git(["config", "user.name", "Test"]);
  await git(["config", "user.email", "test@example.invalid"]);
  await fs.writeFile(path.join(root, "a"), "base\n");
  await git(["add", "a"]);
  await git(["commit", "-m", "base"]);
  await fs.writeFile(path.join(root, "a"), "updated\n");
  await git(["add", "a"]);
  await fs.appendFile(path.join(root, "a"), "trailing whitespace  \n");
  await fs.writeFile(path.join(root, "untracked"), "x");
  const review = await gitReview({ mode: "diff" }, workspace);
  assert.match(review.content, /Staged diff/);
  assert.match(review.content, /updated/);
  assert.match(review.content, /Unstaged diff/);
  const check = await gitReview({ mode: "precommit" }, workspace);
  assert.equal(check.is_error, true);
  assert.match(check.content, /untracked/);
  assert.match(check.content, /whitespace/);
  const log = await git(["log", "--oneline"]);
  assert.equal(log.stdout.trim().split("\n").length, 1);
});
test("hooks block before side effects and append after a tool", async (t) => {
  const { root } = await fixture(t);
  const before = path.join(root, "before.cjs"),
    after = path.join(root, "after.cjs");
  await fs.writeFile(
    before,
    'process.stdout.write(JSON.stringify({block:true,reason:"blocked"}));',
  );
  await fs.writeFile(
    after,
    'let data="";process.stdin.on("data",c=>data+=c);process.stdin.on("end",()=>{const v=JSON.parse(data);process.stdout.write(JSON.stringify({append:v.result.content+" checked"}));});',
  );
  const config = path.join(root, "hooks.json");
  await fs.writeFile(
    config,
    JSON.stringify({ before_tool: [{ command: [process.execPath, before] }] }),
  );
  let executed = false;
  const blocked = await (
    await HookRunner.fromFile(config)
  ).execute(
    "read_file",
    {},
    root,
    async () => {
      executed = true;
      return { content: "x", is_error: false };
    },
    new AbortController().signal,
  );
  assert.equal(executed, false);
  assert.match(blocked.content, /blocked/);
  await fs.writeFile(
    config,
    JSON.stringify({
      after_tool: [
        { command: [process.execPath, after], tools: ["read_file"] },
      ],
    }),
  );
  const result = await (
    await HookRunner.fromFile(config)
  ).execute(
    "read_file",
    {},
    root,
    async () => ({ content: "original", is_error: false }),
    new AbortController().signal,
  );
  assert.match(result.content, /original checked/);
});
test("malformed and overflowing before hooks fail closed", async (t) => {
  const { root } = await fixture(t);
  for (const code of [
    'process.stdout.write("bad json");',
    'process.stdout.write("x".repeat(20000));',
  ]) {
    const runner = new HookRunner([
      { command: [process.execPath, "-e", code], tools: ["*"], timeout: 2 },
    ]);
    let ran = false;
    const result = await runner.execute(
      "create_file",
      {},
      root,
      async () => {
        ran = true;
        return { content: "x", is_error: false };
      },
      new AbortController().signal,
    );
    assert.equal(result.is_error, true);
    assert.equal(ran, false);
  }
});
test("planning never launches hooks or MCP", async (t) => {
  const { root, workspace } = await fixture(t);
  const marker = path.join(root, "marker");
  const hooks = new HookRunner([
    {
      command: [
        process.execPath,
        "-e",
        `require('node:fs').writeFileSync(${JSON.stringify(marker)},'x')`,
      ],
      tools: ["*"],
      timeout: 2,
    },
  ]);
  const mcp = new MCPRegistry(
    [{ name: "invalid", timeout: 1, command: "definitely-no-mcp-server" }],
    root,
  );
  await runAgent("plan", {
    provider: scripted([tools(call("find_files", { pattern: "*" })), final()]),
    workspace,
    maxTokens: 1024,
    planning: true,
    hooks,
    mcp,
  });
  await assert.rejects(fs.access(marker));
  assert.equal(mcp.definitions.length, 0);
});
for (const entry of [
  { url: "file:///bad" },
  { url: "https://u:p@example.com" },
  { url: "https://example.com#frag" },
  { url: "https://example.com", headers: { "mcp-session-id": "fake" } },
  { url: "https://example.com", headers: { Authorization: "${MISSING}" } },
  { url: "https://example.com", timeout: NaN },
  { command: "node", url: "https://example.com" },
])
  test(
    "MCP configuration rejects unsafe or invalid server " +
      JSON.stringify(entry),
    () =>
      assert.throws(() =>
        parseMCPConfig({ mcpServers: { server: entry } }, {}),
      ),
  );
test("MCP environment headers are substituted and not stored in config text", () => {
  assert.equal(
    parseMCPConfig(
      {
        mcpServers: {
          server: {
            url: "https://example.com",
            headers: { Authorization: "Bearer ${TOKEN}" },
          },
        },
      },
      { TOKEN: "secret" },
    )[0]?.headers?.["Authorization"],
    "Bearer secret",
  );
});

const stdioProgram = `const readline=require('node:readline');
const rl=readline.createInterface({input:process.stdin});
rl.on('line',line=>{const req=JSON.parse(line);if(req.id===undefined)return;let result;
if(req.method==='initialize')result={protocolVersion:'2025-06-18',capabilities:{tools:{}},serverInfo:{name:'fake',version:'1'}};
else if(req.method==='tools/list')result=req.params?.cursor?{tools:[{name:'second',inputSchema:{type:'object',properties:{}}}]}:{tools:[{name:'echo',description:'read only hint',annotations:{readOnlyHint:true},inputSchema:{type:'object',properties:{text:{type:'string'}},required:['text']}}],nextCursor:'page2'};
else if(req.method==='tools/call'){if(req.params.arguments.text==='error'){console.log(JSON.stringify({jsonrpc:'2.0',id:req.id,error:{code:-32603,message:'secret detail'}}));return;}result={content:[{type:'text',text:req.params.arguments.text}]};}
else result={};console.log(JSON.stringify({jsonrpc:'2.0',id:req.id,result}));});`;
test("stdio MCP negotiates legacy protocol, paginates and calls tools", async (t) => {
  const { root } = await fixture(t);
  const registry = new MCPRegistry(
    [
      {
        name: "local",
        timeout: 2,
        command: process.execPath,
        args: ["-e", stdioProgram],
      },
    ],
    root,
  );
  try {
    await registry.connect(new AbortController().signal);
    assert.deepEqual(
      registry.definitions.map((d) => d.name),
      ["mcp_local_echo", "mcp_local_second"],
    );
    const result = await registry.execute(
      "mcp_local_echo",
      { text: "hello" },
      new AbortController().signal,
    );
    assert.equal(result.content, "hello");
    assert.equal(
      (
        await registry.execute(
          "mcp_local_echo",
          { text: "error" },
          new AbortController().signal,
        )
      ).is_error,
      true,
    );
  } finally {
    await registry.close();
  }
  assert.equal(registry.definitions.length, 0);
});
test("external MCP tools require permission even when annotated read-only", async (t) => {
  const { root, workspace } = await fixture(t);
  const registry = new MCPRegistry(
    [
      {
        name: "local",
        timeout: 2,
        command: process.execPath,
        args: ["-e", stdioProgram],
      },
    ],
    root,
  );
  let asked = 0;
  const history: Message[] = [];
  try {
    await runAgent("investigate", {
      provider: scripted([
        tools(call("mcp_local_echo", { text: "hello" })),
        final(),
      ]),
      workspace,
      maxTokens: 1024,
      history,
      mcp: registry,
      permissionGate: new PermissionGate(async () => {
        asked++;
        return false;
      }),
    });
    assert.equal(asked, 1);
    assert.match(JSON.stringify(history), /Permission denied/);
  } finally {
    await registry.close();
  }
});
for (const mode of ["json", "sse"] as const)
  test(`HTTP MCP ${mode} preserves headers and does not retry tool failure`, async (t) => {
    const { root } = await fixture(t);
    let calls = 0;
    const server = http.createServer((req, res) => {
      void (async () => {
        if (req.method === "GET") {
          res.writeHead(405).end();
          return;
        }
        if (req.method === "DELETE") {
          res.writeHead(204).end();
          return;
        }
        const chunks: Buffer[] = [];
        for await (const chunk of req)
          chunks.push(Buffer.from(chunk as Buffer));
        const request = JSON.parse(Buffer.concat(chunks).toString()) as {
          id?: number;
          method: string;
          params?: { arguments?: { text?: string } };
        };
        if (request.id === undefined) {
          res.writeHead(202).end();
          return;
        }
        assert.equal(req.headers["authorization"], "Bearer secret");
        let result: unknown;
        if (request.method === "initialize")
          result = {
            protocolVersion: "2025-06-18",
            capabilities: { tools: {} },
            serverInfo: { name: "http", version: "1" },
          };
        else {
          assert.equal(req.headers["mcp-session-id"], "test-session");
          assert.equal(req.headers["mcp-protocol-version"], "2025-06-18");
          if (request.method === "tools/list")
            result = {
              tools: [
                {
                  name: "echo",
                  inputSchema: {
                    type: "object",
                    properties: { text: { type: "string" } },
                    required: ["text"],
                  },
                },
              ],
            };
          else {
            calls++;
            if (request.params?.arguments?.text === "error") {
              res
                .writeHead(503, { "content-type": "application/json" })
                .end(JSON.stringify({ error: "secret detail" }));
              return;
            }
            result = { content: [{ type: "text", text: "http hello" }] };
          }
        }
        const response = JSON.stringify({
          jsonrpc: "2.0",
          id: request.id,
          result,
        });
        res.writeHead(200, {
          "content-type":
            mode === "json" ? "application/json" : "text/event-stream",
          "mcp-session-id": "test-session",
        });
        res.end(
          mode === "json" ? response : `event: message\ndata: ${response}\n\n`,
        );
      })().catch((error) => {
        res.writeHead(500).end();
        throw error;
      });
    });
    await new Promise<void>((resolve) =>
      server.listen(0, "127.0.0.1", resolve),
    );
    t.after(
      () =>
        new Promise<void>((resolve) => {
          server.closeAllConnections();
          server.close(() => resolve());
        }),
    );
    const address = server.address();
    assert.ok(address && typeof address !== "string");
    const registry = new MCPRegistry(
      [
        {
          name: "remote",
          url: `http://127.0.0.1:${address.port}/mcp`,
          headers: { Authorization: "Bearer secret" },
          timeout: 2,
        },
      ],
      root,
    );
    try {
      await registry.connect(new AbortController().signal);
      assert.equal(
        (
          await registry.execute(
            "mcp_remote_echo",
            { text: "ok" },
            new AbortController().signal,
          )
        ).content,
        "http hello",
      );
      const failure = await registry.execute(
        "mcp_remote_echo",
        { text: "error" },
        new AbortController().signal,
      );
      assert.equal(failure.is_error, true);
      assert.doesNotMatch(failure.content, /secret detail/);
      assert.equal(calls, 2);
    } finally {
      await registry.close();
    }
  });
test("terminal layout sanitizes control characters, fits wide text and restores once", () => {
  assert.equal(safeLine("name\x1b[2J\nnext").includes("\x1b"), false);
  assert.equal(stringWidth(fit("你好😀abc", 8)), 8);
  const stream = new EventEmitter() as NodeJS.WriteStream;
  let output = "";
  stream.write = ((text: string) => {
    output += text;
    return true;
  }) as NodeJS.WriteStream["write"];
  const status = new TerminalStatus({
    enabled: true,
    workspace: "工作区",
    model: "m",
    sessionKind: "temporary",
    planning: false,
    contextBudget: 1000,
    maxTokens: 100,
    readWorkers: 4,
    subagentBudget: 8,
    hooks: false,
    mcp: false,
    stream,
    size: { columns: 40, rows: 20 },
  });
  status.open();
  status.agentEvent("request.started", {
    iteration: 1,
    max_iterations: 8,
    context_tokens: 200,
  });
  status.updateTasks([{ id: "1", description: "任务", status: "in_progress" }]);
  assert.ok(status.lines().every((line) => stringWidth(line) === 40));
  status.close();
  status.close();
  assert.equal(output.split("\x1b[?1049l").length - 1, 1);
});
