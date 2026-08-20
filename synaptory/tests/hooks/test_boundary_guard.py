"""Layer 2 — `plugin/hooks/synaptory-boundary-guard.sh` contract tests.

Hypothesis: the blocking `PreToolUse` guard emits a `hookSpecificOutput`
deny payload for the three boundaries it owns, and stays silent (allow) for
everything else. A `PreToolUse` guard that over-blocks is worse than one that
under-blocks, so the allow cases are as load-bearing as the deny cases.

Guards under test:
  G1 — write-tool access to `.synaptory/tracker/` (story ops → tracker_cli.py)
  G2 — direct writes to `pipeline-state.json` (transitions → story_pipeline.py)
  G3 — write-tool access to `.synaptory/sync/` (readiness records are derived
       by `sync_barrier.py declare-ready <N>`, never hand-authored)

Note the guard always exits 0; the decision lives in stdout, not the return
code. Empty stdout is "allow".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def guard_hook(plugin_root: Path) -> Path:
    return plugin_root / "hooks" / "synaptory-boundary-guard.sh"


@pytest.fixture
def guard_env(hook_env) -> dict[str, str]:
    """Guard env with a `.synaptory/` workspace and the guard force-enabled.

    `SYNAPTORY_GUARDRAILS=1` pins activation so the test does not depend on
    the engagement-mode default read from `.orchestrator/settings.md`.
    """
    project = Path(hook_env["CLAUDE_PROJECT_DIR"])
    (project / ".synaptory").mkdir(exist_ok=True)
    return {**hook_env, "SYNAPTORY_GUARDRAILS": "1"}


def _tool_call(tool: str, **tool_input) -> str:
    """A PreToolUse stdin payload as Claude Code sends it."""
    return json.dumps(
        {
            "tool_name": tool,
            "tool_input": tool_input,
            "session_id": "test-session-001",
            "cwd": "/tmp",
            "hook_event_name": "PreToolUse",
        }
    )


def _deny_reason(stdout: str) -> str | None:
    """Return the deny reason, or None when the guard allowed the call."""
    s = stdout.strip()
    if not s:
        return None
    payload = json.loads(s)
    hso = payload.get("hookSpecificOutput") or {}
    if hso.get("permissionDecision") != "deny":
        return None
    return hso.get("permissionDecisionReason") or ""


# ─── G3: .synaptory/sync/ readiness records ───────────────────────────────────


@pytest.mark.hook
@pytest.mark.parametrize("tool", ["Write", "Edit"])
def test_g3_denies_write_tools_on_sync_record(guard_hook, guard_env, run_hook, tool):
    """A Write/Edit at `.synaptory/sync/slice-3/exec.json` is denied.

    The deny message must name the verb to use instead, or the agent has no
    recovery path from the block.
    """
    result = run_hook(
        guard_hook,
        env=guard_env,
        stdin=_tool_call(tool, file_path=".synaptory/sync/slice-3/exec.json"),
    )
    assert result.returncode == 0
    reason = _deny_reason(result.stdout)
    assert reason is not None, f"expected deny, got stdout: {result.stdout!r}"
    assert ".synaptory/sync/" in reason
    assert "declare-ready" in reason


@pytest.mark.hook
def test_g3_denies_absolute_path_into_sync(guard_hook, guard_env, run_hook):
    """Absolute paths resolve against the project dir just like relative ones."""
    project = Path(guard_env["CLAUDE_PROJECT_DIR"])
    target = project / ".synaptory" / "sync" / "slice-3" / "exec.json"
    result = run_hook(
        guard_hook, env=guard_env, stdin=_tool_call("Write", file_path=str(target))
    )
    reason = _deny_reason(result.stdout)
    assert reason is not None, f"expected deny, got stdout: {result.stdout!r}"
    assert "declare-ready" in reason


@pytest.mark.hook
def test_g3_denies_bash_redirect_into_sync(guard_hook, guard_env, run_hook):
    """Shelling out around the write tools is blocked too."""
    result = run_hook(
        guard_hook,
        env=guard_env,
        stdin=_tool_call(
            "Bash", command="echo '{}' > .synaptory/sync/slice-3/exec.json"
        ),
    )
    reason = _deny_reason(result.stdout)
    assert reason is not None, f"expected deny, got stdout: {result.stdout!r}"
    assert "declare-ready" in reason


@pytest.mark.hook
def test_g3_allows_declare_ready_invocation(guard_hook, guard_env, run_hook):
    """The sanctioned producer of the record must not be blocked by its own guard."""
    result = run_hook(
        guard_hook,
        env=guard_env,
        stdin=_tool_call(
            "Bash",
            command="python3 .synaptory/scripts/sync_barrier.py declare-ready 3",
        ),
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "", (
        f"declare-ready must be allowed, got: {result.stdout!r}"
    )


@pytest.mark.hook
def test_g3_allows_reading_a_sync_record(guard_hook, guard_env, run_hook):
    """G3 is a write boundary; Read is not a write tool."""
    result = run_hook(
        guard_hook,
        env=guard_env,
        stdin=_tool_call("Read", file_path=".synaptory/sync/slice-3/exec.json"),
    )
    assert result.stdout.strip() == ""


# ─── Allow path: the guard must not over-block ────────────────────────────────


@pytest.mark.hook
@pytest.mark.parametrize(
    "file_path",
    [
        "src/main.py",
        "docs/README.md",
        ".synaptory/ADR-001.md",
        ".synaptory/signals/signals.jsonl",
    ],
)
def test_allows_unrelated_writes(guard_hook, guard_env, run_hook, file_path):
    """Every path outside the three guarded boundaries stays writable."""
    result = run_hook(
        guard_hook, env=guard_env, stdin=_tool_call("Write", file_path=file_path)
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "", (
        f"{file_path} should be allowed, got: {result.stdout!r}"
    )


@pytest.mark.hook
def test_allows_unrelated_bash_command(guard_hook, guard_env, run_hook):
    result = run_hook(
        guard_hook,
        env=guard_env,
        stdin=_tool_call("Bash", command="pytest -q > /tmp/out.txt"),
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


@pytest.mark.hook
def test_guardrails_zero_disables_the_guard(guard_hook, guard_env, run_hook):
    """`SYNAPTORY_GUARDRAILS=0` is a full off switch — G3 is defence in depth."""
    env = {**guard_env, "SYNAPTORY_GUARDRAILS": "0"}
    result = run_hook(
        guard_hook,
        env=env,
        stdin=_tool_call("Write", file_path=".synaptory/sync/slice-3/exec.json"),
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


@pytest.mark.hook
def test_silent_exit_when_no_workspace(guard_hook, hook_env, run_hook):
    """No `.synaptory/` → not a synaptory project → guard is inert."""
    result = run_hook(
        guard_hook,
        env={**hook_env, "SYNAPTORY_GUARDRAILS": "1"},
        stdin=_tool_call("Write", file_path=".synaptory/sync/slice-3/exec.json"),
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


# ─── G1 / G2 regression ───────────────────────────────────────────────────────


@pytest.mark.hook
def test_g1_still_denies_tracker_writes(guard_hook, guard_env, run_hook):
    result = run_hook(
        guard_hook,
        env=guard_env,
        stdin=_tool_call("Write", file_path=".synaptory/tracker/stories/US-042.md"),
    )
    reason = _deny_reason(result.stdout)
    assert reason is not None, f"expected deny, got stdout: {result.stdout!r}"
    assert ".synaptory/tracker/" in reason
    assert "tracker_cli.py" in reason


@pytest.mark.hook
def test_g1_still_denies_tracker_bash_redirect(guard_hook, guard_env, run_hook):
    result = run_hook(
        guard_hook,
        env=guard_env,
        stdin=_tool_call(
            "Bash", command="echo done > .synaptory/tracker/stories/US-042.md"
        ),
    )
    reason = _deny_reason(result.stdout)
    assert reason is not None, f"expected deny, got stdout: {result.stdout!r}"
    assert "tracker_cli.py" in reason


@pytest.mark.hook
def test_g2_still_denies_pipeline_state_writes(guard_hook, guard_env, run_hook):
    result = run_hook(
        guard_hook,
        env=guard_env,
        stdin=_tool_call(
            "Write", file_path=".synaptory/.orchestrator/pipeline-state.json"
        ),
    )
    reason = _deny_reason(result.stdout)
    assert reason is not None, f"expected deny, got stdout: {result.stdout!r}"
    assert "pipeline-state.json" in reason
    assert "story_pipeline.py" in reason


@pytest.mark.hook
def test_g2_still_denies_pipeline_state_bash_redirect(guard_hook, guard_env, run_hook):
    result = run_hook(
        guard_hook,
        env=guard_env,
        stdin=_tool_call(
            "Bash",
            command="echo '{}' > .synaptory/.orchestrator/pipeline-state.json",
        ),
    )
    reason = _deny_reason(result.stdout)
    assert reason is not None, f"expected deny, got stdout: {result.stdout!r}"
    assert "story_pipeline.py" in reason
