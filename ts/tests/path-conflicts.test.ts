import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs/promises";
import path from "node:path";
import { fixture } from "./helpers.js";
import { CheckpointStore } from "../src/persistence/checkpoints.js";
import { createFile, replaceText } from "../src/tools/files/edit.js";
test("checkpoint restore refuses a parent redirected to another equal-content file", async (t) => {
  const { root, state, workspace } = await fixture(t);
  const checkpoints = new CheckpointStore(workspace, state);
  const result = await createFile(
    { path: "original/a", content: "agent" },
    workspace,
    checkpoints,
  );
  const id = /Checkpoint: ([0-9a-f]{32})/.exec(result.content)?.[1];
  assert.ok(id);
  await fs.mkdir(path.join(root, "other"));
  await fs.writeFile(path.join(root, "other", "a"), "agent");
  await fs.rename(path.join(root, "original"), path.join(root, "moved"));
  try {
    await fs.symlink(
      path.join(root, "other"),
      path.join(root, "original"),
      process.platform === "win32" ? "junction" : "dir",
    );
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === "EPERM") {
      t.skip("Link privilege unavailable.");
      return;
    }
    throw e;
  }
  await assert.rejects(checkpoints.restore(id), /conflict/);
  assert.equal(
    await fs.readFile(path.join(root, "other", "a"), "utf8"),
    "agent",
  );
  assert.equal(
    await fs.readFile(path.join(root, "moved", "a"), "utf8"),
    "agent",
  );
});
test("edit revalidates canonical path after preparing its checkpoint", async (t) => {
  const { root, state, workspace } = await fixture(t);
  await fs.mkdir(path.join(root, "original"));
  await fs.mkdir(path.join(root, "other"));
  await fs.writeFile(path.join(root, "original", "a"), "old");
  await fs.writeFile(path.join(root, "other", "a"), "old");
  class RedirectingStore extends CheckpointStore {
    override async save(
      file: string,
      before: Buffer | null,
      after: Buffer,
    ): Promise<string> {
      const id = await super.save(file, before, after);
      await fs.rename(path.join(root, "original"), path.join(root, "moved"));
      await fs.symlink(
        path.join(root, "other"),
        path.join(root, "original"),
        process.platform === "win32" ? "junction" : "dir",
      );
      return id;
    }
  }
  const result = await replaceText(
    { path: "original/a", old_text: "old", new_text: "new" },
    workspace,
    new RedirectingStore(workspace, state),
  );
  assert.equal(result.is_error, true);
  assert.match(result.content, /path changed/);
  assert.equal(await fs.readFile(path.join(root, "other", "a"), "utf8"), "old");
  assert.equal(await fs.readFile(path.join(root, "moved", "a"), "utf8"), "old");
});
