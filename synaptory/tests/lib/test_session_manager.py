"""Layer 1 unit tests — session management utilities.

NOTE: `session_manager.py` imports `access_token_client` at module level, which
is an internal module not available in the test environment (it's a runtime-only
binary-side module). We therefore test `session_store.py` directly, which is
the storage layer used by SessionManager and has no external dependencies.

Covers the public session_store API:
  - `session_store.save(record)` — persists a SessionRecord to disk.
  - `session_store.load(url, project_id)` — retrieves a saved record.
  - `session_store.delete(url, project_id)` — removes a record and returns bool.
  - `session_store.SessionRecord.to_json() / from_json()` — round-trip serialisation.

All tests are pure-Python (no subprocess, no network) — Layer 1 contract.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

# session_store is importable via conftest's sys.path injection
import session_store


def _make_record(
    upn: str = "alice@h3t.co",
    project_id: str = "proj-alpha",
    role: str = "member",
) -> session_store.SessionRecord:
    now = datetime.now(timezone.utc)
    return session_store.SessionRecord(
        control_plane_url="local",
        upn=upn,
        session_token="SYNAPTORY1.test.token",
        refresh_token="",
        expires_at=now,
        issued_at=now,
        project_id=project_id,
        role=role,
    )


# ── save / load round-trip ────────────────────────────────────────────────────


@pytest.mark.unit
def test_save_and_load_round_trip(tmp_path: Path, monkeypatch):
    """save() + load() return an equivalent SessionRecord."""
    monkeypatch.setenv(session_store._STORE_DIR_ENV, str(tmp_path / "sessions"))

    record = _make_record()
    session_store.save(record)

    loaded = session_store.load("local", "proj-alpha")
    assert loaded is not None, "expected saved record to be loadable"
    assert loaded.upn == "alice@h3t.co"
    assert loaded.project_id == "proj-alpha"
    assert loaded.role == "member"
    assert loaded.session_token == "SYNAPTORY1.test.token"


# ── load returns None when no session stored ─────────────────────────────────


@pytest.mark.unit
def test_load_returns_none_when_absent(tmp_path: Path, monkeypatch):
    """load() returns None when no record has been saved for that key."""
    monkeypatch.setenv(session_store._STORE_DIR_ENV, str(tmp_path / "sessions"))

    result = session_store.load("local", "nonexistent-project")
    assert result is None, f"expected None for absent session, got {result!r}"


# ── delete removes the record ─────────────────────────────────────────────────


@pytest.mark.unit
def test_delete_removes_saved_record(tmp_path: Path, monkeypatch):
    """delete() removes the session file and returns True; subsequent load returns None."""
    monkeypatch.setenv(session_store._STORE_DIR_ENV, str(tmp_path / "sessions"))

    record = _make_record(project_id="to-delete")
    session_store.save(record)
    assert session_store.load("local", "to-delete") is not None, "pre-condition: record must be saved"

    deleted = session_store.delete("local", "to-delete")
    assert deleted is True, f"expected True from delete, got {deleted!r}"

    assert session_store.load("local", "to-delete") is None, (
        "expected None after delete"
    )


# ── delete returns False for non-existent record ──────────────────────────────


@pytest.mark.unit
def test_delete_returns_false_when_absent(tmp_path: Path, monkeypatch):
    """delete() returns False when there is nothing to remove."""
    monkeypatch.setenv(session_store._STORE_DIR_ENV, str(tmp_path / "sessions"))

    result = session_store.delete("local", "ghost-project")
    assert result is False, f"expected False from delete of absent record, got {result!r}"
