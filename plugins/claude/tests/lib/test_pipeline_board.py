"""Layer 1 — `pipeline_board`, the one board accessor (#514).

Hypothesis: one property carries this module, and it is not "returns the right
list". It is that **a board that could not be read and a board that is empty
never share a representation**. Every one of the four independently-found
instances of this defect reported emptiness as a measurement, and each was
found by a human noticing a zero rather than by anything failing.

So the tests below assert the distinction as hard as they assert the contents:
a fresh project, a hydrated Cycle, a Cycle with nothing admitted, and an
un-migrated v3+spq file must all be DIFFERENT answers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import _spq_fixture
import pipeline_board

pytestmark = pytest.mark.unit


@pytest.fixture
def spq_board():
    """Local override of the conftest factory: `open_cycle` is keyword-only now.

    Same contract -- `(project_dir, units=..., goal=...) -> (project, opened)`
    with the board produced by the state machine rather than hand-written --
    but the #644 declaration needs `repository`, `trunk_ref`, `source_region`
    and an `engineering_lead`, and a Cycle cannot open before Discovery
    approves a baseline.
    """
    return _spq_fixture.open_project


def _orch(project: Path) -> Path:
    orch = project / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True, exist_ok=True)
    return orch


def _write(project: Path, payload: dict) -> None:
    (_orch(project) / "pipeline-state.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


# ── SPQ: the board is not in the file the pointer lives in ────────────────────


def test_a_real_cycle_board_is_found_outside_pipeline_state(
    tmp_path: Path, spq_board
):
    """The fixture is produced by `open_cycle`, so `pipeline-state.json` really
    is only a pointer — which is the condition none of the hand-written
    fixtures reproduced (#509)."""
    project, _ = spq_board(
        tmp_path / "p",
        units=[{"id": "WU-1", "title": "a"}, {"id": "WU-2", "title": "b"}],
    )

    pointer = json.loads(
        (project / ".synaptory" / ".orchestrator" / "pipeline-state.json")
        .read_text(encoding="utf-8")
    )
    assert "current_stories" not in pointer, (
        "the fixture must reproduce the real post-open_cycle pointer, not a "
        "board inlined into pipeline-state.json"
    )

    board = pipeline_board.read_board(str(project))
    assert board.available is True
    assert board.hydrated is True
    assert board.story_ids() == ["WU-1", "WU-2"]
    assert board.source.endswith("execution-state.json")


def test_an_empty_board_and_an_unreadable_board_are_different_answers(
    tmp_path: Path, spq_board
):
    """The rule this module exists for, asserted directly.

    The empty side used to be a Cycle that admitted nothing. #644 made that
    state unconstructible -- `cycle_records.problems` refuses an empty
    `admitted_units`, "because the barrier ranges over the set Commit fixed and
    an empty set makes every barrier vacuous" -- so the arm asserts the refusal
    instead of building the state, and the readable-but-empty side is now the
    pre-Commit project, which is the one empty board SPQ can still produce.
    """
    with pytest.raises(Exception) as refusal:
        spq_board(tmp_path / "nothing-admitted", units=[])
    assert "admitted_units" in str(refusal.value)

    import spq_state_machine as spq

    pre_commit = tmp_path / "pre-commit"
    pre_commit.mkdir()
    (pre_commit / ".synaptory.yaml").write_text(
        'build_mode: "spq"\n', encoding="utf-8"
    )
    spq.initialize(str(pre_commit))
    empty = pipeline_board.read_board(str(pre_commit))
    assert (empty.available, empty.stories, empty.problems) == (True, [], [])

    # The retired v3.0 layout with build_mode spq: no Cycle identity resolves
    # from it, and that must not read as "this Cycle admitted nothing".
    stale = tmp_path / "stale"
    _write(stale, {"version": "3.0", "build_mode": "spq",
                   "lifecycle_state": "CYCLE_EXECUTION", "current_cycle": 1})
    refused = pipeline_board.read_board(str(stale))
    assert refused.available is False
    assert refused.stories == []
    assert refused.problems, "a refusal must say why"
    assert "ADR-035" in " ".join(refused.problems), (
        "the refusal names the decision; there is no migrator left to name "
        "instead (#640). Got: %r" % (refused.problems,)
    )


def test_a_pre_commit_spq_project_is_available_but_not_hydrated(tmp_path: Path):
    import spq_state_machine as spq

    project = tmp_path / "fresh"
    project.mkdir()
    (project / ".synaptory.yaml").write_text('build_mode: "spq"\n', encoding="utf-8")
    spq.initialize(str(project))

    board = pipeline_board.read_board(str(project))
    assert board.available is True, "a pre-COMMIT SPQ project is readable"
    assert board.hydrated is False, "no Cycle exists yet"
    assert board.stories == []
    assert board.problems == []


def test_a_cycle_with_no_board_file_still_names_its_admitted_units(
    tmp_path: Path, spq_board
):
    """The manifest fallback, which is what is left of the integration seat.

    `test_an_integration_seat_still_names_its_admitted_units` was deleted with
    its subject: #644 removed the seat, so there is no longer a clone whose
    board is empty by design while its Cycle admitted work (one Engineering
    Lead and one Crew per Cycle means one board, `SC-MTH-012`). What survives
    it is the code path the seat exercised, and it is still reachable and still
    worth pinning: a clone that has the committed declaration but not the
    board file -- the board is per-checkout, the declaration is committed -- must
    name the units rather than report a Cycle that admitted nothing.
    """
    project, opened = spq_board(
        tmp_path / "no-board",
        units=[{"id": "API-1", "title": "a"}, {"id": "CLI-1", "title": "b"}],
    )
    board_path = Path(pipeline_board.read_board(str(project)).source)
    board_path.unlink()

    sealed = json.loads(
        (project / ".synaptory" / ".orchestrator" / "spq" / "cycles"
         / opened["cycle_id"] / "manifest.json").read_text(encoding="utf-8")
    )
    assert [u["id"] for u in sealed["admitted_units"]] == ["API-1", "CLI-1"], (
        "the seal is the durable record; the fixture must produce it"
    )

    board = pipeline_board.read_board(str(project))
    assert board.hydrated is True, "a Cycle exists; only its board is missing"
    assert board.stories == []
    assert board.admitted_units == ["API-1", "CLI-1"], (
        "an empty board on a hydrated Cycle must fall back to the sealed "
        "declaration, or 'this clone has no board' reads as 'this Cycle "
        "admitted nothing'"
    )
    rendered = pipeline_board.render_restore(board)
    assert "none admitted to this Cycle" not in rendered, rendered
    assert "API-1" in rendered and "CLI-1" in rendered, rendered


# ── The other two layouts must not become the next blind spot ────────────────


def test_a_flat_scrum_board_is_read_and_rendered(tmp_path: Path):
    project = tmp_path / "scrum"
    _write(project, {
        "version": "2.0", "build_mode": "scrum",
        "lifecycle_state": "SPRINT_EXECUTION", "current_sprint": 3,
        "sprint_goal": "Deliver search", "sprints_completed": ["s1", "s2"],
        "current_stories": [{"id": "US-1", "state": "done"},
                            {"id": "US-2", "state": "in_progress"}],
    })
    board = pipeline_board.read_board(str(project))
    assert (board.layout, board.build_mode) == ("flat", "scrum")
    assert board.story_ids() == ["US-1", "US-2"]
    assert board.done_count() == 1
    assert pipeline_board.render_title_suffix(board) == " · Sprint 3"
    assert "Sprint Goal: Deliver search" in pipeline_board.render_restore(board)


def test_multi_spec_rolls_up_every_slot_not_just_the_active_one(tmp_path: Path):
    """Reading only `active_spec` is the same defect one layout over."""
    project = tmp_path / "multi"
    _write(project, {
        "version": "3.0", "build_mode": "scrum", "active_spec": "platform",
        "specs": {
            "platform": {"lifecycle_state": "SPRINT_EXECUTION",
                         "current_sprint": 9,
                         "current_stories": [{"id": "US-100", "state": "done"}]},
            "ehr": {"lifecycle_state": "INCEPTION", "current_sprint": 0,
                    "current_stories": [{"id": "US-200", "state": "queued"}]},
        },
    })
    board = pipeline_board.read_board(str(project))
    assert board.layout == "multi-spec"
    assert sorted(board.story_ids()) == ["US-100", "US-200"]
    rendered = pipeline_board.render_restore(board)
    assert "Active Spec: platform" in rendered
    assert "### Spec: platform  (active)" in rendered
    assert "### Spec: ehr" in rendered


def test_a_dangling_active_spec_is_reported_not_swallowed(tmp_path: Path):
    project = tmp_path / "dangling"
    _write(project, {
        "version": "3.0", "build_mode": "scrum", "active_spec": "gone",
        "specs": {"platform": {"lifecycle_state": "INCEPTION",
                               "current_stories": []}},
    })
    board = pipeline_board.read_board(str(project))
    assert board.available is True
    assert any("active_spec" in p for p in board.problems)


# ── Failure shapes ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("payload,expected", [
    ("", "empty"),
    ("{not json", "not valid JSON"),
    ("[1, 2, 3]", "not a JSON object"),
])
def test_a_corrupt_state_file_is_a_problem_not_an_empty_board(
    tmp_path: Path, payload: str, expected: str
):
    project = tmp_path / "corrupt"
    (_orch(project) / "pipeline-state.json").write_text(payload, encoding="utf-8")
    board = pipeline_board.read_board(str(project))
    assert board.available is False
    assert any(expected in p for p in board.problems)


