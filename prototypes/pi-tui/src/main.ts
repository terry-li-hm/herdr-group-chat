/**
 * CLI entry point.
 *
 *   --transcript PATH          append-only Herdr Group Chat JSONL to render
 *   --agent NAME               repeatable; enables @NAME autocomplete
 *   --backend PATH             backend command; required for the interactive TUI
 *   --backend-arg VALUE        repeatable; extra argv elements before --once
 *   --render-fixture PATH      parse and print the plain-text projection, no TUI
 */

import { readFile } from "node:fs/promises";
import process from "node:process";
import { ProcessTerminal } from "@earendil-works/pi-tui";
import { ChatApp, TranscriptFollower } from "./app.js";
import type { BackendConfig } from "./argv.js";
import { projectTranscript } from "./projection.js";
import { parseTranscriptText, TranscriptError } from "./transcript.js";

export interface CliArgs {
  transcript: string | null;
  agents: string[];
  backend: BackendConfig | null;
  renderFixture: string | null;
}

export function parseArgs(argv: string[]): CliArgs {
  const args: CliArgs = {
    transcript: null,
    agents: [],
    backend: null,
    renderFixture: null,
  };
  let backendCommand: string | null = null;
  const backendArgs: string[] = [];
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i] ?? "";
    const next = (): string => {
      i += 1;
      const value = argv[i];
      if (value === undefined) {
        throw new Error(`${arg} requires a value`);
      }
      return value;
    };
    if (arg === "--transcript") args.transcript = next();
    else if (arg === "--agent") args.agents.push(next());
    else if (arg === "--backend") backendCommand = next();
    else if (arg === "--backend-arg") backendArgs.push(next());
    else if (arg === "--render-fixture") args.renderFixture = next();
    else throw new Error(`unknown argument: ${arg}`);
  }
  if (backendCommand !== null) args.backend = { command: backendCommand, args: backendArgs };
  return args;
}

function usage(): never {
  process.stderr.write(
    "usage: main.js --transcript PATH [--agent NAME ...] " +
      "[--backend PATH] [--backend-arg VALUE ...] | --render-fixture PATH\n",
  );
  process.exit(2);
}

export async function main(argv: string[]): Promise<void> {
  let args: CliArgs;
  try {
    args = parseArgs(argv);
  } catch (error) {
    process.stderr.write(`error: ${(error as Error).message}\n`);
    usage();
  }

  if (args.renderFixture !== null) {
    const text = await readFile(args.renderFixture, "utf8");
    const records = parseTranscriptText(text);
    process.stdout.write(projectTranscript(records) + "\n");
    return;
  }

  if (args.transcript === null) usage();
  if (args.backend === null) usage();

  const follower = new TranscriptFollower(args.transcript);
  try {
    await follower.start();
  } catch (error) {
    if (error instanceof TranscriptError) {
      process.stderr.write(`error: ${error.message}\n`);
      process.exit(1);
    }
    process.stderr.write(
      `error: cannot read transcript ${args.transcript}: ${(error as Error).message}\n`,
    );
    process.exit(1);
  }

  const app = new ChatApp(new ProcessTerminal(), {
    transcriptPath: args.transcript,
    agents: args.agents,
    backend: args.backend,
  }, follower);
  await app.start();
}

const entry = process.argv[1];
if (entry !== undefined && entry.includes("main.js")) {
  void main(process.argv.slice(2)).catch((error: unknown) => {
    process.stderr.write(`error: ${(error as Error).message}\n`);
    process.exit(1);
  });
}
