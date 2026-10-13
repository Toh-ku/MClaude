import test from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs/promises";
import path from "node:path";
import { runAgent } from "../src/core/agent.js";
import { instructionsForPath } from "../src/integrations/instructions.js";
import { PermissionGate } from "../src/security/permissions.js";
import { CheckpointStore } from "../src/persistence/checkpoints.js";
import { fixture, scripted, tools, call, final } from "./helpers.js";

test("checkpoint restore reviews newly introduced rules before reverting a file", async (t) => {
  const { root, workspace, state } = await fixture(t);
  await fs.mkdir(path.join(root, "src"));
  const file = path.join(root, "src", "module.ts");
  await fs.writeFile(file, "after");
  const checkpoints = new CheckpointStore(workspace, state);
  const id = await checkpoints.save(
    file,
    Buffer.from("before"),
    Buffer.from("after"),
  );
  await fs.writeFile(path.join(root, "src", "AGENTS.md"), "Review recovery");
  const provider = scripted([
    tools(
      call(
        "restore_edit_checkpoint",
        { checkpoint_id: id.toUpperCase() },
        "restore-1",
      ),
    ),
    tools(
      call(
        "restore_edit_checkpoint",
        { checkpoint_id: id.toUpperCase() },
        "restore-2",
      ),
    ),
    final(),
  ]);
  await runAgent("restore", {
    provider,
    workspace,
    maxTokens: 100,
    checkpoints,
    permissionGate: new PermissionGate(async () => true),
  });
  assert.match(provider.requests[1]!.system!, /Review recovery/);
  assert.equal(await fs.readFile(file, "utf8"), "before");
});

test("removing a delivered rule during approval also defers the edit", async (t) => {
  const { root, workspace } = await fixture(t);
  const rule = path.join(root, "AGENTS.md"),
    file = path.join(root, "module.ts");
  await fs.writeFile(rule, "Old rule");
  await fs.writeFile(file, "before");
  const provider = scripted([
    tools(
      call("replace_text", {
        path: "module.ts",
        old_text: "before",
        new_text: "after",
      }),
    ),
    final(),
  ]);
  await runAgent("edit", {
    provider,
    workspace,
    maxTokens: 100,
    permissionGate: new PermissionGate(async () => {
      await fs.unlink(rule);
      return true;
    }),
  });
  assert.equal(await fs.readFile(file, "utf8"), "before");
  assert.match(
    JSON.stringify(provider.requests[1]!.messages),
    /Previously supplied rule removed/,
  );
});

test("new file rules follow existing ancestors with correct order and sibling isolation", async (t) => {
  const { root, workspace } = await fixture(t);
  await fs.mkdir(path.join(root, "src"));
  await fs.mkdir(path.join(root, "other"));
  await fs.writeFile(path.join(root, "AGENTS.md"), "root");
  await fs.writeFile(path.join(root, "CLAUDE.md"), "root later");
  await fs.writeFile(path.join(root, "src", "AGENTS.md"), "src only");
  await fs.writeFile(path.join(root, "src", "CLAUDE.md"), "src later");
  await fs.writeFile(path.join(root, "other", "AGENTS.md"), "sibling only");
  const rules = await instructionsForPath(workspace, "src/new/deep/module.ts");
  assert.deepEqual(
    rules.sources.map((s) => s.content),
    ["root", "root later", "src only", "src later"],
  );
  assert.doesNotMatch(rules.system, /sibling only/);
  await assert.rejects(
    instructionsForPath(workspace, "../outside.ts"),
    /outside/,
  );
});

test("reading a nested file loads applicable rules into results and the next request", async (t) => {
  const { root, workspace } = await fixture(t);
  await fs.mkdir(path.join(root, "src"));
  await fs.writeFile(path.join(root, "src", "AGENTS.md"), "Use local tests");
  await fs.writeFile(path.join(root, "src", "module.ts"), "value");
  const provider = scripted([
    tools(call("read_file", { path: "src/module.ts" })),
    final(),
  ]);
  await runAgent("read", { provider, workspace, maxTokens: 100 });
  assert.doesNotMatch(provider.requests[0]!.system ?? "", /Use local tests/);
  assert.match(provider.requests[1]!.system!, /Use local tests/);
  assert.match(
    JSON.stringify(provider.requests[1]!.messages),
    /Use local tests/,
  );
});

