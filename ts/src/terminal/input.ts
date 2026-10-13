import * as readline from "node:readline";
import { StringDecoder } from "node:string_decoder";
import { ConfigurationError, TurnCancelled } from "../core/types.js";
export class InputService {
  private readonly rl: readline.Interface;
  private lines: string[] = [];
  private ended = false;
  private waiter:
    | {
        resolve: (line: string | undefined) => void;
        reject: (reason: unknown) => void;
      }
    | undefined;
  constructor(onInterrupt: () => void) {
    this.rl = readline.createInterface({
      input: process.stdin,
      output: process.stderr,
      terminal: !!process.stdin.isTTY && !!process.stderr.isTTY,
    });
    this.rl.on("line", (line) => {
      if (this.waiter) {
        const waiter = this.waiter;
        this.waiter = undefined;
        waiter.resolve(line);
      } else this.lines.push(line);
    });
    this.rl.on("close", () => {
      this.ended = true;
      this.waiter?.resolve(undefined);
      this.waiter = undefined;
    });
    this.rl.on("SIGINT", onInterrupt);
  }
  async question(
    prompt: string,
    signal?: AbortSignal,
  ): Promise<string | undefined> {
    if (signal?.aborted) throw new TurnCancelled();
    process.stderr.write(prompt);
    const queued = this.lines.shift();
    if (queued !== undefined) return queued;
    if (this.ended) return undefined;
    return new Promise((resolve, reject) => {
      const abort = (): void => {
        this.waiter = undefined;
        reject(new TurnCancelled());
      };
      this.waiter = {
        resolve: (line) => {
          signal?.removeEventListener("abort", abort);
          resolve(line);
        },
        reject,
      };
      signal?.addEventListener("abort", abort, { once: true });
    });
  }
  close(): void {
    this.rl.close();
  }
}
export async function hiddenInput(prompt: string): Promise<string> {
  const stream = process.stdin;
  if (
    !stream.isTTY ||
    !process.stderr.isTTY ||
    typeof stream.setRawMode !== "function"
  )
    throw new ConfigurationError(
      "Login requires a real terminal; secret input cannot fall back to echo.",
    );
  process.stderr.write(prompt);
  const wasRaw = stream.isRaw,
    wasPaused = stream.isPaused();
  stream.setRawMode(true);
  stream.resume();
  const decoder = new StringDecoder("utf8");
  return new Promise((resolve, reject) => {
    let value = "";
    let done = false;
    const cleanup = (): void => {
      stream.removeListener("data", data);
      stream.removeListener("end", end);
      stream.setRawMode(wasRaw);
      if (wasPaused) stream.pause();
      process.stderr.write("\n");
    };
    const finish = (cancel = false): void => {
      if (done) return;
      done = true;
      cleanup();
      if (cancel) reject(new TurnCancelled("Login cancelled."));
      else resolve(value);
    };
    const end = (): void => finish(true);
    const data = (chunk: Buffer): void => {
      for (const character of decoder.write(chunk)) {
        if (character === "\r" || character === "\n") {
          finish();
          return;
        }
        if (character === "\x03" || character === "\x04") {
          finish(true);
          return;
        }
        if (character === "\x7f" || character === "\b")
          value = Array.from(value).slice(0, -1).join("");
        else if (character >= " " && character !== "\x7f") value += character;
      }
    };
    stream.on("data", data);
    stream.once("end", end);
  });
}
