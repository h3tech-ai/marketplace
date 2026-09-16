"""Layer 1 — a verifier may not report `passed` for a subject it never read (#529).

`verify_all.py` is the pre-deploy verifier: a green result from it is consumed
as evidence that the state machine is sound. On an SPQ project it returned::

    {"name": "State Machine", "status": "passed", "errors": [],
     "build_mode": "spq", "lifecycle_state": "", "transitions_completed": 0}

having validated **nothing**. Its `if scrum / elif kanban` chain had no `else`,
and the SPQ `pipeline-state.json` is a mode + identity pointer carrying no
`lifecycle_state`, so both branches were skipped and the function fell through
to its own initialiser value of `"passed"`.

That is the worst member of the SPQ-blind-reader class #528 closed. Every other
member fails to see a board and reports **emptiness**; this one reports
**success**. The epic's transferable rule, one level up from where it was
written: *a pass that means "validated, and it held" and a pass that means
"nothing was validated" must never share a representation.*

The fixtures here come from `spq_board`, which drives the real
`initialize -> approve_baseline -> open_cycle` lifecycle. That is the #509
lesson taken literally: three of this class's four original members survived
their own test
suites because their fixtures hand-wrote `{"build_mode": "spq",
"current_stories": [...]}` into `pipeline-state.json`, a shape SPQ has not
produced since #303, so the fixture put the board exactly where the blind
reader was already looking.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import _spq_fixture

pytestmark = pytest.mark.unit


def _paths_for(repo_root: Path):
    return [str(repo_root / "core" / "lib"), str(repo_root / "core" / "scripts")]


@pytest.fixture
def spq_board(repo_root: Path, monkeypatch):
    """Local override of the conftest factory: `open_cycle` is keyword-only now.

    Same contract, and the board is still produced by the state machine rather
    than hand-written; `_spq_fixture.open_project` carries the #644 declaration
    fields and the baseline `open_cycle` now requires.
    """
    for entry in _paths_for(repo_root):
        if entry not in sys.path:
            monkeypatch.syspath_prepend(entry)
    return _spq_fixture.open_project


@pytest.fixture
def verify_all_mod(repo_root: Path, monkeypatch):
    for entry in _paths_for(repo_root):
        if entry not in sys.path:
            monkeypatch.syspath_prepend(entry)
    import verify_all

    return verify_all


@pytest.fixture
def spq_project(tmp_path: Path, spq_board, repo_root: Path, monkeypatch):
    """A Cycle opened by the real state machine, with two admitted Work Units."""
    for entry in _paths_for(repo_root):
        if entry not in sys.path:
            monkeypatch.syspath_prepend(entry)
    project, _state = spq_board(
        tmp_path / "spq-project",
        units=[{"id": "WU-1", "title": "index writer"},
               {"id": "WU-2", "title": "query api"}],
        goal="ship the search feature",
    )
    return project


def _write_flat(project: Path, state: dict) -> Path:
    orch = project / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True, exist_ok=True)
    (orch / "pipeline-state.json").write_text(json.dumps(state), encoding="utf-8")
    return project


# ── The certifying defect itself ──────────────────────────────────────────────


def test_the_verifier_reads_the_spq_board(verify_all_mod, spq_project: Path):
    """The measured defect, inverted: the board is read, and what it says is used."""
    out = verify_all_mod.verify_state_machine(str(spq_project))
    assert out["build_mode"] == "spq"
    assert out["lifecycle_state"] == "CYCLE", (
        "the verifier still reads lifecycle_state off the SPQ pointer, where "
        "there is none; got %r" % (out,)
    )
    assert out["status"] == "passed"
    assert out["checks_performed"], (
        "a `passed` with an empty checks_performed is the defect this ticket "
        "exists for: got %r" % (out,)
    )
    # It read the execution state, not the pointer.
    assert out["board_source"].endswith("execution-state.json")
    # Two entries, not three: `DISCOVERY` and `CYCLE`. The predecessor reached
    # a Cycle through `transition(COMMIT)` and then a second execution state;
    # #644 opens the Cycle directly, so one stage change gets recorded here
    # where two used to. It IS recorded -- `open_cycle` appended nothing to
    # `lifecycle_history` until #640, so a project that spent its whole
    # delivery at CYCLE recorded a `DISCOVERY -> ACCEPTANCE` timeline it never
    # took.
    assert out["transitions_completed"] == 2


def test_a_pass_names_what_it_validated(verify_all_mod, tmp_path: Path):
    """The structural half of the rule: a pass carries its evidence.

    Without `checks_performed` a caller cannot distinguish "the lifecycle state
    was compared against the legal set and matched" from "no comparison
    happened", because both used to be the single token `passed`.
    """
    project = _write_flat(tmp_path / "scrum", {
        "version": "2.0", "build_mode": "scrum",
        "lifecycle_state": "SPRINT_EXECUTION",
        "lifecycle_history": [{"state": "INCEPTION"}],
    })
    out = verify_all_mod.verify_state_machine(str(project))
    assert out["status"] == "passed"
    assert out["checks_performed"] == [
        "scrum lifecycle_state SPRINT_EXECUTION is one of the 8 legal scrum states"
    ]


def test_an_unknown_build_mode_is_unverifiable_not_passed(
    verify_all_mod, tmp_path: Path
):
    """The root of the defect: an if/elif chain with no else certified by falling
    through. A mode this verifier holds no state list for validated nothing, so
    it may not pass."""
    project = _write_flat(tmp_path / "unknown", {
        "version": "2.0", "build_mode": "waterfall",
        "lifecycle_state": "PHASE_GATE_3",
    })
    out = verify_all_mod.verify_state_machine(str(project))
    assert out["status"] == verify_all_mod.UNVERIFIABLE
    assert "waterfall" in out["reason"]
    assert out["checks_performed"] == []


def test_an_unreadable_spq_board_is_unverifiable_not_passed(
    verify_all_mod, tmp_path: Path
):
    """An SPQ project still on the retired v3.0 layout: no Cycle identity
    resolves from that pointer, so there is no board and therefore no verdict.

    The refusal is `pipeline_board.RETIRED_V3_REFUSAL`. It used to come from
    `spq_state_machine.read_state`, which refused such a pointer outright; the
    #644 machine returns the default board instead, and this test is one of the
    two that caught the verifier certifying an unread board again (#640).
    """
    project = _write_flat(tmp_path / "unmigrated", {
        "version": "3.0", "build_mode": "spq",
        "lifecycle_state": "CYCLE_EXECUTION", "current_cycle": 1,
    })
    out = verify_all_mod.verify_state_machine(str(project))
    assert out["status"] == verify_all_mod.UNVERIFIABLE
    assert "ADR-035" in out["reason"], (
        "the refusal must carry its own remedy, and the remedy is now the "
        "decision plus `open_cycle` rather than a migrator; got %r"
        % (out["reason"],)
    )


def test_a_board_with_no_lifecycle_state_is_unverifiable(
    verify_all_mod, tmp_path: Path
):
    """`if lifecycle_state and lifecycle_state not in STATES` passed an empty
    one. An absent lifecycle is nothing to compare, not a comparison that held."""
    project = _write_flat(tmp_path / "nolifecycle", {
        "version": "2.0", "build_mode": "scrum", "current_stories": [],
    })
    out = verify_all_mod.verify_state_machine(str(project))
    assert out["status"] == verify_all_mod.UNVERIFIABLE


def test_every_spec_slot_is_validated_not_only_the_active_one(
    verify_all_mod, tmp_path: Path
):
    """Checking `active_spec` alone is the same defect one layout over: on the
    v3.0 Multi-Spec envelope the top-level `lifecycle_state` is absent, so the
    whole envelope used to validate as `passed` with nothing read."""
    project = _write_flat(tmp_path / "multi", {
        "version": "3.0", "build_mode": "scrum", "active_spec": "platform",
        "specs": {
            "platform": {"lifecycle_state": "SPRINT_EXECUTION"},
            "ehr": {"lifecycle_state": "NOT_A_REAL_STATE"},
        },
    })
    out = verify_all_mod.verify_state_machine(str(project))
    assert out["status"] == "failed"
    assert any("NOT_A_REAL_STATE" in e for e in out["errors"])
    assert len(out["checks_performed"]) == 2


# ── one readable slot cannot carry an unread one to green (#396) ─────────────
#
# `read_board` reports partial Multi-Spec read failures in `board.problems`
# while still reporting `available`, and the verifier consulted those problems
# only when the board was WHOLLY unavailable. So a valid slot supplied a
# `checks_performed` entry, carried the result to `passed`, and sat next to an
# error saying another slot was never validated. #529's rule is that an unread
# subject and a validated subject cannot share a green representation.


@pytest.mark.parametrize(
    "specs,active,expect_in_reason",
    [
        ({"platform": {"lifecycle_state": "SPRINT_EXECUTION"}, "ehr": {}},
         "platform", "ehr"),
        ({"platform": {"lifecycle_state": "SPRINT_EXECUTION"},
          "ehr": "unreadable"},
         "platform", "ehr"),
        ({"platform": {"lifecycle_state": "SPRINT_EXECUTION"}},
         "missing", "missing"),
    ],
    ids=["empty-slot", "not-an-object", "active-slot-absent"],
)
def test_a_partly_read_board_is_not_a_pass(
    verify_all_mod, tmp_path: Path, specs, active, expect_in_reason
):
    """All three of the reviewer's reproductions, and each returned `passed`.

    The affected slot is named in the reason, because "something was not
    validated" without saying which is a report an operator cannot act on.
    """
    project = _write_flat(tmp_path / ("partial-%s" % active), {
        "version": "3.0", "build_mode": "scrum", "active_spec": active,
        "specs": specs,
    })
    out = verify_all_mod.verify_state_machine(str(project))
    assert out["status"] != "passed", (
        "a partly read board certified as passed: %r" % (out,)
    )
    assert expect_in_reason in (out.get("reason") or "") or any(
        expect_in_reason in e for e in out["errors"]
    ), out


def test_a_measured_failure_outranks_an_unread_slot(verify_all_mod, tmp_path: Path):
    """"This state is illegal" is a stronger statement than "this one was not
    read", and the caller should see the first. Otherwise tightening the
    partial-board case would hide every illegal state that shares a board with
    an unreadable sibling."""
    project = _write_flat(tmp_path / "mixed", {
        "version": "3.0", "build_mode": "scrum", "active_spec": "platform",
        "specs": {
            "platform": {"lifecycle_state": "NOT_A_REAL_STATE"},
            "ehr": {},
        },
    })
    out = verify_all_mod.verify_state_machine(str(project))
    assert out["status"] == "failed", out
    assert any("NOT_A_REAL_STATE" in e for e in out["errors"]), out


def test_an_illegal_lifecycle_state_still_fails(verify_all_mod, tmp_path: Path):
    """Regression guard. **This test passes against `origin/dev` as well** — it
    is here so the rewrite cannot lose the one check the function did perform."""
    project = _write_flat(tmp_path / "bad", {
        "version": "2.0", "build_mode": "kanban",
        "lifecycle_state": "SPRINT_RETRO",
    })
    out = verify_all_mod.verify_state_machine(str(project))
    assert out["status"] == "failed"
    assert any("SPRINT_RETRO" in e for e in out["errors"])


# ── The second board read in the same file ────────────────────────────────────


def test_story_receipt_coverage_sees_spq_work_units(
    verify_all_mod, spq_project: Path, repo_root: Path, monkeypatch
):
    """`verify_story_receipts` read `current_stories` off the pointer AND looked
    for receipts in the flat `receipts/` dir, neither of which exists on SPQ."""
    for entry in _paths_for(repo_root):
        if entry not in sys.path:
            monkeypatch.syspath_prepend(entry)
    import spq_state_machine as spq
    import story_pipeline

    state = spq.read_state(str(spq_project))
    for unit in state["current_stories"]:
        unit["state"] = "done"
    spq._write_state(str(spq_project), state)

    receipts = Path(story_pipeline.receipts_dir_for(str(spq_project), intended=True))
    receipts.mkdir(parents=True, exist_ok=True)
    for unit_id in ("WU-1", "WU-2"):
        (receipts / ("%s-se.json" % unit_id)).write_text(
            json.dumps({"role": "se"}), encoding="utf-8")

    out = verify_all_mod.verify_story_receipts(str(spq_project))
    assert out["status"] == "passed"
    assert out["stories_checked"] == 2, (
        "the verifier still reads current_stories off the SPQ pointer; got %r"
        % (out,)
    )
    assert out["stories_with_receipts"] == 2


def test_a_missing_spq_receipt_is_reported_not_skipped(
    verify_all_mod, spq_project: Path, repo_root: Path, monkeypatch
):
    """The half that matters most: a done Work Unit with no SE receipt is a
    finding. Before the fix this whole check returned `skipped: No receipts
    directory` on every SPQ project, and a skip aggregates as a pass."""
    for entry in _paths_for(repo_root):
        if entry not in sys.path:
            monkeypatch.syspath_prepend(entry)
    import spq_state_machine as spq

    state = spq.read_state(str(spq_project))
    state["current_stories"][0]["state"] = "done"
    spq._write_state(str(spq_project), state)

    out = verify_all_mod.verify_story_receipts(str(spq_project))
    assert out["status"] == "failed"
    assert out["stories_checked"] == 1
    assert any("WU-1" in e for e in out["errors"])


def test_no_completed_story_is_a_skip_that_names_what_it_saw(
    verify_all_mod, spq_project: Path
):
    """The counter-case. "Nothing to check" is legitimate — but it must record
    that the board WAS read and how many items were on it, or it is
    indistinguishable from the read that never happened."""
    out = verify_all_mod.verify_story_receipts(str(spq_project))
    assert out["status"] == "skipped"
    assert out["stories_on_board"] == 2


# ── The aggregate, one level up ───────────────────────────────────────────────


def test_an_unverifiable_check_cannot_produce_a_passed_run(
    verify_all_mod, tmp_path: Path, monkeypatch
):
    """`all_passed` folded `skipped` in with `passed`; `unverifiable` must not
    join them, or the file-level fix is undone by the aggregation."""
    monkeypatch.setattr(verify_all_mod, "run_checklist", None)
    project = _write_flat(tmp_path / "unmigrated-run", {
        "version": "3.0", "build_mode": "spq",
        "lifecycle_state": "CYCLE_EXECUTION", "current_cycle": 1,
    })
    result = verify_all_mod.run_all_verifications(str(project), mode="autonomous")
    assert result["all_passed"] is False
    assert "State Machine" in result["unverifiable"]

    rendered = verify_all_mod.format_output(result)
    assert "NOT VERIFIED" in rendered, (
        "'this did not hold' and 'this was never checked' must not render as "
        "the same verdict; got:\n%s" % rendered
    )
