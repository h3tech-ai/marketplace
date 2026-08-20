"""Layer-1 unit tests for multi-spec support (docs/multi-spec-design.md).

Coverage (per the §Verification section of the implementation plan):

    1. Single-spec regression — legacy v2 state + .synaptory.yaml work unchanged.
    2. Migration idempotency — running migrate_to_multispec.py twice is a no-op.
    3. Concurrent sprint advance — three subprocesses each transitioning a
       different spec leave all three sprint counters correct.
    4. JQL injection — _spec_filter_clause() emits the expected fragment for
       each filter.type, with `jql` wrapped in parentheses.
    5. Sentinel round-trip — multi-spec rollup parses back to the same dict;
       v2 flat sentinel still round-trips through the legacy path.
    6. Receipts isolation — two receipts under different active specs land
       in distinct directories.

The Hano dogfood (verification step 7) is a real-world gate; not covered here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

# ─── Imports under test ────────────────────────────────────────────────────

# `plugin-claude/hooks/lib` is on sys.path via conftest.py.
import spec_state  # noqa: E402
import update_claude_md as ucm  # noqa: E402
import scrum_state_machine as sm  # noqa: E402

# `plugin-claude/skills/_shared/scripts` is not auto-added — extend here for the
# tracker-config + migration imports.
_PLUGIN = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PLUGIN / "skills" / "_shared" / "scripts"))
from tracker.config import TrackerConfig, SpecConfig, SpecFilter, JiraConfig  # noqa: E402
from tracker.jira_adapter import JiraAdapter  # noqa: E402
from tracker.base import Story  # noqa: E402


# ─── 1. Single-spec regression ──────────────────────────────────────────────


def test_single_spec_yaml_unchanged(tmp_path: Path) -> None:
    (tmp_path / ".synaptory.yaml").write_text(
        textwrap.dedent(
            """
            build_mode: scrum
            tracker:
              backend: jira
              jira:
                url: https://x.atlassian.net
                project_key: HT
                board_id: 2
            """
        ).strip(),
        encoding="utf-8",
    )
    cfg = TrackerConfig.load(tmp_path)
    assert cfg.backend == "jira"
    assert cfg.jira.project_key == "HT"
    assert cfg.jira.board_id == 2
    assert not cfg.is_multispec()
    assert cfg.specs == []


def test_single_spec_state_round_trip(tmp_path: Path) -> None:
    """v2 state read/write does not silently switch to v3."""
    os.environ.pop("SYNAPTORY_ACTIVE_SPEC", None)
    (tmp_path / ".synaptory" / ".orchestrator").mkdir(parents=True)
    spec_state.write_full_state(str(tmp_path), {
        "version": "2.0",
        "build_mode": "scrum",
        "lifecycle_state": "INCEPTION",
        "current_sprint": 0,
        "current_stories": [],
        "lifecycle_history": [],
    })
    view = sm.read_state(str(tmp_path))
    assert view["version"] == "2.0"
    assert view["lifecycle_state"] == "INCEPTION"
    assert "_spec_id" not in view


# ─── 2. Migration idempotency ──────────────────────────────────────────────


def _seed_v2_project(pd: Path) -> None:
    orch = pd / ".synaptory" / ".orchestrator"
    (orch / "receipts").mkdir(parents=True)
    (pd / ".synaptory.yaml").write_text(
        "build_mode: scrum\n"
        "tracker:\n"
        "  backend: jira\n"
        "  jira:\n"
        "    url: https://x.atlassian.net\n"
        "    project_key: HT\n",
        encoding="utf-8",
    )
    spec_state.write_full_state(str(pd), {
        "version": "2.0",
        "build_mode": "scrum",
        "lifecycle_state": "SPRINT_EXECUTION",
        "current_sprint": 9,
        "current_stories": [{"id": "US-100", "state": "in_progress"}],
        "lifecycle_history": [],
    })
    (orch / "tracker-id-map.json").write_text('{"US-100":{"jira_key":"HT-100"}}')
    (orch / "receipts" / "US-100-se.json").write_text('{"role":"se"}')


def _run_migrate(pd: Path, primary: str, new: list[str]) -> subprocess.CompletedProcess:
    script = _PLUGIN / "skills" / "_shared" / "scripts" / "migrate_to_multispec.py"
    args = [
        sys.executable, str(script),
        "--project-dir", str(pd),
        "--primary-spec", primary,
        "--new-specs", ",".join(new),
        "--yes",
    ]
    return subprocess.run(args, capture_output=True, text=True, check=True)


def test_migration_idempotency(tmp_path: Path) -> None:
    _seed_v2_project(tmp_path)
    r1 = _run_migrate(tmp_path, "platform", ["contract-mastery"])
    assert "done." in r1.stdout

    full = spec_state.read_full_state(str(tmp_path))
    assert spec_state.is_multispec(full)
    assert full["active_spec"] == "platform"
    assert full["specs"]["platform"]["current_sprint"] == 9
    assert full["specs"]["contract-mastery"]["lifecycle_state"] == "INCEPTION"

    # Caches moved under specs/platform/
    spec_dir = tmp_path / ".synaptory" / ".orchestrator" / "specs" / "platform"
    assert (spec_dir / "tracker-id-map.json").exists()
    assert (spec_dir / "receipts" / "US-100-se.json").exists()
    # Backup tar exists
    migrations = (tmp_path / ".synaptory" / ".migrations").glob("*-pre-multispec.tar.gz")
    assert any(migrations)

    # Second run: no-op
    r2 = _run_migrate(tmp_path, "platform", ["contract-mastery"])
    assert "already multi-spec" in r2.stdout
    # File contents unchanged on the second run.
    assert spec_state.read_full_state(str(tmp_path)) == full


def test_migration_fresh_local_project_seeds_primary_and_local_stub(tmp_path: Path) -> None:
    (tmp_path / ".synaptory.yaml").write_text(
        "build_mode: scrum\n"
        "tracker:\n"
        "  backend: local\n",
        encoding="utf-8",
    )

    r = _run_migrate(tmp_path, "venue-agent", ["stars-assistant"])
    assert "done." in r.stdout

    full = spec_state.read_full_state(str(tmp_path))
    assert spec_state.is_multispec(full)
    assert full["active_spec"] == "venue-agent"
    assert set(full["specs"]) == {"venue-agent", "stars-assistant"}
    assert full["specs"]["venue-agent"]["lifecycle_state"] == "INCEPTION"
    assert full["specs"]["stars-assistant"]["lifecycle_state"] == "INCEPTION"

    yaml = (tmp_path / ".synaptory.yaml").read_text(encoding="utf-8")
    assert "tracker:\n  backend: local" in yaml
    assert "local:" in yaml
    assert "requirements_dir: docs/venue-agent/requirements" in yaml
    assert "requirements_dir: docs/stars-assistant/requirements" in yaml


# ─── 3. Concurrent sprint advance ──────────────────────────────────────────


def test_concurrent_sprint_advance(tmp_path: Path) -> None:
    """Three parallel processes advancing three different specs leave all
    three sprint counters correct (verification §11.3)."""
    spec_state.write_full_state(str(tmp_path), {
        "version": "3.0",
        "build_mode": "scrum",
        "active_spec": "platform",
        "specs": {
            sid: {
                "lifecycle_state": "INCEPTION",
                "current_sprint": 0,
                "current_stories": [],
                "lifecycle_history": [],
            }
            for sid in ("platform", "contract-mastery", "ehr-integration")
        },
    })

    script = textwrap.dedent(
        """
        import os, sys
        sys.path.insert(0, %r)
        os.environ['SYNAPTORY_ACTIVE_SPEC'] = sys.argv[1]
        import scrum_state_machine as sm
        sm.transition(%r, 'SPRINT_PLANNING')
        """
    ) % (str(_PLUGIN / "hooks" / "lib"), str(tmp_path))

    procs = []
    for sid in ("platform", "contract-mastery", "ehr-integration"):
        p = subprocess.Popen(
            [sys.executable, "-c", script, sid],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        procs.append((sid, p))

    for sid, p in procs:
        rc = p.wait(timeout=15)
        assert rc == 0, p.stderr.read()

    full = spec_state.read_full_state(str(tmp_path))
    for sid in ("platform", "contract-mastery", "ehr-integration"):
        assert full["specs"][sid]["lifecycle_state"] == "SPRINT_PLANNING", (
            f"spec {sid!r} expected SPRINT_PLANNING, got "
            f"{full['specs'][sid]['lifecycle_state']!r} — concurrent write lost the update"
        )


# ─── 3b. Issue #24 — multi-spec write-path / DoD-read-path regressions ──────


def _story(story_id: str, state: str) -> dict:
    """Minimal story record in an arbitrary pipeline state."""
    return {
        "id": story_id,
        "title": story_id,
        "state": state,
        "blocked_reason": None,
        "blocked_from": None,
        "backend": {},
        "pipeline_log": [{"state": state, "entered_at": "t0", "exited_at": None}],
        "dod": None,
        "receipts": [],
        "retries": {},
        "rejection_feedback": [],
    }


def test_transition_story_preserves_envelope(tmp_path: Path) -> None:
    """Bug A: `transition_story` → done writes the same dict twice (write,
    DoD eval, write). Before the fix, the first write popped the multi-spec
    breadcrumbs, so the second write flattened the envelope and deleted the
    inactive spec. Assert both specs and the envelope survive."""
    (tmp_path / ".synaptory" / ".orchestrator").mkdir(parents=True)
    spec_state.write_full_state(str(tmp_path), {
        "version": "3.0",
        "build_mode": "scrum",
        "active_spec": "stars",
        "specs": {
            "stars": {
                "lifecycle_state": "SPRINT_EXECUTION",
                "current_sprint": 1,
                "current_stories": [_story("SA-001", "reviewing")],
                "lifecycle_history": [],
            },
            "venue": {
                "lifecycle_state": "SPRINT_EXECUTION",
                "current_sprint": 3,
                "current_stories": [_story("VEN-009", "in_progress")],
                "lifecycle_history": [],
            },
        },
    })

    # active_spec drives resolution; no env needed.
    sm.transition_story(str(tmp_path), "SA-001", "done")

    full = spec_state.read_full_state(str(tmp_path))
    # Envelope intact.
    assert full.get("version") == "3.0"
    assert set(full.get("specs", {})) == {"stars", "venue"}, (
        "inactive spec was deleted — envelope was flattened"
    )
    # Inactive spec untouched.
    assert full["specs"]["venue"]["current_sprint"] == 3
    assert full["specs"]["venue"]["current_stories"][0]["state"] == "in_progress"
    # Active spec's story advanced and DoD was evaluated (not None).
    assert full["specs"]["stars"]["current_stories"][0]["state"] == "done"
    assert full["specs"]["stars"]["current_stories"][0]["dod"] is not None


def test_dod_reads_spec_scoped_receipts(tmp_path: Path) -> None:
    """Bug B: DoD aggregation must read `specs/<active>/receipts/`, not the
    shared dir. Write a properly-shaped SE/QE receipt under the spec dir and
    assert the gate credits the executed checks instead of scoring 0%."""
    import story_pipeline as sp

    spec_receipts = (tmp_path / ".synaptory" / ".orchestrator"
                     / "specs" / "stars" / "receipts")
    spec_receipts.mkdir(parents=True)
    spec_state.write_full_state(str(tmp_path), {
        "version": "3.0", "build_mode": "scrum", "active_spec": "stars",
        "specs": {"stars": {"lifecycle_state": "SPRINT_EXECUTION",
                            "current_sprint": 1, "current_stories": [],
                            "lifecycle_history": []}},
    })
    green = {"verification_commands": [{"cmd": "pytest", "exit_code": 0}],
             "metrics": {"findings_critical": 0, "coverage_delta": "+1.0%"},
             "status": "complete"}
    (spec_receipts / "SA-001-se.json").write_text(
        json.dumps({**green, "agent": "software-engineer"}))
    (spec_receipts / "SA-001-qe.json").write_text(
        json.dumps({**green, "agent": "quality-engineer"}))

    # active_spec resolves via the state file; no env required.
    result = sp.evaluate_story_dod(str(tmp_path), "SA-001", "early")

    assert result["checks"]["tests_pass"]["passed"] is True, result
    assert result["passed"] is True, (
        "DoD failed — receipts not found in the spec-scoped dir"
    )


# ─── 4. JQL injection ──────────────────────────────────────────────────────


def _make_adapter(filter_type: str, value: str, sprint_prefix: str = "") -> JiraAdapter:
    spec = SpecConfig(
        id="x", name="X",
        jira=JiraConfig(url="https://x.atlassian.net", project_key="HT", board_id=1,
                        sprint_milestone_prefix=sprint_prefix),
        filter=SpecFilter(type=filter_type, value=value),
    )
    cfg = TrackerConfig(backend="jira", jira=spec.jira)
    return JiraAdapter(Path("/tmp"), cfg, spec=spec)


def test_jql_filter_clause_per_type() -> None:
    ad = _make_adapter("label", "platform")
    assert ad._spec_filter_clause() == '(labels = "platform")'

    ad = _make_adapter("component", "Multi-tenant")
    assert ad._spec_filter_clause() == '(component = "Multi-tenant")'

    ad = _make_adapter("epic", "HT-100")
    assert ad._spec_filter_clause() == '("Epic Link" = HT-100)'

    # jql is wrapped in parens so callers' `AND <clause>` doesn't risk
    # operator precedence surprises.
    raw = 'labels = "a" OR labels = "b"'
    ad = _make_adapter("jql", raw)
    assert ad._spec_filter_clause() == f"({raw})"


def test_jql_filter_refuses_writes() -> None:
    ad = _make_adapter("jql", 'labels = "audit"')
    with pytest.raises(Exception) as exc:
        ad._ensure_spec_writeable("create_ticket")
    assert "read-only" in str(exc.value)


def test_legacy_adapter_has_empty_clause() -> None:
    cfg = TrackerConfig(
        backend="jira",
        jira=JiraConfig(url="https://x.atlassian.net", project_key="HT"),
    )
    ad = JiraAdapter(Path("/tmp"), cfg)  # no spec
    assert ad._spec_filter_clause() == ""


# ─── 4b. Per-spec sprint prefix + scoping (issue #102) ─────────────────────


def test_sprint_number_parse_no_prefix_is_backward_compatible() -> None:
    # Empty prefix ⇒ legacy board-wide `Sprint N` parsing, unchanged.
    ad = _make_adapter("label", "venue-agent")
    assert ad._extract_sprint_number("Sprint 1") == 1
    assert ad._extract_sprint_number("Sprint 12") == 12
    # Separator-tolerance also applies with no prefix.
    assert ad._extract_sprint_number("Sprint_3") == 3
    assert ad._extract_sprint_number("no sprint here") is None


def test_sprint_number_parse_with_prefix_scopes_and_strips() -> None:
    ad = _make_adapter("label", "venue-agent", sprint_prefix="VA_")
    # In-scope names: prefix stripped, number parsed (both separators).
    assert ad._extract_sprint_number("VA_Sprint 1") == 1
    assert ad._extract_sprint_number("VA_Sprint_1") == 1
    # Out-of-scope: another spec's prefix on the shared board is invisible,
    # which is what isolates list_sprints / get_velocity_data per spec.
    assert ad._extract_sprint_number("SA_Sprint 1") is None
    # Even a bare `Sprint 1` (some other team's sprint) is excluded when a
    # prefix is configured — no cross-spec collision on number 1.
    assert ad._extract_sprint_number("Sprint 1") is None


def test_create_sprint_name_uses_prefix(monkeypatch) -> None:
    ad = _make_adapter("label", "venue-agent", sprint_prefix="VA_")
    captured: dict = {}

    def fake_create_sprint(board_id, name, goal):
        captured["name"] = name
        return {"id": 77}

    monkeypatch.setattr(ad.transport, "create_sprint", fake_create_sprint)
    monkeypatch.setattr(ad, "_save_id_mapping", lambda *a, **k: None)
    from tracker.base import SprintInfo
    ad.create_sprint(SprintInfo(number=1, goal="ship it"))
    assert captured["name"] == "VA_Sprint 1"


def test_jira_config_parses_sprint_milestone_prefix(tmp_path: Path) -> None:
    (tmp_path / ".synaptory.yaml").write_text(
        textwrap.dedent(
            """
            tracker:
              backend: jira
              jira:
                url: https://x.atlassian.net
                project_key: STAR
                board_id: 433
                sprint_milestone_prefix: "VA_"
            """
        ).strip(),
        encoding="utf-8",
    )
    cfg = TrackerConfig.load(tmp_path)
    assert cfg.jira.sprint_milestone_prefix == "VA_"


# ─── 5. Sentinel round-trip ────────────────────────────────────────────────


def test_sentinel_rollup_multi_spec(tmp_path: Path) -> None:
    spec_state.write_full_state(str(tmp_path), {
        "version": "3.0",
        "build_mode": "scrum",
        "active_spec": "platform",
        "specs": {
            "platform": {
                "lifecycle_state": "SPRINT_EXECUTION",
                "current_sprint": 9, "current_stories": [],
            },
            "contract-mastery": {
                "lifecycle_state": "INCEPTION",
                "current_sprint": 0, "current_stories": [],
            },
        },
    })
    (tmp_path / "CLAUDE.md").write_text("# proj\n\n")

    rollup = ucm.write_sentinel_rollup(str(tmp_path))
    assert rollup["active_spec"] == "platform"
    assert isinstance(rollup["specs"], dict)
    assert "SPRINT_EXECUTION sprint=9" in rollup["specs"]["platform"]

    parsed = ucm.read_sentinel(str(tmp_path))
    assert parsed["active_spec"] == "platform"
    assert "platform" in parsed["specs"]
    assert "contract-mastery" in parsed["specs"]


def test_sentinel_round_trip_v2(tmp_path: Path) -> None:
    spec_state.write_full_state(str(tmp_path), {
        "version": "2.0",
        "build_mode": "scrum",
        "lifecycle_state": "SPRINT_EXECUTION",
        "current_sprint": 5, "current_stories": [],
    })
    (tmp_path / "CLAUDE.md").write_text("# proj\n")

    rollup = ucm.write_sentinel_rollup(str(tmp_path))
    assert rollup["lifecycle_state"] == "SPRINT_EXECUTION"
    parsed = ucm.read_sentinel(str(tmp_path))
    assert parsed["lifecycle_state"] == "SPRINT_EXECUTION"
    assert parsed["current_sprint"] == "5"  # sentinel values stringify
    assert "specs" not in parsed  # v2 must not emit a specs: block


# ─── 6. Receipts isolation ─────────────────────────────────────────────────


def test_receipts_path_differs_per_spec(tmp_path: Path) -> None:
    """When the JiraAdapter is constructed with two different specs, its
    cache paths (including future receipts location) are namespaced under
    distinct directories."""
    jira = JiraConfig(url="https://x.atlassian.net", project_key="HT", board_id=1)
    cfg = TrackerConfig(backend="jira", jira=jira)

    sp_a = SpecConfig(id="platform", name="P", jira=jira,
                      filter=SpecFilter(type="label", value="platform"))
    sp_b = SpecConfig(id="contract-mastery", name="CM", jira=jira,
                      filter=SpecFilter(type="label", value="contract-mastery"))

    ad_a = JiraAdapter(tmp_path, cfg, spec=sp_a)
    ad_b = JiraAdapter(tmp_path, cfg, spec=sp_b)

    assert ad_a._id_map_path != ad_b._id_map_path
    assert "specs/platform/" in str(ad_a._id_map_path)
    assert "specs/contract-mastery/" in str(ad_b._id_map_path)

    # Save an id-mapping under each — they must end up in separate files.
    ad_a._save_id_mapping("US-1", "HT-1", "")
    ad_b._save_id_mapping("US-2", "HT-2", "")
    assert json.loads(ad_a._id_map_path.read_text()) == {
        "US-1": {"jira_key": "HT-1", "jira_url": ""}
    }
    assert json.loads(ad_b._id_map_path.read_text()) == {
        "US-2": {"jira_key": "HT-2", "jira_url": ""}
    }


# ─── Bonus: mixed-mode rejection (design §9) ───────────────────────────────


def test_mixed_mode_rejected(tmp_path: Path) -> None:
    (tmp_path / ".synaptory.yaml").write_text(
        textwrap.dedent(
            """
            tracker:
              backend: jira
              jira:
                project_key: WRONG
            specs:
              - id: platform
                name: P
                jira:
                  project_key: HT
                  filter:
                    type: label
                    value: platform
            """
        ).strip(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Mixed-mode conflict"):
        TrackerConfig.load(tmp_path)


def test_duplicate_spec_id_rejected(tmp_path: Path) -> None:
    (tmp_path / ".synaptory.yaml").write_text(
        textwrap.dedent(
            """
            tracker:
              backend: jira
            specs:
              - id: platform
                name: A
                jira:
                  project_key: HT
                  filter:
                    type: label
                    value: a
              - id: platform
                name: B
                jira:
                  project_key: HT
                  filter:
                    type: label
                    value: b
            """
        ).strip(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate spec id"):
        TrackerConfig.load(tmp_path)


def test_invalid_filter_type_rejected(tmp_path: Path) -> None:
    (tmp_path / ".synaptory.yaml").write_text(
        textwrap.dedent(
            """
            tracker:
              backend: jira
            specs:
              - id: platform
                name: A
                jira:
                  project_key: HT
                  filter:
                    type: wrong
                    value: a
            """
        ).strip(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="filter.type"):
        TrackerConfig.load(tmp_path)


# ─── 7. Per-spec Jira status_map override ──────────────────────────────────


def test_parse_status_map_normalizes_and_filters() -> None:
    """Key normalization (case, `-`/space → `_`), verbatim values, and
    graceful handling of unknown keys / non-dict input."""
    from tracker.config import _parse_status_map

    parsed = _parse_status_map(
        {
            "status_map": {
                "in_review": "IN CODE REVIEW",
                "AWAITING-ACCEPTANCE": "WAITING FOR PO ACCEPTANCE",
                "in progress": "Doing",
                "bogus": "ignored",  # unknown canonical key → dropped
            }
        }
    )
    assert parsed == {
        "IN_REVIEW": "IN CODE REVIEW",
        "AWAITING_ACCEPTANCE": "WAITING FOR PO ACCEPTANCE",
        "IN_PROGRESS": "Doing",
    }

    # Non-dict status_map and empty jira config ⇒ empty (defaults apply).
    assert _parse_status_map({"status_map": "nope"}) == {}
    assert _parse_status_map({"url": "u", "project_key": "K"}) == {}


def test_jira_config_parses_status_map_top_level(tmp_path: Path) -> None:
    """Top-level `tracker.jira.status_map` lands on JiraConfig.status_map.

    The lite YAML parser cannot descend into a 4-level-nested map, so — like
    Teamwork's workflow_stages — the canonical keys are accepted flattened
    under `jira:` (and still normalized to canonical form)."""
    (tmp_path / ".synaptory.yaml").write_text(
        textwrap.dedent(
            """
            tracker:
              backend: jira
              jira:
                url: https://x.atlassian.net
                project_key: STAR
                board_id: 433
                IN_REVIEW: "IN CODE REVIEW"
                AWAITING_ACCEPTANCE: "WAITING FOR PO ACCEPTANCE"
            """
        ).strip(),
        encoding="utf-8",
    )
    cfg = TrackerConfig.load(tmp_path)
    assert cfg.jira.status_map == {
        "IN_REVIEW": "IN CODE REVIEW",
        "AWAITING_ACCEPTANCE": "WAITING FOR PO ACCEPTANCE",
    }


def test_jira_config_status_map_empty_by_default(tmp_path: Path) -> None:
    """No status_map ⇒ empty dict ⇒ identical behavior to today."""
    (tmp_path / ".synaptory.yaml").write_text(
        textwrap.dedent(
            """
            tracker:
              backend: jira
              jira:
                url: https://x.atlassian.net
                project_key: STAR
            """
        ).strip(),
        encoding="utf-8",
    )
    cfg = TrackerConfig.load(tmp_path)
    assert cfg.jira.status_map == {}


def test_spec_jira_config_parses_status_map(tmp_path: Path) -> None:
    """Per-spec `status_map` (nested dict, parsed by the stack parser)
    lands on the spec's JiraConfig with normalized keys."""
    (tmp_path / ".synaptory.yaml").write_text(
        textwrap.dedent(
            """
            tracker:
              backend: jira
            specs:
              - id: stars-assistant
                name: STARS Assistant
                jira:
                  project_key: STAR
                  status_map:
                    in_review: "IN CODE REVIEW"
                    awaiting_acceptance: "WAITING FOR PO ACCEPTANCE"
                  filter:
                    type: label
                    value: stars
            """
        ).strip(),
        encoding="utf-8",
    )
    cfg = TrackerConfig.load(tmp_path)
    spec = cfg.find_spec("stars-assistant")
    assert spec is not None
    assert spec.jira.status_map == {
        "IN_REVIEW": "IN CODE REVIEW",
        "AWAITING_ACCEPTANCE": "WAITING FOR PO ACCEPTANCE",
    }


