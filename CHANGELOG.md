# Changelog

All notable changes to this project are documented here.

## [Unreleased]

Phase A of the 0.12 redesign ([docs/0.12-redesign.md](docs/0.12-redesign.md)):
the launcher stops owning Herdr topology it cannot control.

- **Ownership by name.** A participant is owned exactly when `agent get`
  reports it live with the matching kind and the room's cwd. Fresh launch,
  reopen and `adopt-peers` all reduce to that single check; the recorded
  workspace, pane and tab identifiers lose all authority and become display
  caches, the workspace-label refusal and cross-state workspace claims are
  deleted, and `adopt-peers` now reports which roles are owned and why the
  rest are not.
- **Identity by process.** The only post-start native proof is
  `pane process-info`: the foreground process must be exactly the
  participant's executable and carry its start arguments as one contiguous
  argv. Every screen-token banner proof, the scrollback fallback and the
  reopen proof variants are deleted; the Pi catalog preflight is unchanged.
- **Placement as a re-runnable step.** `place(layout)` runs after ownership
  from live state alone: compact moves each owned peer that is not in an
  `agents · group-chat` workspace into one (created unfocused on demand),
  grid moves each peer that is not already in the room tab into the
  right-hand stack anchored on the room pane with the probed split ratios.
  It is idempotent, restores the caller's focus, never closes a workspace,
  and `./new-room --place <compact|grid>` re-runs it on demand.
- **Workspace lifecycle is Herdr's.** `close_empty_agents_workspace` is
  deleted; every list or focus on a possibly-vanished id treats
  `workspace_not_found` and `tab_not_found` as already gone.
- **Reopen never depends on a room pane existing.** A missing recorded room
  pane opens a new one in the recorded or a freshly created chat workspace,
  and the recorded-id revalidation that could wipe launcher state is gone.
- **Migration.** Older launcher state files are read for `last_room_id`,
  `selected_profile`, `layout` and the room ids only; unknown or stale id
  fields are ignored, never errors.

## [0.11.3] - 2026-09-03

- Relaunching a grid-layout room no longer aborts after the peers are
  arranged. Herdr auto-closes a workspace once its last pane leaves it, so
  after `arrange_grid` empties the backstage workspace the following
  `pane list --workspace <id>` failed with `workspace_not_found` and the
  launch died before the room pane ever opened. `close_empty_agents_workspace`
  now treats `workspace_not_found` from the pane listing as already closed:
  the recorded id is dropped and setup continues; any other error still
  raises, and a workspace that still holds panes is still never closed.
- Repeated `down` splits no longer halve the peer stack. Probed live on
  herdr 0.8.2 in an isolated session: `pane move <pane> --split down
  --target-pane <target> --ratio r` keeps fraction r of the split pane for
  the TARGET pane and lands the moved pane below it with the remaining 1-r
  (ratio 0.25 on a 39-row column left the target 10 rows on top and the moved
  pane 29 rows below; ratio 0.75 on those 29 rows left 22 above and 7 below).
  Since each peer k splits the pane of peer k-1 exactly once, the k-th of N
  peers now moves with `--ratio 1/(N-k+2)` — computed by
  `grid_peer_split_ratios` — so every peer finishes within one row of the
  others instead of the old 27/13/7/6 fixed-halving stack in a 53-row tab.
  The room keeps GRID_ROOM_RATIO on the first `right` split, which the same
  probe confirmed gives the target pane that fraction of the width.
- Reuse verification now survives a short pane. Relaunching over a grid room
  re-verifies each live peer with `reopen_pane_proof`, and a peer stacked a
  few rows tall can show only its footer, so its status tokens never appeared
  in the visible read and the launch failed. For reopen proofs only, after
  the visible reads fail, the token read is retried exactly once with
  `pane read <id> --source recent-unwrapped --lines 240` (scrollback) and
  accepted only if the tokens appear there; participants with
  `reopen_process_argv0` still must pass their foreground-process proof, and
  fresh-launch proofs stay visible-only.

## [0.11.2] - 2026-09-03

- Relaunching a grid-layout room now reuses its live peers. The previous
  arrangement had moved every peer into the room workspace, but ownership
  still accepted only the freshly created backstage workspace, so `New group
  chat` failed closed for every live peer with "different workspace". In grid
  layout a live participant is now owned in either the backstage workspace or
  the recorded participant workspace, with its cwd and recorded pane id still
  required to match; compact layout is unchanged, and the failure message
  names both accepted workspaces when they differ.
