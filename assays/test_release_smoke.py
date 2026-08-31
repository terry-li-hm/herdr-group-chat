"""Deterministic offline assays for the release-smoke harness."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from typing import Any

import pytest

EFFECTOR = Path(__file__).resolve().parent.parent / "release-smoke"
loader = SourceFileLoader("release_smoke", str(EFFECTOR))
spec = spec_from_loader(loader.name, loader)
assert spec is not None
module = module_from_spec(spec)
sys.modules[module.__name__] = module
loader.exec_module(module)

# The real production contracts, spelled out independently of the harness.
REAL_ACTION_COMMANDS: dict[str, list[str]] = {
    "new": ["./new-room", "--launch", "--profile", "sol-fable-grok-pi"],
    "new-sol-fable": ["./new-room", "--launch", "--profile", "sol-fable"],
    "new-sol-fable-grok-native": ["./new-room", "--launch", "--profile", "sol-fable-grok"],
    "new-sol-fable-glm": ["./new-room", "--launch", "--profile", "sol-fable-glm"],
    "new-classic": ["./new-room", "--launch"],
    "open": ["./new-room", "--open"],
    "adopt-peers": ["./new-room", "--adopt-peers"],
}
REAL_PANE_COMMANDS: dict[str, list[str]] = {
    "new-room": ["./new-room"],
    "room": ["./new-room", "--room-entrypoint"],
}
ROOM_ROLES = {
    "new": ["sol", "fable", "grok"],
    "new-classic": ["pi", "claude", "codex", "grok"],
}
ROOM_PEERS = {
    "new": ["sol-peer", "fable-peer", "grok46pi-peer"],
    "new-classic": ["pi-peer", "claude-peer", "codex-peer", "grok-peer"],
}

FAKE_HERDR = r"""#!/usr/bin/env python3
ROOM_ROLES = {
    "new": ["sol", "fable", "grok"],
    "new-classic": ["pi", "claude", "codex", "grok"],
}
ROOM_PEERS = {
    "new": ["sol-peer", "fable-peer", "grok46pi-peer"],
    "new-classic": ["pi-peer", "claude-peer", "codex-peer", "grok-peer"],
}
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

