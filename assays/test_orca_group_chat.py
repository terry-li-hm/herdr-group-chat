from __future__ import annotations

import re
import subprocess
import sys
import threading
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest

EFFECTOR = Path(__file__).resolve().parent.parent / "orca-group-chat"
loader = SourceFileLoader("orca_group_chat", str(EFFECTOR))
spec = spec_from_loader(loader.name, loader)
assert spec is not None
module = module_from_spec(spec)
sys.modules[module.__name__] = module
loader.exec_module(module)
namespace = module.__dict__

ChatError = namespace["ChatError"]
OrcaClient = namespace["OrcaClient"]
parse_agent_mappings = namespace["parse_agent_mappings"]
resolve_state_dir = namespace["resolve_state_dir"]


def json_result(payload: str) -> str:
    return '{"ok":true,"result":' + payload + "}"


def test_agent_mappings_require_explicit_unique_terminal_handles() -> None:
    assert parse_agent_mappings(["pi=term_pi", "Claude=term_claude", "codex=term_codex"]) == {
        "pi": "term_pi",
        "claude": "term_claude",
        "codex": "term_codex",
    }
    with pytest.raises(ChatError, match="at least one --agent"):
        parse_agent_mappings([])
    with pytest.raises(ChatError, match="duplicate agent mapping"):
        parse_agent_mappings(["pi=term_old", "PI=term_new"])
    with pytest.raises(ChatError, match="duplicate terminal handle"):
        parse_agent_mappings(["pi=term_shared", "claude=term_shared"])
    with pytest.raises(ChatError, match="invalid agent mapping"):
        parse_agent_mappings(["reviewer=term_review"])


def test_state_dir_precedence(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit"
    configured = tmp_path / "configured"
    assert resolve_state_dir(explicit, {"ORCA_GROUP_CHAT_STATE_DIR": str(configured)}) == explicit
    assert resolve_state_dir(None, {"ORCA_GROUP_CHAT_STATE_DIR": str(configured)}) == configured
    assert resolve_state_dir(None, {}) == Path("~/.local/state/orca-group-chat")


def test_live_targets_and_states_come_from_exact_terminal_handles() -> None:
    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        assert argv == ["orca-test", "terminal", "list", "--json"]
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json_result(
                '{"terminals":['
                '{"handle":"term_ready","connected":true,"writable":true,"orphaned":false},'
                '{"handle":"term_readonly","connected":true,"writable":false,"orphaned":false},'
                '{"handle":"term_gone","connected":false,"writable":true,"orphaned":true}'
                "]}"
            ),
            stderr="",
        )

    client = OrcaClient(orca_bin="orca-test", runner=runner)

    assert client.live_targets() == {"term_ready"}
    assert client.states() == {
        "term_ready": "ready",
        "term_readonly": "offline",
        "term_gone": "offline",
    }


