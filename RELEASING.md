# Releasing

## Local release gate

1. Align the versions in `pyproject.toml` and `herdr-plugin.toml`.
2. Run `uv lock --check`, the test suite, Ruff, `sh -n open-room`, and
   Chaperone.
3. Export the tracked tree into a temporary directory, link that copy as a
   Herdr plugin, and verify its manifest, action, and room pane.
4. Commit the release candidate and create the annotated release tag.

## Publication gate

Publication requires explicit approval. After approval:

1. Create the public `terry-li-hm/herdr-group-chat` repository and push `main`
   plus the release tag.
2. Unlink the local development copy, install the tag with
   `herdr plugin install terry-li-hm/herdr-group-chat --ref v0.2.0`, and rerun
   the four-agent smoke test.
3. Enable GitHub private vulnerability reporting.
4. Add the `herdr-plugin` repository topic. Confirm the listing appears in
   Herdr's automatic directory, then restore the desired local installation.
