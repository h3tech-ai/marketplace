"""Layer 1 - the fail-closed advance kernel (#274).

Ports the advance scenarios that previously existed only inside one host's test
suite, and runs them against all three host policies. The coverage asymmetry
these replace:

  scenario                  Codex MCP  Cursor MCP  Claude
  invalid receipt              yes        yes       yes
  receipt substitution         yes        no        no
  symlink / path escape        yes        no        no
  next_action tamper           yes        no        no
  cross-story receipt          yes        yes       no
  stale receipt                yes        yes       no
  consumed replay              yes        yes       no
  done without DoD             no         yes       yes
  full deterministic loop      yes        no        no

Every one of them now runs once, here, against the shared kernel.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import advance_kernel as ak
import story_pipeline as sp

FUTURE = "2099-01-01T00:00:00Z"
PAST = "2000-01-01T00:00:00Z"
STAGE_ENTERED = "2026-01-01T00:00:00+00:00"


# ── fixtures ─────────────────────────────────────────────────────────────────


def _project(tmp_path: Path, *, baa: bool = False, build_mode: str = "scrum") -> Path:
    orch = tmp_path / ".synaptory" / ".orchestrator"
    (orch / "receipts").mkdir(parents=True, exist_ok=True)
    config = "build_mode: %s\n" % build_mode
    if baa:
        config += "healthcare:\n  baa_enforced: true\n"
    (tmp_path / ".synaptory.yaml").write_text(config, encoding="utf-8")
    return tmp_path


def _seed(
    project: Path,
    story_id: str = "US-001",
    state: str = "testing",
    *,
    entered_at: str = STAGE_ENTERED,
    extra_stories: list | None = None,
    build_mode: str = "scrum",
    sprint: int = 1,
) -> dict:
    # Mirrors create_story's record. The lookup key is "id", not "story_id".
    story = {
        "id": story_id,
        "title": "Seed story",
        "state": state,
        "blocked_reason": None,
        "blocked_from": None,
        "backend": {},
        "pipeline_log": [{"state": state, "entered_at": entered_at, "exited_at": None}],
        "dod": None,
        "receipts": [],
        "acceptance_criteria": [],
        "kind": "",
        "labels": [],
        "depends_on": [],
        "file_scope": [],
        "retries": {},
        "rejection_feedback": [],
    }
    # v2.0 flat layout with lifecycle_state, which the state machines require
    # before they will serve next_action.
    payload = {
        "version": "2.0",
        "build_mode": build_mode,
        "lifecycle_state": "SPRINT_EXECUTION" if build_mode == "scrum" else "EXECUTION",
        "current_sprint": sprint,
        "cumulative_ticket_number": 1,
        "sprint_goal": "seed",
        "pipeline_log": [],
        "current_stories": [story] + list(extra_stories or []),
    }
    path = project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def _receipt(
    project: Path,
    story_id: str = "US-001",
    role: str = "quality-engineer",
    abbrev: str = "qe",
    *,
    completed_at: str = FUTURE,
    backend: str = "claude",
    valid: bool = True,
    **overrides,
) -> Path:
    receipts = project / ".synaptory" / ".orchestrator" / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    src = project / "src" / "foo.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("x\n", encoding="utf-8")
    payload = {
        "story_id": story_id,
        "role": role,
        "backend": backend,
        "model": "opus",
        "artifacts": ["src/foo.py"],
        # #403 — an EXECUTED proof object, not the plain string `"true"` this
        # fixture used to carry. A plain string is a replay instruction, so
        # tests_pass and build_succeeds scored `None` on every story built
        # from this helper, which now declares a criteria gap and blocks the
        # done edge. The receipt a kernel test asserts about should be the
        # shape a real one has.
        "verification_commands": [
            {"command": "pytest -q", "exit_code": 0, "summary": "ok"},
            {"command": "npm run build", "exit_code": 0, "summary": "ok"},
        ],
        "metrics": {"n": 1},
        "completed_at": completed_at,
        "token_usage": {
            "input": 1,
            "output": 1,
            "cache_read": 0,
            "cache_write": 0,
            "stage": "qe-verification",
        },
        "story_dod": {
            "tests_pass": True,
            "build_succeeds": True,
            "no_critical_findings": True,
            "code_reviewed": True,
            "coverage_no_decrease": True,
        },
    }
    if not valid:
        # Match what every existing suite calls "invalid": required fields gone.
        payload.pop("model")
        payload.pop("artifacts")
    payload.update(overrides)
    path = receipts / ("%s-%s.json" % (story_id, abbrev))
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return path


def _policy(**kwargs) -> ak.HostPolicy:
    """A strict policy with next_action binding off unless a test asks for it."""
    kwargs.setdefault("host", "test")
    kwargs.setdefault("require_next_action_match", False)
    return ak.HostPolicy(**kwargs)


# ── happy path ───────────────────────────────────────────────────────────────


def test_valid_receipt_advances(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="testing")
    _receipt(project)
    decision = ak.evaluate_advance(
        str(project), "US-001", "reviewing", policy=_policy()
    )
    assert decision.allowed, decision.reason
    assert decision.code == ak.OK
    assert decision.receipt_digest


def test_execute_advance_writes_state_and_ledger(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="testing")
    _receipt(project)
    decision = ak.execute_advance(
        str(project), "US-001", "reviewing", policy=_policy()
    )
    assert decision.allowed, decision.reason
    story = sp.get_story(sp._read_state(str(project)), "US-001")
    assert story["state"] == "reviewing"
    assert decision.receipt_digest in story["mcp_consumed_receipts"]


def test_full_se_qe_cr_loop_is_receipt_gated(tmp_path):
    """The deterministic loop only Codex covered: each stage needs its own receipt.

    Sprint 2 is growing intensity, so reviewing -> done still binds a CR receipt.
    Sprint 1 (early) waives that binding — see test_early_reviewing_to_done_waives_cr.
    """
    project = _project(tmp_path)
    _seed(project, state="in_progress", sprint=2)
    stages = [
        ("testing", "software-engineer", "se"),
        ("reviewing", "quality-engineer", "qe"),
        ("done", "code-reviewer", "cr"),
    ]
    for target, role, abbrev in stages:
        # Refused before the receipt for this stage exists.
        refused = ak.evaluate_advance(
            str(project), "US-001", target, policy=_policy()
        )
        assert not refused.allowed
        assert refused.code == ak.NO_RECEIPT

        _receipt(project, role=role, abbrev=abbrev)
        ok = ak.execute_advance(str(project), "US-001", target, policy=_policy())
        assert ok.allowed, "%s: %s" % (target, ok.reason)

    story = sp.get_story(sp._read_state(str(project)), "US-001")
    assert story["state"] == "done"
    assert len(story["mcp_consumed_receipts"]) == 3


# ── receipt identity ─────────────────────────────────────────────────────────


def test_missing_receipt_refused(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="testing")
    decision = ak.evaluate_advance(
        str(project), "US-001", "reviewing", policy=_policy()
    )
    assert not decision.allowed
    assert decision.code == ak.NO_RECEIPT


def test_invalid_receipt_refused(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="testing")
    _receipt(project, valid=False)
    decision = ak.evaluate_advance(
        str(project), "US-001", "reviewing", policy=_policy()
    )
    assert not decision.allowed
    assert decision.code == ak.RECEIPT_INVALID


def test_cross_story_receipt_refused(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="testing")
    _receipt(project, story_id="US-001", abbrev="qe")
    # Same file, but its payload names a different story.
    path = project / ".synaptory" / ".orchestrator" / "receipts" / "US-001-qe.json"
    payload = json.loads(path.read_text())
    payload["story_id"] = "US-999"
    path.write_text(json.dumps(payload), encoding="utf-8")
    decision = ak.evaluate_advance(
        str(project), "US-001", "reviewing", policy=_policy()
    )
    assert not decision.allowed
    assert decision.code == ak.STORY_MISMATCH


def test_wrong_stage_role_refused(tmp_path):
    """An SE receipt cannot satisfy testing -> reviewing, which is bound to QE."""
    project = _project(tmp_path)
    _seed(project, state="testing")
    _receipt(project, role="software-engineer", abbrev="qe")
    decision = ak.evaluate_advance(
        str(project), "US-001", "reviewing", policy=_policy()
    )
    assert not decision.allowed
    assert decision.code == ak.ROLE_MISMATCH


def test_receipt_substitution_refused(tmp_path):
    """A caller may not point the gate at a different file it happens to like."""
    project = _project(tmp_path)
    _seed(project, state="testing")
    _receipt(project)
    decoy = _receipt(project, story_id="US-002", abbrev="qe")
    decision = ak.evaluate_advance(
        str(project),
        "US-001",
        "reviewing",
        receipt_path=str(decoy),
        policy=_policy(),
    )
    assert not decision.allowed
    assert decision.code == ak.PATH_NOT_CANONICAL


def test_receipt_path_escaping_project_refused(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="testing")
    _receipt(project)
    outside = tmp_path.parent / "evil.json"
    outside.write_text("{}", encoding="utf-8")
    decision = ak.evaluate_advance(
        str(project),
        "US-001",
        "reviewing",
        receipt_path=str(outside),
        policy=_policy(),
    )
    assert not decision.allowed
    assert decision.code == ak.PATH_ESCAPE


def test_symlinked_receipt_refused(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="testing")
    target = tmp_path.parent / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    link = project / ".synaptory" / ".orchestrator" / "receipts" / "US-001-qe.json"
    link.symlink_to(target)
    decision = ak.evaluate_advance(
        str(project), "US-001", "reviewing", policy=_policy()
    )
    assert not decision.allowed
    assert decision.code == ak.PATH_SYMLINK


# ── freshness and replay ─────────────────────────────────────────────────────


def test_stale_receipt_refused(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="testing")
    _receipt(project, completed_at=PAST)
    decision = ak.evaluate_advance(
        str(project), "US-001", "reviewing", policy=_policy()
    )
    assert not decision.allowed
    assert decision.code == ak.STALE_RECEIPT


def test_same_second_receipt_is_fresh(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="testing", entered_at="2026-08-24T16:52:34.647295Z")
    _receipt(project, completed_at="2026-08-24T16:52:34Z")
    decision = ak.evaluate_advance(
        str(project), "US-001", "reviewing", policy=_policy()
    )
    assert decision.allowed


def test_missing_entered_at_fails_closed(tmp_path):
    project = _project(tmp_path)
    payload = _seed(project, state="testing")
    payload["current_stories"][0]["pipeline_log"] = []
    (project / ".synaptory" / ".orchestrator" / "pipeline-state.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    _receipt(project)
    decision = ak.evaluate_advance(
        str(project), "US-001", "reviewing", policy=_policy()
    )
    assert not decision.allowed
    assert decision.code == ak.ENTERED_AT_MISSING


def test_missing_entered_at_allowed_when_policy_opts_in(tmp_path):
    project = _project(tmp_path)
    payload = _seed(project, state="testing")
    payload["current_stories"][0]["pipeline_log"] = []
    (project / ".synaptory" / ".orchestrator" / "pipeline-state.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    _receipt(project)
    decision = ak.evaluate_advance(
        str(project),
        "US-001",
        "reviewing",
        policy=_policy(entered_at_missing="allow"),
    )
    assert decision.allowed
    assert any("staleness check skipped" in w for w in decision.warnings)


def test_consumed_receipt_replay_refused(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="testing")
    _receipt(project)
    first = ak.execute_advance(str(project), "US-001", "reviewing", policy=_policy())
    assert first.allowed

    # Rewind the story to the same stage; the receipt file is untouched.
    state = sp._read_state(str(project))
    story = sp.get_story(state, "US-001")
    story["state"] = "testing"
    story["pipeline_log"] = [
        {"state": "testing", "entered_at": STAGE_ENTERED, "exited_at": None}
    ]
    sp._write_state(str(project), state)

    replay = ak.evaluate_advance(
        str(project), "US-001", "reviewing", policy=_policy()
    )
    assert not replay.allowed
    assert replay.code == ak.REPLAY


def test_non_list_ledger_refused_not_silently_reset(tmp_path):
    """Cursor silently reset a corrupt ledger to [], discarding the replay guard."""
    project = _project(tmp_path)
    payload = _seed(project, state="testing")
    payload["current_stories"][0]["mcp_consumed_receipts"] = "not-a-list"
    (project / ".synaptory" / ".orchestrator" / "pipeline-state.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    _receipt(project)
    decision = ak.evaluate_advance(
        str(project), "US-001", "reviewing", policy=_policy()
    )
    assert not decision.allowed
    assert decision.code == ak.LEDGER_INVALID


# ── transition legality and next_action binding ──────────────────────────────


def test_illegal_transition_refused(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="queued")
    decision = ak.evaluate_advance(str(project), "US-001", "done", policy=_policy())
    assert not decision.allowed
    assert decision.code == ak.ILLEGAL_TRANSITION


def test_unknown_story_refused(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="testing")
    decision = ak.evaluate_advance(str(project), "US-404", "reviewing", policy=_policy())
    assert not decision.allowed
    assert decision.code == ak.STORY_NOT_FOUND


def test_next_action_mismatch_refused_when_bound(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="testing")
    _receipt(project)
    decision = ak.evaluate_advance(
        str(project),
        "US-001",
        "blocked",
        reason="manual",
        policy=_policy(require_next_action_match=True),
    )
    assert not decision.allowed
    assert decision.code == ak.NEXT_ACTION_MISMATCH


# ── host policy differences ──────────────────────────────────────────────────


def test_codex_policy_requires_codex_backend(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="testing")
    _receipt(project, backend="claude")
    policy = ak.policy_for_codex()
    policy.require_next_action_match = False
    decision = ak.evaluate_advance(
        str(project), "US-001", "reviewing", policy=policy
    )
    assert not decision.allowed
    assert decision.code == ak.BACKEND_MISMATCH


def test_cursor_policy_requires_cursor_backend(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="testing")
    _receipt(project, backend="claude")
    policy = ak.policy_for_cursor()
    policy.require_next_action_match = False
    decision = ak.evaluate_advance(
        str(project), "US-001", "reviewing", policy=policy
    )
    assert not decision.allowed
    assert decision.code == ak.BACKEND_MISMATCH


def test_claude_policy_ignores_backend(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="testing")
    _receipt(project, backend="codex")
    decision = ak.evaluate_advance(
        str(project),
        "US-001",
        "reviewing",
        policy=ak.policy_for_claude(enforcement="enforce"),
    )
    assert decision.allowed, decision.reason


def test_baa_gate_refuses_regulated_project(tmp_path):
    project = _project(tmp_path, baa=True)
    _seed(project, state="testing")
    _receipt(project, backend="cursor")
    decision = ak.evaluate_advance(
        str(project), "US-001", "reviewing",
        policy=ak.policy_for_cursor(),
    )
    assert not decision.allowed
    assert decision.code == ak.POLICY_REFUSED


def test_baa_gate_fails_closed_when_parser_raises(tmp_path, monkeypatch):
    """Cursor returned False here, so a broken config permitted PHI work."""
    project = _project(tmp_path)
    _seed(project, state="testing")

    def _boom(_project_dir):
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(sp, "healthcare_baa_enforced", _boom)
    gate = ak.baa_refusal_gate("refused")
    assert gate(str(project)) == {"error": "refused"}


def test_readiness_gate_refusal_short_circuits(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="testing")
    _receipt(project)
    policy = _policy(readiness_gate=lambda _p: {"error": "not ready"})
    decision = ak.evaluate_advance(
        str(project), "US-001", "reviewing", policy=policy
    )
    assert not decision.allowed
    assert decision.code == ak.NOT_READY


def test_warn_mode_downgrades_evidence_checks(tmp_path):
    """Claude's controlled mode: evidence warns, the advance proceeds."""
    project = _project(tmp_path)
    _seed(project, state="testing")
    decision = ak.evaluate_advance(
        str(project),
        "US-001",
        "reviewing",
        policy=_policy(enforcement="warn"),
    )
    assert decision.allowed
    assert any(ak.NO_RECEIPT in w for w in decision.warnings)


