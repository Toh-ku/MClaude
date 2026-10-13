import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import * as fs from "node:fs/promises";
import path from "node:path";
import http from "node:http";
import { fixture } from "./helpers.js";
import { ConfigStore } from "../src/persistence/config-store.js";
import { SessionStore } from "../src/persistence/session.js";
const cli = fileURLToPath(new URL("../src/cli/main.js", import.meta.url));
async function launch(
  root: string,
  state: string,
  args: string[],
  input = "",
  env: NodeJS.ProcessEnv = {},
): Promise<{ code: number | null; stdout: string; stderr: string }> {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [cli, ...args], {
      cwd: root,
      env: {
        ...process.env,
        ANTHROPIC_API_KEY: "",
        ANTHROPIC_MODEL: "",
        ANTHROPIC_BASE_URL: "",
        MCLAUDE_CONFIG_DIR: path.join(state, "config"),
        MCLAUDE_STATE_DIR: state,
        ...env,
      },
      stdio: "pipe",
      windowsHide: true,
    });
    let stdout = "",
      stderr = "";
    child.stdout.on("data", (c: Buffer) => (stdout += c.toString()));
    child.stderr.on("data", (c: Buffer) => (stderr += c.toString()));
    child.stdin.on("error", () => {});
    child.stdin.end(input);
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error("CLI timed out."));
    }, 10000);
    child.once("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.once("close", (code) => {
      clearTimeout(timer);
      resolve({ code, stdout, stderr });
    });
  });
}
function streamResponse(
  content:
    | { type: "text"; text: string }
    | {
        type: "tool_use";
        id: string;
        name: string;
        input: Record<string, unknown>;
      },
): string {
  const message = {
    id: "msg_cli",
    type: "message",
    role: "assistant",
    model: "test",
    content: [],
    stop_reason: null,
    stop_sequence: null,
    usage: { input_tokens: 10, output_tokens: 0 },
  };
  const start =
    content.type === "text"
      ? { type: "text", text: "" }
      : { ...content, input: {} };
  const delta =
    content.type === "text"
      ? { type: "text_delta", text: content.text }
      : {
          type: "input_json_delta",
          partial_json: JSON.stringify(content.input),
        };
  return [
    { type: "message_start", message },
    { type: "content_block_start", index: 0, content_block: start },
    { type: "content_block_delta", index: 0, delta },
    { type: "content_block_stop", index: 0 },
    {
      type: "message_delta",
      delta: {
        stop_reason: content.type === "text" ? "end_turn" : "tool_use",
        stop_sequence: null,
      },
      usage: { output_tokens: 2 },
    },
    { type: "message_stop" },
  ]
    .map((e) => `event: ${e.type}\ndata: ${JSON.stringify(e)}\n\n`)
    .join("");
}
async function modelServer(
  t: import("node:test").TestContext,
  handler: (
    request: Record<string, unknown>,
    index: number,
  ) => ReturnType<typeof streamResponse>,
): Promise<{ url: string; requests: Record<string, unknown>[] }> {
  const requests: Record<string, unknown>[] = [];
  const server = http.createServer((req, res) => {
    void (async () => {
      let body = "";
      for await (const c of req) body += String(c);
      const request = JSON.parse(body) as Record<string, unknown>;
      requests.push(request);
      res.writeHead(200, { "content-type": "text/event-stream" });
      res.end(handler(request, requests.length));
    })().catch(() => res.writeHead(500).end());
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(
    () =>
      new Promise<void>((resolve) => {
        server.closeAllConnections();
        server.close(() => resolve());
      }),
  );
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  return { url: `http://127.0.0.1:${address.port}`, requests };
}
test("CLI help, version and unknown options work without credentials", async (t) => {
  const { root, state } = await fixture(t);
  for (const args of [
    ["--help"],
    ["--version"],
    ["login", "--help"],
    ["logout", "--help"],
    ["sessions", "--help"],
  ])
    assert.equal((await launch(root, state, args)).code, 0);
  const bad = await launch(root, state, ["--unknown"]);
  assert.equal(bad.code, 2);
  assert.match(bad.stderr, /Unknown option/);
  await assert.rejects(fs.access(path.join(state, "config", "config.json")));
});
test("CLI missing credentials does not prompt or reveal secrets", async (t) => {
  const { root, state } = await fixture(t);
  const result = await launch(root, state, ["task"]);
  assert.equal(result.code, 2);
  assert.match(result.stderr, /API_KEY|login/);
  assert.doesNotMatch(result.stderr, /API Key \(hidden\)/);
  const login = await launch(root, state, ["login"]);
  assert.equal(login.code, 2);
  assert.match(login.stderr, /real terminal/);
});
test("CLI rejects conflicting and out-of-range options before requesting a model", async (t) => {
  const { root, state } = await fixture(t);
  for (const args of [
    ["--continue", "--resume", "abc"],
    ["--continue", "--no-persist"],
    ["--read-workers", "0", "task"],
    ["--subagent-budget", "33", "task"],
    ["--context-budget", "1024", "task"],
    ["--max-tokens", "1.5", "task"],
    ["--timeout", "NaN", "task"],
  ])
    assert.equal((await launch(root, state, args)).code, 2);
});
test("history commands operate without API credentials", async (t) => {
  const { root, state } = await fixture(t);
  const store = new SessionStore(state),
    session = await store.create(root, "m");
  await session.append({
    type: "message.appended",
    message: { role: "user", content: "decision needle" },
  });
  await session.close();
  assert.match(
    (await launch(root, state, ["sessions", "list"])).stdout,
    new RegExp(session.id),
  );
  assert.match(
    (await launch(root, state, ["sessions", "search", "needle"])).stdout,
    /decision needle/,
  );
  assert.equal(
    (await launch(root, state, ["sessions", "rename", session.id, "new name"]))
      .code,
    0,
  );
  assert.equal((await store.list(root))[0]?.name, "new name");
});
test("CLI streams one task, uses saved credentials and does not create a session", async (t) => {
  const { root, state } = await fixture(t);
  const server = await modelServer(t, () =>
    streamResponse({ type: "text", text: "模型输出" }),
  );
  await new ConfigStore(path.join(state, "config", "config.json")).save({
    api_key: "saved-secret",
    model: "saved-model",
    base_url: server.url,
  });
  const result = await launch(root, state, ["--plain", "task"], "", {
    ANTHROPIC_BASE_URL: server.url,
  });
  assert.equal(result.code, 0, result.stderr);
  assert.equal(result.stdout, "模型输出\n");
  assert.equal(server.requests[0]?.["model"], "saved-model");
  assert.deepEqual(await new SessionStore(state).list(root), []);
  assert.equal(result.stderr.includes("saved-secret"), false);
  assert.equal(result.stderr.includes("\x1b"), false);
});
test("CLI queued confirmation authorizes exactly one file edit", async (t) => {
  const { root, state } = await fixture(t);
  const server = await modelServer(t, (_request, index) =>
    index === 1
      ? streamResponse({
          type: "tool_use",
          id: "edit",
          name: "create_file",
          input: { path: "new.txt", content: "approved" },
        })
      : streamResponse({ type: "text", text: "done" }),
  );
  const result = await launch(root, state, ["--plain", "task"], "yes\n", {
    ANTHROPIC_API_KEY: "test-key",
    ANTHROPIC_MODEL: "test-model",
    ANTHROPIC_BASE_URL: server.url,
  });
  assert.equal(result.code, 0, result.stderr);
  assert.equal(
    await fs.readFile(path.join(root, "new.txt"), "utf8"),
    "approved",
  );
  assert.match(result.stderr, /Allow this tool call/);
  assert.match(JSON.stringify(server.requests[1]), /Checkpoint/);
});
test("interactive CLI preserves history and handles task/mode commands", async (t) => {
  const { root, state } = await fixture(t);
  const server = await modelServer(t, () =>
    streamResponse({ type: "text", text: "reply" }),
  );
  const result = await launch(
    root,
    state,
    ["-i", "--plain"],
    "first\n/tasks\n/plan\nsecond\n/execute\nthird\n/exit\n",
    {
      ANTHROPIC_API_KEY: "test-key",
      ANTHROPIC_MODEL: "test-model",
      ANTHROPIC_BASE_URL: server.url,
    },
  );
  assert.equal(result.code, 0, result.stderr);
  assert.equal(server.requests.length, 3);
  assert.match(JSON.stringify(server.requests[1]?.["messages"]), /first/);
  const middle = server.requests[1]?.["tools"] as { name: string }[];
  assert.ok(
    middle.every(
      (tool) =>
        !["create_file", "update_tasks", "delegate_readonly"].includes(
          tool.name,
        ),
    ),
  );
  assert.ok(
    (server.requests[2]?.["tools"] as { name: string }[]).some(
      (tool) => tool.name === "create_file",
    ),
  );
  const store = new SessionStore(state);
  assert.equal((await store.list(root)).length, 1);
  const saved = await store.continueLatest(root);
  assert.equal(saved.history.length, 6);
  await saved.close();
});
test("interactive no-persist, resume and logout retain expected state", async (t) => {
  const { root, state } = await fixture(t);
  const server = await modelServer(t, () =>
    streamResponse({ type: "text", text: "reply" }),
  );
  const env = {
    ANTHROPIC_API_KEY: "test-key",
    ANTHROPIC_MODEL: "test-model",
    ANTHROPIC_BASE_URL: server.url,
  };
  assert.equal(
    (
      await launch(
        root,
        state,
        ["-i", "--no-persist", "--plain"],
        "task\n/exit\n",
        env,
      )
    ).code,
    0,
  );
  const store = new SessionStore(state);
  assert.deepEqual(await store.list(root), []);
  const session = await store.create(root, "m");
  await session.append({
    type: "message.appended",
    message: { role: "user", content: "old task" },
  });
  await session.append({
    type: "message.appended",
    message: {
      role: "assistant",
      content: [{ type: "text", text: "old answer" }],
    },
  });
  await session.close();
  assert.equal(
    (
      await launch(
        root,
        state,
        ["--resume", session.id, "--plain"],
        "new task\n/exit\n",
        env,
      )
    ).code,
    0,
  );
  assert.match(
    JSON.stringify(server.requests.at(-1)?.["messages"]),
    /old task/,
  );
  await new ConfigStore(path.join(state, "config", "config.json")).save({
    api_key: "test-key",
    model: "m",
    base_url: "",
  });
  assert.equal((await launch(root, state, ["logout"])).code, 0);
  assert.equal((await store.list(root)).length, 1);
});
