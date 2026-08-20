"""Layer 1 — the SPQ lifecycle state machine (docs/spq-lifecycle-design.md §4, §10).

Hypothesis: two invariants carry this file, and both are the kind that a
plausible-looking refactor silently breaks.

  1. `SLICE_EXECUTION → CHECKPOINT` must be UNREACHABLE. Integration precedes
     demonstration (§4.1); a Checkpoint demoing from an un-integrated branch
     shows 1/N of the system and defers the integration debt to whoever finds
     it later.
  2. all-Work-Units-terminal must yield `await_sync`, never `sprint_complete`
     (§10.2). The shared pipeline has no concept of a barrier, so passing its
     verdict through would close the Slice without integrating.

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
from pathlib import Path

import pytest

import spq_state_machine as m
from story_pipeline import CONTINUE_ELIGIBLE, NEXT_ACTIONS


pytestmark = pytest.mark.unit

LIB = Path(m.__file__).parent


def _project(tmp_path: Path, stories=None, **over) -> Path:
    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True, exist_ok=True)
    state = {
        "version": "2.0",
        "build_mode": "spq",
        "lifecycle_state": "SLICE_EXECUTION",
        "current_slice": 1,
        "slice_goal": "g",
        "slices_completed": [],
        "sync": None,
        "current_stories": stories if stories is not None else [],
        "lifecycle_history": [
            {"state": "SLICE_EXECUTION", "entered_at": "2026-01-01T00:00:00Z",
             "exited_at": None}
        ],
    }
    state.update(over)
    (orch / "pipeline-state.json").write_text(json.dumps(state))
    return tmp_path


def _story(sid: str, st: str, **kw) -> dict:
    return {"id": sid, "title": f"WU {sid}", "state": st, "pipeline_log": [], **kw}


# ---------------------------------------------------------------------------
# Invariant 1 — integration precedes demonstration
# ---------------------------------------------------------------------------

def test_checkpoint_is_unreachable_from_slice_execution():
    assert "CHECKPOINT" not in m.SPQ_TRANSITIONS["SLICE_EXECUTION"]
    assert m.SPQ_TRANSITIONS["SLICE_EXECUTION"] == ["SYNC"]


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
    p = _project(tmp_path, lifecycle_state="SYNC", current_slice=3)
    with pytest.raises(ValueError, match="no green barrier"):
        m.transition(str(p), "CHECKPOINT")


def test_sync_clears_with_a_green_verdict_for_this_slice(tmp_path: Path):
    p = _project(tmp_path, lifecycle_state="SYNC", current_slice=3,
                 sync={"slice": 3, "verdict": "green"})
    out = m.transition(str(p), "CHECKPOINT")
    assert out["lifecycle_state"] == "CHECKPOINT"


def test_a_verdict_for_another_slice_does_not_count(tmp_path: Path):
    """A stale verdict from Slice 2 must not open Slice 3's Checkpoint."""
    p = _project(tmp_path, lifecycle_state="SYNC", current_slice=3,
                 sync={"slice": 2, "verdict": "green"})
    with pytest.raises(ValueError, match="no green barrier"):
        m.transition(str(p), "CHECKPOINT")


def test_force_is_the_documented_escape_hatch(tmp_path: Path):
    p = _project(tmp_path, lifecycle_state="SYNC", current_slice=3)
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


def test_outside_slice_execution_is_not_dispatchable(tmp_path: Path):
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
    assert out["current_slice"] == 1


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
    assert "dod_gate_block_reason" in src, "no pre-transition DoD gate (#238)"
    assert "blocked" in src, "a red DoD gate must redirect to blocked, not complete"
    assert "is_per_story_acceptance_enabled" in src, "#116 redirect missing in SPQ"
    assert 'story["dod"]' in src, "DoD verdict is never persisted onto the unit"
    assert "_sp_ship_evaluated_dod" in src, "verdict never reaches the control plane (#199)"


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
# Slice lifecycle
# ---------------------------------------------------------------------------

