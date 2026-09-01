"""Layer 1 -- attempt identity in the advance kernel (#344, Epic #339).

The attempt is the CP-durable, fenced form of the dispatch binding, NOT a
second identity: `execute_dispatch` already minted a `dispatch_id` into
`mcp_active_dispatches`, and this work extends that same record. These tests
pin the four properties the rest of the pilot depends on:

1. dispatch mints an attempt and a fencing token into the EXISTING binding,
   so one story never carries two competing identities per role,
2. a receipt from another attempt cannot advance, even when its dispatch_id
   somehow matches,
3. a Work Unit the sealed manifest never admitted is refused at DISPATCH,
   which is the cheap moment; the Sync barrier would catch it at criterion 3
   only after every workstream had spent its integration effort,
4. a binding created before this landed still advances, because a migration
   that stranded in-flight work would be paid for by whoever happened to be
   mid-Cycle.
"""

from __future__ import annotations

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
    path = project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


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


def _bind(project: Path, *, story_id: str = "US-001", abbrev: str = "qe", **fields):
    """Write an active-dispatch binding directly, the way execute_dispatch
    would have left it."""
    path = project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
    state = json.loads(path.read_text())
    for story in state["current_stories"]:
        if story["id"] == story_id:
            story.setdefault("mcp_active_dispatches", {})[abbrev] = dict(fields)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _policy(**kwargs) -> ak.HostPolicy:
    kwargs.setdefault("host", "test")
    kwargs.setdefault("require_next_action_match", False)
    kwargs.setdefault("require_dispatch_binding", True)
    return ak.HostPolicy(**kwargs)


class TestAttemptIdentityShape:
    def test_the_attempt_extends_the_dispatch_binding_rather_than_replacing_it(
        self, tmp_path
    ):
        """Both ids live in ONE record. Two records would be two identities
        for one bounded execution, which is the thing this design refuses."""
        project = _project(tmp_path)
        _seed(project)
        binding = ak._mint_attempt_binding(str(project), {}, "US-001", "qe")
        assert binding is not None
        assert "refusal" not in binding
        assert binding["binding"]["attempt_id"].startswith("att_")
        assert binding["binding"]["fencing_token"]

    def test_minted_attempt_ids_match_the_frozen_contract_shape(self, tmp_path):
        import runtime_contracts as rc

        project = _project(tmp_path)
        _seed(project)
        for _ in range(20):
            minted = ak._mint_attempt_binding(str(project), {}, "US-001", "qe")
            assert rc.ATTEMPT_ID_RE.match(minted["binding"]["attempt_id"])

    def test_every_attempt_is_unique(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        seen = {
            ak._mint_attempt_binding(str(project), {}, "US-001", "qe")["binding"][
                "attempt_id"
            ]
            for _ in range(50)
        }
        assert len(seen) == 50, "a retry must be a NEW attempt, never a reused id"

    def test_a_non_spq_project_gets_identity_but_no_cycle_binding(self, tmp_path):
        """Scrum and Kanban are out of the pilot's scope, so they must be
        untouched: an attempt id is harmless, a Cycle binding would be a lie."""
        project = _project(tmp_path, build_mode="scrum")
        _seed(project)
        minted = ak._mint_attempt_binding(str(project), {}, "US-001", "qe")["binding"]
        assert "cycle_id" not in minted
        assert "manifest_hash" not in minted


class TestAttemptBindingOnAdvance:
    def test_a_receipt_from_another_attempt_cannot_advance(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        _bind(project, dispatch_id="a" * 32, attempt_id="att_0000000001")
        _receipt(project, dispatch_id="a" * 32, attempt_id="att_9999999999")
        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_policy()
        )
        assert not decision.allowed
        assert decision.code == ak.ATTEMPT_BINDING_MISMATCH

    def test_the_matching_attempt_advances(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        _bind(project, dispatch_id="a" * 32, attempt_id="att_0000000001")
        _receipt(project, dispatch_id="a" * 32, attempt_id="att_0000000001")
        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_policy()
        )
        assert decision.allowed, decision.reason

    def test_a_pre_existing_binding_without_an_attempt_still_advances(self, tmp_path):
        """Backward compatibility is a conformance row. A binding minted
        before this landed carries no attempt_id, so the check must not fire
        and strand work that is already in flight."""
        project = _project(tmp_path)
        _seed(project)
        _bind(project, dispatch_id="a" * 32)
        _receipt(project, dispatch_id="a" * 32)
        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_policy()
        )
        assert decision.allowed, decision.reason

    def test_a_receipt_that_omits_the_attempt_still_advances(self, tmp_path):
        """The migration window. Every binding now carries an attempt id, so
        enforcing on ABSENCE would refuse every agent not yet taught to stamp
        one, which is all of them until the evidence contract lands and each
        host adopts it. The dispatch_id binding still protects this path, so
        nothing that is enforced today is relaxed.
        """
        project = _project(tmp_path)
        _seed(project)
        _bind(project, dispatch_id="a" * 32, attempt_id="att_0000000001")
        _receipt(project, dispatch_id="a" * 32)
        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_policy()
        )
        assert decision.allowed, decision.reason

    def test_but_a_wrong_dispatch_id_is_still_refused_in_that_window(self, tmp_path):
        """The point above only holds because the older binding keeps
        working. If this ever passes, the migration window has become a
        hole."""
        project = _project(tmp_path)
        _seed(project)
        _bind(project, dispatch_id="a" * 32, attempt_id="att_0000000001")
        _receipt(project, dispatch_id="b" * 32)
        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_policy()
        )
        assert not decision.allowed
        assert decision.code == ak.DISPATCH_BINDING_MISMATCH

    def test_attempt_binding_is_downgradable_under_warn(self, tmp_path):
        """It is evidence strength, like the dispatch binding beside it, so
        `enforcement="warn"` may downgrade it."""
        assert ak.ATTEMPT_BINDING_MISMATCH in ak._EVIDENCE_CODES


