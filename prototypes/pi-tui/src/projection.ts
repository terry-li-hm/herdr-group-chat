/**
 * Deterministic plain-text projection for --render-fixture mode.
 *
 * No ANSI, no TUI, no backend: given parsed transcript records, print a stable
 * projection that can be diffed or asserted in tests.
 */

import type { TranscriptRecord } from "./transcript.js";

/** Strip a small deterministic subset of Markdown to plain text. */
export function markdownToPlainText(markdown: string): string {
  const lines = markdown.split("\n").map((line) => {
    let out = line;
    // Fenced code blocks are kept verbatim aside from the fence markers.
    out = out.replace(/```(\w*)\s*$/, "");
    out = out.replace(/^\s*>\s?/, "");
    out = out.replace(/^(\s*)[-*+]\s+/, "$1  • ");
    out = out.replace(/^(\s*)(\d+)\.\s+/, "$1$2) ");
    out = out.replace(/^#{1,6}\s+/, "");
    // Inline emphasis and code markers.
    out = out.replace(/\*\*([^*]+)\*\*/g, "$1");
    out = out.replace(/\*([^*]+)\*/g, "$1");
    out = out.replace(/`([^`]+)`/g, "$1");
    out = out.replace(/\[([^\]]+)\]\([^)]+\)/g, "$1");
    return out.replace(/\s+$/, "");
  });
  return lines.join("\n");
}

/** Project one record to a stable plain-text block. */
export function projectRecord(record: TranscriptRecord): string {
  const recipients =
    record.recipients.length > 0 ? record.recipients.join(",") : "all";
  const header = `[${record.seq}] ${record.sender} -> ${recipients} (${record.kind})`;
  const body = markdownToPlainText(record.body);
  return `${header}\n${body}`;
}

/** Project all records, separated by blank lines. */
export function projectTranscript(records: TranscriptRecord[]): string {
  return records.map(projectRecord).join("\n\n");
}
