# Releasing

## Local release gate

1. Align the versions in `pyproject.toml` and `herdr-plugin.toml`.
2. Run `uv lock --check`, the test suite, Ruff over both executables and all
   assays, and Chaperone.
3. Export the tracked tree into a temporary directory and link that copy as
   a Herdr plugin. Verify the manifest has no warnings. Confirm all four
   actions are present: `new`, `new-sol-fable`, `new-classic`, and `open`.
   Check both pane entrypoints, exact workspace ownership, and reopen
   behavior. Also check named-session isolation and partial-failure retry
   behavior. Run a verified three-agent default (sol-fable-grok) room round
   and a classic four-agent room round.
   Verify the four actions map to their exact commands: `new` runs
   `./new-room --launch --profile sol-fable-grok`; `new-sol-fable` runs
   `./new-room --launch --profile sol-fable`; `new-classic` runs
   `./new-room --launch`; `open` runs `./new-room --open`.
4. Commit the release candidate and create the annotated release tag.

## Publication gate

Publication requires explicit approval. After approval:

1. Confirm the existing public `terry-li-hm/herdr-group-chat` repository is
   present with `main` as its default branch and an accurate description, then
   push `main` plus the release tag.
2. Unlink the local development copy, install that exact tag with
   `herdr plugin install terry-li-hm/herdr-group-chat --ref <tag>`, and rerun
   both the verified default three-agent (sol-fable-grok) smoke test and the
   classic four-agent smoke test from the managed checkout.
3. Confirm GitHub private vulnerability reporting remains enabled.
4. Confirm the `herdr-plugin` repository topic remains present and Herdr's
   marketplace listing remains accurate (the marketplace is an automatic,
   unreviewed index), then restore the desired local installation.
