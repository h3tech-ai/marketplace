"""Layer 1 — `story_pipeline.next_action` + `progress_digest` (epic #75 P1).

Hypothesis: "what happens next" in the story pipeline is a pure function
of (board state, per-story-acceptance toggle, receipt files on disk).
One test per action in the enum, plus the selection-policy invariants
(furthest-along in-flight first, FIFO within a stage), the crash-recovery
`receipt_present` path, multi-spec routing through the state-machine
wrapper, and digest stability. These are the P2 Stop-hook loop engine's
foundations — the continuation decision is only as safe as this mapping.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from story_pipeline import (
    NEXT_ACTIONS,
    next_action,
    progress_digest,
)


def _story(sid: str, state: str, **kw) -> dict:
    return {"id": sid, "title": f"Story {sid}", "state": state, **kw}


def _state(*stories: dict, sprint: int = 2, **kw) -> dict:
    return {
        "version": "2.0",
        "build_mode": "scrum",
        "lifecycle_state": "SPRINT_EXECUTION",
        "current_sprint": sprint,
        "current_stories": list(stories),
        **kw,
    }


def _receipt(receipts_dir: Path, sid: str, role: str, *, completed_at: str = "2099-01-01T00:00:00Z") -> None:
    receipts_dir.mkdir(parents=True, exist_ok=True)
    (receipts_dir / f"{sid}-{role}.json").write_text(
        json.dumps({"task": "t", "completed_at": completed_at})
    )


# ── one test per action ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_no_stories():
    out = next_action(_state())
    assert out["action"] == "no_stories"
    assert out["story_id"] is None


@pytest.mark.unit
def test_queued_dispatches_se():
    out = next_action(_state(_story("US-1", "queued")))
    assert out["action"] == "dispatch_se"
    assert out["story_id"] == "US-1"
    assert out["role"] == "se"
    assert out["human_gate_pending"] is False


@pytest.mark.unit
def test_in_progress_dispatches_se():
    out = next_action(_state(_story("US-1", "in_progress")))
    assert out["action"] == "dispatch_se"
    assert out["receipt_present"] is False


@pytest.mark.unit
def test_testing_dispatches_qe():
    out = next_action(_state(_story("US-1", "testing")))
    assert out["action"] == "dispatch_qe"
    assert out["role"] == "qe"


@pytest.mark.unit
def test_reviewing_dispatches_cr_when_tier_requires(tmp_path: Path):
    # Sprint 2 → intensity "growing" → code_reviewed required.
    out = next_action(
        _state(_story("US-1", "reviewing"), sprint=2), receipts_dir=str(tmp_path)
    )
    assert out["action"] == "dispatch_cr"
    assert "growing" in out["reason"]


@pytest.mark.unit
def test_reviewing_promotes_when_cr_receipt_present(tmp_path: Path):
    _receipt(tmp_path, "US-1", "cr")
    out = next_action(
        _state(_story("US-1", "reviewing"), sprint=2), receipts_dir=str(tmp_path)
    )
    assert out["action"] == "promote_story"
    assert out["transition_to"] == "done"


@pytest.mark.unit
def test_reviewing_promotes_without_cr_at_early_tier(tmp_path: Path):
    # Sprint 1 → "early" tier has no code_reviewed check → no CR dispatch.
    out = next_action(
        _state(_story("US-1", "reviewing"), sprint=1), receipts_dir=str(tmp_path)
    )
    assert out["action"] == "promote_story"


@pytest.mark.unit
def test_reviewing_requests_acceptance_when_toggle_on(tmp_path: Path):
    out = next_action(
        _state(_story("US-1", "reviewing"), sprint=1),
        per_story_acceptance=True,
        receipts_dir=str(tmp_path),
    )
    assert out["action"] == "request_acceptance"
    assert out["transition_to"] == "awaiting_acceptance"


@pytest.mark.unit
def test_await_acceptance_is_human_gate():
    out = next_action(_state(_story("US-1", "awaiting_acceptance")))
    assert out["action"] == "await_acceptance"
    assert out["human_gate_pending"] is True


@pytest.mark.unit
def test_recover_blocked_adhoc_block_uses_se_ladder():
    # A bare block with no gate reason and no retries → retry ladder default
    # (SE, retry_same_prompt). This is the legitimate ad-hoc-hold case, NOT
    # a gate block (see test_gate_blocked_* below).
    out = next_action(_state(_story("US-1", "blocked", blocked_reason="manual hold")))
    assert out["action"] == "recover_blocked"
    assert out["recovery"]["tier"] == "retry_same_prompt"
    assert out["recovery"]["role"] == "se"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("gate", "expected_role"),
    [
        ("no_critical_findings", "ce"),
        ("runtime_verified", "qe"),
        ("ui_acceptance", "qe"),
        ("integration_verified", "qe"),
    ],
)
def test_gate_blocked_routes_to_remediation_agent(gate: str, expected_role: str):
    """P1a regression guard: a story blocked by a conditional DoD gate must
    route to the gate's remediation agent, NOT the SE retry ladder (which
    can never clear the gate → infinite mis-dispatch loop). Gate-blocked
    stories carry a 'DoD gate:' blocked_reason and zero retries."""
    reason = f"DoD gate: {gate} has not passed — dispatch the remediation agent"
    story = _story("US-1", "blocked", blocked_reason=reason)
    out = next_action(_state(story))
    assert out["action"] == "recover_blocked"
    assert out["recovery"]["tier"] == "gate_remediation"
    assert out["recovery"]["role"] == expected_role
    assert out["recovery"]["gate"] == gate
    # Must NOT fall through to the SE retry ladder.
    assert out["role"] != "se" or expected_role == "se"


@pytest.mark.unit
def test_gate_blocked_beats_retry_blocked_in_priority():
    """When both a gate-blocked and a retry-recoverable story exist, the
    gate-blocked one is surfaced first (its reason names the exact step)."""
    gate = _story("US-1", "blocked", blocked_reason="DoD gate: ui_acceptance has not passed")
    retry = _story("US-2", "blocked", retries={"se": 1})
    out = next_action(_state(gate, retry))
    assert out["story_id"] == "US-1"
    assert out["recovery"]["tier"] == "gate_remediation"


@pytest.mark.unit
def test_recover_blocked_tiebreak_favors_furthest_stage():
    # se/qe tie at 1 retry → tie-break by pipeline position → qe (furthest).
    story = _story("US-1", "blocked", retries={"se": 1, "qe": 1, "cr": 0})
    out = next_action(_state(story))
    assert out["action"] == "recover_blocked"
    assert out["recovery"]["role"] == "qe"
    assert out["recovery"]["tier"] == "retry_augmented_prompt"


@pytest.mark.unit
def test_all_blocked_when_ladder_exhausted():
    # retry cap (2) reached → tier block → no recovery path left.
    story = _story("US-1", "blocked", retries={"se": 2})
    out = next_action(_state(story, _story("US-2", "done")))
    assert out["action"] == "all_blocked"
    assert out["human_gate_pending"] is True


@pytest.mark.unit
def test_sprint_complete_when_all_terminal():
    out = next_action(
        _state(_story("US-1", "done"), _story("US-2", "cancelled"))
    )
    assert out["action"] == "sprint_complete"


@pytest.mark.unit
def test_unknown_substate_degrades_to_all_blocked():
    out = next_action(_state(_story("US-1", "wat")))
    assert out["action"] == "all_blocked"
    assert out["human_gate_pending"] is True


# ── selection-policy invariants ──────────────────────────────────────────────


@pytest.mark.unit
def test_furthest_along_in_flight_drains_first(tmp_path: Path):
    """reviewing beats testing beats in_progress beats queued — finish
    one story before fanning out to the next."""
    out = next_action(
        _state(
            _story("US-4", "queued"),
            _story("US-3", "in_progress"),
            _story("US-2", "testing"),
            _story("US-1", "reviewing"),
            sprint=1,
        ),
        receipts_dir=str(tmp_path),
    )
    assert out["story_id"] == "US-1"


@pytest.mark.unit
def test_fifo_within_a_stage():
    out = next_action(_state(_story("US-7", "queued"), _story("US-3", "queued")))
    assert out["story_id"] == "US-7"  # board order, not id order


@pytest.mark.unit
def test_actionable_work_beats_human_gates():
    """A pending acceptance never starves the rest of the board."""
    out = next_action(
        _state(_story("US-1", "awaiting_acceptance"), _story("US-2", "queued"))
    )
    assert out["action"] == "dispatch_se"
    assert out["story_id"] == "US-2"


# ── crash recovery: receipt present but transition missing ──────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stage", "role", "expected_next"),
    [("in_progress", "se", "testing"), ("testing", "qe", "reviewing")],
)
def test_receipt_present_advances_instead_of_redispatching(
    tmp_path: Path, stage: str, role: str, expected_next: str
):
    _receipt(tmp_path, "US-1", role)
    out = next_action(_state(_story("US-1", stage)), receipts_dir=str(tmp_path))
    assert out["receipt_present"] is True
    assert out["transition_to"] == expected_next


@pytest.mark.unit
def test_missing_receipts_dir_reads_as_absent(tmp_path: Path):
    """receipts_dir=None or nonexistent → dispatch (idempotent), never skip."""
    for rd in (None, str(tmp_path / "nope")):
        out = next_action(_state(_story("US-1", "testing")), receipts_dir=rd)
        assert out["action"] == "dispatch_qe"
        assert out["receipt_present"] is False


def _iso(offset_s: float = 0.0) -> str:
    from datetime import datetime, timezone
    import time

    return (
        datetime.fromtimestamp(time.time() + offset_s, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


@pytest.mark.unit
def test_stale_receipt_redispatches_not_advances(tmp_path: Path):
    """P1b regression guard: a receipt OLDER than the story's entry into its
    current stage — e.g. left behind when reject_story needs-fix reset an
    accepted story back to in_progress — is stale. next_action must
    re-dispatch (redo the work), not advance past it on the old receipt.

    Freshness is `completed_at` vs stage `entered_at`, the same clock
    evaluate_dispatch uses. mtime is not consulted.
    """
    _receipt(tmp_path, "US-1", "se", completed_at="2000-01-01T00:00:00Z")
    story = _story(
        "US-1", "in_progress",
        pipeline_log=[{"state": "in_progress", "entered_at": _iso()}],
    )
    out = next_action(_state(story), receipts_dir=str(tmp_path))
    assert out["action"] == "dispatch_se"
    assert out["receipt_present"] is False


@pytest.mark.unit
def test_fresh_receipt_after_stage_entry_advances(tmp_path: Path):
    """Complement: a receipt completed AFTER the current stage entry is a
    genuine crash-recovery signal → advance, don't re-dispatch."""
    story = _story(
        "US-1", "testing",
        pipeline_log=[{"state": "testing", "entered_at": _iso(-100)}],
    )
    _receipt(tmp_path, "US-1", "qe")
    out = next_action(_state(story), receipts_dir=str(tmp_path))
    assert out["receipt_present"] is True
    assert out["transition_to"] == "reviewing"


