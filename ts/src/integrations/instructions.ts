import * as fs from "node:fs/promises";
import path from "node:path";
import {
  WorkspaceFS,
  inside,
  readBounded,
  utf8,
} from "../security/workspace.js";
import { MClaudeError, codePoints, errorCode } from "../core/types.js";
export interface InstructionSource {
  path: string;
  scope: string;
  content: string;
}
export interface ProjectInstructions {
  sources: InstructionSource[];
  system: string;
}
export function renderInstructions(sources: InstructionSource[]): string {
  return sources.length
    ? [
        "Follow the project instructions below only for files within each stated scope. More narrowly scoped instructions take precedence for that file; sibling scopes never override one another. Within the same directory CLAUDE.md follows AGENTS.md.",
        ...sources.map(
          (s) =>
            `\n<project-instructions source=${JSON.stringify(s.path)} scope=${JSON.stringify(s.scope)}>\n${s.content}\n</project-instructions>`,
        ),
      ].join("\n")
    : "";
}
export async function loadInstructions(
  workspace: WorkspaceFS,
  workingDirectory = workspace.root,
): Promise<ProjectInstructions> {
  const directory = await workspace.resolve(workingDirectory);
  if (!(await fs.stat(directory)).isDirectory())
    throw new MClaudeError("Instruction scope is not a directory.");
  const dirs = [workspace.root];
  let current = workspace.root;
  for (const part of path
    .relative(workspace.root, directory)
    .split(path.sep)
    .filter(Boolean)) {
    current = path.join(current, part);
    dirs.push(current);
  }
  const sources: InstructionSource[] = [];
  for (const dir of dirs)
    for (const name of ["AGENTS.md", "CLAUDE.md"]) {
      const file = path.join(dir, name);
      try {
        if (
          !(await fs.lstat(file)).isFile() ||
          !(await workspace.noLinks(file))
        )
          continue;
        const content = utf8(await readBounded(file, 400004));
        if (codePoints(content) > 100000)
          throw new MClaudeError(
            "Project instructions exceed 100,000 characters.",
          );
        if (content.trim())
          sources.push({
            path: workspace.display(file),
            scope: workspace.display(dir) || ".",
            content,
          });
      } catch (e) {
        if (errorCode(e) === "ENOENT") continue;
        throw e instanceof MClaudeError
          ? e
          : new MClaudeError(
              `Could not read project instructions: ${workspace.display(file)}.`,
            );
      }
    }
  return {
    sources,
    system: renderInstructions(sources),
  };
}

export async function instructionsForPath(
  workspace: WorkspaceFS,
  target: string,
): Promise<ProjectInstructions> {
  const file = await workspace.resolve(target);
  let directory = file;
  try {
    if (!(await fs.stat(directory)).isDirectory())
      directory = path.dirname(directory);
  } catch (e) {
    if (errorCode(e) !== "ENOENT") throw e;
    directory = path.dirname(directory);
  }
  // New files can have parents that do not exist yet.
  while (directory !== workspace.root) {
    try {
      if ((await fs.stat(directory)).isDirectory()) break;
      throw new MClaudeError("Instruction ancestor is not a directory.");
    } catch (e) {
      if (errorCode(e) !== "ENOENT") throw e;
      directory = path.dirname(directory);
    }
  }
  return loadInstructions(workspace, directory);
}

export class InstructionTracker {
  private readonly targets = new Set<string>();
  private delivered = new Map<string, string>();
  constructor(
    private readonly workspace: WorkspaceFS,
    private readonly initial: ProjectInstructions,
  ) {
    for (const source of initial.sources) this.targets.add(source.scope);
  }

  async beginRequest(): Promise<string> {
    const sources = new Map<string, InstructionSource>();
    const root = await loadInstructions(this.workspace);
    // Reload disk sources rather than retaining removed or changed rules.
    for (const source of root.sources) sources.set(source.path, source);
    for (const target of this.targets) {
      const applicable = await instructionsForPath(this.workspace, target);
      for (const source of applicable.sources) sources.set(source.path, source);
    }
    const depth = (scope: string): number =>
      scope === "." ? 0 : scope.split("/").length;
    const ordered = [...sources.values()].sort(
      (a, b) => depth(a.scope) - depth(b.scope) || a.path.localeCompare(b.path),
    );
    this.delivered = new Map(ordered.map((s) => [s.path, s.content]));
    // Preserve explicit injected prompts when they have no filesystem sources.
    return (
      renderInstructions(ordered) +
      (!this.initial.sources.length ? this.initial.system : "")
    );
  }

  async inspect(targets: string[]): Promise<string | undefined> {
    const unseen = new Map<string, InstructionSource>();
    const removed = new Set<string>();
    for (const target of targets) {
      const applicable = await instructionsForPath(this.workspace, target);
      this.targets.add(target);
      const current = new Set(applicable.sources.map((source) => source.path));
      const resolved = await this.workspace.resolve(target);
      for (const source of this.delivered.keys()) {
        if (
          !current.has(source) &&
          inside(path.dirname(path.join(this.workspace.root, source)), resolved)
        )
          removed.add(source);
      }
      for (const source of applicable.sources)
        if (this.delivered.get(source.path) !== source.content)
          unseen.set(source.path, source);
    }
    return unseen.size || removed.size
      ? [...removed]
          .map((source) => `Previously supplied rule removed: ${source}`)
          .join("\n") +
          "\n" +
          renderInstructions([...unseen.values()])
      : undefined;
  }
}
