"""Layer 2 — `plugin-claude/hooks/session-guard.sh` contract tests.

Hypothesis: the SubagentStart-adjacent guard exits silently when the
project is not a synaptory workspace, and emits a structured
`additional_context` payload with ADR / receipt / protocol counts when it is.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def guard_hook(plugin_root: Path) -> Path:
    return plugin_root / "hooks" / "session-guard.sh"


@pytest.mark.hook
def test_silent_exit_when_no_workspace(guard_hook, hook_env, run_hook):
    """Non-crew project (no `.synaptory/`) → exit 0, no stdout."""
    result = run_hook(guard_hook, env=hook_env, stdin="")
    assert result.returncode == 0
    assert result.stdout.strip() == "", (
        f"expected silent exit, got: {result.stdout!r}"
    )


@pytest.mark.hook
def test_emits_context_when_workspace_exists(
    guard_hook, hook_env, run_hook, parse_output
):
    """`.synaptory/` workspace → injects guard prompt with artefact counts."""
    project = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = project / ".synaptory"
    suite.mkdir(exist_ok=True)
    # A few sample artefacts so the counts are non-zero.
    (suite / "ADR-001.md").write_text("# ADR-1", encoding="utf-8")
    (suite / "ADR-002.md").write_text("# ADR-2", encoding="utf-8")
    (suite / ".orchestrator" / "receipts").mkdir(parents=True, exist_ok=True)
    (suite / ".orchestrator" / "receipts" / "US-001-se.json").write_text("{}", encoding="utf-8")
    (suite / ".protocols").mkdir(exist_ok=True)
    (suite / ".protocols" / "iron-laws.md").write_text("# Iron Laws", encoding="utf-8")

    result = run_hook(guard_hook, env=hook_env, stdin="")
    assert result.returncode == 0
    parsed = parse_output(result.stdout)
    assert parsed is not None
    ctx = parsed["additional_context"]

    # Spot-check the counts the guard reports.
    assert "2 architecture decisions" in ctx
    assert "1 pipeline receipts" in ctx
    assert "1 protocols" in ctx
    # Choice prompt language is the user-visible contract.
    assert "AskUserQuestion" in ctx
    assert "Recommended" in ctx
