"""Layer 2 — `plugin-claude/hooks/synaptory-boundary-guard.sh` contract tests.

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
    """A Write/Edit at `.synaptory/sync/cycle-3/exec.json` is denied.

    The deny message must name the verb to use instead, or the agent has no
    recovery path from the block.
    """
    result = run_hook(
        guard_hook,
        env=guard_env,
        stdin=_tool_call(tool, file_path=".synaptory/sync/cycle-3/exec.json"),
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
    target = project / ".synaptory" / "sync" / "cycle-3" / "exec.json"
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
            "Bash", command="echo '{}' > .synaptory/sync/cycle-3/exec.json"
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
        stdin=_tool_call("Read", file_path=".synaptory/sync/cycle-3/exec.json"),
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
        stdin=_tool_call("Write", file_path=".synaptory/sync/cycle-3/exec.json"),
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


@pytest.mark.hook
def test_silent_exit_when_no_workspace(guard_hook, hook_env, run_hook):
    """No `.synaptory/` → not a synaptory project → guard is inert."""
    result = run_hook(
        guard_hook,
        env={**hook_env, "SYNAPTORY_GUARDRAILS": "1"},
        stdin=_tool_call("Write", file_path=".synaptory/sync/cycle-3/exec.json"),
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
    assert "advance_kernel.py" in reason


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
    assert "advance_kernel.py" in reason


# ── G4: the SPQ store and the committed Cycle transport (#303) ─────────────


@pytest.mark.hook
@pytest.mark.parametrize("tool", ["Write", "Edit"])
@pytest.mark.parametrize(
    "target",
    [
        ".synaptory/.orchestrator/spq/cycles/7-abc12345/manifest.json",
        ".synaptory/.orchestrator/spq/cycles/7-abc12345/dependency-ledger.json",
        ".synaptory/.orchestrator/spq/cycles/7-abc12345/workstreams/spine/execution-state.json",
        ".synaptory/.orchestrator/spq/workstream",
        ".synaptory/cycles/7-abc12345/manifest.json",
        ".synaptory/cycles/7-abc12345/events/spine/0001-integrated-WU-1.json",
    ],
)
def test_g4_denies_write_tools_on_the_spq_store(
    guard_hook, guard_env, run_hook, tool, target
):
    """Before G4 the entire SPQ store was unguarded.

    G2 matches only the literal string `pipeline-state.json`, and G3 covers
    only `.synaptory/sync/`, so nothing stopped an agent hand-writing a Cycle
    manifest or a dependency event -- and a hand-written event would unblock a
    Work Unit on a claim nothing verified.
    """
    result = run_hook(
        guard_hook, env=guard_env, stdin=_tool_call(tool, file_path=target)
    )
    assert result.returncode == 0
    reason = _deny_reason(result.stdout)
    assert reason is not None, f"expected deny for {target}, got: {result.stdout!r}"
    # The deny must name the verb to use instead, or the agent has no recovery
    # path from the block.
    assert "spq_state_machine.py" in reason
    assert "hydrate_cycle" in reason


@pytest.mark.hook
def test_g4_denies_a_bash_redirect_into_the_spq_store(guard_hook, guard_env, run_hook):
    result = run_hook(
        guard_hook,
        env=guard_env,
        stdin=_tool_call(
            "Bash",
            command="echo '{}' > .synaptory/cycles/7-abc12345/manifest.json",
        ),
    )
    reason = _deny_reason(result.stdout)
    assert reason is not None, f"expected deny, got: {result.stdout!r}"
    assert "spq_state_machine.py" in reason


@pytest.mark.hook
def test_g4_denies_an_absolute_path_into_the_spq_store(guard_hook, guard_env, run_hook):
    project = Path(guard_env["CLAUDE_PROJECT_DIR"])
    target = project / ".synaptory" / ".orchestrator" / "spq" / "index.json"
    result = run_hook(
        guard_hook, env=guard_env, stdin=_tool_call("Write", file_path=str(target))
    )
    assert _deny_reason(result.stdout) is not None


@pytest.mark.hook
def test_g4_stays_narrow_and_allows_ordinary_project_files(
    guard_hook, guard_env, run_hook
):
    """The carve-out must not swallow normal work. `.synaptory/design/` and
    source files are none of G4's business."""
    for allowed in ("src/app.py", ".synaptory/design/mockups/index.html",
                    "docs/spq.md"):
        result = run_hook(
            guard_hook, env=guard_env, stdin=_tool_call("Write", file_path=allowed)
        )
        assert _deny_reason(result.stdout) is None, allowed


