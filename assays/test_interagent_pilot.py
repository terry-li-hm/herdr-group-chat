"""Deterministic tests for the existing HerdrClient.turn collector.

Exercises the actual loaded HerdrClient.turn (herdr-group-chat, loaded via
SourceFileLoader like the existing excerpts). Fake Herdr runners, synthetic
clocks or bounded real polling (POLL_INTERVAL_S=0), and synthetic session
JSONL fixtures under tmp_path only. No model, Herdr socket, or real transcript
is touched.

After the pilot repair these tests all pass on the repaired source; see
PILOT-NOTES.md for the baseline failure history.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from urllib.parse import quote

import pytest

EFFECTOR = Path(__file__).resolve().parent.parent / "herdr-group-chat"
loader = SourceFileLoader("herdr_group_chat_interagent_pilot", str(EFFECTOR))
spec = spec_from_loader(loader.name, loader)
assert spec is not None
module = module_from_spec(spec)
sys.modules[module.__name__] = module
loader.exec_module(module)
namespace = module.__dict__

ChatError = namespace["ChatError"]
HerdrClient = namespace["HerdrClient"]

PI = "pi"
CLAUDE = "claude"
TARGETS = {PI: "pi-peer", CLAUDE: "claude-peer"}
TOKEN = "a" * 32
OTHER_TOKEN = "b" * 32


REFUSAL_TIMEOUT_MS = 5_000


def bound_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Synthetic monotonic clock that can never run past the 5 s deadline.

    Every refusal-path test uses this with REFUSAL_TIMEOUT_MS, so even a
    regression that silences the refusal terminates at the synthetic timeout
    within a bounded number of polls instead of hanging the suite.
    """
    ticks = iter([0.0] + [0.1 * step for step in range(1, 61)] + [999.0])
    monkeypatch.setattr(namespace["time"], "monotonic", lambda: next(ticks, 999.0))


def prompt_text(token: str) -> str:
    return f"prompt\nHGCHAT_REPLY_BEGIN {token}\nHGCHAT_REPLY_END {token}"


def pi_agent_info(session_file: Path, status: str, target: str) -> dict:
    return {
        "name": target,
        "agent": "pi",
        "agent_status": status,
        "cwd": "/synthetic/project/room-a",
        "agent_session": {
            "agent": "pi",
            "source": "herdr:pi",
            "kind": "path",
            "value": str(session_file),
        },
    }


