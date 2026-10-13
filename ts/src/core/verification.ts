import { WorkspaceFS } from "../security/workspace.js";
import {
  MClaudeError,
  TurnCancelled,
  isObject,
  ok,
  fail,
  type ToolResult,
} from "./types.js";
import { runCommand } from "../tools/command.js";
import { workspaceVersion } from "./workspace-version.js";
export class VerificationWorkflow {
  checks: Record<string, unknown>[] = [];
  attempts = 0;
  state: "not_run" | "passed" | "failed" | "stale" = "not_run";
  version: string | undefined;
  private workspaceRoot: string | undefined;
  async refresh(workspace: WorkspaceFS, signal?: AbortSignal): Promise<void> {
    if (this.state !== "passed") return;
    try {
      if (
        workspace.root !== this.workspaceRoot ||
        this.version !== (await workspaceVersion(workspace, signal))
      )
        this.state = "stale";
    } catch (e) {
      if (e instanceof TurnCancelled) throw e;
      this.state = "stale";
    }
  }
  markEdited(): void {
    if (this.state === "passed") this.state = "stale";
  }
  summary(): string {
    return this.state === "not_run"
      ? "No development checks have been run in this conversation."
      : `Development checks: ${this.state} after ${this.attempts} run(s). Workspace version: ${this.version ?? "unavailable"}. Commands: ${this.checks.map((c) => c["command"]).join("; ")}. If failed or stale, fix and call retest_checks before reporting verification as passed.`;
  }
  async run(
    input: Record<string, unknown>,
    workspace: WorkspaceFS,
    retest = false,
    signal?: AbortSignal,
  ): Promise<ToolResult> {
    if (retest && !this.checks.length)
      return fail("No prior checks are available to retest.");
    const checks = retest ? structuredClone(this.checks) : input["checks"];
    if (
      !Array.isArray(checks) ||
      checks.length < 1 ||
      checks.length > 5 ||
      !checks.every(
        (v: unknown) =>
          isObject(v) &&
          typeof v["command"] === "string" &&
          v["command"].trim(),
      )
    )
      throw new MClaudeError("run_checks requires 1-5 valid checks.");
    this.checks = structuredClone(checks as Record<string, unknown>[]);
    this.attempts++;
    this.state = "failed";
    this.version = undefined;
    this.workspaceRoot = workspace.root;
    let before: string;
    try {
      before = await workspaceVersion(workspace, signal);
    } catch (e) {
      if (e instanceof TurnCancelled) throw e;
      return fail("Cannot establish workspace version; checks were not run.");
    }
    const outputs: string[] = [];
    for (const [index, check] of this.checks.entries()) {
      const result = await runCommand(check, workspace, signal);
      outputs.push(
        `Check ${index + 1}/${this.checks.length}: ${check["command"]}\n${result.content}`,
      );
      if (result.is_error)
        return fail(
          `Verification attempt ${this.attempts} failed. Inspect the output, fix the issue, then call retest_checks.\n\n${outputs.join("\n\n")}`,
        );
    }
    try {
      this.version = await workspaceVersion(workspace, signal);
    } catch (e) {
      if (e instanceof TurnCancelled) throw e;
      this.state = "stale";
      return fail(
        `Checks exited successfully, but workspace version could not be established. Retest required.\n\n${outputs.join("\n\n")}`,
      );
    }
    if (this.version !== before) {
      this.state = "stale";
      return fail(
        `Checks exited successfully, but workspace changed during verification. Retest required.\n\n${outputs.join("\n\n")}`,
      );
    }
    this.state = "passed";
    return ok(
      `Verification attempt ${this.attempts} passed. Workspace version: ${this.version}.\n\n${outputs.join("\n\n")}`,
    );
  }
}
