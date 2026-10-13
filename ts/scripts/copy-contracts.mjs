import { cp, mkdir, chmod } from "node:fs/promises";
import { URL } from "node:url";
const destination = new URL("../dist/contracts/", import.meta.url);
await mkdir(destination, { recursive: true });
await cp(new URL("../../contracts/schemas/", import.meta.url), destination, {
  recursive: true,
});
if (process.platform !== "win32")
  await chmod(new URL("../dist/cli/main.js", import.meta.url), 0o755);
