"""Layer 1 — the SPQ lifecycle state machine (docs/spq-lifecycle-design.md §4, §10).

Hypothesis: two invariants carry this file, and both are the kind that a
plausible-looking refactor silently breaks.

  1. `CYCLE_EXECUTION → CHECKPOINT` must be UNREACHABLE. Integration precedes
     demonstration (§4.1); a Checkpoint demoing from an un-integrated branch
     shows 1/N of the system and defers the integration debt to whoever finds
     it later.
  2. all-Work-Units-terminal must yield `await_sync`, never `sprint_complete`
     (§10.2). The shared pipeline has no concept of a barrier, so passing its
     verdict through would close the Cycle without integrating.

Plus argument parity: both existing lifecycle wrappers shipped for months
dropping `dod_tier_info` and `parallelism`, which made story parallelism
unreachable on the primary orchestrator path (#192). This file is specified as
a mirror of one of them, so parity is asserted rather than assumed.
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
import pathlib
from pathlib import Path

import pytest

import spq_state_machine as m
import spq_paths as _paths_mod
from story_pipeline import CONTINUE_ELIGIBLE, NEXT_ACTIONS


pytestmark = pytest.mark.unit

LIB = Path(m.__file__).parent


def _project(tmp_path: Path, stories=None, **over) -> Path:
    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True, exist_ok=True)
    state = {
        "version": "2.0",
        "build_mode": "spq",
        "lifecycle_state": "CYCLE_EXECUTION",
        "current_cycle": 1,
        "cycle_goal": "g",
        "cycles_completed": [],
        "sync": None,
        "current_stories": stories if stories is not None else [],
        "lifecycle_history": [
            {"state": "CYCLE_EXECUTION", "entered_at": "2026-01-01T00:00:00Z",
             "exited_at": None}
        ],
    }
    state.update(over)
    (orch / "pipeline-state.json").write_text(json.dumps(state))
    return tmp_path


def _story(sid: str, st: str, **kw) -> dict:
    return {"id": sid, "title": f"WU {sid}", "state": st, "pipeline_log": [], **kw}


def _acceptance_receipts(project: Path, cycle: int = 1, **overrides) -> None:
    receipts = project / ".synaptory" / ".orchestrator" / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    roles = {
        "qe": "quality-engineer",
        "ce": "compliance-engineer",
        "pe": "platform-engineer",
        "tw": "technical-writer",
        "cr": "code-reviewer",
    }
    artifact = project / "release-evidence.txt"
    artifact.write_text("release evidence\n")
    for abbrev, role in roles.items():
        payload = {
            "story_id": f"ACCEPTANCE-{cycle}",
            "role": role,
            "backend": "codex",
            "model": "gpt-5.6-sol",
            "status": "complete",
            "artifacts": [artifact.name],
            "verification_commands": [
                {"command": "pytest -q", "exit_code": 0, "summary": "passed"}
            ],
            "metrics": {
                "tests_failed": 0,
                "findings_critical": 0,
            },
            "completed_at": "2026-08-24T00:00:00Z",
        }
        payload.update(overrides.get(abbrev, {}))
        (receipts / f"ACCEPTANCE-{cycle}-{abbrev}.json").write_text(
            json.dumps(payload)
        )


def _checkpoint_receipt(project: Path, cycle: int) -> Path:
    receipts = project / ".synaptory" / ".orchestrator" / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    artifact = project / f"cycle-{cycle}-report.md"
    artifact.write_text("# Cycle report\n")
    path = receipts / f"CHECKPOINT-{cycle}-tw.json"
    path.write_text(
        json.dumps(
            {
                "story_id": f"CHECKPOINT-{cycle}",
                "role": "technical-writer",
                "backend": "codex",
                "model": "gpt-5.6-sol",
                "artifacts": [artifact.name],
                "verification_commands": [
                    {"command": f"test -s {artifact.name}", "exit_code": 0, "summary": "present"}
                ],
                "metrics": {"reports": 1},
                "completed_at": "2026-08-24T00:00:00Z",
            }
        )
    )
    return path


# ---------------------------------------------------------------------------
# Invariant 1 — integration precedes demonstration
# ---------------------------------------------------------------------------

def test_checkpoint_is_unreachable_from_cycle_execution():
    assert "CHECKPOINT" not in m.SPQ_TRANSITIONS["CYCLE_EXECUTION"]
    assert m.SPQ_TRANSITIONS["CYCLE_EXECUTION"] == ["SYNC"]


def test_skipping_sync_is_rejected_with_the_reason(tmp_path: Path):
    p = _project(tmp_path)
    with pytest.raises(ValueError) as e:
        m.transition(str(p), "CHECKPOINT")
    msg = str(e.value)
    assert "Illegal transition" in msg
    assert "integration must precede demonstration" in msg


def test_sync_will_not_clear_without_a_green_verdict(tmp_path: Path):
    """The state machine mirrors scrum's TW-report guard here: without it an
    orchestrator could walk past the barrier and demo an unintegrated tree."""
    p = _project(tmp_path, lifecycle_state="SYNC", current_cycle=3)
    with pytest.raises(ValueError, match="no green barrier"):
        m.transition(str(p), "CHECKPOINT")


def test_sync_clears_with_a_green_verdict_for_this_cycle(tmp_path: Path):
    p = _project(tmp_path, lifecycle_state="SYNC", current_cycle=3,
                 sync={"cycle": 3, "verdict": "green"})
    out = m.transition(str(p), "CHECKPOINT")
    assert out["lifecycle_state"] == "CHECKPOINT"


def test_a_verdict_for_another_cycle_does_not_count(tmp_path: Path):
    """A stale verdict from Cycle 2 must not open Cycle 3's Checkpoint."""
    p = _project(tmp_path, lifecycle_state="SYNC", current_cycle=3,
                 sync={"cycle": 2, "verdict": "green"})
    with pytest.raises(ValueError, match="no green barrier"):
        m.transition(str(p), "CHECKPOINT")


def test_force_is_the_documented_escape_hatch(tmp_path: Path):
    p = _project(tmp_path, lifecycle_state="SYNC", current_cycle=3)
    out = m.transition(str(p), "CHECKPOINT", force=True)
    assert out["lifecycle_state"] == "CHECKPOINT"


# ---------------------------------------------------------------------------
# Invariant 2 — sprint_complete becomes await_sync
# ---------------------------------------------------------------------------

def test_all_work_units_terminal_yields_await_sync(tmp_path: Path):
    p = _project(tmp_path, stories=[_story("WU-1", "done"), _story("WU-2", "done")])
    out = m.next_action(str(p))
    assert out["action"] == "await_sync", out
    assert out["action"] != "sprint_complete"
    assert out["human_gate_pending"] is True
    assert "barrier" in out["reason"]


def test_await_sync_is_a_stop_action_not_continue_eligible():
    """The barrier's whole value: the loop engine must not drive past it.
    Gate detection is an allowlist, so membership in NEXT_ACTIONS plus absence
    from CONTINUE_ELIGIBLE is exactly the required property."""
    assert "await_sync" in NEXT_ACTIONS
    assert "await_sync" not in CONTINUE_ELIGIBLE


def test_outside_cycle_execution_is_not_dispatchable(tmp_path: Path):
    for state in ("DISCOVERY", "COMMIT", "SYNC", "CHECKPOINT", "ACCEPTANCE"):
        p = _project(tmp_path, lifecycle_state=state,
                     stories=[_story("WU-1", "queued")])
        out = m.next_action(str(p))
        assert out["action"] == "not_in_execution", state
        assert out["action"] in NEXT_ACTIONS


def test_normal_dispatch_still_flows(tmp_path: Path):
    p = _project(tmp_path, stories=[_story("WU-1", "queued")])
    out = m.next_action(str(p))
    assert out["action"] == "dispatch_se"
    assert out["story_id"] == "WU-1"
    assert out["current_cycle"] == 1


