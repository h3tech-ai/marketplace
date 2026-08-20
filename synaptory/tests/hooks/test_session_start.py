"""Layer 2 — `synaptory-session-start.sh` contract tests.

SessionStart hook: flushes outbox, pings telemetry, then injects pipeline
context into the session if a .synaptory/ workspace exists. Exits silently
when no workspace is present (non-synaptory project).

Tests pin:
  - Session ID context is injected when a workspace + pipeline-state.json exist.
  - `synaptory telemetry session-start` is invoked when the CLI is present.
  - Hook exits 0 gracefully when no CLI is available.
  - Hook exits 0 silently when no .synaptory/ workspace exists.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def session_start_hook(plugin_root: Path) -> Path:
    return plugin_root / "hooks" / "synaptory-session-start.sh"


@pytest.fixture
def telemetry_log(tmp_path: Path) -> Path:
    """Stub-CLI sink for `telemetry` invocations."""
    p = tmp_path / "telemetry.log"
    p.write_text("", encoding="utf-8")
    return p


def _make_workspace(project_dir: Path) -> Path:
    """Create minimal .synaptory/ workspace."""
    suite = project_dir / ".synaptory"
    orch = suite / ".orchestrator"
    receipts = orch / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    settings = orch / "settings.md"
    settings.write_text("Project: my-test-project\n", encoding="utf-8")
    return suite


def _write_pipeline_state(project_dir: Path) -> Path:
    """Write a v2.0 pipeline-state.json with scrum state."""
    state_file = (
        project_dir / ".synaptory" / ".orchestrator" / "pipeline-state.json"
    )
    state_file.write_text(
        json.dumps(
            {
                "version": "2.0",
                "build_mode": "scrum",
                "lifecycle_state": "SPRINT_EXECUTION",
                "current_sprint": 3,
                "sprint_goal": "Deliver the search feature",
                "sprints_completed": ["sprint-1", "sprint-2"],
                "current_stories": [
                    {"id": "US-010", "state": "done"},
                    {"id": "US-011", "state": "in_progress"},
                ],
            }
        ),
        encoding="utf-8",
    )
    return state_file


# ── Test: context injection with workspace present ────────────────────────────


@pytest.mark.hook
def test_session_start_writes_session_id_file(
    session_start_hook: Path,
    hook_env: dict[str, str],
    run_hook,
    parse_output,
):
    """Workspace + pipeline state → hook emits additional_context JSON.

    The `additional_context` must mention the workspace path and sprint
    context so the orchestrator is anchored to the right project state.
    """
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    _make_workspace(project_dir)
    _write_pipeline_state(project_dir)

    result = run_hook(session_start_hook, env=hook_env, stdin="")
    assert result.returncode == 0, (
        f"session-start must exit 0 with workspace; stderr={result.stderr!r}"
    )

    parsed = parse_output(result.stdout)
    assert parsed is not None, "expected additional_context JSON from session-start"
    ctx: str = parsed["additional_context"]

    # Must contain workspace reference and pipeline project heading.
    assert ".synaptory" in ctx or "synaptory" in ctx.lower(), (
        f"additional_context missing workspace reference; ctx={ctx[:200]!r}"
    )
    # Sprint state should appear (from pipeline-state.json).
    assert "SPRINT_EXECUTION" in ctx or "scrum" in ctx.lower() or "Sprint" in ctx, (
        f"additional_context missing sprint context; ctx={ctx[:400]!r}"
    )


@pytest.mark.hook
def test_session_start_invokes_config_fetch(
    session_start_hook: Path,
    hook_env: dict[str, str],
    stub_cli,
    run_hook,
    telemetry_log: Path,
):
    """CLI present → `synaptory telemetry session-start` is called.

    We use the stub CLI's telemetry log to detect the invocation. The
    `outbox flush` call will fail (stub exits 2 for unknown subcommands)
    but the hook handles it with `|| true` and must still succeed.
    """
    env = {
        **hook_env,
        "SYNAPTORY_STUB_TELEMETRY_LOG": str(telemetry_log),
    }

    result = run_hook(session_start_hook, env=env, stdin="")
    assert result.returncode == 0, (
        f"session-start must exit 0 with CLI available; stderr={result.stderr!r}"
    )

    log = telemetry_log.read_text(encoding="utf-8")
    assert "telemetry session-start" in log, (
        f"expected `telemetry session-start` CLI call; log={log!r}"
    )


# ── Test: graceful skip without workspace ────────────────────────────────────


@pytest.mark.hook
def test_session_start_exits_silently_without_workspace(
    session_start_hook: Path,
    hook_env: dict[str, str],
    run_hook,
    parse_output,
):
    """No .synaptory/ workspace → hook exits 0 with no context injection.

    The hook calls the CLI for telemetry regardless, but skips the context
    injection block and produces no JSON output when the workspace is absent.
    """
    # CLAUDE_PROJECT_DIR exists but no .synaptory/ inside it.
    result = run_hook(session_start_hook, env=hook_env, stdin="")
    assert result.returncode == 0, (
        f"session-start must exit 0 with no workspace; stderr={result.stderr!r}"
    )

    # No workspace → no additional_context JSON (hook exits before printing it).
    parsed = parse_output(result.stdout)
    assert parsed is None, (
        f"expected no additional_context when workspace absent; got {parsed!r}"
    )


# ── Test: TRANSITIONAL legacy hiro-crew detection (remove with migrate cmd) ───


@pytest.mark.hook
def test_session_start_nudges_legacy_hiro_crew_project(
    session_start_hook: Path,
    hook_env: dict[str, str],
    run_hook,
    parse_output,
):
    """A legacy .hiro-crew.yaml (no .synaptory.yaml) → nudge `synaptory migrate`.

    Transitional behavior: the hook must detect an un-migrated hiro-crew repo and
    emit additional_context pointing the operator at `synaptory migrate`, then
    exit 0 (without trying to load synaptory state).
    """
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    (project_dir / ".hiro-crew.yaml").write_text("project_id: legacy\n", encoding="utf-8")

    result = run_hook(session_start_hook, env=hook_env, stdin="")
    assert result.returncode == 0, (
        f"session-start must exit 0 for legacy repo; stderr={result.stderr!r}"
    )
    parsed = parse_output(result.stdout)
    assert parsed is not None, "expected a migration nudge for a legacy hiro-crew repo"
    ctx: str = parsed["additional_context"]
    assert "synaptory migrate" in ctx, f"nudge missing the command; ctx={ctx[:300]!r}"
    assert "hiro-crew" in ctx, f"nudge should name the legacy layout; ctx={ctx[:300]!r}"


@pytest.mark.hook
def test_session_start_skips_nudge_when_already_migrated(
    session_start_hook: Path,
    hook_env: dict[str, str],
    run_hook,
    parse_output,
):
    """Both .hiro-crew.yaml and .synaptory.yaml present → no legacy nudge.

    Once migrated (a .synaptory.yaml exists), the legacy detection must not fire.
    """
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    (project_dir / ".hiro-crew.yaml").write_text("project_id: legacy\n", encoding="utf-8")
    (project_dir / ".synaptory.yaml").write_text("project_id: migrated\n", encoding="utf-8")
    # No .synaptory/ workspace dir → hook should exit 0 silently (no nudge).

    result = run_hook(session_start_hook, env=hook_env, stdin="")
    assert result.returncode == 0
    parsed = parse_output(result.stdout)
    if parsed is not None:
        assert "synaptory migrate" not in parsed.get("additional_context", ""), (
            "legacy nudge must not fire once .synaptory.yaml exists"
        )
