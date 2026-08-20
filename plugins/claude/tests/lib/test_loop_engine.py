"""Layer 1 — `loop_engine.should_continue` (epic #75 P2).

The Stop-hook loop engine decides whether to refuse a session stop. These
tests pin every hard-stop condition (the governance guarantee: never continue
past a human gate), the runaway-guard transitions, corrupt-state fail-safe,
and multi-spec routing — all against fixture project dirs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import loop_engine


def _project(
    tmp_path: Path,
    *,
    engagement: str = "structured",
    lifecycle: str = "SPRINT_EXECUTION",
    stories: list[dict] | None = None,
    build_mode: str = "scrum",
    yaml_extra: str = "",
    multispec: bool = False,
) -> Path:
    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True, exist_ok=True)
    (orch / "settings.md").write_text(f"Engagement: {engagement}\n", encoding="utf-8")
    (tmp_path / ".synaptory.yaml").write_text("project_id: t\n" + yaml_extra, encoding="utf-8")
    sub = {
        "lifecycle_state": lifecycle,
        "current_sprint": 2,
        "cumulative_ticket_number": 2,
        "current_stories": stories if stories is not None else [
            {"id": "US-1", "title": "T", "state": "testing"}
        ],
    }
    if multispec:
        state = {"version": "3.0", "build_mode": build_mode, "active_spec": "be",
                 "specs": {"be": sub}}
    else:
        state = {"version": "2.0", "build_mode": build_mode, **sub}
    (orch / "pipeline-state.json").write_text(json.dumps(state), encoding="utf-8")
    return tmp_path


def _cont(project_dir: Path, session_id: str = "s1", **env) -> dict:
    import os

    old = {k: os.environ.get(k) for k in env}
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        return loop_engine.should_continue(str(project_dir), session_id)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── continue path ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_continues_on_dispatchable_work():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        out = _cont(_project(Path(d)))
    assert out["continue"] is True
    assert "next action: dispatch_qe" in out["reason"]
    assert out["activity"]["kind"] == "loop_continue"


# ── governance hard stops (never continue past these) ────────────────────────


@pytest.mark.unit
def test_stops_in_interactive_mode(tmp_path: Path):
    out = _cont(_project(tmp_path, engagement="interactive"))
    assert out["continue"] is False
    assert out["stop_reason"] == "not_structured"


@pytest.mark.unit
def test_stops_when_env_kill_switch_set(tmp_path: Path):
    out = _cont(_project(tmp_path), SYNAPTORY_LOOP_DISABLE="1")
    assert out["continue"] is False
    assert out["stop_reason"] == "disabled_env"


@pytest.mark.unit
def test_stops_when_config_disabled(tmp_path: Path):
    out = _cont(_project(tmp_path, yaml_extra="resilience:\n  loop_continuation: disabled\n"))
    assert out["continue"] is False
    assert out["stop_reason"] == "disabled_config"


@pytest.mark.unit
@pytest.mark.parametrize("lifecycle", ["SPRINT_REVIEW", "SPRINT_RETRO", "SPRINT_CLOSE", "INCEPTION"])
def test_stops_outside_execution_lifecycle(tmp_path: Path, lifecycle: str):
    out = _cont(_project(tmp_path, lifecycle=lifecycle))
    assert out["continue"] is False
    assert out["stop_reason"] == "action_not_continue_eligible"


@pytest.mark.unit
def test_stops_on_awaiting_acceptance_human_gate(tmp_path: Path):
    out = _cont(_project(tmp_path, stories=[{"id": "US-1", "state": "awaiting_acceptance"}]))
    assert out["continue"] is False
    assert out["stop_reason"] == "action_not_continue_eligible"


@pytest.mark.unit
def test_stops_when_all_stories_done(tmp_path: Path):
    out = _cont(_project(tmp_path, stories=[{"id": "US-1", "state": "done"}]))
    assert out["continue"] is False


@pytest.mark.unit
def test_stops_on_unreadable_state_and_logs_breadcrumb(tmp_path: Path):
    p = _project(tmp_path)
    (p / ".synaptory" / ".orchestrator" / "pipeline-state.json").write_text("{bad json")
    out = _cont(p)
    assert out["continue"] is False
    assert out["stop_reason"] == "state_unreadable"
    events = (p / ".synaptory" / ".orchestrator" / "events.jsonl").read_text()
    assert "loop_engine_state_unreadable" in events


# ── runaway guard ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_guard_trips_after_no_progress_cap(tmp_path: Path):
    p = _project(tmp_path)  # cap defaults to 2
    assert _cont(p, "g")["continue"] is True   # continuation 1, hash set
    assert _cont(p, "g")["continue"] is True   # no progress → count 1
    trip = _cont(p, "g")                        # no progress → count 2 → trip
    assert trip["continue"] is False
    assert trip["stop_reason"] == "guard_tripped"
    assert trip["activity"]["kind"] == "loop_guard_trip"
    guard = json.loads((p / ".synaptory" / ".orchestrator" / "loop-guard.json").read_text())
    assert guard["tripped"] is True
    events = (p / ".synaptory" / ".orchestrator" / "events.jsonl").read_text()
    assert "loop_guard_trip" in events


@pytest.mark.unit
def test_guard_stays_tripped_within_session(tmp_path: Path):
    p = _project(tmp_path)
    for _ in range(3):
        _cont(p, "g")
    # A 4th call on the same session with the guard tripped → still stop.
    out = _cont(p, "g")
    assert out["continue"] is False
    assert out["stop_reason"] == "guard_tripped"


@pytest.mark.unit
def test_guard_resets_on_new_session(tmp_path: Path):
    p = _project(tmp_path)
    for _ in range(3):
        _cont(p, "old")  # trip old session
    out = _cont(p, "fresh")  # new session id → fresh guard → continues
    assert out["continue"] is True


@pytest.mark.unit
def test_progress_resets_no_progress_count(tmp_path: Path):
    p = _project(tmp_path)
    state_path = p / ".synaptory" / ".orchestrator" / "pipeline-state.json"
    assert _cont(p, "g")["continue"] is True   # count 0
    assert _cont(p, "g")["continue"] is True   # count 1
    # Make real progress: advance the story.
    st = json.loads(state_path.read_text())
    st["current_stories"][0]["state"] = "reviewing"
    state_path.write_text(json.dumps(st))
    assert _cont(p, "g")["continue"] is True   # progress → count resets
    # Now it takes two more no-progress stops to trip, not one.
    assert _cont(p, "g")["continue"] is True
    assert _cont(p, "g")["continue"] is False


@pytest.mark.unit
def test_custom_no_progress_cap(tmp_path: Path):
    p = _project(tmp_path, yaml_extra="resilience:\n  loop_no_progress_cap: 1\n")
    assert _cont(p, "g")["continue"] is True   # count 0
    assert _cont(p, "g")["continue"] is False  # count 1 >= cap 1 → trip


# ── multi-spec ───────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_multispec_reads_active_spec(tmp_path: Path, monkeypatch):
    p = _project(
        tmp_path, multispec=True,
        stories=[{"id": "BE-1", "title": "backend", "state": "testing"}],
    )
    monkeypatch.setenv("SYNAPTORY_ACTIVE_SPEC", "be")
    out = _cont(p)
    assert out["continue"] is True
    assert "BE-1" in out["reason"]


@pytest.mark.unit
def test_kanban_execution_continues(tmp_path: Path):
    p = _project(tmp_path, build_mode="kanban", lifecycle="EXECUTION",
                 stories=[{"id": "T-1", "title": "tkt", "state": "in_progress"}])
    out = _cont(p)
    assert out["continue"] is True
    assert "kanban_state_machine.py" in out["reason"]
