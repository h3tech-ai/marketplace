"""Layer 1 — `/synaptory status` on a project migrated to the v3 envelope (#730).

The reported symptom: a project migrated by the bundled `migrate_to_multispec.py`
renders sprint 0, zero stories and DoD pending while a sprint is actively
executing, with nothing saying the numbers are wrong. Three defects behind it:

  1. `assemble()` read `pipeline-state.json` raw. On v3 the per-spec fields live
     under `specs.<id>`, so every top-level lookup found nothing. The reporter's
     A/B: the same builders fed `specs["<active>"]` returned `stories_total=8`;
     fed the envelope, `0`.
  2. `load_receipts_raw()` globbed only `.orchestrator/receipts`. Migration MOVES
     receipts to `.orchestrator/specs/<id>/receipts/` — 18 visible, 98 invisible.
  3. `load_config()` matched `^(\\w+):` against `line.strip()`, erasing
     indentation, so `config["name"]` was whichever `name:` came LAST. With a
     `specs:` list that is always the last spec's display name, and the header
     renamed the project every time a spec was appended.

**The fixture is built by running the real migration script**, not by
hand-writing a v3 file. That is the #509 lesson the board census records: a
fixture that writes the shape the reader already expects cannot detect the
defect. Here the migration is what moves the receipts and appends the `specs:`
block, so writing the end state by hand would remove defects 2 and 3 from the
test along with the layout.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

#: The counts the issue measured, kept as named constants so a drift in the
#: fixture cannot quietly turn into a drift in the assertions.
STORIES = 8
RECEIPTS_MOVED_BY_MIGRATION = 98
RECEIPTS_WRITTEN_AFTER = 18
PROJECT_NAME = "Acme Delivery Platform"
LAST_SPEC = "ehr-integration"


def _paths_for(repo_root: Path):
    return [str(repo_root / "core" / "lib"), str(repo_root / "core" / "scripts")]


@pytest.fixture
def summary_modules(repo_root: Path, monkeypatch):
    for entry in _paths_for(repo_root):
        if entry not in sys.path:
            monkeypatch.syspath_prepend(entry)
    import build_summary
    import summary.pipeline as summary_pipeline

    return build_summary, summary_pipeline


def _write_v2_project(project: Path) -> None:
    """A flat v2 scrum project mid-sprint: sprint 9, 8 stories, 98 receipts."""
    orch = project / ".synaptory" / ".orchestrator"
    (orch / "receipts").mkdir(parents=True)
    now = "2026-09-01T00:00:00+00:00"
    stories = [
        {
            "id": "US-%03d" % i,
            "title": "story %d" % i,
            "state": "done" if i <= 3 else "in_progress",
            "dod": {"passed": i <= 3, "checks": {"tests-pass": {"passed": i <= 3}}},
        }
        for i in range(1, STORIES + 1)
    ]
    (orch / "pipeline-state.json").write_text(json.dumps({
        "version": "2.0",
        "build_mode": "scrum",
        "lifecycle_state": "SPRINT_EXECUTION",
        "started_at": now,
        "lifecycle_history": [
            {"state": "INCEPTION", "entered_at": now, "exited_at": now},
            {"state": "SPRINT_EXECUTION", "entered_at": now, "exited_at": None},
        ],
        "inception": {"completed_at": now},
        "current_sprint": 9,
        "sprint_goal": "ship the search feature",
        "sprints_completed": [
            {"number": n, "stories_completed": 4, "stories_planned": 4,
             "velocity": 4, "dod_compliance": 100}
            for n in range(1, 9)
        ],
        "current_stories": stories,
    }), encoding="utf-8")

    for i in range(RECEIPTS_MOVED_BY_MIGRATION):
        story_id = "US-%03d" % ((i % STORIES) + 1)
        role = ("se", "qe", "cr")[i % 3]
        (orch / "receipts" / ("%s-%s-%d.json" % (story_id, role, i))).write_text(
            json.dumps({
                "task": "task %d" % i, "agent": "software-engineer",
                "backend": "claude", "model": "sonnet",
                "story_id": story_id, "role": role,
                "artifacts": ["src/mod_%d.py" % i],
                "verification_commands": ["pytest tests/test_%d.py" % i],
            }), encoding="utf-8")

    # `project.name` is NESTED, which is the whole of defect 3 — and it is where
    # the canonical schema puts it (skills/_shared/templates/synaptory.yaml.tmpl).
    (project / ".synaptory.yaml").write_text(
        'project:\n'
        '  name: "%s"\n'
        '  stack: "python"\n'
        'build_mode: scrum\n'
        'engagement: autonomous\n'
        'tracker:\n'
        '  backend: local\n' % PROJECT_NAME,
        encoding="utf-8")


def _write_post_migration_receipts(project: Path) -> None:
    """The receipts that land at the ROOT home after the migration.

    Both homes hold real receipts at once, which is the state the reporter
    measured: 18 visible, 98 invisible, 116 total.
    """
    root = project / ".synaptory" / ".orchestrator" / "receipts"
    root.mkdir(parents=True, exist_ok=True)
    for i in range(RECEIPTS_WRITTEN_AFTER):
        story_id = "US-%03d" % ((i % STORIES) + 1)
        (root / ("%s-post-%d.json" % (story_id, i))).write_text(json.dumps({
            "task": "post-migration %d" % i, "agent": "quality-engineer",
            "backend": "claude", "model": "sonnet",
            "story_id": story_id, "role": "qe",
            "artifacts": ["tests/test_post_%d.py" % i],
            "verification_commands": ["pytest -q"],
        }), encoding="utf-8")


@pytest.fixture
def migrated_project(tmp_path: Path, repo_root: Path) -> Path:
    """A project put on the v3 envelope by the bundled migration script itself."""
    project = tmp_path / "multispec-project"
    _write_v2_project(project)

    script = repo_root / "core" / "scripts" / "migrate_to_multispec.py"
    proc = subprocess.run(
        [sys.executable, str(script),
         "--project-dir", str(project),
         "--primary-spec", "platform",
         "--new-specs", "contract-mastery,%s" % LAST_SPEC,
         "--yes"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, (
        "the migration this test is about did not run:\n%s\n%s"
        % (proc.stdout, proc.stderr)
    )
    _write_post_migration_receipts(project)
    return project


# ── The layout the fixture actually produced ─────────────────────────────────


def test_the_fixture_is_really_on_the_v3_layout(migrated_project: Path):
    """Assert the shape before asserting anything read off it.

    A fixture that quietly stayed flat would make every test below pass while
    proving nothing about multi-spec — the failure mode the census file records
    for `conformance/factories.py`.
    """
    state = json.loads(
        (migrated_project / ".synaptory" / ".orchestrator" / "pipeline-state.json")
        .read_text(encoding="utf-8"))
    # The LAYOUT, which is what this fixture check is about. `version` no
    # longer answers it: #710 made that field carry the PRODUCT release, and
    # `state_schema` carry the layout. This assertion was written against the
    # old meaning and merged one minute after the change that retired it, so
    # it read `1.3.0` and failed. `state_schema()` is the only accessor
    # allowed to interpret a legacy `version` string as a layout selector.
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "core" / "lib"))
    from state_schema import state_schema

    assert state_schema(state) == 3, (
        "the migration did not produce a v3 layout: state_schema=%r version=%r"
        % (state.get("state_schema"), state.get("version")))
    assert state["active_spec"] == "platform"
    assert sorted(state["specs"]) == ["contract-mastery", LAST_SPEC, "platform"]
    assert "current_stories" not in state, (
        "the per-spec fields must have moved under specs.<id>; if they are "
        "still at the top level the migration did not happen"
    )

    orch = migrated_project / ".synaptory" / ".orchestrator"
    moved = list((orch / "specs" / "platform" / "receipts").glob("*.json"))
    root = list((orch / "receipts").glob("*.json"))
    assert len(moved) == RECEIPTS_MOVED_BY_MIGRATION
    assert len(root) == RECEIPTS_WRITTEN_AFTER


# ── Defect 1: the board ──────────────────────────────────────────────────────


def test_an_executing_sprint_is_not_reported_as_an_empty_pipeline(
    migrated_project: Path, summary_modules
):
    build_summary, _ = summary_modules
    summary = build_summary.assemble(migrated_project)

    assert summary["pipeline"]["lifecycle_state"] == "SPRINT_EXECUTION"
    assert summary["pipeline"]["state_available"] is True
    assert summary["dod_summary"]["stories_total"] == STORIES, (
        "the reporter's A/B: fed specs['platform'] these builders returned "
        "stories_total=%d, fed the envelope they returned 0. Got %r"
        % (STORIES, summary["dod_summary"])
    )
    assert summary["dod_summary"]["stories_evaluated"] == STORIES
    assert summary["dod_summary"]["stories_passed"] == 3
    assert summary["dod_summary"]["inception_gate"] == "approved"

    sprint = summary["sprint"]
    assert sprint["current_sprint"] == 9
    assert sprint["sprint_goal"] == "ship the search feature"
    assert sprint["sprints_completed"] == 8
    assert sprint["sprints"], "an active sprint must appear in the sprint rows"


def test_the_summary_names_every_spec_not_only_the_active_one(
    migrated_project: Path, summary_modules
):
    """The active slot drives the sprint; the other slots are still NAMED.

    `board_state` reports the active spec because a sprint number belongs to one
    spec and merging slots would invent a project-wide sprint. That is only
    defensible if nothing is dropped silently — so the report carries every
    spec with its own lifecycle and counts.
    """
    build_summary, _ = summary_modules
    board = build_summary.assemble(migrated_project)["board"]

    assert board["available"] is True
    assert board["layout"] == "multi-spec"
    assert board["build_mode"] == "scrum"
    assert board["active_spec"] == "platform"
    assert board["problems"] == []

    by_id = {spec["id"]: spec for spec in board["specs"]}
    assert sorted(by_id) == ["contract-mastery", LAST_SPEC, "platform"]
    assert by_id["platform"]["stories_total"] == STORIES
    assert by_id["platform"]["current_sprint"] == 9
    assert by_id[LAST_SPEC]["lifecycle_state"] == "INCEPTION"
    assert by_id[LAST_SPEC]["stories_total"] == 0


# ── Defect 2: the receipts ───────────────────────────────────────────────────


def test_receipts_are_counted_across_every_home(migrated_project: Path, summary_modules):
    build_summary, summary_pipeline = summary_modules

    raw = summary_pipeline.load_receipts_raw(migrated_project)
    total = RECEIPTS_MOVED_BY_MIGRATION + RECEIPTS_WRITTEN_AFTER
    assert len(raw) == total, (
        "migration moves receipts to specs/<id>/receipts; globbing only the "
        "root home saw %d of %d and truncated findings, verification commands "
        "and agent metrics without saying so. Got %d"
        % (RECEIPTS_WRITTEN_AFTER, total, len(raw))
    )

    summary = build_summary.assemble(migrated_project)
    assert len(summary["receipts_normalized"]) == total
    in_spec = [r for r in summary["receipts_normalized"] if r["_spec_id"] == "platform"]
    assert len(in_spec) == RECEIPTS_MOVED_BY_MIGRATION
    assert len(summary["verification"]["commands"]) == total


def test_receipt_homes_cover_flat_and_per_spec(migrated_project: Path, summary_modules):
    _, summary_pipeline = summary_modules
    homes = [str(d) for d in summary_pipeline.receipt_dirs(migrated_project)]
    assert any(h.endswith("/.orchestrator/receipts") for h in homes), homes
    assert any(h.endswith("/specs/platform/receipts") for h in homes), homes


# ── Defect 3: the config ─────────────────────────────────────────────────────


def test_the_header_names_the_project_not_the_last_spec(
    migrated_project: Path, summary_modules
):
    build_summary, _ = summary_modules
    project = build_summary.assemble(migrated_project)["project"]
    assert project["name"] == PROJECT_NAME, (
        "`config['name']` used to be whichever `name:` appeared last in the "
        "file, which with a `specs:` block is always the last spec — so the "
        "header appeared to rename the project whenever a spec was appended. "
        "Got %r" % (project["name"],)
    )
    assert project["name"] != LAST_SPEC
    assert project["stack"] == "python"


def test_load_config_keeps_nesting(migrated_project: Path, summary_modules):
    """Dotted paths, sequence indices, and no top-level key invented by nesting."""
    _, summary_pipeline = summary_modules
    config = summary_pipeline.load_config(migrated_project)

    assert config["project.name"] == PROJECT_NAME
    assert config["project.stack"] == "python"
    assert config["build_mode"] == "scrum"
    assert config["tracker.backend"] == "local"
    assert config["specs[0].id"] == "platform"
    assert config["specs[2].id"] == LAST_SPEC
    assert "name" not in config, (
        "a nested key must not appear as a top-level one; that collision IS "
        "the defect. Got %r" % (config.get("name"),)
    )


def test_load_config_does_not_import_yaml(repo_root: Path):
    """No runtime YAML dependency, deliberately.

    The suggested fix for defect 3 was "parse with a real YAML loader". Nothing
    in `core/`, `plugin-claude/hooks` or `plugin-codex` imports `yaml`; the
    runtime is zero-dependency and the CI runner has no PyYAML installed, so
    adding one would fail the build rather than fix the header.
    """
    text = (repo_root / "core" / "scripts" / "summary" / "pipeline.py").read_text(
        encoding="utf-8")
    assert "import yaml" not in text
    assert "yaml.safe_load" not in text


def test_an_inline_comment_is_not_part_of_the_value(tmp_path: Path, summary_modules):
    """The canonical template annotates almost every key with a trailing comment."""
    _, summary_pipeline = summary_modules
    project = tmp_path / "commented"
    project.mkdir()
    (project / ".synaptory.yaml").write_text(
        'build_mode: "scrum"            # scrum | kanban | spq\n'
        'engagement: autonomous         # structured | interactive\n'
        'project:\n'
        '  name: ""                     # auto-detected from git if empty\n',
        encoding="utf-8")

    config = summary_pipeline.load_config(project)
    assert config["build_mode"] == "scrum"
    assert config["engagement"] == "autonomous"
    assert "project.name" not in config, (
        "an empty scalar carries no value to record"
    )


def test_an_empty_project_name_falls_back_to_the_directory(
    tmp_path: Path, summary_modules
):
    _, summary_pipeline = summary_modules
    project = tmp_path / "fallback-project"
    (project / ".synaptory").mkdir(parents=True)
    (project / ".synaptory.yaml").write_text(
        'project:\n  name: ""\nbuild_mode: scrum\n', encoding="utf-8")

    built = summary_pipeline.build_project(
        project, {}, summary_pipeline.load_config(project))
    assert built["name"] == "fallback-project"


# ── The import preamble, on a composed host layout ───────────────────────────


def test_the_accessor_resolves_from_a_composed_package_layout(
    tmp_path: Path, repo_root: Path
):
    """`core/scripts` sits at a different depth once composed, and must still
    find the shared lib.

    Source tree      `core/scripts/x.py`                 -> `core/lib`
    Composed Cursor  `<pkg>/skills/_shared/scripts/x.py` -> `<pkg>/hooks/lib`
    Composed Codex   `<pkg>/runtime/scripts/x.py`        -> `<pkg>/hooks/lib`

    If the probe misses there, `load_board` returns None on every composed host
    and the dashboard silently goes back to reporting zeros — the failure this
    ticket is about, reintroduced one layout over. Symlinked rather than copied
    so the test stays cheap and cannot drift from the real trees.
    """
    for rel in ("skills/_shared/scripts", "runtime/scripts"):
        pkg = tmp_path / rel.replace("/", "_")
        (pkg / rel).parent.mkdir(parents=True)
        (pkg / rel).symlink_to(repo_root / "core" / "scripts", target_is_directory=True)
        (pkg / "hooks").mkdir()
        (pkg / "hooks" / "lib").symlink_to(
            repo_root / "core" / "lib", target_is_directory=True)

        sys.path.insert(0, str(repo_root / "core" / "scripts"))
        from _runtime_paths import find_lib_dir

        resolved = Path(find_lib_dir(pkg / rel)).resolve()
        assert resolved == (pkg / "hooks" / "lib").resolve(), (
            "%s resolved to %s, not the package's own hooks/lib" % (rel, resolved)
        )


# ── The flat layout must not regress ─────────────────────────────────────────


def test_a_flat_v2_project_still_reads_exactly_as_before(
    tmp_path: Path, summary_modules
):
    """The layout that always worked keeps working — the regression that would
    otherwise ship unnoticed, since every fixture above is v3."""
    build_summary, _ = summary_modules
    project = tmp_path / "flat-project"
    _write_v2_project(project)

    summary = build_summary.assemble(project)
    assert summary["board"]["layout"] == "flat"
    assert summary["pipeline"]["lifecycle_state"] == "SPRINT_EXECUTION"
    assert summary["dod_summary"]["stories_total"] == STORIES
    assert summary["sprint"]["current_sprint"] == 9
    assert len(summary["receipts_normalized"]) == RECEIPTS_MOVED_BY_MIGRATION
    assert summary["project"]["name"] == PROJECT_NAME
    stage_names = [s["name"] for s in summary["pipeline"]["stages"]]
    assert "SPRINT_EXECUTION" in stage_names
