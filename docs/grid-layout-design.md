# Grid layout design (proposed, not built)

Date: 2026-09-02. Status: implemented in 0.11.0 on 2026-09-03 as the `layout` and `opus` settings; see README "Settings: grid layout and Opus".

## Ask

Terry asked for one tab holding the room pane plus one pane per model, so the
group chat and each participant's native session are visible together, with
Opus 5 as a fourth participant beside Astra, Fable and Grok.

## What the live proof showed

On the v0.10.7 `astra-fable-grok-pi` room, `herdr pane move --tab <room tab>
--split right|down --target-pane <id>` placed the three peer panes beside the
room pane as a right-hand stack. Pane ids changed, agent names did not, and the
relay prompts by agent name, so an `@all` round returned exactly `GRID-OK` from
all three. Three costs surfaced:

1. `pane move` has no `--no-focus`; focus jumped to the `group-chat` workspace
   and had to be restored with `workspace focus` plus `tab focus`.
2. The launcher's recorded `participant_pane_ids` went stale, so `/show <role>`
   and reopen would target closed panes.
3. `adopt-peers` refused the moved peers because it requires them in the
   `agents · group-chat` workspace, and that workspace vanished once its tabs
   emptied.

## Proposed shape

- A `layout` setting in the plugin config directory, values `compact` (current
  default) and `grid`. No new action, so the seven-action smoke contract is
  unchanged.
- In `grid`, the launcher still creates and verifies every participant in the
  backstage workspace exactly as today. After the whole profile verifies, it
  records the caller's workspace and tab, moves each verified peer pane into
  the room tab (room left at ratio 0.55, peers stacked right in roster order),
  rewrites `participant_pane_ids` and the recorded workspace for each role from
  `agent get`, restores the caller's workspace and tab, and closes the emptied
  backstage workspace only after every move succeeds. A failed move fails the
  launch closed and leaves the compact topology intact.
- `adopt-peers` and reopen learn a second accepted home: the recorded room tab.
- `/agents` and `/show` in `grid` focus panes inside the room tab instead of
  switching workspaces.

## Opus participant

- `OPUS_PARTICIPANT`: kind `claude`, name `opus-peer`, start args
  `--model opus --effort high`, pane proofs `(("opus",), ("5",))` and
  `(("high",), ("effort",))`. Verify the exact model line Claude Code 2.1.258
  renders before choosing the version token; the Fable proof broke on `5.1`.
- Profile `astra-fable-grok-opus-pi` = Astra, Fable, Grok (Pi-xAI), Opus. This does
  add an action (`new-astra-fable-grok-opus`), so the smoke contract, its assays
  and `RELEASING.md` must list eight actions. Alternatively make Opus a config
  toggle on the default profile to keep seven actions; decide with Terry.

## Open decisions for Terry

1. Config toggle versus new action for both grid and Opus.
2. Whether grid should become the default.
3. Whether the room pane should stay a plugin-managed pane in `grid` (it does
   today) or move too.

## Gate

Minor release (0.11.0) under standing authority once the full candidate and
installed smokes pass for both layouts. Not a tonight change.

## Open option: in-room launch (not built)

Recorded 8 September 2026 after the emptied backstage workspace was found alive with a default shell tab. Terry asked whether the in-room layouts should stop creating the backstage workspace altogether and launch each peer directly in the room tab.

The mechanism exists: `herdr pane split <pane> --direction right|down --ratio r --cwd --env` creates a shell pane in the same tab, and `herdr agent start <name> --kind <kind> --pane <id>` starts the peer there, so the `grid2_moves` and `quad_moves` tables could drive splits instead of moves.

It was not built because the backstage workspace gives failure isolation (a peer that fails native-UI verification never appears in the room tab and the launch fails closed with the room untouched), the `pane move` ratio semantics are probed and encoded while `pane split --ratio` would need a fresh live probe in an isolated `hgc-probe-*` session, and `compact` would keep the backstage path anyway, leaving two launch paths. The user-visible benefit is small: the backstage workspace is created `--no-focus` and now closes as soon as the peers have moved.

Revisit if the brief appearance of the backstage workspace becomes a problem, or if a layout needs peers that cannot be moved. The first step is the split-ratio probe; the second is a cleanup path that closes every created pane when a peer fails verification.
