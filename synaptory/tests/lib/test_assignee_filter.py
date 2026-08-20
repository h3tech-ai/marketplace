"""Layer 1 — assignee filter: dataclass fields, adapter wiring, CLI flags, config parsing."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2]
    / "skills" / "_shared" / "scripts"
)
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# ── Task 1: Dataclass fields ────────────────────────────────────────────


@pytest.mark.unit
def test_story_has_assignee_field():
    from tracker.base import Story
    s = Story(id="US-001", title="Test", assignee="tai@h3t.co")
    assert s.assignee == "tai@h3t.co"


@pytest.mark.unit
def test_story_assignee_defaults_to_none():
    from tracker.base import Story
    s = Story(id="US-001", title="Test")
    assert s.assignee is None


@pytest.mark.unit
def test_backlog_item_has_assignee_field():
    from tracker.base import BacklogItem
    b = BacklogItem(id="US-001", title="Test", assignee="tai@h3t.co")
    assert b.assignee == "tai@h3t.co"


@pytest.mark.unit
def test_backlog_item_assignee_defaults_to_none():
    from tracker.base import BacklogItem
    b = BacklogItem(id="US-001", title="Test")
    assert b.assignee is None


# ── Task 2: Jira adapter wiring ─────────────────────────────────────────


@pytest.mark.unit
def test_jira_query_tickets_builds_assignee_jql():
    """Jira adapter must add assignee clause to JQL when filter.assignee is set."""
    from tracker.base import QueryFilter
    from tracker.jira_adapter import JiraAdapter

    captured_jql = []

    class FakeTransport:
        url = "https://test.atlassian.net"
        def search_issues(self, jql, **kw):
            captured_jql.append(jql)
            return []

    adapter = JiraAdapter.__new__(JiraAdapter)
    adapter.transport = FakeTransport()
    adapter.project_key = "TEST"
    adapter.spec = None
    adapter._status_map_override = {}
    adapter._jira_issue_types = {}

    qf = QueryFilter(assignee="currentUser()")
    adapter.query_tickets(qf)

    assert len(captured_jql) == 1
    assert "assignee = currentUser()" in captured_jql[0]


@pytest.mark.unit
def test_jira_query_tickets_builds_email_assignee_jql():
    """Jira adapter must quote email-based assignee in JQL."""
    from tracker.base import QueryFilter
    from tracker.jira_adapter import JiraAdapter

    captured_jql = []

    class FakeTransport:
        url = "https://test.atlassian.net"
        def search_issues(self, jql, **kw):
            captured_jql.append(jql)
            return []

    adapter = JiraAdapter.__new__(JiraAdapter)
    adapter.transport = FakeTransport()
    adapter.project_key = "TEST"
    adapter.spec = None
    adapter._status_map_override = {}
    adapter._jira_issue_types = {}

    qf = QueryFilter(assignee="tai@h3t.co")
    adapter.query_tickets(qf)

    assert len(captured_jql) == 1
    assert 'assignee = "tai@h3t.co"' in captured_jql[0]


@pytest.mark.unit
def test_jira_query_tickets_no_assignee_when_none():
    """When assignee is None, no assignee clause in JQL."""
    from tracker.base import QueryFilter
    from tracker.jira_adapter import JiraAdapter

    captured_jql = []

    class FakeTransport:
        url = "https://test.atlassian.net"
        def search_issues(self, jql, **kw):
            captured_jql.append(jql)
            return []

    adapter = JiraAdapter.__new__(JiraAdapter)
    adapter.transport = FakeTransport()
    adapter.project_key = "TEST"
    adapter.spec = None
    adapter._status_map_override = {}
    adapter._jira_issue_types = {}

    qf = QueryFilter()
    adapter.query_tickets(qf)

    assert len(captured_jql) == 1
    assert "assignee" not in captured_jql[0]


@pytest.mark.unit
def test_jira_issue_to_story_populates_assignee():
    """_issue_to_story must extract assignee email from Jira fields."""
    from tracker.jira_adapter import JiraAdapter

    adapter = JiraAdapter.__new__(JiraAdapter)
    adapter.transport = type("T", (), {"url": "https://test.atlassian.net"})()
    adapter.project_key = "TEST"
    adapter.spec = None
    adapter._status_map_override = {}
    adapter._status_reverse_override = {}
    adapter._jira_issue_types = {}
    adapter._id_map_path = Path("/nonexistent")
    adapter._local_cache_path = Path("/nonexistent")
    adapter._sprint_milestone_prefix = ""

    issue = {
        "key": "TEST-1",
        "fields": {
            "summary": "Test story",
            "status": {"name": "To Do"},
            "assignee": {
                "emailAddress": "tai@h3t.co",
                "displayName": "Tai Tran",
            },
        },
    }

    story = adapter._issue_to_story(issue)
    assert story.assignee == "tai@h3t.co"


@pytest.mark.unit
def test_jira_issue_to_story_no_assignee():
    """Unassigned Jira issue must produce Story.assignee=None."""
    from tracker.jira_adapter import JiraAdapter

    adapter = JiraAdapter.__new__(JiraAdapter)
    adapter.transport = type("T", (), {"url": "https://test.atlassian.net"})()
    adapter.project_key = "TEST"
    adapter.spec = None
    adapter._status_map_override = {}
    adapter._status_reverse_override = {}
    adapter._jira_issue_types = {}
    adapter._id_map_path = Path("/nonexistent")
    adapter._local_cache_path = Path("/nonexistent")
    adapter._sprint_milestone_prefix = ""

    issue = {
        "key": "TEST-2",
        "fields": {
            "summary": "Unassigned story",
            "status": {"name": "To Do"},
            "assignee": None,
        },
    }

    story = adapter._issue_to_story(issue)
    assert story.assignee is None


# ── Task 5: Config parsing ──────────────────────────────────────────────


@pytest.mark.unit
def test_tracker_config_parses_scope_mine(tmp_path):
    """tracker.scope: mine must be parsed from .synaptory.yaml."""
    from tracker.config import TrackerConfig

    yaml_content = (
        'version: "3.0"\n'
        'build_mode: scrum\n'
        'tracker:\n'
        '  backend: jira\n'
        '  scope: "mine"\n'
        '  jira:\n'
        '    url: "https://test.atlassian.net"\n'
        '    project_key: "TEST"\n'
    )
    (tmp_path / ".synaptory.yaml").write_text(yaml_content)
    config = TrackerConfig.load(tmp_path)
    assert config.scope == "mine"


@pytest.mark.unit
def test_tracker_config_scope_defaults_to_all(tmp_path):
    """tracker.scope defaults to 'all' when not specified."""
    from tracker.config import TrackerConfig

    yaml_content = (
        'version: "3.0"\n'
        'build_mode: scrum\n'
        'tracker:\n'
        '  backend: local\n'
    )
    (tmp_path / ".synaptory.yaml").write_text(yaml_content)
    config = TrackerConfig.load(tmp_path)
    assert config.scope == "all"


@pytest.mark.unit
def test_tracker_config_scope_invalid_falls_back_to_all(tmp_path):
    """Invalid tracker.scope value falls back to 'all'."""
    from tracker.config import TrackerConfig

    yaml_content = (
        'version: "3.0"\n'
        'build_mode: scrum\n'
        'tracker:\n'
        '  backend: local\n'
        '  scope: "invalid"\n'
    )
    (tmp_path / ".synaptory.yaml").write_text(yaml_content)
    config = TrackerConfig.load(tmp_path)
    assert config.scope == "all"


# ── #187: cross-backend --mine resolution ──────────────────────────────


@pytest.mark.unit
def test_github_query_tickets_resolves_current_user():
    """GitHub adapter must resolve the currentUser() sentinel to the login."""
    from tracker.base import BacklogItem, QueryFilter
    from tracker.github_adapter import GitHubAdapter

    class FakeTransport:
        def current_user(self):
            return "octocat"

    adapter = GitHubAdapter.__new__(GitHubAdapter)
    adapter.transport = FakeTransport()
    adapter.get_backlog = lambda: [  # type: ignore[method-assign]
        BacklogItem(id="US-1", title="a", assignee="octocat"),
        BacklogItem(id="US-2", title="b", assignee="someone-else"),
    ]

    result = adapter.query_tickets(QueryFilter(assignee="currentUser()"))
    assert [i.id for i in result] == ["US-1"]


@pytest.mark.unit
def test_teamwork_query_tickets_resolves_current_user():
    """Teamwork adapter must resolve the currentUser() sentinel to a person id."""
    from tracker.base import BacklogItem, QueryFilter
    from tracker.teamwork_adapter import TeamworkAdapter

    class FakeTransport:
        def current_user(self):
            return "4242"

    adapter = TeamworkAdapter.__new__(TeamworkAdapter)
    adapter.transport = FakeTransport()
    adapter.get_backlog = lambda: [  # type: ignore[method-assign]
        BacklogItem(id="US-1", title="a", assignee="4242"),
        BacklogItem(id="US-2", title="b", assignee="9999"),
    ]

    result = adapter.query_tickets(QueryFilter(assignee="currentUser()"))
    assert [i.id for i in result] == ["US-1"]


@pytest.mark.unit
def test_jira_query_tickets_unresolved_sprint_returns_empty():
    """#187 — an unresolvable sprint must not be silently dropped (which would
    widen `--sprint N --mine` to every ticket assigned to the user)."""
    from tracker.base import QueryFilter
    from tracker.jira_adapter import JiraAdapter

    searched = []

    class FakeTransport:
        url = "https://test.atlassian.net"
        def search_issues(self, jql, **kw):
            searched.append(jql)
            return []

    adapter = JiraAdapter.__new__(JiraAdapter)
    adapter.transport = FakeTransport()
    adapter.project_key = "TEST"
    adapter.spec = None
    adapter._status_map_override = {}
    adapter._jira_issue_types = {}
    adapter._get_sprint_by_number = lambda n: None  # unresolvable

    result = adapter.query_tickets(QueryFilter(sprint=99, assignee="currentUser()"))
    assert result == []
    # And it must NOT have run an unscoped assignee-only query.
    assert searched == []


# ── #187: CLI dispatch seam (real tracker_cli.main() argv path) ─────────


class _FakeConfig:
    def __init__(self, scope="all"):
        self.scope = scope


class _RecordingAdapter:
    """Records the QueryFilter that tracker_cli builds + dispatches."""

    def __init__(self, scope="all", stories=None, backlog=None):
        self.config = _FakeConfig(scope)
        self.captured = []
        self._stories = {s.id: s for s in (stories or [])}
        self._backlog = backlog or []

    def query_tickets(self, qf):
        self.captured.append(qf)
        from tracker.base import BacklogItem
        return [BacklogItem(id=s.id, title=s.title, assignee=s.assignee)
                for s in self._stories.values()]

    def get_story(self, sid):
        return self._stories.get(sid)

    # Non-mine fall-through paths (must not be hit in these tests).
    def get_sprint_backlog(self, n):  # pragma: no cover - guard
        raise AssertionError("scope-default path should have taken query_tickets")

    def list_stories(self, epic_id=None, sprint=None):  # pragma: no cover - guard
        raise AssertionError("--mine path should have taken query_tickets")


def _run_cli(monkeypatch, capsys, adapter, argv):
    import sys as _sys
    from tracker import tracker_cli

    monkeypatch.setattr(tracker_cli, "resolve_spec", lambda pd, sf: None)
    monkeypatch.setattr(tracker_cli, "get_adapter", lambda pd, spec=None: adapter)
    monkeypatch.setattr(
        _sys, "argv", ["tracker_cli.py", "--project-dir", "/x", *argv]
    )
    tracker_cli.main()
    return capsys.readouterr().out


@pytest.mark.unit
def test_cli_query_applies_scope_mine_default(monkeypatch, capsys):
    """`query` with no --mine but tracker.scope: mine must filter to the caller."""
    adapter = _RecordingAdapter(scope="mine")
    _run_cli(monkeypatch, capsys, adapter, ["query"])
    assert len(adapter.captured) == 1
    assert adapter.captured[0].assignee == "currentUser()"


@pytest.mark.unit
def test_cli_query_scope_all_does_not_force_assignee(monkeypatch, capsys):
    """scope: all (default) must leave assignee unset unless a flag is given."""
    adapter = _RecordingAdapter(scope="all")
    _run_cli(monkeypatch, capsys, adapter, ["query"])
    assert adapter.captured[0].assignee is None


@pytest.mark.unit
def test_cli_get_sprint_backlog_applies_scope_mine_default(monkeypatch, capsys):
    """get-sprint-backlog must honour tracker.scope: mine via query_tickets."""
    from tracker.base import Story
    adapter = _RecordingAdapter(
        scope="mine",
        stories=[Story(id="US-1", title="a", assignee="me")],
    )
    _run_cli(monkeypatch, capsys, adapter, ["get-sprint-backlog", "1"])
    assert len(adapter.captured) == 1
    assert adapter.captured[0].assignee == "currentUser()"
    assert adapter.captured[0].sprint == 1


@pytest.mark.unit
def test_cli_list_stories_epic_and_mine_filters_by_epic(monkeypatch, capsys):
    """`list-stories --epic X --mine` must not broaden across every epic."""
    from tracker.base import Story
    adapter = _RecordingAdapter(
        scope="all",
        stories=[
            Story(id="US-1", title="in-epic", epic="EPIC-1", assignee="me"),
            Story(id="US-2", title="other-epic", epic="EPIC-2", assignee="me"),
        ],
    )
    out = _run_cli(
        monkeypatch, capsys, adapter,
        ["list-stories", "--epic", "EPIC-1", "--mine"],
    )
    emitted = json.loads(out)
    ids = [s["id"] for s in emitted]
    assert ids == ["US-1"]
    # The mine path must have run through query_tickets with the sentinel.
    assert adapter.captured[0].assignee == "currentUser()"


@pytest.mark.unit
def test_cli_explicit_assignee_overrides_scope_mine(monkeypatch, capsys):
    """An explicit --assignee must win over the scope: mine default."""
    adapter = _RecordingAdapter(scope="mine")
    _run_cli(monkeypatch, capsys, adapter, ["query", "--assignee", "tai@h3t.co"])
    assert adapter.captured[0].assignee == "tai@h3t.co"
