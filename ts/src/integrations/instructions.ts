import * as fs from "node:fs/promises";
import path from "node:path";
import { WorkspaceFS, readBounded, utf8 } from "../security/workspace.js";
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
    system: sources.length
      ? [
          "Follow the project instructions below. Later, more narrowly scoped files take precedence when instructions conflict. Each section states its source and scope.",
          ...sources.map(
            (s) =>
              `\n<project-instructions source=${JSON.stringify(s.path)} scope=${JSON.stringify(s.scope)}>\n${s.content}\n</project-instructions>`,
          ),
        ].join("\n")
      : "",
  };
}
