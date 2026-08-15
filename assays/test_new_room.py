from __future__ import annotations

import json
import os
import re
import stat
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
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
    launcher_state_path(tmp_path).write_text('{"schema_version":99}\n', encoding="utf-8")

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
        workspace_id = "w-owned-group-chat" if "group-chat" in label else "w-owned-agents"
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
        assert arguments == ["workspace", "rename", "w-exact", "agents"]
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
        ["plugin", "pane", "focus", "w-chat:p-owned"],
        ["plugin", "pane", "focus", "w-chat:p-owned"],
    ]
    assert json.loads(capsys.readouterr().out)["result"]["type"] == "plugin_pane_focused"


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

    assert failures and "outside this plugin-owned project" in failures[0]
    assert calls == [["agent", "list"]]


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

    assert first and second
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


def test_transient_focus_failure_does_not_open_a_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_launcher_state(tmp_path, {"room_pane_id": "w-chat:p-current"})
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path))
    calls: list[list[str]] = []

    def fake_run_json(_herdr_bin: str, arguments: list[str], timeout: int = 30) -> dict:
        del timeout
        calls.append(arguments)
        raise BootstrapError("temporary failure", code="server_unavailable")

    monkeypatch.setattr(module, "run_json", fake_run_json)

    with pytest.raises(BootstrapError, match="temporary failure"):
        launch_room(open_existing=True)
    assert calls == [["plugin", "pane", "focus", "w-chat:p-current"]]


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
        if arguments == ["plugin", "pane", "focus", "w-chat:p-old"]:
            return {
                "result": {
                    "plugin_pane": {
                        "pane": {
                            "pane_id": "w-chat:p-old",
                            "workspace_id": "w-chat",
                            "tab_id": "w-chat:t-old",
                        }
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

    assert launch_room(open_existing=True) == 0
    assert "chat_placeholder_tab_id" not in load_launcher_state(tmp_path)


def test_fresh_room_ids_are_unique_and_valid() -> None:
    first = new_room_id()
    second = new_room_id()

    assert first != second
    assert re.fullmatch(r"chat-\d{8}-\d{6}-[a-f0-9]{6}", first)
    assert len(first) <= 32
