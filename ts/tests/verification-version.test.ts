import test from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs/promises";
import path from "node:path";
import { VerificationWorkflow } from "../src/core/verification.js";
import { workspaceVersion } from "../src/core/workspace-version.js";
import { runAgent } from "../src/core/agent.js";
import { PermissionGate } from "../src/security/permissions.js";
import { fixture, scripted, tools, call, final } from "./helpers.js";

test("passed checks bind to content, detecting same-size external changes and retesting", async (t) => {
  const { root, workspace } = await fixture(t);
  const file = path.join(root, "module.ts");
  await fs.writeFile(file, "export const value = 1;\n");
  const flow = new VerificationWorkflow();
  await flow.run({ checks: [{ command: "echo passed" }] }, workspace);
  const version = flow.version;
  assert.match(version!, /^[a-f0-9]{64}$/);
  await flow.refresh(workspace);
  assert.equal(flow.state, "passed");
  await fs.writeFile(file, "export const value = 2;\n");
  await flow.refresh(workspace);
  assert.equal(flow.state, "stale");
  await flow.run({}, workspace, true);
  assert.equal(flow.state, "passed");
  assert.notEqual(flow.version, version);
});

test("versions include added/deleted untracked files and exclude ignored build output", async (t) => {
  const { root, workspace } = await fixture(t);
  await fs.writeFile(path.join(root, ".gitignore"), "dist/\n");
  const before = await workspaceVersion(workspace);
  await fs.mkdir(path.join(root, "dist"));
  await fs.writeFile(path.join(root, "dist", "build.js"), "artifact");
  assert.equal(await workspaceVersion(workspace), before);
  await fs.writeFile(path.join(root, "new.ts"), "source");
  assert.notEqual(await workspaceVersion(workspace), before);
  await fs.unlink(path.join(root, "new.ts"));
  assert.equal(await workspaceVersion(workspace), before);
});

test("successful commands that modify sources during checks cannot establish passed state", async (t) => {
  const { workspace } = await fixture(t);
  const flow = new VerificationWorkflow();
  const result = await flow.run(
    { checks: [{ command: "echo changed > module.ts" }] },
    workspace,
  );
  assert.equal(result.is_error, true);
  assert.equal(flow.state, "stale");
  assert.match(result.content, /changed during verification/);
});

test("command edits invalidate checks within the same tool batch and before the next request", async (t) => {
  const { root, workspace } = await fixture(t);
  await fs.writeFile(path.join(root, "module.ts"), "before");
  const provider = scripted([
    tools(
      call("run_checks", { checks: [{ command: "echo passed" }] }, "checks"),
      call("run_command", { command: "echo changed > module.ts" }, "edit"),
    ),
    final(),
  ]);
  const flow = new VerificationWorkflow();
  await runAgent("Check then change code", {
    provider,
    workspace,
    maxTokens: 100,
    verification: flow,
    allowHostCommands: true,
    permissionGate: new PermissionGate(async () => true),
  });
  assert.equal(flow.state, "stale");
  assert.match(provider.requests[1]!.system!, /Development checks: stale/);
});

test("unreadable ignore rules fail closed instead of hashing an empty workspace", async (t) => {
  const { root, workspace } = await fixture(t);
  const flow = new VerificationWorkflow();
  await flow.run({ checks: [{ command: "echo passed" }] }, workspace);
  await fs.writeFile(path.join(root, ".gitignore"), Buffer.from([0xff]));
  await flow.refresh(workspace);
  assert.equal(flow.state, "stale");
  const result = await flow.run(
    { checks: [{ command: "echo must-not-run > marker" }] },
    workspace,
  );
  assert.equal(result.is_error, true);
  await assert.rejects(fs.stat(path.join(root, "marker")));
});

test("source changes during model response require a new response with stale verification", async (t) => {
  const { root, workspace } = await fixture(t);
  const file = path.join(root, "module.ts");
  await fs.writeFile(file, "before");
  const flow = new VerificationWorkflow();
  await flow.run({ checks: [{ command: "echo passed" }] }, workspace);
  let requests = 0;
  const result = await runAgent("Report verification", {
    workspace,
    maxTokens: 100,
    verification: flow,
    allowHostCommands: true,
    provider: {
      async createMessage(request) {
        if (++requests === 1) {
          await fs.writeFile(file, "after");
          return final("All checks passed");
        }
        assert.match(request.system!, /Development checks: stale/);
        return final("Code changed; checks require a retest");
      },
    },
  });
  assert.equal(requests, 2);
  assert.match(result.text, /require a retest/);
});
