"""Layer 2 — `synaptory-verify-receipt.sh` contract tests.

SubagentStop hook enforces the receipt protocol after every subagent.
Mode-aware behaviour:
  - Controlled mode (default, SYNAPTORY_MODE not set or ≠ 'autonomous'):
      warns on missing/invalid receipt but exits 0.
  - Autonomous mode (SYNAPTORY_MODE=autonomous):
      exits 1 on missing or invalid receipt, blocking the pipeline.

Receipts live at:
    <CLAUDE_PROJECT_DIR>/.synaptory/.orchestrator/receipts/*.json

The hook reads the subagent description from stdin (SubagentStop JSON payload).
It exits silently (0) when no .synaptory/ workspace exists.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def verify_hook(plugin_root: Path) -> Path:
    return plugin_root / "hooks" / "synaptory-verify-receipt.sh"


# Fixed agent id used across the stdin payload and the SubagentStart marker
# so the two stay in sync (see _make_workspace / Issue #130).
_TEST_AGENT_ID = "test-agent-001"


def _subagent_stop_stdin(
    description: str = "software-engineer", agent_id: str = _TEST_AGENT_ID
) -> str:
    """Minimal SubagentStop JSON payload the hook reads from stdin."""
    return json.dumps(
        {
            "description": description,
            "agent_id": agent_id,
            "session_id": "test-session-001",
            "hook_event_name": "SubagentStop",
        }
    )


def _write_subagent_marker(suite: Path, agent_id: str = _TEST_AGENT_ID) -> None:
    """Simulate the SubagentStart marker synaptory-inject-protocols.sh drops for
    a ``synaptory:*`` dispatch. Without it, SubagentStop treats the subagent as
    non-synaptory and skips enforcement (Issue #130).
    """
    markers = suite / ".orchestrator" / "subagent-markers"
    markers.mkdir(parents=True, exist_ok=True)
    (markers / agent_id).write_text("", encoding="utf-8")


def _make_workspace(
    project_dir: Path,
    engagement: str = "structured",
    quality_enforcement: str = "strict",
) -> Path:
    """Create the minimal .synaptory/ workspace structure.

    engagement: 'structured' (default) or 'interactive'
    quality_enforcement: 'strict' (default), 'standard', or 'lenient'

    The hook determines AUTONOMOUS via mode_reader.py:
        mode_reader.py outputs: autonomous=<bool>
        autonomous is True when quality_enforcement in {strict, standard}
        autonomous is False when quality_enforcement == lenient

    So to test controlled/warn mode: use quality_enforcement='lenient'.
    To test structured/blocking mode: use quality_enforcement='strict' (default).
    """
    suite = project_dir / ".synaptory"
    orch = suite / ".orchestrator"
    receipts = orch / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    settings = orch / "settings.md"
    settings.write_text(
        f"Engagement: {engagement}\nQuality-Enforcement: {quality_enforcement}\n",
        encoding="utf-8",
    )
    # Mark the default test dispatch as a synaptory agent so receipt enforcement
    # runs (Issue #130 gates enforcement on this marker).
    _write_subagent_marker(suite)
    return suite


def _write_receipt(receipts_dir: Path, project_dir: Path, name: str = "US-042-se.json") -> Path:
    """Write a receipt JSON that passes receipt_validator.py schema.

    Required fields per receipt_validator.py _DEFAULT_REQUIRED_FIELDS:
        story_id, role, backend, model, artifacts, verification_commands,
        metrics, completed_at
    verification_commands must include exit_code and summary per the validator.
    artifacts must exist on disk (validator checks file existence).
    """
    # Create the artifact file so the validator's existence check passes.
    artifact_rel = "api/routes/auth.py"
    artifact_abs = project_dir / artifact_rel
    artifact_abs.parent.mkdir(parents=True, exist_ok=True)
    artifact_abs.write_text("# stub\n", encoding="utf-8")

    receipt = receipts_dir / name
    receipt.write_text(
        json.dumps(
            {
                "story_id": "US-042",
                "role": "software-engineer",
                "backend": "claude",
                "model": "claude-sonnet-4-5",
                "artifacts": [artifact_rel],
                "verification_commands": [
                    {
                        "command": "echo ok",
                        "exit_code": 0,
                        "summary": "smoke check passes",
                    }
                ],
                "metrics": {"tests_added": 3, "coverage_delta": 0.02},
                "completed_at": "2026-05-20T12:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return receipt


# ── Test: controlled mode + missing receipt ───────────────────────────────────


@pytest.mark.hook
def test_controlled_mode_missing_receipt_warns_not_errors(
    verify_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """Controlled mode (default): missing receipt → exit 0 with a warning.

    The hook should NOT block the pipeline when no SYNAPTORY_MODE is set.
    It emits a WARNING to stderr but exits 0.
    """
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    # Use 'lenient' quality enforcement so mode_reader reports autonomous=False,
    # making the hook use the controlled/warn path instead of blocking.
    # (The hook's AUTONOMOUS=false branch warns but exits 0.)
    _make_workspace(project_dir, quality_enforcement="lenient")
    # No receipt written — receipts dir is empty.

    env = {**hook_env}
    env.pop("SYNAPTORY_MODE", None)

    result = run_hook(
        verify_hook,
        env=env,
        stdin=_subagent_stop_stdin("software-engineer"),
    )
    assert result.returncode == 0, (
        f"controlled-mode missing receipt must exit 0; stderr={result.stderr!r}"
    )
    # Hook must communicate the problem — check stderr for warning language.
    combined = result.stderr + result.stdout
    assert "WARNING" in combined or "receipt" in combined.lower(), (
        f"expected warning about missing receipt; combined output={combined!r}"
    )


# ── Test: autonomous mode + missing receipt ───────────────────────────────────


@pytest.mark.hook
def test_autonomous_mode_missing_receipt_exits_nonzero(
    verify_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """Structured (autonomous) mode: missing receipt → exit 2 (pipeline blocked).

    The hook reads mode_reader.py which parses Engagement from settings.md.
    Default (structured) maps to autonomous=True → the hook blocks.

    Exit code is **2**, not 1 (epic #75 P2): the Claude Code hook contract
    treats exit 2 as a BLOCKING error that force-feeds stderr to Claude,
    whereas exit 1 is non-blocking — and with the Stop-hook loop engine
    driving continuation, a non-blocking failure could be steamrolled.
    """
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    # 'structured' maps to AUTONOMOUS=true in the hook via mode_reader.py.
    _make_workspace(project_dir, engagement="structured")
    # No receipt written.

    result = run_hook(
        verify_hook,
        env=hook_env,
        stdin=_subagent_stop_stdin("software-engineer"),
    )
    assert result.returncode == 2, (
        f"structured/autonomous missing receipt must exit 2 (blocking); "
        f"got {result.returncode}; stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    combined = result.stderr + result.stdout
    assert "ERROR" in combined or "receipt" in combined.lower(), (
        f"expected error message about missing receipt; combined={combined!r}"
    )


# ── Test: valid receipt passes validation ─────────────────────────────────────


@pytest.mark.hook
def test_valid_receipt_schema_passes(
    verify_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """A receipt that passes schema validation → exit 0.

    Uses lenient quality enforcement so verification_commands are run but
    any execution failures are treated as warnings (exit 0), not errors.
    This isolates the schema-validation path from the execution environment.
    The key invariant under test: a well-formed receipt is not rejected by
    the validator.
    """
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    # Lenient quality → AUTONOMOUS=false → hook warns but exits 0 on any
    # verification runner failures (which are environment-dependent in tests).
    suite = _make_workspace(project_dir, quality_enforcement="lenient")
    receipts = suite / ".orchestrator" / "receipts"
    _write_receipt(receipts, project_dir)

    result = run_hook(
        verify_hook,
        env=hook_env,
        stdin=_subagent_stop_stdin("software-engineer"),
    )
    assert result.returncode == 0, (
        f"valid receipt must pass validation; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# ── Test: non-synaptory subagent is not enforced (Issue #130) ─────────────────


@pytest.mark.hook
def test_non_synaptory_subagent_skips_enforcement(
    verify_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """A subagent with no SubagentStart marker (e.g. a Workflow worker,
    Explore/general-purpose, another plugin's agent) is not a synaptory
    pipeline agent — even in autonomous mode with a missing receipt it must
    exit 0 (no block) and emit no receipt_missing error, so it can't flood the
    reliability error rate. Regression for Issue #130.
    """
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    # Autonomous workspace (would block a real synaptory agent), but do NOT
    # write the marker → this dispatch is treated as non-synaptory.
    suite = _make_workspace(project_dir, engagement="structured")
    marker = suite / ".orchestrator" / "subagent-markers" / "workflow-subagent-99"
    assert not marker.exists()

    result = run_hook(
        verify_hook,
        env=hook_env,
        stdin=_subagent_stop_stdin("workflow subagent", agent_id="workflow-subagent-99"),
    )
    assert result.returncode == 0, (
        f"non-synaptory subagent must not be blocked; got {result.returncode}; "
        f"stderr={result.stderr!r}"
    )
    combined = result.stderr + result.stdout
    assert "receipt" not in combined.lower(), (
        f"non-synaptory subagent must not emit a receipt warning/error; got {combined!r}"
    )


# ── Test: silent exit when no workspace ──────────────────────────────────────


@pytest.mark.hook
def test_silent_exit_when_no_workspace(
    verify_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """No .synaptory/ workspace (non-crew project) → silent exit 0."""
    # CLAUDE_PROJECT_DIR exists but has no .synaptory/ subdirectory.
    result = run_hook(
        verify_hook,
        env=hook_env,
        stdin=_subagent_stop_stdin("software-engineer"),
    )
    assert result.returncode == 0
    # No output expected when there's nothing to do.
    assert result.stdout.strip() == "", (
        f"expected no stdout from hook when workspace absent; got {result.stdout!r}"
    )