test("first nested edit is deferred before approval, then succeeds after rules are delivered", async (t) => {
  const { root, workspace, state } = await fixture(t);
  await fs.mkdir(path.join(root, "src"));
  await fs.writeFile(path.join(root, "src", "AGENTS.md"), "Keep compatibility");
  const file = path.join(root, "src", "module.ts");
  await fs.writeFile(file, "before");
  let approvals = 0,
    requests = 0;
  await runAgent("edit", {
    workspace,
    maxTokens: 100,
    checkpoints: new CheckpointStore(workspace, state),
    permissionGate: new PermissionGate(async () => {
      approvals++;
      return true;
    }),
    provider: {
      async createMessage(request) {
        requests++;
        if (requests === 2) {
          assert.equal(await fs.readFile(file, "utf8"), "before");
          assert.equal(approvals, 0);
          assert.match(request.system!, /Keep compatibility/);
        }
        return requests <= 2
          ? tools(
              call(
                "replace_text",
                {
                  path: "src/module.ts",
                  old_text: "before",
                  new_text: "after",
                },
                `edit-${requests}`,
              ),
            )
          : final();
      },
    },
  });
  assert.equal(approvals, 1);
  assert.equal(await fs.readFile(file, "utf8"), "after");
});

test("a read followed by an edit in one response cannot bypass unseen-rule protection", async (t) => {
  const { root, workspace, state } = await fixture(t);
  await fs.mkdir(path.join(root, "src"));
  await fs.writeFile(path.join(root, "src", "AGENTS.md"), "Nested rule");
  const file = path.join(root, "src", "module.ts");
  await fs.writeFile(file, "before");
  const provider = scripted([
    tools(
      call("read_file", { path: "src/module.ts" }, "read"),
      call(
        "replace_text",
        { path: "src/module.ts", old_text: "before", new_text: "after" },
        "edit",
      ),
    ),
    final("Rules reviewed"),
  ]);
  await runAgent("read and edit", {
    provider,
    workspace,
    maxTokens: 100,
    checkpoints: new CheckpointStore(workspace, state),
    permissionGate: new PermissionGate(async () => true),
  });
  assert.equal(await fs.readFile(file, "utf8"), "before");
  assert.match(
    JSON.stringify(provider.requests[1]!.messages),
    /No file changes were made/,
  );
});

test("batch edits load all target scopes and defer the whole batch", async (t) => {
  const { root, workspace, state } = await fixture(t);
  for (const dir of ["a", "b"]) {
    await fs.mkdir(path.join(root, dir));
    await fs.writeFile(path.join(root, dir, "AGENTS.md"), `${dir} only rule`);
    await fs.writeFile(path.join(root, dir, "module.ts"), "before");
  }
  const edits = ["a", "b"].map((dir) => ({
    kind: "replace",
    path: `${dir}/module.ts`,
    old_text: "before",
    new_text: "after",
  }));
  const provider = scripted([
    tools(call("apply_edits", { edits }, "batch-1")),
    tools(call("apply_edits", { edits }, "batch-2")),
    final(),
  ]);
  await runAgent("edit both", {
    provider,
    workspace,
    maxTokens: 100,
    checkpoints: new CheckpointStore(workspace, state),
    permissionGate: new PermissionGate(async () => true),
  });
  assert.match(provider.requests[1]!.system!, /a only rule/);
  assert.match(provider.requests[1]!.system!, /b only rule/);
  assert.match(provider.requests[1]!.system!, /sibling scopes never override/);
  for (const dir of ["a", "b"])
    assert.equal(
      await fs.readFile(path.join(root, dir, "module.ts"), "utf8"),
      "after",
    );
});

test("rules changed during permission approval block the edit and reload on the next request", async (t) => {
  const { root, workspace } = await fixture(t);
  const rule = path.join(root, "AGENTS.md"),
    file = path.join(root, "module.ts");
  await fs.writeFile(rule, "Old rule");
  await fs.writeFile(file, "before");
  const provider = scripted([
    tools(
      call("replace_text", {
        path: "module.ts",
        old_text: "before",
        new_text: "after",
      }),
    ),
    final(),
  ]);
  await runAgent("edit", {
    provider,
    workspace,
    maxTokens: 100,
    permissionGate: new PermissionGate(async () => {
      await fs.writeFile(rule, "Changed rule");
      return true;
    }),
  });
  assert.equal(await fs.readFile(file, "utf8"), "before");
  assert.match(provider.requests[1]!.system!, /Changed rule/);
});
