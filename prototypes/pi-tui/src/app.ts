/**
 * The standalone pi-tui frontend for Herdr Group Chat.
 *
 * Peer-neutral by construction: it uses ProcessTerminal + TuiAltScreen from
 * @earendil-works/pi-tui directly, never imports @earendil-works/pi-coding-agent,
 * and never creates an AgentSession. The Python relay stays the sole transcript
 * writer; this frontend only reads the JSONL and dispatches submissions as a
 * spawned argv array (no shell).
 */

import { spawn } from "node:child_process";

import { open } from "node:fs/promises";
import type { FileHandle } from "node:fs/promises";
import {
  Editor,
  type EditorTheme,
  Key,
  Markdown,
  matchesKey,
  ScrollView,
  Text,
  TuiAltScreen,
  VStack,
  type Component,
} from "@earendil-works/pi-tui";
import { GroupChatAutocompleteProvider } from "./autocomplete.js";
import { buildSpawnArgv, type BackendConfig } from "./argv.js";
import { parseTranscriptText, inboxMessages, type TranscriptRecord } from "./transcript.js";

const identity = (text: string): string => text;

/** Minimal spawn surface the app needs; injectable for deterministic tests. */
export interface SpawnFn {
  (command: string, args: string[]): {
    on(event: "error", listener: (error: Error) => void): void;
    on(event: "exit", listener: (code: number | null) => void): void;
  };
}

const EDITOR_THEME: EditorTheme = {
  borderColor: identity,
  selectList: {
    selectedPrefix: (text) => `\x1b[1m${text}\x1b[22m`,
    selectedText: (text) => `\x1b[1m${text}\x1b[22m`,
    description: identity,
    scrollInfo: identity,
    noMatch: identity,
  },
};

/** Recover the exact typed text after pi-tui's Editor trims on submit. */
export function resolveExactText(lastExactText: string, submitted: string): string {
  return lastExactText.trim() === submitted ? lastExactText : submitted;
}

/**
 * Exact Ctrl-C test for the exit policy. Uses matchesKey/Key so both the raw
 * legacy control character (\x03) and Kitty-keyboard-protocol encodings
 * (e.g. CSI 99;5u) are recognized.
 */
export function isCtrlC(data: string): boolean {
  return matchesKey(data, Key.ctrl("c"));
}

export type SubmissionKind =
  | { type: "view"; view: "inbox" | "room"; notice: string }
  | { type: "dispatch"; text: string };

/**
 * Exact `/inbox` and `/room` (after whitespace strip, matching the Python
 * `handle_view_command`) are local view switches and never dispatch. Exact
 * typed text is preserved for every dispatch.
 */
export function classifySubmission(submittedText: string): SubmissionKind {
  const stripped = submittedText.trim();
  if (stripped === "/inbox") {
    return {
      type: "view",
      view: "inbox",
      notice:
        "Inbox shows final replies, syntheses, and attention items. Use /room to return.",
    };
  }
  if (stripped === "/room") {
    return { type: "view", view: "room", notice: "Showing the full room transcript." };
  }
  return { type: "dispatch", text: submittedText };
}

export interface AppOptions {
  transcriptPath: string;
  agents: string[];
  backend: BackendConfig;
}

/** Incremental follower for the append-only transcript. */
export class TranscriptFollower {
  private handle: FileHandle | null = null;
  private offset = 0;
  private partial = "";
  readonly records: TranscriptRecord[] = [];

  constructor(private readonly path: string) {}

  async start(): Promise<void> {
    await this.openIfPresent();
    await this.poll();
  }

  /** Open the transcript read-only if it exists; never create it. */
  private async openIfPresent(): Promise<void> {
    if (this.handle !== null) return;
    try {
      this.handle = await open(this.path, "r");
    } catch (error) {
      // A fresh room has no transcript until the relay writes its first
      // record. The frontend is never the writer, so ENOENT is tolerated and
      // retried on poll; every other error surfaces.
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
  }

  /** Poll for appended bytes; return true when new records appeared. */
  async poll(): Promise<boolean> {
    await this.openIfPresent();
    const handle = this.handle;
    if (handle === null) return false;
    const { size } = await handle.stat();
    if (size <= this.offset) return false;
    const buffer = Buffer.alloc(size - this.offset);
    await handle.read(buffer, 0, buffer.length, this.offset);
    this.offset = size;
    this.partial += buffer.toString("utf8");
    const lines = this.partial.split("\n");
    this.partial = lines.pop() ?? "";
    let appended = false;
    for (const line of lines) {
      if (line.trim() === "") continue;
      // New malformed appended lines surface as a status flash, not a crash.
      this.records.push(...parseTranscriptText(line));
      appended = true;
    }
    return appended;
  }

  async stop(): Promise<void> {
    await this.handle?.close();
    this.handle = null;
  }
}

export class ChatApp {
  private readonly tui: TuiAltScreen;
  private readonly editor: Editor;
  private readonly transcriptStack = new VStack([], { gap: 1 });
  private readonly status: Text = new Text("");
  private readonly layoutRoot: VStack;
  private readonly follower: TranscriptFollower;
  private readonly backend: BackendConfig;
  private view: "room" | "inbox" = "room";
  private child: ReturnType<SpawnFn> | null = null;
  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private lastExactText = "";
  private readonly spawnBackend: SpawnFn;
  private spawnedArgv: string[][] = [];

