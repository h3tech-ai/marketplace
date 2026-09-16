"""Layer 1 -- the attempt history and its classification (#492, Epic #410).

`capability-profile-pilot.md` section 5, S1 says the scenario passes "when
attempt history distinguishes steer from retry from fresh dispatch by
classification alone". Three pieces were missing and each is asserted here:

1. `prior_attempt_id` existed in ADR-031 section 11 and in both proposals and
   in no line of code, so nothing said what a new attempt was a response to,
2. `reconcile_dispatch` took no `failure_class`, so `cancelled-by-human` was a
   member of the versioned vocabulary, the control plane's own default at
   `cancel/ack`, and unable to reach the board,
3. `mcp_active_dispatches[abbrev]` is overwritten WHOLE at re-dispatch, so the
   superseded attempt's outcome was erased by its successor.

WHAT THE CLASSIFICATION IS ESTABLISHED FROM, and this is the point of the
design rather than a detail of it: the board, never a transcript. Nothing here
reads a model's words, and the classification is not a parameter on any verb --
`_classify_new_attempt` computes it from state whose only writer is the kernel
(ADR-029), at the one moment the predecessor is still visible.

WHY THE QE STAGE. Most of these dispatch a successor, and on the SE stage a
successor cannot be minted at all: `_execute_dispatch_locked` refuses
`dispatch_se` against an `in_progress` story as `dispatch_already_started`
whether or not the stage's attempt is terminal. That dead end is real, it is
NOT closed by this ticket (dispatch eligibility is #402's single-writer area
and loosening a concurrency guard deserves its own review), and
`test_the_se_stage_has_no_edge_for_a_steers_successor` pins it so it is a
recorded limitation rather than a surprise.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

import advance_kernel as ak


pytestmark = pytest.mark.unit

STAGE_ENTERED = "2026-01-01T00:00:00+00:00"
FUTURE = "2090-01-01T00:00:00+00:00"


def _project(tmp_path: Path, *, build_mode: str = "scrum") -> Path:
    orch = tmp_path / ".synaptory" / ".orchestrator"
    (orch / "receipts").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".synaptory.yaml").write_text(
        "build_mode: %s\n" % build_mode, encoding="utf-8"
    )
    return tmp_path


def _seed(project: Path, *, story_id: str = "US-001", state: str = "testing") -> dict:
    story = {
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
    payload = {
        "version": "2.0",
        "build_mode": "scrum",
        "lifecycle_state": "SPRINT_EXECUTION",
        "current_sprint": 1,
        "cumulative_ticket_number": 1,
        "sprint_goal": "seed",
        "pipeline_log": [],
        "current_stories": [story],
    }
    _write(project, payload)
    return payload


def _state_path(project: Path) -> Path:
    return project / ".synaptory" / ".orchestrator" / "pipeline-state.json"


def _write(project: Path, payload: dict) -> None:
    _state_path(project).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _story(project: Path, story_id: str = "US-001") -> dict:
    state = json.loads(_state_path(project).read_text(encoding="utf-8"))
    for story in state["current_stories"]:
        if story["id"] == story_id:
            return story
    raise AssertionError("story %s left the board" % story_id)


def _policy(**kwargs) -> ak.HostPolicy:
    kwargs.setdefault("host", "test")
    kwargs.setdefault("require_next_action_match", False)
    kwargs.setdefault("require_dispatch_binding", True)
    return ak.HostPolicy(**kwargs)


def _dispatch(project: Path, role: str = "quality-engineer") -> ak.Decision:
    return ak.execute_dispatch(str(project), "US-001", role, policy=_policy())


def _binding(project: Path, abbrev: str = "qe") -> dict:
    return _story(project).get("mcp_active_dispatches", {}).get(abbrev) or {}


def _history(project: Path, abbrev: str = "qe"):
    return ak.attempt_history(_story(project), abbrev)


def _live_attempt(project: Path, abbrev: str = "qe") -> str:
    return str(_binding(project, abbrev).get("attempt_id") or "")


def _cancel(project: Path, attempt_id: str, abbrev: str = "qe", **kwargs):
    """A human steer, projected the way the shipped directive channel does it:
    the attempt's own identity, and a terminal state."""
    return ak.reconcile_dispatch(
        str(project), "US-001", abbrev, attempt_id=attempt_id,
        attempt_state="cancelled", **kwargs
    )