@pytest.mark.unit
def test_fresh_receipt_same_utc_second_is_contemporaneous(tmp_path: Path):
    """Cursor receipts often omit fractional seconds. Same UTC second as
    stage entry is this attempt, not a leftover from a prior one."""
    story = _story(
        "US-1", "testing",
        pipeline_log=[{
            "state": "testing",
            "entered_at": "2026-08-24T16:52:34.647295Z",
        }],
    )
    _receipt(tmp_path, "US-1", "qe", completed_at="2026-08-24T16:52:34Z")
    out = next_action(_state(story), receipts_dir=str(tmp_path))
    assert out["receipt_present"] is True
    assert out["transition_to"] == "reviewing"


@pytest.mark.unit
def test_freshness_uses_completed_at_not_mtime(tmp_path: Path):
    """Wedge guard: an old mtime must not disagree with a fresh completed_at.

    Before this, next_action used mtime while evaluate_dispatch used
    completed_at, so dispatch refused with 'call advance' while next_action
    still wanted a re-dispatch.
    """
    import time

    story = _story(
        "US-1", "testing",
        pipeline_log=[{"state": "testing", "entered_at": _iso(-100)}],
    )
    _receipt(tmp_path, "US-1", "qe", completed_at=_iso())
    old = time.time() - 10_000
    os.utime(tmp_path / "US-1-qe.json", (old, old))
    out = next_action(_state(story), receipts_dir=str(tmp_path))
    assert out["receipt_present"] is True
    assert out["transition_to"] == "reviewing"


