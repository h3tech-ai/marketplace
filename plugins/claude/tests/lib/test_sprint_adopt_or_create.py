"""#793 part 3 — `create_sprint` adopts an existing milestone, or refuses.

A plain POST returned GitHub's 422 whenever the title already existed, which is
exactly what a RE-DELIVERY looks like: the same body of work opened a second
time, naming the tracker cycle it always named. The operator saw a transport
error where the honest answer was "this cycle is already there".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PLUGIN / "skills" / "_shared" / "scripts"))
sys.path.insert(0, str(_PLUGIN / "hooks" / "lib"))

from tracker.base import SprintInfo  # noqa: E402
from tracker.config import GitHubConfig, TrackerConfig  # noqa: E402
from tracker.github_adapter import GitHubAdapter  # noqa: E402

pytestmark = pytest.mark.unit


class _Transport:
    """Records what was POSTed, so 'did not create' is assertable."""

    def __init__(self):
        self.created = []
        self.repo = "h3tech-ai/demo"

    def create_milestone(self, title, description="", due_date=None):
        self.created.append(title)
        return {"number": 7, "html_url": "https://example/7"}


def _adapter(tmp_path, existing):
    cfg = TrackerConfig(backend="github", github=GitHubConfig(repo="h3tech-ai/demo"))
    adapter = GitHubAdapter(tmp_path, cfg)
    adapter.transport = _Transport()
    adapter.list_sprints = lambda: list(existing)
    return adapter


def test_an_absent_milestone_is_created(tmp_path):
    adapter = _adapter(tmp_path, existing=[])
    out = adapter.create_sprint(SprintInfo(number=2, goal="ship the spine"))

    assert adapter.transport.created == [f"{adapter.milestone_prefix}2"]
    assert out.tracker_id == "7"


def test_an_empty_existing_milestone_is_adopted_not_recreated(tmp_path):
    """The re-delivery case: same cycle, opened again."""
    existing = SprintInfo(number=2, goal="old goal", story_ids=[])
    adapter = _adapter(tmp_path, existing=[existing])

    out = adapter.create_sprint(SprintInfo(number=2, goal="ship the spine"))

    assert adapter.transport.created == [], "adopting must not POST"
    assert out is existing
    assert out.goal == "ship the spine", "the fresh goal should win"


def test_a_milestone_already_holding_units_is_refused(tmp_path):
    """Adopting it would merge two Cycles' admitted sets into one backlog."""
    existing = SprintInfo(number=2, goal="someone else", story_ids=["WU-9", "WU-8"])
    adapter = _adapter(tmp_path, existing=[existing])

    with pytest.raises(ValueError) as excinfo:
        adapter.create_sprint(SprintInfo(number=2, goal="ship the spine"))

    message = str(excinfo.value)
    assert "already exists and holds 2 Work Unit(s)" in message
    assert "tracker_ref" in message
    assert adapter.transport.created == []


def test_a_different_number_is_not_adopted(tmp_path):
    existing = SprintInfo(number=3, goal="other cycle", story_ids=[])
    adapter = _adapter(tmp_path, existing=[existing])

    adapter.create_sprint(SprintInfo(number=2, goal="ship the spine"))
    assert adapter.transport.created == [f"{adapter.milestone_prefix}2"]