def _make_status_adapter(status_map: dict) -> JiraAdapter:
    spec = SpecConfig(
        id="x", name="X",
        jira=JiraConfig(url="https://x.atlassian.net", project_key="HT",
                        board_id=1, status_map=status_map),
        filter=SpecFilter(type="label", value="x"),
    )
    cfg = TrackerConfig(backend="jira", jira=spec.jira)
    return JiraAdapter(Path("/tmp"), cfg, spec=spec)


def test_update_story_status_targets_override(monkeypatch) -> None:
    """update_story_status transitions to the overridden Jira name when set,
    and falls back to the built-in STATUS_MAP name when the state is not in
    the override."""
    ad = _make_status_adapter({"IN_REVIEW": "IN CODE REVIEW"})
    captured: list = []

    monkeypatch.setattr(ad, "_validate_status_transition", lambda *a, **k: None)
    monkeypatch.setattr(ad, "_resolve_id", lambda sid: "HT-1")
    monkeypatch.setattr(ad, "_update_local_cache_status", lambda *a, **k: None)
    monkeypatch.setattr(ad, "get_story", lambda sid: None)
    monkeypatch.setattr(ad.transport, "transition_issue",
                        lambda key, name: captured.append(name))

    ad.update_story_status("SA-001", "IN_REVIEW")
    assert captured == ["IN CODE REVIEW"]

    # A state NOT in the override falls back to the built-in STATUS_MAP.
    captured.clear()
    ad.update_story_status("SA-001", "IN_PROGRESS")
    assert captured == ["In Progress"]