@pytest.mark.unit
def test_malformed_receipt_treated_as_absent(tmp_path: Path):
    """A fresh-but-unparseable receipt must NOT count as stage-complete:
    collect_story_receipts (what DoD reads) skips malformed files, so
    advancing on one would recreate the dispatch/DoD disagreement. Re-
    dispatch instead."""
    (tmp_path / "US-1-se.json").write_text("{ this is not valid json")
    story = _story(
        "US-1", "in_progress",
        pipeline_log=[{"state": "in_progress", "entered_at": _iso(-100)}],
    )
    out = next_action(_state(story), receipts_dir=str(tmp_path))
    assert out["action"] == "dispatch_se"
    assert out["receipt_present"] is False


@pytest.mark.unit
def test_non_object_receipt_treated_as_absent(tmp_path: Path):
    """A receipt that parses but isn't a JSON object (a bare list) is not a
    valid receipt → absent."""
    (tmp_path / "US-1-qe.json").write_text("[1, 2, 3]")
    story = _story(
        "US-1", "testing",
        pipeline_log=[{"state": "testing", "entered_at": _iso(-100)}],
    )
    out = next_action(_state(story), receipts_dir=str(tmp_path))
    assert out["action"] == "dispatch_qe"
    assert out["receipt_present"] is False


# ── enum + counts contract ───────────────────────────────────────────────────


@pytest.mark.unit
def test_every_returned_action_is_in_the_enum(tmp_path: Path):
    boards = [
        _state(),
        _state(_story("A", "queued")),
        _state(_story("A", "testing")),
        _state(_story("A", "reviewing"), sprint=2),
        _state(_story("A", "awaiting_acceptance")),
        _state(_story("A", "blocked")),
        _state(_story("A", "done")),
    ]
    for board in boards:
        assert next_action(board, receipts_dir=str(tmp_path))["action"] in NEXT_ACTIONS


@pytest.mark.unit
def test_not_in_execution_is_in_the_action_contract():
    """`not_in_execution` is wrapper-only, but issue #77 lists it in the
    action enum and consumers/contract tests key off NEXT_ACTIONS — so it
    must be part of the shared contract, not just a wrapper string."""
    assert "not_in_execution" in NEXT_ACTIONS


@pytest.mark.unit
def test_counts_cover_every_story_state():
    out = next_action(_state(_story("A", "queued"), _story("B", "done")))
    assert out["counts"]["queued"] == 1
    assert out["counts"]["done"] == 1
    assert sum(out["counts"].values()) == 2


# ── progress_digest ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_digest_stable_and_order_independent():
    a = _state(_story("A", "queued"), _story("B", "testing"))
    b = _state(_story("B", "testing"), _story("A", "queued"))
    assert progress_digest(a) == progress_digest(b)
    assert len(progress_digest(a)) == 16


@pytest.mark.unit
def test_digest_changes_on_state_transition():
    before = _state(_story("A", "queued"))
    after = _state(_story("A", "in_progress"))
    assert progress_digest(before) != progress_digest(after)


@pytest.mark.unit
def test_digest_changes_on_retry_recorded():
    before = _state(_story("A", "blocked"))
    after = _state(_story("A", "blocked", retries={"se": 1}))
    assert progress_digest(before) != progress_digest(after)


@pytest.mark.unit
def test_digest_changes_on_dod_none_to_failed():
    """P2a: never-evaluated (no dod) → evaluated-and-failed ({passed:false})
    IS progress (a gate ran). bool(None)==bool(False) would hide it; the
    tri-state verdict must distinguish them."""
    before = _state(_story("A", "reviewing"))
    after = _state(_story("A", "reviewing", dod={"passed": False}))
    assert progress_digest(before) != progress_digest(after)


@pytest.mark.unit
def test_digest_distinguishes_fail_from_pass():
    fail = _state(_story("A", "done", dod={"passed": False}))
    ok = _state(_story("A", "done", dod={"passed": True}))
    assert progress_digest(fail) != progress_digest(ok)


@pytest.mark.unit
def test_digest_ignores_non_progress_fields():
    """Title edits / cosmetic fields must not read as progress."""
    a = _state(_story("A", "queued"))
    b = _state({**_story("A", "queued"), "title": "renamed", "notes": "x"})
    assert progress_digest(a) == progress_digest(b)


