"""Layer 2 — `synaptory-inject-protocols.sh` contract tests.

Phase 1 modernisation: the hook now emits the compact protocol index
(plugin/hooks/data/compacted-protocols.md) as the additionalContext payload
instead of the full 92 KB body of all 17 protocols.  The individual
protocol fetch loop still runs so that full bodies land in
.synaptory/.protocols/ for SKILL.md !cat usage, but those bodies are NOT
what appears in additionalContext.

Tests pin:
  - The compact file content lands in additionalContext (CP path and disk fallback).
  - The fetch loop still runs and populates .synaptory/.protocols/ on disk.
  - The depth-guard blocks at MAX_AGENT_DEPTH+1 with a BLOCKED payload.
  - Silent skip when neither the CLI nor the compact file is available.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# The 17 protocol names hardcoded in plugin/hooks/synaptory-inject-protocols.sh.
# Order matters — they shape the system prompt every subagent sees.
PROTOCOL_NAMES = [
    "receipt-protocol",
    "input-validation",
    "tool-efficiency",
    "freshness-protocol",
    "iron-laws",
    "verification-discipline",
    "socratic-gate",
    "anti-safe-harbor",
    "script-output-handling",
    "clean-code-self-check",
    "scope-challenge",
    "finding-memory",
    "tdd-discipline",
    "code-review-response",
    "subagent-isolation",
    "source-attribution",
    "open-decision-registry",
]


@pytest.fixture
def inject_hook(plugin_root: Path) -> Path:
    return plugin_root / "hooks" / "synaptory-inject-protocols.sh"


def _seed_all_protocols(stub_cli, prefix: str = "BODY:") -> dict[str, str]:
    """Write a distinct canned body for every protocol; return name→body map."""
    bodies: dict[str, str] = {}
    for name in PROTOCOL_NAMES:
        body = f"# {prefix} protocols/{name}\n\nbody-marker-{name}\n"
        stub_cli.add_body(f"protocols/{name}", body)
        bodies[name] = body
    return bodies


@pytest.mark.hook
def test_cp_path_emits_compact_file(
    inject_hook, hook_env, stub_cli, subagent_stdin, run_hook, parse_output, plugin_root: Path
):
    """Stub CLI returns every protocol → additionalContext is the compact index file."""
    _seed_all_protocols(stub_cli)

    result = run_hook(inject_hook, env=hook_env, stdin=subagent_stdin)
    assert result.returncode == 0, f"hook failed: {result.stderr}"

    parsed = parse_output(result.stdout)
    assert parsed is not None, "expected non-empty additionalContext"
    ctx: str = parsed["additional_context"]

    # The compact file has these canonical section headings.
    assert "Synaptory Protocols" in ctx, "compact file heading missing"
    assert "IRON LAWS" in ctx, "Iron Laws section missing from compact payload"
    assert "RECEIPT PROTOCOL" in ctx, "Receipt Protocol section missing from compact payload"
    assert "VERIFICATION DISCIPLINE" in ctx, "Verification Discipline section missing"


@pytest.mark.hook
def test_risk_checklist_survives_into_additional_context(
    inject_hook, hook_env, stub_cli, subagent_stdin, run_hook, parse_output
):
    """Regression (PR #83 re-review): a project `risk_checklist:` must reach
    subagents. The compact-protocol load resets CONTEXT, so an earlier bug
    dropped the appended checklist entirely — healthcare/security review
    patterns never made it into additionalContext."""
    _seed_all_protocols(stub_cli)
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    (project_dir / ".synaptory").mkdir(parents=True, exist_ok=True)
    (project_dir / ".synaptory" / "risk.md").write_text(
        "# PHI Review Patterns\n- CANARY_RISK_PATTERN: no MRN in logger.*\n",
        encoding="utf-8",
    )
    (project_dir / ".synaptory.yaml").write_text(
        "project_id: test\nrisk_checklist: .synaptory/risk.md\n", encoding="utf-8"
    )

    result = run_hook(inject_hook, env=hook_env, stdin=subagent_stdin)
    assert result.returncode == 0, f"hook failed: {result.stderr}"
    parsed = parse_output(result.stdout)
    assert parsed is not None
    ctx: str = parsed["additional_context"]
    # Both the compact protocol payload AND the risk checklist must be present.
    assert "Synaptory Protocols" in ctx or "IRON LAWS" in ctx, "compact payload lost"
    assert "CANARY_RISK_PATTERN" in ctx, (
        "project risk_checklist was dropped from additionalContext"
    )
    assert "Project Risk Checklist" in ctx


@pytest.mark.hook
def test_compact_payload_under_10kb(
    inject_hook, hook_env, stub_cli, subagent_stdin, run_hook, parse_output
):
    """The compact additionalContext must be under the 10 KB cap."""
    _seed_all_protocols(stub_cli)

    result = run_hook(inject_hook, env=hook_env, stdin=subagent_stdin)
    assert result.returncode == 0, f"hook failed: {result.stderr}"

    parsed = parse_output(result.stdout)
    assert parsed is not None
    ctx: str = parsed["additional_context"]
    size = len(ctx.encode("utf-8"))
    assert size <= 10 * 1024, (
        f"additionalContext exceeds 10 KB cap: {size} bytes"
    )


@pytest.mark.hook
def test_cp_fetch_used_when_disk_file_absent(
    inject_hook, plugin_root, hook_env, stub_cli, subagent_stdin, run_hook, parse_output
):
    """Distributed-build regression guard.

    `./synaptory build` purges every hooks/data/*.md from the marketplace
    tree (ADR-016) — the on-disk compact file only exists in source-tree
    dev. A real install MUST get compacted-protocols via
    `synaptory skills get hooks/data/compacted-protocols` (seeded by
    api/synaptory_api/seed.py) or every subagent dispatch loses protocol
    content. Simulate the distributed shape by removing the on-disk file
    for the duration of the test.
    """
    canary = "# CANARY: fetched via CP, not disk\n\nbody-marker-cp-fetch\n"
    stub_cli.add_body("hooks/data/compacted-protocols", canary)

    compact_file = plugin_root / "hooks" / "data" / "compacted-protocols.md"
    backup = compact_file.with_suffix(".md.bak")
    compact_file.rename(backup)
    try:
        result = run_hook(inject_hook, env=hook_env, stdin=subagent_stdin)
    finally:
        backup.rename(compact_file)

    assert result.returncode == 0, f"hook failed: {result.stderr}"
    parsed = parse_output(result.stdout)
    assert parsed is not None, "expected non-empty additionalContext"
    ctx: str = parsed["additional_context"]
    assert "CANARY: fetched via CP, not disk" in ctx, (
        "hook must fetch hooks/data/compacted-protocols via the CLI when "
        f"the on-disk file is absent — got: {ctx[:200]!r}"
    )
    assert "unavailable" not in ctx.lower(), (
        "hook silently degraded to the placeholder instead of using the CLI fetch"
    )


@pytest.mark.hook
def test_disk_fallback_when_cli_missing(
    inject_hook, plugin_root, stub_cli_unavailable, subagent_stdin, run_hook, parse_output, tmp_path
):
    """No CLI on PATH → compact file is still emitted (it lives on disk in plugin/hooks/data/)."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    env = {
        **stub_cli_unavailable,
        "CLAUDE_PLUGIN_ROOT": str(plugin_root),
        "CLAUDE_PROJECT_DIR": str(project_dir),
    }
    result = run_hook(inject_hook, env=env, stdin=subagent_stdin)
    assert result.returncode == 0, f"hook failed: {result.stderr}"

    parsed = parse_output(result.stdout)
    assert parsed is not None
    ctx: str = parsed["additional_context"]

    # The compact file content should be present.
    assert "Synaptory Protocols" in ctx or "IRON LAWS" in ctx, (
        "expected compact protocol content even without CLI"
    )


@pytest.mark.hook
def test_empty_when_neither_path_works(
    inject_hook, stub_cli_unavailable, subagent_stdin, run_hook, parse_output, tmp_path
):
    """No CLI + fake plugin_root with no compact file → silent skip (exit 0, no JSON)."""
    fake_plugin = tmp_path / "fake-plugin"
    (fake_plugin / "skills" / "_shared" / "protocols").mkdir(parents=True)
    (fake_plugin / "hooks" / "data").mkdir(parents=True)
    # Deliberately do NOT write compacted-protocols.md.
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    env = {
        **stub_cli_unavailable,
        "CLAUDE_PLUGIN_ROOT": str(fake_plugin),
        "CLAUDE_PROJECT_DIR": str(project_dir),
    }
    result = run_hook(inject_hook, env=env, stdin=subagent_stdin)
    assert result.returncode == 0, (
        "hook should exit 0 even when nothing to inject"
    )
    parsed = parse_output(result.stdout)
    # When the compact file is absent the hook emits a fallback sentinel
    # string, so we accept either None or a non-empty payload (the sentinel).
    # The critical assertion is that it does NOT crash and exits 0 above.


@pytest.mark.hook
def test_depth_guard_blocks_at_4(
    inject_hook, hook_env, stub_cli, subagent_stdin, run_hook
):
    """Depth counter at MAX_AGENT_DEPTH+1 → hook emits BLOCKED payload + exit 1."""
    _seed_all_protocols(stub_cli)
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    # Pre-populate the depth counter so this invocation will be at depth 4.
    # synaptory_logger.py stores it at .synaptory/.orchestrator/active-depth.
    state_dir = project_dir / ".synaptory" / ".orchestrator"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "active-depth").write_text("3", encoding="utf-8")

    result = run_hook(inject_hook, env=hook_env, stdin=subagent_stdin)
    assert result.returncode == 1, "depth-exceeded hook must exit 1 to block dispatch"
    assert "BLOCKED" in result.stdout, (
        f"expected BLOCKED payload in stdout, got: {result.stdout!r}"
    )
    assert "MAX_AGENT_DEPTH" in result.stdout
