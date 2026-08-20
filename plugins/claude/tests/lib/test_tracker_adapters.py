"""Layer 1 — regression tests for tracker adapter null-safety and bootstrap.

Pins the two fixes from #108:
  * `_get_task_tag_names` must handle `tags: None` (Teamwork v3 returns
    null, not [], for never-tagged tasks).
  * `sync()` must auto-bootstrap a missing local cache instead of erroring
    out with "run migrate first" — the first sync in a fresh workspace
    pulls from the tracker, no migrate needed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Adapters use relative imports (`from .base import …`), so they must be
# imported through the `tracker` package. Add the scripts/ dir (parent of
# the package) to sys.path.
_SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2]
    / "skills" / "_shared" / "scripts"
)
_TRACKER_DIR = _SCRIPTS_DIR / "tracker"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


@pytest.mark.unit
def test_clean_yaml_scalar_comment_and_quote_handling():
    """#51 follow-up — the YAML-lite scalar cleaner strips a real inline
    comment (after whitespace) and surrounding quotes, but must NOT truncate a
    `#` that is part of an unquoted value or lives inside quotes."""
    from tracker.config import _clean_yaml_scalar

    # Real inline comment (preceded by whitespace) is stripped.
    assert _clean_yaml_scalar("local   # default backend") == "local"
    assert _clean_yaml_scalar("local # c") == "local"
    # Surrounding quotes stripped.
    assert _clean_yaml_scalar('"local"') == "local"
    assert _clean_yaml_scalar("'local'") == "local"
    # A '#' inside an unquoted scalar (no preceding space) is preserved.
    assert _clean_yaml_scalar("pass#1") == "pass#1"
    assert _clean_yaml_scalar("https://h/x#frag") == "https://h/x#frag"
    # A '#' inside quotes is preserved; quotes then stripped.
    assert _clean_yaml_scalar('"p#ss word"') == "p#ss word"
    # A whole-line comment yields empty.
    assert _clean_yaml_scalar("# just a comment") == ""


@pytest.mark.unit
def test_get_task_tag_names_handles_null_tags():
    """#108 Bug 1 — `tags: null` must not raise (Teamwork v3 contract)."""
    from tracker.teamwork_adapter import TeamworkAdapter

    assert TeamworkAdapter._get_task_tag_names({"tags": None}) == []
    assert TeamworkAdapter._get_task_tag_names({}) == []
    assert TeamworkAdapter._get_task_tag_names({"tags": []}) == []
    assert TeamworkAdapter._get_task_tag_names(
        {"tags": [{"name": "phi"}, "release"]}
    ) == ["phi", "release"]


@pytest.mark.unit
def test_all_tag_lookups_use_null_safe_idiom():
    """#108 Bug 1 — pin that all four call sites use `.get("tags") or []`.

    Mechanical sanity check: the unsafe form `.get("tags", [])` masks the
    `tags: null` bug and the reporter's stack trace originated from that.
    """
    src = (_TRACKER_DIR / "teamwork_adapter.py").read_text(encoding="utf-8")
    assert 'task.get("tags", [])' not in src, (
        "found unsafe `task.get(\"tags\", [])` — must be `task.get(\"tags\") or []` "
        "to handle the explicit-null case (#108)"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "adapter_module",
    ["teamwork_adapter", "github_adapter", "jira_adapter", "linear_adapter"],
)
def test_sync_does_not_error_on_missing_cache(adapter_module: str):
    """#108 Bug 2 — `sync` must NOT raise "run migrate first" on a fresh workspace."""
    __import__(f"tracker.{adapter_module}")
    src = (_TRACKER_DIR / f"{adapter_module}.py").read_text(encoding="utf-8")
    assert "run migrate first" not in src, (
        f"{adapter_module}: stale error path still present; sync should "
        f"bootstrap an empty cache on first run (#108)"
    )
    # The bootstrap writes an empty skeleton inline before reading.
    assert '"epics": []' in src and '"stories": []' in src, (
        f"{adapter_module}: expected empty-cache bootstrap with epics/stories keys"
    )


