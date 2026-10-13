import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs/promises";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fixture, scripted, tools, call, final, config } from "./helpers.js";
import { runAgent } from "../src/core/agent.js";
import { PermissionGate } from "../src/security/permissions.js";
import { SessionStore } from "../src/persistence/session.js";
import { CheckpointStore } from "../src/persistence/checkpoints.js";
import { VerificationWorkflow } from "../src/core/verification.js";
import { ToolScheduler } from "../src/runtime/scheduler.js";
import { AnthropicProvider } from "../src/providers/anthropic.js";
import {
  TurnCancelled,
  ModelError,
  checkCancelled,
  type Message,
  type ModelProvider,
} from "../src/core/types.js";

test("agent reads a file and carries tool result to the next request", async (t) => {
  const { workspace, root, state } = await fixture(t);
  await fs.writeFile(path.join(root, "a"), "contents");
  const provider = scripted([
    tools(call("read_file", { path: "a" })),
    final("answer"),
  ]);
  const history: Message[] = [];
  const result = await runAgent("read", {
    provider,
    workspace,
    maxTokens: 1024,
    history,
    sessions: new SessionStore(state),
    checkpoints: new CheckpointStore(workspace, state),
  });
  assert.equal(result.text, "answer");
  assert.equal(history.length, 4);
  assert.match(JSON.stringify(provider.requests[1]?.messages), /contents/);
});
test("planning and host-command flags restrict definitions and actual execution", async (t) => {
  const { workspace, root } = await fixture(t);
  const provider = scripted([
    tools(
      call("create_file", { path: "forbidden", content: "x" }, "a"),
      call("run_command", { command: "echo x" }, "b"),
      call("update_tasks", { tasks: [] }, "c"),
    ),
    final(),
  ]);
  const history: Message[] = [];
  await runAgent("plan", {
    provider,
    workspace,
    maxTokens: 1024,
    planning: true,
    history,
    permissionGate: new PermissionGate(async () => true),
  });
  await assert.rejects(fs.access(path.join(root, "forbidden")));
  assert.ok(
    provider.requests[0]?.tools.every(
      (d) =>
        ![
          "create_file",
          "run_command",
          "update_tasks",
          "delegate_readonly",
        ].includes(d.name),
    ),
  );
  assert.match(
    JSON.stringify(history),
    /blocked in planning|Host commands are disabled/,
  );
});
test("permission denial never executes a file edit", async (t) => {
  const { workspace, root } = await fixture(t);
  let prompted = 0;
  const provider = scripted([
    tools(call("create_file", { path: "a", content: "x" })),
    final(),
  ]);
  const history: Message[] = [];
  await runAgent("edit", {
    provider,
    workspace,
    maxTokens: 1024,
    history,
    permissionGate: new PermissionGate(async () => {
      prompted++;
      return false;
    }),
  });
  assert.equal(prompted, 1);
  await assert.rejects(fs.access(path.join(root, "a")));
  assert.match(JSON.stringify(history), /Permission denied/);
});
test("approved edit produces a checkpoint and invalid asynchronous tools return error blocks", async (t) => {
  const { workspace, root, state } = await fixture(t);
  const provider = scripted([
    tools(
      call("create_file", { path: "a", content: "new" }, "write"),
      call("read_file", { path: "missing" }, "read"),
    ),
    final(),
  ]);
  const history: Message[] = [];
  await runAgent("edit", {
    provider,
    workspace,
    maxTokens: 1024,
    history,
    checkpoints: new CheckpointStore(workspace, state),
    permissionGate: new PermissionGate(async () => true),
  });
  assert.equal(await fs.readFile(path.join(root, "a"), "utf8"), "new");
  const result = history[2];
  assert.ok(result?.role === "user" && typeof result.content !== "string");
  assert.equal(result.content[1]?.is_error, true);
  assert.equal((await new CheckpointStore(workspace, state).list()).length, 1);
});
test("truncated tool calls are neither executed nor persisted", async (t) => {
  const { workspace, root } = await fixture(t);
  const provider = scripted([
    {
      content: [
        { type: "text", text: "partial" },
        call("create_file", { path: "a", content: "bad" }),
      ],
      stop_reason: "max_tokens",
    },
  ]);
  const history: Message[] = [];
  const result = await runAgent("task", {
    provider,
    workspace,
    maxTokens: 1024,
    history,
    permissionGate: new PermissionGate(async () => true),
  });
  assert.equal(result.truncated, true);
  assert.equal(result.text, "partial");
  assert.doesNotMatch(JSON.stringify(history), /tool_use/);
  await assert.rejects(fs.access(path.join(root, "a")));
});
test("duplicate tool IDs fail before any tool side effect", async (t) => {
  const { workspace, root } = await fixture(t);
  const provider = scripted([
    tools(
      call("create_file", { path: "a", content: "x" }, "same"),
      call("create_file", { path: "b", content: "x" }, "same"),
    ),
  ]);
  await assert.rejects(
    runAgent("task", {
      provider,
      workspace,
      maxTokens: 1024,
      permissionGate: new PermissionGate(async () => true),
    }),
    /duplicate/,
  );
  await assert.rejects(fs.access(path.join(root, "a")));
});
for (const stop of ["pause_turn", null])
  test(`unexpected model stop ${stop} is not a success`, async (t) => {
    const { workspace } = await fixture(t);
    await assert.rejects(
      runAgent("task", {
        provider: scripted([{ ...final(), stop_reason: stop }]),
        workspace,
        maxTokens: 1024,
      }),
      /unexpectedly/,
    );
  });
