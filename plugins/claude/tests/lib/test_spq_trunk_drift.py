"""#807 S6 — the trunk moving under an open Cycle is observable, not a surprise.

The reporting engagement lost roughly three hours and a review round to a PR
that merged to the trunk while a Cycle was open against it and edited a file
one of the Cycle's tests pins byte-for-byte. The open Cycle inherited it
through an ordinary `git merge <trunk>`, and it surfaced only when the reviewer
ran the regression -- attributed, at first, to the unit under review.

The barrier already observes the trunk, but it runs at close, after every
dispatch that could have carried the warning.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks" / "lib"))

from _spq_fixture import open_project, unit  # noqa: E402


def _git(project, *args):
    return subprocess.run(
        ["git", *args], cwd=str(project), capture_output=True, text=True, check=True
    )


@pytest.fixture()
def repo(tmp_path):
    """A real checkout, because the whole subject is what git answers."""
    project = tmp_path / "p"
    project.mkdir()
    _git(project, "init", "-q", "-b", "trunk")
    _git(project, "config", "user.email", "t@h3t.co")
    _git(project, "config", "user.name", "T")
    (project / "seed.txt").write_text("one\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "seed")
    return project


def _open(repo, **over):
    return open_project(
        repo, units=[unit("WU-1")], trunk_ref="trunk", **over
    )


def test_commit_seals_the_sha_the_trunk_named(repo):
    import spq_state_machine as sm

    project, opened = _open(repo)
    declaration = sm.read_manifest(str(project), opened["cycle_id"])
    head = _git(project, "rev-parse", "trunk").stdout.strip()

    assert declaration["trunk_sha_at_commit"] == head


def test_an_unmoved_trunk_does_not_drift(repo):
    import spq_state_machine as sm

    project, opened = _open(repo)
    declaration = sm.read_manifest(str(project), opened["cycle_id"])

    drift = sm.trunk_drift(str(project), declaration)
    assert drift["drifted"] is False
    assert sm.next_action(str(project)).get("trunk_drift") is None


def test_a_moved_trunk_drifts_and_says_by_how_much(repo):
    import spq_state_machine as sm

    project, opened = _open(repo)
    declaration = sm.read_manifest(str(project), opened["cycle_id"])
    (project / "someone-elses.txt").write_text("merged meanwhile\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "another PR lands on trunk")

    drift = sm.trunk_drift(str(project), declaration)
    assert drift["drifted"] is True
    assert drift["commits_ahead"] == 1
    assert drift["sealed_sha"] != drift["current_sha"]
    assert "is NOT this unit's change" in drift["notice"]


def test_the_notice_reaches_the_dispatch_that_precedes_the_regression(repo):
    import spq_state_machine as sm

    project, _ = _open(repo)
    (project / "someone-elses.txt").write_text("merged meanwhile\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "another PR lands on trunk")

    action = sm.next_action(str(project))
    assert action["trunk_drift"]["drifted"] is True


def test_an_unresolvable_trunk_is_unknown_rather_than_unchanged(repo):
    """Reporting False would claim a check that did not run."""
    import spq_state_machine as sm

    project, opened = _open(repo)
    declaration = dict(sm.read_manifest(str(project), opened["cycle_id"]))
    declaration["trunk_ref"] = "refs/heads/nonexistent"

    assert sm.trunk_drift(str(project), declaration)["drifted"] is None


def test_a_cycle_sealed_before_this_change_makes_no_claim(repo):
    """An older declaration is not retro-fitted with a seal it never made."""
    import spq_state_machine as sm

    project, opened = _open(repo)
    declaration = dict(sm.read_manifest(str(project), opened["cycle_id"]))
    declaration.pop("trunk_sha_at_commit")

    assert sm.trunk_drift(str(project), declaration) is None