argv = sys.argv[1:]
home = Path(os.environ["FAKE_HERDR_HOME"])
registry = home / "registry"
state_path = home / "state.json"
rooms_path = home / "rooms.json"
socket_path = home / "herdr.sock"
with open(os.environ["FAKE_HERDR_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\n")

args = list(argv)
session = None
if args[:1] == ["--session"]:
    session = args[1]
    args = args[2:]
cmd = args[0] if args else ""

fail = os.environ.get("FAKE_HERDR_FAIL", "")
if fail and " ".join(args[: len(fail.split())]) == fail:
    if fail == "server":
        time.sleep(0.05)
        print("fake herdr injected failure", file=sys.stderr)
        sys.exit(1)
    print("fake herdr injected failure", file=sys.stderr)
    sys.exit(1)

slow_patterns = [
    pattern
    for pattern in os.environ.get("FAKE_HERDR_SLOW", "").split(";")
    if pattern.strip()
]
if any(" ".join(args[: len(pattern.split())]) == pattern for pattern in slow_patterns):
    time.sleep(float(os.environ.get("FAKE_HERDR_SLOW_SECONDS", "10")))


def load_state():
    if state_path.exists():
        return json.loads(state_path.read_text())
    return {"rooms": 0, "stopped": [], "deleted": []}


def save_state(state):
    state_path.write_text(json.dumps(state))


def load_rooms():
    if rooms_path.exists():
        return json.loads(rooms_path.read_text())
    return {"rooms": []}


def save_rooms(rooms):
    rooms_path.write_text(json.dumps(rooms))


def envelope(payload):
    print(json.dumps({"id": "cli:fake", "result": payload}))


state = load_state()

if cmd == "server":
    socket_path.touch()
    (home / f"server-{session}.pid").write_text(str(os.getpid()))
    time.sleep(3600)
    sys.exit(0)

STAGES = ("invoked", "workspaces", "agents", "ready")


def stage_index(room):
    return STAGES.index(room.get("stage", "invoked"))


def all_agents():
    agents = []
    seen = set()
    # Peers are unique live agents: the most recent room owns the mapping and
    # stale peers keep their earlier backstage workspace.
    for room in reversed(load_rooms()["rooms"]):
        if stage_index(room) < STAGES.index("agents"):
            continue
        for peer in room["peers"]:
            if peer["name"] in seen:
                continue
            seen.add(peer["name"])
            entry = {
                "name": peer["name"],
                "kind": "peer",
                "workspace_id": room["agents_ws"],
                "cwd": "/agent",
            }
            if stage_index(room) >= STAGES.index("ready"):
                entry["agent_status"] = peer.get("status", "idle")
            else:
                entry["agent_status"] = "working"
            agents.append(entry)
    agents.reverse()
    return agents


if cmd == "workspace":
    sub = args[1]
    if sub == "list":
        if os.environ.get("FAKE_HERDR_RUNTIME_MALFORMED"):
            envelope({"workspaces": ["bogus"]})
            sys.exit(0)
        focused = state.get("focus", "caller")
        workspaces = [
            {
                "workspace_id": "w-caller",
                "label": "smoke caller",
                "focused": focused == "caller",
                "active_tab_id": "w-caller:t1",
            }
        ]
        latest = load_rooms()["rooms"][-1] if load_rooms()["rooms"] else None
        if latest and stage_index(latest) >= STAGES.index("workspaces"):
            workspaces.extend(
                [
                    {
                        "workspace_id": latest["chat_ws"],
                        "label": "group-chat",
                        "focused": focused == "group",
                        "active_tab_id": f"{latest['chat_ws']}:t1",
                    },
                    {
                        "workspace_id": latest["agents_ws"],
                        "label": "agents · group-chat",
                        "focused": False,
                        "active_tab_id": f"{latest['agents_ws']}:t1",
                    },
                ]
            )
        envelope({"workspaces": workspaces})
    elif sub == "create":
        state.setdefault("focus", "caller")
        save_state(state)
        envelope({"workspace": {"workspace_id": "w-caller", "active_tab_id": "w-caller:t1"}})
    else:
        print(f"unknown workspace subcommand {sub}", file=sys.stderr)
        sys.exit(1)
elif cmd == "agent":
    sub = args[1]
    if sub == "list":
        envelope({"agents": all_agents()})
    else:
        print(f"unknown agent subcommand {sub}", file=sys.stderr)
        sys.exit(1)
elif cmd == "pane":
    sub = args[1]
    rooms = load_rooms()
    if sub == "list":
        workspace_id = args[args.index("--workspace") + 1]
        panes = [
            {
                "pane_id": room["pane"],
                "tab_id": room["tab"],
                "label": "New group chat",
                "workspace_id": room["chat_ws"],
            }
            for room in rooms["rooms"]
            if stage_index(room) >= STAGES.index("ready") and room["chat_ws"] == workspace_id
        ]
        envelope({"panes": panes})
    elif sub == "read":
        pane_id = args[2]
        room = next((r for r in rooms["rooms"] if r["pane"] == pane_id), None)
        if room is None:
            print(f"unknown pane {pane_id}", file=sys.stderr)
            sys.exit(1)
        print(room.get("text", ""))
    elif sub == "send-text":
        pane_id = args[2]
        text = args[3]
        room = next((r for r in rooms["rooms"] if r["pane"] == pane_id), None)
        if room is None:
            print(f"unknown pane {pane_id}", file=sys.stderr)
            sys.exit(1)
        room["pending_text"] = text
        save_rooms(rooms)
    elif sub == "send-keys":
        pane_id = args[2]
        keys = args[3:]
        room = next((r for r in rooms["rooms"] if r["pane"] == pane_id), None)
        if room is None:
            print(f"unknown pane {pane_id}", file=sys.stderr)
            sys.exit(1)
        if "enter" in keys and room.get("pending_text"):
            message = room.pop("pending_text")
            room.setdefault("text", "")
            room["text"] += f"human> {message}\n"
            if os.environ.get("FAKE_HERDR_FOCUS_STEAL_DURING_REPLY"):
                live_state = load_state()
                live_state["focus"] = "group"
                save_state(live_state)
            system_mode = os.environ.get("FAKE_SYSTEM_ERROR", "")
            if system_mode == "indent":
                room["text"] += "  system> delivery failed for this round\n"
            elif system_mode == "status":
                room["text"] += "Delivery failed: @grok\n"
            elif system_mode:
                room["text"] += "system: delivery failed for this round\n"
            save_rooms(rooms)
            mode = os.environ.get("FAKE_REPLY_MODE", "")
            drop = set(filter(None, os.environ.get("FAKE_REPLY_DROP", "").split(",")))
            extra = os.environ.get("FAKE_REPLY_EXTRA", "")

            def deliver():
                rooms_now = load_rooms()
                target = next(
                    (r for r in rooms_now["rooms"] if r["pane"] == pane_id), None
                )
                if target is None:
                    return
                lines = []
                for role in target["roles"]:
                    if role in drop:
                        continue
                    if mode == "prefix":
                        body = "ok: SMOKE-OK"
                    elif mode == "suffix":
                        body = f"SMOKE-OK from {role}"
                    elif mode == "text":
                        body = f"the smoke passed for {role}"
                    elif mode == "explain":
                        lines.append(f"{role}> SMOKE-OK")
                        lines.append("because the smoke passed overall")
                        continue
                    else:
                        body = "SMOKE-OK"
                    lines.append(f"{role}> {body}")
                    if os.environ.get("FAKE_REPLY_DUPLICATE") and role == target["roles"][0]:
                        lines.append(f"{role}> {body}")
                if extra:
                    lines.append(f"{extra}> SMOKE-OK")
                target["text"] += "\n".join(lines) + "\n"
                save_rooms(rooms_now)

            threading.Timer(0.4, deliver).start()
        save_rooms(rooms)
    else:
        print(f"unknown pane subcommand {sub}", file=sys.stderr)
        sys.exit(1)
elif cmd == "plugin":
    sub = args[1]
    if sub == "link":
        root = Path(args[2])
        import tomllib

        manifest = tomllib.loads((root / "herdr-plugin.toml").read_text())
        target = registry / manifest["id"]
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(root, target)
        (target / ".link-source").write_text(str(root))
        save_state(state)
    elif sub == "unlink":
        plugin_id = args[2]
        target = registry / plugin_id
        if not target.exists():
            print(f"unknown plugin {plugin_id}", file=sys.stderr)
            sys.exit(1)
        shutil.rmtree(target)
        save_state(state)
    elif sub == "list":
        if os.environ.get("FAKE_HERDR_BAD_JSON"):
            print("this is not json")
            sys.exit(0)
        plugin_id = args[args.index("--plugin") + 1]
        target = registry / plugin_id
        if not target.exists():
            mode = os.environ.get("FAKE_HERDR_PREFLIGHT_FAIL", "")
            if mode == "json":
                print("definitely not json")
                sys.exit(0)
            if mode == "transport":
                print("connection refused", file=sys.stderr)
                sys.exit(1)
            if mode == "timeout":
                time.sleep(30)
            if os.environ.get("FAKE_HERDR_ID_COLLISION"):
                envelope(
                    {
                        "plugins": [
                            {
                                "plugin_id": plugin_id,
                                "version": "0.0.1",
                                "actions": [],
                                "panes": [],
                                "plugin_root": str(registry / "other-root"),
                            }
                        ]
                    }
                )
                sys.exit(0)
            envelope({"plugins": []})
            sys.exit(0)
        import tomllib

        manifest = tomllib.loads((target / "herdr-plugin.toml").read_text())
        link_source = target / ".link-source"
        plugin_root = link_source.read_text() if link_source.exists() else str(target)
        actions = [
            {"id": a["id"], "command": a["command"], "contexts": a["contexts"]}
            for a in manifest["actions"]
        ]
        if os.environ.get("FAKE_HERDR_DUPLICATE_ACTION") and actions:
            actions.append(dict(actions[0]))
        envelope(
            {
                "plugins": [
                    {
                        "plugin_id": plugin_id,
                        "version": manifest["version"],
                        "actions": actions,
                        "panes": [
                            {"id": p["id"], "command": p["command"], "placement": p["placement"]}
                            for p in manifest["panes"]
                        ],
                        "plugin_root": plugin_root,
                    }
                ]
            }
        )
    elif sub == "action":
        action = args[3]
        state["rooms"] += 1
        room_no = state["rooms"]
        if os.environ.get("FAKE_HERDR_FOCUS_STEAL") == action:
            state["focus"] = "group"
        save_state(state)
        roles = list(ROOM_ROLES[action])
        peers = []
        for peer in ROOM_PEERS[action]:
            status = "idle"
            if os.environ.get("FAKE_HERDR_BLOCKED_PEER") == peer:
                status = "blocked"
            if os.environ.get("FAKE_HERDR_MISSING_PEER") == peer:
                continue
            peers.append({"name": peer, "status": status})
        tab = f"tab-room-{room_no}"
        if os.environ.get("FAKE_HERDR_REUSE_TAB") and room_no > 1:
            tab = f"tab-room-{room_no - 1}"
        room = {
            "no": room_no,
            "action": action,
            "stage": "invoked",
            "chat_ws": f"w-chat-{room_no}",
            "agents_ws": f"w-agents-{room_no}",
            "pane": f"pane-room-{room_no}",
            "tab": tab,
            "roles": roles,
            "peers": peers,
            "text": "",
        }
        rooms_now = load_rooms()
        rooms_now["rooms"].append(room)
        save_rooms(rooms_now)

        def promote(stage_name):
            rooms_now = load_rooms()
            target = next(r for r in rooms_now["rooms"] if r["no"] == room_no)
            target["stage"] = stage_name
            if stage_name == "ready":
                target["text"] = f"room {room_no} ready: " + " ".join(
                    f"@{role}" for role in roles
                ) + "\n"
                if os.environ.get("FAKE_REPLY_PRIOR_CHATTER"):
                    target["text"] += (
                        f"\n{roles[0]}> old business from an earlier round\n"
                        "because it looked similar\n"
                        "human> an old question\n"
                        f"{roles[-1]}> SMOKE-OK\n"
                    )
            save_rooms(rooms_now)

        threading.Timer(0.15, lambda: promote("workspaces")).start()
        threading.Timer(0.3, lambda: promote("agents")).start()
        threading.Timer(0.5, lambda: promote("ready")).start()
    else:
        print(f"unknown plugin subcommand {sub}", file=sys.stderr)
        sys.exit(1)
elif cmd == "session":
    sub = args[1]
    if sub == "list":
        sessions = [
            {
                "name": "default",
                "running": True,
                "socket_path": str(socket_path),
            }
        ]
        if session and (
            (home / f"server-{session}.pid").exists()
            or os.environ.get("FAKE_HERDR_SESSION_COLLISION")
        ):
            sessions.append({"name": session, "running": True, "socket_path": str(socket_path)})
        print(json.dumps({"sessions": sessions}))
    elif sub == "stop":
        pid_file = home / f"server-{args[2]}.pid"
        if pid_file.exists():
            pid = int(pid_file.read_text())
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass
            pid_file.unlink()
        state["stopped"].append(args[2])
        save_state(state)
    elif sub == "delete":
        pid_file = home / f"server-{args[2]}.pid"
        attempts = state.setdefault("delete_attempts", {})
        attempts[args[2]] = attempts.get(args[2], 0) + 1
        save_state(state)
        if os.environ.get("FAKE_HERDR_DELETE_ONCE") and attempts[args[2]] == 1:
            print("transient delete failure", file=sys.stderr)
            sys.exit(1)
        if pid_file.exists():
            pid = int(pid_file.read_text())
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pid_file.unlink()
            else:
                print("refusing to delete a running session", file=sys.stderr)
                sys.exit(1)
        state["deleted"].append(args[2])
        save_state(state)
        print(json.dumps({"deleted": True}))
    else:
        print(f"unknown session subcommand {sub}", file=sys.stderr)
        sys.exit(1)
else:
    print(f"unknown command {cmd}", file=sys.stderr)
    sys.exit(1)
"""


def make_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def manifest_text(version: str) -> str:
    parts = [
        'id = "terry.herdr-group-chat"',
        'name = "Herdr Group Chat"',
        f'version = "{version}"',
        'min_herdr_version = "0.8.0"',
        'description = "A shared local room for full native AI coding agents."',
        'platforms = ["macos", "linux"]',
        "",
    ]
    for action_id, command in REAL_ACTION_COMMANDS.items():
        parts.extend(
            [
                "[[actions]]",
                f'id = "{action_id}"',
                f'title = "{action_id}"',
                'contexts = ["workspace", "tab", "pane"]',
                f"command = {json.dumps(command)}",
                "",
            ]
        )
    for pane_id, command in REAL_PANE_COMMANDS.items():
        parts.extend(
            [
                "[[panes]]",
                f'id = "{pane_id}"',
                f'title = "{pane_id}"',
                'placement = "tab"',
                f"command = {json.dumps(command)}",
                "",
            ]
        )
    return "\n".join(parts)


def make_plugin_root(path: Path, version: str = "0.10.5") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "herdr-plugin.toml").write_text(manifest_text(version))
    return path


def git(root: Path, *arguments: str) -> None:
    git_bin = shutil.which("git")
    assert git_bin is not None, "git is required for these assays"
    subprocess.run(
        [git_bin, "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )


def make_candidate_repo(path: Path, version: str = "0.10.5") -> Path:
    root = make_plugin_root(path, version)
    git(root, "init", "-q")
    git(root, "add", "-A")
    return root


class Harness:
    """A fully faked, offline environment for running release-smoke end to end."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.home = tmp_path / "fake-herdr"
        self.bin_dir = tmp_path / "bin"
        self.bin_dir.mkdir()
        self.log = tmp_path / "commands.jsonl"
        self.log.write_text("")
        make_executable(self.bin_dir / "herdr", FAKE_HERDR)
        self.registry = self.home / "registry"
        self.agent_cwd = tmp_path / "agent-cwd"
        self.agent_cwd.mkdir()
        monkeypatch.setenv("FAKE_HERDR_HOME", str(self.home))
        monkeypatch.setenv("FAKE_HERDR_LOG", str(self.log))
        monkeypatch.delenv("HERDR_ENV", raising=False)

    def install_plugin(self, version: str = "0.10.5") -> Path:
        source = make_plugin_root(self.home / "source" / "installed", version)
        target = self.registry / "terry.herdr-group-chat"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
        return target

    def candidate_repo(self, version: str = "0.10.5") -> Path:
        if not (self.home / "source" / "candidate" / ".git").exists():
            make_candidate_repo(self.home / "source" / "candidate", version)
        return self.home / "source" / "candidate"

    def env(self, **extra: str) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if key != "HERDR_ENV"}
        env.update(extra)
        return env

    def run(
        self, *arguments: str, env_extra: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = self.env(**(env_extra or {}))
        return subprocess.run(
            [sys.executable, str(EFFECTOR), *arguments],
            capture_output=True,
            text=True,
            errors="replace",
            env=env,
            timeout=180,
            check=False,
        )

    def run_candidate(
        self, env_extra: dict[str, str] | None = None, timeout: str = "30"
    ) -> subprocess.CompletedProcess[str]:
        return self.run(
            "candidate",
            "--plugin-root",
            str(self.candidate_repo()),
            "--agent-cwd",
            str(self.agent_cwd),
            "--timeout",
            timeout,
            "--herdr-bin",
            str(self.bin_dir / "herdr"),
            env_extra=env_extra,
        )

    def run_installed(
        self, version: str = "0.10.5", env_extra: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return self.run(
            "installed",
            "--plugin-id",
            "terry.herdr-group-chat",
            "--expected-version",
            version,
            "--agent-cwd",
            str(self.agent_cwd),
            "--timeout",
            "30",
            "--herdr-bin",
            str(self.bin_dir / "herdr"),
            env_extra=env_extra,
        )

    def commands(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def rooms(self) -> list[dict[str, Any]]:
        path = self.home / "rooms.json"
        if not path.exists():
            return []
        return json.loads(path.read_text())["rooms"]

    def state(self) -> dict[str, Any]:
        path = self.home / "state.json"
        return json.loads(path.read_text()) if path.exists() else {}


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    made = Harness(tmp_path, monkeypatch)
    made.install_plugin()
    made.candidate_repo()
    return made


def assert_candidate_ok(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["error"] is None
    assert payload["mode"] == "candidate"
    return payload


def test_candidate_temporary_id_differs_from_installed(harness: Harness) -> None:
    root = harness.candidate_repo()
    payload = assert_candidate_ok(harness.run_candidate())
    temporary_id = payload["temporary_plugin_id"]
    assert temporary_id.startswith("terry.herdr-group-chat.smoke.")
    assert temporary_id != module.INSTALLED_PLUGIN_ID
    assert payload["plugin_id"] == module.INSTALLED_PLUGIN_ID
    import tomllib

    manifest = tomllib.loads((root / "herdr-plugin.toml").read_text())
    assert manifest["id"] == module.INSTALLED_PLUGIN_ID


def test_candidate_uses_staged_git_export_only(harness: Harness) -> None:
    root = harness.candidate_repo()
    junk = root / "junk"
    junk.mkdir()
    (junk / "blob.bin").write_bytes(b"x" * 1024)
    assert_candidate_ok(harness.run_candidate())


def test_export_rejects_escaping_and_absolute_symlinks(harness: Harness) -> None:
    root = harness.candidate_repo()
    os.symlink("../../outside", root / "escape")
    git(root, "add", "-A")
    result = harness.run_candidate()
    failure = json.loads(result.stdout)
    assert result.returncode == 1
    assert failure["error"]["stage"] == "export-candidate"
    assert "escaping symlink" in failure["error"]["message"]

    escape = root / "escape"
    escape.unlink()
    os.symlink("/etc/passwd", escape)
    git(root, "add", "-A")
    result = harness.run_candidate()
    failure = json.loads(result.stdout)
    assert result.returncode == 1
    assert failure["error"]["stage"] == "export-candidate"
    assert "absolute symlink" in failure["error"]["message"]


def test_tmpdir_nested_under_plugin_root_does_not_recurse(harness: Harness) -> None:
    root = harness.candidate_repo()
    nested = root / "tmp"
    nested.mkdir(exist_ok=True)
    result = harness.run_candidate(env_extra={"TMPDIR": str(nested)}, timeout="60")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"] is True


def test_exact_action_and_pane_validation(harness: Harness) -> None:
    payload = assert_candidate_ok(harness.run_candidate())
    assert payload["observed_version"] == "0.10.5"

    result = harness.run_installed(version="0.9.9")
    assert result.returncode == 1
    failure = json.loads(result.stdout)
    assert failure["ok"] is False
    assert failure["error"]["stage"] == "verify-registration"
    assert failure["observed_version"] == "0.10.5"


def test_wrong_action_command_fails_registration(harness: Harness) -> None:
    root = harness.candidate_repo()
    manifest = root / "herdr-plugin.toml"
    corrupted = manifest.read_text().replace(
        json.dumps(REAL_ACTION_COMMANDS["new"]), json.dumps(["./new-room"])
    )
    assert corrupted != manifest.read_text()
    manifest.write_text(corrupted)
    git(root, "add", "-A")
    result = harness.run_candidate()
    failure = json.loads(result.stdout)
    assert result.returncode == 1
    assert failure["error"]["stage"] == "verify-registration"
    assert "'new'" in failure["error"]["message"]
    assert "command" in failure["error"]["message"]


def test_wrong_pane_contract_fails_registration(harness: Harness) -> None:
    root = harness.candidate_repo()
    manifest = root / "herdr-plugin.toml"
    corrupted = manifest.read_text().replace('placement = "tab"', 'placement = "split"', 1)
    manifest.write_text(corrupted)
    git(root, "add", "-A")
    result = harness.run_candidate()
    failure = json.loads(result.stdout)
    assert result.returncode == 1
    assert failure["error"]["stage"] == "verify-registration"
    assert "placement" in failure["error"]["message"]


def test_duplicate_action_ids_fail_registration(harness: Harness) -> None:
    result = harness.run_candidate(env_extra={"FAKE_HERDR_DUPLICATE_ACTION": "1"})
    failure = json.loads(result.stdout)
    assert result.returncode == 1
    assert failure["error"]["stage"] == "verify-registration"
    assert "duplicate action ids" in failure["error"]["message"]
    assert "expected exactly" in failure["error"]["message"]


def test_temporary_id_collision_is_rejected_before_ownership(harness: Harness) -> None:
    result = harness.run_candidate(env_extra={"FAKE_HERDR_ID_COLLISION": "1"})
    failure = json.loads(result.stdout)
    assert result.returncode == 1
    assert failure["error"]["stage"] == "link-candidate"
    assert "already registered" in failure["error"]["message"]
    assert not [c for c in harness.commands() if c[2:4] == ["plugin", "link"]]
    assert "plugin_unlink" not in failure["cleanup"]


def test_session_name_collision_is_rejected_before_spawn(harness: Harness) -> None:
    result = harness.run_candidate(env_extra={"FAKE_HERDR_SESSION_COLLISION": "1"})
    failure = json.loads(result.stdout)
    assert result.returncode == 1
    assert failure["error"]["stage"] == "start-session"
    assert "already exists" in failure["error"]["message"]
    commands = harness.commands()
    assert not [c for c in commands if "server" in c]
    assert not [c for c in commands if c[2:4] == ["session", "stop"]]
    assert not [c for c in commands if c[2:4] == ["session", "delete"]]
    assert "session_stop" not in failure["cleanup"]
    assert "session_delete" not in failure["cleanup"]


def test_session_names_use_a_full_uuid_suffix(harness: Harness) -> None:
    payload = assert_candidate_ok(harness.run_candidate())
    suffix = payload["session"].removeprefix("group-chat-smoke-")
    assert len(suffix) == 32
    int(suffix, 16)


def test_readiness_polls_live_surfaces_until_the_room_is_ready(harness: Harness) -> None:
    started = time.monotonic()
    payload = assert_candidate_ok(harness.run_candidate())
    # Workspaces land at 0.15s, peers at 0.3s, the room pane at 0.5s per room,
    # so a passing run proves the harness polled every surface instead of
    # reading partial state.
    assert time.monotonic() - started >= 1.0
    assert [room["participants"] for room in payload["rooms"]] == [
        ["sol", "fable", "grok"],
        ["pi", "claude", "codex", "grok"],
    ]
    assert payload["rooms"][0]["profile"] == "sol-fable-grok-pi"
    assert payload["rooms"][1]["profile"] is None
    for room in payload["rooms"]:
        assert room["replies"] == {name: "SMOKE-OK" for name in room["participants"]}


def test_blocked_and_missing_peers_time_out_bounded(harness: Harness) -> None:
    for env_extra, expected_stage in (
        ({"FAKE_HERDR_BLOCKED_PEER": "sol-peer"}, "round-new"),
        ({"FAKE_HERDR_MISSING_PEER": "grok-peer"}, "round-new-classic"),
    ):
        result = harness.run_candidate(env_extra=env_extra, timeout="2")
        assert result.returncode == 1, env_extra
        failure = json.loads(result.stdout)
        assert failure["error"]["stage"] == expected_stage, env_extra
        assert "never became ready" in failure["error"]["message"], env_extra
        # The failing round never started: no text was sent to its pane. Only
        # commands from this run's named session are considered.
        sends = [
            c
            for c in harness.commands()
            if c[2:4] == ["pane", "send-text"] and c[1] == failure["session"]
        ]
        if expected_stage == "round-new":
            assert not sends, env_extra
        else:
            # Only this run's default room may have been addressed.
            default_pane = failure["rooms"][0]["room_id"]
            assert all(c[4] == default_pane for c in sends), env_extra


def test_stale_default_peers_survive_into_the_classic_room(harness: Harness) -> None:
    assert_candidate_ok(harness.run_candidate())
    rooms = harness.rooms()
    classic_agents = [p["name"] for p in rooms[-1]["peers"]]
    assert classic_agents == ["pi-peer", "claude-peer", "codex-peer", "grok-peer"]
    # The stale default-room peers remain listed as live backstage agents.
    all_peers = {p["name"] for room in rooms for p in room["peers"]}
    assert {"sol-peer", "fable-peer", "grok46pi-peer"} <= all_peers


def test_replacement_requires_a_new_room_pane_identity(harness: Harness) -> None:
    payload = assert_candidate_ok(harness.run_candidate())
    default_room, classic_room = payload["rooms"]
    assert default_room["room_id"] != classic_room["room_id"]
    assert default_room["room_tab_id"] != classic_room["room_tab_id"]
    assert default_room["room_id"].startswith("pane-room-")
    assert classic_room["room_id"].startswith("pane-room-")


def test_focus_theft_fails_the_round_stage(harness: Harness) -> None:
    result = harness.run_candidate(env_extra={"FAKE_HERDR_FOCUS_STEAL": "new"})
    failure = json.loads(result.stdout)
    assert result.returncode == 1
    assert failure["error"]["stage"] == "round-new"
    assert (
        "not the caller" in failure["error"]["message"]
        or "took focus" in failure["error"]["message"]
    )


def test_visible_system_delivery_error_fails_immediately(harness: Harness) -> None:
    result = harness.run_candidate(env_extra={"FAKE_SYSTEM_ERROR": "1"})
    failure = json.loads(result.stdout)
    assert result.returncode == 1
    assert failure["error"]["stage"] == "round-new"
    assert "system error" in failure["error"]["message"]


def test_reply_validation_fails_closed(harness: Harness) -> None:
    missing = harness.run_candidate(env_extra={"FAKE_REPLY_DROP": "grok"})
    failure = json.loads(missing.stdout)
    assert missing.returncode == 1
    assert failure["error"]["stage"] == "round-new"
    assert "missing exact replies" in failure["error"]["message"]
    assert "grok" in failure["error"]["message"]

    extra = harness.run_candidate(env_extra={"FAKE_REPLY_EXTRA": "intruder"})
    failure = json.loads(extra.stdout)
    assert extra.returncode == 1
    assert "unexpected participant reply" in failure["error"]["message"]

    for mode in ("prefix", "suffix", "text"):
        result = harness.run_candidate(env_extra={"FAKE_REPLY_MODE": mode})
        failure = json.loads(result.stdout)
        assert result.returncode == 1, mode
        assert failure["error"]["stage"] == "round-new", mode
        message = failure["error"]["message"]
        assert "not exactly SMOKE-OK" in message or "missing exact replies" in message, mode


def test_candidate_unlinks_only_temporary_id(harness: Harness) -> None:
    assert_candidate_ok(harness.run_candidate())
    registry = harness.registry
    installed = registry / "terry.herdr-group-chat"
    assert installed.is_dir()
    assert (installed / "herdr-plugin.toml").is_file()
    assert [p.name for p in registry.iterdir()] == ["terry.herdr-group-chat"]
    unlinks = [c for c in harness.commands() if c[2:4] == ["plugin", "unlink"]]
    assert len(unlinks) == 1
    assert unlinks[0][4].startswith("terry.herdr-group-chat.smoke.")
    links = [c for c in harness.commands() if c[2:4] == ["plugin", "link"]]
    assert len(links) == 1
    assert links[0][4] != str(harness.home / "source" / "candidate")


def test_candidate_commands_are_session_scoped_and_never_use_default(harness: Harness) -> None:
    assert_candidate_ok(harness.run_candidate())
    for command in harness.commands():
        assert command[0] == "--session", command
        assert command[1].startswith("group-chat-smoke-"), command


def test_installed_mode_never_links_or_unlinks(harness: Harness) -> None:
    result = harness.run_installed()
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["temporary_plugin_id"] is None
    commands = harness.commands()
    assert not [c for c in commands if c[2:4] == ["plugin", "link"]]
    assert not [c for c in commands if c[2:4] == ["plugin", "unlink"]]
    assert payload["cleanup"].get("plugin_unlink") is None
    assert (harness.registry / "terry.herdr-group-chat").is_dir()


def test_session_stop_and_delete_on_success_and_failure(harness: Harness) -> None:
    assert_candidate_ok(harness.run_candidate())
    state = harness.state()
    assert len(state["stopped"]) == 1 and len(state["deleted"]) == 1
    assert state["stopped"][0].startswith("group-chat-smoke-")
    assert state["deleted"][0] == state["stopped"][0]

    result = harness.run_candidate(
        env_extra={"FAKE_HERDR_FAIL": "plugin action invoke new-classic"}
    )
    failure = json.loads(result.stdout)
    assert result.returncode == 1
    assert failure["error"]["stage"] == "launch-new-classic"
    assert failure["cleanup"]["session_stop"] == "ok"
    assert failure["cleanup"]["session_delete"] == "ok"
    assert failure["cleanup"]["plugin_unlink"] == "ok"


def test_cleanup_order_is_unlink_then_session_stop_and_delete(harness: Harness) -> None:
    assert_candidate_ok(harness.run_candidate())
    tail = [" ".join(c[2:4]) for c in harness.commands()]
    assert tail.index("plugin unlink") < tail.index("session stop")
    assert tail.index("session stop") < tail.index("session delete")


def test_ambiguous_server_startup_failure_cleans_the_session(harness: Harness) -> None:
    result = harness.run_candidate(env_extra={"FAKE_HERDR_FAIL": "server"})
    failure = json.loads(result.stdout)
    assert result.returncode == 1
    assert failure["error"]["stage"] == "start-session"
    assert failure["cleanup"]["session_stop"] == "ok"
    assert failure["cleanup"]["session_delete"] == "ok"
    assert "plugin_unlink" not in failure["cleanup"]


def test_ambiguous_link_failure_still_attempts_exact_temp_id_unlink(harness: Harness) -> None:
    result = harness.run_candidate(env_extra={"FAKE_HERDR_FAIL": "plugin link"})
    failure = json.loads(result.stdout)
    assert result.returncode == 1
    assert failure["error"]["stage"] == "link-candidate"
    unlinks = [c for c in harness.commands() if c[2:4] == ["plugin", "unlink"]]
    assert len(unlinks) == 1
    assert unlinks[0][4] == failure["temporary_plugin_id"]
    assert failure["cleanup"]["plugin_unlink"].startswith("failed:")


def test_cleanup_failure_turns_success_into_named_failure(harness: Harness) -> None:
    result = harness.run_candidate(env_extra={"FAKE_HERDR_FAIL": "plugin unlink"})
    failure = json.loads(result.stdout)
    assert result.returncode == 1
    assert failure["rooms"] and failure["stages_completed"][-1] == "round-new-classic"
    assert failure["error"]["stage"] == "cleanup-link"
    assert failure["cleanup"]["plugin_unlink"].startswith("failed:")
    assert failure["cleanup"]["session_stop"] == "ok"
    assert failure["cleanup"]["session_delete"] == "ok"

    stop = harness.run_candidate(env_extra={"FAKE_HERDR_FAIL": "session stop"})
    failure = json.loads(stop.stdout)
    session = failure["session"]
    assert stop.returncode == 1
    assert failure["error"]["stage"] == "cleanup-session-stop"
    assert failure["cleanup"]["session_stop"].startswith("failed:")
    # The owned server is reaped before deletion, so one delete suffices.
    session_deletes = [c for c in harness.commands() if c[2:5] == ["session", "delete", session]]
    assert session_deletes

    retry = harness.run_candidate(env_extra={"FAKE_HERDR_DELETE_ONCE": "1"})
    assert retry.returncode == 0, retry.stderr
    retry_payload = json.loads(retry.stdout)
    assert retry_payload["cleanup"]["session_delete"] == "ok (after retry)"
    retry_deletes = [
        c for c in harness.commands() if c[2:5] == ["session", "delete", retry_payload["session"]]
    ]
    assert len(retry_deletes) == 2

    delete = harness.run_candidate(env_extra={"FAKE_HERDR_FAIL": "session delete"})
    failure = json.loads(delete.stdout)
    assert delete.returncode == 1
    assert failure["error"]["stage"] == "cleanup-session-delete"


def test_stable_json_and_exit_behavior(harness: Harness) -> None:
    result = harness.run_candidate()
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert set(payload) == {
        "ok",
        "mode",
        "plugin_id",
        "session",
        "expected_version",
        "observed_version",
        "temporary_plugin_id",
        "caller_workspace_id",
        "rooms",
        "stages_completed",
        "cleanup",
        "error",
    }
    assert result.stderr.strip()
    assert payload["session"].startswith("group-chat-smoke-")

    other = harness.run_installed()
    assert other.returncode == 0
    assert json.loads(other.stdout)["session"] != payload["session"]


def test_invalid_json_output_preserves_the_requested_stage(harness: Harness) -> None:
    result = harness.run_installed(env_extra={"FAKE_HERDR_BAD_JSON": "1"})
    failure = json.loads(result.stdout)
    assert result.returncode == 1
    assert failure["error"]["stage"] == "verify-registration"
    assert "invalid JSON" in failure["error"]["message"]


def test_invalid_paths_emit_stable_json(harness: Harness) -> None:
    result = harness.run(
        "candidate",
        "--plugin-root",
        str(harness.home / "nonexistent"),
        "--agent-cwd",
        str(harness.agent_cwd),
        "--herdr-bin",
        str(harness.bin_dir / "herdr"),
    )
    failure = json.loads(result.stdout)
    assert result.returncode == 1
    assert failure["ok"] is False
    assert failure["error"]["stage"] == "usage"
    assert "Git checkout" in failure["error"]["message"]

    bad_cwd = harness.run(
        "candidate",
        "--plugin-root",
        str(harness.candidate_repo()),
        "--agent-cwd",
        str(harness.home / "missing-cwd"),
        "--herdr-bin",
        str(harness.bin_dir / "herdr"),
    )
    failure = json.loads(bad_cwd.stdout)
    assert bad_cwd.returncode == 1
    assert failure["error"]["stage"] == "usage"
    assert "agent-cwd" in failure["error"]["message"]


def test_malformed_invocations_emit_stable_json(harness: Harness) -> None:
    cases = [
        ("candidate", "--agent-cwd", str(harness.agent_cwd)),
        ("candidate", "--plugin-root", str(harness.candidate_repo())),
        (
            "candidate",
            "--plugin-root",
            str(harness.candidate_repo()),
            "--agent-cwd",
            str(harness.agent_cwd),
            "--timeout",
            "not-a-number",
        ),
        (
            "installed",
            "--plugin-id",
            "terry.herdr-group-chat",
            "--agent-cwd",
            str(harness.agent_cwd),
        ),
        ("bogus-subcommand",),
    ]
    for case in cases:
        result = harness.run(*case)
        assert result.returncode == 1, case
        failure = json.loads(result.stdout)
        assert failure["ok"] is False, case
        assert failure["error"]["stage"] == "usage", case
        assert failure["error"]["message"], case


def test_malformed_runtime_reports_the_internal_stage(harness: Harness) -> None:
    result = harness.run_candidate(env_extra={"FAKE_HERDR_RUNTIME_MALFORMED": "1"})
    failure = json.loads(result.stdout)
    assert result.returncode == 1
    assert failure["error"]["stage"] == "internal"


def test_no_focus_command_is_ever_issued(harness: Harness) -> None:
    assert_candidate_ok(harness.run_candidate())
    for command in harness.commands():
        assert "focus" not in command, command


def test_preflight_fails_closed_on_any_herdr_failure(harness: Harness) -> None:
    for mode in ("json", "transport", "timeout"):
        result = harness.run_candidate(env_extra={"FAKE_HERDR_PREFLIGHT_FAIL": mode}, timeout="2")
        assert result.returncode == 1, mode
        failure = json.loads(result.stdout)
        assert failure["error"]["stage"] == "link-candidate", mode
        assert (
            "cannot prove" not in failure["error"]["message"]
            or "malformed" in failure["error"]["message"]
        ), mode
        commands = harness.commands()
        session_commands = [c for c in commands if c[1] == failure["session"]]
        assert not [c for c in session_commands if c[2:4] == ["plugin", "link"]], mode
        assert not [c for c in session_commands if c[2:4] == ["plugin", "unlink"]], mode
        assert "plugin_unlink" not in failure["cleanup"], mode


def test_preflight_accepts_only_an_exact_empty_list(harness: Harness) -> None:
    # The clean run proves absence through plugins: [] and still links.
    payload = assert_candidate_ok(harness.run_candidate())
    assert payload["ok"] is True


def test_explanatory_reply_after_marker_line_fails(harness: Harness) -> None:
    result = harness.run_candidate(env_extra={"FAKE_REPLY_MODE": "explain"})
    failure = json.loads(result.stdout)
    assert result.returncode == 1
    assert failure["error"]["stage"] == "round-new"
    assert "not exactly SMOKE-OK" in failure["error"]["message"]


def test_system_error_detection_normalizes_whitespace_and_status_rows(
    harness: Harness,
) -> None:
    for mode, fragment in (
        ("indent", "system error"),
        ("status", "system error"),
        ("1", "system error"),
    ):
        result = harness.run_candidate(env_extra={"FAKE_SYSTEM_ERROR": mode})
        failure = json.loads(result.stdout)
        assert result.returncode == 1, mode
        assert failure["error"]["stage"] == "round-new", mode
        assert fragment in failure["error"]["message"], mode


def test_tab_reuse_blocks_replacement_readiness(harness: Harness) -> None:
    result = harness.run_candidate(env_extra={"FAKE_HERDR_REUSE_TAB": "1"}, timeout="3")
    failure = json.loads(result.stdout)
    assert result.returncode == 1
    assert failure["error"]["stage"] == "round-new-classic"
    assert "never became ready" in failure["error"]["message"]
    # The default round completed before the replacement stalled.
    assert failure["rooms"]


def test_timeout_is_a_wall_clock_budget(harness: Harness) -> None:
    started = time.monotonic()
    result = harness.run_candidate(env_extra={"FAKE_HERDR_SLOW": "agent list"}, timeout="2")
    elapsed = time.monotonic() - started
    failure = json.loads(result.stdout)
    assert result.returncode == 1
    assert failure["error"]["stage"] == "round-new"
    assert "timed out" in failure["error"]["message"]
    assert elapsed < 6, elapsed


def test_transient_focus_theft_during_reply_polling_fails(harness: Harness) -> None:
    result = harness.run_candidate(env_extra={"FAKE_HERDR_FOCUS_STEAL_DURING_REPLY": "1"})
    failure = json.loads(result.stdout)
    assert result.returncode == 1
    assert failure["error"]["stage"] == "round-new"
    message = failure["error"]["message"]
    assert "not the caller" in message or "took focus" in message


def test_invalid_timeouts_are_rejected_in_stable_json(harness: Harness) -> None:
    for value in ("inf", "nan", "0", "-5"):
        result = harness.run(
            "candidate",
            "--plugin-root",
            str(harness.candidate_repo()),
            "--agent-cwd",
            str(harness.agent_cwd),
            "--timeout",
            value,
            "--herdr-bin",
            str(harness.bin_dir / "herdr"),
        )
        assert result.returncode == 1, value
        failure = json.loads(result.stdout)
        assert failure["ok"] is False, value
        assert failure["error"]["stage"] == "usage", value
        assert "timeout" in failure["error"]["message"], value


def test_symlink_loop_reports_the_internal_stage(harness: Harness, tmp_path: Path) -> None:
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    result = harness.run(
        "candidate",
        "--plugin-root",
        str(harness.candidate_repo()),
        "--agent-cwd",
        str(loop),
        "--herdr-bin",
        str(harness.bin_dir / "herdr"),
    )
    failure = json.loads(result.stdout)
    assert result.returncode == 1
    assert failure["ok"] is False
    assert failure["error"]["stage"] == "internal"


def test_budget_is_recomputed_before_each_sequential_command(harness: Harness) -> None:
    # Two different sequential commands each delay 1.2s against a 2s budget:
    # reusing one stale budget would let the second run to 2.4s and beyond.
    started = time.monotonic()
    result = harness.run_candidate(
        env_extra={
            "FAKE_HERDR_SLOW": "workspace list;agent list",
            "FAKE_HERDR_SLOW_SECONDS": "1.2",
        },
        timeout="2",
    )
    elapsed = time.monotonic() - started
    failure = json.loads(result.stdout)
    assert result.returncode == 1
    assert failure["error"]["stage"] == "round-new"
    assert "timed out" in failure["error"]["message"]
    # Startup (export, boot probe, settle) costs a little over a second; a
    # stale reused budget would instead let this run stretch past 10 seconds.
    assert elapsed < 8, elapsed


def test_prior_role_chatter_does_not_poison_a_valid_reply(harness: Harness) -> None:
    payload = assert_candidate_ok(
        harness.run_candidate(env_extra={"FAKE_REPLY_PRIOR_CHATTER": "1"})
    )
    for room in payload["rooms"]:
        assert room["replies"] == {name: "SMOKE-OK" for name in room["participants"]}


def test_duplicate_post_marker_reply_fails(harness: Harness) -> None:
    result = harness.run_candidate(env_extra={"FAKE_REPLY_DUPLICATE": "1"})
    failure = json.loads(result.stdout)
    assert result.returncode == 1
    assert failure["error"]["stage"] == "round-new"
    assert "replied 2 times" in failure["error"]["message"]


def test_help() -> None:
    top = subprocess.run(
        [sys.executable, str(EFFECTOR), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert top.returncode == 0
    assert "candidate" in top.stdout and "installed" in top.stdout
    for subcommand in ("candidate", "installed"):
        sub = subprocess.run(
            [sys.executable, str(EFFECTOR), subcommand, "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert sub.returncode == 0
        assert "--agent-cwd" in sub.stdout
