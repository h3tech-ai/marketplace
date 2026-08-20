"""Layer 1 — #116 acceptance lifecycle tests.

Pins the new `awaiting_acceptance` / `cancelled` states, the four PO
rejection paths, the opt-in `sprint.review.per_story_acceptance` toggle,
the tracker FSM extension, and the rejection_feedback persistence.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Make hooks/lib and tracker package importable.
_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_HOOKS_LIB = _PLUGIN_ROOT / "hooks" / "lib"
_SCRIPTS_DIR = _PLUGIN_ROOT / "skills" / "_shared" / "scripts"
for p in (_HOOKS_LIB, _SCRIPTS_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import story_pipeline as sp  # noqa: E402
import scrum_state_machine as sm  # noqa: E402
from tracker.transitions import (  # noqa: E402
    TRACKER_STAGES,
    VALID_TRANSITIONS as TR_VALID,
    validate_transition,
    TrackerTransitionError,
    normalize_status,
)


# ---------------------------------------------------------------------------
# State machine — VALID_TRANSITIONS pins (#116)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_reviewing_can_route_through_awaiting_acceptance():
    assert "awaiting_acceptance" in sp.VALID_TRANSITIONS["reviewing"]
    # Back-compat: reviewing → done still legal (toggle off).
    assert "done" in sp.VALID_TRANSITIONS["reviewing"]


@pytest.mark.unit
def test_awaiting_acceptance_fork_targets():
    targets = sp.VALID_TRANSITIONS["awaiting_acceptance"]
    # PO accept → done; rejects → in_progress / queued / cancelled.
    assert {"done", "in_progress", "queued", "cancelled"} <= set(targets)


@pytest.mark.unit
def test_terminal_states_are_terminal():
    assert sp.VALID_TRANSITIONS["done"] == []
    assert sp.VALID_TRANSITIONS["cancelled"] == []


# ---------------------------------------------------------------------------
# create_story + helper functions (#116)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_create_story_initialises_rejection_feedback():
    story = sp.create_story("US-1", title="Sample")
    assert story["rejection_feedback"] == []


@pytest.mark.unit
def test_request_acceptance_moves_reviewing_to_awaiting():
    state = {"current_stories": []}
    state["current_stories"].append(sp.create_story("US-1", "Sample"))
    # Walk to reviewing first.
    state = sp.transition_story(state, "US-1", "in_progress")
    state = sp.transition_story(state, "US-1", "testing")
    state = sp.transition_story(state, "US-1", "reviewing")
    state = sp.request_acceptance(state, "US-1")
    story = sp._find_story(state, "US-1")
    assert story["state"] == "awaiting_acceptance"


@pytest.mark.unit
def test_accept_story_records_accepted_by_and_moves_to_done():
    state = {"current_stories": [sp.create_story("US-2", "Sample")]}
    for s in ("in_progress", "testing", "reviewing", "awaiting_acceptance"):
        state = sp.transition_story(state, "US-2", s)
    state = sp.accept_story(state, "US-2", accepted_by="po@example.com")
    story = sp._find_story(state, "US-2")
    assert story["state"] == "done"
    assert story["accepted_by"] == "po@example.com"
    assert "accepted_at" in story


@pytest.mark.unit
@pytest.mark.parametrize("reason,target_state", [
    ("needs-fix", "in_progress"),
    ("redo", "queued"),
    ("defer", "queued"),
    ("cancel", "cancelled"),
])
def test_reject_story_routes_per_reason(reason, target_state):
    state = {"current_stories": [sp.create_story("US-R", "Sample")]}
    for s in ("in_progress", "testing", "reviewing", "awaiting_acceptance"):
        state = sp.transition_story(state, "US-R", s)
    state = sp.reject_story(
        state, "US-R", reason, feedback_text="needs work", rejected_by="po@x",
    )
    story = sp._find_story(state, "US-R")
    assert story["state"] == target_state
    # Rejection record persisted.
    fb = story["rejection_feedback"]
    assert len(fb) == 1
    assert fb[0]["reason_class"] == reason
    assert fb[0]["feedback_text"] == "needs work"
    assert fb[0]["rejected_by"] == "po@x"
    if reason == "defer":
        assert story.get("carry_over") is True


@pytest.mark.unit
def test_reject_story_invalid_reason_raises():
    state = {"current_stories": [sp.create_story("US-X", "Sample")]}
    for s in ("in_progress", "testing", "reviewing", "awaiting_acceptance"):
        state = sp.transition_story(state, "US-X", s)
    with pytest.raises(ValueError):
        sp.reject_story(
            state, "US-X", "unknown-reason", feedback_text="x", rejected_by="po",
        )


@pytest.mark.unit
def test_reject_only_legal_from_awaiting_acceptance():
    state = {"current_stories": [sp.create_story("US-Y", "Sample")]}
    state = sp.transition_story(state, "US-Y", "in_progress")
    with pytest.raises(ValueError):
        sp.reject_story(
            state, "US-Y", "needs-fix", feedback_text="x", rejected_by="po",
        )


@pytest.mark.unit
def test_rejection_feedback_accumulates_across_iterations():
    """A story can be rejected, fixed, re-reviewed, and rejected again —
    each rejection is appended so the next SE dispatch sees the history."""
    state = {"current_stories": [sp.create_story("US-Z", "Sample")]}
    for s in ("in_progress", "testing", "reviewing", "awaiting_acceptance"):
        state = sp.transition_story(state, "US-Z", s)
    state = sp.reject_story(state, "US-Z", "needs-fix",
                            feedback_text="round 1", rejected_by="po")
    # Story is back in_progress; walk again.
    for s in ("testing", "reviewing", "awaiting_acceptance"):
        state = sp.transition_story(state, "US-Z", s)
    state = sp.reject_story(state, "US-Z", "needs-fix",
                            feedback_text="round 2", rejected_by="po")
    fb = sp._find_story(state, "US-Z")["rejection_feedback"]
    assert [r["feedback_text"] for r in fb] == ["round 1", "round 2"]


@pytest.mark.unit
def test_cancelled_excluded_from_sprint_aggregation():
    """#116: cancelled stories must not skew velocity / DoD aggregates."""
    state = {"current_stories": []}
    s1 = sp.create_story("US-A", "Accept")
    s1["state"] = "done"
    s1["dod"] = {"passed": True, "critical_passed": True, "checks": {}}
    s2 = sp.create_story("US-B", "Cancel")
    s2["state"] = "cancelled"
    s2["dod"] = {"passed": False, "critical_passed": False, "checks": {}}
    state["current_stories"] = [s1, s2]
    agg = sp.aggregate_sprint_dod(state)
    # cancelled story is filtered before counting.
    assert agg["total_stories"] == 1
    assert agg["stories_passed"] == 1


