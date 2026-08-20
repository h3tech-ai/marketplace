"""Layer 2 — `synaptory-watch-drift.sh` FileChanged hook (epic #75 P4).

Execs the real hook against a fixture workspace: a clean board is silent, a
drifted board emits an additionalContext warning + a watch_drift breadcrumb,
and the hook never exits non-zero (FileChanged is non-blocking).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def drift_hook(plugin_root: Path) -> Path:
    return plugin_root / "hooks" / "synaptory-watch-drift.sh"


def _state(project_dir: Path, stories: list[dict]) -> Path:
    orch = project_dir / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True, exist_ok=True)
    (orch / "pipeline-state.json").write_text(
        json.dumps({"version": "2.0", "build_mode": "scrum",
                    "lifecycle_state": "SPRINT_EXECUTION", "current_stories": stories}),
        encoding="utf-8",
    )
    return orch


_STDIN = json.dumps({"hook_event_name": "FileChanged", "file": "pipeline-state.json"})


@pytest.mark.hook
def test_clean_state_is_silent(drift_hook, hook_env, run_hook):
    _state(Path(hook_env["CLAUDE_PROJECT_DIR"]), [{"id": "US-1", "state": "testing"}])
    result = run_hook(drift_hook, env=hook_env, stdin=_STDIN)
    assert result.returncode == 0
    assert result.stdout.strip() == "", f"clean state must be silent, got {result.stdout!r}"


@pytest.mark.hook
def test_drift_emits_warning_and_breadcrumb(drift_hook, hook_env, run_hook):
    orch = _state(Path(hook_env["CLAUDE_PROJECT_DIR"]), [{"id": "US-9", "state": "BOGUS"}])
    result = run_hook(drift_hook, env=hook_env, stdin=_STDIN)
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    out = json.loads(result.stdout)
    ctx = out["additionalContext"]
    assert "drift detected" in ctx
    assert "US-9" in ctx and "BOGUS" in ctx
    # Backticks in the message survived (no shell command-substitution mangling).
    assert "`next_action`" in ctx
    # No stray stderr (the command-substitution bug would print "command not found").
    assert "command not found" not in result.stderr
    # events.jsonl breadcrumb written.
    events = (orch / "events.jsonl").read_text(encoding="utf-8")
    assert "watch_drift" in events


@pytest.mark.hook
def test_corrupt_state_warns(drift_hook, hook_env, run_hook):
    orch = Path(hook_env["CLAUDE_PROJECT_DIR"]) / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True, exist_ok=True)
    (orch / "pipeline-state.json").write_text("{ truncated json")
    result = run_hook(drift_hook, env=hook_env, stdin=_STDIN)
    assert result.returncode == 0
    assert "drift detected" in json.loads(result.stdout)["additionalContext"]


@pytest.mark.hook
def test_config_drift_emits_warning(drift_hook, hook_env, run_hook):
    """Regression (PR #89 re-review): the hook watches .synaptory.yaml, so a
    bad build_mode there must warn even with a clean pipeline-state.json."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    _state(project_dir, [{"id": "US-1", "state": "testing"}])  # clean state
    (project_dir / ".synaptory.yaml").write_text("project_id: t\nbuild_mode: waterfall\n")
    stdin = json.dumps({"hook_event_name": "FileChanged", "file": ".synaptory.yaml"})
    result = run_hook(drift_hook, env=hook_env, stdin=stdin)
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    ctx = json.loads(result.stdout)["additionalContext"]
    assert "build_mode" in ctx and "waterfall" in ctx


@pytest.mark.hook
def test_no_workspace_is_silent(drift_hook, hook_env, run_hook, tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    env = {**hook_env, "CLAUDE_PROJECT_DIR": str(bare)}
    result = run_hook(drift_hook, env=env, stdin=_STDIN)
    assert result.returncode == 0
    assert result.stdout.strip() == ""
