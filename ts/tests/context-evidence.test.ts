import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { compactHistory, ContextBudget } from "../src/core/context.js";
import {
  collectToolEvidence,
  renderEvidence,
  EVIDENCE_PREFIX,
} from "../src/core/evidence.js";
import { validateHistory } from "../src/persistence/validation.js";
import { codePoints, type Message } from "../src/core/types.js";
import { call } from "./helpers.js";

const version = "a".repeat(64);

test("shared compacted evidence fixture remains valid and survives recompression", async () => {
  const fixture = JSON.parse(
    await readFile(
      new URL(
        "../../../contracts/fixtures/compacted-evidence.json",
        import.meta.url,
      ),
      "utf8",
    ),
  ) as { history: Message[]; evidence: unknown[] };
  validateHistory(fixture.history);
  assert.deepEqual(collectToolEvidence(fixture.history), fixture.evidence);
  assert.deepEqual(
    collectToolEvidence(forceCompaction(fixture.history)),
    fixture.evidence,
  );
});
function exchange(
  id: string,
  tool: string,
  input: Record<string, unknown>,
  content: string,
  is_error = false,
): Message[] {
  return [
    { role: "assistant", content: [call(tool, input, id)] },
    {
      role: "user",
      content: [{ type: "tool_result", tool_use_id: id, content, is_error }],
    },
  ];
}
function forceCompaction(
  history: Message[],
  budget = new ContextBudget(1800, 100),
): Message[] {
  const compacted = compactHistory(
    [
      ...history,
      {
        role: "assistant",
        content: [{ type: "text", text: "verbose update ".repeat(1500) }],
      },
      { role: "user", content: "Continue; preserve Windows support" },
    ],
    budget,
  );
  assert.ok(compacted);
  budget.ensureFits(compacted);
  validateHistory(compacted);
  return compacted;
}

test("compaction preserves successful file, search, edit, check and delegated evidence with attribution", () => {
  const history: Message[] = [
    { role: "user", content: "Fix cache races while keeping the API stable" },
    ...exchange(
      "read",
      "read_file",
      { path: "src/cache.ts" },
      "export function lookup() {}",
    ),
    ...exchange(
      "search",
      "search_text",
      { query: "lookup", path: "src" },
      "src/cache.ts:37:lookup() acquires the lock",
    ),
    ...exchange(
      "edit",
      "apply_edits",
      { edits: [{ path: "src/cache.ts" }, { path: "src/lock.ts" }] },
      "Updated src/cache.ts\nCheckpoint: checkpoint-123\n+ acquireLock()",
    ),
    ...exchange(
      "checks",
      "run_checks",
      { checks: [{ command: "npm test" }] },
      `Verification attempt 1 passed. Workspace version: ${version}.\n12 tests passed`,
    ),
    ...exchange(
      "delegate",
      "delegate_readonly",
      { prompt: "Investigate races" },
      "src/cache.ts:37 needs a lock; src/lock.ts provides it",
    ),
    ...exchange(
      "error",
      "read_file",
      { path: "missing.ts" },
      "ENOENT: missing.ts",
      true,
    ),
  ];
  const compacted = forceCompaction(history);
  const evidence = collectToolEvidence(compacted);
  assert.equal(evidence.length, 6);
  assert.deepEqual(evidence.find((e) => e.call_id === "read")!.targets, [
    "src/cache.ts",
  ]);
  assert.match(
    evidence.find((e) => e.call_id === "search")!.excerpt,
    /src\/cache.ts:37/,
  );
  assert.deepEqual(evidence.find((e) => e.call_id === "edit")!.targets, [
    "src/cache.ts",
    "src/lock.ts",
  ]);
  assert.match(
    evidence.find((e) => e.call_id === "edit")!.excerpt,
    /checkpoint-123/,
  );
  assert.equal(
    evidence.find((e) => e.call_id === "checks")!.workspace_version,
    version,
  );
  assert.match(
    evidence.find((e) => e.call_id === "checks")!.excerpt,
    /npm test/,
  );
  assert.match(
    evidence.find((e) => e.call_id === "delegate")!.excerpt,
    /provides it/,
  );
  assert.equal(
    evidence.find((e) => e.call_id === "error")!.outcome,
    "error_or_incomplete",
  );
});

