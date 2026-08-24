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
  Markdown,
  ProcessTerminal,
  ScrollView,
  Text,
  TuiAltScreen,
  VStack,
  type Component,
} from "@earendil-works/pi-tui";
import { GroupChatAutocompleteProvider } from "./autocomplete.js";
import { buildSpawnArgv, type BackendConfig } from "./argv.js";
import { parseTranscriptText, type TranscriptRecord } from "./transcript.js";

const identity = (text: string): string => text;

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
    this.handle = await open(this.path, "r");
    await this.poll();
  }

  /** Poll for appended bytes; return true when new records appeared. */
  async poll(): Promise<boolean> {
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
  private readonly follower: TranscriptFollower;
  private readonly backend: BackendConfig;
  private child: ReturnType<typeof spawn> | null = null;
  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private lastExactText = "";

  constructor(
    terminal: import("@earendil-works/pi-tui").Terminal,
    private readonly options: AppOptions,
    follower: TranscriptFollower,
  ) {
    this.follower = follower;
    this.backend = options.backend;
    this.tui = new TuiAltScreen(terminal, true);
    this.tui.setLayoutRoot(
      new VStack(
        [
          new ScrollView(this.transcriptStack, { primary: true, follow: "end" }),
          this.status,
        ],
        { gap: 1 },
      ),
    );

    this.editor = new Editor(this.tui, EDITOR_THEME, { paddingX: 1 });
    this.editor.setAutocompleteProvider(
      new GroupChatAutocompleteProvider(options.agents),
    );
    this.editor.onChange = (text: string): void => {
      this.lastExactText = text;
    };
    this.editor.onSubmit = (text: string): void => {
      const exact = resolveExactText(this.lastExactText, text);
      this.handleSubmit(exact);
    };
    this.tui.addChild(this.editor);
    this.tui.setFocus(this.editor);

    // Ctrl-C policy: while a dispatch child is running, refuse to exit and
    // leave the child running. Once idle, Ctrl-C exits cleanly. No /cancel and
    // no signals or terminal keys are ever sent to participants.
    this.tui.addInputListener((data: string) => {
      if (data !== "\x03") return undefined;
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

  private renderRecords(): void {
    const children: Component[] = [];
    for (const record of this.follower.records) {
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

  private handleSubmit(exactText: string): void {
    if (exactText.length === 0) return;
    if (this.child !== null) {
      this.tui.flash("a dispatch is already active");
      return;
    }
    const argv = buildSpawnArgv(this.backend, exactText);
    this.editor.disableSubmit = true;
    this.setStatus(`dispatching… (${argv.length} argv elements)`);
    this.tui.requestRender();
    const child = spawn(argv[0] ?? "", argv.slice(1), {
      stdio: ["ignore", "ignore", "ignore"],
      shell: false,
    });
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
