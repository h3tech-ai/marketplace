"""Layer 2 — `synaptory-loop-continue.sh` (epic #75 P2).

Execs the real Stop hook against a fixture workspace and asserts the
decision JSON: `decision:block` when structured-mode work remains, empty
output (allow stop) for every governance hard-stop, and always exit 0.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def loop_hook(plugin_root: Path) -> Path:
    return plugin_root / "hooks" / "synaptory-loop-continue.sh"


def _workspace(project_dir: Path, *, engagement: str = "structured",
               lifecycle: str = "SPRINT_EXECUTION",
               stories: list[dict] | None = None) -> None:
    orch = project_dir / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True, exist_ok=True)
    (orch / "settings.md").write_text(f"Engagement: {engagement}\n", encoding="utf-8")
    (project_dir / ".synaptory.yaml").write_text("project_id: t\n", encoding="utf-8")
    (orch / "pipeline-state.json").write_text(
        json.dumps({
            "version": "2.0", "build_mode": "scrum",
            "lifecycle_state": lifecycle, "current_sprint": 2,
            "current_stories": stories if stories is not None else [
                {"id": "US-1", "title": "T", "state": "testing"}
            ],
        }),
        encoding="utf-8",
    )


_STDIN = json.dumps({"session_id": "sess-1", "hook_event_name": "Stop"})


@pytest.mark.hook
def test_blocks_stop_when_work_remains(loop_hook, hook_env, run_hook):
    _workspace(Path(hook_env["CLAUDE_PROJECT_DIR"]))
    result = run_hook(loop_hook, env=hook_env, stdin=_STDIN)
    assert result.returncode == 0
    out = json.loads(result.stdout)
    assert out["decision"] == "block"
    assert "next action: dispatch_qe" in out["reason"]
    assert "US-1" in out["reason"]


@pytest.mark.hook
@pytest.mark.parametrize(
    ("kw", "why"),
    [
        ({"engagement": "interactive"}, "interactive mode"),
        ({"lifecycle": "SPRINT_REVIEW"}, "non-execution lifecycle"),
        ({"stories": [{"id": "US-1", "state": "awaiting_acceptance"}]}, "human gate"),
        ({"stories": [{"id": "US-1", "state": "done"}]}, "sprint complete"),
    ],
)
def test_allows_stop_silently(loop_hook, hook_env, run_hook, kw, why):
    _workspace(Path(hook_env["CLAUDE_PROJECT_DIR"]), **kw)
    result = run_hook(loop_hook, env=hook_env, stdin=_STDIN)
    assert result.returncode == 0, f"{why}: stderr={result.stderr!r}"
    assert result.stdout.strip() == "", f"{why}: expected no output, got {result.stdout!r}"


@pytest.mark.hook
def test_kill_switch_env_allows_stop(loop_hook, hook_env, run_hook):
    _workspace(Path(hook_env["CLAUDE_PROJECT_DIR"]))
    env = {**hook_env, "SYNAPTORY_LOOP_DISABLE": "1"}
    result = run_hook(loop_hook, env=env, stdin=_STDIN)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


@pytest.mark.hook
def test_no_workspace_is_silent(loop_hook, hook_env, run_hook, tmp_path):
    # CLAUDE_PROJECT_DIR without a .synaptory dir → not a synaptory project.
    bare = tmp_path / "bare"
    bare.mkdir()
    env = {**hook_env, "CLAUDE_PROJECT_DIR": str(bare)}
    result = run_hook(loop_hook, env=env, stdin=_STDIN)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


@pytest.mark.hook
def test_guard_trips_and_then_allows_stop(loop_hook, hook_env, run_hook):
    """Same board across stops on one session → continues twice, then the
    runaway guard lets the session stop."""
    _workspace(Path(hook_env["CLAUDE_PROJECT_DIR"]))
    r1 = run_hook(loop_hook, env=hook_env, stdin=_STDIN)
    r2 = run_hook(loop_hook, env=hook_env, stdin=_STDIN)
    r3 = run_hook(loop_hook, env=hook_env, stdin=_STDIN)
    assert json.loads(r1.stdout)["decision"] == "block"
    assert json.loads(r2.stdout)["decision"] == "block"
    assert r3.stdout.strip() == "", "guard should let the session stop at the cap"