# ---------------------------------------------------------------------------
# Argument parity with story_pipeline.next_action (the #192 defect class)
# ---------------------------------------------------------------------------

def test_wrapper_passes_every_toggle():
    """Asserted on the source because the failure mode is an omitted kwarg,
    which no behavioural test catches when the feature is off by default."""
    src = inspect.getsource(m.next_action)
    for kw in ("per_story_acceptance=", "receipts_dir=", "verification_loops=",
               "dod_tier_info=", "parallelism="):
        assert kw in src, f"next_action must pass {kw} (#192)"


def test_every_wrapper_can_actually_call_its_callee():
    """Mechanical arity check across EVERY story_pipeline call in this module.

    The `next_action` test above guards one function by name. #192 and #238
    are both the same defect class — a wrapper that does not pass what its
    callee needs — and naming one function cannot catch the next instance.
    This walks the AST instead, so a new wrapper is covered the moment it is
    written.

    #238 found two live crashes that had shipped: `evaluate_story_dod` omitted
    the required `intensity` (TypeError on every call) and `reject_story`
    passed `ac_changes` as a sixth positional to a function taking five
    positionals then `*`.
    """
    import ast
    import story_pipeline as sp

    src = Path(m.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    alias = {
        a.asname or a.name: a.name
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module == "story_pipeline"
        for a in n.names
    }
    assert alias, "no story_pipeline imports found — has the module been restructured?"

    problems = []
    for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
        for call in ast.walk(fn):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)):
                continue
            if call.func.id not in alias:
                continue
            target = getattr(sp, alias[call.func.id], None)
            if not callable(target):
                continue
            sig = inspect.signature(target)
            params = list(sig.parameters.values())
            positional = [
                p for p in params
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            ]
            required = [p.name for p in positional if p.default is inspect.Parameter.empty]
            given_kw = {k.arg for k in call.keywords if k.arg}

            # Too many positionals — the reject_story defect.
            if len(call.args) > len(positional) and not any(
                p.kind is p.VAR_POSITIONAL for p in params
            ):
                problems.append(
                    f"{fn.name}() passes {len(call.args)} positionals to "
                    f"{alias[call.func.id]}() which accepts {len(positional)}"
                )
                continue
            # Missing a required argument — the evaluate_story_dod defect.
            unmet = [r for r in required[len(call.args):] if r not in given_kw]
            if unmet:
                problems.append(
                    f"{fn.name}() never supplies {unmet} to {alias[call.func.id]}()"
                )
    assert not problems, "wrapper/callee mismatches:\n  " + "\n  ".join(problems)


def test_transition_story_applies_the_dod_gate():
    """#238: the gate, the acceptance redirect, the persist and the ship.

    Asserted on the source because each omission is silent — a bare
    passthrough returns a perfectly valid result while enforcing nothing, and
    that is precisely what shipped. The vacuous barrier invariant in
    sync_barrier.declare_ready followed from the missing persist: it reads
    story["dod"] through aggregate_sprint_dod, so with nothing written
    checks_failed is always empty.
    """
    src = inspect.getsource(m.transition_story)
    # The gate moved into the shared kernel (#277) so scrum, kanban, spq and both
    # MCP hosts run one implementation. Follow the delegation rather than drop the
    # assertion: a bare passthrough would still be silent.
    assert "resolve_done_edge" in src, "no pre-transition DoD gate (#238)"
    assert 'story["dod"]' in src, "DoD verdict is never persisted onto the unit"
    assert "_sp_ship_evaluated_dod" in src, "verdict never reaches the control plane (#199)"

    import advance_kernel

    gate = inspect.getsource(advance_kernel.resolve_done_edge)
    assert "dod_gate_block_reason" in gate, "kernel gate lost its block check"
    assert '"blocked"' in gate, "a red DoD gate must redirect to blocked, not complete"
    assert "is_per_story_acceptance_enabled" in gate, "#116 redirect missing"


def test_transition_story_dod_gate_actually_blocks(tmp_path, monkeypatch):
    """Behavioural companion to the source assertions above.

    The source checks catch a silent passthrough; this catches a gate that is
    wired but inert.
    """
    monkeypatch.setattr(
        m, "_sp_evaluate_dod", lambda *a, **k: {"passed": False, "checks": {}}
    )
    monkeypatch.setattr(m, "dod_gate_block_reason", lambda _dod: "ui_acceptance unmet")
    import advance_kernel

    monkeypatch.setattr(advance_kernel._sp(), "dod_gate_block_reason",
                        lambda _dod: "ui_acceptance unmet")
    monkeypatch.setattr(advance_kernel._sp(), "evaluate_story_dod",
                        lambda *a, **k: {"passed": False, "checks": {}})

    state = {"current_stories": [{"id": "US-1", "state": "reviewing"}]}
    to_state, reason, _dod = advance_kernel.resolve_done_edge(
        str(tmp_path), state, "US-1", "done", None, mode="spq"
    )
    assert to_state == "blocked"
    assert "ui_acceptance" in (reason or "")


def test_reject_story_forwards_keyword_only_arguments():
    """#238: ac_changes and project_dir are keyword-only on the callee."""
    src = inspect.getsource(m.reject_story)
    assert "acceptance_criteria_change=" in src, "ac_changes passed positionally (#238)"
    assert "project_dir=project_dir" in src, "project_dir never forwarded (#238)"


def test_parallel_block_reaches_the_orchestrator(tmp_path: Path):
    p = _project(tmp_path, stories=[_story("WU-1", "queued"), _story("WU-2", "queued")])
    (p / ".synaptory.yaml").write_text(
        'build_mode: "spq"\nparallelism:\n  story_parallelism: enabled\n'
        "  max_concurrent_subagents: 3\n"
    )
    out = m.next_action(str(p))
    assert out["parallel"]["eligible"] is True
    assert out["parallel"]["batch"][0]["story_id"] == "WU-1"


def test_planned_dod_tier_survives_the_wrapper(tmp_path: Path):
    p = _project(tmp_path, stories=[_story("WU-1", "queued")],
                 dod_tier={"tier": "mature", "decided_by": "l@h3t.co",
                           "decided_at": "2026-01-01T00:00:00Z", "sprint": 1})
    out = m.next_action(str(p))
    assert out["dod"]["tier"] == "mature"
    assert out["dod"]["tier_source"] == "planned"


# ---------------------------------------------------------------------------
# Cycle lifecycle
# ---------------------------------------------------------------------------

def test_full_happy_path_loops_then_accepts(tmp_path: Path):
    p = _project(tmp_path, lifecycle_state="DISCOVERY", current_cycle=0)
    assert m.transition(str(p), "COMMIT")["lifecycle_state"] == "COMMIT"

    s = m.open_cycle(str(p), 1, "first cycle",
                     [{"id": "WU-1", "title": "a"}], tracker_cycle=11)
    assert s["lifecycle_state"] == "CYCLE_EXECUTION"
    assert s["current_cycle"] == 1
    assert s["cycle_tracker_binding"]["tracker_cycle"] == 11

    m.transition_story(str(p), "WU-1", "in_progress")
    m.transition_story(str(p), "WU-1", "testing")
    m.transition_story(str(p), "WU-1", "reviewing")
    m.transition_story(str(p), "WU-1", "done")
    assert m.next_action(str(p))["action"] == "await_sync"

    m.transition(str(p), "SYNC")
    st = m.read_state(str(p))
    st["sync"] = {"cycle": 1, "verdict": "green"}
    m._write_state(str(p), st)
    m.transition(str(p), "CHECKPOINT")

    # Checkpoint's TW report is a gate, not a hope (parity with scrum's
    # SPRINT_REVIEW guard), so the real flow writes this receipt first.
    receipts = p / ".synaptory" / ".orchestrator" / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    _checkpoint_receipt(p, 1)

    out = m.close_cycle(str(p), "COMMIT")
    assert out["lifecycle_state"] == "COMMIT"
    st = m.read_state(str(p))
    assert len(st["cycles_completed"]) == 1
    assert st["cycles_completed"][0]["work_units_done"] == 1
    assert st["current_stories"] == []
    assert st["sync"] is None, "a closed Cycle must not carry its verdict forward"

    # Second Cycle, then release.
    m.open_cycle(str(p), 2, "second", [{"id": "WU-2", "title": "b"}])
    assert m.read_state(str(p))["current_cycle"] == 2
    m.transition(str(p), "SYNC", force=True)
    st = m.read_state(str(p))
    st["sync"] = {"cycle": 2, "verdict": "green"}
    m._write_state(str(p), st)
    m.transition(str(p), "CHECKPOINT")
    _checkpoint_receipt(p, 2)
    m.close_cycle(str(p), "ACCEPTANCE")
    _acceptance_receipts(p, cycle=2)
    assert m.transition(str(p), "COMPLETE")["lifecycle_state"] == "COMPLETE"
    with pytest.raises(ValueError, match="finished"):
        m.transition(str(p), "COMMIT")


