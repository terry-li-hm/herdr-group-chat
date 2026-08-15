# Releasing

## Local release gate

1. Align the versions in `pyproject.toml` and `herdr-plugin.toml`.
2. Run `uv lock --check`, the test suite, Ruff over both executables and all
   assays, and Chaperone.
3. Export the tracked tree into a temporary directory, link that copy as a
   Herdr plugin, and verify the manifest has no warnings, both actions, both
   pane entrypoints, exact workspace ownership, reopen behavior, named-session
   isolation, partial-failure retry behavior, and a four-agent room round.
4. Commit the release candidate and create the annotated release tag.

## Publication gate

Publication requires explicit approval. After approval:

1. Create the public `terry-li-hm/herdr-group-chat` repository with an accurate
   description and push `main` plus the release tag.
2. Unlink the local development copy, install that exact tag with
   `herdr plugin install terry-li-hm/herdr-group-chat --ref <tag>`, and rerun
   the four-agent smoke test from the managed checkout.
3. Enable GitHub private vulnerability reporting.
4. Add the `herdr-plugin` repository topic. Herdr's marketplace is an automatic,
   unreviewed index, so confirm the repository metadata and listing are
   accurate, then restore the desired local installation.