def test_warn_mode_still_enforces_legality(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="queued")
    decision = ak.evaluate_advance(
        str(project), "US-001", "done", policy=_policy(enforcement="warn")
    )
    assert not decision.allowed
    assert decision.code == ak.ILLEGAL_TRANSITION


# ── #755: an invalid receipt is never warned past ────────────────────────────


#: The exact malformed block the #714 pilot's producer filed on `TRACKER-001`:
#: three bare class tokens. Each one is a claim with nothing behind it -- a
#: replay of no command, an attestation bound to no execution, a verdict from
#: no principal about no candidate -- and together they are the eight errors
#: `validate_receipt` reported while the same build advanced the Work Unit.
_PILOT_BARE_EVIDENCE = [
    {"evidence_class": "replayed"},
    {"evidence_class": "attested"},
    {"evidence_class": "judged"},
]


def _governed_se_binding(project: Path, story_id: str = "US-001") -> None:
    """Record a COMPLETED governed producer attempt on the story's se stage.

    `advance_kernel.unadvanceable_receipt` is scoped to a binding recorded
    exactly `completed`, so the #741 recovery ladder is only reachable through
    one. Without this the story would still refuse, and the test would prove
    the refusal while proving nothing about the route out of it.
    """
    path = project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    for story in state["current_stories"]:
        if story["id"] == story_id:
            story["mcp_active_dispatches"] = {
                "se": {
                    "dispatch_id": "a" * 32,
                    "attempt_id": "att_" + "b" * 20,
                    "fencing_token": "c" * 32,
                    "state": "completed",
                    "role": "software-engineer",
                    "started_at": STAGE_ENTERED,
                    "runtime_family": "claude",
                    "placement": "local",
                    "adapter_profile_id": "claude-local-v1",
                }
            }
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def test_receipt_invalid_is_not_downgradable(tmp_path):
    """#755. A document that fails its own schema is correctness, not evidence
    strength, so `enforcement="warn"` must not carry the board past it.

    The membership assertion is not decoration. Putting the code back into
    `_EVIDENCE_CODES` would restore the defect while every behavioural test
    below still had a refusal to find on some other host, so the set itself is
    pinned here in the same idiom `test_attempt_liveness.py` uses for the
    fencing codes.
    """
    project = _project(tmp_path)
    _seed(project, state="in_progress")
    _receipt(
        project,
        role="software-engineer",
        abbrev="se",
        evidence=_PILOT_BARE_EVIDENCE,
    )
    decision = ak.execute_advance(
        str(project),
        "US-001",
        "testing",
        policy=_policy(enforcement="warn"),
    )
    assert not decision.allowed, decision.reason
    assert decision.code == ak.RECEIPT_INVALID
    # The Work Unit stays at the PRODUCER stage. Reaching `testing` is what
    # made the invalid producer receipt unreachable in the pilot: the recovery
    # ladder keys on the current stage's gating role, so a unit one stage past
    # its own bad evidence has nothing left that can see it.
    story = sp.get_story(sp._read_state(str(project)), "US-001")
    assert story["state"] == "in_progress"
    # Pinned last, so the behavioural assertions above are what a regression
    # reports first: putting the code back into `_EVIDENCE_CODES` must read as
    # "the board moved", not as a set that changed shape.
    assert ak.RECEIPT_INVALID not in ak._EVIDENCE_CODES