def test_cycle_numbers_are_monotonic(tmp_path: Path):
    """§8.3: the integration clone's state owns N. Reopening a lower number
    would repoint every derived id (CYCLE-{N}, SYNC-{N}, the readiness dir)."""
    p = _project(tmp_path, lifecycle_state="COMMIT", current_cycle=3)
    with pytest.raises(ValueError, match="monotonic"):
        m.open_cycle(str(p), 2, "backwards", [])
    with pytest.raises(ValueError, match="monotonic"):
        m.open_cycle(str(p), 3, "same", [])


def test_open_cycle_auto_allocates_the_next_number(tmp_path: Path):
    p = _project(tmp_path, lifecycle_state="COMMIT", current_cycle=4)
    assert m.open_cycle(str(p), None, "next", [])["current_cycle"] == 5


def test_open_cycle_requires_commit(tmp_path: Path):
    p = _project(tmp_path, lifecycle_state="CYCLE_EXECUTION")
    with pytest.raises(ValueError, match="expected COMMIT"):
        m.open_cycle(str(p), 2, "g", [])


def test_open_cycle_clears_a_previous_verdict(tmp_path: Path):
    p = _project(tmp_path, lifecycle_state="COMMIT", current_cycle=1,
                 sync={"cycle": 1, "verdict": "green"})
    assert m.open_cycle(str(p), 2, "g", [])["sync"] is None


# ---------------------------------------------------------------------------
# Scope cut (§8.6, the release valve)
# ---------------------------------------------------------------------------

def test_cut_work_unit_marks_cancelled_and_logs(tmp_path: Path):
    p = _project(tmp_path, stories=[_story("WU-1", "queued"), _story("WU-2", "done")])
    out = m.cut_work_unit(str(p), "WU-1", reason="cannot make the barrier")
    assert out["state"] == "cancelled"
    st = m.read_state(str(p))
    cut = [s for s in st["current_stories"] if s["state"] == "cancelled"]
    assert [s["id"] for s in cut] == ["WU-1"]
    assert st["process_log"][-1]["event"] == "work_unit_cut"


def test_cutting_a_done_work_unit_is_refused(tmp_path: Path):
    """Cutting finished work would misreport the Cycle."""
    p = _project(tmp_path, stories=[_story("WU-1", "done")])
    with pytest.raises(ValueError, match="already done"):
        m.cut_work_unit(str(p), "WU-1")


def test_cut_units_are_excluded_from_quality_counts(tmp_path: Path):
    """`aggregate_sprint_dod` already excludes cancelled stories — work the PO
    decided not to ship is not work that failed quality."""
    p = _project(tmp_path, stories=[_story("WU-1", "queued"), _story("WU-2", "done")])
    m.cut_work_unit(str(p), "WU-1")
    st = m.read_state(str(p))
    from story_pipeline import aggregate_sprint_dod
    assert aggregate_sprint_dod(st)["total_stories"] == 1


# ---------------------------------------------------------------------------
# Multi-workstream isolation (§5)
# ---------------------------------------------------------------------------

def test_multi_workstream_state_isolation(tmp_path: Path, monkeypatch):
    """Two workstreams in one checkout keep separate boards.

    Rewritten for native identity (#303/#304/#305). Previously this pinned the
    Multi-Spec coupling as required behaviour: it hand-wrote a
    `{"version":"3.0","active_spec":...,"specs":{...}}` envelope and switched
    workstreams by re-exporting `SYNAPTORY_ACTIVE_SPEC`. Both are gone -- SPQ
    owns `.synaptory/.orchestrator/spq/cycles/<cycle-id>/workstreams/<ws>/`
    and selects a lane by explicit identity.

    The isolation property itself is unchanged and is what this still asserts.
    """
    import spq_paths as sp

    project = str(tmp_path)
    (tmp_path / ".synaptory.yaml").write_text("build_mode: spq\n", encoding="utf-8")
    cycle_id = sp.new_cycle_id(2)

    for ws, lifecycle, cycle, stories in (
        ("frame", "CYCLE_EXECUTION", 2, [_story("FR-1", "queued")]),
        ("spine", "COMMIT", 2, []),
    ):
        path = sp.execution_state_path(project, cycle_id, ws)
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(path).write_text(
            json.dumps(
                {
                    "version": "2.0",
                    "build_mode": "spq",
                    "lifecycle_state": lifecycle,
                    "current_cycle": cycle,
                    "current_stories": stories,
                    "lifecycle_history": [],
                }
            ),
            encoding="utf-8",
        )
    pathlib.Path(sp.index_path(project)).write_text(
        json.dumps({"current_cycle_id": cycle_id, "cycle_seq_high": 2, "cycles": []}),
        encoding="utf-8",
    )
    pathlib.Path(
        tmp_path / ".synaptory" / ".orchestrator" / "pipeline-state.json"
    ).write_text(
        json.dumps({"version": "2.0", "build_mode": "spq",
                    "spq": {"cycle_id": cycle_id, "cycle_seq": 2}}),
        encoding="utf-8",
    )

    # A stale Multi-Spec export must not select anything.
    monkeypatch.setenv("SYNAPTORY_ACTIVE_SPEC", "spine")

    monkeypatch.setenv(sp.ENV_WORKSTREAM, "frame")
    out = m.next_action(project)
    assert out["story_id"] == "FR-1"
    assert out["workstream_id"] == "frame"
    assert out["spec_id"] == "frame", "deprecated mirror, one minor"
    assert out["cycle_id"] == cycle_id
    assert out["current_cycle"] == 2

    monkeypatch.setenv(sp.ENV_WORKSTREAM, "spine")
    out = m.next_action(project)
    assert out["action"] == "not_in_execution"
    assert out["workstream_id"] == "spine"

    # frame untouched
    monkeypatch.setenv(sp.ENV_WORKSTREAM, "frame")
    assert m.read_state(project)["lifecycle_state"] == "CYCLE_EXECUTION"


def test_a_v3_backed_spq_project_refuses_with_the_migration_command(tmp_path: Path):
    """A migration demand is only useful if it names the command.

    The predecessor fell through to a generic "Unrecognized state format",
    which left the operator guessing while the project was fully blocked.
    """
    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "pipeline-state.json").write_text(
        json.dumps({"version": "3.0", "build_mode": "spq",
                    "active_spec": "frame", "specs": {"frame": {}}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        m.read_state(str(tmp_path))
    message = str(excinfo.value)
    assert "migrate_spq_native.py" in message
    assert ".synaptory/.migrations/" in message, "say the snapshot exists"


def test_rejects_a_non_spq_state_file(tmp_path: Path):
    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "pipeline-state.json").write_text(json.dumps({
        "version": "2.0", "build_mode": "scrum",
        "lifecycle_state": "SPRINT_EXECUTION", "current_stories": []}))
    with pytest.raises(ValueError, match="not SPQ format"):
        m.read_state(str(tmp_path))


# ---------------------------------------------------------------------------
# CLI parity — loop_engine shells out to this by filename
# ---------------------------------------------------------------------------

def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(LIB / "spq_state_machine.py"), *args],
        capture_output=True, text=True,
    )


