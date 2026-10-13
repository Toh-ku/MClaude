#!/usr/bin/env node
import { pathToFileURL } from "node:url";
import { runAgent } from "../core/agent.js";
import { resolveConfig } from "../core/config.js";
import {
  ConfigurationError,
  TurnCancelled,
  safeError,
  type Message,
} from "../core/types.js";
import { TaskBoard } from "../core/tasks.js";
import { VerificationWorkflow } from "../core/verification.js";
import { ConfigStore } from "../persistence/config-store.js";
import { SessionStore, type Session } from "../persistence/session.js";
import { WorkspaceFS } from "../security/workspace.js";
import { PermissionGate } from "../security/permissions.js";
import { AnthropicProvider } from "../providers/anthropic.js";
import { loadInstructions } from "../integrations/instructions.js";
import { SkillCatalog } from "../integrations/skills.js";
import { HookRunner } from "../integrations/hooks.js";
import { MCPRegistry } from "../integrations/mcp.js";
import { InputService } from "../terminal/input.js";
import {
  TerminalStatus,
  supportsStatus,
  safeLine,
} from "../terminal/status.js";
import { parseCLI, numeric, HELP } from "./args.js";
import { authCommand } from "./auth.js";
async function sessionsCommand(
  argv: string[],
  workspace: WorkspaceFS,
): Promise<number> {
  const store = new SessionStore();
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log(
      'Usage: mclaude sessions list | search "query" | rename SESSION_ID "name"',
    );
    return 0;
  }
  if (argv[0] === "list" && argv.length === 1) {
    const sessions = await store.list(workspace.root);
    console.log(
      sessions.length
        ? sessions
            .map(
              (s) =>
                `${s.id}  ${s.updated_at}  ${s.model}  ${s.name ?? s.first_request}`,
            )
            .join("\n")
        : "No saved sessions for this workspace.",
    );
    return 0;
  }
  if (argv[0] === "search" && argv.length === 2) {
    const matches = await store.search(workspace.root, argv[1]!);
    console.log(
      matches.length
        ? matches
            .map((m) => `${m.id} ${m.timestamp} ${m.role}: ${m.excerpt}`)
            .join("\n")
        : "No matching saved conversation text.",
    );
    return 0;
  }
  if (argv[0] === "rename" && argv.length === 3) {
    await store.rename(argv[1]!, workspace.root, argv[2]!);
    console.log("Session renamed.");
    return 0;
  }
  throw new ConfigurationError(
    "Invalid sessions command; run mclaude sessions --help.",
  );
}
export async function main(
  argv: string[] = process.argv.slice(2),
): Promise<number> {
  try {
    return await execute(argv);
  } catch (e) {
    if (e instanceof TurnCancelled) {
      console.error(e.message);
      return 130;
    }
    console.error("Error: " + safeError(e));
    return e instanceof ConfigurationError ? 2 : 1;
  }
}
async function execute(argv: string[]): Promise<number> {
  if (argv[0] === "login" || argv[0] === "logout")
    return authCommand(argv[0], argv.slice(1));
  if (argv[0] === "sessions")
    return sessionsCommand(
      argv.slice(1),
      await WorkspaceFS.create(process.cwd()),
    );
  const { values, prompt: initialPrompt } = parseCLI(argv);
  if (values["help"]) {
    console.log(HELP);
    return 0;
  }
  if (values["version"]) {
    console.log("mclaude 0.1.0 (TypeScript)");
    return 0;
  }
  const workspace = await WorkspaceFS.create(process.cwd());
  if (values["show-instructions"]) {
    const instructions = await loadInstructions(workspace);
    console.log(
      instructions.sources.length
        ? instructions.sources
            .map((s) => `${s.path} (scope: ${s.scope})`)
            .join("\n")
        : "No project instruction files apply to this workspace.",
    );
    return 0;
  }
  if (values["list-skills"]) {
    const skills = await SkillCatalog.discover(workspace);
    console.log(skills.summary() || "No skills discovered.");
    for (const warning of skills.warnings) console.error(warning);
    return 0;
  }
  const interactive =
    !!values["interactive"] ||
    !!values["continue"] ||
    typeof values["resume"] === "string" ||
    (initialPrompt === undefined && !!process.stdin.isTTY);
  if (initialPrompt === undefined && !interactive) {
    console.log(HELP);
    return 0;
  }
  if (initialPrompt !== undefined && !initialPrompt.trim())
    throw new ConfigurationError("The prompt must not be empty.");
  const maxTokens = numeric(values, "max-tokens", 1024, true, 1);
  const maxIterations = numeric(values, "max-iterations", 8, true, 1);
  const contextBudget = numeric(
    values,
    "context-budget",
    100000,
    true,
    maxTokens + 1,
  );
  const readWorkers = numeric(values, "read-workers", 4, true, 1, 16);
  const subagentBudget = numeric(values, "subagent-budget", 8, true, 0, 32);
  const configOptions = {
    maxTokens,
    timeout: numeric(values, "timeout", 60, false, Number.MIN_VALUE),
    requestRetries: numeric(values, "request-retries", 2, true, 0, 10),
    retryDelay: numeric(values, "retry-delay", 0.5, false, 0),
    ...(typeof values["model"] === "string" ? { model: values["model"] } : {}),
  };
  const configStore = new ConfigStore();
  let saved = await configStore.load();
  let config;
  try {
    config = resolveConfig(configOptions, saved);
  } catch (e) {
    if (
      !(e instanceof ConfigurationError) ||
      !process.stdin.isTTY ||
      !process.stderr.isTTY ||
      (saved.api_key && saved.model)
    )
      throw e;
    console.error("Configure API access to continue this task.");
    await authCommand(
      "login",
      typeof values["model"] === "string" ? ["--model", values["model"]] : [],
    );
    saved = await configStore.load();
    config = resolveConfig(configOptions, saved);
  }
  const provider = new AnthropicProvider(config);
  const sessions = new SessionStore();
  let session: Session | undefined;
  let planning = !!values["plan"];
  const hooks =
    typeof values["hooks"] === "string"
      ? await HookRunner.fromFile(values["hooks"])
      : undefined;
  const mcp =
    typeof values["mcp-config"] === "string"
      ? await MCPRegistry.fromFile(values["mcp-config"], workspace.root)
      : undefined;
  let status: TerminalStatus | undefined,
    input: InputService | undefined,
    active: AbortController | undefined,
    exiting = false;
  const interrupt = (): void => {
    if (active) {
      active.abort();
    } else {
      exiting = true;
      input?.close();
    }
  };
  process.on("SIGINT", interrupt);
  try {
    if (typeof values["resume"] === "string")
      session = await sessions.resume(values["resume"], workspace.root);
    else if (values["continue"])
      session = await sessions.continueLatest(workspace.root);
    else if (interactive && !values["no-persist"])
      session = await sessions.create(workspace.root, config.model);
    if (session?.recoveryWarning)
      console.error("Recovery: " + session.recoveryWarning);
    const history: Message[] = session?.history ?? [];
    const tasks = new TaskBoard();
    tasks.tasks = session?.tasks ?? [];
    const verification = new VerificationWorkflow();
    status = new TerminalStatus({
      enabled: supportsStatus(!!values["plain"]),
      workspace: workspace.root,
      model: config.model,
      ...(session ? { sessionId: session.id } : {}),
      sessionKind: session ? "saved" : "temporary",
      planning,
      contextBudget,
      maxTokens,
      readWorkers,
      subagentBudget,
      hooks: !!hooks,
      mcp: !!mcp,
    });
    status.open();
    status.updateTasks(tasks.tasks);
    input = new InputService(interrupt);
    const gate = new PermissionGate(async (request, reason, signal) => {
      status?.suspend();
      try {
        console.error(
          `\nPermission: ${safeLine(request.tool_name)} — ${safeLine(reason)}\nInput: ${safeLine(JSON.stringify(request.tool_input))}`,
        );
        return ["y", "yes"].includes(
          (await input!.question("Allow this tool call? [y/N] ", signal))
            ?.trim()
            .toLowerCase() ?? "",
        );
      } finally {
        status?.resume();
      }
    });
    if (interactive)
      console.error(
        "Interactive conversation. /exit to leave; Ctrl+C cancels a turn, at the input prompt it exits.",
      );
    let prompt = initialPrompt,
      exitCode = 0;
    while (!exiting) {
      if (prompt === undefined) {
        status.event("ready", "awaiting input");
        const line = await input.question("You> ");
        if (line === undefined || exiting) break;
        const command = line.trim().toLowerCase();
        if (["/exit", "/quit"].includes(command)) break;
        if (!command) continue;
        if (command === "/tasks") {
          console.log(tasks.render());
          continue;
        }
        if (command === "/plan" || command === "/execute") {
          planning = command === "/plan";
          if (planning) await mcp?.close();
          status.setMode(planning);
          console.error(`Mode: ${planning ? "planning" : "execution"}`);
          continue;
        }
        prompt = line;
      }
      active = new AbortController();
      let streamed = false;
      try {
        const response = await runAgent(prompt, {
          provider,
          workspace,
          maxTokens,
          signal: active.signal,
          history,
          maxIterations,
          contextBudget,
          permissionGate: gate,
          sessions,
          tasks,
          verification,
          planning,
          ...(hooks ? { hooks } : {}),
          ...(mcp ? { mcp } : {}),
          subagentBudget,
          readWorkers,
          allowHostCommands: !!values["allow-host-commands"],
          onText: (text) => {
            streamed = true;
            process.stdout.write(text);
          },
          onHistoryEvent: async (event) => {
            await session?.append(event);
            if (event.type === "tasks.updated")
              status?.updateTasks(event.tasks);
          },
          onStatusEvent: status.agentEvent,
        });
        if (!streamed) console.log(response.text);
        else process.stdout.write("\n");
        await session?.append({
          type: "turn.finished",
          status: response.truncated ? "truncated" : "ok",
        });
        if (response.truncated) {
          console.error("Error: Output truncated; increase --max-tokens.");
          exitCode = 1;
        }
        status.event(
          response.truncated ? "truncated" : "completed",
          "turn finished",
        );
      } catch (e) {
        const cancelled = e instanceof TurnCancelled;
        await session?.append({
          type: "turn.finished",
          status: cancelled ? "cancelled" : "error",
        });
        if (cancelled) {
          console.error("\nTurn cancelled. You can continue the conversation.");
          status.event("cancelled", "turn stopped");
          if (!interactive) return 130;
        } else {
          console.error("Error: " + safeError(e));
          status.event("error", "turn failed");
          exitCode = 1;
        }
      } finally {
        active = undefined;
      }
      if (!interactive) break;
      prompt = undefined;
    }
    return exitCode;
  } finally {
    active?.abort();
    process.removeListener("SIGINT", interrupt);
    input?.close();
    status?.close();
    await mcp?.close();
    await session?.close();
  }
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href)
  process.exitCode = await main();