@pytest.mark.unit
def test_sync_bootstrap_creates_cache_file(tmp_path: Path):
    """#108 Bug 2 — exercise the bootstrap path end-to-end on the Teamwork adapter.

    Bypass __init__ to skip the live transport handshake; inject only the
    attributes `sync` reads.
    """
    from tracker.teamwork_adapter import TeamworkAdapter

    project_dir = tmp_path / "proj"
    cache = project_dir / ".synaptory" / ".orchestrator" / "tracker-data.json"
    assert not cache.exists()

    adapter = object.__new__(TeamworkAdapter)
    adapter._local_cache_path = cache
    # `sync_to_local_cache` calls list_stories() (no sprint_num) after the
    # bootstrap; stub it to return nothing so the test stays hermetic.
    adapter.list_stories = lambda: []  # type: ignore[method-assign]

    stats = adapter.sync_to_local_cache()

    assert cache.exists(), "sync must bootstrap a missing cache, not error out"
    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert payload == {"epics": [], "sprints": [], "stories": []}
    assert stats["errors"] == [], f"unexpected errors after bootstrap: {stats['errors']}"


@pytest.mark.unit
def test_local_adapter_hydrates_story_from_id_named_markdown(tmp_path: Path):
    """#51 — local JSON is compact state; story content comes from US-ID.md."""
    from tracker.local_adapter import LocalAdapter
    from tracker.config import TrackerConfig

    project = tmp_path / "proj"
    tracker_dir = project / ".synaptory" / ".orchestrator"
    story_dir = project / "docs" / "requirements" / "stories"
    tracker_dir.mkdir(parents=True)
    story_dir.mkdir(parents=True)
    (tracker_dir / "tracker-data.json").write_text(
        json.dumps({
            "meta": {"schema_version": 2, "content_source": "markdown"},
            "epics": [],
            "sprints": [],
            "stories": [{"id": "US-005", "status": "DONE", "sprint": "1"}],
        }),
        encoding="utf-8",
    )
    (story_dir / "US-005.md").write_text(
        "\n".join([
            "# US-005: Invite members",
            "",
            "> **Status:** TODO · **Epic:** EPIC-002 · **Feature:** Membership",
            "> **Priority:** Must · **Size:** L · **Sprint:** 1 · **Blocked by:** US-004",
            "",
            "## Acceptance Criteria",
            "* [ ] **AC-01:** Given a Leader, When they invite a user, Then membership is active.",
            "* [x] **AC-02:** Given an uninvited user, When they access, Then access is denied.",
            "",
        ]),
        encoding="utf-8",
    )

    story = LocalAdapter(project, TrackerConfig()).get_story("US-005")

    assert story is not None
    assert story.title == "Invite members"
    assert story.status == "DONE"
    assert story.epic == "EPIC-002"
    assert story.feature == "Membership"
    assert story.priority == "Must"
    assert story.size == "L"
    assert story.blocked_by == "US-004"
    assert story.file_path == "docs/requirements/stories/US-005.md"
    assert "## Acceptance Criteria" in story.raw_text
    assert [(ac.id, ac.met) for ac in story.acceptance_criteria] == [
        ("AC-01", False),
        ("AC-02", True),
    ]


@pytest.mark.unit
def test_local_adapter_create_ticket_writes_markdown_and_compact_json(tmp_path: Path):
    """#51 — new local stories are ID-named Markdown plus small JSON state."""
    from tracker.local_adapter import LocalAdapter
    from tracker.config import TrackerConfig
    from tracker.base import AcceptanceCriterion, Story

    project = tmp_path / "proj"
    adapter = LocalAdapter(project, TrackerConfig())
    story = Story(
        id="US-010",
        title="Canonical fixture list",
        epic="EPIC-003",
        feature="Match Management",
        priority="Must",
        status="TO_DO",
        size="M",
        sprint="1",
        acceptance_criteria=[
            AcceptanceCriterion(id="AC-01", text="Given fixtures, When synced, Then they are canonical.")
        ],
    )

    result = adapter.create_ticket(story)

    story_path = project / "docs" / "requirements" / "stories" / "US-010.md"
    data_path = project / ".synaptory" / ".orchestrator" / "tracker-data.json"
    payload = json.loads(data_path.read_text(encoding="utf-8"))
    assert result.file_path == "docs/requirements/stories/US-010.md"
    assert story_path.exists()
    assert "# US-010: Canonical fixture list" in story_path.read_text(encoding="utf-8")
    assert payload["stories"] == [{"id": "US-010", "status": "TO_DO", "sprint": "1"}]
    assert "raw_text" not in payload["stories"][0]
    assert "acceptance_criteria" not in payload["stories"][0]
    assert "file" not in payload["stories"][0]


