"""Layer 1 -- a receipt bound to a dead attempt has a way out (#690).

THE DEFECT WAS AN AGREEMENT FAILURE, not a wrong answer. Three governed
calls, each correct read on its own:

  `next_action`    saw a fresh canonical receipt on disk and selected the
                   ordinary "transition instead of re-dispatching" edge.
  `advance`        saw the binding recorded `failed` and refused the receipt
                   as `attempt_not_live`, telling the operator to dispatch a
                   new attempt.
  `begin_dispatch` saw the same fresh receipt and refused the new attempt as
                   `receipt_already_present`, telling the operator to advance.

So the two refusals named each other and the Cycle could not mint the
successor the first refusal demanded. Nothing short of deleting evidence by
hand moved it, which is the thing governance exists to prevent.

The fix is one predicate with two readers: `advance_kernel.unadvanceable_attempt`
answers "will this stage's receipt be refused because its attempt is no longer
live", `evaluate_advance` refuses on it, and `story_pipeline.next_action`
selects a ladder recovery on it. They cannot disagree because there is only
one of them.

These tests drive the real loop through the kernel's public entry points, and
the first one is the regression: it FAILS on `985724b3` at the second
`begin_dispatch` refusal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import advance_kernel as ak
import story_pipeline as sp


pytestmark = pytest.mark.unit

STAGE_ENTERED = "2026-01-01T00:00:00+00:00"
FUTURE = "2090-01-01T00:00:00+00:00"
DISPATCH_ID = "a" * 32
DEAD_ATTEMPT = "att_1d17a817f4e284b92446"


def _project(tmp_path: Path) -> Path:
    (tmp_path / ".synaptory" / ".orchestrator" / "receipts").mkdir(parents=True)
    (tmp_path / ".synaptory.yaml").write_text("build_mode: scrum\n", encoding="utf-8")
    return tmp_path


def _seed(project: Path, *, story_id: str = "US-001", state: str = "in_progress"):
    payload = {
        "version": "2.0",
        "build_mode": "scrum",
        "lifecycle_state": "SPRINT_EXECUTION",
        "current_sprint": 1,
        "cumulative_ticket_number": 1,
        "sprint_goal": "seed",
        "pipeline_log": [],
        "current_stories": [
            {
                "id": story_id,
                "title": "Seed story",
                "state": state,
                "blocked_reason": None,
                "blocked_from": None,
                "backend": {},
                "pipeline_log": [
                    {"state": state, "entered_at": STAGE_ENTERED, "exited_at": None}
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
    }
    (project / ".synaptory" / ".orchestrator" / "pipeline-state.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


def _bind(project: Path, *, story_id: str = "US-001", abbrev: str = "se", **fields):
    """Record the live dispatch binding, and the ledger record the mint writes.

    Both, because the real `execute_dispatch` writes both and the recovery has
    to be shown preserving a record that actually exists.
    """
    path = project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
    state = json.loads(path.read_text())
    for story in state["current_stories"]:
        if story["id"] == story_id:
            story.setdefault("mcp_active_dispatches", {})[abbrev] = dict(fields)
            attempt_id = str(fields.get("attempt_id") or "")
            if attempt_id:
                ledger = story.setdefault(ak.AUTHORIZED_ATTEMPTS_KEY, {})
                ledger.setdefault(abbrev, []).append(
                    {
                        "attempt_id": attempt_id,
                        "authorized_at": STAGE_ENTERED,
                        "classification": ak.FRESH_ATTEMPT,
                    }
                )
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _receipt(project: Path, *, story_id: str = "US-001", abbrev: str = "se", **extra):
    receipts = project / ".synaptory" / ".orchestrator" / "receipts"
    src = project / "src" / "foo.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("x\n", encoding="utf-8")
    payload = {
        "story_id": story_id,
        "role": "software-engineer",
        "backend": "claude",
        "model": "opus",
        "artifacts": ["src/foo.py"],
        "verification_commands": [
            {"command": "pytest -q", "exit_code": 0, "summary": "ok"},
            {"command": "npm run build", "exit_code": 0, "summary": "ok"},
        ],
        "metrics": {"n": 1},
        "completed_at": FUTURE,
        "token_usage": {
            "input": 1,
            "output": 1,
            "cache_read": 0,
            "cache_write": 0,
            "stage": "se-implementation",
        },
        "story_dod": {
            "tests_pass": True,
            "build_succeeds": True,
            "no_critical_findings": True,
            "code_reviewed": True,
            "coverage_no_decrease": True,
        },
    }
    payload.update(extra)
    path = receipts / ("%s-%s.json" % (story_id, abbrev))
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return path


def _policy(**kwargs) -> ak.HostPolicy:
    kwargs.setdefault("host", "test")
    kwargs.setdefault("require_dispatch_binding", True)
    return ak.HostPolicy(**kwargs)


def _deadlocked(tmp_path: Path, *, attempt_state: str = "failed") -> Path:
    """A Work Unit in exactly the shape #690 reproduced."""
    project = _project(tmp_path)
    _seed(project)
    _bind(
        project,
        dispatch_id=DISPATCH_ID,
        attempt_id=DEAD_ATTEMPT,
        state=attempt_state,
        failure_class="workflow-structure",
        failure_class_source="reported",
    )
    _receipt(project, dispatch_id=DISPATCH_ID, attempt_id=DEAD_ATTEMPT)
    return project


