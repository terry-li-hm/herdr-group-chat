from __future__ import annotations

import json
import re
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
    assert transcript.cursors()["pi"] == transcript.read()[-1]["seq"]


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


def test_review_controller_is_non_blocking_and_cancels_exact_active_target(
    tmp_path: Path,
) -> None:
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
                        self.cancel(target)
                        raise ChatError("review cancelled")
            return "done", f"reply from {target}"

        def cancel(self, target: str) -> None:
            super().cancel(target)
            self.release.set()

    transcript = Transcript(tmp_path, "cancel-room")
    client = BlockingClient()
    chat = GroupChat(transcript, {"pi": "pi-peer", "claude": "claude-peer"}, client)
    controller = ReviewController(chat)

    notice = controller.start("@claude Check this")
    assert notice == "Review started with @claude; @pi synthesizes."
    assert client.started.wait(timeout=1)
    assert controller.is_active()
    assert "@claude working" in controller.status()
    assert controller.cancel() == "Cancellation requested."
    assert controller.wait(timeout=2)

    assert client.cancelled == ["claude-peer"]
    assert controller.status() == "Review cancelled."
    assert not any(item["kind"] == "review_synthesis" for item in transcript.read())


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
    assert controller.cancel() == "Cancellation requested."
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
            self.cancel(target)
            raise ChatError("review cancelled")

    transcript = Transcript(tmp_path, "synthesis-cancel-room")
    client = BlockingSynthesisClient()
    chat = GroupChat(transcript, {"pi": "pi-peer", "claude": "claude-peer"}, client)
    controller = ReviewController(chat)

    controller.start("@claude Check this")
    assert client.synthesis_started.wait(timeout=1)
    assert controller.cancel() == "Cancellation requested."
    assert controller.wait(timeout=2)

    assert client.cancelled == ["pi-peer"]
    assert controller.status() == "Review cancelled."
    assert not any("synthesis failed" in item["body"] for item in transcript.read())
    assert not any(item["kind"] == "review_synthesis" for item in transcript.read())


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
    assert controller.cancel() == "Cancellation requested."
    client.release_retry_check.set()
    assert controller.wait(timeout=2)

    assert len(client.calls) == 1
    assert controller.status() == "Review cancelled."


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


def test_herdr_cancel_uses_exact_target_and_ctrl_c() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    HerdrClient(herdr_bin="herdr-test", runner=runner).cancel("claude-peer")
    assert calls == [["herdr-test", "agent", "send-keys", "claude-peer", "ctrl+c"]]


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


def test_herdr_turn_cancels_after_submission_if_request_arrived_during_submit() -> None:
    calls: list[list[str]] = []
    cancel_event = threading.Event()
    token = "b" * 32

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["agent", "prompt"]:
            cancel_event.set()
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    with pytest.raises(ChatError, match="review cancelled"):
        client.turn(
            "claude-peer",
            f"prompt\nHGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}",
            12_000,
            cancel_event,
        )

    assert [call[1:3] for call in calls] == [
        ["agent", "prompt"],
        ["agent", "send-keys"],
    ]
    assert calls[-1][-2:] == ["claude-peer", "ctrl+c"]


def test_herdr_submission_process_is_terminated_on_immediate_cancel() -> None:
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
    assert [call[1:3] for call in calls] == [
        ["agent", "prompt"],
        ["agent", "send-keys"],
    ]


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


def test_herdr_turn_timeout_interrupts_active_target(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    token = "d" * 32
    ticks = iter((0.0, 2.0))
    monkeypatch.setattr(namespace["time"], "monotonic", lambda: next(ticks))

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["agent", "get"]:
            stdout = '{"result":{"agent":{"agent_status":"working"}}}'
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    with pytest.raises(ChatError, match="timed out after 1000 ms"):
        client.turn(
            "claude-peer",
            f"prompt\nHGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}",
            1_000,
        )

    assert [call[1:3] for call in calls] == [
        ["agent", "prompt"],
        ["agent", "get"],
        ["agent", "send-keys"],
    ]


def test_herdr_turn_timeout_does_not_ctrl_c_an_idle_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idle Codex treats Ctrl-C as quit; a timeout with no landed prompt must not kill it."""
    calls: list[list[str]] = []
    token = "6" * 32
    monkeypatch.setitem(namespace, "POLL_INTERVAL_S", 0)

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["agent", "get"]:
            stdout = '{"result":{"agent":{"agent_status":"idle"}}}'
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    with pytest.raises(ChatError, match="timed out after 50 ms"):
        client.turn(
            "codex-peer",
            f"prompt\nHGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}",
            50,
        )

    assert ["agent", "send-keys"] not in [call[1:3] for call in calls]


def test_herdr_turn_timeout_ctrl_c_reaches_a_working_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    token = "7" * 32
    monkeypatch.setitem(namespace, "POLL_INTERVAL_S", 0)

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["agent", "get"]:
            stdout = '{"result":{"agent":{"agent_status":"working"}}}'
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    with pytest.raises(ChatError, match="timed out after 50 ms"):
        client.turn(
            "claude-peer",
            f"prompt\nHGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}",
            50,
        )

    assert [call[1:3] for call in calls][-2:] == [["agent", "get"], ["agent", "send-keys"]]
    assert calls[-1][-2:] == ["claude-peer", "ctrl+c"]


def test_herdr_turn_review_cancel_skips_ctrl_c_for_an_idle_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    cancel_event = threading.Event()
    token = "8" * 32
    monkeypatch.setitem(namespace, "POLL_INTERVAL_S", 0)

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["agent", "get"]:
            cancel_event.set()
            stdout = '{"result":{"agent":{"agent_status":"idle"}}}'
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

    assert ["agent", "send-keys"] not in [call[1:3] for call in calls]


def test_invalid_post_submission_status_interrupts_active_target() -> None:
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
        ["agent", "send-keys"],
    ]


def test_submission_command_failure_interrupts_possibly_active_target() -> None:
    calls: list[list[str]] = []
    token = "f" * 32

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["agent", "prompt"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="submit failed")
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    with pytest.raises(ChatError, match="submit failed"):
        client.turn(
            "claude-peer",
            f"prompt\nHGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}",
            12_000,
        )

    assert [call[1:3] for call in calls] == [
        ["agent", "prompt"],
        ["agent", "send-keys"],
    ]


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
        "open",
    ]
    sol_fable = manifest["actions"][1]
    assert sol_fable["title"] == "New Sol + Fable chat"
    assert sol_fable["command"] == ["./new-room", "--launch", "--profile", "sol-fable"]
    assert sol_fable["contexts"] == ["workspace", "tab", "pane"]
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
        assert controller.cancel() == "Cancellation requested."
        assert controller.wait(timeout=2)
    finally:
        client.phase_started.set()

    kinds = [item["kind"] for item in transcript.read()]
    assert "anneal_final" not in kinds
    if phase in ("blind", "challenge"):
        assert "anneal_challenge" not in kinds
    if phase == "blind":
        assert "review_synthesis" not in kinds
    assert controller.status() == "Anneal cancelled."


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
