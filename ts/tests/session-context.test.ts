import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs/promises";
import path from "node:path";
import { fixture, call } from "./helpers.js";
import { SessionStore } from "../src/persistence/session.js";
import { validateHistory } from "../src/persistence/validation.js";
import {
  ContextBudget,
  compactHistory,
  estimateTokens,
} from "../src/core/context.js";
import { TaskBoard } from "../src/core/tasks.js";
import { loadInstructions } from "../src/integrations/instructions.js";
import { SkillCatalog } from "../src/integrations/skills.js";
import type { Message, ToolResultBlock } from "../src/core/types.js";

test("session Unicode roundtrip, list, search and rename", async (t) => {
  const { root, state } = await fixture(t);
  const store = new SessionStore(state),
    session = await store.create(root, "test-model");
  await session.append({
    type: "message.appended",
    message: { role: "user", content: "你好，保留这个决定" },
  });
  await session.append({
    type: "message.appended",
    message: {
      role: "assistant",
      content: [{ type: "text", text: "决定使用 TS" }],
    },
  });
  await session.append({ type: "session.named", name: "名字" });
  await session.append({ type: "turn.finished", status: "ok" });
  await session.close();
  const resumed = await store.resume(session.id, root);
  assert.equal(resumed.name, "名字");
  assert.equal(resumed.history.length, 2);
  await resumed.close();
  assert.equal((await store.list(root))[0]?.name, "名字");
  assert.equal((await store.search(root, "决定")).length, 2);
  await store.rename(session.id, root, "renamed");
  assert.equal((await store.list(root))[0]?.name, "renamed");
});
test("session locking rejects live concurrent writers", async (t) => {
  const { root, state } = await fixture(t);
  const store = new SessionStore(state),
    session = await store.create(root, "m");
  try {
    await assert.rejects(store.resume(session.id, root), /already open/);
  } finally {
    await session.close();
  }
  const next = await store.resume(session.id, root);
  await next.close();
});
test("session stale-lock recovery and replaced-token ownership", async (t) => {
  const { root, state } = await fixture(t);
  const store = new SessionStore(state),
    session = await store.create(root, "m");
  await session.close();
  const lock = session.path.replace(/\.jsonl$/, ".lock");
  await fs.writeFile(lock, "2147483647 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n");
  const resumed = await store.resume(session.id, root);
  await fs.writeFile(lock, `${process.pid} bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n`);
  await resumed.close();
  assert.match(await fs.readFile(lock, "utf8"), /bbbb/);
  await fs.unlink(lock);
});
test("session cancels incomplete tool exchange without replaying and retains completed result", async (t) => {
  const { root, state } = await fixture(t);
  const store = new SessionStore(state),
    session = await store.create(root, "m");
  await session.append({
    type: "message.appended",
    message: { role: "user", content: "task" },
  });
  await session.append({
    type: "message.appended",
    message: {
      role: "assistant",
      content: [
        call("read_file", { path: "a" }, "a"),
        call("create_file", { path: "b", content: "x" }, "b"),
      ],
    },
  });
  await session.append({
    type: "tool.result",
    result: {
      type: "tool_result",
      tool_use_id: "a",
      content: "existing result",
      is_error: false,
    },
  });
  await session.close();
  const resumed = await store.resume(session.id, root);
  const last = resumed.history.at(-1);
  assert.ok(last?.role === "user" && typeof last.content !== "string");
  assert.equal(last.content.length, 2);
  assert.equal(last.content[0]?.content, "existing result");
  assert.match(last.content[1]?.content ?? "", /may have partially executed/);
  assert.match(resumed.recoveryWarning ?? "", /not replayed/);
  await assert.rejects(fs.access(path.join(root, "b")));
  validateHistory(resumed.history);
  await resumed.close();
});
test("partial tail search is read-only; resume truncates it, complete corrupt record errors", async (t) => {
  const { root, state } = await fixture(t);
  const store = new SessionStore(state),
    session = await store.create(root, "m");
  await session.append({
    type: "message.appended",
    message: { role: "user", content: "needle" },
  });
  await session.close();
  await fs.appendFile(session.path, '{"partial":');
  const original = await fs.readFile(session.path);
  assert.equal((await store.search(root, "needle")).length, 1);
  assert.deepEqual(await fs.readFile(session.path), original);
  const resumed = await store.resume(session.id, root);
  assert.match(resumed.recoveryWarning ?? "", /partial/);
  await resumed.close();
  assert.equal((await fs.readFile(session.path)).at(-1), 10);
  await fs.appendFile(session.path, "bad-complete-record\n");
  await assert.rejects(store.resume(session.id, root), /corrupt/);
  await assert.rejects(fs.access(session.path.replace(/\.jsonl$/, ".lock")));
});
test("session rejects invalid events and serializes concurrent appends", async (t) => {
  const { root, state } = await fixture(t);
  const store = new SessionStore(state),
    session = await store.create(root, "m");
  await Promise.all(
    Array.from({ length: 10 }, (_, i) =>
      session.append({ type: "session.named", name: "name " + i }),
    ),
  );
  await assert.rejects(session.append({ type: "session.named", name: " " }));
  await session.close();
  const resumed = await store.resume(session.id, root);
  assert.equal(resumed.name, "name 9");
  await resumed.close();
});
test("sessions stay scoped to their originating workspace", async (t) => {
  const { root, state } = await fixture(t);
  const other = path.join(path.dirname(root), "other");
  await fs.mkdir(other);
  const store = new SessionStore(state),
    session = await store.create(root, "m");
  await session.close();
  assert.deepEqual(await store.list(other), []);
  await assert.rejects(store.resume(session.id, other), /not found/);
  await assert.rejects(store.continueLatest(other), /No saved/);
});
test("task event failures preserve old state; tasks survive compaction", async (t) => {
  const { root, state } = await fixture(t);
  const board = new TaskBoard();
  await board.update([{ id: "1", description: "old", status: "pending" }]);
  await assert.rejects(
    board.update(
      [{ id: "2", description: "new", status: "completed" }],
      async () => {
        throw new Error("disk");
      },
    ),
  );
  assert.equal(board.tasks[0]?.id, "1");
  await assert.rejects(
    board.update([
      { id: "1", description: "a", status: "pending" },
      { id: "1", description: "b", status: "completed" },
    ]),
  );
  const store = new SessionStore(state),
    session = await store.create(root, "m");
  await session.append({ type: "tasks.updated", tasks: board.tasks });
  await session.append({
    type: "context.compacted",
    history: [{ role: "user", content: "summary" }],
  });
  await session.close();
  const resumed = await store.resume(session.id, root);
  assert.equal(resumed.tasks[0]?.id, "1");
  await resumed.close();
});
test("history validator rejects orphaned results, duplicate IDs and unfinished tools", () => {
  const result: ToolResultBlock = {
    type: "tool_result",
    tool_use_id: "x",
    content: "x",
    is_error: false,
  };
  assert.throws(() => validateHistory([{ role: "user", content: [result] }]));
  assert.throws(() =>
    validateHistory([
      {
        role: "assistant",
        content: [call("read_file", {}, "x"), call("read_file", {}, "x")],
      },
    ]),
  );
  assert.throws(() =>
    validateHistory([
      { role: "assistant", content: [call("read_file", {}, "x")] },
    ]),
  );
});
test("budget counts UTF-8, schemas, system and output reserve; truncation fits", () => {
  assert.equal(estimateTokens("你好😀"), 4);
  const budget = new ContextBudget(200, 20);
  const messages: Message[] = [{ role: "user", content: "task" }];
  assert.ok(
    budget.requestTokens(messages, [], "system") >
      budget.requestTokens(messages),
  );
  const [text, truncated] = budget.fitResult(
    "😀".repeat(1000),
    (content) => [...messages, { role: "user", content }],
    [],
    "system",
  );
  assert.equal(truncated, true);
  budget.ensureFits(
    [...messages, { role: "user", content: text }],
    [],
    "system",
  );
  assert.throws(() => new ContextBudget(10, 10));
});
test("compaction preserves original goal, recent requirements and complete tool pairs", () => {
  const messages: Message[] = [
    { role: "user", content: "Original goal: build a coding agent" },
  ];
  for (let i = 0; i < 8; i++)
    messages.push(
      {
        role: "assistant",
        content: [{ type: "text", text: "old update ".repeat(100) }],
      },
      { role: "user", content: "recent requirement " + i },
    );
  messages.push(
    { role: "assistant", content: [call("read_file", { path: "a" }, "last")] },
    {
      role: "user",
      content: [
        {
          type: "tool_result",
          tool_use_id: "last",
          content: "read contents",
          is_error: false,
        },
      ],
    },
    { role: "user", content: "keep Chinese text" },
  );
  const compacted = compactHistory(messages, new ContextBudget(1800, 100));
  assert.ok(compacted);
  assert.match(JSON.stringify(compacted), /Original goal/);
  assert.match(JSON.stringify(compacted), /recent requirement/);
  assert.match(JSON.stringify(compacted), /keep Chinese text/);
  validateHistory(compacted);
});
test("instructions respect directory order and skills load only on demand", async (t) => {
  const { root, workspace } = await fixture(t);
  await fs.mkdir(path.join(root, "nested"));
  await fs.writeFile(path.join(root, "AGENTS.md"), "root agents");
  await fs.writeFile(path.join(root, "CLAUDE.md"), "root claude");
  await fs.writeFile(path.join(root, "nested", "AGENTS.md"), "nested agents");
  const instructions = await loadInstructions(
    workspace,
    path.join(root, "nested"),
  );
  assert.deepEqual(
    instructions.sources.map((s) => s.content),
    ["root agents", "root claude", "nested agents"],
  );
  const dir = path.join(root, ".mclaude", "skills", "example");
  await fs.mkdir(dir, { recursive: true });
  const skillPath = path.join(dir, "SKILL.md");
  await fs.writeFile(
    skillPath,
    "---\nname: example\ndescription: useful skill\n---\nsecret body",
  );
  const skills = await SkillCatalog.discover(workspace);
  assert.equal(skills.summary(), "- example: useful skill");
  assert.doesNotMatch(skills.summary(), /secret body/);
  assert.match((await skills.load("example")).content, /secret body/);
  await fs.writeFile(
    skillPath,
    "---\nname: example\ndescription: changed\n---\nbody",
  );
  await assert.rejects(skills.load("example"), /changed/);
});