def test_full_happy_path_loops_then_accepts(tmp_path: Path):
    p = _project(tmp_path, lifecycle_state="DISCOVERY", current_slice=0)
    assert m.transition(str(p), "COMMIT")["lifecycle_state"] == "COMMIT"

    s = m.open_slice(str(p), 1, "first slice",
                     [{"id": "WU-1", "title": "a"}], tracker_cycle=11)
    assert s["lifecycle_state"] == "SLICE_EXECUTION"
    assert s["current_slice"] == 1
    assert s["slice_tracker_binding"]["tracker_cycle"] == 11

    m.transition_story(str(p), "WU-1", "in_progress")
    m.transition_story(str(p), "WU-1", "testing")
    m.transition_story(str(p), "WU-1", "reviewing")
    m.transition_story(str(p), "WU-1", "done")
    assert m.next_action(str(p))["action"] == "await_sync"

    m.transition(str(p), "SYNC")
    st = m.read_state(str(p))
    st["sync"] = {"slice": 1, "verdict": "green"}
    m._write_state(str(p), st)
    m.transition(str(p), "CHECKPOINT")

    # Checkpoint's TW report is a gate, not a hope (parity with scrum's
    # SPRINT_REVIEW guard), so the real flow writes this receipt first.
    receipts = p / ".synaptory" / ".orchestrator" / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    (receipts / "CHECKPOINT-1-tw.json").write_text("{}")

    out = m.close_slice(str(p), "COMMIT")
    assert out["lifecycle_state"] == "COMMIT"
    st = m.read_state(str(p))
    assert len(st["slices_completed"]) == 1
    assert st["slices_completed"][0]["work_units_done"] == 1
    assert st["current_stories"] == []
    assert st["sync"] is None, "a closed Slice must not carry its verdict forward"

    # Second Slice, then release.
    m.open_slice(str(p), 2, "second", [{"id": "WU-2", "title": "b"}])
    assert m.read_state(str(p))["current_slice"] == 2
    m.transition(str(p), "SYNC", force=True)
    m.transition(str(p), "CHECKPOINT", force=True)
    (receipts / "CHECKPOINT-2-tw.json").write_text("{}")
    m.close_slice(str(p), "ACCEPTANCE")
    assert m.transition(str(p), "COMPLETE")["lifecycle_state"] == "COMPLETE"
    with pytest.raises(ValueError, match="finished"):
        m.transition(str(p), "COMMIT")


def test_slice_numbers_are_monotonic(tmp_path: Path):
    """§8.3: the integration clone's state owns N. Reopening a lower number
    would repoint every derived id (SLICE-{N}, SYNC-{N}, the readiness dir)."""
    p = _project(tmp_path, lifecycle_state="COMMIT", current_slice=3)
    with pytest.raises(ValueError, match="monotonic"):
        m.open_slice(str(p), 2, "backwards", [])
    with pytest.raises(ValueError, match="monotonic"):
        m.open_slice(str(p), 3, "same", [])


def test_open_slice_auto_allocates_the_next_number(tmp_path: Path):
    p = _project(tmp_path, lifecycle_state="COMMIT", current_slice=4)
    assert m.open_slice(str(p), None, "next", [])["current_slice"] == 5


def test_open_slice_requires_commit(tmp_path: Path):
    p = _project(tmp_path, lifecycle_state="SLICE_EXECUTION")
    with pytest.raises(ValueError, match="expected COMMIT"):
        m.open_slice(str(p), 2, "g", [])