def test_invalid_producer_receipt_keeps_the_recovery_ladder_reachable(tmp_path):
    """#755. Refusing is half the contract; the other half is the route out.

    `next_action` must offer producer recovery rather than downstream proof --
    the pilot's `next_action` answered `dispatch_qe` on a Work Unit whose
    producer receipt had never been valid.
    """
    project = _project(tmp_path)
    _seed(project, state="in_progress")
    # Carries the binding's own identity, so the ONLY thing wrong with this
    # receipt is its evidence. Without the attempt id, the dispatch id and the
    # generation, the kernel refuses on `fencing_token_missing` -- which is
    # already non-downgradable -- and the test would pass on a fence that has
    # nothing to do with the evidence contract under test.
    _receipt(
        project,
        role="software-engineer",
        abbrev="se",
        evidence=_PILOT_BARE_EVIDENCE,
        attempt_id="att_" + "b" * 20,
        dispatch_id="a" * 32,
        fencing_token="c" * 32,
        adapter_profile_id="claude-local-v1",
        placement="local",
        source_revision="deadbeef",
        stage_profile="producing",
        capability_profile="producer",
    )
    _governed_se_binding(project)

    # The pilot's own sequence: the host tries to advance first. Whether that
    # call refuses is `test_receipt_invalid_is_not_downgradable`'s subject;
    # what this test measures is the board AFTER it, because a board carried
    # one stage forward is where the recovery stops being reachable.
    ak.execute_advance(
        str(project),
        "US-001",
        "testing",
        policy=_policy(enforcement="warn"),
    )

    state = sp._read_state(str(project))
    hits = ak.inadmissible_receipts(str(project), state)
    assert "US-001" in hits
    assert any("evidence[0]" in e for e in hits["US-001"]["errors"])

    action = ak.next_action(str(project))
    assert action["action"] == "dispatch_se", action
    assert action["role"] == "se"
    assert action["recovery"]["verdict"] == ak.RECEIPT_INVALID


def test_blocked_edge_ungated_for_claude(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="testing")
    decision = ak.evaluate_advance(
        str(project),
        "US-001",
        "blocked",
        reason="agent failed",
        policy=ak.policy_for_claude(enforcement="enforce"),
    )
    assert decision.allowed, decision.reason


def test_blocked_edge_gated_for_mcp_hosts(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="testing")
    decision = ak.evaluate_advance(
        str(project),
        "US-001",
        "blocked",
        reason="agent failed",
        policy=_policy(gate_blocked_transitions=True),
    )
    assert not decision.allowed
    assert decision.code == ak.NO_RECEIPT


# ── DoD gate on the done edge ────────────────────────────────────────────────


def _ready_for_done(project: Path) -> None:
    _seed(project, state="reviewing")
    _receipt(project, role="code-reviewer", abbrev="cr")


def test_done_edge_evaluates_dod(tmp_path):
    project = _project(tmp_path)
    _ready_for_done(project)
    decision = ak.evaluate_advance(str(project), "US-001", "done", policy=_policy())
    assert decision.dod is not None, "DoD must be evaluated on the done edge"


