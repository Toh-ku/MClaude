import stringWidth from "string-width";
import { clip, type StatusSink, type Task } from "../core/types.js";
export interface StatusOptions {
  enabled: boolean;
  workspace: string;
  model: string;
  sessionId?: string;
  sessionKind: string;
  planning: boolean;
  contextBudget: number;
  maxTokens: number;
  readWorkers: number;
  subagentBudget: number;
  hooks: boolean;
  mcp: boolean;
  stream?: NodeJS.WriteStream;
  size?: { columns: number; rows: number };
}
export function safeLine(value: unknown): string {
  return String(value).replace(/[\x00-\x1f\x7f-\x9f]/g, (character) =>
    character === "\n" || character === "\r"
      ? " "
      : `\\x${character.charCodeAt(0).toString(16).padStart(2, "0")}`,
  );
}
export function fit(value: string, width: number): string {
  let out = "";
  for (const point of Array.from(safeLine(value))) {
    if (stringWidth(out + point) > width) break;
    out += point;
  }
  return out + " ".repeat(Math.max(0, width - stringWidth(out)));
}
export function supportsStatus(plain = false): boolean {
  return (
    !plain &&
    !!process.stdin.isTTY &&
    !!process.stdout.isTTY &&
    !!process.stderr.isTTY &&
    process.env["TERM"] !== "dumb"
  );
}
export class TerminalStatus {
  private readonly stream: NodeJS.WriteStream;
  private opened = false;
  private closed = false;
  private paused = false;
  private phase = "starting";
  private context = 0;
  private inputTokens: number | undefined;
  private outputTokens: number | undefined;
  private iteration = "";
  private tasks: Task[] = [];
  private events: string[] = [];
  private readonly started = Date.now();
  private timer: NodeJS.Timeout | undefined;
  constructor(private readonly options: StatusOptions) {
    this.stream = options.stream ?? process.stderr;
  }
  private size(): { columns: number; rows: number; dashboard: number } {
    const columns = Math.max(
        10,
        this.options.size?.columns ?? this.stream.columns ?? 100,
      ),
      rows = Math.max(4, this.options.size?.rows ?? this.stream.rows ?? 30);
    return { columns, rows, dashboard: Math.max(1, Math.min(14, rows - 3)) };
  }
  lines(): string[] {
    const { columns, dashboard } = this.size();
    const percent = Math.min(
      100,
      Math.round((this.context / this.options.contextBudget) * 100),
    );
    const active = this.tasks.find((t) => t.status === "in_progress");
    return [
      `MClaude 0.1.0 | ${this.options.planning ? "PLAN" : "EXECUTE"} | ${this.phase.toUpperCase()}`,
      `workspace  ${this.options.workspace}`,
      `session    ${this.options.sessionId ?? "temporary"} (${this.options.sessionKind}) | model ${this.options.model}`,
      `context    ${this.context}/${this.options.contextBudget} estimated tokens (${percent}%)`,
      `request    ${this.inputTokens === undefined ? "waiting for response" : `input ${this.inputTokens} | output ${this.outputTokens ?? 0}`} | limit ${this.options.maxTokens} ${this.iteration}`,
      `tasks      ${this.tasks.filter((t) => t.status === "completed").length}/${this.tasks.length} completed${active ? " | current: " + active.description : ""}`,
      `runtime    Node ${process.version} | PID ${process.pid} | readers ${this.options.readWorkers} | agents ${this.options.subagentBudget}`,
      `features   hooks ${this.options.hooks ? "on" : "off"} | MCP ${this.options.mcp ? "on" : "off"} | elapsed ${((Date.now() - this.started) / 1000).toFixed(1)}s`,
      "─".repeat(Math.max(1, columns - 2)),
      ...this.events,
    ]
      .slice(0, dashboard)
      .map((line) => fit(line, columns));
  }
  open(): void {
    if (!this.options.enabled || this.opened || this.closed) return;
    this.opened = true;
    const { rows, dashboard } = this.size();
    this.stream.write(
      `\x1b[?1049h\x1b[2J\x1b[H\x1b[${dashboard + 1};${rows}r\x1b[${dashboard + 1};1H`,
    );
    this.stream.on("resize", this.resize);
    this.timer = setInterval(() => this.redraw(), 1000);
    this.timer.unref();
    this.redraw();
  }
  private readonly resize = (): void => {
    if (!this.opened || this.closed) return;
    const { rows, dashboard } = this.size();
    this.stream.write(
      `\x1b[r\x1b[${dashboard + 1};${rows}r\x1b[${dashboard + 1};1H`,
    );
    this.redraw();
  };
  private redraw(): void {
    if (!this.opened || this.closed || this.paused) return;
    const { dashboard } = this.size();
    const lines = this.lines();
    while (lines.length < dashboard) lines.push("");
    this.stream.write(
      "\x1b[s\x1b[H" +
        lines.map((line) => "\x1b[2K" + line).join("\r\n") +
        "\x1b[u",
    );
  }
  suspend(): void {
    this.paused = true;
  }
  resume(): void {
    this.paused = false;
    this.redraw();
  }
  setMode(planning: boolean): void {
    this.options.planning = planning;
    this.event("mode", planning ? "planning" : "execution");
  }
  updateTasks(tasks: Task[]): void {
    this.tasks = structuredClone(tasks);
    this.redraw();
  }
  event(name: string, detail: string): void {
    this.phase = name;
    this.events.push(safeLine(`${name}: ${clip(detail, 200)}`));
    this.events = this.events.slice(-4);
    this.redraw();
  }
  readonly agentEvent: StatusSink = (event, data) => {
    if (event === "request.started") {
      this.phase = "thinking";
      this.context = Number(data["context_tokens"]);
      this.iteration = `| iteration ${data["iteration"]}/${data["max_iterations"]}`;
    } else if (event === "response.received") {
      this.inputTokens =
        typeof data["input_tokens"] === "number"
          ? data["input_tokens"]
          : undefined;
      this.outputTokens =
        typeof data["output_tokens"] === "number"
          ? data["output_tokens"]
          : undefined;
    } else if (event === "context.compacted") {
      this.context = Number(data["context_tokens"]);
    } else if (event === "tools.started") {
      this.phase = "tools";
    } else if (event === "tool.finished") {
      this.event(
        "tool",
        `${data["name"]}: ${data["is_error"] ? "error" : "ok"}`,
      );
    }
    this.redraw();
  };
  close(): void {
    if (this.closed) return;
    this.closed = true;
    if (this.timer) clearInterval(this.timer);
    this.stream.removeListener("resize", this.resize);
    if (this.opened) this.stream.write("\x1b[0m\x1b[r\x1b[?25h\x1b[?1049l");
  }
}
