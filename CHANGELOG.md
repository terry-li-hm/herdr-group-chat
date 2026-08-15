# Changelog

All notable changes to this project are documented here.

## [Unreleased]

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
