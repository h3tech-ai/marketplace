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
  * #501 — the envelope carries THIS STORY's DoD tier and active checks, or
    names the reason it could not resolve them. These tests run the real hook
    against a real board and read what lands in `additionalContext`, because
    the defect #501 records was invisible to a renderer unit test: the
    renderer could always render the stanza, and no caller ever asked it to.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def inject_hook(plugin_root: Path) -> Path:
    return plugin_root / "hooks" / "synaptory-inject-protocols.sh"


def _write_board(hook_env: dict, stories: list[dict], **top) -> Path:
    """Write a pipeline board the hook's resolvers will read.

    `hook_env["CLAUDE_PROJECT_DIR"]` is the project the hook is told about, so
    this is the same file `story_pipeline._read_state` opens at dispatch.
    """
    project = Path(hook_env["CLAUDE_PROJECT_DIR"])
    orch = project / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True, exist_ok=True)
    state = {"current_stories": stories}
    state.update(top)
    path = orch / "pipeline-state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


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


# ─── #501: the graded contract must be the shown contract ────────────────────


@pytest.mark.hook
def test_dispatch_carries_this_storys_tier_and_active_checks(
    inject_hook, hook_env, stub_cli, run_hook, parse_output
):
    """The REAL hook must put THIS story's tier and active checks in the prompt.

    What this asserts is impossible: an SE dispatch reaching the agent without
    the DoD contract it will be graded against. `render_envelope` could always
    render that stanza; the hook never asked it to, because the parameters it
    offered (`--active-checks`, `--tier`) required a per-story computation the
    hook had no story to perform it on. So this reads the hook's OUTPUT, not
    the renderer's capability.

    The board is deliberately non-default in three ways, so a fallback to a
    static tier list cannot pass: `mature` tier (adds `coverage_no_decrease`),
    a UI-bearing story (promotes `ui_acceptance`, which is in no tier list),
    and a `planned` tier_source.
    """
    _write_board(
        hook_env,
        [
            {
                "id": "US-042",
                "title": "Checkout screen",
                "state": "in_progress",
                "ui_bearing": True,
            }
        ],
        dod_tier={"tier": "mature", "decided_by": "po", "decided_at": "2026-01-01"},
    )

    result = run_hook(
        inject_hook, env=hook_env, stdin=_stdin("synaptory:software-engineer")
    )
    assert result.returncode == 0, f"hook failed: {result.stderr}"
    ctx: str = parse_output(result.stdout)["additional_context"]

    assert "Definition of Done for this story" in ctx, (
        "the DoD stanza never reached the dispatch: " + ctx[-600:]
    )
    assert "Tier `mature` (planned)" in ctx, (
        "the story's tier and tier_source did not reach the agent; a computed "
        "source is the silent-promotion tell and the agent cannot see it"
    )
    # `coverage_no_decrease` is the mature tier; `ui_acceptance` is a
    # PROMOTED gate that appears in no tier list at all, so its presence is
    # what proves the per-story computation ran rather than a static lookup.
    assert "coverage_no_decrease" in ctx
    assert "ui_acceptance" in ctx, (
        "the promoted UI gate did not reach the agent, so the agent is graded "
        "on a check it was never shown"
    )


@pytest.mark.hook
def test_unresolvable_story_is_named_rather_than_omitted(
    inject_hook, hook_env, stub_cli, run_hook, parse_output
):
    """Two stories hold SE at once → say so; never present a shorter contract.

    What this asserts is impossible: the envelope silently omitting the DoD
    stanza when it could not tell which story the dispatch is for. Omission
    and "the base tier is all that applies" are the same text, and one of them
    is a much lighter contract than the truth. The resolver already refuses to
    guess here (`?ambiguous`, #396); the envelope must carry that refusal
    forward instead of rendering nothing.
    """
    # BOTH in `in_progress`, which is the state SE holds. Two stories in
    # DIFFERENT states are held by different roles and resolve cleanly, so
    # they are not this scenario: the ambiguity is same-role overlap.
    _write_board(
        hook_env,
        [
            {"id": "US-042", "title": "One", "state": "in_progress"},
            {"id": "US-043", "title": "Two", "state": "in_progress"},
        ],
    )

    result = run_hook(
        inject_hook, env=hook_env, stdin=_stdin("synaptory:software-engineer")
    )
    assert result.returncode == 0, f"hook failed: {result.stderr}"
    ctx: str = parse_output(result.stdout)["additional_context"]

    assert "Definition of Done for this story" in ctx
    assert "NOT RESOLVED" in ctx, (
        "an unresolvable story must be reported, not omitted: " + ctx[-600:]
    )
    assert "two stories hold this role at once" in ctx, (
        "the reason must name the ambiguity, so the agent knows the absence "
        "is unmeasured rather than empty"
    )
    assert "Graded on:" not in ctx, (
        "no check list may be presented when the story is unknown — a list "
        "for the wrong story is worse than no list"
    )


@pytest.mark.hook
def test_dod_stanza_is_never_silently_absent(
    inject_hook, hook_env, stub_cli, run_hook, parse_output
):
    """An empty board still gets the stanza, with its reason.

    The zero rule at the prompt level: "no checks were resolved" and "the
    checks are the base tier" must not share a representation. With no board
    at all the honest answer is the first, and it has to be said out loud.
    """
    result = run_hook(
        inject_hook, env=hook_env, stdin=_stdin("synaptory:quality-engineer")
    )
    assert result.returncode == 0, f"hook failed: {result.stderr}"
    ctx: str = parse_output(result.stdout)["additional_context"]

    assert "Definition of Done for this story" in ctx
    assert "NOT RESOLVED" in ctx
    assert "no dispatch binding and no single story in flight" in ctx
