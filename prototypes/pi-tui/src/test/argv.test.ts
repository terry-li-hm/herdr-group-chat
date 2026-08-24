import assert from "node:assert/strict";
import { test } from "node:test";
import { buildArgv, buildSpawnArgv } from "../argv.js";

const backend = {
  command: "/path/to/herdr-group-chat",
  args: ["--room", "my room"],
};

test("appends --once and the submitted text", () => {
  assert.deepEqual(buildArgv(backend, "hello"), [
    "--room",
    "my room",
    "--once",
    "hello",
  ]);
});

test("spaces and newlines survive as single argv elements", () => {
  const text = "line one\nline two  with  spaces\tand 'quotes' \"here\" $(pwd) $HOME`id`";
  const argv = buildArgv(backend, text);
  assert.equal(argv.length, 4);
  assert.equal(argv[3], text);
  // Shell metacharacters that interpolation would have altered.
  assert.ok(text.includes("$(pwd)"));
  assert.ok(text.includes("$HOME"));
  assert.ok(text.includes("`id`"));
});

test("spawn argv prefixes the backend command", () => {
  assert.deepEqual(buildSpawnArgv(backend, "x")[0], "/path/to/herdr-group-chat");
  assert.deepEqual(buildSpawnArgv(backend, "x").slice(1), buildArgv(backend, "x"));
});

test("empty backend args still append exactly --once and text", () => {
  assert.deepEqual(buildArgv({ command: "relay", args: [] }, "m"), [
    "--once",
    "m",
  ]);
});
