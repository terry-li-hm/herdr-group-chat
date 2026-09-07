"""Acceptance assays for the relay hardening drawn from the 2026 OpenAI agent
incident: the relay refuses credential-shaped payloads at its two write
chokepoints, and silent recipient truncation becomes a visible journal event
plus a room-status signal count.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from test_herdr_group_chat import ChatError, Transcript, make_chat

namespace = sys.modules["herdr_group_chat"].__dict__
CredentialRefused = namespace["CredentialRefused"]
credential_kind = namespace["credential_kind"]
room_signal_counts = namespace["room_signal_counts"]
room_signal_summary = namespace["room_signal_summary"]


CREDENTIAL_SAMPLES = {
    "aws_access_key": "here is AKIAIOSFODNN7EXAMPLE for the bucket",
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----",
    "jwt": (
        "token eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    ),
    "github_token": "use ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8" + " to push",
    "openai_style_key": "OPENAI_API_KEY=sk-proj-" + "abcdefghijklmnopqrstuvwxyz0123456789",
    "anthropic_key": "sk-ant-api03-" + "abcdefghijklmnopqrstuvwxyz0123456789-ABCDEF",
    "slack_token": "xoxb-" + "1234567890-1234567890123-abcdefghijklmnopqrstuvwx",
    "google_api_key": "key AIza" + "SyA-abcdefghijklmnopqrstuvwxyz01234567",
    "bearer_token": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789ABCDEF",
    "onepassword_ref": "read op://Private/github/token first",
}

CAP_META = {"delivered": [], "dropped": [], "max_turns": 1}

BENIGN_SAMPLES = [
    "HGCHAT_REPLY_BEGIN 0123456789abcdef0123456789abcdef",
    "the sha is 694ee30 and the branch is main",
    "commit 3f2a9c1d0b4e5f6a7b8c9d0e1f2a3b4c5d6e7f80 landed",
    "https://simonwillison.net/2026/Aug/7/openai-timeline/",
    "set AWS_REGION=ap-east-1 and retry",
    "a bearer of bad news",
    "op is short for operation here",
    "the model id is claude-fable-5-1",
]


@pytest.mark.parametrize("kind", sorted(CREDENTIAL_SAMPLES))
def test_credential_kind_names_each_credential_shape(kind: str) -> None:
    assert credential_kind(CREDENTIAL_SAMPLES[kind]) == kind


@pytest.mark.parametrize("text", BENIGN_SAMPLES)
def test_credential_kind_passes_benign_text(text: str) -> None:
    assert credential_kind(text) is None


def test_credential_refused_is_a_chat_error() -> None:
    assert issubclass(CredentialRefused, ChatError)


def test_transcript_append_refuses_credentials_without_recording_them(tmp_path: Path) -> None:
    transcript = Transcript(tmp_path, "gate-room")
    with pytest.raises(CredentialRefused, match="aws_access_key"):
        transcript.append("human", ("pi",), CREDENTIAL_SAMPLES["aws_access_key"])
    journal = tmp_path / "gate-room.jsonl"
    assert not journal.exists() or "AKIA" not in journal.read_text(encoding="utf-8")
    assert transcript.read() == []


def test_write_turn_refuses_credentials_and_leaves_no_file(tmp_path: Path) -> None:
    transcript = Transcript(tmp_path, "gate-room")
    token = "c" * 32
    with pytest.raises(CredentialRefused, match="private_key"):
        transcript.write_turn(token, CREDENTIAL_SAMPLES["private_key"])
    assert not (tmp_path / "gate-room.turns" / f"{token}.md").exists()


def test_agent_reply_with_a_credential_is_refused_and_logged_as_a_system_line(
    tmp_path: Path,
) -> None:
    chat, client, transcript = make_chat(tmp_path)

    def leaking_turn(target, prompt, timeout_ms=None, cancel_event=None, agent_label=None):
        client.calls.append((target, prompt))
        return "done", f"sure, use {CREDENTIAL_SAMPLES['github_token']}"

    client.turn = leaking_turn  # type: ignore[method-assign]
    created = chat.dispatch("@pi what is the token")

    items = transcript.read()
    bodies = "\n".join(item["body"] for item in items)
    assert "ghp_" not in bodies
    system = [item for item in items if item["sender"] == "system"]
    assert system, items
    assert system[-1]["kind"] == "credential_refused"
    assert system[-1]["meta"] == {"agent": "pi", "credential_kind": "github_token"}
    assert "credential" in system[-1]["body"]
    assert created[-1] is not None


def test_human_message_with_a_credential_is_refused_before_any_agent_is_prompted(
    tmp_path: Path,
) -> None:
    chat, client, transcript = make_chat(tmp_path)
    with pytest.raises(CredentialRefused):
        chat.dispatch(f"@all {CREDENTIAL_SAMPLES['jwt']}")
    assert client.calls == []
    assert all("eyJ" not in item["body"] for item in transcript.read())


def test_recipient_cap_records_a_visible_journal_event(tmp_path: Path) -> None:
    chat, client, transcript = make_chat(tmp_path, max_turns=2)
    chat.dispatch("@all bounded")

    assert [target for target, _ in client.calls] == ["pi-peer", "claude-peer"]
    capped = [item for item in transcript.read() if item["kind"] == "turn_capped"]
    assert len(capped) == 1
    assert capped[0]["sender"] == "system"
    assert capped[0]["recipients"] == ["human"]
    assert capped[0]["meta"] == {
        "delivered": ["pi", "claude"],
        "dropped": ["codex", "grok"],
        "max_turns": 2,
    }
    assert "@codex" in capped[0]["body"] and "@grok" in capped[0]["body"]


def test_recipient_cap_event_is_not_shown_to_agents(tmp_path: Path) -> None:
    chat, client, _transcript = make_chat(tmp_path, max_turns=2)
    chat.dispatch("@all first")
    client.calls.clear()
    chat.dispatch("@pi second")
    assert "turn cap" not in client.calls[0][1].lower()
    assert "not delivered" not in client.calls[0][1].lower()


def test_uncapped_delivery_records_no_cap_event(tmp_path: Path) -> None:
    chat, _client, transcript = make_chat(tmp_path, max_turns=4)
    chat.dispatch("@all fits")
    assert [item for item in transcript.read() if item["kind"] == "turn_capped"] == []


def test_room_signal_counts_tally_blocked_failed_capped_and_refused(tmp_path: Path) -> None:
    transcript = Transcript(tmp_path, "signals-room")
    transcript.append("human", ("pi",), "hello")
    transcript.append(
        "system", ("human",), "@pi is blocked and needs attention in Herdr.", kind="turn_blocked"
    )
    transcript.append("system", ("human",), "@pi: timed out", kind="turn_failed")
    transcript.append("system", ("human",), "@pi: timed out again", kind="turn_failed")
    transcript.append(
        "system",
        ("human",),
        "cap",
        kind="turn_capped",
        meta={"delivered": ["pi"], "dropped": ["claude"], "max_turns": 1},
    )
    transcript.append(
        "system",
        ("human",),
        "refused",
        kind="credential_refused",
        meta={"agent": "pi", "credential_kind": "jwt"},
    )

    assert room_signal_counts(transcript.read()) == {
        "blocked": 1,
        "failed": 2,
        "capped": 1,
        "refused": 1,
    }


def test_room_signal_summary_is_empty_for_a_quiet_room_and_terse_otherwise(tmp_path: Path) -> None:
    transcript = Transcript(tmp_path, "signals-room")
    transcript.append("human", ("pi",), "hello")
    assert room_signal_summary(transcript.read()) == ""
    transcript.append("system", ("human",), "x", kind="turn_failed")
    transcript.append("system", ("human",), "x", kind="turn_failed")
    transcript.append("system", ("human",), "x", kind="turn_capped", meta=CAP_META)
    summary = room_signal_summary(transcript.read())
    assert summary == "signals: 2 failed · 1 capped"


def test_dispatch_marks_blocked_and_failed_system_lines_with_kinds(tmp_path: Path) -> None:
    chat, client, transcript = make_chat(tmp_path)
    outcomes = iter([("blocked", ""), ChatError("timed out")])

    def turn(target, prompt, timeout_ms=None, cancel_event=None, agent_label=None):
        client.calls.append((target, prompt))
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    client.turn = turn  # type: ignore[method-assign]
    chat.dispatch("@pi,@claude go")
    kinds = [item["kind"] for item in transcript.read() if item["sender"] == "system"]
    assert kinds == ["turn_blocked", "turn_failed"]


def test_room_status_line_includes_signal_summary(tmp_path: Path) -> None:
    participant_status = namespace["participant_status"]
    chat, _client, transcript = make_chat(tmp_path)
    transcript.append("system", ("human",), "x", kind="turn_blocked")
    status = participant_status(chat, "Ready.")
    assert status.endswith("signals: 1 blocked"), status
    assert re.search(r"@pi \w+", status)


def test_room_status_signals_cover_only_the_latest_delivery(tmp_path: Path) -> None:
    """A long-lived room must not accumulate stale signals in its status line."""
    participant_status = namespace["participant_status"]
    chat, _client, transcript = make_chat(tmp_path)
    transcript.append("human", ("pi",), "first")
    transcript.append("system", ("human",), "x", kind="turn_failed")
    transcript.append("system", ("human",), "x", kind="turn_failed")
    transcript.append("human", ("pi",), "second")
    transcript.append("pi", ("human", "all"), "fine")
    assert "signals" not in participant_status(chat, "Ready.")
    transcript.append("system", ("human",), "x", kind="turn_capped", meta=CAP_META)
    assert participant_status(chat, "Ready.").endswith("signals: 1 capped")
