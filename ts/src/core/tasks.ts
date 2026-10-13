import {
  MClaudeError,
  SessionError,
  isObject,
  codePoints,
  type Task,
  type HistorySink,
} from "./types.js";
export function validateTasks(value: unknown): Task[] {
  if (!Array.isArray(value) || value.length > 100)
    throw new MClaudeError("Tasks must be a list with at most 100 steps.");
  const ids = new Set<string>();
  return value.map((item: unknown): Task => {
    if (
      !isObject(item) ||
      Object.keys(item).sort().join(",") !== "description,id,status"
    )
      throw new MClaudeError("Each task needs id, description and status.");
    for (const [key, limit] of [
      ["id", 64],
      ["description", 1000],
      ["status", 20],
    ] as const) {
      const v = item[key];
      if (typeof v !== "string" || !v.trim() || codePoints(v) > limit)
        throw new MClaudeError(`Invalid task ${key}.`);
    }
    if (
      ids.has(String(item["id"])) ||
      !["pending", "in_progress", "completed", "blocked"].includes(
        String(item["status"]),
      )
    )
      throw new MClaudeError("Invalid or duplicate task ID/status.");
    ids.add(String(item["id"]));
    return {
      id: String(item["id"]),
      description: String(item["description"]),
      status: item["status"] as Task["status"],
    };
  });
}
export class TaskBoard {
  tasks: Task[] = [];
  render(): string {
    return JSON.stringify(this.tasks);
  }
  async update(value: unknown, sink?: HistorySink): Promise<void> {
    const updated = validateTasks(value);
    try {
      await sink?.({ type: "tasks.updated", tasks: updated });
    } catch {
      throw new SessionError("Could not persist task state.");
    }
    this.tasks = updated;
  }
}
