"""Layer-1 tests for transcript_usage.py.

Hypothesis: when a subagent ships a receipt with no `token_usage`, the hook
can recover the counts by walking the parent transcript bottom-up for the
last `message.usage` block and patching them in.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PLUGIN_ROOT / "hooks" / "lib" / "transcript_usage.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("transcript_usage", MODULE_PATH)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    sys.modules["transcript_usage"] = m
    spec.loader.exec_module(m)
    return m


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


def test_extract_last_usage_picks_last_assistant_message(tmp_path, mod):
    transcript = tmp_path / "t.jsonl"
    _write_jsonl(transcript, [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "message": {"role": "assistant", "usage": {
            "input_tokens": 10, "output_tokens": 5,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }}},
        {"type": "assistant", "message": {"role": "assistant", "usage": {
            "input_tokens": 100, "output_tokens": 50,
            "cache_read_input_tokens": 200, "cache_creation_input_tokens": 30,
        }}},
    ])
    out = mod.extract_last_usage(transcript)
    assert out == {"input": 100, "output": 50, "cache_read": 200, "cache_write": 30}


def test_extract_last_usage_returns_none_for_empty(tmp_path, mod):
    transcript = tmp_path / "empty.jsonl"
    transcript.write_text("", encoding="utf-8")
    assert mod.extract_last_usage(transcript) is None


def test_extract_last_usage_returns_none_for_missing_file(tmp_path, mod):
    assert mod.extract_last_usage(tmp_path / "nope.jsonl") is None


def test_extract_last_usage_skips_zero_usage_entries(tmp_path, mod):
    """An assistant message with all-zero usage isn't useful — keep walking."""
    transcript = tmp_path / "t.jsonl"
    _write_jsonl(transcript, [
        {"type": "assistant", "message": {"usage": {
            "input_tokens": 7, "output_tokens": 3,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }}},
        {"type": "assistant", "message": {"usage": {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }}},
    ])
    out = mod.extract_last_usage(transcript)
    assert out == {"input": 7, "output": 3, "cache_read": 0, "cache_write": 0}


def test_extract_last_usage_tolerates_malformed_lines(tmp_path, mod):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        "this is not json\n"
        + json.dumps({"message": {"usage": {
            "input_tokens": 1, "output_tokens": 2,
            "cache_read_input_tokens": 3, "cache_creation_input_tokens": 4,
        }}}) + "\n"
        + "{not closed\n",
        encoding="utf-8",
    )
    out = mod.extract_last_usage(transcript)
    assert out == {"input": 1, "output": 2, "cache_read": 3, "cache_write": 4}


def test_patch_receipt_fills_missing_token_usage(tmp_path, mod):
    receipt = tmp_path / "US-1-se.json"
    receipt.write_text(json.dumps({
        "task": "implement", "agent": "software-engineer",
        "backend": "claude", "model": "claude-sonnet-4-5",
        "artifacts": [], "verification_commands": [],
    }), encoding="utf-8")
    transcript = tmp_path / "t.jsonl"
    _write_jsonl(transcript, [
        {"message": {"usage": {
            "input_tokens": 11, "output_tokens": 22,
            "cache_read_input_tokens": 33, "cache_creation_input_tokens": 44,
        }}},
    ])
    assert mod.patch_receipt(receipt, transcript, default_stage="se-implementation") is True
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["token_usage"] == {
        "input": 11, "output": 22, "cache_read": 33, "cache_write": 44,
        "stage": "se-implementation",
    }


def test_patch_receipt_preserves_existing_token_usage(tmp_path, mod):
    """If the agent already populated token_usage with non-zero counts,
    don't overwrite — its self-report is more accurate than the parent's
    aggregate, which may include other concurrent work."""
    receipt = tmp_path / "r.json"
    original = {
        "task": "x", "agent": "se", "backend": "claude", "model": "m",
        "artifacts": [], "verification_commands": [],
        "token_usage": {"input": 1, "output": 2, "cache_read": 0,
                        "cache_write": 0, "stage": "se-implementation"},
    }
    receipt.write_text(json.dumps(original), encoding="utf-8")
    transcript = tmp_path / "t.jsonl"
    _write_jsonl(transcript, [{"message": {"usage": {
        "input_tokens": 999, "output_tokens": 888,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }}}])
    assert mod.patch_receipt(receipt, transcript) is False
    assert json.loads(receipt.read_text(encoding="utf-8")) == original


def test_patch_receipt_overwrites_all_zero_token_usage(tmp_path, mod):
    """Empty token_usage (all zeros, the symptom the bug report describes)
    is the case we *do* want to backfill."""
    receipt = tmp_path / "r.json"
    receipt.write_text(json.dumps({
        "task": "x", "agent": "se", "backend": "claude", "model": "m",
        "artifacts": [], "verification_commands": [],
        "token_usage": {"input": 0, "output": 0, "cache_read": 0,
                        "cache_write": 0, "stage": "se-implementation"},
    }), encoding="utf-8")
    transcript = tmp_path / "t.jsonl"
    _write_jsonl(transcript, [{"message": {"usage": {
        "input_tokens": 5, "output_tokens": 6,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }}}])
    assert mod.patch_receipt(receipt, transcript) is True
    tu = json.loads(receipt.read_text(encoding="utf-8"))["token_usage"]
    assert tu["input"] == 5 and tu["output"] == 6
    assert tu["stage"] == "se-implementation"


def test_patch_receipt_silent_on_unparseable_transcript(tmp_path, mod):
    receipt = tmp_path / "r.json"
    receipt.write_text(json.dumps({"task": "x"}), encoding="utf-8")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("garbage\n{still garbage\n", encoding="utf-8")
    assert mod.patch_receipt(receipt, transcript) is False
    assert "token_usage" not in json.loads(receipt.read_text(encoding="utf-8"))