# ---------------------------------------------------------------------------
# is_per_story_acceptance_enabled — config reader (#116)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_toggle_defaults_off_when_no_config(tmp_path: Path):
    assert sp.is_per_story_acceptance_enabled(str(tmp_path)) is False


@pytest.mark.unit
@pytest.mark.parametrize("value,expected", [
    ("true", True),
    ("True", True),
    ("yes", True),
    ("on", True),
    ("1", True),
    ("false", False),
    ("no", False),
])
def test_toggle_reads_truthy_values(tmp_path: Path, value, expected):
    (tmp_path / ".synaptory.yaml").write_text(
        f"sprint:\n  review:\n    per_story_acceptance: {value}\n"
    )
    assert sp.is_per_story_acceptance_enabled(str(tmp_path)) is expected


@pytest.mark.unit
def test_toggle_off_when_key_missing(tmp_path: Path):
    (tmp_path / ".synaptory.yaml").write_text(
        "sprint:\n  review:\n    other_key: 42\n"
    )
    assert sp.is_per_story_acceptance_enabled(str(tmp_path)) is False


# ---------------------------------------------------------------------------
# scrum_state_machine.transition_story — toggle-aware redirect (#116)
# ---------------------------------------------------------------------------


@pytest.fixture
def scrum_project(tmp_path: Path) -> Path:
    """A Scrum project initialised via the state machine CLI."""
    p = tmp_path / "proj"
    p.mkdir()
    sm.initialize(str(p))
    sm.add_story(str(p), "US-1", title="Toggle test")
    return p


@pytest.mark.unit
def test_toggle_off_preserves_legacy_reviewing_to_done(scrum_project: Path):
    """Back-compat: with the toggle off, `reviewing → done` stays
    direct — today's behavior must not regress."""
    sm.transition_story(str(scrum_project), "US-1", "in_progress")
    sm.transition_story(str(scrum_project), "US-1", "testing")
    sm.transition_story(str(scrum_project), "US-1", "reviewing")
    sm.transition_story(str(scrum_project), "US-1", "done")
    state = sm.read_state(str(scrum_project))
    story = next(s for s in state["current_stories"] if s["id"] == "US-1")
    assert story["state"] == "done"


@pytest.mark.unit
def test_toggle_on_redirects_reviewing_to_done_through_awaiting(scrum_project: Path):
    """With `per_story_acceptance: true`, the orchestrator's
    `reviewing → done` is intercepted and routed to awaiting_acceptance."""
    (scrum_project / ".synaptory.yaml").write_text(
        "sprint:\n  review:\n    per_story_acceptance: true\n"
    )
    sm.transition_story(str(scrum_project), "US-1", "in_progress")
    sm.transition_story(str(scrum_project), "US-1", "testing")
    sm.transition_story(str(scrum_project), "US-1", "reviewing")
    sm.transition_story(str(scrum_project), "US-1", "done")  # redirected
    state = sm.read_state(str(scrum_project))
    story = next(s for s in state["current_stories"] if s["id"] == "US-1")
    assert story["state"] == "awaiting_acceptance"


@pytest.mark.unit
def test_accept_story_via_scrum_wrapper(scrum_project: Path):
    (scrum_project / ".synaptory.yaml").write_text(
        "sprint:\n  review:\n    per_story_acceptance: true\n"
    )
    for s in ("in_progress", "testing", "reviewing", "done"):
        sm.transition_story(str(scrum_project), "US-1", s)
    sm.accept_story(str(scrum_project), "US-1", accepted_by="po@h3t.co")
    state = sm.read_state(str(scrum_project))
    story = next(s for s in state["current_stories"] if s["id"] == "US-1")
    assert story["state"] == "done"
    assert story["accepted_by"] == "po@h3t.co"


