import * as fs from "node:fs/promises";
import path from "node:path";
import { ConfigurationError, errorCode, isObject } from "../core/types.js";
import { resolveConfig, type SavedConfig } from "../core/config.js";
import { atomicWrite, readBounded, utf8 } from "../security/workspace.js";
import { configPath } from "./paths.js";
export class ConfigStore {
  constructor(readonly path = configPath()) {}
  async load(): Promise<Partial<SavedConfig>> {
    try {
      const value: unknown = JSON.parse(
        utf8(await readBounded(this.path, 100000)),
      );
      if (
        !isObject(value) ||
        Object.keys(value).sort().join(",") !==
          "api_key,base_url,model,version" ||
        value["version"] !== 1 ||
        !["api_key", "model", "base_url"].every(
          (k) => typeof value[k] === "string",
        )
      )
        throw new Error();
      const settings = {
        api_key: String(value["api_key"]).trim(),
        model: String(value["model"]).trim(),
        base_url: String(value["base_url"]).trim(),
      };
      resolveConfig({}, settings, {});
      return settings;
    } catch (e) {
      if (errorCode(e) === "ENOENT") return {};
      throw new ConfigurationError(
        "Saved login configuration is invalid or unreadable; run mclaude login to replace it or mclaude logout to remove it.",
      );
    }
  }
  async save(settings: SavedConfig): Promise<void> {
    const config = resolveConfig({}, settings, {});
    try {
      await fs.mkdir(path.dirname(this.path), { recursive: true, mode: 0o700 });
      await atomicWrite(
        this.path,
        Buffer.from(
          JSON.stringify(
            {
              version: 1,
              api_key: config.apiKey,
              model: config.model,
              base_url: config.baseUrl ?? "",
            },
            null,
            2,
          ) + "\n",
        ),
        { mode: 0o600 },
      );
    } catch {
      throw new ConfigurationError("Cannot save login configuration.");
    }
  }
  async logout(): Promise<boolean> {
    try {
      await fs.unlink(this.path);
      return true;
    } catch (e) {
      if (errorCode(e) === "ENOENT") return false;
      throw new ConfigurationError("Cannot remove saved login configuration.");
    }
  }
}