# ── multi-spec routing through the state-machine wrapper ────────────────────


@pytest.mark.unit
def test_wrapper_routes_active_spec(tmp_path: Path, monkeypatch):
    """scrum_state_machine.next_action reads the spec named by
    SYNAPTORY_ACTIVE_SPEC — the loop must never dispatch another spec's
    stories."""
    import scrum_state_machine as ssm

    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    state = {
        "version": "3.0",
        "build_mode": "scrum",
        "active_spec": "backend",
        "specs": {
            "backend": {
                "lifecycle_state": "SPRINT_EXECUTION",
                "current_sprint": 1,
                "current_stories": [_story("BE-1", "queued")],
            },
            "frontend": {
                "lifecycle_state": "SPRINT_EXECUTION",
                "current_sprint": 1,
                "current_stories": [_story("FE-1", "testing")],
            },
        },
    }
    (orch / "pipeline-state.json").write_text(json.dumps(state))

    monkeypatch.setenv("SYNAPTORY_ACTIVE_SPEC", "backend")
    out = ssm.next_action(str(tmp_path))
    assert out["story_id"] == "BE-1"
    assert out["spec_id"] == "backend"

    monkeypatch.setenv("SYNAPTORY_ACTIVE_SPEC", "frontend")
    out = ssm.next_action(str(tmp_path))
    assert out["story_id"] == "FE-1"
    assert out["action"] == "dispatch_qe"


@pytest.mark.unit
def test_wrapper_not_in_execution(tmp_path: Path):
    import scrum_state_machine as ssm

    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "pipeline-state.json").write_text(
        json.dumps(
            {
                "version": "2.0",
                "build_mode": "scrum",
                "lifecycle_state": "SPRINT_REVIEW",
                "current_sprint": 1,
                "current_stories": [_story("US-1", "queued")],
            }
        )
    )
    out = ssm.next_action(str(tmp_path))
    assert out["action"] == "not_in_execution"
    assert out["action"] in NEXT_ACTIONS  # wrapper action must be in the contract
    assert out["lifecycle_state"] == "SPRINT_REVIEW"


@pytest.mark.unit
def test_kanban_wrapper_execution_gate(tmp_path: Path):
    import kanban_state_machine as ksm

    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "pipeline-state.json").write_text(
        json.dumps(
            {
                "version": "2.0",
                "build_mode": "kanban",
                "lifecycle_state": "EXECUTION",
                "cumulative_ticket_number": 1,
                "current_stories": [_story("T-1", "in_progress")],
            }
        )
    )
    out = ksm.next_action(str(tmp_path))
    assert out["action"] == "dispatch_se"
    assert out["story_id"] == "T-1"


# ── record_retry CLI verb (recover_blocked step) ─────────────────────────────


def _write_state_file(tmp_path: Path, story: dict) -> Path:
    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "pipeline-state.json").write_text(
        json.dumps(
            {
                "version": "2.0",
                "build_mode": "scrum",
                "lifecycle_state": "SPRINT_EXECUTION",
                "current_sprint": 2,
                "current_stories": [story],
            }
        )
    )
    return tmp_path


