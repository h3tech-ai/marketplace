"""#797 — a verb that says which Cycles exist and what state each is in.

SPQ state was trapped in the one working tree that produced it. The board lives
under `.synaptory/.orchestrator/`, which the documented recipe gitignores, so a
clean checkout of the trunk answered `lifecycle_state: DISCOVERY` with
`.synaptory/cycles/<id>/manifest.json` sitting committed two directories away.

Not "unknown" — confidently wrong. And of the nineteen verbs, none listed
Cycles, so learning "Cycle 1 closed, Cycle 2 open" meant `ls` plus opening each
manifest by hand. `hydrate_cycle` was the documented recovery and is circular:
it needs the `--cycle-id` that is the thing being asked for.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks" / "lib"))

import spq_paths as _paths  # noqa: E402
import spq_state_machine as sm  # noqa: E402

from _spq_fixture import open_project, unit  # noqa: E402

pytestmark = pytest.mark.unit


def test_an_open_cycle_is_listed_with_its_goal_and_state(tmp_path):
    project, opened = open_project(
        tmp_path / "p", units=[unit("WU-1")], goal="ship the spine"
    )
    listing = sm.list_cycles(str(project))

    assert listing["count"] == 1
    row = listing["cycles"][0]
    assert row["cycle_id"] == opened["cycle_id"]
    assert row["goal"] == "ship the spine"
    assert row["state"] == "open"
    assert row["hydrated_here"] is True


def test_a_checkout_with_only_the_declaration_says_declared_not_discovery(tmp_path):
    """The exact failure: the declaration travelled, the board did not.

    Before #797 this rendered as `DISCOVERY` with `baseline_approved: null` —
    an answer that is wrong rather than absent.
    """
    project, opened = open_project(tmp_path / "p", units=[unit("WU-1")])
    # Simulate the clean checkout: the committed tree survives, the gitignored
    # board does not.
    board = Path(sm._board_path(str(project), opened["cycle_id"]))
    if board.exists():
        board.unlink()

    row = sm.list_cycles(str(project))["cycles"][0]
    assert row["state"] == "declared"
    assert row["hydrated_here"] is False, (
        "a Cycle readable here but not drivable here is the distinction the "
        "verb exists to draw"
    )


def test_it_reads_the_committed_tree_not_the_gitignored_index(tmp_path):
    """A reader that used the index would still answer from one directory."""
    project, opened = open_project(tmp_path / "p", units=[unit("WU-1")])
    index = Path(_paths.index_path(str(project)))
    if index.exists():
        index.unlink()

    listing = sm.list_cycles(str(project))
    assert listing["count"] == 1
    assert listing["cycles"][0]["cycle_id"] == opened["cycle_id"]


def test_a_project_with_no_cycles_lists_none_without_raising(tmp_path):
    project = tmp_path / "bare"
    project.mkdir()
    (project / ".synaptory.yaml").write_text('build_mode: "spq"\n', encoding="utf-8")

    listing = sm.list_cycles(str(project))
    assert listing == {"cycles": [], "current_cycle_id": "", "count": 0}


def test_the_declared_tracker_ref_is_carried_so_an_operator_number_resolves(tmp_path):
    """#799 rests on this: `/synaptory start cycle 2` needs somewhere to look
    `2` up."""
    project = tmp_path / "gh"
    project.mkdir()
    (project / ".synaptory.yaml").write_text(
        'build_mode: "spq"\ntracker:\n  backend: "github"\n', encoding="utf-8"
    )
    project, _ = open_project(project, units=[unit("WU-1")], tracker_ref="2")

    assert sm.list_cycles(str(project))["cycles"][0]["tracker_ref"] == "2"
