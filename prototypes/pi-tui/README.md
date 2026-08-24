# Experimental standalone pi-tui frontend

This prototype tests `@earendil-works/pi-tui` as a peer-neutral interface for Herdr Group Chat. It does not run a Pi agent session, replace the Python relay, or write the room transcript.

**Status:** evaluated on 24 August 2026 and retained as a frozen reference. The production room keeps the curses UI.

## Evaluation outcome

A live A/B assay ran both interfaces against the same relay and append-only transcript. Four Claude Haiku processes filled every participant role, including `@pi`, so the successful round proved that this frontend does not require a Pi runtime. A turn submitted through curses appeared automatically in the pi-tui view, confirming that the relay remained the sole writer.

Curses remains the better production interface:

- it shows the room, participant readiness, delivery state, and input prompt more compactly;
- it preserves substantially more transcript context in the same viewport;
- it reports an actionable failure such as `participant not live in Herdr: @pi`, while this prototype reports only the child exit code;
- it avoids a second language toolchain and 683 lines of TypeScript UI source.

Do not wire this prototype into `herdr-plugin.toml` or add a standing Node CI lane. Revisit it only if a concrete requirement such as rich Markdown or interactive terminal components cannot be met cleanly in curses. Any renewed candidate must first match curses on information density, room and participant status, actionable errors, sole-writer behavior, and operation without a Pi process.

## Verify it

```bash
npm ci
npm test
npm run typecheck
npm run smoke
```

## Try it with a classic standalone room

Start the named Pi, Claude, Codex, and Grok peers in Herdr first. Then run:

```bash
npm run build
node dist/main.js \
  --transcript ~/.local/state/herdr-group-chat/cockpit.jsonl \
  --agent pi --agent claude --agent codex --agent grok \
  --backend ../../herdr-group-chat \
  --backend-arg --room --backend-arg cockpit \
  --backend-arg --state-dir \
  --backend-arg ~/.local/state/herdr-group-chat
```

The frontend may open before the transcript exists. The relay remains its sole writer and creates the file after the first submitted message.

`/inbox` and `/room` switch local views. `/review` and `/anneal` go to the relay. The prototype deliberately has no `/cancel`. It refuses Ctrl-C while a relay dispatch is active and exits after that child settles.

This is not wired into `herdr-plugin.toml`. See [CONTRACT.md](CONTRACT.md) for the architecture and acceptance boundary.
