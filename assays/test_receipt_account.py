"""Acceptance assays: a Pi participant's route receipt names its account
surface, proven by `pi auth check`, so a peer bound by a disclosure rule can
read the receipt instead of touring the constitution before its first
protected read.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_herdr_group_chat import ChatError

relay = sys.modules["herdr_group_chat"].__dict__
load_profile_receipt = relay["load_profile_receipt"]
profile_receipt_body = relay["profile_receipt_body"]


def _new_room():
    import test_new_room

    return test_new_room.module


# --- relay side ---------------------------------------------------------------

BASE_ENTRY = {
    "role": "astra",
    "target": "astra-peer",
    "harness": "pi",
    "model": "gpt-6-astra",
    "effort": "high",
    "provider": "openai-codex",
    "verification": "native-ui verified",
    "evidence": "pi-session",
}
FABLE_ENTRY = {
    "role": "fable",
    "target": "fable-peer",
    "harness": "claude",
    "model": "fable",
    "effort": "high",
    "verification": "native-ui verified",
    "evidence": "process-argv",
}
AGENTS = {"astra": "astra-peer", "fable": "fable-peer"}


def _env(entries: list[dict]) -> dict[str, str]:
    payload = {"profile": "astra-fable", "verified": entries}
    return {relay["PROFILE_RECEIPT_ENV"]: json.dumps(payload)}


def test_receipt_accepts_an_account_field_and_renders_it_after_provider() -> None:
    entry = {**BASE_ENTRY, "account": "pi auth check ready"}
    payload = load_profile_receipt("astra-fable", AGENTS, _env([entry, FABLE_ENTRY]))
    body = profile_receipt_body(payload)
    assert "provider openai-codex · account pi auth check ready · target astra-peer" in body
    assert body.count("account ") == 1  # fable carries no account field


def test_receipt_without_an_account_field_still_loads_and_renders_as_before() -> None:
    payload = load_profile_receipt("astra-fable", AGENTS, _env([BASE_ENTRY, FABLE_ENTRY]))
    assert "account" not in profile_receipt_body(payload)


@pytest.mark.parametrize("bad", ["", 7, None])
def test_receipt_rejects_an_empty_or_non_string_account(bad: object) -> None:
    entry = {**BASE_ENTRY, "account": bad}
    with pytest.raises(ChatError, match="invalid"):
        load_profile_receipt("astra-fable", AGENTS, _env([entry, FABLE_ENTRY]))


# --- launcher side ------------------------------------------------------------


def test_native_auth_ready_requires_exit_zero_and_a_ready_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _new_room()
    seen: list[list[str]] = []

    def run(command, **kwargs):
        seen.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="checking...\nready\n", stderr="")

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.native_auth_ready("openai-codex") is True
    assert seen == [["pi", "auth", "check", "--provider", "openai-codex"]]

    def not_ready(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="not ready\n", stderr="")

    monkeypatch.setattr(module.subprocess, "run", not_ready)
    assert module.native_auth_ready("openai-codex") is False

    def bad_exit(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="ready\n", stderr="")

    monkeypatch.setattr(module.subprocess, "run", bad_exit)
    assert module.native_auth_ready("openai-codex") is False

    def boom(command, **kwargs):
        raise OSError("no pi")

    monkeypatch.setattr(module.subprocess, "run", boom)
    assert module.native_auth_ready("openai-codex") is False


def test_auth_proof_only_binds_pi_participants_with_a_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _new_room()
    monkeypatch.setattr(module, "native_auth_ready", lambda provider: False)
    assert module.auth_proof(module.FABLE_PARTICIPANT) is True
    assert module.auth_proof(module.ASTRA_PARTICIPANT) is False
    monkeypatch.setattr(module, "native_auth_ready", lambda provider: provider == "openai-codex")
    assert module.auth_proof(module.ASTRA_PARTICIPANT) is True


def test_receipt_payload_names_the_account_for_pi_participants_only() -> None:
    module = _new_room()
    payload = module.profile_receipt_payload(
        "astra-fable", [module.ASTRA_PARTICIPANT, module.FABLE_PARTICIPANT]
    )
    by_role = {entry["role"]: entry for entry in payload["verified"]}
    assert by_role["astra"]["account"] == "pi auth check ready"
    assert "account" not in by_role["fable"]


def test_failed_auth_preflight_creates_and_starts_no_astra_tab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirror of the catalog preflight assay: no auth, no tab, no start."""
    import test_new_room as suite

    module = suite.module
    calls: list[list[str]] = []
    suite.install_profile_host(
        monkeypatch,
        calls,
        {"w-agents:p-astra": suite.ASTRA_SCREEN, "w-agents:p-fable": suite.FABLE_SCREEN},
        tmp_path=tmp_path,
        auth_ready={"openai-codex": False},
    )
    state: dict = {"schema_version": 1}

    failures = module.start_participants(
        "herdr",
        str(tmp_path),
        "w-agents",
        tmp_path,
        suite.launcher_state_path(tmp_path),
        state,
        participants=module.resolve_profile("astra-fable"),
    )

    assert any("auth preflight" in failure and "@astra" in failure for failure in failures)
    astra_calls = [call for call in calls if "astra" in " ".join(call)]
    assert not any(call[:2] in (["tab", "create"], ["agent", "start"]) for call in astra_calls)
    assert ["pi", "auth", "check", "--provider", "openai-codex"] in calls
    # Fable still started: the preflight is per-role.
    assert any(call[:2] == ["agent", "start"] and call[2] == "fable-peer" for call in calls)
