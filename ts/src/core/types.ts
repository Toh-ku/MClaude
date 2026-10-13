export type JsonSchema = Record<string, unknown>;
export interface TextBlock {
  type: "text";
  text: string;
}
export interface ToolUseBlock {
  type: "tool_use";
  id: string;
  name: string;
  input: Record<string, unknown>;
}
export interface ToolResultBlock {
  type: "tool_result";
  tool_use_id: string;
  content: string;
  is_error: boolean;
}
export type ContentBlock = TextBlock | ToolUseBlock;
export type Message =
  | { role: "user"; content: string | ToolResultBlock[] }
  | { role: "assistant"; content: ContentBlock[] };
export interface ToolResult {
  content: string;
  is_error: boolean;
}
export interface ToolSpec {
  name: string;
  description: string;
  input_schema: JsonSchema;
}
export interface ModelResponse {
  content: ContentBlock[];
  stop_reason: string | null;
  input_tokens?: number;
  output_tokens?: number;
}
export interface ModelRequest {
  messages: Message[];
  tools: ToolSpec[];
  system?: string;
  onText?: (text: string) => void;
}
export interface ModelProvider {
  createMessage(
    request: ModelRequest,
    signal: AbortSignal,
  ): Promise<ModelResponse>;
}
export interface Task {
  id: string;
  description: string;
  status: "pending" | "in_progress" | "completed" | "blocked";
}
export type TurnStatus = "ok" | "error" | "cancelled" | "truncated";
export type SessionEvent =
  | { type: "message.appended"; message: Message }
  | { type: "tool.result"; result: ToolResultBlock }
  | { type: "turn.finished"; status: TurnStatus }
  | { type: "tasks.updated"; tasks: Task[] }
  | { type: "context.compacted"; history: Message[] }
  | { type: "session.named"; name: string };
export type HistorySink = (event: SessionEvent) => Promise<void>;
export type StatusSink = (event: string, data: Record<string, unknown>) => void;
export interface AgentResponse {
  text: string;
  truncated: boolean;
}
export const ok = (content: string): ToolResult => ({
  content,
  is_error: false,
});
export const fail = (content: string): ToolResult => ({
  content,
  is_error: true,
});

export class MClaudeError extends Error {}
export class ConfigurationError extends MClaudeError {}
export class ModelError extends MClaudeError {}
export class SessionError extends MClaudeError {}
export class CheckpointError extends MClaudeError {}
export class ContextBudgetExceeded extends MClaudeError {}
export class TurnCancelled extends MClaudeError {
  constructor(message = "The user cancelled this turn.") {
    super(message);
  }
}
export function checkCancelled(signal?: AbortSignal): void {
  if (signal?.aborted) throw new TurnCancelled();
}
export function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
export function codePoints(value: string): number {
  return Array.from(value).length;
}
export function clip(value: string, length: number): string {
  return Array.from(value).slice(0, length).join("");
}
export function errorCode(error: unknown): string | undefined {
  return isObject(error) && typeof error["code"] === "string"
    ? error["code"]
    : undefined;
}
export function safeError(error: unknown): string {
  return error instanceof MClaudeError
    ? error.message
    : `Operation failed (${error instanceof Error ? error.name : "Error"}).`;
}
