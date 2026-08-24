import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { Key, matchesKey, setKittyProtocolActive } from "@earendil-works/pi-tui";
import { isCtrlC } from "../app.js";

test("Ctrl-C predicate matches the raw legacy control character", () => {
  setKittyProtocolActive(false);
  assert.equal(isCtrlC("\x03"), true);
  assert.equal(matchesKey("\x03", Key.ctrl("c")), true);
});

test("Ctrl-C predicate matches Kitty keyboard protocol encodings", () => {
  // CSI 99;5u = unicode-keypress: 'c' (99) with ctrl modifier (5).
  for (const active of [false, true]) {
    setKittyProtocolActive(active);
    assert.equal(isCtrlC("\x1b[99;5u"), true, `kittyProtocolActive=${active}`);
    assert.equal(matchesKey("\x1b[99;5u", Key.ctrl("c")), true, `kittyProtocolActive=${active}`);
  }
  setKittyProtocolActive(false);
});

test("Ctrl-C predicate rejects non-Ctrl-C input", () => {
  setKittyProtocolActive(false);
  assert.equal(isCtrlC("c"), false);
  assert.equal(isCtrlC("\r"), false);
  assert.equal(isCtrlC("\x1b"), false);
  // Plain 'c' as a Kitty sequence (no ctrl modifier).
  assert.equal(isCtrlC("\x1b[99u"), false);
});

test("invariant: Ctrl-C exit policy uses isCtrlC and keeps the active-child refusal", async () => {
  const appSource = await readFile(
    new URL("../../src/app.ts", import.meta.url),
    "utf8",
  );
  // Raw equality against \x03 broke Kitty-protocol terminals; require the
  // matchesKey-based predicate instead.
  assert.ok(!appSource.includes('data !== "\\x03"'));
  assert.ok(appSource.includes("if (!isCtrlC(data)) return undefined;"));
  // The refusal contract: while a dispatch child is active, Ctrl-C must not
  // exit and the child must be left running.
  assert.ok(
    appSource.includes("dispatch in progress — Ctrl-C ignored, child left running"),
  );
  assert.ok(
    appSource.includes("if (this.child !== null)") ||
      appSource.includes("this.child !== null"),
  );
});
