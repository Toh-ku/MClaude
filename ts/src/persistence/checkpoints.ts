import * as fs from "node:fs/promises";
import path from "node:path";
import { CheckpointError, errorCode, isObject } from "../core/types.js";
import {
  WorkspaceFS,
  atomicWrite,
  readBounded,
  utf8,
} from "../security/workspace.js";
import {
  stateRoot,
  workspaceKey,
  normalizeWorkspace,
  newId,
  utcNow,
  digest,
  validId,
} from "./paths.js";
interface CheckpointRecord {
  version: 1;
  id: string;
  workspace: string;
  path: string;
  created_at: string;
  before: string | null;
  before_mode: number | null;
  after_sha256: string;
  restored: boolean;
  restored_at?: string;
}
export interface EditCheckpoint {
  id: string;
  path: string;
  created_at: string;
  restored: boolean;
}
export class CheckpointStore {
  readonly directory: string;
  constructor(
    readonly workspace: WorkspaceFS,
    root = stateRoot(),
  ) {
    this.directory = path.join(
      path.resolve(root),
      "checkpoints",
      workspaceKey(workspace.root),
    );
  }
  async save(
    file: string,
    before: Buffer | null,
    after: Buffer,
  ): Promise<string> {
    const resolved = await this.workspace.resolve(file);
    const id = newId();
    const record: CheckpointRecord = {
      version: 1,
      id,
      workspace: this.workspace.root,
      path: this.workspace.display(resolved),
      created_at: utcNow(),
      before: before?.toString("base64") ?? null,
      before_mode: before === null ? null : (await fs.stat(resolved)).mode,
      after_sha256: digest(after),
      restored: false,
    };
    try {
      await fs.mkdir(this.directory, { recursive: true });
      await atomicWrite(
        path.join(this.directory, `${id}.json`),
        Buffer.from(JSON.stringify(record) + "\n"),
        { exclusive: true },
      );
      return id;
    } catch {
      throw new CheckpointError("Could not save edit checkpoint.");
    }
  }
  async discard(id: string): Promise<void> {
    if (/^[0-9a-f]{32}$/.test(id))
      await fs.unlink(path.join(this.directory, `${id}.json`)).catch(() => {});
  }
  private async read(file: string): Promise<CheckpointRecord> {
    let value: unknown;
    try {
      value = JSON.parse(utf8(await readBounded(file, 3000000)));
    } catch {
      throw new CheckpointError("Could not read edit checkpoint.");
    }
    if (
      !isObject(value) ||
      value["version"] !== 1 ||
      value["id"] !== path.basename(file, ".json") ||
      typeof value["workspace"] !== "string" ||
      normalizeWorkspace(value["workspace"]) !==
        normalizeWorkspace(this.workspace.root) ||
      typeof value["path"] !== "string" ||
      !value["path"] ||
      typeof value["created_at"] !== "string" ||
      typeof value["restored"] !== "boolean" ||
      !(value["before"] === null || typeof value["before"] === "string") ||
      !(
        value["before_mode"] === null ||
        (Number.isSafeInteger(value["before_mode"]) &&
          Number(value["before_mode"]) >= 0)
      ) ||
      typeof value["after_sha256"] !== "string" ||
      !/^[0-9a-f]{64}$/.test(value["after_sha256"])
    )
      throw new CheckpointError(
        "Edit checkpoint is invalid or belongs to another workspace.",
      );
    return value as unknown as CheckpointRecord;
  }
  async list(): Promise<EditCheckpoint[]> {
    let names: string[];
    try {
      names = await fs.readdir(this.directory);
    } catch (e) {
      if (errorCode(e) === "ENOENT") return [];
      throw new CheckpointError("Could not list checkpoints.");
    }
    const records = await Promise.all(
      names
        .filter((n) => /^[0-9a-f]{32}\.json$/.test(n))
        .map((n) => this.read(path.join(this.directory, n))),
    );
    return records
      .sort((a, b) => b.created_at.localeCompare(a.created_at))
      .map((r) => ({
        id: r.id,
        path: r.path,
        created_at: r.created_at,
        restored: r.restored,
      }));
  }
  async restore(id: string): Promise<string> {
    try {
      id = validId(id);
    } catch {
      throw new CheckpointError(
        "Checkpoint ID must be 32 hexadecimal characters.",
      );
    }
    const file = path.join(this.directory, `${id}.json`);
    const record = await this.read(file);
    if (record.restored)
      throw new CheckpointError(
        "This edit checkpoint has already been restored.",
      );
    const recordedTarget = path.join(this.workspace.root, record.path);
    if (!(await this.workspace.noLinks(recordedTarget)))
      throw new CheckpointError(
        "Checkpoint conflict: the target path was replaced by a link.",
      );
    const target = await this.workspace.resolve(record.path);
    let current: Buffer;
    try {
      current = await readBounded(target, 1000000);
    } catch {
      throw new CheckpointError(
        "Checkpoint conflict: the edited file is missing or unreadable.",
      );
    }
    if (digest(current) !== record.after_sha256)
      throw new CheckpointError(
        "Checkpoint conflict: the file changed after the Agent edit; refusing to overwrite it.",
      );
    if (record.before === null) {
      await this.workspace.resolve(target);
      if (!(await fs.readFile(target)).equals(current))
        throw new CheckpointError("Checkpoint conflict during restore.");
      await fs.unlink(target);
    } else {
      if (
        !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(
          record.before,
        )
      )
        throw new CheckpointError("Checkpoint snapshot is invalid.");
      const before = Buffer.from(record.before, "base64");
      await this.workspace.resolve(target);
      await atomicWrite(target, before, {
        expected: current,
        ...(record.before_mode !== null
          ? { mode: record.before_mode & 0o7777 }
          : {}),
      });
    }
    record.restored = true;
    record.restored_at = utcNow();
    try {
      await atomicWrite(file, Buffer.from(JSON.stringify(record) + "\n"));
    } catch {
      throw new CheckpointError(
        "File was restored but checkpoint status could not be updated.",
      );
    }
    return `${record.before === null ? "Removed" : "Restored"} ${record.path}`;
  }
}
