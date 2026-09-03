# Herdr Group Chat

A thin Herdr plugin and local group-chat TUI for full native agents. It keeps
Pi, Claude Code, Codex, and Grok Build in their own terminal sessions while a
local relay provides `@pi`, `@claude`, `@codex`, `@grok`, and `@all` addressing.

> Preview release for Herdr 0.8.0 or newer on macOS and Linux.

Ordinary messages are delivered serially and capped at four agent turns by
default. Review rounds run their independent first-pass calls concurrently,
then ask one configured agent to synthesize the collected answers. All messages
are recorded as append-only JSONL in the resolved state directory.

## Architecture boundary

Herdr Group Chat stays a local relay for visible native agents. It does not use
Cumora as its backend. Cumora mechanisms are adopted only when a focused assay
shows the matching failure here. A future networked mode would instead make
this TUI a thin Cumora client, with one backend owner and no dual writes. See
[the Cumora boundary decision](docs/cumora-boundary.md).

## Install

Prerequisites are Herdr 0.8.0 or newer, [`uv`](https://docs.astral.sh/uv/), and
installed, authenticated Pi, Claude Code, Codex, and Grok Build CLIs. The room
executable uses `uv` to run Python 3.13 and installs `regex` plus `wcwidth` for
grapheme-safe terminal-cell layout. The `sol-fable-glm` and
`sol-fable-grok-pi` profiles additionally need Pi configured and authenticated
for their `bigmodel-coding` (`glm-5.3`) and `xai` (`grok-4.6`) providers.

Install the pinned release from GitHub:

```bash
herdr plugin install terry-li-hm/herdr-group-chat --ref v0.10.3
herdr plugin action invoke new --plugin terry.herdr-group-chat
```

For local development, use `herdr plugin link .` from the checkout instead.

## Release smoke

The deterministic `release-smoke` harness replaces the manual candidate and
post-install smoke setup. Both subcommands use argv Herdr subprocesses only,
print human-readable progress to stderr, and print exactly one stable JSON
result to stdout (`"ok": true` and exit 0 on success; failures name the
failed stage — including a cleanup stage — and cleanup still runs; a failed
cleanup can never report success with a surviving session or temp link).
Invalid paths and unexpected runtime errors also produce the same stable JSON
with a nonzero exit.

```bash
# Candidate checkout: exports the staged Git index (run `git add` first),
# links it under a unique temporary plugin id, and unlinks only that id.
./release-smoke candidate --plugin-root . --agent-cwd <isolated caller cwd>

# Installed plugin: verifies the exact version and never links or unlinks.
./release-smoke installed --plugin-id terry.herdr-group-chat \
  --expected-version 0.10.6 --agent-cwd <isolated caller cwd>
```

Each run starts a unique named Herdr session (its name is preflighted for
absence with a full-UUID suffix, and spawn and readiness are separate steps,
so a crashing or never-ready server is still cleaned up), the temporary
plugin id is preflighted for absence before linking, and the registration
verification checks that the entry carries exactly the requested plugin id
and version, with the full action contracts (id, command, and contexts for
all seven actions) and pane contracts (id, command, and placement for both
panes), and, for candidates, exactly the exported copy as its plugin root.
It runs the default `new` (sol/fable/grok) and
`new-classic` (pi/claude/codex/grok) actions with a synthetic `@all` round,
and validates that every participant replies with exactly `SMOKE-OK`. The
candidate export uses `git write-tree` plus `git archive` over the staged
index, extracted with traversal-safe filtering that rejects escaping or
absolute symlinks; unstaged worktree content is deliberately not exported,
so candidate files must be staged (`git add`) first. Because the export only
reads the Git index, a `TMPDIR` nested under the plugin root cannot recurse.

After each launch the harness polls only supported live Herdr surfaces —
`workspace list`, `agent list`, `pane list --workspace`, and
`pane read --source recent-unwrapped` — never launcher state files,
`plugin config-dir`, or the room relay executable directly. Readiness
requires the caller to remain the sole focused workspace, exactly one
`group-chat` and one `agents · group-chat` workspace to exist unfocused,
every expected peer to be live in the backstage workspace and settled
(`idle` or `done`; stale peers from the prior default room are allowed during
classic replacement), and exactly one current room pane whose text reports
the expected room handles ready, with a new pane and tab id for the
replacement room. The synthetic `@all` round runs through the actual room
pane with `pane send-text` and `pane send-keys enter`, then polls
`pane read` until, after this round's unique marker message, exactly one
complete post-marker message body per expected role appears, equal to
`SMOKE-OK`; prior chatter from the same roles is ignored, while duplicate
replies and continuation or explanatory text fail. A visible system
delivery error fails immediately, as do missing, extra, prefixed, or
suffixed replies. The caller's focus is verified inside every poll as well
as after every launch and round, without ever issuing a focus command. Cleanup unlinks the temporary
plugin id first, then stops the session, reaps the owned server, and deletes
only the named session the harness created (retrying the delete once after
the server is gone), all scoped to that session. Named sessions share the global plugin
registry, so candidate smoke must run under a temporary plugin id and never
touches the installed `terry.herdr-group-chat` registration. The harness
neither requires nor modifies `HERDR_ENV` and never uses the UI-focused
default session. Vivesca callers should create a
`deleo --create-session-temp-dir` root and set `TMPDIR` inside it before
running, so the internal candidate copy lands in the audited session temp
root. See [RELEASING.md](RELEASING.md) for the release gate.

The harness is built for offline deterministic assays: its core accepts
injectable runtime dependencies for command execution, process spawning,
monotonic time, and sleep, while production CLI runs always use the real
primitives as definition-time defaults, so an injected fake cannot leak into a
real smoke. The assays exercise the core in-process on a virtual clock with a
fake Herdr runner and reserve black-box subprocess checks for the executable
entrypoint, argparse/stable-JSON behavior, and the staged Git export
boundaries; Git itself always runs for real.

## Hotkey

The plugin actions appear in Herdr's action palette. To bind the default room
to a key, add a `plugin_action` keybinding to `~/.config/herdr/config.toml` and
run `herdr server reload-config`:

```toml
[[keys.command]]
key = "prefix+g"
type = "plugin_action"
command = "terry.herdr-group-chat.new"
description = "new group chat (Sol + Fable + Grok)"
```

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
/anneal @pi,@claude Harden this plan.
/consensus @claude,@codex Decide whether this is ready.
/cancel
/retry claude
/retry synthesis
```

The room stays responsive while ordinary sends and review rounds run. It shows
each participant as queued, working, blocked, done, failed, timed out,
cancelled, synthesizing, or skipped. During ordinary delivery, `/cancel`
requests local cancellation and stops collecting later replies without closing
or interrupting participant tabs, so a participant may continue working.
Ctrl-Q waits up to five seconds for local work to drain before the TUI exits.
A failed, blocked, timed-out, or cancelled first pass can be retried without
rerunning the other reviewers; a successful retry triggers a fresh synthesis.
Use Page Up and Page Down to scroll the active room, inbox, or lane presentation
while work continues.

Typing `@` at the start of the recipient token — at the beginning of the line
or right after `/review `, `/anneal `, `/consensus `, or a comma still inside that token —
opens a compact mention picker on the status row (`Mentions: [@sol] @fable`).
Typed characters filter it case-insensitively, Up/Down cycle the selection,
Tab completes the selected handle without adding a trailing space (a comma
continues the recipient list, a space begins the message), Esc closes it with
the text unchanged, and Enter still submits exactly what is shown. Already
selected handles are excluded from later suggestions; `@all` is offered for
plain messages, `/review`, and `/consensus` but never for `/anneal`.

Use `/anneal @author,@critic QUESTION` for a two-participant adversarial pass
over one question. Both answer blind and concurrently (a missing blind reply
stops the round before synthesis), the author drafts a provisional synthesis,
the critic challenges it once, and the author alone writes the `anneal_final`.
Anneal runs through the same single review controller as `/review`, so
`/cancel` stops local orchestration at every phase without interrupting the
participant tabs, ordinary messages wait, and `/retry` stays review-only until
a later ordinary review replaces the last round.

Use `/consensus [@reviewers] QUESTION` for a four-phase council. Reviewers first
answer blind and concurrently. Use this mode only when the question, payload,
and expected reply class are eligible on every selected reviewer route and the
configured synthesizer route. The relay cannot classify that eligibility. Each
blind prompt discloses verbatim redistribution and permits the exact
non-sensitive reply `CONSENSUS_SHARE_REFUSED`; when it is the exact first
non-empty line, the relay keeps only that sentinel and discards trailing text.
Refusal or an empty reply stops before provisional synthesis with a
non-unanimous status.

The configured synthesizer produces a provisional contention packet. Every
reviewer then ratifies the same byte-identical shared material with `VERDICT:
PASS` or `VERDICT: REVISE` on the first non-empty line. A token-only line is
preferred. A same-line explanation is accepted only after horizontal whitespace,
when it begins with alphanumeric prose, is not led by `or`, `and`, `versus`, or
`vs`, and contains no standalone `PASS` or `REVISE` token. Peer replies,
provisional text, and votes are serialized as untrusted quoted JSON with fresh
boundaries. They never become instructions. A deterministic ledger is unanimous
only when every vote is a valid `PASS`, and it remains authoritative regardless
of final model prose. The final synthesis stays advisory and always requires
human acceptance. A failed provisional stops before voting. Failed or invalid
votes produce a non-unanimous final.
`/cancel` records one terminal local outcome at every phase, and `/retry` is
unavailable after consensus.
Every consensus transcript item carries the ordered council scope. The human
room transcript remains complete, while later ordinary prompts omit those
items for agents outside the selected reviewers and synthesizer.

`/council status` derives a read-only durable council ledger from the
transcript without appending or dispatching anything. It shows the latest
round prefix, its phase (prepared, blind, provisional, voting, ratified, or
closed), the ordered reviewers with each vote verdict or `MISSING`, the
unanimity decision, any terminal outcome, and that human acceptance is
required. Contradictory records — a tampered objective, scope, shared-material
hash, or authoritative status — fail loudly instead of rendering a status.

`/council export PATH` writes that same canonical ledger as compact
sorted-key JSON plus a trailing newline to a new file only. It refuses to
overwrite anything (including symlinks), requires an existing directory
parent, creates the leaf exclusively with mode 0600, fsyncs the complete
write, and removes a partial leaf on any write error. With no council round
recorded, status prints a clear message while export fails.

### Resumable councils

Every schema-v2 consensus round (manifest `recovery_protocol:
checkpoint-replay-v1`) journals a strict, append-only, council-scoped
`council_attempt` record immediately before each model call: a unique attempt
id, the phase (`blind`, `provisional`, `vote`, or `final`), the exact agent,
and the prompt SHA-256. The resulting response, vote, provisional, final, or
phase-failure status settles that attempt by exact id and echoes the prompt
hash in the same atomic artifact append. The journal rejects duplicate
attempt ids, duplicate phase/agent attempts, foreign agents, invalid phases,
mismatched prompt hashes, settlements without attempts, duplicate
settlements, and artifacts whose phase or agent do not match.

Schema-v2 status and export report a `recovery_state`:

- `resumable` — the round is not terminal and every journaled attempt is
  settled with a usable artifact or there is no attempt at all.
- `unresolved` — an attempt started but never settled, or settled as a
  failure with no usable artifact. Because the call may have executed,
  such work fails closed: it is never redispatched, tombstoned, or rolled
  back, and a new council is required.
- `closed` — the round reached a terminal outcome (including `completed`);
  terminal rounds still report any unresolved attempts.

After a process restart, `/council resume` replays the latest durable round
as journal replay of completed leaf calls, not conversational continuation.
In the TUI it is asynchronous and is rejected while any review is active;
`/cancel` before recovery begins appends nothing, and after recovery starts
cancellation keeps the ordinary consensus semantics. The standalone CLI runs
the same command synchronously:

```bash
./herdr-group-chat --once '/council resume'
```

Resume takes the round id, objective, ordered reviewers, and synthesizer
only from the validated manifest and transcript — process defaults never
substitute — and requires every manifest participant name to be configured
and live. Under the room-wide nonblocking council execution lock it performs
the first authoritative read, derivation, eligibility, and liveness checks;
every refusal (legacy schema-v1 round, closed or unresolved round, unknown
or not-live participant, lock contention, or cancellation before recovery)
appends nothing. A resumable round is reconstructed exactly — completed
responses, provisional, and votes keep their byte-identical bodies — and
resume dispatches only `(phase, agent)` work with no prior attempt, at the
safe checkpoints: missing blind reviewers, the provisional, missing voters,
the deterministic verdict status once every vote is durable, then the final.
A completed call is never redispatched. In a profile room the current
non-secret route receipt is recorded (deduplicated) immediately before the
first new dispatch, never on a refusal path. Unexpected post-hydration
failures close the round truthfully with exactly one terminal status.

Recovery never grants authority: the resumed final synthesis stays advisory,
the deterministic verdict ledger stays authoritative, and human acceptance
remains required. Votes are never retried or superseded — an interrupted or
failed vote requires a new council, and schema-v1 legacy rounds cannot
resume.

The same surfaces work offline from the standalone CLI, before any agent or
profile setup:

```bash
./herdr-group-chat --council-status
./herdr-group-chat --council-export council-ledger.json
```

`/inbox` switches the transcript to a presentation-only inbox of final agent
replies, review syntheses, system notices, and review statuses that need
attention (non-unanimous, blocked, failed, timed out, refused, or cancelled);
clean unanimous consensus statuses stay in the room while `consensus_final`
remains in the inbox.

`/lanes` switches to one stable column per configured participant, in configured
roster order. Each lane shows human turns addressed to that participant or all,
that participant's own replies and review artifacts, and system or status items
directly scoped to that participant. Per-agent council statuses use their
structured agent scope. A leading configured legacy marker takes exact scope;
a leading unconfigured marker fails closed instead of becoming council-wide.
Lane-specific wrapping may compact labels but never clips body text. CJK,
combining marks, and emoji are wrapped, clipped, padded, and positioned by
terminal display cells without splitting grapheme clusters. Tabs, NUL, ESC,
and other terminal controls render as deterministic visible escapes while the
stored transcript remains unchanged. Page Up and Page
Down move every lane with one shared scroll offset. If the terminal cannot
keep every configured column readable, the room reports the actual terminal
width and asks you to widen it instead of dropping, stacking, or reordering
lanes. Tiny terminals reserve disjoint title, status, and input rows before
allocating any lane body rows.

`/room` returns to the full transcript. All three commands are pure view
switches: nothing is dispatched, recorded, or kept as unread state, and the
room remains the single live discussion and independent-review surface. The
input and shared status rows remain available in every view.

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
./herdr-group-chat --once "/anneal @pi,@claude Harden this plan."
./herdr-group-chat --once "/consensus @claude,@codex Decide whether this is ready."
# In the interactive TUI: /room, /inbox, and /lanes switch presentations.
./herdr-group-chat --council-status
./herdr-group-chat --council-export council-ledger.json
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

The manifest declares seven actions and two managed pane entrypoints: **New group
chat**, **New Sol + Fable chat**, **New Sol + Fable + Grok native chat**, **New
Sol + Fable + GLM chat**, **New classic four-agent chat**, **Open group chat**,
and **Adopt stale group-chat peers**, plus the visible setup pane and the room
pane. Herdr
supplies `HERDR_PLUGIN_STATE_DIR`, so transcripts and the plugin's exact
workspace and pane identifiers stay in a server-instance-scoped, locked state file. This
keeps multiple Herdr sessions separate and serializes overlapping launcher and
setup processes. Herdr 0.8.2+ pre-creates that directory under the process
umask; an empty pre-created directory is tightened to `0700` on first use,
while a non-empty or foreign-owned directory still fails closed. The launcher
never claims a workspace merely because its label
is `agents · group-chat` or `group-chat`. Standalone CLI use retains
`~/.local/state/herdr-group-chat/`; an explicit `--state-dir` always wins.

Invoking **New group chat** opens a visible setup tab for the verified atomic
Sol/Fable/Pi-xAI Grok default, reuses any live named peers, creates tabs for
missing participants, and then becomes a fresh `group-chat` room. **New classic
four-agent chat** separately opens the Pi/Claude/Codex/Grok composition. New
launches replace the previous plugin-owned room pane but retain that room's
transcript. Fresh launches never steal your focus, even when an earlier room
already exists: the replacement pane opens with `--no-focus`, and the
`group-chat` and `agents · group-chat` workspaces are created without focus,
so you stay where you were. **Open group chat** focuses the recorded plugin
pane or reopens the last room without starting models; because that action is
explicitly focus-oriented, recreating a missing room pane may take focus.
Reopen never depends on the old room pane surviving: a missing pane or a
workspace Herdr has since closed simply means a fresh room pane opens in the
recorded or a newly created chat workspace.
The transcript and input remain visible while you switch among participant
tabs. Failed cleanup remains recorded for a later retry instead of silently
losing ownership of the old pane. Participant setup failures are recorded as
system entries in the new room's transcript, so a missing peer is explained
rather than silently offline.

Compact mode keeps the room alone in the initiating workspace and places the
participant tabs in a secondary `agents · group-chat` workspace, with each
participant tab labeled `<role> · group-chat` (for example `pi · group-chat` or
`sol · group-chat`). A participant is owned when `agent get` reports it live
with the matching kind and the room's cwd, nothing else, so routing and reuse
key on the agent's name and the recorded pane, tab and workspace ids are
display caches that no decision branches on. Placement is one re-runnable
step: it reads only live state, moves each owned peer that is not already
where the layout puts it, records your workspace and tab before the first move
and restores them after the last, and never closes a workspace — Herdr closes
an emptied one itself. `./new-room --place compact` or `--place grid` re-runs
that step for the recorded room at any time and prints one JSON result line.
Enter `/agents` to reveal the backstage workspace or `/show pi`, `/show
claude`, `/show codex`, or `/show grok` to focus one native agent directly.
Use Herdr's workspace switcher or **Open group chat** to return.

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

## Sol + Fable profile

`New Sol + Fable chat` (or `./new-room --launch --profile sol-fable`) opens a
bounded two-role room: `@sol` runs Pi as `sol-peer` with the native arguments
`--provider openai-codex --model gpt-5.6-sol --thinking high`, and `@fable`
runs Claude Code as `fable-peer` with `--model fable --effort high` and no
fallback. Before either participant becomes routable, the launcher verifies
native host evidence: Sol requires an exact `openai-codex  gpt-5.6-sol` row
from `pi --list-models gpt-5.6-sol`, with no competing provider for that exact
model id; after start, each participant's live pane is proven through
`pane process-info`, whose foreground process must be exactly the
participant's executable and carry its start arguments as one contiguous argv
sequence. Reads retry briefly because startups are asynchronous, so a
lookalike model string, reordered arguments, or an MCP child process with the
right flags never verifies, and a model rename in a vendor UI can no longer
break a launch because screen text is never evidence. A `sol-fable` room is
atomic: if any required role fails, the launch
or reopen fails closed and no room opens. A mismatching existing session is
left open and excluded; a newly created tab that fails verification is closed
and never routed. The room records one non-secret verified system receipt per
profile in the transcript, generated only after complete verification and
deduplicated across reopens by its exact structured payload; this describes
what the launcher observed on the host, not a cryptographic or model-service
attestation. Sol synthesizes reviews by default, and the classic four-agent
composition is unchanged.

## Default Sol + Fable + Pi-xAI Grok profile

`New group chat` launches the bounded three-role `sol-fable-grok-pi` room via
`./new-room --launch --profile sol-fable-grok-pi`. It retains `@sol` and
`@fable` unchanged, then adds `@grok` as Pi `grok46pi-peer` with the exact
native arguments `--provider xai --model grok-4.6 --thinking high`. There is
no fallback provider, model, effort, or native Grok Build invocation.

Before `@grok` becomes routable, Pi must return the exact `xai  grok-4.6` row
from `pi --list-models grok-4.6`, with no competing provider for that exact
model id, and its live pane's foreground process must be `pi` carrying
`--provider xai --model grok-4.6 --thinking high` contiguously. The same
proof is required when reopening. Lookalikes, split model tokens, and wrong
effort fail closed.
The atomic receipt carries harness `pi`, provider `xai`, model `grok-4.6`, and
effort `high` only after every role verifies.

Pi-xAI is an external route. Payload eligibility remains task-specific, so the
room disclosure boundary applies to every message addressed to `@grok`.

## Settings: grid layout and Opus

Optional operator settings live in `settings.toml` inside the plugin config
directory printed by `herdr plugin config-dir terry.herdr-group-chat`. A missing
file means defaults; an invalid value or unknown key fails the launch closed.

```toml
layout = "grid"   # "compact" (default) keeps peers in the backstage workspace
opus = true       # default false; adds @opus to the default `new` action
```

With `layout = "grid"`, the default room becomes one tab: the room pane on the
left and each participant's native session stacked on the right in roster
order, so the group chat and every model's own dialogue are visible together.
Peers still start and verify in the backstage workspace exactly as in compact
layout; once every participant is owned, the placement step moves each peer
that is not already in the room tab into it — the room pane anchors the stack,
and the probed split ratios keep the right-hand column at equal heights —
then restores your workspace and tab. `/agents`
focuses the first peer pane when the room is laid out in grid and the backstage
workspace otherwise; `/show <role>` focuses that peer's pane. The room's
`/layout compact|grid` command re-runs the placement step at any time.

### Switching layout

`/layout grid` and `/layout compact` in the room re-run the launcher's
placement step for the recorded room: grid stacks every owned peer in the
room tab beside the room pane, compact moves each one back into an
`agents · group-chat` workspace as its own tab. The room command is refused
while a council round is running; it never moves a pane itself, records the
result as one system line in the transcript, and placement restores your
workspace and tab when it finishes. `./new-room --place <compact|grid>` does
the same from a terminal.

With `opus = true`, `New group chat` launches the `sol-fable-grok-opus-pi`
profile: `@sol`, `@fable` and `@grok` as before plus `@opus` running Claude
Code as `opus-peer` with `--model opus --effort high`, proven by the exact
foreground-process argv on its live pane. Sol synthesises reviews.

## Retained native Sol + Fable + Grok profile

`New Sol + Fable + Grok native chat` retains `sol-fable-grok` for stored rooms
and explicit new launches. Its `@grok` remains Grok Build `grok46-peer` with
the existing exact native arguments, proven by its foreground process.
Switching between native and Pi-xAI profiles replaces the single `@grok` role
and closes the prior profile-owned Grok tab under the existing replacement semantics.
Stored `sol-fable-grok`, `sol-fable`, and classic rooms keep their own profiles.
`New classic four-agent chat` and direct no-profile `./new-room --launch` are
unchanged.

## Sol + Fable + GLM profile

`New Sol + Fable + GLM chat` (or `./new-room --launch --profile sol-fable-glm`)
opens the bounded three-role `sol-fable-glm` room: the existing `@sol` and
`@fable` participants unchanged, plus `@glm`, Pi as `glm-peer` with the exact
native arguments `--provider bigmodel-coding --model glm-5.3 --thinking high`
and no fallback. Before `@glm` becomes routable, the launcher requires the
exact `bigmodel-coding  glm-5.3` catalog row from `pi --list-models glm-5.3`
with no competing provider for that exact model id, plus the foreground
process `pi` carrying `--provider bigmodel-coding --model glm-5.3 --thinking
high` contiguously on its live pane. Suffixed or split lookalikes such as
`glm-5.3.1` or `glm 5.3` never verify. The room is atomic:
if any of the three roles fails verification, the launch or reopen fails
closed, no room opens, and the non-secret `native-ui verified` receipt —
carrying harness `pi`, provider `bigmodel-coding`, model `glm-5.3`, effort
`high` — is emitted only after all three verify. Sol synthesizes reviews by
default. Every GLM turn is an external BigModel API call, so the room's
disclosure boundary applies to anything addressed to `@glm`.

## Limitations

- Ordinary group turns remain serial. `/review`, `/anneal`, and `/consensus`
  blind passes are parallel; consensus votes are also parallel.
- Every addressed agent must already be live in Herdr.
- New-room setup starts the four classic participants, the two bounded
  `sol-fable` participants, or one of the three-role `sol-fable-grok`,
  `sol-fable-grok-pi`, and `sol-fable-glm` profiles. Only these five fixed
  compositions are selectable, and arbitrary participant selection is not
  configurable.
- Retry state is kept in the running room process and does not survive a room
  restart. The transcript itself remains durable.
- Cancellation is local to the room orchestration. The relay never sends
  Ctrl-C or other terminal keys on `/cancel`, timeout, prompt failure, or status
  failure because Herdr has no atomic sequence-conditional interrupt. A
  participant may therefore continue working after the room reports local
  cancellation or timeout.
- Rooms are local to one Herdr installation; this is not a network chat server.
- An agent that finishes without emitting the `HGCHAT_REPLY_*` markers fails the
  turn fast with a clear error once its terminal output is observed stable,
  instead of stalling until the full agent timeout. Pi and Claude replies are
  read from their local session records first, so a TUI that reports idle
  before rendering the reply (Pi) or collapses prompt summaries into the
  capture (Claude) still resolves from the clean transcript. A generic
  unmarked-reply extractor is deliberately not attempted: terminal output has
  no reliable reply boundaries.
- Grok session recovery depends on the local `~/.grok/sessions` history layout.
- The preview release supports macOS and Linux.

## Development

Run the focused gate with:

```bash
uv run --with pytest pytest -q
uv run ruff check herdr-group-chat new-room orca-group-chat assays
uv run ruff format --check herdr-group-chat new-room orca-group-chat assays
```

See [SECURITY.md](SECURITY.md) for private vulnerability reporting and
[CHANGELOG.md](CHANGELOG.md) for release history.

### Troubleshooting

**After a Herdr restart.** A server restart gives the launcher a new
server-instance state key, so its fresh state has no participant records while
the previous launch's peers stay live. Ownership is by name, so peers whose
kind and cwd still match are reused directly by the next launch; only a peer
running under a different cwd needs re-recording. Run the **Adopt stale
group-chat peers** action (or `./new-room --adopt-peers`) to see the current
state of every configured role: it runs the owned check for each one against
the recorded room cwd (or the cwd you invoke it from) and reports which are
owned and why the rest are not, recording the owned roles' live panes and tabs
as display caches. It never closes, prompts, or moves a pane, and workspace
labels carry no weight in its verdict.

The setup pane and the room pane are short-lived processes, so a launcher
failure would otherwise vanish with the pane. Every failure — a known setup
`BootstrapError` or any unexpected exception — is appended as one JSON line to
`launcher-errors.jsonl` inside the plugin state directory
(`$HERDR_PLUGIN_STATE_DIR`, or `~/.local/state/herdr-group-chat` by default).
The file is created `0600` and capped at 200 records, dropping the oldest under
an exclusive lock on the errors file itself, so rotation never blocks on the
launcher state lock. Interactive panes also hold themselves open after an
error with `Press Enter to close (auto-closes in 60s)`; set
`HERDR_GROUP_CHAT_NO_HOLD=1` to skip that hold.

Inspect recorded failures read-only with:

```bash
./new-room --last-error   # the most recent failure, pretty-printed
./new-room --errors 20    # the last 20 failures, one JSON line each
```

## Experimental Orca adapter

`orca-group-chat` is an experimental adapter that reuses this project's core
(transcript, routing, review, synthesis, retry, cancellation, and curses TUI)
while driving **already-live Orca agent terminals by exact terminal handle**.
It is not a Herdr plugin and does not launch participants automatically — you
start the agent terminals in Orca yourself.

Discover exact terminal handles with:

```bash
orca terminal list --json
```

Then map one or more of `pi`, `claude`, `codex`, and `grok` explicitly;
mappings and handles must be unique and nothing is guessed by title:

```bash
orca-group-chat --room myroom \
  --agent pi=term_376a8453-88d7-4c3f-90b9-514d47cf87fb \
  --agent codex=term_0d91c2f4-6b1e-4a30-9f57-c2ad80b1e644
```

A turn sends the prompt with `orca terminal send`, waits for `tui-idle`, and
reads a token-bound reply from a private per-turn response file under
`~/.local/state/orca-group-chat` (override with `--state-dir` or
`ORCA_GROUP_CHAT_STATE_DIR`), falling back to a bounded
`orca terminal read --limit 1000`. Cancellation targets only the exact
terminal via `orca terminal send --interrupt`, and `focus`/`/show` uses
`orca terminal switch`. Only connected, writable, non-orphaned terminals are
considered live. `--once`, `--show`, and the interactive TUI work as in the
Herdr executable.

Accepted tradeoff: this first vertical slice uses Orca terminal control rather
than creating an orchestration Task/Dispatch per conversational turn. It
introduces no new message bus, MCP dependency, plugin framework, cross-host
support, or automatic terminal lifecycle ownership.

## Evaluated pi-tui prototype

A standalone pi-tui frontend for the relay lives in
[`prototypes/pi-tui`](prototypes/pi-tui). A live A/B assay confirmed its
peer-neutral boundary but retained curses for production because curses is
more compact, exposes participant and delivery status, and reports actionable
relay failures. The prototype is a frozen reference. It is not wired into the
plugin and has no standing Node CI commitment.