test("evidence survives repeated compaction without nesting or duplication", () => {
  let history = forceCompaction([
    { role: "user", content: "Keep the public API stable" },
    ...exchange(
      "check",
      "run_checks",
      { checks: [{ command: "npm test" }] },
      `Passed. Workspace version: ${version}.\nTests passed`,
    ),
    ...exchange(
      "search",
      "search_text",
      { query: "race", path: "src" },
      "src/cache.ts:37: race fixed",
    ),
  ]);
  for (let i = 0; i < 5; i++) {
    history = forceCompaction(history);
    const evidence = collectToolEvidence(history);
    assert.equal(evidence.length, 2);
    assert.equal(
      evidence.find((e) => e.call_id === "check")!.workspace_version,
      version,
    );
    assert.match(
      evidence.find((e) => e.call_id === "search")!.excerpt,
      /src\/cache.ts:37/,
    );
    assert.doesNotMatch(
      JSON.stringify(history),
      /Earlier context: Tool evidence/,
    );
    assert.match(JSON.stringify(history), /public API stable/);
    assert.match(JSON.stringify(history), /preserve Windows support/);
  }
});

test("failed retests replace older passed evidence even when reads dominate the history", () => {
  const history: Message[] = [
    { role: "user", content: "Fix failure" },
    ...exchange(
      "passed",
      "run_checks",
      { checks: [{ command: "npm test" }] },
      `Passed. Workspace version: ${version}.`,
    ),
    ...Array.from({ length: 20 }, (_, i) =>
      exchange(`read-${i}`, "read_file", { path: `src/${i}.ts` }, "source"),
    ).flat(),
    ...exchange(
      "failed",
      "retest_checks",
      {},
      "Verification attempt 2 failed. AssertionError: missing lock",
      true,
    ),
  ];
  const evidence = collectToolEvidence(forceCompaction(history));
  assert.ok(
    evidence.some(
      (e) => e.call_id === "failed" && e.outcome === "error_or_incomplete",
    ),
  );
  assert.ok(!evidence.some((e) => e.call_id === "passed"));
});

test("long evidence keeps head and tail, marks excerpts and respects Unicode summary limits", () => {
  const records = collectToolEvidence(
    exchange(
      "read",
      "read_file",
      { path: "中文.ts" },
      "HEAD_IMPORT\n" + "🙂中".repeat(2000) + "\nTAIL_EXPORT",
    ),
  );
  assert.equal(records[0]!.truncated, true);
  assert.match(records[0]!.excerpt, /HEAD_IMPORT/);
  assert.match(records[0]!.excerpt, /TAIL_EXPORT/);
  for (const limit of [200, 400, 800, 2000]) {
    const lines = renderEvidence(records, limit);
    assert.ok(codePoints(lines.join("\n")) <= limit);
    for (const line of lines.filter((l) => l.startsWith(EVIDENCE_PREFIX))) {
      const record = JSON.parse(line.slice(EVIDENCE_PREFIX.length)) as {
        truncated: boolean;
      };
      assert.equal(record.truncated, true);
    }
  }
});

test("compaction refuses to fit by silently erasing all tool evidence", () => {
  const history: Message[] = [
    { role: "user", content: "Goal" },
    ...exchange(
      "check",
      "run_checks",
      {},
      `Passed. Workspace version: ${version}.\n` + "output".repeat(1000),
    ),
    { role: "user", content: "Continue" },
  ];
  assert.equal(compactHistory(history, new ContextBudget(120, 20)), undefined);
});

test("malformed legacy evidence entries do not crash compaction", () => {
  const history: Message[] = [
    {
      role: "assistant",
      content: [
        {
          type: "text",
          text: "Earlier conversation summary (generated locally):\nEarlier goal: Goal\nTool evidence: {invalid-json",
        },
      ],
    },
  ];
  assert.deepEqual(collectToolEvidence(history), []);
  assert.match(JSON.stringify(forceCompaction(history)), /Goal/);
});