- `arrange_grid` reads each peer's live pane id through `agent get` immediately
  before moving it, because a relaunch replaces the previous room tab and the
  recorded pane ids can no longer be trusted to address the live panes. A peer
  already inside the target room tab is still moved into position, so the
  stack order is rebuilt idempotently; recorded ids are rewritten after each
  move as before.
- The emptied backstage workspace no longer accumulates. After a successful
  grid arrangement and its placeholder-tab cleanup, a backstage workspace
  that `pane list` reports as having no panes left is closed with
  `workspace close` and dropped from launcher state; a workspace that still
  holds any pane is never closed.

## [0.11.1] - 2026-09-03

- The room TUI now enables SGR mouse reporting on entry (`\x1b[?1000h` with
  `\x1b[?1006h`) and disables it on every exit path — normal exit, an
  exception propagating out of the event loop, and `--once`, which never
  enables it — so a crashed or closed room never leaves the operator's
  terminal with mouse reporting on. Wheel-up and wheel-down scroll the
  transcript by three lines in `/room`, `/inbox`, and `/lanes`; only wheel
  buttons 64 and 65 act, every other mouse report is ignored, and no mouse
  bytes ever reach the input buffer or any participant. PgUp/PgDn are
  unchanged, and Home/End jump to the oldest and newest lines.
- The room TUI now uses the terminal's default foreground and background
  everywhere and never emits a background, 256-colour, or truecolour
  sequence, under any theme including Herdr's Ochre light theme. Header,
  status row, participant labels, and system entries keep their distinctions
  through ANSI bold, dim, and reverse only, chosen so they read on both a
  light and a dark terminal.

## [0.11.0] - 2026-09-03

- Add operator settings in `settings.toml` under the plugin config directory
  (`herdr plugin config-dir terry.herdr-group-chat`), read by the launcher
  with a missing file meaning defaults and any invalid or unknown key failing
  the launch closed. Two keys: `layout = "compact" | "grid"` (default
  `compact`) and `opus = true | false` (default `false`). No action or pane
  entrypoint changed, so the seven-action smoke contract is unchanged.
- `layout = "grid"`: after every participant of a profile verifies in the
  backstage workspace, the setup pane moves each peer pane into the room tab
  as a right-hand stack beside the room pane (room at ratio 0.55, peers in
  roster order), reads each move back through `agent get`, rewrites the
  recorded pane and tab ids, records the participant workspace, and restores
  the caller's workspace and tab, which `pane move` otherwise takes. A peer
  landing outside the room tab fails the launch closed. Reopen re-verifies
  against the recorded participant workspace, `adopt-peers` accepts the room
  workspace in grid layout, and the relay receives `--agents-workspace` only
  while that workspace still exists.
- `opus = true`: the default `new` action launches `sol-fable-grok-opus-pi`,
  which adds `@opus` as Claude Code `opus-peer` with `--model opus --effort
  high`; its native pane must show the bounded `Opus 5` and `high effort`
  token sequences, matching Claude Code 2.1.258's `Opus 5 with high effort`.
  Sol still synthesises. Other profiles are unchanged.

## [0.10.7] - 2026-09-02

- Accept `Fable 5.1` as native-pane evidence for the `@fable` participant.
  Claude Code 2.1.258 renders the model line as `Fable 5.1 with high effort`,
  and the bounded proof required the exact token `5`, so every
  `sol-fable*` launch failed closed at `@fable` verification. The proof now
  accepts `5` or `5.1` as the version token; `5.10`, `5.1-deluxe` and split
  tokens still fail closed. The reopen proof carries the same alternatives.

## [0.10.6] - 2026-08-31

- Refactor the `release-smoke` harness around injectable runtime
  dependencies (command execution, process spawning, monotonic time, sleep)
  with the real primitives bound as production defaults, so injected fakes
  can never reach a CLI run. Rewrite the offline assays to exercise the core
  in-process on a virtual clock with a fake Herdr command runner, cutting
  the focused suite from roughly 164 seconds to under 3 seconds wall time
  while preserving all 45 behavioral contracts and adding regressions for
  the production-default and fake-leak guarantees (50 assays total). Only
  the executable entrypoint, argparse/stable-JSON handling, and the staged
  Git export boundaries remain black-box CLI checks. Add the harness
  development discipline to RELEASING.md: probe real Herdr response shapes
  before implementation, at most two model-led audit passes per patch,
  focused checks during iteration with the full suite at milestones, and a
  sub-30-second focused-suite budget.

