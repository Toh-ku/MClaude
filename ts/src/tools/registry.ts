import { WorkspaceFS } from "../security/workspace.js";
import { CheckpointStore } from "../persistence/checkpoints.js";
import { SessionStore } from "../persistence/session.js";
import { TaskBoard } from "../core/tasks.js";
import { VerificationWorkflow } from "../core/verification.js";
import { SkillCatalog } from "../integrations/skills.js";
import type { MCPRegistry } from "../integrations/mcp.js";
import {
  READ_ONLY_TOOLS,
  COMMAND_TOOLS,
  EDIT_TOOLS,
} from "../security/permissions.js";
import {
  MClaudeError,
  SessionError,
  TurnCancelled,
  safeError,
  checkCancelled,
  ok,
  fail,
  type ToolSpec,
  type ToolResult,
  type HistorySink,
} from "../core/types.js";
import { TOOL_DEFINITIONS, validateInput, stringInput } from "./definitions.js";
import { readFile, findFiles, searchText } from "./files/read.js";
import { createFile, replaceText, applyEdits } from "./files/edit.js";
import { gitReview, runCommand } from "./command.js";
export interface ToolContext {
  workspace: WorkspaceFS;
  checkpoints: CheckpointStore;
  sessions: SessionStore;
  tasks: TaskBoard;
  verification: VerificationWorkflow;
  skills: SkillCatalog;
  planning: boolean;
  allowHostCommands: boolean;
  signal: AbortSignal;
  maxFileChars: number;
  historySink?: HistorySink;
  mcp?: MCPRegistry;
  delegate?: (input: Record<string, unknown>) => Promise<ToolResult>;
  allowedTools?: ReadonlySet<string>;
}
export class ToolRegistry {
  readonly definitions: ToolSpec[];
  constructor(private readonly context: ToolContext) {
    const all = [
      ...TOOL_DEFINITIONS,
      ...(context.planning ? [] : (context.mcp?.definitions ?? [])),
    ];
    this.definitions = all.filter(
      (d) =>
        (!COMMAND_TOOLS.has(d.name) || context.allowHostCommands) &&
        (d.name !== "delegate_readonly" || !!context.delegate) &&
        (!context.planning || READ_ONLY_TOOLS.has(d.name)) &&
        (!context.allowedTools || context.allowedTools.has(d.name)),
    );
  }
  isExternal(name: string): boolean {
    return this.context.mcp?.routes.has(name) ?? false;
  }
  async execute(name: string, argumentsValue: unknown): Promise<ToolResult> {
    const c = this.context;
    checkCancelled(c.signal);
    if (!this.definitions.some((d) => d.name === name))
      return fail("Tool is outside this agent's available set.");
    try {
      if (this.isExternal(name)) {
        if (c.planning)
          throw new MClaudeError("MCP is blocked in planning mode.");
        return await c.mcp!.execute(
          name,
          argumentsValue as Record<string, unknown>,
          c.signal,
        );
      }
      const input = validateInput(name, argumentsValue);
      if (EDIT_TOOLS.has(name)) c.verification.markEdited();
      switch (name) {
        case "read_file":
          return await readFile(input, c.workspace, c.signal, c.maxFileChars);
        case "find_files":
          return await findFiles(input, c.workspace, c.signal);
        case "search_text":
          return await searchText(input, c.workspace, c.signal);
        case "search_sessions": {
          const matches = await c.sessions.search(
            c.workspace.root,
            stringInput(input, "query"),
          );
          return ok(
            matches.length
              ? matches
                  .map((m) => `${m.id} ${m.timestamp} ${m.role}: ${m.excerpt}`)
                  .join("\n")
              : "No matching saved conversation text.",
          );
        }
        case "create_file":
          return await createFile(input, c.workspace, c.checkpoints, c.signal);
        case "replace_text":
          return await replaceText(input, c.workspace, c.checkpoints, c.signal);
        case "apply_edits":
          return await applyEdits(input, c.workspace, c.checkpoints, c.signal);
        case "git_review":
          return await gitReview(input, c.workspace, c.signal);
        case "run_command":
          return await runCommand(input, c.workspace, c.signal);
        case "list_edit_checkpoints": {
          const items = (await c.checkpoints.list()).slice(0, 50);
          return ok(
            items.length
              ? items
                  .map(
                    (i) =>
                      `${i.id}  ${i.path}  ${i.restored ? "restored" : "available"}  ${i.created_at}`,
                  )
                  .join("\n")
              : "No edit checkpoints exist for this workspace.",
          );
        }
        case "restore_edit_checkpoint":
          return ok(
            await c.checkpoints.restore(stringInput(input, "checkpoint_id")),
          );
        case "list_tasks":
          return ok(c.tasks.render());
        case "update_tasks":
          await c.tasks.update(input["tasks"], c.historySink);
          return ok(c.tasks.render());
        case "load_skill":
          return await c.skills.load(stringInput(input, "name"), c.signal);
        case "run_checks":
          return await c.verification.run(input, c.workspace, false, c.signal);
        case "retest_checks":
          return await c.verification.run(input, c.workspace, true, c.signal);
        case "delegate_readonly":
          return await c.delegate!(input);
        default:
          return fail(`Unknown tool: ${name}`);
      }
    } catch (e) {
      if (e instanceof TurnCancelled || e instanceof SessionError) throw e;
      return fail(safeError(e));
    }
  }
}
