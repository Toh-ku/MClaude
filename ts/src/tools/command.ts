import * as fs from "node:fs/promises";
import { WorkspaceFS } from "../security/workspace.js";
import { MClaudeError, clip, ok, type ToolResult } from "../core/types.js";
import { runProcess } from "../runtime/process-runner.js";
import { stringInput } from "./definitions.js";
export async function runCommand(
  input: Record<string, unknown>,
  workspace: WorkspaceFS,
  signal?: AbortSignal,
): Promise<ToolResult> {
  const command = stringInput(input, "command");
  const cwd = await workspace.resolve(
    typeof input["path"] === "string" ? input["path"] : ".",
  );
  if (!(await fs.stat(cwd)).isDirectory())
    throw new MClaudeError("Command path is not a directory.");
  const timeout = input["timeout_seconds"] ?? 120;
  if (typeof timeout !== "number")
    throw new MClaudeError("Invalid command timeout.");
  const result = await runProcess({
    command,
    cwd,
    shell: true,
    timeoutSeconds: timeout,
    ...(signal ? { signal } : {}),
  });
  return {
    content: `${result.timedOut ? "Command timed out.\n" : ""}Exit code: ${result.exitCode ?? "unknown"}\nstdout:\n${result.stdout || "[none]"}\nstderr:\n${result.stderr || "[none]"}${result.truncated ? "\n[Command output truncated after 100000 characters.]" : ""}`,
    is_error: result.timedOut || result.exitCode !== 0,
  };
}
export async function gitReview(
  input: Record<string, unknown>,
  workspace: WorkspaceFS,
  signal?: AbortSignal,
): Promise<ToolResult> {
  const isDiff = input["mode"] === "diff";
  const commands = [
    ["status", "--short", "--untracked-files=all"],
    ...(isDiff
      ? [
          ["diff", "--cached", "--no-ext-diff", "--no-textconv", "--"],
          ["diff", "--no-ext-diff", "--no-textconv", "--"],
        ]
      : [
          ["diff", "--cached", "--check", "--no-ext-diff", "--"],
          ["diff", "--check", "--no-ext-diff", "--"],
        ]),
  ];
  const labels = isDiff
    ? ["Status", "Staged diff", "Unstaged diff"]
    : ["Status", "Staged whitespace check", "Unstaged whitespace check"];
  const sections: string[] = [];
  let error = false;
  for (let i = 0; i < commands.length; i++) {
    const result = await runProcess({
      command: "git",
      args: commands[i] ?? [],
      cwd: workspace.root,
      timeoutSeconds: 15,
      maxOutputChars: 20000,
      ...(signal ? { signal } : {}),
    });
    error ||= result.exitCode !== 0 || result.timedOut;
    sections.push(
      `${labels[i]} (exit ${result.exitCode}):\n${clip((result.stdout + result.stderr).trim() || "[none]", 20000)}${result.truncated ? "\n[Output truncated after 20000 characters.]" : ""}`,
    );
    if (i === 0 && error) break;
  }
  return { ...ok(sections.join("\n\n")), is_error: error };
}
