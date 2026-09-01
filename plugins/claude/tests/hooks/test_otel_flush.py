"""Layer 2 — the OTel flush hook, and the Stop-hook snapshot beside it.

This file used to open by asserting the opposite of the truth:

    NOTE: The `synaptory-otel-flush.sh` hook described in the test spec does NOT
    exist in this codebase.

It does exist, at `plugin-claude/hooks/synaptory-otel-flush.sh`, registered on
SessionStart and Stop in hooks.json. So the one hook responsible for getting
spans to the control plane had NO coverage, which is how a complete three-host
Cycle reported `otel_events: 0` with nothing going red (#331 G11).

The pipeline-snapshot tests below are kept -- they are real tests of a real
hook, just not of this one.
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


# ─── the actual OTel flush hook (#331 G11) ──────────────────────────────────


@pytest.mark.hook
def test_otel_flush_ships_every_pending_span_batch(
    plugin_root: Path,
    hook_env: dict[str, str],
    run_hook,
    tmp_path: Path,
):
    """Each `spans-*.jsonl` must be handed to `telemetry traces`.

    Asserted against the stub CLI's telemetry log, so "the hook ran" cannot be
    mistaken for "the spans shipped" -- the distinction that made `otel_events:
    0` survive a whole Cycle unnoticed.
    """
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    otel = project_dir / ".synaptory" / ".orchestrator" / "otel"
    otel.mkdir(parents=True)
    batches = ["spans-sess-a.jsonl", "spans-sess-b.jsonl"]
    for name in batches:
        (otel / name).write_text('{"name":"span"}\n', encoding="utf-8")

    log = tmp_path / "telemetry.log"
    env = {**hook_env, "SYNAPTORY_STUB_TELEMETRY_LOG": str(log)}
    hook = plugin_root / "hooks" / "synaptory-otel-flush.sh"

    result = run_hook(hook, env=env, stdin="")
    assert result.returncode == 0, f"stderr={result.stderr!r}"

    assert log.exists(), "the flush hook never invoked `telemetry traces`"
    shipped = log.read_text(encoding="utf-8")
    for name in batches:
        assert name in shipped, f"{name} was not shipped:\n{shipped}"
    assert shipped.count("traces") == len(batches)


@pytest.mark.hook
def test_otel_flush_skips_empty_batches(
    plugin_root: Path,
    hook_env: dict[str, str],
    run_hook,
    tmp_path: Path,
):
    """An empty file is not a batch. Shipping it wastes a call and can look
    like a successful export of nothing."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    otel = project_dir / ".synaptory" / ".orchestrator" / "otel"
    otel.mkdir(parents=True)
    (otel / "spans-empty.jsonl").write_text("", encoding="utf-8")

    log = tmp_path / "telemetry.log"
    env = {**hook_env, "SYNAPTORY_STUB_TELEMETRY_LOG": str(log)}
    hook = plugin_root / "hooks" / "synaptory-otel-flush.sh"

    assert run_hook(hook, env=env, stdin="").returncode == 0
    assert not log.exists() or "spans-empty" not in log.read_text(encoding="utf-8")


@pytest.mark.hook
def test_otel_flush_is_silent_outside_a_synaptory_project(
    plugin_root: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """No otel dir, no work, no noise. Stop hooks fire in every project."""
    hook = plugin_root / "hooks" / "synaptory-otel-flush.sh"
    result = run_hook(hook, env=hook_env, stdin="")
    assert result.returncode == 0
    assert result.stderr.strip() == ""


@pytest.mark.hook
def test_the_writer_and_the_flusher_agree_on_the_path(plugin_root: Path):
    """A drift guard on the two halves of the export.

    `otel_writer` writes `.orchestrator/otel/spans-<sid>.jsonl` and the hook
    globs `$OTEL_DIR/spans-*.jsonl`. If either moves, spans accumulate on disk
    and the control plane silently reports zero -- with no error anywhere,
    because each half works perfectly on its own.
    """
    writer = (plugin_root / "hooks" / "lib" / "otel_writer.py").read_text("utf-8")
    flusher = (plugin_root / "hooks" / "synaptory-otel-flush.sh").read_text("utf-8")

    assert 'f"spans-{sid}.jsonl"' in writer
    assert '".orchestrator" / "otel"' in writer
    assert 'OTEL_DIR="$SUITE_DIR/.orchestrator/otel"' in flusher
    assert '"$OTEL_DIR"/spans-*.jsonl' in flusher