@pytest.mark.unit
def test_local_adapter_acceptance_status_stays_compact(tmp_path: Path):
    """#51 — AC check state is stored separately from Markdown AC text."""
    from tracker.local_adapter import LocalAdapter
    from tracker.config import TrackerConfig
    from tracker.base import AcceptanceCriterion, Story

    project = tmp_path / "proj"
    adapter = LocalAdapter(project, TrackerConfig())
    adapter.create_ticket(Story(
        id="US-012",
        title="Fallback source",
        acceptance_criteria=[
            AcceptanceCriterion(id="AC-01", text="Given primary API fails, When fallback runs, Then data loads.")
        ],
    ))

    adapter.update_acceptance_criteria("US-012", "AC-01", True)
    story = adapter.get_story("US-012")
    payload = json.loads(
        (project / ".synaptory" / ".orchestrator" / "tracker-data.json").read_text(encoding="utf-8")
    )

    assert story.acceptance_criteria[0].met is True
    assert payload["stories"][0]["acceptance_status"] == {"AC-01": True}
    assert "acceptance_criteria" not in payload["stories"][0]


@pytest.mark.unit
def test_local_adapter_finds_legacy_id_prefixed_markdown_without_file_pointer(tmp_path: Path):
    """#51 — projects can remove file pointers before renaming old slug files."""
    from tracker.local_adapter import LocalAdapter
    from tracker.config import TrackerConfig

    project = tmp_path / "proj"
    tracker_dir = project / ".synaptory" / ".orchestrator"
    story_dir = project / "docs" / "requirements" / "stories"
    tracker_dir.mkdir(parents=True)
    story_dir.mkdir(parents=True)
    (tracker_dir / "tracker-data.json").write_text(
        json.dumps({"epics": [], "sprints": [], "stories": [{"id": "US-011"}]}),
        encoding="utf-8",
    )
    (story_dir / "US-011-football-data-org-integration.md").write_text(
        "# US-011: football-data.org integration\n\n"
        "## Acceptance Criteria\n"
        "* [ ] **AC-01:** Given an API token, When sync runs, Then fixtures are imported.\n",
        encoding="utf-8",
    )

    story = LocalAdapter(project, TrackerConfig()).get_story("US-011")

    assert story.title == "football-data.org integration"
    assert story.file_path == "docs/requirements/stories/US-011-football-data-org-integration.md"
    assert story.ac_count == 1


@pytest.mark.unit
def test_local_adapter_hydrates_epic_and_sprint_from_id_named_markdown(tmp_path: Path):
    """#51 — local epic/sprint bodies also live in ID-named Markdown files."""
    from tracker.local_adapter import LocalAdapter
    from tracker.config import TrackerConfig

    project = tmp_path / "proj"
    tracker_dir = project / ".synaptory" / ".orchestrator"
    epic_dir = project / "docs" / "requirements" / "epics"
    sprint_dir = project / "docs" / "requirements" / "sprints"
    tracker_dir.mkdir(parents=True)
    epic_dir.mkdir(parents=True)
    sprint_dir.mkdir(parents=True)
    (tracker_dir / "tracker-data.json").write_text(
        json.dumps({
            "epics": [{"id": "EPIC-002"}],
            "stories": [],
            "sprints": [{"number": 1}],
        }),
        encoding="utf-8",
    )
    (epic_dir / "EPIC-002.md").write_text(
        "# EPIC-002: Group Management\n\n<feature id=\"FEAT-001\"></feature>\n",
        encoding="utf-8",
    )
    (sprint_dir / "SPRINT-001.md").write_text(
        "\n".join([
            "# SPRINT-001: Functional Features",
            "",
            "- Dates: ASAP",
            "- Capacity: 99",
            "",
            "## Stories",
            "",
            "| Story | Title |",
            "|-------|-------|",
            "| US-001 | Bootstrap |",
            "| US-002 | Auth |",
            "",
        ]),
        encoding="utf-8",
    )

    adapter = LocalAdapter(project, TrackerConfig())
    epic = adapter.get_epic("EPIC-002")
    sprint = adapter.get_sprint(1)

    assert epic.title == "Group Management"
    assert epic.feature_count == 1
    assert epic.file_path == "docs/requirements/epics/EPIC-002.md"
    assert sprint.goal == "Functional Features"
    assert sprint.dates == "ASAP"
    assert sprint.capacity == "99"
    assert sprint.story_ids == ["US-001", "US-002"]
    assert sprint.file_path == "docs/requirements/sprints/SPRINT-001.md"


