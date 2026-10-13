import { readFile, writeFile } from "node:fs/promises";
import { Buffer } from "node:buffer";
import { URL } from "node:url";
import { join } from "node:path";
import { createInterface } from "node:readline";
import {
  ConfigStore,
  SessionStore,
  CheckpointStore,
  WorkspaceFS,
} from "../dist/index.js";

const [operation, workspacePath, state, sessionId, checkpointId] =
  process.argv.slice(2);
const fixture = JSON.parse(
  await readFile(
    new URL("../../contracts/fixtures/conversation.json", import.meta.url),
    "utf8",
  ),
);
const workspace = await WorkspaceFS.create(workspacePath);
const config = new ConfigStore(join(state, "config.json"));
const sessions = new SessionStore(state);
const checkpoints = new CheckpointStore(workspace, state);
if (operation === "read-python") {
  const login = await config.load();
  const session = await sessions.resume(sessionId, workspace.root);
  try {
    if (JSON.stringify(session.history) !== JSON.stringify(fixture.history))
      throw new Error("History mismatch");
    if (JSON.stringify(session.tasks) !== JSON.stringify(fixture.tasks))
      throw new Error("Tasks mismatch");
    await checkpoints.restore(checkpointId);
    if (
      (await readFile(join(workspace.root, "example.txt"), "utf8")) !==
      fixture.before
    )
      throw new Error("Checkpoint bytes mismatch");
    console.log(JSON.stringify({ model: login.model, name: session.name }));
  } finally {
    await session.close();
  }
} else if (operation === "create-ts") {
  await config.save(fixture.config);
  const session = await sessions.create(workspace.root, fixture.config.model);
  try {
    for (const message of fixture.history) {
      if (message.role === "user" && Array.isArray(message.content)) {
        for (const result of message.content)
          await session.append({ type: "tool.result", result });
      } else await session.append({ type: "message.appended", message });
    }
    await session.append({ type: "tasks.updated", tasks: fixture.tasks });
    await session.append({ type: "session.named", name: "TS 会话" });
    await session.append({ type: "turn.finished", status: "ok" });
    const file = join(workspace.root, "example.txt");
    await writeFile(file, fixture.before);
    const checkpoint = await checkpoints.save(
      file,
      Buffer.from(fixture.before),
      Buffer.from(fixture.after),
    );
    await writeFile(file, fixture.after);
    console.log(JSON.stringify({ id: session.id, checkpoint }));
  } finally {
    await session.close();
  }
} else if (operation === "check-lock") {
  try {
    const session = await sessions.resume(sessionId, workspace.root);
    await session.close();
    throw new Error("Concurrent lock was accepted");
  } catch (error) {
    if (!error.message.includes("already open")) throw error;
    console.log(JSON.stringify({ locked: true }));
  }
} else if (operation === "hold-lock") {
  const session = await sessions.resume(sessionId, workspace.root);
  console.log(JSON.stringify({ locked: true }));
  const input = createInterface({ input: process.stdin });
  await new Promise((resolve) => {
    input.once("line", resolve);
    input.once("close", resolve);
  });
  input.close();
  await session.close();
} else throw new Error("Unknown interop operation");
