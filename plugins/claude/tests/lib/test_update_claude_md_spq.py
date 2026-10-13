"""Layer 1 -- `update_claude_md.py` writes no sentinel under `build_mode: spq` (#845).

The sentinel (`<!-- synaptory-state ... -->`) carries `lifecycle_state`,
`current_sprint` and `last_agent`: a Scrum/Kanban board. Under SPQ
`pipeline-state.json` is only a mode + identity pointer, so the writers emitted
`lifecycle_state: ` / `current_sprint: 0` and a `last_agent` taken from stale
Scrum receipts, and rewrote the file on every PreCompact and SessionEnd.

What is pinned here, at the module level (the hook layer is in
tests/hooks/test_multi_spec_hooks.py):

  * both writers (`--rollup-sentinel`, `--sentinel-write`) write nothing on an
    SPQ project, and create no `CLAUDE.md`;
  * a Scrum-shaped block an earlier hook left in an SPQ project is stripped once
    and a second run touches nothing;
  * Scrum, Kanban and Multi-Spec behavior is unchanged.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import _spq_fixture
import spec_state
import update_claude_md as ucm

pytestmark = pytest.mark.unit

LIB = Path(ucm.__file__).resolve().parent

USER_DOC = "# proj\n\nUser-written notes.\n"
FENCED_DOC = USER_DOC + "\n" + ucm.FENCE_START + "\nsection body\n" + ucm.FENCE_END + "\n"


def _spq_project(tmp_path: Path, *, cycle: bool = False) -> Path:
    """An SPQ project as the state machine produces it, never hand-written."""
    import spq_state_machine as sm

    project = tmp_path / "spq"
    if cycle:
        _spq_fixture.open_project(project)
    else:
        project.mkdir()
        (project / ".synaptory.yaml").write_text('build_mode: "spq"\n', encoding="utf-8")
        sm.initialize(str(project))
    return project


def _scrum_project(tmp_path: Path) -> Path:
    project = tmp_path / "scrum"
    project.mkdir()
    spec_state.write_full_state(str(project), {
        "version": "2.0", "build_mode": "scrum",
        "lifecycle_state": "SPRINT_EXECUTION", "current_sprint": 5,
        "current_stories": [],
    })
    return project


def _become_spq(project: Path) -> None:
    """Cut a Scrum project over to SPQ the way the reporter did: move the old
    state aside and initialise."""
    import spq_state_machine as sm

    (project / ".synaptory" / ".orchestrator" / "pipeline-state.json").unlink()
    sm.initialize(str(project))


# ── The writers write nothing under SPQ ──────────────────────────────────────


@pytest.mark.parametrize("cycle", [False, True], ids=["discovery", "cycle-open"])
def test_rollup_writes_nothing_on_an_spq_project(tmp_path: Path, cycle: bool):
    project = _spq_project(tmp_path, cycle=cycle)
    doc = project / "CLAUDE.md"
    doc.write_text(FENCED_DOC, encoding="utf-8")
    # A stale Scrum receipt: the old `last_agent` source.
    stale = project / ".synaptory" / ".orchestrator" / "receipts"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "INCEPTION-sa.json").write_text("{}", encoding="utf-8")

    assert ucm.write_sentinel_rollup(str(project)) == {}
    assert doc.read_text(encoding="utf-8") == FENCED_DOC


def test_rollup_creates_no_claude_md_on_an_spq_project(tmp_path: Path):
    project = _spq_project(tmp_path)
    assert ucm.write_sentinel_rollup(str(project)) == {}
    assert not (project / "CLAUDE.md").exists()


def test_sentinel_write_is_a_noop_on_an_spq_project(tmp_path: Path):
    """The settings overlay appends a block even where none exists, so guarding
    only the rollup would not be enough."""
    project = _spq_project(tmp_path)
    doc = project / "CLAUDE.md"

    ucm.write_sentinel(str(project), {"engagement": "structured"})
    assert not doc.exists()

    doc.write_text(FENCED_DOC, encoding="utf-8")
    ucm.write_sentinel(str(project), {"engagement": "structured"})
    assert doc.read_text(encoding="utf-8") == FENCED_DOC


def _cli(project: Path, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(LIB / "update_claude_md.py"), str(project), *args],
        input=stdin, capture_output=True, text=True, check=False,
    )


def test_both_cli_entry_points_write_nothing_on_an_spq_project(tmp_path: Path):
    project = _spq_project(tmp_path)
    doc = project / "CLAUDE.md"
    doc.write_text(USER_DOC, encoding="utf-8")

    rolled = _cli(project, "--rollup-sentinel")
    assert rolled.returncode == 0, rolled.stderr
    assert json.loads(rolled.stdout) == {}

    overlaid = _cli(project, "--sentinel-write", stdin='{"engagement": "structured"}')
    assert overlaid.returncode == 0, overlaid.stderr

    assert doc.read_text(encoding="utf-8") == USER_DOC

    doc.unlink()
    assert _cli(project, "--rollup-sentinel").returncode == 0
    assert _cli(project, "--sentinel-write", stdin='{"engagement": "x"}').returncode == 0
    assert not doc.exists()


def test_the_generic_section_updater_still_works_on_an_spq_project(tmp_path: Path):
    """The fenced section is how the SPQ CLAUDE.md is generated; only the
    sentinel writers are guarded."""
    project = _spq_project(tmp_path)
    result = _cli(project, stdin="# Synaptory Pipeline\n- Build mode: spq")
    assert result.returncode == 0, result.stderr
    text = (project / "CLAUDE.md").read_text(encoding="utf-8")
    assert ucm.FENCE_START in text and "Build mode: spq" in text
    assert "synaptory-state" not in text


# ── A block an earlier hook left behind ──────────────────────────────────────


@pytest.mark.parametrize(
    "original",
    [USER_DOC, FENCED_DOC],
    ids=["no-fence-appended", "after-fence"],
)
def test_a_stale_scrum_block_is_stripped_once_and_exactly(
    tmp_path: Path, original: str, monkeypatch
):
    """Built with the writer's own old layout (Scrum rollup), then cut over to
    SPQ. After one run the file is byte-for-byte what it was before the hook
    first touched it; a second run does not write at all."""
    project = _scrum_project(tmp_path)
    doc = project / "CLAUDE.md"
    doc.write_text(original, encoding="utf-8")
    ucm.write_sentinel_rollup(str(project))
    assert "lifecycle_state: SPRINT_EXECUTION" in doc.read_text(encoding="utf-8")

    _become_spq(project)
    assert ucm.write_sentinel_rollup(str(project)) == {}
    assert doc.read_text(encoding="utf-8") == original

    writes = []
    monkeypatch.setattr(ucm, "_atomic_write", lambda *a, **k: writes.append(a))
    assert ucm.write_sentinel_rollup(str(project)) == {}
    assert ucm.strip_stale_scrum_sentinel(str(project)) is False
    assert writes == []
    assert doc.read_text(encoding="utf-8") == original


def test_the_reporters_exact_block_is_stripped(tmp_path: Path):
    project = _spq_project(tmp_path)
    doc = project / "CLAUDE.md"
    doc.write_text(
        USER_DOC + "\n<!-- synaptory-state\n"
        "last_updated: 2026-10-05T08:38:56Z\n"
        "lifecycle_state:\n"
        "current_sprint: 0\n"
        "last_agent: INCEPTION-sa\n"
        "-->\n",
        encoding="utf-8",
    )
    ucm.write_sentinel_rollup(str(project))
    assert doc.read_text(encoding="utf-8") == USER_DOC


def test_a_file_holding_only_the_stale_block_is_removed(tmp_path: Path):
    """The hook created it, nobody else wrote a byte of it."""
    project = _scrum_project(tmp_path)
    ucm.write_sentinel_rollup(str(project))
    assert (project / "CLAUDE.md").exists()

    _become_spq(project)
    ucm.write_sentinel_rollup(str(project))
    assert not (project / "CLAUDE.md").exists()


def test_an_spq_shaped_block_is_left_alone(tmp_path: Path):
    """Only the Scrum keys mark a block as stale. A `stage:` block, or the
    `phase:` block `inception.md` embeds inside the fence, is not ours to
    remove."""
    project = _spq_project(tmp_path)
    doc = project / "CLAUDE.md"
    text = (
        USER_DOC + "\n<!-- synaptory-state\nstage: DISCOVERY\n-->\n"
        + ucm.FENCE_START + "\nbody\n<!-- synaptory-state\n"
        "phase: INCEPTION\nsprint: 0/0\nengagement: guided\n-->\n"
        + ucm.FENCE_END + "\n"
    )
    doc.write_text(text, encoding="utf-8")
    ucm.write_sentinel_rollup(str(project))
    assert doc.read_text(encoding="utf-8") == text


# ── Scrum, Kanban and Multi-Spec are unchanged ───────────────────────────────


def test_scrum_still_writes_the_rollup_and_the_overlay(tmp_path: Path):
    project = _scrum_project(tmp_path)
    doc = project / "CLAUDE.md"
    doc.write_text(USER_DOC, encoding="utf-8")

    rollup = ucm.write_sentinel_rollup(str(project))
    assert rollup["lifecycle_state"] == "SPRINT_EXECUTION"
    ucm.write_sentinel(str(project), {"engagement": "structured"})

    parsed = ucm.read_sentinel(str(project))
    assert parsed["lifecycle_state"] == "SPRINT_EXECUTION"
    assert parsed["current_sprint"] == "5"
    assert parsed["engagement"] == "structured"


def test_kanban_still_writes_the_rollup(tmp_path: Path):
    project = tmp_path / "kanban"
    project.mkdir()
    spec_state.write_full_state(str(project), {
        "version": "2.0", "build_mode": "kanban",
        "lifecycle_state": "EXECUTION", "current_sprint": 0, "current_stories": [],
    })
    (project / "CLAUDE.md").write_text(USER_DOC, encoding="utf-8")
    assert ucm.write_sentinel_rollup(str(project))["lifecycle_state"] == "EXECUTION"


def test_a_project_with_no_state_file_keeps_its_old_behavior(tmp_path: Path):
    """No pipeline-state.json is not SPQ: the generic writer still creates the
    file, exactly as before."""
    ucm.write_sentinel(str(tmp_path), {"engagement": "structured"})
    assert ucm.read_sentinel(str(tmp_path)) == {"engagement": "structured"}
    assert ucm.is_spq_project(str(tmp_path)) is False
