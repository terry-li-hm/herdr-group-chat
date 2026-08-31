# Releasing

## Local release gate

1. Align the versions in `pyproject.toml` and `herdr-plugin.toml`.
2. Run `uv lock --check`, the test suite, Ruff over both executables and all
   assays, and Chaperone.
3. Export the tracked tree into a temporary directory — Vivesca callers should
   first create a session temp root with
   `deleo --create-session-temp-dir <exact OS-temp path>` and set `TMPDIR`
   inside it — stage the candidate (`git add`), then run the deterministic
   candidate smoke, which replaces the earlier manual candidate smoke setup:

   ```bash
   git add -- <exact release files>
   ./release-smoke candidate --plugin-root . --agent-cwd <isolated caller cwd>
   ```

   The harness exports the staged Git index (`git write-tree` plus
   `git archive`) into its own internal temporary directory with
   traversal-safe extraction that rejects escaping or absolute symlinks, so
   candidate files must be staged first and a `TMPDIR` nested under the
   checkout cannot recurse. It rewrites only the copied manifest's plugin id
   to a unique temporary id, starts a unique named Herdr session (the name is
   preflighted for absence with a full-UUID suffix, and server spawn and
   readiness are separate, so a crashing or never-ready server is still
   cleaned up), preflights that the temporary id is unregistered, links the
   temporary id scoped to that session, and
   verifies the full contracts: the exact seven actions (`new`,
   `new-sol-fable`, `new-sol-fable-grok-native`, `new-sol-fable-glm`,
   `new-classic`, `open`, and `adopt-peers`) with their exact commands and
   contexts, and both pane entrypoints (`new-room`, `room`) with their exact
   commands and placement. It then runs the default `new` (sol/fable/grok)
   and `new-classic` (pi/claude/codex/grok) actions. After each launch it
   polls only supported live Herdr surfaces — session-scoped `workspace
   list`, `agent list`, `pane list --workspace`, and `pane read --source
   recent-unwrapped` — never launcher state files, `plugin config-dir`, or
   the room relay executable directly. Readiness requires the caller to stay
   the sole focused workspace, exactly one `group-chat` and one
   `agents · group-chat` workspace to exist unfocused, every expected peer to
   be live in the backstage workspace and settled (`idle` or `done`, with
   stale peers from the prior default room allowed during classic
   replacement), and exactly one current room pane whose text reports the
   expected handles ready, with a new pane and tab id for the replacement
   room; all polling is wall-clock bounded, and the caller's focus is
   verified inside every poll. Each synthetic `@all` round is
   sent through the actual room pane with `pane send-text` and
   `pane send-keys enter` and requires exactly one complete post-marker
   message body per expected role, equal to `SMOKE-OK`: prior chatter from
   the same roles is ignored, while duplicates and continuation or
   explanatory text fail, as do missing, extra, prefixed, or suffixed
   replies and any visible system delivery error. The harness proves the
   caller workspace and tab remain focused, never focuses a
   group-chat or agents workspace, and never issues a focus command. Cleanup
   unlinks only the temporary plugin id first, then stops the session, reaps
   the owned server, and deletes only the named session it created (retrying
   the delete once after the server is gone), all scoped to that session and
   only after it proved the session name absent and spawned the server
   itself; any cleanup
   failure makes the run fail with the cleanup stage named, so success never
   leaves a surviving session or temp link behind. It never requires or
   modifies `HERDR_ENV`, never uses the default session, and never touches
   the installed `terry.herdr-group-chat` registration.
   A zero exit with `"ok": true` on stdout passes this step; any failure
   names the failed stage.

   Named sessions share the global plugin registry: a candidate linked under
   the real plugin id would shadow or collide with the installed
   registration, which is exactly why candidate smoke must use a temporary
   plugin id.
4. Commit the release candidate and create the annotated release tag.

## Standing publication authority

Terry granted standing release authority for the existing public personal
repository `terry-li-hm/herdr-group-chat` on 26 August 2026. After every local
release gate above passes, the cockpit may publish and install a non-major
SemVer release without seeking repeated per-release approval. This authority
covers only a clean fast-forward push of `main`, creation and push of a new
annotated version tag, creation of the matching GitHub release, and replacement
of the current local managed plugin with that exact tag.

The standing authority does not cover a major release, repository creation,
force push or any history rewrite, movement or replacement of an existing tag,
visibility or security-setting changes, credentials or secrets, workflow or
secret-permission escalation, a new registry or distribution lane, deletion,
cross-host installation, or any release with a failed or incomplete gate.
Workers and reviewers provide evidence but never inherit this authority.

For an eligible release:

1. Confirm the existing public repository has `main` as its default branch,
   the expected description and `herdr-plugin` topic, private vulnerability
   reporting enabled, no existing target tag or release, and a remote `main`
   that can fast-forward to the verified release commit.
2. Push `main`, create and push the new annotated release tag, then create the
   matching non-draft, non-prerelease GitHub release. Never replace or move an
   existing remote object.
3. Install that exact tag with
   `herdr plugin install terry-li-hm/herdr-group-chat --ref <tag> --yes`. Read
   back the requested ref and resolved commit, compare the managed checkout's
   tracked hashes with the tagged source, and verify all seven actions and both
   pane entrypoints.
4. Rerun the post-install smoke with the harness, which replaces the earlier
   manual default and classic smoke runs:

   ```bash
   ./release-smoke installed \
     --plugin-id terry.herdr-group-chat \
     --expected-version <released version> \
     --agent-cwd <isolated caller cwd>
   ```

   It verifies the exact installed version plus the exact action and pane
   contracts, runs the same isolated named-session default and classic smoke
   with exact `SMOKE-OK` reply validation, never links or unlinks (or touches
   the installed registration in any way), and cleans up only the named
   session it created, unlink first then session stop and delete. Read back
   the remote branch, annotated tag target, GitHub release, repository
   metadata, vulnerability-reporting state, and installed plugin before
   calling the release complete. If post-publication verification fails,
   preserve every remote object, restore the previously verified managed tag,
   record the failure, and stop.