def test_update_story_status_no_override_is_backward_compatible(monkeypatch) -> None:
    ad = _make_status_adapter({})
    captured: list = []
    monkeypatch.setattr(ad, "_validate_status_transition", lambda *a, **k: None)
    monkeypatch.setattr(ad, "_resolve_id", lambda sid: "HT-1")
    monkeypatch.setattr(ad, "_update_local_cache_status", lambda *a, **k: None)
    monkeypatch.setattr(ad, "get_story", lambda sid: None)
    monkeypatch.setattr(ad.transport, "transition_issue",
                        lambda key, name: captured.append(name))
    ad.update_story_status("SA-001", "IN_REVIEW")
    assert captured == ["In Review"]  # built-in STATUS_MAP default


def test_status_readback_honors_override() -> None:
    """A custom Jira status name reads back as the canonical state via the
    per-spec override; without an override the built-in reverse table wins."""
    ad = _make_status_adapter({"AWAITING_ACCEPTANCE": "WAITING FOR PO ACCEPTANCE"})
    issue = {
        "key": "HT-9",
        "fields": {"summary": "s", "status": {"name": "WAITING FOR PO ACCEPTANCE"}},
    }
    story = ad._issue_to_story(issue)
    assert story.status == "AWAITING_ACCEPTANCE"

    # Standard names still resolve through STATUS_REVERSE unchanged.
    issue2 = {"key": "HT-10", "fields": {"summary": "s", "status": {"name": "In Progress"}}}
    assert ad._issue_to_story(issue2).status == "IN_PROGRESS"


