from __future__ import annotations

import json
import multiprocessing
import os
import re
import stat
import subprocess
import sys
import threading
import tomllib
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from os import X_OK, access
from pathlib import Path

import pytest

EFFECTOR = Path(__file__).resolve().parent.parent / "herdr-group-chat"
loader = SourceFileLoader("herdr_group_chat", str(EFFECTOR))
spec = spec_from_loader(loader.name, loader)
assert spec is not None
module = module_from_spec(spec)
sys.modules[module.__name__] = module
loader.exec_module(module)
namespace = module.__dict__

ChatError = namespace["ChatError"]
GroupChat = namespace["GroupChat"]
HerdrClient = namespace["HerdrClient"]
ReviewController = namespace["ReviewController"]
Route = namespace["Route"]
Transcript = namespace["Transcript"]
extract_reply = namespace["extract_reply"]
extract_grok_session_reply = namespace["extract_grok_session_reply"]
extract_claude_session_reply = namespace.get("extract_claude_session_reply")
claude_project_dir_name = namespace.get("claude_project_dir_name")
build_prompt = namespace["build_prompt"]
build_review_prompt = namespace["build_review_prompt"]
build_synthesis_prompt = namespace["build_synthesis_prompt"]
message_lines = namespace["message_lines"]
parse_agent_timeouts = namespace["parse_agent_timeouts"]
parse_route = namespace["parse_route"]
parse_anneal = namespace["parse_anneal"]
parse_consensus_verdict = namespace.get("parse_consensus_verdict")
main = namespace["main"]
resolve_state_dir = namespace["resolve_state_dir"]
participant_status = namespace["participant_status"]
handle_local_command = namespace["handle_local_command"]
handle_view_command = namespace["handle_view_command"]
mention_fragment = namespace["mention_fragment"]
mention_suggestions = namespace["mention_suggestions"]
complete_mention = namespace["complete_mention"]
mention_display = namespace["mention_display"]
handle_picker_key = namespace["handle_picker_key"]
inbox_messages = namespace["inbox_messages"]
inbox_rendered_lines = namespace["inbox_rendered_lines"]
visible_message_lines = namespace["visible_message_lines"]


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.timeouts: list[int | None] = []
        self.focused: list[str] = []
        self.cancelled: list[str] = []

    def live_targets(self) -> set[str]:
        return {"pi-peer", "claude-peer", "codex-peer", "grok-peer"}

    def states(self) -> dict[str, str]:
        return {
            "pi-peer": "idle",
            "claude-peer": "working",
            "codex-peer": "done",
            "grok-peer": "blocked",
        }

    def focus(self, target: str) -> None:
        self.focused.append(target)

    def focus_workspace(self, workspace_id: str) -> None:
        self.focused.append(f"workspace:{workspace_id}")

    def turn(
        self,
        target: str,
        prompt: str,
        timeout_ms: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[str, str]:
        self.calls.append((target, prompt))
        self.timeouts.append(timeout_ms)
        if cancel_event is not None and cancel_event.is_set():
            raise ChatError("review cancelled")
        match = re.search(r"HGCHAT_REPLY_BEGIN ([a-f0-9]{32})", prompt)
        assert match
        return "done", f"reply from {target}"

    def cancel(self, target: str) -> None:
        self.cancelled.append(target)


def make_chat(tmp_path: Path, max_turns: int = 4) -> tuple[object, FakeClient, object]:
    transcript = Transcript(tmp_path, "test-room")
    client = FakeClient()
    chat = GroupChat(
        transcript,
        {
            "pi": "pi-peer",
            "claude": "claude-peer",
            "codex": "codex-peer",
            "grok": "grok-peer",
        },
        client,
        max_turns=max_turns,
        agents_workspace_id="w-agents",
    )
    return chat, client, transcript


def concurrent_transcript_writer(
    state_dir: str,
    room: str,
    writer: str,
    count: int,
    start: object,
    barrier: object,
) -> None:
    transcript = Transcript(Path(state_dir), room)
    assert start.wait(5)
    for index in range(count):
        barrier.wait(timeout=5)
        transcript.append(writer, ("human",), f"{writer}-{index}")


def concurrent_cursor_advance(
    state_dir: str,
    room: str,
    agent: str,
    seq: int,
    start: object,
    barrier: object,
) -> None:
    transcript = Transcript(Path(state_dir), room)
    assert start.wait(5)
    barrier.wait(timeout=5)
    transcript.advance_cursor(agent, seq)


def append_one_message(state_dir: str, room: str, body: str, recipient: str = "pi") -> None:
    Transcript(Path(state_dir), room).append("human", (recipient,), body)


def concurrent_record_profile_receipt(
    state_dir: str, room: str, payload: dict, start: object, barrier: object
) -> None:
    transcript = Transcript(Path(state_dir), room)
    assert start.wait(5)
    barrier.wait(timeout=5)
    namespace["record_profile_receipt"](transcript, payload)


def concurrent_dispatch(
    state_dir: str,
    room: str,
    body: str,
    start: object,
    barrier: object,
    prompts: object,
) -> None:
    class ProcessClient:
        def live_targets(self) -> set[str]:
            return {"pi-peer"}

        def turn(
            self,
            target: str,
            prompt: str,
            timeout_ms: int | None = None,
            cancel_event: threading.Event | None = None,
        ) -> tuple[str, str]:
            prompts.put(prompt)
            return "done", f"reply to {body}"

    transcript = Transcript(Path(state_dir), room)
    assert start.wait(5)
    barrier.wait(timeout=5)
    GroupChat(transcript, {"pi": "pi-peer"}, ProcessClient()).dispatch(f"@pi {body}")


def probe_room_authority(
    state_dir: str,
    room: str,
    authority: str,
    results: object,
) -> None:
    try:
        transcript = Transcript(Path(state_dir), room)
        if authority == "cursor":
            transcript.cursors()
        elif authority == "delivery_lock":
            with transcript.delivery_lock():
                pass
        else:
            transcript.read()
    except ChatError as error:
        results.put(("rejected", str(error)))
    except BaseException as error:
        results.put(("unexpected", repr(error)))
    else:
        results.put(("accepted", ""))


def test_plain_message_routes_to_all_in_stable_order() -> None:
    assert parse_route("hello", ("pi", "claude", "codex", "grok")) == Route(
        ("pi", "claude", "codex", "grok"), "hello"
    )


def test_participant_status_normalizes_ready_and_preserves_attention_states(
    tmp_path: Path,
) -> None:
    chat, _, _ = make_chat(tmp_path)
    assert participant_status(chat, "Ready.") == (
        "Ready. @pi ready · @claude working · @codex ready · @grok blocked"
    )


def test_local_navigation_commands_focus_agents_without_entering_transcript(tmp_path: Path) -> None:
    chat, client, transcript = make_chat(tmp_path)
    assert handle_local_command("/show codex", chat) == "Focused @codex."
    assert handle_local_command("/agents", chat) == "Focused the agents workspace."
    assert handle_local_command("/show nobody", chat) == "Unknown participant: nobody"
    assert handle_local_command("ordinary message", chat) is None
    assert client.focused == ["codex-peer", "workspace:w-agents"]
    assert transcript.read() == []


def test_inbox_keeps_final_deliverables_and_attention_without_review_drafts() -> None:
    messages = [
        {"seq": 1, "sender": "human", "kind": "message", "body": "question"},
        {"seq": 2, "sender": "pi", "kind": "message", "body": "ordinary reply"},
        {"seq": 3, "sender": "claude", "kind": "review_response", "body": "draft"},
        {"seq": 4, "sender": "pi", "kind": "review_synthesis", "body": "decision"},
        {
            "seq": 5,
            "sender": "system",
            "kind": "review_status",
            "body": "@grok review blocked: login required",
        },
        {
            "seq": 6,
            "sender": "system",
            "kind": "review_status",
            "body": "@codex review done",
        },
        {"seq": 7, "sender": "system", "kind": "message", "body": "setup: pi failed"},
        {"seq": 8, "sender": "human", "kind": "review_question", "body": "review this"},
    ]

    assert [item["seq"] for item in inbox_messages(messages)] == [2, 4, 5, 7]


def test_inbox_empty_state_is_explicit_and_nonempty_inbox_uses_message_rendering() -> None:
    assert inbox_rendered_lines([], 80) == [
        "Inbox is clear. Final replies and items needing attention appear here."
    ]
    messages = [{"seq": 1, "sender": "pi", "kind": "message", "body": "answer"}]
    assert inbox_rendered_lines(messages, 80) == message_lines(messages, 80)


def test_view_commands_are_exact_and_do_not_enter_the_transcript(tmp_path: Path) -> None:
    _, _, transcript = make_chat(tmp_path)

    assert handle_view_command("/inbox") == (
        "inbox",
        "Inbox shows final replies, syntheses, and attention items. Use /room to return.",
    )
    assert handle_view_command("/room") == ("room", "Showing the full room transcript.")
    assert handle_view_command("/inbox later") is None
    assert handle_view_command("ordinary message") is None
    assert transcript.read() == []


def test_agents_command_does_not_guess_a_workspace_by_label(tmp_path: Path) -> None:
    transcript = Transcript(tmp_path, "test-room")
    client = FakeClient()
    chat = GroupChat(transcript, {"pi": "pi-peer"}, client)

    assert handle_local_command("/agents", chat) == (
        "The agents workspace is unavailable; use /show <agent>."
    )
    assert client.focused == []


def test_mentions_route_to_named_agents_and_reject_unknown() -> None:
    assert parse_route("@pi,@grok compare", ("pi", "claude", "codex", "grok")) == Route(
        ("pi", "grok"), "compare"
    )
    with pytest.raises(ChatError, match="unknown participant"):
        parse_route("@gemini hello", ("pi", "claude", "codex", "grok"))
    assert parse_route("@Pi hello", ("pi", "claude", "codex", "grok")) == Route(("pi",), "hello")
    for malformed in ("@pi", "@pi:", "@Claude"):
        with pytest.raises(ChatError, match="invalid mention syntax"):
            parse_route(malformed, ("pi", "claude", "codex", "grok"))


def test_transcript_is_append_only_jsonl_with_monotonic_sequence(
    tmp_path: Path,
) -> None:
    transcript = Transcript(tmp_path, "room")
    transcript.append("human", ("pi",), "one")
    transcript.append("pi", ("human",), "two")
    assert [item["seq"] for item in transcript.read()] == [1, 2]
    assert len(transcript.path.read_text(encoding="utf-8").splitlines()) == 2


def test_independent_transcript_writers_have_unique_strict_sequences(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("fork")
    writer_count = 4
    messages_per_writer = 20
    start = context.Event()
    barrier = context.Barrier(writer_count)
    processes = [
        context.Process(
            target=concurrent_transcript_writer,
            args=(
                str(tmp_path),
                "concurrent-room",
                f"writer-{index}",
                messages_per_writer,
                start,
                barrier,
            ),
        )
        for index in range(writer_count)
    ]
    for process in processes:
        process.start()
    start.set()
    try:
        for process in processes:
            process.join(timeout=10)
        assert all(not process.is_alive() for process in processes)
        assert [process.exitcode for process in processes] == [0] * writer_count
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)

    transcript = Transcript(tmp_path, "concurrent-room")
    raw_lines = transcript.path.read_text(encoding="utf-8").splitlines()
    messages = [json.loads(line) for line in raw_lines]
    expected_count = writer_count * messages_per_writer
    assert len(messages) == expected_count
    assert [message["seq"] for message in messages] == list(range(1, expected_count + 1))
    assert {message["body"] for message in messages} == {
        f"writer-{writer}-{index}"
        for writer in range(writer_count)
        for index in range(messages_per_writer)
    }
    assert transcript.path.stat().st_mode & 0o777 == 0o600
    assert transcript.lock_path.stat().st_mode & 0o777 == 0o600


def test_independent_cursor_advances_merge_without_lost_updates(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    start = context.Event()
    barrier = context.Barrier(2)
    processes = [
        context.Process(
            target=concurrent_cursor_advance,
            args=(str(tmp_path), "cursor-room", agent, seq, start, barrier),
        )
        for agent, seq in (("pi", 17), ("claude", 23))
    ]
    for process in processes:
        process.start()
    start.set()
    try:
        for process in processes:
            process.join(timeout=10)
        assert all(not process.is_alive() for process in processes)
        assert [process.exitcode for process in processes] == [0, 0]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)

    transcript = Transcript(tmp_path, "cursor-room")
    assert transcript.cursors() == {"claude": 23, "pi": 17}
    assert transcript.cursor_path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".cursor-room.state.json.*.tmp"))


