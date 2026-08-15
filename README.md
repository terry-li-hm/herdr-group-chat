# herdr-group-chat

A thin Herdr plugin and local group-chat TUI for full native agents. It keeps
Pi, Claude Code, Codex, and Grok Build in their own terminal sessions while a
local relay provides `@pi`, `@claude`, `@codex`, `@grok`, and `@all` addressing.

Messages are delivered serially, capped at four agent turns by default, and
recorded as append-only JSONL in the resolved state directory. Each agent
receives only group messages added since its previous delivered turn.

```bash
herdr-group-chat
herdr-group-chat --once "@all Compare these options."
herdr-group-chat --show
```

The default Herdr targets are `pi-peer`, `claude-peer`, `codex-peer`, and
`grok-peer`. Override one with `--agent pi=another-live-name`.

Link the native plugin from this checkout:

```bash
herdr plugin link . --enabled
herdr plugin action invoke open --plugin terry.herdr-group-chat
```

The plugin declares one action and one managed room pane. Herdr supplies
`HERDR_PLUGIN_STATE_DIR`, so plugin rooms stay in Herdr's plugin state location;
standalone CLI use retains `~/.local/state/herdr-group-chat/`. An explicit
`--state-dir` always wins.

Run the focused gate with:

```bash
uv run --with pytest pytest -q
ruff check herdr-group-chat assays/test_herdr_group_chat.py
ruff format --check herdr-group-chat assays/test_herdr_group_chat.py
```