def _story(project: Path, story_id: str = "US-001") -> dict:
    return sp.get_story(sp._read_state(str(project)), story_id) or {}


class TestTheLoopClosesInsteadOfDeadlocking:
    def test_failed_attempt_with_receipt_recovers_to_a_new_immutable_attempt(
        self, tmp_path
    ):
        """The regression the issue asks for: the whole loop, no contradiction.

        FAILS BEFORE THE FIX at the `begin_dispatch` assertion --
        `receipt_already_present`, because `next_action` carried no recovery
        and `evaluate_dispatch` only waives that refusal for a recovery.
        """
        project = _deadlocked(tmp_path)

        # 1. The deterministic decision no longer reads the receipt's mere
        #    existence as advanceable.
        selected = ak.next_action(str(project))
        assert selected["action"] == "dispatch_se"
        assert selected["receipt_present"] is True
        assert selected["transition_to"] is None
        recovery = selected["recovery"]
        assert recovery["verdict"] == ak.ATTEMPT_NOT_LIVE
        assert recovery["attempt_id"] == DEAD_ATTEMPT
        assert recovery["attempt_state"] == "failed"
        assert recovery["failure_class"] == "workflow-structure"
        assert recovery["role"] == "se"

        # 2. `advance` still refuses, and the refusal is now CONSISTENT with
        #    the decision above rather than contradicting it. On a host that
        #    binds to the deterministic planner the edge is not even offered
        #    any more; on Claude's shape, which does not bind, the kernel's
        #    own liveness refusal still catches it. Neither now points back at
        #    a call that refuses in the other direction.
        bound = ak.execute_advance(str(project), "US-001", "testing", policy=_policy())
        assert not bound.allowed
        assert bound.code == ak.NEXT_ACTION_MISMATCH

        unbound = ak.execute_advance(
            str(project),
            "US-001",
            "testing",
            policy=_policy(require_next_action_match=False),
        )
        assert not unbound.allowed
        assert unbound.code == ak.ATTEMPT_NOT_LIVE
        assert "begin_dispatch" in unbound.reason

        # 3. `begin_dispatch` grants the successor the refusal asked for.
        granted = ak.execute_dispatch(str(project), "US-001", role="se", policy=_policy())
        assert granted.allowed, granted.reason
        contract = granted.extra["receipt_contract"]
        successor = contract["attempt_id"]
        assert successor and successor != DEAD_ATTEMPT
        assert contract["classification"] == ak.RETRY_ATTEMPT
        assert contract["prior_attempt_id"] == DEAD_ATTEMPT

    def test_the_failed_attempt_and_its_evidence_survive_the_recovery(self, tmp_path):
        """Immutable history: the predecessor is superseded, never erased."""
        project = _deadlocked(tmp_path)
        canonical = (
            project / ".synaptory" / ".orchestrator" / "receipts" / "US-001-se.json"
        )
        digest_before = __import__("hashlib").sha256(canonical.read_bytes()).hexdigest()

        granted = ak.execute_dispatch(str(project), "US-001", role="se", policy=_policy())
        assert granted.allowed, granted.reason

        archived = Path(granted.extra["archived_receipt"])
        assert archived.is_file()
        assert not canonical.exists()
        assert (
            __import__("hashlib").sha256(archived.read_bytes()).hexdigest()
            == digest_before
        )

        history = {
            rec["attempt_id"]: rec
            for rec in ak.attempt_history(_story(project), "se")
        }
        dead = history[DEAD_ATTEMPT]
        assert dead["state"] == "failed"
        assert dead["failure_class"] == "workflow-structure"
        assert dead["superseded_by"] == granted.extra["receipt_contract"]["attempt_id"]
        assert dead["receipt_digest"] == digest_before
        assert dead["archived_receipt"] == str(archived)

        successor = history[granted.extra["receipt_contract"]["attempt_id"]]
        assert successor["classification"] == ak.RETRY_ATTEMPT
        assert successor["prior_attempt_id"] == DEAD_ATTEMPT
        assert successor["prior_failure_class"] == "workflow-structure"

    def test_the_successor_can_then_advance(self, tmp_path):
        """The loop terminates: a live attempt's receipt moves the unit."""
        project = _deadlocked(tmp_path)
        granted = ak.execute_dispatch(str(project), "US-001", role="se", policy=_policy())
        contract = granted.extra["receipt_contract"]
        _receipt(
            project,
            dispatch_id=contract["dispatch_id"],
            attempt_id=contract["attempt_id"],
        )
        moved = ak.execute_advance(
            str(project), "US-001", "testing", policy=_policy()
        )
        assert moved.allowed, moved.reason
        assert _story(project)["state"] == "testing"

    @pytest.mark.parametrize("attempt_state", sorted(ak._ATTEMPT_NOT_LIVE_STATES))
    def test_every_not_live_state_deadlocks_identically_so_all_recover(
        self, tmp_path, attempt_state
    ):
        """`failed` is the reported shape; the refusal set is what deadlocks.

        Fixing only `failed` would leave the same dead end two states over,
        so the recovery mirrors `_ATTEMPT_NOT_LIVE_STATES` exactly.
        """
        project = _deadlocked(tmp_path, attempt_state=attempt_state)
        selected = ak.next_action(str(project))
        assert selected["recovery"]["verdict"] == ak.ATTEMPT_NOT_LIVE
        assert selected["recovery"]["attempt_state"] == attempt_state
        granted = ak.execute_dispatch(str(project), "US-001", role="se", policy=_policy())
        assert granted.allowed, granted.reason

    def test_a_cancelled_attempt_recovers_as_a_steer_not_a_retry(self, tmp_path):
        """The lineage vocabulary is the kernel's, not a second one."""
        project = _deadlocked(tmp_path, attempt_state="cancelled")
        path = project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
        state = json.loads(path.read_text())
        state["current_stories"][0]["mcp_active_dispatches"]["se"][
            "failure_class"
        ] = ak.HUMAN_CANCEL_FAILURE_CLASS
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")

        granted = ak.execute_dispatch(str(project), "US-001", role="se", policy=_policy())
        assert granted.allowed, granted.reason
        assert granted.extra["receipt_contract"]["classification"] == ak.STEER_ATTEMPT


