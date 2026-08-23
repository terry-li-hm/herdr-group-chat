# Changelog

All notable changes to this project are documented here.

## [Unreleased]

## [0.4.0] - 2026-08-23

- Add an experimental Orca adapter that reuses the transcript, routing, review,
  synthesis, retry, cancellation, and TUI core with exact terminal handles.
- Recover marker-bound Orca replies after stale waiters and require an
  authoritative terminal completion before accepting a turn.
- Add `/inbox` and `/room` view switches: a presentation-only inbox projection
  of final replies, syntheses, system notices, and attention-bearing review
  statuses over the unchanged full-room transcript.
- Recheck Codex after participant startup so a late-rendering hooks notice is
  dismissed without trusting unreviewed hooks.
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
