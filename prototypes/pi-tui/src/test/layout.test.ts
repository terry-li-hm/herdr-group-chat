import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { test } from "node:test";
import { mkdtemp, writeFile, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  Editor,
  type EditorTheme,
  TuiAltScreen,
  type Terminal,
} from "@earendil-works/pi-tui";
import { ChatApp, TranscriptFollower } from "../app.js";

const identity = (text: string): string => text;
const EDITOR_THEME: EditorTheme = {
  borderColor: identity,
  selectList: {
    selectedPrefix: identity,
    selectedText: identity,
    description: identity,
    scrollInfo: identity,
    noMatch: identity,
  },
};

/** Minimal headless Terminal stub; the app is never started in these tests. */
class StubTerminal implements Terminal {
  start(): void {}
  stop(): void {}
  drainInput(): Promise<void> {
    return Promise.resolve();
  }
  write(): void {}
  get columns(): number {
    return 80;
  }
  get rows(): number {
    return 24;
  }
  get kittyProtocolActive(): boolean {
    return false;
  }
  moveBy(): void {}
  hideCursor(): void {}
  showCursor(): void {}
  clearLine(): void {}
  clearFromCursor(): void {}
  clearScreen(): void {}
  setTitle(): void {}
  setProgress(): void {}
}

async function makeApp(): Promise<{
  app: ChatApp;
  follower: TranscriptFollower;
  spawned: string[][];
}> {
  const dir = await mkdtemp(join(tmpdir(), "pi-tui-layout-"));
  const path = join(dir, "room.jsonl");
  await writeFile(
    path,
    '{"seq":1,"at":"","sender":"pi","recipients":[],"body":"hello **world**","kind":"message"}\n',
  );
  const follower = new TranscriptFollower(path);
  await follower.start();
  const spawned: string[][] = [];
  const app = new ChatApp(
    new StubTerminal(),
    {
      transcriptPath: path,
      agents: ["pi", "claude"],
      backend: { command: "/nonexistent/relay", args: [] },
    },
    follower,
    (command, args) => {
      spawned.push([command, ...args]);
      const fake = new EventEmitter() as unknown as ReturnType<
        typeof import("node:child_process").spawn
      >;
      setImmediate(() => fake.emit("error", new Error("stub")));
      setImmediate(() => fake.emit("exit", 1));
      return fake;
    },
  );
  return { app, follower, spawned };
}

test("structural: the editor is rendered inside the mounted layout", async () => {
  const { app } = await makeApp();
  const width = 80;
  // Independently render an identically configured editor.
  const referenceTui = new TuiAltScreen(new StubTerminal(), true);
  const referenceEditor = new Editor(referenceTui, EDITOR_THEME, { paddingX: 1 });
  referenceEditor.focused = true; // match the app's focused composer
  const referenceLines = referenceEditor.render(width);

  const mounted = app.mountedLayout.render(width);
  // The editor's exact rendered block must appear as the trailing lines of
  // the mounted layout. This fails if the editor sits outside layoutRoot,
  // because TuiAltScreen mounts only the layout root.
  const tail = mounted.slice(mounted.length - referenceLines.length);
  assert.deepEqual(tail, referenceLines);
});

test("invariant: dispatch options stay explicit — shell:false, ignored stdio", async () => {
  // Source invariant: the default SpawnFn wrapper must keep the explicit
  // spawn options from the original contract implementation.
  const appSource = await readFile(
    new URL("../../src/app.ts", import.meta.url),
    "utf8",
  );
  assert.ok(
    appSource.includes(
      'spawn(command, args, { shell: false, stdio: ["ignore", "ignore", "ignore"] })',
    ),
    "default spawn wrapper must pass shell:false and ignored stdio explicitly",
  );
});

test("invariant: app.ts does not import ProcessTerminal (main.ts owns it)", async () => {
  const appSource = await readFile(new URL("../../src/app.ts", import.meta.url), "utf8");
  const imports = appSource
    .split("\n")
    .filter((line) => /^\s*import\b/.test(line))
    .join("\n");
  assert.ok(!imports.includes("ProcessTerminal"));
});

test("injected SpawnFn still receives exact argv without spawn options", async () => {
  const dir = await mkdtemp(join(tmpdir(), "pi-tui-spawn-"));
  const path = join(dir, "room.jsonl");
  await writeFile(
    path,
    '{"seq":1,"at":"","sender":"pi","recipients":[],"body":"x","kind":"message"}\n',
  );
  const follower = new TranscriptFollower(path);
  await follower.start();
  const seen: Array<{ command: string; args: string[] }> = [];
  const app = new ChatApp(
    new StubTerminal(),
    {
      transcriptPath: path,
      agents: [],
      backend: { command: "relay", args: [] },
    },
    follower,
    (command, args) => {
      seen.push({ command, args });
      const fake = new EventEmitter();
      setImmediate(() => {
        fake.emit("error", new Error("stub"));
        fake.emit("exit", 1);
      });
      return fake as never;
    },
  );
  app.submit("hello");
  assert.deepEqual(seen, [{ command: "relay", args: ["--once", "hello"] }]);
  await new Promise((resolve) => setImmediate(resolve));
});

test("structural: the status line renders inside the mounted layout above the editor", async () => {
  const { app } = await makeApp();
  app.submit("/room"); // sets the status notice without dispatching
  const mounted = app.mountedLayout.render(80);
  assert.ok(mounted.some((line) => line.includes("Showing the full room transcript.")));
});

test("view commands switch locally and never build or spawn backend argv", async () => {
  const { app, spawned } = await makeApp();
  app.submit("/inbox");
  assert.equal(app.currentView, "inbox");
  app.submit("  /room  "); // exact-command semantics after whitespace strip
  assert.equal(app.currentView, "room");
  assert.equal(spawned.length, 0);
});

test("non-exact inbox-like text still dispatches with exact text preserved", async () => {
  const { app, spawned } = await makeApp();
  app.submit("/inbox please summarize");
  assert.equal(app.dispatchedArgv.length, 1);
  assert.equal(spawned.length, 1);
  assert.equal(spawned[0]?.at(-1), "/inbox please summarize");
  assert.equal(spawned[0]?.at(-2), "--once");
  // Let the stubbed child's error/exit events settle synchronously-set state.
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(app.currentView, "room");
});

test("structural: the status line renders inside the mounted layout above the editor", async () => {
  const { app } = await makeApp();
  app.submit("/room"); // sets the status notice without dispatching
  const mounted = app.mountedLayout.render(80);
  assert.ok(mounted.some((line) => line.includes("Showing the full room transcript.")));
});
