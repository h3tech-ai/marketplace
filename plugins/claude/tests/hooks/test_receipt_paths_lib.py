"""Layer 2 — the shared receipt sweep in core/lib/receipt-paths.sh (#336).

Six hooks each carried their own copy of this glob and the SPQ layout was
missing from all six: receipts shipped from no host (#326) and every SPQ
session reported `0 receipts` (#336). The sweep is now defined once, so it gets
tested once, directly, rather than only through whichever hook happens to call
it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.hook

HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parent.parent
LIB = PLUGIN_ROOT / "hooks" / "lib" / "receipt-paths.sh"


def _sh(script: str, cwd: Path | None = None) -> str:
    """Source the helper and run `script`, returning stdout."""
    result = subprocess.run(
        ["bash", "-c", f'set -u; . "{LIB}"\n{script}'],
        capture_output=True, text=True, check=False,
        cwd=str(cwd) if cwd else None,
    )
    assert result.returncode == 0, (
        f"script failed ({result.returncode}): {result.stderr}"
    )
    return result.stdout


@pytest.fixture
def orch(tmp_path: Path) -> Path:
    """One receipt in every layout the sweep must see, plus decoys."""
    o = tmp_path / "project" / ".synaptory" / ".orchestrator"
    seeded = {
        "receipts/FLAT-1-se.json": "flat",
        "specs/platform/receipts/US-1-se.json": "multi-spec",
        "spq/cycles/1-abcdef12/receipts/WU-API-1-se.json": "spq",
        "spq/cycles/1-abcdef12/receipts/CHECKPOINT-1-tw.json": "spq",
        # A receipt written under the PRE-ADR-035 depth. Not a layout the
        # product creates any more, and kept on purpose: those receipts are
        # immutable evidence a hook still has to count and ship, and this is
        # the only case that proves the SPQ line is still a `*/receipts/`
        # sweep rather than one pinned to the current depth.
        "spq/cycles/0-aaaaaaaa/lanes/api/receipts/WU-LEGACY-1-se.json": "spq, historical depth",
    }
    for rel in seeded:
        p = o / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"story_id": "X"}), encoding="utf-8")

    # Decoys that must NOT be swept:
    #  - a shipped sentinel (not `.json`)
    #  - a receipt nested one level deeper than the flat dir's maxdepth 1
    #  - a non-json file inside a receipts dir
    (o / "receipts" / "FLAT-1-se.json.shipped").write_text("", encoding="utf-8")
    (o / "receipts" / "archive").mkdir(parents=True, exist_ok=True)
    (o / "receipts" / "archive" / "OLD-1-se.json").write_text("{}", encoding="utf-8")
    (o / "spq" / "cycles" / "1-abcdef12" / "receipts" / "notes.md").write_text(
        "x", encoding="utf-8")
    return o


def test_sweep_sees_every_layout(orch: Path):
    out = _sh(f'synaptory_receipt_files "{orch}"')
    names = sorted(Path(line).name for line in out.split() if line)
    assert names == [
        "CHECKPOINT-1-tw.json",   # spq
        "FLAT-1-se.json",         # flat
        "US-1-se.json",           # multi-spec
        "WU-API-1-se.json",       # spq
        "WU-LEGACY-1-se.json",       # spq, pre-ADR-035 depth
    ], f"sweep returned {names}"


def test_sweep_excludes_sentinels_and_non_json(orch: Path):
    out = _sh(f'synaptory_receipt_files "{orch}"')
    assert ".shipped" not in out, "a .shipped sentinel was swept as a receipt"
    assert "notes.md" not in out, "a non-json file was swept as a receipt"


def test_flat_sweep_stays_maxdepth_one(orch: Path):
    """The flat glob is `-maxdepth 1` and must stay that way.

    Widening it would pull in `receipts/archive/`, and re-shipping archived
    receipts on every session end is a worse bug than the one being fixed.
    """
    out = _sh(f'synaptory_receipt_files "{orch}"')
    assert "OLD-1-se.json" not in out, (
        "the flat glob recursed into receipts/archive/"
    )


def test_count_matches_the_sweep(orch: Path):
    assert _sh(f'synaptory_receipt_count "{orch}"').strip() == "5"


def test_recent_is_newest_first(orch: Path):
    """Ordering is by mtime, and the helper sorts it itself.

    The callers (`reanchor`, `pipeline-snapshot`, `verify-receipt`) all want
    "the N newest", so a sweep in directory order would silently show the wrong
    receipts.
    """
    import os
    import time

    now = time.time()
    order = [
        "spq/cycles/1-abcdef12/receipts/WU-API-1-se.json",
        "receipts/FLAT-1-se.json",
        "specs/platform/receipts/US-1-se.json",
    ]
    # Newest first in `order`: stamp descending mtimes.
    for i, rel in enumerate(order):
        os.utime(orch / rel, (now - i * 100, now - i * 100))
    for i, rel in enumerate([
        "spq/cycles/1-abcdef12/receipts/CHECKPOINT-1-tw.json",
        "spq/cycles/0-aaaaaaaa/lanes/api/receipts/WU-LEGACY-1-se.json",
    ]):
        os.utime(orch / rel, (now - 10_000 - i, now - 10_000 - i))

    out = _sh(f'synaptory_recent_receipts "{orch}" 3')
    got = [Path(line).name for line in out.split() if line]
    assert got == [Path(r).name for r in order], f"got {got}"


def test_recent_respects_the_limit(orch: Path):
    out = _sh(f'synaptory_recent_receipts "{orch}" 2')
    assert len([l for l in out.split() if l]) == 2


def test_recent_is_empty_and_does_not_fall_back_to_cwd(tmp_path: Path):
    """The cross-platform trap this helper exists to avoid.

    `synaptory_receipt_files ... | xargs ls -1t` was the old idiom. With no
    receipts, GNU xargs runs `ls -1t` with no operands, which lists the CURRENT
    DIRECTORY and hands back an unrelated filename; BSD xargs skips the command
    and returns empty. A caller testing `[ -z "$RECENT" ]` to mean "no receipt"
    therefore took the WRONG branch on Linux CI and the right one on a Mac.
    `synaptory-verify-receipt.sh` did exactly that to decide whether to log
    `receipt_missing`.

    Run from a directory full of plausible decoys, so a cwd fallback cannot
    pass by luck.
    """
    empty_orch = tmp_path / "empty" / ".orchestrator"
    empty_orch.mkdir(parents=True)
    decoys = tmp_path / "decoys"
    decoys.mkdir()
    for name in ("US-999-se.json", "receipt.json", "a.json"):
        (decoys / name).write_text("{}", encoding="utf-8")

    out = _sh(f'synaptory_recent_receipts "{empty_orch}" 1', cwd=decoys)
    assert out.strip() == "", (
        f"empty sweep fell back to the cwd and returned {out.strip()!r}"
    )
    assert _sh(f'synaptory_receipt_count "{empty_orch}"', cwd=decoys).strip() == "0"


def test_sweep_tolerates_a_missing_orchestrator_dir(tmp_path: Path):
    """Hooks fire in non-synaptory projects too; the sweep must not error."""
    missing = tmp_path / "nope" / ".orchestrator"
    assert _sh(f'synaptory_receipt_files "{missing}"').strip() == ""
    assert _sh(f'synaptory_receipt_count "{missing}"').strip() == "0"
    assert _sh('synaptory_receipt_files ""').strip() == ""
