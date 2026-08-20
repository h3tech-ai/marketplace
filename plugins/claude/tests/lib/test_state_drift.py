"""Layer 1 — `state_drift` validator (epic #75 P4).

Pins what counts as pipeline-state drift: unknown story sub-states, dangling
v3 active_spec, unknown build_mode, non-object shapes, and corrupt/empty
files — while a clean v2/v3 state and a missing file (fresh project) are NOT
drift. The FileChanged hook turns a non-empty problem list into a warning.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import state_drift
from state_drift import validate_pipeline_state, validate_config_text, check_drift


def _v2(stories: list[dict], **kw) -> dict:
    return {"version": "2.0", "build_mode": "scrum",
            "lifecycle_state": "SPRINT_EXECUTION", "current_stories": stories, **kw}


# ── validate_pipeline_state (pure) ───────────────────────────────────────────


@pytest.mark.unit
def test_clean_v2_state_has_no_problems():
    assert validate_pipeline_state(_v2([{"id": "US-1", "state": "testing"}])) == []


@pytest.mark.unit
def test_unknown_substate_is_drift():
    problems = validate_pipeline_state(_v2([{"id": "US-1", "state": "WATFACE"}]))
    assert any("WATFACE" in p and "US-1" in p for p in problems)


@pytest.mark.unit
def test_every_valid_substate_accepted():
    stories = [{"id": f"S{i}", "state": s} for i, s in enumerate(state_drift.STORY_STATES)]
    assert validate_pipeline_state(_v2(stories)) == []


@pytest.mark.unit
def test_unknown_build_mode_is_drift():
    st = _v2([{"id": "US-1", "state": "testing"}])
    st["build_mode"] = "waterfall"
    assert any("build_mode" in p for p in validate_pipeline_state(st))


@pytest.mark.unit
def test_missing_lifecycle_state_is_drift():
    st = {"version": "2.0", "build_mode": "scrum", "current_stories": []}
    assert any("lifecycle_state" in p for p in validate_pipeline_state(st))


@pytest.mark.unit
def test_current_stories_not_a_list_is_drift():
    st = {"version": "2.0", "build_mode": "scrum",
          "lifecycle_state": "SPRINT_EXECUTION", "current_stories": {"oops": 1}}
    assert any("not a list" in p for p in validate_pipeline_state(st))


@pytest.mark.unit
def test_non_object_state_is_drift():
    assert validate_pipeline_state(["not", "a", "dict"]) == ["pipeline-state.json is not a JSON object"]


# ── v3 multi-spec ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_clean_v3_multispec_has_no_problems():
    st = {
        "version": "3.0", "build_mode": "scrum", "active_spec": "be",
        "specs": {
            "be": {"lifecycle_state": "SPRINT_EXECUTION",
                   "current_stories": [{"id": "BE-1", "state": "queued"}]},
        },
    }
    assert validate_pipeline_state(st) == []


@pytest.mark.unit
def test_dangling_active_spec_is_drift():
    st = {"version": "3.0", "build_mode": "scrum", "active_spec": "frontend",
          "specs": {"be": {"lifecycle_state": "SPRINT_EXECUTION", "current_stories": []}}}
    assert any("active_spec" in p and "frontend" in p for p in validate_pipeline_state(st))


@pytest.mark.unit
def test_bad_substate_in_a_spec_is_drift():
    st = {"version": "3.0", "build_mode": "scrum", "active_spec": "be",
          "specs": {"be": {"lifecycle_state": "SPRINT_EXECUTION",
                           "current_stories": [{"id": "BE-1", "state": "nope"}]}}}
    assert any("spec be" in p and "nope" in p for p in validate_pipeline_state(st))


# ── .synaptory.yaml config drift (PR #89 re-review) ──────────────────────────


@pytest.mark.unit
def test_config_unknown_build_mode_is_drift():
    problems = validate_config_text("project_id: t\nbuild_mode: waterfall\n")
    assert any("build_mode" in p and "waterfall" in p for p in problems)


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["scrum", "kanban"])
def test_config_known_build_mode_ok(mode: str):
    assert validate_config_text(f"project_id: t\nbuild_mode: {mode}\n") == []


@pytest.mark.unit
def test_config_absent_build_mode_ok():
    assert validate_config_text("project_id: t\n") == []


@pytest.mark.unit
def test_check_drift_catches_config_with_clean_state(tmp_path: Path):
    """Regression: the FileChanged hook watches .synaptory.yaml, so a bad
    build_mode there must surface even when pipeline-state.json is clean."""
    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "pipeline-state.json").write_text(json.dumps(_v2([{"id": "US-1", "state": "done"}])))
    (tmp_path / ".synaptory.yaml").write_text("project_id: t\nbuild_mode: waterfall\n")
    out = check_drift(tmp_path)
    assert out["ok"] is False
    assert any(".synaptory.yaml" in p for p in out["problems"])


# ── check_drift (file I/O) ───────────────────────────────────────────────────


@pytest.mark.unit
def test_missing_file_is_not_drift(tmp_path: Path):
    out = check_drift(tmp_path)
    assert out["ok"] is True and out["checked"] is False


@pytest.mark.unit
def test_clean_file_ok(tmp_path: Path):
    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "pipeline-state.json").write_text(json.dumps(_v2([{"id": "US-1", "state": "done"}])))
    out = check_drift(tmp_path)
    assert out["ok"] is True and out["checked"] is True and out["problems"] == []


@pytest.mark.unit
def test_corrupt_json_is_drift(tmp_path: Path):
    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "pipeline-state.json").write_text("{ truncated")
    out = check_drift(tmp_path)
    assert out["ok"] is False and out["checked"] is True
    assert any("not valid JSON" in p for p in out["problems"])


@pytest.mark.unit
def test_empty_file_is_drift(tmp_path: Path):
    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "pipeline-state.json").write_text("   \n")
    out = check_drift(tmp_path)
    assert out["ok"] is False
    assert any("empty" in p for p in out["problems"])
