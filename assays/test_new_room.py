from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from itertools import pairwise
from pathlib import Path
from threading import Event, Thread

import pytest

EFFECTOR = Path(__file__).resolve().parent.parent / "new-room"
loader = SourceFileLoader("new_room", str(EFFECTOR))
spec = spec_from_loader(loader.name, loader)
assert spec is not None
module = module_from_spec(spec)
sys.modules[module.__name__] = module
loader.exec_module(module)

BootstrapError = module.BootstrapError
ensure_agents_workspace = module.ensure_agents_workspace
ensure_chat_workspace = module.ensure_chat_workspace
launch_room = module.launch_room
launcher_state_lock = module.launcher_state_lock
launcher_state_path = module.launcher_state_path
load_launcher_state = module.load_launcher_state
new_room_id = module.new_room_id
save_launcher_state = module.save_launcher_state


@pytest.fixture(autouse=True)
def herdr_socket_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    socket_path = tmp_path / "herdr.sock"
    socket_path.touch()
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(socket_path))


def _run_launcher_lock(state_dir: Path) -> None:
    with launcher_state_lock(state_dir):
        pass


@pytest.mark.parametrize("failure", ["fstat", "fchmod"])
def test_launcher_directory_closes_descriptor_on_validation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    opened: list[int] = []
    real_open = module.os.open
    real_fstat = module.os.fstat

    def tracking_open(*args: object, **kwargs: object) -> int:
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(module.os, "open", tracking_open)
    if failure == "fstat":
        monkeypatch.setattr(
            module.os, "fstat", lambda _descriptor: (_ for _ in ()).throw(OSError("injected"))
        )
    else:

        def fail_fchmod(_descriptor: int, _mode: int) -> None:
            raise OSError("injected")

        monkeypatch.setattr(module.os, "fchmod", fail_fchmod)

    with pytest.raises(BootstrapError, match="invalid launcher state directory"):
        module._open_launcher_directory(tmp_path / "injected-state")
    assert opened
    for descriptor in opened:
        with pytest.raises(OSError):
            real_fstat(descriptor)


def test_precreated_empty_umask_state_dir_is_tightened_and_launch_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Herdr 0.8.2+ pre-creates the state directory under the process umask.

    An empty owned 0775 directory must be tightened to 0700 on the open
    descriptor, not rejected, so the launch action still runs.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_dir.chmod(0o775)
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(state_dir))

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        if arguments == ["workspace", "list"]:
            return {"result": {"workspaces": []}}
        if arguments[:2] == ["workspace", "create"]:
            return {
                "result": {
                    "workspace": {"workspace_id": "w-chat"},
                    "tab": {"tab_id": "w-chat:t-placeholder"},
                }
            }
        if arguments[:3] == ["plugin", "pane", "open"]:
            return {
                "result": {
                    "plugin_pane": {"pane": {"pane_id": "w-chat:p-new", "tab_id": "w-chat:t-new"}}
                }
            }
        return {"result": {"type": "ok"}}

    monkeypatch.setattr(module, "run_json", fake_run_json)

    assert launch_room(open_existing=False) == 0
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    assert load_launcher_state(state_dir)["room_pane_id"] == "w-chat:p-new"


def test_precreated_nonempty_state_dir_still_fails_closed(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_dir.chmod(0o775)
    (state_dir / "foreign.json").write_text("foreign\n", encoding="utf-8")

    with pytest.raises(BootstrapError, match="invalid launcher state directory authority"):
        load_launcher_state(state_dir)

    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o775


def test_precreated_private_state_dir_is_adopted_without_retightening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_dir.chmod(0o700)
    directory_chmods: list[int] = []
    real_fchmod = module.os.fchmod

    def recording_fchmod(descriptor: int, mode: int) -> None:
        if stat.S_ISDIR(module.os.fstat(descriptor).st_mode):
            directory_chmods.append(mode)
        real_fchmod(descriptor, mode)

    monkeypatch.setattr(module.os, "fchmod", recording_fchmod)

    assert load_launcher_state(state_dir) == {"schema_version": 1}
    assert directory_chmods == []
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700


def test_launcher_state_is_atomic_private_and_versioned(tmp_path: Path) -> None:
    state = {
        "chat_workspace_id": "w-chat",
        "agents_workspace_id": "w-agents",
        "room_pane_id": "w-chat:p1",
        "room_tab_id": "w-chat:t1",
        "last_room_id": "chat-20260815-120000-abcdef",
    }

    save_launcher_state(tmp_path, state)

    path = launcher_state_path(tmp_path)
    assert load_launcher_state(tmp_path) == {**state, "schema_version": 1}
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not any(path.name.endswith(".tmp") for path in tmp_path.iterdir())


def test_invalid_launcher_state_fails_closed(tmp_path: Path) -> None:
    path = launcher_state_path(tmp_path)
    path.write_text('{"schema_version":99}\n', encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(BootstrapError, match="invalid launcher state schema"):
        load_launcher_state(tmp_path)


def test_launcher_state_is_partitioned_by_herdr_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_socket = tmp_path / "first.sock"
    second_socket = tmp_path / "second.sock"
    first_socket.touch()
    second_socket.touch()
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(first_socket))
    save_launcher_state(tmp_path, {"chat_workspace_id": "w-first"})
    first_path = launcher_state_path(tmp_path)

    monkeypatch.setenv("HERDR_SOCKET_PATH", str(second_socket))
    assert load_launcher_state(tmp_path) == {"schema_version": 1}
    save_launcher_state(tmp_path, {"chat_workspace_id": "w-second"})
    second_path = launcher_state_path(tmp_path)

    assert first_path != second_path
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(first_socket))
    assert load_launcher_state(tmp_path)["chat_workspace_id"] == "w-first"


def test_launcher_state_changes_when_a_server_reuses_the_same_socket_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket_path = tmp_path / "reused.sock"
    socket_path.touch()
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(socket_path))
    first_path = launcher_state_path(tmp_path)

    socket_path.unlink()
    socket_path.touch()
    second_path = launcher_state_path(tmp_path)

    assert first_path != second_path


def test_launcher_lock_serializes_competing_transactions(tmp_path: Path) -> None:
    first_entered = Event()
    release_first = Event()
    second_attempting = Event()
    second_entered = Event()

    def first() -> None:
        with launcher_state_lock(tmp_path):
            first_entered.set()
            assert release_first.wait(2)

    def second() -> None:
        assert first_entered.wait(2)
        second_attempting.set()
        with launcher_state_lock(tmp_path):
            second_entered.set()

    first_thread = Thread(target=first)
    second_thread = Thread(target=second)
    first_thread.start()
    second_thread.start()
    assert second_attempting.wait(2)
    assert not second_entered.wait(0.05)
    release_first.set()
    assert second_entered.wait(2)
    first_thread.join(2)
    second_thread.join(2)
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()


def test_launcher_transaction_descriptor_survives_parent_replacement(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "bound-state"
    decoy_dir = tmp_path / "decoy-state"
    save_launcher_state(state_dir, {"chat_workspace_id": "bound-before"})
    state_path = launcher_state_path(state_dir)

    with launcher_state_lock(state_dir) as locked_path:
        assert locked_path == state_path
        state_dir.rename(decoy_dir)
        state_dir.mkdir(mode=0o700)
        decoy_path = state_dir / state_path.name
        decoy_path.write_text(
            '{"schema_version":1,"chat_workspace_id":"decoy"}\n', encoding="utf-8"
        )
        decoy_path.chmod(0o600)

        assert load_launcher_state(state_dir, state_path)["chat_workspace_id"] == "bound-before"
        save_launcher_state(state_dir, {"chat_workspace_id": "bound-after"}, state_path)
        assert load_launcher_state(state_dir, state_path)["chat_workspace_id"] == "bound-after"

    assert load_launcher_state(decoy_dir, state_path)["chat_workspace_id"] == "bound-after"
    assert decoy_path.read_text(encoding="utf-8").count("decoy") == 1
    assert load_launcher_state(state_dir, state_path)["chat_workspace_id"] == "decoy"


def test_launcher_symlink_authority_never_mutates_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target-state"
    target.mkdir(mode=0o700)
    target.chmod(0o750)
    sentinel = target / "sentinel"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    sentinel.chmod(0o640)
    state_dir = tmp_path / "state-link"
    state_dir.symlink_to(target, target_is_directory=True)

    for operation in (
        lambda: load_launcher_state(state_dir),
        lambda: _run_launcher_lock(state_dir),
        lambda: save_launcher_state(state_dir, {"chat_workspace_id": "must-not-write"}),
    ):
        with pytest.raises(BootstrapError):
            operation()

    assert stat.S_IMODE(target.stat().st_mode) == 0o750
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"


def test_launcher_error_record_rejects_symlinked_state_directory_without_mutating_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target-state"
    target.mkdir(mode=0o700)
    target.chmod(0o750)
    sentinel = target / "sentinel"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    sentinel.chmod(0o640)
    state_dir = tmp_path / "state-link"
    state_dir.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(state_dir))

    module.append_launcher_error("setup", [], BootstrapError("boom"))

    assert stat.S_IMODE(target.stat().st_mode) == 0o750
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"
    assert not (target / module.LAUNCHER_ERRORS_NAME).exists()


def test_launcher_error_record_rejects_hostile_existing_error_file_shapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(state_dir))
    target = tmp_path / "target-errors"
    target.write_text("do not touch\n", encoding="utf-8")
    target.chmod(0o640)
    error_path = state_dir / module.LAUNCHER_ERRORS_NAME
    error_path.symlink_to(target)

    module.append_launcher_error("setup", [], BootstrapError("boom"))

    assert target.read_text(encoding="utf-8") == "do not touch\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    error_path.unlink()
    error_path.write_text("untrusted\n", encoding="utf-8")
    error_path.chmod(0o644)

    module.append_launcher_error("setup", [], BootstrapError("boom"))

    assert error_path.read_text(encoding="utf-8") == "untrusted\n"
    assert stat.S_IMODE(error_path.stat().st_mode) == 0o644


def test_nested_launcher_transaction_fails_without_waiting_for_second_lock(
    tmp_path: Path,
) -> None:
    with (
        launcher_state_lock(tmp_path),
        pytest.raises(BootstrapError, match="nested launcher state transaction"),
    ):
        started = time.monotonic()
        with launcher_state_lock(tmp_path):
            pass
        assert time.monotonic() - started < 0.5


def test_launcher_lock_closes_directory_on_socket_identity_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    missing_socket = tmp_path / "missing.sock"
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(missing_socket))
    opened: list[int] = []
    real_open = module._open_launcher_directory

    def tracking_open(path: Path) -> int:
        descriptor = real_open(path)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(module, "_open_launcher_directory", tracking_open)
    with (
        pytest.raises(BootstrapError, match="socket is unavailable"),
        launcher_state_lock(state_dir),
    ):
        pass

    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_transaction_refuses_to_write_after_server_socket_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket_path = Path(os.environ["HERDR_SOCKET_PATH"])
    with launcher_state_lock(tmp_path) as state_path:
        socket_path.unlink()
        socket_path.touch()
        with pytest.raises(BootstrapError, match="Herdr server changed"):
            save_launcher_state(tmp_path, {"chat_workspace_id": "w-stale"}, state_path)


def test_transaction_refuses_cleanup_after_server_socket_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket_path = Path(os.environ["HERDR_SOCKET_PATH"])
    monkeypatch.setattr(
        module,
        "run_json",
        lambda *_args, **_kwargs: pytest.fail("must not mutate the replacement server"),
    )
    with launcher_state_lock(tmp_path) as state_path:
        socket_path.unlink()
        socket_path.touch()
        state = {"stale_room_pane_ids": ["w-old:p1"]}
        module.clean_stale_room_panes("herdr", state, tmp_path, state_path)
        assert state["stale_room_pane_ids"] == ["w-old:p1"]


def test_workspace_creation_never_claims_generic_user_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: int = 30) -> dict:
        del timeout
        calls.append(arguments)
        if arguments == ["workspace", "list"]:
            return {
                "result": {
                    "workspaces": [
                        {"workspace_id": "w-user-chat", "label": "group-chat"},
                        {"workspace_id": "w-user-agents", "label": "agents"},
                    ]
                }
            }
        if arguments[:2] == ["workspace", "rename"]:
            return {"result": {"type": "workspace_info"}}
        label = arguments[arguments.index("--label") + 1]
        workspace_id = (
            "w-owned-agents" if label.startswith("hgchat-agents") else "w-owned-group-chat"
        )
        return {
            "result": {
                "workspace": {"workspace_id": workspace_id},
                "tab": {"tab_id": f"{workspace_id}:t1"},
            }
        }

    monkeypatch.setattr(module, "run_json", fake_run_json)
    state: dict = {"schema_version": 1}
    state_path = launcher_state_path(tmp_path)

    chat_id, _ = ensure_chat_workspace("herdr", str(tmp_path), tmp_path, state, state_path)
    agents_id, _ = ensure_agents_workspace("herdr", str(tmp_path), tmp_path, state, state_path)

    assert chat_id == "w-owned-group-chat"
    assert agents_id == "w-owned-agents"
    rename_targets = {
        arguments[2] for arguments in calls if arguments[:2] == ["workspace", "rename"]
    }
    assert rename_targets == {"w-owned-group-chat", "w-owned-agents"}


def test_recorded_workspace_is_reused_by_id_not_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: int = 30) -> dict:
        del timeout
        if arguments == ["workspace", "list"]:
            return {"result": {"workspaces": [{"workspace_id": "w-exact", "label": "renamed"}]}}
        assert arguments == ["workspace", "rename", "w-exact", "agents · group-chat"]
        return {"result": {"type": "workspace_info"}}

    monkeypatch.setattr(module, "run_json", fake_run_json)
    state = {"schema_version": 1, "agents_workspace_id": "w-exact"}

    assert ensure_agents_workspace(
        "herdr", str(tmp_path), tmp_path, state, launcher_state_path(tmp_path)
    ) == (
        "w-exact",
        None,
    )


def test_response_lost_workspace_is_reconciled_by_its_operation_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending_label = "hgchat-group-chat-0123456789"
    state = {"schema_version": 1, "pending_chat_workspace_label": pending_label}
    calls: list[list[str]] = []

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        if arguments == ["workspace", "list"]:
            return {
                "result": {
                    "workspaces": [
                        {
                            "workspace_id": "w-reconciled",
                            "active_tab_id": "w-reconciled:t1",
                            "label": pending_label,
                        }
                    ]
                }
            }
        assert arguments == ["workspace", "rename", "w-reconciled", "group-chat"]
        return {"result": {"type": "workspace_info"}}

    monkeypatch.setattr(module, "run_json", fake_run_json)

    assert ensure_chat_workspace(
        "herdr", str(tmp_path), tmp_path, state, launcher_state_path(tmp_path)
    ) == (
        "w-reconciled",
        "w-reconciled:t1",
    )
    assert not any(arguments[:2] == ["workspace", "create"] for arguments in calls)
    assert state["chat_workspace_id"] == "w-reconciled"
    assert "pending_chat_workspace_label" not in state


def test_open_focuses_only_the_recorded_plugin_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    save_launcher_state(
        tmp_path,
        {
            "room_pane_id": "w-chat:p-owned",
            "room_tab_id": "w-chat:t-owned",
            "chat_workspace_id": "w-chat",
            "last_room_id": "cockpit",
        },
    )
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))
    calls: list[list[str]] = []

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: int = 30) -> dict:
        del timeout
        calls.append(arguments)
        if arguments == ["pane", "get", "w-chat:p-owned"]:
            return {
                "result": {
                    "pane": {
                        "pane_id": "w-chat:p-owned",
                        "workspace_id": "w-chat",
                        "tab_id": "w-chat:t-owned",
                    }
                }
            }
        return {
            "result": {
                "type": "plugin_pane_focused",
                "plugin_pane": {
                    "pane": {
                        "pane_id": "w-chat:p-owned",
                        "workspace_id": "w-chat",
                        "tab_id": "w-chat:t-owned",
                    }
                },
            }
        }

    monkeypatch.setattr(module, "run_json", fake_run_json)

    assert launch_room(open_existing=True) == 0
    assert calls == [
        ["pane", "get", "w-chat:p-owned"],
        ["plugin", "pane", "focus", "w-chat:p-owned"],
    ]
    assert json.loads(capsys.readouterr().out)["result"]["type"] == "plugin_pane_focused"


def test_fresh_replacement_launch_never_focuses_the_existing_room(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_launcher_state(
        tmp_path,
        {
            "chat_workspace_id": "w-chat",
            "room_pane_id": "w-chat:p-old",
            "room_tab_id": "w-chat:t-old",
            "last_room_id": "chat-old",
        },
    )
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))
    calls: list[list[str]] = []

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        if arguments == ["workspace", "list"]:
            return {"result": {"workspaces": [{"workspace_id": "w-chat"}]}}
        if arguments == ["pane", "get", "w-chat:p-old"]:
            return {
                "result": {
                    "pane": {
                        "pane_id": "w-chat:p-old",
                        "workspace_id": "w-chat",
                        "tab_id": "w-chat:t-old",
                    }
                }
            }
        if arguments[:3] == ["plugin", "pane", "open"]:
            return {
                "result": {
                    "plugin_pane": {"pane": {"pane_id": "w-chat:p-new", "tab_id": "w-chat:t-new"}}
                }
            }
        return {"result": {"type": "tab_info"}}

    monkeypatch.setattr(module, "run_json", fake_run_json)

    assert launch_room(open_existing=False) == 0
    assert not any(call[:3] == ["plugin", "pane", "focus"] for call in calls)
    open_arguments = next(
        arguments for arguments in calls if arguments[:3] == ["plugin", "pane", "open"]
    )
    assert open_arguments[-1] == "--no-focus"
    state = load_launcher_state(tmp_path)
    assert state["room_pane_id"] == "w-chat:p-new"


def test_fresh_launch_opens_room_pane_and_workspaces_without_focus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))
    calls: list[list[str]] = []

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        if arguments == ["workspace", "list"]:
            return {"result": {"workspaces": []}}
        if arguments[:2] == ["workspace", "create"]:
            return {
                "result": {
                    "workspace": {"workspace_id": "w-created"},
                    "tab": {"tab_id": "w-created:t-placeholder"},
                }
            }
        if arguments[:2] == ["workspace", "rename"]:
            return {"result": {"type": "workspace_info"}}
        if arguments[:3] == ["plugin", "pane", "open"]:
            return {
                "result": {
                    "plugin_pane": {
                        "pane": {"pane_id": "w-created:p-new", "tab_id": "w-created:t-new"}
                    }
                }
            }
        return {"result": {"type": "tab_info"}}

    monkeypatch.setattr(module, "run_json", fake_run_json)

    assert launch_room(open_existing=False) == 0
    open_arguments = next(
        arguments for arguments in calls if arguments[:3] == ["plugin", "pane", "open"]
    )
    assert open_arguments[-1] == "--no-focus"
    assert "--focus" not in open_arguments
    workspace_creates = [
        arguments for arguments in calls if arguments[:2] == ["workspace", "create"]
    ]
    assert workspace_creates and all("--no-focus" in call for call in workspace_creates)


def test_open_recreates_missing_room_pane_with_focus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_launcher_state(
        tmp_path,
        {
            "chat_workspace_id": "w-chat",
            "room_pane_id": "w-chat:p-gone",
            "room_tab_id": "w-chat:t-gone",
            "last_room_id": "chat-kept",
        },
    )
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))
    calls: list[list[str]] = []

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        if arguments == ["workspace", "list"]:
            return {"result": {"workspaces": [{"workspace_id": "w-chat"}]}}
        if arguments == ["pane", "get", "w-chat:p-gone"]:
            raise BootstrapError("pane gone", code="pane_not_found")
        if arguments[:3] == ["plugin", "pane", "open"]:
            return {
                "result": {
                    "plugin_pane": {"pane": {"pane_id": "w-chat:p-new", "tab_id": "w-chat:t-new"}}
                }
            }
        return {"result": {"type": "tab_info"}}

    monkeypatch.setattr(module, "run_json", fake_run_json)

    assert launch_room(open_existing=True) == 0
    open_arguments = next(
        arguments for arguments in calls if arguments[:3] == ["plugin", "pane", "open"]
    )
    assert open_arguments[-1] == "--focus"
    assert "--no-focus" not in open_arguments
    state = load_launcher_state(tmp_path)
    assert state["last_room_id"] == "chat-kept"
    assert state["room_pane_id"] == "w-chat:p-new"


def test_existing_room_retries_recorded_placeholder_cleanup_before_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_launcher_state(
        tmp_path,
        {
            "chat_workspace_id": "w-chat",
            "agents_workspace_id": "w-agents",
            "room_pane_id": "w-chat:p-owned",
            "room_tab_id": "w-chat:t-owned",
            "chat_placeholder_tab_id": "w-chat:t-placeholder",
            "agents_placeholder_tab_id": "w-agents:t-placeholder",
        },
    )
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))
    calls: list[list[str]] = []

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        if arguments == ["pane", "get", "w-chat:p-owned"]:
            return {
                "result": {
                    "pane": {
                        "pane_id": "w-chat:p-owned",
                        "workspace_id": "w-chat",
                        "tab_id": "w-chat:t-owned",
                    }
                }
            }
        if arguments[:3] == ["plugin", "pane", "focus"]:
            return {
                "result": {
                    "plugin_pane": {
                        "pane": {
                            "pane_id": "w-chat:p-owned",
                            "workspace_id": "w-chat",
                            "tab_id": "w-chat:t-owned",
                        }
                    }
                }
            }
        return {"result": {"type": "tab_closed"}}

    monkeypatch.setattr(module, "run_json", fake_run_json)

    assert launch_room(open_existing=True) == 0
    assert ["tab", "close", "w-chat:t-placeholder"] in calls
    assert ["tab", "close", "w-agents:t-placeholder"] in calls
    state = load_launcher_state(tmp_path)
    assert "chat_placeholder_tab_id" not in state
    assert "agents_placeholder_tab_id" not in state


