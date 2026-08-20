"""Layer 1 — tracker transition guard tests (#111).

Pins the FSM table, normalization, rejection / bypass semantics, and the
local_adapter integration (the simplest adapter to set up — it just edits
a JSON file in `.synaptory/`).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# Make the tracker package importable.
_SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2]
    / "skills" / "_shared" / "scripts"
)
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from tracker.transitions import (  # noqa: E402
    TRACKER_STAGES,
    VALID_TRANSITIONS,
    TrackerTransitionError,
    normalize_status,
    validate_transition,
)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("raw,expected", [
    ("BACKLOG", "BACKLOG"),
    ("TO_DO", "TO_DO"),
    ("to do", "TO_DO"),
    ("To Do", "TO_DO"),
    ("TODO", "TO_DO"),
    ("Open", "TO_DO"),
    ("IN_PROGRESS", "IN_PROGRESS"),
    ("In Progress", "IN_PROGRESS"),
    ("IN_REVIEW", "IN_REVIEW"),
    ("In Review", "IN_REVIEW"),
    ("Review", "IN_REVIEW"),
    ("DONE", "DONE"),
    ("Done", "DONE"),
    ("Completed", "DONE"),
    ("Closed", "DONE"),
    ("Resolved", "DONE"),
    ("BLOCKED", "BLOCKED"),
    ("Blocked", "BLOCKED"),
    # Unknown values pass through uppercased (with no alias coercion).
    ("CUSTOM_STAGE", "CUSTOM_STAGE"),
])
def test_normalize_status(raw, expected):
    assert normalize_status(raw) == expected


@pytest.mark.unit
def test_normalize_empty_defaults_to_backlog():
    """Empty / None inputs map to BACKLOG (the default tracker starting stage)."""
    assert normalize_status("") == "BACKLOG"
    assert normalize_status(None) == "BACKLOG"


# ---------------------------------------------------------------------------
# Audit-gate rejection
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_to_do_to_done_rejected():
    """#111's headline case — TO_DO → DONE must not erase the QE+CR gate."""
    with pytest.raises(TrackerTransitionError) as excinfo:
        validate_transition("US-1", "TO_DO", "DONE")
    err = excinfo.value
    assert err.story_id == "US-1"
    assert err.current == "TO_DO"
    assert err.target == "DONE"
    assert "IN_PROGRESS" in err.valid


@pytest.mark.unit
def test_in_progress_to_done_rejected():
    """Must go through IN_REVIEW before DONE."""
    with pytest.raises(TrackerTransitionError):
        validate_transition("US-1", "IN_PROGRESS", "DONE")


@pytest.mark.unit
def test_backlog_to_in_review_rejected():
    with pytest.raises(TrackerTransitionError):
        validate_transition("US-1", "BACKLOG", "IN_REVIEW")


# ---------------------------------------------------------------------------
# Audit-gate happy path
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("current,target", [
    ("BACKLOG", "TO_DO"),
    ("BACKLOG", "IN_PROGRESS"),
    ("TO_DO", "IN_PROGRESS"),
    ("TO_DO", "BACKLOG"),
    ("IN_PROGRESS", "IN_REVIEW"),
    ("IN_PROGRESS", "TO_DO"),
    ("IN_REVIEW", "DONE"),
    ("IN_REVIEW", "IN_PROGRESS"),
    ("DONE", "IN_PROGRESS"),
])
def test_legal_transitions_pass(current, target):
    validate_transition("US-1", current, target)  # no raise


@pytest.mark.unit
def test_idempotent_set_is_allowed():
    """Setting the same stage twice is a no-op, not a violation."""
    validate_transition("US-1", "DONE", "DONE")
    validate_transition("US-1", "TO_DO", "TO_DO")


@pytest.mark.unit
def test_blocked_label_always_allowed():
    """BLOCKED is a label, not a stage. Both directions must pass."""
    validate_transition("US-1", "TO_DO", "BLOCKED")
    validate_transition("US-1", "DONE", "BLOCKED")
    validate_transition("US-1", "BLOCKED", "IN_PROGRESS")
    validate_transition("US-1", "BLOCKED", "DONE")


