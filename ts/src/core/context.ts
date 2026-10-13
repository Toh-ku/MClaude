import {
  ContextBudgetExceeded,
  clip,
  type Message,
  type ToolSpec,
} from "./types.js";
const SUMMARY_PREFIX = "Earlier conversation summary (generated locally):";
const TRUNCATION_MARKER = "\n...[tool result truncated to fit context budget]";
export function estimateTokens(value: unknown): number {
  return Math.max(
    1,
    Math.ceil(
      Buffer.byteLength(
        typeof value === "string" ? value : JSON.stringify(value),
        "utf8",
      ) / 4,
    ) + 1,
  );
}
export class ContextBudget {
  constructor(
    readonly maxTokens = 100000,
    readonly outputTokens = 1024,
  ) {
    if (
      !Number.isSafeInteger(maxTokens) ||
      !Number.isSafeInteger(outputTokens) ||
      outputTokens <= 0 ||
      maxTokens <= outputTokens
    )
      throw new ContextBudgetExceeded(
        "Context budget must exceed the positive output token reserve.",
      );
  }
  requestTokens(
    messages: Message[],
    tools: ToolSpec[] = [],
    system = "",
  ): number {
    return (
      estimateTokens(messages) +
      (tools.length ? estimateTokens(tools) : 0) +
      (system ? estimateTokens(system) : 0)
    );
  }
  ensureFits(messages: Message[], tools: ToolSpec[] = [], system = ""): number {
    const used = this.requestTokens(messages, tools, system);
    if (used + this.outputTokens > this.maxTokens)
      throw new ContextBudgetExceeded(
        `Conversation context exceeds budget (${used} estimated input + ${this.outputTokens} reserved output > ${this.maxTokens} tokens).`,
      );
    return used;
  }
  fitResult(
    content: string,
    build: (candidate: string) => Message[],
    tools: ToolSpec[] = [],
    system = "",
  ): [string, boolean] {
    const fits = (text: string): boolean =>
      this.requestTokens(build(text), tools, system) + this.outputTokens <=
      this.maxTokens;
    if (fits(content)) return [content, false];
    const points = Array.from(content);
    let low = 0,
      high = points.length;
    while (low < high) {
      const middle = Math.floor((low + high + 1) / 2);
      if (fits(points.slice(0, middle).join("") + TRUNCATION_MARKER))
        low = middle;
      else high = middle - 1;
    }
    const candidate = points.slice(0, low).join("") + TRUNCATION_MARKER;
    return [
      fits(candidate)
        ? candidate
        : "Tool result omitted: context budget exhausted.",
      true,
    ];
  }
}
function groups(messages: Message[]): Message[][] {
  const output: Message[][] = [];
  for (let i = 0; i < messages.length; i++) {
    const m = messages[i];
    if (!m) continue;
    const group = [m];
    const ids =
      m.role === "assistant"
        ? m.content.filter((b) => b.type === "tool_use").map((b) => b.id)
        : [];
    const next = messages[i + 1];
    if (
      ids.length &&
      next?.role === "user" &&
      typeof next.content !== "string" &&
      next.content.length === ids.length &&
      next.content.every((b) => ids.includes(b.tool_use_id))
    ) {
      group.push(next);
      i++;
    }
    output.push(group);
  }
  return output;
}
function summarize(messages: Message[], limit: number): string {
  const previous: string[] = [],
    users: string[] = [],
    updates: string[] = [],
    errors: string[] = [];
  for (const m of messages) {
    if (typeof m.content === "string") {
      if (
        m.content !== "Use this compacted context for the earlier conversation."
      )
        users.push(m.content);
      continue;
    }
    if (m.role === "assistant")
      for (const b of m.content) {
        if (b.type !== "text") continue;
        if (b.text.startsWith(SUMMARY_PREFIX))
          previous.push(
            ...b.text.slice(SUMMARY_PREFIX.length).trim().split("\n"),
          );
        else updates.push(b.text);
      }
    else for (const b of m.content) if (b.is_error) errors.push(b.content);
  }
  let goal = "";
  const prior: string[] = [],
    earlier: string[] = [];
  for (const line of previous) {
    if (/^(Earlier goal|Original request): /.test(line) && !goal)
      goal = line.slice(line.indexOf(": ") + 2);
    else if (line.startsWith("Recent user request: "))
      prior.push(line.slice(21));
    else earlier.push(line);
  }
  goal ||= previous[0] || users[0] || "";
  const lines: string[] = [];
  let used = 0;
  const add = (label: string, text: string, cap: number): void => {
    const remaining = limit - used - label.length - 2;
    if (remaining <= 12 || !text.trim()) return;
    let value = text.replace(/\s+/g, " ").trim();
    const max = Math.min(cap, remaining);
    if (Array.from(value).length > max) value = clip(value, max - 3) + "...";
    const line = `${label}: ${value}`;
    lines.push(line);
    used += Array.from(line).length + 1;
  };
  add("Earlier goal", goal, Math.min(350, Math.floor(limit / 3)));
  for (const u of [...prior.reverse(), ...users].slice(-5).reverse())
    if (u !== goal)
      add("Recent user request", u, Math.min(500, Math.floor(limit / 3)));
  for (const u of earlier.reverse())
    add("Earlier context", u, Math.min(300, Math.floor(limit / 4)));
  for (const u of updates.slice(-4).reverse())
    add("Assistant update", u, Math.min(350, Math.floor(limit / 4)));
  for (const u of errors.slice(-2).reverse())
    add("Tool error", u, Math.min(250, Math.floor(limit / 5)));
  return lines.join("\n");
}
export function compactHistory(
  messages: Message[],
  budget: ContextBudget,
  tools: ToolSpec[] = [],
  system = "",
): Message[] | undefined {
  const grouped = groups(messages);
  if (grouped.length < 2) return;
  for (const limit of [4000, 2000, 1000, 500, 200])
    for (let cut = 1; cut < grouped.length; cut++) {
      const candidate: Message[] = [
        {
          role: "user",
          content: "Use this compacted context for the earlier conversation.",
        },
        {
          role: "assistant",
          content: [
            {
              type: "text",
              text:
                SUMMARY_PREFIX +
                "\n" +
                summarize(grouped.slice(0, cut).flat(), limit),
            },
          ],
        },
        ...grouped.slice(cut).flat(),
      ];
      try {
        budget.ensureFits(candidate, tools, system);
        return candidate;
      } catch (e) {
        if (!(e instanceof ContextBudgetExceeded)) throw e;
      }
    }
}
