from __future__ import annotations

import re
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
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
Route = namespace["Route"]
Transcript = namespace["Transcript"]
extract_reply = namespace["extract_reply"]
parse_route = namespace["parse_route"]


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def live_targets(self) -> set[str]:
        return {"pi-peer", "claude-peer", "grok-peer"}

    def turn(self, target: str, prompt: str) -> tuple[str, str]:
        self.calls.append((target, prompt))
        match = re.search(r"HGCHAT_REPLY_BEGIN ([a-f0-9]{32})", prompt)
        assert match
        return "done", f"reply from {target}"


def make_chat(tmp_path: Path, max_turns: int = 3) -> tuple[object, FakeClient, object]:
    transcript = Transcript(tmp_path, "test-room")
    client = FakeClient()
    chat = GroupChat(
        transcript,
        {"pi": "pi-peer", "claude": "claude-peer", "grok": "grok-peer"},
        client,
        max_turns=max_turns,
    )
    return chat, client, transcript


def test_plain_message_routes_to_all_in_stable_order() -> None:
    assert parse_route("hello", ("pi", "claude", "grok")) == Route(
        ("pi", "claude", "grok"), "hello"
    )


def test_mentions_route_to_named_agents_and_reject_unknown() -> None:
    assert parse_route("@pi,@grok compare", ("pi", "claude", "grok")) == Route(
        ("pi", "grok"), "compare"
    )
    with pytest.raises(ChatError, match="unknown participant"):
        parse_route("@codex hello", ("pi", "claude", "grok"))


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
        "grok-peer",
    ]
    assert [item["sender"] for item in created] == ["human", "pi", "claude", "grok"]
    assert "[pi] reply from pi-peer" in client.calls[1][1]
    assert "[claude] reply from claude-peer" in client.calls[2][1]
    assert "[pi] reply from pi-peer" not in client.calls[0][1]
    assert len(transcript.read()) == 4


def test_max_turns_caps_an_all_round(tmp_path: Path) -> None:
    chat, client, _ = make_chat(tmp_path, max_turns=2)
    chat.dispatch("@all bounded")
    assert [target for target, _ in client.calls] == ["pi-peer", "claude-peer"]


def test_direct_message_calls_only_one_agent(tmp_path: Path) -> None:
    chat, client, _ = make_chat(tmp_path)
    chat.dispatch("@grok answer")
    assert [target for target, _ in client.calls] == ["grok-peer"]


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


def test_extract_reply_removes_grok_tui_timestamp_chrome() -> None:
    token = "b" * 32
    terminal = (
        f"HGCHAT_REPLY_BEGIN {token}\n"
        "I am here with my     1:12 PM\n"
        "     ordinary tools.\n"
        f"HGCHAT_REPLY_END {token}\n"
    )
    assert extract_reply(terminal, token) == "I am here with my ordinary tools."


def test_missing_live_agent_fails_before_writing(tmp_path: Path) -> None:
    chat, client, transcript = make_chat(tmp_path)
    client.live_targets = lambda: {"pi-peer"}
    with pytest.raises(ChatError, match="participant not live"):
        chat.dispatch("@claude hello")
    assert transcript.read() == []
