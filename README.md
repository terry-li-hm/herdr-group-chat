# herdr-group-chat

A thin local group-chat TUI for full agents already running in Herdr. It keeps
Pi, Claude Code, and Grok Build in their native terminal sessions while a local
relay provides `@pi`, `@claude`, `@grok`, and `@all` addressing.

Messages are delivered serially, capped at three agent turns by default, and
recorded as append-only JSONL under `~/.local/state/herdr-group-chat/`. Each
agent receives only group messages added since its previous delivered turn.

```bash
herdr-group-chat
herdr-group-chat --once "@all Compare these options."
herdr-group-chat --show
```

The default Herdr targets are `pi-peer`, `claude-peer`, and `grok-peer`. Override
one with `--agent pi=another-live-name`.

Run the focused gate with:

```bash
uv run --with pytest pytest -q
ruff check herdr-group-chat assays/test_herdr_group_chat.py
ruff format --check herdr-group-chat assays/test_herdr_group_chat.py
```