@pytest.mark.unit
def test_record_retry_cli_verb_increments_and_escalates(tmp_path: Path):
    """Regression (PR #84 re-review): modes/sprint.md's recover_blocked step
    tells the orchestrator to `story_pipeline.py record_retry ...` before
    re-dispatching, so the verb MUST exist and drive the H3-F1 ladder. It was
    a Python function with no CLI action → 'Unknown action: record_retry'."""
    import subprocess
    import sys

    proj = _write_state_file(
        tmp_path, _story("US-1", "blocked", blocked_reason="se failed", pipeline_log=[])
    )
    sp = str(Path(__file__).resolve().parents[2] / "hooks" / "lib" / "story_pipeline.py")

    def _record(reason: str) -> dict:
        r = subprocess.run(
            [sys.executable, sp, "record_retry", str(proj), "US-1", "se", reason],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 0, f"record_retry failed: {r.stderr}"
        assert "Unknown action" not in r.stderr
        return json.loads(r.stdout)

    first = _record("verification failed")
    assert first["retry_count"] == 1
    assert first["recovery"]["tier"] == "retry_augmented_prompt"

    second = _record("verification failed again")
    assert second["retry_count"] == 2
    assert second["recovery"]["tier"] == "block"  # ladder exhausted → block


# ── P5 verification loops (strict + config-gated, default off) ────────────────

from story_pipeline import verification_loops_active  # noqa: E402


def _failed(receipts_dir: Path, sid: str, role: str) -> None:
    """A fresh receipt whose verification FAILED (non-zero exit / not complete)."""
    receipts_dir.mkdir(parents=True, exist_ok=True)
    if role == "cr":
        body = {
            "task": "t",
            "status": "changes-requested",
            "completed_at": "2099-01-01T00:00:00Z",
        }
    else:  # se/qe → build_succeeds / tests_pass
        body = {
            "task": "t",
            "verification_commands": [{"command": "x", "exit_code": 1}],
            "completed_at": "2099-01-01T00:00:00Z",
        }
    (receipts_dir / f"{sid}-{role}.json").write_text(json.dumps(body))


def _testing_story(sid: str = "US-1") -> dict:
    return _story(sid, "testing",
                  pipeline_log=[{"state": "testing", "entered_at": _iso(-100)}])


@pytest.mark.unit
def test_vloop_off_advances_on_failed_receipt(tmp_path: Path):
    """Default (verification_loops=False): a failed QE receipt still advances —
    the DoD gate catches it later (no P1 regression)."""
    _failed(tmp_path, "US-1", "qe")
    out = next_action(_state(_testing_story()), receipts_dir=str(tmp_path),
                      verification_loops=False)
    assert out["action"] == "dispatch_qe"
    assert out["receipt_present"] is True
    assert out["transition_to"] == "reviewing"
    assert out["recovery"] is None


@pytest.mark.unit
def test_vloop_on_redispatches_failed_qe_receipt(tmp_path: Path):
    """verification_loops=True: a failed QE receipt re-dispatches QE (loop),
    carrying a recovery tier — instead of advancing on the failed receipt."""
    _failed(tmp_path, "US-1", "qe")
    out = next_action(_state(_testing_story()), receipts_dir=str(tmp_path),
                      verification_loops=True)
    assert out["action"] == "dispatch_qe"
    assert out["recovery"]["tier"] == "retry_same_prompt"
    assert out["transition_to"] is None  # not advancing


@pytest.mark.unit
def test_vloop_on_redispatches_failed_cr_receipt(tmp_path: Path):
    _failed(tmp_path, "US-1", "cr")
    story = _story("US-1", "reviewing",
                   pipeline_log=[{"state": "reviewing", "entered_at": _iso(-100)}])
    out = next_action(_state(story, sprint=2), receipts_dir=str(tmp_path),
                      verification_loops=True)
    assert out["action"] == "dispatch_cr"
    assert out["recovery"]["tier"] == "retry_same_prompt"


@pytest.mark.unit
def test_vloop_on_passing_receipt_still_advances(tmp_path: Path):
    """A PASSING receipt advances even with verification_loops on — only
    definitive failures loop."""
    receipts = tmp_path
    receipts.mkdir(parents=True, exist_ok=True)
    (receipts / "US-1-qe.json").write_text(
        json.dumps({
            "task": "t",
            "verification_commands": [{"command": "x", "exit_code": 0}],
            "completed_at": "2099-01-01T00:00:00Z",
        })
    )
    out = next_action(_state(_testing_story()), receipts_dir=str(receipts),
                      verification_loops=True)
    assert out["transition_to"] == "reviewing"
    assert out["recovery"] is None


@pytest.mark.unit
def test_vloop_ladder_exhausted_blocks_story(tmp_path: Path):
    """When the H3-F1 ladder is exhausted (block tier), the loop BLOCKS the
    story and moves on (#81) — it must NOT advance a known-failed receipt
    into a wasted CR/DoD cycle."""
    _failed(tmp_path, "US-1", "qe")
    story = _testing_story()
    story["retries"] = {"qe": 2}  # cap reached → recommend_recovery_action → block
    out = next_action(_state(story), receipts_dir=str(tmp_path), verification_loops=True)
    assert out["action"] == "block_story"
    assert out["transition_to"] == "blocked"
    assert out["recovery"]["tier"] == "block"


@pytest.mark.unit
def test_vloop_degenerate_hash_blocks_story(tmp_path: Path):
    """BEA5-F1: a repeated identical failure hash escalates to block even
    below the cap → block the story, don't advance."""
    _failed(tmp_path, "US-1", "qe")
    story = _testing_story()
    story["retries"] = {"qe": 1}
    story["retry_failure_hashes"] = {"qe": ["deadbeefdeadbeef", "deadbeefdeadbeef"]}
    out = next_action(_state(story), receipts_dir=str(tmp_path), verification_loops=True)
    assert out["action"] == "block_story"
    assert out["transition_to"] == "blocked"


@pytest.mark.unit
def test_block_story_in_action_contract():
    assert "block_story" in NEXT_ACTIONS
    from story_pipeline import CONTINUE_ELIGIBLE
    assert "block_story" in CONTINUE_ELIGIBLE  # board-changing → loop continues


@pytest.mark.unit
def test_verification_loops_active_gate(tmp_path: Path):
    """The config+mode gate. #179 E2 (verifier-first coupling): explicit
    enabled/disabled are honoured; absent key / `auto` follows
    loop_continuation (whose own default is ON) — an autonomous loop must
    not be more autonomous than its verifier is strong."""
    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)

    def _set(quality: str, cfg: str):
        (orch / "settings.md").write_text(f"Engagement: structured\nQuality-Enforcement: {quality}\n")
        (tmp_path / ".synaptory.yaml").write_text(cfg)

    _set("strict", "resilience:\n  verification_loops: enabled\n")
    assert verification_loops_active(tmp_path) is True
    _set("strict", "resilience:\n  verification_loops: disabled\n")
    assert verification_loops_active(tmp_path) is False  # explicit opt-out respected
    _set("standard", "resilience:\n  verification_loops: enabled\n")  # not strict
    assert verification_loops_active(tmp_path) is False

    # #179 E2 — absent / auto couples to loop_continuation:
    _set("strict", "project_id: t\n")  # both keys absent → continuation on → loops on
    assert verification_loops_active(tmp_path) is True
    _set("strict", "resilience:\n  verification_loops: auto\n")
    assert verification_loops_active(tmp_path) is True
    _set("strict", "resilience:\n  loop_continuation: disabled\n")
    assert verification_loops_active(tmp_path) is False  # continuation off → coupled off
    _set("strict", "resilience:\n  loop_continuation: disabled\n  verification_loops: enabled\n")
    assert verification_loops_active(tmp_path) is True  # explicit enable beats coupling
    _set("standard", "project_id: t\n")  # coupling never bypasses strict
    assert verification_loops_active(tmp_path) is False


# ── #179 E6a — unverifiable receipts loop at mature/release tiers ─────────────