def test_launch_keeps_entrypoint_in_plugin_root_and_passes_agent_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    save_launcher_state(tmp_path, {"chat_workspace_id": "w-chat"})
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HERDR_PLUGIN_CONTEXT_JSON", json.dumps({"focused_pane_cwd": str(project)}))
    calls: list[list[str]] = []

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: int = 30) -> dict:
        del timeout
        calls.append(arguments)
        if arguments == ["workspace", "list"]:
            return {"result": {"workspaces": [{"workspace_id": "w-chat"}]}}
        if arguments[:3] == ["plugin", "pane", "open"]:
            return {
                "result": {
                    "plugin_pane": {"pane": {"pane_id": "w-chat:p-new", "tab_id": "w-chat:t-new"}}
                }
            }
        return {"result": {"type": "tab_info"}}

    monkeypatch.setattr(module, "run_json", fake_run_json)

    assert launch_room(open_existing=False) == 0
    open_arguments = next(
        arguments for arguments in calls if arguments[:3] == ["plugin", "pane", "open"]
    )
    assert "--cwd" not in open_arguments
    assert f"{module.AGENT_CWD_ENV}={project}" in open_arguments


def test_open_failure_keeps_created_workspace_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: int = 30) -> dict:
        del timeout
        if arguments == ["workspace", "list"]:
            return {"result": {"workspaces": []}}
        if arguments[:2] == ["workspace", "create"]:
            return {
                "result": {
                    "workspace": {"workspace_id": "w-created"},
                    "tab": {"tab_id": "w-created:t-placeholder"},
                }
            }
        if arguments[:2] == ["workspace", "rename"]:
            return {"result": {"type": "workspace_info"}}
        if arguments[:3] == ["plugin", "pane", "open"]:
            raise BootstrapError("temporary failure", code="server_unavailable")
        raise AssertionError(arguments)

    monkeypatch.setattr(module, "run_json", fake_run_json)

    with pytest.raises(BootstrapError, match="temporary failure"):
        launch_room(open_existing=False)
    state = load_launcher_state(tmp_path)
    assert state["chat_workspace_id"] == "w-created"
    assert state["chat_placeholder_tab_id"] == "w-created:t-placeholder"
    assert state["pending_room_id"].startswith("chat-")


def test_pending_room_prevents_a_duplicate_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_launcher_state(
        tmp_path,
        {
            "chat_workspace_id": "w-chat",
            "room_pane_id": "w-chat:p-old",
            "pending_room_id": "chat-pending",
            "pending_room_operation_id": "operation-pending",
            "pending_room_started_unix_ms": int(module.time.time() * 1000),
        },
    )
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))

    def unexpected_call(*_args: object, **_kwargs: object) -> dict:
        raise AssertionError("no Herdr mutation should occur while a room launch is pending")

    monkeypatch.setattr(module, "run_json", unexpected_call)

    with pytest.raises(BootstrapError, match="previous room launch is still pending"):
        launch_room(open_existing=True)


def test_room_registration_recovers_lost_response_and_tracks_displaced_pane() -> None:
    state = {
        "room_pane_id": "w-chat:p-old",
        "pending_room_id": "chat-new",
        "pending_room_operation_id": "operation-new",
        "pending_room_started_unix_ms": 1,
    }

    module.register_room_pane(
        state,
        workspace_id="w-chat",
        pane_id="w-chat:p-new",
        tab_id="w-chat:t-new",
        room="chat-new",
        operation_id="operation-new",
    )

    assert state["room_pane_id"] == "w-chat:p-new"
    assert state["stale_room_pane_ids"] == ["w-chat:p-old"]
    assert "pending_room_operation_id" not in state


def test_outer_registration_records_ownership_but_retains_pending_operation() -> None:
    """The launcher registers the pane yet leaves pending state for the inner claim."""
    state = {
        "room_pane_id": "w-chat:p-old",
        "pending_room_id": "chat-new",
        "pending_room_operation_id": "operation-new",
        "pending_room_profile": "sol-fable",
        "pending_room_started_unix_ms": 1,
    }

    module.register_room_pane(
        state,
        workspace_id="w-chat",
        pane_id="w-chat:p-new",
        tab_id="w-chat:t-new",
        room="chat-new",
        operation_id="operation-new",
        consume_pending=False,
    )

    assert state["room_pane_id"] == "w-chat:p-new"
    assert state["room_tab_id"] == "w-chat:t-new"
    assert state["stale_room_pane_ids"] == ["w-chat:p-old"]
    assert state["last_room_id"] == "chat-new"
    assert state["pending_room_id"] == "chat-new"
    assert state["pending_room_operation_id"] == "operation-new"
    assert state["pending_room_profile"] == "sol-fable"
    assert state["pending_room_started_unix_ms"] == 1

    # The matching inner registration then consumes the pending fields.
    module.register_room_pane(
        state,
        workspace_id="w-chat",
        pane_id="w-chat:p-new",
        tab_id="w-chat:t-new",
        room="chat-new",
        operation_id="operation-new",
    )
    assert state["room_pane_id"] == "w-chat:p-new"
    for field in (
        "pending_room_id",
        "pending_room_operation_id",
        "pending_room_profile",
        "pending_room_started_unix_ms",
    ):
        assert field not in state


def test_inner_registration_with_mismatched_operation_fails_without_consuming() -> None:
    """A superseding operation must not steal or clear another launch's pending claim."""
    state = {
        "pending_room_id": "chat-new",
        "pending_room_operation_id": "operation-new",
        "pending_room_profile": "sol-fable",
        "pending_room_started_unix_ms": 1,
    }

    with pytest.raises(BootstrapError, match="superseded by a newer operation"):
        module.register_room_pane(
            state,
            workspace_id="w-chat",
            pane_id="w-chat:p-new",
            tab_id="w-chat:t-new",
            room="chat-new",
            operation_id="operation-other",
        )

    assert state["pending_room_operation_id"] == "operation-new"
    assert state["pending_room_profile"] == "sol-fable"
    assert "room_pane_id" not in state


def test_participant_start_does_not_move_a_globally_named_external_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "PARTICIPANTS", (("pi", "pi-peer"),))
    calls: list[list[str]] = []

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        assert arguments == ["agent", "list"]
        return {
            "result": {
                "agents": [
                    {
                        "name": "pi-peer",
                        "kind": "pi",
                        "pane_id": "w-agents:p-unrecorded",
                        "workspace_id": "w-agents",
                        "cwd": str(tmp_path),
                    }
                ]
            }
        }

    monkeypatch.setattr(module, "run_json", fake_run_json)
    state: dict = {"schema_version": 1}
    failures = module.start_participants(
        "herdr", str(tmp_path), "w-agents", tmp_path, launcher_state_path(tmp_path), state
    )

    assert failures and "(pane not recorded)" in failures[0]
    assert "Adopt stale group-chat peers" in failures[0]
    assert calls == [["agent", "list"]]


@pytest.mark.parametrize(
    ("reported_kind", "reported_agent"),
    [("claude", "pi"), ("pi", "claude")],
)
def test_same_named_wrong_kind_agent_is_left_open_and_never_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reported_kind: str,
    reported_agent: str,
) -> None:
    calls: list[list[str]] = []
    participant = module.Participant(role="pi", kind="pi", name="pi-peer")

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        assert arguments == ["agent", "list"]
        return {
            "result": {
                "agents": [
                    {
                        "name": "pi-peer",
                        "kind": reported_kind,
                        "agent": reported_agent,
                        "workspace_id": "w-agents",
                        "cwd": str(tmp_path),
                        "pane_id": "w-agents:p-pi",
                        "tab_id": "w-agents:t-pi",
                    }
                ]
            }
        }

    monkeypatch.setattr(module, "run_json", fake_run_json)
    state: dict = {
        "schema_version": 1,
        "participant_pane_ids": {"pi": "w-agents:p-pi"},
        "participant_tab_ids": {"pi": "w-agents:t-pi"},
    }

    failures = module.start_participants(
        "herdr",
        str(tmp_path),
        "w-agents",
        tmp_path,
        launcher_state_path(tmp_path),
        state,
        participants=(participant,),
    )

    assert failures == [
        "@pi: pi-peer is already running with a different agent kind; the session was left open"
    ]
    assert calls == [["agent", "list"]]
    assert state["participant_pane_ids"] == {"pi": "w-agents:p-pi"}
    assert state["participant_tab_ids"] == {"pi": "w-agents:t-pi"}


def test_indeterminate_agent_start_is_recorded_and_not_duplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "PARTICIPANTS", (("pi", "pi-peer"),))
    calls: list[list[str]] = []

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        if arguments == ["agent", "list"]:
            return {"result": {"agents": []}}
        if arguments[:2] == ["tab", "create"]:
            return {
                "result": {
                    "tab": {"tab_id": "w-agents:t-pending"},
                    "root_pane": {"pane_id": "w-agents:p-pending"},
                }
            }
        if arguments[:2] == ["pane", "process-info"]:
            return {"result": {"process_info": {"foreground_processes": [{"name": "zsh"}]}}}
        if arguments[:2] == ["agent", "start"]:
            raise BootstrapError("connection lost", code="server_unavailable")
        raise AssertionError(arguments)

    monkeypatch.setattr(module, "run_json", fake_run_json)
    state: dict = {"schema_version": 1}
    state_path = launcher_state_path(tmp_path)

    first = module.start_participants(
        "herdr", str(tmp_path), "w-agents", tmp_path, state_path, state
    )
    second = module.start_participants(
        "herdr", str(tmp_path), "w-agents", tmp_path, state_path, state
    )

    assert first
    assert second == ["@pi: previous agent start is still pending"]
    assert len([call for call in calls if call[:2] == ["tab", "create"]]) == 1
    assert state["pending_participant_tabs"]["pi"]["pane_id"] == "w-agents:p-pending"


def test_failed_pending_cleanup_retains_ownership_and_blocks_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "PARTICIPANTS", (("pi", "pi-peer"),))
    state = {
        "schema_version": 1,
        "pending_participant_tabs": {
            "pi": {
                "label": "hgchat-pi-owned",
                "tab_id": "w-agents:t-old",
                "started_unix_ms": 0,
            }
        },
    }
    calls: list[list[str]] = []

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        if arguments == ["agent", "list"]:
            return {"result": {"agents": []}}
        if arguments == ["tab", "close", "w-agents:t-old"]:
            raise BootstrapError("temporary failure", code="server_unavailable")
        raise AssertionError(arguments)

    monkeypatch.setattr(module, "run_json", fake_run_json)
    failures = module.start_participants(
        "herdr",
        str(tmp_path),
        "w-agents",
        tmp_path,
        launcher_state_path(tmp_path),
        state,
    )

    assert failures and "cleanup failed" in failures[0]
    assert state["pending_participant_tabs"]["pi"]["tab_id"] == "w-agents:t-old"
    assert not any(call[:2] == ["tab", "create"] for call in calls)


def test_exited_recorded_agent_tab_is_closed_before_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "PARTICIPANTS", (("pi", "pi-peer"),))
    state = {
        "schema_version": 1,
        "participant_pane_ids": {"pi": "w-agents:p-old"},
        "participant_tab_ids": {"pi": "w-agents:t-old"},
    }
    calls: list[list[str]] = []

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        if arguments == ["agent", "list"]:
            return {"result": {"agents": []}}
        if arguments == ["tab", "close", "w-agents:t-old"]:
            raise BootstrapError("temporary failure", code="server_unavailable")
        raise AssertionError(arguments)

    monkeypatch.setattr(module, "run_json", fake_run_json)
    failures = module.start_participants(
        "herdr",
        str(tmp_path),
        "w-agents",
        tmp_path,
        launcher_state_path(tmp_path),
        state,
    )

    assert failures and "previous-tab cleanup failed" in failures[0]
    assert state["participant_pane_ids"]["pi"] == "w-agents:p-old"
    assert not any(call[:2] == ["tab", "create"] for call in calls)


def test_definite_pane_open_failure_clears_pending_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_launcher_state(tmp_path, {"chat_workspace_id": "w-chat"})
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        if arguments == ["workspace", "list"]:
            return {"result": {"workspaces": [{"workspace_id": "w-chat", "label": "group-chat"}]}}
        if arguments[:3] == ["plugin", "pane", "open"]:
            raise BootstrapError("spawn failed", code="plugin_pane_open_failed")
        raise AssertionError(arguments)

    monkeypatch.setattr(module, "run_json", fake_run_json)

    with pytest.raises(BootstrapError, match="spawn failed"):
        launch_room(open_existing=False)
    state = load_launcher_state(tmp_path)
    assert "pending_room_id" not in state
    assert "pending_room_started_unix_ms" not in state


def test_transient_validation_failure_does_not_open_a_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_launcher_state(
        tmp_path,
        {
            "chat_workspace_id": "w-chat",
            "room_pane_id": "w-chat:p-current",
            "room_tab_id": "w-chat:t-current",
        },
    )
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))
    calls: list[list[str]] = []

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: int = 30) -> dict:
        del timeout
        calls.append(arguments)
        raise BootstrapError("temporary failure", code="server_unavailable")

    monkeypatch.setattr(module, "run_json", fake_run_json)

    with pytest.raises(BootstrapError, match="temporary failure"):
        launch_room(open_existing=True)
    assert calls == [["pane", "get", "w-chat:p-current"]]


def test_failed_old_pane_close_remains_in_cleanup_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_launcher_state(
        tmp_path,
        {
            "chat_workspace_id": "w-chat",
            "room_pane_id": "w-chat:p-old",
            "room_tab_id": "w-chat:t-old",
        },
    )
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: int = 30) -> dict:
        del timeout
        if arguments == ["workspace", "list"]:
            return {"result": {"workspaces": [{"workspace_id": "w-chat"}]}}
        if arguments == ["pane", "get", "w-chat:p-old"]:
            return {
                "result": {
                    "pane": {
                        "pane_id": "w-chat:p-old",
                        "workspace_id": "w-chat",
                        "tab_id": "w-chat:t-old",
                    }
                }
            }
        if arguments[:3] == ["plugin", "pane", "open"]:
            return {
                "result": {
                    "plugin_pane": {"pane": {"pane_id": "w-chat:p-new", "tab_id": "w-chat:t-new"}}
                }
            }
        if arguments == ["plugin", "pane", "close", "w-chat:p-old"]:
            raise BootstrapError("temporary failure", code="server_unavailable")
        return {"result": {"type": "tab_info"}}

    monkeypatch.setattr(module, "run_json", fake_run_json)

    assert launch_room(open_existing=False) == 0
    state = load_launcher_state(tmp_path)
    assert state["room_pane_id"] == "w-chat:p-new"
    assert state["stale_room_pane_ids"] == ["w-chat:p-old"]


def test_rename_failure_is_recovered_before_the_next_open_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_launcher_state(
        tmp_path,
        {
            "chat_workspace_id": "w-chat",
            "chat_placeholder_tab_id": "w-chat:t-placeholder",
        },
    )
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))
    rename_failed = False
    placeholder_close_failed = False

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        nonlocal placeholder_close_failed, rename_failed
        del timeout
        if arguments == ["workspace", "list"]:
            return {"result": {"workspaces": [{"workspace_id": "w-chat"}]}}
        if arguments == ["pane", "get", "w-chat:p-new"]:
            return {
                "result": {
                    "pane": {
                        "pane_id": "w-chat:p-new",
                        "workspace_id": "w-chat",
                        "tab_id": "w-chat:t-new",
                    }
                }
            }
        if arguments[:3] == ["plugin", "pane", "open"]:
            return {
                "result": {
                    "plugin_pane": {"pane": {"pane_id": "w-chat:p-new", "tab_id": "w-chat:t-new"}}
                }
            }
        if arguments[:2] == ["tab", "rename"] and not rename_failed:
            rename_failed = True
            raise BootstrapError("temporary failure", code="server_unavailable")
        if arguments == ["tab", "close", "w-chat:t-placeholder"] and not placeholder_close_failed:
            placeholder_close_failed = True
            raise BootstrapError("temporary failure", code="server_unavailable")
        if arguments[:3] == ["plugin", "pane", "focus"]:
            return {
                "result": {
                    "plugin_pane": {
                        "pane": {
                            "pane_id": "w-chat:p-new",
                            "workspace_id": "w-chat",
                            "tab_id": "w-chat:t-new",
                        }
                    }
                }
            }
        return {"result": {"type": "ok"}}

    monkeypatch.setattr(module, "run_json", fake_run_json)

    with pytest.raises(BootstrapError, match="temporary failure"):
        launch_room(open_existing=False)
    assert load_launcher_state(tmp_path)["chat_placeholder_tab_id"] == ("w-chat:t-placeholder")

    # The outer registration retains the pending operation for the inner claim;
    # a retry stays grace-blocked until the pending launch ages out.
    with pytest.raises(BootstrapError, match="still pending"):
        launch_room(open_existing=True)
    aged = load_launcher_state(tmp_path)
    aged["pending_room_started_unix_ms"] = 1
    save_launcher_state(tmp_path, aged)

    assert launch_room(open_existing=True) == 0
    assert "chat_placeholder_tab_id" not in load_launcher_state(tmp_path)


def test_fresh_room_ids_are_unique_and_valid() -> None:
    first = new_room_id()
    second = new_room_id()

    assert first != second
    assert re.fullmatch(r"chat-\d{8}-\d{6}-[a-f0-9]{6}", first)
    assert len(first) <= 32


