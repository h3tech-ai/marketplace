"""#799 — the operator's own number reaches code.

`routing-rules.json` advertises `open|start|run|next cycle N`, so an operator
types `/synaptory start cycle 2`. Nothing could act on that `2`: it is not the
sequence, and before #793 no field mapped it to one. The only identifier the
operator knows was the one the method could not accept.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks" / "lib"))

import spq_state_machine as sm  # noqa: E402

from _spq_fixture import open_project, unit  # noqa: E402

pytestmark = pytest.mark.unit


def _github(tmp_path, name):
    project = tmp_path / name
    project.mkdir()
    (project / ".synaptory.yaml").write_text(
        'build_mode: "spq"\ntracker:\n  backend: "github"\n', encoding="utf-8"
    )
    return project


def test_the_operators_number_resolves_through_tracker_ref(tmp_path):
    project, opened = open_project(
        _github(tmp_path, "p"), units=[unit("WU-1")], tracker_ref="2"
    )
    answer = sm.resolve_cycle_number(str(project), "2")

    assert answer["resolved"] is True
    assert answer["cycle_id"] == opened["cycle_id"]
    assert answer["match"] == "tracker_ref"


def test_a_sequence_match_resolves_but_says_it_was_a_fallback(tmp_path):
    """Legacy and `local` projects: usable, but the operator is told the two
    numbers can differ."""
    project, opened = open_project(tmp_path / "local", units=[unit("WU-1")])
    seq = sm.list_cycles(str(project))["cycles"][0]["cycle_seq"]

    answer = sm.resolve_cycle_number(str(project), str(seq))
    assert answer["resolved"] is True
    assert answer["match"] == "cycle_seq"
    assert "re-delivered" in answer["message"]


def test_an_unknown_number_does_not_resolve(tmp_path):
    project, _ = open_project(tmp_path / "p", units=[unit("WU-1")])
    answer = sm.resolve_cycle_number(str(project), "99")

    assert answer["resolved"] is False
    assert "no Cycle" in answer["message"]


def test_an_empty_number_returns_the_choices_rather_than_guessing(tmp_path):
    project, _ = open_project(tmp_path / "p", units=[unit("WU-1")])
    answer = sm.resolve_cycle_number(str(project), "")

    assert answer["resolved"] is False
    assert len(answer["candidates"]) == 1


def test_tracker_ref_beats_a_sequence_that_happens_to_match(tmp_path):
    """Matching the sequence first would resolve CONFIDENTLY to the wrong
    Cycle, which is the failure #793 and #799 share."""
    project = _github(tmp_path, "p")
    project, first = open_project(project, units=[unit("WU-1")], tracker_ref="7")
    seq = sm.list_cycles(str(project))["cycles"][0]["cycle_seq"]

    # The first Cycle's own sequence must not shadow its declared ref.
    assert sm.resolve_cycle_number(str(project), "7")["cycle_id"] == first["cycle_id"]
    assert sm.resolve_cycle_number(str(project), str(seq))["match"] == "cycle_seq"