def test_a_project_with_no_state_file_renders_nothing(tmp_path: Path):
    """Absent state is a fresh project, not a failure to report."""
    project = tmp_path / "nothing"
    project.mkdir()
    board = pipeline_board.read_board(str(project))
    assert (board.available, board.layout) == (False, "none")
    assert pipeline_board.render_restore(board) == ""
    assert pipeline_board.render_title_suffix(board) == ""


def test_read_board_never_raises(tmp_path: Path):
    """A hook, a report and a metric are the callers; none may take down the
    session, and a raise would be the one failure mode worse than a wrong zero
    only if it were caught and turned back into a zero — which is exactly what
    the four instances did."""
    assert pipeline_board.read_board(str(tmp_path / "does-not-exist")).available is False
    weird = tmp_path / "weird"
    _write(weird, {"build_mode": "spq", "spq": {"cycle_id": "not-an-id"}})
    board = pipeline_board.read_board(str(weird))
    assert board.build_mode == "spq"


# ── stories_in_state: the injected-state path ────────────────────────────────


def test_stories_in_state_covers_multi_spec(tmp_path: Path):
    state = {"specs": {"a": {"current_stories": [{"id": "X"}]},
                       "b": {"current_stories": [{"id": "Y"}]}}}
    assert sorted(s["id"] for s in pipeline_board.stories_in_state(state)) == ["X", "Y"]