def install_hook_notice_fake(
    monkeypatch: pytest.MonkeyPatch, reads: list[str], calls: list[list[str]]
) -> None:
    """Serve pane reads as raw terminal text via run_text; sends stay structured JSON."""

    def fake_run_text(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> str:
        del timeout
        calls.append(arguments)
        assert arguments[:2] == ["pane", "read"], arguments
        return reads.pop(0) if reads else ""

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        assert arguments[:2] != ["pane", "read"], "pane read must use the raw-text contract"
        return {"result": {"type": "ok"}}

    monkeypatch.setattr(module, "run_text", fake_run_text)
    monkeypatch.setattr(module, "run_json", fake_run_json)


def test_hook_notice_summary_is_dismissed_with_esc(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[list[str]] = []
    install_hook_notice_fake(
        monkeypatch,
        [
            "Codex detected unreviewed lifecycle hooks.\n"
            "Press t to trust all; enter to review hooks; esc to close"
        ],
        calls,
    )

    module.dismiss_inactive_hook_notice("herdr", "codex-peer", "w-agents:p-codex")

    assert calls == [
        ["pane", "read", "w-agents:p-codex", "--source", "visible", "--lines", "80"],
        ["agent", "send-keys", "codex-peer", "esc"],
    ]
    assert "unreviewed hooks left inactive" in capsys.readouterr().out


def test_hook_notice_detection_survives_viewport_clipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    install_hook_notice_fake(
        monkeypatch, ["Press T to TRUST ALL; enter to review hooks; es"], calls
    )

    module.dismiss_inactive_hook_notice("herdr", "codex-peer", "w-agents:p-codex")

    assert calls[-1] == ["agent", "send-keys", "codex-peer", "esc"]


def test_hook_menu_variant_continues_without_trusting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    install_hook_notice_fake(
        monkeypatch,
        [
            "\u203a 1. Review hooks\n"
            "  2. Trust all and continue\n"
            "  3. Continue without trusting (hooks won't run)\n"
            "Press enter to confirm or esc to go back"
        ],
        calls,
    )

    module.dismiss_inactive_hook_notice("herdr", "codex-peer", "w-agents:p-codex")

    assert calls[1:] == [
        ["agent", "send-keys", "codex-peer", "down"],
        ["agent", "send-keys", "codex-peer", "down"],
        ["agent", "send-keys", "codex-peer", "enter"],
    ]


def test_hook_notice_retry_catches_a_late_rendering_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(module, "HOOK_DISMISS_INTERVAL_S", 0)
    install_hook_notice_fake(
        monkeypatch,
        ["", "", "Press t to trust all; enter to review hooks; es"],
        calls,
    )

    module.dismiss_inactive_hook_notice("herdr", "codex-peer", "w-agents:p-codex")

    reads = [call for call in calls if call[:2] == ["pane", "read"]]
    assert len(reads) == 3
    assert calls[-1] == ["agent", "send-keys", "codex-peer", "esc"]


def test_hook_notice_absent_dialog_is_silent_and_bounded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(module, "HOOK_DISMISS_INTERVAL_S", 0)
    install_hook_notice_fake(monkeypatch, [], calls)

    module.dismiss_inactive_hook_notice("herdr", "codex-peer", "w-agents:p-codex")

    assert len(calls) == module.HOOK_DISMISS_ATTEMPTS
    assert all(call[:2] == ["pane", "read"] for call in calls)
    assert capsys.readouterr().out == ""


HOOK_MENU_SCREEN = (
    "Codex detected unreviewed lifecycle hooks.\n"
    "\u203a 1. Review hooks\n"
    "  2. Trust all and continue\n"
    "  3. Continue without trusting\n"
    "Press enter to confirm or esc to go back"
)


def test_pane_read_follows_herdrs_raw_text_cli_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: pane read emits raw terminal text with no JSON envelope.

    Dismissal must run end to end against that raw output. Routing the read
    through run_json, or parsing the raw text as JSON, raises
    "Herdr returned invalid JSON" and no key is ever sent.
    """
    sent_keys = tmp_path / "sent-keys"
    monkeypatch.setenv("HOOK_FAKE_SENT_KEYS", str(sent_keys))
    herdr = tmp_path / "herdr"
    herdr.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "pane" ] && [ "$2" = "read" ]; then\n'
        f"    cat <<'HOOKS'\n{HOOK_MENU_SCREEN}\nHOOKS\n"
        "    exit 0\n"
        "fi\n"
        'if [ "$1" = "agent" ] && [ "$2" = "send-keys" ]; then\n'
        '    printf \'%s\\n\' "$4" >> "$HOOK_FAKE_SENT_KEYS"\n'
        '    printf \'{"result":{"type":"ok"}}\'\n'
        "    exit 0\n"
        "fi\n"
        'printf \'{"error":{"code":"unexpected_command"}}\' >&2\n'
        "exit 1\n",
        encoding="utf-8",
    )
    herdr.chmod(0o755)

    module.dismiss_inactive_hook_notice(str(herdr), "codex-peer", "w-agents:p-codex")

    assert sent_keys.read_text(encoding="utf-8").splitlines() == ["down", "down", "enter"]


def test_hook_notice_reads_and_sleeps_share_one_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One monotonic deadline bounds every pane read, retry sleep, and key send."""
    calls: list[list[str]] = []
    time_now = [0.0]
    read_budgets: list[float] = []
    sleeps: list[float] = []

    def fake_run_text(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> str:
        calls.append(arguments)
        read_budgets.append(timeout or 0.0)
        time_now[0] += 0.6
        return ""

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        return {"result": {"type": "ok"}}

    def fake_sleep(duration: float) -> None:
        sleeps.append(duration)
        time_now[0] += duration

    monkeypatch.setattr(module.time, "monotonic", lambda: time_now[0])
    monkeypatch.setattr(module.time, "sleep", fake_sleep)
    monkeypatch.setattr(module, "run_text", fake_run_text)
    monkeypatch.setattr(module, "run_json", fake_run_json)

    module.dismiss_inactive_hook_notice("herdr", "codex-peer", "w-agents:p-codex")

    reads = [call for call in calls if call[:2] == ["pane", "read"]]
    # 0.6 s reads + 1.0 s sleeps exhaust the 5 s budget before the 10-attempt ceiling.
    assert len(reads) == 4
    assert not any(call[:2] == ["agent", "send-keys"] for call in calls)
    assert read_budgets == pytest.approx([5.0, 3.4, 1.8, 0.2])
    assert all(earlier > later for earlier, later in pairwise(read_budgets))
    assert sleeps == pytest.approx([1.0, 1.0, 1.0])


def test_hook_notice_send_keys_receive_the_remaining_shared_budget(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[list[str]] = []
    budgets: list[float] = []
    time_now = [0.0]

    def fake_run_text(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> str:
        del timeout
        calls.append(arguments)
        time_now[0] += 0.5
        return HOOK_MENU_SCREEN

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        calls.append(arguments)
        budgets.append(timeout or 0.0)
        time_now[0] += 0.5
        return {"result": {"type": "ok"}}

    monkeypatch.setattr(module.time, "monotonic", lambda: time_now[0])
    monkeypatch.setattr(module, "run_text", fake_run_text)
    monkeypatch.setattr(module, "run_json", fake_run_json)

    module.dismiss_inactive_hook_notice("herdr", "codex-peer", "w-agents:p-codex")

    assert [call[-1] for call in calls[1:]] == ["down", "down", "enter"]
    assert budgets == pytest.approx([4.5, 4.0, 3.5])
    assert all(earlier > later for earlier, later in pairwise(budgets))
    assert "unreviewed hooks left inactive" in capsys.readouterr().out


def test_hook_notice_deadline_expiry_sends_no_further_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[list[str]] = []
    time_now = [0.0]

    def fake_run_text(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> str:
        del timeout
        calls.append(arguments)
        time_now[0] = 4.0
        return HOOK_MENU_SCREEN

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        time_now[0] += 0.75
        return {"result": {"type": "ok"}}

    monkeypatch.setattr(module.time, "monotonic", lambda: time_now[0])
    monkeypatch.setattr(module, "run_text", fake_run_text)
    monkeypatch.setattr(module, "run_json", fake_run_json)

    module.dismiss_inactive_hook_notice("herdr", "codex-peer", "w-agents:p-codex")

    # The menu was found at 4.0 s; two key sends fit, the third does not, and the
    # confirming enter is never sent, so nothing is trusted.
    assert [call[-1] for call in calls[1:]] == ["down", "down"]
    assert capsys.readouterr().out == ""


def test_started_participant_tabs_are_labeled_for_group_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "PARTICIPANTS", (("pi", "pi-peer"), ("codex", "codex-peer")))
    monkeypatch.setattr(module, "HOOK_DISMISS_INTERVAL_S", 0)
    calls: list[list[str]] = []

    def fake_run_text(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> str:
        del timeout
        calls.append(arguments)
        return ""

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        if arguments == ["agent", "list"]:
            return {"result": {"agents": []}}
        if arguments[:2] == ["tab", "create"]:
            label = arguments[arguments.index("--label") + 1]
            kind = label.split("-")[1]
            return {
                "result": {
                    "tab": {"tab_id": f"w-agents:t-{kind}"},
                    "root_pane": {"pane_id": f"w-agents:p-{kind}"},
                }
            }
        if arguments[:2] == ["pane", "process-info"]:
            return {"result": {"process_info": {"foreground_processes": [{"name": "zsh"}]}}}
        return {"result": {"type": "ok"}}

    monkeypatch.setattr(module, "run_text", fake_run_text)
    monkeypatch.setattr(module, "run_json", fake_run_json)
    state: dict = {"schema_version": 1}

    failures = module.start_participants(
        "herdr", str(tmp_path), "w-agents", tmp_path, launcher_state_path(tmp_path), state
    )

    assert failures == []
    renames = [call for call in calls if call[:2] == ["tab", "rename"]]
    assert renames == [
        ["tab", "rename", "w-agents:t-pi", "pi · group-chat"],
        ["tab", "rename", "w-agents:t-codex", "codex · group-chat"],
    ]
    reads = [call for call in calls if call[:2] == ["pane", "read"]]
    assert reads and all(call[2] == "w-agents:p-codex" for call in reads)
    assert not any(call[:2] == ["agent", "send-keys"] for call in calls)


def launch_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured: dict[str, object]
) -> None:
    """Set the pane context main() requires and capture the exec'd room process."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w-chat")
    monkeypatch.setenv("HERDR_TAB_ID", "w-chat:t-room")
    monkeypatch.setenv("HERDR_PANE_ID", "w-chat:p-room")
    monkeypatch.setenv(module.ROOM_OPERATION_ENV, "operation-test")
    monkeypatch.setattr(
        module.os,
        "execv",
        lambda path, argv: captured.update(path=path, argv=argv, env=dict(os.environ)),
    )


def test_launch_hands_participant_failures_to_the_room_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "PARTICIPANTS", (("pi", "pi-peer"),))
    captured: dict[str, object] = {}
    launch_env(tmp_path, monkeypatch, captured)

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        if arguments == ["workspace", "list"]:
            return {"result": {"workspaces": []}}
        if arguments[:2] == ["workspace", "create"]:
            return {
                "result": {
                    "workspace": {"workspace_id": "w-agents"},
                    "tab": {"tab_id": "w-agents:t1"},
                }
            }
        if arguments == ["agent", "list"]:
            return {
                "result": {
                    "agents": [
                        {
                            "name": "pi-peer",
                            "kind": "pi",
                            "workspace_id": "w-other",
                            "cwd": str(tmp_path),
                            "pane_id": "w-other:p1",
                            "tab_id": "w-other:t1",
                        }
                    ]
                }
            }
        return {"result": {"type": "ok"}}

    monkeypatch.setattr(module, "run_json", fake_run_json)

    module.main()

    env = captured["env"]
    assert env[module.SETUP_FAILURES_ENV] == (
        "@pi: pi-peer is live in workspace w-other but this launcher owns "
        "w-agents (different workspace, different cwd, pane not recorded); "
        "run the Adopt stale group-chat peers action or close that peer tab"
    )
    argv = captured["argv"]
    assert argv[0].endswith("herdr-group-chat") and "--room" in argv


def test_launch_without_failures_clears_stale_setup_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "PARTICIPANTS", (("pi", "pi-peer"),))
    captured: dict[str, object] = {}
    launch_env(tmp_path, monkeypatch, captured)
    monkeypatch.setenv(module.SETUP_FAILURES_ENV, "@pi: stale failure from a previous launch")
    save_launcher_state(
        tmp_path,
        {
            "agents_workspace_id": "w-agents",
            "agents_cwd": str(tmp_path),
            "participant_pane_ids": {"pi": "w-agents:p1"},
            "participant_tab_ids": {"pi": "w-agents:t1"},
        },
    )

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        if arguments == ["workspace", "list"]:
            return {
                "result": {
                    "workspaces": [{"workspace_id": "w-agents", "label": "agents · group-chat"}]
                }
            }
        if arguments == ["agent", "list"]:
            return {
                "result": {
                    "agents": [
                        {
                            "name": "pi-peer",
                            "kind": "pi",
                            "workspace_id": "w-agents",
                            "cwd": str(tmp_path),
                            "pane_id": "w-agents:p1",
                            "tab_id": "w-agents:t1",
                        }
                    ]
                }
            }
        return {"result": {"type": "ok"}}

    monkeypatch.setattr(module, "run_json", fake_run_json)

    module.main()

    assert module.SETUP_FAILURES_ENV not in captured["env"]


PANE_BUSY_ERROR = BootstrapError(
    '{"error":{"code":"agent_pane_busy","message":"agent target pane is not an available shell"}}',
    code="agent_pane_busy",
)


def participant_start_fake(
    calls: list[list[str]], start_outcomes: list[Exception | None]
) -> Callable[..., dict]:
    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        if arguments == ["agent", "list"]:
            return {"result": {"agents": []}}
        if arguments[:2] == ["tab", "create"]:
            return {
                "result": {
                    "tab": {"tab_id": "w-agents:t-grok"},
                    "root_pane": {"pane_id": "w-agents:p-grok"},
                }
            }
        if arguments[:2] == ["pane", "process-info"]:
            return {"result": {"process_info": {"foreground_processes": [{"name": "zsh"}]}}}
        if arguments[:2] == ["agent", "start"]:
            outcome = start_outcomes.pop(0) if start_outcomes else None
            if outcome is not None:
                raise outcome
        return {"result": {"type": "ok"}}

    return fake_run_json


def run_grok_participant_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start_outcomes: list[Exception | None],
) -> tuple[list[str], list[list[str]]]:
    monkeypatch.setattr(module, "PARTICIPANTS", (("grok", "grok-peer"),))
    monkeypatch.setattr(module, "AGENT_START_BUSY_INTERVAL_S", 0)
    calls: list[list[str]] = []
    monkeypatch.setattr(module, "run_json", participant_start_fake(calls, start_outcomes))
    state: dict = {"schema_version": 1}
    failures = module.start_participants(
        "herdr", str(tmp_path), "w-agents", tmp_path, launcher_state_path(tmp_path), state
    )
    return failures, calls


def test_agent_start_retries_transient_pane_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failures, calls = run_grok_participant_start(
        tmp_path, monkeypatch, [PANE_BUSY_ERROR, PANE_BUSY_ERROR]
    )

    assert failures == []
    starts = [call for call in calls if call[:2] == ["agent", "start"]]
    assert len(starts) == 3


def test_agent_start_persistent_pane_busy_records_failure_after_bounded_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failures, calls = run_grok_participant_start(
        tmp_path, monkeypatch, [PANE_BUSY_ERROR] * module.AGENT_START_BUSY_ATTEMPTS
    )

    assert len(failures) == 1 and "agent_pane_busy" in failures[0]
    starts = [call for call in calls if call[:2] == ["agent", "start"]]
    assert len(starts) == module.AGENT_START_BUSY_ATTEMPTS


def test_agent_start_does_not_retry_other_error_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failures, calls = run_grok_participant_start(
        tmp_path, monkeypatch, [BootstrapError("pane is gone", code="pane_not_found")]
    )

    assert len(failures) == 1 and "pane is gone" in failures[0]
    starts = [call for call in calls if call[:2] == ["agent", "start"]]
    assert len(starts) == 1


@pytest.mark.parametrize(
    ("participant", "product"),
    [
        (module.Participant(role="claude", kind="claude", name="claude-peer"), "Claude"),
        (module.Participant(role="codex", kind="codex", name="codex-peer"), "Codex"),
    ],
)
def test_agent_not_ready_preserves_the_pending_trust_prompt_tab(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    participant: module.Participant,
    product: str,
) -> None:
    calls: list[list[str]] = []
    pane_id = f"w-agents:p-{participant.role}"
    tab_id = f"w-agents:t-{participant.role}"

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        if arguments == ["agent", "list"]:
            return {"result": {"agents": []}}
        if arguments[:2] == ["tab", "create"]:
            return {"result": {"tab": {"tab_id": tab_id}, "root_pane": {"pane_id": pane_id}}}
        if arguments[:2] == ["pane", "process-info"]:
            return {"result": {"process_info": {"foreground_processes": [{"name": "zsh"}]}}}
        if arguments[:2] == ["agent", "start"]:
            raise BootstrapError("agent needs directory trust", code="agent_not_ready")
        return {"result": {"type": "ok"}}

    monkeypatch.setattr(module, "run_json", fake_run_json)
    state: dict = {"schema_version": 1}

    failures = module.start_participants(
        "herdr",
        str(tmp_path),
        "w-agents",
        tmp_path,
        launcher_state_path(tmp_path),
        state,
        participants=(participant,),
    )

    assert failures == [
        f"@{participant.role}: {product} is waiting at a blocked startup prompt in the "
        "preserved tab; answer the prompt there, then rerun group-chat setup"
    ]
    assert state["pending_participant_tabs"][participant.role]["pane_id"] == pane_id
    assert state["pending_participant_tabs"][participant.role]["tab_id"] == tab_id
    assert state["pending_participant_tabs"][participant.role]["blocked_startup"] is True
    assert participant.role not in state.get("participant_pane_ids", {})
    assert participant.role not in state.get("participant_tab_ids", {})
    assert not any(call[:2] == ["tab", "close"] for call in calls)


def test_stale_blocked_startup_tab_is_preserved_and_repeats_prompt_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant = module.Participant(role="codex", kind="codex", name="codex-peer")
    pending = {
        "label": "hgchat-codex-trust",
        "pane_id": "w-agents:p-codex",
        "tab_id": "w-agents:t-codex",
        "started_unix_ms": 0,
        "blocked_startup": True,
    }
    calls: list[list[str]] = []

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        if arguments == ["agent", "list"]:
            return {"result": {"agents": []}}
        if arguments == ["tab", "list", "--workspace", "w-agents"]:
            return {
                "result": {
                    "tabs": [
                        {
                            "tab_id": pending["tab_id"],
                            "workspace_id": "w-agents",
                            "root_pane": {"pane_id": pending["pane_id"]},
                        }
                    ]
                }
            }
        raise AssertionError(arguments)

    monkeypatch.setattr(module, "run_json", fake_run_json)
    state: dict = {"schema_version": 1, "pending_participant_tabs": {"codex": pending}}

    failures = module.start_participants(
        "herdr",
        str(tmp_path),
        "w-agents",
        tmp_path,
        launcher_state_path(tmp_path),
        state,
        participants=(participant,),
    )

    assert failures == [module.blocked_startup_prompt_guidance(participant)]
    assert state["pending_participant_tabs"]["codex"] == pending
    assert not any(call[:2] in (["tab", "close"], ["tab", "create"]) for call in calls)


def test_missing_blocked_startup_tab_is_cleared_before_a_fresh_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant = module.Participant(role="codex", kind="codex", name="codex-peer")
    pending = {
        "label": "hgchat-codex-trust",
        "pane_id": "w-agents:p-closed",
        "tab_id": "w-agents:t-closed",
        "started_unix_ms": 0,
        "blocked_startup": True,
    }
    other_pending = {"label": "hgchat-other", "started_unix_ms": 0}
    calls: list[list[str]] = []

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        if arguments == ["agent", "list"]:
            return {"result": {"agents": []}}
        if arguments == ["tab", "list", "--workspace", "w-agents"]:
            return {"result": {"tabs": []}}
        if arguments[:2] == ["tab", "create"]:
            return {
                "result": {
                    "tab": {"tab_id": "w-agents:t-fresh"},
                    "root_pane": {"pane_id": "w-agents:p-fresh"},
                }
            }
        if arguments[:2] == ["pane", "process-info"]:
            return {"result": {"process_info": {"foreground_processes": [{"name": "zsh"}]}}}
        if arguments[:2] == ["agent", "start"]:
            return {"result": {"type": "ok"}}
        return {"result": {"type": "ok"}}

    monkeypatch.setattr(module, "run_json", fake_run_json)
    state: dict = {
        "schema_version": 1,
        "pending_participant_tabs": {"codex": pending, "other": other_pending},
    }

    failures = module.start_participants(
        "herdr",
        str(tmp_path),
        "w-agents",
        tmp_path,
        launcher_state_path(tmp_path),
        state,
        participants=(participant,),
    )

    assert failures == []
    assert state["participant_pane_ids"]["codex"] == "w-agents:p-fresh"
    assert state["participant_tab_ids"]["codex"] == "w-agents:t-fresh"
    assert state["pending_participant_tabs"] == {"other": other_pending}
    assert not any(call[:2] == ["tab", "close"] for call in calls)
    assert len([call for call in calls if call[:2] == ["tab", "create"]]) == 1


def test_matching_live_agent_promotes_only_its_exact_pending_tab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant = module.Participant(role="codex", kind="codex", name="codex-peer")
    calls: list[list[str]] = []
    pending = {
        "label": "hgchat-codex-trust",
        "pane_id": "w-agents:p-codex",
        "tab_id": "w-agents:t-codex",
        "started_unix_ms": int(module.time.time() * 1000),
    }

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        if arguments == ["agent", "list"]:
            return {
                "result": {
                    "agents": [
                        {
                            "name": participant.name,
                            "kind": participant.kind,
                            "workspace_id": "w-agents",
                            "cwd": str(tmp_path),
                            "pane_id": pending["pane_id"],
                            "tab_id": pending["tab_id"],
                        }
                    ]
                }
            }
        return {"result": {"type": "ok"}}

    monkeypatch.setattr(module, "run_json", fake_run_json)
    state: dict = {"schema_version": 1, "pending_participant_tabs": {"codex": pending}}

    failures = module.start_participants(
        "herdr",
        str(tmp_path),
        "w-agents",
        tmp_path,
        launcher_state_path(tmp_path),
        state,
        participants=(participant,),
    )

    assert failures == []
    assert state["participant_pane_ids"] == {"codex": pending["pane_id"]}
    assert state["participant_tab_ids"] == {"codex": pending["tab_id"]}
    assert "pending_participant_tabs" not in state
    assert not any(call[:2] in (["tab", "close"], ["pane", "close"]) for call in calls)


def test_recorded_live_agent_closes_a_different_stale_pending_tab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant = module.Participant(role="codex", kind="codex", name="codex-peer")
    calls: list[list[str]] = []
    pending = {
        "label": "hgchat-codex-stale",
        "pane_id": "w-agents:p-stale",
        "tab_id": "w-agents:t-stale",
        "started_unix_ms": int(module.time.time() * 1000),
    }
    recorded_pane_id = "w-agents:p-codex"
    recorded_tab_id = "w-agents:t-codex"

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        if arguments == ["agent", "list"]:
            return {
                "result": {
                    "agents": [
                        {
                            "name": participant.name,
                            "kind": participant.kind,
                            "workspace_id": "w-agents",
                            "cwd": str(tmp_path),
                            "pane_id": recorded_pane_id,
                            "tab_id": recorded_tab_id,
                        }
                    ]
                }
            }
        return {"result": {"type": "ok"}}

    monkeypatch.setattr(module, "run_json", fake_run_json)
    state: dict = {
        "schema_version": 1,
        "participant_pane_ids": {"codex": recorded_pane_id},
        "participant_tab_ids": {"codex": recorded_tab_id},
        "pending_participant_tabs": {"codex": pending},
    }

    failures = module.start_participants(
        "herdr",
        str(tmp_path),
        "w-agents",
        tmp_path,
        launcher_state_path(tmp_path),
        state,
        participants=(participant,),
    )

    assert failures == []
    assert state["participant_pane_ids"] == {"codex": recorded_pane_id}
    assert state["participant_tab_ids"] == {"codex": recorded_tab_id}
    assert "pending_participant_tabs" not in state
    assert ["tab", "close", pending["tab_id"]] in calls


@pytest.mark.parametrize("blocked_startup", [False, True], ids=["ordinary", "blocked-startup"])
def test_exact_live_pending_prompt_preserves_state_when_native_proof_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked_startup: bool
) -> None:
    participant = module.FABLE_PARTICIPANT
    calls: list[list[str]] = []
    pending = {
        "label": "hgchat-fable-trust",
        "pane_id": "w-agents:p-fable",
        "tab_id": "w-agents:t-fable",
        "started_unix_ms": 0,
    }
    if blocked_startup:
        pending["blocked_startup"] = True

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        if arguments == ["agent", "list"]:
            return {
                "result": {
                    "agents": [
                        {
                            "name": participant.name,
                            "kind": participant.kind,
                            "workspace_id": "w-agents",
                            "cwd": str(tmp_path),
                            "pane_id": pending["pane_id"],
                            "tab_id": pending["tab_id"],
                        }
                    ]
                }
            }
        return {"result": {"type": "ok"}}

    monkeypatch.setattr(module, "run_json", fake_run_json)
    monkeypatch.setattr(module, "run_text", lambda *_args, **_kwargs: "Claude Code\n")
    monkeypatch.setattr(module, "VERIFY_PANE_INTERVAL_S", 0)
    state: dict = {"schema_version": 1, "pending_participant_tabs": {"fable": pending}}

    failures = module.start_participants(
        "herdr",
        str(tmp_path),
        "w-agents",
        tmp_path,
        launcher_state_path(tmp_path),
        state,
        participants=(participant,),
    )

    if blocked_startup:
        assert failures == [module.blocked_startup_prompt_guidance(participant)]
    else:
        assert len(failures) == 1 and "failed native-ui verification" in failures[0]
    assert state["pending_participant_tabs"]["fable"] == pending
    assert "participant_pane_ids" not in state
    assert "participant_tab_ids" not in state
    assert not any(call[:2] in (["tab", "close"], ["pane", "close"]) for call in calls)


def test_exact_live_blocked_startup_promotes_after_native_proof_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant = module.FABLE_PARTICIPANT
    pending = {
        "label": "hgchat-fable-trust",
        "pane_id": "w-agents:p-fable",
        "tab_id": "w-agents:t-fable",
        "started_unix_ms": 0,
        "blocked_startup": True,
    }
    calls: list[list[str]] = []

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        if arguments == ["agent", "list"]:
            return {
                "result": {
                    "agents": [
                        {
                            "name": participant.name,
                            "kind": participant.kind,
                            "workspace_id": "w-agents",
                            "cwd": str(tmp_path),
                            "pane_id": pending["pane_id"],
                            "tab_id": pending["tab_id"],
                        }
                    ]
                }
            }
        if arguments[:2] == ["pane", "process-info"]:
            return {"result": {"process_info": {"foreground_processes": CLAUDE_FABLE_PROCESS}}}
        return {"result": {"type": "ok"}}

    monkeypatch.setattr(module, "run_json", fake_run_json)
    monkeypatch.setattr(module, "run_text", lambda *_args, **_kwargs: FABLE_POST_TURN_SCREEN)
    monkeypatch.setattr(module, "VERIFY_PANE_INTERVAL_S", 0)
    state: dict = {"schema_version": 1, "pending_participant_tabs": {"fable": pending}}

    failures = module.start_participants(
        "herdr",
        str(tmp_path),
        "w-agents",
        tmp_path,
        launcher_state_path(tmp_path),
        state,
        participants=(participant,),
    )

    assert failures == []
    assert state["participant_pane_ids"] == {"fable": pending["pane_id"]}
    assert state["participant_tab_ids"] == {"fable": pending["tab_id"]}
    assert "pending_participant_tabs" not in state
    assert not any(call[:2] in (["tab", "close"], ["pane", "close"]) for call in calls)


@pytest.mark.parametrize(
    ("live_pane_id", "live_tab_id"),
    [
        ("w-agents:p-other", "w-agents:t-codex"),
        ("w-agents:p-codex", "w-agents:t-other"),
    ],
)
def test_mismatched_live_agent_is_not_adopted_or_closed_from_pending_tab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live_pane_id: str, live_tab_id: str
) -> None:
    participant = module.Participant(role="codex", kind="codex", name="codex-peer")
    calls: list[list[str]] = []
    pending = {
        "label": "hgchat-codex-trust",
        "pane_id": "w-agents:p-codex",
        "tab_id": "w-agents:t-codex",
        "started_unix_ms": int(module.time.time() * 1000),
    }

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        if arguments == ["agent", "list"]:
            return {
                "result": {
                    "agents": [
                        {
                            "name": participant.name,
                            "kind": participant.kind,
                            "workspace_id": "w-agents",
                            "cwd": str(tmp_path),
                            "pane_id": live_pane_id,
                            "tab_id": live_tab_id,
                        }
                    ]
                }
            }
        return {"result": {"type": "ok"}}

    monkeypatch.setattr(module, "run_json", fake_run_json)
    state: dict = {"schema_version": 1, "pending_participant_tabs": {"codex": pending}}

    failures = module.start_participants(
        "herdr",
        str(tmp_path),
        "w-agents",
        tmp_path,
        launcher_state_path(tmp_path),
        state,
        participants=(participant,),
    )

    assert len(failures) == 1 and "pane not recorded" in failures[0]
    assert state["pending_participant_tabs"]["codex"] == pending
    assert "participant_pane_ids" not in state
    assert "participant_tab_ids" not in state
    assert not any(call[:2] in (["tab", "close"], ["pane", "close"]) for call in calls)


# --- bounded sol-fable model profile -------------------------------------------------

SOL_SCREEN = "Pi\nprovider openai-codex\nmodel gpt-5.6-sol • high\n"
FABLE_SCREEN = "Claude Code\nFable 5\nreasoning: high effort\n"
FABLE_POST_TURN_SCREEN = "Claude Code\nFable 5\nwaiting for input\n"
CLAUDE_FABLE_PROCESS = [
    {
        "argv0": "claude",
        "name": "claude.exe",
        "argv": ["claude", "--model", "fable", "--effort", "high"],
    }
]


def install_profile_host(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[list[str]],
    pane_screens: dict[str, str],
    catalog_rows: dict[tuple[str, str], bool] | None = None,
    live_agents: list[dict] | None = None,
    process_infos: dict[str, list[dict]] | None = None,
    workspaces: list[dict] | None = None,
) -> None:
    """Fake Herdr plus the native Pi catalog for profile participant flows."""
    catalog_rows = catalog_rows if catalog_rows is not None else {}
    live_agents = [] if live_agents is None else live_agents
    workspaces = (
        workspaces
        if workspaces is not None
        else [{"workspace_id": "w-agents", "label": "agents · group-chat"}]
    )

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        if arguments == ["agent", "list"]:
            return {"result": {"agents": live_agents}}
        if arguments == ["workspace", "list"]:
            return {"result": {"workspaces": workspaces}}
        if arguments[:2] == ["tab", "create"]:
            label = arguments[arguments.index("--label") + 1]
            role = label.split("-")[1]
            return {
                "result": {
                    "tab": {"tab_id": f"w-agents:t-{role}"},
                    "root_pane": {"pane_id": f"w-agents:p-{role}"},
                }
            }
        if arguments[:2] == ["pane", "process-info"]:
            pane = arguments[arguments.index("--pane") + 1]
            processes = (process_infos or {}).get(pane)
            if processes is not None:
                return {"result": {"process_info": {"foreground_processes": processes}}}
            return {"result": {"process_info": {"foreground_processes": [{"name": "zsh"}]}}}
        return {"result": {"type": "ok"}}

    def fake_run_text(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> str:
        del timeout
        calls.append(arguments)
        return pane_screens.get(arguments[2], "")

    def fake_catalog(command: object, provider: object, model: object) -> bool:
        assert isinstance(command, tuple)
        calls.append(list(command))
        return catalog_rows.get((str(provider), str(model)), True)

    monkeypatch.setattr(module, "run_json", fake_run_json)
    monkeypatch.setattr(module, "run_text", fake_run_text)
    monkeypatch.setattr(module, "native_catalog_row_present", fake_catalog)
    monkeypatch.setattr(module, "VERIFY_PANE_INTERVAL_S", 0)


def test_sol_fable_start_uses_exact_ordered_participants_and_native_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    install_profile_host(
        monkeypatch,
        calls,
        {"w-agents:p-sol": SOL_SCREEN, "w-agents:p-fable": FABLE_SCREEN},
    )
    state: dict = {"schema_version": 1}

    failures = module.start_participants(
        "herdr",
        str(tmp_path),
        "w-agents",
        tmp_path,
        launcher_state_path(tmp_path),
        state,
        participants=module.resolve_profile("sol-fable"),
    )

    assert failures == []
    starts = [call for call in calls if call[:2] == ["agent", "start"]]
    assert starts == [
        [
            "agent",
            "start",
            "sol-peer",
            "--kind",
            "pi",
            "--pane",
            "w-agents:p-sol",
            "--timeout",
            "120000",
            "--",
            "--provider",
            "openai-codex",
            "--model",
            "gpt-5.6-sol",
            "--thinking",
            "high",
        ],
        [
            "agent",
            "start",
            "fable-peer",
            "--kind",
            "claude",
            "--pane",
            "w-agents:p-fable",
            "--timeout",
            "120000",
            "--",
            "--model",
            "fable",
            "--effort",
            "high",
        ],
    ]
    # The catalog proof precedes every Sol tab creation and agent start.
    first_sol_tab = next(i for i, call in enumerate(calls) if call[:2] == ["tab", "create"])
    sol_catalog = calls.index(["pi", "--list-models", "gpt-5.6-sol"])
    assert sol_catalog < first_sol_tab


def test_catalog_proof_is_structural_not_substring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def with_stdout(stdout: str, returncode: int = 0):
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")

    def install(stdout: str, returncode: int = 0) -> None:
        monkeypatch.setattr(
            module.subprocess,
            "run",
            lambda *_args, **_kwargs: with_stdout(stdout, returncode),
        )

    catalog = module.native_catalog_row_present
    install("openai-codex  gpt-5.6-sol\n")
    assert catalog(("pi",), "openai-codex", "gpt-5.6-sol")
    install("  openai-codex  gpt-5.6-sol  extra columns\n")
    assert catalog(("pi",), "openai-codex", "gpt-5.6-sol")
    install("openai-codex  gpt-5.6-sol-01\n")  # suffixed model
    assert not catalog(("pi",), "openai-codex", "gpt-5.6-sol")
    install("xopenai-codex  gpt-5.6-sol\n")  # prefixed provider
    assert not catalog(("pi",), "openai-codex", "gpt-5.6-sol")
    install("openai-codex-remote  gpt-5.6-sol\n")
    assert not catalog(("pi",), "openai-codex", "gpt-5.6-sol")
    install("openai-codex  gpt-5.6-sol\n", returncode=1)
    assert not catalog(("pi",), "openai-codex", "gpt-5.6-sol")


def test_pane_proof_is_bounded_token_and_sequence_matching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def install(screen: str) -> None:
        calls.clear()

        def fake_run_text(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> str:
            del timeout
            calls.append(arguments)
            return screen

        monkeypatch.setattr(module, "run_text", fake_run_text)

    def proves(screen: str, proofs: object) -> bool:
        install(screen)
        monkeypatch.setattr(module, "VERIFY_PANE_INTERVAL_S", 0)
        assert isinstance(proofs, tuple)
        return module.native_pane_proves("herdr", "w-agents:p-x", proofs)

    sol = (module.SOL_PANE_PROOF,)
    fable = module.FABLE_PANE_PROOFS
    assert proves("model gpt-5.6-sol • high\n", sol)
    assert proves("model: gpt-5.6-sol · high (local)\n", sol)
    assert not proves("model gpt-5.6-sol-01 • high\n", sol)  # suffixed model identifier
    assert not proves("model gpt-5.6-sol • highest\n", sol)  # suffixed effort token
    assert not proves("provider gpt-5.6-sol openai-codex • high\n", sol)
    assert proves("Claude Code\nFable 5\nreasoning: high effort\n", fable)
    assert not proves("Claude Code\nFable 5-deluxe\nhigh effort\n", fable)
    assert not proves("Claude Code\nFable 5\nreasoning: effortful\n", fable)
    assert not proves("Claude Code\nPrefable 5\nhigh effort\n", fable)
    # Retries stay bounded when evidence never renders.
    assert not proves("", sol)
    assert len(calls) == module.VERIFY_PANE_ATTEMPTS


def test_pane_proof_retries_because_startup_uis_render_asynchronously(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    screens = ["", FABLE_SCREEN]  # evidence appears on the second read

    def fake_run_text(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> str:
        del timeout
        calls.append(arguments)
        return screens.pop(0) if screens else FABLE_SCREEN

    monkeypatch.setattr(module, "run_text", fake_run_text)
    monkeypatch.setattr(module, "VERIFY_PANE_INTERVAL_S", 0)

    assert module.native_pane_proves("herdr", "w-agents:p-fable", module.FABLE_PANE_PROOFS)
    assert len(calls) == 2


def test_failed_catalog_preflight_creates_and_starts_no_sol_tab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    install_profile_host(
        monkeypatch,
        calls,
        {"w-agents:p-sol": SOL_SCREEN, "w-agents:p-fable": FABLE_SCREEN},
        catalog_rows={("openai-codex", "gpt-5.6-sol"): False},
    )
    state: dict = {"schema_version": 1}

    failures = module.start_participants(
        "herdr",
        str(tmp_path),
        "w-agents",
        tmp_path,
        launcher_state_path(tmp_path),
        state,
        participants=module.resolve_profile("sol-fable"),
    )

    assert any("catalog preflight" in failure and "@sol" in failure for failure in failures)
    sol_calls = [call for call in calls if "sol" in " ".join(call)]
    assert not any(call[:2] in (["tab", "create"], ["agent", "start"]) for call in sol_calls)
    # Fable still started: the preflight is per-role and precedes only Sol's tab.
    assert any(call[:2] == ["agent", "start"] and call[2] == "fable-peer" for call in calls)


def test_new_tab_failing_pane_verification_is_closed_and_not_routable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    install_profile_host(monkeypatch, calls, {"w-agents:p-fable": "Claude Code\n"})
    state: dict = {"schema_version": 1}

    failures = module.start_participants(
        "herdr",
        str(tmp_path),
        "w-agents",
        tmp_path,
        launcher_state_path(tmp_path),
        state,
        participants=(module.PROFILE_PARTICIPANTS["sol-fable"][1],),
    )

    assert len(failures) == 1 and "the new tab was closed" in failures[0]
    assert ["tab", "close", "w-agents:t-fable"] in calls
    assert "fable" not in state.get("participant_pane_ids", {})
    assert "fable" not in state.get("participant_tab_ids", {})
    assert "fable" not in state.get("pending_participant_tabs", {})


def test_agent_start_passes_native_arguments_behind_exactly_one_separator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        return {"result": {"type": "ok"}}

    monkeypatch.setattr(module, "run_json", fake_run_json)

    sol, fable = module.resolve_profile("sol-fable")
    module.start_agent("herdr", "sol-peer", sol.kind, "w-agents:p-sol", sol.start_args)
    module.start_agent("herdr", "fable-peer", fable.kind, "w-agents:p-fable", fable.start_args)
    # A default participant with no native arguments adds no separator.
    module.start_agent("herdr", "pi-peer", "pi", "w-agents:p-pi")

    starts = [call for call in calls if call[:2] == ["agent", "start"]]
    assert starts == [
        [
            "agent",
            "start",
            "sol-peer",
            "--kind",
            "pi",
            "--pane",
            "w-agents:p-sol",
            "--timeout",
            "120000",
            "--",
            "--provider",
            "openai-codex",
            "--model",
            "gpt-5.6-sol",
            "--thinking",
            "high",
        ],
        [
            "agent",
            "start",
            "fable-peer",
            "--kind",
            "claude",
            "--pane",
            "w-agents:p-fable",
            "--timeout",
            "120000",
            "--",
            "--model",
            "fable",
            "--effort",
            "high",
        ],
        [
            "agent",
            "start",
            "pi-peer",
            "--kind",
            "pi",
            "--pane",
            "w-agents:p-pi",
            "--timeout",
            "120000",
        ],
    ]


def test_default_participants_stay_unverified_and_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "PARTICIPANTS", (("pi", "pi-peer"),))

    def unexpected_text(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("default participants must not read native panes")

    def unexpected_catalog(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("default participants must not query native catalogs")

    monkeypatch.setattr(module, "run_text", unexpected_text)
    monkeypatch.setattr(module, "native_catalog_row_present", unexpected_catalog)
    calls: list[list[str]] = []

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        if arguments == ["agent", "list"]:
            return {
                "result": {
                    "agents": [
                        {
                            "name": "pi-peer",
                            "kind": "pi",
                            "workspace_id": "w-agents",
                            "cwd": str(tmp_path),
                            "pane_id": "w-agents:p1",
                            "tab_id": "w-agents:t1",
                        }
                    ]
                }
            }
        return {"result": {"type": "ok"}}

    monkeypatch.setattr(module, "run_json", fake_run_json)
    state: dict = {
        "schema_version": 1,
        "participant_pane_ids": {"pi": "w-agents:p1"},
        "participant_tab_ids": {"pi": "w-agents:t1"},
    }

    failures = module.start_participants(
        "herdr", str(tmp_path), "w-agents", tmp_path, launcher_state_path(tmp_path), state
    )

    assert failures == []


def profile_launch_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured: dict[str, object]
) -> None:
    """The default launch_env context, plus profile env hygiene and capture."""
    launch_env(tmp_path, monkeypatch, captured)
    monkeypatch.delenv(module.PROFILE_ENV, raising=False)
    monkeypatch.delenv(module.PROFILE_RECEIPT_ENV, raising=False)


def install_launch_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pane_screens: dict[str, str],
    catalog_rows: dict[tuple[str, str], bool] | None = None,
) -> list[list[str]]:
    """Wire a fake Herdr for main() with a pre-existing agents workspace/agents."""
    calls: list[list[str]] = []
    live = [
        {
            "name": "sol-peer",
            "kind": "pi",
            "workspace_id": "w-agents",
            "cwd": str(tmp_path),
            "pane_id": "w-agents:p-sol",
            "tab_id": "w-agents:t-sol",
        },
        {
            "name": "fable-peer",
            "kind": "claude",
            "workspace_id": "w-agents",
            "cwd": str(tmp_path),
            "pane_id": "w-agents:p-fable",
            "tab_id": "w-agents:t-fable",
        },
    ]
    install_profile_host(
        monkeypatch,
        calls,
        pane_screens,
        catalog_rows=catalog_rows,
        live_agents=live,
        process_infos={"w-agents:p-fable": CLAUDE_FABLE_PROCESS},
    )
    return calls


def profile_room_state(tmp_path: Path, room: str = "chat-profile") -> dict:
    return {
        "schema_version": 1,
        "agents_workspace_id": "w-agents",
        "agents_cwd": str(tmp_path),
        "participant_pane_ids": {"sol": "w-agents:p-sol", "fable": "w-agents:p-fable"},
        "participant_tab_ids": {"sol": "w-agents:t-sol", "fable": "w-agents:t-fable"},
        "pending_room_id": room,
        "pending_room_operation_id": "operation-test",
        "pending_room_profile": "sol-fable",
        "pending_room_started_unix_ms": int(module.time.time() * 1000),
    }


def test_main_execs_profile_room_only_after_complete_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    profile_launch_env(tmp_path, monkeypatch, captured)
    monkeypatch.setenv(module.PROFILE_ENV, "sol-fable")
    monkeypatch.setenv(module.ROOM_ENV, "chat-profile")
    calls = install_launch_host(
        tmp_path, monkeypatch, {"w-agents:p-sol": SOL_SCREEN, "w-agents:p-fable": FABLE_SCREEN}
    )
    save_launcher_state(tmp_path, profile_room_state(tmp_path))

    module.main()

    # The verified existing sessions are reused and re-verified, not restarted.
    assert not any(call[:2] in (["tab", "create"], ["agent", "start"]) for call in calls)
    assert ["pi", "--list-models", "gpt-5.6-sol"] in calls
    argv = captured["argv"]
    assert argv[argv.index("--profile") + 1] == "sol-fable"
    mappings = [value for index, value in enumerate(argv) if argv[index - 1] == "--agent"]
    assert mappings == ["sol=sol-peer", "fable=fable-peer"]
    assert argv[argv.index("--synthesizer") + 1] == "sol"
    receipt = json.loads(captured["env"][module.PROFILE_RECEIPT_ENV])
    assert receipt["profile"] == "sol-fable"
    assert {entry["role"] for entry in receipt["verified"]} == {"sol", "fable"}
    state = load_launcher_state(tmp_path)
    assert state["selected_profile"] == "sol-fable"
    assert state["last_room_id"] == "chat-profile"


def test_main_never_execs_a_profile_room_when_one_role_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    profile_launch_env(tmp_path, monkeypatch, captured)
    monkeypatch.setenv(module.PROFILE_ENV, "sol-fable")
    monkeypatch.setenv(module.ROOM_ENV, "chat-profile")
    calls = install_launch_host(
        tmp_path,
        monkeypatch,
        # Sol's pane shows a suffixed model identifier: verification fails.
        {"w-agents:p-sol": "Pi\nmodel gpt-5.6-sol-01 • high\n", "w-agents:p-fable": FABLE_SCREEN},
    )
    save_launcher_state(tmp_path, profile_room_state(tmp_path))

    with pytest.raises(BootstrapError, match="requires every participant"):
        module.main()

    assert "argv" not in captured
    assert "selected_profile" not in load_launcher_state(tmp_path)
    # The mismatched existing Sol session is excluded without being closed.
    mutations = (("tab", "close"), ("pane", "close"), ("agent", "send-keys"))
    assert not any(tuple(call[:2]) in mutations for call in calls)


def test_main_never_execs_a_profile_room_when_both_roles_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    profile_launch_env(tmp_path, monkeypatch, captured)
    monkeypatch.setenv(module.PROFILE_ENV, "sol-fable")
    monkeypatch.setenv(module.ROOM_ENV, "chat-profile")
    install_launch_host(
        tmp_path,
        monkeypatch,
        {"w-agents:p-sol": "Pi\n", "w-agents:p-fable": "Claude Code\n"},
    )
    save_launcher_state(tmp_path, profile_room_state(tmp_path))

    with pytest.raises(BootstrapError, match="requires every participant"):
        module.main()

    assert "argv" not in captured
    assert "selected_profile" not in load_launcher_state(tmp_path)


def test_main_default_room_exec_carries_no_profile_and_clears_stale_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    launch_env(tmp_path, monkeypatch, captured)
    monkeypatch.setenv(module.ROOM_ENV, "chat-default")
    install_launch_host(
        tmp_path, monkeypatch, {"w-agents:p-sol": SOL_SCREEN, "w-agents:p-fable": FABLE_SCREEN}
    )
    state = profile_room_state(tmp_path, room="chat-default")
    state.pop("pending_room_profile")
    state["selected_profile"] = "sol-fable"
    state["last_room_id"] = "chat-older"
    save_launcher_state(tmp_path, state)

    module.main()

    argv = captured["argv"]
    assert "--profile" not in argv and "--agent" not in argv and "--synthesizer" not in argv
    final_state = load_launcher_state(tmp_path)
    assert "selected_profile" not in final_state


def room_reopen_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured: dict[str, object], room: str
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w-chat")
    monkeypatch.setenv("HERDR_TAB_ID", "w-chat:t-room")
    monkeypatch.setenv("HERDR_PANE_ID", "w-chat:p-room")
    monkeypatch.setenv(module.ROOM_OPERATION_ENV, "operation-reopen")
    monkeypatch.setenv(module.ROOM_ENV, room)
    monkeypatch.delenv(module.PROFILE_RECEIPT_ENV, raising=False)
    monkeypatch.setattr(
        module.os, "execv", lambda path, argv: captured.update(path=path, argv=argv)
    )


def test_room_reopen_reverifies_and_execs_with_receipt_and_mappings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    room_reopen_env(tmp_path, monkeypatch, captured, "chat-profile")
    calls = install_launch_host(
        tmp_path, monkeypatch, {"w-agents:p-sol": SOL_SCREEN, "w-agents:p-fable": FABLE_SCREEN}
    )
    state = profile_room_state(tmp_path)
    state.pop("pending_room_id")
    state.pop("pending_room_operation_id")
    state.pop("pending_room_profile")
    state.pop("pending_room_started_unix_ms")
    state["selected_profile"] = "sol-fable"
    state["last_room_id"] = "chat-profile"
    save_launcher_state(tmp_path, state)

    module.room_entrypoint()

    argv = captured["argv"]
    assert argv[argv.index("--profile") + 1] == "sol-fable"
    mappings = [value for index, value in enumerate(argv) if argv[index - 1] == "--agent"]
    assert mappings == ["sol=sol-peer", "fable=fable-peer"]
    assert argv[argv.index("--synthesizer") + 1] == "sol"
    receipt = json.loads(os.environ[module.PROFILE_RECEIPT_ENV])
    assert receipt == module.profile_receipt_payload(
        "sol-fable", module.resolve_profile("sol-fable")
    )
    # Reopen only re-verifies: nothing is started or closed.
    assert not any(
        call[:2] in (["agent", "start"], ["tab", "close"], ["pane", "close"]) for call in calls
    )


def test_profile_reverify_accepts_foreground_cwd_and_checks_native_pane_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant = module.FABLE_PARTICIPANT
    pane_id = "w-agents:p-fable"
    tab_id = "w-agents:t-fable"
    calls: list[list[str]] = []

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        if arguments == ["agent", "list"]:
            return {
                "result": {
                    "agents": [
                        {
                            "name": participant.name,
                            "kind": participant.kind,
                            "workspace_id": "w-agents",
                            "foreground_cwd": str(tmp_path),
                            "pane_id": pane_id,
                            "tab_id": tab_id,
                        }
                    ]
                }
            }
        if arguments[:2] == ["pane", "process-info"]:
            return {"result": {"process_info": {"foreground_processes": CLAUDE_FABLE_PROCESS}}}
        return {"result": {"type": "ok"}}

    monkeypatch.setattr(module, "run_json", fake_run_json)
    monkeypatch.setattr(module, "run_text", lambda *_args, **_kwargs: FABLE_POST_TURN_SCREEN)
    monkeypatch.setattr(module, "VERIFY_PANE_INTERVAL_S", 0)
    state = {
        "schema_version": 1,
        "participant_pane_ids": {"fable": pane_id},
        "participant_tab_ids": {"fable": tab_id},
    }

    module.reverify_profile_participants("herdr", "w-agents", str(tmp_path), state, (participant,))

    assert ["pane", "process-info", "--pane", pane_id] in calls


def test_room_reopen_fails_closed_on_a_renamed_or_replaced_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    room_reopen_env(tmp_path, monkeypatch, captured, "chat-profile")
    calls = install_launch_host(
        tmp_path, monkeypatch, {"w-agents:p-sol": SOL_SCREEN, "w-agents:p-fable": FABLE_SCREEN}
    )
    # The live Sol agent moved to a new pane: the recorded pane is stale.
    calls.clear()
    original = module.run_json

    def shifted_agents(herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        result = original(herdr_bin, arguments, timeout)
        if arguments == ["agent", "list"]:
            for agent in result["result"]["agents"]:
                if agent["name"] == "sol-peer":
                    agent["pane_id"] = "w-agents:p-replaced"
        return result

    monkeypatch.setattr(module, "run_json", shifted_agents)
    state = profile_room_state(tmp_path)
    state.pop("pending_room_id")
    state.pop("pending_room_operation_id")
    state.pop("pending_room_profile")
    state.pop("pending_room_started_unix_ms")
    state["selected_profile"] = "sol-fable"
    state["last_room_id"] = "chat-profile"
    save_launcher_state(tmp_path, state)

    with pytest.raises(BootstrapError, match="re-verification"):
        module.room_entrypoint()

    assert "argv" not in captured
    assert not any(call[:2] in (["agent", "start"], ["tab", "close"]) for call in calls)


def test_room_reopen_fails_closed_when_native_evidence_lapsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    room_reopen_env(tmp_path, monkeypatch, captured, "chat-profile")
    install_launch_host(
        tmp_path,
        monkeypatch,
        {"w-agents:p-sol": "Pi\nmodel gpt-5.6-sol-01 • high\n", "w-agents:p-fable": FABLE_SCREEN},
    )
    state = profile_room_state(tmp_path)
    state.pop("pending_room_id")
    state.pop("pending_room_operation_id")
    state.pop("pending_room_profile")
    state.pop("pending_room_started_unix_ms")
    state["selected_profile"] = "sol-fable"
    state["last_room_id"] = "chat-profile"
    save_launcher_state(tmp_path, state)

    with pytest.raises(BootstrapError, match=r"@sol.*re-verification"):
        module.room_entrypoint()

    assert "argv" not in captured


def test_fresh_fable_proof_still_requires_the_high_effort_banner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh launch keeps the strict two-sequence pane proof."""
    monkeypatch.setattr(module, "VERIFY_PANE_INTERVAL_S", 0)
    monkeypatch.setattr(
        module, "run_text", lambda _herdr_bin, arguments, timeout=30: FABLE_POST_TURN_SCREEN
    )
    fable = module.resolve_profile("sol-fable")[1]

    assert not module.pane_proof("herdr", fable, "w-agents:p-fable")
    # The bounded reopen pane sequence alone must not satisfy the fresh proof.
    assert not module.native_pane_proves("herdr", "w-agents:p-fable", module.FABLE_PANE_PROOFS)


def test_reopen_accepts_post_turn_fable_status_with_exact_process_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real reopen contract: bounded `Fable 5` status plus native claude argv."""
    captured: dict[str, object] = {}
    room_reopen_env(tmp_path, monkeypatch, captured, "chat-profile")
    calls = install_launch_host(
        tmp_path,
        monkeypatch,
        {"w-agents:p-sol": SOL_SCREEN, "w-agents:p-fable": FABLE_POST_TURN_SCREEN},
    )
    state = profile_room_state(tmp_path)
    state.pop("pending_room_id")
    state.pop("pending_room_operation_id")
    state.pop("pending_room_profile")
    state.pop("pending_room_started_unix_ms")
    state["selected_profile"] = "sol-fable"
    state["last_room_id"] = "chat-profile"
    save_launcher_state(tmp_path, state)

    module.room_entrypoint()

    receipt = json.loads(os.environ[module.PROFILE_RECEIPT_ENV])
    assert receipt["profile"] == "sol-fable"
    # Only Fable is process-verified; Sol keeps catalog + pane proof alone.
    process_panes = {
        call[call.index("--pane") + 1] for call in calls if call[:2] == ["pane", "process-info"]
    }
    assert process_panes == {"w-agents:p-fable"}


@pytest.mark.parametrize(
    "process",
    [
        pytest.param(
            {"argv0": "claude", "argv": ["claude", "--model", "fable", "--effort", "high"]},
            id="argv0-herdr-0.8.2",
        ),
        pytest.param(
            {"name": "claude", "argv": ["claude", "--model", "fable", "--effort", "high"]},
            id="name-protocol-21",
        ),
        pytest.param(
            {"argv": ["claude", "--model", "fable", "--effort", "high"]},
            id="argv-only",
        ),
    ],
)
def test_reopen_fable_process_proof_accepts_each_herdr_identity_field(
    monkeypatch: pytest.MonkeyPatch, process: dict
) -> None:
    monkeypatch.setattr(module, "VERIFY_PANE_INTERVAL_S", 0)
    monkeypatch.setattr(
        module,
        "run_json",
        lambda _herdr_bin, arguments, timeout=30: (
            {"result": {"process_info": {"foreground_processes": [process]}}}
            if arguments[:2] == ["pane", "process-info"]
            else (_ for _ in ()).throw(AssertionError(arguments))
        ),
    )
    assert module.native_pane_process_proves(
        "herdr", "w-agents:p-fable", "claude", ("--model", "fable", "--effort", "high")
    )


@pytest.mark.parametrize(
    "processes",
    [
        pytest.param([], id="missing-process"),
        pytest.param(
            [{"argv0": "claude", "argv": ["claude", "--model", "fable"]}],
            id="missing-effort",
        ),
        pytest.param(
            [{"argv0": "claude", "argv": ["claude", "--model", "fable", "--effort", "medium"]}],
            id="wrong-effort",
        ),
        pytest.param(
            [{"argv0": "claude", "argv": ["claude", "--effort", "high", "--model", "fable"]}],
            id="reordered-args",
        ),
        pytest.param(
            [
                {
                    "argv0": "claude",
                    "argv": ["claude", "--model", "fable", "--session", "x", "--effort", "high"],
                }
            ],
            id="non-contiguous-args",
        ),
        pytest.param(
            [{"argv0": "claude-code", "argv": ["claude", "--model", "fable", "--effort", "high"]}],
            id="wrong-argv0",
        ),
        pytest.param(
            [{"name": "claude-code", "argv": ["claude", "--model", "fable", "--effort", "high"]}],
            id="wrong-name-protocol-21",
        ),
        pytest.param(
            [{"argv0": "claude", "argv": ["claude", "--model", "fable-5", "--effort", "high"]}],
            id="model-lookalike",
        ),
        pytest.param(
            [
                {
                    "argv0": "node",
                    "argv": ["node", "mcp", "--model", "fable", "--effort", "high"],
                }
            ],
            id="mcp-child",
        ),
    ],
)
def test_reopen_fable_process_proof_fails_closed_on_inexact_evidence(
    monkeypatch: pytest.MonkeyPatch, processes: list[dict]
) -> None:
    monkeypatch.setattr(module, "VERIFY_PANE_INTERVAL_S", 0)
    monkeypatch.setattr(
        module,
        "run_json",
        lambda _herdr_bin, arguments, timeout=30: (
            {"result": {"process_info": {"foreground_processes": processes}}}
            if arguments[:2] == ["pane", "process-info"]
            else (_ for _ in ()).throw(AssertionError(arguments))
        ),
    )
    monkeypatch.setattr(
        module, "run_text", lambda _herdr_bin, arguments, timeout=30: FABLE_POST_TURN_SCREEN
    )
    fable = module.resolve_profile("sol-fable")[1]

    assert not module.reopen_pane_proof("herdr", fable, "w-agents:p-fable")


def test_reopen_rejects_a_fable_pane_without_bounded_model_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "VERIFY_PANE_INTERVAL_S", 0)
    monkeypatch.setattr(
        module,
        "run_json",
        lambda _herdr_bin, arguments, timeout=30: (
            {"result": {"process_info": {"foreground_processes": CLAUDE_FABLE_PROCESS}}}
            if arguments[:2] == ["pane", "process-info"]
            else (_ for _ in ()).throw(AssertionError(arguments))
        ),
    )
    monkeypatch.setattr(
        module, "run_text", lambda _herdr_bin, arguments, timeout=30: "Claude Code\nwaiting\n"
    )
    fable = module.resolve_profile("sol-fable")[1]

    assert not module.reopen_pane_proof("herdr", fable, "w-agents:p-fable")


def test_sol_reopen_proof_never_requires_process_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        module,
        "run_json",
        lambda _herdr_bin, arguments, timeout=30: (
            (_ for _ in ()).throw(AssertionError("Sol reopen must not read process info"))
            if arguments[:2] == ["pane", "process-info"]
            else {"result": {"type": "ok"}}
        ),
    )
    monkeypatch.setattr(module, "run_text", lambda _herdr_bin, arguments, timeout=30: SOL_SCREEN)
    monkeypatch.setattr(
        module,
        "native_catalog_row_present",
        lambda command, provider, model: True,
    )
    sol = module.resolve_profile("sol-fable")[0]

    assert module.reopen_pane_proof("herdr", sol, "w-agents:p-sol")


def test_room_reopen_rejects_stale_cross_room_profile_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    room_reopen_env(tmp_path, monkeypatch, captured, "chat-newer")
    install_launch_host(
        tmp_path, monkeypatch, {"w-agents:p-sol": SOL_SCREEN, "w-agents:p-fable": FABLE_SCREEN}
    )
    state = profile_room_state(tmp_path)
    state.pop("pending_room_id")
    state.pop("pending_room_operation_id")
    state.pop("pending_room_profile")
    state.pop("pending_room_started_unix_ms")
    state["selected_profile"] = "sol-fable"
    state["last_room_id"] = "chat-profile"  # bound to a different room
    save_launcher_state(tmp_path, state)

    with pytest.raises(BootstrapError, match="different room"):
        module.room_entrypoint()

    assert "argv" not in captured


def test_room_reopen_without_a_profile_skips_reverification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    room_reopen_env(tmp_path, monkeypatch, captured, "chat-default")
    calls = install_launch_host(
        tmp_path, monkeypatch, {"w-agents:p-sol": SOL_SCREEN, "w-agents:p-fable": FABLE_SCREEN}
    )
    save_launcher_state(tmp_path, {"agents_workspace_id": "w-agents"})

    module.room_entrypoint()

    argv = captured["argv"]
    assert "--profile" not in argv and "--agent" not in argv
    # No agent listing or pane read happens for a default reopen.
    assert not any(call[:2] in (["agent", "list"], ["pane", "read"]) for call in calls)


def test_room_reopen_consumes_its_matching_pending_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inner room entrypoint claims the pane left pending by the outer open."""
    captured: dict[str, object] = {}
    room_reopen_env(tmp_path, monkeypatch, captured, "chat-default")
    install_launch_host(
        tmp_path, monkeypatch, {"w-agents:p-sol": SOL_SCREEN, "w-agents:p-fable": FABLE_SCREEN}
    )
    save_launcher_state(
        tmp_path,
        {
            "agents_workspace_id": "w-agents",
            "pending_room_id": "chat-default",
            "pending_room_operation_id": "operation-reopen",
            "pending_room_started_unix_ms": int(module.time.time() * 1000),
        },
    )

    module.room_entrypoint()

    assert captured["argv"][0].endswith("herdr-group-chat")
    state = load_launcher_state(tmp_path)
    assert state["room_pane_id"] == "w-chat:p-room"
    for field in (
        "pending_room_id",
        "pending_room_operation_id",
        "pending_room_profile",
        "pending_room_started_unix_ms",
    ):
        assert field not in state


def test_room_reopen_rejects_mismatched_pending_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reopen from a different operation never consumes another launch's claim."""
    captured: dict[str, object] = {}
    room_reopen_env(tmp_path, monkeypatch, captured, "chat-default")
    monkeypatch.setattr(
        module,
        "run_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must fail first")),
    )
    save_launcher_state(
        tmp_path,
        {
            "pending_room_id": "chat-default",
            "pending_room_operation_id": "operation-newer",
            "pending_room_started_unix_ms": int(module.time.time() * 1000),
        },
    )

    with pytest.raises(BootstrapError, match="superseded by a newer operation"):
        module.room_entrypoint()

    state = load_launcher_state(tmp_path)
    assert state["pending_room_operation_id"] == "operation-newer"
    assert "room_pane_id" not in state
    assert "argv" not in captured


def test_launch_room_with_profile_propagates_env_and_pending_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_launcher_state(tmp_path, {"chat_workspace_id": "w-chat"})
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HERDR_PLUGIN_CONTEXT_JSON", json.dumps({"focused_pane_cwd": str(tmp_path)}))
    calls: list[list[str]] = []

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        del timeout
        calls.append(arguments)
        if arguments == ["workspace", "list"]:
            return {"result": {"workspaces": [{"workspace_id": "w-chat"}]}}
        if arguments[:3] == ["plugin", "pane", "open"]:
            return {
                "result": {
                    "plugin_pane": {"pane": {"pane_id": "w-chat:p-new", "tab_id": "w-chat:t-new"}}
                }
            }
        return {"result": {"type": "tab_info"}}

    monkeypatch.setattr(module, "run_json", fake_run_json)

    assert launch_room(open_existing=False, profile="sol-fable") == 0
    open_arguments = next(call for call in calls if call[:3] == ["plugin", "pane", "open"])
    assert f"{module.PROFILE_ENV}=sol-fable" in open_arguments
    state = load_launcher_state(tmp_path)
    assert state["room_pane_id"] == "w-chat:p-new"
    # The outer registration records pane ownership but retains the pending
    # operation/profile for the inner entrypoint to consume after the lock.
    operation = next(
        setting.split("=", 1)[1]
        for setting in open_arguments
        if setting.startswith(f"{module.ROOM_OPERATION_ENV}=")
    )
    assert state["pending_room_operation_id"] == operation
    assert state["pending_room_profile"] == "sol-fable"
    assert state["pending_room_id"].startswith("chat-")
    assert state["last_room_id"] == state["pending_room_id"]


def test_launch_then_inner_claim_completes_the_two_phase_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the outer launch must not clear the pending profile binding
    before the inner new-room process claims it after acquiring the lock."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HERDR_PLUGIN_CONTEXT_JSON", json.dumps({"focused_pane_cwd": str(tmp_path)}))
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w-chat")
    monkeypatch.setenv("HERDR_TAB_ID", "w-chat:p-room:t")
    monkeypatch.setenv("HERDR_PANE_ID", "w-chat:p-room")
    monkeypatch.setenv(module.PROFILE_ENV, "sol-fable")
    monkeypatch.setenv(module.ROOM_ENV, "chat-profile")
    monkeypatch.delenv(module.PROFILE_RECEIPT_ENV, raising=False)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        module.os,
        "execv",
        lambda path, argv: captured.update(path=path, argv=argv, env=dict(os.environ)),
    )
    install_profile_host(
        monkeypatch,
        [],
        {"w-agents:p-sol": SOL_SCREEN, "w-agents:p-fable": FABLE_SCREEN},
    )
    original_run_json = module.run_json

    def fake_run_json(herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        if arguments[:3] == ["plugin", "pane", "open"]:
            operation = next(
                setting.split("=", 1)[1]
                for setting in arguments
                if setting.startswith(f"{module.ROOM_OPERATION_ENV}=")
            )
            # The inner process starts concurrently and observes the retained
            # pending operation/profile once it acquires the lock.
            monkeypatch.setenv(module.ROOM_OPERATION_ENV, operation)
            return {
                "result": {
                    "plugin_pane": {
                        "pane": {"pane_id": "w-chat:p-room", "tab_id": "w-chat:p-room:t"}
                    }
                }
            }
        result = original_run_json(herdr_bin, arguments, timeout)
        if arguments == ["workspace", "list"]:
            result["result"]["workspaces"].insert(
                0, {"workspace_id": "w-chat", "label": "group-chat"}
            )
        return result

    monkeypatch.setattr(module, "run_json", fake_run_json)
    save_launcher_state(
        tmp_path, {"chat_workspace_id": "w-chat", "agents_workspace_id": "w-agents"}
    )

    launch_room(open_existing=False, profile="sol-fable")
    # The inner claim now succeeds instead of failing with profile_operation_mismatch.
    module.main()

    assert captured["argv"][0].endswith("herdr-group-chat")
    state = load_launcher_state(tmp_path)
    assert state["selected_profile"] == "sol-fable"
    for field in (
        "pending_room_id",
        "pending_room_operation_id",
        "pending_room_profile",
        "pending_room_started_unix_ms",
    ):
        assert field not in state


def test_unknown_profile_fails_before_opening_any_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_call(*_args: object, **_kwargs: object) -> dict:
        raise AssertionError("an unknown profile must fail before any Herdr call")

    monkeypatch.setattr(module, "run_json", unexpected_call)

    with pytest.raises(BootstrapError, match="unknown profile: bogus"):
        launch_room(open_existing=False, profile="bogus")


def test_profile_must_be_tied_to_the_pending_room_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    profile_launch_env(tmp_path, monkeypatch, captured)
    monkeypatch.setenv(module.PROFILE_ENV, "sol-fable")
    save_launcher_state(
        tmp_path,
        {
            "pending_room_id": "chat-pending",
            "pending_room_operation_id": "operation-test",
            "pending_room_profile": "default-ish",
            "pending_room_started_unix_ms": int(module.time.time() * 1000),
        },
    )

    def unexpected_call(*_args: object, **_kwargs: object) -> dict:
        raise AssertionError("a cross-wired profile must fail before any Herdr mutation")

    monkeypatch.setattr(module, "run_json", unexpected_call)

    with pytest.raises(BootstrapError, match="not tied to this launch operation"):
        module.main()

    assert "argv" not in captured


def test_launch_argument_parsing_is_exact() -> None:
    assert module.parse_launch_arguments([]) == ("setup", None)
    assert module.parse_launch_arguments(["--launch"]) == ("--launch", None)
    assert module.parse_launch_arguments(["--launch", "--profile", "sol-fable"]) == (
        "--launch",
        "sol-fable",
    )
    assert module.parse_launch_arguments(["--open"]) == ("--open", None)
    with pytest.raises(BootstrapError, match="only applies to --launch"):
        module.parse_launch_arguments(["--open", "--profile", "sol-fable"])
    with pytest.raises(BootstrapError, match="requires a profile name"):
        module.parse_launch_arguments(["--launch", "--profile"])
    with pytest.raises(BootstrapError, match="unknown arguments"):
        module.parse_launch_arguments(["--launch", "--profile", "sol-fable", "extra"])
    with pytest.raises(BootstrapError, match="unknown arguments"):
        module.parse_launch_arguments(["--frobnicate"])


# --- default sol-fable-grok profile ---------------------------------------------------

GROK_SCREEN = "Grok CLI\nmodel Grok 4.6\nreasoning effort: high\n"
GROK_POST_TURN_SCREEN = "Grok CLI\nGrok 4.6 (high)\nwaiting for input\n"
GROK_PROCESS = [
    {
        "argv0": "grok",
        "name": "grok.exe",
        "argv": [
            "grok",
            "--model",
            "grok-4.6",
            "--reasoning-effort",
            "high",
            "--no-memory",
            "--disable-web-search",
            "--no-subagents",
            "--permission-mode",
            "bypassPermissions",
        ],
    }
]
SFG_LIVE_AGENTS = [
    {
        "name": "sol-peer",
        "kind": "pi",
        "workspace_id": "w-agents",
        "cwd": None,
        "pane_id": "w-agents:p-sol",
        "tab_id": "w-agents:t-sol",
    },
    {
        "name": "fable-peer",
        "kind": "claude",
        "workspace_id": "w-agents",
        "cwd": None,
        "pane_id": "w-agents:p-fable",
        "tab_id": "w-agents:t-fable",
    },
    {
        "name": "grok46-peer",
        "kind": "grok",
        "workspace_id": "w-agents",
        "cwd": None,
        "pane_id": "w-agents:p-grok",
        "tab_id": "w-agents:t-grok",
    },
]


def sfg_room_state(tmp_path: Path, room: str = "chat-sfg") -> dict:
    return {
        "schema_version": 1,
        "agents_workspace_id": "w-agents",
        "agents_cwd": str(tmp_path),
        "participant_pane_ids": {
            "sol": "w-agents:p-sol",
            "fable": "w-agents:p-fable",
            "grok": "w-agents:p-grok",
        },
        "participant_tab_ids": {
            "sol": "w-agents:t-sol",
            "fable": "w-agents:t-fable",
            "grok": "w-agents:t-grok",
        },
        "pending_room_id": room,
        "pending_room_operation_id": "operation-test",
        "pending_room_profile": "sol-fable-grok",
        "pending_room_started_unix_ms": int(module.time.time() * 1000),
    }


def install_sfg_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pane_screens: dict[str, str],
    live_agents: list[dict] | None = None,
    process_infos: dict[str, list[dict]] | None = None,
) -> list[list[str]]:
    """Fake Herdr for the three-role profile with all live sessions pre-seeded."""
    calls: list[list[str]] = []
    live = live_agents if live_agents is not None else SFG_LIVE_AGENTS
    for agent in live:
        agent["cwd"] = str(tmp_path)
    install_profile_host(
        monkeypatch,
        calls,
        pane_screens,
        live_agents=live,
        process_infos=process_infos,
    )
    return calls


def test_sol_fable_grok_composes_reused_participants_in_exact_order() -> None:
    sol_fable = module.resolve_profile("sol-fable")
    triple = module.resolve_profile("sol-fable-grok")

    assert [(p.role, p.name, p.kind) for p in triple] == [
        ("sol", "sol-peer", "pi"),
        ("fable", "fable-peer", "claude"),
        ("grok", "grok46-peer", "grok"),
    ]
    assert triple[0] is sol_fable[0]  # Sol and Fable are reused, not duplicated
    assert triple[1] is sol_fable[1]
    # The stored sol-fable tuple is exactly the same objects and no Grok.
    assert sol_fable == (module.SOL_PARTICIPANT, module.FABLE_PARTICIPANT)
    assert all(participant is not module.GROK_PARTICIPANT for participant in sol_fable)
    arguments, receipt = module.profile_room_exec("sol-fable-grok", triple)
    assert arguments[arguments.index("--synthesizer") + 1] == "sol"
    grok_entry = json.loads(receipt)["verified"]
    grok = next(entry for entry in grok_entry if entry["role"] == "grok")
    assert grok == {
        "role": "grok",
        "target": "grok46-peer",
        "harness": "grok",
        "model": "grok-4.6",
        "effort": "high",
        "verification": "native-ui verified",
    }
    assert len(sol_fable) == 2  # the stored sol-fable profile is unchanged


def test_reopen_grok_fails_when_pane_text_mismatches_despite_exact_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact process argv alone is insufficient: the pane must still show 4.6."""
    monkeypatch.setattr(module, "VERIFY_PANE_INTERVAL_S", 0)
    monkeypatch.setattr(
        module,
        "run_json",
        lambda _herdr_bin, arguments, timeout=30: (
            {"result": {"process_info": {"foreground_processes": GROK_PROCESS}}}
            if arguments[:2] == ["pane", "process-info"]
            else (_ for _ in ()).throw(AssertionError(arguments))
        ),
    )
    monkeypatch.setattr(
        module, "run_text", lambda _herdr_bin, _arguments, timeout=30: "Grok CLI\nGrok 4.6.1\n"
    )
    grok = module.resolve_profile("sol-fable-grok")[2]

    assert not module.reopen_pane_proof("herdr", grok, "w-agents:p-grok")


def test_grok_participant_uses_exact_start_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    grok = module.resolve_profile("sol-fable-grok")[2]

    assert grok.start_args == (
        "--model",
        "grok-4.6",
        "--reasoning-effort",
        "high",
        "--no-memory",
        "--disable-web-search",
        "--no-subagents",
        "--permission-mode",
        "bypassPermissions",
    )
    assert grok.tab_path_prefix == "~/.grok/bin"
    assert (grok.reopen_process_argv0, grok.reopen_process_args) == (
        "grok",
        grok.start_args,
    )


def test_grok_tab_gets_prepended_grok_bin_path_and_exact_start_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    calls = install_sfg_host(
        monkeypatch,
        tmp_path,
        {
            "w-agents:p-sol": SOL_SCREEN,
            "w-agents:p-fable": FABLE_SCREEN,
            "w-agents:p-grok": GROK_SCREEN,
        },
        live_agents=[],
    )
    state: dict = {"schema_version": 1}

    failures = module.start_participants(
        "herdr",
        str(tmp_path),
        "w-agents",
        tmp_path,
        launcher_state_path(tmp_path),
        state,
        participants=module.resolve_profile("sol-fable-grok"),
    )

    assert failures == []
    creates = [call for call in calls if call[:2] == ["tab", "create"]]
    expected_env = f"PATH={Path('~/.grok/bin').expanduser()}{os.pathsep}/usr/bin:/bin"
    grok_create = next(
        create
        for create in creates
        if create[create.index("--label") + 1].startswith("hgchat-grok-")
    )
    other_creates = [create for create in creates if create is not grok_create]
    assert grok_create[grok_create.index("--env") : grok_create.index("--env") + 2] == [
        "--env",
        expected_env,
    ]
    for create in other_creates:
        assert "--env" not in create  # only the Grok tab's PATH is touched
    starts = {call[2]: call for call in calls if call[:2] == ["agent", "start"]}
    assert starts["grok46-peer"] == [
        "agent",
        "start",
        "grok46-peer",
        "--kind",
        "grok",
        "--pane",
        "w-agents:p-grok",
        "--timeout",
        "120000",
        "--",
        "--model",
        "grok-4.6",
        "--reasoning-effort",
        "high",
        "--no-memory",
        "--disable-web-search",
        "--no-subagents",
        "--permission-mode",
        "bypassPermissions",
    ]
    # The launcher's own environment is never mutated.
    assert os.environ["PATH"] == "/usr/bin:/bin"


def test_grok_tab_env_has_no_trailing_separator_without_parent_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty/unset parent PATH must not leave an empty component (cwd risk)."""
    grok = module.GROK_PARTICIPANT
    expected = f"PATH={Path('~/.grok/bin').expanduser()}"

    monkeypatch.setenv("PATH", "")
    assert module.participant_tab_env(grok) == ["--env", expected]
    monkeypatch.delenv("PATH", raising=False)
    assert module.participant_tab_env(grok) == ["--env", expected]
    # Only a participant that declares a prefix gets any env at all.
    assert module.participant_tab_env(module.SOL_PARTICIPANT) == []


def test_grok_fresh_pane_proof_requires_exact_grok_46_and_high(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def proves(screen: str) -> bool:
        monkeypatch.setattr(module, "run_text", lambda _herdr_bin, _arguments, timeout=30: screen)
        monkeypatch.setattr(module, "VERIFY_PANE_INTERVAL_S", 0)
        grok = module.resolve_profile("sol-fable-grok")[2]
        return module.pane_proof("herdr", grok, "w-agents:p-grok")

    assert proves(GROK_SCREEN)
    assert proves("Grok CLI\nmodel: Grok 4.6 (high)\nsession active\n")
    assert not proves("Grok CLI\nmodel Grok 4.6.1\nreasoning effort: high\n")  # suffixed version
    assert not proves("Grok CLI\nmodel groklette 4.6\nreasoning effort: high\n")  # prefix lookalike
    assert not proves("Grok CLI\nmodel Grok 4.6\nreasoning effort: highest\n")  # suffixed effort
    assert not proves("Grok CLI\nmodel Grok 4\nreasoning effort: high\n")  # version without 4.6
    assert not proves("Grok CLI\nmodel Grok 4.6\nwaiting for input\n")  # high missing entirely
    assert not proves("Grok CLI\nreasoning effort: high\nwaiting\n")  # model token missing
    assert not proves("Grok CLI\nmodel Grok 4 (high)\nwaiting\n")  # version without 4.6
    assert not proves("Grok CLI\nwaiting for input\n")  # no evidence at all


@pytest.mark.parametrize(
    "processes",
    [
        pytest.param([], id="missing-process"),
        pytest.param(
            [
                {
                    "argv0": "grok",
                    "argv": ["grok", "--model", "grok-4.6", "--reasoning-effort", "high"],
                }
            ],
            id="missing-flags",
        ),
        pytest.param(
            [
                {
                    "argv0": "grok",
                    "argv": [
                        "grok",
                        "--reasoning-effort",
                        "high",
                        "--model",
                        "grok-4.6",
                        "--no-memory",
                        "--disable-web-search",
                        "--no-subagents",
                        "--permission-mode",
                        "bypassPermissions",
                    ],
                }
            ],
            id="reordered-args",
        ),
        pytest.param(
            [
                {
                    "argv0": "grok",
                    "argv": [
                        "grok",
                        "--model",
                        "grok-4.6-fast",
                        "--reasoning-effort",
                        "high",
                        "--no-memory",
                        "--disable-web-search",
                        "--no-subagents",
                        "--permission-mode",
                        "bypassPermissions",
                    ],
                }
            ],
            id="model-lookalike",
        ),
        pytest.param(
            [
                {
                    "argv0": "grok-cli",
                    "argv": [
                        "grok",
                        "--model",
                        "grok-4.6",
                        "--reasoning-effort",
                        "high",
                        "--no-memory",
                        "--disable-web-search",
                        "--no-subagents",
                        "--permission-mode",
                        "bypassPermissions",
                    ],
                }
            ],
            id="wrong-argv0",
        ),
        pytest.param(
            [
                {
                    "argv0": "node",
                    "argv": [
                        "node",
                        "mcp",
                        "--model",
                        "grok-4.6",
                        "--reasoning-effort",
                        "high",
                        "--no-memory",
                        "--disable-web-search",
                        "--no-subagents",
                        "--permission-mode",
                        "bypassPermissions",
                    ],
                }
            ],
            id="mcp-child",
        ),
        pytest.param(
            [
                {
                    "argv0": "grok",
                    "argv": [
                        "grok",
                        "--model",
                        "grok-4.6",
                        "--reasoning-effort",
                        "low",
                        "--no-memory",
                        "--disable-web-search",
                        "--no-subagents",
                        "--permission-mode",
                        "bypassPermissions",
                    ],
                }
            ],
            id="wrong-effort",
        ),
    ],
)
def test_reopen_grok_process_proof_fails_closed_on_inexact_evidence(
    monkeypatch: pytest.MonkeyPatch, processes: list[dict]
) -> None:
    monkeypatch.setattr(module, "VERIFY_PANE_INTERVAL_S", 0)
    monkeypatch.setattr(
        module,
        "run_json",
        lambda _herdr_bin, arguments, timeout=30: (
            {"result": {"process_info": {"foreground_processes": processes}}}
            if arguments[:2] == ["pane", "process-info"]
            else (_ for _ in ()).throw(AssertionError(arguments))
        ),
    )
    monkeypatch.setattr(
        module, "run_text", lambda _herdr_bin, _arguments, timeout=30: GROK_POST_TURN_SCREEN
    )
    grok = module.resolve_profile("sol-fable-grok")[2]

    assert not module.reopen_pane_proof("herdr", grok, "w-agents:p-grok")


def test_reopen_grok_accepts_stable_ui_and_exact_process_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "VERIFY_PANE_INTERVAL_S", 0)
    monkeypatch.setattr(
        module,
        "run_json",
        lambda _herdr_bin, arguments, timeout=30: (
            {"result": {"process_info": {"foreground_processes": GROK_PROCESS}}}
            if arguments[:2] == ["pane", "process-info"]
            else (_ for _ in ()).throw(AssertionError(arguments))
        ),
    )
    monkeypatch.setattr(
        module, "run_text", lambda _herdr_bin, _arguments, timeout=30: GROK_POST_TURN_SCREEN
    )
    grok = module.resolve_profile("sol-fable-grok")[2]

    assert module.reopen_pane_proof("herdr", grok, "w-agents:p-grok")


def test_main_never_execs_sol_fable_grok_when_grok_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    profile_launch_env(tmp_path, monkeypatch, captured)
    monkeypatch.setenv(module.PROFILE_ENV, "sol-fable-grok")
    monkeypatch.setenv(module.ROOM_ENV, "chat-sfg")
    calls = install_sfg_host(
        monkeypatch,
        tmp_path,
        # Grok's pane shows a suffixed version: the existing session mismatches.
        {
            "w-agents:p-sol": SOL_SCREEN,
            "w-agents:p-fable": FABLE_SCREEN,
            "w-agents:p-grok": "Grok CLI\nmodel Grok 4.6.1\n",
        },
    )
    save_launcher_state(tmp_path, sfg_room_state(tmp_path))

    with pytest.raises(BootstrapError, match="requires every participant"):
        module.main()

    assert "argv" not in captured
    assert "selected_profile" not in load_launcher_state(tmp_path)
    # The mismatched existing Grok session is left open, and nothing else closes.
    mutations = (("tab", "close"), ("pane", "close"), ("agent", "send-keys"))
    assert not any(tuple(call[:2]) in mutations for call in calls)
    # Already-verified earlier peers remain recorded so a retry can reuse them.
    state = load_launcher_state(tmp_path)
    assert state["participant_pane_ids"]["sol"] == "w-agents:p-sol"
    assert state["participant_pane_ids"]["fable"] == "w-agents:p-fable"


def test_failed_new_grok_tab_is_closed_while_verified_peers_remain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    calls = install_sfg_host(
        monkeypatch,
        tmp_path,
        {"w-agents:p-sol": SOL_SCREEN, "w-agents:p-fable": FABLE_SCREEN, "w-agents:p-grok": ""},
        live_agents=[],
    )
    state: dict = {"schema_version": 1}

    failures = module.start_participants(
        "herdr",
        str(tmp_path),
        "w-agents",
        tmp_path,
        launcher_state_path(tmp_path),
        state,
        participants=module.resolve_profile("sol-fable-grok"),
    )

    assert any("@grok" in failure and "the new tab was closed" in failure for failure in failures)
    assert ["tab", "close", "w-agents:t-grok"] in calls
    assert "grok" not in state.get("participant_pane_ids", {})
    assert "grok" not in state.get("pending_participant_tabs", {})
    assert state["participant_pane_ids"]["sol"] == "w-agents:p-sol"
    assert state["participant_pane_ids"]["fable"] == "w-agents:p-fable"


def install_role_replacement_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    live_agents: list[dict],
    pane_screens: dict[str, str],
) -> list[list[str]]:
    """Fake Herdr whose created tabs always get fresh, never-reused pane ids."""
    calls: list[list[str]] = []
    created = 0
    for agent in live_agents:
        agent["cwd"] = str(tmp_path)

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> dict:
        nonlocal created
        del timeout
        calls.append(arguments)
        if arguments == ["agent", "list"]:
            return {"result": {"agents": live_agents}}
        if arguments[:2] == ["pane", "process-info"]:
            # A reused Fable peer has taken turns, so its startup banner is gone;
            # the setup path must accept its reopen evidence (pane text plus the
            # exact foreground argv under the protocol-21 ``name`` field).
            if arguments[-1] == "w-agents:p-fable":
                return {
                    "result": {
                        "process_info": {
                            "foreground_processes": [
                                {
                                    "name": "claude",
                                    "argv": ["claude", "--model", "fable", "--effort", "high"],
                                }
                            ]
                        }
                    }
                }
            return {"result": {"process_info": {"foreground_processes": [{"name": "zsh"}]}}}
        if arguments[:2] == ["tab", "create"]:
            created += 1
            return {
                "result": {
                    "tab": {"tab_id": f"w-agents:t-fresh{created}"},
                    "root_pane": {"pane_id": f"w-agents:p-fresh{created}"},
                }
            }
        return {"result": {"type": "ok"}}

    def fake_run_text(_herdr_bin: str, arguments: list[str], timeout: float | None = 30) -> str:
        del timeout
        calls.append(arguments)
        return pane_screens.get(arguments[2], "")

    monkeypatch.setattr(module, "run_json", fake_run_json)
    monkeypatch.setattr(module, "run_text", fake_run_text)
    monkeypatch.setattr(module, "native_catalog_row_present", lambda *_arguments: True)
    monkeypatch.setattr(module, "VERIFY_PANE_INTERVAL_S", 0)
    return calls