def _unverifiable(receipts_dir: Path, sid: str, role: str) -> None:
    """A fresh receipt whose evidence is INTENT, not proof (#106): plain-string
    verification_commands → _evaluate_check returns None."""
    receipts_dir.mkdir(parents=True, exist_ok=True)
    (receipts_dir / f"{sid}-{role}.json").write_text(
        json.dumps({
            "task": "t",
            "verification_commands": ["pytest -q"],
            "completed_at": "2099-01-01T00:00:00Z",
        })
    )


@pytest.mark.unit
def test_vloop_unverified_advances_at_early_tier(tmp_path: Path):
    """At early/growing tiers an unverifiable receipt still advances (the DoD
    gate remains the catcher) — E6a only tightens the high-assurance tiers."""
    _unverifiable(tmp_path, "US-1", "qe")
    out = next_action(_state(_testing_story()), receipts_dir=str(tmp_path),
                      verification_loops=True)
    assert out["transition_to"] == "reviewing"
    assert out["recovery"] is None


@pytest.mark.unit
def test_vloop_unverified_loops_at_mature_tier(tmp_path: Path):
    """#179 E6a: at mature/release the DoD gate would reject unverifiable
    evidence anyway — loop at the stage instead of one DoD cycle later."""
    _unverifiable(tmp_path, "US-1", "qe")
    out = next_action(_state(_testing_story()), receipts_dir=str(tmp_path),
                      verification_loops=True,
                      dod_tier_info={"tier": "mature", "tier_source": "planned"})
    assert out["action"] == "dispatch_qe"
    assert out["recovery"]["tier"] == "retry_same_prompt"
    assert out["recovery"]["verdict"] == "unverified"
    assert "unverifiable" in out["reason"]
    assert out["transition_to"] is None  # not advancing


@pytest.mark.unit
def test_vloop_failed_verdict_labelled(tmp_path: Path):
    """A hard failure keeps the 'failed verification' reason wording and
    carries verdict=failed on the recovery dict."""
    _failed(tmp_path, "US-1", "qe")
    out = next_action(_state(_testing_story()), receipts_dir=str(tmp_path),
                      verification_loops=True)
    assert out["recovery"]["verdict"] == "failed"
    assert "failed verification" in out["reason"]


# ── #179 E5 — gate evidence digest on human gates ─────────────────────────────


@pytest.mark.unit
def test_await_acceptance_carries_evidence_digest(tmp_path: Path):
    """The PO walk sees run data: commands + exit codes, artifacts, and
    structured proof objects from the story's receipts."""
    (tmp_path / "US-1-qe.json").write_text(json.dumps({
        "task": "t", "agent": "quality-engineer",
        "verification_commands": [
            {"command": "pytest -q", "exit_code": 0},
            "manual smoke",  # string = intent → excluded from the digest
        ],
        "artifacts": ["tests/test_x.py"],
        "metrics": {"runtime_verification": {"deployed": True, "logs_inspected": True}},
    }))
    story = _story("US-1", "awaiting_acceptance", pipeline_log=[])
    out = next_action(_state(story), receipts_dir=str(tmp_path),
                      per_story_acceptance=True)
    assert out["action"] == "await_acceptance"
    (entry,) = out["evidence"]
    assert entry["story_id"] == "US-1"
    (receipt,) = entry["receipts"]
    assert receipt["role"] == "quality-engineer"
    assert receipt["commands"] == [{"command": "pytest -q", "exit_code": 0}]
    assert receipt["artifacts"] == ["tests/test_x.py"]
    assert receipt["runtime_verification"] == {"deployed": True, "logs_inspected": True}


@pytest.mark.unit
def test_all_blocked_carries_evidence_digest(tmp_path: Path):
    """all_blocked surfaces each blocked story's reason + DoD verdict so the
    human decision starts from evidence, not just a board state."""
    story = _story("US-1", "blocked", blocked_reason="DoD gate: runtime_verified",
                   pipeline_log=[])
    story["retries"] = {"qe": 2}
    story["dod"] = {"passed": False,
                    "checks": {"tests_pass": {"required": True, "passed": False}}}
    # gate-blocked stories route to remediation first; exhaust that path by
    # removing the gate marker so the board is terminally blocked.
    story["blocked_reason"] = "qe retry ladder exhausted"
    story["retry_failure_hashes"] = {"qe": ["aaaa", "aaaa"]}
    out = next_action(_state(story), receipts_dir=str(tmp_path))
    assert out["action"] == "all_blocked"
    (entry,) = out["evidence"]
    assert entry["blocked_reason"] == "qe retry ladder exhausted"
    assert entry["dod_passed"] is False
    assert entry["dod_failed_checks"] == ["tests_pass"]


@pytest.mark.unit
def test_inconsistent_board_all_blocked_carries_evidence(tmp_path: Path):
    """#179 E5 — the inconsistent-board fallback (unknown sub-state) is also
    an all_blocked human gate, so it must carry an evidence digest too."""
    story = _story("US-1", "in_limbo")  # not in STORY_STATES
    out = next_action(_state(story), receipts_dir=str(tmp_path))
    assert out["action"] == "all_blocked"
    assert out["human_gate_pending"] is True
    (entry,) = out["evidence"]
    assert entry["story_id"] == "US-1"
    assert entry["state"] == "in_limbo"


