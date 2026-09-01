"""Layer 2 -- SubagentStop receipt SELECTION binds to the active dispatch (#340).

`synaptory-verify-receipt.sh` used to pick the receipt to validate by newest
mtime, so a receipt belonging to another story or role could satisfy the hook.
The shipping half of that race was fixed by per-receipt sentinels (#287, which
closed #290); this suite covers the successor: validation now binds to the
(story, role) pairs the kernel recorded in `mcp_active_dispatches`, resolved
through the canonical receipt resolver by `dispatched_receipts.py`, and falls
back to newest-by-mtime only when no binding exists.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest


@pytest.fixture
def verify_hook(plugin_root: Path) -> Path:
    return plugin_root / "hooks" / "synaptory-verify-receipt.sh"


# Fixed agent id used across the stdin payload and the SubagentStart marker
# so the two stay in sync (see _make_workspace / Issue #130).
_TEST_AGENT_ID = "test-agent-340"


def _subagent_stop_stdin(
    description: str = "software-engineer", agent_id: str = _TEST_AGENT_ID
) -> str:
    """Minimal SubagentStop JSON payload the hook reads from stdin."""
    return json.dumps(
        {
            "description": description,
            "agent_id": agent_id,
            "session_id": "test-session-340",
            "hook_event_name": "SubagentStop",
        }
    )


def _make_workspace(
    project_dir: Path,
    engagement: str = "structured",
    quality_enforcement: str = "strict",
) -> Path:
    """Create the minimal .synaptory/ workspace structure.

    Mirrors test_verify_receipt.py: quality_enforcement='lenient' selects the
    controlled/warn path (autonomous=False), 'strict' the blocking path.
    """
    suite = project_dir / ".synaptory"
    orch = suite / ".orchestrator"
    (orch / "receipts").mkdir(parents=True, exist_ok=True)
    (orch / "settings.md").write_text(
        f"Engagement: {engagement}\nQuality-Enforcement: {quality_enforcement}\n",
        encoding="utf-8",
    )
    markers = orch / "subagent-markers"
    markers.mkdir(parents=True, exist_ok=True)
    (markers / _TEST_AGENT_ID).write_text("", encoding="utf-8")
    return suite


def _write_receipt(
    receipts_dir: Path,
    project_dir: Path,
    name: str,
    *,
    story_id: str,
    role: str,
    valid: bool = True,
    mtime: float | None = None,
) -> Path:
    """A receipt that passes (or, with valid=False, fails) receipt_validator.py."""
    artifact_rel = "api/routes/auth.py"
    artifact_abs = project_dir / artifact_rel
    artifact_abs.parent.mkdir(parents=True, exist_ok=True)
    artifact_abs.write_text("# stub\n", encoding="utf-8")

    payload: dict = {
        "story_id": story_id,
        "role": role,
        "backend": "claude",
        "model": "claude-sonnet-4-5",
        "artifacts": [artifact_rel],
        "verification_commands": [
            {"command": "echo ok", "exit_code": 0, "summary": "smoke check passes"}
        ],
        "metrics": {"tests_added": 1, "coverage_delta": 0.0},
        "completed_at": "2026-05-20T12:00:00Z",
    }
    if not valid:
        # Drop required fields so the validator errors on this file. If the
        # hook picks it, the run warns (lenient) or blocks (strict).
        del payload["verification_commands"]
        del payload["completed_at"]
    receipts_dir.mkdir(parents=True, exist_ok=True)
    receipt = receipts_dir / name
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    if mtime is not None:
        os.utime(receipt, (mtime, mtime))
    return receipt


def _write_flat_state(orch: Path, *, bind_se_on: str | None) -> None:
    """A flat scrum board; bind_se_on names the story carrying an active
    se dispatch, None writes the same board with no binding at all."""
    story: dict = {"id": "US-042", "state": "in_progress"}
    if bind_se_on:
        story["mcp_active_dispatches"] = {
            "se": {"dispatch_id": "d" * 32, "started_at": "2026-05-20T11:00:00Z"}
        }
    (orch / "pipeline-state.json").write_text(
        json.dumps({"build_mode": "scrum", "current_stories": [story]}),
        encoding="utf-8",
    )


# ── binding present: the canonical receipt wins over a newer decoy ────────────


@pytest.mark.hook
def test_binding_selects_canonical_over_newer_wrong_receipt(
    verify_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """The #340 defect: a NEWER receipt for the wrong story/role is on disk,
    schema-invalid, while the dispatched story's canonical receipt is valid
    and older. Under mtime selection the hook validated the decoy and warned;
    under dispatch binding it must validate the canonical receipt and stay
    silent."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(project_dir, quality_enforcement="lenient")
    orch = suite / ".orchestrator"
    receipts = orch / "receipts"
    _write_flat_state(orch, bind_se_on="US-042")

    now = time.time()
    _write_receipt(
        receipts, project_dir, "US-042-se.json",
        story_id="US-042", role="software-engineer", mtime=now - 1000,
    )
    _write_receipt(
        receipts, project_dir, "US-099-qe.json",
        story_id="US-099", role="quality-engineer", valid=False, mtime=now,
    )

    result = run_hook(verify_hook, env=hook_env, stdin=_subagent_stop_stdin())
    combined = result.stderr + result.stdout
    assert result.returncode == 0, (
        f"bound canonical receipt is valid, hook must pass; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "Receipt validation issues" not in combined, (
        f"hook validated the newer wrong-story receipt instead of the bound "
        f"canonical one; combined={combined!r}"
    )


