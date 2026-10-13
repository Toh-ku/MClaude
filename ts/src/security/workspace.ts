import * as fs from "node:fs/promises";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { MClaudeError, checkCancelled, errorCode } from "../core/types.js";

export const utf8 = (bytes: Uint8Array): string =>
  new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
export function inside(root: string, target: string): boolean {
  const relative = path.relative(root, target);
  return (
    relative === "" ||
    (!relative.startsWith(`..${path.sep}`) &&
      relative !== ".." &&
      !path.isAbsolute(relative))
  );
}
export async function canonicalPath(value: string): Promise<string> {
  const absolute = path.resolve(value);
  try {
    return await fs.realpath(absolute);
  } catch (e) {
    if (errorCode(e) !== "ENOENT") throw e;
    const parent = path.dirname(absolute);
    if (parent === absolute) throw e;
    return path.join(await canonicalPath(parent), path.basename(absolute));
  }
}
export async function readBounded(
  file: string,
  maxBytes: number,
  signal?: AbortSignal,
): Promise<Buffer> {
  checkCancelled(signal);
  const handle = await fs.open(file, "r");
  try {
    if (!(await handle.stat()).isFile())
      throw new MClaudeError("Expected a regular file.");
    const chunks: Buffer[] = [];
    let size = 0;
    while (true) {
      checkCancelled(signal);
      const buffer = Buffer.alloc(Math.min(65536, maxBytes + 1 - size));
      const { bytesRead } = await handle.read(buffer);
      if (!bytesRead) break;
      chunks.push(buffer.subarray(0, bytesRead));
      size += bytesRead;
      if (size > maxBytes)
        throw new MClaudeError(`File exceeds ${maxBytes} bytes.`);
    }
    return Buffer.concat(chunks);
  } finally {
    await handle.close();
  }
}
export async function atomicWrite(
  file: string,
  bytes: Uint8Array,
  options: { mode?: number; expected?: Buffer; exclusive?: boolean } = {},
): Promise<void> {
  if (options.exclusive) {
    const handle = await fs.open(file, "wx", options.mode ?? 0o600);
    try {
      await handle.writeFile(bytes);
      await handle.sync();
    } finally {
      await handle.close();
    }
    return;
  }
  const temporary = path.join(
    path.dirname(file),
    `.${path.basename(file)}.${randomUUID()}.tmp`,
  );
  try {
    const handle = await fs.open(temporary, "wx", options.mode ?? 0o600);
    try {
      await handle.writeFile(bytes);
      await handle.sync();
    } finally {
      await handle.close();
    }
    if (options.expected && !(await fs.readFile(file)).equals(options.expected))
      throw new MClaudeError(
        "File changed during edit; refusing to overwrite it.",
      );
    await fs.rename(temporary, file);
  } finally {
    await fs.unlink(temporary).catch(() => {});
  }
}
export class WorkspaceFS {
  private constructor(readonly root: string) {}
  static async create(root: string): Promise<WorkspaceFS> {
    return new WorkspaceFS(await fs.realpath(root));
  }
  async resolve(value: string): Promise<string> {
    if (!value || value.includes("\0"))
      throw new MClaudeError("Invalid workspace path.");
    const requested = path.resolve(this.root, value);
    if (!inside(this.root, requested))
      throw new MClaudeError("Path is outside the workspace.");
    const resolved = await canonicalPath(requested);
    if (!inside(this.root, resolved))
      throw new MClaudeError("Path is outside the workspace.");
    return resolved;
  }
  display(value: string): string {
    return path.relative(this.root, value).split(path.sep).join("/");
  }
  async prepareParent(file: string): Promise<void> {
    await this.resolve(file);
    await fs.mkdir(path.dirname(file), { recursive: true });
    await this.resolve(file);
  }
  async noLinks(file: string): Promise<boolean> {
    if (!inside(this.root, path.resolve(file))) return false;
    for (
      let current = path.resolve(file);
      current !== this.root;
      current = path.dirname(current)
    ) {
      try {
        if ((await fs.lstat(current)).isSymbolicLink()) return false;
      } catch (e) {
        if (errorCode(e) !== "ENOENT") return false;
      }
    }
    return inside(this.root, await canonicalPath(file));
  }
}
