"""Layer 1 — which dispatches the board's in-flight work can speak for.

`active_dispatch_for` falls back to the board when a role holds no dispatch
binding, and that fallback reads `in_progress` / `testing` / `reviewing` as
"this role's assignment". Those states are held by SE, QE and CR. Applying
them to every role made the number of in-flight STORIES answer a question
about a role that is not on the story pipeline: one in flight assigned a
Project Owner stop to that story, two called it ambiguous — and selection
refuses under ambiguity, so the SubagentStop hook reported `receipt_missing`
and blocked the session over a receipt that was on disk under its canonical
name. Sprint planning dispatches the PO onto a board of several in-flight
stories, so it was the ordinary path, not an edge.

Scoping it to the pipeline roles left the same defect one level in: the map
names a HOLDER per state, and the candidate list read only its keys. So an SE
story in `in_progress` made a QE stop on the one story in `testing`
`?ambiguous`, and that board is the ordinary pipeline rather than an overlap.
Each role now reads only the states it holds.

The ambiguity itself is untouched and still load-bearing for the roles it was
written for: two overlapping QE agents cannot be told apart, and #396 chose to
refuse rather than let one answer for the other. That is two stories in the
SAME state, not two stories in flight.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import advance_kernel as ak
import dispatched_receipts as dr


def _board(project_dir: Path, stories: list[dict]) -> None:
    orch = project_dir / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True, exist_ok=True)
    (orch / "pipeline-state.json").write_text(
        json.dumps({"build_mode": "scrum", "current_stories": stories}),
        encoding="utf-8",
    )


def _in_flight(*ids: str, state: str = "in_progress") -> list[dict]:
    return [{"id": sid, "state": state} for sid in ids]


def test_the_in_flight_holders_match_the_kernels_own_table(tmp_path):
    """The duplicated map is the kernel's, for the three states it covers.

    `_IN_FLIGHT_HOLDER` is copied rather than imported so this module stays
    loadable on a partial install. A comment asking the next person to keep
    them in step would not survive one refactor of either side.
    """
    assert dr._IN_FLIGHT_HOLDER == {
        state: abbrev
        for state, (_role, abbrev) in ak.BLOCKED_FROM_ROLE.items()
        if state in dr._IN_FLIGHT_HOLDER
    }
    # And the states really are the ones the fallback treats as in flight, so
    # adding a stage to the kernel without deciding about this one is visible.
    assert set(dr._IN_FLIGHT_HOLDER) == {"in_progress", "testing", "reviewing"}


@pytest.mark.parametrize("count", [1, 2, 3])
def test_an_off_pipeline_role_is_unaffected_by_in_flight_stories(tmp_path, count):
    """A PO stop resolves by declaration whatever the board is busy with.

    Parametrized over the counts on purpose: the defect was that the ANSWER
    changed with the count — assigned at one, ambiguous at two — for a role
    the count says nothing about.
    """
    _board(tmp_path, _in_flight(*["US-%03d" % n for n in range(1, count + 1)]))
    assert dr.active_dispatch_for(str(tmp_path), "project-owner") == ("", "")


@pytest.mark.parametrize(
    "role,state",
    [
        ("software-engineer", "in_progress"),
        ("quality-engineer", "testing"),
        ("code-reviewer", "reviewing"),
    ],
)
def test_a_pipeline_role_still_takes_one_story_in_the_state_it_holds(
    tmp_path, role, state
):
    """The case the fallback exists for, per role and per state it holds."""
    _board(tmp_path, _in_flight("US-001", state=state))
    assert dr.active_dispatch_for(str(tmp_path), role) == ("US-001", "")


@pytest.mark.parametrize(
    "role,state",
    [
        ("software-engineer", "in_progress"),
        ("quality-engineer", "testing"),
        ("code-reviewer", "reviewing"),
    ],
)
def test_a_pipeline_role_still_records_ambiguity_on_two_stories_it_holds(
    tmp_path, role, state
):
    """#396's refusal, which this change must not weaken.

    Two stories in the SAME state is the parallel case: the role holds both
    and nothing the host gives SubagentStop tells them apart.
    """
    _board(tmp_path, _in_flight("US-001", "US-002", state=state))
    assert dr.active_dispatch_for(str(tmp_path), role) == (dr._AMBIGUOUS, "")


@pytest.mark.parametrize(
    "role,expected",
    [
        ("software-engineer", "US-SE"),
        ("quality-engineer", "US-QE"),
        ("code-reviewer", "US-CR"),
    ],
)
def test_a_pipeline_role_ignores_stories_another_role_holds(tmp_path, role, expected):
    """The ordinary pipeline board, which is not an ambiguous one.

    SE building one story while QE verifies a second and CR reviews a third
    is what the per-story pipeline is FOR. Reading the count across all three
    states made each of those stops `?ambiguous`, so selection returned no
    receipt and the SubagentStop hook reported `receipt_missing` against a
    receipt sitting under its canonical name.
    """
    _board(tmp_path, [
        {"id": "US-SE", "state": "in_progress"},
        {"id": "US-QE", "state": "testing"},
        {"id": "US-CR", "state": "reviewing"},
    ])
    assert dr.active_dispatch_for(str(tmp_path), role) == (expected, "")


@pytest.mark.parametrize("role", ["quality-engineer", "code-reviewer"])
def test_a_pipeline_role_holding_nothing_resolves_by_declaration(tmp_path, role):
    """Busy elsewhere is not this role's assignment.

    Both stories sit in `in_progress`, which SE holds. QE and CR hold nothing
    here, so there is no assignment to fall back to and ("", "") hands the
    question to the receipt's own declared id, which is the correct answer.
    Reading the count across all three states called this `?ambiguous` and
    made selection refuse instead.
    """
    _board(tmp_path, _in_flight("US-001", "US-002"))
    assert dr.active_dispatch_for(str(tmp_path), role) == ("", "")


def test_an_off_pipeline_role_still_honours_a_real_binding(tmp_path):
    """Scoping the FALLBACK does not scope the binding.

    A dispatch the kernel actually recorded for `po` is an assignment however
    off-pipeline the role is, and it must keep resolving.
    """
    _board(tmp_path, [
        {"id": "US-001", "state": "in_progress"},
        {
            "id": "US-002",
            "state": "reviewing",
            "mcp_active_dispatches": {
                "po": {"dispatch_id": "d" * 32, "started_at": "2026-09-01T09:00:00Z"}
            },
        },
    ])
    assert dr.active_dispatch_for(str(tmp_path), "project-owner") == (
        "US-002",
        "d" * 32,
    )
