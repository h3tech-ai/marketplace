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


# ── #304: deps_blocked must stop the loop ──────────────────────────────────


def test_deps_blocked_is_a_declared_stop_action():
    import loop_engine as le

    assert "deps_blocked" in le.STOP_ACTIONS
    assert "deps_blocked" not in le.CONTINUE_ELIGIBLE


def test_continuation_reason_names_deps_blocked():
    """The reason string is what an operator reads to know why the session is
    still running; omitting the new terminal action makes it misleading."""
    import inspect

    import loop_engine as le

    assert "deps_blocked" in inspect.getsource(le)


# ── #791: the loop's AUDIENCE — only the session that holds the Cycle ────────
#
# Before this, `should_continue` instructed whatever session the Stop hook
# fired in, which is a property of where a terminal is pointed. On
# `ptb-assistant` that told a coordination-only session to dispatch WU-1101
# for a Cycle held by a different session, against a sealed declaration that
# names exactly one Engineering Lead — and then refused to let it stop.


def test_unclaimed_cycle_still_continues(tmp_path: Path):
    """No claim → the single-session default is untouched. The loop must not
    go mute on every project that never dispatched through the new hook."""
    out = _cont(_project(tmp_path), session_id="s-only")
    assert out["continue"] is True


def test_the_holder_is_driven(tmp_path: Path):
    import cycle_holder
    import loop_engine as le

    p = _project(tmp_path)
    cycle_holder.claim(str(p), le.cycle_scope(str(p)), "s-delivery")
    assert _cont(p, session_id="s-delivery")["continue"] is True


def test_a_session_that_does_not_hold_the_cycle_is_not_instructed(tmp_path: Path):
    """THE REGRESSION. The coordination session dispatched nothing, so it
    holds nothing, so the loop has nothing to say to it."""
    import cycle_holder
    import loop_engine as le

    p = _project(tmp_path)
    cycle_holder.claim(str(p), le.cycle_scope(str(p)), "s-delivery")

    out = _cont(p, session_id="s-orchestration")
    assert out["continue"] is False
    assert out["reason"] is None
    assert out["stop_reason"] == "held_by_other_session"
    assert out["holder_session_id"] == "s-delivery"


def test_the_mismatch_leaves_a_breadcrumb_naming_the_holder(tmp_path: Path):
    """Silence in the wrong session, but never silence in the record: a
    coordinator's operator must be able to see whose Cycle it was."""
    import cycle_holder
    import loop_engine as le

    p = _project(tmp_path)
    cycle_holder.claim(str(p), le.cycle_scope(str(p)), "s-delivery")
    _cont(p, session_id="s-orchestration")

    events = (p / ".synaptory" / ".orchestrator" / "events.jsonl").read_text(
        encoding="utf-8"
    )
    record = [json.loads(line) for line in events.splitlines() if line.strip()]
    mismatch = [e for e in record if e.get("event") == "loop_holder_mismatch"]
    assert mismatch, record
    assert mismatch[-1]["holder_session_id"] == "s-delivery"
    assert mismatch[-1]["session_id"] == "s-orchestration"


def test_the_muted_session_does_not_consume_the_runaway_guard(tmp_path: Path):
    """The guard file is one per project keyed on session id, so two sessions
    reaching it alternately reset each other's counters and the runaway cap
    never trips. The audience check runs first, which is why it cannot."""
    import cycle_holder
    import loop_engine as le

    p = _project(tmp_path)
    cycle_holder.claim(str(p), le.cycle_scope(str(p)), "s-delivery")
    _cont(p, session_id="s-delivery")
    before = json.loads(
        (p / ".synaptory" / ".orchestrator" / "loop-guard.json").read_text("utf-8")
    )
    _cont(p, session_id="s-orchestration")
    after = json.loads(
        (p / ".synaptory" / ".orchestrator" / "loop-guard.json").read_text("utf-8")
    )
    assert after == before
    assert after["session_id"] == "s-delivery"