@pytest.mark.unit
def test_markdown_backend_is_removed(tmp_path: Path):
    """#51 — `markdown` is not a backend or alias; use `local` only."""
    from tracker import get_adapter_by_name, AdapterError
    from tracker.config import TrackerConfig

    project = tmp_path / "proj"
    project.mkdir()
    (project / ".synaptory.yaml").write_text(
        "tracker:\n  backend: markdown\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="backend='markdown' has been removed"):
        TrackerConfig.load(project)

    clean_project = tmp_path / "clean"
    clean_project.mkdir()
    with pytest.raises(AdapterError, match="Unsupported tracker backend: 'markdown'"):
        get_adapter_by_name(clean_project, "markdown")


@pytest.mark.unit
def test_local_backend_allows_quoted_value_with_inline_comment(tmp_path: Path):
    """Backend validation must not reject ordinary YAML inline comments."""
    from tracker.config import TrackerConfig

    project = tmp_path / "proj"
    project.mkdir()
    (project / ".synaptory.yaml").write_text(
        'tracker:\n  backend: "local"               # local only\n',
        encoding="utf-8",
    )

    assert TrackerConfig.load(project).backend == "local"


@pytest.mark.unit
def test_supported_backends_string_lists_every_dispatch_branch():
    """Adding a backend means touching ~15 hardcoded lists; the two
    "Supported backends" error strings are the ones users actually read when
    they typo a backend name, and they are easy to forget."""
    src = (_TRACKER_DIR / "__init__.py").read_text(encoding="utf-8")
    messages = [ln for ln in src.splitlines() if "Supported backends:" in ln]
    assert len(messages) == 2, "expected both factory error strings"
    for backend in ("local", "github", "jira", "teamwork", "linear"):
        for msg in messages:
            assert backend in msg, f"{backend!r} missing from: {msg.strip()}"


@pytest.mark.unit
def test_linear_backend_dispatches_to_linear_adapter(tmp_path: Path, monkeypatch):
    """End-to-end: `backend: linear` loads and resolves to LinearAdapter.

    The env var is removed explicitly rather than assumed absent. `./synaptory
    test` sources the repo-root `.env`, so on any machine that keeps a real
    LINEAR_API_KEY there this Layer 1 test would otherwise reach the live
    Linear API and assert `offline` against a genuine `ok` — turning a
    pure-logic test into a network test that fails for the wrong reason.
    plugin-claude/tests/README.md scopes Layer 1 as "no subprocess, no network".
    """
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)

    from tracker import get_adapter, clear_cache
    from tracker.linear_adapter import LinearAdapter

    project = tmp_path / "lin"
    project.mkdir()
    (project / ".synaptory.yaml").write_text(
        'build_mode: scrum\n'
        'tracker:\n'
        '  backend: "linear"\n'
        '  linear:\n'
        '    team_key: "ENG"\n',
        encoding="utf-8",
    )
    clear_cache()
    try:
        adapter = get_adapter(project)
        assert isinstance(adapter, LinearAdapter)
        # No LINEAR_API_KEY in the test env → offline, not a crash.
        assert adapter.health_check()["status"] == "offline"
    finally:
        clear_cache()
