import os from "node:os";
import path from "node:path";
import { createHash, randomUUID } from "node:crypto";
export function expandHome(value: string): string {
  return value === "~"
    ? os.homedir()
    : value.startsWith("~/") || value.startsWith("~\\")
      ? path.join(os.homedir(), value.slice(2))
      : value;
}
export function stateRoot(env: NodeJS.ProcessEnv = process.env): string {
  if (env["MCLAUDE_STATE_DIR"])
    return path.resolve(expandHome(env["MCLAUDE_STATE_DIR"]));
  if (process.platform === "win32")
    return path.join(
      env["LOCALAPPDATA"] || path.join(os.homedir(), "AppData", "Local"),
      "MClaude",
    );
  return path.join(
    expandHome(
      env["XDG_STATE_HOME"] || path.join(os.homedir(), ".local", "state"),
    ),
    "mclaude",
  );
}
export function configPath(env: NodeJS.ProcessEnv = process.env): string {
  if (env["MCLAUDE_CONFIG_DIR"])
    return path.join(
      path.resolve(expandHome(env["MCLAUDE_CONFIG_DIR"])),
      "config.json",
    );
  if (process.platform === "win32")
    return path.join(
      env["LOCALAPPDATA"] || path.join(os.homedir(), "AppData", "Local"),
      "MClaude",
      "config.json",
    );
  return path.join(
    expandHome(env["XDG_CONFIG_HOME"] || path.join(os.homedir(), ".config")),
    "mclaude",
    "config.json",
  );
}
export function normalizeWorkspace(
  value: string,
  platform = process.platform,
): string {
  const resolved = path.resolve(value);
  return platform === "win32"
    ? resolved.replaceAll("/", "\\").toLowerCase()
    : resolved;
}
export function workspaceKey(workspace: string): string {
  return createHash("sha256")
    .update(normalizeWorkspace(workspace), "utf8")
    .digest("hex")
    .slice(0, 20);
}
export const newId = (): string => randomUUID().replaceAll("-", "");
export const digest = (bytes: Uint8Array): string =>
  createHash("sha256").update(bytes).digest("hex");
export const utcNow = (): string =>
  new Date().toISOString().replace("Z", "000+00:00");
export function validId(id: string): string {
  const value = id.trim().toLowerCase();
  if (!/^[0-9a-f]{32}$/.test(value))
    throw new Error("ID must be 32 hexadecimal characters.");
  return value;
}