def test_done_with_red_dod_refused_under_mcp_semantics(tmp_path):
    """A UI-bearing story with backend-only proof must not reach done.

    dod_gate_block_reason only blocks on the CONDITIONAL gates, so a fixture
    that merely flips tests_pass would not exercise the gate at all. A
    UI-bearing title with no metrics.ui_verification trips `ui_acceptance`,
    which is the same path scrum_state_machine covers end to end.
    """
    project = _project(tmp_path)
    payload = _seed(project, state="reviewing")
    payload["current_stories"][0]["title"] = "View the analytics dashboard screen"
    payload["current_stories"][0]["ui_bearing"] = True
    (project / ".synaptory" / ".orchestrator" / "pipeline-state.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    for role, abbrev in (
        ("software-engineer", "se"),
        ("quality-engineer", "qe"),
        ("code-reviewer", "cr"),
    ):
        _receipt(
            project,
            role=role,
            abbrev=abbrev,
            # default metrics carry no ui_verification -> ui_acceptance unverified
            verification_commands=[
                {"command": "npm test", "exit_code": 0, "summary": "suite green"}
            ],
        )

    refuse = ak.evaluate_advance(
        str(project), "US-001", "done", policy=_policy(dod_red_behavior="refuse")
    )
    assert not refuse.allowed
    assert refuse.code == ak.DOD_FAILED
    assert "ui_acceptance" in (refuse.reason or "")


def test_done_with_red_dod_redirects_to_blocked_under_claude_semantics(tmp_path):
    """Claude redirects rather than refusing, which keeps the recovery ladder alive."""
    project = _project(tmp_path)
    payload = _seed(project, state="reviewing")
    payload["current_stories"][0]["title"] = "View the analytics dashboard screen"
    payload["current_stories"][0]["ui_bearing"] = True
    (project / ".synaptory" / ".orchestrator" / "pipeline-state.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    for role, abbrev in (
        ("software-engineer", "se"),
        ("quality-engineer", "qe"),
        ("code-reviewer", "cr"),
    ):
        _receipt(project, role=role, abbrev=abbrev)

    decision = ak.execute_advance(
        str(project),
        "US-001",
        "done",
        policy=_policy(dod_red_behavior="redirect_blocked"),
    )
    assert decision.allowed
    assert decision.effective_to_state == "blocked"
    story = sp.get_story(sp._read_state(str(project)), "US-001")
    assert story["state"] == "blocked"
    assert "ui_acceptance" in (story.get("blocked_reason") or "")


def test_a_redirect_to_blocked_records_its_verdict_on_the_board(tmp_path):
    """The verdict that caused the block has to survive it.

    Persistence used to key on where the story LANDED, so a done edge the DoD
    gate redirected to `blocked` wrote no `dod` at all: the gate evaluated,
    decided, moved the story and then discarded its own reasoning. The Sync
    barrier reads `dod_evaluated` off the board, so it had nothing to read on
    exactly the units the gate had stopped, and a caller could only recover the
    verdict from the return value of the call that produced it.

    `scrum_state_machine` closed this on its own path in #403. This is the
    shared kernel every other mode and both MCP hosts advance through, so
    scrum was the only one that kept the verdict. Asserted alongside the
    `done` case below so the two cannot drift apart again.
    """
    project = _project(tmp_path)
    payload = _seed(project, state="reviewing")
    payload["current_stories"][0]["title"] = "View the analytics dashboard screen"
    payload["current_stories"][0]["ui_bearing"] = True
    (project / ".synaptory" / ".orchestrator" / "pipeline-state.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    for role, abbrev in (
        ("software-engineer", "se"),
        ("quality-engineer", "qe"),
        ("code-reviewer", "cr"),
    ):
        _receipt(project, role=role, abbrev=abbrev)

    decision = ak.execute_advance(
        str(project),
        "US-001",
        "done",
        policy=_policy(dod_red_behavior="redirect_blocked"),
    )
    assert decision.effective_to_state == "blocked"

    story = sp.get_story(sp._read_state(str(project)), "US-001")
    recorded = story.get("dod") or {}
    assert recorded.get("evaluated_at"), (
        "the gate blocked the story and recorded no verdict, so nothing "
        "downstream can read why: %r" % story.get("dod")
    )
    assert recorded.get("passed") is False, recorded
    # The SAME verdict, not a second evaluation: a re-run could disagree with
    # the one the block was actually decided on.
    assert recorded == decision.dod


def test_a_story_that_reaches_done_still_records_its_verdict(tmp_path):
    """The case the redirect fix must not change."""
    project = _project(tmp_path)
    _ready_for_done(project)
    decision = ak.execute_advance(str(project), "US-001", "done", policy=_policy())
    assert decision.effective_to_state == "done"
    story = sp.get_story(sp._read_state(str(project)), "US-001")
    assert (story.get("dod") or {}).get("evaluated_at"), story.get("dod")


def test_dod_disabled_policy_skips_the_gate(tmp_path):
    project = _project(tmp_path)
    _ready_for_done(project)
    decision = ak.evaluate_advance(
        str(project), "US-001", "done", policy=_policy(dod_on_done=False)
    )
    assert decision.dod is None


def test_resolve_done_edge_is_noop_off_the_done_edge(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="testing")
    state = sp._read_state(str(project))
    target, reason, dod = ak.resolve_done_edge(
        str(project), state, "US-001", "reviewing", None
    )
    assert (target, reason, dod) == ("reviewing", None, None)


def test_kanban_skips_acceptance_redirect(tmp_path):
    """kanban_state_machine has no PO acceptance step; the kernel preserves that."""
    project = _project(tmp_path, build_mode="kanban")
    _seed(project, state="reviewing", build_mode="kanban")
    (project / ".synaptory.yaml").write_text(
        "build_mode: kanban\nsprint:\n  review:\n    per_story_acceptance: true\n",
        encoding="utf-8",
    )
    state = sp._read_state(str(project))
    target, _reason, _dod = ak.resolve_done_edge(
        str(project), state, "US-001", "done", None, mode="kanban"
    )
    assert target != "awaiting_acceptance"


# ── purity and single-write guarantees ───────────────────────────────────────


def test_evaluate_advance_never_writes_state(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="testing")
    _receipt(project)
    path = project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
    before = path.read_bytes()
    ak.evaluate_advance(str(project), "US-001", "reviewing", policy=_policy())
    assert path.read_bytes() == before


def test_refused_advance_leaves_state_untouched(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="testing")
    _receipt(project, completed_at=PAST)
    path = project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
    before = path.read_bytes()
    decision = ak.execute_advance(str(project), "US-001", "reviewing", policy=_policy())
    assert not decision.allowed
    assert path.read_bytes() == before


def test_execute_advance_writes_once(tmp_path, monkeypatch):
    """The MCP servers wrote twice, leaving a replayable-receipt crash window."""
    project = _project(tmp_path)
    _seed(project, state="testing")
    _receipt(project)

    calls = {"n": 0}
    real = sp._write_state

    def _counting(project_dir, state):
        calls["n"] += 1
        return real(project_dir, state)

    monkeypatch.setattr(sp, "_write_state", _counting)
    decision = ak.execute_advance(str(project), "US-001", "reviewing", policy=_policy())
    assert decision.allowed
    assert calls["n"] == 1, "expected exactly one state write, got %d" % calls["n"]


def test_receipt_validation_uses_already_read_bytes(tmp_path, monkeypatch):
    """validate_receipt used to reopen the file after the digest was hashed.

    Runtime repro: bytes missing model/artifacts were hashed, then a valid
    file was swapped in before validate_receipt reopened the path, so the
    recorded digest identified the invalid bytes. Validate the payload that
    was already read.
    """
    import receipt_validator as rv

    project = _project(tmp_path)
    _seed(project, state="testing")
    path = _receipt(project)
    valid = path.read_text(encoding="utf-8")
    _receipt(project, valid=False)

    original = rv.validate_receipt

    def swap_then_validate(receipt_path, project_dir):
        Path(receipt_path).write_text(valid, encoding="utf-8")
        return original(receipt_path, project_dir)

    monkeypatch.setattr(rv, "validate_receipt", swap_then_validate)
    decision = ak.execute_advance(str(project), "US-001", "reviewing", policy=_policy())
    assert not decision.allowed, decision
    assert decision.code == ak.RECEIPT_INVALID


# ── role tables ──────────────────────────────────────────────────────────────


