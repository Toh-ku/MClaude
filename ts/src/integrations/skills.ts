import * as fs from "node:fs/promises";
import path from "node:path";
import { WorkspaceFS, readBounded, utf8 } from "../security/workspace.js";
import {
  MClaudeError,
  codePoints,
  ok,
  type ToolResult,
} from "../core/types.js";
interface Skill {
  name: string;
  description: string;
  path: string;
}
async function metadata(
  file: string,
): Promise<{ name: string; description: string }> {
  const handle = await fs.open(file, "r");
  let text: string;
  try {
    const bytes = Buffer.alloc(33000);
    const { bytesRead } = await handle.read(bytes);
    text = new TextDecoder("utf8", { fatal: true }).decode(
      bytes.subarray(0, bytesRead),
      { stream: true },
    );
  } finally {
    await handle.close();
  }
  const lines = text.split(/\r?\n/);
  if (lines.shift()?.trim() !== "---")
    throw new MClaudeError("Missing skill frontmatter.");
  const data: Record<string, unknown> = {};
  let size = 0,
    ended = false;
  for (const line of lines) {
    size += codePoints(line) + 1;
    if (size > 8192) throw new MClaudeError("Invalid skill frontmatter.");
    if (line.trim() === "---") {
      ended = true;
      break;
    }
    if (!line.trim() || line.trimStart().startsWith("#")) continue;
    const separator = line.indexOf(":");
    const key = line.slice(0, separator),
      value = line.slice(separator + 1).trim();
    if (separator < 0 || !["name", "description"].includes(key) || key in data)
      throw new MClaudeError("Expected unique name and description fields.");
    data[key] = value.startsWith('"')
      ? JSON.parse(value)
      : value.startsWith("'") && value.endsWith("'")
        ? value.slice(1, -1).replaceAll("''", "'")
        : value;
  }
  if (
    !ended ||
    typeof data["name"] !== "string" ||
    !/^[a-z0-9][a-z0-9-]{0,63}$/.test(data["name"]) ||
    typeof data["description"] !== "string" ||
    codePoints(data["description"]) < 1 ||
    codePoints(data["description"]) > 500
  )
    throw new MClaudeError("Invalid skill metadata.");
  return { name: data["name"], description: data["description"] };
}
export class SkillCatalog {
  readonly skills = new Map<string, Skill>();
  readonly warnings: string[] = [];
  private constructor(private readonly workspace: WorkspaceFS) {}
  static async discover(workspace: WorkspaceFS): Promise<SkillCatalog> {
    const catalog = new SkillCatalog(workspace);
    const directory = path.join(workspace.root, ".mclaude", "skills");
    if (!(await workspace.noLinks(directory))) return catalog;
    let names: string[];
    try {
      names = await fs.readdir(directory);
    } catch {
      return catalog;
    }
    for (const folder of names.sort()) {
      if (catalog.skills.size >= 100) {
        catalog.warnings.push("Skill catalog limited to 100 entries.");
        break;
      }
      const file = path.join(directory, folder, "SKILL.md");
      if (!(await workspace.noLinks(file))) continue;
      try {
        const value = await metadata(file);
        if (catalog.skills.has(value.name))
          throw new MClaudeError("Duplicate skill name.");
        catalog.skills.set(value.name, { ...value, path: file });
      } catch {
        catalog.warnings.push(
          `Skipped ${workspace.display(file)}: invalid or unreadable skill.`,
        );
      }
    }
    return catalog;
  }
  summary(): string {
    return [...this.skills.values()]
      .map((s) => `- ${s.name}: ${s.description}`)
      .join("\n");
  }
  async load(name: string, signal?: AbortSignal): Promise<ToolResult> {
    const skill = this.skills.get(name);
    if (!skill) throw new MClaudeError("Unknown skill name.");
    if (!(await this.workspace.noLinks(skill.path)))
      throw new MClaudeError("Skill path is no longer inside the workspace.");
    const current = await metadata(skill.path);
    if (
      current.name !== skill.name ||
      current.description !== skill.description
    )
      throw new MClaudeError(
        "Skill metadata changed; rediscover on the next turn.",
      );
    const content = utf8(await readBounded(skill.path, 400004, signal));
    if (codePoints(content) > 100000)
      throw new MClaudeError("Skill exceeds 100,000 characters.");
    return ok(
      `Skill source: ${this.workspace.display(skill.path)}\n${content}`,
    );
  }
}
