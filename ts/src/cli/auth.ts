import { parseArgs } from "node:util";
import { ConfigStore } from "../persistence/config-store.js";
import { ConfigurationError, TurnCancelled } from "../core/types.js";
import { InputService, hiddenInput } from "../terminal/input.js";
export async function authCommand(
  command: string,
  argv: string[],
): Promise<number> {
  let parsed;
  try {
    parsed = parseArgs({
      args: argv,
      options:
        command === "login"
          ? {
              help: { type: "boolean", short: "h" },
              model: { type: "string" },
              "base-url": { type: "string" },
            }
          : { help: { type: "boolean", short: "h" } },
      strict: true,
    });
  } catch {
    throw new ConfigurationError(`Invalid ${command} option.`);
  }
  if (parsed.values.help) {
    console.log(
      command === "login"
        ? "Usage: mclaude login [--model MODEL] [--base-url URL]"
        : "Usage: mclaude logout",
    );
    return 0;
  }
  const store = new ConfigStore();
  if (command === "logout") {
    console.error(
      (await store.logout())
        ? "Saved login configuration removed."
        : "No saved login configuration.",
    );
    console.error(
      "Sessions and checkpoints are retained; environment variables remain independent configuration sources.",
    );
    return 0;
  }
  let saved: Awaited<ReturnType<ConfigStore["load"]>> = {};
  try {
    saved = await store.load();
  } catch {
    /* Login is also how a corrupt config is repaired. */
  }
  const api_key = (await hiddenInput("API Key (hidden): ")).trim();
  let cancelled = false;
  const input = new InputService(() => {
    cancelled = true;
    input.close();
  });
  try {
    const defaultModel = String(
      parsed.values.model ??
        process.env["ANTHROPIC_MODEL"] ??
        saved.model ??
        "",
    );
    const defaultUrl = String(
      parsed.values["base-url"] ??
        process.env["ANTHROPIC_BASE_URL"] ??
        saved.base_url ??
        "",
    );
    const model = await input.question(
      `Model ID${defaultModel ? ` [${defaultModel}]` : ""}: `,
    );
    if (model === undefined || cancelled)
      throw new TurnCancelled("Login cancelled.");
    const url = await input.question(
      `API base URL (blank: official API)${defaultUrl ? ` [${defaultUrl}]` : ""}: `,
    );
    if (url === undefined || cancelled)
      throw new TurnCancelled("Login cancelled.");
    await store.save({
      api_key,
      model: model.trim() || defaultModel,
      base_url: url.trim(),
    });
    console.error(
      "Login configuration saved. Credentials will be checked on the first model request.",
    );
    return 0;
  } finally {
    input.close();
  }
}
