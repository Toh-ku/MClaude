import * as fs from "node:fs/promises";
import path from "node:path";
import { createHash } from "node:crypto";
import { WorkspaceFS, inside } from "../security/workspace.js";
import { stateRoot, configPath } from "../persistence/paths.js";
import { workspaceFiles } from "../tools/files/read.js";
import { checkCancelled, MClaudeError } from "./types.js";

// Hash discoverable files, including untracked files, without requiring Git.
// Build artifacts follow .gitignore; agent journals/config are excluded explicitly.
export async function workspaceVersion(
  workspace: WorkspaceFS,
  signal?: AbortSignal,
): Promise<string> {
  const excluded = [
    path.join(stateRoot(), "sessions"),
    path.join(stateRoot(), "checkpoints"),
  ];
  const files: string[] = [];
  for await (const file of workspaceFiles(workspace, signal, true)) {
    const resolved = await workspace.resolve(file);
    if (
      resolved === configPath() ||
      excluded.some((dir) => inside(dir, resolved))
    )
      continue;
    files.push(file);
  }
  files.sort();
  const version = createHash("sha256");
  for (const file of files) {
    checkCancelled(signal);
    const resolved = await workspace.resolve(file);
    const handle = await fs.open(resolved, "r");
    try {
      const before = await handle.stat();
      if (!before.isFile())
        throw new MClaudeError("Versioned path is not a file.");
      const content = createHash("sha256");
      const buffer = Buffer.alloc(65536);
      while (true) {
        checkCancelled(signal);
        const { bytesRead } = await handle.read(buffer);
        if (!bytesRead) break;
        content.update(buffer.subarray(0, bytesRead));
      }
      const after = await handle.stat();
      if (
        before.size !== after.size ||
        before.mtimeMs !== after.mtimeMs ||
        before.ctimeMs !== after.ctimeMs
      )
        throw new MClaudeError(
          "Workspace changed while computing its version.",
        );
      version.update(
        JSON.stringify([
          workspace.display(file),
          before.mode & 0o7777,
          content.digest("hex"),
        ]) + "\n",
      );
    } finally {
      await handle.close();
    }
  }
  return version.digest("hex");
}