def test_status_reverse_normalized_no_status_map() -> None:
    """A Jira status differing only by case/separators reads back to its
    canonical state WITHOUT any status_map entry."""
    ad = _make_status_adapter({})
    for name in ("IN REVIEW", "in_review", "In-Review", "in review"):
        issue = {"key": "HT-1", "fields": {"summary": "s", "status": {"name": name}}}
        assert ad._issue_to_story(issue).status == "IN_REVIEW"


def test_status_reverse_explicit_map_wins_over_normalized() -> None:
    """An explicit status_map entry (a genuinely different word) still wins on
    read-back over the normalized built-in table."""
    ad = _make_status_adapter({"AWAITING_ACCEPTANCE": "WAITING FOR PO ACCEPTANCE"})
    issue = {"key": "HT-1",
             "fields": {"summary": "s", "status": {"name": "WAITING FOR PO ACCEPTANCE"}}}
    assert ad._issue_to_story(issue).status == "AWAITING_ACCEPTANCE"


def _mock_transitions(monkeypatch, ad, to_name: str) -> list:
    """Wire ad.transport._api_call so the issue offers a single transition
    whose destination status is `to_name`; capture the POSTed transition id."""
    posted: list = []

    def fake_api_call(method, path, body=None):
        if method == "GET" and path.endswith("/transitions"):
            return {"transitions": [{"id": "42", "name": to_name,
                                     "to": {"name": to_name}}]}
        if method == "POST" and path.endswith("/transitions"):
            posted.append(body["transition"]["id"])
            return {}
        return {"key": "HT-1", "fields": {}}

    monkeypatch.setattr(ad.transport, "_api_call", fake_api_call)
    monkeypatch.setattr(ad, "_validate_status_transition", lambda *a, **k: None)
    monkeypatch.setattr(ad, "_resolve_id", lambda sid: "HT-1")
    monkeypatch.setattr(ad, "_update_local_cache_status", lambda *a, **k: None)
    monkeypatch.setattr(ad, "get_story", lambda sid: None)
    return posted


