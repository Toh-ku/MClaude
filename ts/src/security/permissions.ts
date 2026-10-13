import { checkCancelled, isObject } from "../core/types.js";
export interface PermissionRequest {
  tool_name: string;
  tool_input: unknown;
  external?: boolean;
}
export interface PermissionDecision {
  action: "allow" | "ask" | "deny";
  reason: string;
}
export type PermissionPolicy = (
  request: PermissionRequest,
) => PermissionDecision | Promise<PermissionDecision>;
export type PermissionPrompt = (
  request: PermissionRequest,
  reason: string,
  signal?: AbortSignal,
) => Promise<boolean>;
export const READ_ONLY_TOOLS = new Set([
  "read_file",
  "find_files",
  "search_text",
  "search_sessions",
  "list_edit_checkpoints",
  "list_tasks",
  "load_skill",
  "git_review",
]);
export const EDIT_TOOLS = new Set([
  "create_file",
  "replace_text",
  "apply_edits",
  "restore_edit_checkpoint",
]);
export const COMMAND_TOOLS = new Set([
  "run_command",
  "run_checks",
  "retest_checks",
]);
export function defaultPolicy(request: PermissionRequest): PermissionDecision {
  if (request.external)
    return {
      action: "ask",
      reason: "External MCP tool; side effects are not trusted.",
    };
  if (
    READ_ONLY_TOOLS.has(request.tool_name) ||
    ["update_tasks", "delegate_readonly"].includes(request.tool_name)
  )
    return { action: "allow", reason: "Known workspace tool." };
  if (EDIT_TOOLS.has(request.tool_name))
    return { action: "ask", reason: "This tool modifies workspace files." };
  if (COMMAND_TOOLS.has(request.tool_name))
    return { action: "ask", reason: "This tool executes a shell command." };
  return {
    action: "deny",
    reason: "Unknown tool is not included in the permission policy.",
  };
}
export class PermissionGate {
  constructor(
    private readonly prompt?: PermissionPrompt,
    private readonly policy: PermissionPolicy = defaultPolicy,
  ) {}
  async check(
    request: PermissionRequest,
    signal?: AbortSignal,
  ): Promise<PermissionDecision> {
    checkCancelled(signal);
    try {
      const decision: unknown = await this.policy(request);
      if (
        !isObject(decision) ||
        typeof decision["reason"] !== "string" ||
        !["allow", "ask", "deny"].includes(String(decision["action"]))
      )
        return {
          action: "deny",
          reason: "Permission policy returned an invalid decision.",
        };
      const action = decision["action"];
      const reason = decision["reason"];
      if (action === "allow" || action === "deny") return { action, reason };
      if (!this.prompt)
        return {
          action: "deny",
          reason: `${reason} No permission prompt is available.`,
        };
      const approved: unknown = await this.prompt(request, reason, signal);
      checkCancelled(signal);
      return {
        action: approved === true ? "allow" : "deny",
        reason: `${reason} ${approved === true ? "Approved" : "Denied"} by the user.`,
      };
    } catch {
      checkCancelled(signal);
      return { action: "deny", reason: "Permission evaluation failed." };
    }
  }
}
