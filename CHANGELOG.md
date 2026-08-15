# Changelog

All notable changes to this project are documented here.

## [Unreleased]

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