def test_classic_room_replaces_only_the_recorded_grok_role_and_keeps_profile_peers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(module, "HOOK_DISMISS_TIMEOUT_S", 0)
    calls = install_role_replacement_host(
        monkeypatch,
        tmp_path,
        [dict(agent) for agent in SFG_LIVE_AGENTS],
        {"w-agents:p-sol": SOL_SCREEN, "w-agents:p-fable": FABLE_SCREEN},
    )
    state = sfg_room_state(tmp_path)
    for field in (
        "pending_room_id",
        "pending_room_operation_id",
        "pending_room_profile",
        "pending_room_started_unix_ms",
    ):
        state.pop(field)

    failures = module.start_participants(
        "herdr",
        str(tmp_path),
        "w-agents",
        tmp_path,
        launcher_state_path(tmp_path),
        state,
    )

    assert failures == []
    assert [call for call in calls if call[:2] == ["tab", "close"]] == [
        ["tab", "close", "w-agents:t-grok"]
    ]
    starts = {call[2]: call for call in calls if call[:2] == ["agent", "start"]}
    assert set(starts) == {"pi-peer", "claude-peer", "codex-peer", "grok-peer"}
    recorded_grok_pane = state["participant_pane_ids"]["grok"]
    assert recorded_grok_pane != "w-agents:p-grok"
    assert starts["grok-peer"][starts["grok-peer"].index("--kind") + 1] == "grok"
    assert starts["grok-peer"][starts["grok-peer"].index("--pane") + 1] == recorded_grok_pane
    assert state["participant_pane_ids"]["sol"] == "w-agents:p-sol"
    assert state["participant_pane_ids"]["fable"] == "w-agents:p-fable"
    assert state["participant_tab_ids"]["sol"] == "w-agents:t-sol"
    assert state["participant_tab_ids"]["fable"] == "w-agents:t-fable"
    assert state["participant_tab_ids"]["grok"] != "w-agents:t-grok"
    assert "replace @grok" in capsys.readouterr().out.splitlines()


