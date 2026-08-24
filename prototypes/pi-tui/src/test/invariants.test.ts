import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { readdir, stat } from "node:fs/promises";
import { resolveExactText, TranscriptFollower } from "../app.js";

// Tests run from dist/, so resolve the real sources under ../../src/.
const srcDir = new URL("../../src/", import.meta.url);

async function readSrcFiles(): Promise<string> {
  const entries = await readdir(srcDir, { withFileTypes: true });
  let combined = "";
  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.endsWith(".ts")) continue;
    combined += await readFile(new URL(entry.name, srcDir), "utf8");
  }
  assert.ok(combined.length > 0, "source files must actually be read");
  return combined;
}

/** Only import/export-from statements — comments and strings excluded. */
function importStatements(source: string): string {
  return source
    .split("\n")
    .filter((line) => /^\s*import\b/.test(line))
    .join("\n");
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
  assert.ok(!importStatements(sources).includes("pi-coding-agent"));
  assert.ok(!importStatements(sources).includes("AgentSession"));
  assert.ok(!/new\s+AgentSession/.test(sources));
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

test("fresh room: absent transcript starts empty and the path stays absent", async () => {
  const dir = await mkdtemp(join(tmpdir(), "pi-tui-fresh-"));
  const path = join(dir, "room.jsonl");
  const follower = new TranscriptFollower(path);
  await follower.start(); // must not throw ENOENT
  assert.equal(follower.records.length, 0);
  assert.equal(await follower.poll(), false); // still absent, still quiet
  assert.equal(
    (await readdir(dir)).includes("room.jsonl"),
    false,
    "follower must never create the transcript",
  );
  // A non-ENOENT error still surfaces clearly.
  await writeFile(join(dir, "blocker"), "x");
  const blocked = new TranscriptFollower(join(dir, "blocker", "room.jsonl"));
  // ENOTDIR on the path component, not ENOENT:
  await assert.rejects(blocked.start(), (error: unknown) => {
    const code = (error as NodeJS.ErrnoException).code;
    return code !== undefined && code !== "ENOENT";
  });
});

test("fresh room: first record is followed after the relay creates the transcript", async () => {
  const dir = await mkdtemp(join(tmpdir(), "pi-tui-fresh-"));
  const path = join(dir, "room.jsonl");
  const follower = new TranscriptFollower(path);
  await follower.start();
  assert.equal(await follower.poll(), false);
  // Simulate the relay creating and appending the first record (sole writer).
  await writeFile(
    path,
    '{"seq":1,"at":"","sender":"system","recipients":[],"body":"room opened","kind":"message"}\n',
  );
  assert.equal(await follower.poll(), true);
  assert.equal(follower.records.length, 1);
  assert.equal(follower.records[0]?.body, "room opened");
  await follower.stop();
  // The follower's read handle must not have truncated or rewritten anything.
  assert.equal(
    (await readFile(path, "utf8")).endsWith("\n"),
    true,
  );
});
