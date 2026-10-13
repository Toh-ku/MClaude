import js from "@eslint/js";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist/**", ".test-build/**", "node_modules/**"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  // These modules intentionally match control characters for sanitizing and line splitting.
  {
    files: [
      "src/integrations/mcp.ts",
      "src/terminal/status.ts",
      "src/tools/files/read.ts",
    ],
    rules: { "no-control-regex": "off" },
  },
  {
    files: ["**/*.mjs"],
    languageOptions: { globals: { process: "readonly", console: "readonly" } },
  },
);
