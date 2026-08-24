import assert from "node:assert/strict";
import { test } from "node:test";
import {
  GroupChatAutocompleteProvider,
  SLASH_COMMANDS,
} from "../autocomplete.js";

const provider = new GroupChatAutocompleteProvider(["pi", "claude", "codex"]);

test("slash commands complete at the start of the first line", async () => {
  const result = await provider.getSuggestions(["/rev"], 0, 4);
  assert.ok(result);
  assert.deepEqual(
    result.items.map((item) => item.value),
    ["/review"],
  );
});

test("all slash commands list for a bare slash", async () => {
  const result = await provider.getSuggestions(["/"], 0, 1);
  assert.ok(result);
  assert.deepEqual(
    result.items.map((item) => item.value),
    SLASH_COMMANDS.map((command) => command.value),
  );
});

test("no slash completion mid-line or on later lines", async () => {
  assert.equal(await provider.getSuggestions(["hello /"], 0, 7), null);
  assert.equal(await provider.getSuggestions(["/review"], 1, 1), null);
});

test("agent mentions complete after @", async () => {
  const result = await provider.getSuggestions(["@cl"], 0, 3);
  assert.ok(result);
  assert.deepEqual(
    result.items.map((item) => item.value),
    ["@claude"],
  );
});

test("all agents list for a bare @", async () => {
  const result = await provider.getSuggestions(["hello @"], 0, 7);
  assert.ok(result);
  assert.deepEqual(
    result.items.map((item) => item.value),
    ["@claude", "@codex", "@pi"],
  );
});

test("completion replaces the token and adds a trailing space", () => {
  const applied = provider.applyCompletion(
    ["hi @cl there"],
    0,
    6,
    { value: "@claude", label: "@claude" },
    "@cl",
  );
  assert.deepEqual(applied.lines, ["hi @claude  there"]);
  assert.equal(applied.cursorCol, 11);
});
