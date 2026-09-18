"""Layer 1 — the sprint report / technical summary read receipts across every home (#733).

Sibling of #730 (PR #732, `31a9886d`), which found the identical defect class
in a DIFFERENT consumer: `core/scripts/summary/pipeline.py::load_receipts_raw`,
used by `/synaptory status`. #730 fixed that reader by routing it through the
board accessor and by teaching `load_receipts_raw` to glob every receipt home.
It deliberately left `core/scripts/summary/versioned_data.py` untouched — a
different consumer (the sprint report / technical summary generator used by
the Technical Writer agent) producing a different, client-facing and
sometimes-immutable output.

`versioned_data.py` had the same flat-path defect in three spots:

  1. `build_technical_summary` looked up a single SA receipt at a literal
     `.orchestrator/receipts/T2-solution-architect.json` path.
  2. `build_sprint_report_data` built `receipts_dir = .../.orchestrator/receipts`
     and globbed only that directory for every other receipt.

`migrate_to_multispec.py` moves `receipts/*` into
`.orchestrator/specs/<primary-spec>/receipts/`, so both reads went blind to
every receipt written before migration once a project migrated, while new
receipts kept landing in both homes at once.

**The fixture is built by running the real migration script**, not by
hand-writing a v3 layout — the #509 lesson recorded in `test_board_reader_census.py`
and in #730's commit message: a hand-written fixture cannot exercise a defect
in receipt *discovery*, only one in receipt *interpretation* the reader already
expects.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

#: Named constants so a drift in the fixture cannot quietly turn into a drift
#: in the assertions (the same #730 discipline: a non-zero count is not
#: evidence of a correct one).
PRE_MIGRATION_RECEIPTS = 12
POST_MIGRATION_RECEIPTS = 5
PRIMARY_SPEC = "platform"
NEW_SPEC = "ehr-integration"


def _paths_for(repo_root: Path):
    return [str(repo_root / "core" / "lib"), str(repo_root / "core" / "scripts")]


@pytest.fixture
def versioned_data_module(repo_root: Path, monkeypatch):
    for entry in _paths_for(repo_root):
        if entry not in sys.path:
            monkeypatch.syspath_prepend(entry)
    import summary.versioned_data as versioned_data
    import summary.pipeline as summary_pipeline

    return versioned_data, summary_pipeline


def _write_v2_project(project: Path) -> None:
    """A flat v2 scrum project with a solution-architect receipt plus story receipts."""
    orch = project / ".synaptory" / ".orchestrator"
    receipts = orch / "receipts"
    receipts.mkdir(parents=True)
    now = "2026-09-01T00:00:00+00:00"

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
        "current_sprint": 3,
        "sprint_goal": "ship the search feature",
        "sprints_completed": [],
        "current_stories": [],
    }), encoding="utf-8")

    # The single-file lookup case: `build_technical_summary` reads this by a
    # literal filename, not a glob.
    (receipts / "T2-solution-architect.json").write_text(json.dumps({
        "task": "design the platform architecture", "agent": "solution-architect",
        "backend": "claude", "model": "opus",
        "metrics": {"components_designed": 7, "adrs_written": 3},
    }), encoding="utf-8")

    for i in range(PRE_MIGRATION_RECEIPTS):
        story_id = "US-%03d" % (i + 1)
        role = ("se", "qe", "cr")[i % 3]
        (receipts / ("%s-%s.json" % (story_id, role))).write_text(json.dumps({
            "task": "task %d" % i, "agent": "software-engineer",
            "backend": "claude", "model": "sonnet",
            "story_id": story_id, "role": role,
            "artifacts": ["src/mod_%d.py" % i],
            "verification_commands": ["pytest tests/test_%d.py" % i],
            "metrics": {
                "tests_written": 4, "tests_passing": 4, "tests_failing": 0,
                # findings_fixed is summed over every receipt in
                # `build_sprint_report_data`. Every pre-migration receipt
                # contributes exactly 1, so the total is a precise probe: it
                # can only equal PRE_MIGRATION_RECEIPTS if the reader saw
                # receipts moved under specs/<primary>/receipts, not just the
                # flat root migration leaves behind.
                "findings_fixed": 1,
            },
        }), encoding="utf-8")

    (project / ".synaptory.yaml").write_text(
        'project:\n'
        '  name: "Receipts Path Fixture"\n'
        '  stack: "python"\n'
        'build_mode: scrum\n'
        'engagement: autonomous\n'
        'tracker:\n'
        '  backend: local\n',
        encoding="utf-8")


def _write_post_migration_receipts(project: Path) -> None:
    """Receipts landing at the flat root home after migration, beside the
    pre-migration ones the migration moved under `specs/<primary>/receipts`.

    Both homes hold real receipts at once, matching #730's measured shape.
    Deliberately role="se" and no `findings_fixed`, so a reader that only
    globs this flat root (the pre-fix behavior) reports zero fixed findings
    and finds no QE receipt at all -- the two assertions in
    `test_sprint_report_finds_receipts_moved_by_migration` would fail against
    the old code for that reason, not by coincidence.
    """
    root = project / ".synaptory" / ".orchestrator" / "receipts"
    root.mkdir(parents=True, exist_ok=True)
    for i in range(POST_MIGRATION_RECEIPTS):
        story_id = "US-%03d" % (i + 1)
        (root / ("%s-post-%d.json" % (story_id, i))).write_text(json.dumps({
            "task": "post-migration %d" % i, "agent": "software-engineer",
            "backend": "claude", "model": "sonnet",
            "story_id": story_id, "role": "se",
            "artifacts": ["tests/test_post_%d.py" % i],
            "verification_commands": ["pytest -q"],
            "metrics": {"tests_written": 2, "tests_passing": 2, "tests_failing": 0},
        }), encoding="utf-8")


@pytest.fixture
def migrated_project(tmp_path: Path, repo_root: Path) -> Path:
    """A project put on the v3 Multi-Spec envelope by the bundled migration script."""
    project = tmp_path / "versioned-data-project"
    _write_v2_project(project)

    script = repo_root / "core" / "scripts" / "migrate_to_multispec.py"
    proc = subprocess.run(
        [sys.executable, str(script),
         "--project-dir", str(project),
         "--primary-spec", PRIMARY_SPEC,
         "--new-specs", NEW_SPEC,
         "--yes"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, (
        "the migration this test is about did not run:\n%s\n%s"
        % (proc.stdout, proc.stderr)
    )
    _write_post_migration_receipts(project)
    return project


def test_the_fixture_really_moved_receipts_under_the_primary_spec(migrated_project: Path):
    """Assert the shape before trusting anything read off it (#509 discipline)."""
    orch = migrated_project / ".synaptory" / ".orchestrator"
    moved = list((orch / "specs" / PRIMARY_SPEC / "receipts").glob("*.json"))
    root = list((orch / "receipts").glob("*.json"))
    # +1 for the T2-solution-architect.json receipt moved alongside the story ones.
    assert len(moved) == PRE_MIGRATION_RECEIPTS + 1, moved
    assert len(root) == POST_MIGRATION_RECEIPTS
    assert (orch / "specs" / PRIMARY_SPEC / "receipts" / "T2-solution-architect.json").exists()
    assert not (orch / "receipts" / "T2-solution-architect.json").exists()


def test_technical_summary_finds_the_sa_receipt_after_migration(
    migrated_project: Path, versioned_data_module
):
    versioned_data, _ = versioned_data_module
    summary = versioned_data.build_technical_summary(migrated_project)

    assert summary["sa_metrics"] == {"components_designed": 7, "adrs_written": 3}, (
        "a literal '.orchestrator/receipts/T2-solution-architect.json' lookup "
        "goes blind once migration moves the file under "
        "specs/%s/receipts/ -- got %r" % (PRIMARY_SPEC, summary["sa_metrics"])
    )


def test_sprint_report_finds_receipts_moved_by_migration(
    migrated_project: Path, versioned_data_module
):
    """`build_sprint_report_data` must see receipts under `specs/<primary>/receipts`.

    The fixture is built so both assertions fail against the pre-fix code for
    the RIGHT reason, not by coincidence:

      * `remediation.fixed` sums `metrics.findings_fixed` over every receipt.
        Only the PRE_MIGRATION receipts (moved to `specs/platform/receipts`)
        declare it; the flat-root post-migration receipts do not. A reader
        that only globs the flat root sees 0; the fix must see
        PRE_MIGRATION_RECEIPTS.
      * `tests.total` comes from the single QE receipt `_find_receipt("qe")`
        selects. The only `role: "qe"` receipt in the whole fixture was moved
        to `specs/platform/receipts` by migration; the flat-root receipts are
        all `role: "se"`. A reader that only globs the flat root finds no QE
        receipt at all (`tests.total == 0`); the fix must find the moved one
        (`tests_written == 4`).
    """
    versioned_data, _ = versioned_data_module
    data = versioned_data.build_sprint_report_data(migrated_project, sprint_num=3)

    assert data["remediation"]["fixed"] == PRE_MIGRATION_RECEIPTS, (
        "findings_fixed is only declared on receipts migration moved under "
        "specs/%s/receipts -- a flat-root-only glob would report 0. Got %r"
        % (PRIMARY_SPEC, data["remediation"])
    )
    assert data["tests"]["total"] == 4, (
        "the only QE receipt in the fixture lives under specs/%s/receipts "
        "after migration -- a flat-root-only glob finds no QE receipt at all "
        "and reports 0. Got %r" % (PRIMARY_SPEC, data["tests"])
    )
