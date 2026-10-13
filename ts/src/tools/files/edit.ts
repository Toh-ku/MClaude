import * as fs from "node:fs/promises";
import { createTwoFilesPatch } from "diff";
import {
  WorkspaceFS,
  atomicWrite,
  readBounded,
  utf8,
} from "../../security/workspace.js";
import { CheckpointStore } from "../../persistence/checkpoints.js";
import {
  MClaudeError,
  TurnCancelled,
  errorCode,
  isObject,
  clip,
  codePoints,
  checkCancelled,
  safeError,
  ok,
  fail,
  type ToolResult,
} from "../../core/types.js";
import { stringInput } from "../definitions.js";
interface EditPlan {
  file: string;
  before: Buffer | null;
  after: Buffer;
  mode: number | undefined;
  checkpoint?: string;
}
const EDIT_LIMIT = 1000000;
function diff(workspace: WorkspaceFS, plan: EditPlan): string {
  const relative = workspace.display(plan.file);
  const text = createTwoFilesPatch(
    plan.before === null ? "/dev/null" : `a/${relative}`,
    `b/${relative}`,
    plan.before === null ? "" : utf8(plan.before),
    utf8(plan.after),
    "",
    "",
    { context: 3 },
  ).replace(/^={3,}\n/, "");
  return codePoints(text) > 20000
    ? clip(text, 20000) + "\n[Diff truncated after 20000 characters.]"
    : text;
}
async function prepare(
  input: Record<string, unknown>,
  kind: "create" | "replace",
  workspace: WorkspaceFS,
): Promise<EditPlan> {
  const file = await workspace.resolve(stringInput(input, "path"));
  if (kind === "create") {
    try {
      await fs.lstat(file);
      throw new MClaudeError("Path already exists; refusing to overwrite it.");
    } catch (e) {
      if (errorCode(e) !== "ENOENT") throw e;
    }
    const after = Buffer.from(stringInput(input, "content", true));
    if (after.length > EDIT_LIMIT)
      throw new MClaudeError("File exceeds the edit limit.");
    return { file, before: null, after, mode: undefined };
  }
  const old = stringInput(input, "old_text"),
    next = stringInput(input, "new_text", true);
  if (old === next)
    throw new MClaudeError("old_text and new_text must differ.");
  const before = await readBounded(file, EDIT_LIMIT);
  const original = utf8(before);
  const first = original.indexOf(old);
  if (first < 0)
    throw new MClaudeError(
      "Expected text was not found; the file may have changed.",
    );
  if (original.indexOf(old, first + old.length) >= 0)
    throw new MClaudeError("Expected text must match exactly once.");
  const after = Buffer.from(
    original.slice(0, first) + next + original.slice(first + old.length),
  );
  if (after.length > EDIT_LIMIT)
    throw new MClaudeError("Updated file exceeds the edit limit.");
  return { file, before, after, mode: (await fs.stat(file)).mode & 0o7777 };
}
export async function applyEdits(
  input: Record<string, unknown>,
  workspace: WorkspaceFS,
  checkpoints: CheckpointStore,
  signal?: AbortSignal,
): Promise<ToolResult> {
  if (
    !Array.isArray(input["edits"]) ||
    input["edits"].length < 1 ||
    input["edits"].length > 20
  )
    throw new MClaudeError("apply_edits requires 1-20 edits.");
  const planned: EditPlan[] = [];
  const seen = new Set<string>();
  for (const value of input["edits"] as unknown[]) {
    checkCancelled(signal);
    if (
      !isObject(value) ||
      !["create", "replace"].includes(String(value["kind"]))
    )
      throw new MClaudeError("Invalid edit kind.");
    const kind = value["kind"] as "create" | "replace";
    const keys =
      kind === "create"
        ? ["kind", "path", "content"]
        : ["kind", "path", "old_text", "new_text"];
    if (Object.keys(value).sort().join(",") !== keys.sort().join(","))
      throw new MClaudeError("Invalid edit fields.");
    const plan = await prepare(value, kind, workspace);
    const identity =
      process.platform === "win32" ? plan.file.toLowerCase() : plan.file;
    if (seen.has(identity))
      throw new MClaudeError("Edit repeats a file in this batch.");
    seen.add(identity);
    planned.push(plan);
  }
  try {
    for (const plan of planned) {
      checkCancelled(signal);
      plan.checkpoint = await checkpoints.save(
        plan.file,
        plan.before,
        plan.after,
      );
    }
  } catch (e) {
    for (const plan of planned)
      if (plan.checkpoint) await checkpoints.discard(plan.checkpoint);
    throw e;
  }
  const applied: EditPlan[] = [];
  try {
    for (const plan of planned) {
      checkCancelled(signal);
      await workspace.prepareParent(plan.file);
      if ((await workspace.resolve(plan.file)) !== plan.file)
        throw new MClaudeError(
          "File path changed during the edit; refusing to overwrite it.",
        );
      if (plan.before === null) {
        const handle = await fs.open(plan.file, "wx", 0o666);
        applied.push(plan);
        try {
          await handle.writeFile(plan.after);
          await handle.sync();
        } finally {
          await handle.close();
        }
      } else {
        await atomicWrite(plan.file, plan.after, {
          expected: plan.before,
          ...(plan.mode !== undefined ? { mode: plan.mode } : {}),
        });
        applied.push(plan);
      }
    }
  } catch (e) {
    let rollbackFailed = false;
    for (const plan of applied.reverse()) {
      try {
        if ((await workspace.resolve(plan.file)) !== plan.file)
          throw new MClaudeError("File path changed during rollback.");
        const current = await fs.readFile(plan.file);
        if (!current.equals(plan.after)) {
          rollbackFailed = true;
          continue;
        }
        if (plan.before === null) await fs.unlink(plan.file);
        else
          await atomicWrite(plan.file, plan.before, {
            expected: plan.after,
            ...(plan.mode !== undefined ? { mode: plan.mode } : {}),
          });
      } catch {
        rollbackFailed = true;
      }
    }
    if (!rollbackFailed)
      for (const plan of planned)
        if (plan.checkpoint) await checkpoints.discard(plan.checkpoint);
    const detail = rollbackFailed
      ? "Some files may need checkpoint recovery."
      : "Changes were rolled back.";
    if (e instanceof TurnCancelled)
      throw new TurnCancelled(`Batch edit cancelled. ${detail}`);
    return fail(`Batch edit failed: ${safeError(e)} ${detail}`);
  }
  return ok(
    planned
      .map(
        (plan) =>
          `${plan.before === null ? "Created" : "Updated"} ${workspace.display(plan.file)}\nCheckpoint: ${plan.checkpoint}\n\n${diff(workspace, plan)}`,
      )
      .join("\n\n"),
  );
}
export async function createFile(
  input: Record<string, unknown>,
  workspace: WorkspaceFS,
  checkpoints: CheckpointStore,
  signal?: AbortSignal,
): Promise<ToolResult> {
  return applyEdits(
    { edits: [{ kind: "create", ...input }] },
    workspace,
    checkpoints,
    signal,
  );
}
export async function replaceText(
  input: Record<string, unknown>,
  workspace: WorkspaceFS,
  checkpoints: CheckpointStore,
  signal?: AbortSignal,
): Promise<ToolResult> {
  return applyEdits(
    { edits: [{ kind: "replace", ...input }] },
    workspace,
    checkpoints,
    signal,
  );
}
