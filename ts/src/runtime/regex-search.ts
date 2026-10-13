import { Worker } from "node:worker_threads";
import { MClaudeError, TurnCancelled, checkCancelled } from "../core/types.js";
export interface LineMatch {
  line: number;
  text: string;
}
export class RegexSearch {
  private readonly worker: Worker;
  constructor(source: string, flags: string) {
    this.worker = new Worker(new URL("./regex-worker.js", import.meta.url), {
      workerData: { source, flags },
      resourceLimits: { maxOldGenerationSizeMb: 32 },
    });
  }
  async search(
    lines: string[],
    limit: number,
    signal?: AbortSignal,
  ): Promise<LineMatch[]> {
    checkCancelled(signal);
    return new Promise((resolve, reject) => {
      const cleanup = (): void => {
        clearTimeout(timer);
        this.worker.off("message", message);
        this.worker.off("error", error);
        this.worker.off("exit", exit);
        signal?.removeEventListener("abort", abort);
      };
      const message = (matches: LineMatch[]): void => {
        cleanup();
        resolve(matches);
      };
      const error = (): void => {
        cleanup();
        reject(new MClaudeError("Regular expression worker failed."));
      };
      const exit = (): void => {
        cleanup();
        reject(
          new MClaudeError("Regular expression worker ended unexpectedly."),
        );
      };
      const abort = (): void => {
        cleanup();
        reject(new TurnCancelled());
      };
      const timer = setTimeout(() => {
        cleanup();
        reject(
          new MClaudeError(
            "Regular expression search exceeded 2 seconds for one file.",
          ),
        );
      }, 2000);
      this.worker.once("message", message);
      this.worker.once("error", error);
      this.worker.once("exit", exit);
      signal?.addEventListener("abort", abort, { once: true });
      this.worker.postMessage({ lines, limit });
    });
  }
  async close(): Promise<void> {
    await this.worker.terminate();
  }
}