def test_cli_next_action_matches_the_library(tmp_path: Path):
    """`loop_engine` tells the orchestrator to run
    `<build_mode>_state_machine.py next_action "$(pwd)"`, so the CLI is the
    real interface, not a convenience."""
    p = _project(tmp_path, stories=[_story("WU-1", "queued")])
    r = _cli("next_action", str(p))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["action"] == "dispatch_se"


def test_cli_verb_parity_with_scrum():
    """Any verb scrum exposes and SPQ plausibly needs must exist here too."""
    src = (LIB / "spq_state_machine.py").read_text()
    for verb in ("init", "read", "transition", "add_story", "transition_story",
                 "evaluate_dod", "request_acceptance", "accept_story",
                 "reject_story", "next_action", "summary"):
        assert f'action == "{verb}"' in src, f"CLI missing verb {verb}"


def test_cli_reports_illegal_transitions_as_errors(tmp_path: Path):
    p = _project(tmp_path)
    r = _cli("transition", str(p), "CHECKPOINT")
    assert r.returncode == 1
    assert "Illegal transition" in json.loads(r.stderr)["error"]


def test_summary_shape(tmp_path: Path):
    p = _project(tmp_path, stories=[_story("WU-1", "queued"), _story("WU-2", "done")])
    out = m.summary(str(p))
    assert out["current_cycle"] == 1
    assert out["work_units"]["queued"] == 1
    assert out["work_units"]["done"] == 1


# ---------------------------------------------------------------------------
# loop_engine dispatch (the fail-open regression)
# ---------------------------------------------------------------------------

def test_loop_engine_dispatches_spq_not_scrum(tmp_path: Path, monkeypatch):
    """Asserted as a NEGATIVE because the bug was fail-open: the old `else`
    routed every unrecognised build_mode to the scrum state machine."""
    import loop_engine

    p = _project(tmp_path, stories=[_story("WU-1", "queued")])
    called: list[str] = []

    real_import = __import__

    def spy(name, *a, **k):
        if name.endswith("_state_machine"):
            called.append(name)
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", spy)
    loop_engine._lifecycle_next_action(str(p))
    assert "spq_state_machine" in called
    assert "scrum_state_machine" not in called


def test_loop_engine_refuses_an_unknown_build_mode(tmp_path: Path):
    """Fail closed: an unrecognised mode must not be silently driven by scrum."""
    import loop_engine

    p = _project(tmp_path, build_mode="not-a-mode")
    assert loop_engine._lifecycle_next_action(str(p)) is None


# ---------------------------------------------------------------------------
# Prompt/code drift
#
# The skill files are prompts an LLM executes, so a verb that does not exist
# fails at runtime in front of a user rather than in CI. This is a real failure
# class in this codebase: `modes/sprint.md` told orchestrators to look for a
# `parallel` block that the wrapper could never emit (#192). Pin the coupling.
# ---------------------------------------------------------------------------

import re as _re

SKILLS = Path(__file__).resolve().parents[2] / "skills" / "synaptory"


def _cli_verbs(module_path: Path) -> set[str]:
    return set(_re.findall(r'action == "([a-z_\-]+)"', module_path.read_text()))


def _spq_skill_files() -> list[Path]:
    selected: list[Path] = []
    for file in sorted(SKILLS.rglob("*.md")):
        relative = file.relative_to(SKILLS)
        if relative == Path("modes/spq.md") or relative.parts[0] == "spq":
            selected.append(file)
    return selected


def test_spq_skill_files_exist():
    names = {f.name for f in _spq_skill_files()}
    assert "spq.md" in names, "modes/spq.md (the lifecycle dispatcher) is missing"
    for state in ("discovery", "commit", "sync", "checkpoint", "acceptance"):
        assert f"{state}.md" in names, f"spq/{state}.md is missing"


def test_every_cli_verb_referenced_by_a_prompt_exists():
    sm_verbs = _cli_verbs(Path(m.__file__))
    sb_verbs = _cli_verbs(LIB / "sync_barrier.py")
    bad: list[str] = []
    for f in _spq_skill_files():
        txt = f.read_text()
        for verb in _re.findall(r'spq_state_machine\.py"?\s+([a-z_\-]+)', txt):
            if verb not in sm_verbs:
                bad.append(f"{f.name}: spq_state_machine.py {verb}")
        for verb in _re.findall(r'sync_barrier\.py"?\s+([a-z_\-]+)', txt):
            if verb not in sb_verbs:
                bad.append(f"{f.name}: sync_barrier.py {verb}")
    assert not bad, "prompts reference CLI verbs that do not exist: " + "; ".join(bad)


def test_execution_prompt_wires_cross_workstream_dependency_recovery():
    prompt = (SKILLS / "modes" / "spq.md").read_text(encoding="utf-8")
    for required in (
        "evaluate-incremental",
        "publish_event",
        "spq_publish_event",
        "refresh_ledger",
        "spq_refresh_ledger",
        "dep_stale_manifest",
        "dep_ledger_stale",
        "dep_other_workstream_unresolved",
        "contract_published",
        '"digest":"sha256:..."',
        "every admitted non-cut Work Unit",
        "--sha {WORK_SHA}",
        "--evaluation '{INCREMENTAL_ATTESTATION_JSON}'",
        "Independent units still complete steps 1–3",
    ):
        assert required in prompt, "SPQ execution prompt omits %s" % required


def test_sync_prompt_uses_the_manifest_ref_and_all_runtime_criteria():
    prompt = (SKILLS / "spq" / "sync.md").read_text(encoding="utf-8")
    for required in (
        "verify_manifest",
        "{INTEGRATION_REF}",
        "9/9 criteria",
        "manifest_agreement",
        "manifest_closure",
        "branches_merged",
        "dependency_closure",
        "regression_green",
        "digests_match",
        "journey_green",
    ):
        assert required in prompt, "SPQ Sync prompt omits %s" % required
    for obsolete in (
        "Integration branch:  sync/cycle-{N}",
        "on sync/cycle-{N}",
        "5/5 criteria",
        "criteria 3, 4 and 5",
    ):
        assert obsolete not in prompt, "SPQ Sync prompt retains %s" % obsolete


def test_sync_prompt_warns_that_replay_warnings_are_expected():
    """§9: the regression exceeds the hook's 30s budget and the 60s replay cap
    by design. Without this note in the prompt, the first person to see the
    warning files a bug against a working barrier."""
    sync_md = next((f for f in _spq_skill_files() if f.name == "sync.md"), None)
    assert sync_md is not None
    txt = sync_md.read_text().lower()
    assert "replay" in txt and "expected" in txt


def test_po_dispatches_name_their_stage_explicitly():
    """`project-owner` is absent from ROLE_STAGE_FALLBACK (it spans three
    stages) and STAGE_PREFIX_FALLBACK omits `pro-`, so an unnamed stage costs
    the PO's cost attribution silently (§6.2)."""
    for f in _spq_skill_files():
        txt = f.read_text()
        if "project-owner" not in txt and '"po"' not in txt and "`po`" not in txt:
            continue
        if f.name in ("spq.md", "sync.md", "acceptance.md"):
            continue  # no po dispatch of their own
        assert "pro-discovery" in txt or "pro-brd" in txt or "pro-ux-spec" in txt, (
            f"{f.name} dispatches po without naming token_usage.stage"
        )


# ---------------------------------------------------------------------------
# Gate emission (§13.2 maps all four v3 gates; three were silent)
# ---------------------------------------------------------------------------

