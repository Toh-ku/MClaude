import { spawn } from "node:child_process";
import { StringDecoder } from "node:string_decoder";
import {
  MClaudeError,
  TurnCancelled,
  checkCancelled,
  clip,
  codePoints,
} from "../core/types.js";
import { terminateTree } from "./process-platform.js";
export interface ProcessOptions {
  command: string;
  args?: string[];
  cwd: string;
  shell?: boolean;
  timeoutSeconds?: number;
  maxOutputChars?: number;
  maxOutputBytes?: number;
  input?: string;
  signal?: AbortSignal;
}
export interface ProcessResult {
  exitCode: number | null;
  stdout: string;
  stderr: string;
  timedOut: boolean;
  truncated: boolean;
}
export async function runProcess(
  options: ProcessOptions,
): Promise<ProcessResult> {
  checkCancelled(options.signal);
  const timeout = options.timeoutSeconds ?? 120;
  const limit = options.maxOutputChars ?? 100000;
  if (
    !Number.isFinite(timeout) ||
    timeout <= 0 ||
    timeout > 600 ||
    !Number.isSafeInteger(limit) ||
    limit <= 0
  )
    throw new MClaudeError("Invalid command timeout or output limit.");
  return new Promise<ProcessResult>((resolve, reject) => {
    const child = spawn(options.command, options.args ?? [], {
      cwd: options.cwd,
      shell: options.shell ?? false,
      windowsHide: true,
      detached: process.platform !== "win32",
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "",
      stderr = "",
      timedOut = false,
      truncated = false,
      outputBytes = 0,
      overflow = false,
      settled = false,
      terminating: Promise<void> | undefined;
    const outDecoder = new StringDecoder("utf8"),
      errDecoder = new StringDecoder("utf8");
    const stop = (): void => {
      if (child.pid && !terminating) terminating = terminateTree(child.pid);
    };
    const abort = (): void => stop();
    const timer = setTimeout(() => {
      timedOut = true;
      stop();
    }, timeout * 1000);
    const cleanup = (): void => {
      clearTimeout(timer);
      options.signal?.removeEventListener("abort", abort);
    };
    const capture = (chunk: Buffer, kind: "stdout" | "stderr"): void => {
      outputBytes += chunk.length;
      if (
        options.maxOutputBytes !== undefined &&
        outputBytes > options.maxOutputBytes
      ) {
        overflow = true;
        stop();
      }
      const decoder = kind === "stdout" ? outDecoder : errDecoder;
      const text = decoder.write(chunk);
      const current = kind === "stdout" ? stdout : stderr;
      const remaining = limit - codePoints(current);
      if (codePoints(text) > remaining) truncated = true;
      const combined = current + clip(text, Math.max(0, remaining));
      if (kind === "stdout") stdout = combined;
      else stderr = combined;
    };
    child.stdout.on("data", (c: Buffer) => capture(c, "stdout"));
    child.stderr.on("data", (c: Buffer) => capture(c, "stderr"));
    child.stdin.on("error", () => {});
    child.stdin.end(options.input ?? "");
    child.once("error", () => {
      if (!settled) {
        settled = true;
        cleanup();
        reject(new MClaudeError("Could not start command."));
      }
    });
    child.once("close", (code) => {
      void (async () => {
        if (settled) return;
        settled = true;
        cleanup();
        await terminating;
        if (options.signal?.aborted) {
          reject(
            new TurnCancelled(
              "Command cancelled; it may have partially executed. Inspect the workspace.",
            ),
          );
          return;
        }
        if (overflow) {
          reject(new MClaudeError("Process output exceeded its byte limit."));
          return;
        }
        stdout += clip(
          outDecoder.end(),
          Math.max(0, limit - codePoints(stdout)),
        );
        stderr += clip(
          errDecoder.end(),
          Math.max(0, limit - codePoints(stderr)),
        );
        resolve({ exitCode: code, stdout, stderr, timedOut, truncated });
      })().catch(reject);
    });
    options.signal?.addEventListener("abort", abort, { once: true });
    if (options.signal?.aborted) abort();
  });
}
