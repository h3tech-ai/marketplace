"""Layer 1 — `receipt_recovery.py` transcript fallback.

The SubagentStop verify hook walks `.synaptory/.orchestrator/receipts/`
to detect "missing receipt". If a subagent emitted valid receipt JSON
in its response text without calling Write, the receipt would be lost.
This module recovers it from the transcript.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hooks.lib.receipt_recovery import (
    find_receipt_in_transcript,
    recover_from_transcript,
)


def _make_transcript(tmp_path: Path, *assistant_texts: str) -> Path:
    """Write a JSONL transcript with the supplied assistant messages."""
    lines = []
    for text in assistant_texts:
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": text}],
                    },
                }
            )
        )
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


_VALID_RECEIPT = {
    "story_id": "HT-350",
    "role": "software-engineer",
    "backend": "claude",
    "model": "claude-sonnet-4-6",
    "artifacts": ["src/feature.ts"],
    "verification_commands": ["test -s src/feature.ts"],
    "metrics": {"loc": 42},
    "completed_at": "2026-05-12T08:30:00Z",
}


@pytest.mark.unit
def test_find_in_fenced_block(tmp_path):
    text = f"Done. Here's the receipt:\n\n```json\n{json.dumps(_VALID_RECEIPT)}\n```\n"
    p = _make_transcript(tmp_path, text)
    got = find_receipt_in_transcript(p)
    assert got is not None
    assert got["story_id"] == "HT-350"
    assert got["role"] == "software-engineer"


@pytest.mark.unit
def test_find_in_unfenced_trailing_json(tmp_path):
    text = "Receipt follows:\n\n" + json.dumps(_VALID_RECEIPT, indent=2)
    p = _make_transcript(tmp_path, text)
    got = find_receipt_in_transcript(p)
    assert got is not None
    assert got["artifacts"] == ["src/feature.ts"]


@pytest.mark.unit
def test_returns_most_recent_when_multiple_candidates(tmp_path):
    older = {**_VALID_RECEIPT, "story_id": "HT-100"}
    newer = {**_VALID_RECEIPT, "story_id": "HT-350"}
    # newer message comes second (later) in the JSONL — must be returned
    p = _make_transcript(
        tmp_path,
        f"```json\n{json.dumps(older)}\n```",
        f"```json\n{json.dumps(newer)}\n```",
    )
    got = find_receipt_in_transcript(p)
    assert got["story_id"] == "HT-350"


@pytest.mark.unit
def test_ignores_non_receipt_json(tmp_path):
    # A code-fence with unrelated JSON should not be picked up.
    text = '```json\n{"foo": "bar"}\n```'
    p = _make_transcript(tmp_path, text)
    assert find_receipt_in_transcript(p) is None


@pytest.mark.unit
def test_invalid_json_returns_none(tmp_path):
    text = "```json\n{not valid}\n```"
    p = _make_transcript(tmp_path, text)
    assert find_receipt_in_transcript(p) is None


@pytest.mark.unit
def test_missing_transcript_returns_none(tmp_path):
    assert find_receipt_in_transcript(tmp_path / "nope.jsonl") is None


@pytest.mark.unit
def test_recover_writes_canonical_filename(tmp_path):
    text = f"```json\n{json.dumps(_VALID_RECEIPT)}\n```"
    p = _make_transcript(tmp_path, text)
    receipts_dir = tmp_path / "receipts"
    target = recover_from_transcript(p, receipts_dir)
    assert target is not None
    assert target.name == "HT-350-se.json"
    written = json.loads(target.read_text())
    assert written["story_id"] == "HT-350"
    assert written["role"] == "software-engineer"


@pytest.mark.unit
def test_recover_uses_hint_when_story_id_missing(tmp_path):
    receipt_no_story = {k: v for k, v in _VALID_RECEIPT.items() if k != "story_id"}
    text = f"```json\n{json.dumps(receipt_no_story)}\n```"
    p = _make_transcript(tmp_path, text)
    target = recover_from_transcript(p, tmp_path / "receipts", story_id_hint="HT-999")
    assert target is not None
    assert target.name == "HT-999-se.json"


@pytest.mark.unit
def test_recover_refuses_without_story_id_or_hint(tmp_path):
    receipt_no_story = {k: v for k, v in _VALID_RECEIPT.items() if k != "story_id"}
    text = f"```json\n{json.dumps(receipt_no_story)}\n```"
    p = _make_transcript(tmp_path, text)
    assert recover_from_transcript(p, tmp_path / "receipts") is None


@pytest.mark.unit
def test_recover_handles_legacy_product_manager_role(tmp_path):
    receipt = {**_VALID_RECEIPT, "role": "product-manager"}
    text = f"```json\n{json.dumps(receipt)}\n```"
    p = _make_transcript(tmp_path, text)
    target = recover_from_transcript(p, tmp_path / "receipts")
    assert target is not None
    # Legacy alias maps to the canonical `po` abbrev.
    assert target.name == "HT-350-po.json"


@pytest.mark.unit
def test_recover_returns_none_when_nothing_to_recover(tmp_path):
    p = _make_transcript(tmp_path, "Nothing structured here.")
    assert recover_from_transcript(p, tmp_path / "receipts") is None
