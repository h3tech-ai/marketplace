"""Layer 2 — `plugin-claude/hooks/synaptory-boundary-guard.sh` contract tests.

Hypothesis: the blocking `PreToolUse` guard emits a `hookSpecificOutput`
deny payload for the three boundaries it owns, and stays silent (allow) for
everything else. A `PreToolUse` guard that over-blocks is worse than one that
under-blocks, so the allow cases are as load-bearing as the deny cases.

Guards under test:
  G1 — write-tool access to `.synaptory/tracker/` (story ops → tracker_cli.py)
  G2 — direct writes to `pipeline-state.json` (transitions → story_pipeline.py)
  G4 — write-tool access to the SPQ Cycle store, on both roots

G3 is absent because it is retired with `.synaptory/sync/` (ADR-035). Its cases
are deleted rather than relaxed: a test that asserted a write to that directory
is *allowed* would pin the absence of a guard on a path nothing creates, which
is coverage of nothing. What replaced the readiness record it protected — the
sealed declaration, the cut record, the dependency events — is under G4.

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
    """`SYNAPTORY_GUARDRAILS=0` is a full off switch — every guard here is
    defence in depth, not a security boundary.

    The probe path is one G1 actually denies. It used to be a
    `.synaptory/sync/` write, which after ADR-035 no guard denies at all: the
    test would then have passed whether the off switch worked or not.
    """
    env = {**guard_env, "SYNAPTORY_GUARDRAILS": "0"}
    result = run_hook(
        guard_hook,
        env=env,
        stdin=_tool_call("Write", file_path=".synaptory/tracker/stories/US-042.md"),
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


@pytest.mark.hook
def test_silent_exit_when_no_workspace(guard_hook, hook_env, run_hook):
    """No `.synaptory/` → not a synaptory project → guard is inert.

    Same reasoning as the off-switch test above for why the probe is a path a
    guard would otherwise deny.
    """
    result = run_hook(
        guard_hook,
        env={**hook_env, "SYNAPTORY_GUARDRAILS": "1"},
        stdin=_tool_call("Write", file_path=".synaptory/tracker/stories/US-042.md"),
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


# ── G4: the SPQ Cycle store, on both of its roots ───────────────────────────


@pytest.mark.hook
@pytest.mark.parametrize("tool", ["Write", "Edit"])
@pytest.mark.parametrize(
    "target",
    [
        ".synaptory/.orchestrator/spq/index.json",
        ".synaptory/.orchestrator/spq/cycles/7-abc12345/execution-state.json",
        ".synaptory/.orchestrator/spq/cycles/7-abc12345/dependency-ledger.json",
        ".synaptory/cycles/7-abc12345/manifest.json",
        ".synaptory/cycles/7-abc12345/cuts.json",
        ".synaptory/cycles/7-abc12345/events/0001-integrated-WU-1.json",
    ],
)
def test_g4_denies_write_tools_on_the_spq_store(
    guard_hook, guard_env, run_hook, tool, target
):
    """Before G4 the entire SPQ store was unguarded.

    G2 matches only the literal string `pipeline-state.json`, so nothing
    stopped an agent hand-writing a Cycle declaration, a dependency event, or a
    cut. A hand-written event unblocks a Work Unit on a claim nothing verified;
    a hand-written cut shrinks the set the Checkpoint barrier has to clear.

    Both roots are exercised, because they are two different exposures and a
    fix to one has repeatedly not been a fix to the other: the local store
    holds the board this clone acts on, the committed one holds what every
    other clone reads.
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


# ── G4: the two roots stay independently matched ────────────────────────────


@pytest.mark.hook
def test_g4_matches_both_roots_on_both_ingress_paths(
    guard_hook, guard_env, run_hook
):
    """Regression pin, now that the third root is gone.

    `coordination-cycles` used to be an alternative in the same regex and
    contains `cycles` as a substring, so removing it is exactly the kind of
    edit that can collapse the remaining alternation or drop an arm. Both roots
    have to be denied through the file-path ingress and through the Bash
    redirect ingress -- four cases, because a guard fixed on one ingress and
    not the other is a hole on the path nobody tested.
    """
    for target in (
        ".synaptory/cycles/7-abc12345/manifest.json",
        ".synaptory/.orchestrator/spq/index.json",
    ):
        result = run_hook(
            guard_hook, env=guard_env, stdin=_tool_call("Write", file_path=target)
        )
        assert _deny_reason(result.stdout) is not None, target
        result = run_hook(
            guard_hook,
            env=guard_env,
            stdin=_tool_call("Bash", command="echo x > " + target),
        )
        assert _deny_reason(result.stdout) is not None, target


@pytest.mark.hook
def test_g4_roots_and_the_bash_regex_name_the_same_trees(guard_hook):
    """Two spellings of one rule; a fix applied to one and not the other is a
    hole that only shows up on the path nobody tested.

    Asserted in both directions: the retired third root must be absent from
    both spellings, or the guard would still advertise a tree the product does
    not create.
    """
    text = Path(guard_hook).read_text(encoding="utf-8")
    for tree in (".orchestrator', 'spq'", "'cycles'"):
        assert tree in text, tree
    assert r"(\.orchestrator/spq|cycles)/" in text
    assert "coordination-cycles" not in text
    assert "'.synaptory', 'sync'" not in text
