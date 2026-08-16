# Herdr Group Chat

A thin Herdr plugin and local group-chat TUI for full native agents. It keeps
Pi, Claude Code, Codex, and Grok Build in their own terminal sessions while a
local relay provides `@pi`, `@claude`, `@codex`, `@grok`, and `@all` addressing.

> Preview release for Herdr 0.8.0 or newer on macOS and Linux.

Ordinary messages are delivered serially and capped at four agent turns by
default. Review rounds run their independent first-pass calls concurrently,
then ask one configured agent to synthesize the collected answers. All messages
are recorded as append-only JSONL in the resolved state directory.

## Install

Prerequisites are Herdr 0.8.0 or newer, [`uv`](https://docs.astral.sh/uv/), and
installed, authenticated Pi, Claude Code, Codex, and Grok Build CLIs. The room
executable uses `uv` to run Python 3.13.

Install the pinned release from GitHub:

```bash
herdr plugin install terry-li-hm/herdr-group-chat --ref v0.2.0
herdr plugin action invoke new --plugin terry.herdr-group-chat
```

For local development, use `herdr plugin link .` from the checkout instead.

## Use

Plain messages address everyone. Prefix a message to select participants:

```text
@all Review this email draft and agree the two most important changes.
@claude,@codex Challenge the proposed architecture.
@pi Summarise the decision.
```

Use `/review` when the agents should reach their views independently before any
answer can influence another. Mentions select reviewers; without mentions, all
participants review. Pi synthesizes by default.

```text
/review Review this email draft and recommend the final wording.
/review @claude,@codex Challenge this architecture.
/cancel
/retry claude
/retry synthesis
```

The room stays responsive while a review runs and shows each participant as
queued, working, blocked, done, failed, timed out, cancelled, synthesizing, or
skipped. `/cancel` interrupts only active participant tabs. A failed, blocked,
timed-out, or cancelled first pass can be retried without rerunning the other
reviewers; a successful retry triggers a fresh synthesis. Use Page Up and Page
Down to scroll the transcript while work continues.

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
./herdr-group-chat --once "/review @claude,@codex Compare these options."
./herdr-group-chat --synthesizer codex --agent-timeout grok=900000
./herdr-group-chat --show
```

Set `HERDR_GROUP_CHAT_SYNTHESIZER` or pass `--synthesizer` to select Pi,
Claude, Codex, or Grok for phase two. `--agent-timeout NAME=MILLISECONDS`
overrides the default timeout for one participant and may be repeated.

Uninstall a GitHub-managed copy with:

```bash
herdr plugin uninstall terry.herdr-group-chat
```

The manifest declares two actions and two managed pane entrypoints: **New group
chat**, **Open group chat**, the visible setup pane, and the room pane. Herdr
supplies `HERDR_PLUGIN_STATE_DIR`, so transcripts and the plugin's exact
workspace and pane identifiers stay in a server-instance-scoped, locked state file. This
keeps multiple Herdr sessions separate and serializes overlapping launcher and
setup processes. The launcher never claims a workspace merely because its label
is `agents · group-chat` or `group-chat`. Standalone CLI use retains
`~/.local/state/herdr-group-chat/`; an explicit `--state-dir` always wins.

Invoking **New group chat** opens a visible setup tab, reuses any live named
peers, creates tabs for missing Pi, Claude, Codex, and Grok participants, and
then becomes a fresh `group-chat` room. It replaces the previous plugin-owned
room pane but retains that room's transcript. **Open group chat** focuses the
exact recorded plugin pane or reopens the last room without starting models.
The transcript and input remain visible while you switch among participant
tabs. Failed cleanup remains recorded for a later retry instead of silently
losing ownership of the old pane.

Compact mode keeps the room alone in the initiating workspace and places the
four native agent tabs in a secondary `agents · group-chat` workspace, with
each participant tab labeled `<kind> · group-chat` (for example `pi ·
group-chat`). These labels are display-only; routing and reuse still key on the
exact recorded workspace and pane identifiers. Enter `/agents` to
reveal that workspace or `/show pi`, `/show claude`, `/show codex`, or `/show
grok` to focus one native agent directly. Use Herdr's workspace switcher or
**Open group chat** to return.

If Codex reports unreviewed lifecycle hooks during startup, setup detects the
dialog even when the pane read clips its text, retries briefly while it renders,
and leaves those hooks inactive: it closes the summary notice or chooses
**Continue without trusting** in the menu variant. It never selects **trust
all** for you.

Like every Herdr plugin, this is ordinary local code running as your user and it
can call the full Herdr CLI. Review the manifest and executable scripts before
installing, and pin a trusted release tag. The plugin contains no separate
network client and does not broaden agent permissions, but the native agents it
prompts may use their normal network connections and tools. Room transcripts
contain message content; the relay stores them locally with user-only
permissions. Each participant applies its own disclosure rules. For an
explicitly bounded disclosure, an eligible participant verifies its active
route and records a non-secret `ROUTE_RECEIPT`; addressing `@all` never
transfers one agent's eligibility to another.

The relay waits up to ten minutes for an agent turn so an interactive approval
can be completed in the native agent pane. Grok's alternate-screen TUI can hide
a completed reply above the visible viewport; when that happens, the plugin
recovers only the token-bound reply from the active local Grok session history.

The review protocol and remaining v0.3 boundaries are specified in
[docs/v0.3-review.md](docs/v0.3-review.md).

## Limitations

- Ordinary group turns remain serial; only `/review` first passes are parallel.
- Every addressed agent must already be live in Herdr.
- New-room setup starts all four default participants; participant selection is
  not yet configurable.
- Retry state is kept in the running room process and does not survive a room
  restart. The transcript itself remains durable.
- Cancellation sends Ctrl-C to the exact active Herdr agent tab, and only when
  the agent was observed working: an idle Codex interprets Ctrl-C as quit,
  which previously killed the participant process on a timeout whose prompt
  never landed. It remains best-effort because the underlying CLI has no
  stronger cancellation primitive.
- Rooms are local to one Herdr installation; this is not a network chat server.
- An agent that finishes without emitting the `HGCHAT_REPLY_*` markers fails the
  turn fast with a clear error once its terminal output is observed stable,
  instead of stalling until the full agent timeout. A generic unmarked-reply
  extractor is deliberately not attempted: terminal output has no reliable
  reply boundaries.
- Grok session recovery depends on the local `~/.grok/sessions` history layout.
- The preview release supports macOS and Linux.

## Development

Run the focused gate with:

```bash
uv run --with pytest pytest -q
uv run ruff check herdr-group-chat new-room assays
uv run ruff format --check herdr-group-chat new-room assays
```

See [SECURITY.md](SECURITY.md) for private vulnerability reporting and
[CHANGELOG.md](CHANGELOG.md) for release history.