def test_open_slice_clears_a_previous_verdict(tmp_path: Path):
    p = _project(tmp_path, lifecycle_state="COMMIT", current_slice=1,
                 sync={"slice": 1, "verdict": "green"})
    assert m.open_slice(str(p), 2, "g", [])["sync"] is None


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
    """Cutting finished work would misreport the Slice."""
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
    """The integration clone holds one slot per workstream; a transition in one
    must not touch another."""
    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "pipeline-state.json").write_text(json.dumps({
        "version": "3.0", "build_mode": "spq", "active_spec": "frame",
        "specs": {
            "frame": {"lifecycle_state": "SLICE_EXECUTION", "current_slice": 2,
                      "current_stories": [_story("FR-1", "queued")],
                      "lifecycle_history": []},
            "spine": {"lifecycle_state": "COMMIT", "current_slice": 1,
                      "current_stories": [], "lifecycle_history": []},
        },
    }))

    monkeypatch.setenv("SYNAPTORY_ACTIVE_SPEC", "frame")
    out = m.next_action(str(tmp_path))
    assert out["story_id"] == "FR-1"
    assert out["spec_id"] == "frame"
    assert out["current_slice"] == 2

    monkeypatch.setenv("SYNAPTORY_ACTIVE_SPEC", "spine")
    out = m.next_action(str(tmp_path))
    assert out["action"] == "not_in_execution"
    assert out["current_slice"] == 1

    # frame untouched
    monkeypatch.setenv("SYNAPTORY_ACTIVE_SPEC", "frame")
    assert m.read_state(str(tmp_path))["lifecycle_state"] == "SLICE_EXECUTION"


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
    assert out["current_slice"] == 1
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

SKILLS = Path(m.__file__).resolve().parents[2] / "skills" / "synaptory"


def _cli_verbs(module_path: Path) -> set[str]:
    return set(_re.findall(r'action == "([a-z_\-]+)"', module_path.read_text()))


def _spq_skill_files() -> list[Path]:
    return [f for f in sorted(SKILLS.rglob("*.md")) if "spq" in f.as_posix()]


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

    p = _project(tmp_path, lifecycle_state="DISCOVERY", current_slice=0)
    m.transition(str(p), "COMMIT")
    assert "project_inception" in emitted
    assert "spec_ready" in emitted

    st = m.read_state(str(p))
    st["lifecycle_state"] = "ACCEPTANCE"
    m._write_state(str(p), st)
    m.transition(str(p), "COMPLETE")
    assert "release" in emitted


def test_gate_failures_never_block_a_transition(tmp_path: Path, monkeypatch):
    """Gate emission is best-effort by design: a DoD verdict or lifecycle
    transition must never depend on the control plane being reachable."""
    import gate_emitter

    def boom(*a, **k):
        raise RuntimeError("CP unreachable")

    monkeypatch.setattr(gate_emitter, "emit_spec_ready_opened", boom)
    monkeypatch.setattr(gate_emitter, "emit_project_inception_approved", boom)
    p = _project(tmp_path, lifecycle_state="DISCOVERY", current_slice=0)
    assert m.transition(str(p), "COMMIT")["lifecycle_state"] == "COMMIT"


# ---------------------------------------------------------------------------
# Checkpoint TW-report guard (parity with scrum's SPRINT_REVIEW guard)
# ---------------------------------------------------------------------------

def test_close_slice_requires_a_tw_receipt(tmp_path: Path):
    p = _project(tmp_path, lifecycle_state="CHECKPOINT", current_slice=4)
    with pytest.raises(ValueError, match="Technical Writer receipt"):
        m.close_slice(str(p), "COMMIT")


def test_close_slice_proceeds_with_the_tw_receipt(tmp_path: Path):
    p = _project(tmp_path, lifecycle_state="CHECKPOINT", current_slice=4)
    receipts = p / ".synaptory" / ".orchestrator" / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    (receipts / "CHECKPOINT-4-tw.json").write_text("{}")
    assert m.close_slice(str(p), "COMMIT")["lifecycle_state"] == "COMMIT"


def test_close_slice_force_is_the_backfill_hatch(tmp_path: Path):
    p = _project(tmp_path, lifecycle_state="CHECKPOINT", current_slice=4)
    assert m.close_slice(str(p), "COMMIT", force=True)["lifecycle_state"] == "COMMIT"


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
    p = _project(tmp_path, lifecycle_state="CHECKPOINT", current_slice=2)
    out = m.record_method_signal(
        str(p), "profile_straddle", "po spanned Analyst and Planner",
        {"state": "COMMIT", "agent": "po"})
    assert out["data"]["state"] == "COMMIT"
    assert out["slice"] == 2
    assert m.read_state(str(p))["method_signals"][-1]["kind"] == "profile_straddle"