def test_all_four_gates_are_mapped(tmp_path: Path, monkeypatch):
    """Only spec_ready was emitted, so /overview would show an SPQ project as
    having never passed Inception or shipped a release."""
    emitted: list[str] = []
    import gate_emitter

    monkeypatch.setattr(gate_emitter, "emit_project_inception_approved",
                        lambda *a, **k: emitted.append("project_inception"))
    monkeypatch.setattr(gate_emitter, "emit_spec_ready_opened",
                        lambda *a, **k: emitted.append("spec_ready"))
    monkeypatch.setattr(gate_emitter, "emit_release_approved",
                        lambda *a, **k: emitted.append("release"))

    p = _project(tmp_path, lifecycle_state="DISCOVERY", current_cycle=0)
    m.transition(str(p), "COMMIT")
    assert "project_inception" in emitted
    assert "spec_ready" in emitted

    st = m.read_state(str(p))
    st["lifecycle_state"] = "ACCEPTANCE"
    st["current_cycle"] = 1
    st["cycles_completed"] = [{"cycle": 1, "sync": {"verdict": "green"}}]
    m._write_state(str(p), st)
    _acceptance_receipts(p)
    m.transition(str(p), "COMPLETE")
    assert "release" in emitted


def test_acceptance_to_complete_requires_all_five_valid_receipts(tmp_path: Path):
    p = _project(
        tmp_path,
        lifecycle_state="ACCEPTANCE",
        cycles_completed=[{"cycle": 1, "sync": {"verdict": "green"}}],
    )
    _acceptance_receipts(p)
    (p / ".synaptory" / ".orchestrator" / "receipts" / "ACCEPTANCE-1-pe.json").unlink()

    readiness = m.acceptance_readiness(str(p))

    assert readiness["ready"] is False
    assert any("ACCEPTANCE-1-pe.json" in item for item in readiness["blocking"])
    with pytest.raises(ValueError, match="Acceptance evidence is blocked"):
        m.transition(str(p), "COMPLETE")


def test_acceptance_to_complete_refuses_failed_release_evidence(tmp_path: Path):
    p = _project(
        tmp_path,
        lifecycle_state="ACCEPTANCE",
        cycles_completed=[{"cycle": 1, "sync": {"verdict": "green"}}],
    )
    _acceptance_receipts(
        p,
        qe={
            "metrics": {"tests_failed": 2},
            "verification_commands": [
                {"command": "pytest -q", "exit_code": 1, "summary": "2 failed"}
            ],
        },
        ce={"metrics": {"findings_critical": 1}},
        cr={"status": "needs-work"},
    )

    readiness = m.acceptance_readiness(str(p))

    assert readiness["ready"] is False
    assert any("failing release tests" in item for item in readiness["blocking"])
    assert any("critical security" in item for item in readiness["blocking"])
    assert any("must report status complete" in item for item in readiness["blocking"])
    with pytest.raises(ValueError, match="Acceptance evidence is blocked"):
        m.transition(str(p), "COMPLETE")


def test_acceptance_qe_does_not_block_checks_owned_by_later_roles(tmp_path: Path):
    """A truthful QE receipt must not deadlock the sequential release fan-out.

    QE runs before CE, PE, TW, and CR.  Its false values for evidence that has
    not been established yet are not release failures; the owning role receipt
    enforces each of those checks when that role runs.
    """
    p = _project(
        tmp_path,
        lifecycle_state="ACCEPTANCE",
        cycles_completed=[{"cycle": 1, "sync": {"verdict": "green"}}],
    )
    _acceptance_receipts(
        p,
        qe={
            "story_dod": {
                "tests_pass": True,
                "build_succeeds": False,
                "no_critical_findings": False,
                "code_reviewed": False,
                "coverage_no_decrease": False,
            }
        },
    )

    readiness = m.acceptance_readiness(str(p))

    assert readiness["ready"] is True, readiness["blocking"]


@pytest.mark.parametrize(
    ("abbrev", "check"),
    [
        ("qe", "tests_pass"),
        ("ce", "no_critical_findings"),
        ("pe", "build_succeeds"),
        ("cr", "code_reviewed"),
    ],
)
def test_acceptance_refuses_explicit_failure_from_check_owner(
    tmp_path: Path, abbrev: str, check: str
):
    p = _project(
        tmp_path,
        lifecycle_state="ACCEPTANCE",
        cycles_completed=[{"cycle": 1, "sync": {"verdict": "green"}}],
    )
    _acceptance_receipts(p, **{abbrev: {"story_dod": {check: False}}})

    readiness = m.acceptance_readiness(str(p))

    assert readiness["ready"] is False
    assert any(check in item for item in readiness["blocking"])


def test_acceptance_accepts_nested_approved_code_review_verdict(tmp_path: Path):
    p = _project(
        tmp_path,
        lifecycle_state="ACCEPTANCE",
        cycles_completed=[{"cycle": 1, "sync": {"verdict": "green"}}],
    )
    _acceptance_receipts(
        p,
        cr={
            "status": None,
            "code_review": {"verdict": "approved", "findings": []},
            "story_dod": {"code_reviewed": True},
        },
    )

    readiness = m.acceptance_readiness(str(p))

    assert readiness["ready"] is True, readiness["blocking"]


def test_acceptance_refuses_nested_rejected_code_review_verdict(tmp_path: Path):
    p = _project(
        tmp_path,
        lifecycle_state="ACCEPTANCE",
        cycles_completed=[{"cycle": 1, "sync": {"verdict": "green"}}],
    )
    _acceptance_receipts(
        p,
        cr={
            "status": "complete",
            "code_review": {"verdict": "rejected", "findings": ["blocker"]},
        },
    )

    readiness = m.acceptance_readiness(str(p))

    assert readiness["ready"] is False
    assert any("review verdict rejected" in item for item in readiness["blocking"])


def test_acceptance_to_complete_succeeds_only_when_release_gate_is_green(tmp_path: Path):
    p = _project(
        tmp_path,
        lifecycle_state="ACCEPTANCE",
        cycles_completed=[{"cycle": 1, "sync": {"verdict": "green"}}],
    )
    _acceptance_receipts(p)

    assert m.acceptance_readiness(str(p))["ready"] is True
    assert m.transition(str(p), "COMPLETE")["lifecycle_state"] == "COMPLETE"


def test_gate_failures_never_block_a_transition(tmp_path: Path, monkeypatch):
    """Gate emission is best-effort by design: a DoD verdict or lifecycle
    transition must never depend on the control plane being reachable."""
    import gate_emitter

    def boom(*a, **k):
        raise RuntimeError("CP unreachable")

    monkeypatch.setattr(gate_emitter, "emit_spec_ready_opened", boom)
    monkeypatch.setattr(gate_emitter, "emit_project_inception_approved", boom)
    p = _project(tmp_path, lifecycle_state="DISCOVERY", current_cycle=0)
    assert m.transition(str(p), "COMMIT")["lifecycle_state"] == "COMMIT"


# ---------------------------------------------------------------------------
# Checkpoint TW-report guard (parity with scrum's SPRINT_REVIEW guard)
# ---------------------------------------------------------------------------

def test_close_cycle_requires_a_tw_receipt(tmp_path: Path):
    p = _project(tmp_path, lifecycle_state="CHECKPOINT", current_cycle=4)
    with pytest.raises(ValueError, match="Technical Writer receipt"):
        m.close_cycle(str(p), "COMMIT")


def test_close_cycle_proceeds_with_the_tw_receipt(tmp_path: Path):
    p = _project(tmp_path, lifecycle_state="CHECKPOINT", current_cycle=4)
    _checkpoint_receipt(p, 4)
    assert m.close_cycle(str(p), "COMMIT")["lifecycle_state"] == "COMMIT"


def test_close_cycle_force_is_the_backfill_hatch(tmp_path: Path):
    p = _project(tmp_path, lifecycle_state="CHECKPOINT", current_cycle=4)
    assert m.close_cycle(str(p), "COMMIT", force=True)["lifecycle_state"] == "COMMIT"