## [0.10.5] - 2026-08-31

- Add the deterministic `release-smoke` harness. `release-smoke candidate
  --plugin-root PATH --agent-cwd PATH` exports the staged Git index of the
  candidate into an internal temporary directory, rewrites only the copied
  manifest's plugin id to a unique temporary id (preflighted for absence),
  links it, and drives an isolated named Herdr session through the default
  `new` and `new-classic` actions. `release-smoke installed --plugin-id ID
  --expected-version VERSION --agent-cwd PATH` runs the same smoke against
  the installed plugin without ever linking or unlinking it. Both modes
  verify the exact version, plugin identity and root, the full action and
  pane contracts (id, command, contexts/placement), and the exact
  participant replies (sol/fable/grok and pi/claude/codex/grok): after each
  launch they poll only supported live surfaces (workspace, agent, pane
  list/read, session-scoped, wall-clock budgeted, focus checked inside
  every poll), and each synthetic `@all` round runs through the actual room
  pane and requires exactly one complete post-marker message body per
  expected role equal to `SMOKE-OK` — prior chatter is ignored, duplicates
  and continuation or explanatory text fail. Cleanup unlinks only the
  temporary id, then stops, reaps, and deletes only the created named
  session; any cleanup failure fails the run. Because named sessions share
  the global plugin registry, candidate smoke must run under a temporary
  plugin id; the installed `terry.herdr-group-chat` registration is never
  touched.

## [0.10.4] - 2026-08-31

- Fresh **New group chat** launches no longer steal focus: the plugin pane
  `open` call uses `--no-focus`, and neither the `group-chat` nor the
  `agents · group-chat` workspace creation takes focus. The recorded room pane
  is revalidated through read-only tab listing instead of a focus call, so a
  second fresh launch also preserves the caller's focus. The explicit
  **Open group chat** action still focuses the recorded room pane, and a
  reopen that must recreate a missing room pane may use `--focus` because the
  action is explicitly focus-oriented.

## [0.10.3] - 2026-08-31

- Make `sol-fable-grok-pi` the default three-role room. It preserves `@sol`
  and `@fable`, and runs `@grok` as Pi `grok46pi-peer` with only
  `--provider xai --model grok-4.6 --thinking high`. The launcher fails closed
  unless `pi --list-models grok-4.6` contains the exact `xai  grok-4.6` row and
  no competing provider exposes that exact model id. Pi's pane proves the
  bounded `grok-4.6 • high` sequence on launch and reopen. The receipt carries
  harness `pi`, provider `xai`, model `grok-4.6`, and effort `high`. Pi-xAI is
  an external route, so payload eligibility remains task-specific.
- Retain the native Grok Build `sol-fable-grok` profile for stored rooms and
  add `new-sol-fable-grok-native` for explicit new native rooms. The `new`
  action now selects `sol-fable-grok-pi`; classic, Sol/Fable, GLM, open, and
  adoption actions remain available.

## [0.10.2] - 2026-08-30

- Preserve promotable trust-prompt tabs across launch and reopen, including
  stale blocked prompts, while reusing the foreground cwd only when its native
  evidence remains exact.
- Make human terminal output safe and readable with word-preferring,
  grapheme- and display-cell-aware wrapping, visible bidi-control escapes, and
  one flat output record per `--once` transcript item.
- Run ordinary TUI delivery asynchronously with per-recipient progress,
  overlap blocking, local `/cancel`, bounded Ctrl-Q draining, and late-reply
  dropping without closing participant tabs.
- Keep status handoffs current between ordinary delivery and reviews, and keep
  delivery controls responsive while final participant state is read.
- Fix fresh Codex sessions reporting done with a blank terminal capture: relay
  turns make one at-most-once `herdr agent prompt --wait` call, recover
  marker-bound output only from the exact Herdr-named current-user-owned Codex
  rollout, retain terminal fallback, and never replay the payload after a
  local timeout, cancellation, or identity failure.

