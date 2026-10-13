import { ConfigurationError } from "./types.js";

export interface SavedConfig {
  api_key: string;
  model: string;
  base_url: string;
}
export interface ModelConfig {
  apiKey: string;
  model: string;
  baseUrl?: string;
  maxTokens: number;
  timeout: number;
  requestRetries: number;
  retryDelay: number;
}
export const DEFAULT_API_BASE_URL = "https://api.anthropic.com";
export function validateUrl(value: string, query = false): boolean {
  try {
    const u = new URL(value);
    return (
      ["http:", "https:"].includes(u.protocol) &&
      !!u.hostname &&
      u.port !== "0" &&
      !u.username &&
      !u.password &&
      (query || !u.search) &&
      !u.hash
    );
  } catch {
    return false;
  }
}
export function validateConfig(config: ModelConfig): ModelConfig {
  if (!config.apiKey.trim())
    throw new ConfigurationError("Set ANTHROPIC_API_KEY or run mclaude login.");
  if (!config.model.trim())
    throw new ConfigurationError("Set ANTHROPIC_MODEL or pass --model.");
  if (config.baseUrl !== undefined && !validateUrl(config.baseUrl))
    throw new ConfigurationError(
      "API base URL must be HTTP(S) without credentials, query parameters or fragments.",
    );
  if (!Number.isSafeInteger(config.maxTokens) || config.maxTokens <= 0)
    throw new ConfigurationError("--max-tokens must be a positive integer.");
  if (!Number.isFinite(config.timeout) || config.timeout <= 0)
    throw new ConfigurationError("--timeout must be a finite positive number.");
  if (
    !Number.isSafeInteger(config.requestRetries) ||
    config.requestRetries < 0 ||
    config.requestRetries > 10
  )
    throw new ConfigurationError("--request-retries must be between 0 and 10.");
  if (!Number.isFinite(config.retryDelay) || config.retryDelay < 0)
    throw new ConfigurationError(
      "--retry-delay must be a finite non-negative number.",
    );
  return Object.freeze({ ...config });
}
export function resolveConfig(
  options: Partial<ModelConfig> = {},
  saved: Partial<SavedConfig> = {},
  env: NodeJS.ProcessEnv = process.env,
): ModelConfig {
  const baseUrl = (
    options.baseUrl ??
    env["ANTHROPIC_BASE_URL"] ??
    saved.base_url ??
    ""
  ).trim();
  return validateConfig({
    apiKey:
      options.apiKey ??
      (env["ANTHROPIC_API_KEY"]?.trim() || saved.api_key || ""),
    model: (
      options.model ??
      (env["ANTHROPIC_MODEL"]?.trim() || saved.model || "")
    ).trim(),
    maxTokens: options.maxTokens ?? 1024,
    timeout: options.timeout ?? 60,
    requestRetries: options.requestRetries ?? 2,
    retryDelay: options.retryDelay ?? 0.5,
    ...(baseUrl ? { baseUrl } : {}),
  });
}
