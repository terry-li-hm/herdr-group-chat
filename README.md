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
grapheme-safe terminal-cell layout. The `sol-fable-glm` profile additionally
needs Pi configured and authenticated for its `bigmodel-coding`
provider (`glm-5.3`).

Install the pinned release from GitHub:

```bash
herdr plugin install terry-li-hm/herdr-group-chat --ref v0.9.1
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
/anneal @pi,@claude Harden this plan.
/consensus @claude,@codex Decide whether this is ready.
/cancel
/retry claude
/retry synthesis
```

The room stays responsive while a review runs and shows each participant as
queued, working, blocked, done, failed, timed out, cancelled, synthesizing, or
skipped. `/cancel` stops only the room's local orchestration; it never sends
Ctrl-C or other keys, so a participant may continue working and its later reply
is not collected by the cancelled round. A failed, blocked, timed-out, or
cancelled first pass can be retried without rerunning the other reviewers; a
successful retry triggers a fresh synthesis. Use Page Up and Page
Down to scroll the active room, inbox, or lane presentation while work continues.

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

The manifest declares five actions and two managed pane entrypoints: **New group
chat**, **New Sol + Fable chat**, **New Sol + Fable + GLM chat**, **New classic
four-agent chat**, and **Open group chat**, plus the visible setup pane and the
room pane. Herdr
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
Sol/Fable/Grok default, reuses any live named peers, creates tabs for missing
participants, and then becomes a fresh `group-chat` room. **New classic
four-agent chat** separately opens the Pi/Claude/Codex/Grok composition. New
launches replace the previous plugin-owned room pane but retain that room's
transcript. **Open group chat** focuses the exact recorded plugin pane or
reopens the last room without starting models.
The transcript and input remain visible while you switch among participant
tabs. Failed cleanup remains recorded for a later retry instead of silently
losing ownership of the old pane. Participant setup failures are recorded as
system entries in the new room's transcript, so a missing peer is explained
rather than silently offline.

Compact mode keeps the room alone in the initiating workspace and places the
participant tabs in a secondary `agents · group-chat` workspace, with each
participant tab labeled `<role> · group-chat` (for example `pi · group-chat` or
`sol · group-chat`). These labels are display-only; routing and reuse still key
on the exact recorded workspace and pane identifiers. Enter `/agents` to
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

## Sol + Fable profile

`New Sol + Fable chat` (or `./new-room --launch --profile sol-fable`) opens a
bounded two-role room: `@sol` runs Pi as `sol-peer` with the native arguments
`--provider openai-codex --model gpt-5.6-sol --thinking high`, and `@fable`
runs Claude Code as `fable-peer` with `--model fable --effort high` and no
fallback. Before either participant becomes routable, the launcher verifies
native host evidence: Sol requires an exact `openai-codex  gpt-5.6-sol` row
from `pi --list-models gpt-5.6-sol` plus the model and high-thinking tokens in
the native pane; Fable requires `Fable 5` and `high effort` in its native
pane. Reads retry briefly because startup UIs render asynchronously, and both the
catalog row and the pane evidence are matched as exact tokens and bounded
sequences, so lookalikes such as `gpt-5.6-sol-01` or `Fable 5-deluxe` never
verify. A `sol-fable` room is atomic: if any required role fails, the launch
or reopen fails closed and no room opens. A mismatching existing session is
left open and excluded; a newly created tab that fails verification is closed
and never routed. The room records one non-secret `native-ui verified` system
receipt per profile in the transcript, generated only after complete
verification and deduplicated across reopens by its exact structured payload;
this describes what the launcher saw in the native UIs, not a cryptographic or
model-service attestation. Sol synthesizes reviews by default, and the classic
four-agent composition is unchanged.

## Default Sol + Fable + Grok profile

`New group chat` is now the opinionated default: action id `new` changes from
the classic four-agent room to the bounded three-role `sol-fable-grok` room,
launched via `./new-room --launch --profile sol-fable-grok`. The direct
no-profile `./new-room --launch` command and the new `new-classic` action
(`New classic four-agent chat`) preserve classic behavior. The room composes the existing `sol-fable` participants
unchanged — `@sol` (Pi as `sol-peer`, `--provider openai-codex --model
gpt-5.6-sol --thinking high`) and `@fable` (Claude Code as `fable-peer`,
`--model fable --effort high`) — plus `@grok`, Grok as `grok46-peer` with
the exact native arguments `--model grok-4.6 --reasoning-effort high
--no-memory --disable-web-search --no-subagents --permission-mode
bypassPermissions` and no fallback. Only the Grok participant tab's PATH is
prepended with `~/.grok/bin` through the Herdr `tab create` environment, so
the launcher's own environment and global config are never touched.

Before any participant becomes routable, `@grok` must show the bounded
`Grok 4.6` and `high` token sequences in its native pane (a live canary
confirmed the UI keeps `Grok 4.6 (high)` after a turn); on reopen the proof
additionally requires the exact native foreground process argv0 `grok` with
the contiguous start-argument sequence. Suffixed or prefixed lookalikes such
as `Grok 4.6.1` or `groklette 4.6` never verify. The room is atomic: if any
of the three roles fails verification, the launch or reopen fails closed, no
room opens, and the non-secret `native-ui verified` receipt is emitted only
after all three verify. A mismatching existing Grok session is left open,
blocks the launch, and already-verified peers stay available for a retry.
The stored room keeps its own profile: previously opened `sol-fable` or
classic rooms reopen unchanged, Sol synthesizes, and `New Sol + Fable chat`
remains available. `New classic four-agent chat` (or a direct
`./new-room --launch` without a profile) still opens the unchanged classic
Pi/Claude/Codex/Grok four-agent room.

## Sol + Fable + GLM profile

`New Sol + Fable + GLM chat` (or `./new-room --launch --profile sol-fable-glm`)
opens the bounded three-role `sol-fable-glm` room: the existing `@sol` and
`@fable` participants unchanged, plus `@glm`, Pi as `glm-peer` with the exact
native arguments `--provider bigmodel-coding --model glm-5.3 --thinking high`
and no fallback. Before `@glm` becomes routable, the launcher requires the
exact `bigmodel-coding  glm-5.3` catalog row from `pi --list-models glm-5.3`
plus the bounded `glm-5.3` bullet `high` token sequence in its native pane; a
live canary confirmed Pi keeps `glm-5.3 • high` on its status and footer
lines after the first turn, so the same pane proof applies on reopen and, like
Sol, no foreground-process argv evidence is required. Suffixed or split
lookalikes such as `glm-5.3.1` or `glm 5.3` never verify. The room is atomic:
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
  `sol-fable` profile participants, the three `sol-fable-grok` profile
  participants, or the three `sol-fable-glm` profile participants; only these
  four fixed compositions are selectable, and arbitrary participant selection
  is not configurable.
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
