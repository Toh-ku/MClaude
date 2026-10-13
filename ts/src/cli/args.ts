import { parseArgs, type ParseArgsOptionsConfig } from "node:util";
import { ConfigurationError } from "../core/types.js";
export const OPTIONS: ParseArgsOptionsConfig = {
  help: { type: "boolean", short: "h" },
  version: { type: "boolean" },
  interactive: { type: "boolean", short: "i" },
  plan: { type: "boolean" },
  continue: { type: "boolean" },
  resume: { type: "string" },
  "no-persist": { type: "boolean" },
  plain: { type: "boolean" },
  "show-instructions": { type: "boolean" },
  "list-skills": { type: "boolean" },
  model: { type: "string" },
  "max-tokens": { type: "string" },
  timeout: { type: "string" },
  "request-retries": { type: "string" },
  "retry-delay": { type: "string" },
  "max-iterations": { type: "string" },
  "context-budget": { type: "string" },
  "read-workers": { type: "string" },
  "subagent-budget": { type: "string" },
  "allow-host-commands": { type: "boolean" },
  hooks: { type: "string" },
  "mcp-config": { type: "string" },
};
export function parseCLI(argv: string[]): {
  values: Record<string, string | boolean | undefined>;
  prompt: string | undefined;
} {
  let parsed;
  try {
    parsed = parseArgs({
      args: argv,
      options: OPTIONS,
      allowPositionals: true,
      strict: true,
    });
  } catch {
    throw new ConfigurationError(
      "Unknown option or missing value; run mclaude --help.",
    );
  }
  if (parsed.positionals.length > 1)
    throw new ConfigurationError("Expected one quoted task prompt.");
  const v = parsed.values;
  if (v["continue"] && v["resume"])
    throw new ConfigurationError(
      "--continue and --resume are mutually exclusive.",
    );
  if (v["no-persist"] && (v["continue"] || v["resume"]))
    throw new ConfigurationError(
      "--no-persist cannot be combined with --continue or --resume.",
    );
  return {
    values: v as Record<string, string | boolean | undefined>,
    prompt: parsed.positionals[0],
  };
}
export function numeric(
  values: Record<string, string | boolean | undefined>,
  name: string,
  fallback: number,
  integer = false,
  min = 0,
  max = Infinity,
): number {
  const raw = values[name];
  const value = raw === undefined ? fallback : Number(raw);
  if (
    typeof raw === "boolean" ||
    (typeof raw === "string" && !raw.trim()) ||
    !Number.isFinite(value) ||
    (integer && !Number.isSafeInteger(value)) ||
    value < min ||
    value > max
  )
    throw new ConfigurationError(
      `--${name} must be ${integer ? "an integer" : "a finite number"} between ${min} and ${max}.`,
    );
  return value;
}
export const HELP = `MClaude: a local-first TypeScript coding agent for the terminal.

Usage: mclaude [options] ["task"]
       mclaude login [--model MODEL] [--base-url URL]
       mclaude logout
       mclaude sessions list | search "query" | rename SESSION_ID "name"

No task in a terminal starts an interactive conversation.
/tasks, /plan, /execute, /exit, /quit; Ctrl+C cancels the current turn.
Model text goes to stdout; status and errors go to stderr.

Options:
  -h, --help                 Show this help
      --version              Show version
  -i, --interactive          Start a conversation
      --continue             Resume latest workspace session
      --resume SESSION_ID    Resume a specific session
      --no-persist           Do not persist an interactive session
      --plan                 Read-only planning mode
      --plain                Disable terminal dashboard
      --show-instructions    Show applicable project rules
      --list-skills          List discovered skills
      --model MODEL          Override ANTHROPIC_MODEL / saved model
      --max-tokens N         Output limit (1024)
      --timeout SECONDS      Model request timeout (60)
      --request-retries N    Transient model retries, 0-10 (2)
      --retry-delay SECONDS  Initial retry delay (0.5)
      --max-iterations N     Model requests per turn (8)
      --context-budget N     Estimated context budget (100000)
      --read-workers N       Concurrent read tools, 1-16 (4)
      --subagent-budget N    Delegated requests, 0-32 (8)
      --allow-host-commands  Enable approved commands on the host
      --hooks FILE           Explicit hook JSON configuration
      --mcp-config FILE      Explicit stdio/HTTP MCP configuration
`;
