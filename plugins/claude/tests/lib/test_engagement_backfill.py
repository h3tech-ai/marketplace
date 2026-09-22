"""#800 — a pre-1.3.3 project reaches a committed engagement.json without
re-approving its baseline.

#771 moved engagement facts to the committed file, and the READ side is
properly backward compatible. But the write side has two callers,
`approve_baseline` and `record_handover`, and both are once-per-engagement
governance acts — so a project that approved its baseline before 1.3.3 had no
reachable verb that would ever write the committed file, and kept the exact
symptom #771 fixed.

The only route on offer was to re-run `approve_baseline`, which records a new
date and a new approver for a decision the owner already made. A tooling
upgrade must not require re-performing a governance act.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks" / "lib"))

import spq_paths as _paths  # noqa: E402
import spq_state_machine as sm  # noqa: E402
import state_store as _store  # noqa: E402

pytestmark = pytest.mark.unit

LEGACY = {
    "baseline_approved": True,
    "baseline_approved_by": "owner@h3t.co",
    "baseline_approved_at": "2026-09-17T04:11:00Z",
    "baseline_ref": "baseline-1",
}


def _legacy_project(tmp_path):
    """A project whose engagement facts live only on the gitignored pointer."""
    project = tmp_path / "legacy"
    project.mkdir()
    (project / ".synaptory.yaml").write_text('build_mode: "spq"\n', encoding="utf-8")
    sm.initialize(str(project))
    pointer = sm.read_pointer(str(project))
    pointer["engagement"] = dict(LEGACY)
    _store.write_json_atomic(sm._pointer_path(str(project)), pointer)
    committed = Path(_paths.engagement_path(str(project)))
    if committed.exists():
        committed.unlink()
    return project


def test_reading_promotes_the_pointer_facts_into_the_committed_file(tmp_path):
    project = _legacy_project(tmp_path)
    assert not Path(_paths.engagement_path(str(project))).exists()

    sm.read_engagement(str(project))

    body = _store.read_json(_paths.engagement_path(str(project)))
    assert body["engagement"]["baseline_approved"] is True


def test_the_original_approval_is_carried_across_unchanged(tmp_path):
    """It is a migration, not an approval: it must invent no date and no
    principal, or the record has been falsified to satisfy the tool."""
    project = _legacy_project(tmp_path)
    sm.read_engagement(str(project))

    promoted = _store.read_json(
        _paths.engagement_path(str(project))
    )["engagement"]
    assert promoted["baseline_approved_by"] == LEGACY["baseline_approved_by"]
    assert promoted["baseline_approved_at"] == LEGACY["baseline_approved_at"]
    assert promoted["baseline_ref"] == LEGACY["baseline_ref"]


def test_the_promotion_is_idempotent(tmp_path):
    project = _legacy_project(tmp_path)
    first = sm.read_engagement(str(project))
    second = sm.read_engagement(str(project))
    assert first == second == dict(LEGACY)


def test_a_project_with_no_engagement_facts_writes_nothing(tmp_path):
    project = tmp_path / "fresh"
    project.mkdir()
    (project / ".synaptory.yaml").write_text('build_mode: "spq"\n', encoding="utf-8")
    sm.initialize(str(project))

    assert sm.read_engagement(str(project)) == {}
    body = _store.read_json(_paths.engagement_path(str(project)))
    assert not (body or {}).get("engagement")


def test_the_committed_file_still_wins_over_the_pointer(tmp_path):
    """The shim must stay a shim: a committed file is never overwritten from
    a stale pointer."""
    project = _legacy_project(tmp_path)
    sm._write_engagement(str(project), {"baseline_approved_by": "current@h3t.co"})

    assert sm.read_engagement(str(project))["baseline_approved_by"] == "current@h3t.co"


def test_a_read_that_cannot_write_still_answers(tmp_path, monkeypatch):
    """Answering is this function's job; migrating is a side effect that must
    never fail it."""
    project = _legacy_project(tmp_path)
    monkeypatch.setattr(
        sm, "_write_engagement",
        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")),
    )
    assert sm.read_engagement(str(project)) == dict(LEGACY)