## [0.10.1] - 2026-08-30

- Verify a reused live peer in the setup path with its reopen evidence rather
  than the fresh startup proof: a Fable or Grok peer that has already taken
  turns no longer fails `native-ui verification` and aborts an atomic launch.
- Fix native process proofs on the protocol-21 Herdr server, which reports the
  foreground executable as `name` instead of `argv0`: the proof now accepts
  `argv0`, then `name`, then `argv[0]`, still requiring the exact identity. On the
  updated host every Fable and Grok reopen and adoption had failed closed.
- Add the **Adopt stale group-chat peers** action (`./new-room --adopt-peers`)
  for the Herdr-server-restart case: a restart gives the launcher a new
  server-instance state key, so its fresh state has no participant records
  while the previous launch's peers stay live. The action is fail-closed: every
  live configured peer (classic plus every profile) must match its exact kind
  and native evidence, all candidates must share exactly one
  `agents · group-chat` workspace that no other `launcher-state-*.json` still
  claims, and they must share one cwd. On success it records the agents
  workspace, the common cwd, and each adopted role's pane and tab ids, prints
  one line per role plus a summary, and never closes, prompts, or moves a pane;
  a previous empty placeholder workspace is left untouched and mentioned. Any
  mismatch adopts nothing and refuses with one listed reason per failure.
- Make `profile_incomplete` actionable: the BootstrapError message now lists
  each failed role with its exact reason, the per-role reasons travel as a
  `failures` list on the error and in the durable `launcher-errors.jsonl`
  record, and the ownership failure distinguishes which check failed
  (different workspace, different cwd, or pane not recorded) while naming the
  live workspace, the launcher's workspace, and the adopt-or-close recovery.
## [0.10.0] - 2026-08-30

- Record launcher failures durably: the setup pane, `--room-entrypoint`, and
  the `--launch`/`--open` action now append every `BootstrapError` and every
  unexpected exception as one JSON line to `launcher-errors.jsonl` in the
  plugin state directory (created `0600`, capped at 200 records, never raised
  from the logger). Bootstrap failures keep their stderr line and exit code 2;
  unexpected exceptions print their traceback and exit 1. Interactive setup or
  room panes hold themselves open after an error with a 60-second Enter prompt,
  skipped when not on a TTY or when `HERDR_GROUP_CHAT_NO_HOLD=1`. Read-only
  inspection: `new-room --last-error` and `new-room --errors N`.
- Add the exact presentation-only `/lanes` command beside `/room` and `/inbox`.
  It keeps one stable column per configured participant and projects only human
  turns addressed to that participant or all, the participant's own replies
  and review artifacts, and directly scoped system or status items. All columns
  use one shared Page Up/Page Down offset. Narrow terminals show an explicit
  widen-terminal message rather than dropping or rearranging lanes. Switching
  views does not append to the transcript or dispatch agent work, and the
  shared status and input rows remain usable.
- Harden lane scope and rendering: per-agent review statuses now carry an exact
  structured agent alongside council scope, malformed scope fails closed, and
  legacy fallback recognizes only a leading participant marker. A compact
  lane renderer preserves complete bodies at minimum width. Shared and inbox
  rendering tolerate missing optional legacy fields, while tiny terminals use
  disjoint regions and width guidance reports the actual terminal width.
- Make leading legacy participant markers override council-wide scope when no
  structured agent is available. Add `regex` and `wcwidth` so lane wrapping,
  clipping, padding, separators, and input cursor placement use terminal cells
  without splitting CJK, combining-mark, or emoji grapheme clusters.
- Distinguish absent legacy markers from leading unconfigured markers, which
  now fail closed instead of inheriting council-wide scope. Escape tabs, NUL,
  ESC, and other terminal controls visibly before all terminal-cell operations
  and curses writes without changing stored transcript text.

## [0.9.1] - 2026-08-29