# ---------------------------------------------------------------------------
# Wrapper argument parity — the parallelism / DoD-tier seam
#
# `modes/sprint.md` and `modes/kanban.md` drive their loops through the
# lifecycle wrappers, NOT through story_pipeline.py's own CLI. The wrappers
# used to call next_action with three of its five toggles, so
# `_attach_parallel_batch` bailed out on `not parallelism` and
# `parallel.eligible` could never be true on the primary path — story
# parallelism was unreachable however the project was configured — while a
# DoD tier pinned at Sprint Planning was discarded and silently recomputed.
# These tests pin the parity so the seam cannot regress.
# ---------------------------------------------------------------------------


def _exec_project(
    tmp_path: Path,
    *stories: dict,
    config: str = "",
    build_mode: str = "scrum",
    **state_kw,
) -> str:
    """A project dir whose board is mid-execution, with an optional config."""
    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True, exist_ok=True)
    lifecycle = "SPRINT_EXECUTION" if build_mode == "scrum" else "EXECUTION"
    state = {
        "version": "2.0",
        "build_mode": build_mode,
        "lifecycle_state": lifecycle,
        "current_sprint": 1,
        "current_stories": list(stories),
        **state_kw,
    }
    (orch / "pipeline-state.json").write_text(json.dumps(state))
    if config:
        (tmp_path / ".synaptory.yaml").write_text(config)
    return str(tmp_path)


_PARALLELISM_ON = (
    "parallelism:\n"
    "  story_parallelism: enabled\n"
    "  max_concurrent_subagents: 3\n"
    "  isolation: worktree\n"
)


@pytest.mark.unit
def test_scrum_wrapper_attaches_parallel_batch_when_enabled(tmp_path: Path):
    import scrum_state_machine as ssm

    project = _exec_project(
        tmp_path,
        _story("US-1", "queued"),
        _story("US-2", "queued"),
        config=_PARALLELISM_ON,
    )
    out = ssm.next_action(project)

    assert out["action"] == "dispatch_se" and out["story_id"] == "US-1"
    par = out["parallel"]
    assert par["eligible"] is True
    # batch[0] is ALWAYS the serial action, so an orchestrator that ignores
    # the block behaves exactly as before. The invariant is the DISPATCH
    # IDENTITY, not the absence of other keys: each member also carries its own
    # per-story `dod` contract, and for the lead that block must equal the
    # top-level one or the two would name different contracts for one dispatch.
    lead = par["batch"][0]
    assert {k: lead[k] for k in ("story_id", "action", "role")} == {
        "story_id": "US-1", "action": "dispatch_se", "role": "se",
    }
    assert lead["dod"]["active_checks"] == out["dod"]["active_checks"]
    assert lead["dod"]["tier"] == out["dod"]["tier"]
    assert [b["story_id"] for b in par["batch"]] == ["US-1", "US-2"]
    assert par["max_concurrent"] == 3
    assert par["isolation"] == "worktree"


@pytest.mark.unit
def test_scrum_wrapper_omits_parallel_block_when_unset(tmp_path: Path):
    """Absent config keeps parallelism OFF (back-compat posture), so the
    additive block must not appear at all."""
    import scrum_state_machine as ssm

    project = _exec_project(
        tmp_path, _story("US-1", "queued"), _story("US-2", "queued")
    )
    out = ssm.next_action(project)

    assert out["action"] == "dispatch_se"
    assert "parallel" not in out


@pytest.mark.unit
def test_kanban_wrapper_attaches_parallel_batch_when_enabled(tmp_path: Path):
    import kanban_state_machine as ksm

    project = _exec_project(
        tmp_path,
        _story("KB-1", "queued"),
        _story("KB-2", "queued"),
        config=_PARALLELISM_ON,
        build_mode="kanban",
    )
    out = ksm.next_action(project)

    assert out["action"] == "dispatch_se" and out["story_id"] == "KB-1"
    assert out["parallel"]["eligible"] is True
    assert [b["story_id"] for b in out["parallel"]["batch"]] == ["KB-1", "KB-2"]


@pytest.mark.unit
def test_kanban_wrapper_omits_parallel_block_when_unset(tmp_path: Path):
    import kanban_state_machine as ksm

    project = _exec_project(
        tmp_path,
        _story("KB-1", "queued"),
        _story("KB-2", "queued"),
        build_mode="kanban",
    )
    out = ksm.next_action(project)

    assert out["action"] == "dispatch_se"
    assert "parallel" not in out


@pytest.mark.unit
def test_scrum_wrapper_honours_planned_dod_tier(tmp_path: Path):
    """A tier recorded at Sprint Planning must survive the wrapper. Sprint 1
    computes to `early`, so a recorded `mature` proves the record won and
    `tier_source` is not the silent-promotion tell."""
    import scrum_state_machine as ssm

    project = _exec_project(
        tmp_path,
        _story("US-1", "queued"),
        dod_tier={
            "tier": "mature",
            "decided_by": "lead@h3t.co",
            "decided_at": "2026-01-01T00:00:00Z",
            "sprint": 1,
        },
    )
    out = ssm.next_action(project)

    assert out["dod"]["tier"] == "mature"
    assert out["dod"]["tier_source"] == "planned"
    assert "coverage_no_decrease" in out["dod"]["active_checks"]


@pytest.mark.unit
def test_scrum_wrapper_dod_tier_falls_back_to_computed(tmp_path: Path):
    """No recorded decision and no config override reproduces the previous
    behaviour exactly, so this change is additive."""
    import scrum_state_machine as ssm

    project = _exec_project(tmp_path, _story("US-1", "queued"))
    out = ssm.next_action(project)

    assert out["dod"]["tier_source"] == "computed"
    assert out["dod"]["tier"] == "early"