def test_transcript_descriptor_survives_parent_replacement_for_all_authorities(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "bound-state"
    decoy_dir = tmp_path / "decoy-state"
    transcript = Transcript(state_dir, "bound-room")
    transcript.append("human", ("pi",), "before replacement")
    with transcript.delivery_lock():
        pass
    original_lock_inode = transcript.lock_path.stat().st_ino
    original_delivery_inode = transcript.delivery_lock_path.stat().st_ino

    state_dir.rename(decoy_dir)
    state_dir.mkdir(mode=0o700)
    (state_dir / transcript.path.name).write_text('{"seq":999}\n', encoding="utf-8")
    (state_dir / transcript.path.name).chmod(0o600)

    transcript.append("human", ("pi",), "after replacement")
    transcript.advance_cursor("pi", 2)
    with transcript._process_lock():
        pass
    with transcript.delivery_lock():
        pass

    assert [item["body"] for item in transcript.read()] == [
        "before replacement",
        "after replacement",
    ]
    assert transcript.cursors() == {"pi": 2}
    assert (state_dir / transcript.path.name).read_text(encoding="utf-8").count("999") == 1
    assert (decoy_dir / transcript.lock_path.name).stat().st_ino == original_lock_inode
    assert (decoy_dir / transcript.delivery_lock_path.name).stat().st_ino == original_delivery_inode
    assert not (state_dir / transcript.lock_path.name).exists()
    assert not (state_dir / transcript.delivery_lock_path.name).exists()


def test_transcript_close_is_idempotent_and_context_managed(tmp_path: Path) -> None:
    with Transcript(tmp_path, "close-room") as transcript:
        descriptor = transcript._state_directory_descriptor
        assert descriptor is not None
        transcript.append("human", ("pi",), "closed after context")
    with pytest.raises(OSError):
        os.fstat(descriptor)
    transcript.close()
    with pytest.raises(ChatError, match="transcript is closed"):
        transcript.read()


@pytest.mark.parametrize("failure", ["fstat", "fchmod"])
def test_transcript_constructor_closes_descriptor_on_validation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    opened: list[int] = []
    real_open = namespace["os"].open
    real_fstat = namespace["os"].fstat

    def tracking_open(*args: object, **kwargs: object) -> int:
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(namespace["os"], "open", tracking_open)
    if failure == "fstat":
        monkeypatch.setattr(
            namespace["os"], "fstat", lambda _descriptor: (_ for _ in ()).throw(OSError("injected"))
        )
    else:

        def fail_fchmod(_descriptor: int, _mode: int) -> None:
            raise OSError("injected")

        monkeypatch.setattr(namespace["os"], "fchmod", fail_fchmod)

    with pytest.raises(ChatError, match="invalid state directory"):
        Transcript(tmp_path / "injected-state", "injected-room")
    assert opened
    for descriptor in opened:
        with pytest.raises(OSError):
            real_fstat(descriptor)


def test_existing_state_directory_with_wrong_mode_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "existing-state"
    state_dir.mkdir(mode=0o700)
    state_dir.chmod(0o750)

    with pytest.raises(ChatError, match="invalid state directory authority"):
        Transcript(state_dir, "mode-room")

    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o750


def test_cursor_symlink_is_refused_without_touching_its_target(tmp_path: Path) -> None:
    transcript = Transcript(tmp_path, "symlink-room")
    target = tmp_path / "outside-cursors.json"
    original = '{"outside":41}\n'
    target.write_text(original, encoding="utf-8")
    transcript.cursor_path.symlink_to(target)

    with pytest.raises(ChatError, match="invalid room state"):
        transcript.cursors()
    with pytest.raises(ChatError, match="invalid room state"):
        transcript.advance_cursor("pi", 42)

    assert transcript.cursor_path.is_symlink()
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("authority", ["transcript", "cursor", "lock", "delivery_lock"])
@pytest.mark.parametrize("unsafe_kind", ["symlink", "fifo", "directory"])
def test_room_authority_rejects_unsafe_files_without_blocking_or_touching_targets(
    tmp_path: Path,
    authority: str,
    unsafe_kind: str,
) -> None:
    state_dir = tmp_path / "state"
    transcript = Transcript(state_dir, "unsafe-room")
    authority_path = {
        "transcript": transcript.path,
        "cursor": transcript.cursor_path,
        "lock": transcript.lock_path,
        "delivery_lock": transcript.delivery_lock_path,
    }[authority]
    target = tmp_path / f"outside-{authority}.txt"
    original = b"external authority target\n"
    if unsafe_kind == "symlink":
        target.write_bytes(original)
        authority_path.symlink_to(target)
    elif unsafe_kind == "fifo":
        os.mkfifo(authority_path, 0o600)
    else:
        authority_path.mkdir(mode=0o700)

    context = multiprocessing.get_context("fork")
    results = context.Queue()
    process = context.Process(
        target=probe_room_authority,
        args=(str(state_dir), "unsafe-room", authority, results),
    )
    process.start()
    process.join(timeout=2)
    try:
        assert not process.is_alive(), f"{authority} {unsafe_kind} probe blocked"
        assert process.exitcode == 0
        outcome, detail = results.get(timeout=1)
        assert outcome == "rejected", detail
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        results.close()
        results.join_thread()

    if unsafe_kind == "symlink":
        assert authority_path.is_symlink()
        assert target.read_bytes() == original
    elif unsafe_kind == "fifo":
        assert stat.S_ISFIFO(authority_path.lstat().st_mode)
    else:
        assert authority_path.is_dir()


@pytest.mark.parametrize("authority", ["transcript", "cursor", "lock", "delivery_lock"])
def test_room_authority_rejects_regular_files_without_exact_private_mode(
    tmp_path: Path,
    authority: str,
) -> None:
    transcript = Transcript(tmp_path, "mode-room")
    authority_path = {
        "transcript": transcript.path,
        "cursor": transcript.cursor_path,
        "lock": transcript.lock_path,
        "delivery_lock": transcript.delivery_lock_path,
    }[authority]
    authority_path.write_text("{}\n" if authority == "cursor" else "", encoding="utf-8")
    authority_path.chmod(0o640)

    with pytest.raises(ChatError, match="invalid room state authority"):
        if authority == "cursor":
            transcript.cursors()
        elif authority == "delivery_lock":
            with transcript.delivery_lock():
                pass
        else:
            transcript.read()

    assert stat.S_IMODE(authority_path.stat().st_mode) == 0o640


@pytest.mark.parametrize("path_name", ["path", "cursor_path", "lock_path", "delivery_lock_path"])
def test_room_authority_requires_current_euid_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path_name: str,
) -> None:
    transcript = Transcript(tmp_path, "owner-room")
    authority_path = getattr(transcript, path_name)
    authority_path.write_text("", encoding="utf-8")
    authority_path.chmod(0o600)
    real_euid = os.geteuid()
    monkeypatch.setattr(namespace["os"], "geteuid", lambda: real_euid + 1)

    with pytest.raises(ChatError, match="invalid room state authority"):
        transcript._open_regular(authority_path, os.O_RDONLY)


def test_cursor_replace_fsyncs_file_and_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = Transcript(tmp_path, "fsync-room")
    real_fsync = os.fsync
    synced_types: list[str] = []

    def tracking_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        synced_types.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(descriptor)

    monkeypatch.setattr(namespace["os"], "fsync", tracking_fsync)
    transcript.advance_cursor("pi", 7)

    assert synced_types == ["file", "directory"]


def test_all_dispatch_is_serial_and_incremental(tmp_path: Path) -> None:
    chat, client, transcript = make_chat(tmp_path)
    created = chat.dispatch("@all choose a design")

    assert [target for target, _ in client.calls] == [
        "pi-peer",
        "claude-peer",
        "codex-peer",
        "grok-peer",
    ]
    assert [item["sender"] for item in created] == [
        "human",
        "pi",
        "claude",
        "codex",
        "grok",
    ]
    assert "[pi] reply from pi-peer" in client.calls[1][1]
    assert "[claude] reply from claude-peer" in client.calls[2][1]
    assert "[codex] reply from codex-peer" in client.calls[3][1]
    assert "[pi] reply from pi-peer" not in client.calls[0][1]
    assert len(transcript.read()) == 5

    chat.dispatch("@pi second question")
    second_pi_prompt = client.calls[-1][1]
    assert "[human] second question" in second_pi_prompt
    assert "[human] choose a design" not in second_pi_prompt


def test_message_appended_by_another_process_during_turn_stays_eligible(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "interleaved-state"
    transcript = Transcript(state_dir, "interleaved-room")

    class InterleavingClient(FakeClient):
        def turn(
            self,
            target: str,
            prompt: str,
            timeout_ms: int | None = None,
            cancel_event: threading.Event | None = None,
        ) -> tuple[str, str]:
            self.calls.append((target, prompt))
            self.timeouts.append(timeout_ms)
            if len(self.calls) == 1:
                context = multiprocessing.get_context("fork")
                process = context.Process(
                    target=append_one_message,
                    args=(str(state_dir), "interleaved-room", "arrived during turn"),
                )
                process.start()
                process.join(timeout=2)
                try:
                    assert not process.is_alive()
                    assert process.exitcode == 0
                finally:
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=2)
            return "done", f"reply from {target}"

    client = InterleavingClient()
    chat = GroupChat(transcript, {"pi": "pi-peer"}, client)

    chat.dispatch("@pi first")
    first_messages = transcript.read()
    assert [item["body"] for item in first_messages] == [
        "first",
        "arrived during turn",
        "reply from pi-peer",
    ]
    assert transcript.cursors()["pi"] == first_messages[0]["seq"]

    chat.dispatch("@pi second")
    second_prompt = client.calls[-1][1]
    assert "[human] arrived during turn" in second_prompt
    assert "[human] second" in second_prompt
    assert "[human] first" not in second_prompt


def test_independent_same_agent_dispatches_do_not_duplicate_delivery(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    start = context.Event()
    barrier = context.Barrier(2)
    prompts = context.Queue()
    bodies = ("first concurrent", "second concurrent")
    processes = [
        context.Process(
            target=concurrent_dispatch,
            args=(str(tmp_path), "delivery-room", body, start, barrier, prompts),
        )
        for body in bodies
    ]
    for process in processes:
        process.start()
    start.set()
    try:
        for process in processes:
            process.join(timeout=10)
        assert all(not process.is_alive() for process in processes)
        assert [process.exitcode for process in processes] == [0, 0]
        observed_prompts = [prompts.get(timeout=1) for _ in processes]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
        prompts.close()
        prompts.join_thread()

    for body in bodies:
        assert sum(f"[human] {body}" in prompt for prompt in observed_prompts) == 1
    transcript = Transcript(tmp_path, "delivery-room")
    assert transcript.delivery_lock_path.stat().st_mode & 0o777 == 0o600


def test_max_turns_caps_an_all_round(tmp_path: Path) -> None:
    chat, client, _ = make_chat(tmp_path, max_turns=2)
    chat.dispatch("@all bounded")
    assert [target for target, _ in client.calls] == ["pi-peer", "claude-peer"]


def test_direct_message_calls_only_one_agent(tmp_path: Path) -> None:
    chat, client, _ = make_chat(tmp_path)
    chat.dispatch("@grok answer")
    assert [target for target, _ in client.calls] == ["grok-peer"]


def test_codex_is_addressable_and_roster_is_derived() -> None:
    route = parse_route("@codex review", ("pi", "claude", "codex", "grok"))
    assert route == Route(("codex",), "review")
    prompt = build_prompt("codex", [], "a" * 32, ("pi", "claude", "codex", "grok"))
    assert "a human, @pi, @claude, @codex, @grok" in prompt
    assert "another participant's eligibility never transfers to you" in prompt
    assert "ROUTE_RECEIPT" in prompt


def test_state_dir_precedence(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit path"
    plugin = tmp_path / "plugin path"
    assert resolve_state_dir(explicit, {"HERDR_PLUGIN_STATE_DIR": str(plugin)}) == explicit
    assert resolve_state_dir(None, {"HERDR_PLUGIN_STATE_DIR": str(plugin)}) == plugin
    assert resolve_state_dir(None, {}) == Path("~/.local/state/herdr-group-chat")


def test_extract_reply_uses_the_last_marker_pair() -> None:
    token = "a" * 32
    terminal = (
        f"prompt HGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}\n"
        f"HGCHAT_REPLY_BEGIN {token}\nactual answer\nHGCHAT_REPLY_END {token}\n"
    )
    assert extract_reply(terminal, token) == "actual answer"


def test_extract_reply_accepts_terminal_wrapping_inside_markers() -> None:
    token = "c" * 32
    terminal = (
        "HGCHAT_REPLY_BEGIN\n"
        f" {token[:20]}\n"
        f" {token[20:]}\n"
        "wrapped answer\n"
        "HGCHAT_REPLY_END\n"
        f" {token[:17]}\n"
        f" {token[17:]}\n"
    )
    assert extract_reply(terminal, token) == "wrapped answer"


def test_extract_reply_ignores_grok_timestamp_on_marker_line() -> None:
    token = "d" * 32
    terminal = (
        "HGCHAT_REPLY_BEGIN               1:55 PM   █\n"
        f"{token[:30]}\n"
        f"{token[30:]}\n"
        "Grok answer\n"
        "HGCHAT_REPLY_END\n"
        f"{token[:30]}\n"
        f"{token[30:]}\n"
    )
    assert extract_reply(terminal, token) == "Grok answer"


def test_extract_reply_removes_grok_tui_timestamp_chrome() -> None:
    token = "b" * 32
    terminal = (
        f"HGCHAT_REPLY_BEGIN {token}\n"
        "I am here with my     1:12 PM\n"
        "     ordinary tools.\n"
        f"HGCHAT_REPLY_END {token}\n"
    )
    assert extract_reply(terminal, token) == "I am here with my ordinary tools."


def test_extract_grok_session_reply_recovers_hidden_alternate_screen_output(
    tmp_path: Path,
) -> None:
    token = "e" * 32
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    history = session_dir / "chat_history.jsonl"
    history.write_text(
        "\n".join(
            [
                '{"type":"assistant","content":"older answer"}',
                '{"malformed":',
                '{"type":"assistant","content":"HGCHAT_REPLY_BEGIN '
                + token
                + "\\nrecovered Grok reply\\nHGCHAT_REPLY_END "
                + token
                + '"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert extract_grok_session_reply(session_dir, token) == "recovered Grok reply"


def write_claude_session(projects: Path, cwd: str, session_id: str, token: str) -> Path:
    """Create a synthetic Claude session JSONL under ~/.claude/projects."""
    session_dir = projects / claude_project_dir_name(cwd)
    session_dir.mkdir(parents=True)
    session_file = session_dir / f"{session_id}.jsonl"
    records = [
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": (
                    "\u276f You are @claude, a full participant in a local group chat\n"
                    "HGCHAT_REPLY_BEGIN " + token
                ),
            },
        },
        {"type": "summary", "summary": "collapsed prompt summary"},
        {"not": "a real record"},
        "not json at all",
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "thinking about "},
                    {"type": "tool_use", "name": "Read", "input": {}},
                    {
                        "type": "text",
                        "text": (
                            "HGCHAT_REPLY_BEGIN "
                            + token
                            + "\nclean Claude reply\nHGCHAT_REPLY_END "
                            + token
                        ),
                    },
                ],
            },
        },
    ]
    session_file.write_text(
        "\n".join(record if isinstance(record, str) else json.dumps(record) for record in records)
        + "\n",
        encoding="utf-8",
    )
    return session_file


def test_extract_claude_session_reply_prefers_clean_transcript_over_tui_artifact(
    tmp_path: Path,
) -> None:
    assert extract_claude_session_reply is not None
    token = "c" * 32
    cwd = "/work space/herdr_rooms/claude-room"
    session_id = "11111111-2222-3333-4444-555555555555"
    session_file = write_claude_session(tmp_path / ".claude" / "projects", cwd, session_id, token)

    assert extract_claude_session_reply(session_file, token) == "clean Claude reply"


def test_extract_claude_session_reply_fails_closed_on_missing_or_markerless_sessions(
    tmp_path: Path,
) -> None:
    assert extract_claude_session_reply is not None
    token = "d" * 32
    missing = tmp_path / "absent.jsonl"
    markerless = tmp_path / "markerless.jsonl"
    markerless.write_text(
        '{"type":"assistant","message":{"role":"assistant","content":'
        '[{"type":"text","text":"no markers here"}]}}\n',
        encoding="utf-8",
    )

    with pytest.raises(ChatError):
        extract_claude_session_reply(missing, token)
    with pytest.raises(ChatError):
        extract_claude_session_reply(markerless, token)


