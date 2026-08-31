"""Deterministic offline assays for the release-smoke harness.

The core runs in-process against a virtual clock and a fake Herdr command
runner, so every behavioral contract is exercised without real sleeps or
spawns. Only the executable entrypoint, argparse/stable-JSON handling, and
the staged Git export boundaries run as black-box CLI subprocesses. Git
commands always dispatch to the real git executable, so the staged export
path is exercised for real.
"""

from __future__ import annotations

import inspect
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
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

# Virtual-time room lifecycles: the fake Herdr promotes a room purely from
# elapsed virtual time, so polling loops spin without any real sleeping.
WORKSPACES_AT = 0.15
AGENTS_AT = 0.3
READY_AT = 0.5
REPLIES_AT = 0.4
COMMAND_LATENCY = 0.16


class VirtualClock:
    """Monotonic clock whose sleep advances time instead of blocking."""

    def __init__(self) -> None:
        self.now = 10_000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.0)


class FakePopen:
    """Popen-like handle for the spawned fake session server."""

    def __init__(self, fake: FakeHerdr, session: str, returncode: int | None) -> None:
        self._fake = fake
        self._session = session
        self.returncode = returncode

    def _die(self, code: int) -> None:
        if self.returncode is None:
            self.returncode = code
        self._fake.live_sessions.discard(self._session)

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self._die(-15)

    def kill(self) -> None:
        self._die(-9)

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode


