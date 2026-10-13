import Anthropic from "@anthropic-ai/sdk";
import type {
  MessageParam,
  Tool,
  Message as ApiMessage,
} from "@anthropic-ai/sdk/resources/messages";
import { setTimeout as delay } from "node:timers/promises";
import { DEFAULT_API_BASE_URL, type ModelConfig } from "../core/config.js";
import {
  ModelError,
  checkCancelled,
  isObject,
  type ContentBlock,
  type ModelProvider,
  type ModelRequest,
  type ModelResponse,
} from "../core/types.js";

const RETRYABLE = new Set([429, 500, 502, 503, 504, 529]);
function normalize(message: ApiMessage): ModelResponse {
  const content: ContentBlock[] = [];
  for (const block of message.content) {
    if (block.type === "text") content.push({ type: "text", text: block.text });
    else if (block.type === "tool_use") {
      if (!isObject(block.input))
        throw new ModelError("The model returned invalid tool arguments.");
      content.push({
        type: "tool_use",
        id: block.id,
        name: block.name,
        input: block.input,
      });
    }
  }
  return {
    content,
    stop_reason: message.stop_reason,
    input_tokens: message.usage.input_tokens,
    output_tokens: message.usage.output_tokens,
  };
}
function mappedError(error: unknown): ModelError {
  if (error instanceof ModelError) return error;
  if (error instanceof Anthropic.APIConnectionTimeoutError)
    return new ModelError(
      "Request timed out after retry attempts; increase --timeout.",
    );
  if (error instanceof Anthropic.APIConnectionError)
    return new ModelError(
      "Cannot connect to the API; check your network and ANTHROPIC_BASE_URL.",
    );
  if (error instanceof Anthropic.APIError && error.status) {
    const messages: Record<number, string> = {
      400: "Invalid request; check model and request parameters.",
      401: "Authentication failed; run mclaude login.",
      403: "Access denied; check your API key permissions.",
      404: "Model or endpoint not found.",
      429: "API rate limit or quota reached; try later.",
    };
    return new ModelError(
      `HTTP ${error.status}: ${messages[error.status] ?? "API service unavailable or rejected the request."}`,
    );
  }
  return new ModelError(
    "The API response stream failed or contained invalid data.",
  );
}
export class AnthropicProvider implements ModelProvider {
  private readonly client: Anthropic;
  constructor(
    private readonly config: ModelConfig,
    fetcher?: typeof fetch,
  ) {
    this.client = new Anthropic({
      apiKey: config.apiKey,
      baseURL: config.baseUrl ?? DEFAULT_API_BASE_URL,
      timeout: config.timeout * 1000,
      maxRetries: 0,
      ...(fetcher ? { fetch: fetcher } : {}),
    });
  }
  async createMessage(
    request: ModelRequest,
    signal: AbortSignal,
  ): Promise<ModelResponse> {
    const options = {
      model: this.config.model,
      max_tokens: this.config.maxTokens,
      messages: request.messages as MessageParam[],
      ...(request.tools.length ? { tools: request.tools as Tool[] } : {}),
      ...(request.system ? { system: request.system } : {}),
    };
    for (let attempt = 0; attempt <= this.config.requestRetries; attempt++) {
      checkCancelled(signal);
      let emitted = false;
      try {
        if (!request.onText)
          return normalize(
            await this.client.messages.create(options, { signal }),
          );
        const stream = this.client.messages.stream(options, { signal });
        let finished = false,
          seenText = false;
        const open = new Set<number>();
        const toolJson = new Map<number, string>();
        try {
          for await (const event of stream) {
            checkCancelled(signal);
            if (event.type === "content_block_start") {
              if (open.has(event.index))
                throw new ModelError(
                  "The model returned duplicate content blocks.",
                );
              open.add(event.index);
              if (event.content_block.type === "text") {
                if (seenText) request.onText("\n");
                seenText = true;
                if (event.content_block.text) {
                  emitted = true;
                  request.onText(event.content_block.text);
                }
              }
            } else if (event.type === "content_block_delta") {
              if (!open.has(event.index))
                throw new ModelError(
                  "The model returned an invalid content delta.",
                );
              if (event.delta.type === "text_delta") {
                if (event.delta.text) emitted = true;
                request.onText(event.delta.text);
              } else if (event.delta.type === "input_json_delta")
                toolJson.set(
                  event.index,
                  (toolJson.get(event.index) ?? "") + event.delta.partial_json,
                );
            } else if (event.type === "content_block_stop") {
              if (!open.delete(event.index))
                throw new ModelError(
                  "The model closed an unknown content block.",
                );
            } else if (event.type === "message_stop") finished = true;
          }
          if (!finished || open.size)
            throw new ModelError(
              "The response stream ended before the message was complete.",
            );
          const message = await stream.finalMessage();
          if (message.stop_reason !== "max_tokens")
            for (const json of toolJson.values()) {
              let value: unknown;
              try {
                value = JSON.parse(json);
              } catch {
                throw new ModelError(
                  "The model returned incomplete tool JSON.",
                );
              }
              if (!isObject(value))
                throw new ModelError(
                  "The model returned invalid tool arguments.",
                );
            }
          return normalize(message);
        } finally {
          stream.abort();
        }
      } catch (e) {
        checkCancelled(signal);
        if (emitted)
          throw new ModelError(
            "The response stream failed after output began; it was not retried.",
          );
        const retryable =
          e instanceof Anthropic.APIConnectionError ||
          (e instanceof Anthropic.APIError &&
            e.status !== undefined &&
            RETRYABLE.has(e.status));
        if (retryable && attempt < this.config.requestRetries) {
          try {
            await delay(
              this.config.retryDelay * 1000 * 2 ** attempt,
              undefined,
              { signal },
            );
          } catch {
            checkCancelled(signal);
          }
          continue;
        }
        throw mappedError(e);
      }
    }
    throw new ModelError("Model request attempts exhausted.");
  }
}
