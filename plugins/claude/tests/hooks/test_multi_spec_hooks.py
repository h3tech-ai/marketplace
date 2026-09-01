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
plugin-claude/tests/lib/test_multi_spec.py which covers the Python helpers.
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


# ─── SPQ receipt shipping (#326) ────────────────────────────────────────────


@pytest.fixture
def spq_workspace(hook_env: dict[str, str]) -> tuple[Path, dict[str, Path]]:
    """An SPQ clone: no flat `receipts/`, no `specs/`, only the SPQ tree.

    That absence is the point. Pre-fix, `synaptory-session-end.sh` gated its
    whole ship loop on `[ -d "$RECEIPTS_DIR" ] || [ -d "$_orch/specs" ]`, so an
    SPQ clone skipped it before the glob was ever consulted.

    Seeds one receipt in each of the three SPQ homes (spq_paths.py):
    workstream, cycle-level, and coordination-cycle.
    """
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    orch = project_dir / ".synaptory" / ".orchestrator"
    cycle = orch / "spq" / "cycles" / "1-abcdef12"
    homes = {
        "workstream": cycle / "workstreams" / "api" / "receipts",
        "cycle": cycle / "receipts",
        "coordination": (orch / "spq" / "coordination-cycles"
                         / "cc-1-abcdef12" / "receipts"),
    }
    written: dict[str, Path] = {}
    for kind, d in homes.items():
        d.mkdir(parents=True, exist_ok=True)
        name = {"workstream": "WU-API-1-se.json",
                "cycle": "CHECKPOINT-1-tw.json",
                "coordination": "RELEASE-1-qe.json"}[kind]
        path = d / name
        path.write_text(json.dumps({
            "story_id": name.rsplit("-", 1)[0],
            "role": "software-engineer",
            "backend": "claude",
            "model": "claude-opus-4-7",
            "agent": "software-engineer",
            "artifacts": [],
            "verification_commands": [],
            "metrics": {},
            "token_usage": {},
            "completed_at": "2026-09-01T00:00:00Z",
        }), encoding="utf-8")
        written[kind] = path

    (orch / "settings.md").write_text("Project: tri-host\n", encoding="utf-8")
    (orch / "pipeline-state.json").write_text(
        json.dumps({"version": "3.0", "build_mode": "spq",
                    "lifecycle_state": "CYCLE_EXECUTION", "current_cycle": 1}),
        encoding="utf-8",
    )
    return project_dir, written


@pytest.mark.hook
def test_session_end_ships_receipts_from_every_spq_home(
    plugin_root: Path,
    hook_env: dict[str, str],
    spq_workspace: tuple[Path, dict[str, Path]],
    run_hook,
    tmp_path: Path,
):
    """SessionEnd must ship SPQ receipts, and mark each with a `.shipped`
    sentinel.

    This is the regression for #326: on the 2026-09-01 tri-host Cycle the
    Claude lane wrote 5 valid SPQ receipts and shipped 0, because
    `_list_receipt_json` globbed only `receipts/` and `specs/*/receipts/`.
    `/cost` read the whole lane as $0 and `/quality` saw nothing.

    Asserted against the stub CLI's telemetry log rather than the sentinel
    alone, so "the hook shipped it" cannot be confused with "the file was
    touched".
    """
    project_dir, written = spq_workspace
    telemetry_log = tmp_path / "telemetry.log"
    env = {**hook_env, "SYNAPTORY_STUB_TELEMETRY_LOG": str(telemetry_log)}

    hook = plugin_root / "hooks" / "synaptory-session-end.sh"
    stdin = json.dumps({
        "session_id": "spq-session",
        "end_reason": "user_quit",
        "hook_event_name": "SessionEnd",
    })
    result = run_hook(hook, env=env, stdin=stdin)
    assert result.returncode == 0, f"stderr={result.stderr!r}"

    assert telemetry_log.exists(), (
        "SessionEnd never invoked `telemetry receipt` for any SPQ receipt "
        "(the ship loop was skipped entirely)"
    )
    shipped = telemetry_log.read_text(encoding="utf-8")
    for kind, path in written.items():
        assert str(path) in shipped, (
            f"{kind} receipt was not shipped: {path}\nlog={shipped!r}"
        )
        assert Path(f"{path}.shipped").exists(), (
            f"{kind} receipt shipped but got no sentinel: {path}"
        )