class FakeHerdr:
    """In-process port of the fake Herdr CLI, driven by the virtual clock.

    Failure-injection flags mirror the old subprocess fake's environment
    variables one for one; state (rooms, registry, sessions) persists across
    runs within one test, exactly like the old on-disk JSON state.
    """

    def __init__(self, clock: VirtualClock, registry: Path, log: list[list[str]]) -> None:
        self.clock = clock
        self.registry = registry
        self.log = log
        self.rooms: list[dict[str, Any]] = []
        self.linked: list[dict[str, Any]] = []
        self.state: dict[str, Any] = {"rooms": 0, "stopped": [], "deleted": []}
        self.live_sessions: set[str] = set()
        # Injection flags.
        self.fail = ""
        self.slow: list[str] = []
        self.slow_seconds = 10.0
        self.malformed_workspaces = False
        self.bad_json = False
        self.preflight_fail = ""
        self.id_collision = False
        self.duplicate_action = False
        self.focus_steal = ""
        self.focus_steal_during_reply = False
        self.system_error = ""
        self.reply_mode = ""
        self.reply_drop: set[str] = set()
        self.reply_extra = ""
        self.reply_duplicate = False
        self.reply_prior_chatter = False
        self.blocked_peer = ""
        self.missing_peer = ""
        self.reuse_tab = False
        self.session_collision = False
        self.delete_once = False
        self._defaults = {
            key: getattr(self, key)
            for key in (
                "fail",
                "slow",
                "slow_seconds",
                "malformed_workspaces",
                "bad_json",
                "preflight_fail",
                "id_collision",
                "duplicate_action",
                "focus_steal",
                "focus_steal_during_reply",
                "system_error",
                "reply_mode",
                "reply_drop",
                "reply_extra",
                "reply_duplicate",
                "reply_prior_chatter",
                "blocked_peer",
                "missing_peer",
                "reuse_tab",
                "session_collision",
                "delete_once",
            )
        }

    # -- plumbing ---------------------------------------------------------
    def _ok(self, stdout: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    def _err(self, stderr: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, stdout="", stderr=stderr)

    def _envelope(self, payload: dict[str, Any]) -> subprocess.CompletedProcess[str]:
        return self._ok(json.dumps({"id": "cli:fake", "result": payload}))

    def stage_of(self, room: dict[str, Any]) -> str:
        elapsed = self.clock.monotonic() - room["created"]
        if elapsed >= READY_AT:
            stage = "ready"
        elif elapsed >= AGENTS_AT:
            stage = "agents"
        elif elapsed >= WORKSPACES_AT:
            stage = "workspaces"
        else:
            stage = "invoked"
        if stage == "ready" and not room.get("ready_done"):
            room["ready_done"] = True
            room["text"] += (
                f"room {room['no']} ready: " + " ".join(f"@{role}" for role in room["roles"]) + "\n"
            )
            if self.reply_prior_chatter:
                roles = room["roles"]
                room["text"] += (
                    f"\n{roles[0]}> old business from an earlier round\n"
                    "because it looked similar\n"
                    "human> an old question\n"
                    f"{roles[-1]}> SMOKE-OK\n"
                )
        return stage

    def _deliver(self, room: dict[str, Any]) -> None:
        entered = room.get("entered")
        if entered is None or room.get("delivered"):
            return
        if self.clock.monotonic() < entered + REPLIES_AT:
            return
        room["delivered"] = True
        lines: list[str] = []
        for role in room["roles"]:
            if role in self.reply_drop:
                continue
            if self.reply_mode == "prefix":
                body = "ok: SMOKE-OK"
            elif self.reply_mode == "suffix":
                body = f"SMOKE-OK from {role}"
            elif self.reply_mode == "text":
                body = f"the smoke passed for {role}"
            elif self.reply_mode == "explain":
                lines.append(f"{role}> SMOKE-OK")
                lines.append("because the smoke passed overall")
                continue
            else:
                body = "SMOKE-OK"
            lines.append(f"{role}> {body}")
            if self.reply_duplicate and role == room["roles"][0]:
                lines.append(f"{role}> {body}")
        if self.reply_extra:
            lines.append(f"{self.reply_extra}> SMOKE-OK")
        room["text"] += "\n".join(lines) + "\n"

    def _room_by_pane(self, pane_id: str) -> dict[str, Any] | None:
        return next((room for room in self.rooms if room["pane"] == pane_id), None)

    def spawn(self, argv: list[str]) -> FakePopen:
        """Popen for `herdr --session <s> server`."""
        session = argv[argv.index("--session") + 1]
        self.log.append(list(argv[1:]))
        if self.fail == "server":
            return FakePopen(self, session, 1)
        self.live_sessions.add(session)
        return FakePopen(self, session, None)

    def call(
        self, argv: list[str], timeout: float | None = None
    ) -> subprocess.CompletedProcess[Any]:
        self.log.append(list(argv[1:]))
        args = list(argv[1:])
        if args[:1] == ["--session"]:
            args = args[2:]
        cmd = args[0] if args else ""

        if self.fail and " ".join(args[: len(self.fail.split())]) == self.fail:
            return self._err("fake herdr injected failure")

        # A real invocation pays subprocess latency plus any slow-command
        # delay; both count against the caller's budget. When the budget is
        # exhausted, only the remaining budget is spent before timing out.
        cost = COMMAND_LATENCY
        for pattern in self.slow:
            if " ".join(args[: len(pattern.split())]) == pattern:
                cost += self.slow_seconds
                break
        if (
            self.preflight_fail == "timeout"
            and cmd == "plugin"
            and args[1:2] == ["list"]
            and "--plugin" in args
            and not (self.registry / args[args.index("--plugin") + 1]).exists()
        ):
            cost += 30.0
        if timeout is not None and cost > timeout:
            self.clock.sleep(timeout)
            raise subprocess.TimeoutExpired(argv, timeout)
        self.clock.sleep(cost)

        if cmd == "workspace":
            return self._workspace(args)
        if cmd == "agent":
            return self._agent(args)
        if cmd == "pane":
            return self._pane(args)
        if cmd == "plugin":
            return self._plugin(args)
        if cmd == "session":
            return self._session(args, argv)
        return self._err(f"unknown command {cmd}")

    # -- command groups ----------------------------------------------------
    def _workspace(self, args: list[str]) -> subprocess.CompletedProcess[Any]:
        sub = args[1]
        if sub == "list":
            if self.malformed_workspaces:
                return self._envelope({"workspaces": ["bogus"]})
            focused = self.state.get("focus", "caller")
            workspaces = [
                {
                    "workspace_id": "w-caller",
                    "label": "smoke caller",
                    "focused": focused == "caller",
                    "active_tab_id": "w-caller:t1",
                }
            ]
            latest = self.rooms[-1] if self.rooms else None
            if latest and self.stage_of(latest) != "invoked":
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
            return self._envelope({"workspaces": workspaces})
        if sub == "create":
            self.state.setdefault("focus", "caller")
            return self._envelope(
                {"workspace": {"workspace_id": "w-caller", "active_tab_id": "w-caller:t1"}}
            )
        return self._err(f"unknown workspace subcommand {sub}")

    def _agent(self, args: list[str]) -> subprocess.CompletedProcess[Any]:
        if args[1] != "list":
            return self._err(f"unknown agent subcommand {args[1]}")
        agents: list[dict[str, Any]] = []
        seen: set[str] = set()
        # Peers are unique live agents: the most recent room owns the mapping
        # and stale peers keep their earlier backstage workspace.
        for room in reversed(self.rooms):
            if self.stage_of(room) in ("invoked", "workspaces"):
                continue
            for peer in room["peers"]:
                if peer["name"] in seen:
                    continue
                seen.add(peer["name"])
                settled = self.stage_of(room) == "ready"
                agents.append(
                    {
                        "name": peer["name"],
                        "kind": "peer",
                        "workspace_id": room["agents_ws"],
                        "cwd": "/agent",
                        "agent_status": peer.get("status", "idle") if settled else "working",
                    }
                )
        agents.reverse()
        return self._envelope({"agents": agents})

    def _pane(self, args: list[str]) -> subprocess.CompletedProcess[Any]:
        sub = args[1]
        if sub == "list":
            workspace_id = args[args.index("--workspace") + 1]
            panes = [
                {
                    "pane_id": room["pane"],
                    "tab_id": room["tab"],
                    "label": "New group chat",
                    "workspace_id": room["chat_ws"],
                }
                for room in self.rooms
                if self.stage_of(room) == "ready" and room["chat_ws"] == workspace_id
            ]
            return self._envelope({"panes": panes})
        if sub == "read":
            room = self._room_by_pane(args[2])
            if room is None:
                return self._err(f"unknown pane {args[2]}")
            self.stage_of(room)
            self._deliver(room)
            return self._ok(room.get("text", ""))
        if sub == "send-text":
            room = self._room_by_pane(args[2])
            if room is None:
                return self._err(f"unknown pane {args[2]}")
            room["pending_text"] = args[3]
            return self._ok()
        if sub == "send-keys":
            room = self._room_by_pane(args[2])
            if room is None:
                return self._err(f"unknown pane {args[2]}")
            if "enter" in args[3:] and room.get("pending_text"):
                message = room.pop("pending_text")
                room.setdefault("text", "")
                room["text"] += f"human> {message}\n"
                room["entered"] = self.clock.monotonic()
                if self.focus_steal_during_reply:
                    self.state["focus"] = "group"
                if self.system_error == "indent":
                    room["text"] += "  system> delivery failed for this round\n"
                elif self.system_error == "status":
                    room["text"] += "Delivery failed: @grok\n"
                elif self.system_error:
                    room["text"] += "system: delivery failed for this round\n"
            return self._ok()
        return self._err(f"unknown pane subcommand {sub}")

    def _plugin(self, args: list[str]) -> subprocess.CompletedProcess[Any]:
        import tomllib

        sub = args[1]
        if sub == "link":
            root = Path(args[2])
            manifest = tomllib.loads((root / "herdr-plugin.toml").read_text())
            target = self.registry / manifest["id"]
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(root, target)
            (target / ".link-source").write_text(str(root))
            # Snapshot the linked candidate at link time, before cleanup can
            # delete the export, so staged-export evidence survives the run.
            self.linked.append(
                {
                    "plugin_id": manifest["id"],
                    "source": str(root),
                    "files": sorted(
                        str(item.relative_to(target))
                        for item in target.rglob("*")
                        if item.is_file() and item.name != ".link-source"
                    ),
                }
            )
            return self._ok()
        if sub == "unlink":
            plugin_id = args[2]
            target = self.registry / plugin_id
            if not target.exists():
                return self._err(f"unknown plugin {plugin_id}")
            shutil.rmtree(target)
            return self._ok()
        if sub == "list":
            if self.bad_json:
                return self._ok("this is not json")
            plugin_id = args[args.index("--plugin") + 1]
            target = self.registry / plugin_id
            if not target.exists():
                if self.preflight_fail == "json":
                    return self._ok("definitely not json")
                if self.preflight_fail == "transport":
                    return self._err("connection refused")
                if self.id_collision:
                    return self._envelope(
                        {
                            "plugins": [
                                {
                                    "plugin_id": plugin_id,
                                    "version": "0.0.1",
                                    "actions": [],
                                    "panes": [],
                                    "plugin_root": str(self.registry / "other-root"),
                                }
                            ]
                        }
                    )
                return self._envelope({"plugins": []})
            manifest = tomllib.loads((target / "herdr-plugin.toml").read_text())
            link_source = target / ".link-source"
            plugin_root = link_source.read_text() if link_source.exists() else str(target)
            actions = [
                {"id": a["id"], "command": a["command"], "contexts": a["contexts"]}
                for a in manifest["actions"]
            ]
            if self.duplicate_action and actions:
                actions.append(dict(actions[0]))
            return self._envelope(
                {
                    "plugins": [
                        {
                            "plugin_id": plugin_id,
                            "version": manifest["version"],
                            "actions": actions,
                            "panes": [
                                {
                                    "id": p["id"],
                                    "command": p["command"],
                                    "placement": p["placement"],
                                }
                                for p in manifest["panes"]
                            ],
                            "plugin_root": plugin_root,
                        }
                    ]
                }
            )
        if sub == "action":
            action = args[3]
            self.state["rooms"] += 1
            room_no = self.state["rooms"]
            if self.focus_steal == action:
                self.state["focus"] = "group"
            roles = list(ROOM_ROLES[action])
            peers = []
            for peer in ROOM_PEERS[action]:
                status = "idle"
                if self.blocked_peer == peer:
                    status = "blocked"
                if self.missing_peer == peer:
                    continue
                peers.append({"name": peer, "status": status})
            tab = f"tab-room-{room_no}"
            if self.reuse_tab and room_no > 1:
                tab = f"tab-room-{room_no - 1}"
            self.rooms.append(
                {
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
                    "created": self.clock.monotonic(),
                }
            )
            return self._ok()
        return self._err(f"unknown plugin subcommand {sub}")

    def _session(self, args: list[str], argv: list[str]) -> subprocess.CompletedProcess[Any]:
        sub = args[1]
        session = argv[2] if len(argv) > 2 and argv[1] == "--session" else None
        if sub == "list":
            sessions = [{"name": "default", "running": True, "socket_path": "/fake/herdr.sock"}]
            if session and (session in self.live_sessions or self.session_collision):
                sessions.append(
                    {"name": session, "running": True, "socket_path": "/fake/herdr.sock"}
                )
            return self._ok(json.dumps({"sessions": sessions}))
        if sub == "stop":
            self.live_sessions.discard(args[2])
            self.state["stopped"].append(args[2])
            return self._ok()
        if sub == "delete":
            attempts = self.state.setdefault("delete_attempts", {})
            attempts[args[2]] = attempts.get(args[2], 0) + 1
            if self.delete_once and attempts[args[2]] == 1:
                return self._err("transient delete failure")
            if args[2] in self.live_sessions:
                return self._err("refusing to delete a running session")
            self.state["deleted"].append(args[2])
            return self._ok(json.dumps({"deleted": True}))
        return self._err(f"unknown session subcommand {sub}")


class FakeRuntime:
    """Runtime whose Herdr commands hit the fake and Git commands hit reality."""

    def __init__(self, fake: FakeHerdr, clock: VirtualClock, git_bin: str) -> None:
        self.fake = fake
        self.clock = clock
        self.git_bin = git_bin
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if argv and argv[0] == self.git_bin:
            return subprocess.run(argv, **kwargs)
        self.calls.append(list(argv))
        return self.fake.call(list(argv), kwargs.get("timeout"))

    def popen(self, argv: list[str], **kwargs: Any) -> FakePopen:
        self.calls.append(list(argv))
        return self.fake.spawn(list(argv))

    def monotonic(self) -> float:
        return self.clock.monotonic()

    def sleep(self, seconds: float) -> None:
        self.clock.sleep(seconds)


# Old environment-variable injection names, mapped to fake flags one for one.
ENV_TO_FLAG = {
    "FAKE_HERDR_FAIL": "fail",
    "FAKE_HERDR_SLOW": "slow",
    "FAKE_HERDR_SLOW_SECONDS": "slow_seconds",
    "FAKE_HERDR_RUNTIME_MALFORMED": "malformed_workspaces",
    "FAKE_HERDR_BAD_JSON": "bad_json",
    "FAKE_HERDR_PREFLIGHT_FAIL": "preflight_fail",
    "FAKE_HERDR_ID_COLLISION": "id_collision",
    "FAKE_HERDR_DUPLICATE_ACTION": "duplicate_action",
    "FAKE_HERDR_FOCUS_STEAL": "focus_steal",
    "FAKE_HERDR_FOCUS_STEAL_DURING_REPLY": "focus_steal_during_reply",
    "FAKE_SYSTEM_ERROR": "system_error",
    "FAKE_REPLY_MODE": "reply_mode",
    "FAKE_REPLY_DROP": "reply_drop",
    "FAKE_REPLY_EXTRA": "reply_extra",
    "FAKE_REPLY_DUPLICATE": "reply_duplicate",
    "FAKE_REPLY_PRIOR_CHATTER": "reply_prior_chatter",
    "FAKE_HERDR_BLOCKED_PEER": "blocked_peer",
    "FAKE_HERDR_MISSING_PEER": "missing_peer",
    "FAKE_HERDR_REUSE_TAB": "reuse_tab",
    "FAKE_HERDR_SESSION_COLLISION": "session_collision",
    "FAKE_HERDR_DELETE_ONCE": "delete_once",
}


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


@dataclass
class RunResult:
    """One in-process harness run: the parsed stable JSON plus captured stderr."""

    payload: dict[str, Any]
    stderr: str
    exit_code: int

    @property
    def returncode(self) -> int:
        return self.exit_code


class Harness:
    """A fully faked, offline environment for running release-smoke in-process."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.home = tmp_path / "fake-herdr"
        self.registry = self.home / "registry"
        self.agent_cwd = tmp_path / "agent-cwd"
        self.agent_cwd.mkdir()
        self.log: list[list[str]] = []
        self.clock = VirtualClock()
        self.fake = FakeHerdr(self.clock, self.registry, self.log)
        git_bin = shutil.which("git")
        assert git_bin is not None, "git is required for these assays"
        self.git_bin = git_bin
        self.runtime = FakeRuntime(self.fake, self.clock, git_bin)
        monkeypatch.delenv("HERDR_ENV", raising=False)

    def inject(self, env_extra: dict[str, str] | None) -> None:
        # Injection flags last for exactly one run, like the old per-run env.
        for key, value in self.fake._defaults.items():
            setattr(self.fake, key, value)
        for key, value in (env_extra or {}).items():
            if key == "TMPDIR":
                continue  # applied and restored by run_smoke itself
            flag = ENV_TO_FLAG[key]
            if flag == "slow":
                self.fake.slow = [p.strip() for p in value.split(";") if p.strip()]
            elif flag == "slow_seconds":
                self.fake.slow_seconds = float(value)
            elif flag == "reply_drop":
                self.fake.reply_drop = {v for v in value.split(",") if v}
            else:
                setattr(self.fake, flag, value)

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

    def run_smoke(self, arguments: list[Any], env_extra: dict[str, str] | None = None) -> RunResult:
        """Run module.smoke in-process with the fake runtime injected.

        An injected TMPDIR applies only inside this run; the inherited
        environment value and the inherited tempfile.tempdir cache are
        restored exactly afterward, even when the run fails.
        """
        inherited_env = os.environ.get("TMPDIR")
        inherited_cache = tempfile.tempdir
        injected = (env_extra or {}).get("TMPDIR")
        buffer = io.StringIO()
        try:
            if injected is not None:
                os.environ["TMPDIR"] = injected
                # In-process tempfile caches its root; reset it so the run's
                # internal candidate copy genuinely lands under the new root.
                tempfile.tempdir = injected
            self.inject(env_extra)
            with redirect_stderr(buffer):
                result = module.smoke(*arguments, runtime=self.runtime)
            return RunResult(
                payload=json.loads(result.to_json()),
                stderr=buffer.getvalue(),
                exit_code=0 if result.ok else 1,
            )
        finally:
            if inherited_env is None:
                os.environ.pop("TMPDIR", None)
            else:
                os.environ["TMPDIR"] = inherited_env
            tempfile.tempdir = inherited_cache

    def run_candidate(
        self, env_extra: dict[str, str] | None = None, timeout: str = "30"
    ) -> RunResult:
        return self.run_smoke(
            ["candidate", "herdr", self.agent_cwd, float(timeout), self.candidate_repo()],
            env_extra=env_extra,
        )

    def run_installed(
        self, version: str = "0.10.5", env_extra: dict[str, str] | None = None
    ) -> RunResult:
        return self.run_smoke(
            [
                "installed",
                "herdr",
                self.agent_cwd,
                30.0,
                None,
                "terry.herdr-group-chat",
                version,
            ],
            env_extra=env_extra,
        )

    def run_main(self, argv: list[str]) -> RunResult:
        """Run module.main in-process, capturing the stable JSON from stdout."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = module.main(argv)
        try:
            payload = json.loads(out.getvalue())
        except json.JSONDecodeError:
            payload = {"ok": False, "error": {"stage": "internal", "message": out.getvalue()}}
        return RunResult(payload=payload, stderr=err.getvalue(), exit_code=code)

    def commands(self) -> list[list[str]]:
        return list(self.log)

    def rooms(self) -> list[dict[str, Any]]:
        return self.fake.rooms

    def linked_snapshots(self) -> list[dict[str, Any]]:
        return list(self.fake.linked)

    def state(self) -> dict[str, Any]:
        return self.fake.state


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    made = Harness(tmp_path, monkeypatch)
    made.install_plugin()
    made.candidate_repo()
    yield made


def assert_candidate_ok(result: RunResult) -> dict[str, Any]:
    assert result.returncode == 0, result.stderr
    payload = result.payload
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
    (root / "tracked-room-marker").write_text("tracked\n")
    git(root, "add", "tracked-room-marker")
    junk = root / "junk"
    junk.mkdir()
    (junk / "blob.bin").write_bytes(b"x" * 1024)
    assert_candidate_ok(harness.run_candidate())
    snapshots = harness.linked_snapshots()
    assert len(snapshots) == 1
    files = snapshots[0]["files"]
    # Only the staged Git index was exported and linked: tracked files are
    # present, unstaged worktree junk never reaches the linked candidate.
    assert "herdr-plugin.toml" in files
    assert "tracked-room-marker" in files
    assert "junk/blob.bin" not in files
    assert not [name for name in files if name.startswith("junk/")]


def test_export_rejects_escaping_and_absolute_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Black-box: the staged Git export boundary is proven through the real
    # executable; the run fails inside the export before any Herdr call.
    agent_cwd = tmp_path / "agent-cwd"
    agent_cwd.mkdir()
    repo = make_candidate_repo(tmp_path / "candidate")
    monkeypatch.delenv("HERDR_ENV", raising=False)

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(EFFECTOR),
                "candidate",
                "--plugin-root",
                str(repo),
                "--agent-cwd",
                str(agent_cwd),
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
            check=False,
        )

    os.symlink("../../outside", repo / "escape")
    git(repo, "add", "-A")
    result = run()
    failure = json.loads(result.stdout)
    assert result.returncode == 1
    assert failure["error"]["stage"] == "export-candidate"
    assert "escaping symlink" in failure["error"]["message"]

    escape = repo / "escape"
    escape.unlink()
    os.symlink("/etc/passwd", escape)
    git(repo, "add", "-A")
    result = run()
    failure = json.loads(result.stdout)
    assert result.returncode == 1
    assert failure["error"]["stage"] == "export-candidate"
    assert "absolute symlink" in failure["error"]["message"]