def test_profile_relaunch_replaces_only_the_recorded_classic_grok_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    live_agents = [
        {
            "name": "sol-peer",
            "kind": "pi",
            "workspace_id": "w-agents",
            "cwd": None,
            "pane_id": "w-agents:p-sol",
            "tab_id": "w-agents:t-sol",
        },
        {
            "name": "fable-peer",
            "kind": "claude",
            "workspace_id": "w-agents",
            "cwd": None,
            "pane_id": "w-agents:p-fable",
            "tab_id": "w-agents:t-fable",
        },
        {
            "name": "grok-peer",
            "kind": "grok",
            "workspace_id": "w-agents",
            "cwd": None,
            "pane_id": "w-agents:p-grok-peer",
            "tab_id": "w-agents:t-grok-peer",
        },
    ]
    calls = install_role_replacement_host(
        monkeypatch,
        tmp_path,
        live_agents,
        {
            "w-agents:p-sol": SOL_SCREEN,
            "w-agents:p-fable": FABLE_SCREEN,
            "w-agents:p-fresh1": GROK_SCREEN,
        },
    )
    state = {
        "schema_version": 1,
        "agents_workspace_id": "w-agents",
        "agents_cwd": str(tmp_path),
        "participant_pane_ids": {
            "sol": "w-agents:p-sol",
            "fable": "w-agents:p-fable",
            "grok": "w-agents:p-grok-peer",
        },
        "participant_tab_ids": {
            "sol": "w-agents:t-sol",
            "fable": "w-agents:t-fable",
            "grok": "w-agents:t-grok-peer",
        },
    }

    failures = module.start_participants(
        "herdr",
        str(tmp_path),
        "w-agents",
        tmp_path,
        launcher_state_path(tmp_path),
        state,
        participants=module.resolve_profile("sol-fable-grok"),
    )

    assert failures == []
    assert [call for call in calls if call[:2] == ["tab", "close"]] == [
        ["tab", "close", "w-agents:t-grok-peer"]
    ]
    starts = {call[2]: call for call in calls if call[:2] == ["agent", "start"]}
    assert set(starts) == {"grok46-peer"}
    grok46 = starts["grok46-peer"]
    assert grok46[grok46.index("--kind") + 1] == "grok"
    assert grok46[grok46.index("--pane") + 1] == "w-agents:p-fresh1"
    assert grok46[grok46.index("--") + 1 :] == list(module.GROK_START_ARGS)
    assert state["participant_pane_ids"] == {
        "sol": "w-agents:p-sol",
        "fable": "w-agents:p-fable",
        "grok": "w-agents:p-fresh1",
    }
    assert state["participant_tab_ids"] == {
        "sol": "w-agents:t-sol",
        "fable": "w-agents:t-fable",
        "grok": "w-agents:t-fresh1",
    }
    output = capsys.readouterr().out.splitlines()
    assert "replace @grok" in output
    assert "ready  @sol (existing sol-peer)" in output
    assert "ready  @fable (existing fable-peer)" in output

    pi_calls = install_role_replacement_host(
        monkeypatch,
        tmp_path,
        [dict(agent) for agent in SFG_LIVE_AGENTS],
        {
            "w-agents:p-sol": SOL_SCREEN,
            "w-agents:p-fable": FABLE_POST_TURN_SCREEN,
            "w-agents:p-fresh1": GROK_PI_SCREEN,
        },
    )
    pi_state = sfg_room_state(tmp_path)
    assert (
        module.start_participants(
            "herdr",
            str(tmp_path),
            "w-agents",
            tmp_path,
            launcher_state_path(tmp_path),
            pi_state,
            participants=module.resolve_profile("sol-fable-grok-pi"),
        )
        == []
    )
    assert [call for call in pi_calls if call[:2] == ["tab", "close"]] == [
        ["tab", "close", "w-agents:t-grok"]
    ]
    pi_starts = {call[2]: call for call in pi_calls if call[:2] == ["agent", "start"]}
    assert set(pi_starts) == {"grok46pi-peer"}
    assert pi_state["participant_pane_ids"] == {
        "sol": "w-agents:p-sol",
        "fable": "w-agents:p-fable",
        "grok": "w-agents:p-fresh1",
    }

    native_calls = install_role_replacement_host(
        monkeypatch,
        tmp_path,
        [dict(agent) for agent in SFGPI_LIVE_AGENTS],
        {
            "w-agents:p-sol": SOL_SCREEN,
            "w-agents:p-fable": FABLE_POST_TURN_SCREEN,
            "w-agents:p-fresh1": GROK_SCREEN,
        },
    )
    native_state = sfgpi_room_state(tmp_path)
    assert (
        module.start_participants(
            "herdr",
            str(tmp_path),
            "w-agents",
            tmp_path,
            launcher_state_path(tmp_path),
            native_state,
            participants=module.resolve_profile("sol-fable-grok"),
        )
        == []
    )
    assert [call for call in native_calls if call[:2] == ["tab", "close"]] == [
        ["tab", "close", "w-agents:t-grokpi"]
    ]
    native_starts = {call[2]: call for call in native_calls if call[:2] == ["agent", "start"]}
    assert set(native_starts) == {"grok46-peer"}
    assert native_state["participant_pane_ids"] == {
        "sol": "w-agents:p-sol",
        "fable": "w-agents:p-fable",
        "grok": "w-agents:p-fresh1",
    }