def test_a_stale_claim_stops_muting(tmp_path: Path):
    """A crashed holder must not mute its project forever. Expiry only ever
    PERMITS continuation; it takes nothing from a session still working."""
    import cycle_holder
    import loop_engine as le

    p = _project(tmp_path, yaml_extra="resilience:\n  cycle_holder_ttl_minutes: 1\n")
    from datetime import datetime, timedelta, timezone

    long_ago = datetime.now(timezone.utc) - timedelta(minutes=30)
    cycle_holder.claim(str(p), le.cycle_scope(str(p)), "s-dead", now=long_ago)
    assert _cont(p, session_id="s-fresh")["continue"] is True


def test_holder_ttl_is_configurable(tmp_path: Path):
    import loop_engine as le

    p = _project(tmp_path, yaml_extra="resilience:\n  cycle_holder_ttl_minutes: 5\n")
    assert le._holder_ttl_seconds(str(p)) == 300
    assert le._holder_ttl_seconds(str(_project(tmp_path / "plain"))) == 7200


def test_a_dispatch_reclaims_after_a_restart(tmp_path: Path):
    """The restarted Engineering Lead has a new session id and is muted until
    it acts — and its first dispatch is the act that reclaims."""
    import cycle_holder
    import loop_engine as le

    p = _project(tmp_path)
    cycle_holder.claim(str(p), le.cycle_scope(str(p)), "s-before")
    assert _cont(p, session_id="s-after")["continue"] is False

    le.claim_dispatch(str(p), "s-after")
    assert _cont(p, session_id="s-after")["continue"] is True


def test_claim_dispatch_and_should_continue_agree_on_the_scope(tmp_path: Path):
    """The claim written at dispatch and the claim read at Stop must be one
    key. Two derivations of it is how this fix would silently do nothing."""
    import cycle_holder
    import loop_engine as le

    p = _project(tmp_path)
    le.claim_dispatch(str(p), "s-delivery")
    assert le.cycle_scope(str(p)) in cycle_holder.read_holders(str(p))
    assert _cont(p, session_id="s-elsewhere")["stop_reason"] == "held_by_other_session"


# ── #791 on the lifecycle that reported it: SPQ, a real opened Cycle ─────────


def _spq_project(tmp_path: Path) -> Path:
    import _spq_fixture

    project, _ = _spq_fixture.open_project(tmp_path / "spq")
    orch = project / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True, exist_ok=True)
    (orch / "settings.md").write_text("Engagement: structured\n", encoding="utf-8")
    return project


def test_spq_scope_is_the_cycle(tmp_path: Path):
    import loop_engine as le

    scope = le.cycle_scope(str(_spq_project(tmp_path)))
    assert scope.startswith("cycle:") and scope != "cycle:unresolved"


def test_spq_coordination_session_is_not_told_to_dispatch(tmp_path: Path):
    """The reported failure, end to end: the delivery session holds the Cycle
    and is driven; the coordination session sharing the directory is not."""
    import loop_engine as le

    p = _spq_project(tmp_path)
    lead = _cont(p, session_id="s-delivery")
    assert lead["continue"] is True, lead
    assert "dispatch_" in lead["reason"]

    le.claim_dispatch(str(p), "s-delivery")

    out = _cont(p, session_id="s-orchestration")
    assert out["continue"] is False
    assert out["stop_reason"] == "held_by_other_session"
    assert _cont(p, session_id="s-delivery")["continue"] is True


def test_an_advisory_dispatch_does_not_move_the_claim(tmp_path: Path):
    """The coordination session in #791 must not be able to take the Cycle by
    asking a question. Only the roles the loop instructs move the claim."""
    import cycle_holder
    import loop_engine as le

    p = _project(tmp_path)
    le.claim_dispatch(str(p), "s-delivery", "software-engineer")
    out = le.claim_dispatch(str(p), "s-orchestration", "research-advisor")
    assert out["claimed"] is False

    holder = cycle_holder.read_holders(str(p))[le.cycle_scope(str(p))]
    assert holder["session_id"] == "s-delivery"
    assert _cont(p, session_id="s-orchestration")["stop_reason"] == "held_by_other_session"
