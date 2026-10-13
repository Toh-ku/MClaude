import * as fs from "node:fs/promises";
import path from "node:path";
import {
  SessionError,
  isObject,
  errorCode,
  codePoints,
  clip,
  type Message,
  type SessionEvent,
  type ToolResultBlock,
  type Task,
} from "../core/types.js";
import { validateTasks } from "../core/tasks.js";
import {
  validateHistory,
  validateMessage,
  validateToolResult,
} from "./validation.js";
import {
  stateRoot,
  workspaceKey,
  normalizeWorkspace,
  newId,
  utcNow,
  validId,
} from "./paths.js";
import { atomicWrite, utf8 } from "../security/workspace.js";
import { pidIsRunning } from "../runtime/process-platform.js";

export interface SessionInfo {
  id: string;
  updated_at: string;
  model: string;
  first_request: string;
  name?: string;
}
export interface SessionMatch {
  id: string;
  timestamp: string;
  role: "user" | "assistant";
  excerpt: string;
}
const MAX_EVENT_BYTES = 2000000;
async function acquireLock(file: string): Promise<string> {
  const token = `${process.pid} ${newId()}`;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      await atomicWrite(file, Buffer.from(token + "\n"), { exclusive: true });
      return token;
    } catch (e) {
      if (errorCode(e) !== "EEXIST")
        throw new SessionError("Could not acquire session lock.");
      const original = await fs.readFile(file, "utf8");
      const pid = Number(original.trim().split(/\s+/)[0]);
      if (pidIsRunning(pid))
        throw new SessionError(
          "This session is already open in another MClaude process.",
        );
      if (!/^\d+ [0-9a-f]{32}\s*$/.test(original))
        throw new SessionError(
          "Session lock is invalid; inspect it before removing it.",
        );
      if ((await fs.readFile(file, "utf8")) !== original)
        throw new SessionError("Session lock changed during acquisition.");
      await fs.unlink(file);
    }
  }
  throw new SessionError("Could not acquire session lock.");
}
async function releaseLock(file: string, token: string): Promise<void> {
  try {
    if ((await fs.readFile(file, "utf8")).trim() === token)
      await fs.unlink(file);
  } catch {
    /* Missing or replaced lock is not ours. */
  }
}
function validateEvent(event: SessionEvent): void {
  switch (event.type) {
    case "message.appended":
      validateMessage(event.message);
      break;
    case "tool.result":
      validateToolResult(event.result);
      break;
    case "context.compacted":
      validateHistory(event.history);
      break;
    case "tasks.updated":
      validateTasks(event.tasks);
      break;
    case "turn.finished":
      if (!["ok", "error", "cancelled", "truncated"].includes(event.status))
        throw new SessionError("Invalid turn status.");
      break;
    case "session.named":
      if (!event.name.trim() || codePoints(event.name) > 120)
        throw new SessionError("Session name must be 1-120 characters.");
      break;
  }
}
export class Session {
  history: Message[] = [];
  tasks: Task[] = [];
  name: string | undefined;
  recoveryWarning: string | undefined;
  private closed = false;
  private failed = false;
  private queue: Promise<void> = Promise.resolve();
  constructor(
    readonly id: string,
    readonly workspace: string,
    readonly path: string,
    private readonly lockToken: string,
    private sequence = 0,
  ) {}
  async append(event: SessionEvent): Promise<void> {
    validateEvent(event);
    const snapshot = structuredClone(event);
    const work = this.queue.then(async () => {
      if (this.closed || this.failed)
        throw new SessionError("Cannot update a closed or failed session.");
      const bytes = Buffer.from(
        JSON.stringify({
          seq: this.sequence + 1,
          timestamp: utcNow(),
          ...snapshot,
        }) + "\n",
      );
      if (bytes.length - 1 > MAX_EVENT_BYTES)
        throw new SessionError("Session event exceeds the 2 MB safety limit.");
      try {
        const handle = await fs.open(this.path, "a");
        try {
          await handle.writeFile(bytes);
          await handle.sync();
        } finally {
          await handle.close();
        }
      } catch {
        this.failed = true;
        throw new SessionError("Could not durably update session.");
      }
      this.sequence++;
      if (snapshot.type === "tasks.updated") this.tasks = snapshot.tasks;
      if (snapshot.type === "session.named") this.name = snapshot.name;
    });
    this.queue = work.catch(() => {});
    return work;
  }
  async close(): Promise<void> {
    await this.queue;
    if (this.closed) return;
    this.closed = true;
    await releaseLock(this.path.replace(/\.jsonl$/, ".lock"), this.lockToken);
  }
}
export class SessionStore {
  readonly root: string;
  constructor(root = stateRoot()) {
    this.root = path.join(path.resolve(root), "sessions");
  }
  private directory(workspace: string): string {
    return path.join(this.root, workspaceKey(workspace));
  }
  private async logs(workspace: string): Promise<string[]> {
    const dir = this.directory(workspace);
    try {
      const entries = await fs.readdir(dir);
      const files = await Promise.all(
        entries
          .filter((n) => /^[0-9a-f]{32}\.jsonl$/.test(n))
          .map(async (n) => ({
            name: path.join(dir, n),
            time: (await fs.stat(path.join(dir, n))).mtimeMs,
          })),
      );
      return files
        .sort((a, b) => b.time - a.time || a.name.localeCompare(b.name))
        .map((f) => f.name);
    } catch (e) {
      if (errorCode(e) === "ENOENT") return [];
      throw new SessionError("Could not inspect saved sessions.");
    }
  }
  private async records(
    file: string,
    workspace: string,
    repair = false,
  ): Promise<{ records: Record<string, unknown>[]; repaired: boolean }> {
    let bytes = await fs.readFile(file);
    const boundary = bytes.lastIndexOf(10) + 1;
    const repaired = boundary !== bytes.length;
    if (repaired) {
      bytes = bytes.subarray(0, boundary);
      if (repair) {
        const h = await fs.open(file, "r+");
        try {
          await h.truncate(boundary);
          await h.sync();
        } finally {
          await h.close();
        }
      }
    }
    const records: Record<string, unknown>[] = [];
    let start = 0;
    for (let i = 0; i < bytes.length; i++)
      if (bytes[i] === 10) {
        const line = bytes.subarray(start, i);
        start = i + 1;
        if (line.length > MAX_EVENT_BYTES)
          throw new SessionError(
            "Session record exceeds the 2 MB safety limit.",
          );
        try {
          const value: unknown = JSON.parse(utf8(line));
          if (!isObject(value)) throw new Error();
          records.push(value);
        } catch {
          throw new SessionError("Session record is corrupt.");
        }
      }
    const header = records[0];
    if (
      !header ||
      header["seq"] !== 0 ||
      header["type"] !== "session.created" ||
      header["version"] !== 1 ||
      header["id"] !== path.basename(file, ".jsonl") ||
      typeof header["workspace"] !== "string" ||
      normalizeWorkspace(header["workspace"]) !== normalizeWorkspace(workspace)
    )
      throw new SessionError(
        "Session header is invalid or belongs to another workspace.",
      );
    return { records, repaired };
  }
  async create(workspace: string, model: string): Promise<Session> {
    workspace = await fs.realpath(workspace);
    const dir = this.directory(workspace);
    await fs.mkdir(dir, { recursive: true });
    const id = newId();
    const file = path.join(dir, `${id}.jsonl`);
    const lock = file.replace(/\.jsonl$/, ".lock");
    const token = await acquireLock(lock);
    try {
      await atomicWrite(
        file,
        Buffer.from(
          JSON.stringify({
            seq: 0,
            timestamp: utcNow(),
            type: "session.created",
            version: 1,
            id,
            workspace,
            model,
          }) + "\n",
        ),
        { exclusive: true },
      );
      return new Session(id, workspace, file, token);
    } catch {
      await releaseLock(lock, token);
      throw new SessionError("Could not create session.");
    }
  }
  async resume(id: string, workspace: string): Promise<Session> {
    workspace = await fs.realpath(workspace);
    let normalized: string;
    try {
      normalized = validId(id);
    } catch {
      throw new SessionError("Session ID must be 32 hexadecimal characters.");
    }
    const file = path.join(this.directory(workspace), `${normalized}.jsonl`);
    try {
      await fs.access(file);
    } catch {
      throw new SessionError("Session not found in this workspace.");
    }
    const lock = file.replace(/\.jsonl$/, ".lock");
    const token = await acquireLock(lock);
    try {
      const { records, repaired } = await this.records(file, workspace, true);
      const history: Message[] = [];
      let pending: string[] = [];
      let sequence = 0;
      let results: ToolResultBlock[] | undefined;
      let tasks: Task[] = [];
      let name: string | undefined;
      for (const record of records.slice(1)) {
        if (record["seq"] !== sequence + 1)
          throw new SessionError("Session event sequence is invalid.");
        sequence++;
        switch (record["type"]) {
          case "message.appended": {
            if (pending.length)
              throw new SessionError("Message precedes unfinished tools.");
            const message = validateMessage(record["message"]);
            history.push(message);
            results = undefined;
            if (message.role === "assistant")
              pending = message.content
                .filter((b) => b.type === "tool_use")
                .map((b) => b.id);
            break;
          }
          case "tool.result": {
            const result = validateToolResult(record["result"]);
            if (!pending.includes(result.tool_use_id))
              throw new SessionError("Unexpected or duplicate tool result.");
            if (!results) {
              results = [];
              history.push({ role: "user", content: results });
            }
            results.push(result);
            pending = pending.filter((id) => id !== result.tool_use_id);
            break;
          }
          case "turn.finished":
            if (
              !["ok", "error", "cancelled", "truncated"].includes(
                String(record["status"]),
              )
            )
              throw new SessionError("Invalid turn status.");
            break;
          case "tasks.updated":
            tasks = validateTasks(record["tasks"]);
            break;
          case "session.named":
            if (
              typeof record["name"] !== "string" ||
              !record["name"].trim() ||
              codePoints(record["name"]) > 120
            )
              throw new SessionError("Invalid session name.");
            name = record["name"];
            break;
          case "context.compacted":
            if (pending.length)
              throw new SessionError("Compaction precedes unfinished tools.");
            history.splice(
              0,
              history.length,
              ...validateHistory(record["history"]),
            );
            results = undefined;
            break;
          default:
            throw new SessionError("Unsupported session event.");
        }
      }
      const session = new Session(normalized, workspace, file, token, sequence);
      session.history = history;
      session.tasks = tasks;
      session.name = name;
      const warnings: string[] = [];
      if (repaired) warnings.push("a partial final log record was discarded");
      if (pending.length) {
        if (!results) {
          results = [];
          history.push({ role: "user", content: results });
        }
        for (const id of pending) {
          const result: ToolResultBlock = {
            type: "tool_result",
            tool_use_id: id,
            content:
              "The previous process ended before this tool result was saved. The tool may have partially executed; inspect the workspace before retrying.",
            is_error: true,
          };
          await session.append({ type: "tool.result", result });
          results.push(result);
        }
        warnings.push(
          "unfinished tool calls were marked as uncertain and were not replayed",
        );
      }
      if (warnings.length) session.recoveryWarning = warnings.join("; ") + ".";
      return session;
    } catch (e) {
      await releaseLock(lock, token);
      throw e;
    }
  }
  async continueLatest(workspace: string): Promise<Session> {
    const logs = await this.logs(await fs.realpath(workspace));
    if (!logs[0])
      throw new SessionError("No saved session exists for this workspace.");
    return this.resume(path.basename(logs[0], ".jsonl"), workspace);
  }
  async list(workspace: string): Promise<SessionInfo[]> {
    workspace = await fs.realpath(workspace);
    const infos: SessionInfo[] = [];
    for (const file of await this.logs(workspace)) {
      const { records } = await this.records(file, workspace);
      let first = "",
        name: string | undefined;
      for (const r of records.slice(1)) {
        if (r["type"] === "message.appended") {
          const m = validateMessage(r["message"]);
          if (m.role === "user" && typeof m.content === "string" && !first)
            first = clip(m.content.replace(/\s+/g, " "), 120);
        }
        if (r["type"] === "session.named" && typeof r["name"] === "string")
          name = r["name"];
      }
      const last = records.at(-1);
      infos.push({
        id: path.basename(file, ".jsonl"),
        updated_at: String(last?.["timestamp"] ?? ""),
        model: String(records[0]?.["model"] ?? ""),
        first_request: first,
        ...(name ? { name } : {}),
      });
    }
    return infos;
  }
  async rename(id: string, workspace: string, name: string): Promise<void> {
    const session = await this.resume(id, workspace);
    try {
      await session.append({ type: "session.named", name: name.trim() });
    } finally {
      await session.close();
    }
  }
  async search(
    workspace: string,
    query: string,
    maxResults = 20,
  ): Promise<SessionMatch[]> {
    if (!query.trim() || !Number.isSafeInteger(maxResults) || maxResults <= 0)
      throw new SessionError(
        "Search needs a non-empty query and positive result limit.",
      );
    workspace = await fs.realpath(workspace);
    const matches: SessionMatch[] = [];
    const needle = query.toLowerCase();
    for (const file of await this.logs(workspace)) {
      const { records } = await this.records(file, workspace);
      for (const r of records.slice(1).reverse()) {
        if (r["type"] !== "message.appended") continue;
        const m = validateMessage(r["message"]);
        const texts =
          typeof m.content === "string"
            ? [m.content]
            : m.role === "assistant"
              ? m.content.filter((b) => b.type === "text").map((b) => b.text)
              : [];
        for (const text of texts) {
          const offset = text.toLowerCase().indexOf(needle);
          if (offset < 0) continue;
          matches.push({
            id: path.basename(file, ".jsonl"),
            timestamp: String(r["timestamp"] ?? ""),
            role: m.role,
            excerpt: text
              .slice(Math.max(0, offset - 80), offset + query.length + 120)
              .replace(/\s+/g, " "),
          });
          if (matches.length >= maxResults) return matches;
        }
      }
    }
    return matches;
  }
}