def test_expected_receipt_role_covers_the_pipeline():
    assert ak.expected_receipt_role("testing", "reviewing") == (
        "quality-engineer",
        "qe",
    )
    assert ak.expected_receipt_role("reviewing", "done") == ("code-reviewer", "cr")
    assert ak.expected_receipt_role("in_progress", "blocked") == (
        "software-engineer",
        "se",
    )
    assert ak.expected_receipt_role("queued", "done") is None


def test_role_tables_match_the_host_servers():
    """These tables were byte-identical copies in both MCP servers."""
    assert ak.TRANSITION_RECEIPT[("queued", "in_progress")] == (
        "software-engineer",
        "se",
    )
    assert ak.TRANSITION_RECEIPT[("awaiting_acceptance", "done")] == (
        "project-owner",
        "po",
    )
    assert set(ak.BLOCKED_FROM_ROLE) == {
        "in_progress",
        "testing",
        "reviewing",
        "awaiting_acceptance",
    }


# ── dispatch ─────────────────────────────────────────────────────────────────


def test_dispatch_refused_for_unselected_story(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="testing")
    decision = ak.evaluate_dispatch(str(project), "US-999", policy=_policy())
    assert not decision.allowed
    assert decision.code == ak.NEXT_ACTION_MISMATCH


def test_dispatch_starts_a_queued_story(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="queued")
    decision = ak.execute_dispatch(str(project), "US-001", policy=_policy())
    if not decision.allowed:
        pytest.skip("next_action did not select a dispatch for this fixture")
    story = sp.get_story(sp._read_state(str(project)), "US-001")
    assert story["state"] == "in_progress"
    assert "receipt_contract" in decision.extra


def test_second_dispatch_of_a_claimed_story_is_refused(tmp_path):
    """begin_dispatch is the start gate. A story already in_progress is claimed."""
    project = _project(tmp_path)
    _seed(project, state="queued")
    first = ak.execute_dispatch(str(project), "US-001", policy=_policy())
    if not first.allowed:
        pytest.skip("next_action did not select a dispatch for this fixture")
    second = ak.execute_dispatch(str(project), "US-001", policy=_policy())
    assert not second.allowed
    assert second.code == ak.DISPATCH_ALREADY_STARTED
    story = sp.get_story(sp._read_state(str(project)), "US-001")
    assert story["state"] == "in_progress"


def test_dispatch_and_next_action_share_completed_at_freshness(tmp_path):
    """A fresh completed_at with an old mtime is still 'call advance'.

    next_action used mtime; evaluate_dispatch used completed_at. Unify both
    on completed_at so the two verbs cannot disagree.
    """
    import time

    project = _project(tmp_path)
    _seed(project, state="testing")
    path = _receipt(project, completed_at=FUTURE)
    old = time.time() - 10_000
    os.utime(path, (old, old))
    na = sp.next_action(
        sp._read_state(str(project)),
        receipts_dir=str(project / ".synaptory" / ".orchestrator" / "receipts"),
    )
    assert na["receipt_present"] is True
    assert na.get("transition_to") == "reviewing"
    decision = ak.evaluate_dispatch(str(project), "US-001", policy=_policy())
    assert not decision.allowed
    # Clocks agree: next_action is an advance, so dispatch is ineligible.
    # Previously mtime-stale next_action still selected dispatch_qe while
    # evaluate_dispatch refused with RECEIPT_ALREADY_PRESENT.
    assert decision.code in (ak.DISPATCH_NOT_ELIGIBLE, ak.RECEIPT_ALREADY_PRESENT)
    advance = ak.evaluate_advance(
        str(project), "US-001", "reviewing", policy=_policy()
    )
    assert advance.allowed, advance.reason


# ── decision serialisation ───────────────────────────────────────────────────


def test_decision_to_dict_is_json_serialisable(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="testing")
    decision = ak.evaluate_advance(str(project), "US-001", "reviewing", policy=_policy())
    payload = decision.to_dict()
    json.dumps(payload, default=str)
    assert payload["allowed"] is False
    assert payload["code"] == ak.NO_RECEIPT
    assert isinstance(payload["checks"], list)


# ── shipped-prompt audit (#277) ──────────────────────────────────────────────


def test_shipped_content_routes_transitions_through_the_kernel():
    """No shipped markdown may still instruct a bare state-machine transition.

    The orchestrator is the caller on Claude, so a stale instruction is a live
    bypass of the gate, not merely stale documentation.
    """
    root = Path(__file__).resolve().parents[2]
    offenders = []
    for area in ("skills", "agents"):
        for md in (root / area).rglob("*.md"):
            text = md.read_text(encoding="utf-8", errors="ignore")
            if "transition_story" in text:
                offenders.append(str(md.relative_to(root)))
    assert not offenders, (
        "shipped content still instructs a bare transition_story call: %s"
        % ", ".join(offenders)
    )


def test_kernel_cli_is_the_documented_entry_point():
    root = Path(__file__).resolve().parents[2]
    sprint = (root / "skills" / "synaptory" / "modes" / "sprint.md").read_text(
        encoding="utf-8"
    )
    assert "advance_kernel.py" in sprint
    assert "begin_dispatch" in sprint


# ── the bare transition verb is gated (#278) ─────────────────────────────────


def _run_bare_transition(project: Path, story_id: str, to_state: str, *extra: str):
    import subprocess
    import sys

    script = Path(sp.__file__).resolve()
    return subprocess.run(
        [sys.executable, str(script), "transition", str(project), story_id, to_state, *extra],
        capture_output=True,
        text=True,
    )


def test_bare_transition_refuses_receipt_gated_edges(tmp_path):
    """The verb wrote state with no receipt, DoD or replay check whatsoever.

    modes/sprint.md used to acknowledge that avoiding it was a prompt convention;
    it is now enforced.
    """
    project = _project(tmp_path)
    _seed(project, state="testing")
    result = _run_bare_transition(project, "US-001", "reviewing")
    assert result.returncode != 0
    assert "receipt-gated" in result.stderr
    assert "advance_kernel.py" in result.stderr
    story = sp.get_story(sp._read_state(str(project)), "US-001")
    assert story["state"] == "testing", "refused transition must not write state"


def test_bare_transition_still_allows_de_escalation(tmp_path):
    """Parking a failing story is not promotion; the recovery ladder needs it."""
    project = _project(tmp_path)
    _seed(project, state="testing")
    result = _run_bare_transition(
        project, "US-001", "blocked", "--reason", "agent failed"
    )
    assert result.returncode == 0, result.stderr
    story = sp.get_story(sp._read_state(str(project)), "US-001")
    assert story["state"] == "blocked"


def test_force_recovery_hatch_works_and_requires_a_reason(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="testing")

    without_reason = _run_bare_transition(
        project, "US-001", "reviewing", "--force-recovery"
    )
    assert without_reason.returncode != 0
    assert "--reason" in without_reason.stderr

    forced = _run_bare_transition(
        project,
        "US-001",
        "reviewing",
        "--force-recovery",
        "--reason",
        "repairing a crashed board",
    )
    assert forced.returncode == 0, forced.stderr
    story = sp.get_story(sp._read_state(str(project)), "US-001")
    assert story["state"] == "reviewing"


def _run_lifecycle_transition_story(
    machine: str, project: Path, story_id: str, to_state: str, *extra: str
):
    import subprocess
    import sys

    script = Path(sp.__file__).resolve().parent / ("%s_state_machine.py" % machine)
    return subprocess.run(
        [
            sys.executable,
            str(script),
            "transition_story",
            str(project),
            story_id,
            to_state,
            *extra,
        ],
        capture_output=True,
        text=True,
    )


