/**
 * Backend argv construction.
 *
 * Dispatch is always `spawn` with an argv array — never a shell — so message
 * text containing spaces, quotes, or newlines is passed verbatim as one argv
 * element. The frontend never interprets or writes the transcript itself.
 */

export interface BackendConfig {
  command: string;
  args: string[];
}

/** The exact argv used to submit one message. */
export function buildArgv(backend: BackendConfig, submittedText: string): string[] {
  return [...backend.args, "--once", submittedText];
}

/** Full spawn argv including the backend command path. */
export function buildSpawnArgv(backend: BackendConfig, submittedText: string): string[] {
  return [backend.command, ...buildArgv(backend, submittedText)];
}
