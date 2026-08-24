/**
 * Parsing for append-only Herdr Group Chat transcript JSONL.
 *
 * The frontend is read-only here: it parses and renders records but never
 * appends. The Python relay stays the sole transcript writer.
 */

export interface TranscriptRecord {
  seq: number;
  at: string;
  sender: string;
  recipients: string[];
  body: string;
  kind: string;
  /** True for provisional records (e.g. draft review syntheses). */
  provisional: boolean;
}

export class TranscriptError extends Error {}

/** Parse one JSONL line into a record, or throw TranscriptError. */
export function parseTranscriptLine(line: string, lineNumber: number): TranscriptRecord {
  const where = `transcript line ${lineNumber}`;
  let item: unknown;
  try {
    item = JSON.parse(line);
  } catch (error) {
    throw new TranscriptError(`${where}: invalid JSON (${(error as Error).message})`);
  }
  if (typeof item !== "object" || item === null || Array.isArray(item)) {
    throw new TranscriptError(`${where}: not a JSON object`);
  }
  const record = item as Record<string, unknown>;
  if (typeof record["seq"] !== "number" || !Number.isInteger(record["seq"])) {
    throw new TranscriptError(`${where}: missing or non-integer "seq"`);
  }
  if (typeof record["sender"] !== "string") {
    throw new TranscriptError(`${where}: missing or non-string "sender"`);
  }
  if (typeof record["body"] !== "string") {
    throw new TranscriptError(`${where}: missing or non-string "body"`);
  }
  if (typeof record["kind"] !== "string") {
    throw new TranscriptError(`${where}: missing or non-string "kind"`);
  }
  const at = record["at"];
  const recipients = record["recipients"];
  return {
    seq: record["seq"],
    at: typeof at === "string" ? at : "",
    sender: record["sender"],
    recipients: Array.isArray(recipients)
      ? recipients.filter((r): r is string => typeof r === "string")
      : [],
    body: record["body"],
    kind: record["kind"],
    provisional: record["provisional"] === true,
  };
}

/**
 * Presentation-only inbox projection, mirroring the Python relay's
 * `inbox_messages`: exclude human turns; keep messages, non-provisional
 * review syntheses, anneal finals, and attention-bearing review statuses.
 */
export function inboxMessages(records: TranscriptRecord[]): TranscriptRecord[] {
  const attentionMarkers = ["blocked", "failed", "timed out", "cancelled"];
  return records.filter((record) => {
    if (record.sender === "human") return false;
    if (record.kind === "anneal_final") return true;
    if (record.kind === "review_synthesis" && !record.provisional) return true;
    if (record.kind === "review_status") {
      const body = record.body.toLowerCase();
      return attentionMarkers.some((marker) => body.includes(marker));
    }
    return record.kind === "message";
  });
}

/** Parse a full JSONL transcript text (blank lines are skipped). */
export function parseTranscriptText(text: string): TranscriptRecord[] {
  const records: TranscriptRecord[] = [];
  let lineNumber = 0;
  for (const line of text.split("\n")) {
    lineNumber += 1;
    if (line.trim() === "") continue;
    records.push(parseTranscriptLine(line, lineNumber));
  }
  return records;
}
