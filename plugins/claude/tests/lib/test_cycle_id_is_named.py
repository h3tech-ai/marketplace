"""Layer 1 -- a verb acts on the Cycle it was told, not the one that wrote last.

`_write_state` moves a single shared pointer on EVERY board write. Eight verbs
accept `--cycle-id` and resolve through `identity(project_dir, cycle_id=...)`;
six did not, and read `state["_cycle_id"]` instead. With two Cycles open in one
checkout those six acted on whichever Cycle wrote last, silently, with no
argument through which a caller could say which one it meant.

The set left out was the worst available one. `run_barrier`, `promote_cycle`
and `close_cycle` are all explicit, so the Checkpoint was safe; what was not
safe was everything that makes concurrent Cycles work together. `dep_status`
and `record_sync` ARE the cross-Cycle dependency machinery -- concurrent Cycles
are the case they exist for -- and `cut_work_unit` is the one whose mistakes
are hardest to undo.

`record_handover` was reported in that set and is NOT in it: it writes the
engagement through `_write_engagement`, reads no board and follows no pointer,
so an id there would name something it does not act on.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

import spq_state_machine as sm

from _spq_fixture import CYCLE_KWARGS, unit as _fx_unit

#: Every verb that acts on ONE Cycle must let the caller name it.
CYCLE_SCOPED_VERBS = (
    "read", "next_action", "revise_manifest", "hydrate_cycle",
    "run_barrier", "promote_cycle", "close_cycle", "publish_event",
    "cut_work_unit", "rebind_cut", "record_sync", "dep_status",
    "refresh_ledger", "acceptance_status",
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=False)


@pytest.fixture
def two_cycles(tmp_path: Path, monkeypatch):
    """Two Cycles open in ONE checkout, with the pointer left on the second."""
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
        str(project), approved_by="lead@h3t.co", baseline_ref="baseline-1",
        calibration={"sample_units": 2, "measured_hours": 8},
    )
    sm.open_cycle(
        str(project), goal="cycle one",
        admitted_units=[_fx_unit("WU-01"), _fx_unit("WU-02")], **CYCLE_KWARGS,
    )
    first = sm.identity(str(project)).cycle_id
    kwargs = dict(CYCLE_KWARGS)
    kwargs["source_region"] = ["second/"]
    sm.open_cycle(
        str(project), goal="cycle two",
        admitted_units=[_fx_unit("WU-11", path_scope=["second/a/"]),
                        _fx_unit("WU-12", path_scope=["second/b/"])],
        **kwargs,
    )
    second = sm.identity(str(project)).cycle_id
    assert first != second
    return project, first, second


def _cli_source() -> str:
    body = Path(sm.__file__).read_text(encoding="utf-8")
    return body[body.index("def main("):]


@pytest.mark.unit
@pytest.mark.parametrize("verb", CYCLE_SCOPED_VERBS)
def test_every_cycle_scoped_verb_accepts_cycle_id(verb: str):
    """Read from the DISPATCH, not from a docstring: a flag is offered only if
    the branch that handles the verb actually reads it."""
    source = _cli_source()
    start = source.index('verb == "%s"' % verb)
    end = min(
        (source.index(m, start + 1) for m in ('elif verb ==', 'else:')
         if m in source[start + 1:]),
        default=len(source),
    )
    assert '--cycle-id' in source[start:end], (
        "%s acts on one Cycle but its CLI branch offers no --cycle-id, so a "
        "caller with two Cycles open cannot say which one it means" % verb
    )


@pytest.mark.unit
def test_record_handover_is_engagement_scoped_and_needs_no_id():
    """Reported in the unsafe set and not in it. It writes the engagement, so
    an id here would name something the verb does not act on."""
    source = Path(sm.__file__).read_text(encoding="utf-8")
    body = source[source.index("def record_handover("):]
    body = body[:body.index("\ndef ", 1)]
    assert "_write_engagement" in body
    assert "read_state" not in body
    assert "_cycle_id" not in body


@pytest.mark.unit
def test_dep_status_reports_the_named_cycle_not_the_last_written(two_cycles):
    project, first, second = two_cycles
    assert sm.identity(str(project)).cycle_id == second, "pointer is on the second"

    verdict = sm.dep_status(str(project), cycle_id=first)

    assert verdict["cycle_id"] == first
    assert {u["unit_id"] for u in verdict["units"]} == {"WU-01", "WU-02"}


@pytest.mark.unit
def test_acceptance_status_counts_the_named_cycles_receipts(two_cycles):
    """Readiness is read off `ACCEPTANCE-{seq}-{role}.json` under one Cycle's
    receipts directory, so following the pointer reports another Cycle's
    readiness as this one's."""
    import spq_paths

    project, first, second = two_cycles
    receipts = Path(spq_paths.receipts_dir(str(project), first))
    receipts.mkdir(parents=True, exist_ok=True)
    (receipts / "ACCEPTANCE-1-qe.json").write_text("{}", encoding="utf-8")

    named = sm.acceptance_readiness(str(project), cycle_id=first)
    pointed = sm.acceptance_readiness(str(project))

    assert "quality-engineer" in named["present"]
    assert "quality-engineer" in pointed["missing"], (
        "the pointed-at Cycle has no such receipt and must not inherit one"
    )


@pytest.mark.unit
def test_a_cut_lands_on_the_named_cycle(two_cycles):
    """The dangerous one: a cut aimed at the wrong Cycle is hard to undo."""
    project, first, second = two_cycles
    assert sm.identity(str(project)).cycle_id == second

    sm.cut_work_unit(
        str(project), "WU-02", "deferred", cut_by="lead@h3t.co", cycle_id=first,
    )

    assert [c["unit_id"] for c in sm.read_cuts(str(project), first)] == ["WU-02"]
    assert sm.read_cuts(str(project), second) == [], (
        "the pointed-at Cycle must be untouched"
    )


@pytest.mark.unit
def test_record_sync_lands_on_the_named_waiting_cycle(two_cycles):
    project, first, second = two_cycles

    sm.record_sync(
        str(project), waiting_unit_id="WU-01", producing_cycle_id=second,
        resolution="published", cycle_id=first,
    )

    def _syncs(cid: str) -> int:
        events = sm.read_state(str(project), cycle_id=cid).get("method_events") or []
        return sum(1 for e in events if str(e.get("kind")) == "sync")

    assert _syncs(first) == 1
    assert _syncs(second) == 0