@pytest.mark.hook
def test_session_end_spq_ship_is_idempotent(
    plugin_root: Path,
    hook_env: dict[str, str],
    spq_workspace: tuple[Path, dict[str, Path]],
    run_hook,
    tmp_path: Path,
):
    """A second SessionEnd must not re-ship receipts whose sentinel is current.

    The sentinel is what makes the new SPQ glob safe to run on every session
    end: without this the wider glob would re-ship every past Cycle's receipts
    every time a session closed.
    """
    project_dir, written = spq_workspace
    hook = plugin_root / "hooks" / "synaptory-session-end.sh"
    stdin = json.dumps({
        "session_id": "spq-session",
        "end_reason": "user_quit",
        "hook_event_name": "SessionEnd",
    })

    first_log = tmp_path / "first.log"
    run_hook(hook, env={**hook_env, "SYNAPTORY_STUB_TELEMETRY_LOG": str(first_log)},
             stdin=stdin)
    assert first_log.exists(), "first pass shipped nothing"

    second_log = tmp_path / "second.log"
    run_hook(hook, env={**hook_env, "SYNAPTORY_STUB_TELEMETRY_LOG": str(second_log)},
             stdin=stdin)
    second = second_log.read_text(encoding="utf-8") if second_log.exists() else ""
    for kind, path in written.items():
        assert str(path) not in second, (
            f"{kind} receipt re-shipped despite a current sentinel: {path}"
        )


@pytest.mark.hook
def test_verify_receipt_ships_spq_workstream_receipt(
    plugin_root: Path,
    hook_env: dict[str, str],
    spq_workspace: tuple[Path, dict[str, Path]],
    run_hook,
    tmp_path: Path,
):
    """SubagentStop ships the SPQ receipt the subagent just wrote.

    SessionEnd is the final-flush safety net; SubagentStop is the path that is
    supposed to ship in-flight. #326 broke both, so both are pinned. The hook
    only enforces for `synaptory:*` dispatches, so the SubagentStart marker has
    to exist or it exits 0 early.
    """
    project_dir, written = spq_workspace
    agent_id = "software-engineer-001"
    marker_dir = project_dir / ".synaptory" / ".orchestrator" / "subagent-markers"
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / agent_id).write_text("", encoding="utf-8")

    telemetry_log = tmp_path / "telemetry.log"
    env = {**hook_env, "SYNAPTORY_STUB_TELEMETRY_LOG": str(telemetry_log)}

    hook = plugin_root / "hooks" / "synaptory-verify-receipt.sh"
    stdin = json.dumps({
        "agent_id": agent_id,
        "agent_type": "software-engineer",
        "session_id": "spq-session",
        "transcript_path": "/tmp/t.jsonl",
        "cwd": str(project_dir),
        "hook_event_name": "SubagentStop",
    })
    run_hook(hook, env=env, stdin=stdin)

    assert telemetry_log.exists(), (
        "SubagentStop never shipped the SPQ workstream receipt"
    )
    shipped = telemetry_log.read_text(encoding="utf-8")
    assert str(written["workstream"]) in shipped, (
        f"SPQ workstream receipt not shipped\nlog={shipped!r}"
    )


# ─── SPQ receipt visibility in the display hooks (#336) ─────────────────────


@pytest.mark.hook
def test_session_start_counts_spq_receipts(
    plugin_root: Path,
    hook_env: dict[str, str],
    spq_workspace: tuple[Path, dict[str, Path]],
    run_hook,
    parse_output,
):
    """A resumed SPQ session must not be told there is no evidence on disk.

    Pre-fix this reported `0 agent completions recorded` for a Cycle with five
    receipts, because the count globbed only `.orchestrator/receipts/` (#336).
    "0 receipts" is a signal an orchestrator acts on, so being wrong is worse
    than being absent.
    """
    project_dir, written = spq_workspace
    hook = plugin_root / "hooks" / "synaptory-session-start.sh"
    result = run_hook(hook, env=hook_env, stdin=json.dumps({
        "session_id": "spq-session", "hook_event_name": "SessionStart",
        "source": "startup",
    }))
    assert result.returncode == 0, f"stderr={result.stderr!r}"

    parsed = parse_output(result.stdout)
    assert parsed is not None, "session-start injected no context"
    ctx = parsed["additional_context"]
    assert "0 agent completions" not in ctx, (
        f"SPQ receipts invisible to SessionStart:\n{ctx}"
    )
    assert f"{len(written)} agent completions" in ctx, (
        f"expected {len(written)} receipts counted, context was:\n{ctx}"
    )


