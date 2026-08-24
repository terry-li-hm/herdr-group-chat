# Experimental standalone pi-tui frontend

This prototype tests `@earendil-works/pi-tui` as a peer-neutral interface for Herdr Group Chat. It does not run a Pi agent session, replace the Python relay, or write the room transcript.

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