def test_room_reopen_reverifies_sol_fable_grok_and_execs_with_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    room_reopen_env(tmp_path, monkeypatch, captured, "chat-sfg")
    calls = install_sfg_host(
        monkeypatch,
        tmp_path,
        {
            "w-agents:p-sol": SOL_SCREEN,
            "w-agents:p-fable": FABLE_POST_TURN_SCREEN,
            "w-agents:p-grok": GROK_POST_TURN_SCREEN,
        },
        process_infos={
            "w-agents:p-fable": CLAUDE_FABLE_PROCESS,
            "w-agents:p-grok": GROK_PROCESS,
        },
    )
    state = sfg_room_state(tmp_path)
    state.pop("pending_room_id")
    state.pop("pending_room_operation_id")
    state.pop("pending_room_profile")
    state.pop("pending_room_started_unix_ms")
    state["selected_profile"] = "sol-fable-grok"
    state["last_room_id"] = "chat-sfg"
    save_launcher_state(tmp_path, state)

    module.room_entrypoint()

    argv = captured["argv"]
    assert argv[argv.index("--profile") + 1] == "sol-fable-grok"
    mappings = [value for index, value in enumerate(argv) if argv[index - 1] == "--agent"]
    assert mappings == ["sol=sol-peer", "fable=fable-peer", "grok=grok46-peer"]
    assert argv[argv.index("--synthesizer") + 1] == "sol"
    receipt = json.loads(os.environ[module.PROFILE_RECEIPT_ENV])
    assert receipt == module.profile_receipt_payload(
        "sol-fable-grok", module.resolve_profile("sol-fable-grok")
    )
    # Reopen only re-verifies: nothing is started or closed.
    assert not any(
        call[:2] in (["agent", "start"], ["tab", "close"], ["pane", "close"]) for call in calls
    )
    process_panes = {
        call[call.index("--pane") + 1] for call in calls if call[:2] == ["pane", "process-info"]
    }
    assert process_panes == {"w-agents:p-fable", "w-agents:p-grok"}

    pi_captured: dict[str, object] = {}
    room_reopen_env(tmp_path, monkeypatch, pi_captured, "chat-sfgpi")
    pi_calls = install_sfgpi_host(
        monkeypatch,
        tmp_path,
        {
            "w-agents:p-sol": SOL_SCREEN,
            "w-agents:p-fable": FABLE_POST_TURN_SCREEN,
            "w-agents:p-grokpi": GROK_PI_POST_TURN_SCREEN,
        },
    )
    pi_state = sfgpi_room_state(tmp_path)
    for field in (
        "pending_room_id",
        "pending_room_operation_id",
        "pending_room_profile",
        "pending_room_started_unix_ms",
    ):
        pi_state.pop(field)
    pi_state["selected_profile"] = "sol-fable-grok-pi"
    pi_state["last_room_id"] = "chat-sfgpi"
    save_launcher_state(tmp_path, pi_state)

    module.room_entrypoint()

    pi_argv = pi_captured["argv"]
    assert pi_argv[pi_argv.index("--profile") + 1] == "sol-fable-grok-pi"
    assert [value for index, value in enumerate(pi_argv) if pi_argv[index - 1] == "--agent"] == [
        "sol=sol-peer",
        "fable=fable-peer",
        "grok=grok46pi-peer",
    ]
    assert json.loads(os.environ[module.PROFILE_RECEIPT_ENV]) == module.profile_receipt_payload(
        "sol-fable-grok-pi", module.resolve_profile("sol-fable-grok-pi")
    )
    assert not any(
        call[:2] in (["agent", "start"], ["tab", "close"], ["pane", "close"]) for call in pi_calls
    )
    assert {
        call[call.index("--pane") + 1] for call in pi_calls if call[:2] == ["pane", "process-info"]
    } == {"w-agents:p-fable"}


