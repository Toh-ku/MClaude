import { execFile } from "node:child_process";
import { promisify } from "node:util";
import {
  mkdtemp,
  mkdir,
  readFile,
  rm,
  access,
  realpath,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import {
  dirname,
  join,
  resolve,
  relative,
  isAbsolute,
  delimiter,
} from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import assert from "node:assert/strict";
const execute = promisify(execFile);
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const temporary = await mkdtemp(join(tmpdir(), "mclaude-package-"));
const cleanupTarget = relative(tmpdir(), temporary);
assert.ok(
  cleanupTarget.startsWith("mclaude-package-") &&
    !cleanupTarget.includes("..") &&
    !isAbsolute(cleanupTarget),
  "Unsafe temporary cleanup target.",
);
let npmCLI = process.env["npm_execpath"];
if (!npmCLI) {
  const candidates = [
    join(dirname(process.execPath), "node_modules/npm/bin/npm-cli.js"),
    resolve(
      dirname(process.execPath),
      "../lib/node_modules/npm/bin/npm-cli.js",
    ),
    ...(process.platform === "win32"
      ? []
      : (process.env["PATH"] ?? "")
          .split(delimiter)
          .map((p) => join(p, "npm"))),
  ];
  for (const candidate of candidates) {
    try {
      await access(candidate);
      npmCLI = await realpath(candidate);
      break;
    } catch {
      /* Continue to the next runtime layout. */
    }
  }
}
if (!npmCLI)
  throw new Error(
    "Cannot locate npm CLI; invoke this script through npm run package:smoke.",
  );
const npm = async (args, cwd) =>
  execute(process.execPath, [npmCLI, ...args], {
    cwd,
    timeout: 120000,
    maxBuffer: 1000000,
  });
try {
  await npm(["run", "build"], root);
  const packed = await npm(
    ["pack", "--ignore-scripts", "--json", "--pack-destination", temporary],
    root,
  );
  const report = JSON.parse(packed.stdout);
  const packedPackage = Array.isArray(report)
    ? report[0]
    : Object.values(report)[0];
  assert.ok(packedPackage?.filename, "npm pack returned no package.");
  const tarball = join(temporary, packedPackage.filename);
  const installed = join(temporary, "installed");
  await mkdir(installed);
  await npm(
    [
      "install",
      "--omit=dev",
      "--ignore-scripts",
      "--no-audit",
      "--no-fund",
      "--fetch-retries=0",
      "--fetch-timeout=30000",
      tarball,
    ],
    installed,
  );
  const packageRoot = join(installed, "node_modules", "mclaude");
  const manifest = JSON.parse(
    await readFile(join(packageRoot, "package.json"), "utf8"),
  );
  assert.equal(manifest.bin.mclaude, "dist/cli/main.js");
  const api = await import(
    pathToFileURL(join(packageRoot, "dist/index.js")).href
  );
  assert.equal(typeof api.runAgent, "function");
  const entry = join(packageRoot, manifest.bin.mclaude);
  for (const args of [["--help"], ["--version"], ["sessions", "list"]]) {
    const result = await execute(process.execPath, [entry, ...args], {
      cwd: installed,
      timeout: 15000,
      env: {
        ...process.env,
        ANTHROPIC_API_KEY: "",
        ANTHROPIC_MODEL: "",
        MCLAUDE_CONFIG_DIR: join(temporary, "config"),
        MCLAUDE_STATE_DIR: join(temporary, "state"),
      },
    });
    assert.ok(result.stdout.trim());
  }
  console.log(
    "Installed npm package: exports, bundled contracts, CLI help/version/sessions verified.",
  );
} finally {
  await rm(temporary, { recursive: true, force: true });
}
