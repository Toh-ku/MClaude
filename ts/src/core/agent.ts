import { WorkspaceFS } from "../security/workspace.js";
import {
  PermissionGate,
  READ_ONLY_TOOLS,
  COMMAND_TOOLS,
} from "../security/permissions.js";
import { CheckpointStore } from "../persistence/checkpoints.js";
import { SessionStore } from "../persistence/session.js";
import { SkillCatalog } from "../integrations/skills.js";
import {
  loadInstructions,
  type ProjectInstructions,
} from "../integrations/instructions.js";
import type { HookRunner } from "../integrations/hooks.js";
import type { MCPRegistry } from "../integrations/mcp.js";
import { ToolRegistry } from "../tools/registry.js";
import { ToolScheduler } from "../runtime/scheduler.js";
import { validateInput, stringInput } from "../tools/definitions.js";
import { TaskBoard } from "./tasks.js";
import { VerificationWorkflow } from "./verification.js";
import { ContextBudget, compactHistory } from "./context.js";
import {
  ModelError,
  ContextBudgetExceeded,
  TurnCancelled,
  checkCancelled,
  fail,
  clip,
  codePoints,
  type AgentResponse,
  type ModelProvider,
  type Message,
  type HistorySink,
  type StatusSink,
  type ToolResult,
  type ToolUseBlock,
  type ToolResultBlock,
} from "./types.js";
export interface AgentOptions {
  provider: ModelProvider;
  workspace: WorkspaceFS;
  maxTokens: number;
  signal?: AbortSignal;
  history?: Message[];
  maxIterations?: number;
  maxFileChars?: number;
  contextBudget?: number;
  permissionGate?: PermissionGate;
  checkpoints?: CheckpointStore;
  sessions?: SessionStore;
  tasks?: TaskBoard;
  verification?: VerificationWorkflow;
  planning?: boolean;
  hooks?: HookRunner;
  mcp?: MCPRegistry;
  subagentBudget?: number;
  subagentDepth?: number;
  readWorkers?: number;
  allowHostCommands?: boolean;
  allowedTools?: ReadonlySet<string>;
  instructions?: ProjectInstructions;
  onText?: (text: string) => void;
  onHistoryEvent?: HistorySink;
  onStatusEvent?: StatusSink;
}
export async function runAgent(
  prompt: string,
  options: AgentOptions,
): Promise<AgentResponse> {
  if (!prompt.trim()) throw new ModelError("The prompt must not be empty.");
  const maxIterations = options.maxIterations ?? 8,
    maxFileChars = options.maxFileChars ?? 100000,
    subagentBudget = options.subagentBudget ?? 8,
    readWorkers = options.readWorkers ?? 4;
  if (
    !Number.isSafeInteger(maxIterations) ||
    maxIterations <= 0 ||
    !Number.isSafeInteger(maxFileChars) ||
    maxFileChars <= 0
  )
    throw new ModelError(
      "Iteration and file limits must be positive integers.",
    );
  if (
    !Number.isSafeInteger(subagentBudget) ||
    subagentBudget < 0 ||
    subagentBudget > 32
  )
    throw new ModelError("subagent_budget must be between 0 and 32.");
  const scheduler = new ToolScheduler(
    readWorkers,
    !options.hooks || !!options.planning,
  );
  const signal = options.signal ?? new AbortController().signal;
  checkCancelled(signal);
  const history = options.history ?? [],
    tasks = options.tasks ?? new TaskBoard(),
    verification = options.verification ?? new VerificationWorkflow();
  const skills = await SkillCatalog.discover(options.workspace);
  const instructions =
    options.instructions ?? (await loadInstructions(options.workspace));
  if (options.mcp && !options.planning) await options.mcp.connect(signal);
  let remaining = subagentBudget;
  const delegate = async (
    input: Record<string, unknown>,
  ): Promise<ToolResult> => {
    const validated = validateInput("delegate_readonly", input);
    const prompt = stringInput(validated, "prompt");
    const iterations = Number(validated["max_iterations"] ?? 4);
    if (codePoints(prompt) > 8000 || (options.subagentDepth ?? 0) >= 1)
      return fail("Subagent prompt or recursion limit exceeded.");
    if (remaining <= 0)
      return fail("Subagent request budget exhausted for this turn.");
    const provider: ModelProvider = {
      createMessage: async (request, signal) => {
        if (remaining <= 0)
          throw new ModelError(
            "Subagent request budget exhausted for this turn.",
          );
        remaining--;
        return options.provider.createMessage(request, signal);
      },
    };
    try {
      const result = await runAgent(prompt, {
        provider,
        workspace: options.workspace,
        maxTokens: options.maxTokens,
        signal,
        history: [],
        maxIterations: Math.min(iterations, remaining),
        maxFileChars,
        contextBudget: options.contextBudget ?? 100000,
        planning: true,
        subagentBudget: 0,
        subagentDepth: 1,
        readWorkers,
        allowedTools: new Set(["read_file", "find_files", "search_text"]),
      });
      return {
        content:
          codePoints(result.text) > 16000
            ? clip(result.text, 16000) + "\n[Subagent report truncated]"
            : result.text,
        is_error: result.truncated || codePoints(result.text) > 16000,
      };
    } catch (e) {
      if (e instanceof TurnCancelled) throw e;
      if (e instanceof ModelError) return fail("Subagent failed: " + e.message);
      throw e;
    }
  };
  const registry = new ToolRegistry({
    workspace: options.workspace,
    checkpoints: options.checkpoints ?? new CheckpointStore(options.workspace),
    sessions: options.sessions ?? new SessionStore(),
    tasks,
    verification,
    skills,
    planning: options.planning ?? false,
    allowHostCommands: options.allowHostCommands ?? false,
    signal,
    maxFileChars,
    ...(options.onHistoryEvent ? { historySink: options.onHistoryEvent } : {}),
    ...(options.mcp ? { mcp: options.mcp } : {}),
    ...(!options.planning &&
    (options.subagentDepth ?? 0) === 0 &&
    subagentBudget > 0
      ? { delegate }
      : {}),
    ...(options.allowedTools ? { allowedTools: options.allowedTools } : {}),
  });
  const gate = options.permissionGate ?? new PermissionGate();
  const budget = new ContextBudget(
    options.contextBudget ?? 100000,
    options.maxTokens,
  );
  let base = instructions.system;
  if (skills.skills.size)
    base += "\nAvailable skills (load on demand):\n" + skills.summary();
  if (options.planning)
    base +=
      "\nPlanning mode: analyze with read-only tools and return a plan. File modifications, commands and task updates are prohibited.";
  const status = (event: string, data: Record<string, unknown>): void => {
    try {
      options.onStatusEvent?.(event, data);
    } catch {
      /* Display failures cannot corrupt durable history. */
    }
  };
  const append = async (message: Message): Promise<void> => {
    await options.onHistoryEvent?.({ type: "message.appended", message });
    history.push(message);
  };
  await append({ role: "user", content: prompt });
  for (let iteration = 1; iteration <= maxIterations; iteration++) {
    checkCancelled(signal);
    const system =
      base +
      (tasks.tasks.length ? "\nCurrent task state:\n" + tasks.render() : "") +
      (options.allowHostCommands && verification.state !== "not_run"
        ? "\n" + verification.summary()
        : "");
    let contextTokens: number;
    try {
      contextTokens = budget.ensureFits(history, registry.definitions, system);
    } catch (e) {
      if (!(e instanceof ContextBudgetExceeded)) throw e;
      const compacted = compactHistory(
        history,
        budget,
        registry.definitions,
        system,
      );
      if (!compacted) throw new ModelError(e.message);
      await options.onHistoryEvent?.({
        type: "context.compacted",
        history: compacted,
      });
      history.splice(0, history.length, ...compacted);
      contextTokens = budget.ensureFits(history, registry.definitions, system);
      status("context.compacted", { context_tokens: contextTokens });
    }
    status("request.started", {
      iteration,
      max_iterations: maxIterations,
      context_tokens: contextTokens,
      output_tokens: options.maxTokens,
    });
    const chunks: string[] = [];
    let response;
    try {
      response = await options.provider.createMessage(
        {
          messages: structuredClone(history),
          tools: registry.definitions,
          ...(system ? { system } : {}),
          ...(options.onText
            ? {
                onText: (text: string) => {
                  chunks.push(text);
                  options.onText?.(text);
                },
              }
            : {}),
        },
        signal,
      );
      checkCancelled(signal);
    } catch (e) {
      const partial = chunks.join("");
      if (partial.trim())
        await append({
          role: "assistant",
          content: [{ type: "text", text: partial }],
        });
      throw e;
    }
    status("response.received", {
      stop_reason: response.stop_reason,
      input_tokens: response.input_tokens,
      output_tokens: response.output_tokens,
    });
    const text = response.content
      .filter((b) => b.type === "text")
      .map((b) => b.text)
      .join("\n");
    if (response.stop_reason === "max_tokens") {
      await append({
        role: "assistant",
        content: response.content.filter((b) => b.type === "text"),
      });
      return { text, truncated: true };
    }
    const calls = response.content.filter((b) => b.type === "tool_use");
    if (response.stop_reason === "end_turn") {
      if (calls.length)
        throw new ModelError("The model ended while requesting a tool.");
      if (!text.trim()) throw new ModelError("The model returned no text.");
      await append({ role: "assistant", content: response.content });
      return { text, truncated: false };
    }
    if (response.stop_reason !== "tool_use" || !calls.length)
      throw new ModelError(
        `The model stopped unexpectedly (${response.stop_reason ?? "unknown"}).`,
      );
    if (new Set(calls.map((c) => c.id)).size !== calls.length)
      throw new ModelError("The model returned duplicate tool call IDs.");
    const results: ToolResultBlock[] = [];
    const order = new Map(calls.map((call, index) => [call.id, index]));
    await append({ role: "assistant", content: response.content });
    status("tools.started", {
      count: calls.length,
      names: calls.map((c) => c.name),
    });
    const record = async (
      call: ToolUseBlock,
      result: ToolResult,
    ): Promise<void> => {
      if (results.some((r) => r.tool_use_id === call.id)) return;
      const [content, truncated] = budget.fitResult(
        result.content,
        (candidate) => [
          ...history,
          {
            role: "user",
            content: [
              ...results,
              {
                type: "tool_result",
                tool_use_id: call.id,
                content: candidate,
                is_error: result.is_error,
              },
            ],
          },
        ],
        registry.definitions,
        system,
      );
      const block: ToolResultBlock = {
        type: "tool_result",
        tool_use_id: call.id,
        content,
        is_error: result.is_error || truncated,
      };
      await options.onHistoryEvent?.({ type: "tool.result", result: block });
      results.push(block);
      results.sort(
        (a, b) => order.get(a.tool_use_id)! - order.get(b.tool_use_id)!,
      );
      status("tool.finished", { name: call.name, is_error: block.is_error });
    };
    const prepare = async (
      call: ToolUseBlock,
    ): Promise<ToolResult | undefined> => {
      if (COMMAND_TOOLS.has(call.name) && !options.allowHostCommands)
        return fail("Host commands are disabled; use --allow-host-commands.");
      if (options.allowedTools && !options.allowedTools.has(call.name))
        return fail("Tool is outside this agent's allowed set.");
      if (options.planning && !READ_ONLY_TOOLS.has(call.name))
        return fail("Tool is blocked in planning mode.");
      if (call.name === "retest_checks" && !verification.checks.length)
        return fail("No prior checks are available to retest.");
      const decision = await gate.check(
        {
          tool_name: call.name,
          tool_input:
            call.name === "retest_checks"
              ? { checks: structuredClone(verification.checks) }
              : call.input,
          external: registry.isExternal(call.name),
        },
        signal,
      );
      if (decision.action !== "allow")
        return fail(
          `Permission denied for tool '${call.name}': ${decision.reason}`,
        );
    };
    const execute = async (call: ToolUseBlock): Promise<ToolResult> => {
      const run = () => registry.execute(call.name, call.input);
      return options.hooks && !options.planning
        ? options.hooks.execute(
            call.name,
            call.input,
            options.workspace.root,
            run,
            signal,
          )
        : run();
    };
    try {
      await scheduler.run(calls, prepare, execute, record, signal);
      history.push({ role: "user", content: results });
    } catch (e) {
      // Repair in memory and on disk before another prompt is accepted. No replay.
      for (const call of calls) {
        if (results.some((r) => r.tool_use_id === call.id)) continue;
        const block: ToolResultBlock = {
          type: "tool_result",
          tool_use_id: call.id,
          content: scheduler.started.has(call.id)
            ? "Tool interrupted or its result was not saved. It may have partially executed; inspect the workspace before retrying."
            : "Not executed: the turn ended before this tool started.",
          is_error: true,
        };
        await options.onHistoryEvent?.({ type: "tool.result", result: block });
        results.push(block);
      }
      results.sort(
        (a, b) => order.get(a.tool_use_id)! - order.get(b.tool_use_id)!,
      );
      history.push({ role: "user", content: results });
      throw e;
    }
  }
  throw new ModelError(
    `Agent exceeded the maximum of ${maxIterations} model requests.`,
  );
}