# --- sol-fable-glm profile ------------------------------------------------------------

# Derived from a live scratch run of `pi --provider bigmodel-coding --model
# glm-5.3 --thinking high` (pi v0.84.4): the startup status line shows
# `glm-5.3 • high`, and after the first turn the input-box footer keeps
# `╰ glm-5.3 • high … ctx 2% ╯`.
GLM_SCREEN = "Pi\nprovider bigmodel-coding\nmodel glm-5.3 • high\n"
GLM_POST_TURN_SCREEN = "Pi\nturn complete\n╰ glm-5.3 • high ─────────────────────── ctx 2% ╯\n"
SFGLM_LIVE_AGENTS = [
    {
        "name": "sol-peer",
        "kind": "pi",
        "workspace_id": "w-agents",
        "cwd": None,
        "pane_id": "w-agents:p-sol",
        "tab_id": "w-agents:t-sol",
    },
    {
        "name": "fable-peer",
        "kind": "claude",
        "workspace_id": "w-agents",
        "cwd": None,
        "pane_id": "w-agents:p-fable",
        "tab_id": "w-agents:t-fable",
    },
    {
        "name": "glm-peer",
        "kind": "pi",
        "workspace_id": "w-agents",
        "cwd": None,
        "pane_id": "w-agents:p-glm",
        "tab_id": "w-agents:t-glm",
    },
]


def sfglm_room_state(tmp_path: Path, room: str = "chat-sfglm") -> dict:
    return {
        "schema_version": 1,
        "agents_workspace_id": "w-agents",
        "agents_cwd": str(tmp_path),
        "participant_pane_ids": {
            "sol": "w-agents:p-sol",
            "fable": "w-agents:p-fable",
            "glm": "w-agents:p-glm",
        },
        "participant_tab_ids": {
            "sol": "w-agents:t-sol",
            "fable": "w-agents:t-fable",
            "glm": "w-agents:t-glm",
        },
        "pending_room_id": room,
        "pending_room_operation_id": "operation-test",
        "pending_room_profile": "sol-fable-glm",
        "pending_room_started_unix_ms": int(module.time.time() * 1000),
    }


def install_sfglm_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pane_screens: dict[str, str],
    live_agents: list[dict] | None = None,
    process_infos: dict[str, list[dict]] | None = None,
) -> list[list[str]]:
    """Fake Herdr for the three-role GLM profile with all sessions pre-seeded."""
    calls: list[list[str]] = []
    live = live_agents if live_agents is not None else SFGLM_LIVE_AGENTS
    for agent in live:
        agent["cwd"] = str(tmp_path)
    install_profile_host(
        monkeypatch,
        calls,
        pane_screens,
        live_agents=live,
        process_infos=process_infos,
    )
    return calls


def test_sol_fable_glm_composes_reused_participants_in_exact_order() -> None:
    sol_fable = module.resolve_profile("sol-fable")
    triple = module.resolve_profile("sol-fable-glm")

    assert [(p.role, p.name, p.kind) for p in triple] == [
        ("sol", "sol-peer", "pi"),
        ("fable", "fable-peer", "claude"),
        ("glm", "glm-peer", "pi"),
    ]
    assert triple[0] is sol_fable[0]  # Sol and Fable are reused, not duplicated
    assert triple[1] is sol_fable[1]
    assert all(participant is not module.GLM_PARTICIPANT for participant in sol_fable)
    arguments, receipt = module.profile_room_exec("sol-fable-glm", triple)
    assert arguments[arguments.index("--synthesizer") + 1] == "sol"
    glm_entry = json.loads(receipt)["verified"]
    glm = next(entry for entry in glm_entry if entry["role"] == "glm")
    assert glm == {
        "role": "glm",
        "target": "glm-peer",
        "harness": "pi",
        "provider": "bigmodel-coding",
        "model": "glm-5.3",
        "effort": "high",
        "verification": "native-ui verified",
    }
    # The stored profiles are unchanged and never pick up the GLM participant.
    assert sol_fable == (module.SOL_PARTICIPANT, module.FABLE_PARTICIPANT)
    assert module.resolve_profile("sol-fable-grok") == (
        module.SOL_PARTICIPANT,
        module.FABLE_PARTICIPANT,
        module.GROK_PARTICIPANT,
    )


def test_glm_participant_uses_exact_start_argv() -> None:
    glm = module.resolve_profile("sol-fable-glm")[2]

    assert glm.start_args == (
        "--provider",
        "bigmodel-coding",
        "--model",
        "glm-5.3",
        "--thinking",
        "high",
    )
    assert glm.catalog_command == ("pi", "--list-models", "glm-5.3")
    assert (glm.provider, glm.model, glm.effort) == ("bigmodel-coding", "glm-5.3", "high")
    # Like Sol, GLM needs no tab PATH prefix and no reopen process argv: the
    # pane keeps its `glm-5.3 • high` evidence after the first turn.
    assert glm.tab_path_prefix == ""
    assert glm.reopen_pane_proofs == ()
    assert glm.reopen_process_argv0 == ""
    assert glm.reopen_process_args == ()


def test_glm_fresh_pane_proof_requires_exact_glm_53_and_high(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def proves(screen: str) -> bool:
        monkeypatch.setattr(module, "run_text", lambda _herdr_bin, _arguments, timeout=30: screen)
        monkeypatch.setattr(module, "VERIFY_PANE_INTERVAL_S", 0)
        glm = module.resolve_profile("sol-fable-glm")[2]
        return module.pane_proof("herdr", glm, "w-agents:p-glm")

    assert proves(GLM_SCREEN)
    assert proves("Pi\nmodel: glm-5.3 • high (auto)\nsession active\n")
    assert not proves("Pi\nmodel glm-5.3.1 • high\n")  # suffixed version
    assert not proves("Pi\nmodel glm 5.3 • high\n")  # split model token
    assert not proves("Pi\nmodel glm-5.3 • highest\n")  # suffixed effort
    assert not proves("Pi\nmodel glm-5.3 • medium\n")  # wrong effort
    assert not proves("Pi\nmodel glm-5.3 ─ high\n")  # wrong separator token
    assert not proves("Pi\nmodel glm-5.3\nwaiting for input\n")  # high missing
    assert not proves("Pi\nthinking high\nwaiting\n")  # model token missing
    assert not proves("Pi\nwaiting for input\n")  # no evidence at all


def test_reopen_glm_uses_persistent_pane_proof_without_process_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module,
        "run_json",
        lambda _herdr_bin, arguments, timeout=30: (
            (_ for _ in ()).throw(AssertionError("GLM reopen must not read process info"))
            if arguments[:2] == ["pane", "process-info"]
            else {"result": {"type": "ok"}}
        ),
    )
    monkeypatch.setattr(
        module, "run_text", lambda _herdr_bin, _arguments, timeout=30: GLM_POST_TURN_SCREEN
    )
    monkeypatch.setattr(
        module,
        "native_catalog_row_present",
        lambda command, provider, model: True,
    )
    glm = module.resolve_profile("sol-fable-glm")[2]

    assert module.reopen_pane_proof("herdr", glm, "w-agents:p-glm")


def test_glm_tab_is_created_without_path_env_and_started_with_exact_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    calls = install_sfglm_host(
        monkeypatch,
        tmp_path,
        {
            "w-agents:p-sol": SOL_SCREEN,
            "w-agents:p-fable": FABLE_SCREEN,
            "w-agents:p-glm": GLM_SCREEN,
        },
        live_agents=[],
    )
    state: dict = {"schema_version": 1}

    failures = module.start_participants(
        "herdr",
        str(tmp_path),
        "w-agents",
        tmp_path,
        launcher_state_path(tmp_path),
        state,
        participants=module.resolve_profile("sol-fable-glm"),
    )

    assert failures == []
    creates = [call for call in calls if call[:2] == ["tab", "create"]]
    assert len(creates) == 3
    for create in creates:  # no GLM (or any) tab gets an environment override
        assert "--env" not in create
        assert "--no-focus" in create  # participant tabs never steal focus
    starts = {call[2]: call for call in calls if call[:2] == ["agent", "start"]}
    assert starts["glm-peer"] == [
        "agent",
        "start",
        "glm-peer",
        "--kind",
        "pi",
        "--pane",
        "w-agents:p-glm",
        "--timeout",
        "120000",
        "--",
        "--provider",
        "bigmodel-coding",
        "--model",
        "glm-5.3",
        "--thinking",
        "high",
    ]
    assert state["participant_pane_ids"]["glm"] == "w-agents:p-glm"
    assert state["participant_tab_ids"]["glm"] == "w-agents:t-glm"


def test_main_never_execs_sol_fable_glm_when_glm_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    profile_launch_env(tmp_path, monkeypatch, captured)
    monkeypatch.setenv(module.PROFILE_ENV, "sol-fable-glm")
    monkeypatch.setenv(module.ROOM_ENV, "chat-sfglm")
    calls = install_sfglm_host(
        monkeypatch,
        tmp_path,
        # GLM's pane shows a suffixed version: the existing session mismatches.
        {
            "w-agents:p-sol": SOL_SCREEN,
            "w-agents:p-fable": FABLE_SCREEN,
            "w-agents:p-glm": "Pi\nmodel glm-5.3.1 • high\n",
        },
    )
    save_launcher_state(tmp_path, sfglm_room_state(tmp_path))

    with pytest.raises(BootstrapError, match="requires every participant"):
        module.main()

    assert "argv" not in captured
    assert "selected_profile" not in load_launcher_state(tmp_path)
    # The mismatched existing GLM session is left open, and nothing else closes.
    mutations = (("tab", "close"), ("pane", "close"), ("agent", "send-keys"))
    assert not any(tuple(call[:2]) in mutations for call in calls)
    # Already-verified earlier peers remain recorded so a retry can reuse them.
    state = load_launcher_state(tmp_path)
    assert state["participant_pane_ids"]["sol"] == "w-agents:p-sol"
    assert state["participant_pane_ids"]["fable"] == "w-agents:p-fable"


def test_room_reopen_reverifies_sol_fable_glm_and_execs_with_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    room_reopen_env(tmp_path, monkeypatch, captured, "chat-sfglm")
    calls = install_sfglm_host(
        monkeypatch,
        tmp_path,
        {
            "w-agents:p-sol": SOL_SCREEN,
            "w-agents:p-fable": FABLE_POST_TURN_SCREEN,
            "w-agents:p-glm": GLM_POST_TURN_SCREEN,
        },
        process_infos={"w-agents:p-fable": CLAUDE_FABLE_PROCESS},
    )
    state = sfglm_room_state(tmp_path)
    state.pop("pending_room_id")
    state.pop("pending_room_operation_id")
    state.pop("pending_room_profile")
    state.pop("pending_room_started_unix_ms")
    state["selected_profile"] = "sol-fable-glm"
    state["last_room_id"] = "chat-sfglm"
    save_launcher_state(tmp_path, state)

    module.room_entrypoint()

    argv = captured["argv"]
    assert argv[argv.index("--profile") + 1] == "sol-fable-glm"
    mappings = [value for index, value in enumerate(argv) if argv[index - 1] == "--agent"]
    assert mappings == ["sol=sol-peer", "fable=fable-peer", "glm=glm-peer"]
    assert argv[argv.index("--synthesizer") + 1] == "sol"
    receipt = json.loads(os.environ[module.PROFILE_RECEIPT_ENV])
    assert receipt == module.profile_receipt_payload(
        "sol-fable-glm", module.resolve_profile("sol-fable-glm")
    )
    # Reopen only re-verifies: nothing is started or closed.
    assert not any(
        call[:2] in (["agent", "start"], ["tab", "close"], ["pane", "close"]) for call in calls
    )
    # GLM, like Sol, verifies from its persistent pane text and needs no
    # foreground-process argv evidence on reopen.
    process_panes = {
        call[call.index("--pane") + 1] for call in calls if call[:2] == ["pane", "process-info"]
    }
    assert process_panes == {"w-agents:p-fable"}


# --- default sol-fable-grok-pi profile -----------------------------------------------

GROK_PI_SCREEN = "Pi\nprovider xai\nmodel grok-4.6 • high\n"
GROK_PI_POST_TURN_SCREEN = "Pi\nturn complete\n╰ grok-4.6 • high ───────────── ctx 2% ╯\n"
SFGPI_LIVE_AGENTS = [
    {
        "name": "sol-peer",
        "kind": "pi",
        "workspace_id": "w-agents",
        "cwd": None,
        "pane_id": "w-agents:p-sol",
        "tab_id": "w-agents:t-sol",
    },
    {
        "name": "fable-peer",
        "kind": "claude",
        "workspace_id": "w-agents",
        "cwd": None,
        "pane_id": "w-agents:p-fable",
        "tab_id": "w-agents:t-fable",
    },
    {
        "name": "grok46pi-peer",
        "kind": "pi",
        "workspace_id": "w-agents",
        "cwd": None,
        "pane_id": "w-agents:p-grokpi",
        "tab_id": "w-agents:t-grokpi",
    },
]


def sfgpi_room_state(tmp_path: Path, room: str = "chat-sfgpi") -> dict:
    return {
        "schema_version": 1,
        "agents_workspace_id": "w-agents",
        "agents_cwd": str(tmp_path),
        "participant_pane_ids": {
            "sol": "w-agents:p-sol",
            "fable": "w-agents:p-fable",
            "grok": "w-agents:p-grokpi",
        },
        "participant_tab_ids": {
            "sol": "w-agents:t-sol",
            "fable": "w-agents:t-fable",
            "grok": "w-agents:t-grokpi",
        },
        "pending_room_id": room,
        "pending_room_operation_id": "operation-test",
        "pending_room_profile": "sol-fable-grok-pi",
        "pending_room_started_unix_ms": int(module.time.time() * 1000),
    }


def install_sfgpi_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pane_screens: dict[str, str],
    live_agents: list[dict] | None = None,
) -> list[list[str]]:
    calls: list[list[str]] = []
    live = live_agents if live_agents is not None else SFGPI_LIVE_AGENTS
    for agent in live:
        agent["cwd"] = str(tmp_path)
    install_profile_host(
        monkeypatch,
        calls,
        pane_screens,
        live_agents=live,
        process_infos={"w-agents:p-fable": CLAUDE_FABLE_PROCESS},
    )
    return calls


def test_sol_fable_grok_pi_reuses_sol_fable_and_preserves_native_grok_profile() -> None:
    sol_fable = module.resolve_profile("sol-fable")
    pi_grok = module.resolve_profile("sol-fable-grok-pi")

    assert [(p.role, p.name, p.kind) for p in pi_grok] == [
        ("sol", "sol-peer", "pi"),
        ("fable", "fable-peer", "claude"),
        ("grok", "grok46pi-peer", "pi"),
    ]
    assert pi_grok[:2] == sol_fable
    assert pi_grok[0] is sol_fable[0]
    assert pi_grok[1] is sol_fable[1]
    assert module.resolve_profile("sol-fable-grok") == (
        module.SOL_PARTICIPANT,
        module.FABLE_PARTICIPANT,
        module.GROK_PARTICIPANT,
    )
    _, receipt = module.profile_room_exec("sol-fable-grok-pi", pi_grok)
    assert next(entry for entry in json.loads(receipt)["verified"] if entry["role"] == "grok") == {
        "role": "grok",
        "target": "grok46pi-peer",
        "harness": "pi",
        "provider": "xai",
        "model": "grok-4.6",
        "effort": "high",
        "verification": "native-ui verified",
    }


def test_grok_pi_uses_the_exact_xai_argv_and_no_fallback() -> None:
    grok = module.resolve_profile("sol-fable-grok-pi")[2]

    assert grok.start_args == (
        "--provider",
        "xai",
        "--model",
        "grok-4.6",
        "--thinking",
        "high",
    )
    assert grok.catalog_command == ("pi", "--list-models", "grok-4.6")
    assert (grok.provider, grok.model, grok.effort) == ("xai", "grok-4.6", "high")
    assert grok.tab_path_prefix == ""
    assert grok.reopen_pane_proofs == ()
    assert grok.reopen_process_argv0 == ""
    assert grok.reopen_process_args == ()


def test_grok_pi_catalog_requires_the_exact_xai_grok_46_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grok = module.resolve_profile("sol-fable-grok-pi")[2]

    def proves(stdout: str) -> bool:
        monkeypatch.setattr(
            module.subprocess,
            "run",
            lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout, ""),
        )
        return module.catalog_proof(grok)

    assert proves("xai  grok-4.6\n")
    assert proves("xai  grok-4.6\nxai  grok-4.6\n")
    assert proves("xai  grok-4.6\nother  different-model\n")
    assert not proves("xai  grok-4.6-fast\n")
    assert not proves("xai  grok 4.6\n")
    assert not proves("xai-compatible  grok-4.6\n")
    assert not proves("other  grok-4.6\n")
    assert not proves("xai  grok-4.6\nother  grok-4.6\n")
    assert not proves("other  grok-4.6\nxai  grok-4.6\n")


def test_grok_pi_pane_proof_requires_exact_bullet_high_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def proves(screen: str) -> bool:
        monkeypatch.setattr(module, "run_text", lambda *_args, **_kwargs: screen)
        monkeypatch.setattr(module, "VERIFY_PANE_INTERVAL_S", 0)
        return module.pane_proof("herdr", module.resolve_profile("sol-fable-grok-pi")[2], "p-grok")

    assert proves(GROK_PI_SCREEN)
    assert proves(GROK_PI_POST_TURN_SCREEN)
    assert not proves("Pi\nmodel grok-4.6.1 • high\n")
    assert not proves("Pi\nmodel grok 4.6 • high\n")
    assert not proves("Pi\nmodel grok-4.6 • highest\n")
    assert not proves("Pi\nmodel grok-4.6 ─ high\n")


def test_grok_pi_catalog_failure_creates_no_tab_and_never_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    install_profile_host(
        monkeypatch,
        calls,
        {"w-agents:p-grok": GROK_PI_SCREEN},
        catalog_rows={("xai", "grok-4.6"): False},
    )
    state: dict = {"schema_version": 1}

    failures = module.start_participants(
        "herdr",
        str(tmp_path),
        "w-agents",
        tmp_path,
        launcher_state_path(tmp_path),
        state,
        participants=(module.GROK_PI_PARTICIPANT,),
    )

    assert failures == [
        "@grok: grok46pi-peer failed the native catalog preflight; no tab was created"
    ]
    assert ["pi", "--list-models", "grok-4.6"] in calls
    assert not any(call[:2] in (["tab", "create"], ["agent", "start"]) for call in calls)


def test_grok_pi_starts_with_exact_argv_after_catalog_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    install_profile_host(
        monkeypatch,
        calls,
        {
            "w-agents:p-sol": SOL_SCREEN,
            "w-agents:p-fable": FABLE_SCREEN,
            "w-agents:p-grok": GROK_PI_SCREEN,
        },
    )
    state: dict = {"schema_version": 1}

    failures = module.start_participants(
        "herdr",
        str(tmp_path),
        "w-agents",
        tmp_path,
        launcher_state_path(tmp_path),
        state,
        participants=module.resolve_profile("sol-fable-grok-pi"),
    )

    assert failures == []
    starts = {call[2]: call for call in calls if call[:2] == ["agent", "start"]}
    assert starts["grok46pi-peer"] == [
        "agent",
        "start",
        "grok46pi-peer",
        "--kind",
        "pi",
        "--pane",
        "w-agents:p-grok",
        "--timeout",
        "120000",
        "--",
        "--provider",
        "xai",
        "--model",
        "grok-4.6",
        "--thinking",
        "high",
    ]
    grok_tab_create = next(
        index
        for index, call in enumerate(calls)
        if call[:2] == ["tab", "create"]
        and call[call.index("--label") + 1].startswith("hgchat-grok-")
    )
    assert calls.index(["pi", "--list-models", "grok-4.6"]) < grok_tab_create