@pytest.mark.parametrize("to_name", ["To Do", "TO DO", "to_do", "To-Do"])
def test_update_story_status_normalized_transition_match(monkeypatch, to_name) -> None:
    """TO_DO (no status_map) resolves a transition whose destination status
    differs only by case/separators — all forms match via normalization."""
    ad = _make_status_adapter({})
    posted = _mock_transitions(monkeypatch, ad, to_name)
    ad.update_story_status("SA-001", "TO_DO")
    assert posted == ["42"]


def test_update_story_status_explicit_map_still_overrides(monkeypatch) -> None:
    """A status_map entry (a genuinely different word) is what we match the
    transition against — the normalized default does not shadow it."""
    ad = _make_status_adapter({"BACKLOG": "Open"})
    posted = _mock_transitions(monkeypatch, ad, "Open")
    ad.update_story_status("SA-001", "BACKLOG")
    assert posted == ["42"]


# ─── 8. Per-entity Jira issue_type override ─────────────────────────────────


def test_parse_issue_type_string_maps_story_path() -> None:
    """String form applies to the story path only (story + enhancement)."""
    from tracker.config import _parse_issue_type
    assert _parse_issue_type({"issue_type": "Synaptory"}) == {
        "story": "Synaptory", "enhancement": "Synaptory",
    }
    # Empty / missing ⇒ empty ⇒ ISSUE_TYPES defaults.
    assert _parse_issue_type({"issue_type": ""}) == {}
    assert _parse_issue_type({"url": "u"}) == {}