@pytest.mark.unit
def test_native_vocabulary_normalized_before_check():
    """Jira / Teamwork native values should reach the FSM through normalize."""
    # "In Review" → IN_REVIEW; "Done" → DONE; transition is legal.
    validate_transition("US-1", "In Review", "Done")
    # "To Do" → TO_DO; "Done" → DONE; illegal.
    with pytest.raises(TrackerTransitionError):
        validate_transition("US-1", "To Do", "Done")


# ---------------------------------------------------------------------------
# allow_skip escape hatch
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_allow_skip_bypasses_validation():
    """`allow_skip=True` is the documented bypass — must not raise even on
    the most egregious skip (BACKLOG → DONE)."""
    validate_transition("US-1", "BACKLOG", "DONE", allow_skip=True)
    validate_transition("US-1", "TO_DO", "DONE", allow_skip=True)


# ---------------------------------------------------------------------------
# FSM shape pins
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_fsm_table_is_complete():
    """Every non-terminal stage in TRACKER_STAGES must have entries in
    VALID_TRANSITIONS; otherwise stories can get stranded."""
    for stage in TRACKER_STAGES:
        assert stage in VALID_TRANSITIONS, f"missing FSM entry for {stage}"


# ---------------------------------------------------------------------------
# Integration — local_adapter end-to-end
# ---------------------------------------------------------------------------

@pytest.fixture
def local_project(tmp_path: Path) -> Path:
    """Bootstrap a project with a single TO_DO story in local-adapter format."""
    project = tmp_path / "proj"
    (project / ".synaptory" / ".orchestrator").mkdir(parents=True)
    (project / ".synaptory" / ".orchestrator" / "tracker-data.json").write_text(
        json.dumps({
            "epics": [], "sprints": [],
            "stories": [{
                "id": "US-1", "title": "Test", "feature": "", "epic": "",
                "priority": "", "status": "TO_DO", "size": "", "sprint": "",
                "ac_count": 0, "acceptance_criteria": [], "raw_text": "",
            }],
        }),
        encoding="utf-8",
    )
    (project / ".synaptory.yaml").write_text(
        "tracker:\n  backend: local\n", encoding="utf-8",
    )
    return project


@pytest.mark.unit
def test_local_adapter_rejects_skip(local_project: Path):
    from tracker import get_adapter
    adapter = get_adapter(local_project)
    with pytest.raises(TrackerTransitionError):
        adapter.update_story_status("US-1", "DONE")


@pytest.mark.unit
def test_local_adapter_honors_allow_skip(local_project: Path):
    from tracker import get_adapter
    adapter = get_adapter(local_project)
    story = adapter.update_story_status("US-1", "DONE", allow_skip=True)
    assert story.status == "DONE"


@pytest.mark.unit
def test_local_adapter_legal_walk(local_project: Path):
    from tracker import get_adapter
    adapter = get_adapter(local_project)
    for stage in ("IN_PROGRESS", "IN_REVIEW", "DONE"):
        adapter.update_story_status("US-1", stage)
    assert adapter.get_story("US-1").status == "DONE"


# ---------------------------------------------------------------------------
# tracker_cli.py --allow-skip flag
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_cli_rejects_skip_by_default(local_project: Path):
    """Exit non-zero on illegal transition; no `--allow-skip`."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPTS_DIR / "tracker" / "tracker_cli.py"),
         "--project-dir", str(local_project), "update-status", "US-1", "DONE"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "Illegal tracker transition" in (result.stderr + result.stdout)


@pytest.mark.unit
def test_cli_allow_skip_flag(local_project: Path):
    """`--allow-skip` lets the same illegal transition through."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPTS_DIR / "tracker" / "tracker_cli.py"),
         "--project-dir", str(local_project), "update-status", "US-1", "DONE",
         "--allow-skip"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    payload = json.loads(result.stdout)
    assert payload["status"] == "DONE"
