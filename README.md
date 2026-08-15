# herdr-group-chat

A thin Herdr plugin and local group-chat TUI for full native agents. It keeps
Pi, Claude Code, Codex, and Grok Build in their own terminal sessions while a
local relay provides `@pi`, `@claude`, `@codex`, `@grok`, and `@all` addressing.

> Preview release for Herdr 0.8.0 or newer on macOS and Linux.

Messages are delivered serially, capped at four agent turns by default, and
recorded as append-only JSONL in the resolved state directory. Each agent
receives only group messages added since its previous delivered turn.

## Install

Install the tagged release from GitHub:

```bash
herdr plugin install terry-li-hm/herdr-group-chat --ref v0.2.0
herdr plugin action invoke open --plugin terry.herdr-group-chat
```

The agents must already be running in Herdr under the default names
`pi-peer`, `claude-peer`, `codex-peer`, and `grok-peer`.

## Use

Plain messages address everyone. Prefix a message to select participants:

```text
@all Review this email draft and agree the two most important changes.
@claude,@codex Challenge the proposed architecture.
@pi Summarise the decision.
```

A verified four-agent round looks like:

```text
human> Reply exactly HERDR_PLUGIN_FINAL_OK.
pi> HERDR_PLUGIN_FINAL_OK
claude> HERDR_PLUGIN_FINAL_OK
codex> HERDR_PLUGIN_FINAL_OK
grok> HERDR_PLUGIN_FINAL_OK
```

The standalone CLI remains available:

```bash
./herdr-group-chat
./herdr-group-chat --once "@all Compare these options."
./herdr-group-chat --show
```

For local development, link the checkout instead:

```bash
herdr plugin link . --enabled
herdr plugin action invoke open --plugin terry.herdr-group-chat
```

Uninstall a GitHub-managed copy with:

```bash
herdr plugin uninstall terry.herdr-group-chat
```

The plugin declares one action and one managed room pane. Herdr supplies
`HERDR_PLUGIN_STATE_DIR`, so plugin rooms stay in Herdr's plugin state location;
standalone CLI use retains `~/.local/state/herdr-group-chat/`. An explicit
`--state-dir` always wins.

The plugin contains no network client and does not broaden agent permissions.
It relays prompts through Herdr to each native agent, so the agent provider's
own data-handling terms still apply. Room transcripts contain message content;
Herdr stores them locally and the relay creates them with user-only permissions.

## Limitations

- Turns are serial and capped at four by default.
- Every addressed agent must already be live in Herdr.
- Rooms are local to one Herdr installation; this is not a network chat server.
- The preview release supports macOS and Linux.

## Development

Run the focused gate with:

```bash
uv run --with pytest pytest -q
ruff check herdr-group-chat assays/test_herdr_group_chat.py
ruff format --check herdr-group-chat assays/test_herdr_group_chat.py
sh -n open-room
```

See [SECURITY.md](SECURITY.md) for private vulnerability reporting and
[CHANGELOG.md](CHANGELOG.md) for release history.
