"""Layer 1 — TeamworkTransport.list_tasks milestone filter (#122).

The previous implementation filtered client-side by reading
`task.milestoneId`, which v3 doesn't return on task objects — so
`get-sprint-backlog` returned `[]` for any project organising sprints
as milestones. These tests pin the server-side `milestoneIds=` filter
contract and the absence of any client-side filtering.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2]
    / "skills" / "_shared" / "scripts"
)
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


@pytest.fixture
def transport():
    """A TeamworkTransport with __init__ bypassed so we don't need creds.

    The tests only exercise `list_tasks` via a patched `_api_call`, so
    we can skip session setup.
    """
    from tracker.transport.teamwork_transport import TeamworkTransport
    t = object.__new__(TeamworkTransport)
    t.site_name = "h3tech-test"
    t.project_id = 1598948
    return t


# ---------------------------------------------------------------------------
# #122 — milestone filter must travel server-side via `milestoneIds=`
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_milestone_id_sent_as_milestoneIds_query_param(transport, monkeypatch):
    """The whole point of #122 — milestone_id reaches the wire as
    `milestoneIds=<id>`, not as a client-side post-fetch filter."""
    captured = {}

    def fake_api_call(self, method, path, body=None, params=None):
        captured["method"] = method
        captured["path"] = path
        captured["params"] = dict(params or {})
        # Real v3 response shape: tasks have NO `milestoneId` field; just
        # the `dueDateFromMilestone: bool` discriminator. Return a small
        # page so pagination doesn't recurse.
        return {"tasks": [
            {"id": 39937184, "name": "US-018", "dueDateFromMilestone": True},
            {"id": 39937185, "name": "US-019", "dueDateFromMilestone": True},
        ]}

    monkeypatch.setattr(
        type(transport), "_api_call", fake_api_call, raising=True,
    )
    tasks = transport.list_tasks(milestone_id=459678)

    assert captured["method"] == "GET"
    assert captured["path"] == "/tasks.json"
    assert captured["params"]["milestoneIds"] == "459678", (
        f"milestone_id must be sent as milestoneIds query param, "
        f"got params: {captured['params']}"
    )
    # No client-side filter: every task returned by the API is included
    # in the result. The bug was that the post-fetch filter dropped them
    # all because `task.milestoneId` was always None.
    assert len(tasks) == 2
    assert {t["id"] for t in tasks} == {39937184, 39937185}


@pytest.mark.unit
def test_no_milestone_id_means_no_milestoneIds_param(transport, monkeypatch):
    """Omitting the filter must not inject milestoneIds=None or similar."""
    captured = {}

    def fake_api_call(self, method, path, body=None, params=None):
        captured["params"] = dict(params or {})
        return {"tasks": []}

    monkeypatch.setattr(
        type(transport), "_api_call", fake_api_call, raising=True,
    )
    transport.list_tasks()

    assert "milestoneIds" not in captured["params"]


@pytest.mark.unit
def test_v3_tasks_without_milestoneId_field_are_not_dropped(transport, monkeypatch):
    """Pin the #122 fix: tasks lacking `milestoneId` in the response (the
    real v3 shape) must NOT be filtered out client-side."""
    def fake_api_call(self, method, path, body=None, params=None):
        return {"tasks": [
            # Realistic v3 task — no `milestoneId`, no `milestone` object.
            {"id": 1, "name": "US-A", "dueDateFromMilestone": True},
            {"id": 2, "name": "US-B", "dueDateFromMilestone": True},
            {"id": 3, "name": "US-C", "dueDateFromMilestone": True},
        ]}

    monkeypatch.setattr(
        type(transport), "_api_call", fake_api_call, raising=True,
    )
    tasks = transport.list_tasks(milestone_id=459678)
    assert len(tasks) == 3, (
        "Server-side filter trusts the API. Tasks lacking client-side "
        "milestone metadata must still come through (#122 root cause)."
    )


@pytest.mark.unit
def test_other_filters_still_compose(transport, monkeypatch):
    """tag_ids + milestone_id + project_id must all reach params together."""
    captured = {}

    def fake_api_call(self, method, path, body=None, params=None):
        captured["params"] = dict(params or {})
        return {"tasks": []}

    monkeypatch.setattr(
        type(transport), "_api_call", fake_api_call, raising=True,
    )
    transport.list_tasks(
        project_id=1598948,
        tag_ids=[10, 20, 30],
        milestone_id=459678,
        include_completed=False,
    )

    p = captured["params"]
    assert p["projectIds"] == "1598948"
    assert p["tagIds"] == "10,20,30"
    assert p["milestoneIds"] == "459678"
    assert p["includeCompletedTasks"] == "false"