#: The lifecycle CLIs that still expose `transition_story`. SPQ dropped every
#: per-story verb at #644 -- its usage line lists Cycle verbs only -- so there
#: is no SPQ CLI left to refuse anything.
#:
#: That absence is asserted somewhere instead of only removed from here:
#: `test_adr029_acceptance_edge.py::test_the_spq_cli_names_no_per_unit_acceptance_verb`
#: pins it in both directions. It is a settled design as of #640 P6 -- SPQ has
#: no per-Work-Unit acceptance edge to wrap, and acceptance is a named human
#: recorded at Checkpoint and read by the barrier -- and that test records how
#: the mismatch it used to describe was resolved.
_TRANSITION_CLIS = [("scrum", "scrum"), ("kanban", "kanban")]


@pytest.mark.parametrize("machine,build_mode", _TRANSITION_CLIS)
def test_lifecycle_cli_refuses_receipt_gated_edges(tmp_path, machine, build_mode):
    """The public transition_story verbs wrote state with no receipt check.

    ADR-029: no valid receipt means no promotion, including these CLIs.
    """
    project = _project(tmp_path, build_mode=build_mode)
    _seed(project, state="testing", build_mode=build_mode)
    result = _run_lifecycle_transition_story(machine, project, "US-001", "reviewing")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "receipt-gated" in result.stderr
    assert "advance_kernel.py" in result.stderr
    story = sp.get_story(sp._read_state(str(project)), "US-001")
    assert story["state"] == "testing", "refused transition must not write state"


@pytest.mark.parametrize("machine,build_mode", _TRANSITION_CLIS)
def test_lifecycle_cli_force_recovery_hatch(tmp_path, machine, build_mode):
    project = _project(tmp_path, build_mode=build_mode)
    _seed(project, state="testing", build_mode=build_mode)
    result = _run_lifecycle_transition_story(
        machine,
        project,
        "US-001",
        "reviewing",
        "--force-recovery",
        "--reason",
        "repairing a crashed board",
    )
    assert result.returncode == 0, result.stderr
    story = sp.get_story(sp._read_state(str(project)), "US-001")
    assert story["state"] == "reviewing"


def test_receipt_gated_edges_match_the_kernel_role_table():
    """The guard duplicates the edge list on purpose; keep the copies in step.

    story_pipeline cannot import advance_kernel for this: the refusal has to hold
    even when the kernel is unavailable, so an import failure must not open the
    gate. This test is what keeps the deliberate duplication honest.
    """
    assert sp._RECEIPT_GATED_EDGES == frozenset(ak.TRANSITION_RECEIPT)


# ── concurrency (#279) ───────────────────────────────────────────────────────


def _advance_in_subprocess(project: Path, story_id: str, to_state: str) -> dict:
    """Run one advance in a separate OS process, as a real second session would."""
    import subprocess
    import sys

    code = (
        "import json,sys;"
        "sys.path.insert(0, %r);"
        "import advance_kernel as ak;"
        "d = ak.execute_advance(%r, %r, %r, policy=ak.HostPolicy("
        "host='test', require_next_action_match=False));"
        "print(json.dumps({'allowed': d.allowed, 'code': d.code}))"
        % (str(Path(ak.__file__).resolve().parent), str(project), story_id, to_state)
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    if result.returncode != 0:
        return {"allowed": False, "code": "subprocess_error", "stderr": result.stderr}
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_concurrent_advances_produce_exactly_one_transition(tmp_path):
    """Two processes, one receipt: exactly one wins.

    Without a lock over the whole read-decide-write window both read the same
    state, both pass the replay-ledger check against that stale copy, and both
    write -- a lost update and a defeated anti-replay guard. write_state's own
    lock covers only its internal re-read, which is not enough.
    """
    import concurrent.futures

    project = _project(tmp_path)
    _seed(project, state="testing")
    _receipt(project)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_advance_in_subprocess, project, "US-001", "reviewing")
            for _ in range(2)
        ]
        results = [f.result() for f in futures]

    winners = [r for r in results if r.get("allowed")]
    losers = [r for r in results if not r.get("allowed")]
    assert len(winners) == 1, "expected exactly one winner, got %r" % (results,)
    assert len(losers) == 1
    # The loser is refused by the gate, never by a crash.
    assert losers[0]["code"] in (
        ak.REPLAY,
        ak.ILLEGAL_TRANSITION,
        ak.NO_RECEIPT,
        ak.STATE_LOCK_TIMEOUT,
    ), losers[0]

    story = sp.get_story(sp._read_state(str(project)), "US-001")
    assert story["state"] == "reviewing"
    assert len(story["mcp_consumed_receipts"]) == 1, "receipt consumed more than once"


def _dispatch_in_subprocess(project: Path, story_id: str) -> dict:
    import subprocess
    import sys

    code = (
        "import json,sys;"
        "sys.path.insert(0, %r);"
        "import advance_kernel as ak;"
        "d = ak.execute_dispatch(%r, %r, policy=ak.HostPolicy("
        "host='test', require_next_action_match=False));"
        "print(json.dumps({'allowed': d.allowed, 'code': d.code}))"
        % (str(Path(ak.__file__).resolve().parent), str(project), story_id)
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    if result.returncode != 0:
        return {"allowed": False, "code": "subprocess_error", "stderr": result.stderr}
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_concurrent_dispatches_authorize_exactly_one_start(tmp_path):
    """Two processes, one queued story: exactly one begin_dispatch wins.

    Evaluating eligibility before the lock let both callers pass against the
    same queued snapshot and both launch an agent. The claim is the
    queued -> in_progress write, held across the whole window.
    """
    import concurrent.futures

    project = _project(tmp_path)
    _seed(project, state="queued")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_dispatch_in_subprocess, project, "US-001") for _ in range(2)
        ]
        results = [f.result() for f in futures]

    winners = [r for r in results if r.get("allowed")]
    losers = [r for r in results if not r.get("allowed")]
    assert len(winners) == 1, "expected exactly one winner, got %r" % (results,)
    assert len(losers) == 1
    assert losers[0]["code"] in (
        ak.DISPATCH_ALREADY_STARTED,
        ak.DISPATCH_NOT_ELIGIBLE,
        ak.NEXT_ACTION_MISMATCH,
        ak.STATE_LOCK_TIMEOUT,
    ), losers[0]

    story = sp.get_story(sp._read_state(str(project)), "US-001")
    assert story["state"] == "in_progress"


def test_state_transaction_is_reentrant(tmp_path):
    """A transaction body may call write_state, which locks too.

    flock is per open-file-description, so a naive second acquisition inside the
    same process would block against itself and deadlock the whole hook.
    """
    from spec_state import state_transaction

    project = _project(tmp_path)
    _seed(project, state="testing")
    with state_transaction(str(project)):
        state = sp._read_state(str(project))
        sp._write_state(str(project), state)  # takes the lock again
    assert sp.get_story(sp._read_state(str(project)), "US-001") is not None


def test_state_lock_acquisition_is_bounded(tmp_path):
    """A stuck holder must surface as a refusal, never as a hang.

    SubagentStop runs under a 30s budget, so an unbounded wait would blow it.
    """
    import time

    import spec_state

    project = _project(tmp_path)
    _seed(project, state="testing")

    import subprocess
    import sys

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys,time;sys.path.insert(0, %r);"
            "from spec_state import state_transaction;"
            "ctx = state_transaction(%r);ctx.__enter__();"
            "print('held', flush=True);time.sleep(30)"
            % (str(Path(spec_state.__file__).resolve().parent), str(project)),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        started = time.monotonic()
        with pytest.raises(spec_state.StateLockTimeout):
            with spec_state.state_transaction(str(project), timeout=0.5):
                pass
        assert time.monotonic() - started < 5, "timeout was not bounded"
    finally:
        holder.kill()
        holder.wait(timeout=5)


