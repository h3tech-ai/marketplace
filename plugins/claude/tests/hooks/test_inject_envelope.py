"""Layer 2 — `synaptory-inject-protocols.sh` Execution Envelope injection (#163).

After the compact protocol payload (and any project risk checklist), the hook
renders the role-scoped Execution Envelope via
`hooks/lib/evidence_contract.py envelope --agent-type <type>` and appends it
to additionalContext. The envelope tells every synaptory delivery agent the
exact machine-checked contract SubagentStop enforces: receipt path + required
fields, the two verification_commands forms (proof vs replay instruction),
and the replay rules.

Contract pinned here:
  * A `synaptory:*` agent_type → the envelope block IS in additionalContext,
    with role-specific literals (receipt filename abbrev, fixed stage).
  * A non-synaptory agent_type → NO envelope (evidence_contract renders "").
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def inject_hook(plugin_root: Path) -> Path:
    return plugin_root / "hooks" / "synaptory-inject-protocols.sh"


def _stdin(agent_type: str) -> str:
    """SubagentStart JSON payload with a controllable agent_type."""
    return json.dumps(
        {
            "agent_id": "test-agent-envelope-001",
            "agent_type": agent_type,
            "session_id": "test-session-001",
            "transcript_path": "/tmp/transcript.jsonl",
            "cwd": "/tmp",
            "hook_event_name": "SubagentStart",
        }
    )


@pytest.mark.hook
def test_synaptory_agent_gets_execution_envelope(
    inject_hook, hook_env, stub_cli, run_hook, parse_output
):
    """synaptory:software-engineer → envelope appended to additionalContext."""
    result = run_hook(
        inject_hook, env=hook_env, stdin=_stdin("synaptory:software-engineer")
    )
    assert result.returncode == 0, f"hook failed: {result.stderr}"

    parsed = parse_output(result.stdout)
    assert parsed is not None, "expected non-empty additionalContext"
    ctx: str = parsed["additional_context"]

    assert "Execution Envelope" in ctx, (
        f"envelope block missing from additionalContext: {ctx[:300]!r}"
    )
    # Role-scoped literals: SE receipts are {story_id}-se.json at the fixed
    # stage se-implementation.
    assert "US-042-se.json" in ctx, "SE receipt filename example missing"
    assert "se-implementation" in ctx, "SE fixed stage missing"
    # The two-forms teaching block must be present (proof vs replay).
    assert "verification_commands" in ctx


@pytest.mark.hook
def test_non_synaptory_agent_gets_no_envelope(
    inject_hook, hook_env, stub_cli, run_hook, parse_output
):
    """general-purpose (non-synaptory) → protocols still injected, NO envelope."""
    result = run_hook(inject_hook, env=hook_env, stdin=_stdin("general-purpose"))
    assert result.returncode == 0, f"hook failed: {result.stderr}"

    parsed = parse_output(result.stdout)
    assert parsed is not None
    ctx: str = parsed["additional_context"]

    assert "Execution Envelope" not in ctx, (
        "non-synaptory subagents must not receive the evidence-contract envelope"
    )
