"""Layer 1 unit tests — `plugin-claude/hooks/lib/synaptory_logger.py`.

Covers the public API:
  - `chain_id(project_dir)` — mints, persists, and re-reads a UUID chain ID.
  - `increment_depth` / `decrement_depth` — active-depth counter round-trip.
  - `emit(event, project_dir, **payload)` — appends a JSONL record to
    .synaptory/.orchestrator/events.jsonl without raising on any error.

All tests are pure-Python (no subprocess, no network) — Layer 1 contract.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

# synaptory_logger is importable via conftest's sys.path injection
import synaptory_logger


# ── chain_id ──────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_chain_id_minted_and_persisted(tmp_path: Path):
    """chain_id mints a new UUID on first call and writes it to disk."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    # Ensure the env var doesn't override the file-based path in this test.
    env_backup = os.environ.pop(synaptory_logger.CHAIN_ID_ENV, None)
    try:
        cid = synaptory_logger.chain_id(project_dir)
        assert cid and len(cid) == 32, f"expected 32-char hex UUID, got {cid!r}"
        # Should be persisted so the next call returns the same value.
        assert synaptory_logger.chain_id(project_dir) == cid
    finally:
        if env_backup is not None:
            os.environ[synaptory_logger.CHAIN_ID_ENV] = env_backup


@pytest.mark.unit
def test_chain_id_env_overrides_file(tmp_path: Path, monkeypatch):
    """SYNAPTORY_CHAIN_ID env var takes precedence over the persisted file."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.setenv(synaptory_logger.CHAIN_ID_ENV, "override-chain-id")
    assert synaptory_logger.chain_id(project_dir) == "override-chain-id"


# ── increment_depth / decrement_depth ─────────────────────────────────────────


@pytest.mark.unit
def test_increment_decrement_depth_round_trip(tmp_path: Path, monkeypatch):
    """increment_depth and decrement_depth track the counter correctly."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))
    # Remove SYNAPTORY_AGENT_DEPTH so depth reads from file, not env.
    monkeypatch.delenv(synaptory_logger.AGENT_DEPTH_ENV, raising=False)

    d1 = synaptory_logger.increment_depth(project_dir)
    assert d1 == 1, f"expected depth 1 after first increment, got {d1}"

    d2 = synaptory_logger.increment_depth(project_dir)
    assert d2 == 2

    d3 = synaptory_logger.decrement_depth(project_dir)
    assert d3 == 1, f"expected depth 1 after decrement, got {d3}"

    d4 = synaptory_logger.decrement_depth(project_dir)
    assert d4 == 0

    # Decrement below zero must clamp at 0.
    d5 = synaptory_logger.decrement_depth(project_dir)
    assert d5 == 0, f"depth must not go below 0; got {d5}"


# ── emit ──────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_emit_writes_valid_jsonl_record(tmp_path: Path, monkeypatch):
    """emit() appends a valid JSONL line with ts, chain_id, depth, event."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))
    monkeypatch.delenv(synaptory_logger.CHAIN_ID_ENV, raising=False)
    monkeypatch.delenv(synaptory_logger.AGENT_DEPTH_ENV, raising=False)

    synaptory_logger.emit("test_event", project_dir=project_dir, story_id="US-001", from_state="testing")

    sink = project_dir / ".synaptory" / ".orchestrator" / "events.jsonl"
    assert sink.exists(), f"expected events.jsonl to be created at {sink}"

    lines = sink.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, f"expected 1 JSONL line, got {len(lines)}"

    record = json.loads(lines[0])
    assert record["event"] == "test_event"
    assert record["story_id"] == "US-001"
    assert record["from_state"] == "testing"
    assert "ts" in record
    assert "chain_id" in record
    assert "depth" in record


@pytest.mark.unit
def test_emit_is_silent_when_project_dir_unwritable(tmp_path: Path, monkeypatch):
    """emit() must not raise when the project dir is not writable."""
    # Point at a non-existent read-only path — emit should silently swallow
    # the OSError and not propagate it.
    bad_dir = tmp_path / "nonexistent_subdir" / "deeper"
    # We don't create the dir — emit should handle the mkdir failure silently.
    # Suppress real HOME so emit doesn't accidentally write to our real project.
    monkeypatch.setenv("HOME", str(tmp_path))
    try:
        synaptory_logger.emit("silent_event", project_dir=bad_dir)
    except Exception as exc:  # pragma: no cover
        pytest.fail(f"emit() raised {exc!r} but must never raise")
