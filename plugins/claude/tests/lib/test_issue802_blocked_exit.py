"""Layer 1 — #802: a CR-rejected unit must be able to reach QE again.

The defect had two halves that were one defect. `advance` refused every exit
from `blocked`, so the recovery ladder was not the SUPPORTED way out but the
ONLY way out; and the ladder restored a CR-rejected unit to `reviewing`, which
has no edge to `testing`. Between them, a unit rejected at review could never
be re-verified for the rest of its life. The measured consequence was a unit
that was repaired, re-reviewed to a PASS, and still failed its DoD on
`tests_pass` with no route to the evidence that would clear it.

The kernel half is covered in `test_advance_kernel.py`. This file covers the
pipeline half: where a recovering unit resumes, and what the board recommends
when the ladder is spent.
"""

from __future__ import annotations

import copy
import datetime
import json
import os

import pytest

import story_pipeline as sp


def _iso(minute: int) -> str:
    return datetime.datetime(
        2026, 9, 1, 12, minute, 0, tzinfo=datetime.timezone.utc
    ).isoformat()


def _receipts(tmp_path, **role_minutes):
    d = str(tmp_path)
    for role, minute in role_minutes.items():
        path = os.path.join(d, sp.get_story_receipt_path("WU-1101", role))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {"task": "t", "agent": role, "completed_at": _iso(minute)}, f
            )
    return d


def _blocked_unit(blocked_from="reviewing"):
    return {
        "id": "WU-1101",
        "state": "blocked",
        "blocked_from": blocked_from,
        "blocked_reason": "2 critical findings",
        "pipeline_log": [
            {"state": "blocked", "entered_at": _iso(30), "exited_at": None}
        ],
    }


# ── where a recovering unit resumes ──────────────────────────────────────────


@pytest.mark.unit
def test_repaired_unit_resumes_at_testing_so_qe_can_see_it(tmp_path):
    """THE REGRESSION. SE's receipt post-dates QE's, so the code moved after
    the verifier last looked. Resuming at `reviewing` would review repaired
    code no QE receipt describes -- the exact shape that failed `tests_pass`
    with no route to fix it."""
    d = _receipts(tmp_path, qe=10, se=40)
    assert sp.blocked_resume_state(_blocked_unit(), d) == "testing"


@pytest.mark.unit
def test_untouched_code_still_resumes_at_reviewing(tmp_path):
    """Conditional on purpose. A reject that needed no code change leaves SE's
    receipt older than QE's, and forcing that unit through a fresh QE pass
    would buy nothing."""
    d = _receipts(tmp_path, qe=40, se=10)
    assert sp.blocked_resume_state(_blocked_unit(), d) == "reviewing"


@pytest.mark.unit
@pytest.mark.parametrize("blocked_from", ["in_progress", "testing", "queued"])
def test_only_review_rejections_are_rerouted(tmp_path, blocked_from):
    d = _receipts(tmp_path, qe=10, se=40)
    assert sp.blocked_resume_state(_blocked_unit(blocked_from), d) == blocked_from


@pytest.mark.unit
def test_missing_evidence_does_not_guess(tmp_path):
    """A resume target is not the place to guess. With either receipt absent
    or unreadable the answer is `blocked_from`, exactly as before."""
    assert sp.blocked_resume_state(_blocked_unit(), None) == "reviewing"
    assert sp.blocked_resume_state(_blocked_unit(), str(tmp_path)) == "reviewing"
    d = _receipts(tmp_path, se=40)  # QE receipt never written
    assert sp.blocked_resume_state(_blocked_unit(), d) == "reviewing"


@pytest.mark.unit
def test_unreadable_receipt_does_not_guess(tmp_path):
    d = _receipts(tmp_path, se=40)
    with open(
        os.path.join(d, sp.get_story_receipt_path("WU-1101", "qe")), "w"
    ) as f:
        f.write("{not json")
    assert sp.blocked_resume_state(_blocked_unit(), d) == "reviewing"


# ── unblock_story honours the resume target ──────────────────────────────────


@pytest.mark.unit
def test_unblock_story_accepts_the_resume_target():
    state = {"current_stories": [_blocked_unit()]}
    out = sp.unblock_story(copy.deepcopy(state), "WU-1101", None, restore_to="testing")
    assert out["current_stories"][0]["state"] == "testing"
    # Default path unchanged.
    out = sp.unblock_story(copy.deepcopy(state), "WU-1101", None)
    assert out["current_stories"][0]["state"] == "reviewing"


