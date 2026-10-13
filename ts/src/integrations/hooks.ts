import { readBounded, utf8 } from "../security/workspace.js";
import {
  MClaudeError,
  TurnCancelled,
  isObject,
  safeError,
  fail,
  type ToolResult,
} from "../core/types.js";
import { runProcess } from "../runtime/process-runner.js";
interface Hook {
  command: string[];
  tools: string[];
  timeout: number;
}
function parseGroup(value: unknown): Hook[] {
  if (!Array.isArray(value) || value.length > 20)
    throw new MClaudeError("Each hook phase supports at most 20 hooks.");
  return (value as unknown[]).map((entry) => {
    if (
      !isObject(entry) ||
      Object.keys(entry).some(
        (k) => !["command", "tools", "timeout"].includes(k),
      )
    )
      throw new MClaudeError("Invalid hook entry.");
    const command = entry["command"],
      tools = entry["tools"] ?? ["*"],
      timeout = entry["timeout"] ?? 10;
    if (
      !Array.isArray(command) ||
      !command.length ||
      !command.every((v: unknown) => typeof v === "string" && !!v) ||
      !Array.isArray(tools) ||
      !tools.length ||
      !tools.every((v: unknown) => typeof v === "string" && !!v) ||
      typeof timeout !== "number" ||
      !Number.isFinite(timeout) ||
      timeout <= 0 ||
      timeout > 60
    )
      throw new MClaudeError("Invalid hook command, tools or timeout.");
    return { command: command as string[], tools: tools as string[], timeout };
  });
}
export class HookRunner {
  constructor(
    readonly before: Hook[] = [],
    readonly after: Hook[] = [],
  ) {}
  static async fromFile(file: string): Promise<HookRunner> {
    let raw: unknown;
    try {
      raw = JSON.parse(utf8(await readBounded(file, 400004)));
    } catch {
      throw new MClaudeError("Cannot load hook configuration.");
    }
    if (
      !isObject(raw) ||
      Object.keys(raw).some((k) => !["before_tool", "after_tool"].includes(k))
    )
      throw new MClaudeError("Expected before_tool and/or after_tool arrays.");
    return new HookRunner(
      parseGroup(raw["before_tool"] ?? []),
      parseGroup(raw["after_tool"] ?? []),
    );
  }
  private async invoke(
    hook: Hook,
    payload: Record<string, unknown>,
    workspace: string,
    signal: AbortSignal,
  ): Promise<Record<string, unknown>> {
    const result = await runProcess({
      command: hook.command[0]!,
      args: hook.command.slice(1),
      cwd: workspace,
      timeoutSeconds: hook.timeout,
      maxOutputChars: 16384,
      maxOutputBytes: 16384,
      input: JSON.stringify(payload),
      signal,
    });
    if (result.timedOut || result.exitCode !== 0)
      throw new MClaudeError("Hook timed out or exited unsuccessfully.");
    let raw: unknown;
    try {
      raw = JSON.parse(result.stdout || "{}");
    } catch {
      throw new MClaudeError("Invalid hook JSON response.");
    }
    if (
      !isObject(raw) ||
      Object.keys(raw).some(
        (k) => !["block", "reason", "append"].includes(k),
      ) ||
      typeof (raw["block"] ?? false) !== "boolean" ||
      typeof (raw["reason"] ?? "") !== "string" ||
      typeof (raw["append"] ?? "") !== "string"
    )
      throw new MClaudeError("Invalid hook response fields.");
    return raw;
  }
  async execute(
    name: string,
    input: unknown,
    workspace: string,
    execute: () => Promise<ToolResult>,
    signal: AbortSignal,
  ): Promise<ToolResult> {
    const payload = { tool_name: name, tool_input: input };
    try {
      for (const hook of this.before) {
        if (!hook.tools.includes("*") && !hook.tools.includes(name)) continue;
        const result = await this.invoke(
          hook,
          { ...payload, event: "before_tool" },
          workspace,
          signal,
        );
        if (result["block"])
          return fail(
            "Hook blocked tool: " +
              String(result["reason"] ?? "No reason supplied."),
          );
      }
    } catch (e) {
      if (e instanceof TurnCancelled) throw e;
      return fail("Before-tool hook failed: " + safeError(e));
    }
    let result = await execute();
    try {
      for (const hook of this.after) {
        if (!hook.tools.includes("*") && !hook.tools.includes(name)) continue;
        const response = await this.invoke(
          hook,
          { ...payload, event: "after_tool", result },
          workspace,
          signal,
        );
        let append = String(response["append"] ?? "");
        if (response["block"])
          append +=
            "\nAfter-tool hook flagged result: " +
            String(response["reason"] ?? "");
        result = {
          content: result.content + (append ? "\n[Hook]\n" + append : ""),
          is_error: result.is_error || response["block"] === true,
        };
      }
    } catch (e) {
      if (e instanceof TurnCancelled) throw e;
      return fail(result.content + "\nAfter-tool hook failed: " + safeError(e));
    }
    return result;
  }
}
