import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs/promises";
import path from "node:path";
import { fixture } from "./helpers.js";
import { searchText } from "../src/tools/files/read.js";
import { TurnCancelled } from "../src/core/types.js";
test("catastrophic regex evaluation has a bounded time and remains cancellable", async (t) => {
  const { root, workspace } = await fixture(t);
  await fs.writeFile(path.join(root, "long"), "a".repeat(100000) + "!");
  await assert.rejects(
    searchText({ query: "(a+)+$", is_regex: true }, workspace),
    /exceeded 2 seconds/,
  );
  const controller = new AbortController();
  const pending = searchText(
    { query: "(a+)+$", is_regex: true },
    workspace,
    controller.signal,
  );
  const timer = setTimeout(() => controller.abort(), 100);
  try {
    await assert.rejects(pending, TurnCancelled);
  } finally {
    clearTimeout(timer);
  }
});
