"""Layer 1 -- a Cycle can be opened and continued in any checkout (#766).

SPQ state is split across three tiers and the split is principled. Two items
sat in the wrong one.

**Engagement facts.** `baseline_approved` and the baseline reference lived in
the gitignored pointer, so a fresh `git worktree` of the same commit read
`lifecycle_state: DISCOVERY` and `baseline_approved: False` -- a project that
never ran Discovery. `open_cycle` refused there, so a Cycle could be neither
started nor continued anywhere but the one directory that happened to run
`approve_baseline`. They are a decision, not working state, and a decision
belongs in a diff.

**Receipts.** The barrier credits a declared criterion from the DoD gate, and
the DoD gate reads receipts. Gitignored, they were evidence a reviewer could
not see: the pull request carried the code and the verdict and not the proof
either rested on.

A third finding is fixed with them: `_engagement_view` promised that "no Cycle
holds a copy that can drift", while `_write_state` wrote the merged dict
straight back onto every board. The merge at read time hid it.

What is NOT changed: tier 1 stays in git rather than moving to the control
plane, so the seal and the code it governs keep travelling in one commit; and
the implicit "current cycle" is left alone here, being a separate decision.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import spq_paths
import spq_state_machine as sm
import story_pipeline as story

from _spq_fixture import CYCLE_KWARGS, unit as _fx_unit

GITIGNORE = ".synaptory/*\n!.synaptory/cycles/\n!.synaptory/engagement.json\n"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
    ).stdout


@pytest.fixture
def project(tmp_path: Path, monkeypatch):
    """A git project whose gitignore follows the documented recipe."""
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@e.co")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    (root / "f.txt").write_text("x", encoding="utf-8")
    (root / ".synaptory.yaml").write_text("build_mode: spq\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    monkeypatch.delenv("SYNAPTORY_ACTIVE_SPEC", raising=False)
    sm.initialize(str(root))
    return root


def _approve(root: Path) -> None:
    sm.approve_baseline(
        str(root), approved_by="lead@h3t.co", baseline_ref="baseline-1",
        calibration={"sample_units": 2, "measured_hours": 8},
    )


@pytest.mark.unit
def test_the_baseline_approval_is_committed_not_gitignored(project: Path):
    _approve(project)

    path = Path(spq_paths.engagement_path(str(project)))
    assert path.is_file(), "the approval must land in the committed tree"
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", spq_paths.ENGAGEMENT_RELPATH],
        cwd=str(project), capture_output=True,
    )
    assert ignored.returncode != 0, "a decision nobody else can read is not a record"
    assert json.loads(path.read_text())["engagement"]["discovery"][
        "baseline_approved"] is True


@pytest.mark.unit
def test_a_fresh_worktree_sees_the_approved_baseline(project: Path, tmp_path: Path):
    """The blocking case: the same commit, a different directory."""
    _approve(project)
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "approve the baseline")

    elsewhere = tmp_path / "wt"
    _git(project, "worktree", "add", "-q", str(elsewhere), "HEAD")

    state = sm.read_state(str(elsewhere))
    assert state["discovery"]["baseline_approved"] is True
    assert state["discovery"]["baseline_ref"] == "baseline-1"


@pytest.mark.unit
def test_a_fresh_worktree_can_open_a_cycle(project: Path, tmp_path: Path):
    """`open_cycle` refused in every checkout but one, for want of a baseline
    it could not see."""
    _approve(project)
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "approve the baseline")
    elsewhere = tmp_path / "wt2"
    _git(project, "worktree", "add", "-q", str(elsewhere), "HEAD")

    opened = sm.open_cycle(
        str(elsewhere), goal="a Cycle opened somewhere else",
        admitted_units=[_fx_unit("WU-01")], **CYCLE_KWARGS,
    )

    assert opened["ok"] is True
    assert sm.read_state(str(elsewhere))["lifecycle_state"] == "CYCLE"


@pytest.mark.unit
def test_approving_into_a_gitignored_tree_is_refused_and_names_the_lines(
    project: Path,
):
    (project / ".gitignore").write_text(".synaptory/\n", encoding="utf-8")

    with pytest.raises(sm.HydrationRefusal) as excinfo:
        _approve(project)

    message = str(excinfo.value)
    assert "!.synaptory/cycles/" in message
    assert "refuse to open a Cycle" in message


@pytest.mark.unit
def test_no_board_holds_a_copy_of_the_engagement(project: Path):
    """The guarantee `_engagement_view` stated and the mechanism did not keep:
    the merge happens at read time, and `_write_state` wrote it straight back."""
    _approve(project)
    sm.open_cycle(
        str(project), goal="cycle one", admitted_units=[_fx_unit("WU-01")],
        **CYCLE_KWARGS,
    )
    cycle_id = sm.identity(str(project)).cycle_id

    on_disk = json.loads(
        Path(spq_paths.execution_state_path(str(project), cycle_id)).read_text()
    )
    assert "discovery" not in on_disk, "a board must hold no engagement copy"
    # The read path still answers, because the merge is what serves it.
    assert sm.read_state(str(project))["discovery"]["baseline_approved"] is True


@pytest.mark.unit
def test_receipts_resolve_into_the_committed_tree(project: Path):
    _approve(project)
    sm.open_cycle(
        str(project), goal="cycle one", admitted_units=[_fx_unit("WU-01")],
        **CYCLE_KWARGS,
    )
    cycle_id = sm.identity(str(project)).cycle_id

    intended = story.receipts_dir_for(str(project), intended=True)

    assert intended == spq_paths.receipts_dir(str(project), cycle_id)
    assert ".synaptory/cycles/" in intended.replace("\\", "/")
    assert ".orchestrator" not in intended


@pytest.mark.unit
def test_receipts_written_under_the_old_layout_are_still_found(project: Path):
    """A receipt is immutable evidence a recorded verdict may cite by path, so
    the read falls back rather than a migration moving it."""
    _approve(project)
    sm.open_cycle(
        str(project), goal="cycle one", admitted_units=[_fx_unit("WU-01")],
        **CYCLE_KWARGS,
    )
    cycle_id = sm.identity(str(project)).cycle_id
    legacy = Path(spq_paths.legacy_receipts_dir(str(project), cycle_id))
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "WU-01-se.json").write_text("{}", encoding="utf-8")

    resolved = story.receipts_dir_for(str(project), intended=False)

    assert Path(resolved) == legacy, "an existing Cycle keeps finding its receipts"
    assert story.receipts_dir_for(str(project), intended=True) != str(legacy), (
        "nothing new is written to the old home"
    )


@pytest.mark.unit
def test_the_shell_sweep_finds_receipts_in_both_homes(project: Path):
    """Missing this is #326/#336 again -- receipts shipped from no host and
    every SPQ session reporting `0 receipts`, with the new layout in the blind
    spot the old one used to occupy."""
    _approve(project)
    sm.open_cycle(
        str(project), goal="cycle one", admitted_units=[_fx_unit("WU-01")],
        **CYCLE_KWARGS,
    )
    cycle_id = sm.identity(str(project)).cycle_id
    for directory in (
        Path(spq_paths.legacy_receipts_dir(str(project), cycle_id)),
        Path(spq_paths.receipts_dir(str(project), cycle_id)),
    ):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "WU-01-se.json").write_text("{}", encoding="utf-8")

    sweep = Path(story.__file__).parent / "receipt-paths.sh"
    orch = Path(project) / ".synaptory" / ".orchestrator"
    out = subprocess.run(
        ["bash", "-c",
         'source "$1"; synaptory_receipt_count "$2"', "_", str(sweep), str(orch)],
        capture_output=True, text=True,
    )

    assert out.stdout.strip() == "2", out.stderr


@pytest.mark.unit
def test_the_companion_search_still_reaches_every_other_home(project: Path):
    """`companion_receipt_dirs` is THE place that knows all the layouts, and it
    found them by climbing until it saw `.orchestrator`.

    A committed receipts directory has no such ancestor, so the climb ran off
    the top and the function returned the one directory it was handed. It did
    not raise and it did not report a problem -- `collect_story_receipts` and
    `_fresh_receipt` simply stopped seeing receipts filed anywhere else, and
    the gate behind them asked for a stage that had already run. The failure
    surfaced three levels away from the cause, as `next_action` proposing
    `dispatch_cr` against a Cycle whose CR receipt was on disk.
    """
    _approve(project)
    sm.open_cycle(
        str(project), goal="cycle one", admitted_units=[_fx_unit("WU-01")],
        **CYCLE_KWARGS,
    )
    cycle_id = sm.identity(str(project)).cycle_id
    flat = Path(project) / ".synaptory" / ".orchestrator" / "receipts"
    flat.mkdir(parents=True, exist_ok=True)
    (flat / "WU-01-cr.json").write_text('{"agent": "code-reviewer"}', encoding="utf-8")

    committed = spq_paths.receipts_dir(str(project), cycle_id)
    companions = story.companion_receipt_dirs(committed)

    assert str(flat) in companions, companions
    assert [r for r in story.collect_story_receipts(committed, "WU-01")], (
        "a receipt filed under a home this Cycle does not write to is still "
        "evidence, and the gate reads it through this function"
    )


@pytest.mark.unit
def test_a_committed_receipt_path_is_admitted_by_both_boundaries(project: Path):
    """The kernel issues the path; two guards decide whether it may be named.

    They are separate code in separate packages on purpose -- the MCP server
    re-checks what a disabled hook could skip -- which is exactly why a layout
    move has to reach both. It reached neither at first: the kernel refused
    `path_escape`, and behind it the Codex boundary refused a path the same
    kernel had just written into the dispatch contract.
    """
    import advance_kernel

    _approve(project)
    sm.open_cycle(
        str(project), goal="cycle one", admitted_units=[_fx_unit("WU-01")],
        **CYCLE_KWARGS,
    )
    cycle_id = sm.identity(str(project)).cycle_id
    receipt = Path(spq_paths.receipts_dir(str(project), cycle_id)) / "WU-01-se.json"

    assert advance_kernel._scoped_path(str(project), str(receipt)) == receipt.resolve()
    with pytest.raises(Exception):
        advance_kernel._scoped_path(str(project), str(Path(project) / "outside.json"))
