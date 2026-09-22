"""#805 — acceptance criteria are credited from evidence, not from three booleans.

`_unit_results` built its per-criterion map with `checks.get(criterion)`, where
`checks` is keyed by DoD check id (`tests_pass`, …) and `criterion` is criterion
prose. The keys could never intersect, so the branch was dead and every
criterion of every `done` unit fell to a blanket credit from the DoD gate.

Measured consequence: a QE closed two findings, filed two new HIGH ones,
recorded `verdict.unit_criteria_met: false`, and warned in its own receipt that
the gate might not read it. Nothing did — `grep -rn unit_criteria_met` returned
zero readers. All six of that unit's criteria would have been credited passed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks" / "lib"))

import spq_state_machine as sm  # noqa: E402

from _spq_fixture import open_project, unit  # noqa: E402

pytestmark = pytest.mark.unit

CRIT_A = "A result arriving after cancellation is not adopted"
CRIT_B = "One component launches, observes and recovers"


def _project(tmp_path, **over):
    return open_project(
        tmp_path / "p",
        units=[unit("WU-1", acceptance_criteria=[CRIT_A, CRIT_B], **over)],
    )


def _write_receipt(project, cycle_id, role, payload):
    import spq_paths

    d = Path(spq_paths.receipts_dir(str(project), cycle_id))
    d.mkdir(parents=True, exist_ok=True)
    body = {"story_id": "WU-1", "role": role, "agent": role, "backend": "claude"}
    body.update(payload)
    (d / f"WU-1-{role}.json").write_text(json.dumps(body), encoding="utf-8")


def _criteria(project, cycle_id, monkeypatch=None):
    """The unit's per-criterion map, with the unit marked `done`.

    `monkeypatch` neutralises the DoD gate's block reason. Without it a unit
    with no full receipt set blocks, the derivation branch never runs, and the
    map is empty — which would make these tests about receipt scaffolding
    rather than about WHICH branch credits a criterion.
    """
    if monkeypatch is not None:
        monkeypatch.setattr(sm, "dod_gate_block_reason", lambda _d: None)
    state = sm.read_state(str(project), cycle_id=cycle_id)
    for story in state.get("current_stories") or []:
        if str(story.get("id")) == "WU-1":
            story["state"] = "done"
    return sm._unit_results(str(project), state)["WU-1"]["acceptance_criteria"]


def test_an_explicit_criteria_refusal_is_read(tmp_path):
    """The field the QE protocol produces and nothing consumed."""
    project, opened = _project(tmp_path)
    _write_receipt(
        project, opened["cycle_id"], "qe",
        {"verdict": {"unit_criteria_met": False,
                     "summary": "two open high findings"}},
    )
    criteria = _criteria(project, opened["cycle_id"])

    assert criteria[CRIT_A]["passed"] is False
    assert criteria[CRIT_B]["passed"] is False
    assert "unit_criteria_met" in criteria[CRIT_A]["detail"]
    assert "two open high findings" in criteria[CRIT_A]["detail"]


def test_a_producer_cannot_speak_for_its_own_criteria(tmp_path, monkeypatch):
    """An SE asserting its own work met the criteria is self-attestation."""
    project, opened = _project(tmp_path)
    _write_receipt(
        project, opened["cycle_id"], "se",
        {"verdict": {"unit_criteria_met": False, "summary": "ignored"}},
    )
    criteria = _criteria(project, opened["cycle_id"], monkeypatch)

    # Not refused by the builder's own verdict — falls back to the derivation.
    assert criteria[CRIT_A]["passed"] is True


def test_the_blanket_credit_now_says_it_read_no_per_criterion_evidence(tmp_path, monkeypatch):
    """#805's core complaint: it read like an itemised result and was not."""
    project, opened = _project(tmp_path)
    criteria = _criteria(project, opened["cycle_id"], monkeypatch)

    assert criteria[CRIT_A]["passed"] is True
    assert criteria[CRIT_A]["evidence"] == "derived"
    assert "NO per-criterion evidence was read" in criteria[CRIT_A]["detail"]


def test_absent_or_true_verdicts_change_nothing(tmp_path, monkeypatch):
    """Only an explicit `false` refuses — #817's lesson about stranding."""
    project, opened = _project(tmp_path)
    _write_receipt(
        project, opened["cycle_id"], "qe",
        {"verdict": {"unit_criteria_met": True}},
    )
    assert _criteria(project, opened["cycle_id"], monkeypatch)[CRIT_A]["passed"] is True


def test_a_malformed_verdict_refuses_nothing(tmp_path, monkeypatch):
    project, opened = _project(tmp_path)
    _write_receipt(project, opened["cycle_id"], "qe", {"verdict": "not-a-dict"})
    assert _criteria(project, opened["cycle_id"], monkeypatch)[CRIT_A]["passed"] is True


def test_the_dead_branch_is_gone(tmp_path, monkeypatch):
    """A DoD check id must never be mistaken for a criterion.

    The old code looked criteria up in the DoD `checks` map; a unit whose
    criterion happened to be spelled `tests_pass` would have picked up that
    check's verdict, which is the confusion the dead branch encoded.
    """
    project, opened = open_project(
        tmp_path / "p2", units=[unit("WU-1", acceptance_criteria=["tests_pass"])]
    )
    criteria = _criteria(project, opened["cycle_id"], monkeypatch)

    # Credited as derived, NOT as the DoD check of the same name.
    assert criteria["tests_pass"]["evidence"] == "derived"
