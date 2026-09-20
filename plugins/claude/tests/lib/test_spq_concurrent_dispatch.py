"""#807 S1 — SPQ names the Work Units that may run alongside the one it picked.

The measured defect was not that concurrent work was refused. It was that it
was never RECOMMENDED: `advance_kernel.evaluate_dispatch` has authorized a
disjoint second unit since `C-07` shipped, but `next_action` returns one
action, an orchestrator executes the action it is given, and nothing on the
board ever said a second unit was legal. Seven mutually disjoint units ran at
a mean concurrency of 0.95.

So the assertions here are about the RECOMMENDATION agreeing with the
AUTHORIZATION — every unit the batch names must be one `evaluate_dispatch`
would allow, and no unit it refuses may appear.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks" / "lib"))

from _spq_fixture import open_project, unit  # noqa: E402


def _next(project):
    import spq_state_machine as sm

    return sm.next_action(str(project))


def test_disjoint_units_are_named_alongside_the_selected_one(tmp_path):
    project, _ = open_project(
        tmp_path / "p",
        units=[unit("WU-1"), unit("WU-2"), unit("WU-3")],
    )
    action = _next(project)

    assert action["action"].startswith("dispatch_")
    parallel = action.get("parallel")
    assert parallel is not None, "SPQ produced no concurrency recommendation at all"
    assert parallel["eligible"] is True
    assert parallel["governed_by"] == "declared path scope (C-07)"

    named = [m["story_id"] for m in parallel["batch"]]
    assert named[0] == action["story_id"], "batch[0] must be the serial action"
    assert set(named) == {"WU-1", "WU-2", "WU-3"}
    # Every member carries the role its own dispatch would, not a fabricated one.
    assert all(m["role"] for m in parallel["batch"])


def test_the_batch_never_names_a_unit_the_kernel_would_refuse(tmp_path):
    """The recommendation is checked against the authorization, per member.

    This is the assertion that would have caught a batch built from the board's
    `file_scope` (agent-writable) instead of the sealed declaration.
    """
    import advance_kernel as ak

    project, _ = open_project(
        tmp_path / "p",
        units=[unit("WU-1"), unit("WU-2"), unit("WU-3")],
    )
    action = _next(project)
    for member in action["parallel"]["batch"][1:]:
        decision = ak.evaluate_dispatch(
            str(project), member["story_id"], role=member["role"]
        )
        assert decision.allowed, (
            "batch named %s but the kernel refused it: %s"
            % (member["story_id"], decision.reason)
        )


def test_an_intersecting_scope_is_held_back_with_a_stated_reason(tmp_path):
    """Two units sharing a path are sequential work, and the batch says so.

    They can only be ADMITTED together with distinct `execution_order` -- the
    declaration refuses the pair otherwise -- so this is precisely the shape
    the batch has to hold back rather than one Commit already prevents.
    """
    project, _ = open_project(
        tmp_path / "p",
        units=[
            unit("WU-1", path_scope=["api/shared/"], execution_order=1),
            unit("WU-2", path_scope=["api/shared/deeper/"], execution_order=2),
        ],
    )
    parallel = _next(project)["parallel"]

    assert parallel["eligible"] is False
    assert [m["story_id"] for m in parallel["batch"]] == [
        _next(project)["story_id"]
    ]
    assert "path scope intersects the batch" in parallel["reason"]


def test_the_batch_is_pairwise_disjoint_not_merely_disjoint_from_the_lead(tmp_path):
    """A and B disjoint, A and C disjoint, B and C NOT — C must be held back.

    Checking only against the lead would put two colliding units in flight
    together, which is the collision `C-07` exists to prevent.
    """
    project, _ = open_project(
        tmp_path / "p",
        units=[
            unit("WU-1", path_scope=["api/one/"]),
            unit("WU-2", path_scope=["api/two/"], execution_order=1),
            unit("WU-3", path_scope=["api/two/nested/"], execution_order=2),
        ],
    )
    parallel = _next(project)["parallel"]
    named = [m["story_id"] for m in parallel["batch"]]

    assert "WU-1" in named
    assert not ("WU-2" in named and "WU-3" in named), (
        "WU-2 and WU-3 intersect each other; both were named: %s" % named
    )


def test_the_ceiling_bounds_the_batch(tmp_path):
    project, _ = open_project(
        tmp_path / "p",
        units=[unit("WU-%d" % n) for n in range(1, 6)],
    )
    (project / ".synaptory.yaml").write_text(
        'build_mode: "spq"\nparallelism:\n  max_concurrent_subagents: 2\n',
        encoding="utf-8",
    )
    parallel = _next(project)["parallel"]

    assert parallel["max_concurrent"] == 2
    assert len(parallel["batch"]) == 2


def test_a_unit_already_holding_a_live_dispatch_is_not_named_again(tmp_path):
    import advance_kernel as ak

    project, _ = open_project(
        tmp_path / "p", units=[unit("WU-1"), unit("WU-2")]
    )
    first = _next(project)
    ak.execute_dispatch(str(project), first["story_id"], role=first["role"])

    parallel = _next(project)["parallel"]
    running = first["story_id"]
    members = [m["story_id"] for m in parallel["batch"][1:]]
    assert running not in members, (
        "%s already holds a live attempt; recommending it again double-dispatches"
        % running
    )


def test_scrum_is_untouched(tmp_path):
    """The SPQ block must not appear on a lifecycle with no declared scope."""
    import scrum_state_machine as sm

    project = tmp_path / "p"
    project.mkdir()
    (project / ".synaptory.yaml").write_text(
        'build_mode: "scrum"\n', encoding="utf-8"
    )
    sm.initialize(str(project))
    action = sm.next_action(str(project))
    assert action.get("parallel", {}).get("governed_by") is None
