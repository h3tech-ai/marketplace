"""Layer 1 — `plugin-claude/hooks/lib/hook_io.py` output-contract helper.

Pins the camelCase contract and the 10 KB additionalContext cap. The cap
is the reason this module exists (SubagentStart injection can overflow the
harness limit), so its byte-exactness is the contract.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

import hook_io

_CAP = 10 * 1024


def _emit(**kw) -> dict:
    buf = io.StringIO()
    with redirect_stdout(buf):
        hook_io.emit(**kw)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    return json.loads(lines[-1]) if lines else {}


@pytest.mark.unit
def test_camelcase_additional_context():
    out = _emit(event_name="SessionStart", additional_context="hello")
    assert out["additionalContext"] == "hello"


@pytest.mark.unit
def test_under_cap_passes_through_untouched():
    body = "x" * 100
    out = _emit(event_name="SessionStart", additional_context=body)
    assert out["additionalContext"] == body


@pytest.mark.unit
def test_oversized_additional_context_capped_including_marker():
    """Regression (PR #83 re-review): the final string — marker included —
    must not exceed the 10 KB cap. The old code cycled to the cap and THEN
    appended the marker, overshooting to 10,294 bytes."""
    out = _emit(event_name="SessionStart", additional_context="x" * (11 * 1024))
    encoded = out["additionalContext"].encode("utf-8")
    assert len(encoded) <= _CAP, f"exceeds cap: {len(encoded)} bytes"
    assert "truncated to 10 KB cap" in out["additionalContext"]


@pytest.mark.unit
def test_cap_boundary_exact():
    """A payload exactly at the cap is not truncated."""
    out = _emit(event_name="SessionStart", additional_context="x" * _CAP)
    assert "truncated" not in out["additionalContext"]
    assert len(out["additionalContext"].encode("utf-8")) == _CAP


# ─── #501: protected_context survives the cap whole ──────────────────────────


@pytest.mark.unit
def test_protected_context_survives_an_overflowing_head():
    """What this asserts is impossible: the cap silently eating the contract.

    The cap is a blind head-truncation, so whatever is appended LAST is what
    disappears — and the SubagentStart hook appended the Execution Envelope
    last. Reserving the protected bytes moves the trim onto the reference
    material, and announces it where it happens.
    """
    envelope = "ENVELOPE-START " + ("e" * 2000) + " ENVELOPE-END"
    out = _emit(
        event_name="SubagentStart",
        additional_context="p" * (11 * 1024),
        protected_context=envelope,
    )
    ctx = out["additionalContext"]

    assert len(ctx.encode("utf-8")) <= _CAP, "reservation must respect the cap"
    assert envelope in ctx, "the protected block lost bytes to the cap"
    assert ctx.endswith(envelope), "the protected block must be last and whole"
    assert "protocol index truncated" in ctx, (
        "a trim that is not announced reads as a shorter contract, not a "
        "truncated one"
    )


@pytest.mark.unit
def test_protected_context_is_not_announced_when_nothing_is_trimmed():
    out = _emit(
        event_name="SubagentStart",
        additional_context="p" * 100,
        protected_context="ENVELOPE",
    )
    assert out["additionalContext"] == "p" * 100 + "ENVELOPE"
    assert "truncated" not in out["additionalContext"]


@pytest.mark.unit
def test_protected_context_larger_than_the_cap_drops_the_head_not_its_own_start():
    """No budget exists, so spend the cap on the contract rather than on the
    reference material plus the contract's first few lines."""
    out = _emit(
        event_name="SubagentStart",
        additional_context="PROTOCOLS" * 100,
        protected_context="E" * (11 * 1024),
    )
    ctx = out["additionalContext"]
    assert len(ctx.encode("utf-8")) <= _CAP
    assert "PROTOCOLS" not in ctx
    assert ctx.startswith("E")


@pytest.mark.unit
def test_pretooluse_permission_decision_nested():
    out = _emit(
        event_name="PreToolUse",
        permission_decision="deny",
        permission_decision_reason="nope",
    )
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"] == "nope"
