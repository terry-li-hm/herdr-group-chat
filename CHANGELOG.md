# Changelog

All notable changes to this project are documented here.

## [Unreleased]

## [0.5.3] - 2026-08-24

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
