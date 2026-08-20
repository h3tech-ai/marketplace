"""Layer 2 — `synaptory-load-rules.sh` contract tests.

Hypothesis: the SessionStart hook fetches the 5 always-on rules from
the CP via `synaptory skills get rules/<name>`, falls back to on-disk
`plugin/rules/*.md` when the CLI is absent, and assembles them into a
`additional_context` JSON payload preserving declared order.

Background: commit 7b9dac8 (the ADR-016 rules-via-CP rewrite) replaced
the plaintext `cat plugin/rules/*.md` loop with a CP-first fetch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Order matters — these shape the always-on session prompt.
RULE_NAMES = [
    "synaptory-ux",
    "synaptory-visual-identity",
    "synaptory-conflict-resolution",
    "synaptory-boundary-safety",
    "synaptory-welcome",
]


@pytest.fixture
def rules_hook(plugin_root: Path) -> Path:
    return plugin_root / "hooks" / "synaptory-load-rules.sh"


def _seed_all_rules(stub_cli) -> None:
    for name in RULE_NAMES:
        body = f"# rules/{name} stub body\n\nrule-marker-{name}\n"
        stub_cli.add_body(f"rules/{name}", body)


@pytest.mark.hook
def test_cp_path_returns_all_5_rules_in_order(
    rules_hook, hook_env, stub_cli, run_hook, parse_output
):
    """All 5 rules land in additional_context, in declared order."""
    _seed_all_rules(stub_cli)
    # SessionStart reads no stdin; pass empty string.
    result = run_hook(rules_hook, env=hook_env, stdin="")
    assert result.returncode == 0, f"hook failed: {result.stderr}"

    parsed = parse_output(result.stdout)
    assert parsed is not None
    ctx: str = parsed["additional_context"]

    for name in RULE_NAMES:
        assert f"rule-marker-{name}" in ctx, f"missing rule body for {name}"

    positions = [ctx.find(f"rule-marker-{n}") for n in RULE_NAMES]
    assert positions == sorted(positions), (
        f"declared rule order not preserved; positions={positions}"
    )
    # 5 watermarks (one per rule fetched from CP).
    assert ctx.count("<!-- synaptory-id:") == 5


@pytest.mark.hook
def test_disk_fallback_when_cli_missing(
    rules_hook, plugin_root, stub_cli_unavailable, run_hook, parse_output, tmp_path
):
    """No CLI on PATH → bodies come from on-disk plugin/rules/*.md."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    env = {
        **stub_cli_unavailable,
        "CLAUDE_PLUGIN_ROOT": str(plugin_root),
        "CLAUDE_PROJECT_DIR": str(project_dir),
    }
    result = run_hook(rules_hook, env=env, stdin="")
    assert result.returncode == 0
    parsed = parse_output(result.stdout)
    assert parsed is not None
    ctx = parsed["additional_context"]
    assert "<!-- synaptory-id:" not in ctx, "disk fallback shouldn't carry CP watermarks"
    # Real rule bodies have specific content; spot-check one.
    assert "UX Protocol" in ctx or "Interaction Rules" in ctx, (
        "expected disk fallback to load real rule bodies"
    )


@pytest.mark.hook
def test_session_start_hook_reads_no_stdin(
    rules_hook, hook_env, stub_cli, run_hook, parse_output
):
    """SessionStart hooks don't receive a stdin payload (unlike SubagentStart).

    Firing with `</dev/null` (empty string) must succeed — the hook
    must not block on read or barf on missing input.
    """
    _seed_all_rules(stub_cli)
    result = run_hook(rules_hook, env=hook_env, stdin="")
    assert result.returncode == 0
    parsed = parse_output(result.stdout)
    assert parsed is not None and parsed.get("additional_context")