def test_integration_seat_does_not_dispatch_during_cycle_execution(tmp_path: Path):
    p = _project(
        tmp_path,
        lifecycle_state="CYCLE_EXECUTION",
        current_cycle=1,
        stories=[_story("API-1", "queued")],
    )
    (tmp_path / ".synaptory.yaml").write_text(
        "spq:\n  workstreams:\n    - id: integration\n      integration: true\n"
        "    - id: api\n"
    )
    import spq_paths

    spq_paths.write_pin(str(p), "integration")
    out = m.next_action(str(p))
    assert out["action"] == "await_sync"
    assert not out.get("story_id")


def test_open_cycle_on_integration_seat_keeps_an_empty_board(tmp_path: Path):
    p = _project(tmp_path, lifecycle_state="COMMIT", current_cycle=0)
    (tmp_path / ".synaptory.yaml").write_text(
        "spq:\n  workstreams:\n    - id: integration\n      integration: true\n"
        "    - id: api\n"
    )
    import spq_paths

    spq_paths.write_pin(str(p), "integration")
    out = m.open_cycle(
        str(p), 1, "g", [{"id": "API-1", "title": "api"}]
    )
    assert out["current_stories"] == []
    assert out["current_cycle"] == 1
    assert out.get("_integration_cycle_units") == ["API-1"]


def test_close_cycle_integration_rollup_from_barrier_records(tmp_path: Path):
    """Integration's board is empty; Checkpoint counts come from merged
    readiness records, not the empty local board."""
    p = _project(tmp_path, lifecycle_state="COMMIT", current_cycle=0)
    (tmp_path / ".synaptory.yaml").write_text(
        "spq:\n  workstreams:\n    - id: integration\n      integration: true\n"
        "    - id: api\n    - id: cli\n"
    )
    import spq_paths

    spq_paths.write_pin(str(p), "integration")
    m.open_cycle(str(p), 1, "g", [{"id": "API-1"}, {"id": "CLI-1"}])
    m.transition(str(p), "SYNC")
    st = m.read_state(str(p))
    st["sync"] = {"cycle": 1, "verdict": "green"}
    m._write_state(str(p), st)
    m.transition(str(p), "CHECKPOINT")
    _checkpoint_receipt(p, 1)
    rec_dir = tmp_path / ".synaptory" / "sync" / "cycle-1"
    rec_dir.mkdir(parents=True)
    (rec_dir / "api.json").write_text(
        json.dumps({"work_units": {"admitted": 1, "done": 1, "cut": 0}})
    )
    (rec_dir / "cli.json").write_text(
        json.dumps({"work_units": {"admitted": 0, "done": 0, "cut": 1}})
    )
    m.close_cycle(str(p), "COMMIT")
    closed = m.read_state(str(p))["cycles_completed"][0]
    assert closed["work_units_total"] == 2
    assert closed["work_units_done"] == 1
    assert closed["work_units_cut"] == 1


def test_empty_integration_board_may_enter_sync(tmp_path: Path):
    """Delivery clones must finish units before SYNC; integration has none."""
    p = _project(tmp_path, lifecycle_state="COMMIT", current_cycle=0)
    (tmp_path / ".synaptory.yaml").write_text(
        "spq:\n  workstreams:\n    - id: integration\n      integration: true\n"
        "    - id: api\n"
    )
    import spq_paths

    spq_paths.write_pin(str(p), "integration")
    m.open_cycle(str(p), 1, "g", [])
    out = m.transition(str(p), "SYNC")
    assert out["lifecycle_state"] == "SYNC"


def test_integration_close_rolls_up_merged_readiness_counts(tmp_path: Path):
    p = _project(tmp_path, lifecycle_state="COMMIT", current_cycle=0)
    (tmp_path / ".synaptory.yaml").write_text(
        "spq:\n"
        "  workstreams:\n"
        "    - id: core\n"
        "      branch: ws/core\n"
        "    - id: integration\n"
        "      integration: true\n",
        encoding="utf-8",
    )
    import spq_paths

    spq_paths.write_pin(str(p), "integration")
    m.open_cycle(str(p), 1, "g", [{"id": "WU-1", "title": "unit"}])
    readiness = p / ".synaptory" / "sync" / "cycle-1" / "core.json"
    readiness.parent.mkdir(parents=True, exist_ok=True)
    readiness.write_text(
        json.dumps({
            "cycle": 1,
            "workstream": "core",
            "work_units": {"admitted": 1, "done": 1, "cut": 0},
            "dod": {
                "stories_evaluated": 1,
                "stories_passed": 1,
                "checks_failed": {},
            },
        }),
        encoding="utf-8",
    )
    m.transition(str(p), "SYNC")
    state = m.read_state(str(p))
    state["sync"] = {"cycle": 1, "verdict": "green"}
    m._write_state(str(p), state)
    m.transition(str(p), "CHECKPOINT")
    _checkpoint_receipt(p, 1)

    m.close_cycle(str(p), "COMMIT")

    summary = m.read_state(str(p))["cycles_completed"][0]
    assert summary["work_units_total"] == 1
    assert summary["work_units_done"] == 1
    assert summary["work_units_cut"] == 0
    assert summary["dod"]["stories_evaluated"] == 1
    assert summary["dod"]["stories_passed"] == 1


# ---------------------------------------------------------------------------
# CLI flags that were silently dropped
# ---------------------------------------------------------------------------

def test_reject_story_parses_repeated_ac_change(tmp_path: Path):
    """The signature accepted ac_changes and scrum collects repeated flags, but
    the SPQ CLI never parsed them, so they vanished with no error."""
    src = (LIB / "spq_state_machine.py").read_text()
    assert '"--ac-change"' in src, "reject_story must parse --ac-change"


def test_record_method_signal_accepts_structured_data(tmp_path: Path):
    """§6.3 asks for the state a profile straddle occurred in, which needs
    somewhere structured to go."""
    p = _project(tmp_path, lifecycle_state="CHECKPOINT", current_cycle=2)
    out = m.record_method_signal(
        str(p), "profile_straddle", "po spanned Analyst and Planner",
        {"state": "COMMIT", "agent": "po"})
    assert out["data"]["state"] == "COMMIT"
    assert out["cycle"] == 2
    assert m.read_state(str(p))["method_signals"][-1]["kind"] == "profile_straddle"


# ---------------------------------------------------------------------------
# SPD-173 — the Slice → Cycle rename is a read shim, not an accepted alias
# ---------------------------------------------------------------------------

def _legacy_project(tmp_path: Path, **over) -> Path:
    """A pipeline-state.json exactly as v1.1.x wrote it, pre-SPD-173."""
    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True, exist_ok=True)
    state = {
        "version": "2.0",
        "build_mode": "spq",
        "lifecycle_state": "SLICE_EXECUTION",
        "current_slice": 3,
        "slice_goal": "ship the barrier",
        "slices_completed": [1, 2],
        "slice_tracker_binding": {"slice": 3, "tracker_cycle": 11},
        "sync": None,
        "current_stories": [],
        "lifecycle_history": [
            {"state": "COMMIT", "entered_at": "2026-01-01T00:00:00Z",
             "exited_at": "2026-01-02T00:00:00Z"},
            {"state": "SLICE_EXECUTION", "entered_at": "2026-01-02T00:00:00Z",
             "exited_at": None},
        ],
    }
    state.update(over)
    (orch / "pipeline-state.json").write_text(json.dumps(state))
    return tmp_path


def test_legacy_slice_state_is_read_as_cycle(tmp_path: Path):
    """A pre-rename state file must not strand a live pilot mid-Cycle."""
    s = m.read_state(str(_legacy_project(tmp_path)))
    assert s["lifecycle_state"] == "CYCLE_EXECUTION"
    assert s["current_cycle"] == 3
    assert s["cycle_goal"] == "ship the barrier"
    assert s["cycles_completed"] == [1, 2]
    assert s["cycle_tracker_binding"]["tracker_cycle"] == 11
    # the old spellings are gone, not carried alongside as aliases
    for dead in ("current_slice", "slice_goal", "slices_completed",
                 "slice_tracker_binding"):
        assert dead not in s, f"{dead} survived the migration as an alias"