class TestManifestAgreement:
    def test_a_disagreeing_manifest_hash_cannot_advance(self, tmp_path):
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
            str(project), "US-001", "reviewing", policy=_policy()
        )
        assert not decision.allowed
        assert decision.code == ak.MANIFEST_DISAGREEMENT

    def test_an_agreeing_manifest_hash_advances(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        digest = "sha256:" + "1" * 64
        _bind(
            project,
            dispatch_id="a" * 32,
            attempt_id="att_0000000001",
            manifest_hash=digest,
        )
        _receipt(
            project,
            dispatch_id="a" * 32,
            attempt_id="att_0000000001",
            manifest_hash=digest,
        )
        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_policy()
        )
        assert decision.allowed, decision.reason

    def test_a_receipt_predating_manifest_stamping_still_advances(self, tmp_path):
        """Only checked when BOTH sides carry a hash, for the same
        in-flight-work reason as the attempt check."""
        project = _project(tmp_path)
        _seed(project)
        _bind(
            project,
            dispatch_id="a" * 32,
            attempt_id="att_0000000001",
            manifest_hash="sha256:" + "1" * 64,
        )
        _receipt(project, dispatch_id="a" * 32, attempt_id="att_0000000001")
        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_policy()
        )
        assert decision.allowed, decision.reason

    def test_manifest_disagreement_is_correctness_not_evidence(self):
        """Which Cycle a Work Unit belongs to must not be downgradable by a
        per-session enforcement mode: a warned-past disagreement produces a
        receipt the Sync barrier rejects anyway, after the integration effort
        has been spent."""
        assert ak.MANIFEST_DISAGREEMENT not in ak._EVIDENCE_CODES


class TestScrumAndKanbanAreUntouched:
    @pytest.mark.parametrize("mode", ["scrum", "kanban"])
    def test_no_cycle_binding_is_attached(self, tmp_path, mode):
        project = _project(tmp_path, build_mode=mode)
        _seed(project)
        minted = ak._mint_attempt_binding(str(project), {}, "US-001", "qe")
        assert "refusal" not in minted
        assert set(minted["binding"]) == {"attempt_id", "fencing_token"}

    def test_an_unreadable_project_never_blocks_dispatch(self, tmp_path):
        """Attempt identity must never be the reason a delivery-critical
        dispatch cannot proceed."""
        project = tmp_path / "nonexistent"
        assert ak._sealed_cycle_binding(str(project)) is None