def _receipt(project: Path, *, story_id: str = "US-001", abbrev: str = "qe", **extra):
    receipts = project / ".synaptory" / ".orchestrator" / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    src = project / "src" / "foo.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("x\n", encoding="utf-8")
    payload = {
        "story_id": story_id,
        "role": "quality-engineer",
        "backend": "claude",
        "model": "opus",
        "artifacts": ["src/foo.py"],
        "verification_commands": ["true"],
        "metrics": {"n": 1},
        "completed_at": FUTURE,
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
    payload.update(extra)
    path = receipts / ("%s-%s.json" % (story_id, abbrev))
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return path


# ── the three cases, each established from stored fields ──────────────────────


class TestTheThreeCasesAreDistinguishableFromTheBoard:
    """S1's whole claim. Each case is answered by reading one stored field,
    with no ordering comparison and no timestamp arithmetic at read time."""

    def test_a_first_attempt_for_a_stage_is_fresh(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        assert _dispatch(project).allowed
        record = _history(project)[-1]
        assert record["classification"] == ak.FRESH_ATTEMPT
        assert "prior_attempt_id" not in record, (
            "a fresh dispatch has nothing to be a response to, so naming a "
            "predecessor would be an invented one"
        )
        assert "prior_attempt_id" not in _binding(project)

    def test_a_human_cancel_makes_the_next_attempt_a_steer(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        assert _dispatch(project).allowed
        first = _live_attempt(project)
        assert _cancel(project, first).allowed
        assert _dispatch(project).allowed

        record = _history(project)[-1]
        assert record["classification"] == ak.STEER_ATTEMPT
        assert record["prior_attempt_id"] == first
        # WHY it says steer, on the successor's own record, so the answer does
        # not require dereferencing the predecessor.
        assert record["prior_failure_class"] == ak.HUMAN_CANCEL_FAILURE_CLASS

    def test_a_technical_failure_makes_the_next_attempt_a_retry(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        assert _dispatch(project).allowed
        first = _live_attempt(project)
        assert ak.reconcile_dispatch(
            str(project), "US-001", "qe", attempt_id=first,
            attempt_state="failed", failure_class="runtime-death",
        ).allowed
        assert _dispatch(project).allowed

        record = _history(project)[-1]
        assert record["classification"] == ak.RETRY_ATTEMPT
        assert record["prior_attempt_id"] == first
        assert record["prior_failure_class"] == "runtime-death"

    def test_all_three_are_different_answers_on_one_board(self, tmp_path):
        """The negative control. Distinguishability is a property of the three
        together: a scheme that labelled everything `retry` would satisfy each
        test above that happened to expect `retry`."""
        project = _project(tmp_path)
        _seed(project)
        assert _dispatch(project).allowed
        first = _live_attempt(project)
        assert _cancel(project, first).allowed
        assert _dispatch(project).allowed
        second = _live_attempt(project)
        assert ak.reconcile_dispatch(
            str(project), "US-001", "qe", attempt_id=second,
            attempt_state="failed", failure_class="deterministic-check",
        ).allowed
        assert _dispatch(project).allowed

        classes = [r["classification"] for r in _history(project)]
        assert classes == [ak.FRESH_ATTEMPT, ak.STEER_ATTEMPT, ak.RETRY_ATTEMPT]
        assert len(set(classes)) == 3
        assert set(classes) <= set(ak.ATTEMPT_CLASSIFICATIONS)

    def test_a_fresh_dispatch_of_another_stage_is_neither(self, tmp_path):
        """Per role, like the authorization ledger it extends. A QE attempt is
        not a retry of the SE attempt that preceded it on the same Work Unit;
        they are different work, and merging them would report every normal
        pipeline as a ladder of retries."""
        project = _project(tmp_path)
        _seed(project, state="queued")
        assert _dispatch(project, "software-engineer").allowed
        se_attempt = _live_attempt(project, "se")
        assert _cancel(project, se_attempt, "se").allowed

        story = _story(project)
        story["state"] = "testing"
        story["pipeline_log"].append(
            {"state": "testing", "entered_at": STAGE_ENTERED, "exited_at": None}
        )
        state = json.loads(_state_path(project).read_text(encoding="utf-8"))
        state["current_stories"][0] = story
        _write(project, state)

        assert _dispatch(project, "quality-engineer").allowed
        qe_record = _history(project, "qe")[-1]
        assert qe_record["classification"] == ak.FRESH_ATTEMPT
        assert "prior_attempt_id" not in qe_record
        # And the SE record is untouched by the QE dispatch.
        assert _history(project, "se")[-1]["classification"] == ak.FRESH_ATTEMPT


# ── the superseded attempt outlives its successor ─────────────────────────────


class TestASupersededAttemptSurvivesItsSuccessor:
    def test_the_predecessors_outcome_is_still_on_the_board(self, tmp_path):
        """`dispatches[abbrev] = binding` is a whole-value overwrite, so this
        is the assertion the ledger exists for: strip the live slot and the
        cancelled attempt's identity, state and class are all still there."""
        project = _project(tmp_path)
        _seed(project)
        assert _dispatch(project).allowed
        first = _live_attempt(project)
        assert _cancel(project, first).allowed
        assert _dispatch(project).allowed
        second = _live_attempt(project)

        assert second != first
        story = _story(project)
        survivor = dict(story)
        survivor.pop("mcp_active_dispatches", None)
        assert first in json.dumps(survivor)

        record = next(r for r in _history(project) if r["attempt_id"] == first)
        assert record["state"] == "cancelled"
        assert record["failure_class"] == ak.HUMAN_CANCEL_FAILURE_CLASS
        assert record["superseded_by"] == second
        assert record["superseded_at"]

    def test_the_dead_generations_fencing_token_is_not_copied_forward(self, tmp_path):
        """Deliberate omission. A superseded attempt's token is read by
        nothing, and a dead generation duplicated onto the board is a value a
        stale runner could be handed back."""
        project = _project(tmp_path)
        _seed(project)
        assert _dispatch(project).allowed
        first = _live_attempt(project)
        token = _binding(project)["fencing_token"]
        assert _cancel(project, first).allowed
        assert _dispatch(project).allowed

        record = next(r for r in _history(project) if r["attempt_id"] == first)
        assert "fencing_token" not in record
        assert token not in json.dumps(record)

    def test_an_attempt_whose_stage_advanced_is_not_an_abandoned_one(self, tmp_path):
        """The commit path POPS the binding, which is the other way an attempt
        leaves the board and the good one: its work landed. Without this the
        history could not tell a successful attempt from one dropped
        mid-flight, which is the epic's own rule about two facts sharing a
        representation."""
        project = _project(tmp_path)
        _seed(project)
        assert _dispatch(project).allowed
        attempt = _live_attempt(project)
        binding = _binding(project)
        _receipt(
            project,
            dispatch_id=binding["dispatch_id"],
            attempt_id=attempt,
            fencing_token=binding["fencing_token"],
        )
        decision = ak.execute_advance(
            str(project), "US-001", "reviewing", policy=_policy()
        )
        assert decision.allowed, decision.reason

        assert "qe" not in (_story(project).get("mcp_active_dispatches") or {})
        record = next(r for r in _history(project) if r["attempt_id"] == attempt)
        assert record["advanced_at"]
        assert "superseded_by" not in record


# ── failure_class reaches the board, through the shipped projection verb ──────


class TestFailureClassReachesTheBoard:
    def test_a_bare_cancellation_records_a_human_cancel_and_says_it_inferred_it(
        self, tmp_path
    ):
        """The default is the control plane's own: `/cancel` with no live
        claimant sets `cancelled-by-human` itself, `cancel/ack` defaults to
        it, and only a supervisor can request a cancellation at all. The
        SOURCE field is what keeps that inference from reading as a report."""
        project = _project(tmp_path)
        _seed(project)
        assert _dispatch(project).allowed
        assert _cancel(project, _live_attempt(project)).allowed

        binding = _binding(project)
        assert binding["failure_class"] == ak.HUMAN_CANCEL_FAILURE_CLASS
        assert binding["failure_class_source"] == ak.CLASS_KERNEL_DEFAULT
        assert _history(project)[-1]["failure_class_source"] == ak.CLASS_KERNEL_DEFAULT

    def test_a_reported_class_is_recorded_as_reported(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        assert _dispatch(project).allowed
        assert _cancel(
            project, _live_attempt(project),
            failure_class=ak.HUMAN_CANCEL_FAILURE_CLASS,
        ).allowed
        binding = _binding(project)
        assert binding["failure_class"] == ak.HUMAN_CANCEL_FAILURE_CLASS
        assert binding["failure_class_source"] == ak.CLASS_REPORTED

    def test_a_failed_attempt_with_no_class_stays_unclassified(self, tmp_path):
        """The default is narrow on purpose. Nothing here knows why a `failed`
        attempt failed, and inventing a cause is worse than an absent field."""
        project = _project(tmp_path)
        _seed(project)
        assert _dispatch(project).allowed
        assert ak.reconcile_dispatch(
            str(project), "US-001", "qe", attempt_id=_live_attempt(project),
            attempt_state="failed",
        ).allowed
        assert "failure_class" not in _binding(project)

    def test_the_default_fires_on_a_reported_cancel_not_on_a_stored_one(self, tmp_path):
        """The inference is narrowed to the state THIS CALL reports. A board
        that already reads `cancelled` with no class was classified by nobody,
        and classifying it on the occasion of an unrelated fencing-token
        reconciliation would attribute a human cancellation to an event that
        did not report one."""
        project = _project(tmp_path)
        payload = _seed(project)
        payload["current_stories"][0]["mcp_active_dispatches"] = {
            "qe": {
                "dispatch_id": "a" * 32,
                "attempt_id": "att_0000000001",
                "fencing_token": "000000000001",
                "state": "cancelled",
            }
        }
        _write(project, payload)
        assert ak.reconcile_dispatch(
            str(project), "US-001", "qe", attempt_id="att_0000000001",
            fencing_token="000000000002",
        ).allowed
        assert "failure_class" not in _binding(project)

    def test_a_class_outside_the_versioned_vocabulary_is_refused(self, tmp_path):
        """Closed, for the same reason the attempt state's vocabulary is
        closed: `cancelled-by-humans` stored happily would read downstream as
        NO classification, which is a technical failure."""
        project = _project(tmp_path)
        _seed(project)
        assert _dispatch(project).allowed
        decision = _cancel(
            project, _live_attempt(project), failure_class="cancelled-by-humans"
        )
        assert not decision.allowed
        assert decision.code == ak.POLICY_REFUSED
        assert "failure_class" not in _binding(project)

    def test_unclassified_may_not_close_an_attempt_on_the_board(self, tmp_path):
        """SP-INT-017, the same refusal the control plane makes at close and
        `runtime_contracts` makes on a receipt. A runner that does not know
        narrows to `runtime-death`, which is a cause; `unclassified` is the
        absence of one, and it would give a caller a way to end an attempt on
        the board without saying why."""
        project = _project(tmp_path)
        _seed(project)
        assert _dispatch(project).allowed
        decision = ak.reconcile_dispatch(
            str(project), "US-001", "qe", attempt_id=_live_attempt(project),
            attempt_state="failed", failure_class="unclassified",
        )
        assert not decision.allowed
        assert decision.code == ak.POLICY_REFUSED
        assert "runtime-death" in decision.reason

    def test_a_class_cannot_be_relabelled_once_it_is_on_the_board(self, tmp_path):
        """The anti-relabelling rule. A late callback that could rewrite
        `cancelled-by-human` as `runtime-death` would erase a steer after the
        fact and turn it back into a retry in every count downstream."""
        project = _project(tmp_path)
        _seed(project)
        assert _dispatch(project).allowed
        attempt = _live_attempt(project)
        assert _cancel(project, attempt).allowed
        decision = ak.reconcile_dispatch(
            str(project), "US-001", "qe", attempt_id=attempt,
            failure_class="runtime-death",
        )
        assert not decision.allowed
        assert decision.code == ak.POLICY_REFUSED
        assert _binding(project)["failure_class"] == ak.HUMAN_CANCEL_FAILURE_CLASS

    def test_repeating_the_same_class_is_idempotent(self, tmp_path):
        """A retried callback must not be a refusal."""
        project = _project(tmp_path)
        _seed(project)
        assert _dispatch(project).allowed
        attempt = _live_attempt(project)
        assert _cancel(project, attempt).allowed
        assert ak.reconcile_dispatch(
            str(project), "US-001", "qe", attempt_id=attempt,
            failure_class=ak.HUMAN_CANCEL_FAILURE_CLASS,
        ).allowed

    def test_a_class_alone_is_something_to_reconcile(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        assert _dispatch(project).allowed
        decision = ak.reconcile_dispatch(
            str(project), "US-001", "qe", attempt_id=_live_attempt(project),
            failure_class="budget-exhausted",
        )
        assert decision.allowed, decision.reason
        assert _binding(project)["failure_class"] == "budget-exhausted"

    def test_a_superseded_attempt_cannot_classify_the_one_that_replaced_it(
        self, tmp_path
    ):
        """The #396 guard still holds over the new field: a delayed callback
        from the predecessor cannot write a class onto its successor's
        binding, which is how a fresh retry would be relabelled a steer."""
        project = _project(tmp_path)
        _seed(project)
        assert _dispatch(project).allowed
        first = _live_attempt(project)
        assert _cancel(project, first).allowed
        assert _dispatch(project).allowed

        decision = ak.reconcile_dispatch(
            str(project), "US-001", "qe", attempt_id=first,
            failure_class="budget-exhausted",
        )
        assert not decision.allowed
        assert decision.code == ak.ATTEMPT_BINDING_MISMATCH
        assert "failure_class" not in _binding(project)


# ── forgeability: who writes the classification ───────────────────────────────


class TestTheClassificationIsWrittenByTheKernel:
    """The epic's mandatory review, on this ticket's own surface.

    Untrusted input: whatever an agent can hand a dispatch verb. Forgery: an
    agent labelling its own retry a steer to keep a retry counter clean, or a
    steer a fresh dispatch to hide that a human intervened.
    """

    def test_no_dispatch_verb_accepts_a_classification(self):
        """The refusing line is an ABSENCE, and an absence has to be pinned or
        a later signature change reopens it silently."""
        for verb in (
            ak.execute_dispatch,
            ak.evaluate_dispatch,
            ak.reconcile_dispatch,
            ak.execute_advance,
            ak.evaluate_advance,
        ):
            params = set(inspect.signature(verb).parameters)
            assert "classification" not in params, verb.__name__
            assert "prior_attempt_id" not in params, verb.__name__

    def test_the_classification_is_a_function_of_the_board_alone(self, tmp_path):
        """Same board, same answer, and the only input is state the kernel
        wrote. `_classify_new_attempt` takes a story and a role abbrev; there
        is no receipt, no transcript and no caller argument in reach."""
        project = _project(tmp_path)
        _seed(project)
        assert _dispatch(project).allowed
        assert _cancel(project, _live_attempt(project)).allowed
        story = _story(project)
        assert set(inspect.signature(ak._classify_new_attempt).parameters) == {
            "story", "abbrev"
        }
        first = ak._classify_new_attempt(story, "qe")
        assert ak._classify_new_attempt(story, "qe") == first
        assert first["classification"] == ak.STEER_ATTEMPT

    def test_a_receipt_cannot_widen_or_relabel_the_history(self, tmp_path):
        """The history is derived from kernel-written state, so an attacker
        who controls only the receipt cannot add to it or edit it. Asserted by
        writing a receipt that claims both a lineage and a class, and reading
        the board back."""
        project = _project(tmp_path)
        _seed(project)
        assert _dispatch(project).allowed
        attempt = _live_attempt(project)
        binding = _binding(project)
        _receipt(
            project,
            dispatch_id=binding["dispatch_id"],
            attempt_id=attempt,
            fencing_token=binding["fencing_token"],
            classification=ak.STEER_ATTEMPT,
            prior_attempt_id="att_ffffffffffffffffffff",
            failure_class=ak.HUMAN_CANCEL_FAILURE_CLASS,
        )
        decision = ak.execute_advance(
            str(project), "US-001", "reviewing", policy=_policy()
        )
        assert decision.allowed, decision.reason
        records = _history(project)
        assert [r["attempt_id"] for r in records] == [attempt]
        assert records[0]["classification"] == ak.FRESH_ATTEMPT
        assert "prior_attempt_id" not in records[0]
        assert "att_ffffffffffffffffffff" not in json.dumps(_story(project))

    def test_buying_the_steer_label_costs_the_predecessor_its_receipt(self, tmp_path):
        """The residual, and why it is self-defeating rather than open.

        A caller that can reach the kernel's own verbs -- an agent with a
        shell, on any host -- can call `reconcile_dispatch` against its own
        attempt id and cancel itself, which would make its next attempt a
        `steer`. It cannot do that and keep its work: `cancelled` is terminal,
        the receipt it already wrote is refused as `attempt_not_live`, and
        `_state_regression` will not move the attempt back to a live state. So
        the forgery converts "my retry" into "my output is discarded", which
        is a price, not an escape.
        """
        project = _project(tmp_path)
        _seed(project)
        assert _dispatch(project).allowed
        attempt = _live_attempt(project)
        binding = _binding(project)
        _receipt(
            project,
            dispatch_id=binding["dispatch_id"],
            attempt_id=attempt,
            fencing_token=binding["fencing_token"],
        )
        assert _cancel(project, attempt).allowed

        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_policy()
        )
        assert not decision.allowed
        assert decision.code == ak.ATTEMPT_NOT_LIVE
        assert _story(project)["state"] == "testing"
        # And it cannot be undone to recover the receipt.
        revived = ak.reconcile_dispatch(
            str(project), "US-001", "qe", attempt_id=attempt, attempt_state="running"
        )
        assert not revived.allowed
        assert _binding(project)["state"] == "cancelled"


# ── the steer CONTENT stays out of V1 state ───────────────────────────────────


class TestTheSteerContentStaysOffTheBoard:
    """Ticket scope item 4, decided against carrying it.

    `rf_supervision_privacy` is the precedent: the supervision transport
    carries a per-kind key ALLOWLIST because content that reaches state has no
    redaction path, and `pipeline-state.json` is committed board state with
    none at all. The classification does not need the content -- a steer is
    established by the predecessor's cancellation class -- and the human's own
    words already have a home on the control plane (`cancel_reason`,
    `cancel_requested_by`).
    """

    ALLOWED_RECORD_KEYS = frozenset({
        "attempt_id", "authorized_at", "classification", "prior_attempt_id",
        "prior_failure_class", "state", "failure_class", "failure_class_source",
        "superseded_by", "superseded_at", "advanced_at",
    })

    def test_no_reconciliation_parameter_can_carry_content(self):
        params = set(inspect.signature(ak.reconcile_dispatch).parameters)
        assert params == {
            "project_dir", "story_id", "role", "attempt_id", "fencing_token",
            "attempt_state", "failure_class",
        }

    def test_every_history_field_is_on_a_closed_allowlist(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        assert _dispatch(project).allowed
        first = _live_attempt(project)
        assert _cancel(project, first).allowed
        assert _dispatch(project).allowed
        for record in _history(project):
            assert set(record) <= self.ALLOWED_RECORD_KEYS, record


# ── reading the history ───────────────────────────────────────────────────────


class TestTheHistoryReader:
    def test_it_accepts_a_role_name_or_its_abbrev(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        assert _dispatch(project).allowed
        story = _story(project)
        assert ak.attempt_history(story, "quality-engineer") == ak.attempt_history(
            story, "qe"
        )

    def test_it_hands_back_copies_so_a_reader_cannot_write_the_board(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        assert _dispatch(project).allowed
        story = _story(project)
        ak.attempt_history(story, "qe")[0]["classification"] = "forged"
        assert ak.attempt_history(story, "qe")[0]["classification"] == (
            ak.FRESH_ATTEMPT
        )

    def test_a_corrupt_ledger_reads_as_no_history_rather_than_raising(self):
        assert ak.attempt_history({ak.AUTHORIZED_ATTEMPTS_KEY: "junk"}, "qe") == ()
        assert ak.attempt_history({ak.AUTHORIZED_ATTEMPTS_KEY: {"qe": 7}}, "qe") == ()
        assert ak.attempt_history({ak.AUTHORIZED_ATTEMPTS_KEY: {"qe": [7, None]}}, "qe") == ()
        assert ak.attempt_history(None, "qe") == ()
        assert ak.attempt_history({}, "") == ()

    def test_a_read_never_writes_the_story(self):
        """The string-to-record normalization is on the write path only. A
        reader that rewrote state to answer a question would make every
        advance a state write."""
        story = {ak.AUTHORIZED_ATTEMPTS_KEY: {"qe": ["att_0000000001"]}}
        before = json.dumps(story, sort_keys=True)
        assert ak.attempt_history(story, "qe")[0]["attempt_id"] == "att_0000000001"
        assert ak._authorized_attempts(story, "qe") == ("att_0000000001",)
        assert json.dumps(story, sort_keys=True) == before


# ── migration: a story dispatched before this landed ──────────────────────────


class TestAPre492LedgerStillWorks:
    def test_a_bare_string_entry_keeps_its_authorization(self):
        """#493 shipped this ledger as a list of ids. A story mid-flight when
        #492 merges carries that shape, and reading it as anything other than
        "this attempt was authorized" would refuse honest attestations on the
        day it merged."""
        story = {ak.AUTHORIZED_ATTEMPTS_KEY: {"qe": ["att_0000000001"]}}
        assert ak._authorized_attempts(story, "qe") == ("att_0000000001",)
        assert ak.attempt_history(story, "qe") == ({"attempt_id": "att_0000000001"},)

    def test_a_bare_string_predecessor_still_classifies_its_successor(self, tmp_path):
        """It classifies as a RETRY, not a steer, and that is correct rather
        than convenient: a pre-#492 record carries no class, so nothing
        establishes a human cancellation, and inferring one would manufacture
        steers across the migration."""
        project = _project(tmp_path)
        payload = _seed(project)
        payload["current_stories"][0][ak.AUTHORIZED_ATTEMPTS_KEY] = {
            "qe": ["att_0000000001"]
        }
        _write(project, payload)
        assert _dispatch(project).allowed
        record = _history(project)[-1]
        assert record["classification"] == ak.RETRY_ATTEMPT
        assert record["prior_attempt_id"] == "att_0000000001"
        assert "prior_failure_class" not in record

    def test_a_legacy_binding_with_no_ledger_is_backfilled_not_lost(self, tmp_path):
        """The predecessor of the first post-#492 dispatch may exist only in
        the live binding. Backfilling it adds nothing to the authorization set
        (`_authorized_attempts` already unions the binding in) and everything
        to durability: without it that attempt's record is erased by the very
        overwrite the ledger exists to survive."""
        project = _project(tmp_path)
        payload = _seed(project)
        payload["current_stories"][0]["mcp_active_dispatches"] = {
            "qe": {
                "dispatch_id": "a" * 32,
                "attempt_id": "att_0000000001",
                "fencing_token": "000000000003",
                "state": "cancelled",
                "failure_class": ak.HUMAN_CANCEL_FAILURE_CLASS,
            }
        }
        _write(project, payload)
        assert _dispatch(project).allowed

        records = _history(project)
        assert [r["attempt_id"] for r in records][0] == "att_0000000001"
        assert records[0]["state"] == "cancelled"
        assert records[0]["superseded_by"] == _live_attempt(project)
        assert records[-1]["classification"] == ak.STEER_ATTEMPT


# ── ordering independence, and one recorded limitation ────────────────────────


class TestThePredecessorIsNotTheOldestRecord:
    def test_the_live_binding_wins_over_the_ledgers_first_entry(self, tmp_path):
        """The AC says the answer must not rely on ordering. At mint the live
        binding IS the immediate predecessor by construction, because it is
        the record about to be overwritten, so nothing sorts or compares
        timestamps to find it."""
        project = _project(tmp_path)
        _seed(project)
        assert _dispatch(project).allowed
        first = _live_attempt(project)
        assert ak.reconcile_dispatch(
            str(project), "US-001", "qe", attempt_id=first,
            attempt_state="failed", failure_class="environment",
        ).allowed
        assert _dispatch(project).allowed
        second = _live_attempt(project)
        assert _cancel(project, second).allowed

        # Shuffle the ledger so the oldest record is last. The live binding is
        # still the predecessor, so the answer must not move.
        payload = json.loads(_state_path(project).read_text(encoding="utf-8"))
        ledger = payload["current_stories"][0][ak.AUTHORIZED_ATTEMPTS_KEY]["qe"]
        ledger.reverse()
        _write(project, payload)

        assert _dispatch(project).allowed
        record = next(
            r for r in _history(project)
            if r["attempt_id"] == _live_attempt(project)
        )
        assert record["prior_attempt_id"] == second
        assert record["classification"] == ak.STEER_ATTEMPT

    def test_a_steer_on_the_producing_stage_mints_its_successor(self, tmp_path):
        """The producing stage is where S1's steer actually happens.

        This was pinned as a recorded limitation: `dispatch_se` was refused
        against an `in_progress` story as `dispatch_already_started` whether or
        not the stage's attempt was terminal, so a human cancel had no edge to
        mint the attempt ADR-031 section 11 says must follow it, and the
        classification machinery was correct and unreachable on the one stage
        the produce-verify loop is about.

        The guard is a CONCURRENCY guard: it exists to stop a second process
        starting work a first one claimed. A cancelled predecessor is not that,
        so it now refuses a LIVE incumbent and admits a successor (#396).
        """
        project = _project(tmp_path)
        _seed(project, state="queued")
        assert _dispatch(project, "software-engineer").allowed
        first = _live_attempt(project, "se")
        assert _cancel(project, first, "se").allowed

        decision = _dispatch(project, "software-engineer")
        assert decision.allowed, (decision.code, decision.reason)

        successor = _live_attempt(project, "se")
        assert successor and successor != first, (first, successor)

        record = next(
            (r for r in _history(project, "se") if r["attempt_id"] == successor),
            None,
        )
        assert record is not None, "the successor left no history record"
        assert record.get("classification") == "steer", record
        assert record.get("prior_attempt_id") == first, record

        # The predecessor's own history survives its successor.
        prior = next(
            (r for r in _history(project, "se") if r["attempt_id"] == first),
            None,
        )
        assert prior is not None, "the cancelled predecessor was forgotten"
        assert prior.get("state") == "cancelled", prior

    def test_a_live_incumbent_still_refuses_a_second_dispatch(self, tmp_path):
        """The narrowing must not cost the guard its reason for existing: two
        processes racing a queued story is what it is for, and an attempt
        nobody terminated is still somebody's."""
        project = _project(tmp_path)
        _seed(project, state="queued")
        assert _dispatch(project, "software-engineer").allowed

        decision = _dispatch(project, "software-engineer")
        assert not decision.allowed
        assert decision.code == ak.DISPATCH_ALREADY_STARTED
