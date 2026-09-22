"""Layer 1 -- an authorization this kernel destroyed is not one it never
issued (#822, #803).

`mcp_active_dispatches[role]` is overwritten WHOLE by the next dispatch of
that stage. A receipt produced under the previous one then arrives at the gate
naming an identity the live binding does not know, and the refusal said it
"claims dispatch X, but this kernel authorized no dispatch ... an unrefutable
claim of authority is not authority (#592)".

That wording is correct for a forged identity and wrong for this one. The
identity WAS issued, by this kernel, minutes earlier, and then destroyed by an
unrelated recovery. The reporting engagement read the forgery wording, went
looking at the producing agent, and re-ran a review round that had already
passed -- and in the same unit needed three separate recoveries before working
out what the lever had done.

BOTH STILL REFUSE. The receipt is genuinely unbound and crediting it would be
worse than refusing it; the evidence here is that the refusal now says which
of the two happened, and #803's half says it at the moment it is caused rather
than one or two transitions later.

WHAT IS NOT WEAKENED: the live binding is still what authorizes (#608). The
ledger is read only to word a refusal that has already been decided.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import advance_kernel as ak


pytestmark = pytest.mark.unit

STAGE_ENTERED = "2026-01-01T00:00:00+00:00"
AFTER = "2026-06-01T00:00:00+00:00"
BEFORE = "2025-01-01T00:00:00+00:00"


def _project(tmp_path: Path) -> Path:
    (tmp_path / ".synaptory" / ".orchestrator" / "receipts").mkdir(parents=True)
    (tmp_path / ".synaptory.yaml").write_text("build_mode: scrum\n", encoding="utf-8")
    return tmp_path


def _state_path(project: Path) -> Path:
    return project / ".synaptory" / ".orchestrator" / "pipeline-state.json"


def _seed(project: Path, *, state: str = "testing") -> None:
    story = {
        "id": "US-001", "title": "Seed story", "state": state,
        "blocked_reason": None, "blocked_from": None, "backend": {},
        "pipeline_log": [
            {"state": state, "entered_at": STAGE_ENTERED, "exited_at": None}
        ],
        "dod": None, "receipts": [], "acceptance_criteria": [], "kind": "",
        "labels": [], "depends_on": [], "file_scope": [], "retries": {},
        "rejection_feedback": [],
    }
    _state_path(project).write_text(json.dumps({
        "version": "2.0", "build_mode": "scrum",
        "lifecycle_state": "SPRINT_EXECUTION", "current_sprint": 1,
        "cumulative_ticket_number": 1, "sprint_goal": "seed",
        "pipeline_log": [], "current_stories": [story],
    }, indent=2), encoding="utf-8")


def _story(project: Path) -> dict:
    state = json.loads(_state_path(project).read_text(encoding="utf-8"))
    return state["current_stories"][0]


def _policy(**kwargs) -> ak.HostPolicy:
    kwargs.setdefault("host", "test")
    kwargs.setdefault("require_next_action_match", False)
    kwargs.setdefault("require_dispatch_binding", True)
    return ak.HostPolicy(**kwargs)


def _dispatch(project: Path) -> ak.Decision:
    return ak.execute_dispatch(
        str(project), "US-001", "quality-engineer", policy=_policy()
    )


def _binding(project: Path) -> dict:
    return _story(project).get("mcp_active_dispatches", {}).get("qe") or {}


def _receipt(project: Path, *, completed_at: str = AFTER, **extra) -> Path:
    src = project / "src" / "foo.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("x\n", encoding="utf-8")
    payload = {
        "story_id": "US-001", "role": "quality-engineer", "backend": "claude",
        "model": "opus", "artifacts": ["src/foo.py"],
        "verification_commands": ["true"], "metrics": {"n": 1},
        "completed_at": completed_at,
        "token_usage": {
            "input": 1, "output": 1, "cache_read": 0, "cache_write": 0,
            "stage": "qe-verification",
        },
        "story_dod": {
            "tests_pass": True, "build_succeeds": True,
            "no_critical_findings": True, "code_reviewed": True,
            "coverage_no_decrease": True,
        },
    }
    payload.update(extra)
    path = project / ".synaptory" / ".orchestrator" / "receipts" / "US-001-qe.json"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return path


def _supersede(project: Path) -> dict:
    """Two real dispatches for one stage. The second overwrites the first's
    binding, which is the mechanism under test -- driven through
    `execute_dispatch` rather than hand-written onto the board, so the ledger
    is populated by the code that will have to answer for it."""
    assert _dispatch(project).allowed
    first = dict(_binding(project))
    assert first.get("dispatch_id") and first.get("attempt_id")
    second = _dispatch(project)
    assert second.allowed, second.reason
    assert _binding(project)["dispatch_id"] != first["dispatch_id"]
    return first


# ── the refusal now says which of the two happened ────────────────────────────


class TestOrphanedIsNotForged:
    def test_a_superseded_dispatch_is_refused_as_orphaned(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        first = _supersede(project)
        _receipt(
            project,
            dispatch_id=first["dispatch_id"],
            attempt_id=first["attempt_id"],
        )

        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_policy()
        )

        assert not decision.allowed
        assert decision.code == ak.DISPATCH_BINDING_ORPHANED
        assert decision.code != ak.DISPATCH_BINDING_MISMATCH

    def test_the_reason_names_the_supersession_and_not_a_forgery(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        first = _supersede(project)
        successor = _binding(project)["attempt_id"]
        _receipt(
            project,
            dispatch_id=first["dispatch_id"],
            attempt_id=first["attempt_id"],
        )

        reason = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_policy()
        ).reason

        # What actually happened, in the order an operator needs it.
        assert "This kernel DID issue that dispatch" in reason
        assert first["attempt_id"] in reason
        assert successor in reason
        assert "orphaned, not forged" in reason
        # The wrong place to look, named so it is not looked at.
        assert "Do not go looking at the producing agent" in reason
        # And the way out, which the reporter had to find by experiment.
        assert "prior_attempt_id" in reason
        # The forgery accusation must be GONE from this case, not merely
        # joined by a better sentence.
        assert "#592" not in reason
        assert "never issued" not in reason

    def test_a_genuinely_unissued_dispatch_still_reads_as_forgery(self, tmp_path):
        """The #592 case is the reason the refusal exists and must survive
        intact. An identity absent from the ledger was never granted, and
        forgiving it to make the orphan case read nicely would be the exact
        hole #592 closed."""
        project = _project(tmp_path)
        _seed(project)
        _receipt(project, dispatch_id="f" * 32)

        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_policy()
        )

        assert not decision.allowed
        assert decision.code == ak.DISPATCH_BINDING_MISMATCH
        assert "#592" in decision.reason

    def test_a_live_attempts_receipt_still_advances(self, tmp_path):
        """The successor's own receipt is bound and must not be caught by any
        of this."""
        project = _project(tmp_path)
        _seed(project)
        _supersede(project)
        live = _binding(project)
        _receipt(
            project,
            dispatch_id=live["dispatch_id"],
            attempt_id=live["attempt_id"],
        )

        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_policy()
        )

        assert decision.allowed, decision.reason

    def test_the_new_code_is_downgradable_like_its_sibling(self):
        """It is a statement about the evidence's identity, exactly as
        `DISPATCH_BINDING_MISMATCH` is. A host that downgrades one and refuses
        the other would be made stricter by a change that only renames a
        refusal it already made."""
        assert ak.DISPATCH_BINDING_ORPHANED in ak._EVIDENCE_CODES
        assert ak.DISPATCH_BINDING_MISMATCH in ak._EVIDENCE_CODES


