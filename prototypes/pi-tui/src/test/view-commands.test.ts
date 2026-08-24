import assert from "node:assert/strict";
import { test } from "node:test";
import { classifySubmission } from "../app.js";
import { inboxMessages, parseTranscriptText } from "../transcript.js";

const transcript = parseTranscriptText(
  [
    '{"seq":1,"at":"","sender":"human","recipients":[],"body":"question","kind":"message"}',
    '{"seq":2,"at":"","sender":"claude","recipients":[],"body":"reply","kind":"message"}',
    '{"seq":3,"at":"","sender":"sol","recipients":[],"body":"draft","kind":"review_synthesis","provisional":true}',
    '{"seq":4,"at":"","sender":"sol","recipients":[],"body":"final","kind":"review_synthesis"}',
    '{"seq":5,"at":"","sender":"sol","recipients":[],"body":"done","kind":"anneal_final"}',
    '{"seq":6,"at":"","sender":"system","recipients":[],"body":"round 2 complete","kind":"review_status"}',
    '{"seq":7,"at":"","sender":"system","recipients":[],"body":"agent pi TIMED OUT","kind":"review_status"}',
    '{"seq":8,"at":"","sender":"pi","recipients":[],"body":"ok","kind":"message"}',
  ].join("\n"),
);

test("inbox projection matches the relay semantics", () => {
  const kept = inboxMessages(transcript);
  // human dropped; message kept (x2); provisional synthesis dropped;
  // final synthesis kept; anneal_final kept; quiet status dropped;
  // attention status kept.
  assert.deepEqual(
    kept.map((record) => record.seq),
    [2, 4, 5, 7, 8],
  );
});

test("inbox projection drops non-attention review statuses", () => {
  const records = parseTranscriptText(
    '{"seq":1,"at":"","sender":"system","recipients":[],"body":"all agents finished","kind":"review_status"}',
  );
  assert.deepEqual(inboxMessages(records), []);
});

test("exact /inbox and /room classify as local view switches", () => {
  const inbox = classifySubmission("/inbox");
  assert.equal(inbox.type, "view");
  if (inbox.type === "view") assert.equal(inbox.view, "inbox");

  const room = classifySubmission("/room");
  assert.equal(room.type, "view");
  if (room.type === "view") assert.equal(room.view, "room");
});

test("whitespace-stripped exact commands still switch views, matching Python", () => {
  assert.equal(classifySubmission("  /inbox \n").type, "view");
  assert.equal(classifySubmission("\t/room").type, "view");
});

test("near-miss and argument-bearing commands dispatch with exact text", () => {
  for (const text of ["/inboxx", "/inbox hi", "/rooms", "/REVIEW", "/review x"]) {
    const result = classifySubmission(text);
    assert.equal(result.type, "dispatch", text);
    if (result.type === "dispatch") assert.equal(result.text, text);
  }
});
