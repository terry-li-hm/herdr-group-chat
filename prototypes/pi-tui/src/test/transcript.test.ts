import assert from "node:assert/strict";
import { test } from "node:test";
import {
  parseTranscriptLine,
  parseTranscriptText,
  TranscriptError,
} from "../transcript.js";

test("parses well-formed records", () => {
  const record = parseTranscriptLine(
    '{"seq":3,"at":"2026-01-01T00:00:00Z","sender":"pi","recipients":["claude"],"body":"hi","kind":"message"}',
    1,
  );
  assert.equal(record.seq, 3);
  assert.equal(record.sender, "pi");
  assert.deepEqual(record.recipients, ["claude"]);
  assert.equal(record.body, "hi");
  assert.equal(record.kind, "message");
});

test("parses a full transcript text with blank lines", () => {
  const records = parseTranscriptText(
    '{"seq":1,"at":"","sender":"a","recipients":[],"body":"one","kind":"message"}\n\n{"seq":2,"at":"","sender":"b","recipients":[],"body":"two","kind":"message"}\n',
  );
  assert.equal(records.length, 2);
  assert.equal(records[1]?.body, "two");
});

test("rejects malformed JSON", () => {
  assert.throws(
    () => parseTranscriptLine("{not json", 7),
    (error: unknown) =>
      error instanceof TranscriptError &&
      /line 7: invalid JSON/.test(error.message),
  );
});

test("rejects non-object records", () => {
  assert.throws(
    () => parseTranscriptLine("[1,2,3]", 1),
    TranscriptError,
  );
  assert.throws(() => parseTranscriptLine("42", 1), TranscriptError);
});

test("rejects records with missing or non-integer seq", () => {
  assert.throws(
    () => parseTranscriptLine('{"sender":"a","body":"b","kind":"message"}', 1),
    TranscriptError,
  );
  assert.throws(
    () => parseTranscriptLine('{"seq":1.5,"sender":"a","body":"b","kind":"message"}', 1),
    TranscriptError,
  );
});

test("rejects records with non-string sender or body", () => {
  assert.throws(
    () => parseTranscriptLine('{"seq":1,"sender":1,"body":"b","kind":"message"}', 1),
    TranscriptError,
  );
  assert.throws(
    () => parseTranscriptLine('{"seq":1,"sender":"a","body":null,"kind":"message"}', 1),
    TranscriptError,
  );
});

test("non-string recipients are dropped, non-list tolerated", () => {
  const record = parseTranscriptLine(
    '{"seq":1,"sender":"a","recipients":[1,"b"],"body":"x","kind":"message"}',
    1,
  );
  assert.deepEqual(record.recipients, ["b"]);
});