class TestLadderExhaustionDoesNotRedeadlock:
    def test_the_block_edge_is_reachable_when_the_ladder_is_spent(self, tmp_path):
        """A spent ladder parks the unit instead of stranding it.

        `gate_blocked_transitions` is ON for both MCP hosts, so before this
        the exhausted ladder produced `block_story` and then `advance ->
        blocked` hit `attempt_not_live` again: the same deadlock one tier
        down.
        """
        project = _deadlocked(tmp_path)
        path = project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
        state = json.loads(path.read_text())
        state["current_stories"][0]["retries"] = {"se": sp.DEFAULT_RETRY_CAP}
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")

        selected = ak.next_action(str(project))
        assert selected["action"] == "block_story"
        assert selected["transition_to"] == "blocked"
        assert selected["recovery"]["verdict"] == ak.ATTEMPT_NOT_LIVE

        parked = ak.execute_advance(
            str(project),
            "US-001",
            "blocked",
            reason="attempt failed and the ladder is spent",
            policy=_policy(gate_blocked_transitions=True),
        )
        assert parked.allowed, parked.reason
        assert _story(project)["state"] == "blocked"

    def test_blocking_does_not_waive_any_other_receipt_check(self, tmp_path):
        """The carve-out is one refusal wide. An invalid receipt still blocks
        the gated `-> blocked` edge, so de-escalation is not a bypass."""
        project = _project(tmp_path)
        _seed(project)
        _bind(project, dispatch_id=DISPATCH_ID, attempt_id=DEAD_ATTEMPT, state="failed")
        _receipt(project, dispatch_id=DISPATCH_ID, attempt_id=DEAD_ATTEMPT, model=None)
        refused = ak.evaluate_advance(
            str(project),
            "US-001",
            "blocked",
            policy=_policy(gate_blocked_transitions=True, require_next_action_match=False),
        )
        assert not refused.allowed
        assert refused.code == ak.RECEIPT_INVALID


