"""Layer 1 -- an attempt must still be live, and still be THIS generation,
for its receipt to advance a story (#447, Epic #339 rows `rf_lease_fencing`
and `rf_attempt_cancellation`).

Three properties, and the third is what makes the first two safe to enable on
every host at once:

1. A receipt presenting a fencing token the binding no longer holds is refused.
   The token is the only refutable claim a runner can make about WHICH lease
   generation it holds, so a superseded generation landing a result is the
   split brain the token exists to prevent.
2. A receipt from an attempt recorded as cancelled, expired or failed is
   refused. "A cancellation that only stops future work while its in-flight
   result still lands is not a cancellation." `completed` is deliberately NOT
   in that set: an attempt that closed successfully is exactly the one whose
   receipt should advance.
3. Every check here fires on MISMATCH or on a PRESENT terminal state, never on
   absence, and none of them is gated on `require_dispatch_binding` any more.
   That combination is why lifting them out of the host flag relaxes nothing
   and refuses nothing that advances today: the fields they read are written by
   the coordination lane, and a receipt that carries none of them advances
   exactly as it did before this landed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import advance_kernel as ak
import runtime_contracts as rc


pytestmark = pytest.mark.unit

STAGE_ENTERED = "2026-01-01T00:00:00+00:00"
FUTURE = "2090-01-01T00:00:00+00:00"


def _project(tmp_path: Path) -> Path:
    (tmp_path / ".synaptory" / ".orchestrator" / "receipts").mkdir(parents=True)
    (tmp_path / ".synaptory.yaml").write_text("build_mode: scrum\n", encoding="utf-8")
    return tmp_path


def _seed(project: Path, *, story_id: str = "US-001", state: str = "testing") -> None:
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


#: What a governed dispatch's receipt ALREADY has to carry. `receipt_validator`
#: refuses a receipt with no `stage_profile`/`capability_profile` when the
#: binding names a runtime profile, because the kernel handed the agent an
#: envelope carrying them (#402, #473). That rule is the precedent for this
#: file's fencing-presence tests: the governed path already requires the
#: identity fields it supplies, and the lease generation was the one such field
#: still treated as optional.
_GOVERNED_OVERLAY = {"stage_profile": "verifying", "capability_profile": "prover"}


def _bind(project: Path, *, story_id: str = "US-001", abbrev: str = "qe", **fields):
    path = project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
    state = json.loads(path.read_text())
    for story in state["current_stories"]:
        if story["id"] == story_id:
            story.setdefault("mcp_active_dispatches", {})[abbrev] = dict(fields)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _receipt(project: Path, *, story_id: str = "US-001", abbrev: str = "qe", **extra):
    receipts = project / ".synaptory" / ".orchestrator" / "receipts"
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


def _claude_shaped(**kwargs) -> ak.HostPolicy:
    """A policy shaped like `policy_for_claude`: the dispatch-binding flag OFF.

    Every test in this file uses it, because "these checks run even where
    `require_dispatch_binding` is false" IS the change, and a policy with the
    flag on could not tell the difference.
    """
    kwargs.setdefault("host", "test")
    kwargs.setdefault("require_next_action_match", False)
    kwargs.setdefault("require_dispatch_binding", False)
    return ak.HostPolicy(**kwargs)


class TestFencingTokenStaleness:
    def test_a_superseded_fencing_token_cannot_advance(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        _bind(
            project,
            dispatch_id="a" * 32,
            attempt_id="att_0000000001",
            fencing_token="generation-2",
        )
        _receipt(
            project,
            dispatch_id="a" * 32,
            attempt_id="att_0000000001",
            fencing_token="generation-1",
        )
        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_claude_shaped()
        )
        assert not decision.allowed
        assert decision.code == ak.FENCING_TOKEN_STALE

    def test_the_current_generation_advances(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        _bind(
            project,
            dispatch_id="a" * 32,
            attempt_id="att_0000000001",
            fencing_token="generation-2",
        )
        _receipt(
            project,
            dispatch_id="a" * 32,
            attempt_id="att_0000000001",
            fencing_token="generation-2",
        )
        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_claude_shaped()
        )
        assert decision.allowed, decision.reason

    def test_an_ungoverned_receipt_that_stamps_no_token_still_advances(self, tmp_path):
        """The normal Claude pipeline, which no bridge stamps.

        `execute_dispatch` mints a fencing token for EVERY dispatch, including
        ordinary SE work where the agent writes its own receipt by hand. This
        binding records no runtime family, so the kernel never placed the
        attempt on a governed runtime and nothing was ever going to stamp the
        generation onto the receipt. Requiring it here would refuse the normal
        pipeline, which is why the presence rule below is keyed on placement
        rather than applied to everything.
        """
        project = _project(tmp_path)
        _seed(project)
        _bind(project, dispatch_id="a" * 32, fencing_token="generation-2")
        _receipt(project, dispatch_id="a" * 32)
        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_claude_shaped()
        )
        assert decision.allowed, decision.reason

    def test_a_governed_receipt_that_stamps_no_token_is_refused(self, tmp_path):
        """Asserts impossible: opting out of the fence by saying nothing.

        The comparison beside this one only fires when BOTH tokens are present,
        so omitting the field skipped it entirely and a superseded runner had a
        cheaper move than presenting an old token: present none. #592 measured
        the consequence end to end, with an active binding at `generation-2`, a
        matching attempt and dispatch, and a receipt omitting only the token:
        `execute_advance` returned allowed, persisted the transition, and
        consumed the binding.

        `runtime_family` on the binding is what makes this answerable. The
        kernel writes it at dispatch when runtime selection SELECTS a profile,
        which is the same branch that builds the envelope the bridge runs
        under, and `stampReceiptIdentity` stamps the generation onto every
        receipt that bridge produces (#607). So on this path the token is owed,
        and its absence is a missing proof rather than an unfamiliar agent.
        """
        project = _project(tmp_path)
        _seed(project)
        _bind(
            project,
            dispatch_id="a" * 32,
            fencing_token="generation-2",
            runtime_family="claude-code",
        )
        _receipt(project, dispatch_id="a" * 32, **_GOVERNED_OVERLAY)
        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_claude_shaped()
        )
        assert not decision.allowed, decision.reason
        assert decision.code == ak.FENCING_TOKEN_MISSING

    def test_the_governed_board_does_not_move_on_the_omission(self, tmp_path):
        """The board, not the decision object. #608's own lesson.

        The reproduction that found this did not merely get an `allowed` back:
        it persisted `testing -> reviewing` and popped the binding. A refusal
        that returns False while the world still moves is the refusal that
        consumed the dispatch, so the assertion is on the file.
        """
        project = _project(tmp_path)
        _seed(project)
        _bind(
            project,
            dispatch_id="a" * 32,
            fencing_token="generation-2",
            runtime_family="claude-code",
        )
        _receipt(project, dispatch_id="a" * 32, **_GOVERNED_OVERLAY)
        ak.execute_advance(
            str(project), "US-001", "reviewing", policy=_claude_shaped()
        )
        story = json.loads(
            (
                project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
            ).read_text()
        )["current_stories"][0]
        assert story["state"] == "testing"
        assert story["mcp_active_dispatches"]["qe"]["fencing_token"] == "generation-2"

    def test_a_governed_receipt_that_stamps_the_current_token_advances(self, tmp_path):
        """The positive control, so the rule is a fence and not a wall.

        Without this, refusing every governed receipt would also pass the two
        assertions above.
        """
        project = _project(tmp_path)
        _seed(project)
        _bind(
            project,
            dispatch_id="a" * 32,
            fencing_token="generation-2",
            runtime_family="claude-code",
        )
        _receipt(
            project,
            dispatch_id="a" * 32,
            fencing_token="generation-2",
            **_GOVERNED_OVERLAY,
        )
        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_claude_shaped()
        )
        assert decision.allowed, decision.reason

    def test_absence_is_not_downgradable_under_warn(self, tmp_path):
        """Same class as staleness: authority, not evidence strength.

        A warned-past authority failure is the cancellation that did not
        cancel, and absence buys exactly what staleness buys if it can be
        warned past.
        """
        assert ak.FENCING_TOKEN_MISSING not in ak._EVIDENCE_CODES
        project = _project(tmp_path)
        _seed(project)
        _bind(
            project,
            dispatch_id="a" * 32,
            fencing_token="generation-2",
            runtime_family="claude-code",
        )
        _receipt(project, dispatch_id="a" * 32, **_GOVERNED_OVERLAY)
        decision = ak.evaluate_advance(
            str(project),
            "US-001",
            "reviewing",
            policy=_claude_shaped(enforcement="warn"),
        )
        assert not decision.allowed
        assert decision.code == ak.FENCING_TOKEN_MISSING

    def test_staleness_is_not_downgradable_under_warn(self, tmp_path):
        """A warned-past authority failure is not a fence. Contrast
        ATTEMPT_BINDING_MISMATCH, which IS downgradable because it is a
        statement about evidence identity rather than about permission."""
        assert ak.FENCING_TOKEN_STALE not in ak._EVIDENCE_CODES
        project = _project(tmp_path)
        _seed(project)
        _bind(project, attempt_id="att_0000000001", fencing_token="generation-2")
        _receipt(project, attempt_id="att_0000000001", fencing_token="generation-1")
        decision = ak.evaluate_advance(
            str(project),
            "US-001",
            "reviewing",
            policy=_claude_shaped(enforcement="warn"),
        )
        assert not decision.allowed
        assert decision.code == ak.FENCING_TOKEN_STALE


class TestAttemptLiveness:
    @pytest.mark.parametrize("state", sorted(ak._ATTEMPT_NOT_LIVE_STATES))
    def test_an_attempt_that_is_no_longer_live_cannot_advance(self, tmp_path, state):
        project = _project(tmp_path)
        _seed(project)
        _bind(project, dispatch_id="a" * 32, attempt_id="att_0000000001", state=state)
        _receipt(project, dispatch_id="a" * 32, attempt_id="att_0000000001")
        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_claude_shaped()
        )
        assert not decision.allowed
        assert decision.code == ak.ATTEMPT_NOT_LIVE
        assert state in decision.reason

    def test_the_refusal_set_is_terminal_minus_completed(self):
        """`completed` is the success path the bridge produces. Refusing it
        would mean no attempt could ever land its own result."""
        assert ak._ATTEMPT_NOT_LIVE_STATES < rc.TERMINAL_ATTEMPT_STATES
        assert "completed" not in ak._ATTEMPT_NOT_LIVE_STATES
        assert rc.TERMINAL_ATTEMPT_STATES - ak._ATTEMPT_NOT_LIVE_STATES == {"completed"}

    def test_a_completed_attempt_advances(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        _bind(
            project,
            dispatch_id="a" * 32,
            attempt_id="att_0000000001",
            state="completed",
        )
        _receipt(project, dispatch_id="a" * 32, attempt_id="att_0000000001")
        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_claude_shaped()
        )
        assert decision.allowed, decision.reason

    @pytest.mark.parametrize("state", ["", "pending", "claimed", "running"])
    def test_a_live_or_unrecorded_attempt_advances(self, tmp_path, state):
        project = _project(tmp_path)
        _seed(project)
        _bind(
            project, dispatch_id="a" * 32, attempt_id="att_0000000001", state=state
        )
        _receipt(project, dispatch_id="a" * 32, attempt_id="att_0000000001")
        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_claude_shaped()
        )
        assert decision.allowed, decision.reason

    def test_liveness_is_not_downgradable_under_warn(self):
        assert ak.ATTEMPT_NOT_LIVE not in ak._EVIDENCE_CODES


class TestChecksAreNoLongerHostGated:
    """The half that makes `rf_attempt_identity_binding` close on Claude.

    `policy_for_claude` ships `require_dispatch_binding=False` and always has.
    Until #447 that flag also gated the attempt-identity refusals, so the host
    that recorded every field enforced none of them.
    """

    def test_a_foreign_attempt_is_refused_without_the_dispatch_binding_flag(
        self, tmp_path
    ):
        project = _project(tmp_path)
        _seed(project)
        _bind(project, dispatch_id="a" * 32, attempt_id="att_0000000001")
        _receipt(project, dispatch_id="a" * 32, attempt_id="att_9999999999")
        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_claude_shaped()
        )
        assert not decision.allowed
        assert decision.code == ak.ATTEMPT_BINDING_MISMATCH

    def test_a_disagreeing_manifest_hash_is_refused_without_the_flag(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        _bind(
            project,
            dispatch_id="a" * 32,
            attempt_id="att_0000000001",
            manifest_hash="sha256:" + "1" * 64,
        )
        _receipt(
            project,
            dispatch_id="a" * 32,
            attempt_id="att_0000000001",
            manifest_hash="sha256:" + "2" * 64,
        )
        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_claude_shaped()
        )
        assert not decision.allowed
        assert decision.code == ak.MANIFEST_DISAGREEMENT

    def test_but_an_absent_dispatch_id_is_still_only_refused_where_the_flag_is_on(
        self, tmp_path
    ):
        """The one check that fires on ABSENCE stays host-gated. Lifting it
        would refuse every existing Claude receipt, none of which stamps a
        dispatch id."""
        project = _project(tmp_path)
        _seed(project)
        _bind(project, dispatch_id="a" * 32, attempt_id="att_0000000001")
        _receipt(project, attempt_id="att_0000000001")
        assert ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_claude_shaped()
        ).allowed
        strict = ak.evaluate_advance(
            str(project),
            "US-001",
            "reviewing",
            policy=_claude_shaped(require_dispatch_binding=True),
        )
        assert not strict.allowed
        assert strict.code == ak.DISPATCH_BINDING_MISMATCH