def test_state_lock_is_not_reentrant_across_threads(tmp_path):
    """A second thread must not inherit the first thread's nested-lock depth.

    Process-global depth treated a concurrent thread as an inner acquisition,
    so both threads entered the critical section. Depth is thread-local; flock
    still serializes the two callers.
    """
    import threading
    import time

    import spec_state
    import state_store

    # The flock probe follows the implementation: the primitives moved to
    # `state_store` when SPQ stopped borrowing this module (#303/#304/#305).
    # `spec_state.state_transaction` still delegates to them, so the property
    # under test is unchanged.
    if state_store.fcntl is None:
        pytest.skip("fcntl flock is required")

    project = _project(tmp_path)
    _seed(project, state="testing")
    barrier = threading.Barrier(2)
    inside = 0
    max_inside = []
    lock = threading.Lock()
    errors = []

    def worker():
        nonlocal inside
        barrier.wait()
        try:
            with spec_state.state_transaction(str(project), timeout=3.0):
                with lock:
                    inside += 1
                    max_inside.append(inside)
                time.sleep(0.2)
                with lock:
                    inside -= 1
        except spec_state.StateLockTimeout as exc:
            errors.append(exc)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert not errors
    assert max_inside
    assert max(max_inside) == 1


# ── remaining P1s after 3055b809 ─────────────────────────────────────────────


def test_early_reviewing_to_done_waives_cr_receipt(tmp_path):
    """Sprint 1 next_action is promote_story with no CR dispatch; the kernel
    must not deadlock on a CR receipt the planner will never write."""
    project = _project(tmp_path)
    _seed(project, state="reviewing", sprint=1)
    _receipt(project, role="software-engineer", abbrev="se")
    _receipt(project, role="quality-engineer", abbrev="qe")
    decision = ak.evaluate_advance(str(project), "US-001", "done", policy=_policy())
    assert decision.allowed, decision.reason
    assert ak.expected_receipt_role("reviewing", "done", intensity="early") is None
    assert ak.expected_receipt_role("reviewing", "done", intensity="growing") == (
        "code-reviewer",
        "cr",
    )


def test_strict_next_action_match_refuses_when_calculation_raises(tmp_path):
    project = _project(tmp_path, build_mode="corrupt-mode")
    _seed(project, state="testing", build_mode="corrupt-mode")
    _receipt(project)
    decision = ak.evaluate_advance(
        str(project),
        "US-001",
        "reviewing",
        policy=_policy(require_next_action_match=True),
    )
    assert not decision.allowed
    assert decision.code == ak.NEXT_ACTION_MISMATCH
    assert "next_action unavailable" in (decision.reason or "")


