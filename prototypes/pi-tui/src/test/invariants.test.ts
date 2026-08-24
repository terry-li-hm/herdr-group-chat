import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { readdir, stat } from "node:fs/promises";
import { resolveExactText, TranscriptFollower } from "../app.js";

const srcDir = new URL("..", import.meta.url);

async function readSrcFiles(): Promise<string> {
  const entries = await readdir(srcDir, { withFileTypes: true });
  let combined = "";
  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.endsWith(".ts")) continue;
    combined += await readFile(new URL(entry.name, srcDir), "utf8");
  }
  return combined;
}

test("invariant: no dependency on @earendil-works/pi-coding-agent", async () => {
  const pkg = JSON.parse(
    await readFile(new URL("../../package.json", import.meta.url), "utf8"),
  ) as { dependencies?: Record<string, string>; devDependencies?: Record<string, string> };
  assert.equal(pkg.dependencies?.["@earendil-works/pi-coding-agent"], undefined);
  assert.equal(pkg.devDependencies?.["@earendil-works/pi-coding-agent"], undefined);
});

test("invariant: sources never import pi-coding-agent or create an AgentSession", async () => {
  const sources = await readSrcFiles();
  assert.ok(!sources.includes("pi-coding-agent"));
  assert.ok(!sources.includes("AgentSession"));
});

test("invariant: the frontend never writes the transcript (sole writer is the relay)", async () => {
  const sources = await readSrcFiles();
  for (const forbidden of [
    "appendFile",
    "writeFile",
    "createWriteStream",
    "O_APPEND",
    "os.O_WRONLY",
  ]) {
    assert.ok(!sources.includes(forbidden), `unexpected ${forbidden} in src`);
  }
});

test("sole-writer: following a transcript leaves its bytes untouched", async () => {
  const dir = await mkdtemp(join(tmpdir(), "pi-tui-fixture-"));
  const path = join(dir, "room.jsonl");
  await writeFile(
    path,
    '{"seq":1,"at":"","sender":"a","recipients":[],"body":"one","kind":"message"}\n',
  );
  const before = await readFile(path, "utf8");
  const statsBefore = await stat(path);
  const follower = new TranscriptFollower(path);
  await follower.start();
  assert.equal(follower.records.length, 1);
  await follower.stop();
  const after = await readFile(path, "utf8");
  const statsAfter = await stat(path);
  assert.equal(after, before);
  assert.equal(statsAfter.mtimeMs, statsBefore.mtimeMs);
  assert.equal(statsAfter.size, statsBefore.size);
});

test("sole-writer: appended records are followed without rewrites", async () => {
  const dir = await mkdtemp(join(tmpdir(), "pi-tui-fixture-"));
  const path = join(dir, "room.jsonl");
  await writeFile(
    path,
    '{"seq":1,"at":"","sender":"a","recipients":[],"body":"one","kind":"message"}\n',
  );
  const follower = new TranscriptFollower(path);
  await follower.start();
  const appended =
    '{"seq":2,"at":"","sender":"b","recipients":[],"body":"two","kind":"message"}\n';
  const { appendFile } = await import("node:fs/promises");
  // Simulate the relay's append — the follower must pick it up read-only.
  await appendFile(path, appended, { encoding: "utf8" });
  assert.equal(await follower.poll(), true);
  assert.equal(follower.records.length, 2);
  assert.equal(follower.records[1]?.body, "two");
  await follower.stop();
});

test("exact typed text is preserved on submit despite editor trimming", () => {
  assert.equal(resolveExactText("  keep inner  spacing  \n", "keep inner  spacing"), "  keep inner  spacing  \n");
  assert.equal(resolveExactText("plain", "plain"), "plain");
  // Mismatch falls back to the submitted value.
  assert.equal(resolveExactText("different", "plain"), "plain");
});
