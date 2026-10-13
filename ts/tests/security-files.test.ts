import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs/promises";
import path from "node:path";
import { fixture } from "./helpers.js";
import { resolveConfig, validateConfig } from "../src/core/config.js";
import { PermissionGate, defaultPolicy } from "../src/security/permissions.js";
import { CheckpointStore } from "../src/persistence/checkpoints.js";
import { readFile, findFiles, searchText } from "../src/tools/files/read.js";
import {
  createFile,
  replaceText,
  applyEdits,
} from "../src/tools/files/edit.js";
import { ConfigStore } from "../src/persistence/config-store.js";
import {
  digest,
  normalizeWorkspace,
  workspaceKey,
} from "../src/persistence/paths.js";
import { MClaudeError } from "../src/core/types.js";

test("configuration priorities, empty env and official endpoint", () => {
  const saved = {
    api_key: "saved",
    model: "saved-model",
    base_url: "https://saved.example/api",
  };
  assert.equal(
    resolveConfig({ model: "flag" }, saved, {
      ANTHROPIC_API_KEY: "env",
      ANTHROPIC_MODEL: "env-model",
    }).model,
    "flag",
  );
  assert.equal(
    resolveConfig({}, saved, {
      ANTHROPIC_API_KEY: "",
      ANTHROPIC_MODEL: "",
      ANTHROPIC_BASE_URL: "",
    }).baseUrl,
    undefined,
  );
  assert.equal(resolveConfig({}, saved, {}).baseUrl, saved.base_url);
  assert.throws(() => resolveConfig({}, {}, {}), /API_KEY/);
});
for (const baseUrl of [
  "file:///secret",
  "https://user:pass@example.com",
  "https://example.com/?key=secret",
  "https://example.com/#frag",
  "https://example.com:0",
])
  test(`reject invalid API URL ${baseUrl}`, () =>
    assert.throws(() =>
      resolveConfig({ apiKey: "key", model: "m", baseUrl }, {}, {}),
    ));
