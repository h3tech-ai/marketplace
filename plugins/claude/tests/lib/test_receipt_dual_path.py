"""Wave 1: kernel accepts a receipt in either the spec-scoped or flat dir."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import story_pipeline as sp


pytestmark = pytest.mark.unit


def _write(path: Path, story_id: str = "API-1", role: str = "se") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "story_id": story_id,
                "role": "software-engineer",
                "completed_at": "2099-01-01T00:00:00Z",
            }
        )
    )


def test_fresh_receipt_sees_flat_file_when_spec_dir_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SYNAPTORY_ACTIVE_SPEC", "api")
    orch = tmp_path / ".synaptory" / ".orchestrator"
    spec_dir = orch / "specs" / "api" / "receipts"
    spec_dir.mkdir(parents=True)
    flat = orch / "receipts"
    _write(flat / "API-1-se.json")

    receipts_dir = sp._resolve_receipts_dir(str(tmp_path))
    assert receipts_dir.endswith("specs/api/receipts")
    story = {"id": "API-1", "pipeline_log": []}
    assert sp._fresh_receipt(receipts_dir, "API-1", "se", story, "in_progress")
    found = sp.collect_story_receipts(receipts_dir, "API-1")
    assert found
    assert sp.find_story_receipt_file(receipts_dir, "API-1", "se")


def test_fresh_receipt_same_utc_second_second_precision(tmp_path: Path):
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    (receipts / "API-1-se.json").write_text(
        json.dumps(
            {
                "story_id": "API-1",
                "role": "software-engineer",
                "completed_at": "2026-08-24T16:52:34Z",
            }
        )
    )
    story = {
        "id": "API-1",
        "pipeline_log": [{
            "state": "in_progress",
            "entered_at": "2026-08-24T16:52:34.647295Z",
        }],
    }
    assert sp._fresh_receipt(
        str(receipts), "API-1", "se", story, "in_progress"
    )


def test_fresh_receipt_prefers_spec_scoped_when_both_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SYNAPTORY_ACTIVE_SPEC", "api")
    orch = tmp_path / ".synaptory" / ".orchestrator"
    spec = orch / "specs" / "api" / "receipts" / "API-1-se.json"
    flat = orch / "receipts" / "API-1-se.json"
    _write(spec)
    _write(flat)
    receipts_dir = sp._resolve_receipts_dir(str(tmp_path))
    path = sp.find_story_receipt_file(receipts_dir, "API-1", "se")
    assert path and path.replace("\\", "/").endswith("specs/api/receipts/API-1-se.json")