@pytest.mark.unit
def test_scrum_wrapper_dod_tier_config_override(tmp_path: Path):
    """`quality.dod_tier` in .synaptory.yaml reaches the wrapper too."""
    import scrum_state_machine as ssm

    project = _exec_project(
        tmp_path,
        _story("US-1", "queued"),
        config="quality:\n  dod_tier: growing\n",
    )
    out = ssm.next_action(project)

    assert out["dod"]["tier"] == "growing"
    assert out["dod"]["tier_source"] == "config"


@pytest.mark.unit
def test_wrapper_parallel_block_respects_depends_on(tmp_path: Path):
    """The eligibility rules are story_pipeline's; the wrapper must not
    loosen them. A dependent story stays out of the batch."""
    import scrum_state_machine as ssm

    project = _exec_project(
        tmp_path,
        _story("US-1", "queued"),
        _story("US-2", "queued", depends_on=["US-1"]),
        config=_PARALLELISM_ON,
    )
    out = ssm.next_action(project)

    assert out["parallel"]["eligible"] is False
    assert [b["story_id"] for b in out["parallel"]["batch"]] == ["US-1"]


# ── #304 action contract ────────────────────────────────────────────────────


def test_deps_blocked_is_in_the_action_contract():
    """Every consumer keys off NEXT_ACTIONS; a wrapper action outside it fails
    the contract test."""
    from story_pipeline import CONTINUE_ELIGIBLE

    assert "deps_blocked" in NEXT_ACTIONS
    assert "deps_blocked" not in CONTINUE_ELIGIBLE


# ─── the board does not repeat a promotion the gate refused ─────────────────


def _reviewing_board(refusal: str | None = None, evidence: str = "") -> dict:
    story = {
        "id": "WU-3552",
        "title": "conditional gate",
        "state": "reviewing",
        "pipeline_log": [],
        "receipts": [],
        "labels": [],
        "depends_on": [],
    }
    if refusal:
        story["dod_gate_refusal"] = {
            "reason": refusal,
            "at": "2026-09-03T00:00:00Z",
            "evidence": evidence,
        }
    return {"build_mode": "spq", "current_stories": [story]}


def test_the_first_pass_still_offers_the_promotion(tmp_path):
    """The DoD gate is what should refuse a red unit, with its own typed
    reason, and it cannot do that if the board never offers the edge. This is
    the property the cross-host contract asserts, so the fix for the loop must
    not quietly replace `dod_failed` with a generic mismatch."""
    action = next_action(_reviewing_board(), receipts_dir=str(tmp_path))
    assert action["action"] == "promote_story", action
    assert action["transition_to"] == "done", action


def test_a_refused_promotion_is_not_advertised_again(tmp_path):
    """#396, the deterministic dead end:

        next_action  -> promote_story / done
        advance      -> dod_failed, story stays in reviewing
        next_action  -> promote_story / done

    A compliant orchestrator retries an action that cannot succeed, forever.
    The kernel records why the gate refused, and the board routes to the agent
    that owes the missing result.
    """
    refusal = (
        "DoD gate: user-facing acceptance (ui_acceptance) has not passed"
    )
    action = next_action(_reviewing_board(refusal), receipts_dir=str(tmp_path))

    assert action["action"] != "promote_story", (
        "the board advertised the promotion the gate had just refused: %r" % action
    )
    assert action["action"] == "recover_blocked", action
    assert action.get("transition_to") in (None, ""), action
    assert action["recovery"]["gate"] == "ui_acceptance", action
    # The remediation role, never the SE retry ladder, which cannot clear a
    # conditional gate and would loop in a different way.
    assert action["role"] == action["recovery"]["role"], action
    assert action["role"] != "se", action


def test_an_unrelated_refusal_note_does_not_reroute(tmp_path):
    """A note that names no known gate still routes somewhere deliberate
    rather than back to the promotion."""
    action = next_action(
        _reviewing_board("DoD gate: something nobody mapped"),
        receipts_dir=str(tmp_path),
    )
    assert action["action"] == "recover_blocked", action
    assert action["recovery"]["gate"] == "unknown", action


def test_a_refusal_about_evidence_that_changed_is_ignored(tmp_path):
    """The note is scoped to the evidence it judged, and this is the failure
    mode that matters more than the loop: an engineer who dispatched the
    remediation, produced the missing result and came back would find the board
    still routing to remediation and the advance refused as
    `next_action_mismatch`, with no way to clear it. The Cursor MCP suite
    caught it on a coverage fix (#396).
    """
    from story_pipeline import receipt_evidence_digest

    receipts = tmp_path / "receipts"
    receipts.mkdir()
    (receipts / "WU-3552-cr.json").write_text("{}", encoding="utf-8")
    stale = "sha-of-the-evidence-that-was-refused"
    current = receipt_evidence_digest(str(receipts), "WU-3552")
    assert current and current != stale

    action = next_action(
        _reviewing_board("DoD gate: ui_acceptance has not passed", stale),
        receipts_dir=str(receipts),
    )
    assert action["action"] == "promote_story", (
        "a refusal about evidence that has since changed still blocked the "
        "retry: %r" % action
    )


def test_a_refusal_about_the_same_evidence_still_routes(tmp_path):
    """And it must not become a no-op: the same evidence gets the same
    answer, which is what breaks the loop."""
    from story_pipeline import receipt_evidence_digest

    receipts = tmp_path / "receipts"
    receipts.mkdir()
    (receipts / "WU-3552-cr.json").write_text("{}", encoding="utf-8")
    same = receipt_evidence_digest(str(receipts), "WU-3552")

    action = next_action(
        _reviewing_board("DoD gate: ui_acceptance has not passed", same),
        receipts_dir=str(receipts),
    )
    assert action["action"] == "recover_blocked", action
