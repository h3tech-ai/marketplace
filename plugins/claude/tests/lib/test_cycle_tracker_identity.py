"""#793 — a Cycle says which cycle it is in the tracker, instead of guessing.

`cycle_seq` is an allocation counter and was substituted for the tracker's
number. Three ordinary things separate them permanently: a body of work
re-delivered (one tracker cycle, two sequences), Cycles opened concurrently
(sequences land in Commit order), and a Cycle abandoned before close (which
still spends a sequence).

The dangerous half is the read: `get-sprint-backlog <seq>` on a drifted project
SUCCEEDS and returns a different Cycle's Work Units — no error, no warning, a
well-formed wrong answer.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks" / "lib"))

import cycle_records as cr  # noqa: E402
import spq_state_machine as sm  # noqa: E402

from _spq_fixture import open_project, unit  # noqa: E402

pytestmark = pytest.mark.unit


def _yaml(project, body):
    (project / ".synaptory.yaml").write_text(body, encoding="utf-8")


# ── which backend needs a declared identity ──────────────────────────────────

def test_local_tracker_needs_no_declared_identity(tmp_path):
    """`local` numbers are Synaptory's own, so there is no second namespace
    for the two to drift apart in."""
    project, opened = open_project(tmp_path / "p", units=[unit("WU-1")])
    assert opened["cycle_id"]


@pytest.mark.parametrize("backend", sm.MIRRORED_TRACKER_BACKENDS)
def test_a_mirrored_tracker_refuses_a_cycle_with_no_tracker_ref(tmp_path, backend):
    """A warning would not close this: the failure it prevents is a read that
    SUCCEEDS, so anything the orchestrator can continue past leaves the wrong
    answer looking right."""
    project = tmp_path / backend
    project.mkdir()
    _yaml(project, 'build_mode: "spq"\ntracker:\n  backend: "%s"\n' % backend)

    with pytest.raises(cr.DeclarationError) as excinfo:
        open_project(project, units=[unit("WU-1")])

    message = str(excinfo.value)
    assert "tracker_ref is required" in message
    assert "--tracker-ref" in message
    # It has to say WHY the counter is not the number.
    assert "re-delivered" in message


def test_a_mirrored_tracker_seals_when_the_identity_is_declared(tmp_path):
    project = tmp_path / "gh"
    project.mkdir()
    _yaml(project, 'build_mode: "spq"\ntracker:\n  backend: "github"\n')

    project, opened = open_project(
        project, units=[unit("WU-1")], tracker_ref="2"
    )
    declaration = sm.read_manifest(str(project), opened["cycle_id"])
    assert declaration["tracker_ref"] == "2"


def test_the_backend_reader_defaults_to_local(tmp_path):
    project = tmp_path / "bare"
    project.mkdir()
    assert sm.tracker_backend(str(project)) == "local"


# ── what the tracker flows read ──────────────────────────────────────────────

def test_a_declared_ref_is_used_and_says_nothing():
    answer = sm.cycle_tracker_ref({"tracker_ref": "2", "cycle_seq": 3})
    assert answer == {"ref": "2", "declared": True, "warning": ""}


def test_an_undeclared_ref_falls_back_but_warns():
    """Legacy Cycles and `local` projects reach this; silence is what #793 is."""
    answer = sm.cycle_tracker_ref({"cycle_seq": 3})

    assert answer["ref"] == "3"
    assert answer["declared"] is False
    assert "re-delivered" in answer["warning"]
    assert "cycle_seq=3" in answer["warning"]


def test_the_sequence_remains_synaptory_s_own_identity():
    """`CYCLE-{seq}` is the pseudo Work Unit id receipts bind to. Replacing it
    with the tracker ref would move every receipt path — the bug this fix must
    not introduce while fixing the other one."""
    assert sm.PSEUDO_UNIT_IDS["CYCLE"] == "CYCLE-{n}"