def test_turn_prefers_exact_claude_session_over_contaminated_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verified Claude target must return the clean session reply even when
    the terminal capture itself contains both markers plus a TUI artifact."""
    token = "7" * 32
    cwd = "/work space/herdr_rooms/claude-room"
    session_id = "99999999-8888-7777-6666-555555555555"
    write_claude_session(tmp_path / ".claude" / "projects", cwd, session_id, token)
    monkeypatch.setenv("HOME", str(tmp_path))

    agent_meta = {
        "agent": "claude",
        "agent_session": {"source": "herdr:claude", "value": session_id},
        "cwd": cwd,
    }
    statuses = ["working", "idle"]
    calls: list[list[str]] = []

    class FakeCompleted:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout
            self.stderr = ""
            self.returncode = 0

    def fake_run(arguments: list[str], timeout: float = 30, **_kwargs: object) -> FakeCompleted:
        calls.append(list(arguments))
        if arguments[1:3] == ["agent", "get"]:
            status = statuses.pop(0) if statuses else "idle"
            return FakeCompleted(
                json.dumps({"result": {"agent": {"agent_status": status, **agent_meta}}})
            )
        if arguments[1:3] == ["agent", "read"]:
            return FakeCompleted(
                f"HGCHAT_REPLY_BEGIN {token}\n"
                "\u276f You are @claude, a full participant in a local group chat\n"
                f"HGCHAT_REPLY_END {token}\n"
            )
        raise AssertionError(f"unexpected herdr call: {arguments}")

    client = HerdrClient(runner=fake_run)
    monkeypatch.setattr(client, "_run_interruptible", lambda arguments, cancel_event, timeout: None)
    monkeypatch.setitem(namespace, "POLL_INTERVAL_S", 0)
    prompt = f"HGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}"

    status, body = client.turn("claude-peer", prompt, timeout_ms=5_000)

    assert status == "idle"
    assert body == "clean Claude reply"
    # Direct terminal extraction of the same contaminated capture would include
    # the prompt-summary artifact, proving the clean session was preferred.
    assert "You are @claude" in extract_reply(
        f"HGCHAT_REPLY_BEGIN {token}\n"
        "\u276f You are @claude, a full participant in a local group chat\n"
        f"HGCHAT_REPLY_END {token}\n",
        token,
    )
    # The exact session is preferred before the terminal is read at all.
    assert not any(call[1:3] == ["agent", "read"] for call in calls)


def test_turn_falls_back_to_terminal_when_claude_session_is_markerless(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "8" * 32
    cwd = "/work space/herdr_rooms/claude-room"
    session_id = "12345678-1234-1234-1234-123456789012"
    session_dir = tmp_path / ".claude" / "projects" / claude_project_dir_name(cwd)
    session_dir.mkdir(parents=True)
    (session_dir / f"{session_id}.jsonl").write_text(
        '{"type":"assistant","message":{"role":"assistant",'
        '"content":[{"type":"text","text":"no markers here"}]}}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    terminal = f"HGCHAT_REPLY_BEGIN {token}\nterminal reply\nHGCHAT_REPLY_END {token}\n"

    class FakeCompleted:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout
            self.stderr = ""
            self.returncode = 0

    def fake_run(arguments: list[str], timeout: float = 30, **_kwargs: object) -> FakeCompleted:
        if arguments[1:3] == ["agent", "get"]:
            return FakeCompleted(
                json.dumps(
                    {
                        "result": {
                            "agent": {
                                "agent_status": "done",
                                "agent": "claude",
                                "agent_session": {
                                    "source": "herdr:claude",
                                    "value": session_id,
                                },
                                "cwd": cwd,
                            }
                        }
                    }
                )
            )
        if arguments[1:3] == ["agent", "read"]:
            return FakeCompleted(terminal)
        raise AssertionError(f"unexpected herdr call: {arguments}")

    client = HerdrClient(runner=fake_run)
    monkeypatch.setattr(client, "_run_interruptible", lambda arguments, cancel_event, timeout: None)
    monkeypatch.setitem(namespace, "POLL_INTERVAL_S", 0)
    prompt = f"HGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}"

    status, body = client.turn("claude-peer", prompt, timeout_ms=5_000)

    assert (status, body) == ("done", "terminal reply")


def test_local_claude_reply_uses_exact_session_and_groks_fall_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = HerdrClient(runner=object)  # _run is stubbed below
    token = "f" * 32
    cwd = "/work space/herdr_rooms/claude-room"
    session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    write_claude_session(tmp_path / ".claude" / "projects", cwd, session_id, token)

    def fake_run(arguments: list[str], timeout: float = 30) -> object:
        assert arguments[:2] == ["agent", "get"]
        return subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps(
                {
                    "result": {
                        "agent": {
                            "agent": "claude",
                            "agent_session": {
                                "source": "herdr:claude",
                                "value": session_id,
                            },
                            "cwd": cwd,
                        }
                    }
                }
            ),
        )

    monkeypatch.setattr(client, "_run", fake_run)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert client._local_claude_reply("claude-peer", token) == "clean Claude reply"

    # Non-Claude or wrong-source metadata never touches the filesystem.
    grok_meta = json.dumps(
        {
            "result": {
                "agent": {
                    "agent": "grok",
                    "agent_session": {
                        "source": "herdr:grok",
                        "value": session_id,
                    },
                    "cwd": cwd,
                }
            }
        }
    )

    def grok_run(arguments: list[str], timeout: float = 30) -> object:
        return subprocess.CompletedProcess([], 0, stdout=grok_meta)

    monkeypatch.setattr(client, "_run", grok_run)
    assert client._local_claude_reply("grok-peer", token) is None


def test_missing_live_agent_fails_before_writing(tmp_path: Path) -> None:
    chat, client, transcript = make_chat(tmp_path)
    client.live_targets = lambda: {"pi-peer"}
    with pytest.raises(ChatError, match="participant not live"):
        chat.dispatch("@claude hello")
    assert transcript.read() == []


def test_failed_turn_does_not_drop_context_and_later_agents_continue(tmp_path: Path) -> None:
    chat, client, transcript = make_chat(tmp_path)
    original_turn = client.turn

    def fail_pi_once(target: str, prompt: str, timeout_ms: int | None = None) -> tuple[str, str]:
        if target == "pi-peer":
            client.calls.append((target, prompt))
            client.timeouts.append(timeout_ms)
            client.turn = original_turn
            raise ChatError("simulated delivery failure")
        return original_turn(target, prompt, timeout_ms)

    client.turn = fail_pi_once
    created = chat.dispatch("@all preserve this")

    assert [item["sender"] for item in created] == [
        "human",
        "system",
        "claude",
        "codex",
        "grok",
    ]
    assert [target for target, _ in client.calls] == [
        "pi-peer",
        "claude-peer",
        "codex-peer",
        "grok-peer",
    ]

    chat.dispatch("@pi retry")
    retry_prompt = client.calls[-1][1]
    assert "[human] preserve this" in retry_prompt
    assert "[human] retry" in retry_prompt
    assert transcript.cursors()["pi"] == transcript.read()[-2]["seq"]


class ParallelReviewClient(FakeClient):
    def __init__(self, reviewer_count: int) -> None:
        super().__init__()
        self.barrier = threading.Barrier(reviewer_count)
        self.call_lock = threading.Lock()

    def turn(
        self,
        target: str,
        prompt: str,
        timeout_ms: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[str, str]:
        with self.call_lock:
            self.calls.append((target, prompt))
            self.timeouts.append(timeout_ms)
        if "Question for independent review" in prompt:
            self.barrier.wait(timeout=2)
            return "done", f"independent reply from {target}"
        return "done", f"synthesis from {target}"


def test_review_runs_independent_phase_in_parallel_then_synthesizes(tmp_path: Path) -> None:
    transcript = Transcript(tmp_path, "review-room")
    client = ParallelReviewClient(reviewer_count=3)
    chat = GroupChat(
        transcript,
        {
            "pi": "pi-peer",
            "claude": "claude-peer",
            "codex": "codex-peer",
            "grok": "grok-peer",
        },
        client,
        synthesizer="pi",
        agent_timeouts={"claude": 11_000, "codex": 12_000, "grok": 13_000, "pi": 14_000},
    )

    review = chat.review("@claude,@codex,@grok Review this draft")

    review_calls = [call for call in client.calls if "Question for independent review" in call[1]]
    assert {target for target, _ in review_calls} == {
        "claude-peer",
        "codex-peer",
        "grok-peer",
    }
    assert all("independent reply from" not in prompt for _, prompt in review_calls)
    synthesis_target, synthesis_prompt = client.calls[-1]
    assert synthesis_target == "pi-peer"
    assert "Independent reviews:" in synthesis_prompt
    assert all(
        f"independent reply from {target}" in synthesis_prompt
        for target in ("claude-peer", "codex-peer", "grok-peer")
    )
    assert set(client.timeouts[:-1]) == {11_000, 12_000, 13_000}
    assert client.timeouts[-1] == 14_000
    assert review.states == {"claude": "done", "codex": "done", "grok": "done", "pi": "done"}

    messages = transcript.read()
    assert messages[0]["kind"] == "review_question"
    assert messages[0]["recipients"] == ["claude", "codex", "grok"]
    assert [item["kind"] for item in messages[1:-1]] == [
        "review_response",
        "review_response",
        "review_response",
    ]
    assert messages[-1]["kind"] == "review_synthesis"
    assert len({item["round_id"] for item in messages}) == 1


def test_review_interleaving_cannot_hide_an_unrelated_ordinary_message(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "review-interleaving-state"
    transcript = Transcript(state_dir, "review-interleaving-room")

    class InterleavingReviewClient(FakeClient):
        def turn(
            self,
            target: str,
            prompt: str,
            timeout_ms: int | None = None,
            cancel_event: threading.Event | None = None,
        ) -> tuple[str, str]:
            if "Question for independent review" in prompt:
                append_one_message(
                    str(state_dir),
                    "review-interleaving-room",
                    "unrelated ordinary message",
                    "claude",
                )
            return super().turn(target, prompt, timeout_ms, cancel_event)

    client = InterleavingReviewClient()
    chat = GroupChat(
        transcript,
        {"pi": "pi-peer", "claude": "claude-peer"},
        client,
        synthesizer="pi",
    )

    chat.review("@claude Check review cursor safety")
    assert transcript.cursors() == {}

    chat.dispatch("@claude ordinary follow-up")
    ordinary_prompt = client.calls[-1][1]
    assert "[human] unrelated ordinary message" in ordinary_prompt
    assert "[human] Check review cursor safety" in ordinary_prompt
    assert "[human] ordinary follow-up" in ordinary_prompt


def test_late_success_after_cancellation_is_discarded_before_review_append(
    tmp_path: Path,
) -> None:
    transcript = Transcript(tmp_path, "late-success-room")
    chat = GroupChat(transcript, {"claude": "claude-peer"}, FakeClient(), synthesizer="claude")
    review = chat.plan_review("@claude Check the late reply")
    assert chat._activate_review(review)

    class LateSuccess:
        def result(self) -> tuple[str, str]:
            chat.cancel_review(review)
            return "done", "late reply that must be discarded"

    chat._record_reviewer_result(review, "claude", LateSuccess(), None)

    assert review.states["claude"] == "cancelled"
    assert review.responses == {}
    assert not any(item["kind"] == "review_response" for item in transcript.read())


@pytest.mark.parametrize(
    "kind,outcome",
    [
        ("review_synthesis", "success"),
        ("anneal_challenge", "blocked"),
        ("anneal_final", "failure"),
        ("review_synthesis", "unexpected"),
    ],
)
def test_cancelled_terminal_phase_siblings_commit_no_late_artifact(
    tmp_path: Path, kind: str, outcome: str
) -> None:
    transcript = Transcript(tmp_path, "phase-cancel-room")

    class CancelBeforeOutcomeClient(FakeClient):
        def turn(
            self,
            target: str,
            prompt: str,
            timeout_ms: int | None = None,
            cancel_event: threading.Event | None = None,
        ) -> tuple[str, str]:
            del target, prompt, timeout_ms
            assert cancel_event is not None
            cancel_event.set()
            if outcome == "blocked":
                return "blocked", ""
            if outcome == "failure":
                raise ChatError("late phase failure")
            if outcome == "unexpected":
                raise RuntimeError("late phase crash")
            return "done", "late phase reply"

    chat = GroupChat(
        transcript,
        {"pi": "pi-peer", "claude": "claude-peer"},
        CancelBeforeOutcomeClient(),
    )
    review = chat.plan_review("@claude Check phase cancellation")
    assert chat._activate_review(review)

    result = chat._execute_phase(
        review,
        "pi",
        "phase prompt",
        "synthesizing",
        None,
        kind=kind,
        failure_prefix="@pi phase",
        blocked_notice="@pi phase is blocked",
    )

    assert result is None
    assert review.responses == {}
    assert review.synthesis is None
    assert review.states["pi"] == "cancelled"
    assert not any(item["kind"] in {kind, "review_status"} for item in transcript.read())


def test_review_timeout_is_visible_and_does_not_block_other_agents(tmp_path: Path) -> None:
    class TimeoutClient(FakeClient):
        def turn(
            self,
            target: str,
            prompt: str,
            timeout_ms: int | None = None,
            cancel_event: threading.Event | None = None,
        ) -> tuple[str, str]:
            if target == "claude-peer" and "Question for independent review" in prompt:
                self.calls.append((target, prompt))
                self.timeouts.append(timeout_ms)
                raise ChatError("Herdr command timed out")
            return super().turn(target, prompt, timeout_ms, cancel_event)

    transcript = Transcript(tmp_path, "timeout-room")
    client = TimeoutClient()
    chat = GroupChat(
        transcript,
        {"pi": "pi-peer", "claude": "claude-peer", "codex": "codex-peer"},
        client,
    )

    review = chat.review("@claude,@codex Find the issue")

    assert review.states["claude"] == "timed_out"
    assert review.states["codex"] == "done"
    assert review.states["pi"] == "done"
    assert any("@claude review timed out" in item["body"] for item in transcript.read())
    assert "[@claude] (no usable response)" in client.calls[-1][1]
    assert "reply from codex-peer" in client.calls[-1][1]


def test_unexpected_reviewer_error_still_drains_peers_and_synthesizes(tmp_path: Path) -> None:
    class CrashingClient(FakeClient):
        def turn(
            self,
            target: str,
            prompt: str,
            timeout_ms: int | None = None,
            cancel_event: threading.Event | None = None,
        ) -> tuple[str, str]:
            if target == "claude-peer" and "Question for independent review" in prompt:
                raise RuntimeError("relay exploded")
            return super().turn(target, prompt, timeout_ms, cancel_event)

    transcript = Transcript(tmp_path, "crash-room")
    client = CrashingClient()
    chat = GroupChat(
        transcript,
        {"pi": "pi-peer", "claude": "claude-peer", "codex": "codex-peer"},
        client,
    )

    review = chat.review("@claude,@codex Check the invariant")

    assert review.states["claude"] == "failed"
    assert review.states["codex"] == "done"
    assert review.states["pi"] == "done"
    statuses = [item for item in transcript.read() if item["kind"] == "review_status"]
    assert len(statuses) == 1
    assert statuses[0]["sender"] == "system"
    assert "RuntimeError" in statuses[0]["body"]
    assert "relay exploded" in statuses[0]["body"]
    assert review.responses == {"codex": "reply from codex-peer"}
    synthesis_prompt = client.calls[-1][1]
    assert "[" + "@claude] (no usable response)" in synthesis_prompt
    assert "reply from codex-peer" in synthesis_prompt
    assert review.synthesis == "reply from pi-peer"


def test_review_controller_cancel_stops_only_local_orchestration(tmp_path: Path) -> None:
    class BlockingClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def turn(
            self,
            target: str,
            prompt: str,
            timeout_ms: int | None = None,
            cancel_event: threading.Event | None = None,
        ) -> tuple[str, str]:
            self.calls.append((target, prompt))
            self.timeouts.append(timeout_ms)
            if "Question for independent review" in prompt:
                self.started.set()
                while not self.release.wait(timeout=0.01):
                    if cancel_event is not None and cancel_event.is_set():
                        raise ChatError("review cancelled")
            return "done", f"reply from {target}"

    transcript = Transcript(tmp_path, "cancel-room")
    client = BlockingClient()
    chat = GroupChat(transcript, {"pi": "pi-peer", "claude": "claude-peer"}, client)
    controller = ReviewController(chat)

    notice = controller.start("@claude Check this")
    assert notice == "Review started with @claude; @pi synthesizes."
    assert client.started.wait(timeout=1)
    assert controller.is_active()
    assert "@claude working" in controller.status()
    assert controller.cancel() == "Local cancellation requested; participants may continue working."
    assert controller.wait(timeout=2)

    assert client.cancelled == []
    assert controller.status() == "Review cancelled locally; participants may continue working."
    assert not any(item["kind"] == "review_synthesis" for item in transcript.read())


def test_review_completion_wins_and_cancel_rejects_after_terminal_commit(
    tmp_path: Path,
) -> None:
    class CompletingClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def turn(
            self,
            target: str,
            prompt: str,
            timeout_ms: int | None = None,
            cancel_event: threading.Event | None = None,
        ) -> tuple[str, str]:
            self.started.set()
            assert self.release.wait(timeout=2)
            return super().turn(target, prompt, timeout_ms, cancel_event)

    transcript = Transcript(tmp_path, "completion-wins-room")
    client = CompletingClient()
    controller = ReviewController(
        GroupChat(transcript, {"claude": "claude-peer"}, client, synthesizer="claude")
    )
    controller.start("@claude Complete this")
    assert client.started.wait(timeout=1)
    client.release.set()
    assert controller.wait(timeout=2)

    assert controller.status() == "Review complete."
    with pytest.raises(ChatError, match="no active review"):
        controller.cancel()
    assert any(item["kind"] == "review_response" for item in transcript.read())


def test_review_cancellation_wins_and_discards_late_success(
    tmp_path: Path,
) -> None:
    class LateClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def turn(
            self,
            target: str,
            prompt: str,
            timeout_ms: int | None = None,
            cancel_event: threading.Event | None = None,
        ) -> tuple[str, str]:
            self.started.set()
            assert self.release.wait(timeout=2)
            return "done", "late successful reply"

    transcript = Transcript(tmp_path, "cancellation-wins-room")
    client = LateClient()
    controller = ReviewController(
        GroupChat(transcript, {"claude": "claude-peer"}, client, synthesizer="claude")
    )
    controller.start("@claude Cancel this")
    assert client.started.wait(timeout=1)
    assert controller.cancel() == "Local cancellation requested; participants may continue working."
    client.release.set()
    assert controller.wait(timeout=2)

    assert controller.status() == "Review cancelled locally; participants may continue working."
    assert not any(item["kind"] == "review_response" for item in transcript.read())
    with pytest.raises(ChatError, match="no active review"):
        controller.cancel()


def test_cancelled_review_keeps_occupancy_until_worker_drains_before_retry(
    tmp_path: Path,
) -> None:
    class RetryAfterDrainClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.first_started = threading.Event()
            self.release_first = threading.Event()
            self.review_calls = 0

        def turn(
            self,
            target: str,
            prompt: str,
            timeout_ms: int | None = None,
            cancel_event: threading.Event | None = None,
        ) -> tuple[str, str]:
            self.calls.append((target, prompt))
            self.timeouts.append(timeout_ms)
            if "Question for independent review" in prompt:
                self.review_calls += 1
                if self.review_calls == 1:
                    self.first_started.set()
                    assert self.release_first.wait(timeout=2)
                    return "done", "late first reply"
                return "done", "retry reply"
            return "done", "one synthesis"

    transcript = Transcript(tmp_path, "retry-occupancy-room")
    client = RetryAfterDrainClient()
    chat = GroupChat(transcript, {"pi": "pi-peer", "claude": "claude-peer"}, client)
    controller = ReviewController(chat)
    controller.start("@claude Check overlap")
    assert client.first_started.wait(timeout=1)

    assert controller.cancel() == "Local cancellation requested; participants may continue working."
    assert controller.is_active()
    with pytest.raises(ChatError, match="already running"):
        controller.start("@claude Must not overlap")
    with pytest.raises(ChatError, match="active review"):
        controller.retry("claude")

    client.release_first.set()
    assert controller.wait(timeout=2)
    assert not controller.is_active()
    assert controller.retry("claude") == "Retrying @claude."
    assert controller.wait(timeout=2)

    items = transcript.read()
    assert [item["kind"] for item in items].count("review_response") == 1
    assert [item["kind"] for item in items].count("review_synthesis") == 1
    assert not any(item["body"] == "late first reply" for item in items)
    assert client.review_calls == 2


def test_review_start_does_not_block_on_liveness_and_immediate_cancel_writes_nothing(
    tmp_path: Path,
) -> None:
    class SlowLivenessClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.check_started = threading.Event()
            self.release_check = threading.Event()

        def live_targets(self) -> set[str]:
            self.check_started.set()
            assert self.release_check.wait(timeout=2)
            return super().live_targets()

    transcript = Transcript(tmp_path, "pending-cancel-room")
    client = SlowLivenessClient()
    chat = GroupChat(transcript, {"pi": "pi-peer", "claude": "claude-peer"}, client)
    controller = ReviewController(chat)

    assert controller.start("@claude Check this").startswith("Review started")
    assert client.check_started.wait(timeout=1)
    assert controller.cancel() == "Local cancellation requested; participants may continue working."
    assert "@claude cancelled" in controller.status()
    client.release_check.set()
    assert controller.wait(timeout=2)

    assert client.calls == []
    assert client.cancelled == []
    assert transcript.read() == []


def test_interrupted_synthesis_is_cancelled_without_failure_entry(tmp_path: Path) -> None:
    class BlockingSynthesisClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.synthesis_started = threading.Event()

        def turn(
            self,
            target: str,
            prompt: str,
            timeout_ms: int | None = None,
            cancel_event: threading.Event | None = None,
        ) -> tuple[str, str]:
            if "Independent reviews:" not in prompt:
                return super().turn(target, prompt, timeout_ms, cancel_event)
            self.calls.append((target, prompt))
            self.timeouts.append(timeout_ms)
            self.synthesis_started.set()
            assert cancel_event is not None
            assert cancel_event.wait(timeout=2)
            raise ChatError("review cancelled")

    transcript = Transcript(tmp_path, "synthesis-cancel-room")
    client = BlockingSynthesisClient()
    chat = GroupChat(transcript, {"pi": "pi-peer", "claude": "claude-peer"}, client)
    controller = ReviewController(chat)

    controller.start("@claude Check this")
    assert client.synthesis_started.wait(timeout=1)
    assert controller.cancel() == "Local cancellation requested; participants may continue working."
    assert controller.wait(timeout=2)

    assert client.cancelled == []
    assert controller.status() == "Review cancelled locally; participants may continue working."
    assert not any("synthesis failed" in item["body"] for item in transcript.read())
    assert not any(item["kind"] == "review_synthesis" for item in transcript.read())


def test_direct_group_chat_retry_waits_for_cancelled_attempt_to_drain(
    tmp_path: Path,
) -> None:
    class DirectRetryClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.first_started = threading.Event()
            self.release_first = threading.Event()
            self.review_calls = 0

        def turn(
            self,
            target: str,
            prompt: str,
            timeout_ms: int | None = None,
            cancel_event: threading.Event | None = None,
        ) -> tuple[str, str]:
            self.calls.append((target, prompt))
            self.timeouts.append(timeout_ms)
            if "Question for independent review" in prompt:
                self.review_calls += 1
                if self.review_calls == 1:
                    self.first_started.set()
                    assert self.release_first.wait(timeout=2)
                    return "done", "late direct reply"
                return "done", "direct retry reply"
            return "done", "direct synthesis"

    transcript = Transcript(tmp_path, "direct-retry-room")
    client = DirectRetryClient()
    chat = GroupChat(transcript, {"pi": "pi-peer", "claude": "claude-peer"}, client)
    review = chat.plan_review("@claude Check direct retry safety")
    errors: list[BaseException] = []

    def run_review() -> None:
        try:
            chat.execute_review(review)
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=run_review)
    worker.start()
    assert client.first_started.wait(timeout=1)
    chat.cancel_review(review)

    with pytest.raises(ChatError, match="review attempt is already running"):
        chat.retry_review(review, "claude")
    with pytest.raises(ChatError, match="review attempt is already running"):
        chat.retry_synthesis(review)

    client.release_first.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert errors == []
    assert review.responses == {}

    chat.retry_review(review, "claude")
    items = transcript.read()
    assert [item["kind"] for item in items].count("review_response") == 1
    assert [item["kind"] for item in items].count("review_synthesis") == 1
    assert not any(item["body"] == "late direct reply" for item in items)
    assert client.review_calls == 2


def test_failed_review_can_retry_with_fresh_marker_and_resynthesize(tmp_path: Path) -> None:
    class FlakyClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        def turn(
            self,
            target: str,
            prompt: str,
            timeout_ms: int | None = None,
            cancel_event: threading.Event | None = None,
        ) -> tuple[str, str]:
            if target == "claude-peer" and not self.failed:
                self.failed = True
                self.calls.append((target, prompt))
                self.timeouts.append(timeout_ms)
                raise ChatError("temporary failure")
            return super().turn(target, prompt, timeout_ms, cancel_event)

    transcript = Transcript(tmp_path, "retry-room")
    client = FlakyClient()
    chat = GroupChat(transcript, {"pi": "pi-peer", "claude": "claude-peer"}, client)
    review = chat.review("@claude Review once")
    first_prompt = client.calls[0][1]
    assert review.states == {"claude": "failed", "pi": "skipped"}

    chat.retry_review(review, "claude")

    second_prompt = client.calls[1][1]
    first_token = re.search(r"HGCHAT_REPLY_BEGIN ([a-f0-9]{32})", first_prompt)
    second_token = re.search(r"HGCHAT_REPLY_BEGIN ([a-f0-9]{32})", second_prompt)
    assert first_token and second_token and first_token.group(1) != second_token.group(1)
    assert review.states == {"claude": "done", "pi": "done"}
    assert [item["kind"] for item in transcript.read()][-2:] == [
        "review_response",
        "review_synthesis",
    ]

    with pytest.raises(ChatError, match="already has a usable review response"):
        chat.retry_review(review, "claude")


def test_completed_review_notice_can_be_cleared_for_later_chat_status(tmp_path: Path) -> None:
    chat, _, _ = make_chat(tmp_path)
    controller = ReviewController(chat)
    controller.start("@claude Check this")
    assert controller.wait(timeout=2)
    assert controller.status() == "Review complete."
    controller.clear_notice()
    assert controller.status() == ""


def test_immediate_cancel_before_retry_worker_start_targets_fresh_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RetryClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.failed_once = False

        def turn(
            self,
            target: str,
            prompt: str,
            timeout_ms: int | None = None,
            cancel_event: threading.Event | None = None,
        ) -> tuple[str, str]:
            if target == "claude-peer" and not self.failed_once:
                self.failed_once = True
                self.calls.append((target, prompt))
                self.timeouts.append(timeout_ms)
                raise ChatError("first attempt failed")
            return super().turn(target, prompt, timeout_ms, cancel_event)

    transcript = Transcript(tmp_path, "immediate-retry-cancel-room")
    client = RetryClient()
    chat = GroupChat(transcript, {"pi": "pi-peer", "claude": "claude-peer"}, client)
    controller = ReviewController(chat)
    controller.start("@claude Prepare retry")
    assert controller.wait(timeout=2)

    worker_entered = threading.Event()
    release_worker = threading.Event()
    original_retry = chat.retry_review

    def gated_retry(*args: object, **kwargs: object) -> object:
        worker_entered.set()
        assert release_worker.wait(timeout=2)
        return original_retry(*args, **kwargs)

    monkeypatch.setattr(chat, "retry_review", gated_retry)
    assert controller.retry("claude") == "Retrying @claude."
    assert worker_entered.wait(timeout=1)
    assert controller.cancel() == "Local cancellation requested; participants may continue working."
    release_worker.set()
    assert controller.wait(timeout=2)

    assert controller.status() == "Review cancelled locally; participants may continue working."
    assert not any(item["kind"] == "review_response" for item in transcript.read())
    assert len(client.calls) == 1


def test_cancel_during_retry_liveness_is_not_erased(tmp_path: Path) -> None:
    class RetryLivenessClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.live_checks = 0
            self.retry_check_started = threading.Event()
            self.release_retry_check = threading.Event()

        def live_targets(self) -> set[str]:
            self.live_checks += 1
            if self.live_checks == 2:
                self.retry_check_started.set()
                assert self.release_retry_check.wait(timeout=2)
            return super().live_targets()

        def turn(
            self,
            target: str,
            prompt: str,
            timeout_ms: int | None = None,
            cancel_event: threading.Event | None = None,
        ) -> tuple[str, str]:
            if target == "claude-peer":
                self.calls.append((target, prompt))
                self.timeouts.append(timeout_ms)
                raise ChatError("temporary failure")
            return super().turn(target, prompt, timeout_ms, cancel_event)

    transcript = Transcript(tmp_path, "retry-cancel-room")
    client = RetryLivenessClient()
    chat = GroupChat(transcript, {"pi": "pi-peer", "claude": "claude-peer"}, client)
    controller = ReviewController(chat)
    controller.start("@claude Check this")
    assert controller.wait(timeout=2)
    assert len(client.calls) == 1

    assert controller.retry("claude") == "Retrying @claude."
    assert client.retry_check_started.wait(timeout=1)
    assert controller.cancel() == "Local cancellation requested; participants may continue working."
    client.release_retry_check.set()
    assert controller.wait(timeout=2)

    assert len(client.calls) == 1
    assert controller.status() == "Review cancelled locally; participants may continue working."


def test_unstarted_review_cannot_append_retry_artifacts(tmp_path: Path) -> None:
    transcript = Transcript(tmp_path, "unstarted-room")
    chat = GroupChat(transcript, {"pi": "pi-peer", "claude": "claude-peer"}, FakeClient())
    review = chat.plan_review("@claude Check this")

    with pytest.raises(ChatError, match="never started"):
        chat.retry_review(review, "claude")
    with pytest.raises(ChatError, match="never started"):
        chat.retry_synthesis(review)
    assert transcript.read() == []


def test_review_commands_require_exact_names_and_support_retry(tmp_path: Path) -> None:
    chat, _, _ = make_chat(tmp_path)
    controller = ReviewController(chat)
    assert handle_local_command("/reviewer hello", chat, controller) == (
        "Unknown command. Use /help."
    )
    assert handle_local_command("/review", chat, controller) == (
        "Usage: /review [@agent,@agent] QUESTION"
    )
    with pytest.raises(ChatError, match="no review to retry"):
        handle_local_command("/retry claude", chat, controller)


def test_review_prompt_and_synthesis_preserve_targeted_order() -> None:
    review_prompt = build_review_prompt(
        "claude", "Question", "a" * 32, ("pi", "claude", "codex", "grok")
    )
    assert "answers are deliberately withheld until synthesis" in review_prompt
    synthesis_prompt = build_synthesis_prompt(
        "grok",
        "Question",
        ("codex", "claude"),
        {"claude": "second", "codex": "first"},
        "b" * 32,
    )
    assert synthesis_prompt.index("[@codex] first") < synthesis_prompt.index("[@claude] second")


def test_agent_timeout_parser_validates_and_overrides() -> None:
    assert parse_agent_timeouts(["Claude=12000", "grok=900000"], ("pi", "claude", "grok")) == {
        "claude": 12_000,
        "grok": 900_000,
    }
    with pytest.raises(ChatError, match="unknown participant"):
        parse_agent_timeouts(["gemini=12000"], ("pi", "claude"))
    with pytest.raises(ChatError, match="between 1000 and 3600000"):
        parse_agent_timeouts(["pi=999"], ("pi",))


def test_parallel_review_rejects_duplicate_herdr_targets(tmp_path: Path) -> None:
    with pytest.raises(ChatError, match="agent targets must be unique: shared-peer"):
        GroupChat(
            Transcript(tmp_path, "duplicate-room"),
            {"pi": "shared-peer", "claude": "shared-peer"},
            FakeClient(),
        )


def test_transcript_scrolling_and_review_labels() -> None:
    messages = [
        {"sender": "claude", "body": "review", "kind": "review_response"},
        {"sender": "pi", "body": "synthesis", "kind": "review_synthesis"},
        {"sender": "human", "body": "latest", "kind": "message"},
    ]
    assert message_lines(messages, 80)[0] == "claude [review]> review"
    assert "pi [synthesis]> synthesis" in message_lines(messages, 80)
    latest, max_offset = visible_message_lines(messages, 80, available=2, scroll_offset=0)
    older, _ = visible_message_lines(messages, 80, available=2, scroll_offset=max_offset)
    assert "human> latest" in latest
    assert "claude [review]> review" in older


def test_transcript_render_cache_is_reused_and_invalidated_on_append(tmp_path: Path) -> None:
    transcript = Transcript(tmp_path, "render-room")
    transcript.append("human", ("pi",), "first")
    first = transcript.rendered_lines(80)
    assert transcript.rendered_lines(80) is first
    transcript.append("pi", ("human",), "second")
    second = transcript.rendered_lines(80)
    assert second is not first
    assert "pi> second" in second


def test_herdr_turn_submits_then_polls_for_token_bound_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    token = "a" * 32
    monkeypatch.setitem(namespace, "POLL_INTERVAL_S", 0)
    statuses = iter(("working", "done"))

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["agent", "get"]:
            stdout = f'{{"result":{{"agent":{{"agent_status":"{next(statuses)}"}}}}}}'
        elif argv[1:3] == ["agent", "read"]:
            stdout = f"HGCHAT_REPLY_BEGIN {token}\nanswer\nHGCHAT_REPLY_END {token}\n"
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    status, reply = client.turn(
        "claude-peer",
        f"prompt\nHGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}",
        12_000,
    )

    assert (status, reply) == ("done", "answer")
    assert calls[0][1:4] == ["agent", "prompt", "claude-peer"]
    assert "--wait" not in calls[0]
    assert [call[1:3] for call in calls[1:]] == [
        ["agent", "get"],
        ["agent", "get"],
        ["agent", "read"],
    ]


def test_herdr_turn_defers_terminal_read_until_agent_seen_working_or_grace_elapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale idle right after submission must not trigger the slow scrolling read."""
    calls: list[list[str]] = []
    token = "1" * 32
    monkeypatch.setitem(namespace, "POLL_INTERVAL_S", 0)
    monkeypatch.setitem(namespace, "SUBMIT_GRACE_S", 2.0)
    clock = iter((0.0, 0.5, 0.5, 1.0, 1.0, 2.5, 2.5, 3.0, 3.0))
    monkeypatch.setattr(namespace["time"], "monotonic", lambda: next(clock, 3.0))

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["agent", "get"]:
            stdout = '{"result":{"agent":{"agent_status":"idle"}}}'
        elif argv[1:3] == ["agent", "read"]:
            stdout = f"HGCHAT_REPLY_BEGIN {token}\nanswer\nHGCHAT_REPLY_END {token}\n"
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    status, reply = client.turn(
        "claude-peer",
        f"prompt\nHGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}",
        12_000,
    )

    assert (status, reply) == ("idle", "answer")
    kinds = [call[1:3] for call in calls]
    first_read = kinds.index(["agent", "read"])
    assert kinds[first_read - 1] == ["agent", "get"]
    assert kinds[1:first_read] == [["agent", "get"]] * (first_read - 1)
    assert first_read - 1 >= 2, "idle polls inside the grace period must not read"