def test_tmpdir_nested_under_plugin_root_does_not_recurse(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Seed distinct inherited values for both the environment and the cached
    # tempfile root so exact restoration is observable.
    inherited_env = str(tmp_path / "inherited-env-tmp")
    monkeypatch.setenv("TMPDIR", inherited_env)
    inherited_cache = str(tmp_path / "inherited-cache-tmp")
    monkeypatch.setattr(tempfile, "tempdir", inherited_cache)
    root = harness.candidate_repo()
    nested = root / "tmp"
    nested.mkdir(exist_ok=True)
    result = harness.run_candidate(env_extra={"TMPDIR": str(nested)}, timeout="60")
    assert result.returncode == 0, result.stderr
    assert result.payload["ok"] is True
    # The link command saw the internal candidate copy inside the nested
    # TMPDIR while it was active, proving the export really landed there.
    links = [c for c in harness.commands() if c[2:4] == ["plugin", "link"]]
    assert links and Path(links[0][4]).is_relative_to(nested)
    # Owned cleanup removed the internal copy, and nothing leaks afterward.
    assert not list(nested.glob("herdr-group-chat-smoke-*"))
    # Both exact inherited values are restored after the run.
    assert os.environ["TMPDIR"] == inherited_env
    assert tempfile.tempdir == inherited_cache


def test_exact_action_and_pane_validation(harness: Harness) -> None:
    payload = assert_candidate_ok(harness.run_candidate())
    assert payload["observed_version"] == "0.10.5"

    result = harness.run_installed(version="0.9.9")
    assert result.returncode == 1
    failure = result.payload
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
    failure = result.payload
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
    failure = result.payload
    assert result.returncode == 1
    assert failure["error"]["stage"] == "verify-registration"
    assert "placement" in failure["error"]["message"]


def test_duplicate_action_ids_fail_registration(harness: Harness) -> None:
    result = harness.run_candidate(env_extra={"FAKE_HERDR_DUPLICATE_ACTION": "1"})
    failure = result.payload
    assert result.returncode == 1
    assert failure["error"]["stage"] == "verify-registration"
    assert "duplicate action ids" in failure["error"]["message"]
    assert "expected exactly" in failure["error"]["message"]


def test_temporary_id_collision_is_rejected_before_ownership(harness: Harness) -> None:
    result = harness.run_candidate(env_extra={"FAKE_HERDR_ID_COLLISION": "1"})
    failure = result.payload
    assert result.returncode == 1
    assert failure["error"]["stage"] == "link-candidate"
    assert "already registered" in failure["error"]["message"]
    assert not [c for c in harness.commands() if c[2:4] == ["plugin", "link"]]
    assert "plugin_unlink" not in failure["cleanup"]


def test_session_name_collision_is_rejected_before_spawn(harness: Harness) -> None:
    result = harness.run_candidate(env_extra={"FAKE_HERDR_SESSION_COLLISION": "1"})
    failure = result.payload
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


def test_fake_call_enforces_timeout_against_command_latency(tmp_path: Path) -> None:
    clock = VirtualClock()
    fake = FakeHerdr(clock, tmp_path / "registry", [])
    before = clock.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        fake.call(["herdr", "session", "list", "--json"], timeout=0.1)
    # Only the remaining budget is spent before the timeout fires.
    assert clock.monotonic() - before == pytest.approx(0.1)


def test_fake_call_charges_latency_and_slow_cost_within_budget(tmp_path: Path) -> None:
    clock = VirtualClock()
    fake = FakeHerdr(clock, tmp_path / "registry", [])
    fake.slow = ["agent list"]
    fake.slow_seconds = 1.0
    before = clock.monotonic()
    result = fake.call(["herdr", "--session", "s", "agent", "list"], timeout=2.0)
    assert result.returncode == 0
    assert json.loads(result.stdout)["result"] == {"agents": []}
    # Both the command latency and the slow-command delay are charged.
    assert clock.monotonic() - before == pytest.approx(COMMAND_LATENCY + 1.0)

    # The same combined cost against a budget of exactly that size fits.
    before = clock.monotonic()
    result = fake.call(["herdr", "--session", "s", "agent", "list"], timeout=COMMAND_LATENCY + 1.0)
    assert result.returncode == 0
    # One budget-second short, it times out after spending the budget.
    before = clock.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        fake.call(
            ["herdr", "--session", "s", "agent", "list"],
            timeout=COMMAND_LATENCY + 1.0 - 0.001,
        )
    assert clock.monotonic() - before == pytest.approx(COMMAND_LATENCY + 1.0 - 0.001)


def test_readiness_polls_live_surfaces_until_the_room_is_ready(harness: Harness) -> None:
    started = harness.clock.monotonic()
    payload = assert_candidate_ok(harness.run_candidate())
    # Workspaces land at 0.15s, peers at 0.3s, the room pane at 0.5s per room,
    # so a passing run proves the harness polled every surface instead of
    # reading partial state; two rooms cost at least a virtual second.
    assert harness.clock.monotonic() - started >= 1.0
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
        failure = result.payload
        assert failure["error"]["stage"] == expected_stage, env_extra
        # The bounded round fails either at the explicit budget guard or at a
        # command whose combined latency exceeds its remaining budget share.
        message = failure["error"]["message"]
        assert "never became ready" in message or "timed out" in message, env_extra
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
    failure = result.payload
    assert result.returncode == 1
    assert failure["error"]["stage"] == "round-new"
    assert (
        "not the caller" in failure["error"]["message"]
        or "took focus" in failure["error"]["message"]
    )


def test_visible_system_delivery_error_fails_immediately(harness: Harness) -> None:
    result = harness.run_candidate(env_extra={"FAKE_SYSTEM_ERROR": "1"})
    failure = result.payload
    assert result.returncode == 1
    assert failure["error"]["stage"] == "round-new"
    assert "system error" in failure["error"]["message"]


def test_reply_validation_fails_closed(harness: Harness) -> None:
    missing = harness.run_candidate(env_extra={"FAKE_REPLY_DROP": "grok"})
    failure = missing.payload
    assert missing.returncode == 1
    assert failure["error"]["stage"] == "round-new"
    assert "missing exact replies" in failure["error"]["message"]
    assert "grok" in failure["error"]["message"]

    extra = harness.run_candidate(env_extra={"FAKE_REPLY_EXTRA": "intruder"})
    failure = extra.payload
    assert extra.returncode == 1
    assert "unexpected participant reply" in failure["error"]["message"]

    for mode in ("prefix", "suffix", "text"):
        result = harness.run_candidate(env_extra={"FAKE_REPLY_MODE": mode})
        failure = result.payload
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
    payload = result.payload
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
    failure = result.payload
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
    failure = result.payload
    assert result.returncode == 1
    assert failure["error"]["stage"] == "start-session"
    assert failure["cleanup"]["session_stop"] == "ok"
    assert failure["cleanup"]["session_delete"] == "ok"
    assert "plugin_unlink" not in failure["cleanup"]


def test_ambiguous_link_failure_still_attempts_exact_temp_id_unlink(harness: Harness) -> None:
    result = harness.run_candidate(env_extra={"FAKE_HERDR_FAIL": "plugin link"})
    failure = result.payload
    assert result.returncode == 1
    assert failure["error"]["stage"] == "link-candidate"
    unlinks = [c for c in harness.commands() if c[2:4] == ["plugin", "unlink"]]
    assert len(unlinks) == 1
    assert unlinks[0][4] == failure["temporary_plugin_id"]
    assert failure["cleanup"]["plugin_unlink"].startswith("failed:")


def test_cleanup_failure_turns_success_into_named_failure(harness: Harness) -> None:
    result = harness.run_candidate(env_extra={"FAKE_HERDR_FAIL": "plugin unlink"})
    failure = result.payload
    assert result.returncode == 1
    assert failure["rooms"] and failure["stages_completed"][-1] == "round-new-classic"
    assert failure["error"]["stage"] == "cleanup-link"
    assert failure["cleanup"]["plugin_unlink"].startswith("failed:")
    assert failure["cleanup"]["session_stop"] == "ok"
    assert failure["cleanup"]["session_delete"] == "ok"

    stop = harness.run_candidate(env_extra={"FAKE_HERDR_FAIL": "session stop"})
    failure = stop.payload
    session = failure["session"]
    assert stop.returncode == 1
    assert failure["error"]["stage"] == "cleanup-session-stop"
    assert failure["cleanup"]["session_stop"].startswith("failed:")
    # The owned server is reaped before deletion, so one delete suffices.
    session_deletes = [c for c in harness.commands() if c[2:5] == ["session", "delete", session]]
    assert session_deletes

    retry = harness.run_candidate(env_extra={"FAKE_HERDR_DELETE_ONCE": "1"})
    assert retry.returncode == 0, retry.stderr
    retry_payload = retry.payload
    assert retry_payload["cleanup"]["session_delete"] == "ok (after retry)"
    retry_deletes = [
        c for c in harness.commands() if c[2:5] == ["session", "delete", retry_payload["session"]]
    ]
    assert len(retry_deletes) == 2

    delete = harness.run_candidate(env_extra={"FAKE_HERDR_FAIL": "session delete"})
    failure = delete.payload
    assert delete.returncode == 1
    assert failure["error"]["stage"] == "cleanup-session-delete"


def test_stable_json_and_exit_behavior(harness: Harness) -> None:
    result = harness.run_candidate()
    assert result.returncode == 0
    payload = result.payload
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
    assert other.payload["session"] != payload["session"]


def test_invalid_json_output_preserves_the_requested_stage(harness: Harness) -> None:
    result = harness.run_installed(env_extra={"FAKE_HERDR_BAD_JSON": "1"})
    failure = result.payload
    assert result.returncode == 1
    assert failure["error"]["stage"] == "verify-registration"
    assert "invalid JSON" in failure["error"]["message"]


def test_invalid_paths_emit_stable_json(harness: Harness) -> None:
    result = harness.run_main(
        [
            "candidate",
            "--plugin-root",
            str(harness.home / "nonexistent"),
            "--agent-cwd",
            str(harness.agent_cwd),
        ]
    )
    failure = result.payload
    assert result.returncode == 1
    assert failure["ok"] is False
    assert failure["error"]["stage"] == "usage"
    assert "Git checkout" in failure["error"]["message"]

    bad_cwd = harness.run_main(
        [
            "candidate",
            "--plugin-root",
            str(harness.candidate_repo()),
            "--agent-cwd",
            str(harness.home / "missing-cwd"),
        ]
    )
    failure = bad_cwd.payload
    assert bad_cwd.returncode == 1
    assert failure["error"]["stage"] == "usage"
    assert "agent-cwd" in failure["error"]["message"]


def test_malformed_invocations_emit_stable_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Black-box: argparse and the stable JSON contract are proven through the
    # real executable; every case fails in argument handling before any Herdr
    # subprocess could run.
    agent_cwd = tmp_path / "agent-cwd"
    agent_cwd.mkdir()
    repo = make_candidate_repo(tmp_path / "candidate")
    monkeypatch.delenv("HERDR_ENV", raising=False)
    cases = [
        ("candidate", "--agent-cwd", str(agent_cwd)),
        ("candidate", "--plugin-root", str(repo)),
        (
            "candidate",
            "--plugin-root",
            str(repo),
            "--agent-cwd",
            str(agent_cwd),
            "--timeout",
            "not-a-number",
        ),
        (
            "installed",
            "--plugin-id",
            "terry.herdr-group-chat",
            "--agent-cwd",
            str(agent_cwd),
        ),
        ("bogus-subcommand",),
    ]
    for case in cases:
        result = subprocess.run(
            [sys.executable, str(EFFECTOR), *case],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
            check=False,
        )
        assert result.returncode == 1, case
        failure = json.loads(result.stdout)
        assert failure["ok"] is False, case
        assert failure["error"]["stage"] == "usage", case
        assert failure["error"]["message"], case


def test_malformed_runtime_reports_the_internal_stage(harness: Harness) -> None:
    result = harness.run_candidate(env_extra={"FAKE_HERDR_RUNTIME_MALFORMED": "1"})
    failure = result.payload
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
        failure = result.payload
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
    failure = result.payload
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
        failure = result.payload
        assert result.returncode == 1, mode
        assert failure["error"]["stage"] == "round-new", mode
        assert fragment in failure["error"]["message"], mode


def test_tab_reuse_blocks_replacement_readiness(harness: Harness) -> None:
    result = harness.run_candidate(env_extra={"FAKE_HERDR_REUSE_TAB": "1"}, timeout="3")
    failure = result.payload
    assert result.returncode == 1
    assert failure["error"]["stage"] == "round-new-classic"
    assert "never became ready" in failure["error"]["message"]
    # The default round completed before the replacement stalled.
    assert failure["rooms"]


def test_timeout_is_a_wall_clock_budget(harness: Harness) -> None:
    started = harness.clock.monotonic()
    result = harness.run_candidate(env_extra={"FAKE_HERDR_SLOW": "agent list"}, timeout="2")
    virtual_elapsed = harness.clock.monotonic() - started
    failure = result.payload
    assert result.returncode == 1
    assert failure["error"]["stage"] == "round-new"
    assert "timed out" in failure["error"]["message"]
    # The whole 2-second round budget was consumed, bounded by the fixed
    # startup and cleanup command latency on either side of it.
    assert virtual_elapsed >= 2.0
    assert virtual_elapsed < 2.0 + 16 * COMMAND_LATENCY


def test_transient_focus_theft_during_reply_polling_fails(harness: Harness) -> None:
    result = harness.run_candidate(env_extra={"FAKE_HERDR_FOCUS_STEAL_DURING_REPLY": "1"})
    failure = result.payload
    assert result.returncode == 1
    assert failure["error"]["stage"] == "round-new"
    message = failure["error"]["message"]
    assert "not the caller" in message or "took focus" in message


def test_invalid_timeouts_are_rejected_in_stable_json(harness: Harness) -> None:
    for value in ("inf", "nan", "0", "-5"):
        result = harness.run_main(
            [
                "candidate",
                "--plugin-root",
                str(harness.candidate_repo()),
                "--agent-cwd",
                str(harness.agent_cwd),
                "--timeout",
                value,
            ]
        )
        assert result.returncode == 1, value
        failure = result.payload
        assert failure["ok"] is False, value
        assert failure["error"]["stage"] == "usage", value
        assert "timeout" in failure["error"]["message"], value


def test_symlink_loop_reports_the_internal_stage(harness: Harness, tmp_path: Path) -> None:
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    result = harness.run_main(
        [
            "candidate",
            "--plugin-root",
            str(harness.candidate_repo()),
            "--agent-cwd",
            str(loop),
        ]
    )
    failure = result.payload
    assert result.returncode == 1
    assert failure["ok"] is False
    assert failure["error"]["stage"] == "internal"


def test_budget_is_recomputed_before_each_sequential_command(harness: Harness) -> None:
    # Two different sequential commands each delay 1.2s against a 2s budget:
    # reusing one stale budget would let the second run to 2.4s and beyond.
    started = harness.clock.monotonic()
    result = harness.run_candidate(
        env_extra={
            "FAKE_HERDR_SLOW": "workspace list;agent list",
            "FAKE_HERDR_SLOW_SECONDS": "1.2",
        },
        timeout="2",
    )
    virtual_elapsed = harness.clock.monotonic() - started
    failure = result.payload
    assert result.returncode == 1
    assert failure["error"]["stage"] == "round-new"
    assert "timed out" in failure["error"]["message"]
    # The slowed startup commands are charged in full, the round budget is
    # enforced, and a stale reused budget - letting the second slow command
    # run to completion - would add its full 1.36s past this bound.
    assert virtual_elapsed >= 2.0
    assert virtual_elapsed < 7.0


def test_prior_role_chatter_does_not_poison_a_valid_reply(harness: Harness) -> None:
    payload = assert_candidate_ok(
        harness.run_candidate(env_extra={"FAKE_REPLY_PRIOR_CHATTER": "1"})
    )
    for room in payload["rooms"]:
        assert room["replies"] == {name: "SMOKE-OK" for name in room["participants"]}


def test_duplicate_post_marker_reply_fails(harness: Harness) -> None:
    result = harness.run_candidate(env_extra={"FAKE_REPLY_DUPLICATE": "1"})
    failure = result.payload
    assert result.returncode == 1
    assert failure["error"]["stage"] == "round-new"
    assert "replied 2 times" in failure["error"]["message"]


def test_production_runtime_is_the_default() -> None:
    assert module.REAL_RUNTIME.run is subprocess.run
    assert module.REAL_RUNTIME.popen is subprocess.Popen
    assert module.REAL_RUNTIME.monotonic is time.monotonic
    assert module.REAL_RUNTIME.sleep is time.sleep
    signatures = (
        inspect.signature(module.Herdr.__init__).parameters["runtime"].default,
        inspect.signature(module.smoke).parameters["runtime"].default,
        inspect.signature(module._main).parameters["runtime"].default,
        inspect.signature(module._cli).parameters["runtime"].default,
    )
    for default in signatures:
        assert default is module.REAL_RUNTIME
    # The public entrypoint exposes no runtime injection of any kind.
    assert set(inspect.signature(module.main).parameters) == {"argv"}


def test_private_main_seam_runs_end_to_end_on_the_fake_runtime(harness: Harness) -> None:
    # The private serialization seam accepts an injected runtime, so the full
    # CLI path - argparse, stages, cleanup, and the stable JSON print - runs
    # in-process and succeeds on the fake.
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = module._main(
            [
                "candidate",
                "--plugin-root",
                str(harness.candidate_repo()),
                "--agent-cwd",
                str(harness.agent_cwd),
                "--herdr-bin",
                "herdr",
            ],
            runtime=harness.runtime,
        )
    raw = out.getvalue()
    payload = json.loads(raw)
    assert code == 0
    assert payload["ok"] is True
    assert payload["error"] is None
    assert len(payload["rooms"]) == 2
    # Raw stdout is exactly the canonical stable JSON object.
    assert raw.strip() == json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert err.getvalue().strip()


def test_fake_runtime_cannot_leak_into_cli_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Swap the module-level runtime name for a recording fake: the CLI path
    # must still execute real subprocesses, because the production defaults
    # are bound at definition time and never re-read from the module global.
    agent_cwd = tmp_path / "agent-cwd"
    agent_cwd.mkdir()
    repo = make_candidate_repo(tmp_path / "candidate")
    marker = tmp_path / "real-subprocess-ran"
    sentinel = tmp_path / "herdr-sentinel"
    make_executable(
        sentinel,
        '#!/bin/sh\ntouch "$SMOKE_SENTINEL_MARKER"\nexit 0\n',
    )
    monkeypatch.delenv("HERDR_ENV", raising=False)
    monkeypatch.setenv("SMOKE_SENTINEL_MARKER", str(marker))
    recording: list[list[str]] = []

    class RecordingRuntime:
        def run(self, argv, **kwargs):
            recording.append(list(argv))
            raise AssertionError("fake runtime must not execute CLI commands")

        def popen(self, argv, **kwargs):
            recording.append(list(argv))
            raise AssertionError("fake runtime must not spawn CLI processes")

        def monotonic(self):
            return 0.0

        def sleep(self, seconds):
            return None

    monkeypatch.setattr(module, "REAL_RUNTIME", RecordingRuntime())
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = module.main(
            [
                "candidate",
                "--plugin-root",
                str(repo),
                "--agent-cwd",
                str(agent_cwd),
                "--herdr-bin",
                str(sentinel),
            ]
        )
    payload = json.loads(out.getvalue())
    assert marker.is_file(), "the CLI executed a real subprocess, not the fake runtime"
    assert recording == []
    assert code == 1
    assert payload["ok"] is False


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