@pytest.mark.hook
def test_binding_with_missing_canonical_blocks_despite_newer_receipt(
    verify_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """When a dispatch is active and its canonical receipt is absent, the
    hook must report receipt_missing (blocking in autonomous mode) even
    though a newer, perfectly VALID receipt for another story/role sits on
    disk. That decoy passing the gate was exactly the #340 defect."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(project_dir, engagement="structured")
    orch = suite / ".orchestrator"
    receipts = orch / "receipts"
    _write_flat_state(orch, bind_se_on="US-042")

    _write_receipt(
        receipts, project_dir, "US-099-qe.json",
        story_id="US-099", role="quality-engineer",
    )

    result = run_hook(verify_hook, env=hook_env, stdin=_subagent_stop_stdin())
    assert result.returncode == 2, (
        f"bound dispatch without its canonical receipt must block; "
        f"got {result.returncode}; stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    combined = result.stderr + result.stdout
    assert "without writing a receipt" in combined, (
        f"expected the receipt_missing error; combined={combined!r}"
    )


# ── no binding: newest-by-mtime fallback is preserved ─────────────────────────


@pytest.mark.hook
def test_no_binding_falls_back_to_newest_by_mtime(
    verify_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """Same receipts as the binding test, but no story carries an active
    dispatch: the hook must take the legacy mtime path and validate the
    newest receipt (the invalid one), producing the validation warning.
    Legacy sessions and ceremony receipts with pseudo story ids depend on
    this fallback staying alive."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(project_dir, quality_enforcement="lenient")
    orch = suite / ".orchestrator"
    receipts = orch / "receipts"
    _write_flat_state(orch, bind_se_on=None)

    now = time.time()
    _write_receipt(
        receipts, project_dir, "US-042-se.json",
        story_id="US-042", role="software-engineer", mtime=now - 1000,
    )
    _write_receipt(
        receipts, project_dir, "US-099-qe.json",
        story_id="US-099", role="quality-engineer", valid=False, mtime=now,
    )

    result = run_hook(verify_hook, env=hook_env, stdin=_subagent_stop_stdin())
    combined = result.stderr + result.stdout
    assert result.returncode == 0, (
        f"lenient fallback warns but never blocks; stderr={result.stderr!r}"
    )
    assert "Receipt validation issues" in combined, (
        f"without a binding the hook must validate the newest receipt "
        f"(mtime fallback); combined={combined!r}"
    )


# ── SPQ layout: the binding resolves the workstream receipts dir ──────────────


@pytest.mark.hook
def test_spq_binding_resolves_workstream_receipt(
    verify_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """SPQ Work Unit receipts live under
    spq/cycles/<cycle-id>/workstreams/<ws>/receipts/ (never the flat dir).
    With an active dispatch recorded in the workstream's execution state,
    the hook must validate the canonical Work Unit receipt there and ignore
    a newer invalid decoy in the flat directory."""
    import spq_paths

    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(project_dir, quality_enforcement="lenient")
    orch = suite / ".orchestrator"
    cycle_id = "1-abcdef12"
    workstream = "api"

    (orch / "pipeline-state.json").write_text(
        json.dumps({"build_mode": "spq",
                    "lifecycle_state": "CYCLE_EXECUTION", "current_cycle": 1}),
        encoding="utf-8",
    )
    (orch / "spq").mkdir(parents=True, exist_ok=True)
    (orch / "spq" / "index.json").write_text(
        json.dumps({"cycle_seq_high": 1, "current_cycle_id": cycle_id,
                    "cycles": [{"cycle_id": cycle_id}]}),
        encoding="utf-8",
    )
    spq_paths.write_pin(str(project_dir), workstream)
    exec_state = Path(spq_paths.execution_state_path(
        str(project_dir), cycle_id, workstream))
    exec_state.parent.mkdir(parents=True, exist_ok=True)
    exec_state.write_text(
        json.dumps({
            "build_mode": "spq",
            "lifecycle_state": "CYCLE_EXECUTION",
            "current_stories": [{
                "id": "WU-API-1",
                "state": "in_progress",
                "mcp_active_dispatches": {
                    "se": {"dispatch_id": "e" * 32,
                           "started_at": "2026-05-20T11:00:00Z"}
                },
            }],
        }),
        encoding="utf-8",
    )

    ws_receipts = Path(spq_paths.receipts_dir(
        str(project_dir), cycle_id=cycle_id, workstream_id=workstream))
    now = time.time()
    _write_receipt(
        ws_receipts, project_dir, "WU-API-1-se.json",
        story_id="WU-API-1", role="software-engineer", mtime=now - 1000,
    )
    # Newer invalid decoy in the FLAT dir: mtime selection would pick it.
    _write_receipt(
        orch / "receipts", project_dir, "US-099-qe.json",
        story_id="US-099", role="quality-engineer", valid=False, mtime=now,
    )

    result = run_hook(verify_hook, env=hook_env, stdin=_subagent_stop_stdin())
    combined = result.stderr + result.stdout
    assert result.returncode == 0, (
        f"bound SPQ workstream receipt is valid, hook must pass; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "Receipt validation issues" not in combined, (
        f"hook validated the flat-dir decoy instead of the SPQ workstream "
        f"receipt; combined={combined!r}"
    )