def test_parse_issue_type_dict_per_entity_and_unknown_dropped() -> None:
    """Dict form maps per-entity; keys lowercased; unknown keys dropped."""
    from tracker.config import _parse_issue_type
    parsed = _parse_issue_type(
        {"issue_type": {"Story": "Synaptory", "EPIC": "Initiative",
                        "bug": "Defect", "task": "Task", "bogus": "x"}}
    )
    assert parsed == {"story": "Synaptory", "epic": "Initiative",
                      "bug": "Defect", "task": "Task"}


def test_jira_config_parses_issue_type_dict_top_level(tmp_path: Path) -> None:
    """Top-level dict form — flattened under `jira:` by the lite YAML parser —
    lands on JiraConfig.issue_type_overrides per entity."""
    (tmp_path / ".synaptory.yaml").write_text(
        textwrap.dedent(
            """
            tracker:
              backend: jira
              jira:
                url: https://x.atlassian.net
                project_key: STAR
                issue_type:
                  story: Synaptory
                  epic: Initiative
                  bug: Defect
            """
        ).strip(),
        encoding="utf-8",
    )
    cfg = TrackerConfig.load(tmp_path)
    assert cfg.jira.issue_type_overrides == {
        "story": "Synaptory", "epic": "Initiative", "bug": "Defect",
    }


def test_jira_config_parses_issue_type_string_top_level(tmp_path: Path) -> None:
    """Top-level string form maps the story path (story + enhancement)."""
    (tmp_path / ".synaptory.yaml").write_text(
        textwrap.dedent(
            """
            tracker:
              backend: jira
              jira:
                url: https://x.atlassian.net
                project_key: STAR
                issue_type: "Synaptory"
            """
        ).strip(),
        encoding="utf-8",
    )
    cfg = TrackerConfig.load(tmp_path)
    assert cfg.jira.issue_type_overrides == {
        "story": "Synaptory", "enhancement": "Synaptory",
    }