def test_herdr_turn_tolerates_refused_or_slow_terminal_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """agent_not_idle at the idle->working flip and a slow capture are 'not ready', not failure."""
    calls: list[list[str]] = []
    token = "2" * 32
    monkeypatch.setitem(namespace, "POLL_INTERVAL_S", 0)
    reads = iter(("refused", "slow", "ok"))

    def runner(
        argv: list[str], timeout: float = 30, **_: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["agent", "get"]:
            status = "working" if len(calls) == 2 else "done"
            return subprocess.CompletedProcess(
                argv, 0, stdout=f'{{"result":{{"agent":{{"agent_status":"{status}"}}}}}}', stderr=""
            )
        if argv[1:3] == ["agent", "read"]:
            assert timeout > 5, "terminal reads are no longer capped at five seconds"
            outcome = next(reads)
            if outcome == "refused":
                return subprocess.CompletedProcess(
                    argv, 1, stdout='{"error":{"code":"agent_not_idle"}}', stderr=""
                )
            if outcome == "slow":
                raise subprocess.TimeoutExpired(argv, timeout)
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=f"HGCHAT_REPLY_BEGIN {token}\nanswer\nHGCHAT_REPLY_END {token}\n",
                stderr="",
            )
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    status, reply = client.turn(
        "claude-peer",
        f"prompt\nHGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}",
        12_000,
    )

    assert (status, reply) == ("done", "answer")
    assert ["agent", "send-keys"] not in [call[1:3] for call in calls]
    assert [call[1:3] for call in calls].count(["agent", "read"]) == 3


