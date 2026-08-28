# Releasing

## Local release gate

1. Align the versions in `pyproject.toml` and `herdr-plugin.toml`.
2. Run `uv lock --check`, the test suite, Ruff over both executables and all
   assays, and Chaperone.
3. Export the tracked tree into a temporary directory and link that copy as
   a Herdr plugin. Verify the manifest has no warnings. Confirm all five
   actions are present: `new`, `new-sol-fable`, `new-sol-fable-glm`,
   `new-classic`, and `open`.
   Check both pane entrypoints, exact workspace ownership, and reopen
   behavior. Also check named-session isolation and partial-failure retry
   behavior. Run a verified three-agent default (sol-fable-grok) room round
   and a classic four-agent room round.
   Verify the five actions map to their exact commands: `new` runs
   `./new-room --launch --profile sol-fable-grok`; `new-sol-fable` runs
   `./new-room --launch --profile sol-fable`; `new-sol-fable-glm` runs
   `./new-room --launch --profile sol-fable-glm`; `new-classic` runs
   `./new-room --launch`; `open` runs `./new-room --open`.
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
   tracked hashes with the tagged source, and verify all five actions and both
   pane entrypoints.
4. Rerun the verified default three-agent (sol-fable-grok) smoke test and the
   classic four-agent smoke test from the managed checkout. Read back the
   remote branch, annotated tag target, GitHub release, repository metadata,
   vulnerability-reporting state, and installed plugin before calling the
   release complete. If post-publication verification fails, preserve every
   remote object, restore the previously verified managed tag, record the
   failure, and stop.