@pytest.mark.hook
def test_g4_is_suppressed_by_the_guardrails_override(guard_hook, guard_env, run_hook):
    """Honest scope, asserted rather than only documented: G4 is defence in
    depth, not a security boundary. The manifest HASH is what makes deliberate
    tampering detectable."""
    env = {**guard_env, "SYNAPTORY_GUARDRAILS": "0"}
    result = run_hook(
        guard_hook,
        env=env,
        stdin=_tool_call("Write", file_path=".synaptory/cycles/7-abc12345/manifest.json"),
    )
    assert _deny_reason(result.stdout) is None


# ── G4 extended: the committed Coordination-Cycle transport (#305) ──────────


@pytest.mark.hook
@pytest.mark.parametrize("tool", ["Write", "Edit"])
@pytest.mark.parametrize(
    "target",
    [
        ".synaptory/coordination-cycles/cc-1-abc12345/manifest.json",
        ".synaptory/coordination-cycles/cc-1-abc12345/events/3-aabbccdd/0001-contract_published-WU-1.json",
        ".synaptory/.orchestrator/spq/coordination-cycles/cc-1-abc12345/manifest.json",
    ],
)
def test_g4_denies_write_tools_on_the_coordination_store(
    guard_hook, guard_env, run_hook, tool, target
):
    """A hand-written coordination manifest is the highest-value forgery here.

    Its `selected_sha` list decides which child increments ship. Everything else
    G4 guards can, at worst, unblock one Work Unit on an unverified claim; this
    one can put a child increment nobody integrated into a release.
    """
    result = run_hook(
        guard_hook, env=guard_env, stdin=_tool_call(tool, file_path=target)
    )
    assert result.returncode == 0
    reason = _deny_reason(result.stdout)
    assert reason is not None, f"expected deny for {target}, got: {result.stdout!r}"
    assert "spq_state_machine.py" in reason


@pytest.mark.hook
def test_g4_denies_a_bash_redirect_into_the_coordination_store(
    guard_hook, guard_env, run_hook
):
    result = run_hook(
        guard_hook,
        env=guard_env,
        stdin=_tool_call(
            "Bash",
            command="echo '{}' > .synaptory/coordination-cycles/cc-1-abc12345/manifest.json",
        ),
    )
    assert _deny_reason(result.stdout) is not None


@pytest.mark.hook
def test_g4_still_denies_the_303_roots_after_the_305_widening(
    guard_hook, guard_env, run_hook
):
    """Regression pin: widening the alternation must not shadow the old arms.

    `coordination-cycles` contains `cycles` as a substring, so the two roots and
    the two regex alternatives have to stay independently matched.
    """
    for target in (".synaptory/cycles/7-abc12345/manifest.json",
                   ".synaptory/.orchestrator/spq/index.json"):
        result = run_hook(
            guard_hook, env=guard_env, stdin=_tool_call("Write", file_path=target)
        )
        assert _deny_reason(result.stdout) is not None, target
    result = run_hook(
        guard_hook,
        env=guard_env,
        stdin=_tool_call(
            "Bash", command="echo x > .synaptory/cycles/7-abc12345/manifest.json"
        ),
    )
    assert _deny_reason(result.stdout) is not None


@pytest.mark.hook
def test_g4_roots_and_the_bash_regex_name_the_same_trees(guard_hook):
    """Two spellings of one rule; a fix applied to one and not the other is a
    hole that only shows up on the path nobody tested."""
    text = Path(guard_hook).read_text(encoding="utf-8")
    for tree in (".orchestrator', 'spq'", "'cycles'", "'coordination-cycles'"):
        assert tree in text, tree
    assert r"(\.orchestrator/spq|cycles|coordination-cycles)/" in text