@pytest.mark.unit
def test_reject_story_via_scrum_wrapper(scrum_project: Path):
    (scrum_project / ".synaptory.yaml").write_text(
        "sprint:\n  review:\n    per_story_acceptance: true\n"
    )
    for s in ("in_progress", "testing", "reviewing", "done"):
        sm.transition_story(str(scrum_project), "US-1", s)
    sm.reject_story(
        str(scrum_project), "US-1",
        reason_class="needs-fix",
        feedback_text="missing input validation",
        rejected_by="po@h3t.co",
    )
    state = sm.read_state(str(scrum_project))
    story = next(s for s in state["current_stories"] if s["id"] == "US-1")
    assert story["state"] == "in_progress"
    assert story["rejection_feedback"][0]["reason_class"] == "needs-fix"


# ---------------------------------------------------------------------------
# Tracker FSM extension (#116)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tracker_fsm_has_awaiting_acceptance():
    assert "AWAITING_ACCEPTANCE" in TRACKER_STAGES
    assert "AWAITING_ACCEPTANCE" in TR_VALID


@pytest.mark.unit
def test_in_review_to_awaiting_acceptance_is_legal():
    validate_transition("US-1", "IN_REVIEW", "AWAITING_ACCEPTANCE")


@pytest.mark.unit
def test_awaiting_acceptance_to_done_is_legal():
    validate_transition("US-1", "AWAITING_ACCEPTANCE", "DONE")


@pytest.mark.unit
def test_awaiting_acceptance_to_to_do_is_legal_redo_path():
    """The `redo` reject reason routes IN_PROGRESS… but the tracker side
    needs to be able to walk AWAITING_ACCEPTANCE → TO_DO directly when
    the PO rejects all the way back."""
    validate_transition("US-1", "AWAITING_ACCEPTANCE", "TO_DO")


@pytest.mark.unit
def test_in_review_can_still_go_directly_to_done_back_compat():
    """Toggle-off projects still walk IN_REVIEW → DONE direct."""
    validate_transition("US-1", "IN_REVIEW", "DONE")


@pytest.mark.unit
@pytest.mark.parametrize("alias", [
    "Awaiting Acceptance",
    "awaiting_acceptance",
    "awaitingacceptance",
    "Pending Acceptance",
    "PO Review",
])
def test_awaiting_acceptance_aliases_normalise(alias):
    assert normalize_status(alias) == "AWAITING_ACCEPTANCE"


# ---------------------------------------------------------------------------
# CLI subcommands — accept_story / reject_story (#116)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_accept_story_succeeds(scrum_project: Path):
    (scrum_project / ".synaptory.yaml").write_text(
        "sprint:\n  review:\n    per_story_acceptance: true\n"
    )
    for s in ("in_progress", "testing", "reviewing", "done"):
        sm.transition_story(str(scrum_project), "US-1", s)
    r = subprocess.run(
        [sys.executable, str(_HOOKS_LIB / "scrum_state_machine.py"),
         "accept_story", str(scrum_project), "US-1",
         "--accepted-by", "po@h3t.co"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    state = sm.read_state(str(scrum_project))
    assert state["current_stories"][0]["state"] == "done"


@pytest.mark.unit
def test_cli_reject_story_succeeds(scrum_project: Path):
    (scrum_project / ".synaptory.yaml").write_text(
        "sprint:\n  review:\n    per_story_acceptance: true\n"
    )
    for s in ("in_progress", "testing", "reviewing", "done"):
        sm.transition_story(str(scrum_project), "US-1", s)
    r = subprocess.run(
        [sys.executable, str(_HOOKS_LIB / "scrum_state_machine.py"),
         "reject_story", str(scrum_project), "US-1",
         "--reason", "redo",
         "--feedback", "wrong approach",
         "--rejected-by", "po@h3t.co",
         "--ac-change", "AC-01 must support webhooks"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    state = sm.read_state(str(scrum_project))
    story = state["current_stories"][0]
    assert story["state"] == "queued"
    assert story["rejection_feedback"][0]["reason_class"] == "redo"
    assert story["rejection_feedback"][0]["acceptance_criteria_change"] == \
        ["AC-01 must support webhooks"]


@pytest.mark.unit
def test_cli_reject_requires_all_three_flags(scrum_project: Path):
    """Missing --reason / --feedback / --rejected-by exits non-zero."""
    (scrum_project / ".synaptory.yaml").write_text(
        "sprint:\n  review:\n    per_story_acceptance: true\n"
    )
    for s in ("in_progress", "testing", "reviewing", "done"):
        sm.transition_story(str(scrum_project), "US-1", s)
    r = subprocess.run(
        [sys.executable, str(_HOOKS_LIB / "scrum_state_machine.py"),
         "reject_story", str(scrum_project), "US-1",
         "--reason", "needs-fix"],
        capture_output=True, text=True,
    )
    assert r.returncode != 0
