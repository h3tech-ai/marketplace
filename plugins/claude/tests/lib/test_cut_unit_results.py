"""Layer 1 -- a cut unit reports no result, so a Cycle that cut can close.

`_eval_admitted_set_closed` asserts closure BY IDENTITY: it refuses a missing
result and an extra one alike. That only works when both sides of the
comparison are derived from the same authority. They were not -- `effective_set`
reads the committed cut record while `_unit_results` read the board, and a cut
unit stays on the board as `cancelled` and is re-projected from `admitted_units`
on every `hydrate_cycle`. So every Cycle that used a cut reported one result too
many, `extra` was never empty, and `admitted_set_closed` could not pass by any
honest action. Cutting is the only valve `SC-MTH-009` gives a Cycle that cannot
finish its admitted set, so this blocked the close of any Cycle that used it.

The filter is on the RECORD, never on the board's `cancelled` state: the board
is agent-writable and the committed record is what `effective_set` already
reads. A unit cancelled on the board with no cut behind it must still report,
and must still block.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import cycle_barrier as barrier
import spq_state_machine as sm

from _spq_fixture import CYCLE_KWARGS, unit as _fx_unit

TRUNK = {
    "trunk_ref": "dev",
    "observed_sha": "a" * 40,
    "current_sha": "a" * 40,
    "candidate_sha": "b" * 40,
    "candidate_is_ancestor_of_trunk": True,
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=False)


@pytest.fixture
def cycle(tmp_path: Path, monkeypatch):
    """A Cycle with two admitted units, opened by the product."""
    project = tmp_path / "proj"
    project.mkdir()
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "t@e.co")
    _git(project, "config", "user.name", "t")
    (project / "f.txt").write_text("x", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")
    (project / ".synaptory.yaml").write_text("build_mode: spq\n", encoding="utf-8")
    monkeypatch.delenv("SYNAPTORY_ACTIVE_SPEC", raising=False)

    sm.initialize(str(project))
    sm.approve_baseline(
        str(project),
        approved_by="lead@h3t.co",
        baseline_ref="baseline-1",
        calibration={"sample_units": 2, "measured_hours": 8},
    )
    sm.open_cycle(
        str(project),
        goal="cycle 1",
        admitted_units=[_fx_unit("WU-01"), _fx_unit("WU-02")],
        **CYCLE_KWARGS,
    )
    return project, sm.identity(str(project)).cycle_id


def _results(project: Path) -> dict:
    return sm._unit_results(str(project), sm.read_state(str(project)))


def _verdict(project: Path, cycle_id: str) -> dict:
    """The barrier's own verdict over the real declaration, cuts and results.

    Only the proofs and the trunk observation are stubbed -- what this file is
    about is which units the two set-derivations name, and those are real.
    """
    return barrier.evaluate(
        declaration=sm.read_manifest(str(project), cycle_id),
        cuts=sm.read_cuts(str(project), cycle_id),
        unit_results=_results(project),
        proofs={"regression": {"passed": True, "detail": "stub"}},
        trunk=TRUNK,
        changed_paths=["api/wu-01/impl.py"],
    )


@pytest.mark.unit
def test_a_cut_unit_is_not_reported_as_a_unit_result(cycle):
    project, _ = cycle
    assert set(_results(project)) == {"WU-01", "WU-02"}

    sm.cut_work_unit(
        str(project), "WU-02", "the client deferred the export view",
        cut_by="lead@h3t.co",
    )

    assert set(_results(project)) == {"WU-01"}, (
        "a cut unit has no result to report; emitting an empty one for it is "
        "what made the reported set differ from the effective set"
    )


@pytest.mark.unit
def test_admitted_set_closed_passes_after_a_cut(cycle):
    """The blocking case, end to end: before the fix this criterion reported
    `results for units not in the effective set: WU-02` and no honest action
    could clear it."""
    project, cycle_id = cycle
    sm.cut_work_unit(
        str(project), "WU-02", "deferred to the next Cycle", cut_by="lead@h3t.co",
    )

    closed = _verdict(project, cycle_id)["criteria"]["admitted_set_closed"]

    assert closed["extra"] == []
    assert closed["missing"] == []
    assert closed["passed"] is True, closed["detail"]


@pytest.mark.unit
def test_the_effective_and_reported_sets_agree_after_a_cut(cycle):
    project, cycle_id = cycle
    sm.cut_work_unit(str(project), "WU-02", "deferred", cut_by="lead@h3t.co")

    verdict = _verdict(project, cycle_id)

    assert verdict["cut_unit_ids"] == ["WU-02"]
    assert set(verdict["effective_unit_ids"]) == set(_results(project))


@pytest.mark.unit
def test_a_cancelled_board_state_with_no_recorded_cut_still_reports(cycle):
    """The authority distinction, which is the whole reason the filter reads
    the record. `cancelled` on the board is agent-writable; withdrawing a unit
    by writing that state must not shrink the barrier's subject."""
    project, cycle_id = cycle
    state = sm.read_state(str(project))
    next(u for u in state["current_stories"] if u["id"] == "WU-02")["state"] = (
        "cancelled"
    )
    sm._write_state(str(project), state)

    assert sm.read_cuts(str(project), cycle_id) == []
    assert "WU-02" in _results(project), (
        "a unit cancelled on the board with no cut record behind it must still "
        "report, and must still block"
    )

    closed = _verdict(project, cycle_id)["criteria"]["admitted_set_closed"]
    assert closed["extra"] == []
    assert closed["missing"] == []


@pytest.mark.unit
def test_an_uncut_cycle_is_unchanged(cycle):
    project, cycle_id = cycle
    closed = _verdict(project, cycle_id)["criteria"]["admitted_set_closed"]
    assert closed["extra"] == []
    assert closed["missing"] == []
    assert set(_results(project)) == {"WU-01", "WU-02"}