def write_pi_session(path: Path, assistant_text: str) -> Path:
    record = {
        "type": "message",
        "message": {"role": "assistant", "content": assistant_text},
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def claude_agent_info(session_id: str, status: str, target: str) -> dict:
    return {
        "name": target,
        "agent": "claude",
        "agent_status": status,
        "cwd": "/synthetic/project/room-a",
        "agent_session": {"source": "herdr:claude", "value": session_id},
    }


def claude_session_path(home: Path, session_id: str) -> Path:
    project = (
        home
        / ".claude"
        / "projects"
        / namespace["claude_project_dir_name"]("/synthetic/project/room-a")
    )
    project.mkdir(parents=True, exist_ok=True)
    return project / f"{session_id}.jsonl"


def write_claude_session(path: Path, assistant_text: str) -> Path:
    record = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": assistant_text}]},
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# (1) A complete exact-token reply already persisted in the named session can
# settle the request while agent_status remains "working", and the observed
# working status must be preserved (never falsely reported done/idle).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [PI, CLAUDE])
def test_session_reply_settles_while_status_stays_working(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Desired and repaired behavior: the persisted exact-token reply settles
    the turn as ("working", reply) while the observed working status is
    preserved; the prompt is submitted exactly once and no keys are sent.
    On the pre-repair baseline this timed out because turn() only consulted
    the session readers in the idle/done branch.
    """
    token = TOKEN
    answer = "answer"
    reply_text = f"HGCHAT_REPLY_BEGIN {token}\n{answer}\nHGCHAT_REPLY_END {token}"
    if kind == PI:
        session_file = write_pi_session(tmp_path / "session.jsonl", reply_text)
    else:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        session_file = write_claude_session(claude_session_path(tmp_path, "0" * 16), reply_text)

    calls: list[list[str]] = []
    monkeypatch.setitem(namespace, "POLL_INTERVAL_S", 0)
    ticks = iter([0.0] + [0.5 * step for step in range(1, 40)] + [999.0])
    monkeypatch.setattr(namespace["time"], "monotonic", lambda: next(ticks, 999.0))
    target = TARGETS[kind]

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["agent", "get"]:
            info = (
                pi_agent_info(session_file, "working", target)
                if kind == PI
                else claude_agent_info("0" * 16, "working", target)
            )
            stdout = json.dumps({"result": {"agent": info}})
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    status, reply = client.turn(target, prompt_text(token), 12_000)

    assert status == "working", "must preserve the observed working status"
    assert reply == answer
    assert calls[0][1:3] == ["agent", "prompt"]
    assert ["agent", "prompt"] not in [call[1:3] for call in calls][1:]
    assert ["agent", "send-keys"] not in [call[1:3] for call in calls]


# ---------------------------------------------------------------------------
# (2) Wrong-token and incomplete session replies never settle the request and
# eventually produce the existing timeout or refusal, without re-submitting.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply_text", "label"),
    [
        (
            f"HGCHAT_REPLY_BEGIN {OTHER_TOKEN}\nother answer\nHGCHAT_REPLY_END {OTHER_TOKEN}",
            "wrong-token",
        ),
        (f"HGCHAT_REPLY_BEGIN {TOKEN}\nnever finished", "incomplete"),
    ],
)
def test_bad_session_reply_then_done_yields_existing_refusal(
    reply_text: str, label: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrong-token/incomplete Pi session reply, then done, fails via the
    existing unmarked-stability refusal; the prompt is never re-submitted."""
    assert label
    token = TOKEN
    session_file = write_pi_session(tmp_path / "session.jsonl", reply_text)
    calls: list[list[str]] = []
    monkeypatch.setitem(namespace, "POLL_INTERVAL_S", 0)
    bound_clock(monkeypatch)
    polls = 0
    target = "pi-peer"

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal polls
        calls.append(argv)
        if argv[1:3] == ["agent", "get"]:
            polls += 1
            status = "working" if polls == 1 else "done"
            info = pi_agent_info(session_file, status, target)
            stdout = json.dumps({"result": {"agent": info}})
        elif argv[1:3] == ["agent", "read"]:
            stdout = "unrelated terminal text without markers"
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    with pytest.raises(
        ChatError,
        match=rf"reply markers HGCHAT_REPLY_\* not found in @{target}'s completed response",
    ):
        client.turn(target, prompt_text(token), REFUSAL_TIMEOUT_MS)

    kinds = [call[1:3] for call in calls]
    assert kinds.count(["agent", "prompt"]) == 1
    assert ["agent", "send-keys"] not in kinds


@pytest.mark.parametrize(
    ("reply_text", "label"),
    [
        (
            f"HGCHAT_REPLY_BEGIN {OTHER_TOKEN}\nother answer\nHGCHAT_REPLY_END {OTHER_TOKEN}",
            "wrong-token",
        ),
        (f"HGCHAT_REPLY_BEGIN {TOKEN}\nnever finished", "incomplete"),
    ],
)
def test_bad_session_reply_while_working_times_out_without_resubmitting(
    reply_text: str, label: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While the target keeps reporting working, a wrong-token/incomplete
    session reply never settles the turn: the existing timeout fires and the
    payload is not replayed."""
    assert label
    token = TOKEN
    session_file = write_pi_session(tmp_path / "session.jsonl", reply_text)
    calls: list[list[str]] = []
    monkeypatch.setitem(namespace, "POLL_INTERVAL_S", 0)
    ticks = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(namespace["time"], "monotonic", lambda: next(ticks, 2.0))
    target = "pi-peer"

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["agent", "get"]:
            stdout = json.dumps(
                {"result": {"agent": pi_agent_info(session_file, "working", target)}}
            )
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    with pytest.raises(ChatError, match="timed out after 1000 ms"):
        client.turn(target, prompt_text(token), 1_000)

    kinds = [call[1:3] for call in calls]
    assert kinds.count(["agent", "prompt"]) == 1
    assert ["agent", "send-keys"] not in kinds


# ---------------------------------------------------------------------------
# (3) A replacement session identity must not let a new session's record be
# harvested as the old recipients' result. The exact-token binding is the only
# verifiable seam in the current source: a replacement session whose record is
# bound to a different token must never satisfy the current request. The wider
# same-token replacement case has no identity seam today; see PILOT-NOTES.md.
# ---------------------------------------------------------------------------


def test_replacement_session_with_other_token_is_not_harvested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-turn the agent_session path swaps to a replacement terminal's
    session file whose reply is bound to a DIFFERENT token: the changed
    recipient identity is rejected before any reader touches the replacement
    session, with no re-submission."""
    token = TOKEN
    old_session = write_pi_session(
        tmp_path / "old-session.jsonl",
        f"HGCHAT_REPLY_BEGIN {OTHER_TOKEN}\nstale\nHGCHAT_REPLY_END {OTHER_TOKEN}",
    )
    replacement_session = write_pi_session(
        tmp_path / "replacement-session.jsonl",
        f"HGCHAT_REPLY_BEGIN {OTHER_TOKEN}\nreplacement terminal answer\n"
        f"HGCHAT_REPLY_END {OTHER_TOKEN}",
    )
    calls: list[list[str]] = []
    monkeypatch.setitem(namespace, "POLL_INTERVAL_S", 0)
    bound_clock(monkeypatch)
    polls = 0
    target = "pi-peer"

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal polls
        calls.append(argv)
        if argv[1:3] == ["agent", "get"]:
            polls += 1
            status = "working" if polls == 1 else "done"
            session = replacement_session if polls > 1 else old_session
            info = pi_agent_info(session, status, target)
            stdout = json.dumps({"result": {"agent": info}})
        elif argv[1:3] == ["agent", "read"]:
            stdout = "replacement terminal shows an occupants record"
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    with pytest.raises(ChatError, match="recipient identity changed"):
        client.turn(target, prompt_text(token), REFUSAL_TIMEOUT_MS)

    kinds = [call[1:3] for call in calls]
    assert kinds.count(["agent", "prompt"]) == 1
    assert ["agent", "send-keys"] not in kinds
    assert ["agent", "read"] not in kinds
    assert not any("replacement terminal answer" in str(part) for call in calls for part in call)


def test_replacement_session_same_token_is_rejected_by_identity_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Desired and repaired behavior: the prompt wait result (parsed by the
    existing HerdrClient._prompt_result_agent) records the recipient's
    terminal and session identity; a later `agent get` naming a DIFFERENT
    terminal/session — even one holding a complete reply with the SAME
    request token — raises a ChatError for the changed recipient identity
    instead of harvesting that reply. On the pre-repair baseline turn()
    discarded the non-Codex prompt result and harvested the replacement.
    """
    token = TOKEN
    answer = "replacement answer"
    original_session = write_pi_session(
        tmp_path / "original-session.jsonl",
        f"HGCHAT_REPLY_BEGIN {OTHER_TOKEN}\noriginal\nHGCHAT_REPLY_END {OTHER_TOKEN}",
    )
    replacement_session = write_pi_session(
        tmp_path / "replacement-session.jsonl",
        f"HGCHAT_REPLY_BEGIN {token}\n{answer}\nHGCHAT_REPLY_END {token}",
    )
    submitted_agent = {
        "name": "pi-peer",
        "agent": "pi",
        "pane_id": "pane-1",
        "terminal_id": "term-1",
        "cwd": "/synthetic/project/room-a",
        "agent_session": {
            "agent": "pi",
            "source": "herdr:pi",
            "kind": "path",
            "value": str(original_session),
        },
    }
    calls: list[list[str]] = []
    monkeypatch.setitem(namespace, "POLL_INTERVAL_S", 0)
    bound_clock(monkeypatch)
    polls = 0
    target = "pi-peer"

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal polls
        calls.append(argv)
        if argv[1:3] == ["agent", "prompt"]:
            stdout = json.dumps({"result": {"agent": dict(submitted_agent)}})
        elif argv[1:3] == ["agent", "get"]:
            polls += 1
            status = "working" if polls == 1 else "done"
            replaced = {
                **submitted_agent,
                "pane_id": "pane-2",
                "terminal_id": "term-2",
                "agent_session": {
                    **submitted_agent["agent_session"],
                    "value": str(replacement_session),
                },
                "agent_status": status,
            }
            stdout = json.dumps({"result": {"agent": replaced}})
        elif argv[1:3] == ["agent", "read"]:
            stdout = "quiet terminal"
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    with pytest.raises(ChatError, match="recipient identity"):
        client.turn(target, prompt_text(token), REFUSAL_TIMEOUT_MS)

    kinds = [call[1:3] for call in calls]
    assert kinds.count(["agent", "prompt"]) == 1
    assert ["agent", "send-keys"] not in kinds
    assert not any(answer in str(part) for call in calls for part in call)


@pytest.mark.parametrize("changed", ["session", "cwd", "pane_id", "terminal_id"])
def test_changed_recipient_metadata_rejects_same_token_replacement(
    changed: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any changed identity field (session value, cwd, pane_id, terminal_id)
    between the bound record and a later poll record is rejected before any
    reader can use the replacement session, even when that session holds a
    complete reply with the SAME request token."""
    token = TOKEN
    answer = "replacement answer"
    original_session = write_pi_session(
        tmp_path / "original-session.jsonl",
        f"HGCHAT_REPLY_BEGIN {OTHER_TOKEN}\noriginal\nHGCHAT_REPLY_END {OTHER_TOKEN}",
    )
    replacement_session = write_pi_session(
        tmp_path / "replacement-session.jsonl",
        f"HGCHAT_REPLY_BEGIN {token}\n{answer}\nHGCHAT_REPLY_END {token}",
    )
    base = {
        "name": "pi-peer",
        "agent": "pi",
        "pane_id": "pane-1",
        "terminal_id": "term-1",
        "cwd": "/synthetic/project/room-a",
        "agent_session": {
            "agent": "pi",
            "source": "herdr:pi",
            "kind": "path",
            "value": str(original_session),
        },
    }
    calls: list[list[str]] = []
    monkeypatch.setitem(namespace, "POLL_INTERVAL_S", 0)
    bound_clock(monkeypatch)
    polls = 0
    target = "pi-peer"

    def agent_record(status: str) -> dict:
        record = dict(base)
        record["agent_session"] = dict(base["agent_session"])
        if changed == "session":
            record["agent_session"]["value"] = str(replacement_session)
        elif changed == "cwd":
            record["cwd"] = "/synthetic/project/room-b"
            record["agent_session"]["value"] = str(replacement_session)
        else:
            record[changed] = "term-2" if changed == "terminal_id" else "pane-2"
            record["agent_session"]["value"] = str(replacement_session)
        record["agent_status"] = status
        return record

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal polls
        calls.append(argv)
        if argv[1:3] == ["agent", "get"]:
            polls += 1
            if polls == 1:
                record = dict(base)
                record["agent_status"] = "working"
            else:
                record = agent_record("done")
            stdout = json.dumps({"result": {"agent": record}})
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    with pytest.raises(ChatError, match="recipient identity changed"):
        client.turn(target, prompt_text(token), REFUSAL_TIMEOUT_MS)

    kinds = [call[1:3] for call in calls]
    assert kinds.count(["agent", "prompt"]) == 1
    assert ["agent", "send-keys"] not in kinds
    assert ["agent", "read"] not in kinds
    assert not any(answer in str(part) for call in calls for part in call)


def test_cancel_during_session_harvest_wins_over_matching_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation set while the working-status session reader would find a
    complete matching reply must win: no reply is returned and no keys go out."""
    token = TOKEN
    session_file = write_pi_session(
        tmp_path / "session.jsonl",
        f"HGCHAT_REPLY_BEGIN {token}\nanswer\nHGCHAT_REPLY_END {token}",
    )
    cancel_event = threading.Event()
    calls: list[list[str]] = []
    monkeypatch.setitem(namespace, "POLL_INTERVAL_S", 0)
    bound_clock(monkeypatch)
    target = "pi-peer"

    real_extractor = namespace["extract_pi_session_reply"]
    wrapper_ran = threading.Event()

    def cancelling_extractor(session_file: Path, token: str) -> str:
        """Delegate to the real extractor, then cancel after the matching
        reply is found, so only the post-harvest cancellation check in turn()
        can observe it."""
        reply = real_extractor(session_file, token)
        wrapper_ran.set()
        cancel_event.set()
        return reply

    monkeypatch.setitem(namespace, "extract_pi_session_reply", cancelling_extractor)

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["agent", "get"]:
            stdout = json.dumps(
                {"result": {"agent": pi_agent_info(session_file, "working", target)}}
            )
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    with pytest.raises(ChatError, match="review cancelled"):
        client.turn(target, prompt_text(token), REFUSAL_TIMEOUT_MS, cancel_event)

    assert wrapper_ran.is_set(), "the post-reader cancellation path must be exercised"
    kinds = [call[1:3] for call in calls]
    assert kinds.count(["agent", "prompt"]) == 1
    assert ["agent", "send-keys"] not in kinds
    assert ["agent", "read"] not in kinds


@pytest.mark.parametrize(
    "degradation",
    ["no-session", "no-harness", "foreign-harness", "malformed-session", "bad-source"],
)
def test_bound_identity_rejects_disappearing_or_foreign_metadata(
    degradation: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a valid prompt-result binding, a poll record whose metadata has
    disappeared, changed harness, or carries a malformed/foreign-source
    session descriptor is rejected before any local reader or terminal
    fallback runs; no replacement session is read and the prompt is never
    replayed."""
    token = TOKEN
    answer = "replacement answer"
    original_session = write_pi_session(
        tmp_path / "original-session.jsonl",
        f"HGCHAT_REPLY_BEGIN {OTHER_TOKEN}\noriginal\nHGCHAT_REPLY_END {OTHER_TOKEN}",
    )
    replacement_session = write_pi_session(
        tmp_path / "replacement-session.jsonl",
        f"HGCHAT_REPLY_BEGIN {token}\n{answer}\nHGCHAT_REPLY_END {token}",
    )
    submitted_agent = {
        "name": "pi-peer",
        "agent": "pi",
        "pane_id": "pane-1",
        "terminal_id": "term-1",
        "cwd": "/synthetic/project/room-a",
        "agent_session": {
            "agent": "pi",
            "source": "herdr:pi",
            "kind": "path",
            "value": str(original_session),
        },
    }

    def degraded_record(status: str) -> dict:
        record = {
            **submitted_agent,
            "agent_status": status,
            "agent_session": {
                **submitted_agent["agent_session"],
                "value": str(replacement_session),
            },
        }
        if degradation == "no-session":
            record.pop("agent_session")
        elif degradation == "no-harness":
            record.pop("agent")
        elif degradation == "foreign-harness":
            record["agent"] = "claude"
            record["agent_session"] = {"source": "herdr:claude", "value": "0" * 16}
        elif degradation == "malformed-session":
            record["agent_session"] = {"source": "herdr:pi", "value": 12345}
        elif degradation == "bad-source":
            record["agent_session"] = {
                "source": "herdr:codex",
                "kind": "path",
                "value": str(replacement_session),
            }
        return record

    calls: list[list[str]] = []
    monkeypatch.setitem(namespace, "POLL_INTERVAL_S", 0)
    bound_clock(monkeypatch)
    target = "pi-peer"

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["agent", "prompt"]:
            stdout = json.dumps({"result": {"agent": dict(submitted_agent)}})
        elif argv[1:3] == ["agent", "get"]:
            stdout = json.dumps({"result": {"agent": degraded_record("done")}})
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    with pytest.raises(ChatError, match="recipient identity changed"):
        client.turn(target, prompt_text(token), REFUSAL_TIMEOUT_MS)

    kinds = [call[1:3] for call in calls]
    assert kinds.count(["agent", "prompt"]) == 1
    assert ["agent", "send-keys"] not in kinds
    assert ["agent", "read"] not in kinds
    assert not any(answer in str(part) for call in calls for part in call)


def test_pane_id_target_binding_allows_renamed_agent_on_same_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the supplied target is an exact pane ID, a poll record with a
    different agent name is still the bound recipient as long as pane_id
    matches that target."""
    token = TOKEN
    session_file = write_pi_session(
        tmp_path / "session.jsonl",
        f"HGCHAT_REPLY_BEGIN {token}\nanswer\nHGCHAT_REPLY_END {token}",
    )
    submitted_agent = {
        "name": "pi-peer",
        "agent": "pi",
        "pane_id": "pane-42",
        "cwd": "/synthetic/project/room-a",
        "agent_session": {
            "agent": "pi",
            "source": "herdr:pi",
            "kind": "path",
            "value": str(session_file),
        },
    }
    calls: list[list[str]] = []
    monkeypatch.setitem(namespace, "POLL_INTERVAL_S", 0)
    bound_clock(monkeypatch)
    target = "pane-42"

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["agent", "prompt"]:
            stdout = json.dumps({"result": {"agent": dict(submitted_agent)}})
        elif argv[1:3] == ["agent", "get"]:
            record = {**submitted_agent, "name": "pi-peer-renamed", "agent_status": "working"}
            stdout = json.dumps({"result": {"agent": record}})
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    assert client.turn(target, prompt_text(token), REFUSAL_TIMEOUT_MS) == ("working", "answer")

    kinds = [call[1:3] for call in calls]
    assert kinds.count(["agent", "prompt"]) == 1
    assert ["agent", "send-keys"] not in kinds


SUPPORTED_KINDS = ["pi", "claude", "codex", "grok"]


def kind_agent_record(kind: str, target: str, session_value: str, status: str) -> dict:
    return {
        "name": target,
        "agent": kind,
        "agent_status": status,
        "cwd": "/synthetic/project/room-a",
        "agent_session": {
            "source": f"herdr:{kind}",
            "value": session_value,
        },
    }


@pytest.mark.parametrize("kind", SUPPORTED_KINDS)
@pytest.mark.parametrize("change", ["changed", "missing"])
def test_bound_identity_rejects_replacement_for_every_supported_kind(
    kind: str, change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once a valid binding exists (from the accepted prompt-result record,
    including the Codex --wait accepted result), a later poll record whose
    session is replaced — or whose identity metadata disappears entirely — is
    rejected for every supported native kind before any reader can follow the
    replacement."""
    token = TOKEN
    target = f"{kind}-peer"
    submitted = kind_agent_record(kind, target, f"/synthetic/sessions/{kind}-orig", "working")
    if change == "changed":
        poll_record = kind_agent_record(
            kind, target, f"/synthetic/sessions/{kind}-replacement", "done"
        )
    else:
        poll_record = {key: value for key, value in submitted.items() if key != "agent_session"}
        poll_record["agent_status"] = "done"
    calls: list[list[str]] = []
    monkeypatch.setitem(namespace, "POLL_INTERVAL_S", 0)
    bound_clock(monkeypatch)

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["agent", "prompt"]:
            stdout = json.dumps({"result": {"agent": dict(submitted)}})
        elif argv[1:3] == ["agent", "get"]:
            stdout = json.dumps({"result": {"agent": dict(poll_record)}})
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    if kind == "codex":
        client._agent_kinds[target] = "codex"
    with pytest.raises(ChatError, match="recipient identity changed"):
        client.turn(target, prompt_text(token), REFUSAL_TIMEOUT_MS)

    kinds = [call[1:3] for call in calls]
    prompt_index = kinds.index(["agent", "prompt"])
    assert kinds.count(["agent", "prompt"]) == 1
    assert ["agent", "send-keys"] not in kinds
    # The pre-prompt Codex trust-dialog probe is the only permitted read; no
    # terminal read may follow the prompt.
    assert ["agent", "read"] not in kinds[prompt_index + 1 :]


def test_grok_settled_fallback_uses_supplied_record_without_second_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Grok terminal-viewport fallback uses the already identity-validated
    poll record instead of a second agent get, and hands the extractor exactly
    the synthetic session directory derived from that record."""
    token = TOKEN
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setitem(namespace, "POLL_INTERVAL_S", 0)
    bound_clock(monkeypatch)
    calls: list[list[str]] = []
    polls = 0
    target = "grok-peer"
    record = kind_agent_record("grok", target, "sess-1", "done")

    received: list[Path] = []

    def fake_grok_extractor(session_dir: Path, reply_token: str) -> str:
        received.append(session_dir)
        assert reply_token == token
        return "grok answer"

    monkeypatch.setitem(namespace, "extract_grok_session_reply", fake_grok_extractor)

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal polls
        calls.append(argv)
        if argv[1:3] == ["agent", "get"]:
            polls += 1
            record["agent_status"] = "working" if polls == 1 else "done"
            stdout = json.dumps({"result": {"agent": dict(record)}})
        elif argv[1:3] == ["agent", "read"]:
            stdout = "reply hidden above the grok viewport"
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    assert client.turn(target, prompt_text(token), REFUSAL_TIMEOUT_MS) == ("done", "grok answer")

    kinds = [call[1:3] for call in calls]
    assert kinds.count(["agent", "prompt"]) == 1
    assert kinds.count(["agent", "get"]) == 2, "no second get may follow the validated record"
    assert ["agent", "send-keys"] not in kinds
    expected = (
        tmp_path / ".grok" / "sessions" / quote("/synthetic/project/room-a", safe="") / "sess-1"
    )
    assert received == [expected]


def test_pi_session_kind_must_be_an_owned_path_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The only other verifiable identity seam: a session record that is not a
    kind="path" record pointing at an owned regular file is never read."""
    token = TOKEN
    directory = tmp_path / "sessions-root"
    directory.mkdir()
    info = {
        "name": "pi-peer",
        "agent": "pi",
        "agent_status": "done",
        "cwd": "/synthetic/project/room-a",
        "agent_session": {
            "agent": "pi",
            "source": "herdr:pi",
            "kind": "id",
            "value": str(directory),
        },
    }
    calls: list[list[str]] = []
    monkeypatch.setitem(namespace, "POLL_INTERVAL_S", 0)
    bound_clock(monkeypatch)
    polls = 0

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal polls
        calls.append(argv)
        if argv[1:3] == ["agent", "get"]:
            polls += 1
            # First poll working so saw_working is true and the existing
            # unmarked-stability refusal can fire deterministically once the
            # invalid session record is ignored.
            info["agent_status"] = "working" if polls == 1 else "done"
            stdout = json.dumps({"result": {"agent": dict(info)}})
        elif argv[1:3] == ["agent", "read"]:
            stdout = "quiet terminal"
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    with pytest.raises(ChatError, match="reply markers HGCHAT_REPLY_"):
        client.turn("pi-peer", prompt_text(token), REFUSAL_TIMEOUT_MS)

    kinds = [call[1:3] for call in calls]
    assert kinds.count(["agent", "prompt"]) == 1
    assert ["agent", "send-keys"] not in kinds


# ---------------------------------------------------------------------------
# (4) Cancellation and failed collection never send terminal keys or replay
# the payload. Covered on the baseline by the existing excerpts:
#   - test_review_cancel_stops_the_local_prompt_process_without_sending_keys
#   - test_review_cancel_never_sends_keys_for_working_or_idle_status
#   - test_herdr_turn_timeout_never_sends_keys_to_observed_working_target
#   - test_prompt_failure_preserves_error_and_never_interrupts
# Re-adding duplicates adds no value; the one gap is a cancel arriving while a
# session-JSONL collection itself is the failure mode, so that variant is
# asserted here via the wrong-token refusal path above plus this explicit
# cancel-during-session-poll check.
# ---------------------------------------------------------------------------


def test_cancel_during_session_poll_sends_no_keys_and_no_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = TOKEN
    session_file = write_pi_session(
        tmp_path / "session.jsonl",
        f"HGCHAT_REPLY_BEGIN {OTHER_TOKEN}\nwrong\nHGCHAT_REPLY_END {OTHER_TOKEN}",
    )
    cancel_event = threading.Event()
    calls: list[list[str]] = []
    monkeypatch.setitem(namespace, "POLL_INTERVAL_S", 0)
    target = "pi-peer"

    def runner(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["agent", "get"]:
            cancel_event.set()
            stdout = json.dumps({"result": {"agent": pi_agent_info(session_file, "done", target)}})
        elif argv[1:3] == ["agent", "read"]:
            stdout = "quiet terminal"
        else:
            stdout = "{}"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    client = HerdrClient(herdr_bin="herdr-test", runner=runner)
    with pytest.raises(ChatError, match="review cancelled"):
        client.turn(target, prompt_text(token), REFUSAL_TIMEOUT_MS, cancel_event)

    kinds = [call[1:3] for call in calls]
    assert kinds.count(["agent", "prompt"]) == 1
    assert ["agent", "send-keys"] not in kinds