def test_awaiting_acceptance_to_done_evaluates_dod_before_write(tmp_path):
    """Acceptance must not persist done with dod.passed=false (Cursor semantics)."""
    project = _project(tmp_path)
    payload = _seed(project, state="awaiting_acceptance")
    payload["current_stories"][0]["title"] = "View the analytics dashboard screen"
    payload["current_stories"][0]["ui_bearing"] = True
    (project / ".synaptory" / ".orchestrator" / "pipeline-state.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    _receipt(project, role="project-owner", abbrev="po")
    for role, abbrev in (
        ("software-engineer", "se"),
        ("quality-engineer", "qe"),
        ("code-reviewer", "cr"),
    ):
        _receipt(project, role=role, abbrev=abbrev)

    refused = ak.execute_advance(
        str(project),
        "US-001",
        "done",
        policy=_policy(dod_requires_pass=True, dod_red_behavior="refuse"),
    )
    assert not refused.allowed
    assert refused.code == ak.DOD_FAILED
    story = sp.get_story(sp._read_state(str(project)), "US-001")
    assert story["state"] == "awaiting_acceptance"


def test_dispatch_contract_refuses_receipts_symlink_escape(tmp_path):
    project = _project(tmp_path)
    _seed(project, state="queued")
    receipts = project / ".synaptory" / ".orchestrator" / "receipts"
    outside = tmp_path / "outside-receipts"
    outside.mkdir()
    receipts.rmdir()
    receipts.symlink_to(outside)
    decision = ak.execute_dispatch(str(project), "US-001", policy=_policy())
    assert not decision.allowed
    assert decision.code == ak.PATH_SYMLINK
    story = sp.get_story(sp._read_state(str(project)), "US-001")
    assert story["state"] == "queued"


def test_evaluate_dod_cli_writes_under_state_transaction(tmp_path, monkeypatch):
    import sys

    project = _project(tmp_path)
    _seed(project, state="reviewing")
    seen: list[str] = []
    real = sp._state_transaction

    def _wrap(project_dir):
        seen.append(str(project_dir))
        return real(project_dir)

    monkeypatch.setattr(sp, "_state_transaction", _wrap)
    monkeypatch.setattr(
        sys, "argv", ["story_pipeline.py", "evaluate_dod", str(project), "US-001"]
    )
    sp.main()
    assert seen == [str(project)]
    story = sp.get_story(sp._read_state(str(project)), "US-001")
    assert story["dod"] is not None


# ── #304: the dependency gate at the dispatch boundary ─────────────────────


def _dep_seed(project: Path, *, upstream_state: str = "queued") -> None:
    """A blocked downstream unit plus its upstream, downstream FIRST.

    Order matters: `next_action` skips a dep-blocked candidate, so with the
    upstream first the binding check would refuse before the dependency check
    is reached and the test would pass for the wrong reason.
    """
    _seed(
        project,
        story_id="US-002",
        state="queued",
        extra_stories=[
            {
                "id": "US-001",
                "title": "Upstream",
                "state": upstream_state,
                "blocked_reason": None,
                "blocked_from": None,
                "backend": {},
                "pipeline_log": [
                    {"state": upstream_state, "entered_at": STAGE_ENTERED,
                     "exited_at": None}
                ],
                "dod": None,
                "receipts": [],
                "acceptance_criteria": [],
                "kind": "",
                "labels": [],
                "depends_on": [],
                "file_scope": [],
                "retries": {},
                "rejection_feedback": [],
            }
        ],
    )
    path = project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["current_stories"][0]["depends_on"] = ["US-001"]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_evaluate_dispatch_refuses_a_dep_blocked_story(tmp_path):
    project = _project(tmp_path)
    _dep_seed(project)
    decision = ak.evaluate_dispatch(str(project), "US-002", policy=_policy())
    assert not decision.allowed
    assert decision.code == ak.DEPS_UNMET, decision.reason
    assert decision.extra["unmet_dependencies"]


def test_evaluate_dispatch_names_the_dependency_not_a_generic_mismatch(tmp_path):
    """Both refuse, so the gate holds either way -- but `next_action_mismatch`
    cannot distinguish "held by a dependency" from "wrong story", which leaves
    the operator with nothing to act on."""
    project = _project(tmp_path)
    _dep_seed(project)
    decision = ak.evaluate_dispatch(str(project), "US-002", policy=_policy())
    assert "US-001" in decision.reason


def test_evaluate_dispatch_allows_the_story_once_its_dependency_is_done(tmp_path):
    project = _project(tmp_path)
    _dep_seed(project, upstream_state="done")
    decision = ak.evaluate_dispatch(str(project), "US-002", policy=_policy())
    assert decision.allowed, decision.reason


def test_unknown_dependency_fails_closed_at_dispatch(tmp_path):
    """A typo'd or cross-workstream upstream must not license a dispatch."""
    project = _project(tmp_path)
    _seed(project, story_id="US-002", state="queued")
    path = project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["current_stories"][0]["depends_on"] = ["US-NOPE"]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    decision = ak.evaluate_dispatch(str(project), "US-002", policy=_policy())
    assert not decision.allowed
    assert decision.code == ak.DEPS_UNMET


def test_deps_unmet_is_not_downgradable_under_warn_enforcement():
    """Dependency order is correctness, in the same class as
    ILLEGAL_TRANSITION and DOD_FAILED -- not evidence strength. The
    project-level opt-out is `resilience.dependency_gate`, where a reviewer can
    see it, never a per-session enforcement mode."""
    assert ak.DEPS_UNMET not in ak._EVIDENCE_CODES


def test_qe_dispatch_is_not_dep_gated(tmp_path):
    """QE runs on work that is already built; blocking there strands it."""
    project = _project(tmp_path)
    _seed(project, story_id="US-001", state="testing")
    path = project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["current_stories"][0]["depends_on"] = ["US-NOPE"]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    decision = ak.evaluate_dispatch(
        str(project), "US-001", role="quality-engineer", policy=_policy()
    )
    assert decision.allowed, decision.reason


# ─── a refused DoD gate leaves the board something to route on ──────────────


def test_the_kernel_records_why_the_gate_refused(tmp_path):
    """#396. Under a `refuse` policy the unit stays where it was, which is
    right: red evidence does not advance a board. But the refusal was invisible
    to `next_action`, which reads the board, so the board kept advertising the
    promotion the gate had just declined and a compliant orchestrator retried
    an action that cannot succeed, forever.

    The note is the whole fix: the story's STATE is untouched, so `refuse`
    still refuses, and the gate's own reason is what routes the unit to the
    agent that owes the missing result rather than to the SE retry ladder.
    """
    import advance_kernel as ak
    import story_pipeline as sp

    project = tmp_path / "proj"
    (project / ".synaptory" / ".orchestrator").mkdir(parents=True)
    (project / ".synaptory.yaml").write_text(
        "build_mode: scrum\nproject_id: taskflow\n", encoding="utf-8"
    )
    (project / ".synaptory" / ".orchestrator" / "pipeline-state.json").write_text(
        json.dumps({
            "version": "2.0",
            "build_mode": "scrum",
            "lifecycle_state": "SPRINT_EXECUTION",
            "current_sprint": 1,
            "sprints_completed": [],
            "current_stories": [{
                "id": "US-1",
                "title": "conditional gate",
                "state": "reviewing",
                "pipeline_log": [],
                "receipts": [],
                "labels": [],
                "depends_on": [],
            }],
        }),
        encoding="utf-8",
    )

    reason = "user-facing acceptance (ui_acceptance) has not passed"
    ak._note_dod_gate_refusal(str(project), "US-1", reason)

    state = sp._read_state(str(project)) if hasattr(sp, "_read_state") else json.loads(
        (project / ".synaptory" / ".orchestrator" / "pipeline-state.json")
        .read_text(encoding="utf-8")
    )
    story = next(s for s in state["current_stories"] if s["id"] == "US-1")

    # The state did not move. `refuse` still refuses.
    assert story["state"] == "reviewing", story
    note = story.get("dod_gate_refusal")
    assert isinstance(note, dict), story
    assert reason in note["reason"], note
    assert note["reason"].startswith("DoD gate:"), (
        "the note must carry the marker `_gate_remediation` routes on: %r" % note
    )
    assert note.get("at"), note

    # And the board now routes to remediation instead of re-offering the
    # promotion the gate refused.
    action = sp.next_action(state, receipts_dir=str(tmp_path / "receipts"))
    assert action["action"] != "promote_story", action


def test_a_note_that_cannot_be_written_does_not_replace_the_refusal(tmp_path):
    """Best effort by design: the gate's answer is what the caller needs, and a
    note that fails to persist must not turn a typed refusal into a write
    error."""
    import advance_kernel as ak

    # No project at all.
    ak._note_dod_gate_refusal(str(tmp_path / "nope"), "US-1", "whatever")
    # An empty reason writes nothing rather than an empty marker.
    ak._note_dod_gate_refusal(str(tmp_path), "US-1", "")


def test_the_gate_remediation_action_can_actually_be_dispatched(tmp_path):
    """#396. The whole sequence, not the action name.

    `next_action` returning `recover_blocked` for a story the gate left in
    `reviewing` was only half an answer: `execute_dispatch` handled every
    `recover_blocked` by calling `unblock_story`, which refuses a story that is
    not blocked. So the very next operation a host performs after reading the
    board raised `Story ... is not blocked (state: reviewing)` and the dead end
    had moved rather than closed. My own tests stopped at the action name,
    which is exactly why they missed it.
    """
    import advance_kernel as ak
    import story_pipeline as sp

    project = tmp_path / "proj"
    (project / ".synaptory" / ".orchestrator" / "receipts").mkdir(parents=True)
    (project / ".synaptory.yaml").write_text(
        "build_mode: scrum\nproject_id: taskflow\n", encoding="utf-8"
    )
    (project / ".synaptory" / ".orchestrator" / "pipeline-state.json").write_text(
        json.dumps({
            "version": "2.0",
            "build_mode": "scrum",
            "lifecycle_state": "SPRINT_EXECUTION",
            "current_sprint": 1,
            "sprints_completed": [],
            "current_stories": [{
                "id": "US-1",
                "title": "conditional gate",
                "state": "reviewing",
                "pipeline_log": [],
                "receipts": [],
                "labels": [],
                "depends_on": [],
            }],
        }),
        encoding="utf-8",
    )

    # The gate refused, and the board recorded why.
    ak._note_dod_gate_refusal(
        str(project), "US-1",
        "user-facing acceptance (ui_acceptance) has not passed",
    )

    receipts = str(project / ".synaptory" / ".orchestrator" / "receipts")
    state = sp._read_state(str(project))
    action = sp.next_action(state, receipts_dir=receipts)
    assert action["action"] == "recover_blocked", action
    role = action["role"]

    # THE NEXT OPERATION A HOST PERFORMS. It must not raise, and it must not
    # move the story out of `reviewing`: `refuse` means the board did not
    # advance, and a dispatch is not an advance.
    decision = ak.execute_dispatch(str(project), "US-1", role=role)
    assert decision.allowed is True, (decision.code, decision.reason)

    after = sp.get_story(sp._read_state(str(project)), "US-1")
    assert after["state"] == "reviewing", (
        "the gate remediation dispatch moved a story the gate deliberately "
        "left in reviewing: %r" % after
    )


def test_a_genuinely_blocked_story_is_still_unblocked_on_recovery(tmp_path):
    """And the narrowing must not cost the behaviour it narrowed: a story the
    board persisted as `blocked` still gets restored before it is dispatched."""
    import advance_kernel as ak
    import story_pipeline as sp

    project = tmp_path / "proj"
    (project / ".synaptory" / ".orchestrator" / "receipts").mkdir(parents=True)
    (project / ".synaptory.yaml").write_text(
        "build_mode: scrum\nproject_id: taskflow\n", encoding="utf-8"
    )
    (project / ".synaptory" / ".orchestrator" / "pipeline-state.json").write_text(
        json.dumps({
            "version": "2.0",
            "build_mode": "scrum",
            "lifecycle_state": "SPRINT_EXECUTION",
            "current_sprint": 1,
            "sprints_completed": [],
            "current_stories": [{
                "id": "US-2",
                "title": "blocked on a gate",
                "state": "blocked",
                "blocked_from": "reviewing",
                "blocked_reason": (
                    "DoD gate: user-facing acceptance (ui_acceptance) has not passed"
                ),
                "pipeline_log": [],
                "receipts": [],
                "labels": [],
                "depends_on": [],
            }],
        }),
        encoding="utf-8",
    )

    decision = ak.execute_dispatch(str(project), "US-2", role="qe")
    assert decision.allowed is True, (decision.code, decision.reason)
    after = sp.get_story(sp._read_state(str(project)), "US-2")
    assert after["state"] != "blocked", (
        "a genuinely blocked story was dispatched without being unblocked: %r"
        % after
    )
