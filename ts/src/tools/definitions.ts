import { readFile } from "node:fs/promises";
import { Ajv } from "ajv";
import { MClaudeError, isObject, type ToolSpec } from "../core/types.js";

async function loadDefinitions(): Promise<ToolSpec[]> {
  // Installed packages carry their contracts; tests use the repository copy.
  const candidates = [
    new URL("../contracts/tools.json", import.meta.url),
    new URL("../../../../contracts/schemas/tools.json", import.meta.url),
  ];
  for (const file of candidates) {
    try {
      const value: unknown = JSON.parse(await readFile(file, "utf8"));
      if (
        !Array.isArray(value) ||
        !value.every(
          (s: unknown) =>
            isObject(s) &&
            typeof s["name"] === "string" &&
            typeof s["description"] === "string" &&
            isObject(s["input_schema"]),
        )
      )
        throw new Error("Invalid tool contracts.");
      return value as ToolSpec[];
    } catch (e) {
      if (file === candidates.at(-1)) throw e;
    }
  }
  throw new MClaudeError("Tool contracts are missing.");
}
export const TOOL_DEFINITIONS = await loadDefinitions();
const ajv = new Ajv({
  allErrors: false,
  strict: true,
  coerceTypes: false,
  useDefaults: false,
  removeAdditional: false,
});
const validators = new Map(
  TOOL_DEFINITIONS.map((d) => [d.name, ajv.compile(d.input_schema)]),
);
export function validateInput(
  name: string,
  input: unknown,
): Record<string, unknown> {
  const validate = validators.get(name);
  if (!validate || !isObject(input) || !validate(input))
    throw new MClaudeError(`Invalid arguments for ${name}.`);
  return input;
}
export function stringInput(
  input: Record<string, unknown>,
  key: string,
  allowEmpty = false,
): string {
  const value = input[key];
  if (typeof value !== "string" || (!allowEmpty && !value.trim()))
    throw new MClaudeError(
      `${key} must be a ${allowEmpty ? "" : "non-empty "}string.`,
    );
  return value;
}
