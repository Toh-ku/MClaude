import { parentPort, workerData } from "node:worker_threads";
interface Request {
  lines: string[];
  limit: number;
}
const data = workerData as { source: string; flags: string };
const regex = new RegExp(data.source, data.flags);
parentPort?.on("message", ({ lines, limit }: Request) => {
  const matches: { line: number; text: string }[] = [];
  for (let index = 0; index < lines.length; index++) {
    const text = lines[index] ?? "";
    if (regex.test(text)) {
      matches.push({ line: index + 1, text });
      if (matches.length >= limit) break;
    }
  }
  parentPort?.postMessage(matches);
});