def test_stories_in_state_tolerates_junk():
    assert pipeline_board.stories_in_state({}) == []
    assert pipeline_board.stories_in_state({"current_stories": "nope"}) == []
    assert pipeline_board.stories_in_state({"current_stories": [1, {"id": "Z"}]}) == [
        {"id": "Z"}
    ]


# ── completed_cycles: the C-16 route (#646) ──────────────────────────────────
#
# `C-16` plans concurrency against recorded per-Cycle history, and until this
# accessor existed a throughput / cut-rate consumer had no sanctioned route to
# it: `Board`'s slots stopped at `admitted_units`, so the consumer's only
# option was the direct read this whole module exists to prevent. The
# conformance measurement probe hit that immediately and recorded the gap
# rather than taking the forbidden route.


def _rollup(cycle_id="007-9f2c41ab", **over):
    record = {
        "cycle_id": cycle_id,
        "admitted_unit_ids": ["WU-01", "WU-02"],
        "admitted_count_at_commit": 2,
        "cut_unit_ids": [],
        "accepted_units": [],
        "closed_at": "2026-09-08T00:00:00Z",
    }
    record.update(over)
    return record


def test_a_fresh_cycle_has_an_empty_history_and_a_readable_board(
    tmp_path: Path, spq_board
):
    """An absence, and it must be reported as one: `available` says the board
    was read, and the empty history says no Cycle has closed. A consumer that
    read the empty list as a history of zero throughput would plan capacity
    against a number nobody measured."""
    project, _ = spq_board(tmp_path / "fresh", units=[{"id": "WU-1", "title": "a"}])
    board = pipeline_board.read_board(str(project))
    assert board.available is True
    assert board.completed_cycles() == []


def test_recorded_history_is_found_on_the_spq_board_and_not_in_the_pointer(
    tmp_path: Path, spq_board
):
    """The defect shape, one field over: a reader that looked for the rollup in
    the mode pointer would report no history for a project with plenty, exactly
    as four readers did for the unit list."""
    import json as _json

    project, _state = spq_board(
        tmp_path / "closed", units=[{"id": "WU-1", "title": "a"}]
    )
    board_path = Path(pipeline_board.read_board(str(project)).source)
    on_board = _json.loads(board_path.read_text(encoding="utf-8"))
    on_board["cycles_completed"] = [_rollup()]
    board_path.write_text(_json.dumps(on_board), encoding="utf-8")

    pointer_path = (
        project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
    )
    pointer = _json.loads(pointer_path.read_text(encoding="utf-8"))
    assert "cycles_completed" not in pointer

    board = pipeline_board.read_board(str(project))
    assert [c["cycle_id"] for c in board.completed_cycles()] == ["007-9f2c41ab"]