def test_jira_config_issue_type_empty_by_default(tmp_path: Path) -> None:
    """No issue_type ⇒ empty overrides ⇒ ISSUE_TYPES defaults apply."""
    (tmp_path / ".synaptory.yaml").write_text(
        textwrap.dedent(
            """
            tracker:
              backend: jira
              jira:
                url: https://x.atlassian.net
                project_key: STAR
            """
        ).strip(),
        encoding="utf-8",
    )
    cfg = TrackerConfig.load(tmp_path)
    assert cfg.jira.issue_type_overrides == {}


def test_spec_jira_config_parses_issue_type_dict(tmp_path: Path) -> None:
    """Per-spec dict form (nested, parsed by the stack parser) lands per
    entity; a sibling spec without the key gets empty overrides."""
    (tmp_path / ".synaptory.yaml").write_text(
        textwrap.dedent(
            """
            tracker:
              backend: jira
            specs:
              - id: stars-assistant
                name: STARS Assistant
                jira:
                  project_key: STAR
                  issue_type:
                    story: Synaptory
                    epic: Initiative
                  filter:
                    type: label
                    value: stars
              - id: venue-agent
                name: Venue Agent
                jira:
                  project_key: VEN
                  filter:
                    type: label
                    value: venue
            """
        ).strip(),
        encoding="utf-8",
    )
    cfg = TrackerConfig.load(tmp_path)
    sa = cfg.find_spec("stars-assistant")
    va = cfg.find_spec("venue-agent")
    assert sa is not None and sa.jira.issue_type_overrides == {
        "story": "Synaptory", "epic": "Initiative",
    }
    assert va is not None and va.jira.issue_type_overrides == {}


def _make_issue_type_adapter(overrides: dict | None = None) -> JiraAdapter:
    spec = SpecConfig(
        id="x", name="X",
        jira=JiraConfig(url="https://x.atlassian.net", project_key="HT",
                        board_id=1, issue_type_overrides=overrides or {}),
        filter=SpecFilter(type="label", value="x"),
    )
    cfg = TrackerConfig(backend="jira", jira=spec.jira)
    return JiraAdapter(Path("/tmp"), cfg, spec=spec)


def test_jira_issue_type_resolver_defaults() -> None:
    """No overrides ⇒ ISSUE_TYPES defaults; unknown kind ⇒ 'Story'."""
    ad = _make_issue_type_adapter()
    assert ad._jira_issue_type("story") == "Story"
    assert ad._jira_issue_type("epic") == "Epic"
    assert ad._jira_issue_type("bug") == "Bug"
    assert ad._jira_issue_type("task") == "Task"
    assert ad._jira_issue_type("subtask") == "Sub-task"
    assert ad._jira_issue_type("mystery") == "Story"


def test_create_ticket_uses_per_entity_issue_type(monkeypatch) -> None:
    """create_ticket sends the per-entity override for story/bug/epic and the
    ISSUE_TYPES default where no override is set."""
    ad = _make_issue_type_adapter({"story": "Synaptory", "bug": "Defect"})
    captured: list = []
    monkeypatch.setattr(ad, "_ensure_spec_writeable", lambda *a, **k: None)
    monkeypatch.setattr(ad, "_resolve_template_path", lambda *a, **k: None)
    monkeypatch.setattr(ad, "_apply_spec_write_side_effect",
                        lambda labels, parent_key: (labels, parent_key, {}))
    monkeypatch.setattr(ad, "_save_id_mapping", lambda *a, **k: None)
    monkeypatch.setattr(
        ad.transport, "create_issue",
        lambda **kw: captured.append(kw["issue_type"]) or {"key": "HT-1"},
    )
    ad.create_ticket(Story(id="SA-001", title="A story"))
    ad.create_ticket(Story(id="B-001", title="A bug"))
    ad.create_ticket(Story(id="T-001", title="A task"))   # no override ⇒ default
    ad.create_ticket(Story(id="EPIC-1", title="An epic"))  # no override ⇒ default
    assert captured == ["Synaptory", "Defect", "Task", "Epic"]


def test_create_ticket_default_issue_type_is_story(monkeypatch) -> None:
    """No overrides ⇒ story create still uses "Story" (backward compatible)."""
    ad = _make_issue_type_adapter()
    captured: list = []
    monkeypatch.setattr(ad, "_ensure_spec_writeable", lambda *a, **k: None)
    monkeypatch.setattr(ad, "_resolve_template_path", lambda *a, **k: None)
    monkeypatch.setattr(ad, "_apply_spec_write_side_effect",
                        lambda labels, parent_key: (labels, parent_key, {}))
    monkeypatch.setattr(ad, "_save_id_mapping", lambda *a, **k: None)
    monkeypatch.setattr(
        ad.transport, "create_issue",
        lambda **kw: captured.append(kw["issue_type"]) or {"key": "HT-1"},
    )
    ad.create_ticket(Story(id="SA-001", title="A story"))
    assert captured == ["Story"]


def test_create_aux_ticket_uses_per_entity_issue_type(monkeypatch) -> None:
    """create_aux_ticket honors task/bug/enhancement overrides."""
    ad = _make_issue_type_adapter({"task": "Chore", "bug": "Defect"})
    captured: list = []
    monkeypatch.setattr(ad, "_ensure_spec_writeable", lambda *a, **k: None)
    monkeypatch.setattr(ad, "_apply_spec_write_side_effect",
                        lambda labels, parent_key: (labels, parent_key, {}))
    monkeypatch.setattr(
        ad.transport, "create_issue",
        lambda **kw: captured.append(kw["issue_type"]) or {"key": "HT-1"},
    )
    ad.create_aux_ticket("task", "A task")
    ad.create_aux_ticket("bug", "A bug")
    ad.create_aux_ticket("enhancement", "An enh")  # no override ⇒ Story default
    assert captured == ["Chore", "Defect", "Story"]