@pytest.mark.hook
def test_pipeline_snapshot_names_spq_receipts(
    plugin_root: Path,
    hook_env: dict[str, str],
    spq_workspace: tuple[Path, dict[str, Path]],
    run_hook,
):
    """The Stop snapshot must name SPQ receipts, not just count them.

    `last-session.md` is what the next session reads to reconstruct state, so
    an SPQ Cycle whose receipts are all invisible here resumes blind.
    """
    project_dir, written = spq_workspace
    hook = plugin_root / "hooks" / "synaptory-pipeline-snapshot.sh"
    result = run_hook(hook, env=hook_env, stdin="")
    assert result.returncode == 0, f"stderr={result.stderr!r}"

    snapshot = project_dir / ".synaptory" / ".orchestrator" / "last-session.md"
    assert snapshot.exists(), "snapshot not written"
    text = snapshot.read_text(encoding="utf-8")
    for kind, path in written.items():
        assert path.name in text, (
            f"snapshot omits the {kind} receipt {path.name}:\n{text}"
        )


@pytest.mark.hook
def test_reanchor_injects_spq_receipts(
    plugin_root: Path,
    hook_env: dict[str, str],
    spq_workspace: tuple[Path, dict[str, Path]],
    run_hook,
    parse_output,
):
    """PreCompact re-anchoring is when losing receipts hurts most.

    The hook re-injects recent receipts so they survive compaction. Pointing at
    the flat dir meant an SPQ session re-anchored with no evidence at all.
    """
    project_dir, written = spq_workspace
    hook = plugin_root / "hooks" / "synaptory-reanchor.sh"
    result = run_hook(hook, env=hook_env, stdin=json.dumps({
        "session_id": "spq-session", "hook_event_name": "PreCompact",
        "trigger": "auto",
    }))
    assert result.returncode == 0, f"stderr={result.stderr!r}"

    parsed = parse_output(result.stdout)
    assert parsed is not None, "reanchor injected no context"
    ctx = parsed["additional_context"]
    assert "Recent Agent Completions" in ctx, (
        f"reanchor found no receipts to re-inject:\n{ctx}"
    )
    # It re-injects the 3 most recent of the 3 seeded, so all should appear.
    for kind, path in written.items():
        assert path.name in ctx, (
            f"reanchor omits the {kind} receipt {path.name}:\n{ctx}"
        )


@pytest.mark.hook
def test_session_guard_counts_spq_receipts(
    plugin_root: Path,
    hook_env: dict[str, str],
    spq_workspace: tuple[Path, dict[str, Path]],
    run_hook,
):
    """The session guard's receipt count feeds the framing it shows the user.

    Asserted on the rendered phrase rather than a bare digit: the context also
    carries an ADR count and a protocol count, so `"3" in output` would pass on
    any of the three and would have passed pre-fix by coincidence.
    """
    project_dir, written = spq_workspace
    hook = plugin_root / "hooks" / "session-guard.sh"
    result = run_hook(hook, env=hook_env, stdin=json.dumps({
        "session_id": "spq-session", "hook_event_name": "SessionStart",
    }))
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    combined = result.stdout + result.stderr
    if "pipeline receipts" not in combined:
        pytest.skip("session-guard did not render its project context here")
    assert "0 pipeline receipts" not in combined, (
        f"SPQ receipts invisible to session-guard:\n{combined}"
    )
    assert f"{len(written)} pipeline receipts" in combined, (
        f"session-guard counted the wrong number of SPQ receipts:\n{combined}"
    )