def test_legacy_lifecycle_history_is_migrated(tmp_path: Path):
    """§13 gate rollups key on history states; a stale one splits the series."""
    s = m.read_state(str(_legacy_project(tmp_path)))
    states = [h["state"] for h in s["lifecycle_history"]]
    assert "SLICE_EXECUTION" not in states
    assert states == ["COMMIT", "CYCLE_EXECUTION"]


def test_migration_never_clobbers_new_form_data(tmp_path: Path):
    """A half-upgraded file must keep the CURRENT value, not the stale copy."""
    p = _legacy_project(tmp_path, current_cycle=9)
    s = m.read_state(str(p))
    assert s["current_cycle"] == 9, "a stale current_slice overwrote live data"


def test_migration_is_idempotent_on_new_state(tmp_path: Path):
    s = m.read_state(str(_project(tmp_path)))
    assert s["lifecycle_state"] == "CYCLE_EXECUTION"
    assert s["current_cycle"] == 1


# ── #304 incidental: SPQ's DoD tier was pinned to `early` forever ──────────


def test_resolve_dod_tier_reads_current_cycle_for_spq(tmp_path):
    """`resolve_dod_tier` and `next_action` both read `state["current_sprint"]`,
    which SPQ never sets (it counts Cycles). Both therefore handed
    `determine_dod_intensity` two Nones and computed `early` regardless of
    Cycle number -- contradicting `_dod_intensity`'s own docstring, and
    silently waiving `code_reviewed` for the whole life of an SPQ project.
    """
    from story_pipeline import resolve_dod_tier

    (tmp_path / ".synaptory.yaml").write_text("build_mode: spq\n", encoding="utf-8")
    early = resolve_dod_tier(str(tmp_path), {"current_cycle": 1})
    mature = resolve_dod_tier(str(tmp_path), {"current_cycle": 7})
    assert early["tier"] == "early"
    assert mature["tier"] != "early", "Cycle number must move the tier"
    assert mature["tier_source"] == "computed"


def test_scrum_dod_tier_is_unaffected_by_the_cycle_fallback(tmp_path):
    """Scrum and kanban never set `current_cycle`, so the fallback is inert."""
    from story_pipeline import resolve_dod_tier

    (tmp_path / ".synaptory.yaml").write_text("build_mode: scrum\n", encoding="utf-8")
    assert resolve_dod_tier(str(tmp_path), {"current_sprint": 1})["tier"] == "early"
    assert resolve_dod_tier(str(tmp_path), {"current_sprint": 7})["tier"] != "early"


def test_spq_next_action_passes_dep_context_and_gate_mode(tmp_path):
    """The wrapper is the only place that can resolve identity off state, so a
    silently-unthreaded dep_context would leave the gate resolving every
    cross-workstream edge as a plain unknown with no cycle scoping."""
    import inspect

    import spq_state_machine as ssm

    source = inspect.getsource(ssm.next_action)
    assert "dep_context=dep_context(project_dir, state)" in source
    assert "dependency_gate=dependency_gate_mode(project_dir)" in source


def test_accept_story_emits_evidence_dod_accepted(tmp_path, monkeypatch):
    """Pulse Notes uses per-story acceptance; Quality Gate Queue needs the
    approved row that Scrum's wrapper already emitted and SPQ skipped."""
    import gate_emitter

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gate_emitter,
        "emit_evidence_dod_accepted",
        lambda story_id, who: calls.append((story_id, who)) or True,
    )
    p = _project(tmp_path, stories=[_story("US-1", "awaiting_acceptance")])
    m.accept_story(str(p), "US-1", "po@h3t.co")
    assert calls == [("US-1", "po@h3t.co")]
    state = m.read_state(str(p))
    story = next(s for s in state["current_stories"] if s["id"] == "US-1")
    assert story["state"] == "done"
    assert story["accepted_by"] == "po@h3t.co"


# ── #305 groundwork: the committed transport must actually be committable ──
#
# The un-ignore rule (`.synaptory/*` plus `!.synaptory/cycles/`) has only ever
# lived in a runbook, a prompt and some fixtures. Nothing checked it. A project
# that skipped that line gets a manifest that writes cleanly, reads back cleanly
# and is invisible to every other clone -- the integration seat looks fine while
# the workstreams see nothing. That is the same class of failure #303 hit as
# "a fresh clone could not discover the Cycle at all".


def _repo_with_config(tmp_path, gitignore: str, workstreams: str):
    import subprocess

    project = tmp_path / "proj"
    project.mkdir(parents=True)
    for args in (["init", "-q"], ["config", "user.email", "t@e.co"],
                 ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=project, check=True,
                       capture_output=True)
    (project / ".gitignore").write_text(gitignore, encoding="utf-8")
    (project / ".synaptory.yaml").write_text(
        "build_mode: \"spq\"\nspq:\n  workstreams:\n" + workstreams,
        encoding="utf-8",
    )
    (project / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=project, check=True,
                   capture_output=True)
    return project


_TWO_LANES = '    - id: "spine"\n      shared_owner: true\n    - id: "frame"\n'
_ONE_LANE = '    - id: "spine"\n      shared_owner: true\n'
#: The .gitignore rule `_repo_with_config` needs for the committed transport to
#: be reachable. Spelled once rather than re-typed per test.
_UNIGNORED = ".synaptory/*\n!.synaptory/sync/\n!.synaptory/cycles/\n"


def test_transport_ignored_detects_a_missing_un_ignore(tmp_path):
    project = _repo_with_config(tmp_path, ".synaptory/*\n", _TWO_LANES)
    assert m.transport_ignored(
        str(project), ".synaptory/cycles/7-abc12345/manifest.json"
    ) is True


def test_transport_ignored_accepts_the_documented_rule(tmp_path):
    project = _repo_with_config(
        tmp_path,
        ".synaptory/*\n!.synaptory/sync/\n!.synaptory/cycles/\n"
        "!.synaptory/coordination-cycles/\n",
        _TWO_LANES,
    )
    for rel in (".synaptory/cycles/7-abc12345/manifest.json",
                ".synaptory/coordination-cycles/cc-1-abc12345/manifest.json"):
        assert m.transport_ignored(str(project), rel) is False, rel


def test_transport_ignored_never_guesses_outside_a_repo(tmp_path):
    """No git, no verdict. A refusal built on a failed probe would be worse
    than the hole it closes."""
    plain = tmp_path / "plain"
    plain.mkdir()
    assert m.transport_ignored(str(plain), ".synaptory/cycles/x/manifest.json") is False


def test_the_documented_gitignore_rule_is_the_one_that_works(tmp_path):
    """`.synaptory/` + a negation silently does nothing.

    Git cannot re-include a path whose parent directory is excluded, so the
    obvious spelling leaves every transport file ignored with no visible cause.
    The trailing `/*` is what makes the negation take effect.
    """
    broken = _repo_with_config(
        tmp_path / "a",
        ".synaptory/\n!.synaptory/cycles/\n",
        _TWO_LANES,
    )
    assert m.transport_ignored(
        str(broken), ".synaptory/cycles/7-abc12345/manifest.json"
    ) is True, "the naive spelling must be detected as broken"


def _seal(project, seq=1, workstreams=("spine", "frame")):
    cycle_id = _paths_mod.new_cycle_id(seq)
    return m.seal_manifest(
        str(project),
        cycle_id=cycle_id,
        cycle_seq=seq,
        goal="auth",
        work_units=[
            {"id": "WU-%02d" % (i + 1), "owner_workstream": ws}
            for i, ws in enumerate(workstreams)
        ],
        baseline_sha="a" * 40,
    )


