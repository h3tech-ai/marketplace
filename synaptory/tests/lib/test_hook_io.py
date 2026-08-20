"""Layer 1 — `plugin/hooks/lib/hook_io.py` output-contract helper.

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
    must not exceed the 10 KB cap. The old code sliced to the cap and THEN
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
