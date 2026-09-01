"""Layer 1 — SPQ ceremony receipts land where the gate actually looks (#327).

Three readiness gates resolve their receipts directory through
``story_pipeline._resolve_receipts_dir``:

    checkpoint_readiness    spq_state_machine.py   CHECKPOINT-{N}-tw.json
    acceptance_readiness    spq_state_machine.py   ACCEPTANCE-{N}-{abbrev}.json
    _write_barrier_receipt  sync_barrier.py        SYNC-{N}-barrier.json

Inside a Cycle that resolves to the SPQ workstream path, NOT
``.orchestrator/receipts/``. The three ceremony files used to spell the flat
path in prose, so an agent that followed the doc wrote where no gate looked and
``close_cycle`` refused with ``missing CHECKPOINT-1-tw.json``. Observed on the
2026-09-01 tri-host Cycle (#323 G13).

Unlike per-story dispatches, these ceremony agents are spawned straight from the
ceremony file and never pass through ``advance_kernel``, so nothing supplies
them a ``receipt_path`` at runtime. The ceremony prose IS the contract, which is
why it gets a drift guard rather than a comment.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import spq_paths


pytestmark = pytest.mark.unit

HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parent.parent
REPO_ROOT = PLUGIN_ROOT.parent
SPQ_SKILLS = PLUGIN_ROOT / "skills" / "synaptory" / "spq"
STATE_MACHINE = PLUGIN_ROOT / "hooks" / "lib" / "spq_state_machine.py"

# The ceremonies whose receipts a gate resolves. discovery.md and commit.md are
# deliberately absent: they run before a Cycle is open, `resolve_identity`
# returns no cycle_id, and the resolver correctly yields the flat dir there. A
# hardcoded flat path in those files is right, so guarding them would be wrong.
GATED_CEREMONIES = ("checkpoint.md", "sync.md", "acceptance.md")

# "The TW writes its receipt to `<path>` as its last action."
_WRITES_RECEIPT_TO = re.compile(
    r"writes its receipt to `([^`]+)`", re.IGNORECASE
)


def _seed_spq_cycle(project_dir: Path, workstream: str = "api") -> Path:
    """A clone mid-Cycle: pinned workstream, one open Cycle in the index."""
    orch = project_dir / ".synaptory" / ".orchestrator"
    cycle_id = "1-abcdef12"
    (orch / "spq").mkdir(parents=True, exist_ok=True)
    (orch / "pipeline-state.json").write_text(
        json.dumps({"version": "3.0", "build_mode": "spq",
                    "lifecycle_state": "CYCLE_EXECUTION", "current_cycle": 1}),
        encoding="utf-8",
    )
    spq_paths.write_pin(str(project_dir), workstream)
    (orch / "spq" / "index.json").write_text(
        json.dumps({"cycle_seq_high": 1, "current_cycle_id": cycle_id,
                    "cycles": [{"cycle_id": cycle_id}]}),
        encoding="utf-8",
    )
    return Path(spq_paths.receipts_dir(
        str(project_dir), cycle_id=cycle_id, workstream_id=workstream))


def _run_verb(project_dir: Path) -> str:
    result = subprocess.run(
        [sys.executable, str(STATE_MACHINE), "receipts_dir", str(project_dir)],
        capture_output=True, text=True, check=False,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": ":".join([
            str(PLUGIN_ROOT / "hooks" / "lib"),
            str(PLUGIN_ROOT / "skills" / "_shared" / "scripts"),
        ])},
    )
    assert result.returncode == 0, (
        f"`receipts_dir` verb failed: {result.returncode}\n{result.stderr}"
    )
    return result.stdout.strip()


# ─── The verb the ceremonies call ───────────────────────────────────────────


def test_receipts_dir_verb_resolves_the_spq_workstream_path(tmp_path: Path):
    """Inside a Cycle the verb must return the SPQ workstream dir.

    This is the value the ceremony interpolates, so if it ever returns the flat
    dir again every gated ceremony silently regresses to the #327 behavior.
    """
    project_dir = tmp_path / "clone"
    project_dir.mkdir()
    expected = _seed_spq_cycle(project_dir)

    assert _run_verb(project_dir) == str(expected)


def test_receipts_dir_verb_agrees_with_the_gate(tmp_path: Path):
    """The verb and `_resolve_receipts_dir` must not be two derivations.

    The whole defect was two independent spellings of one path. A test that
    only pinned the verb's output shape would let them drift apart again.
    """
    from story_pipeline import _resolve_receipts_dir

    project_dir = tmp_path / "clone"
    project_dir.mkdir()
    expected = _seed_spq_cycle(project_dir)
    # The gate reads with intended=False, so the dir has to exist for the two
    # to be comparable; that is the state a gate ever runs in.
    expected.mkdir(parents=True, exist_ok=True)

    assert _run_verb(project_dir) == _resolve_receipts_dir(str(project_dir))


def test_receipts_dir_verb_falls_back_to_flat_outside_a_cycle(tmp_path: Path):
    """No open Cycle means the flat dir, which is what DISCOVERY/COMMIT want.

    Pinned so the fix cannot over-correct: making every ceremony resolve
    dynamically is only safe because the resolver still answers "flat" before a
    Cycle exists.
    """
    project_dir = tmp_path / "clone"
    (project_dir / ".synaptory" / ".orchestrator").mkdir(parents=True)
    (project_dir / ".synaptory" / ".orchestrator"
     / "pipeline-state.json").write_text(
        json.dumps({"version": "3.0", "build_mode": "spq"}), encoding="utf-8")

    resolved = _run_verb(project_dir)
    assert resolved.endswith(str(Path(".synaptory") / ".orchestrator" / "receipts")), (
        f"expected the flat dir outside a Cycle, got {resolved}"
    )


# ─── Drift guard on the ceremony prose ──────────────────────────────────────


@pytest.mark.parametrize("filename", GATED_CEREMONIES)
def test_gated_ceremony_never_hardcodes_the_flat_receipts_path(filename: str):
    """No gated ceremony may tell an agent to write to `.orchestrator/receipts/`.

    Every `writes its receipt to <path>` instruction must interpolate
    `${RECEIPTS_DIR}`. A literal path here is invisible to the gate that
    validates it.
    """
    text = (SPQ_SKILLS / filename).read_text(encoding="utf-8")
    offenders = [
        path for path in _WRITES_RECEIPT_TO.findall(text)
        if "RECEIPTS_DIR" not in path
    ]
    assert not offenders, (
        f"{filename} hardcodes receipt path(s) the gate never reads: {offenders}"
    )


@pytest.mark.parametrize("filename", GATED_CEREMONIES)
def test_gated_ceremony_resolves_receipts_dir_before_dispatching(filename: str):
    """Every `${RECEIPTS_DIR}` reference must have a resolver that defines it.

    Interpolating an unset variable would put receipts at `/CHECKPOINT-1-tw.json`,
    which fails more loudly than #327 did but is still broken.
    """
    text = (SPQ_SKILLS / filename).read_text(encoding="utf-8")
    if "${RECEIPTS_DIR}" not in text:
        pytest.skip(f"{filename} dispatches no receipt-writing agent")

    # The ceremony prompts use the host-neutral `{{path: ...}}` compose
    # directive (#298), not a `${CLAUDE_PLUGIN_ROOT}` literal. Matching on the
    # verb and the directive rather than one host's expansion keeps this guard
    # working for whichever host the file is composed for.
    resolvers = text.count(
        'RECEIPTS_DIR=$(python3 "{{path: hooks/lib/spq_state_machine.py}}" '
        'receipts_dir "$(pwd)")'
    )
    assert resolvers > 0, (
        f"{filename} interpolates ${{RECEIPTS_DIR}} but never resolves it"
    )
