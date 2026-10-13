import {
  SessionError,
  isObject,
  type Message,
  type ToolResultBlock,
} from "../core/types.js";
function exact(value: Record<string, unknown>, keys: string[]): boolean {
  return Object.keys(value).sort().join(",") === keys.sort().join(",");
}
export function validateToolResult(value: unknown): ToolResultBlock {
  if (
    !isObject(value) ||
    !exact(value, ["type", "tool_use_id", "content", "is_error"]) ||
    value["type"] !== "tool_result" ||
    typeof value["tool_use_id"] !== "string" ||
    typeof value["content"] !== "string" ||
    typeof value["is_error"] !== "boolean"
  )
    throw new SessionError("Session contains an invalid tool result.");
  return value as unknown as ToolResultBlock;
}
export function validateMessage(value: unknown): Message {
  if (
    !isObject(value) ||
    !exact(value, ["role", "content"]) ||
    !["user", "assistant"].includes(String(value["role"]))
  )
    throw new SessionError("Session contains an invalid message.");
  const content = value["content"];
  if (typeof content === "string" && value["role"] === "user")
    return { role: "user", content };
  if (!Array.isArray(content))
    throw new SessionError("Session contains invalid message content.");
  const ids = new Set<string>();
  for (const block of content as unknown[]) {
    if (!isObject(block)) throw new SessionError("Invalid content block.");
    if (value["role"] === "user") {
      validateToolResult(block);
      continue;
    }
    if (
      block["type"] === "text" &&
      exact(block, ["type", "text"]) &&
      typeof block["text"] === "string"
    )
      continue;
    if (
      block["type"] === "tool_use" &&
      exact(block, ["type", "id", "name", "input"]) &&
      typeof block["id"] === "string" &&
      typeof block["name"] === "string" &&
      isObject(block["input"]) &&
      !ids.has(block["id"])
    ) {
      ids.add(block["id"]);
      continue;
    }
    throw new SessionError("Invalid or duplicate assistant content block.");
  }
  return value as unknown as Message;
}
export function validateHistory(value: unknown): Message[] {
  if (!Array.isArray(value))
    throw new SessionError("Invalid compacted history.");
  const messages = (value as unknown[]).map(validateMessage);
  let pending = new Set<string>();
  for (const message of messages) {
    if (message.role === "assistant") {
      if (pending.size)
        throw new SessionError("Unfinished tool calls in history.");
      pending = new Set(
        message.content.filter((b) => b.type === "tool_use").map((b) => b.id),
      );
    } else if (typeof message.content !== "string") {
      const ids = message.content.map((b) => b.tool_use_id);
      if (
        ids.length !== pending.size ||
        new Set(ids).size !== ids.length ||
        ids.some((id) => !pending.has(id))
      )
        throw new SessionError("Mismatched tool results in history.");
      pending.clear();
    } else if (pending.size)
      throw new SessionError("Message precedes unfinished tools.");
  }
  if (pending.size) throw new SessionError("Unfinished tool calls in history.");
  return messages;
}
