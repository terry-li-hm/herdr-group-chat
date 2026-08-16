from __future__ import annotations

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
build_prompt = namespace["build_prompt"]
build_review_prompt = namespace["build_review_prompt"]
build_synthesis_prompt = namespace["build_synthesis_prompt"]
message_lines = namespace["message_lines"]
parse_agent_timeouts = namespace["parse_agent_timeouts"]
parse_route = namespace["parse_route"]
resolve_state_dir = namespace["resolve_state_dir"]
participant_status = namespace["participant_status"]
handle_local_command = namespace["handle_local_command"]
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
    assert [action["id"] for action in manifest["actions"]] == ["new", "open"]
    assert [pane["id"] for pane in manifest["panes"]] == ["new-room", "room"]
    assert all(pane["placement"] == "tab" for pane in manifest["panes"])
    assert "events" not in manifest
    assert "startup" not in manifest
    commands = [item["command"] for item in (*manifest["actions"], *manifest["panes"])]
    for command in commands:
        target = EFFECTOR.parent / command[0]
        assert target.is_file()
        assert access(target, X_OK)
