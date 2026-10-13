import * as fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import type { TestContext } from "node:test";
import { WorkspaceFS } from "../src/security/workspace.js";
import type {
  ModelProvider,
  ModelResponse,
  ModelRequest,
  ToolUseBlock,
} from "../src/core/types.js";
export async function fixture(
  t: TestContext,
): Promise<{ root: string; state: string; workspace: WorkspaceFS }> {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "mclaude-ts-"));
  const root = path.join(directory, "workspace"),
    state = path.join(directory, "state");
  await fs.mkdir(root);
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  return { root, state, workspace: await WorkspaceFS.create(root) };
}
export function call(
  name: string,
  input: Record<string, unknown>,
  id = "call-1",
): ToolUseBlock {
  return { type: "tool_use", id, name, input };
}
export function final(text = "done"): ModelResponse {
  return { content: [{ type: "text", text }], stop_reason: "end_turn" };
}
export function tools(...calls: ToolUseBlock[]): ModelResponse {
  return { content: calls, stop_reason: "tool_use" };
}
export function scripted(
  responses: ModelResponse[],
): ModelProvider & { requests: ModelRequest[] } {
  const requests: ModelRequest[] = [];
  return {
    requests,
    async createMessage(request) {
      requests.push(
        structuredClone({
          messages: request.messages,
          tools: request.tools,
          ...(request.system ? { system: request.system } : {}),
        }),
      );
      const response = responses.shift();
      if (!response) throw new Error("Unexpected model request.");
      return response;
    },
  };
}
export const config = {
  apiKey: "test-secret",
  model: "test-model",
  maxTokens: 1024,
  timeout: 2,
  requestRetries: 0,
  retryDelay: 0,
};
