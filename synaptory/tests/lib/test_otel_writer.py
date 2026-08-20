"""Layer 1 — ``plugin/hooks/lib/otel_writer.py`` JSONL writer tests.

Pure-logic tests for the OTLP span-event writer. The bash hooks that
call this are exercised separately.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hooks.lib.otel_writer import (
    append_event,
    build_end,
    build_start,
    derive_span_id,
    derive_trace_id,
    jsonl_path,
)


@pytest.mark.unit
def test_derive_span_id_is_deterministic_and_32_hex():
    a = derive_span_id("acd2b6501423b3392")
    b = derive_span_id("acd2b6501423b3392")
    assert a == b
    assert len(a) == 32
    assert all(c in "0123456789abcdef" for c in a)


@pytest.mark.unit
def test_derive_span_id_distinct_inputs_distinct_outputs():
    assert derive_span_id("agent-A") != derive_span_id("agent-B")


@pytest.mark.unit
def test_derive_span_id_empty_yields_random_id():
    """Empty agent_id falls back to UUIDv4 — two calls should differ."""
    a = derive_span_id("")
    b = derive_span_id("")
    assert a != b
    assert len(a) == 32 and len(b) == 32


@pytest.mark.unit
def test_derive_trace_id_is_session_scoped():
    """Every span in a session shares a trace id; different sessions differ."""
    same_a = derive_trace_id("sess-1")
    same_b = derive_trace_id("sess-1")
    other = derive_trace_id("sess-2")
    assert same_a == same_b
    assert same_a != other
    assert len(same_a) == 32


@pytest.mark.unit
def test_build_start_records_required_fields():
    evt = build_start(
        span_id=None,
        trace_id=None,
        role="software-engineer",
        backend="claude",
        model="sonnet",
        story_id="US-1",
    )
    assert evt["event"] == "start"
    assert evt["role"] == "software-engineer"
    assert evt["backend"] == "claude"
    assert evt["model"] == "sonnet"
    assert evt["story_id"] == "US-1"
    assert isinstance(evt["start_ns"], int) and evt["start_ns"] > 0
    assert len(evt["span_id"]) == 32
    assert len(evt["trace_id"]) == 32


@pytest.mark.unit
def test_build_end_carries_status_and_message():
    evt = build_end(span_id="abc", status="error", message="receipt_missing")
    assert evt["event"] == "end"
    assert evt["span_id"] == "abc"
    assert evt["status"] == "error"
    assert evt["message"] == "receipt_missing"
    assert isinstance(evt["end_ns"], int)


@pytest.mark.unit
def test_build_end_ok_drops_message_field():
    evt = build_end(span_id="abc", status="ok")
    assert "message" not in evt


@pytest.mark.unit
def test_append_event_round_trips_through_jsonl(tmp_path: Path):
    start = build_start(
        span_id=None, trace_id=None, role="qe",
        backend="claude", model="sonnet", story_id="",
    )
    end = build_end(span_id=start["span_id"], status="ok")
    p = append_event(tmp_path, "session-X", start)
    append_event(tmp_path, "session-X", end)
    assert p == jsonl_path(tmp_path, "session-X")
    assert p.exists()
    lines = p.read_text().strip().split("\n")
    assert len(lines) == 2
    parsed = [json.loads(l) for l in lines]
    assert parsed[0]["event"] == "start" and parsed[1]["event"] == "end"
    assert parsed[0]["span_id"] == parsed[1]["span_id"]


@pytest.mark.unit
def test_jsonl_path_falls_back_for_missing_session(tmp_path: Path):
    p = jsonl_path(tmp_path, "")
    assert p.name == "spans-no-session.jsonl"
