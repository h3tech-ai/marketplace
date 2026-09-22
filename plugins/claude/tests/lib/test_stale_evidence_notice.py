"""#816 — a receipt older than the tree it describes says so.

`build_succeeds` is sourced from the SE receipt and never compared against the
tree. After a repair made without an SE dispatch, that receipt describes code
that no longer exists — and the gate credited it silently. On the engagement
that filed this, one receipt predated two repairs and passed every evaluation
in between. It happened to be true; the gate had no way to know that.

Reports rather than refuses: a repair that legitimately needs no rebuild is
ordinary, and failing on it would strand work the gate has no reason to doubt.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks" / "lib"))

import story_pipeline as sp  # noqa: E402

pytestmark = pytest.mark.unit


def _stamp(delta_seconds: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture()
def unit_tree(tmp_path):
    (tmp_path / "api" / "wu-1").mkdir(parents=True)
    (tmp_path / "api" / "wu-1" / "impl.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


def test_a_receipt_older_than_the_scope_is_flagged(unit_tree):
    """The measured shape: a repair landed after the last SE receipt."""
    story = {"id": "WU-1", "path_scope": ["api/wu-1/"]}
    stale = sp._receipt_predates_scope(
        str(unit_tree), story, {"completed_at": _stamp(-3600)}
    )

    assert stale is not None
    assert stale["newest_path"] == os.path.join("api", "wu-1", "impl.py")
    assert "has since moved" in stale["summary"]


def test_a_receipt_newer_than_the_scope_is_not_flagged(unit_tree):
    story = {"id": "WU-1", "path_scope": ["api/wu-1/"]}
    assert sp._receipt_predates_scope(
        str(unit_tree), story, {"completed_at": _stamp(60)}
    ) is None


def test_it_asks_about_the_declared_scope_not_the_whole_tree(unit_tree):
    """A unit is answerable for its region, not for every file in the repo."""
    (unit_tree / "unrelated").mkdir()
    time.sleep(0.01)
    (unit_tree / "unrelated" / "other.py").write_text("y = 2\n", encoding="utf-8")

    story = {"id": "WU-1", "path_scope": ["api/wu-1/"]}
    stale = sp._receipt_predates_scope(
        str(unit_tree), story, {"completed_at": _stamp(60)}
    )
    assert stale is None, "a file outside the declared scope must not flag the unit"


@pytest.mark.parametrize("story", [
    {"id": "WU-1"},                       # no scope declared
    {"id": "WU-1", "path_scope": []},     # empty scope
])
def test_no_declared_scope_makes_no_claim(unit_tree, story):
    assert sp._receipt_predates_scope(
        str(unit_tree), story, {"completed_at": _stamp(-3600)}
    ) is None


@pytest.mark.parametrize("receipt", [None, {}, {"completed_at": "not-a-date"}])
def test_an_unreadable_receipt_makes_no_claim(unit_tree, receipt):
    story = {"id": "WU-1", "path_scope": ["api/wu-1/"]}
    assert sp._receipt_predates_scope(str(unit_tree), story, receipt) is None


def test_a_missing_scope_directory_makes_no_claim(tmp_path):
    story = {"id": "WU-1", "path_scope": ["api/absent/"]}
    assert sp._receipt_predates_scope(
        str(tmp_path), story, {"completed_at": _stamp(-3600)}
    ) is None