# ── the reader, and what it must refuse to answer ─────────────────────────────


class TestTheSupersededAuthorizationReader:
    def test_it_finds_the_record_by_the_dispatch_a_receipt_names(self, tmp_path):
        """A receipt names the dispatch. Before #822 the ledger recorded only
        the attempt, so the question could not be asked in the vocabulary the
        refusal holds."""
        project = _project(tmp_path)
        _seed(project)
        first = _supersede(project)

        record = ak.superseded_authorization(
            _story(project), "qe", dispatch_id=first["dispatch_id"]
        )

        assert record is not None
        assert record["attempt_id"] == first["attempt_id"]
        assert record["superseded_by"] == _binding(project)["attempt_id"]
        assert record["superseded_at"]

    def test_it_finds_the_record_by_attempt_for_a_receipt_that_carries_one(
        self, tmp_path
    ):
        project = _project(tmp_path)
        _seed(project)
        first = _supersede(project)

        assert ak.superseded_authorization(
            _story(project), "qe", attempt_id=first["attempt_id"]
        ) is not None

    def test_a_live_attempt_is_not_reported_superseded(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        assert _dispatch(project).allowed
        live = _binding(project)

        assert ak.superseded_authorization(
            _story(project), "qe", dispatch_id=live["dispatch_id"]
        ) is None

    def test_an_identity_the_ledger_never_recorded_is_not_an_orphan(self, tmp_path):
        """The load-bearing negative. If an unknown identity answered
        "orphaned", the forgery refusal would be unreachable."""
        project = _project(tmp_path)
        _seed(project)
        _supersede(project)

        assert ak.superseded_authorization(
            _story(project), "qe", dispatch_id="f" * 32
        ) is None

    def test_asking_nothing_answers_nothing(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        _supersede(project)

        assert ak.superseded_authorization(_story(project), "qe") is None
        assert ak.superseded_authorization(
            _story(project), "qe", dispatch_id="", attempt_id=""
        ) is None


# ── #803: said where it is caused ─────────────────────────────────────────────


class TestTheDispatchSaysWhatItOrphaned:
    def test_superseding_an_attempt_with_a_receipt_on_disk_reports_it(
        self, tmp_path
    ):
        """The operator's model is that this call authorizes work. It also
        unbinds whatever evidence the role already had, and said nothing --
        the consequence surfaced at the next gate as a missing verification
        rather than as a destroyed binding."""
        project = _project(tmp_path)
        _seed(project)
        assert _dispatch(project).allowed
        first = dict(_binding(project))
        # Stale, so the fresh-receipt guard does not refuse the re-dispatch.
        # This is the shape the report describes: evidence from before the
        # stage was re-entered.
        receipt = _receipt(
            project, completed_at=BEFORE,
            dispatch_id=first["dispatch_id"], attempt_id=first["attempt_id"],
        )

        second = _dispatch(project)

        assert second.allowed, second.reason
        orphaned = second.extra.get("orphaned_receipt")
        assert orphaned is not None, second.extra
        assert orphaned["path"] == str(receipt)
        assert orphaned["superseded_attempt_id"] == first["attempt_id"]
        assert orphaned["role"] == "qe"
        assert "dispatch_binding_orphaned" in orphaned["summary"]
        assert "not deleted and not invalid" in orphaned["summary"]

    def test_a_fresh_dispatch_with_no_predecessor_reports_nothing(self, tmp_path):
        """A notice attached to every dispatch is a notice nobody reads."""
        project = _project(tmp_path)
        _seed(project)

        assert "orphaned_receipt" not in _dispatch(project).extra

    def test_superseding_with_no_receipt_on_disk_reports_nothing(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)

        _supersede(project)

        assert "orphaned_receipt" not in _dispatch(project).extra
