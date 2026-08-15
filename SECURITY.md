# Security policy

## Supported versions

Security fixes are provided for the latest tagged release.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository. Do
not include credentials, private transcripts, or client information in a
public issue.

Herdr plugins are ordinary local programs: this plugin runs as the current user
and can call the full Herdr CLI. Review `herdr-plugin.toml`, `new-room`, and
`herdr-group-chat` before installing, and pin a trusted release tag.

The plugin does not grant permissions to agents or make network requests of its
own. It relays complete room messages to the addressed native agents, which may
use their normal network connections and tools, and stores the resulting
transcript in `HERDR_PLUGIN_STATE_DIR` with user-only permissions. Reports
concerning provider-side retention or an agent's own tool permissions should be
directed to that provider.
