"""Regression assay: pi_session_proves must verify a long-running Pi session
whose file exceeds the 64 KiB read cap.

A peer that did substantial work grows its session JSONL past the cap; the
capped read then cuts the final line mid-JSON. That truncation is an artefact
of our own bounded read, not a corrupt file, so it must not fail the proof
when the identity events sit at the top. A file read in full whose real last
line is malformed still fails closed.
"""

from __future__ import annotations

import json
from pathlib import Path

import test_new_room as suite

module = suite.module


def _identity_prefix() -> str:
    return suite.pi_session_lines("openai-codex", "gpt-6-astra", "high")


def _filler_line(index: int) -> str:
    return json.dumps(
        {
            "type": "message",
            "id": f"u{index}",
            "role": "assistant",
            "content": "x" * 400,
        }
    )


def _oversized_session(read_limit: int, *, valid_tail: bool) -> str:
    """Identity events, then filler until the file passes read_limit.

    With valid_tail False the file is cut so the last line is a partial JSON
    fragment beyond the cap, reproducing the observed failure.
    """
    body = _identity_prefix()
    index = 0
    while len(body.encode("utf-8")) <= read_limit + 4096:
        body += _filler_line(index) + "\n"
        index += 1
    if not valid_tail:
        # Chop the trailing newline and part of the last complete line so the
        # file ends mid-object, past the cap.
        body = body.rstrip("\n")[:-40]
    return body


def test_proof_accepts_a_session_larger_than_the_read_cap(tmp_path: Path) -> None:
    limit = module.PI_SESSION_READ_LIMIT
    content = _oversized_session(limit, valid_tail=False)
    assert len(content.encode("utf-8")) > limit
    session = suite.write_pi_session(tmp_path, "astra-peer", content)
    # The capped read cuts the last line; the identity events above still prove.
    assert module.pi_session_proves(str(session), "openai-codex", "gpt-6-astra", "high")


def test_proof_over_cap_still_rejects_a_wrong_identity(tmp_path: Path) -> None:
    content = _oversized_session(module.PI_SESSION_READ_LIMIT, valid_tail=True)
    session = suite.write_pi_session(tmp_path, "astra-peer", content)
    assert module.pi_session_proves(str(session), "openai-codex", "gpt-6-astra", "high")
    assert not module.pi_session_proves(str(session), "bigmodel-coding", "glm-5.3", "high")


def test_small_file_with_a_real_malformed_last_line_still_fails_closed(
    tmp_path: Path,
) -> None:
    # Read in full (well under the cap): a genuinely malformed final line is a
    # corrupt file, not a cap artefact, and must fail closed as before.
    content = _identity_prefix() + '{"type": "thinking_level_'
    assert len(content.encode("utf-8")) < module.PI_SESSION_READ_LIMIT
    session = suite.write_pi_session(tmp_path, "astra-peer", content)
    assert not module.pi_session_proves(str(session), "openai-codex", "gpt-6-astra", "high")


def test_over_cap_file_needs_a_valid_identity_before_the_cap(tmp_path: Path) -> None:
    # If the identity events are pushed beyond the cap by leading filler, the
    # proof still fails closed: only the first PI_SESSION_READ_LIMIT is read.
    limit = module.PI_SESSION_READ_LIMIT
    leading = "".join(_filler_line(i) + "\n" for i in range(400))
    content = leading + _identity_prefix()
    assert len(leading.encode("utf-8")) > limit
    session = suite.write_pi_session(tmp_path, "astra-peer", content)
    assert not module.pi_session_proves(str(session), "openai-codex", "gpt-6-astra", "high")
