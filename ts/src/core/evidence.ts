import {
  clip,
  codePoints,
  isObject,
  type Message,
  type ToolUseBlock,
} from "./types.js";

export const EVIDENCE_PREFIX = "Tool evidence: ";
const NOTE =
  "Evidence note: Historical excerpts; omitted evidence must be re-inspected.";
export interface ToolEvidence {
  call_id: string;
  tool: string;
  outcome: "ok" | "error_or_incomplete";
  targets: string[];
  excerpt: string;
  truncated: boolean;
  workspace_version?: string;
}
const CHECKS = new Set(["run_checks", "retest_checks"]);
const CHANGES = new Set([
  "create_file",
  "replace_text",
  "apply_edits",
  "restore_edit_checkpoint",
]);

function excerpt(text: string, limit: number): string {
  if (codePoints(text) <= limit) return text;
  const marker = "\n...[evidence excerpt]...\n";
  if (limit <= marker.length + 20)
    return clip(text, Math.max(0, limit - 3)) + (limit >= 3 ? "..." : "");
  const available = limit - marker.length;
  const head = Math.ceil(available * 0.65);
  return (
    clip(text, head) +
    marker +
    Array.from(text)
      .slice(-(available - head))
      .join("")
  );
}
function parseEvidence(text: string): ToolEvidence | undefined {
  try {
    const record: unknown = JSON.parse(text);
    if (
      isObject(record) &&
      typeof record["call_id"] === "string" &&
      typeof record["tool"] === "string" &&
      ["ok", "error_or_incomplete"].includes(String(record["outcome"])) &&
      Array.isArray(record["targets"]) &&
      record["targets"].every((v: unknown) => typeof v === "string") &&
      typeof record["excerpt"] === "string" &&
      typeof record["truncated"] === "boolean"
    )
      return record as unknown as ToolEvidence;
  } catch {
    /* Ignore malformed legacy summary entries. */
  }
  return undefined;
}
function targets(call: ToolUseBlock | undefined): string[] {
  if (!call) return [];
  if (typeof call.input["path"] === "string") return [call.input["path"]];
  if (Array.isArray(call.input["edits"]))
    return call.input["edits"]
      .filter(isObject)
      .map((edit) => edit["path"])
      .filter((p): p is string => typeof p === "string");
  return [];
}
export function collectToolEvidence(messages: Message[]): ToolEvidence[] {
  const calls = new Map<string, ToolUseBlock>();
  const records = new Map<string, ToolEvidence>();
  for (const message of messages) {
    if (typeof message.content === "string") continue;
    if (message.role === "assistant") {
      for (const block of message.content) {
        if (block.type === "tool_use") calls.set(block.id, block);
        else if (
          block.text.startsWith(
            "Earlier conversation summary (generated locally):",
          )
        )
          for (const line of block.text.split("\n")) {
            if (!line.startsWith(EVIDENCE_PREFIX)) continue;
            const record = parseEvidence(line.slice(EVIDENCE_PREFIX.length));
            if (record) records.set(record.call_id, record);
          }
      }
    } else
      for (const result of message.content) {
        const call = calls.get(result.tool_use_id);
        const context =
          call?.input["query"] ??
          call?.input["pattern"] ??
          call?.input["checkpoint_id"];
        const commands = call?.input["checks"];
        const verifiedVersion = CHECKS.has(call?.name ?? "")
          ? result.content.match(/Workspace version: ([a-f0-9]{64})/)?.[1]
          : undefined;
        let text = result.content;
        if (typeof context === "string")
          text = `Request reference: ${clip(context, 200)}\n${text}`;
        if (Array.isArray(commands))
          text =
            "Checks requested: " +
            commands
              .filter(isObject)
              .map((c) => String(c["command"]))
              .join("; ") +
            "\n" +
            text;
        records.set(result.tool_use_id, {
          call_id: result.tool_use_id,
          tool: call?.name ?? "unknown",
          outcome: result.is_error ? "error_or_incomplete" : "ok",
          targets: targets(call),
          excerpt: excerpt(text, 600),
          truncated:
            codePoints(text) > 600 ||
            (typeof context === "string" && codePoints(context) > 200) ||
            /\[(?:Output|Subagent report|Diff|Results|Command output).*truncated|tool result truncated to fit context budget|Tool result omitted:/i.test(
              result.content,
            ),
          ...(verifiedVersion ? { workspace_version: verifiedVersion } : {}),
        });
      }
  }
  return [...records.values()];
}

export function renderEvidence(
  records: ToolEvidence[],
  limit: number,
): string[] {
  if (!records.length || limit < NOTE.length) return [];
  if (limit <= NOTE.length + 100) return [NOTE];
  // Keep the most recent check result, so older passed checks cannot crowd out a failed retest.
  const lastCheck = records.findLastIndex((r) => CHECKS.has(r.tool));
  const eligible = records
    .map((record, index) => ({ record, index }))
    .filter(
      ({ record, index }) => !CHECKS.has(record.tool) || index === lastCheck,
    );
  const ranked = [...eligible].sort((a, b) => {
    const priority = (r: ToolEvidence): number =>
      CHECKS.has(r.tool)
        ? 4
        : CHANGES.has(r.tool) || r.outcome !== "ok"
          ? 3
          : r.tool === "delegate_readonly"
            ? 2
            : 1;
    return priority(b.record) - priority(a.record) || b.index - a.index;
  });
  const selected = ranked.slice(
    0,
    Math.min(8, Math.max(1, Math.floor((limit - NOTE.length) / 320))),
  );
  let remaining = limit - NOTE.length - 1;
  const lines: { index: number; text: string }[] = [];
  for (const [position, { record, index }] of selected.entries()) {
    const allowance = Math.floor(remaining / (selected.length - position));
    let low = 0,
      high = codePoints(record.excerpt);
    const line = (length: number): string =>
      EVIDENCE_PREFIX +
      JSON.stringify({
        ...record,
        excerpt: excerpt(record.excerpt, length),
        truncated: record.truncated || length < codePoints(record.excerpt),
      });
    if (codePoints(line(0)) > allowance) continue;
    while (low < high) {
      const middle = Math.floor((low + high + 1) / 2);
      if (codePoints(line(middle)) <= allowance) low = middle;
      else high = middle - 1;
    }
    const text = line(low);
    lines.push({ index, text });
    remaining -= codePoints(text) + 1;
  }
  return [
    ...lines.sort((a, b) => a.index - b.index).map((item) => item.text),
    NOTE,
  ];
}