@pytest.mark.unit
def test_unblock_story_refuses_an_illegal_resume_target():
    """The override must not become a way around the transition table."""
    state = {"current_stories": [_blocked_unit()]}
    with pytest.raises(ValueError, match="not a legal target"):
        sp.unblock_story(state, "WU-1101", None, restore_to="done")


# ── the exhausted ladder names the exit that exists ──────────────────────────


def _spent_board(build_mode):
    """One blocked unit whose retry ladder is spent, and nothing else to do."""
    unit = _blocked_unit()
    unit["retries"] = {"se": 99}
    return {"build_mode": build_mode, "current_stories": [unit]}


@pytest.mark.unit
def test_exhausted_ladder_names_the_cut_on_spq():
    """The cut was available, correct, recorded and never suggested, which
    left `story_retry_cap` as the only thing that could move a blocked unit --
    merging the runaway-loop guard with the budget for finishing normal work
    into one dial."""
    out = sp.next_action(_spent_board("spq"))
    assert out["action"] == "all_blocked"
    assert out.get("recommended_exit") == "cut_work_unit"
    assert "cut" in out["reason"]
    assert "story_retry_cap" in out["reason"]


@pytest.mark.unit
def test_non_spq_boards_are_not_told_to_cut():
    """`cut_work_unit` is SPQ's own method event and `next_action` is shared;
    naming it on a scrum board would recommend an impossible action."""
    for mode in ("scrum", "kanban"):
        out = sp.next_action(_spent_board(mode))
        assert out["action"] == "all_blocked"
        assert "recommended_exit" not in out
        assert "cut" not in out["reason"]


# ── what next_action hands the kernel ────────────────────────────────────────


def _recoverable_board():
    """One blocked unit whose ladder still has room."""
    return {"build_mode": "spq", "current_stories": [_blocked_unit()]}


@pytest.mark.unit
def test_recovery_payload_carries_the_override_when_rerouting(tmp_path):
    d = _receipts(tmp_path, qe=10, se=40)
    out = sp.next_action(_recoverable_board(), receipts_dir=d)
    assert out["action"] == "recover_blocked"
    assert out["recovery"]["resume_to"] == "testing"
    assert "resuming at 'testing'" in out["reason"]


@pytest.mark.unit
def test_recovery_payload_omits_the_override_when_unchanged(tmp_path):
    """The unchanged path must stay byte-identical. `unblock_story` validates
    a resume target it is handed, so carrying a redundant one would make a
    board with a hand-corrupted `blocked_from` start raising inside a dispatch
    where it used to restore silently."""
    d = _receipts(tmp_path, qe=40, se=10)
    out = sp.next_action(_recoverable_board(), receipts_dir=d)
    assert out["action"] == "recover_blocked"
    assert "resume_to" not in out["recovery"]
    assert "resuming at" not in out["reason"]


@pytest.mark.unit
def test_rerouted_unit_dispatches_qe_not_se(tmp_path):
    """The role must follow the resume target.

    Rerouting to `testing` while dispatching SE would put the builder on a
    unit the board says is being verified, and would spend the BUILDER's retry
    ladder on a verification the builder cannot perform. `testing` is QE's
    stage, so QE is who gets dispatched -- this is what the issue means by
    "offer qe when the code has changed since the last QE receipt"."""
    d = _receipts(tmp_path, qe=10, se=40)
    out = sp.next_action(_recoverable_board(), receipts_dir=d)
    assert out["recovery"]["resume_to"] == "testing"
    assert out["recovery"]["role"] == "qe"
    assert out["role"] == "qe"


@pytest.mark.unit
def test_unit_blocked_from_testing_keeps_its_ladder_role(tmp_path):
    """Scoped to the reroute: a unit that was already blocked from `testing`
    resumes there as it always did, with whatever role its ladder names. That
    path predates #802."""
    d = _receipts(tmp_path, qe=10, se=40)
    unit = _blocked_unit("testing")
    unit["retries"] = {"se": 1}
    out = sp.next_action(
        {"build_mode": "spq", "current_stories": [unit]}, receipts_dir=d
    )
    assert out["action"] == "recover_blocked"
    assert "resume_to" not in out["recovery"]
    assert out["recovery"]["role"] == "se"
