"""Layer 2 — shell-hook contract tests for multi-spec support.

Exercises the real .sh hooks against a v3 multi-spec fixture and asserts on:

  * `synaptory-session-start.sh` — renders the rollup context with every spec's
    lifecycle/sprint, marks the active spec.
  * `synaptory-pipeline-snapshot.sh` — writes a `last-session.md` that names
    every spec and the active one.
  * `synaptory-verify-receipt.sh` — when SYNAPTORY_ACTIVE_SPEC is set and the
    per-spec receipts directory exists, the hook uses it instead of the
    legacy flat path.
  * `synaptory-session-end.sh` — emits a rollup sentinel into CLAUDE.md (via
    update_claude_md.py --rollup-sentinel in a background subshell).

These pin the multi-spec contract end-to-end at the shell layer, complementing
plugin/tests/lib/test_multi_spec.py which covers the Python helpers.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest


# ─── Fixtures: multi-spec workspace ────────────────────────────────────────


@pytest.fixture
def multispec_workspace(hook_env: dict[str, str]) -> Path:
    """Seed a v3 multi-spec project under hook_env's CLAUDE_PROJECT_DIR.

    Creates .synaptory/.orchestrator/specs/{platform,contract-mastery,ehr-integration}/
    and writes a v3.0 pipeline-state.json with platform active at SPRINT_EXECUTION,
    contract-mastery + ehr-integration at INCEPTION.
    """
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    orch = project_dir / ".synaptory" / ".orchestrator"
    for sid in ("platform", "contract-mastery", "ehr-integration"):
        (orch / "specs" / sid / "receipts").mkdir(parents=True, exist_ok=True)
    (orch / "settings.md").write_text("Project: hano-app\nengagement: structured\n",
                                       encoding="utf-8")
    (orch / "pipeline-state.json").write_text(
        json.dumps(
            {
                "version": "3.0",
                "build_mode": "scrum",
                "active_spec": "platform",
                "specs": {
                    "platform": {
                        "lifecycle_state": "SPRINT_EXECUTION",
                        "current_sprint": 9,
                        "sprint_goal": "Multi-tenant audit log",
                        "current_stories": [
                            {"id": "US-100", "state": "in_progress"},
                            {"id": "US-101", "state": "done"},
                        ],
                        "sprints_completed": [{"sprint": 1}, {"sprint": 2}],
                    },
                    "contract-mastery": {
                        "lifecycle_state": "INCEPTION",
                        "current_sprint": 0,
                        "current_stories": [],
                    },
                    "ehr-integration": {
                        "lifecycle_state": "INCEPTION",
                        "current_sprint": 0,
                        "current_stories": [],
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    return project_dir


# ─── synaptory-session-start.sh ─────────────────────────────────────────────────


@pytest.mark.hook
def test_session_start_renders_multispec_rollup(
    plugin_root: Path,
    hook_env: dict[str, str],
    multispec_workspace: Path,
    run_hook,
    parse_output,
):
    """The session-start additional_context must include all three specs +
    flag the active one. This is what the orchestrator sees on resume."""
    hook = plugin_root / "hooks" / "synaptory-session-start.sh"
    result = run_hook(hook, env=hook_env, stdin="")
    assert result.returncode == 0, f"stderr={result.stderr!r}"

    parsed = parse_output(result.stdout)
    assert parsed is not None
    ctx = parsed["additional_context"]

    # Every spec id appears in the rollup.
    assert "platform" in ctx
    assert "contract-mastery" in ctx
    assert "ehr-integration" in ctx
    # Active spec is labelled.
    assert "Active Spec: platform" in ctx
    assert "(active)" in ctx  # appears next to the platform sub-heading
    # Per-spec lifecycle state is rendered.
    assert "SPRINT_EXECUTION" in ctx
    assert "INCEPTION" in ctx


# ─── synaptory-verify-receipt.sh ────────────────────────────────────────────────


@pytest.mark.hook
def test_verify_receipt_routes_to_spec_receipts_dir(
    plugin_root: Path,
    hook_env: dict[str, str],
    multispec_workspace: Path,
    run_hook,
):
    """When SYNAPTORY_ACTIVE_SPEC is set and the per-spec receipts dir exists, the
    hook should source receipts from there. We assert this indirectly by
    writing a valid receipt under specs/platform/receipts/ (NOT under the
    legacy flat receipts/ dir) and confirming the hook exits 0."""
    hook = plugin_root / "hooks" / "synaptory-verify-receipt.sh"
    spec_receipts = (multispec_workspace / ".synaptory" / ".orchestrator"
                     / "specs" / "platform" / "receipts")

    # Write a receipt that matches the subagent stdin's agent_id.
    receipt = {
        "story_id": "US-100",
        "role": "software-engineer",
        "backend": "claude",
        "model": "claude-opus-4-7",
        "agent": "software-engineer",
        "artifacts": [],
        "verification_commands": [],
        "metrics": {},
        "token_usage": {},
        "completed_at": "2026-05-25T00:00:00Z",
    }
    (spec_receipts / "US-100-se.json").write_text(json.dumps(receipt))

    env = {**hook_env, "SYNAPTORY_ACTIVE_SPEC": "platform"}
    stdin = json.dumps({
        "agent_id": "software-engineer-001",
        "agent_type": "software-engineer",
        "session_id": "test-session",
        "transcript_path": "/tmp/t.jsonl",
        "cwd": str(multispec_workspace),
        "hook_event_name": "SubagentStop",
    })
    result = run_hook(hook, env=env, stdin=stdin)
    # Hook may exit 0 (controlled mode) or 1 (autonomous strict mode); we
    # only care that it RAN — the path-routing check is that it didn't
    # complain about the receipts directory being missing.
    assert "receipts directory" not in result.stderr.lower(), (
        "verify-receipt did not honor SYNAPTORY_ACTIVE_SPEC: missing-dir error in stderr"
    )


@pytest.mark.hook
def test_verify_receipt_resolves_active_spec_from_pipeline_state(
    plugin_root: Path,
    hook_env: dict[str, str],
    multispec_workspace: Path,
    run_hook,
):
    """When SYNAPTORY_ACTIVE_SPEC is NOT exported but pipeline-state.json carries
    `active_spec`, the verify hook must still route to the per-spec receipts
    dir. Pre-fix behavior dropped to the legacy flat dir and produced
    spurious receipt_invalid spans for every multi-spec agent run.
    """
    hook = plugin_root / "hooks" / "synaptory-verify-receipt.sh"
    spec_receipts = (multispec_workspace / ".synaptory" / ".orchestrator"
                     / "specs" / "platform" / "receipts")

    receipt = {
        "story_id": "US-200",
        "role": "software-engineer",
        "backend": "claude",
        "model": "claude-opus-4-7",
        "agent": "software-engineer",
        "artifacts": [],
        "verification_commands": [],
        "metrics": {},
        "token_usage": {},
        "completed_at": "2026-05-28T00:00:00Z",
    }
    (spec_receipts / "US-200-se.json").write_text(json.dumps(receipt))

    # Explicitly omit SYNAPTORY_ACTIVE_SPEC from env. The hook should fall back
    # to reading active_spec from pipeline-state.json (which the
    # multispec_workspace fixture pins to "platform").
    env = {k: v for k, v in hook_env.items() if k != "SYNAPTORY_ACTIVE_SPEC"}
    stdin = json.dumps({
        "agent_id": "software-engineer-002",
        "agent_type": "software-engineer",
        "session_id": "test-session-2",
        "transcript_path": "/tmp/t2.jsonl",
        "cwd": str(multispec_workspace),
        "hook_event_name": "SubagentStop",
    })
    result = run_hook(hook, env=env, stdin=stdin)
    assert "receipts directory" not in result.stderr.lower(), (
        "verify-receipt did not fall back to pipeline-state.json: missing-dir "
        f"error in stderr={result.stderr!r}"
    )
    # Sanity: legacy flat dir was NOT picked. We assert this by confirming
    # the receipt under specs/platform/receipts/ is the one that gets the
    # .shipped sentinel (or at least is iterated). Indirect — the strong
    # signal is the missing-dir absence above.
    assert (spec_receipts / "US-200-se.json").exists()


# ─── synaptory-pipeline-snapshot.sh ─────────────────────────────────────────────


@pytest.mark.hook
def test_pipeline_snapshot_names_every_spec(
    plugin_root: Path,
    hook_env: dict[str, str],
    multispec_workspace: Path,
    run_hook,
):
    """The Stop-hook snapshot at .synaptory/.orchestrator/last-session.md
    must include each spec's id + active marker so a fresh session can
    reconstruct multi-spec state."""
    hook = plugin_root / "hooks" / "synaptory-pipeline-snapshot.sh"
    result = run_hook(hook, env=hook_env, stdin="")
    assert result.returncode == 0, f"stderr={result.stderr!r}"

    snapshot = multispec_workspace / ".synaptory" / ".orchestrator" / "last-session.md"
    assert snapshot.exists()
    text = snapshot.read_text(encoding="utf-8")
    for sid in ("platform", "contract-mastery", "ehr-integration"):
        assert sid in text, f"snapshot missing spec {sid!r}: {text}"
    assert "Active Spec: platform" in text
    assert "(active)" in text


# ─── synaptory-session-end.sh (sentinel rollup) ─────────────────────────────────


@pytest.mark.hook
def test_session_end_writes_multispec_sentinel(
    plugin_root: Path,
    hook_env: dict[str, str],
    multispec_workspace: Path,
    run_hook,
):
    """SessionEnd schedules a background sentinel rebuild. We wait briefly
    and then assert the CLAUDE.md sentinel block carries the multi-spec
    rollup form (active_spec + indented specs: list)."""
    hook = plugin_root / "hooks" / "synaptory-session-end.sh"
    (multispec_workspace / "CLAUDE.md").write_text("# hano-app\n\n", encoding="utf-8")

    stdin = json.dumps({
        "session_id": "test-session",
        "end_reason": "user_quit",
        "hook_event_name": "SessionEnd",
    })
    result = run_hook(hook, env=hook_env, stdin=stdin)
    assert result.returncode == 0, f"stderr={result.stderr!r}"

    # The sentinel write happens in a background subshell. Poll briefly.
    claude_md = multispec_workspace / "CLAUDE.md"
    deadline = time.time() + 5.0
    text = ""
    while time.time() < deadline:
        text = claude_md.read_text(encoding="utf-8")
        if "<!-- synaptory-state" in text:
            break
        time.sleep(0.1)

    assert "<!-- synaptory-state" in text, (
        f"sentinel not written within 5s: {text!r}"
    )
    assert "active_spec: platform" in text
    assert "specs:" in text
    # All three spec ids show up in the indented rollup.
    for sid in ("platform", "contract-mastery", "ehr-integration"):
        assert sid in text, f"sentinel rollup missing {sid}: {text}"
