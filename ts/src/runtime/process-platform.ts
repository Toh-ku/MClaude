import { spawn } from "node:child_process";
import { errorCode } from "../core/types.js";
export function pidIsRunning(pid: number): boolean {
  if (!Number.isSafeInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    return errorCode(e) !== "ESRCH" && errorCode(e) !== "EINVAL";
  }
}
export async function terminateTree(pid: number): Promise<void> {
  if (process.platform === "win32") {
    await new Promise<void>((resolve) => {
      const child = spawn("taskkill", ["/PID", String(pid), "/T", "/F"], {
        windowsHide: true,
        stdio: "ignore",
      });
      const timeout = setTimeout(() => {
        child.kill();
        resolve();
      }, 10000);
      child.once("error", () => {
        clearTimeout(timeout);
        resolve();
      });
      child.once("close", () => {
        clearTimeout(timeout);
        resolve();
      });
    });
  } else {
    try {
      process.kill(-pid, "SIGKILL");
      return;
    } catch {
      /* May have exited before a group was established. */
    }
  }
  if (pidIsRunning(pid)) {
    try {
      process.kill(pid, "SIGKILL");
    } catch {
      /* Already exited. */
    }
  }
}
