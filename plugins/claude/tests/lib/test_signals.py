"""Layer 1 — cross-loop signals store (#179 E3).

Hypothesis: signals are an append-only JSONL store under
`.synaptory/signals/` that any loop can write (`write_signal`) and read
(`read_signals`, most-recent-first), and `harvest_pipeline_signals` distils
the board + event log into the four signal kinds (recurring_failure,
dod_gate_hotspot, blocked_story, replay_mismatch) without ever raising —
signal loss must not break the pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from signals import harvest_pipeline_signals, read_signals, write_signal


@pytest.mark.unit
def test_write_and_read_roundtrip(tmp_path: Path):
    write_signal(tmp_path, "recurring_failure", "first", {"n": 1}, "test")
    write_signal(tmp_path, "dod_gate_hotspot", "second")

    out = read_signals(tmp_path)
    assert [r["summary"] for r in out] == ["second", "first"]  # newest first
    assert out[1]["data"] == {"n": 1}
    assert out[1]["source"] == "test"
    # store + schema README materialize on first write
    assert (tmp_path / ".synaptory" / "signals" / "signals.jsonl").exists()
    assert (tmp_path / ".synaptory" / "signals" / "README.md").exists()


@pytest.mark.unit
def test_read_filters_and_limits(tmp_path: Path):
    for i in range(5):
        write_signal(tmp_path, "blocked_story", f"b{i}")
    write_signal(tmp_path, "recurring_failure", "r0")

    assert len(read_signals(tmp_path, kinds=["blocked_story"])) == 5
    assert [r["summary"] for r in read_signals(tmp_path, limit=2)] == ["r0", "b4"]
    assert read_signals(tmp_path / "nowhere") == []  # missing store → no signals


@pytest.mark.unit
def test_harvest_recurring_failures_and_blocked(tmp_path: Path):
    """The BEA5-F1 hash ledger (same hash 2+) and blocked stories become
    signals; a single-occurrence hash does not."""
    state = {"current_stories": [
        {"id": "US-1", "state": "blocked",
         "blocked_reason": "qe ladder exhausted",
         "retry_failure_hashes": {"qe": ["aaaa", "aaaa", "bbbb"]}},
        {"id": "US-2", "state": "done",
         "retry_failure_hashes": {"se": ["cccc"]}},
    ]}
    written = harvest_pipeline_signals(tmp_path, state=state, events=[])

    kinds = sorted(r["kind"] for r in written)
    assert kinds == ["blocked_story", "recurring_failure"]
    rec = next(r for r in written if r["kind"] == "recurring_failure")
    assert rec["data"] == {"story_id": "US-1", "role": "qe",
                           "reason_hash": "aaaa", "count": 2}
    blk = next(r for r in written if r["kind"] == "blocked_story")
    assert blk["data"]["blocked_reason"] == "qe ladder exhausted"


@pytest.mark.unit
def test_harvest_gate_hotspots_and_replay_mismatches(tmp_path: Path):
    """A DoD check failing on 2+ stories is a hotspot (systemic, not
    story-local); every replay mismatch is a signal."""
    events = [
        {"event": "dod_evaluated", "story_id": "US-1",
         "failed_required": ["tests_pass"]},
        {"event": "dod_evaluated", "story_id": "US-2",
         "failed_required": ["tests_pass", "ui_acceptance"]},
        {"event": "dod_evaluated", "story_id": "US-3", "failed_required": []},
        {"event": "evidence_replay_mismatch", "story_id": "US-2",
         "check": "tests_pass", "command": "pytest -q",
         "attested_exit_code": 0, "replayed_exit_code": 1},
    ]
    written = harvest_pipeline_signals(tmp_path, state={}, events=events)

    hotspots = [r for r in written if r["kind"] == "dod_gate_hotspot"]
    assert len(hotspots) == 1  # ui_acceptance failed on only one story
    assert hotspots[0]["data"] == {"check": "tests_pass",
                                   "story_ids": ["US-1", "US-2"]}
    (mismatch,) = [r for r in written if r["kind"] == "replay_mismatch"]
    assert "pytest -q" in mismatch["summary"]


@pytest.mark.unit
def test_harvest_is_idempotent(tmp_path: Path):
    """Re-running harvest over the same (lifetime) event log must NOT
    re-append signals — the P2 finding: repeated retros kept stale failures
    permanently fresh and crowded out newer signals."""
    state = {"current_stories": [
        {"id": "US-1", "state": "blocked", "blocked_reason": "stuck",
         "retry_failure_hashes": {"qe": ["aaaa", "aaaa"]}},
    ]}
    events = [
        {"event": "dod_evaluated", "story_id": "US-1", "failed_required": ["tests_pass"]},
        {"event": "dod_evaluated", "story_id": "US-2", "failed_required": ["tests_pass"]},
        {"event": "evidence_replay_mismatch", "story_id": "US-1",
         "check": "tests_pass", "command": "pytest -q"},
    ]

    first = harvest_pipeline_signals(tmp_path, state=state, events=events)
    assert len(first) == 4  # recurring + blocked + hotspot + replay_mismatch
    count_after_first = len(read_signals(tmp_path, limit=10_000))

    second = harvest_pipeline_signals(tmp_path, state=state, events=events)
    assert second == []  # nothing new
    assert len(read_signals(tmp_path, limit=10_000)) == count_after_first


@pytest.mark.unit
def test_harvest_recurring_key_ignores_count(tmp_path: Path):
    """A persisting failure whose retry count grows must stay ONE signal
    (the dedup key excludes the volatile count), not re-fire each retro."""
    s1 = {"current_stories": [{"id": "US-1", "state": "testing",
          "retry_failure_hashes": {"qe": ["aaaa", "aaaa"]}}]}
    s2 = {"current_stories": [{"id": "US-1", "state": "testing",
          "retry_failure_hashes": {"qe": ["aaaa", "aaaa", "aaaa"]}}]}
    assert len(harvest_pipeline_signals(tmp_path, state=s1, events=[])) == 1
    assert harvest_pipeline_signals(tmp_path, state=s2, events=[]) == []


@pytest.mark.unit
def test_harvest_reads_project_files(tmp_path: Path):
    """Without injected state/events, harvest reads pipeline-state.json and
    events.jsonl from the project — the retro ceremony's invocation shape."""
    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "pipeline-state.json").write_text(json.dumps({
        "current_stories": [{"id": "US-9", "state": "blocked",
                             "blocked_reason": "stuck"}],
    }))
    (orch / "events.jsonl").write_text(
        json.dumps({"event": "dod_evaluated", "story_id": "US-9",
                    "failed_required": ["build_succeeds"]}) + "\n"
        + "not-json\n"  # corrupt line must not break the harvest
    )
    written = harvest_pipeline_signals(tmp_path)
    assert [r["kind"] for r in written] == ["blocked_story"]
    assert read_signals(tmp_path)[0]["data"]["story_id"] == "US-9"
