import * as fs from "node:fs/promises";
import type { Dirent } from "node:fs";
import path from "node:path";
import ignore, { type Ignore } from "ignore";
import {
  WorkspaceFS,
  inside,
  readBounded,
  utf8,
} from "../../security/workspace.js";
import {
  MClaudeError,
  TurnCancelled,
  checkCancelled,
  codePoints,
  clip,
  ok,
  type ToolResult,
} from "../../core/types.js";
import { stringInput } from "../definitions.js";
import { RegexSearch } from "../../runtime/regex-search.js";
interface IgnoreLayer {
  base: string;
  matcher: Ignore;
}
export async function* workspaceFiles(
  workspace: WorkspaceFS,
  signal?: AbortSignal,
): AsyncGenerator<string> {
  async function* walk(
    directory: string,
    layers: IgnoreLayer[],
  ): AsyncGenerator<string> {
    checkCancelled(signal);
    const ignoreFile = path.join(directory, ".gitignore");
    const current = [...layers];
    try {
      const stat = await fs.lstat(ignoreFile);
      if (stat.isFile() && !stat.isSymbolicLink())
        current.push({
          base: directory,
          matcher: ignore().add(utf8(await readBounded(ignoreFile, 1000000))),
        });
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code !== "ENOENT") return;
    }
    let entries: Dirent[];
    try {
      entries = await fs.readdir(directory, { withFileTypes: true });
    } catch {
      return;
    }
    entries.sort((a, b) =>
      a.name.toLowerCase() < b.name.toLowerCase()
        ? -1
        : a.name.toLowerCase() > b.name.toLowerCase()
          ? 1
          : 0,
    );
    for (const entry of entries) {
      checkCancelled(signal);
      if (entry.name === ".git") continue;
      const file = path.join(directory, entry.name);
      let resolved: string;
      try {
        resolved = await workspace.resolve(file);
      } catch {
        continue;
      }
      let isDirectory = entry.isDirectory();
      let isFile = entry.isFile();
      if (entry.isSymbolicLink()) {
        try {
          const stat = await fs.stat(resolved);
          isDirectory = stat.isDirectory();
          isFile = stat.isFile();
        } catch {
          continue;
        }
      }
      let ignored = false;
      for (const layer of current) {
        const relative =
          path.relative(layer.base, file).split(path.sep).join("/") +
          (isDirectory ? "/" : "");
        const result = layer.matcher.test(relative);
        if (result.ignored) ignored = true;
        else if (result.unignored) ignored = false;
      }
      if (ignored) continue;
      if (isDirectory && !entry.isSymbolicLink()) yield* walk(file, current);
      else if (isFile) yield file;
    }
  }
  yield* walk(workspace.root, []);
}
export async function readFile(
  input: Record<string, unknown>,
  workspace: WorkspaceFS,
  signal?: AbortSignal,
  maxChars = 100000,
): Promise<ToolResult> {
  const file = await workspace.resolve(stringInput(input, "path"));
  const h = await fs.open(file, "r");
  try {
    if (!(await h.stat()).isFile())
      throw new MClaudeError("Path is not a file.");
    const decoder = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });
    const chunks: string[] = [];
    let count = 0;
    while (count <= maxChars) {
      checkCancelled(signal);
      const buffer = Buffer.alloc(Math.min(65536, (maxChars + 1 - count) * 4));
      const { bytesRead } = await h.read(buffer);
      const text = decoder.decode(buffer.subarray(0, bytesRead), {
        stream: bytesRead > 0,
      });
      chunks.push(text);
      count += codePoints(text);
      if (!bytesRead) break;
    }
    let text = chunks.join("").replace(/\r\n?/g, "\n");
    if (codePoints(text) > maxChars)
      text =
        clip(text, maxChars) +
        `\n\n[Output truncated after ${maxChars} characters.]`;
    return ok(text);
  } finally {
    await h.close();
  }
}
export async function findFiles(
  input: Record<string, unknown>,
  workspace: WorkspaceFS,
  signal?: AbortSignal,
  maxResults = 200,
): Promise<ToolResult> {
  const pattern = stringInput(input, "pattern");
  const matcher = ignore().add(pattern);
  const matches: string[] = [];
  for await (const file of workspaceFiles(workspace, signal)) {
    const relative = workspace.display(file);
    if (!matcher.ignores(relative)) continue;
    if (matches.length === maxResults) {
      matches.push(`[Results truncated after ${maxResults} files.]`);
      break;
    }
    matches.push(relative);
  }
  return ok(
    matches.length
      ? matches.join("\n")
      : `No files matched pattern: ${pattern}`,
  );
}
export async function searchText(
  input: Record<string, unknown>,
  workspace: WorkspaceFS,
  signal?: AbortSignal,
  maxResults = 200,
): Promise<ToolResult> {
  const query = stringInput(input, "query");
  if (input["is_regex"] === true && codePoints(query) > 8000)
    throw new MClaudeError("Regular expression exceeds 8,000 characters.");
  const scope = await workspace.resolve(
    typeof input["path"] === "string" ? input["path"] : ".",
  );
  const stat = await fs.stat(scope);
  if (!stat.isFile() && !stat.isDirectory())
    throw new MClaudeError("Search path is not a file or directory.");
  let regex: RegExp;
  try {
    if (
      input["is_regex"] === true &&
      /\(\?P[<=]|\(\?[aimsx-]+\)|\\[AZ]/.test(query)
    )
      throw new Error();
    regex = new RegExp(
      input["is_regex"] === true
        ? query
        : query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"),
      input["case_sensitive"] === false ? "iu" : "u",
    );
  } catch {
    throw new MClaudeError(
      "Invalid or unsupported regular expression; use JavaScript Unicode regular expression syntax.",
    );
  }
  const matches: string[] = [];
  let truncated = false;
  const regexWorker =
    input["is_regex"] === true
      ? new RegexSearch(regex.source, regex.flags)
      : undefined;
  try {
    for await (const file of workspaceFiles(workspace, signal)) {
      const resolved = await workspace.resolve(file);
      if (stat.isFile() ? resolved !== scope : !inside(scope, resolved))
        continue;
      let content: string;
      try {
        content = utf8(await readBounded(file, 1000000, signal));
      } catch (e) {
        if (e instanceof TurnCancelled) throw e;
        continue;
      }
      const lines = content.split(/\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/u);
      if (regexWorker) {
        const found = await regexWorker.search(
          lines,
          maxResults - matches.length + 1,
          signal,
        );
        for (const match of found) {
          if (matches.length === maxResults) {
            truncated = true;
            break;
          }
          matches.push(
            `${workspace.display(file)}:${match.line}:${codePoints(match.text) > 500 ? clip(match.text, 500) + "…" : match.text}`,
          );
        }
        if (truncated) break;
        continue;
      }
      for (let i = 0; i < lines.length; i++) {
        checkCancelled(signal);
        const line = lines[i] ?? "";
        if (!regex.test(line)) continue;
        if (matches.length === maxResults) {
          truncated = true;
          break;
        }
        matches.push(
          `${workspace.display(file)}:${i + 1}:${codePoints(line) > 500 ? clip(line, 500) + "…" : line}`,
        );
      }
      if (truncated) break;
    }
  } finally {
    await regexWorker?.close();
  }
  if (truncated)
    matches.push(`[Results truncated after ${maxResults} matches.]`);
  return ok(
    matches.length ? matches.join("\n") : `No matches found for: ${query}`,
  );
}