test("agent retains complete tools after a later model failure and can continue", async (t) => {
  const { workspace, root } = await fixture(t);
  await fs.writeFile(path.join(root, "a"), "data");
  let n = 0;
  const provider: ModelProvider = {
    async createMessage() {
      if (n++ === 0) return tools(call("read_file", { path: "a" }));
      throw new ModelError("network failed");
    },
  };
  const history: Message[] = [];
  await assert.rejects(
    runAgent("read", { provider, workspace, maxTokens: 1024, history }),
    /network/,
  );
  assert.equal(history.length, 3);
  await runAgent("continue", {
    provider: scripted([final()]),
    workspace,
    maxTokens: 1024,
    history,
  });
  assert.equal(history.length, 5);
});
test("durable tool intent and results are ordered around side effects", async (t) => {
  const { workspace, root, state } = await fixture(t);
  const store = new SessionStore(state),
    session = await store.create(root, "m");
  const events: string[] = [];
  await runAgent("write", {
    provider: scripted([
      tools(call("create_file", { path: "a", content: "x" })),
      final(),
    ]),
    workspace,
    maxTokens: 1024,
    history: session.history,
    checkpoints: new CheckpointStore(workspace, state),
    permissionGate: new PermissionGate(async () => true),
    onHistoryEvent: async (event) => {
      if (
        event.type === "message.appended" &&
        event.message.role === "assistant" &&
        event.message.content.some((b) => b.type === "tool_use")
      )
        await assert.rejects(fs.access(path.join(root, "a")));
      if (event.type === "tool.result")
        assert.equal(await fs.readFile(path.join(root, "a"), "utf8"), "x");
      await session.append(event);
      events.push(event.type);
    },
  });
  assert.deepEqual(events, [
    "message.appended",
    "message.appended",
    "tool.result",
    "message.appended",
  ]);
  await session.close();
  const resumed = await store.resume(session.id, root);
  assert.equal(resumed.history.length, 4);
  await resumed.close();
});
test("cancelling a permission prompt fills results and keeps the next turn usable", async (t) => {
  const { workspace, root } = await fixture(t);
  const controller = new AbortController();
  const history: Message[] = [];
  const gate = new PermissionGate(async () => {
    controller.abort();
    return false;
  });
  await assert.rejects(
    runAgent("edit", {
      provider: scripted([
        tools(
          call("create_file", { path: "a", content: "x" }, "a"),
          call("create_file", { path: "b", content: "x" }, "b"),
        ),
      ]),
      workspace,
      maxTokens: 1024,
      history,
      signal: controller.signal,
      permissionGate: gate,
    }),
    TurnCancelled,
  );
  await assert.rejects(fs.access(path.join(root, "a")));
  const last = history.at(-1);
  assert.ok(last?.role === "user" && typeof last.content !== "string");
  assert.equal(last.content.length, 2);
  await runAgent("continue", {
    provider: scripted([final()]),
    workspace,
    maxTokens: 1024,
    history,
  });
});
test("readonly subagent has independent history and shares a request budget", async (t) => {
  const { workspace } = await fixture(t);
  const requests: Message[][] = [];
  let n = 0;
  const provider: ModelProvider = {
    async createMessage(request) {
      requests.push(request.messages);
      switch (n++) {
        case 0:
          return tools(
            call("delegate_readonly", { prompt: "investigate" }, "a"),
            call("delegate_readonly", { prompt: "second" }, "b"),
          );
        case 1:
          assert.ok(
            request.tools.every((d) =>
              ["read_file", "find_files", "search_text"].includes(d.name),
            ),
          );
          return final("child report");
        default:
          return final();
      }
    },
  };
  const history: Message[] = [];
  await runAgent("main", {
    provider,
    workspace,
    maxTokens: 1024,
    history,
    subagentBudget: 1,
  });
  assert.equal(requests.length, 3);
  assert.equal(
    (requests[1]?.[0] as { content: string }).content,
    "investigate",
  );
  assert.match(JSON.stringify(history), /child report/);
  assert.match(JSON.stringify(history), /budget exhausted/);
});
test("scheduler limits concurrency, retains completion order and respects write barriers", async () => {
  const scheduler = new ToolScheduler(2);
  let active = 0,
    max = 0;
  const results: string[] = [],
    barriers: string[] = [];
  await scheduler.run(
    [
      call("read_file", {}, "a"),
      call("read_file", {}, "b"),
      call("replace_text", {}, "write"),
      call("read_file", {}, "c"),
    ],
    async () => undefined,
    async (c) => {
      if (c.name === "replace_text") {
        assert.equal(active, 0);
        barriers.push(c.id);
      } else {
        active++;
        max = Math.max(max, active);
        await delay(c.id === "a" ? 30 : 5);
        active--;
      }
      return { content: c.id, is_error: false };
    },
    async (c) => {
      results.push(c.id);
    },
    new AbortController().signal,
  );
  assert.equal(max, 2);
  assert.deepEqual(barriers, ["write"]);
  assert.deepEqual(results, ["b", "a", "write", "c"]);
});
test("scheduler preserves completed reads on cancellation and propagates journal failure", async () => {
  const controller = new AbortController(),
    scheduler = new ToolScheduler(2);
  const recorded: string[] = [];
  await assert.rejects(
    scheduler.run(
      [call("read_file", {}, "a"), call("read_file", {}, "b")],
      async () => undefined,
      async (c) => {
        await delay(c.id === "a" ? 1 : 50);
        checkCancelled(controller.signal);
        return { content: c.id, is_error: false };
      },
      async (c) => {
        recorded.push(c.id);
        controller.abort();
      },
      controller.signal,
    ),
    TurnCancelled,
  );
  assert.deepEqual(recorded, ["a"]);
  await assert.rejects(
    new ToolScheduler().run(
      [call("read_file", {}, "a"), call("read_file", {}, "b")],
      async () => undefined,
      async () => ({ content: "ok", is_error: false }),
      async () => {
        throw new Error("disk failure");
      },
      new AbortController().signal,
    ),
    /disk failure/,
  );
});
test("verification stops at failure, preserves commands and becomes stale after an edit", async (t) => {
  const { workspace } = await fixture(t);
  const flow = new VerificationWorkflow();
  const bad = process.platform === "win32" ? "exit /b 1" : "exit 1";
  const result = await flow.run(
    { checks: [{ command: bad }, { command: "echo should-not-run" }] },
    workspace,
  );
  assert.equal(result.is_error, true);
  assert.doesNotMatch(result.content, /Check 2/);
  assert.equal(flow.state, "failed");
  await flow.run({ checks: [{ command: "echo passed" }] }, workspace);
  assert.equal(flow.state, "passed");
  flow.markEdited();
  assert.equal(flow.state, "stale");
  assert.equal((await flow.run({}, workspace, true)).is_error, false);
  assert.equal(flow.attempts, 3);
  assert.equal(
    (await new VerificationWorkflow().run({}, workspace, true)).is_error,
    true,
  );
});