# --- adopting stale peers after a Herdr server restart ---------------------------------


def adopt_live_agents(workspace_id: str, cwd: str, names: tuple[str, ...] = ()) -> list[dict]:
    """The six live peers the restart orphaned, exact names and kinds."""
    specs = [
        ("sol-peer", "pi"),
        ("fable-peer", "claude"),
        ("grok46-peer", "grok"),
        ("pi-peer", "pi"),
        ("claude-peer", "claude"),
        ("codex-peer", "codex"),
    ]
    if "grok46pi-peer" in names:
        specs.append(("grok46pi-peer", "pi"))
    return [
        {
            "name": name,
            "kind": kind,
            "workspace_id": workspace_id,
            "cwd": cwd,
            "pane_id": f"{workspace_id}:p-{name.removesuffix('-peer')}",
            "tab_id": f"{workspace_id}:t-{name.removesuffix('-peer')}",
        }
        for name, kind in specs
        if not names or name in names
    ]


def install_adopt_host(
    monkeypatch: pytest.MonkeyPatch,
    live: list[dict],
    workspaces: list[dict] | None = None,
    pane_screens: dict[str, str] | None = None,
) -> list[list[str]]:
    """Fake Herdr for --adopt-peers: live peers, one agents workspace, proofs."""
    calls: list[list[str]] = []
    # Adopted peers have already taken turns, so Fable and Grok present their
    # reopen evidence — post-turn pane text plus the exact native foreground
    # process argv — wherever they are live; Sol keeps its persistent footer.
    reopen_processes: dict[str, list[dict]] = {}
    for agent in live:
        if agent.get("name") == "fable-peer":
            reopen_processes[str(agent["pane_id"])] = CLAUDE_FABLE_PROCESS
        if agent.get("name") == "grok46-peer":
            reopen_processes[str(agent["pane_id"])] = GROK_PROCESS
    screens = (
        pane_screens
        if pane_screens is not None
        else {
            "w1E:p-sol": SOL_SCREEN,
            "w1E:p-fable": FABLE_POST_TURN_SCREEN,
            "w1E:p-grok46": GROK_POST_TURN_SCREEN,
        }
    )
    install_profile_host(
        monkeypatch,
        calls,
        screens,
        live_agents=live,
        process_infos=reopen_processes,
        workspaces=workspaces
        if workspaces is not None
        else [{"workspace_id": "w1E", "label": "agents · group-chat"}],
    )
    return calls


ADOPT_MUTATIONS = (
    ("tab", "close"),
    ("pane", "close"),
    ("tab", "create"),
    ("tab", "rename"),
    ("workspace", "create"),
    ("workspace", "rename"),
    ("agent", "start"),
    ("agent", "send-keys"),
)


def test_adopt_peers_adopts_a_clean_single_workspace_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))
    calls = install_adopt_host(monkeypatch, adopt_live_agents("w1E", str(tmp_path)))

    assert module.adopt_stale_peers() == 0

    state = load_launcher_state(tmp_path)
    assert state["agents_workspace_id"] == "w1E"
    assert state["agents_cwd"] == str(tmp_path)
    assert state["participant_pane_ids"] == {
        role: f"w1E:p-{pane}"
        for role, pane in (
            ("sol", "sol"),
            ("fable", "fable"),
            ("grok", "grok46"),
            ("pi", "pi"),
            ("claude", "claude"),
            ("codex", "codex"),
        )
    }
    assert state["participant_tab_ids"] == {
        role: f"w1E:t-{pane}"
        for role, pane in (
            ("sol", "sol"),
            ("fable", "fable"),
            ("grok", "grok46"),
            ("pi", "pi"),
            ("claude", "claude"),
            ("codex", "codex"),
        )
    }
    out = capsys.readouterr().out.splitlines()
    assert [line.split()[1] for line in out[:-1]] == [
        "@pi",
        "@claude",
        "@codex",
        "@sol",
        "@fable",
        "@grok",
    ]
    assert "adopted 6 peers in workspace w1E (agents · group-chat)" in out[-1]
    assert not any(tuple(call[:2]) in ADOPT_MUTATIONS for call in calls)


def test_adopt_peers_recognizes_the_pi_xai_grok_profile_peer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))
    calls = install_adopt_host(
        monkeypatch,
        adopt_live_agents("w1E", str(tmp_path), names=("grok46pi-peer",)),
        pane_screens={"w1E:p-grok46pi": GROK_PI_POST_TURN_SCREEN},
    )

    assert module.adopt_stale_peers() == 0

    state = load_launcher_state(tmp_path)
    assert state["participant_pane_ids"] == {"grok": "w1E:p-grok46pi"}
    assert state["participant_tab_ids"] == {"grok": "w1E:t-grok46pi"}
    assert ["pi", "--list-models", "grok-4.6"] in calls
    assert "adopted @grok (grok46pi-peer, pane w1E:p-grok46pi)" in capsys.readouterr().out
    assert not any(tuple(call[:2]) in ADOPT_MUTATIONS for call in calls)

    before = load_launcher_state(tmp_path)
    conflicting_calls = install_adopt_host(
        monkeypatch,
        adopt_live_agents("w1E", str(tmp_path), names=("grok46-peer", "grok46pi-peer")),
        pane_screens={
            "w1E:p-grok46": GROK_POST_TURN_SCREEN,
            "w1E:p-grok46pi": GROK_PI_POST_TURN_SCREEN,
        },
    )
    with pytest.raises(BootstrapError, match="grok46-peer, grok46pi-peer are both live"):
        module.adopt_stale_peers()

    assert load_launcher_state(tmp_path) == before
    assert not any(tuple(call[:2]) in ADOPT_MUTATIONS for call in conflicting_calls)


def test_adopt_peers_leaves_a_previous_placeholder_workspace_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))
    save_launcher_state(
        tmp_path,
        {
            "agents_workspace_id": "w1Z",
            "agents_placeholder_tab_id": "w1Z:t-placeholder",
        },
    )
    calls = install_adopt_host(
        monkeypatch,
        adopt_live_agents("w1E", str(tmp_path)),
        workspaces=[
            {"workspace_id": "w1E", "label": "agents · group-chat"},
            {"workspace_id": "w1Z", "label": "agents · group-chat"},
        ],
    )

    assert module.adopt_stale_peers() == 0

    state = load_launcher_state(tmp_path)
    assert state["agents_workspace_id"] == "w1E"
    assert state["agents_placeholder_tab_id"] == "w1Z:t-placeholder"
    summary = capsys.readouterr().out.splitlines()[-1]
    assert "previous placeholder workspace w1Z was left untouched" in summary
    assert not any(tuple(call[:2]) in ADOPT_MUTATIONS for call in calls)


def _adopt_kind_mismatch_live() -> list[dict]:
    live = adopt_live_agents("w1E", "/gone", names=("sol-peer", "codex-peer"))
    live[1]["kind"] = "pi"  # codex-peer is reported as a pi agent
    return live


@pytest.mark.parametrize(
    ("live", "screens", "workspaces", "needle", "other_state"),
    [
        pytest.param(
            _adopt_kind_mismatch_live(),
            None,
            None,
            "@codex: codex-peer is live with a different agent kind",
            None,
            id="kind-mismatch",
        ),
        pytest.param(
            adopt_live_agents("w1E", "/gone", names=("sol-peer",)),
            # No fresh footer and no reopen evidence for Sol: adoption must refuse.
            {"w1E:p-sol": "Pi\nmodel gpt-5.6-sol-01 • high\n"},
            None,
            "@sol: sol-peer failed native-ui verification",
            None,
            id="native-proof",
        ),
        pytest.param(
            [
                *adopt_live_agents("w1E", "/gone", names=("sol-peer",)),
                *adopt_live_agents("w2E", "/gone", names=("fable-peer",)),
            ],
            {"w1E:p-sol": SOL_SCREEN, "w2E:p-fable": FABLE_POST_TURN_SCREEN},
            [
                {"workspace_id": "w1E", "label": "agents · group-chat"},
                {"workspace_id": "w2E", "label": "agents · group-chat"},
            ],
            "the live peers span several workspaces: w1E, w2E",
            None,
            id="two-workspaces",
        ),
        pytest.param(
            adopt_live_agents("w1E", "/gone"),
            None,
            None,
            "workspace w1E is still recorded by launcher-state-deadbee000.json",
            {"schema_version": 1, "agents_workspace_id": "w1E"},
            id="claimed-by-other-state",
        ),
        pytest.param(
            [
                *adopt_live_agents("w1E", "/gone", names=("sol-peer", "fable-peer")),
                *adopt_live_agents("w1E", "/elsewhere", names=("grok46-peer",)),
            ],
            {
                "w1E:p-sol": SOL_SCREEN,
                "w1E:p-fable": FABLE_POST_TURN_SCREEN,
                "w1E:p-grok46": GROK_POST_TURN_SCREEN,
            },
            None,
            "grok46-peer (/elsewhere)",
            None,
            id="differing-cwds",
        ),
        pytest.param(
            [],
            None,
            None,
            "no configured group-chat participant is live",
            None,
            id="nothing-live",
        ),
    ],
)
def test_adopt_peers_refuses_and_adopts_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live: list[dict],
    screens: dict[str, str] | None,
    workspaces: list[dict] | None,
    needle: str,
    other_state: dict | None,
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))
    if other_state is not None:
        (tmp_path / "launcher-state-deadbee000.json").write_text(
            json.dumps(other_state), encoding="utf-8"
        )
    calls = install_adopt_host(monkeypatch, live, workspaces=workspaces, pane_screens=screens)

    with pytest.raises(BootstrapError) as excinfo:
        module.adopt_stale_peers()

    error = excinfo.value
    assert error.code == "adopt_refused"
    assert needle in str(error)
    assert error.failures and any(needle in failure for failure in error.failures)
    assert load_launcher_state(tmp_path) == {"schema_version": module.LAUNCHER_STATE_VERSION}
    assert not any(tuple(call[:2]) in ADOPT_MUTATIONS for call in calls)


def test_adopt_peers_ignores_claims_of_workspaces_that_no_longer_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restart-dead launcher states point at workspaces Herdr deleted."""
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))
    (tmp_path / "launcher-state-deadbee000.json").write_text(
        json.dumps({"schema_version": 1, "agents_workspace_id": "w3"}), encoding="utf-8"
    )
    calls = install_adopt_host(monkeypatch, adopt_live_agents("w1E", str(tmp_path)))

    assert module.adopt_stale_peers() == 0
    assert load_launcher_state(tmp_path)["agents_workspace_id"] == "w1E"
    assert not any(tuple(call[:2]) in ADOPT_MUTATIONS for call in calls)


def test_adopt_then_atomic_profile_launch_succeeds_against_the_same_fake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    profile_launch_env(tmp_path, monkeypatch, captured)
    monkeypatch.setenv(module.PROFILE_ENV, "sol-fable-grok")
    monkeypatch.setenv(module.ROOM_ENV, "chat-sfg")
    calls = install_adopt_host(
        monkeypatch,
        [
            *adopt_live_agents("w1E", str(tmp_path), names=("sol-peer", "fable-peer")),
            *adopt_live_agents("w1E", str(tmp_path), names=("grok46-peer",)),
        ],
        # The launch after adoption re-verifies Fable on reuse with the fresh
        # banner, so its pane keeps the startup screen, which also carries the
        # reopen `Fable 5` sequence; the reopen process argv is still supplied.
        pane_screens={
            "w1E:p-sol": SOL_SCREEN,
            "w1E:p-fable": FABLE_SCREEN,
            "w1E:p-grok46": GROK_POST_TURN_SCREEN,
        },
    )
    state = sfg_room_state(tmp_path)
    state.pop("agents_workspace_id")  # the restart wiped this launcher's records
    state.pop("agents_cwd")
    state.pop("participant_pane_ids")
    state.pop("participant_tab_ids")
    save_launcher_state(tmp_path, state)

    assert module.adopt_stale_peers() == 0
    module.main()

    argv = captured["argv"]
    assert argv[argv.index("--profile") + 1] == "sol-fable-grok"
    mappings = [value for index, value in enumerate(argv) if argv[index - 1] == "--agent"]
    assert mappings == ["sol=sol-peer", "fable=fable-peer", "grok=grok46-peer"]
    # The adopted sessions are reused and re-verified, never restarted.
    assert not any(call[:2] in (["tab", "create"], ["agent", "start"]) for call in calls)
    final_state = load_launcher_state(tmp_path)
    assert final_state["selected_profile"] == "sol-fable-grok"
    assert final_state["agents_workspace_id"] == "w1E"


def test_profile_incomplete_message_and_record_carry_each_role_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    profile_launch_env(tmp_path, monkeypatch, captured)
    monkeypatch.setenv(module.PROFILE_ENV, "sol-fable-grok")
    monkeypatch.setenv(module.ROOM_ENV, "chat-sfg")
    live = [
        {**agent, "workspace_id": "w-old"}
        if agent["name"] == "sol-peer"
        else {**agent, "cwd": str(tmp_path / "elsewhere")}
        if agent["name"] == "fable-peer"
        else agent
        for agent in SFG_LIVE_AGENTS
    ]
    calls = install_sfg_host(
        monkeypatch,
        tmp_path,
        {
            "w-agents:p-sol": SOL_SCREEN,
            "w-agents:p-fable": FABLE_SCREEN,
            "w-agents:p-grok": GROK_SCREEN,
        },
        live_agents=live,
    )
    # install_sfg_host normalises every cwd to tmp_path; move Fable afterwards.
    live[1]["cwd"] = str(tmp_path / "elsewhere")
    state = sfg_room_state(tmp_path)
    # This session recorded Sol and Fable, but never Grok's pane.
    state["participant_pane_ids"] = {"sol": "w-agents:p-sol", "fable": "w-agents:p-fable"}
    state["participant_tab_ids"] = {"sol": "w-agents:t-sol", "fable": "w-agents:t-fable"}
    save_launcher_state(tmp_path, state)

    with pytest.raises(BootstrapError) as excinfo:
        module.main()

    error = excinfo.value
    assert error.code == "profile_incomplete"
    message = str(error)
    assert (
        "- @sol: sol-peer is live in workspace w-old but this launcher owns "
        "w-agents (different workspace)" in message
    )
    assert (
        "- @fable: fable-peer is live in workspace w-agents but this launcher owns "
        "w-agents (different cwd)" in message
    )
    assert (
        "- @grok: grok46-peer is live in workspace w-agents but this launcher owns "
        "w-agents (pane not recorded)" in message
    )
    assert "run the Adopt stale group-chat peers action or close that peer tab" in message
    assert error.failures == [
        "@sol: sol-peer is live in workspace w-old but this launcher owns w-agents "
        "(different workspace); run the Adopt stale group-chat peers action or "
        "close that peer tab",
        "@fable: fable-peer is live in workspace w-agents but this launcher owns "
        "w-agents (different cwd); run the Adopt stale group-chat peers action or "
        "close that peer tab",
        "@grok: grok46-peer is live in workspace w-agents but this launcher owns "
        "w-agents (pane not recorded); run the Adopt stale group-chat peers action "
        "or close that peer tab",
    ]
    record = module._launcher_error_record(
        "--launch", ["--launch", "--profile", "sol-fable-grok"], error
    )
    assert record["failures"] == error.failures
    plain = module._launcher_error_record("setup", [], BootstrapError("boom"))
    assert "failures" not in plain
    assert "argv" not in captured
    # Nothing was closed while refusing: the peers stay exactly as they were.
    assert not any(tuple(call[:2]) in (("tab", "close"), ("pane", "close")) for call in calls)


# --- durable launcher failure records --------------------------------------------------


class _FakeStdin:
    """A TTY stdin whose readline can be observed without touching the test runner."""

    def __init__(self) -> None:
        self.readline_calls = 0

    def isatty(self) -> bool:
        return True

    def readline(self) -> str:
        self.readline_calls += 1
        return "\n"


def _record_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    arguments: list[str] | None = None,
) -> int:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w-ctx")
    monkeypatch.setenv("HERDR_TAB_ID", "w-ctx:t-ctx")
    monkeypatch.setenv("HERDR_PANE_ID", "w-ctx:p-ctx")
    monkeypatch.setenv(module.PROFILE_ENV, "sol-fable")
    monkeypatch.setenv(
        "HERDR_PLUGIN_CONTEXT_JSON",
        json.dumps(
            {
                "workspace_id": "w-ctx",
                "tab_id": "w-ctx:t-ctx",
                "focused_pane_id": "w-ctx:p-focus",
                "focused_pane_cwd": "/never/recorded",
            }
        ),
    )
    monkeypatch.setattr(module, "main", lambda: (_ for _ in ()).throw(error))

    try:
        module.run_launcher(arguments or [])
    except SystemExit as exited:
        assert exited.code is not None
        return exited.code
    raise AssertionError("a recorded failure must exit")


def test_bootstrap_failure_is_recorded_and_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = _record_failure(tmp_path, monkeypatch, BootstrapError("boom", code="plugin_x"))
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "new-room: boom" in captured.err

    error_path = tmp_path / "launcher-errors.jsonl"
    lines = error_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["mode"] == "setup"
    assert record["argv"] == []
    assert record["pid"] == os.getpid()
    assert record["error_type"] == "BootstrapError"
    assert record["code"] == "plugin_x"
    assert record["message"] == "boom"
    assert record["traceback"] is None
    assert record["at"].endswith("+00:00")
    assert record["herdr"] == {
        "workspace_id": "w-ctx",
        "tab_id": "w-ctx:t-ctx",
        "pane_id": "w-ctx:p-ctx",
        "plugin_state_dir": str(tmp_path),
        "profile": "sol-fable",
        "plugin_context": {
            "workspace_id": "w-ctx",
            "tab_id": "w-ctx:t-ctx",
            "focused_pane_id": "w-ctx:p-focus",
        },
    }
    assert stat.S_IMODE(error_path.stat().st_mode) == 0o600


def test_unexpected_failure_is_recorded_with_traceback_and_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = _record_failure(tmp_path, monkeypatch, RuntimeError("kaboom"))
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Traceback" in captured.err
    assert "RuntimeError: kaboom" in captured.err

    record = json.loads(
        (tmp_path / "launcher-errors.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert record["error_type"] == "RuntimeError"
    assert record["code"] is None
    assert "RuntimeError: kaboom" in record["traceback"]


def test_hold_skipped_under_no_hold_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(module.NO_HOLD_ENV, "1")
    monkeypatch.setattr(
        module.select,
        "select",
        lambda *_args: (_ for _ in ()).throw(AssertionError("hold must be skipped")),
    )
    monkeypatch.setattr(sys, "stdin", _FakeStdin())

    exit_code = _record_failure(tmp_path, monkeypatch, BootstrapError("boom", code="plugin_x"))

    assert exit_code == 2
    assert module.HOLD_PROMPT not in capsys.readouterr().out


def test_hold_waits_for_enter_with_sixty_second_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[object, float]] = []
    stdin = _FakeStdin()

    def fake_select(
        rlist: list[object], _wlist: object, _xlist: object, timeout: float
    ) -> tuple[list[object], list[object], list[object]]:
        calls.append((rlist, timeout))
        return [], [], []

    monkeypatch.setattr(module.select, "select", fake_select)
    monkeypatch.setattr(sys, "stdin", stdin)

    exit_code = _record_failure(tmp_path, monkeypatch, BootstrapError("boom", code="plugin_x"))

    assert exit_code == 2
    assert module.HOLD_PROMPT in capsys.readouterr().out
    assert calls == [([stdin], module.HOLD_TIMEOUT_SECONDS)]
    assert module.HOLD_TIMEOUT_SECONDS == 60.0
    assert stdin.readline_calls == 0


def test_hold_returns_immediately_after_enter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdin = _FakeStdin()

    def fake_select(
        rlist: list[object], _wlist: object, _xlist: object, timeout: float
    ) -> tuple[list[object], list[object], list[object]]:
        del timeout
        return rlist, [], []

    monkeypatch.setattr(module.select, "select", fake_select)
    monkeypatch.setattr(sys, "stdin", stdin)

    exit_code = _record_failure(tmp_path, monkeypatch, BootstrapError("boom", code="plugin_x"))

    assert exit_code == 2
    assert stdin.readline_calls == 1


def test_last_error_prints_most_recent_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))
    module.append_launcher_error("--room-entrypoint", ["--room-entrypoint"], BootstrapError("old"))
    module.append_launcher_error("setup", [], BootstrapError("newest", code="plugin_x"))

    assert module.run_launcher(["--last-error"]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["message"] == "newest"
    assert record["code"] == "plugin_x"
    assert record["mode"] == "setup"


def test_errors_prints_last_n_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))
    for index in range(3):
        module.append_launcher_error("setup", [], BootstrapError(f"failure-{index}"))

    assert module.run_launcher(["--errors", "2"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["message"] for line in lines] == ["failure-1", "failure-2"]


def test_last_error_without_records_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))

    assert module.run_launcher(["--last-error"]) == 0
    assert capsys.readouterr().out.strip() == "no launcher errors recorded"


def test_error_records_are_capped_at_two_hundred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))
    for index in range(205):
        module.append_launcher_error("setup", [], BootstrapError(f"failure-{index}"))

    exit_code = _record_failure(tmp_path, monkeypatch, BootstrapError("boom", code="plugin_x"))
    assert exit_code == 2
    capsys.readouterr()

    lines = (tmp_path / "launcher-errors.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == module.LAUNCHER_ERRORS_MAX_RECORDS == 200
    assert json.loads(lines[0])["message"] == "failure-6"
    assert json.loads(lines[-1])["message"] == "boom"


def test_inspection_argument_parsing_is_exact() -> None:
    assert module.parse_launch_arguments(["--last-error"]) == ("--last-error", None)
    assert module.parse_launch_arguments(["--errors", "5"]) == ("--errors", None)
    with pytest.raises(BootstrapError, match="unknown arguments"):
        module.parse_launch_arguments(["--last-error", "extra"])
    for arguments in (["--errors"], ["--errors", "0"], ["--errors", "x"], ["--errors", "1", "2"]):
        with pytest.raises(BootstrapError, match="requires a positive record count"):
            module.parse_launch_arguments(arguments)