class TestOnePredicateTwoReaders:
    def test_the_verdict_is_the_refusal_code(self):
        """Two spellings of one fact is the shape of the defect. If these ever
        drift, a host reading the recovery verdict and a host reading the
        refusal code are back to describing different things."""
        assert sp.ATTEMPT_NOT_LIVE_VERDICT == ak.ATTEMPT_NOT_LIVE

    def test_the_kernel_and_the_planner_read_the_same_answer(self, tmp_path):
        project = _deadlocked(tmp_path)
        story = _story(project)
        assert ak.unadvanceable_attempt(story, "se")["attempt_state"] == "failed"
        assert ak.unadvanceable_attempt(story, "software-engineer") is not None
        assert ak.unadvanceable_attempt(story, "qe") is None

    def test_a_live_attempt_is_not_a_recovery(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        _bind(project, dispatch_id=DISPATCH_ID, attempt_id=DEAD_ATTEMPT, state="running")
        _receipt(project, dispatch_id=DISPATCH_ID, attempt_id=DEAD_ATTEMPT)
        assert ak.unadvanceable_attempt(_story(project), "se") is None
        selected = ak.next_action(str(project))
        assert selected["recovery"] is None
        assert selected["transition_to"] == "testing"

    def test_a_completed_attempt_is_not_a_recovery(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        _bind(
            project, dispatch_id=DISPATCH_ID, attempt_id=DEAD_ATTEMPT, state="completed"
        )
        _receipt(project, dispatch_id=DISPATCH_ID, attempt_id=DEAD_ATTEMPT)
        assert ak.unadvanceable_attempt(_story(project), "se") is None
        selected = ak.next_action(str(project))
        assert selected["recovery"] is None
        assert selected["transition_to"] == "testing"

    def test_an_unbound_stage_is_not_a_recovery(self, tmp_path):
        """No binding is no record, and no record is not a dead attempt."""
        project = _project(tmp_path)
        _seed(project)
        _receipt(project)
        assert ak.unadvanceable_attempt(_story(project), "se") is None
        assert ak.next_action(str(project))["transition_to"] == "testing"


class TestInadmissibleReceiptRecovery:
    """#741 -- a receipt can be unadvanceable for a reason `unadvanceable_attempt`
    cannot see: its ATTEMPT is perfectly live (state stayed `completed`, which
    is deliberately excluded from `_ATTEMPT_NOT_LIVE_STATES`), but the receipt's
    own bytes fail schema validation -- the pilot #714 shape, where `evidence`
    was filed as an object instead of a list of typed items.

    Same fix shape as #690: one predicate (`unadvanceable_receipt`), one
    aggregator (`inadmissible_receipts`) threaded as data into the pure
    `next_action`, and the SAME H3-F1 ladder / `RETRY_ATTEMPT` classification
    #690 already wired end to end -- confirmed decisions, not a second
    recovery concept. NO AUTO-REPAIR: the malformed receipt is archived, never
    rewritten.
    """

    def test_a_completed_attempt_with_an_invalid_receipt_gets_a_recovery(
        self, tmp_path
    ):
        """The regression the issue asks for, mirroring #690's own shape:
        `next_action` now recommends a governed recovery instead of the
        ordinary advance, and `begin_dispatch` authorizes the successor that
        recovery calls for -- no deadlock, no hand-edited state.
        """
        project = _deadlocked(tmp_path, attempt_state="completed")
        # Overwrite the receipt with the pilot #714 shape: `evidence` present
        # as an object rather than a list of typed items.
        _receipt(
            project,
            dispatch_id=DISPATCH_ID,
            attempt_id=DEAD_ATTEMPT,
            evidence={"note": "not a list"},
        )

        # The attempt itself is live by #690's own predicate -- this is a
        # DIFFERENT reason the receipt cannot advance.
        assert ak.unadvanceable_attempt(_story(project), "se") is None

        selected = ak.next_action(str(project))
        assert selected["action"] == "dispatch_se"
        assert selected["receipt_present"] is True
        assert selected["transition_to"] is None
        recovery = selected["recovery"]
        assert recovery is not None
        assert recovery["verdict"] == sp.RECEIPT_INADMISSIBLE_VERDICT
        assert recovery["role"] == "se"
        assert recovery["receipt_digest"]
        assert any("evidence" in e for e in recovery["errors"])

        # `advance` still refuses the malformed predecessor -- fail-closed,
        # per the issue's explicit expectation.
        refused = ak.execute_advance(
            str(project),
            "US-001",
            "testing",
            policy=_policy(require_next_action_match=False),
        )
        assert not refused.allowed
        assert refused.code == ak.RECEIPT_INVALID

        # `begin_dispatch` authorizes a successor: the waiver is verdict-
        # agnostic (#690's own machinery), and the successor reuses the SAME
        # retry-ladder classification a dead-attempt recovery would.
        granted = ak.execute_dispatch(str(project), "US-001", role="se", policy=_policy())
        assert granted.allowed, granted.reason
        contract = granted.extra["receipt_contract"]
        successor = contract["attempt_id"]
        assert successor and successor != DEAD_ATTEMPT
        assert contract["classification"] == ak.RETRY_ATTEMPT
        assert contract["prior_attempt_id"] == DEAD_ATTEMPT

        # The invalid receipt's bytes are archived -- never edited, never
        # deleted -- and tagged `.invalid-`, not `.failed-`: its attempt was
        # never recorded dead, only its evidence was refused.
        archived = Path(granted.extra["archived_receipt"])
        assert archived.is_file()
        assert ".invalid-" in archived.name
        assert ".failed-" not in archived.name
        canonical = (
            project / ".synaptory" / ".orchestrator" / "receipts" / "US-001-se.json"
        )
        assert not canonical.exists()

    def test_the_literal_verdict_is_the_kernel_refusal_code(self):
        """Same duplication discipline as `ATTEMPT_NOT_LIVE_VERDICT` /
        `ATTEMPT_NOT_LIVE` above -- pinned so the planner's recovery verdict
        and the kernel's refusal code cannot drift apart and reopen #690's
        agreement failure through a different pair of names."""
        assert sp.RECEIPT_INADMISSIBLE_VERDICT == ak.RECEIPT_INVALID

    def test_a_completed_attempt_with_a_valid_receipt_is_unaffected(self, tmp_path):
        """Non-regression: a normal completed producer attempt with a VALID
        receipt advances exactly as it did before #741."""
        project = _deadlocked(tmp_path, attempt_state="completed")
        story = _story(project)
        assert ak.unadvanceable_receipt(str(project), story, "se") is None
        selected = ak.next_action(str(project))
        assert selected["recovery"] is None
        assert selected["transition_to"] == "testing"

    def test_awaiting_acceptance_is_excluded_even_with_an_invalid_receipt(
        self, tmp_path
    ):
        """The human PO sign-off stage is explicitly out of #741's scope --
        `inadmissible_receipts` must not report a hit for it no matter what
        its receipt contains."""
        project = _project(tmp_path)
        _seed(project, state="awaiting_acceptance")
        _receipt(project, abbrev="po", evidence={"note": "not a list"})
        state = sp._read_state(str(project))
        assert ak.inadmissible_receipts(str(project), state) == {}