function apiMessage(
  content: unknown[] = [{ type: "text", text: "hello" }],
  stop_reason = "end_turn",
): Record<string, unknown> {
  return {
    id: "msg_1",
    type: "message",
    role: "assistant",
    model: "test-model",
    content,
    stop_reason,
    stop_sequence: null,
    usage: { input_tokens: 10, output_tokens: 2 },
  };
}
function sse(events: Record<string, unknown>[]): Response {
  return new Response(
    events
      .map(
        (event) =>
          `event: ${event["type"]}\ndata: ${JSON.stringify(event)}\n\n`,
      )
      .join(""),
    { headers: { "content-type": "text/event-stream" } },
  );
}
function streamEvents(tool = false): Record<string, unknown>[] {
  return [
    {
      type: "message_start",
      message: apiMessage([], null as unknown as string),
    },
    {
      type: "content_block_start",
      index: 0,
      content_block: tool
        ? { type: "tool_use", id: "tool_1", name: "read_file", input: {} }
        : { type: "text", text: "" },
    },
    {
      type: "content_block_delta",
      index: 0,
      delta: tool
        ? { type: "input_json_delta", partial_json: '{"path":"a"}' }
        : { type: "text_delta", text: "hello" },
    },
    { type: "content_block_stop", index: 0 },
    {
      type: "message_delta",
      delta: {
        stop_reason: tool ? "tool_use" : "end_turn",
        stop_sequence: null,
      },
      usage: { output_tokens: 2 },
    },
    { type: "message_stop" },
  ];
}
test("provider uses configured endpoint, key and SDK normalized response", async () => {
  let seen = "";
  const provider = new AnthropicProvider(
    { ...config, baseUrl: "https://custom.example" },
    async (input, init) => {
      seen = String(input);
      assert.equal(new Headers(init?.headers).get("x-api-key"), "test-secret");
      const request = JSON.parse(String(init?.body)) as Record<string, unknown>;
      assert.equal(request["model"], "test-model");
      return new Response(JSON.stringify(apiMessage()), {
        headers: { "content-type": "application/json" },
      });
    },
  );
  const response = await provider.createMessage(
    { messages: [{ role: "user", content: "task" }], tools: [] },
    new AbortController().signal,
  );
  assert.match(seen, /custom.example\/v1\/messages/);
  assert.equal(response.content[0]?.type, "text");
  assert.equal(response.input_tokens, 10);
});
test("provider retries only transient errors and redacts response bodies", async () => {
  let attempts = 0;
  const provider = new AnthropicProvider(
    { ...config, requestRetries: 2 },
    async () => {
      attempts++;
      return attempts === 1
        ? new Response("test-secret echoed", { status: 503 })
        : new Response(JSON.stringify(apiMessage()), {
            headers: { "content-type": "application/json" },
          });
    },
  );
  await provider.createMessage(
    { messages: [{ role: "user", content: "task" }], tools: [] },
    new AbortController().signal,
  );
  assert.equal(attempts, 2);
  attempts = 0;
  const denied = new AnthropicProvider(
    { ...config, requestRetries: 2 },
    async () => {
      attempts++;
      return new Response("test-secret", { status: 401 });
    },
  );
  await assert.rejects(
    denied.createMessage(
      { messages: [], tools: [] },
      new AbortController().signal,
    ),
    (e) =>
      e instanceof Error &&
      e.message.includes("Authentication") &&
      !e.message.includes("test-secret"),
  );
  assert.equal(attempts, 1);
});
test("streaming provider emits text and waits for complete tool JSON", async () => {
  let output = "";
  const provider = new AnthropicProvider(config, async () =>
    sse(streamEvents()),
  );
  const response = await provider.createMessage(
    {
      messages: [],
      tools: [],
      onText: (t) => {
        output += t;
      },
    },
    new AbortController().signal,
  );
  assert.equal(output, "hello");
  assert.equal(response.stop_reason, "end_turn");
  const toolProvider = new AnthropicProvider(config, async () =>
    sse(streamEvents(true)),
  );
  const result = await toolProvider.createMessage(
    { messages: [], tools: [], onText: () => {} },
    new AbortController().signal,
  );
  assert.deepEqual(result.content, [
    call("read_file", { path: "a" }, "tool_1"),
  ]);
});
test("incomplete stream after text is not retried; agent retains partial text", async (t) => {
  const { workspace } = await fixture(t);
  let attempts = 0,
    output = "";
  const provider = new AnthropicProvider(
    { ...config, requestRetries: 2 },
    async () => {
      attempts++;
      return sse(streamEvents().slice(0, 3));
    },
  );
  const history: Message[] = [];
  await assert.rejects(
    runAgent("task", {
      provider,
      workspace,
      maxTokens: 1024,
      history,
      onText: (t) => {
        output += t;
      },
    }),
    /not retried/,
  );
  assert.equal(attempts, 1);
  assert.equal(output, "hello");
  assert.match(JSON.stringify(history), /hello/);
});
test("cancelled network wait closes pending model call", async () => {
  const provider = new AnthropicProvider(
    config,
    async (_input, init) =>
      new Promise((_resolve, reject) => {
        init?.signal?.addEventListener(
          "abort",
          () => reject(new DOMException("Abort", "AbortError")),
          { once: true },
        );
      }),
  );
  const controller = new AbortController();
  const pending = provider.createMessage(
    { messages: [], tools: [], onText: () => {} },
    controller.signal,
  );
  setTimeout(() => controller.abort(), 20);
  await assert.rejects(pending, TurnCancelled);
});
