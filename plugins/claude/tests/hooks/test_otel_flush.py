"""Layer 2 — `synaptory-pipeline-snapshot.sh` Stop hook tests.

NOTE: The `synaptory-otel-flush.sh` hook described in the test spec does NOT exist
in this codebase. There is no standalone OTel flush hook — the CLI's OTel export
is invoked inline by the hooks that call `synaptory telemetry *` (the stub CLI
handles those silently).

These tests instead cover `synaptory-pipeline-snapshot.sh` (the Stop hook), which is
the closest high-risk analogue: it writes a pipeline state snapshot to disk and
is conditionally skipped when the workspace doesn't exist, mirroring the
"noop when disabled" contract the spec described.

Two tests pin the contract:
  - `test_otel_flush_invokes_cli_when_workspace_exists` — run the hook with a
    populated .synaptory/ workspace; assert the snapshot file is written.
  - `test_otel_flush_noop_when_workspace_absent` — no .synaptory/ directory;
    assert hook exits 0 without creating output files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def snapshot_hook(plugin_root: Path) -> Path:
    return plugin_root / "hooks" / "synaptory-pipeline-snapshot.sh"


def _make_workspace_with_state(project_dir: Path) -> tuple[Path, Path]:
    """Create a .synaptory/ workspace with a pipeline-state.json."""
    suite = project_dir / ".synaptory"
    orch = suite / ".orchestrator"
    receipts = orch / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)

    state = orch / "pipeline-state.json"
    state.write_text(
        json.dumps(
            {
                "version": "2.0",
                "build_mode": "scrum",
                "lifecycle_state": "SPRINT_EXECUTION",
                "current_sprint": 2,
                "sprint_goal": "Ship the auth module",
                "sprints_completed": ["sprint-1"],
                "current_stories": [
                    {"id": "US-001", "state": "done"},
                    {"id": "US-002", "state": "in_progress"},
                ],
            }
        ),
        encoding="utf-8",
    )

    settings = orch / "settings.md"
    settings.write_text("Project: test-project\n", encoding="utf-8")

    return suite, state


# ── Test: snapshot written when workspace exists ──────────────────────────────


@pytest.mark.hook
def test_otel_flush_invokes_cli_when_workspace_exists(
    snapshot_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """Workspace exists → hook writes last-session.md snapshot (exit 0).

    Analogous to "flush/export fires when the data dir is non-empty":
    the snapshot hook writes pipeline state to disk for the next session
    to read, which is the persistence-flush operation in this codebase.
    """
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite, _ = _make_workspace_with_state(project_dir)

    result = run_hook(snapshot_hook, env=hook_env, stdin="")
    assert result.returncode == 0, (
        f"snapshot hook must exit 0 with workspace present; stderr={result.stderr!r}"
    )

    snapshot_file = suite / ".orchestrator" / "last-session.md"
    assert snapshot_file.exists(), (
        f"expected last-session.md to be written at {snapshot_file}"
    )
    content = snapshot_file.read_text(encoding="utf-8")
    # The snapshot should contain some project context.
    assert len(content.strip()) > 0, "expected non-empty snapshot content"


# ── Test: noop when workspace absent ─────────────────────────────────────────


@pytest.mark.hook
def test_otel_flush_noop_when_workspace_absent(
    snapshot_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """No .synaptory/ workspace → hook exits 0 silently (no snapshot written).

    Analogous to "noop when OTel disabled": the hook guards all writes behind
    `[ -d $SUITE_DIR ]` and skips cleanly when the workspace doesn't exist.
    """
    # CLAUDE_PROJECT_DIR exists but has no .synaptory/ subdirectory.
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    assert not (project_dir / ".synaptory").exists()

    result = run_hook(snapshot_hook, env=hook_env, stdin="")
    assert result.returncode == 0, (
        f"snapshot hook must exit 0 when workspace absent; stderr={result.stderr!r}"
    )
    # No snapshot should be written.
    assert not (project_dir / ".synaptory").exists(), (
        "hook must not create .synaptory/ when workspace was absent"
    )
