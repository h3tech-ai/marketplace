"""Shared SPQ fixture shapes, so one required field does not edit thirty files.

`cycle_records.seal` requires every admitted unit to declare a `kind`, at least
one acceptance criterion and a non-empty `path_scope` (#644). Those are the
rules that make the barrier's subject knowable, so they are required rather
than defaulted -- and that is correct for the product and awkward for a fixture
whose subject is the dependency ledger, or the seal, or the prove path, and
which only needs a Cycle to exist at all.

So the shape lives here once. A test that cares about a field passes it; a test
that does not gets a valid declaration and stays about its own subject.

WHY THIS IS NOT A BACKDOOR. It supplies the fields, it does not bypass the
check: `seal` still validates everything, and a fixture that wants to prove a
refusal passes the bad value explicitly. `test_cycle_records.py` is where the
requirements themselves are asserted, and it builds its units inline for
exactly that reason -- a helper that both satisfied and tested the rules would
be testing itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

#: The declaration fields every Cycle needs and most fixtures do not care about.
CYCLE_KWARGS: Dict[str, Any] = {
    "repository": "h3tech-ai/synaptory-v1",
    "trunk_ref": "refs/heads/dev",
    "source_region": ["api/"],
    "engineering_lead": "alice@h3t.co",
}


def unit(unit_id: str = "WU-1", **over: Any) -> Dict[str, Any]:
    """One admitted Work Unit, valid by default.

    `path_scope` is derived from the id rather than shared, because two units
    with one scope is a collision the declaration refuses -- a fixture with two
    units and a constant scope would fail for a reason that has nothing to do
    with its subject.
    """
    record: Dict[str, Any] = {
        "id": unit_id,
        "title": over.pop("title", "work"),
        "kind": "story",
        "acceptance_criteria": ["it works"],
        "path_scope": ["api/%s/" % unit_id.lower().replace("_", "-")],
    }
    record.update(over)
    return record


def units(*ids: str) -> List[Dict[str, Any]]:
    return [unit(uid) for uid in ids]


def normalize_unit(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Complete a partial unit dict, keeping whatever the caller declared.

    Exists so a fixture whose subject is a board reader can keep writing
    `{"id": "WU-1", "title": "index writer"}` -- the shape every pre-#644
    fixture used -- and still produce a declaration `seal` accepts.
    """
    given = dict(record)
    unit_id = str(given.pop("id", None) or "WU-1")
    return unit(unit_id, **given)


def open_project(
    project_dir: Any,
    units: Optional[Sequence[Mapping[str, Any]]] = None,
    goal: str = "cycle goal",
    **over: Any,
) -> Tuple[Path, Dict[str, Any]]:
    """A project at `CYCLE` whose board came from `open_cycle`.

    **The board is produced by `spq_state_machine`, never hand-written**, and
    that is the point (#514, the lesson from #509). Three of the four
    independently-found "reader is blind to the SPQ board" defects survived
    their own suites because the fixture wrote
    `{"build_mode": "spq", "current_stories": [...]}` straight into
    `pipeline-state.json` -- a shape SPQ has not produced since #303. A fixture
    that puts the board where the blind reader already looks cannot reach the
    defect.

    What the real lifecycle produces instead: `initialize` -> `approve_baseline`
    -> `open_cycle` leaves `pipeline-state.json` as a mode + identity POINTER
    and writes the board to `spq/cycles/<cycle-id>/execution-state.json`.

    `approve_baseline` is not scaffolding here. `open_cycle` refuses a project
    whose Discovery approved no baseline, because the baseline is the line a cut
    must not move (`SC-MTH-009`) -- so a fixture that skipped it would be
    testing the refusal instead of the reader.
    """
    import spq_state_machine as sm

    project = Path(project_dir)
    project.mkdir(parents=True, exist_ok=True)
    cfg = project / ".synaptory.yaml"
    if not cfg.exists():
        cfg.write_text('build_mode: "spq"\n', encoding="utf-8")

    sm.initialize(str(project))
    sm.approve_baseline(
        str(project),
        approved_by="alice@h3t.co",
        baseline_ref="baseline-1",
        calibration={"sample_units": 1, "measured_hours": 1},
    )
    kwargs = dict(CYCLE_KWARGS)
    kwargs.update(over)
    admitted = [
        normalize_unit(u)
        for u in (units if units is not None else [{"id": "WU-1", "title": "first unit"}])
    ]
    opened = sm.open_cycle(
        str(project), goal=goal, admitted_units=admitted, **kwargs
    )
    return project, opened