def test_review_cancel_stops_the_local_prompt_process_without_sending_keys() -> None:
    calls: list[list[str]] = []
    cancel_event = threading.Event()
    token = "c" * 32

    class WaitingProcess:
        def __init__(self) -> None:
            self.returncode = 0
            self.terminated = False

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            if not self.terminated:
                raise subprocess.TimeoutExpired("herdr-test", timeout)
            return "", ""

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True

    process = WaitingProcess()

    def popen_factory(argv: list[str], **_: object) -> WaitingProcess:
        calls.append(argv)
        cancel_event.set()
        return process

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    client = HerdrClient(
        herdr_bin="herdr-test",
        runner=runner,
        popen_factory=popen_factory,
    )
    with pytest.raises(ChatError, match="review cancelled"):
        client.turn(
            "claude-peer",
            f"prompt\nHGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}",
            12_000,
            cancel_event,
        )

    assert process.terminated
    assert [call[1:3] for call in calls] == [["agent", "prompt"]]


def test_herdr_stop_process_returns_after_a_second_communicate_timeout() -> None:
    class StubbornProcess:
        def __init__(self) -> None:
            self.returncode = 0
            self.terminated = False
            self.killed = False
            self.communicate_timeouts: list[float | None] = []

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            self.communicate_timeouts.append(timeout)
            raise subprocess.TimeoutExpired("herdr-test", timeout)

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

    process = StubbornProcess()

    HerdrClient._stop_process(process)

    assert process.terminated
    assert process.killed
    assert len(process.communicate_timeouts) == 2
    assert all(timeout is not None and timeout > 0 for timeout in process.communicate_timeouts)


def test_herdr_turn_timeout_never_sends_keys_to_observed_working_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    token = "d" * 32
    ticks = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(namespace["time"], "monotonic", lambda: next(ticks))
    monkeypatch.setitem(namespace, "POLL_INTERVAL_S", 0)

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        stdout = (
            '{"result":{"agent":{"agent_status":"working"}}}'
            if argv[1:3] == ["agent", "get"]
            else "{}"
        )
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    with pytest.raises(ChatError, match="timed out after 1000 ms"):
        client.turn(
            "claude-peer",
            f"prompt\nHGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}",
            1_000,
        )

    assert [call[1:3] for call in calls] == [["agent", "prompt"], ["agent", "get"]]


@pytest.mark.parametrize("status", ["working", "idle"])
def test_review_cancel_never_sends_keys_for_working_or_idle_status(
    status: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    cancel_event = threading.Event()
    token = "6" * 32
    monkeypatch.setitem(namespace, "POLL_INTERVAL_S", 0)

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["agent", "get"]:
            cancel_event.set()
            stdout = f'{{"result":{{"agent":{{"agent_status":"{status}"}}}}}}'
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    with pytest.raises(ChatError, match="review cancelled"):
        client.turn(
            "codex-peer",
            f"prompt\nHGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}",
            12_000,
            cancel_event,
        )

    assert [call[1:3] for call in calls] == [["agent", "prompt"], ["agent", "get"]]


def test_review_cancel_never_acts_on_a_stale_working_observation() -> None:
    calls: list[list[str]] = []
    cancel_event = threading.Event()
    token = "0" * 32
    gets = 0

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal gets
        calls.append(argv)
        if argv[1:3] == ["agent", "get"]:
            gets += 1
            if gets == 1:
                cancel_event.set()
                stdout = '{"result":{"agent":{"agent_status":"working"}}}'
            else:
                stdout = '{"result":{"agent":{"agent_status":"idle"}}}'
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    with pytest.raises(ChatError, match="review cancelled"):
        client.turn(
            "claude-peer",
            f"prompt\nHGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}",
            12_000,
            cancel_event,
        )

    assert gets == 1
    assert [call[1:3] for call in calls] == [["agent", "prompt"], ["agent", "get"]]


def test_invalid_post_submission_status_preserves_error_without_interrupting() -> None:
    calls: list[list[str]] = []
    token = "e" * 32

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        stdout = "not-json" if argv[1:3] == ["agent", "get"] else "{}"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    with pytest.raises(ChatError, match="invalid result"):
        client.turn(
            "claude-peer",
            f"prompt\nHGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}",
            12_000,
        )

    assert [call[1:3] for call in calls] == [
        ["agent", "prompt"],
        ["agent", "get"],
    ]


def test_failed_post_submission_status_preserves_error_without_interrupting() -> None:
    calls: list[list[str]] = []
    token = "9" * 32

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["agent", "get"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="status failed")
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    with pytest.raises(ChatError, match="status failed"):
        client.turn(
            "claude-peer",
            f"prompt\nHGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}",
            12_000,
        )

    assert [call[1:3] for call in calls] == [["agent", "prompt"], ["agent", "get"]]


def test_prompt_failure_preserves_error_and_never_interrupts() -> None:
    calls: list[list[str]] = []
    token = "f" * 32

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert argv[1:3] == ["agent", "prompt"]
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="submit failed")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    with pytest.raises(ChatError, match="submit failed"):
        client.turn(
            "claude-peer",
            f"prompt\nHGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}",
            12_000,
        )

    assert [call[1:3] for call in calls] == [["agent", "prompt"]]


def test_herdr_turn_detects_clipped_hooks_dialog_as_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "3" * 32
    monkeypatch.setitem(namespace, "POLL_INTERVAL_S", 0)
    statuses = iter(("working", "done"))

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if argv[1:3] == ["agent", "get"]:
            stdout = f'{{"result":{{"agent":{{"agent_status":"{next(statuses)}"}}}}}}'
        elif argv[1:3] == ["agent", "read"]:
            stdout = "Press t to trust all; enter to review hooks; es"
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    assert client.turn(
        "codex-peer",
        f"prompt\nHGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}",
        12_000,
    ) == ("blocked", "")


def test_herdr_turn_fails_fast_when_a_completed_reply_stays_unmarked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    token = "4" * 32
    monkeypatch.setitem(namespace, "POLL_INTERVAL_S", 0)
    polls = 0

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal polls
        calls.append(argv)
        if argv[1:3] == ["agent", "get"]:
            polls += 1
            status = "working" if polls == 1 else "done"
            stdout = f'{{"result":{{"agent":{{"agent_status":"{status}"}}}}}}'
        elif argv[1:3] == ["agent", "read"]:
            stdout = "a complete answer that ignored the marker protocol"
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    with pytest.raises(
        ChatError,
        match=r"reply markers HGCHAT_REPLY_\* not found in @claude-peer's completed response",
    ):
        client.turn(
            "claude-peer",
            f"prompt\nHGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}",
            600_000,
        )

    reads = [call for call in calls if call[1:3] == ["agent", "read"]]
    assert len(reads) == namespace["STABLE_UNMARKED_POLLS"] + 1
    assert ["agent", "send-keys"] not in [call[1:3] for call in calls]


def test_herdr_turn_unmarked_streak_resets_when_terminal_content_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    token = "5" * 32
    monkeypatch.setitem(namespace, "POLL_INTERVAL_S", 0)
    polls = 0
    streak_limit = namespace["STABLE_UNMARKED_POLLS"]

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal polls
        calls.append(argv)
        if argv[1:3] == ["agent", "get"]:
            polls += 1
            status = "working" if polls == 1 else "done"
            stdout = f'{{"result":{{"agent":{{"agent_status":"{status}"}}}}}}'
        elif argv[1:3] == ["agent", "read"]:
            reads = len([call for call in calls if call[1:3] == ["agent", "read"]])
            if reads > streak_limit * 2:
                stdout = f"HGCHAT_REPLY_BEGIN {token}\nanswer\nHGCHAT_REPLY_END {token}\n"
            else:
                stdout = f"still drafting chunk {reads}"
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    status, reply = client.turn(
        "claude-peer",
        f"prompt\nHGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}",
        600_000,
    )

    assert (status, reply) == ("done", "answer")
    reads = [call for call in calls if call[1:3] == ["agent", "read"]]
    assert len(reads) > streak_limit


def test_setup_failures_are_recorded_as_system_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = Transcript(tmp_path, "room")
    monkeypatch.setenv("HERDR_GROUP_CHAT_SETUP_FAILURES", "@pi: start failed\n@claude: pane lost\n")

    namespace["record_setup_failures"](transcript)

    assert [(item["sender"], item["body"]) for item in transcript.read()] == [
        ("system", "setup: @pi: start failed"),
        ("system", "setup: @claude: pane lost"),
    ]


def test_setup_failures_absent_or_empty_record_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = Transcript(tmp_path, "room")
    monkeypatch.delenv("HERDR_GROUP_CHAT_SETUP_FAILURES", raising=False)
    namespace["record_setup_failures"](transcript)
    monkeypatch.setenv("HERDR_GROUP_CHAT_SETUP_FAILURES", "")
    namespace["record_setup_failures"](transcript)

    assert transcript.read() == []


def test_plugin_manifest_is_minimal_and_targets_herdr_0_8() -> None:
    manifest = tomllib.loads((EFFECTOR.parent / "herdr-plugin.toml").read_text(encoding="utf-8"))
    project = tomllib.loads((EFFECTOR.parent / "pyproject.toml").read_text(encoding="utf-8"))
    assert manifest["min_herdr_version"] == "0.8.0"
    assert manifest["version"] == project["project"]["version"]
    assert [action["id"] for action in manifest["actions"]] == [
        "new",
        "new-sol-fable",
        "new-classic",
        "open",
    ]
    new_default = manifest["actions"][0]
    assert new_default["title"] == "New group chat"
    assert new_default["command"] == [
        "./new-room",
        "--launch",
        "--profile",
        "sol-fable-grok",
    ]
    sol_fable = manifest["actions"][1]
    assert sol_fable["title"] == "New Sol + Fable chat"
    assert sol_fable["command"] == ["./new-room", "--launch", "--profile", "sol-fable"]
    assert sol_fable["contexts"] == ["workspace", "tab", "pane"]
    new_classic = manifest["actions"][2]
    assert new_classic["title"] == "New classic four-agent chat"
    assert new_classic["command"] == ["./new-room", "--launch"]
    assert new_classic["contexts"] == ["workspace", "tab", "pane"]
    assert [pane["id"] for pane in manifest["panes"]] == ["new-room", "room"]
    assert all(pane["placement"] == "tab" for pane in manifest["panes"])
    assert "events" not in manifest
    assert "startup" not in manifest
    commands = [item["command"] for item in (*manifest["actions"], *manifest["panes"])]
    for command in commands:
        target = EFFECTOR.parent / command[0]
        assert target.is_file()
        assert access(target, X_OK)


