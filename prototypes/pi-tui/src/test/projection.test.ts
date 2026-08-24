import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { markdownToPlainText, projectTranscript } from "../projection.js";
import { parseTranscriptText } from "../transcript.js";

test("markdown is projected deterministically", () => {
  assert.equal(markdownToPlainText("**bold** and *italic*"), "bold and italic");
  assert.equal(markdownToPlainText("`code`"), "code");
  assert.equal(markdownToPlainText("# Heading"), "Heading");
  assert.equal(markdownToPlainText("- item"), "  • item");
  assert.equal(markdownToPlainText("[link](https://x)"), "link");
});

test("projection is stable across repeated runs", () => {
  const text = '{"seq":1,"at":"","sender":"a","recipients":[],"body":"**x**","kind":"message"}';
  const first = projectTranscript(parseTranscriptText(text));
  const second = projectTranscript(parseTranscriptText(text));
  assert.equal(first, second);
  assert.ok(first.includes("[1] a -> all (message)"));
});

test("the bundled fixture renders to the expected projection", async () => {
  const fixture = await readFile(
    new URL("../../fixtures/sample.jsonl", import.meta.url),
    "utf8",
  );
  const output = projectTranscript(parseTranscriptText(fixture));
  assert.equal(
    output,
    [
      "[1] pi -> all (message)",
      "Morning. Starting the review round now.",
      "",
      "[2] claude -> pi (message)",
      "Acknowledged.\n\n  • first item\n  • second item",
      "",
      "[3] system -> all (notice)",
      "/review complete",
    ].join("\n"),
  );
});
