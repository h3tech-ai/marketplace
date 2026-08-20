"""Layer-1 unit tests for multi-spec on GitHub Issues + Teamwork backends.

Covers (v2.8):

  GitHub:
    1. Config parses a github-bound spec.
    2. label-typed filter produces `label:"..."` search fragment + label
       append on write.
    3. milestone-typed filter produces `milestone:"..."` fragment + milestone
       set on write (when caller didn't already pin one).
    4. Per-spec cache paths route under specs/<id>/.
    5. Legacy single-spec GitHub adapter still has empty filter.

  Teamwork:
    6. Config parses a teamwork-bound spec.
    7. tag-typed filter resolves tag id via transport.get_or_create_tag()
       (stub patched) and adds it to tag_ids on reads and writes.
    8. tasklist-typed filter overrides tasklist_id on writes.
    9. Per-spec cache paths route under specs/<id>/.
    10. Legacy single-spec Teamwork adapter has no spec filter.

  Cross-backend:
    11. Backend-binding mismatch is rejected (spec.jira under
        tracker.backend=github).
    12. Filter-type mismatch is rejected (component on github,
        milestone on teamwork, etc.).
    13. Local multi-spec parses and partitions tracker-data.json per spec.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_PLUGIN = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PLUGIN / "skills" / "_shared" / "scripts"))
sys.path.insert(0, str(_PLUGIN / "hooks" / "lib"))

from tracker.config import (  # noqa: E402
    TrackerConfig, SpecConfig, SpecFilter,
    GitHubConfig, GitHubSpecBinding,
    LocalSpecBinding, TeamworkConfig, TeamworkSpecBinding,
)
from tracker.github_adapter import GitHubAdapter  # noqa: E402
from tracker.local_adapter import LocalAdapter  # noqa: E402
from tracker.teamwork_adapter import TeamworkAdapter  # noqa: E402
from tracker.base import Story  # noqa: E402


# ─── GitHub: config + adapter ──────────────────────────────────────────────


def test_github_multi_spec_yaml_parses(tmp_path: Path) -> None:
    (tmp_path / ".synaptory.yaml").write_text(
        textwrap.dedent(
            """
            build_mode: scrum
            tracker:
              backend: github
              github:
                repo: h3tech-ai/synaptory

            specs:
              - id: platform
                name: Platform
                github:
                  repo: h3tech-ai/synaptory
                  filter:
                    type: label
                    value: platform
              - id: contract-mastery
                name: CM
                github:
                  repo: h3tech-ai/synaptory
                  filter:
                    type: milestone
                    value: CM-2026Q1
            """
        ).strip(),
        encoding="utf-8",
    )
    cfg = TrackerConfig.load(tmp_path)
    assert cfg.is_multispec()
    assert cfg.specs[0].backend() == "github"
    assert cfg.specs[0].github.repo == "h3tech-ai/synaptory"
    assert cfg.specs[0].filter.type == "label"
    assert cfg.specs[1].filter.type == "milestone"
    assert cfg.specs[1].filter.value == "CM-2026Q1"


def _gh_adapter(spec_type: str, value: str, tmp_path: Path = Path("/tmp")) -> GitHubAdapter:
    sp = SpecConfig(
        id="platform", name="P",
        github=GitHubSpecBinding(repo="h3tech-ai/synaptory"),
        filter=SpecFilter(type=spec_type, value=value),
    )
    cfg = TrackerConfig(backend="github", github=GitHubConfig(repo="h3tech-ai/synaptory"))
    return GitHubAdapter(tmp_path, cfg, spec=sp)


def test_github_label_filter_search_fragment_and_writes(tmp_path: Path) -> None:
    ad = _gh_adapter("label", "platform", tmp_path)
    assert ad._spec_search_fragment() == 'label:"platform"'
    assert ad._spec_label_for_list_query() == "platform"
    assert ad._spec_milestone_for_list_query() is None
    # Write side-effect appends label; no milestone changes.
    labels, milestone = ad._apply_spec_write_side_effect(labels=["US-1"])
    assert "platform" in labels
    assert milestone is None
    # Dedup — second call should not re-append.
    labels, _ = ad._apply_spec_write_side_effect(labels=labels)
    assert labels.count("platform") == 1


def test_github_milestone_filter_search_fragment_and_writes(tmp_path: Path) -> None:
    ad = _gh_adapter("milestone", "CM-2026Q1", tmp_path)
    assert ad._spec_search_fragment() == 'milestone:"CM-2026Q1"'
    assert ad._spec_label_for_list_query() is None
    assert ad._spec_milestone_for_list_query() == "CM-2026Q1"
    # Milestone is set when caller did not already pin one.
    labels, milestone = ad._apply_spec_write_side_effect(labels=["US-1"])
    assert milestone == "CM-2026Q1"
    # Caller-pinned milestone wins (sprint > spec).
    labels, milestone = ad._apply_spec_write_side_effect(
        labels=["US-1"], milestone_title="Sprint 5",
    )
    assert milestone == "Sprint 5"


def test_github_per_spec_cache_paths(tmp_path: Path) -> None:
    cfg = TrackerConfig(backend="github", github=GitHubConfig(repo="h3tech-ai/synaptory"))
    sp_a = SpecConfig(id="platform", name="P",
                      github=GitHubSpecBinding(repo="h3tech-ai/synaptory"),
                      filter=SpecFilter(type="label", value="platform"))
    sp_b = SpecConfig(id="cm", name="CM",
                      github=GitHubSpecBinding(repo="h3tech-ai/synaptory"),
                      filter=SpecFilter(type="label", value="cm"))
    ad_a = GitHubAdapter(tmp_path, cfg, spec=sp_a)
    ad_b = GitHubAdapter(tmp_path, cfg, spec=sp_b)
    assert ad_a._id_map_path != ad_b._id_map_path
    assert "specs/platform/" in str(ad_a._id_map_path)
    assert "specs/cm/" in str(ad_b._id_map_path)
    # Sibling cache files also branch:
    assert ad_a._backlog_order_path.parent == ad_a._id_map_path.parent
    assert ad_a._local_cache_path.parent == ad_a._id_map_path.parent


def test_github_legacy_adapter_no_spec_filter(tmp_path: Path) -> None:
    cfg = TrackerConfig(backend="github", github=GitHubConfig(repo="h3tech-ai/synaptory"))
    ad = GitHubAdapter(tmp_path, cfg)
    assert ad.spec is None
    assert ad._spec_search_fragment() == ""
    assert ad._spec_label_for_list_query() is None
    assert ad._spec_milestone_for_list_query() is None
    labels, milestone = ad._apply_spec_write_side_effect(labels=["US-1"])
    assert labels == ["US-1"]
    assert milestone is None
    # Legacy cache path is flat.
    assert "specs/" not in str(ad._id_map_path)


# ─── Teamwork: config + adapter ────────────────────────────────────────────


def test_teamwork_multi_spec_yaml_parses(tmp_path: Path) -> None:
    (tmp_path / ".synaptory.yaml").write_text(
        textwrap.dedent(
            """
            build_mode: scrum
            tracker:
              backend: teamwork
              teamwork:
                site_name: hano
                project_id: 999

            specs:
              - id: platform
                name: Platform
                teamwork:
                  site_name: hano
                  project_id: 999
                  filter:
                    type: tag
                    value: platform
              - id: contract-mastery
                name: CM
                teamwork:
                  site_name: hano
                  project_id: 999
                  filter:
                    type: tasklist
                    value: 12345
            """
        ).strip(),
        encoding="utf-8",
    )
    cfg = TrackerConfig.load(tmp_path)
    assert cfg.is_multispec()
    assert cfg.specs[0].backend() == "teamwork"
    assert cfg.specs[0].teamwork.project_id == 999
    assert cfg.specs[0].filter.type == "tag"
    assert cfg.specs[1].filter.type == "tasklist"
    assert cfg.specs[1].filter.value == "12345"


def _tw_adapter(spec_type: str, value: str, tmp_path: Path = Path("/tmp")) -> TeamworkAdapter:
    sp = SpecConfig(
        id="platform", name="P",
        teamwork=TeamworkSpecBinding(site_name="hano", project_id=999),
        filter=SpecFilter(type=spec_type, value=value),
    )
    cfg = TrackerConfig(
        backend="teamwork",
        teamwork=TeamworkConfig(site_name="hano", project_id=999),
    )
    return TeamworkAdapter(tmp_path, cfg, spec=sp)


def test_teamwork_tag_filter_resolves_via_transport(tmp_path: Path) -> None:
    ad = _tw_adapter("tag", "platform", tmp_path)
    # Patch the transport's tag lookup; we want to verify the adapter
    # threads the tag value into get_or_create_tag and caches the id.
    ad.transport.get_or_create_tag = MagicMock(return_value=42)
    assert ad._spec_tag_filter_ids() == [42]
    # Cache hit on second call — no re-lookup.
    ad.transport.get_or_create_tag.assert_called_once_with("platform")
    assert ad._spec_tag_filter_ids() == [42]
    assert ad.transport.get_or_create_tag.call_count == 1


def test_teamwork_tag_write_side_effect(tmp_path: Path) -> None:
    ad = _tw_adapter("tag", "platform", tmp_path)
    ad.transport.get_or_create_tag = MagicMock(return_value=42)
    tag_ids, tasklist_id = ad._apply_spec_write_side_effect(tag_ids=[1, 2])
    assert 42 in tag_ids
    assert tasklist_id is None
    # Dedup
    tag_ids, _ = ad._apply_spec_write_side_effect(tag_ids=tag_ids)
    assert tag_ids.count(42) == 1


def test_teamwork_tasklist_filter_and_write_override(tmp_path: Path) -> None:
    ad = _tw_adapter("tasklist", "12345", tmp_path)
    assert ad._spec_tag_filter_ids() is None
    assert ad._spec_tasklist_id() == 12345
    # Write side-effect overrides tasklist only when caller didn't pin one.
    tag_ids, tasklist_id = ad._apply_spec_write_side_effect(tag_ids=[1])
    assert tasklist_id == 12345
    tag_ids, tasklist_id = ad._apply_spec_write_side_effect(
        tag_ids=[1], tasklist_id=99999,
    )
    assert tasklist_id == 99999  # caller wins


def test_teamwork_per_spec_cache_paths(tmp_path: Path) -> None:
    cfg = TrackerConfig(
        backend="teamwork",
        teamwork=TeamworkConfig(site_name="hano", project_id=999),
    )
    sp_a = SpecConfig(id="platform", name="P",
                      teamwork=TeamworkSpecBinding(site_name="hano", project_id=999),
                      filter=SpecFilter(type="tag", value="platform"))
    sp_b = SpecConfig(id="cm", name="CM",
                      teamwork=TeamworkSpecBinding(site_name="hano", project_id=999),
                      filter=SpecFilter(type="tag", value="cm"))
    ad_a = TeamworkAdapter(tmp_path, cfg, spec=sp_a)
    ad_b = TeamworkAdapter(tmp_path, cfg, spec=sp_b)
    assert "specs/platform/" in str(ad_a._id_map_path)
    assert "specs/cm/" in str(ad_b._id_map_path)


def test_teamwork_per_spec_workflow_stages_yaml_parses(tmp_path: Path) -> None:
    (tmp_path / ".synaptory.yaml").write_text(
        textwrap.dedent(
            """
            build_mode: scrum
            tracker:
              backend: teamwork
              teamwork:
                site_name: lmhealthcarecommunications

            specs:
              - id: venue-agent
                name: Venue Agent
                teamwork:
                  project_id: 1519491
                  workflow_stages:
                    TO_DO: 25086
                    IN_PROGRESS: 25087
                    DONE: 25091
                  filter:
                    type: tag
                    value: venue-agent
              - id: stars-assistant
                name: STARS Assistant
                teamwork:
                  project_id: 1598948
                  workflow_stages:
                    TO_DO: 385922
                    IN_PROGRESS: 385923
                    IN_REVIEW: 385924
                    DONE: 385929
                  filter:
                    type: tag
                    value: stars-assistant
            """
        ).strip(),
        encoding="utf-8",
    )
    cfg = TrackerConfig.load(tmp_path)
    assert cfg.is_multispec()
    assert cfg.specs[0].teamwork.workflow_stages == {
        "TO_DO": 25086, "IN_PROGRESS": 25087, "DONE": 25091,
    }
    assert cfg.specs[1].teamwork.workflow_stages == {
        "TO_DO": 385922, "IN_PROGRESS": 385923, "IN_REVIEW": 385924, "DONE": 385929,
    }


def test_teamwork_adapter_prefers_spec_workflow_stages(tmp_path: Path) -> None:
    cfg = TrackerConfig(
        backend="teamwork",
        teamwork=TeamworkConfig(
            site_name="hano",
            project_id=999,
            workflow_stages={"TO_DO": 1, "IN_PROGRESS": 2, "DONE": 3},
        ),
    )
    sp = SpecConfig(
        id="stars", name="STARS",
        teamwork=TeamworkSpecBinding(
            site_name="hano", project_id=1598948,
            workflow_stages={"TO_DO": 385922, "IN_PROGRESS": 385923, "DONE": 385929},
        ),
        filter=SpecFilter(type="tag", value="stars"),
    )
    ad = TeamworkAdapter(tmp_path, cfg, spec=sp)
    assert ad._config_workflow_stages == {
        "TO_DO": 385922, "IN_PROGRESS": 385923, "DONE": 385929,
    }


def test_teamwork_adapter_falls_back_to_top_level_workflow_stages(tmp_path: Path) -> None:
    cfg = TrackerConfig(
        backend="teamwork",
        teamwork=TeamworkConfig(
            site_name="hano",
            project_id=999,
            workflow_stages={"TO_DO": 1, "IN_PROGRESS": 2, "DONE": 3},
        ),
    )
    sp = SpecConfig(
        id="platform", name="P",
        teamwork=TeamworkSpecBinding(site_name="hano", project_id=999),
        filter=SpecFilter(type="tag", value="platform"),
    )
    ad = TeamworkAdapter(tmp_path, cfg, spec=sp)
    assert ad._config_workflow_stages == {"TO_DO": 1, "IN_PROGRESS": 2, "DONE": 3}


def test_teamwork_legacy_adapter_no_spec_filter(tmp_path: Path) -> None:
    cfg = TrackerConfig(
        backend="teamwork",
        teamwork=TeamworkConfig(site_name="hano", project_id=999),
    )
    ad = TeamworkAdapter(tmp_path, cfg)
    assert ad.spec is None
    assert ad._spec_tag_filter_ids() is None
    assert ad._spec_tasklist_id() is None
    tag_ids, tasklist_id = ad._apply_spec_write_side_effect(tag_ids=[1])
    assert tag_ids == [1]
    assert tasklist_id is None
    assert "specs/" not in str(ad._id_map_path)


# ─── Cross-backend validation ──────────────────────────────────────────────


@pytest.mark.parametrize("tracker_backend,spec_block,expected", [
    # spec binds to jira but tracker is github
    ("github", "jira:\n      project_key: HT\n      filter:\n        type: label\n        value: a",
     "binds to 'jira' but tracker.backend='github'"),
    # spec binds to github but tracker is teamwork
    ("teamwork", "github:\n      repo: a/b\n      filter:\n        type: label\n        value: a",
     "binds to 'github' but tracker.backend='teamwork'"),
    # spec binds to teamwork but tracker is jira
    ("jira", "teamwork:\n      project_id: 1\n      filter:\n        type: tag\n        value: a",
     "binds to 'teamwork' but tracker.backend='jira'"),
])
def test_backend_binding_mismatch_rejected(
    tmp_path: Path, tracker_backend: str, spec_block: str, expected: str,
) -> None:
    yaml = (
        f"tracker:\n  backend: {tracker_backend}\n"
        + ("  github:\n    repo: a/b\n" if tracker_backend == "github" else "")
        + ("  teamwork:\n    project_id: 1\n" if tracker_backend == "teamwork" else "")
        + f"specs:\n  - id: platform\n    name: P\n    {spec_block}\n"
    )
    (tmp_path / ".synaptory.yaml").write_text(yaml, encoding="utf-8")
    with pytest.raises(ValueError, match=expected):
        TrackerConfig.load(tmp_path)


@pytest.mark.parametrize("tracker_backend,bad_filter_type,backend_block,binding_block", [
    # component is jira-only — invalid on github
    ("github", "component",
     "  github:\n    repo: a/b\n",
     "github:\n      repo: a/b"),
    # milestone is github-only — invalid on jira
    ("jira", "milestone",
     "",
     "jira:\n      project_key: HT"),
    # jql is jira-only — invalid on teamwork
    ("teamwork", "jql",
     "  teamwork:\n    project_id: 1\n",
     "teamwork:\n      project_id: 1"),
    # tag is teamwork-only — invalid on github
    ("github", "tag",
     "  github:\n    repo: a/b\n",
     "github:\n      repo: a/b"),
])
def test_filter_type_mismatch_rejected(
    tmp_path: Path,
    tracker_backend: str,
    bad_filter_type: str,
    backend_block: str,
    binding_block: str,
) -> None:
    yaml = (
        f"tracker:\n  backend: {tracker_backend}\n"
        f"{backend_block}"
        f"specs:\n  - id: platform\n    name: P\n    {binding_block}\n"
        f"      filter:\n        type: {bad_filter_type}\n        value: x\n"
    )
    (tmp_path / ".synaptory.yaml").write_text(yaml, encoding="utf-8")
    with pytest.raises(ValueError, match="filter.type"):
        TrackerConfig.load(tmp_path)


def test_local_multi_spec_yaml_parses(tmp_path: Path) -> None:
    (tmp_path / ".synaptory.yaml").write_text(
        textwrap.dedent(
            """
            build_mode: scrum
            tracker:
              backend: local
            specs:
              - id: venue-agent
                name: Venue Agent
                local:
                  requirements_dir: docs/venue-agent/requirements
              - id: stars-assistant
                name: STARS Assistant
            """
        ).strip(),
        encoding="utf-8",
    )
    cfg = TrackerConfig.load(tmp_path)
    assert cfg.is_multispec()
    assert cfg.backend == "local"
    assert cfg.specs[0].backend() == "local"
    assert cfg.specs[0].local.requirements_dir == "docs/venue-agent/requirements"
    # A local spec may omit the backend block entirely; id/name are enough to
    # derive the filesystem partition.
    assert cfg.specs[1].backend() == ""


def test_local_multi_spec_rejects_remote_binding(tmp_path: Path) -> None:
    (tmp_path / ".synaptory.yaml").write_text(
        textwrap.dedent(
            """
            tracker:
              backend: local
            specs:
              - id: platform
                name: P
                github:
                  repo: a/b
                  filter:
                    type: label
                    value: platform
            """
        ).strip(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="tracker.backend='local'"):
        TrackerConfig.load(tmp_path)


def test_local_adapter_per_spec_tracker_data_paths(tmp_path: Path) -> None:
    cfg = TrackerConfig(backend="local")
    spec_a = SpecConfig(
        id="venue-agent", name="Venue",
        local=LocalSpecBinding(requirements_dir="docs/venue-agent/requirements"),
    )
    spec_b = SpecConfig(id="stars-assistant", name="STARS", local=LocalSpecBinding())

    ad_a = LocalAdapter(tmp_path, cfg, spec=spec_a)
    ad_b = LocalAdapter(tmp_path, cfg, spec=spec_b)
    assert "specs/venue-agent/tracker-data.json" in str(ad_a._data_path)
    assert "specs/stars-assistant/tracker-data.json" in str(ad_b._data_path)

    ad_a.create_ticket(Story(id="VA-001", title="Venue story"))
    ad_b.create_ticket(Story(id="SA-001", title="Stars story"))

    data_a = json.loads(ad_a._data_path.read_text(encoding="utf-8"))
    data_b = json.loads(ad_b._data_path.read_text(encoding="utf-8"))
    assert [s["id"] for s in data_a["stories"]] == ["VA-001"]
    assert [s["id"] for s in data_b["stories"]] == ["SA-001"]
    assert (tmp_path / "docs" / "venue-agent" / "requirements" / "stories"
            / "VA-001.md").exists()


def test_tracker_cli_reads_local_specs_independently(tmp_path: Path) -> None:
    (tmp_path / ".synaptory.yaml").write_text(
        textwrap.dedent(
            """
            build_mode: scrum
            tracker:
              backend: local
            specs:
              - id: venue-agent
                name: Venue Agent
                local:
                  requirements_dir: docs/venue-agent/requirements
              - id: stars-assistant
                name: STARS Assistant
                local:
                  requirements_dir: docs/stars-assistant/requirements
            """
        ).strip(),
        encoding="utf-8",
    )
    venue_dir = tmp_path / ".synaptory" / ".orchestrator" / "specs" / "venue-agent"
    stars_dir = tmp_path / ".synaptory" / ".orchestrator" / "specs" / "stars-assistant"
    venue_dir.mkdir(parents=True)
    stars_dir.mkdir(parents=True)
    (venue_dir / "tracker-data.json").write_text(
        json.dumps({"epics": [], "sprints": [], "stories": [{"id": "VA-001", "status": "TO_DO"}]}),
        encoding="utf-8",
    )
    (stars_dir / "tracker-data.json").write_text(
        json.dumps({"epics": [], "sprints": [], "stories": [{"id": "SA-001", "status": "TO_DO"}]}),
        encoding="utf-8",
    )

    cli = _PLUGIN / "skills" / "_shared" / "scripts" / "tracker" / "tracker_cli.py"
    venue = subprocess.run(
        [sys.executable, str(cli), "--project-dir", str(tmp_path),
         "--spec", "venue-agent", "get-backlog"],
        capture_output=True, text=True, check=True,
    )
    stars = subprocess.run(
        [sys.executable, str(cli), "--project-dir", str(tmp_path),
         "--spec", "stars-assistant", "get-backlog"],
        capture_output=True, text=True, check=True,
    )
    assert "VA-001" in venue.stdout
    assert "SA-001" not in venue.stdout
    assert "SA-001" in stars.stdout
    assert "VA-001" not in stars.stdout


# ─── Mixed-mode (legacy top-level disagreement) ────────────────────────────


def test_github_mixed_mode_rejected(tmp_path: Path) -> None:
    (tmp_path / ".synaptory.yaml").write_text(
        textwrap.dedent(
            """
            tracker:
              backend: github
              github:
                repo: h3tech-ai/wrong-repo
            specs:
              - id: platform
                name: P
                github:
                  repo: h3tech-ai/synaptory
                  filter:
                    type: label
                    value: platform
            """
        ).strip(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Mixed-mode conflict"):
        TrackerConfig.load(tmp_path)


def test_teamwork_mixed_mode_rejected(tmp_path: Path) -> None:
    (tmp_path / ".synaptory.yaml").write_text(
        textwrap.dedent(
            """
            tracker:
              backend: teamwork
              teamwork:
                site_name: hano
                project_id: 111
            specs:
              - id: platform
                name: P
                teamwork:
                  site_name: hano
                  project_id: 999
                  filter:
                    type: tag
                    value: platform
            """
        ).strip(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Mixed-mode conflict"):
        TrackerConfig.load(tmp_path)


# ─── Generalized issue_type / status_map overrides (github + teamwork) ─────


def test_github_config_parses_issue_type_and_status_map(tmp_path: Path) -> None:
    (tmp_path / ".synaptory.yaml").write_text(
        textwrap.dedent(
            """
            build_mode: scrum
            tracker:
              backend: github
              github:
                repo: h3tech-ai/synaptory
                issue_type:
                  story: Deliverable
                  epic: Initiative
                status_map:
                  in_review: "code-review"
                  blocked: "stuck"
            """
        ).strip(),
        encoding="utf-8",
    )
    cfg = TrackerConfig.load(tmp_path)
    assert cfg.github.issue_type_overrides == {
        "story": "Deliverable", "epic": "Initiative",
    }
    assert cfg.github.status_map == {
        "IN_REVIEW": "code-review", "BLOCKED": "stuck",
    }


def test_github_spec_binding_parses_overrides(tmp_path: Path) -> None:
    (tmp_path / ".synaptory.yaml").write_text(
        textwrap.dedent(
            """
            build_mode: scrum
            tracker:
              backend: github
              github:
                repo: h3tech-ai/synaptory
            specs:
              - id: platform
                name: P
                github:
                  repo: h3tech-ai/synaptory
                  issue_type: Deliverable
                  status_map:
                    blocked: "stuck"
                  filter:
                    type: label
                    value: platform
            """
        ).strip(),
        encoding="utf-8",
    )
    cfg = TrackerConfig.load(tmp_path)
    sp = cfg.find_spec("platform")
    assert sp is not None
    assert sp.github.issue_type_overrides == {
        "story": "Deliverable", "enhancement": "Deliverable",
    }
    assert sp.github.status_map == {"BLOCKED": "stuck"}


def test_teamwork_status_map_coexists_with_workflow_stages(tmp_path: Path) -> None:
    """Flattened int values on canonical keys stay workflow-stage IDs; string
    values parse as status_map tag names (and never crash the int() stage
    parser)."""
    (tmp_path / ".synaptory.yaml").write_text(
        textwrap.dedent(
            """
            build_mode: scrum
            tracker:
              backend: teamwork
              teamwork:
                site_name: hano
                project_id: 111
                workflow_stages:
                  IN_PROGRESS: 379227
                  DONE: 379229
                status_map:
                  blocked: "impediment"
                  in_review: "peer-review"
                issue_type:
                  story: "hc:deliverable"
            """
        ).strip(),
        encoding="utf-8",
    )
    cfg = TrackerConfig.load(tmp_path)
    assert cfg.teamwork.workflow_stages == {"IN_PROGRESS": 379227, "DONE": 379229}
    assert cfg.teamwork.status_map == {
        "BLOCKED": "impediment", "IN_REVIEW": "peer-review",
    }
    assert cfg.teamwork.issue_type_overrides == {"story": "hc:deliverable"}


def test_teamwork_flattened_non_int_stage_value_does_not_crash(tmp_path: Path) -> None:
    """A flattened canonical key with a non-int value is a status_map entry,
    not a stage ID — the config must load without ValueError."""
    (tmp_path / ".synaptory.yaml").write_text(
        textwrap.dedent(
            """
            build_mode: scrum
            tracker:
              backend: teamwork
              teamwork:
                site_name: hano
                project_id: 111
                IN_PROGRESS: 379227
                IN_REVIEW: "peer-review"
            """
        ).strip(),
        encoding="utf-8",
    )
    cfg = TrackerConfig.load(tmp_path)
    assert cfg.teamwork.workflow_stages == {"IN_PROGRESS": 379227}
    assert cfg.teamwork.status_map == {"IN_REVIEW": "peer-review"}


def _gh_override_adapter(issue_types: dict | None = None,
                         status_map: dict | None = None,
                         tmp_path: Path = Path("/tmp")) -> GitHubAdapter:
    cfg = TrackerConfig(backend="github", github=GitHubConfig(
        repo="h3tech-ai/synaptory",
        issue_type_overrides=issue_types or {},
        status_map=status_map or {},
    ))
    return GitHubAdapter(tmp_path, cfg)


def test_github_issue_type_resolver(tmp_path: Path) -> None:
    ad = _gh_override_adapter({"story": "Deliverable", "epic": "Initiative"}, tmp_path=tmp_path)
    assert ad._gh_issue_type("story") == "Deliverable"
    assert ad._gh_issue_type("epic") == "Initiative"
    assert ad._gh_issue_type("bug") == "Bug"          # default preserved
    assert ad._gh_issue_type("mystery") == "Story"    # unknown → default
    ad0 = _gh_override_adapter(tmp_path=tmp_path)
    assert ad0._gh_issue_type("story") == "Story"
    assert ad0._gh_issue_type("enhancement") == "Feature"


def test_github_status_label_resolver(tmp_path: Path) -> None:
    ad = _gh_override_adapter(status_map={"BLOCKED": "stuck"}, tmp_path=tmp_path)
    assert ad._status_label("BLOCKED") == "stuck"
    assert ad._status_label("IN_REVIEW") == "in-review"   # default preserved
    ad0 = _gh_override_adapter(tmp_path=tmp_path)
    assert ad0._status_label("CANCELLED") == "cancelled"


def test_github_update_story_status_uses_override_labels(tmp_path: Path, monkeypatch) -> None:
    ad = _gh_override_adapter(status_map={"BLOCKED": "stuck", "IN_REVIEW": "code-review"},
                              tmp_path=tmp_path)
    captured: list = []
    monkeypatch.setattr(ad, "_validate_status_transition", lambda *a, **k: None)
    monkeypatch.setattr(ad, "_resolve_id", lambda sid: 7)
    monkeypatch.setattr(ad, "_update_local_cache_status", lambda *a, **k: None)
    monkeypatch.setattr(ad, "get_story", lambda sid: None)
    monkeypatch.setattr(ad.transport, "update_issue",
                        lambda num, **kw: captured.append(kw))
    monkeypatch.setattr(ad.transport, "reopen_issue", lambda num: None)
    monkeypatch.setattr(ad.transport, "close_issue", lambda num: None)

    ad.update_story_status("US-1", "BLOCKED")
    assert captured[0]["add_labels"] == ["stuck"]
    assert "code-review" in captured[0]["remove_labels"]
    assert "awaiting-acceptance" in captured[0]["remove_labels"]  # default kept

    captured.clear()
    ad.update_story_status("US-1", "DONE")
    assert set(captured[0]["remove_labels"]) == {
        "stuck", "code-review", "awaiting-acceptance", "cancelled",
    }


def test_github_readback_honors_override_labels(tmp_path: Path) -> None:
    ad = _gh_override_adapter(status_map={"BLOCKED": "stuck"}, tmp_path=tmp_path)
    issue = {"number": 7, "title": "t", "state": "open", "body": "",
             "labels": [{"name": "US-1"}, {"name": "stuck"}]}
    assert ad._issue_to_story(issue).status == "BLOCKED"
    # Default label no longer matches when overridden ⇒ falls through.
    issue2 = {"number": 8, "title": "t", "state": "open", "body": "",
              "labels": [{"name": "US-2"}, {"name": "in-review"}]}
    assert ad._issue_to_story(issue2).status == "IN_REVIEW"


def test_github_required_issue_types_with_override(tmp_path: Path) -> None:
    """Overridden custom names get a generic creation spec; displaced defaults
    are dropped; GitHub built-ins (Bug/Feature/Task) are never created."""
    ad = _gh_override_adapter({"story": "Deliverable"}, tmp_path=tmp_path)
    required = ad._required_issue_types()
    assert "Deliverable" in required
    assert required["Deliverable"]["description"]
    assert "Story" not in required      # displaced by the override
    assert "Epic" in required           # still needed for epics
    assert "Bug" not in required        # GitHub built-in
    ad0 = _gh_override_adapter(tmp_path=tmp_path)
    assert set(ad0._required_issue_types()) == {"Epic", "Story"}


def _tw_override_adapter(issue_types: dict | None = None,
                         status_map: dict | None = None,
                         tmp_path: Path = Path("/tmp")) -> TeamworkAdapter:
    cfg = TrackerConfig(backend="teamwork", teamwork=TeamworkConfig(
        site_name="hano", project_id=111,
        issue_type_overrides=issue_types or {},
        status_map=status_map or {},
    ))
    return TeamworkAdapter(tmp_path, cfg)


def test_teamwork_entity_tag_resolver(tmp_path: Path) -> None:
    ad = _tw_override_adapter({"story": "hc:deliverable", "bug": "hc:defect"}, tmp_path=tmp_path)
    assert ad._entity_tag("story") == "hc:deliverable"
    assert ad._entity_tag("bug") == "hc:defect"
    assert ad._entity_tag("epic") == "hc:epic"                 # default preserved
    assert ad._entity_tag("mystery") == "hc:story"             # unknown → story
    assert ad._entity_tag("mystery", default_key="task") == "hc:task"
    ad0 = _tw_override_adapter(tmp_path=tmp_path)
    assert ad0._entity_tag("story") == "hc:story"


def test_teamwork_status_tag_resolver(tmp_path: Path) -> None:
    ad = _tw_override_adapter(status_map={"BLOCKED": "impediment"}, tmp_path=tmp_path)
    assert ad._status_tag("BLOCKED") == "impediment"
    assert ad._status_tag("IN_REVIEW") == "in-review"          # default preserved
    ad0 = _tw_override_adapter(tmp_path=tmp_path)
    assert ad0._status_tag("AWAITING_ACCEPTANCE") == "awaiting-acceptance"


def test_teamwork_readback_honors_override_tags(tmp_path: Path) -> None:
    ad = _tw_override_adapter(status_map={"BLOCKED": "impediment"}, tmp_path=tmp_path)
    task = {"id": 1, "name": "t", "completed": False,
            "tags": [{"name": "US-1"}, {"name": "impediment"}]}
    assert ad._task_to_story(task).status == "BLOCKED"
    task2 = {"id": 2, "name": "t", "completed": False,
             "tags": [{"name": "US-2"}, {"name": "in-review"}]}
    assert ad._task_to_story(task2).status == "IN_REVIEW"


def test_teamwork_spec_binding_overrides_win(tmp_path: Path) -> None:
    """Per-spec issue_type/status_map override the top-level maps; empty
    per-spec falls back to top-level."""
    top = TeamworkConfig(site_name="hano", project_id=111,
                         issue_type_overrides={"story": "hc:top-story"},
                         status_map={"BLOCKED": "top-blocked"})
    sp = SpecConfig(
        id="platform", name="P",
        teamwork=TeamworkSpecBinding(site_name="hano", project_id=111,
                                     issue_type_overrides={"story": "hc:spec-story"},
                                     status_map={"BLOCKED": "spec-blocked"}),
        filter=SpecFilter(type="tag", value="platform"),
    )
    cfg = TrackerConfig(backend="teamwork", teamwork=top)
    ad = TeamworkAdapter(tmp_path, cfg, spec=sp)
    assert ad._entity_tag("story") == "hc:spec-story"
    assert ad._status_tag("BLOCKED") == "spec-blocked"

    sp_empty = SpecConfig(
        id="other", name="O",
        teamwork=TeamworkSpecBinding(site_name="hano", project_id=111),
        filter=SpecFilter(type="tag", value="other"),
    )
    ad2 = TeamworkAdapter(tmp_path, cfg, spec=sp_empty)
    assert ad2._entity_tag("story") == "hc:top-story"
    assert ad2._status_tag("BLOCKED") == "top-blocked"


# ─── Re-review fixes: override-aware read paths (#155 / PR #156) ───────────


def test_github_list_paths_query_override_type(tmp_path: Path, monkeypatch) -> None:
    """list_stories / get_backlog / get_sprint_backlog / list_epics query the
    resolved custom type names, not the hardcoded defaults."""
    ad = _gh_override_adapter({"story": "Deliverable", "epic": "Initiative"},
                              tmp_path=tmp_path)
    captured: list = []
    monkeypatch.setattr(ad, "_get_type_ids",
                        lambda: {"Deliverable": "IT_1", "Initiative": "IT_2"})
    monkeypatch.setattr(ad.transport, "search_issues",
                        lambda q, **k: captured.append(q) or [])
    ad.list_stories()
    ad.get_backlog()
    ad.get_sprint_backlog(1)
    ad.list_epics()
    assert all('type:"Deliverable"' in q for q in captured[:3])
    assert 'type:"Initiative"' in captured[3]
    # And the availability check keys on the custom name — no label fallback.
    assert len(captured) == 4


def test_github_readback_awaiting_and_cancelled(tmp_path: Path) -> None:
    """AWAITING_ACCEPTANCE and CANCELLED round-trip through read-back,
    including overridden label names."""
    ad = _gh_override_adapter(
        status_map={"AWAITING_ACCEPTANCE": "ready-for-po", "CANCELLED": "dropped"},
        tmp_path=tmp_path)
    open_awaiting = {"number": 1, "title": "t", "state": "open", "body": "",
                     "labels": [{"name": "US-1"}, {"name": "ready-for-po"}]}
    assert ad._issue_to_story(open_awaiting).status == "AWAITING_ACCEPTANCE"
    closed_cancelled = {"number": 2, "title": "t", "state": "closed", "body": "",
                        "labels": [{"name": "US-2"}, {"name": "dropped"}]}
    assert ad._issue_to_story(closed_cancelled).status == "CANCELLED"
    closed_done = {"number": 3, "title": "t", "state": "closed", "body": "",
                   "labels": [{"name": "US-3"}]}
    assert ad._issue_to_story(closed_done).status == "DONE"
    # Defaults (no override) round-trip too.
    ad0 = _gh_override_adapter(tmp_path=tmp_path)
    default_awaiting = {"number": 4, "title": "t", "state": "open", "body": "",
                        "labels": [{"name": "US-4"}, {"name": "awaiting-acceptance"}]}
    assert ad0._issue_to_story(default_awaiting).status == "AWAITING_ACCEPTANCE"
    default_cancelled = {"number": 5, "title": "t", "state": "closed", "body": "",
                         "labels": [{"name": "US-5"}, {"name": "cancelled"}]}
    assert ad0._issue_to_story(default_cancelled).status == "CANCELLED"


def test_github_move_incomplete_skips_cancelled(tmp_path: Path, monkeypatch) -> None:
    ad = _gh_override_adapter(tmp_path=tmp_path)
    stories = [Story(id="US-1", title="a", status="CANCELLED"),
               Story(id="US-2", title="b", status="IN_PROGRESS"),
               Story(id="US-3", title="c", status="DONE")]
    monkeypatch.setattr(ad, "get_sprint_backlog", lambda n: stories)
    moved: list = []
    monkeypatch.setattr(ad, "assign_to_sprint", lambda sid, n: moved.append(sid))
    incomplete = ad.move_incomplete_to_next(1, 2)
    assert [s.id for s in incomplete] == ["US-2"]
    assert moved == ["US-2"]


def test_teamwork_readback_awaiting_and_cancelled(tmp_path: Path) -> None:
    ad = _tw_override_adapter(
        status_map={"AWAITING_ACCEPTANCE": "ready-for-po", "CANCELLED": "dropped"},
        tmp_path=tmp_path)
    open_awaiting = {"id": 1, "name": "t", "completed": False,
                     "tags": [{"name": "US-1"}, {"name": "ready-for-po"}]}
    assert ad._task_to_story(open_awaiting).status == "AWAITING_ACCEPTANCE"
    done_cancelled = {"id": 2, "name": "t", "completed": True,
                      "tags": [{"name": "US-2"}, {"name": "dropped"}]}
    assert ad._task_to_story(done_cancelled).status == "CANCELLED"
    done_plain = {"id": 3, "name": "t", "completed": True,
                  "tags": [{"name": "US-3"}]}
    assert ad._task_to_story(done_plain).status == "DONE"
    ad0 = _tw_override_adapter(tmp_path=tmp_path)
    default_cancelled = {"id": 4, "name": "t", "completed": True,
                         "tags": [{"name": "US-4"}, {"name": "cancelled"}]}
    assert ad0._task_to_story(default_cancelled).status == "CANCELLED"


def test_teamwork_move_incomplete_skips_cancelled(tmp_path: Path, monkeypatch) -> None:
    ad = _tw_override_adapter(tmp_path=tmp_path)
    stories = [Story(id="US-1", title="a", status="CANCELLED"),
               Story(id="US-2", title="b", status="TO_DO")]
    monkeypatch.setattr(ad, "get_sprint_backlog", lambda n: stories)
    moved: list = []
    monkeypatch.setattr(ad, "assign_to_sprint", lambda sid, n: moved.append(sid))
    incomplete = ad.move_incomplete_to_next(1, 2)
    assert [s.id for s in incomplete] == ["US-2"]
    assert moved == ["US-2"]


# ─── Linear: config + validation ───────────────────────────────────────────


_LINEAR_YAML = textwrap.dedent(
    """
    build_mode: scrum
    tracker:
      backend: "linear"
    specs:
      - id: alpha
        name: "Alpha"
        linear:
          team_key: "ENG"
          filter:
            type: label
            value: spec-alpha
      - id: beta
        name: "Beta"
        linear:
          team_key: "ENG"
          filter:
            type: project
            value: Platform
    """
).lstrip()


def test_linear_multi_spec_yaml_parses(tmp_path: Path) -> None:
    (tmp_path / ".synaptory.yaml").write_text(_LINEAR_YAML, encoding="utf-8")
    cfg = TrackerConfig.load(tmp_path)

    assert cfg.backend == "linear"
    assert [s.id for s in cfg.specs] == ["alpha", "beta"]
    assert all(s.backend() == "linear" for s in cfg.specs)
    assert cfg.specs[0].linear.team_key == "ENG"
    assert cfg.specs[0].filter == SpecFilter(type="label", value="spec-alpha")
    assert cfg.specs[1].filter == SpecFilter(type="project", value="Platform")


@pytest.mark.parametrize("bad_type", ["tag", "tasklist", "jql", "milestone",
                                      "component", "epic"])
def test_linear_rejects_other_backends_filter_types(tmp_path: Path, bad_type: str) -> None:
    (tmp_path / ".synaptory.yaml").write_text(
        _LINEAR_YAML.replace("type: label", f"type: {bad_type}"), encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not supported by"):
        TrackerConfig.load(tmp_path)


def test_linear_spec_requires_team_key(tmp_path: Path) -> None:
    (tmp_path / ".synaptory.yaml").write_text(
        _LINEAR_YAML.replace('      team_key: "ENG"\n', ""), encoding="utf-8",
    )
    with pytest.raises(ValueError, match="team_key is required"):
        TrackerConfig.load(tmp_path)


def test_linear_spec_inherits_top_level_team_key(tmp_path: Path) -> None:
    """A spec may omit team_key when the top-level block supplies it."""
    yaml = _LINEAR_YAML.replace(
        '  backend: "linear"\n',
        '  backend: "linear"\n  linear:\n    team_key: "ENG"\n',
    ).replace('      team_key: "ENG"\n', "")
    (tmp_path / ".synaptory.yaml").write_text(yaml, encoding="utf-8")

    cfg = TrackerConfig.load(tmp_path)
    assert cfg.linear.team_key == "ENG"
    assert cfg.specs[0].linear.team_key == ""


def test_linear_mixed_mode_team_key_conflict_rejected(tmp_path: Path) -> None:
    yaml = _LINEAR_YAML.replace(
        '  backend: "linear"\n',
        '  backend: "linear"\n  linear:\n    team_key: "OTHER"\n',
    )
    (tmp_path / ".synaptory.yaml").write_text(yaml, encoding="utf-8")
    with pytest.raises(ValueError, match="Mixed-mode conflict"):
        TrackerConfig.load(tmp_path)


def test_linear_spec_backend_mismatch_rejected(tmp_path: Path) -> None:
    (tmp_path / ".synaptory.yaml").write_text(
        _LINEAR_YAML.replace('  backend: "linear"', '  backend: "github"'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="binds to 'linear'"):
        TrackerConfig.load(tmp_path)


def test_linear_per_spec_cache_paths_route_under_spec_dir(tmp_path: Path) -> None:
    from tracker.linear_adapter import LinearAdapter

    (tmp_path / ".synaptory.yaml").write_text(_LINEAR_YAML, encoding="utf-8")
    cfg = TrackerConfig.load(tmp_path)
    ad = LinearAdapter(tmp_path, cfg, spec=cfg.specs[0])

    expected = tmp_path / ".synaptory" / ".orchestrator" / "specs" / "alpha"
    assert ad._id_map_path == expected / "tracker-id-map.json"
    assert ad._local_cache_path == expected / "tracker-data.json"


def test_linear_legacy_single_spec_has_no_filter(tmp_path: Path) -> None:
    from tracker.linear_adapter import LinearAdapter

    (tmp_path / ".synaptory.yaml").write_text(
        'build_mode: scrum\ntracker:\n  backend: "linear"\n'
        '  linear:\n    team_key: "ENG"\n',
        encoding="utf-8",
    )
    cfg = TrackerConfig.load(tmp_path)
    ad = LinearAdapter(tmp_path, cfg)

    assert ad.spec is None
    assert ad._spec_filter_fragment() == {}
    assert ad._id_map_path == tmp_path / ".synaptory" / ".orchestrator" / "tracker-id-map.json"


def test_linear_per_spec_status_map_override_resolves(tmp_path: Path) -> None:
    """Per-spec status_map must reach the adapter's tier-1 resolution."""
    from tracker.linear_adapter import LinearAdapter

    old = "      filter:\n        type: label\n        value: spec-alpha\n"
    new = ('      status_map:\n        IN_REVIEW: "Peer Review"\n') + old
    yaml = _LINEAR_YAML.replace(old, new, 1)
    assert yaml != _LINEAR_YAML, "fixture edit did not apply"
    (tmp_path / ".synaptory.yaml").write_text(yaml, encoding="utf-8")
    cfg = TrackerConfig.load(tmp_path)

    assert cfg.specs[0].linear.status_map == {"IN_REVIEW": "Peer Review"}

    ad = LinearAdapter(tmp_path, cfg, spec=cfg.specs[0])
    ad._states = [
        {"id": "x", "name": "Building", "type": "started", "position": 1},
        {"id": "y", "name": "Peer Review", "type": "started", "position": 2},
    ]
    assert ad._resolve_state("IN_REVIEW")["id"] == "y"