def test_turn_sends_waits_and_reads_token_bound_response_file(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    token = "a" * 32

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["terminal", "send"]:
            prompt = argv[argv.index("--text") + 1]
            output_match = re.search(r"ORCA_GROUP_CHAT_RESPONSE_FILE: (.+)", prompt)
            assert output_match is not None
            output = Path(output_match.group(1))
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                f"HGCHAT_REPLY_BEGIN {token}\nanswer from Orca\nHGCHAT_REPLY_END {token}\n",
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(argv, 0, stdout=json_result("{}"), stderr="")

    client = OrcaClient(orca_bin="orca-test", state_dir=tmp_path, runner=runner)
    status, reply = client.turn(
        "term_pi",
        f"prompt\nHGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}",
        12_000,
    )

    assert (status, reply) == ("done", "answer from Orca")
    response_file = next((tmp_path / "responses").glob("*.txt"))
    assert response_file.stat().st_mode & 0o777 == 0o600
    assert calls[0][:5] == ["orca-test", "terminal", "send", "--terminal", "term_pi"]
    assert calls[0][-2:] == ["--enter", "--json"]
    assert calls[1] == [
        "orca-test",
        "terminal",
        "wait",
        "--terminal",
        "term_pi",
        "--for",
        "tui-idle",
        "--timeout-ms",
        "12000",
        "--json",
    ]
    assert len(calls) == 2


def test_turn_falls_back_to_bounded_terminal_read(tmp_path: Path) -> None:
    token = "b" * 32
    calls: list[list[str]] = []

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["terminal", "read"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json_result(
                    '{"terminal":{"tail":['
                    f'"HGCHAT_REPLY_BEGIN {token}","fallback answer","HGCHAT_REPLY_END {token}"'
                    "]}}"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(argv, 0, stdout=json_result("{}"), stderr="")

    client = OrcaClient(orca_bin="orca-test", state_dir=tmp_path, runner=runner)
    assert client.turn(
        "term_codex",
        f"prompt\nHGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}",
        12_000,
    ) == ("done", "fallback answer")
    assert calls[-1] == [
        "orca-test",
        "terminal",
        "read",
        "--terminal",
        "term_codex",
        "--limit",
        "1000",
        "--json",
    ]


@pytest.mark.parametrize("stale_wait", [False, True])
def test_wait_rendezvous_collects_a_delayed_token_bound_response_file(
    tmp_path: Path, stale_wait: bool
) -> None:
    token = "e" * 32
    response_file = tmp_path / "responses" / f"{token}.txt"
    writer: threading.Thread | None = None

    def write_response() -> None:
        response_file.parent.mkdir(parents=True, exist_ok=True)
        response_file.write_text(
            f"HGCHAT_REPLY_BEGIN {token}\ndelayed answer\nHGCHAT_REPLY_END {token}\n",
            encoding="utf-8",
        )

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal writer
        if argv[1:3] == ["terminal", "wait"]:
            writer = threading.Timer(0.02, write_response)
            writer.start()
            if stale_wait:
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    stdout='{"ok":false,"error":{"code":"terminal_handle_stale",'
                    '"message":"terminal_handle_stale"}}',
                    stderr="",
                )
        return subprocess.CompletedProcess(argv, 0, stdout=json_result("{}"), stderr="")

    client = OrcaClient(orca_bin="orca-test", state_dir=tmp_path, runner=runner)
    assert client.turn(
        "term_pi",
        f"prompt\nHGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}",
        1_000,
    ) == ("done", "delayed answer")
    assert writer is not None
    writer.join(timeout=1)
    assert response_file.stat().st_mode & 0o777 == 0o600


def test_cancel_targets_only_the_exact_terminal_handle() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=json_result("{}"), stderr="")

    OrcaClient(orca_bin="orca-test", runner=runner).cancel("term_claude")
    assert calls == [
        [
            "orca-test",
            "terminal",
            "send",
            "--terminal",
            "term_claude",
            "--interrupt",
            "--json",
        ]
    ]


def test_immediate_review_cancel_interrupts_after_submission(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    cancel_event = threading.Event()
    token = "c" * 32

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["terminal", "send"] and "--interrupt" not in argv:
            cancel_event.set()
        return subprocess.CompletedProcess(argv, 0, stdout=json_result("{}"), stderr="")

    client = OrcaClient(orca_bin="orca-test", state_dir=tmp_path, runner=runner)
    with pytest.raises(ChatError, match="review cancelled"):
        client.turn(
            "term_grok",
            f"prompt\nHGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}",
            12_000,
            cancel_event,
        )

    assert [call[1:3] for call in calls] == [
        ["terminal", "send"],
        ["terminal", "send"],
    ]
    assert "--interrupt" in calls[-1]


def test_cancel_interrupts_a_running_terminal_wait(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    wait_calls: list[list[str]] = []
    cancel_event = threading.Event()
    token = "d" * 32

    class WaitingProcess:
        def __init__(self) -> None:
            self.returncode = 0
            self.terminated = False

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            if not self.terminated:
                cancel_event.set()
                raise subprocess.TimeoutExpired("orca-test", timeout)
            return "", ""

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True

    process = WaitingProcess()

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=json_result("{}"), stderr="")

    def popen_factory(argv: list[str], **_: object) -> WaitingProcess:
        wait_calls.append(argv)
        return process

    client = OrcaClient(
        orca_bin="orca-test",
        state_dir=tmp_path,
        runner=runner,
        popen_factory=popen_factory,
    )
    with pytest.raises(ChatError, match="review cancelled"):
        client.turn(
            "term_claude",
            f"prompt\nHGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}",
            12_000,
            cancel_event,
        )

    assert process.terminated
    assert wait_calls == [
        [
            "orca-test",
            "terminal",
            "wait",
            "--terminal",
            "term_claude",
            "--for",
            "tui-idle",
            "--timeout-ms",
            "12000",
            "--json",
        ]
    ]
    assert [call[1:3] for call in calls] == [
        ["terminal", "send"],
        ["terminal", "send"],
    ]
    assert "--interrupt" in calls[-1]


def test_orca_json_error_is_user_visible() -> None:
    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout='{"ok":false,"error":{"code":"terminal_handle_stale","message":"gone"}}',
            stderr="",
        )

    client = OrcaClient(orca_bin="orca-test", runner=runner)
    with pytest.raises(ChatError, match=r"terminal_handle_stale.*gone"):
        client.live_targets()