test("config limits reject NaN and invalid retry counts", () => {
  const c = resolveConfig({ apiKey: "key", model: "m" }, {}, {});
  for (const change of [
    { timeout: NaN },
    { maxTokens: 1.5 },
    { requestRetries: 11 },
    { requestRetries: -1 },
    { retryDelay: -1 },
  ])
    assert.throws(() => validateConfig({ ...c, ...change }));
});
test("permission policy is fail-closed and external annotations cannot grant access", async () => {
  assert.equal(
    defaultPolicy({ tool_name: "read_file", tool_input: {} }).action,
    "allow",
  );
  assert.equal(
    defaultPolicy({ tool_name: "new_tool", tool_input: {} }).action,
    "deny",
  );
  assert.equal(
    defaultPolicy({ tool_name: "read_file", tool_input: {}, external: true })
      .action,
    "ask",
  );
  assert.equal(
    (
      await new PermissionGate().check({
        tool_name: "create_file",
        tool_input: {},
      })
    ).action,
    "deny",
  );
  assert.equal(
    (
      await new PermissionGate(async () => true).check({
        tool_name: "create_file",
        tool_input: {},
      })
    ).action,
    "allow",
  );
  assert.equal(
    (
      await new PermissionGate(async () => {
        throw new Error("secret");
      }).check({ tool_name: "create_file", tool_input: {} })
    ).action,
    "deny",
  );
  assert.equal(
    (
      await new PermissionGate(undefined, () => {
        throw new Error();
      }).check({ tool_name: "read_file", tool_input: {} })
    ).action,
    "deny",
  );
});
test("workspace containment rejects sibling prefixes and parent escapes", async (t) => {
  const { workspace, root } = await fixture(t);
  await assert.rejects(workspace.resolve("../workspace-other/a"), /outside/);
  await assert.rejects(
    workspace.resolve(path.join(root + "-other", "a")),
    /outside/,
  );
  assert.equal(
    await workspace.resolve("nested/new.txt"),
    path.join(root, "nested", "new.txt"),
  );
});
test("outside directory junction cannot be read or written", async (t) => {
  const { workspace, root, state } = await fixture(t);
  await fs.mkdir(state);
  try {
    await fs.symlink(
      state,
      path.join(root, "escape"),
      process.platform === "win32" ? "junction" : "dir",
    );
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === "EPERM") {
      t.skip("Link privilege unavailable.");
      return;
    }
    throw e;
  }
  await assert.rejects(workspace.resolve("escape/file"), /outside/);
  assert.equal(
    await workspace.noLinks(path.join(root, "escape", "file")),
    false,
  );
});
test("read Unicode, truncate by code point, reject invalid UTF-8 and directories", async (t) => {
  const { workspace, root } = await fixture(t);
  await fs.writeFile(path.join(root, "text"), "你好😀abc\r\n");
  assert.equal(
    (await readFile({ path: "text" }, workspace)).content,
    "你好😀abc\n",
  );
  assert.match(
    (await readFile({ path: "text" }, workspace, undefined, 3)).content,
    /^你好😀\n\n\[Output truncated/,
  );
  await fs.writeFile(path.join(root, "bad"), Buffer.from([0xff]));
  await assert.rejects(readFile({ path: "bad" }, workspace));
  await assert.rejects(readFile({ path: "." }, workspace));
});
test("nested gitignore negation and stable git-style glob discovery", async (t) => {
  const { workspace, root } = await fixture(t);
  await fs.mkdir(path.join(root, "nested"));
  await fs.writeFile(path.join(root, ".gitignore"), "*.log\nignored/\n");
  await fs.writeFile(path.join(root, "a.log"), "secret");
  await fs.writeFile(path.join(root, "a.ts"), "needle");
  await fs.writeFile(path.join(root, "nested", ".gitignore"), "!keep.log\n");
  await fs.writeFile(path.join(root, "nested", "keep.log"), "needle");
  await fs.writeFile(path.join(root, "nested", "other.log"), "secret");
  await fs.writeFile(path.join(root, "nested", "b.ts"), "needle");
  assert.equal(
    (await findFiles({ pattern: "*.ts" }, workspace)).content,
    "a.ts\nnested/b.ts",
  );
  assert.equal(
    (await findFiles({ pattern: "*.log" }, workspace)).content,
    "nested/keep.log",
  );
  const result = await searchText({ query: "needle" }, workspace);
  assert.match(result.content, /a.ts:1:needle/);
  assert.match(result.content, /nested\/keep.log:1:needle/);
  assert.doesNotMatch(result.content, /secret/);
});
test("search regex, scope, line limit, result limit and binary exclusion", async (t) => {
  const { workspace, root } = await fixture(t);
  await fs.writeFile(
    path.join(root, "a.txt"),
    "HELLO\nhello there\n" + "x".repeat(600),
  );
  await fs.writeFile(path.join(root, "bad.txt"), Buffer.from([0xff]));
  assert.equal(
    (
      await searchText(
        { query: "hello", case_sensitive: false, path: "a.txt" },
        workspace,
        undefined,
        1,
      )
    ).content,
    "a.txt:1:HELLO\n[Results truncated after 1 matches.]",
  );
  assert.match(
    (
      await searchText(
        { query: "^hello", is_regex: true, path: "a.txt" },
        workspace,
      )
    ).content,
    /a.txt:2:/,
  );
  assert.match((await searchText({ query: "x" }, workspace)).content, /…$/);
  await assert.rejects(searchText({ query: "[", is_regex: true }, workspace));
  await assert.rejects(
    searchText({ query: "(?P<word>x)", is_regex: true }, workspace),
    /unsupported/,
  );
});
test("create, unique replace and byte-exact checkpoint recovery", async (t) => {
  const { workspace, root, state } = await fixture(t);
  const checkpoints = new CheckpointStore(workspace, state);
  const created = await createFile(
    { path: "nested/a.txt", content: "hello\r\n你好" },
    workspace,
    checkpoints,
  );
  assert.equal(created.is_error, false);
  await assert.rejects(
    createFile(
      { path: "nested/a.txt", content: "overwrite" },
      workspace,
      checkpoints,
    ),
    /overwrite/,
  );
  const original = await fs.readFile(path.join(root, "nested", "a.txt"));
  const replaced = await replaceText(
    { path: "nested/a.txt", old_text: "hello", new_text: "world" },
    workspace,
    checkpoints,
  );
  assert.equal(replaced.is_error, false);
  assert.equal(
    await fs.readFile(path.join(root, "nested", "a.txt"), "utf8"),
    "world\r\n你好",
  );
  const id = /Checkpoint: ([0-9a-f]{32})/.exec(replaced.content)?.[1];
  assert.ok(id);
  await checkpoints.restore(id);
  assert.deepEqual(
    await fs.readFile(path.join(root, "nested", "a.txt")),
    original,
  );
  await assert.rejects(checkpoints.restore(id), /already/);
});
test("replacement refuses missing, ambiguous and unchanged text", async (t) => {
  const { workspace, root, state } = await fixture(t);
  await fs.writeFile(path.join(root, "a"), "xx xx");
  const c = new CheckpointStore(workspace, state);
  for (const [old_text, new_text] of [
    ["absent", "new"],
    ["xx", "new"],
    ["xx xx", "xx xx"],
  ])
    await assert.rejects(
      replaceText({ path: "a", old_text, new_text }, workspace, c),
    );
  assert.equal(await fs.readFile(path.join(root, "a"), "utf8"), "xx xx");
});
test("checkpoint detects external edits and removes unchanged created files", async (t) => {
  const { workspace, root, state } = await fixture(t);
  const c = new CheckpointStore(workspace, state);
  const result = await createFile({ path: "a", content: "a" }, workspace, c);
  const id = /Checkpoint: ([0-9a-f]{32})/.exec(result.content)?.[1];
  assert.ok(id);
  await fs.writeFile(path.join(root, "a"), "external");
  await assert.rejects(c.restore(id), /conflict/);
  await fs.writeFile(path.join(root, "a"), "a");
  await c.restore(id);
  await assert.rejects(fs.access(path.join(root, "a")));
});
test("batch preflight prevents partial edits and duplicate file identities", async (t) => {
  const { workspace, root, state } = await fixture(t);
  const c = new CheckpointStore(workspace, state);
  await fs.writeFile(path.join(root, "a"), "old");
  await assert.rejects(
    applyEdits(
      {
        edits: [
          { kind: "replace", path: "a", old_text: "old", new_text: "new" },
          { kind: "replace", path: "missing", old_text: "x", new_text: "y" },
        ],
      },
      workspace,
      c,
    ),
  );
  assert.equal(await fs.readFile(path.join(root, "a"), "utf8"), "old");
  assert.deepEqual(await c.list(), []);
  await assert.rejects(
    applyEdits(
      {
        edits: [
          { kind: "create", path: "new", content: "x" },
          { kind: "create", path: "./new", content: "y" },
        ],
      },
      workspace,
      c,
    ),
    /repeats/,
  );
});
test("batch rolls back prior changes when a later file is created externally", async (t) => {
  const { workspace, root, state } = await fixture(t);
  await fs.writeFile(path.join(root, "a"), "old");
  class RacingStore extends CheckpointStore {
    override async save(
      file: string,
      before: Buffer | null,
      after: Buffer,
    ): Promise<string> {
      const id = await super.save(file, before, after);
      if (path.basename(file) === "b") await fs.writeFile(file, "external");
      return id;
    }
  }
  const c = new RacingStore(workspace, state);
  const result = await applyEdits(
    {
      edits: [
        { kind: "replace", path: "a", old_text: "old", new_text: "new" },
        { kind: "create", path: "b", content: "agent" },
      ],
    },
    workspace,
    c,
  );
  assert.equal(result.is_error, true);
  assert.match(result.content, /rolled back/);
  assert.equal(await fs.readFile(path.join(root, "a"), "utf8"), "old");
  assert.equal(await fs.readFile(path.join(root, "b"), "utf8"), "external");
  assert.deepEqual(await c.list(), []);
});
test("checkpoint persistence failure occurs before modifying any file", async (t) => {
  const { workspace, root, state } = await fixture(t);
  await fs.writeFile(path.join(root, "a"), "old");
  class FailingStore extends CheckpointStore {
    override async save(): Promise<string> {
      throw new MClaudeError("Disk full.");
    }
  }
  await assert.rejects(
    replaceText(
      { path: "a", old_text: "old", new_text: "new" },
      workspace,
      new FailingStore(workspace, state),
    ),
    /Disk full/,
  );
  assert.equal(await fs.readFile(path.join(root, "a"), "utf8"), "old");
});
test("config store is atomic, redacted and logout preserves state", async (t) => {
  const { state } = await fixture(t);
  const file = path.join(state, "config.json"),
    store = new ConfigStore(file);
  await store.save({ api_key: "super-secret", model: "m", base_url: "" });
  assert.deepEqual(await store.load(), {
    api_key: "super-secret",
    model: "m",
    base_url: "",
  });
  const before = await fs.readFile(file);
  await assert.rejects(
    store.save({ api_key: "new-secret", model: "m", base_url: "file://bad" }),
  );
  assert.deepEqual(await fs.readFile(file), before);
  await fs.writeFile(file, "super-secret-invalid-json");
  await assert.rejects(
    store.load(),
    (error) =>
      error instanceof Error && !error.message.includes("super-secret"),
  );
  assert.equal(await store.logout(), true);
  assert.equal(await store.logout(), false);
  assert.deepEqual(await store.load(), {});
});
test("workspace key uses the documented normalized UTF-8 hash", () => {
  const workspace = process.cwd();
  assert.equal(
    workspaceKey(workspace),
    digest(Buffer.from(normalizeWorkspace(workspace))).slice(0, 20),
  );
});
