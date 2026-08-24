/**
 * Autocomplete for the composer: explicit --agent NAME values via @NAME
 * mentions and the four supported slash commands at the start of the first
 * line.
 */

import type {
  AutocompleteItem,
  AutocompleteProvider,
  AutocompleteSuggestions,
} from "@earendil-works/pi-tui";

export const SLASH_COMMANDS: AutocompleteItem[] = [
  { value: "/review", label: "/review", description: "run a review round" },
  { value: "/anneal", label: "/anneal", description: "run an anneal round" },
  { value: "/inbox", label: "/inbox", description: "show undelivered messages" },
  { value: "/room", label: "/room", description: "show room status" },
];

export class GroupChatAutocompleteProvider implements AutocompleteProvider {
  private readonly agents: string[];

  constructor(agents: string[]) {
    this.agents = [...new Set(agents)].sort();
  }

  getAgentNames(): string[] {
    return [...this.agents];
  }

  async getSuggestions(
    lines: string[],
    cursorLine: number,
    cursorCol: number,
  ): Promise<AutocompleteSuggestions | null> {
    const line = lines[cursorLine] ?? "";
    const before = line.slice(0, cursorCol);

    // Slash commands only at the very start of the transcript.
    if (cursorLine === 0 && before.startsWith("/") && !before.includes(" ")) {
      const prefix = before;
      const items = SLASH_COMMANDS.filter((command) =>
        command.value.startsWith(prefix),
      );
      if (items.length === 0) return null;
      return { items, prefix };
    }

    // @agent mentions at a token boundary.
    const mention = before.match(/(?:^|\s)@([\w-]*)$/);
    if (mention) {
      const prefix = `@${mention[1] ?? ""}`;
      const items: AutocompleteItem[] = this.agents
        .filter((agent) => agent.startsWith(mention[1] ?? ""))
        .map((agent) => ({ value: `@${agent}`, label: `@${agent}` }));
      if (items.length === 0) return null;
      return { items, prefix };
    }
    return null;
  }

  applyCompletion(
    lines: string[],
    cursorLine: number,
    cursorCol: number,
    item: AutocompleteItem,
    prefix: string,
  ): { lines: string[]; cursorLine: number; cursorCol: number } {
    const next = [...lines];
    const line = next[cursorLine] ?? "";
    const start = cursorCol - prefix.length;
    next[cursorLine] = line.slice(0, start) + item.value + " " + line.slice(cursorCol);
    return { lines: next, cursorLine, cursorCol: start + item.value.length + 1 };
  }
}
