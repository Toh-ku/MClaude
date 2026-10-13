import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { Ajv } from "ajv";
import { validateHistory } from "../src/persistence/validation.js";
import { validateTasks } from "../src/core/tasks.js";
test("shared wire schemas validate Unicode fixtures and reject malformed messages", async () => {
  const read = async (file: string): Promise<unknown> =>
    JSON.parse(
      await readFile(
        new URL("../../../contracts/" + file, import.meta.url),
        "utf8",
      ),
    );
  const fixture = (await read("fixtures/conversation.json")) as {
    config: unknown;
    tasks: unknown;
    history: unknown[];
  };
  const ajv = new Ajv({ strict: true, strictRequired: false });
  ajv.addSchema((await read("schemas/tasks.json")) as object, "tasks.json");
  const config = ajv.compile((await read("schemas/config.json")) as object),
    event = ajv.compile((await read("schemas/session-event.json")) as object);
  assert.equal(config(fixture.config), true);
  assert.equal(
    event({
      type: "tasks.updated",
      seq: 1,
      timestamp: "2026-10-13T00:00:00+00:00",
      tasks: fixture.tasks,
    }),
    true,
  );
  for (const message of fixture.history)
    assert.equal(
      event({
        type: "message.appended",
        seq: 2,
        timestamp: "2026-10-13T00:00:00+00:00",
        message,
      }),
      true,
      JSON.stringify(event.errors),
    );
  assert.equal(
    event({
      type: "message.appended",
      seq: 1,
      timestamp: "now",
      message: { role: "assistant", content: "invalid" },
    }),
    false,
  );
  validateHistory(fixture.history);
  validateTasks(fixture.tasks);
});
