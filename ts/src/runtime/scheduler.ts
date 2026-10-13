import {
  checkCancelled,
  TurnCancelled,
  fail,
  type ToolResult,
  type ToolUseBlock,
} from "../core/types.js";
const PARALLEL = new Set([
  "read_file",
  "find_files",
  "search_text",
  "git_review",
]);
export class ToolScheduler {
  readonly started = new Set<string>();
  constructor(
    readonly workers = 4,
    readonly enabled = true,
  ) {
    if (!Number.isInteger(workers) || workers < 1 || workers > 16)
      throw new RangeError("read_workers must be between 1 and 16.");
  }
  async run(
    calls: ToolUseBlock[],
    prepare: (call: ToolUseBlock) => Promise<ToolResult | undefined>,
    execute: (call: ToolUseBlock) => Promise<ToolResult>,
    record: (call: ToolUseBlock, result: ToolResult) => Promise<void>,
    signal: AbortSignal,
  ): Promise<void> {
    let position = 0;
    while (position < calls.length) {
      checkCancelled(signal);
      const first = calls[position];
      if (!first) break;
      if (!this.enabled || this.workers === 1 || !PARALLEL.has(first.name)) {
        position++;
        const denied = await prepare(first);
        checkCancelled(signal);
        if (denied) await record(first, denied);
        else {
          this.started.add(first.id);
          const result = await execute(first);
          await record(first, result);
        }
        continue;
      }
      const batch: ToolUseBlock[] = [];
      while (calls[position] && PARALLEL.has(calls[position]!.name))
        batch.push(calls[position++]!);
      const approved: ToolUseBlock[] = [];
      for (const call of batch) {
        const denied = await prepare(call);
        if (denied) await record(call, denied);
        else approved.push(call);
        checkCancelled(signal);
      }
      let index = 0;
      let failure: unknown;
      let failed = false;
      const worker = async (): Promise<void> => {
        while (index < approved.length && !signal.aborted && !failed) {
          const call = approved[index++];
          if (!call) return;
          this.started.add(call.id);
          let result: ToolResult;
          try {
            result = await execute(call);
          } catch (e) {
            if (e instanceof TurnCancelled) return;
            result = fail("Read-only tool failed.");
          }
          try {
            await record(call, result);
          } catch (e) {
            failed = true;
            failure = e;
            return;
          }
        }
      };
      await Promise.all(
        Array.from({ length: Math.min(this.workers, approved.length) }, worker),
      );
      if (failed) throw failure;
      checkCancelled(signal);
    }
  }
}