def test_open_refuses_an_ignored_transport_when_other_clones_depend_on_it(tmp_path):
    """Two lanes means two clones, which means the committed copy is the only
    way the Cycle travels. Sealing into an ignored path would produce a Cycle
    that works perfectly here and does not exist anywhere else."""
    project = _repo_with_config(tmp_path, ".synaptory/*\n", _TWO_LANES)
    with pytest.raises(ValueError) as exc:
        _seal(project)
    assert ".gitignore" in str(exc.value)
    assert "!.synaptory/cycles/" in str(exc.value), (
        "the refusal must carry the exact fix, or the operator has no recovery "
        "path from a rule that is invisible by construction"
    )


def test_open_does_not_refuse_a_solo_cycle(tmp_path):
    """A single-lane Cycle never reads the committed copy -- the local sealed
    manifest is authoritative for the only clone there is. Refusing there would
    break solo projects to protect them from a problem they cannot have."""
    project = _repo_with_config(tmp_path, ".synaptory/*\n", _ONE_LANE)
    sealed = _seal(project, workstreams=("spine",))
    assert sealed["manifest_hash"]


def test_open_is_happy_with_the_documented_rule(tmp_path):
    project = _repo_with_config(
        tmp_path,
        ".synaptory/*\n!.synaptory/sync/\n!.synaptory/cycles/\n",
        _TWO_LANES,
    )
    sealed = _seal(project)
    assert sealed["manifest_hash"]


# ─── #334 G8 — every manifest path reports, and a failure is retried ─────────


@pytest.fixture
def observed(monkeypatch):
    """Record what `seal_manifest` / `revise_manifest` report to the CP."""
    import manifest_emitter as me

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        me, "_ship",
        lambda verb, manifest, project_dir: (
            calls.append((verb, manifest.get("manifest_hash"))) or True
        ),
    )
    return calls


def test_seal_reports_the_manifest_it_sealed(tmp_path, observed):
    project = _repo_with_config(tmp_path, _UNIGNORED, _ONE_LANE)
    sealed = _seal(project, workstreams=("spine",))
    assert ("cycle-manifest", sealed["manifest_hash"]) in observed


def _seal_with_id(project, cycle_id, seq=1, workstreams=("spine",)):
    """Seal against a FIXED cycle_id. `_seal` allocates a new hashed id per
    call by design (two clones can both open a Cycle 7), so re-sealing the same
    Cycle needs the id pinned."""
    return m.seal_manifest(
        str(project),
        cycle_id=cycle_id,
        cycle_seq=seq,
        goal="auth",
        work_units=[
            {"id": "WU-%02d" % (i + 1), "owner_workstream": ws}
            for i, ws in enumerate(workstreams)
        ],
        baseline_sha="a" * 40,
    )


def test_re_sealing_an_existing_manifest_still_reports_it(tmp_path, monkeypatch):
    """`_seal_manifest` returns early when the same hash is already on disk, and
    that early return never reached the old emission — so a re-open shipped
    nothing at all (#334 G8). The retry marker is what keeps the fix from
    turning that into a duplicate ship on every re-open."""
    import manifest_emitter as me

    project = _repo_with_config(tmp_path, _UNIGNORED, _ONE_LANE)
    cycle_id = _paths_mod.new_cycle_id(1)

    # First seal fails to ship, so no marker is written.
    monkeypatch.setattr(me, "_ship", lambda *_a, **_k: False)
    first = _seal_with_id(project, cycle_id)

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        me, "_ship",
        lambda verb, manifest, project_dir: (
            calls.append((verb, manifest.get("manifest_hash"))) or True
        ),
    )
    # Re-open: `_seal_manifest` takes `return existing`, which used to ship
    # nothing. It now reports, and the manifest finally lands.
    again = _seal_with_id(project, cycle_id)
    assert again["manifest_hash"] == first["manifest_hash"]
    assert ("cycle-manifest", first["manifest_hash"]) in calls

    # And a third re-open does not ship it again.
    calls.clear()
    _seal_with_id(project, cycle_id)
    assert calls == []


def test_a_seal_whose_ship_failed_is_retried_on_the_next_seal(tmp_path, monkeypatch):
    import manifest_emitter as me

    project = _repo_with_config(tmp_path, _UNIGNORED, _ONE_LANE)
    monkeypatch.setattr(me, "_ship", lambda *_a, **_k: False)
    sealed = _seal(project, seq=1, workstreams=("spine",))

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        me, "_ship",
        lambda verb, manifest, project_dir: (
            calls.append((verb, manifest.get("manifest_hash"))) or True
        ),
    )
    me.resend_pending(str(project))
    assert calls == [("cycle-manifest", sealed["manifest_hash"])]


def test_revising_a_manifest_reports_the_revision(tmp_path, observed):
    """A revision had NO producer: `seal_manifest` was the only emitter and
    `revise_manifest` never called it, so an append-only table designed to hold
    one row per revision received none."""
    project = _repo_with_config(tmp_path, _UNIGNORED, _TWO_LANES)
    sealed = _seal(project, seq=1)
    observed.clear()

    revised = m.revise_manifest(
        str(project),
        cycle_id=sealed["cycle_id"],
        reason="dropped a unit",
        drop_units=["WU-01"],
        add_units=[{"id": "WU-99", "labels": ["ws:frame"]}],
        revised_by="po@h3t.co",
    )
    assert revised["manifest_hash"] != sealed["manifest_hash"]
    assert ("cycle-manifest", revised["manifest_hash"]) in observed


def test_a_ceremony_survives_a_control_plane_that_explodes(tmp_path, monkeypatch):
    """Git holds the seal. A reporting failure must never become a delivery one."""
    import manifest_emitter as me

    project = _repo_with_config(tmp_path, _UNIGNORED, _ONE_LANE)
    monkeypatch.setattr(
        me, "resend_pending",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("CP down")),
    )
    monkeypatch.setattr(
        me, "emit_cycle_manifest",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("CP down")),
    )
    sealed = _seal(project, workstreams=("spine",))
    assert sealed["manifest_hash"]


# ─── #334 G15 — a rollup never mixes scopes silently ────────────────────────


def test_close_cycle_records_the_scope_of_its_work_unit_counts(tmp_path):
    """`work_units_*` are seat-local on a delivery clone and manifest-wide on the
    integration seat, under the same three key names. The record now says which,
    so a reader comparing it to seat-local `cycles_completed` can tell."""
    project = _project(
        tmp_path,
        stories=[_story("WU-1", "done"), _story("WU-2", "cancelled")],
        lifecycle_state="CHECKPOINT",
    )
    _checkpoint_receipt(project, 1)
    m.close_cycle(str(project), proceed_to="COMMIT")

    record = m.read_state(str(project))["cycles_completed"][-1]
    assert record["work_units_scope"] == m.SCOPE_SEAT
    assert record["work_units_source"] == "local_board"
    assert record["work_units_total"] == 2


def test_summary_says_both_of_its_counts_are_seat_local(tmp_path):
    project = _project(tmp_path, stories=[_story("WU-1", "done")])
    out = m.summary(str(project))
    assert out["scope"] == {
        "cycles_completed": m.SCOPE_SEAT,
        "work_units": m.SCOPE_SEAT,
    }


def test_the_two_scopes_are_distinct_values(tmp_path):
    """If these ever collapse to one string the labelling stops labelling."""
    assert m.SCOPE_MANIFEST != m.SCOPE_SEAT


def test_seal_manifest_wrapper_does_not_drift_from_its_implementation():
    """`seal_manifest` forwards to `_seal_manifest` argument by argument so the
    public contract stays explicit rather than collapsing to **kwargs. That
    duplication is only safe if it cannot silently diverge."""
    assert str(inspect.signature(m.seal_manifest)) == str(
        inspect.signature(m._seal_manifest)
    ), "seal_manifest and _seal_manifest signatures have drifted apart"