class AnnealClient(FakeClient):
    """Routes anneal phases by prompt markers; blind replies meet at a barrier."""

    def __init__(self) -> None:
        super().__init__()
        self.blind = threading.Barrier(2)
        self.call_lock = threading.Lock()
        self.fail_next_review_blind = False
        self.single_blind = False

    def _record(self, target: str, prompt: str, timeout_ms: int | None) -> None:
        with self.call_lock:
            self.calls.append((target, prompt))
            self.timeouts.append(timeout_ms)

    def turn(
        self,
        target: str,
        prompt: str,
        timeout_ms: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[str, str]:
        self._record(target, prompt, timeout_ms)
        if "Question for independent review" in prompt:
            if self.fail_next_review_blind and target == "claude-peer":
                self.fail_next_review_blind = False
                raise ChatError("temporary blind failure")
            if not self.single_blind:
                self.blind.wait(timeout=2)
            return "done", f"blind from {target}"
        if "Independent reviews:" in prompt:
            return "done", f"synthesis from {target}"
        if "the critic in a two-participant anneal" in prompt:
            return "done", f"challenge from {target}"
        if "the sole final author in a two-participant anneal" in prompt:
            return "done", f"final from {target}"
        return "done", f"reply from {target}"


def make_anneal_chat(
    tmp_path: Path, client: FakeClient, room: str = "anneal-room"
) -> tuple[GroupChat, Transcript]:
    transcript = Transcript(tmp_path, room)
    chat = GroupChat(
        transcript,
        {"pi": "pi-peer", "claude": "claude-peer", "grok": "grok-peer"},
        client,
        synthesizer="pi",
    )
    return chat, transcript


def test_anneal_parser_requires_exactly_two_distinct_known_participants() -> None:
    known = ("pi", "claude", "codex", "grok")
    assert parse_anneal("@pi,@claude refine", known) == ("pi", "claude", "refine")
    assert parse_anneal("@Pi,@claude  refine this  ", known) == ("pi", "claude", "refine this")
    for bad, message in [
        ("", "usage"),
        ("refine this", "usage"),
        ("@all refine", "all"),
        ("@pi,@all refine", "all"),
        ("@pi refine", "exactly two"),
        ("@pi,@pi refine", "exactly two"),
        ("@pi,@claude,@codex refine", "exactly two"),
        ("@pi,@gemini refine", "unknown participant"),
        ("@gemini,@pi refine", "unknown participant"),
        ("@pi,@claude", "usage"),
        ("@pi,@claude   ", "usage"),
    ]:
        with pytest.raises(ChatError, match=message):
            parse_anneal(bad, known)


def test_anneal_runs_blind_concurrently_then_challenge_and_final_in_order(
    tmp_path: Path,
) -> None:
    client = AnnealClient()
    chat, transcript = make_anneal_chat(tmp_path, client)

    review = chat.anneal("@claude,@grok Harden this plan")

    blind = [call for call in client.calls if "Question for independent review" in call[1]]
    assert {target for target, _ in blind} == {"claude-peer", "grok-peer"}
    assert all("blind from" not in prompt for _, prompt in blind)
    synthesis = [call for call in client.calls if "Independent reviews:" in call[1]]
    assert [target for target, _ in synthesis] == ["claude-peer"]
    assert "blind from claude-peer" in synthesis[0][1]
    assert "blind from grok-peer" in synthesis[0][1]
    challenge = [
        call for call in client.calls if "the critic in a two-participant anneal" in call[1]
    ]
    assert [target for target, _ in challenge] == ["grok-peer"]
    assert "Harden this plan" in challenge[0][1]
    assert "synthesis from claude-peer" in challenge[0][1]
    final = [
        call
        for call in client.calls
        if "the sole final author in a two-participant anneal" in call[1]
    ]
    assert [target for target, _ in final] == ["claude-peer"]
    for needle in (
        "Harden this plan",
        "blind from claude-peer",
        "blind from grok-peer",
        "synthesis from claude-peer",
        "challenge from grok-peer",
    ):
        assert needle in final[0][1]
    for prompt in (challenge[0][1], final[0][1]):
        assert "eligibility never transfers" in prompt
        assert "routing identities, not attestation" in prompt

    messages = transcript.read()
    kinds = [item["kind"] for item in messages]
    assert kinds[0] == "review_question"
    assert kinds.count("review_response") == 2
    assert kinds[-3:] == ["review_synthesis", "anneal_challenge", "anneal_final"]
    assert messages[-3].get("provisional") is True
    assert messages[-2].get("provisional") is None
    assert messages[-1].get("provisional") is None
    assert messages[-3]["sender"] == "claude"
    assert messages[-2]["sender"] == "grok"
    assert messages[-1]["sender"] == "claude"
    assert len({item["round_id"] for item in messages}) == 1
    assert review.mode == "anneal"
    assert review.states == {"claude": "done", "grok": "done"}

    client.single_blind = True
    chat.review("@claude plain review")
    assert client.calls[-1][0] == "pi-peer"


def test_anneal_missing_blind_reply_stops_without_synthesis_or_final(tmp_path: Path) -> None:
    class DropBlindClient(AnnealClient):
        def turn(
            self,
            target: str,
            prompt: str,
            timeout_ms: int | None = None,
            cancel_event: threading.Event | None = None,
        ) -> tuple[str, str]:
            if target == "grok-peer" and "Question for independent review" in prompt:
                self._record(target, prompt, timeout_ms)
                self.blind.wait(timeout=2)
                raise ChatError("simulated blind failure")
            return super().turn(target, prompt, timeout_ms, cancel_event)

    client = DropBlindClient()
    chat, transcript = make_anneal_chat(tmp_path, client)

    review = chat.anneal("@claude,@grok Harden this plan")

    kinds = [item["kind"] for item in transcript.read()]
    assert "review_synthesis" not in kinds
    assert "anneal_challenge" not in kinds
    assert "anneal_final" not in kinds
    assert review.states["grok"] == "failed"
    assert any(
        "Anneal stopped" in item["body"] and item["kind"] == "review_status"
        for item in transcript.read()
    )


def test_one_controller_prevents_review_and_anneal_overlap(tmp_path: Path) -> None:
    class BlockingBlindClient(AnnealClient):
        def __init__(self) -> None:
            super().__init__()
            self.blind_started = threading.Event()
            self.release = threading.Event()

        def turn(
            self,
            target: str,
            prompt: str,
            timeout_ms: int | None = None,
            cancel_event: threading.Event | None = None,
        ) -> tuple[str, str]:
            if "Question for independent review" in prompt:
                self._record(target, prompt, timeout_ms)
                self.blind_started.set()
                assert self.release.wait(timeout=5)
                return "done", f"blind from {target}"
            return super().turn(target, prompt, timeout_ms, cancel_event)

    client = BlockingBlindClient()
    chat, _ = make_anneal_chat(tmp_path, client, "overlap-room")
    controller = ReviewController(chat)
    try:
        assert (
            controller.start_anneal("@claude,@grok Harden this plan")
            == "Anneal started: @claude authors, @grok critiques."
        )
        assert client.blind_started.wait(timeout=1)
        with pytest.raises(ChatError, match="already running"):
            controller.start("@claude ordinary review")
        with pytest.raises(ChatError, match="already running"):
            controller.start_anneal("@claude,@grok again")
        assert controller.is_active()
        assert "Anneal:" in controller.status()
    finally:
        client.release.set()
    assert controller.wait(timeout=2)
    assert controller.status() == "Anneal complete."


@pytest.mark.parametrize(
    "phase,marker",
    [
        ("blind", "Question for independent review"),
        ("challenge", "the critic in a two-participant anneal"),
        ("final", "the sole final author in a two-participant anneal"),
    ],
)
def test_cancel_stops_anneal_at_each_phase(tmp_path: Path, phase: str, marker: str) -> None:
    class PhaseGateClient(AnnealClient):
        def __init__(self) -> None:
            super().__init__()
            self.phase_started = threading.Event()

        def turn(
            self,
            target: str,
            prompt: str,
            timeout_ms: int | None = None,
            cancel_event: threading.Event | None = None,
        ) -> tuple[str, str]:
            if marker in prompt:
                self._record(target, prompt, timeout_ms)
                self.phase_started.set()
                assert cancel_event is not None
                while not cancel_event.wait(timeout=0.01):
                    pass
                raise ChatError("review cancelled")
            return super().turn(target, prompt, timeout_ms, cancel_event)

    client = PhaseGateClient()
    chat, transcript = make_anneal_chat(tmp_path, client, f"cancel-{phase}-room")
    controller = ReviewController(chat)
    try:
        controller.start_anneal("@claude,@grok Harden this plan")
        assert client.phase_started.wait(timeout=2)
        assert (
            controller.cancel()
            == "Local cancellation requested; participants may continue working."
        )
        assert controller.wait(timeout=2)
    finally:
        client.phase_started.set()

    kinds = [item["kind"] for item in transcript.read()]
    assert "anneal_final" not in kinds
    if phase in ("blind", "challenge"):
        assert "anneal_challenge" not in kinds
    if phase == "blind":
        assert "review_synthesis" not in kinds
    assert controller.status() == "Anneal cancelled locally; participants may continue working."


def test_retry_is_rejected_after_anneal_until_a_later_review(tmp_path: Path) -> None:
    client = AnnealClient()
    chat, _ = make_anneal_chat(tmp_path, client, "retry-room")
    controller = ReviewController(chat)
    controller.start_anneal("@claude,@grok Harden this plan")
    assert controller.wait(timeout=2)
    for target in ("claude", "grok", "synthesis"):
        with pytest.raises(ChatError, match="review-only"):
            controller.retry(target)

    client.fail_next_review_blind = True
    client.single_blind = True
    controller.start("@claude ordinary review")
    assert controller.wait(timeout=2)
    assert controller.retry("claude") == "Retrying @claude."
    assert controller.wait(timeout=2)
    assert controller.status() == "Review complete."


def test_once_anneal_runs_synchronously_through_group_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: dict[str, str] = {}

    def fake_anneal(self: GroupChat, text: str) -> object:
        recorded["text"] = text
        return None

    monkeypatch.setattr(GroupChat, "anneal", fake_anneal)
    monkeypatch.delenv("HERDR_GROUP_CHAT_SETUP_FAILURES", raising=False)

    code = main(
        [
            "--room",
            "once-room",
            "--state-dir",
            str(tmp_path),
            "--once",
            "/anneal @pi,@claude anneal once",
        ]
    )

    assert code == 0
    assert recorded["text"] == "@pi,@claude anneal once"


def test_inbox_keeps_anneal_final_and_attention_but_drops_challenge() -> None:
    messages = [
        {
            "seq": 1,
            "sender": "claude",
            "kind": "review_synthesis",
            "body": "provisional",
            "provisional": True,
        },
        {"seq": 2, "sender": "pi", "kind": "review_synthesis", "body": "ordinary synthesis"},
        {"seq": 3, "sender": "grok", "kind": "anneal_challenge", "body": "challenge"},
        {"seq": 4, "sender": "claude", "kind": "anneal_final", "body": "final"},
        {
            "seq": 5,
            "sender": "system",
            "kind": "review_status",
            "body": "@grok anneal challenge failed: boom",
        },
        {"seq": 6, "sender": "system", "kind": "review_status", "body": "quiet update"},
    ]

    assert [item["seq"] for item in inbox_messages(messages)] == [2, 4, 5]


def test_inbox_after_anneal_keeps_only_the_final_not_the_provisional_synthesis(
    tmp_path: Path,
) -> None:
    client = AnnealClient()
    chat, transcript = make_anneal_chat(tmp_path, client, "inbox-anneal-room")

    chat.anneal("@claude,@grok Harden this plan")

    inbox = inbox_messages(transcript.read())
    assert [item["kind"] for item in inbox] == ["anneal_final"]
    assert inbox[0]["sender"] == "claude"
    assert inbox[0]["body"] == "final from claude-peer"


def test_anneal_command_wiring_rejects_before_any_transcript_mutation(tmp_path: Path) -> None:
    chat, _, transcript = make_chat(tmp_path)
    controller = ReviewController(chat)

    assert handle_local_command("/anneal", chat, None) == "Review control is unavailable."
    assert handle_local_command("/anneal", chat, controller) == (
        "Usage: /anneal @author,@critic QUESTION"
    )
    assert handle_local_command("/annealx hi", chat, controller) == "Unknown command. Use /help."
    for bad in ("/anneal @pi,@pi hi", "/anneal @pi,@claude,@grok hi", "/anneal @gemini,@pi hi"):
        with pytest.raises(ChatError):
            handle_local_command(bad, chat, controller)
    assert transcript.read() == []


# --- bounded consensus council ------------------------------------------------------


class ConsensusClient(FakeClient):
    def __init__(self, votes: dict[str, str] | None = None) -> None:
        super().__init__()
        self.votes = votes or {
            "claude-peer": "VERDICT: PASS\nNo remaining objection.",
            "codex-peer": "VERDICT: PASS\nRatified.",
        }
        self.blind_barrier = threading.Barrier(2)
        self.call_lock = threading.Lock()

    def turn(
        self,
        target: str,
        prompt: str,
        timeout_ms: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[str, str]:
        with self.call_lock:
            self.calls.append((target, prompt))
            self.timeouts.append(timeout_ms)
        if "Question for independent review" in prompt:
            self.blind_barrier.wait(timeout=2)
            return "done", f"blind from {target}"
        if "CONSENSUS_SHARED_MATERIAL_BEGIN" in prompt:
            return "done", self.votes[target]
        if "Deterministic unanimity ledger" in prompt:
            return "done", f"final from {target}"
        if "Produce a Provisional contention packet" in prompt:
            return "done", "agreements; unresolved contention; candidate resolution"
        return super().turn(target, prompt, timeout_ms, cancel_event)


def make_consensus_chat(
    tmp_path: Path, client: FakeClient, room: str = "consensus-room"
) -> tuple[GroupChat, Transcript]:
    transcript = Transcript(tmp_path, room)
    chat = GroupChat(
        transcript,
        {"pi": "pi-peer", "claude": "claude-peer", "codex": "codex-peer"},
        client,
        synthesizer="pi",
    )
    return chat, transcript


def test_consensus_verdict_parser_requires_exact_first_nonempty_line() -> None:
    assert parse_consensus_verdict("VERDICT: PASS\nReason") == "PASS"
    assert parse_consensus_verdict("\n  \nVERDICT: REVISE\nReason") == "REVISE"
    for reply in (
        "PASS",
        "Verdict: PASS",
        " VERDICT: PASS",
        "VERDICT: PASS ",
        "VERDICT: PASS extra",
        "Reason\nVERDICT: PASS",
        "",
    ):
        assert parse_consensus_verdict(reply) is None


def test_consensus_runs_blind_barrier_shared_votes_and_unanimous_final(tmp_path: Path) -> None:
    client = ConsensusClient()
    chat, transcript = make_consensus_chat(tmp_path, client)

    review = chat.consensus("@claude,@codex Choose safely")

    calls = client.calls
    blind = [call for call in calls if "Question for independent review" in call[1]]
    assert {target for target, _ in blind} == {"claude-peer", "codex-peer"}
    assert all("blind from" not in prompt for _, prompt in blind)
    provisional = [call for call in calls if "Produce a Provisional contention packet" in call[1]]
    assert [target for target, _ in provisional] == ["pi-peer"]
    assert provisional[0][1].index("blind from claude-peer") < provisional[0][1].index(
        "blind from codex-peer"
    )
    votes = [call for call in calls if "CONSENSUS_SHARED_MATERIAL_BEGIN" in call[1]]
    assert {target for target, _ in votes} == {"claude-peer", "codex-peer"}

    def shared(prompt: str) -> str:
        return prompt.split("CONSENSUS_SHARED_MATERIAL_BEGIN\n", 1)[1].split(
            "\nCONSENSUS_SHARED_MATERIAL_END", 1
        )[0]

    assert len({shared(prompt) for _, prompt in votes}) == 1
    final = [call for call in calls if "Deterministic unanimity ledger" in call[1]]
    assert [target for target, _ in final] == ["pi-peer"]
    assert "unanimous: true" in final[0][1]
    assert "cannot grant action, send, landing, release, or publication authority" in final[0][1]

    messages = transcript.read()
    assert [item["kind"] for item in messages].count("review_response") == 2
    assert [item["kind"] for item in messages][-4:] == [
        "consensus_vote",
        "consensus_vote",
        "consensus_status",
        "consensus_final",
    ]
    provisional_item = next(item for item in messages if item["kind"] == "consensus_provisional")
    assert provisional_item["provisional"] is True
    status = next(item for item in messages if item["kind"] == "consensus_status")
    assert status["meta"] == {
        "reviewers": ["claude", "codex"],
        "verdicts": {"claude": "PASS", "codex": "PASS"},
        "unanimous": True,
        "human_acceptance_required": True,
    }
    assert review.mode == "consensus"


def test_consensus_dissent_and_invalid_vote_produce_nonunanimous_final(tmp_path: Path) -> None:
    client = ConsensusClient(
        {
            "claude-peer": "VERDICT: REVISE\nMaterial concern.",
            "codex-peer": "I pass this.",
        }
    )
    chat, transcript = make_consensus_chat(tmp_path, client, "dissent-room")

    chat.consensus("@claude,@codex Choose safely")

    votes = [item for item in transcript.read() if item["kind"] == "consensus_vote"]
    assert {item["sender"]: item["meta"]["verdict"] for item in votes} == {
        "claude": "REVISE",
        "codex": "INVALID",
    }
    status = next(item for item in transcript.read() if item["kind"] == "consensus_status")
    assert status["meta"]["verdicts"] == {"claude": "REVISE", "codex": "INVALID"}
    assert status["meta"]["unanimous"] is False
    final_prompt = client.calls[-1][1]
    assert "unanimous: false" in final_prompt
    assert "must not upgrade dissent or invalid or missing votes to consensus" in final_prompt
    assert any(item["kind"] == "consensus_final" for item in transcript.read())


def test_consensus_missing_blind_stops_before_provisional(tmp_path: Path) -> None:
    class MissingBlindClient(ConsensusClient):
        def turn(
            self, target: str, prompt: str, *args: object, **kwargs: object
        ) -> tuple[str, str]:
            if "Question for independent review" in prompt and target == "codex-peer":
                with self.call_lock:
                    self.calls.append((target, prompt))
                self.blind_barrier.wait(timeout=2)
                raise ChatError("blind failed")
            return super().turn(target, prompt, *args, **kwargs)

    client = MissingBlindClient()
    chat, transcript = make_consensus_chat(tmp_path, client, "missing-blind-room")

    chat.consensus("@claude,@codex Choose safely")

    kinds = [item["kind"] for item in transcript.read()]
    assert "consensus_provisional" not in kinds
    assert "consensus_vote" not in kinds
    assert "consensus_final" not in kinds
    status = next(item for item in transcript.read() if item["kind"] == "consensus_status")
    assert status["meta"]["unanimous"] is False
    assert status["meta"]["verdicts"] == {"claude": "MISSING", "codex": "MISSING"}


def test_consensus_failed_provisional_stops_before_vote(tmp_path: Path) -> None:
    class FailedProvisionalClient(ConsensusClient):
        def turn(
            self, target: str, prompt: str, *args: object, **kwargs: object
        ) -> tuple[str, str]:
            if "Produce a Provisional contention packet" in prompt:
                with self.call_lock:
                    self.calls.append((target, prompt))
                raise ChatError("provisional failed")
            return super().turn(target, prompt, *args, **kwargs)

    client = FailedProvisionalClient()
    chat, transcript = make_consensus_chat(tmp_path, client, "failed-provisional-room")

    chat.consensus("@claude,@codex Choose safely")

    kinds = [item["kind"] for item in transcript.read()]
    assert "consensus_vote" not in kinds
    assert "consensus_status" not in kinds
    assert "consensus_final" not in kinds


def test_consensus_missing_vote_is_nonunanimous_and_still_finalizes(tmp_path: Path) -> None:
    class MissingVoteClient(ConsensusClient):
        def turn(
            self, target: str, prompt: str, *args: object, **kwargs: object
        ) -> tuple[str, str]:
            if "CONSENSUS_SHARED_MATERIAL_BEGIN" in prompt and target == "codex-peer":
                with self.call_lock:
                    self.calls.append((target, prompt))
                raise ChatError("vote timed out")
            return super().turn(target, prompt, *args, **kwargs)

    client = MissingVoteClient()
    chat, transcript = make_consensus_chat(tmp_path, client, "missing-vote-room")

    chat.consensus("@claude,@codex Choose safely")

    status = next(item for item in transcript.read() if item["kind"] == "consensus_status")
    assert status["meta"]["verdicts"] == {"claude": "PASS", "codex": "MISSING"}
    assert status["meta"]["unanimous"] is False
    assert any(item["kind"] == "consensus_final" for item in transcript.read())


@pytest.mark.parametrize(
    "phase,marker,forbidden",
    [
        ("blind", "Question for independent review", "consensus_provisional"),
        ("provisional", "Produce a Provisional contention packet", "consensus_vote"),
        ("voting", "CONSENSUS_SHARED_MATERIAL_BEGIN", "consensus_status"),
        ("final", "Deterministic unanimity ledger", "consensus_final"),
    ],
)
def test_cancel_stops_consensus_at_each_phase(
    tmp_path: Path, phase: str, marker: str, forbidden: str
) -> None:
    class PhaseGateClient(ConsensusClient):
        def __init__(self) -> None:
            super().__init__()
            self.started = threading.Event()

        def turn(
            self,
            target: str,
            prompt: str,
            timeout_ms: int | None = None,
            cancel_event: threading.Event | None = None,
        ) -> tuple[str, str]:
            if marker in prompt:
                with self.call_lock:
                    self.calls.append((target, prompt))
                self.started.set()
                assert cancel_event is not None
                assert cancel_event.wait(timeout=2)
                raise ChatError("review cancelled")
            return super().turn(target, prompt, timeout_ms, cancel_event)

    client = PhaseGateClient()
    chat, transcript = make_consensus_chat(tmp_path, client, f"cancel-consensus-{phase}")
    controller = ReviewController(chat)
    controller.start_consensus("@claude,@codex Choose safely")
    assert client.started.wait(timeout=2)
    assert controller.cancel() == "Local cancellation requested; participants may continue working."
    assert controller.wait(timeout=2)

    assert forbidden not in [item["kind"] for item in transcript.read()]
    assert controller.status() == "Consensus cancelled locally; participants may continue working."


def test_consensus_controller_wiring_retry_picker_help_and_rendering(tmp_path: Path) -> None:
    client = ConsensusClient()
    chat, transcript = make_consensus_chat(tmp_path, client, "wiring-room")
    controller = ReviewController(chat)
    assert handle_local_command("/consensus", chat, None) == "Review control is unavailable."
    assert handle_local_command("/consensus", chat, controller) == (
        "Usage: /consensus [@agent,@agent] QUESTION"
    )
    assert "/consensus [@agents] QUESTION" in handle_local_command("/help", chat, controller)
    assert "all" in (mention_suggestions("/consensus @", tuple(chat.agents)) or ())
    assert controller.start_consensus("@claude,@codex Choose safely").startswith(
        "Consensus started"
    )
    assert controller.wait(timeout=2)
    with pytest.raises(ChatError, match="review-only"):
        controller.retry("claude")
    lines = message_lines(transcript.read(), 100)
    assert any("[provisional]>" in line for line in lines)
    assert any("[vote PASS]>" in line for line in lines)
    assert any("[final]>" in line for line in lines)
    inbox = inbox_messages(transcript.read())
    assert [item["kind"] for item in inbox] == ["consensus_status", "consensus_final"]


def test_once_consensus_runs_synchronously_through_group_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: dict[str, str] = {}

    def fake_consensus(self: GroupChat, text: str) -> object:
        recorded["text"] = text
        return None

    monkeypatch.setattr(GroupChat, "consensus", fake_consensus)
    monkeypatch.delenv("HERDR_GROUP_CHAT_SETUP_FAILURES", raising=False)
    code = main(
        [
            "--room",
            "once-consensus-room",
            "--state-dir",
            str(tmp_path),
            "--once",
            "/consensus @pi,@claude decide once",
        ]
    )
    assert code == 0
    assert recorded["text"] == "@pi,@claude decide once"


# --- bounded sol-fable model profile -------------------------------------------------


class ProfileClient(FakeClient):
    def live_targets(self) -> set[str]:
        return {"sol-peer", "fable-peer"}

    def states(self) -> dict[str, str]:
        return {"sol-peer": "idle", "fable-peer": "idle"}


def make_sol_fable_chat(tmp_path: Path) -> tuple[GroupChat, ProfileClient, Transcript]:
    transcript = Transcript(tmp_path, "sol-fable-room")
    client = ProfileClient()
    chat = GroupChat(
        transcript,
        {"sol": "sol-peer", "fable": "fable-peer"},
        client,
        synthesizer="sol",
    )
    return chat, client, transcript


VALID_RECEIPT = {
    "profile": "sol-fable",
    "verified": [
        {
            "role": "sol",
            "target": "sol-peer",
            "harness": "pi",
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "effort": "high",
            "verification": "native-ui verified",
        },
        {
            "role": "fable",
            "target": "fable-peer",
            "harness": "claude",
            "model": "fable",
            "effort": "high",
            "verification": "native-ui verified",
        },
    ],
}
valid_receipt_json = json.dumps(VALID_RECEIPT)


def valid_receipt_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(namespace["PROFILE_RECEIPT_ENV"], valid_receipt_json)


def test_profile_room_routes_only_sol_and_fable_and_composes_review_and_anneal(
    tmp_path: Path,
) -> None:
    chat, client, transcript = make_sol_fable_chat(tmp_path)

    created = chat.dispatch("hello")
    chat.review("challenge this plan")
    chat.anneal("@sol,@fable harden this plan")

    assert [item["sender"] for item in created] == ["human", "sol", "fable"]
    assert [target for target, _prompt in client.calls if "group chat" in _prompt][:2] == [
        "sol-peer",
        "fable-peer",
    ]
    kinds = [(item["sender"], item["kind"]) for item in transcript.read()]
    assert ("sol", "review_synthesis") in kinds
    assert ("sol", "anneal_final") in kinds
    assert ("fable", "anneal_challenge") in kinds


def test_default_overrides_stay_unchanged_and_closed() -> None:
    parse = namespace["parse_agent_overrides"]
    assert parse([]) == dict(namespace["DEFAULT_AGENTS"])
    assert parse(["pi=other-peer"])["pi"] == "other-peer"
    with pytest.raises(ChatError, match="invalid agent mapping"):
        parse(["sol=sol-peer"])  # profile roles are not default-roster names


def test_profile_requires_exactly_both_explicit_agent_roles_through_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StaticClient(ProfileClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__()

    monkeypatch.setattr(module, "HerdrClient", StaticClient)
    valid_receipt_env(monkeypatch)
    base = ["--state-dir", str(tmp_path), "--room", "strict-room", "--profile", "sol-fable"]

    # main() reports user-visible failures as exit code 2 without writing.
    assert main([*base, "--once", "hi"]) == 2  # zero mappings
    assert main([*base, "--agent", "sol=sol-peer", "--once", "hi"]) == 2  # partial
    assert (
        main(
            [
                *base,
                "--agent",
                "sol=sol-peer",
                "--agent",
                "fable=fable-peer",
                "--agent",
                "zork=zork-peer",  # extra ROOM_RE-valid role
                "--once",
                "hi",
            ]
        )
        == 2
    )
    assert Transcript(tmp_path, "strict-room").read() == []


def test_multiprocess_profile_receipt_append_is_atomic(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    start = context.Event()
    barrier = context.Barrier(2)
    payload = json.loads(valid_receipt_json)
    processes = [
        context.Process(
            target=concurrent_record_profile_receipt,
            args=(str(tmp_path), "receipt-race-room", payload, start, barrier),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    try:
        for process in processes:
            process.join(timeout=10)
        assert all(not process.is_alive() for process in processes)
        assert [process.exitcode for process in processes] == [0, 0]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)

    receipts = [
        item
        for item in Transcript(tmp_path, "receipt-race-room").read()
        if item["kind"] == namespace["PROFILE_RECEIPT_KIND"]
    ]
    assert len(receipts) == 1
    assert receipts[0]["meta"] == payload


def test_exact_two_role_mapping_runs_and_receipt_is_recorded_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class StaticClient(ProfileClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__()

    monkeypatch.setattr(module, "HerdrClient", StaticClient)
    valid_receipt_env(monkeypatch)
    base = [
        "--state-dir",
        str(tmp_path),
        "--room",
        "receipt-room",
        "--profile",
        "sol-fable",
        "--agent",
        "sol=sol-peer",
        "--agent",
        "fable=fable-peer",
    ]

    assert main([*base, "--once", "hello from the human"]) == 0
    assert main([*base, "--once", "hello again after reopen"]) == 0

    transcript = Transcript(tmp_path, "receipt-room")
    items = transcript.read()
    receipts = [item for item in items if item["kind"] == namespace["PROFILE_RECEIPT_KIND"]]
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["meta"]["profile"] == "sol-fable"
    assert {entry["role"] for entry in receipt["meta"]["verified"]} == {"sol", "fable"}
    body = receipt["body"]
    for needle in (
        "native-ui verified",
        "harness pi",
        "provider openai-codex",
        "model gpt-5.6-sol",
        "effort high",
        "model fable",
        "sol-peer",
        "fable-peer",
    ):
        assert needle in body, needle
    assert "attest" not in body.lower()
    senders = [item["sender"] for item in items]
    assert senders.count("sol") == 2 and senders.count("fable") == 2  # both turns, one receipt


def test_missing_receipt_fails_the_profile_room_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StaticClient(ProfileClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__()

    monkeypatch.setattr(module, "HerdrClient", StaticClient)
    monkeypatch.delenv(namespace["PROFILE_RECEIPT_ENV"], raising=False)

    assert (
        main(
            [
                "--state-dir",
                str(tmp_path),
                "--room",
                "no-receipt-room",
                "--profile",
                "sol-fable",
                "--agent",
                "sol=sol-peer",
                "--agent",
                "fable=fable-peer",
                "--once",
                "hi",
            ]
        )
        == 2
    )

    assert Transcript(tmp_path, "no-receipt-room").read() == []


def test_receipt_roster_mismatch_fails_the_profile_room_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StaticClient(ProfileClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__()

    monkeypatch.setattr(module, "HerdrClient", StaticClient)
    valid_receipt_env(monkeypatch)

    assert (
        main(
            [
                "--state-dir",
                str(tmp_path),
                "--room",
                "mismatch-room",
                "--profile",
                "sol-fable",
                "--agent",
                "sol=someone-else",
                "--agent",
                "fable=fable-peer",
                "--once",
                "hi",
            ]
        )
        == 2
    )

    assert Transcript(tmp_path, "mismatch-room").read() == []


def test_invalid_receipt_payloads_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StaticClient(ProfileClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__()

    monkeypatch.setattr(module, "HerdrClient", StaticClient)
    base = [
        "--state-dir",
        str(tmp_path),
        "--room",
        "invalid-room",
        "--profile",
        "sol-fable",
        "--agent",
        "sol=sol-peer",
        "--agent",
        "fable=fable-peer",
        "--once",
        "hi",
    ]
    for payload in (
        "not json",
        json.dumps({"profile": "other", "verified": []}),
        json.dumps({"profile": "sol-fable", "verified": [{"role": "sol"}]}),
        json.dumps(
            {
                "profile": "sol-fable",
                "verified": [
                    {
                        "role": "sol",
                        "target": "sol-peer",
                        "harness": "pi",
                        "model": "gpt-5.6-sol",
                        "effort": "high",
                        "verification": "model-service attested",
                    },
                    {
                        "role": "fable",
                        "target": "fable-peer",
                        "harness": "claude",
                        "model": "fable",
                        "effort": "high",
                        "verification": "native-ui verified",
                    },
                ],
            }
        ),
    ):
        monkeypatch.setenv(namespace["PROFILE_RECEIPT_ENV"], payload)
        assert main(base) == 2

    assert Transcript(tmp_path, "invalid-room").read() == []


def test_main_profile_defaults_the_synthesizer_to_sol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synthesizers: list[str] = []

    class SynthTrackingClient(ProfileClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__()

        def turn(
            self,
            target: str,
            prompt: str,
            timeout_ms: int | None = None,
            cancel_event: threading.Event | None = None,
        ) -> tuple[str, str]:
            if "designated synthesizer" in prompt:
                synthesizers.append(target)
            return super().turn(target, prompt, timeout_ms, cancel_event)

    monkeypatch.setattr(module, "HerdrClient", SynthTrackingClient)
    valid_receipt_env(monkeypatch)
    monkeypatch.delenv("HERDR_GROUP_CHAT_SYNTHESIZER", raising=False)
    assert (
        main(
            [
                "--state-dir",
                str(tmp_path),
                "--room",
                "synth-room",
                "--profile",
                "sol-fable",
                "--agent",
                "sol=sol-peer",
                "--agent",
                "fable=fable-peer",
                "--once",
                "/review question",
            ]
        )
        == 0
    )
    assert synthesizers == ["sol-peer"]
    transcript = Transcript(tmp_path, "synth-room")
    synthesis = [item for item in transcript.read() if item["kind"] == "review_synthesis"]
    assert synthesis and synthesis[0]["sender"] == "sol"


def test_main_default_room_records_no_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StaticClient(FakeClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__()

    monkeypatch.setattr(module, "HerdrClient", StaticClient)
    monkeypatch.delenv(namespace["PROFILE_RECEIPT_ENV"], raising=False)

    assert main(["--state-dir", str(tmp_path), "--room", "plain-room", "--once", "hi"]) == 0

    assert all(
        item.get("kind") != namespace["PROFILE_RECEIPT_KIND"]
        for item in Transcript(tmp_path, "plain-room").read()
    )


def test_receipt_dedupe_uses_exact_structured_metadata_not_profile_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StaticClient(ProfileClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__()

    monkeypatch.setattr(module, "HerdrClient", StaticClient)
    base = [
        "--state-dir",
        str(tmp_path),
        "--room",
        "dedupe-room",
        "--profile",
        "sol-fable",
        "--agent",
        "sol=sol-peer",
        "--agent",
        "fable=fable-peer",
        "--once",
        "hi",
    ]
    receipt = json.loads(valid_receipt_json)
    changed_effort = {
        "profile": "sol-fable",
        "verified": [dict(receipt["verified"][0], effort="low"), receipt["verified"][1]],
    }

    monkeypatch.setenv(namespace["PROFILE_RECEIPT_ENV"], valid_receipt_json)
    assert main(base) == 0
    monkeypatch.setenv(namespace["PROFILE_RECEIPT_ENV"], json.dumps(changed_effort))
    assert main(base) == 0

    receipts = [
        item
        for item in Transcript(tmp_path, "dedupe-room").read()
        if item["kind"] == namespace["PROFILE_RECEIPT_KIND"]
    ]

    # Identical payload dedupes; different evidence metadata is a new receipt.
    def normalized(payload: dict) -> dict:
        return {**payload, "verified": sorted(payload["verified"], key=lambda e: e["role"])}

    assert [item["meta"] for item in receipts] == [
        normalized(receipt),
        normalized(changed_effort),
    ]


DEFAULT_ROSTER = ("pi", "claude", "codex", "grok")
SOL_FABLE_ROSTER = ("sol", "fable")


def test_picker_opens_at_start_of_line_and_filters_case_insensitively() -> None:
    assert mention_suggestions("@", DEFAULT_ROSTER) == (*DEFAULT_ROSTER, "all")
    assert mention_suggestions("@CO", DEFAULT_ROSTER) == ("codex",)
    assert mention_suggestions("@sol", SOL_FABLE_ROSTER) == ("sol",)


def test_review_includes_all_but_anneal_hides_it() -> None:
    assert "all" in (mention_suggestions("/review @", DEFAULT_ROSTER) or ())
    assert mention_suggestions("/anneal @", DEFAULT_ROSTER) == DEFAULT_ROSTER
    assert "all" not in (mention_suggestions("/anneal @", DEFAULT_ROSTER) or ())


def test_anneal_second_mention_offers_only_the_remaining_role() -> None:
    assert mention_suggestions("/anneal @sol,@", SOL_FABLE_ROSTER) == ("fable",)
    assert mention_suggestions("/anneal @fable,@", SOL_FABLE_ROSTER) == ("sol",)


def test_emails_and_mid_draft_mentions_never_open_the_picker() -> None:
    for text in ("user@host", "hello @pi", "/review @pi question @codex", "x@y hi"):
        assert mention_fragment(text) is None, text
        assert mention_suggestions(text, DEFAULT_ROSTER) is None, text


def test_a_comma_alone_does_not_open_the_picker() -> None:
    assert mention_fragment("@sol,") is None
    assert mention_suggestions("@pi,", DEFAULT_ROSTER) is None


def test_tab_completion_replaces_only_the_active_fragment_without_a_space() -> None:
    assert complete_mention("@cl", "claude") == "@claude"
    assert complete_mention("/anneal @sol,@fab", "fable") == "/anneal @sol,@fable"
    assert not complete_mention("@co", "codex").endswith(" ")
    # An already-selected handle stays excluded from the next fragment's roster.
    assert mention_suggestions("/anneal @sol,@", SOL_FABLE_ROSTER) == ("fable",)


def test_esc_and_enter_leave_the_buffer_untouched() -> None:
    suggestions = mention_suggestions("@co", DEFAULT_ROSTER)
    assert suggestions == ("codex",)
    escaped = handle_picker_key("@co", "ESC", suggestions, 0)
    assert escaped == ("@co", 0)
    # ENTER is not consumed by the picker, so submission sees the exact buffer.
    assert handle_picker_key("@co", "ENTER", suggestions, 0) is None


def test_up_down_selection_cycles_through_candidates() -> None:
    suggestions = mention_suggestions("/anneal @", SOL_FABLE_ROSTER)
    assert suggestions == ("sol", "fable")
    assert handle_picker_key("/anneal @", "DOWN", suggestions, 0) == ("/anneal @", 1)
    assert handle_picker_key("/anneal @", "UP", suggestions, 0) == ("/anneal @", 1)
    assert mention_display(suggestions, 0) == "Mentions: [@sol] @fable"
    assert mention_display(suggestions, 1) == "Mentions: @sol [@fable]"


def test_parse_route_is_unchanged_for_picker_completed_inputs() -> None:
    assert parse_route("@claude hi there", DEFAULT_ROSTER) == Route(("claude",), "hi there")
    assert parse_route("@all hi", DEFAULT_ROSTER) == Route(DEFAULT_ROSTER, "hi")
    assert parse_route("@codex q", DEFAULT_ROSTER) == Route(("codex",), "q")
    assert parse_anneal("@sol,@fable QUESTION", SOL_FABLE_ROSTER) == (
        "sol",
        "fable",
        "QUESTION",
    )
    with pytest.raises(ChatError):
        parse_route("@nope q", SOL_FABLE_ROSTER)


def test_candidate_rosters_come_from_actual_room_participants(tmp_path: Path) -> None:
    chat, _, _ = make_chat(tmp_path)
    assert mention_suggestions("@", tuple(chat.agents)) == (*DEFAULT_ROSTER, "all")
    profile_chat, _, _ = make_chat(tmp_path)
    profile_chat.agents = {"sol": "sol-peer", "fable": "fable-peer"}
    assert mention_suggestions("/anneal @", tuple(profile_chat.agents)) == ("sol", "fable")


def test_tab_closes_the_fragment_and_fresh_mention_after_comma_reopens() -> None:
    completed = handle_picker_key("/anneal @so", "TAB", ("sol", "fable"), 0)
    assert completed == ("/anneal @sol", 0)
    # The completed fragment is closed, but a later comma plus fresh @ reopens
    # with the remaining role only.
    assert mention_suggestions("/anneal @sol,@", SOL_FABLE_ROSTER) == ("fable",)


def test_esc_closes_an_unmatched_query_without_changing_text() -> None:
    assert mention_suggestions("@zzz", DEFAULT_ROSTER) == ()
    assert handle_picker_key("@zzz", "ESC", (), 0) == ("@zzz", 0)


def test_tab_on_an_unmatched_query_closes_without_changing_text() -> None:
    assert handle_picker_key("@zzz", "TAB", (), 0) == ("@zzz", 0)
    # Up/Down remain no-ops with no suggestions.
    assert handle_picker_key("@zzz", "UP", (), 0) is None
    assert handle_picker_key("@zzz", "DOWN", (), 0) is None


# --- default sol-fable-grok profile ---------------------------------------------------


class TripleClient(ProfileClient):
    def live_targets(self) -> set[str]:
        return {"sol-peer", "fable-peer", "grok46-peer"}

    def states(self) -> dict[str, str]:
        return {"sol-peer": "idle", "fable-peer": "idle", "grok46-peer": "idle"}


def make_sol_fable_grok_chat(tmp_path: Path) -> tuple[GroupChat, TripleClient, Transcript]:
    transcript = Transcript(tmp_path, "sfg-room")
    client = TripleClient()
    chat = GroupChat(
        transcript,
        {"sol": "sol-peer", "fable": "fable-peer", "grok": "grok46-peer"},
        client,
        synthesizer="sol",
    )
    return chat, client, transcript


VALID_SFG_RECEIPT = {
    "profile": "sol-fable-grok",
    "verified": [
        dict(entry)
        for entry in (
            *VALID_RECEIPT["verified"][:2],
            {
                "role": "grok",
                "target": "grok46-peer",
                "harness": "grok",
                "model": "grok-4.6",
                "effort": "high",
                "verification": "native-ui verified",
            },
        )
    ],
}


def test_sol_fable_grok_profile_has_exact_ordered_roles_and_synthesizer() -> None:
    assert namespace["PROFILE_ROLES"]["sol-fable-grok"] == ("sol", "fable", "grok")
    assert namespace["PROFILE_SYNTHESIZER"]["sol-fable-grok"] == "sol"
    assert namespace["PROFILE_ROLES"]["sol-fable"] == ("sol", "fable")


def test_sol_fable_grok_room_routes_mention_review_and_anneal_over_all_three(
    tmp_path: Path,
) -> None:
    chat, client, transcript = make_sol_fable_grok_chat(tmp_path)

    created = chat.dispatch("@grok summarize the launch flags")
    chat.review("challenge this plan")
    chat.anneal("@sol,@grok harden this plan")

    assert [item["sender"] for item in created] == ["human", "grok"]
    routed = [target for target, _prompt in client.calls if "group chat" in _prompt]
    assert routed[0] == "grok46-peer"  # the explicit mention
    assert set(routed[1:4]) == {"sol-peer", "fable-peer", "grok46-peer"}  # review round
    kinds = [(item["sender"], item["kind"]) for item in transcript.read()]
    assert ("sol", "review_synthesis") in kinds
    assert ("sol", "anneal_final") in kinds
    assert ("grok", "anneal_challenge") in kinds


def test_sol_fable_grok_requires_exactly_three_explicit_roles_through_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StaticClient(TripleClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__()

    monkeypatch.setattr(module, "HerdrClient", StaticClient)
    monkeypatch.setenv(namespace["PROFILE_RECEIPT_ENV"], json.dumps(VALID_SFG_RECEIPT))
    base = ["--state-dir", str(tmp_path), "--room", "strict-sfg", "--profile", "sol-fable-grok"]

    assert main([*base, "--once", "hi"]) == 2  # zero mappings
    assert main([*base, "--agent", "grok=grok46-peer", "--once", "hi"]) == 2  # partial
    assert main([*base, "--agent", "zork=zork-peer", "--once", "hi"]) == 2  # unknown role
    assert (
        main(
            [
                *base,
                "--agent",
                "sol=sol-peer",
                "--agent",
                "fable=fable-peer",
                "--agent",
                "grok=grok46-peer",
                "--agent",
                "sol=dup-peer",  # duplicate role
                "--once",
                "hi",
            ]
        )
        == 2
    )
    assert Transcript(tmp_path, "strict-sfg").read() == []


def test_sol_fable_grok_exact_roster_records_the_verified_receipt_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StaticClient(TripleClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__()

    monkeypatch.setattr(module, "HerdrClient", StaticClient)
    monkeypatch.setenv(namespace["PROFILE_RECEIPT_ENV"], json.dumps(VALID_SFG_RECEIPT))
    base = [
        "--state-dir",
        str(tmp_path),
        "--room",
        "receipt-sfg",
        "--profile",
        "sol-fable-grok",
        "--agent",
        "sol=sol-peer",
        "--agent",
        "fable=fable-peer",
        "--agent",
        "grok=grok46-peer",
    ]

    assert main([*base, "--once", "hello from the human"]) == 0

    receipts = [
        item
        for item in Transcript(tmp_path, "receipt-sfg").read()
        if item["kind"] == namespace["PROFILE_RECEIPT_KIND"]
    ]
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["meta"]["profile"] == "sol-fable-grok"
    assert [entry["role"] for entry in receipt["meta"]["verified"]] == ["fable", "grok", "sol"]
    body = receipt["body"]
    for needle in (
        "harness grok",
        "model grok-4.6",
        "effort high",
        "target grok46-peer",
        "native-ui verified",
    ):
        assert needle in body, needle
    # The stored sol-fable room stays default-able: its profile is unchanged.
    assert namespace["PROFILE_ROLES"]["sol-fable"] == ("sol", "fable")
