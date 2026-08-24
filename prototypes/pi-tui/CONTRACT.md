# Standalone pi-tui prototype contract

## ATP label

- **Tool surface:** Read, edit/write, Bash, npm, and Git only inside this worktree.
- **Context:** This public repository and dependencies installed from the public npm registry. Do not read `~/chromatin`, `~/germline`, `~/epigenome`, `~/.pi` sessions, or other repositories.
- **Model lane:** `bigmodel-coding/glm-5.3`, low thinking.
- **Output:** One focused commit plus test and typecheck evidence.
- **Session boundary:** This prototype only.
- **Side effects:** Files, npm installation, tests, and a local branch commit. No push, publication, Herdr control, messages, or external state changes.
- **Deterministic alternative:** Preserve the Python relay as backend and JSONL as authority. Do not reimplement relay logic.

## Goal

Prove that Herdr Group Chat can use `@earendil-works/pi-tui` as a peer-neutral standalone UI without running a Pi `AgentSession` or making Pi a required participant.

## Scope

Allowed modifications only:

- `.gitignore`
- `README.md`, limited to a short pointer to the prototype
- `prototypes/pi-tui/**`

Do not modify `herdr-group-chat`, `new-room`, `orca-group-chat`, `herdr-plugin.toml`, Python tests, release files, or any other path.

Build a runnable TypeScript prototype under `prototypes/pi-tui` using `@earendil-works/pi-tui` pinned to `0.84.2`.

1. Instantiate `ProcessTerminal` and `TuiAltScreen` directly. Do not import or depend on `@earendil-works/pi-coding-agent`, and do not create an `AgentSession`.
2. Accept an exact transcript path through `--transcript PATH`. Parse append-only Herdr Group Chat JSONL records, reject malformed records clearly, render speaker-labelled bodies as Markdown, follow appended records, and keep the transcript scrollable and searchable through pi-tui's viewport.
3. Provide a multiline `Editor` composer. Add autocomplete for explicit repeatable `--agent NAME` values and these slash commands: `/review`, `/anneal`, `/inbox`, `/room`. Preserve typed text exactly on submit.
4. Dispatch by spawning the existing Python relay as an argv array, never a shell. `--backend PATH` and repeatable `--backend-arg VALUE` define the command prefix. Append exactly `["--once", submittedText]`. The frontend must never write the transcript itself.
5. Disable further submission while one backend child is active and show a status line.
6. Do not expose `/cancel` and do not send signals or terminal keys to participants. Ctrl-C while a dispatch is active must refuse to exit and leave the child running. After the child settles, Ctrl-C may exit cleanly. This preserves the current cancellation decision.
7. Include a safe noninteractive `--render-fixture PATH` mode that parses and prints a deterministic plain-text projection without starting a TUI or backend. Include a small synthetic fixture.
8. Keep this explicitly experimental. Do not wire it into `herdr-plugin.toml` or replace the curses UI.

## Tests

Cover:

- transcript parsing;
- malformed JSONL;
- deterministic fixture rendering;
- exact argv construction, including spaces and newlines without shell interpolation;
- agent and command autocomplete;
- sole-writer behavior;
- the no-Pi-`AgentSession` dependency invariant.

Use Node's test runner or another small deterministic surface. Add scripts for test, typecheck, and fixture smoke. Ensure `node_modules` is ignored.

## Acceptance

From `prototypes/pi-tui`:

```bash
npm ci
npm test
npm run typecheck
npm run smoke
```

From the repository root:

```bash
uv run --with pytest pytest -q
uv run ruff check herdr-group-chat new-room orca-group-chat assays
uv run ruff format --check herdr-group-chat new-room orca-group-chat assays
git diff --check origin/main...
```

## Handoff

Commit the focused result with a meaningful message. Finish with changed paths, exact verifier results, commit hash, and any residual prototype limitation. Do not push.