- Fix a Pi participant's turn failing with `reply markers HGCHAT_REPLY_* not
  found` even though Pi produced the marker-wrapped reply: Pi 0.84.4 reports
  idle/done before its TUI renders the assistant text, so the quiet terminal
  tripped the unmarked-stability rule first. Mirroring the Claude design, a
  verified Pi target (`agent` `pi`, session source `herdr:pi`, session `kind`
  `path`) now gets its reply from the exact session JSONL named by Herdr's
  agent metadata, before any terminal capture: the last assistant message's
  text parts are joined and scanned for the turn's markers, so a second turn
  reads its own new reply and never replays the previous turn's. A record that
  is missing, unreadable, not a regular file owned by the current user, or
  markerless returns nothing and the terminal path (including the
  unmarked-stability rule) stays the fallback.

## [0.9.0] - 2026-08-29

- Add the bounded `sol-fable-glm` three-role profile and the `new-sol-fable-glm`
  action (`New Sol + Fable + GLM chat`, or `./new-room --launch --profile
  sol-fable-glm`). The room composes the existing `@sol`/`@fable` participants
  unchanged plus `@glm` (Pi as `glm-peer` with the exact native arguments
  `--provider bigmodel-coding --model glm-5.3 --thinking high`, no fallback).
  Before `@glm` becomes routable the launcher requires the exact
  `bigmodel-coding  glm-5.3` catalog row from `pi --list-models glm-5.3` plus
  the bounded `glm-5.3` bullet `high` pane token sequence; a live canary
  confirmed Pi keeps `glm-5.3 • high` on its status and footer lines after the
  first turn, so the same pane proof applies on reopen and, like Sol, no
  foreground-process argv evidence is required. Lookalikes such as `glm-5.3.1`
  or split `glm 5.3` tokens never verify. The room stays atomic — any role
  failure aborts the launch or reopen with no room and no receipt — and the
  non-secret receipt carries harness `pi`, provider `bigmodel-coding`, model
  `glm-5.3`, effort `high`. Sol synthesizes, `@glm` works in mentions, the
  mention picker, `/review`, `/anneal`, and `/consensus` selection, and the
  stored `sol-fable`, `sol-fable-grok`, and classic rooms keep their own
  profiles.
- Fix the launcher rejecting a pre-created state directory: Herdr 0.8.2
  pre-creates the directory it exports as `HERDR_PLUGIN_STATE_DIR` under the
  process umask, so on Linux it arrives as `0775` and every launch action
  failed with `invalid launcher state directory authority` before doing
  anything. A pre-existing directory that is a real directory, owned by the
  current user, and empty is now tightened to `0700` with `fchmod` on the
  already-open descriptor and the launch continues; a non-empty directory with
  looser permissions, one not owned by the user, or a symlink still fails
  closed, and no other authority check is weakened.

## [0.8.0] - 2026-08-26

- Add the schema-v2 council attempt journal, recovery ledger, and execution
  lock: every consensus model call is preceded by a scoped append-only
  `council_attempt` (unique id, phase, exact agent, prompt SHA-256) and settled
  atomically with its artifact or failure status by exact id and echoed prompt
  hash. Strict validation rejects duplicate ids and phase/agent attempts,
  foreign agents, invalid phases, mismatched hashes, orphan and duplicate
  settlements, and mismatched artifacts. A room-wide nonblocking council
  execution flock serializes every schema-v2 run; contention fails without
  appending. Schema-v2 status/export report `recovery_state` as `resumable`,
  `unresolved`, or `closed` plus ordered unresolved attempts; a started but
  unsettled attempt, or a settled failure with no usable artifact, fails closed
  as unresolved — it is never redispatched and a new council is required.
  Schema-v1 export stays byte-identical.
- Add transcript reconstruction and `/council resume`: journal replay of only
  `(phase, agent)` work with no prior attempt, in order — missing blind
  reviewers, provisional, missing voters, the deterministic verdict status
  after all votes are durable, then the final. Completed artifacts are
  reconstructed byte-identically and never redispatched. The first authoritative
  read, derivation, roster, and liveness checks run under the execution lock,
  so every refusal (legacy schema-v1, closed, unresolved, unconfigured or
  not-live participant, lock contention, cancellation before recovery) appends
  nothing. The manifest roster and synthesizer override process defaults.
  `/council resume` is asynchronous in the TUI (rejected while any review is
  active) and synchronous through `--once '/council resume'`. Recovery stays
  advisory: the human acceptance gate survives, votes are never retried or
  superseded, and unexpected post-hydration failures close truthfully with one
  terminal.
- Keep the exact `--once '/council resume'` path append-free on every refusal:
  setup-failure and startup profile-receipt writes are skipped for that path,
  and the validated profile receipt is recorded (deduplicated) under the
  council lock only after all checks pass and recovery hydrates, immediately
  before the first new dispatch. Non-resume once paths and TUI startup behave
  unchanged.

## [0.7.0] - 2026-08-26

- Persist a durable council manifest and shared-material hash: every consensus
  round records its schema-1 manifest on the review question and stamps one
  authoritative `shared_material_sha256` over the exact question, ordered blind
  responses, and provisional synthesis on every vote, authoritative status, and
  post-provisional terminal status. A vote cannot be appended without its hash.
- Add `/council status` and `/council export PATH` plus `--council-status` and
  `--council-export PATH`: a read-only derived durable council ledger validating
  manifest shape, objective hash, council scope, per-reviewer response and vote
  uniqueness, verdict validity, one consistent shared-material hash, and
  authoritative status verdicts and unanimity against the vote artifacts.
  Contradictions raise a relay error; unrelated records are ignored. Export
  writes canonical compact sorted-key JSON plus a newline to a new 0600 leaf
  only, refusing overwrites and symlinks, fsyncing the complete write, and
  removing a partial leaf on write failure.

## [0.6.0] - 2026-08-26

- Add `/consensus [@reviewers] QUESTION`: concurrent blind reviews, one
  provisional contention synthesis, concurrent strict PASS/REVISE ratification,
  a deterministic unanimous anchored PASS/REVISE ledger, and one advisory final
  synthesis. The mode reuses the review controller and append-only transcript.
- Make consensus sharing fail closed: blind prompts disclose verbatim
  redistribution and permit refusal, invalid or empty replies stop the round,
  and cancellation or phase failure records one terminal local outcome.
- Scope every consensus transcript item to its ordered council for later agent
  delivery, isolate shared model text as token-bound JSON data, and keep direct
  retries unavailable after consensus. Final output remains advisory and requires
  human acceptance; the relay does not provide native Herdr group chat,
  automated judgment, or release authority.
- Accept unambiguous same-line prose after an anchored consensus verdict token
  while rejecting token lookalikes, punctuation-led suffixes, connector-led
  alternatives, and embedded verdict tokens.
- Bind transcript and launcher transactions to validated directory descriptors,
  deduplicate profile receipts atomically, and discard late review replies after
  local cancellation while committing one completion or cancellation outcome.
- Make the one-recorded-participant-per-role contract explicit: the same named
  participant is reused across profiles, a different requested name for a recorded
  role intentionally closes the plugin-recorded tab before starting its replacement
  (visible as `replace @role` in setup output), with symmetric regression coverage
  for `sol-fable-grok` → classic and classic → `sol-fable-grok` switches.
- Evaluate and retain the standalone, peer-neutral `prototypes/pi-tui` frontend
  as a frozen reference. It uses `@earendil-works/pi-tui` directly, keeps the
  Python relay as sole transcript writer, preserves exact argv dispatch and local
  room/inbox views, and has fixtures and tests. The live A/B retains curses
  because it is more compact and exposes participant, delivery, and actionable
  failure state. The prototype remains unwired from the plugin, has no standing
  Node CI commitment, and is not an active experimental adoption.

## [0.5.2] - 2026-08-24

- Stop local review/anneal orchestration without sending terminal keys; retain
  interleaved transcript messages for later delivery, and reject unsafe or
  non-private transcript, cursor, and lock authorities without blocking.

## [0.5.1] - 2026-08-24

- Make the `sol-fable-grok` three-role room the default composition: action
  id `new` (`New group chat`) now runs `./new-room --launch --profile
  sol-fable-grok` instead of the classic four-agent launch, while the direct
  no-profile `./new-room --launch` command and the new `new-classic` action
  (`New classic four-agent chat`) preserve classic behavior. The profile
  composes the existing `sol-fable` participants unchanged — `@sol`
  (`sol-peer`, Pi with `--provider openai-codex --model gpt-5.6-sol --thinking
  high`) and `@fable` (`fable-peer`, Claude with `--model fable --effort
  high`) — plus `@grok` (`grok46-peer`, Grok with the exact native arguments
  `--model grok-4.6 --reasoning-effort high --no-memory --disable-web-search
  --no-subagents --permission-mode bypassPermissions`, no fallback). Only the
  Grok participant tab is created with a prepended `~/.grok/bin` PATH through
  the Herdr `tab create` environment; the launcher's own environment and
  global config stay untouched. A fresh launch requires the bounded `Grok
  4.6` and `high` pane token sequences — a live canary confirmed the native
  UI keeps `Grok 4.6 (high)` after a turn — and a reopen additionally
  requires the exact native foreground process argv0 `grok` with the
  contiguous start-argument sequence; lookalikes and reordered or missing
  argv never verify. The room stays atomic: any role failure aborts the
  launch or reopen with no room opened and no receipt; a mismatching
  existing Grok session is left open and blocks the launch, while
  already-verified peers remain reusable on retry and an unverified new Grok
  tab is closed through the existing pending-tab cleanup. The receipt records
  harness `grok`, model `grok-4.6`, effort `high`, `native-ui verified`.
  Stored rooms keep their own profile, so existing `sol-fable` and classic
  rooms reopen unchanged.

## [0.5.0] - 2026-08-24

- Keep room-pane registration two-phase so the outer launcher records the
  returned pane without clearing the profile operation before the inner setup
  process claims it; an unclaimed launch remains recoverable through the
  existing pending-operation grace path.
- Reverify post-turn Fable sessions with bounded `Fable 5` pane evidence and
  exact native `claude --model fable --effort high` process arguments, while
  fresh launches retain the stronger startup-banner check.
- Add a presentation-only mention picker in the TUI. Typing `@` at the start
  of the routing token (line start, right after exact `/review `/`/anneal `,
  or right after a comma still inside that token) shows the live handles on
  the status row; prose, emails, and bare commas never open it. Typing filters
  case-insensitively, Up/Down cycle, Tab replaces only the active `@query`
  fragment with no trailing space, Esc closes byte-identical, and Enter keeps
  normal submission. Already selected handles are excluded from subsequent
  suggestions, `@all` is offered for ordinary messages and `/review` but hidden
  for `/anneal`, and candidate rosters derive from `tuple(chat.agents)` so
  profile rooms show exactly `@sol`/`@fable`. `parse_route`, `parse_anneal`,
  routing maps, and profile verification are unchanged.

- Add the bounded `sol-fable` model profile, selected by `new-room --launch
  --profile sol-fable` or the `New Sol + Fable chat` action. The room is
  atomic: it opens only when both exact ordered participants verify — `@sol`
  (`sol-peer`, Pi with `--provider openai-codex --model gpt-5.6-sol --thinking
  high`) and `@fable` (`fable-peer`, Claude with `--model fable --effort high`,
  no fallback) — on every fresh launch and every room-process reopen. Sol
  requires a structurally exact `pi --list-models` catalog row (first token the
  provider, second the model) checked before its tab is created or started,
  plus the bounded `gpt-5.6-sol • high` pane token sequence; Fable requires
  bounded `Fable 5` and `high effort` sequences; prefixed or suffixed
  lookalikes never verify. A mismatching existing session is left open and
  excluded, a newly created unverified tab is closed and never routed, and any
  role failure aborts the profile launch or reopen closed. The room process
  requires exactly the two explicit `--agent` role mappings plus the
  launcher's deterministic receipt payload — generated only after complete
  verification and carrying the canonical matched evidence — and records it
  once per transcript, deduplicated by exact structured metadata across
  reopens. Reopens re-verify recorded workspace, pane, tab, cwd, kind/name, and
  native evidence before exec, bind the stored profile to the exact room id,
  and reject stale cross-room profile state. The default Pi/Claude/Codex/Grok
  room is unchanged.

## [0.4.1] - 2026-08-24

- Add `/anneal @author,@critic QUESTION`: a thin two-participant composition over
  the existing review round. Both participants answer blind and concurrently, the
  author synthesizes provisionally, the critic appends one `anneal_challenge`,
  and the author alone appends `anneal_final`. Anneal reuses the single review
  controller (`/cancel` at any phase, ordinary-message exclusion) and a missing
  blind reply stops the round without synthesis; `/retry` stays review-only
  until a later ordinary review replaces the last round. The inbox keeps
  `anneal_final` and attention-bearing failures.
- Read Codex hook screens through Herdr's raw pane-output contract so startup
  actually leaves unreviewed hooks inactive before the room opens, with one
  five-second deadline across reads, sleeps, and key sends while the launcher
  lock is held.
- Bound relay process reaping and isolate an unexpected reviewer exception so
  one failed participant cannot strand the remaining review round.
- Harden experimental Orca turns against unsafe reply-file types, incomplete
  interruption, unbounded process-stop waits, and CI coverage drift.

## [0.4.0] - 2026-08-24

- Add an experimental Orca adapter that reuses the transcript, routing, review,
  synthesis, retry, cancellation, and TUI core with exact terminal handles.
- Recover marker-bound Orca replies after stale waiters and require an
  authoritative terminal completion before accepting a turn.
- Add `/inbox` and `/room` view switches: a presentation-only inbox projection
  of final replies, syntheses, system notices, and attention-bearing review
  statuses over the unchanged full-room transcript.
- Keep Cumora outside the local relay boundary, with measured-gap reuse gates
  and a single-owner pivot rule for any future networked mode.

## [0.3.0] - 2026-08-16

- Add a two-phase `/review` flow: independent reviewers run concurrently, then
  a configurable participant synthesizes the available responses.
- Keep the room responsive with per-participant review states, targeted
  cancellation, retry for a reviewer or synthesis, and per-agent timeouts.
- Label review and synthesis entries while preserving the append-only ordered
  transcript, targeted mentions, and ordinary serial chat behavior.
- Add Page Up and Page Down transcript scrolling.
- Poll Herdr at half-second intervals, defer the first terminal read until the agent has
  been seen working, and treat a refused or slow terminal read as not-ready instead of
  failing the turn and interrupting the agent. Ctrl-Q now waits for an active review's
  cancellation to reach the agents.
- Detect Codex's unreviewed-hooks dialog with clipping-safe needles, retry briefly
  while it renders, and answer the menu variant with "Continue without trusting";
  hooks are never trusted. The room also recognizes the clipped dialog as a
  blocked turn.
- Fail a turn fast with a clear error when an agent completes without emitting the
  `HGCHAT_REPLY_*` markers and its terminal output stays stable, instead of
  polling until the full agent timeout.
- Label participant tabs `<kind> · group-chat` and the secondary workspace
  `agents · group-chat`; routing and reuse still key on exact recorded workspace
  and pane identifiers.
- Only send cancellation Ctrl-C to agents observed working; an idle Codex
  treats Ctrl-C as quit, which previously killed the participant on a timeout
  whose prompt never landed.
- Record participant setup failures as system entries in the new room's
  transcript instead of losing them when the room redraws the setup pane.
- Retry a participant's `agent start` a few times when Herdr transiently
  refuses it with `agent_pane_busy` after a fresh tab's shell comes up.

## [0.2.0] - 2026-08-15

- Add a visible New group chat setup flow that reuses live peers, starts missing
  Pi, Claude, Codex, and Grok tabs, and opens a fresh room.
- Add compact mode: keep native agent tabs in an `agents` workspace and expose
  `/agents` and `/show <agent>` navigation from the room.
- Record exact plugin-owned workspace and pane identifiers so generic user
  workspace labels are never claimed or focused.
- Reopen the last room after its pane closes and replace the previous
  plugin-owned pane when starting a fresh room.
- Align installation, security, development, and release guidance with Herdr's
  0.8 plugin contract.
- Keep pane entrypoints rooted in the installed plugin so actions work from any
  project directory while agent tabs still inherit the invoking project.
- Partition launcher identity by Herdr server instance, serialize competing setup
  processes, and retain partial-failure cleanup state for safe retries.

## [0.1.0] - 2026-08-15

- Open the room as a named, persistent Herdr tab instead of an easy-to-lose overlay.
- Package the relay as a native Herdr plugin with one action and one managed
  room pane.
- Add Codex alongside Pi, Claude Code, and Grok Build.
- Store plugin rooms under Herdr's plugin-owned state directory.
- Reject malformed mentions instead of accidentally broadcasting them.
- Preserve undelivered context when an agent turn fails.
- Preserve each agent's disclosure boundary and request a route receipt for bounded
  disclosures.
- Allow time for native approval prompts and recover token-bound Grok replies that
  its alternate-screen viewport hides.
- Add the local serial group-chat relay, append-only transcripts, bounded
  turns, agent mentions, and terminal reply-marker extraction.