def test_a_history_in_the_pointer_is_not_the_spq_history(
    tmp_path: Path, spq_board
):
    """Non-vacuity for the test above: if the reader took the pointer's copy,
    this would pass too and the previous test would prove nothing about where
    it looked."""
    import json as _json

    project, _ = spq_board(tmp_path / "decoy", units=[{"id": "WU-1", "title": "a"}])
    pointer_path = (
        project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
    )
    pointer = _json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["cycles_completed"] = [_rollup(cycle_id="999-decoy000")]
    pointer_path.write_text(_json.dumps(pointer), encoding="utf-8")

    board = pipeline_board.read_board(str(project))
    assert board.completed_cycles() == []


def test_a_flat_board_exposes_its_completed_cycles(tmp_path: Path):
    project = tmp_path / "flat-history"
    _write(project, {
        "build_mode": "scrum", "lifecycle_state": "SPRINT_EXECUTION",
        "current_stories": [], "cycles_completed": [_rollup()],
    })
    board = pipeline_board.read_board(str(project))
    assert len(board.completed_cycles()) == 1


def test_multi_spec_history_rolls_up_every_slot(tmp_path: Path):
    """Reading only the active slot would be the same blind spot one layout
    over, which is the rule the story reader already follows here."""
    project = tmp_path / "ms-history"
    _write(project, {
        "build_mode": "scrum", "active_spec": "a",
        "specs": {
            "a": {"lifecycle_state": "X", "cycles_completed": [_rollup("a-1")]},
            "b": {"lifecycle_state": "X", "cycles_completed": [_rollup("b-1")]},
        },
    })
    board = pipeline_board.read_board(str(project))
    assert sorted(c["cycle_id"] for c in board.completed_cycles()) == ["a-1", "b-1"]


def test_an_unreadable_board_reports_no_history_it_could_not_read(tmp_path: Path):
    """`available: False` with an empty history is `nothing was read`, and a
    caller must not report it as `no Cycle has closed`."""
    project = tmp_path / "corrupt-history"
    (_orch(project) / "pipeline-state.json").write_text("{not json", encoding="utf-8")
    board = pipeline_board.read_board(str(project))
    assert (board.available, board.completed_cycles()) == (False, [])
    assert board.problems


def test_completed_cycles_hands_back_a_copy():
    """A consumer that mutated the list would edit the board's own record, and a
    metric that edits its input is a metric nobody can reproduce."""
    board = pipeline_board.Board(cycles=[_rollup()])
    board.completed_cycles().append(_rollup("junk"))
    assert len(board.cycles) == 1


def test_cycles_in_state_covers_multi_spec_for_an_injected_state():
    state = {"specs": {"a": {"cycles_completed": [_rollup("a-1")]},
                       "b": {"cycles_completed": [_rollup("b-1")]}}}
    assert sorted(
        c["cycle_id"] for c in pipeline_board.cycles_in_state(state)
    ) == ["a-1", "b-1"]


def test_cycles_in_state_tolerates_junk():
    assert pipeline_board.cycles_in_state({}) == []
    assert pipeline_board.cycles_in_state({"cycles_completed": "nope"}) == []
    assert pipeline_board.cycles_in_state({"cycles_completed": [1, _rollup()]}) == [
        _rollup()
    ]


def test_the_restore_render_counts_cycles_from_one_definition(
    tmp_path: Path, spq_board
):
    """The count the session restore prints and the list a metric reads must be
    the same fact; two definitions is how a product ships three different
    counts of one thing."""
    import json as _json

    project, _state = spq_board(
        tmp_path / "render", units=[{"id": "WU-1", "title": "a"}]
    )
    board_path = Path(pipeline_board.read_board(str(project)).source)
    on_board = _json.loads(board_path.read_text(encoding="utf-8"))
    on_board["cycles_completed"] = [_rollup(), _rollup("008-aabbccdd")]
    board_path.write_text(_json.dumps(on_board), encoding="utf-8")

    board = pipeline_board.read_board(str(project))
    assert "Completed Cycles: 2" in pipeline_board.render_restore(board)