  constructor(
    terminal: import("@earendil-works/pi-tui").Terminal,
    private readonly options: AppOptions,
    follower: TranscriptFollower,
    spawnBackend: SpawnFn = (command, args) =>
      spawn(command, args, { shell: false, stdio: ["ignore", "ignore", "ignore"] }),
  ) {
    this.spawnBackend = spawnBackend;
    this.follower = follower;
    this.backend = options.backend;
    this.tui = new TuiAltScreen(terminal, true);

    this.editor = new Editor(this.tui, EDITOR_THEME, { paddingX: 1 });
    this.editor.setAutocompleteProvider(
      new GroupChatAutocompleteProvider(options.agents),
    );
    this.editor.onChange = (text: string): void => {
      this.lastExactText = text;
    };
    this.editor.onSubmit = (text: string): void => {
      const exact = resolveExactText(this.lastExactText, text);
      this.submit(exact);
    };

    // The composer MUST live inside the explicit layout root: TuiAltScreen
    // mounts only layoutRoot when set, so an editor added to the TUI itself
    // would be focused but never rendered.
    this.layoutRoot = new VStack(
      [
        {
          component: new ScrollView(this.transcriptStack, {
            primary: true,
            follow: "end",
          }),
          grow: 1,
          basis: 0,
          minSize: 1,
        },
        this.status,
        this.editor,
      ],
      { gap: 1 },
    );
    this.tui.setLayoutRoot(this.layoutRoot);
    this.tui.setFocus(this.editor);

    // Ctrl-C policy: while a dispatch child is running, refuse to exit and
    // leave the child running. Once idle, Ctrl-C exits cleanly. No /cancel and
    // no signals or terminal keys are ever sent to participants.
    this.tui.addInputListener((data: string) => {
      if (!isCtrlC(data)) return undefined;
      if (this.child !== null) {
        this.tui.flash("dispatch in progress — Ctrl-C ignored, child left running");
        this.tui.requestRender();
        return { consume: true };
      }
      void this.shutdown();
      return { consume: true };
    });
  }

  async start(): Promise<void> {
    this.renderRecords();
    this.setStatus("ready");
    this.tui.start();
    this.pollTimer = setInterval(() => {
      void this.follower
        .poll()
        .then((appended) => {
          if (appended) {
            this.renderRecords();
            this.tui.requestRender();
          }
        })
        .catch((error: unknown) => {
          this.setStatus(`transcript error: ${(error as Error).message}`);
          this.tui.requestRender();
        });
    }, 500);
  }

  /** The explicitly mounted layout; exposed for structural tests. */
  get currentView(): "room" | "inbox" {
    return this.view;
  }

  /** Argv of every spawned dispatch, for sole-writer/view tests. */
  get dispatchedArgv(): readonly string[][] {
    return this.spawnedArgv;
  }

  get mountedLayout(): Component {
    return this.layoutRoot;
  }

  private renderRecords(): void {
    const children: Component[] = [];
    const source =
      this.view === "inbox"
        ? inboxMessages(this.follower.records)
        : this.follower.records;
    for (const record of source) {
      const recipients =
        record.recipients.length > 0 ? record.recipients.join(", ") : "all";
      children.push(new Text(`${record.sender} -> ${recipients}`));
      children.push(new Markdown(record.body, 1, 0, {
        heading: identity,
        link: identity,
        linkUrl: identity,
        code: identity,
        codeBlock: identity,
        codeBlockBorder: identity,
        quote: identity,
        quoteBorder: identity,
        hr: identity,
        listBullet: identity,
        bold: identity,
        italic: identity,
        strikethrough: identity,
        underline: identity,
      }));
    }
    this.transcriptStack.clear();
    for (const child of children) this.transcriptStack.addChild(child);
    this.transcriptStack.invalidate();
  }

  private setStatus(text: string): void {
    this.status.setText(text);
  }

  /** Submit entry point; public for deterministic tests. */
  submit(exactText: string): void {
    if (exactText.length === 0) return;
    const submission = classifySubmission(exactText);
    if (submission.type === "view") {
      // Presentation-only local view switch; never builds or spawns backend argv.
      this.view = submission.view;
      this.setStatus(submission.notice);
      this.renderRecords();
      this.tui.requestRender();
      return;
    }
    if (this.child !== null) {
      this.tui.flash("a dispatch is already active");
      return;
    }
    const argv = buildSpawnArgv(this.backend, submission.text);
    this.editor.disableSubmit = true;
    this.setStatus(`dispatching… (${argv.length} argv elements)`);
    this.tui.requestRender();
    this.spawnedArgv.push(argv);
    const child = this.spawnBackend(argv[0] ?? "", argv.slice(1));
    this.child = child;
    const settle = (outcome: string, code: number | null): void => {
      if (this.child === child) this.child = null;
      this.editor.disableSubmit = false;
      this.setStatus(`${outcome} (exit ${code ?? "n/a"})`);
      this.tui.requestRender();
    };
    child.on("error", (error: Error) => {
      settle(`dispatch failed: ${error.message}`, null);
    });
    child.on("exit", (code) => {
      settle("dispatch complete", code);
    });
  }

  async shutdown(): Promise<void> {
    if (this.pollTimer !== null) clearInterval(this.pollTimer);
    this.pollTimer = null;
    await this.follower.stop();
    this.tui.stop();
    process.exit(0);
  }
}