def test_list_epics_honors_epic_override(monkeypatch) -> None:
    """list_epics builds JQL with the epic override (was hardcoded 'Epic')."""
    ad = _make_issue_type_adapter({"epic": "Initiative"})
    captured: list = []
    monkeypatch.setattr(ad, "_spec_filter_clause", lambda: "")
    monkeypatch.setattr(ad.transport, "search_issues",
                        lambda jql, **k: captured.append(jql) or [])
    ad.list_epics()
    assert 'issuetype = "Initiative"' in captured[0]


def test_list_stories_and_backlog_use_story_override(monkeypatch) -> None:
    """list_stories and get_backlog build JQL with the story override."""
    ad = _make_issue_type_adapter({"story": "Synaptory"})
    captured: list = []
    monkeypatch.setattr(ad, "_spec_filter_clause", lambda: "")
    monkeypatch.setattr(ad.transport, "search_issues",
                        lambda jql, **k: captured.append(jql) or [])
    ad.list_stories()
    ad.get_backlog()
    assert all('issuetype = "Synaptory"' in jql for jql in captured)
    assert len(captured) == 2


def test_list_stories_jql_quotes_multi_word_override(monkeypatch) -> None:
    """A custom type name containing spaces must be quoted in JQL — unquoted
    `issuetype = User Story` is a Jira 400."""
    ad = _make_issue_type_adapter({"story": "User Story"})
    captured: list = []
    monkeypatch.setattr(ad, "_spec_filter_clause", lambda: "")
    monkeypatch.setattr(ad.transport, "search_issues",
                        lambda jql, **k: captured.append(jql) or [])
    ad.list_stories()
    assert 'issuetype = "User Story"' in captured[0]


def test_list_stories_jql_default_is_story(monkeypatch) -> None:
    """No override ⇒ JQL keeps `issuetype = Story` (backward compatible)."""
    ad = _make_issue_type_adapter()
    captured: list = []
    monkeypatch.setattr(ad, "_spec_filter_clause", lambda: "")
    monkeypatch.setattr(ad.transport, "search_issues",
                        lambda jql, **k: captured.append(jql) or [])
    ad.list_stories()
    assert 'issuetype = "Story"' in captured[0]


def test_query_tickets_maps_mixed_types(monkeypatch) -> None:
    """query_tickets maps each requested ticket_type by its entity — overrides
    where set, ISSUE_TYPES defaults otherwise."""
    from tracker.base import QueryFilter
    ad = _make_issue_type_adapter({"story": "Synaptory", "epic": "Initiative"})
    captured: list = []
    monkeypatch.setattr(ad, "_spec_filter_clause", lambda: "")
    monkeypatch.setattr(ad.transport, "search_issues",
                        lambda jql, **k: captured.append(jql) or [])
    ad.query_tickets(QueryFilter(ticket_type=["story", "epic", "bug"]))
    jql = captured[0]
    assert 'issuetype IN ("Synaptory", "Initiative", "Bug")' in jql


def test_sprint_backlog_includes_custom_story_type(monkeypatch) -> None:
    """get_sprint_backlog accepts the resolved story/task types (issue_type
    override) alongside the built-in defaults — custom-typed stories are no
    longer dropped from sprint backlog/metrics/velocity."""
    ad = _make_issue_type_adapter({"story": "Synaptory"})
    issues = [
        {"key": "HT-1", "fields": {"summary": "custom", "status": {"name": "To Do"},
                                   "issuetype": {"name": "Synaptory"}}},
        {"key": "HT-2", "fields": {"summary": "default", "status": {"name": "To Do"},
                                   "issuetype": {"name": "Story"}}},
        {"key": "HT-3", "fields": {"summary": "epic", "status": {"name": "To Do"},
                                   "issuetype": {"name": "Epic"}}},
    ]
    monkeypatch.setattr(ad, "_get_sprint_by_number", lambda n: {"id": 5})
    monkeypatch.setattr(ad.transport, "get_sprint_issues", lambda sid: issues)
    backlog = ad.get_sprint_backlog(1)
    assert len(backlog) == 2  # custom + default story types; epic excluded


def test_query_tickets_status_honors_override(monkeypatch) -> None:
    """query_tickets(status=[...]) resolves status names through the per-spec
    status_map override before the built-in STATUS_MAP."""
    from tracker.base import QueryFilter
    ad = _make_status_adapter({"BACKLOG": "Open"})
    captured: list = []
    monkeypatch.setattr(ad, "_spec_filter_clause", lambda: "")
    monkeypatch.setattr(ad.transport, "search_issues",
                        lambda jql, **k: captured.append(jql) or [])
    ad.query_tickets(QueryFilter(status=["BACKLOG", "IN_PROGRESS"]))
    assert 'status IN ("Open", "In Progress")' in captured[0]


def test_move_incomplete_honors_done_override(monkeypatch) -> None:
    """move_incomplete_to_next treats an overridden DONE name (e.g.
    "Finished") as terminal instead of carrying those stories forward."""
    ad = _make_status_adapter({"DONE": "Finished"})
    issues = [
        {"key": "HT-1", "fields": {"summary": "done", "status": {"name": "Finished"},
                                   "issuetype": {"name": "Story"}}},
        {"key": "HT-2", "fields": {"summary": "open", "status": {"name": "In Progress"},
                                   "issuetype": {"name": "Story"}}},
    ]
    monkeypatch.setattr(ad, "_get_sprint_by_number",
                        lambda n: {"id": n})
    monkeypatch.setattr(ad.transport, "get_sprint_issues", lambda sid: issues)
    moved_keys: list = []
    monkeypatch.setattr(ad.transport, "move_issues_to_sprint",
                        lambda sid, keys: moved_keys.extend(keys))
    incomplete = ad.move_incomplete_to_next(1, 2)
    assert moved_keys == ["HT-2"]
    assert len(incomplete) == 1
