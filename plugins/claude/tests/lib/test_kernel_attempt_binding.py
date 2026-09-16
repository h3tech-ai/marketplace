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
from datetime import timedelta
from pathlib import Path

import pytest

import advance_kernel as ak
import cycle_records


pytestmark = pytest.mark.unit

STAGE_ENTERED = "2026-01-01T00:00:00+00:00"
FUTURE = "2090-01-01T00:00:00+00:00"

#: A stand-in seal digest. Nothing in this file verifies it: the kernel's
#: admission check reads the declaration's hash to BIND an attempt to it, and
#: whether the document hashes to its own claim is `cycle_records.verify_hash`'s
#: subject, tested where the seal is.
DIGEST = "sha256:" + "1" * 64


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
        assert set(minted["binding"]) == {
            "attempt_id",
            "fencing_token",
            # #447 added the attempt's own deadline to the SAME binding.
            # Asserted here rather than loosened to a subset check: this
            # test exists to catch a Cycle binding leaking onto a
            # non-SPQ project, and a subset check would stop catching it.
            "expires_at",
        }

    def test_an_unreadable_project_never_blocks_dispatch(self, tmp_path):
        """Attempt identity must never be the reason a delivery-critical
        dispatch cannot proceed."""
        project = tmp_path / "nonexistent"
        assert ak._sealed_cycle_binding(str(project)) is None


class TestUnreadableCycleAuthorityFailsClosed:
    """Review finding 8. An SPQ project with an OPEN Cycle whose manifest
    cannot be read has missing admission authority, not absent authority.

    The distinction matters because the two look identical to a naive reader
    and behave oppositely: a project with no Cycle open is legitimately
    unbound and must dispatch, while one mid-Cycle with a corrupt manifest
    cannot show that any Work Unit is admitted and must refuse. Collapsing
    them, as the first revision did, turned the mandatory admission check into
    one that silently skipped itself exactly when its authority was damaged.
    """

    def _spq(self, tmp_path: Path, *, cycle_id: str = "003-9f2c41ab") -> Path:
        project = _project(tmp_path, build_mode="spq")
        _seed(project)
        state_path = project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
        state = json.loads(state_path.read_text())
        # The authoritative location, not the `_cycle_id` mirror that
        # `_read_state` normalizes away. Getting this wrong is what made the
        # admission check a no-op in the first revision.
        state["spq"] = {"cycle_id": cycle_id, "cycle_seq": 3}
        state["build_mode"] = "spq"
        state["lifecycle_state"] = "CYCLE"
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return project

    def _sealed(self, *unit_ids: str, cycle_id: str = "003-9f2c41ab",
                digest: str = DIGEST) -> dict:
        """A minimal sealed declaration, in the keys the kernel reads.

        Both key names moved at #644 and are taken from `cycle_records` rather
        than spelled here: `manifest_hash` became `declaration_hash` (what is
        sealed is a DECLARATION, and calling it a manifest is what let a lane
        topology live inside it) and `work_units` became `admitted_units`.

        Spelled by hand rather than through `cycle_records.seal`, deliberately,
        and unlike every other SPQ fixture in the suite: this class's whole
        subject is a manifest that is CORRUPT, INCOMPLETE or DISAGREES with the
        pointer, and `seal` refuses every one of those by construction. The
        digest is a stand-in for the same reason -- nothing here verifies it.
        """
        record: dict = {"cycle_id": cycle_id,
                        "admitted_units": [{"id": uid} for uid in unit_ids]}
        if digest:
            record[cycle_records.HASH_FIELD] = digest
        return record

    def _manifest_path(self, project: Path, cycle_id: str = "003-9f2c41ab") -> Path:
        import spq_paths

        return Path(spq_paths.manifest_path(str(project), cycle_id))

    def _write_manifest(self, project: Path, payload, cycle_id="003-9f2c41ab"):
        path = self._manifest_path(project, cycle_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            payload if isinstance(payload, str) else json.dumps(payload),
            encoding="utf-8",
        )
        return path

    def test_a_clone_carrying_only_the_committed_manifest_still_dispatches(
        self, tmp_path
    ):
        """The shape of every hydrated workstream clone, and the case the
        first revision of this guard got wrong.

        `open_cycle` writes both copies in the integration clone, but only the
        committed one travels through git, and `hydrate_cycle` writes a
        projection rather than a second sealed copy. Reading only the local
        store would therefore have refused every delivery clone in the
        product while passing every fixture that ran in an integration clone.
        """
        import spq_paths

        project = self._spq(tmp_path)
        committed = Path(
            spq_paths.committed_manifest_path(str(project), "003-9f2c41ab")
        )
        committed.parent.mkdir(parents=True, exist_ok=True)
        committed.write_text(
            json.dumps(self._sealed("US-001")), encoding="utf-8"
        )
        assert not Path(
            spq_paths.manifest_path(str(project), "003-9f2c41ab")
        ).exists(), "the local sealed copy must be absent for this to mean anything"

        minted = ak._mint_attempt_binding(str(project), {}, "US-001", "qe")
        assert "refusal" not in minted, minted
        assert minted["binding"]["cycle_id"] == "003-9f2c41ab"

    def test_a_missing_manifest_refuses_rather_than_dispatching_unbound(
        self, tmp_path
    ):
        project = self._spq(tmp_path)
        minted = ak._mint_attempt_binding(str(project), {}, "US-001", "qe")
        assert "refusal" in minted, "a missing manifest must not dispatch"
        assert minted["refusal"]["code"] == ak.MANIFEST_UNREADABLE
        assert "no sealed manifest" in minted["refusal"]["reason"]

    def test_a_corrupt_manifest_refuses(self, tmp_path):
        project = self._spq(tmp_path)
        self._write_manifest(project, "{not json at all")
        minted = ak._mint_attempt_binding(str(project), {}, "US-001", "qe")
        assert minted["refusal"]["code"] == ak.MANIFEST_UNREADABLE

    def test_a_manifest_that_is_not_an_object_refuses(self, tmp_path):
        project = self._spq(tmp_path)
        self._write_manifest(project, ["not", "an", "object"])
        minted = ak._mint_attempt_binding(str(project), {}, "US-001", "qe")
        assert minted["refusal"]["code"] == ak.MANIFEST_UNREADABLE

    def test_a_manifest_without_its_own_hash_refuses(self, tmp_path):
        """A manifest carrying no hash cannot be the thing a receipt later
        agrees with, so it is authority in name only."""
        project = self._spq(tmp_path)
        self._write_manifest(project, self._sealed("US-001", digest=""))
        minted = ak._mint_attempt_binding(str(project), {}, "US-001", "qe")
        assert minted["refusal"]["code"] == ak.MANIFEST_UNREADABLE

    def test_a_manifest_naming_a_different_cycle_refuses(self, tmp_path):
        """One of the two is stale and this dispatch cannot tell which."""
        project = self._spq(tmp_path)
        self._write_manifest(
            project, self._sealed("US-001", cycle_id="004-deadbeef")
        )
        minted = ak._mint_attempt_binding(str(project), {}, "US-001", "qe")
        assert minted["refusal"]["code"] == ak.MANIFEST_UNREADABLE

    def test_a_readable_manifest_still_binds_normally(self, tmp_path):
        """The control: fail-closed must not have broken the working path."""
        project = self._spq(tmp_path)
        self._write_manifest(project, self._sealed("US-001"))
        minted = ak._mint_attempt_binding(str(project), {}, "US-001", "qe")
        assert "refusal" not in minted, minted
        assert minted["binding"]["cycle_id"] == "003-9f2c41ab"
        assert minted["binding"]["manifest_hash"] == DIGEST

    def test_an_unadmitted_unit_still_refuses_with_the_admission_code(
        self, tmp_path
    ):
        """Unreadable authority and a readable manifest that excludes the unit
        are different failures and keep different codes."""
        project = self._spq(tmp_path)
        self._write_manifest(project, self._sealed("US-999"))
        minted = ak._mint_attempt_binding(str(project), {}, "US-001", "qe")
        assert minted["refusal"]["code"] == ak.MANIFEST_DISAGREEMENT

    def test_an_spq_project_with_no_open_cycle_is_not_refused(self, tmp_path):
        """The case the fail-closed change must NOT catch."""
        project = _project(tmp_path, build_mode="spq")
        _seed(project)
        minted = ak._mint_attempt_binding(str(project), {}, "US-001", "qe")
        assert "refusal" not in minted
        assert set(minted["binding"]) == {
            "attempt_id",
            "fencing_token",
            # #447 added the attempt's own deadline to the SAME binding.
            # Asserted here rather than loosened to a subset check: this
            # test exists to catch a Cycle binding leaking onto a
            # non-SPQ project, and a subset check would stop catching it.
            "expires_at",
        }

    def test_unreadable_authority_is_correctness_not_evidence(self):
        assert ak.MANIFEST_UNREADABLE not in ak._EVIDENCE_CODES


# ── #493: the attested item's attempt is AUTHORIZED, not merely well formed ──


def _authorize(
    project: Path, *, story_id: str = "US-001", abbrev: str = "qe", attempt_id: str
):
    """Record an attempt in the authorization ledger without a live binding,
    the way a SUPERSEDED generation is left after its successor overwrites
    `mcp_active_dispatches[abbrev]`."""
    path = project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
    state = json.loads(path.read_text())
    for story in state["current_stories"]:
        if story["id"] == story_id:
            ak._record_authorized_attempt(story, abbrev, attempt_id)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _attested_item(attempt_id: str, **extra) -> dict:
    item = {
        "evidence_class": "attested",
        "attempt_id": attempt_id,
        "finding": "graded 3 of 3 sections against the golden output",
        "check_id": "back_test",
    }
    item.update(extra)
    return item


class TestAttestedEvidenceIsBoundToAnAuthorizedAttempt:
    """#493. `attested` has no replay and no derived candidate, so its attempt
    identity is the entire discipline of the class -- and until this landed
    that identity was matched against `ATTEMPT_ID_RE` and compared to nothing.

    A format check refuses a TYPO of the forgery. These tests are the
    difference between the two.
    """

    def _bound(self, tmp_path, evidence_attempt, *, live="att_0000000001"):
        project = _project(tmp_path)
        _seed(project)
        _bind(project, dispatch_id="a" * 32, attempt_id=live)
        _receipt(
            project,
            dispatch_id="a" * 32,
            attempt_id=live,
            evidence=[_attested_item(evidence_attempt)],
        )
        return project, ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_policy()
        )

    def test_a_grade_naming_an_attempt_that_never_existed_cannot_advance(
        self, tmp_path
    ):
        """The reproduction from the ticket. The receipt's TOP-LEVEL
        attempt_id is correct, so every check that existed before this one is
        satisfied; only the evidence item lies."""
        _, decision = self._bound(tmp_path, "att_ffffffffffffffffffff")
        assert not decision.allowed
        assert decision.code == ak.ATTEMPT_BINDING_MISMATCH
        assert "att_ffffffffffffffffffff" in decision.reason

    def test_the_story_does_not_move_on_the_refusal(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        _bind(project, dispatch_id="a" * 32, attempt_id="att_0000000001")
        _receipt(
            project,
            dispatch_id="a" * 32,
            attempt_id="att_0000000001",
            evidence=[_attested_item("att_ffffffffffffffffffff")],
        )
        decision = ak.execute_advance(
            str(project), "US-001", "reviewing", policy=_policy()
        )
        assert not decision.allowed
        state = json.loads(
            (
                project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
            ).read_text()
        )
        assert state["current_stories"][0]["state"] == "testing"

    def test_a_grade_naming_the_authorized_attempt_advances(self, tmp_path):
        """The positive control. Refusing the honest case would be a different
        failure, not a safer one."""
        _, decision = self._bound(tmp_path, "att_0000000001")
        assert decision.allowed, decision.reason

    def test_a_forged_grade_is_refused_even_when_it_names_a_real_check(
        self, tmp_path
    ):
        """#432 derives control-plane gate depth from the class each item
        names, so a forged item that also names a check is the shape that
        would be counted as attested depth."""
        project = _project(tmp_path)
        _seed(project)
        _bind(project, dispatch_id="a" * 32, attempt_id="att_0000000001")
        _receipt(
            project,
            dispatch_id="a" * 32,
            attempt_id="att_0000000001",
            evidence=[
                _attested_item("att_ffffffffffffffffffff", check_id="tests_pass")
            ],
        )
        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_policy()
        )
        assert not decision.allowed
        assert decision.code == ak.ATTEMPT_BINDING_MISMATCH

    def test_one_forged_item_among_honest_ones_refuses_the_whole_receipt(
        self, tmp_path
    ):
        project = _project(tmp_path)
        _seed(project)
        _bind(project, dispatch_id="a" * 32, attempt_id="att_0000000001")
        _receipt(
            project,
            dispatch_id="a" * 32,
            attempt_id="att_0000000001",
            evidence=[
                _attested_item("att_0000000001"),
                {"evidence_class": "replayed", "command": "pytest -q", "exit_code": 0},
                _attested_item("att_ffffffffffffffffffff"),
            ],
        )
        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_policy()
        )
        assert not decision.allowed
        assert decision.code == ak.ATTEMPT_BINDING_MISMATCH
        assert "evidence[2]" in decision.reason


class TestWhichAttemptsAnAttestationMayName:
    """The rule, in both directions. "Any attempt on this story" and "this
    attempt only" both pass a green suite that only ever tests the forged id,
    so each boundary gets a test."""

    def test_a_superseded_attempt_of_the_SAME_stage_is_admitted(self, tmp_path):
        """The retry case, and the reason the ledger exists. Attempt 1
        produced the finding; attempt 2 carries it forward correctly
        attributed. Refusing this would leave a producer two options, both
        worse: relabel the finding with attempt 2 (a lie the check cannot
        see) or drop the evidence."""
        project = _project(tmp_path)
        _seed(project)
        _authorize(project, attempt_id="att_0000000001")
        _bind(project, dispatch_id="a" * 32, attempt_id="att_0000000002")
        _authorize(project, attempt_id="att_0000000002")
        _receipt(
            project,
            dispatch_id="a" * 32,
            attempt_id="att_0000000002",
            evidence=[_attested_item("att_0000000001")],
        )
        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_policy()
        )
        assert decision.allowed, decision.reason

    def test_another_stages_attempt_on_the_SAME_story_is_refused(self, tmp_path):
        """The rule is per ROLE, not per story. 3.3 says the item carries the
        PRODUCING attempt identity, and producer is not subject: a CR
        attestation ABOUT the SE stage is produced by the CR attempt and names
        it. So this refusal costs no honest case and closes the one #493 names
        illegitimate -- a receipt attributing its own finding to another
        stage's execution."""
        project = _project(tmp_path)
        _seed(project)
        _authorize(project, abbrev="se", attempt_id="att_00000000se")
        _bind(project, dispatch_id="a" * 32, attempt_id="att_0000000001")
        _receipt(
            project,
            dispatch_id="a" * 32,
            attempt_id="att_0000000001",
            evidence=[_attested_item("att_00000000se")],
        )
        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_policy()
        )
        assert not decision.allowed
        assert decision.code == ak.ATTEMPT_BINDING_MISMATCH

    def test_an_attempt_authorized_for_another_STORY_is_refused(self, tmp_path):
        """The ledger is per story, so a well-formed id minted elsewhere is
        exactly as unauthorized here as one invented from nothing."""
        project = _project(tmp_path)
        payload = _seed(project)
        other = json.loads(json.dumps(payload["current_stories"][0]))
        other["id"] = "US-002"
        ak._record_authorized_attempt(other, "qe", "att_00000000ot")
        payload["current_stories"].append(other)
        (project / ".synaptory" / ".orchestrator" / "pipeline-state.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
        _bind(project, dispatch_id="a" * 32, attempt_id="att_0000000001")
        _receipt(
            project,
            dispatch_id="a" * 32,
            attempt_id="att_0000000001",
            evidence=[_attested_item("att_00000000ot")],
        )
        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_policy()
        )
        assert not decision.allowed
        assert decision.code == ak.ATTEMPT_BINDING_MISMATCH

    def test_a_story_dispatched_before_this_landed_still_advances(self, tmp_path):
        """Migration. An in-flight story has a live binding and no ledger, so
        `_authorized_attempts` unions the binding in rather than reading the
        ledger alone. Reading the ledger alone would have refused every
        in-flight attestation on the day this merged."""
        project = _project(tmp_path)
        _seed(project)
        _bind(project, dispatch_id="a" * 32, attempt_id="att_0000000001")
        _receipt(
            project,
            dispatch_id="a" * 32,
            attempt_id="att_0000000001",
            evidence=[_attested_item("att_0000000001")],
        )
        state = json.loads(
            (
                project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
            ).read_text()
        )
        assert ak.AUTHORIZED_ATTEMPTS_KEY not in state["current_stories"][0]
        decision = ak.evaluate_advance(
            str(project), "US-001", "reviewing", policy=_policy()
        )
        assert decision.allowed, decision.reason


class TestAnUnauthorizedAttestationIsRefused:
    """The tolerance this class used to pin, and why it did not survive.

    It read: "A stage the kernel never authorized an attempt for has nothing to
    compare against, and refusing cannot invent one, it would only refuse
    honest evidence on a host that does not own every launch."

    #592's audit reproduced the consequence through `execute_advance`: with no
    dispatch begun, a receipt carrying an invented `attempt_id` and an attested
    item naming it advanced `testing -> reviewing` under the strict Claude
    policy, and `backing_evidence_class` then credited the well-shaped id as
    `attested` gate depth. That is #493's acceptance criterion inverted.

    The premise was checked rather than argued about. An attested item's
    `attempt_id` has exactly one legitimate origin: a kernel-issued dispatch
    envelope. The Go bridge stamps `env.AttemptID`, and the envelope comes from
    `execute_dispatch`. So there is no honest producer of an attempt id the
    kernel did not issue, and "refuse honest evidence" described a case that
    cannot arise.

    What is still tolerated, and belongs here rather than in the refusal: a
    receipt carrying NO attested items advances unaffected. It claims no
    execution, so there is nothing unrefutable about it.
    """

    def _unbound(self, tmp_path, evidence_attempt):
        project = _project(tmp_path)
        _seed(project)
        _receipt(project, evidence=[_attested_item(evidence_attempt)])
        return ak.evaluate_advance(
            str(project),
            "US-001",
            "reviewing",
            policy=_policy(require_dispatch_binding=False),
        )

    def test_with_no_authorized_attempt_the_attestation_is_refused(self, tmp_path):
        """Asserts impossible: an attestation the kernel cannot refute advancing.

        This used to assert `decision.allowed`. The reversal is the finding.
        """
        decision = self._unbound(tmp_path, "att_ffffffffffffffffffff")
        assert not decision.allowed
        assert decision.code == ak.ATTEMPT_BINDING_MISMATCH
        assert "att_ffffffffffffffffffff" in decision.reason

    def test_the_refusal_names_what_it_could_not_check(self, tmp_path):
        """A refusal that does not say why sends the reader to the wrong fix."""
        decision = self._unbound(tmp_path, "att_ffffffffffffffffffff")
        assert "authorized no attempt" in decision.reason
        assert "#493" in decision.reason and "#592" in decision.reason

    def test_the_story_does_not_move_on_that_refusal(self, tmp_path):
        """Reproduced through the PUBLIC execute path, not the evaluator.

        The audit's reproduction persisted the transition, so a test that only
        checks the decision object would have passed while the board moved.
        """
        project = _project(tmp_path)
        _seed(project)
        _receipt(project, evidence=[_attested_item("att_ffffffffffffffffffff")])
        decision = ak.execute_advance(
            str(project), "US-001", "reviewing", policy=_policy()
        )
        assert not decision.allowed
        state = json.loads(
            (
                project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
            ).read_text()
        )
        assert state["current_stories"][0]["state"] == "testing"

    def test_a_receipt_with_no_attested_evidence_gets_no_warning(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        _receipt(project)
        decision = ak.evaluate_advance(
            str(project),
            "US-001",
            "reviewing",
            policy=_policy(require_dispatch_binding=False),
        )
        assert not any("bound to nothing" in w for w in decision.warnings)


class TestTheAuthorizationLedger:
    def test_dispatch_records_the_attempt_it_authorized(self, tmp_path):
        """The ledger is written where the authorization happens, which is the
        only moment the kernel knows it authorized anything."""
        project = _project(tmp_path)
        _seed(project, state="queued")
        decision = ak.execute_dispatch(
            str(project), "US-001", role="software-engineer", policy=_policy()
        )
        assert decision.allowed, decision.reason
        state = json.loads(
            (
                project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
            ).read_text()
        )
        story = state["current_stories"][0]
        live = story["mcp_active_dispatches"]["se"]["attempt_id"]
        # #492 turned each entry into a RECORD (lineage, outcome, supersession)
        # rather than a bare id, as the constant's own note said it would. The
        # claim under test is unchanged: the authorization is recorded at mint,
        # for this role, and nothing else is.
        assert [r["attempt_id"] for r in ak.attempt_history(story, "se")] == [live]

    def test_the_ledger_outlives_the_binding_the_advance_pops(self, tmp_path):
        """`mcp_active_dispatches[abbrev]` is removed on advance, which is why
        it cannot answer "did this story ever run attempt X"."""
        project = _project(tmp_path)
        _seed(project, state="queued")
        ak.execute_dispatch(
            str(project), "US-001", role="software-engineer", policy=_policy()
        )
        path = project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
        story = json.loads(path.read_text())["current_stories"][0]
        live = story["mcp_active_dispatches"]["se"]["attempt_id"]
        _receipt(
            project,
            abbrev="se",
            role="software-engineer",
            dispatch_id=story["mcp_active_dispatches"]["se"]["dispatch_id"],
            attempt_id=live,
        )
        decision = ak.execute_advance(
            str(project), "US-001", "testing", policy=_policy()
        )
        assert decision.allowed, decision.reason
        after = json.loads(path.read_text())["current_stories"][0]
        assert "se" not in (after.get("mcp_active_dispatches") or {})
        assert [r["attempt_id"] for r in ak.attempt_history(after, "se")] == [live]

    def test_recording_the_same_attempt_twice_does_not_duplicate_it(self):
        story: dict = {}
        ak._record_authorized_attempt(story, "qe", "att_0000000001")
        ak._record_authorized_attempt(story, "qe", "att_0000000001")
        assert [
            r["attempt_id"] for r in ak.attempt_history(story, "qe")
        ] == ["att_0000000001"]

    def test_a_corrupt_ledger_does_not_crash_the_lookup(self):
        """State is kernel-written, but a hand-edited or truncated file must
        degrade to "nothing authorized" rather than raising inside an
        advance."""
        assert ak._authorized_attempts({ak.AUTHORIZED_ATTEMPTS_KEY: "junk"}, "qe") == ()
        assert (
            ak._authorized_attempts({ak.AUTHORIZED_ATTEMPTS_KEY: {"qe": 7}}, "qe") == ()
        )
        assert ak._authorized_attempts(None, "qe") == ()
        assert ak._authorized_attempts({}, "") == ()


class TestAnUnauthorizedReceiptLevelAttemptIsRefused:
    """#592's re-review of #606, and the reason it is a separate class.

    #606 refused an invented `dispatch_id` and an invented item-level
    attestation when the kernel had authorized nothing, and left the top-level
    `attempt_id`. That is the field which PAYS:
    `story_pipeline.backing_evidence_class` credits an ordinary receipt-backed
    check as `attested` because the value is present, so #606's "the receipt
    claims none" case still carried an execution identity and still earned
    attempt-scoped credit for it.

    Reproduced on `514fbed7` through the public path with no dispatch, no
    `dispatch_id`, no typed evidence and an invented attempt id: advanced to
    `reviewing`, and the check classed `attested`.
    """

    def test_the_forged_attempt_id_alone_does_not_advance(self, tmp_path):
        """Asserts impossible: an unrefutable execution identity advancing.

        Nothing else is claimed here. No dispatch was begun, the receipt has
        no `dispatch_id` and no `evidence`, so this is exactly the case #606
        left open.
        """
        project = _project(tmp_path)
        _seed(project)
        _receipt(project, attempt_id="att_ffffffffffffffffffff")
        decision = ak.execute_advance(
            str(project), "US-001", "reviewing", policy=_policy()
        )
        assert not decision.allowed
        assert decision.code == ak.ATTEMPT_BINDING_MISMATCH
        assert "att_ffffffffffffffffffff" in decision.reason

    def test_and_the_board_does_not_move(self, tmp_path):
        """The audit's reproduction PERSISTED the transition, so a test that
        reads only the decision would pass while the state changed."""
        project = _project(tmp_path)
        _seed(project)
        _receipt(project, attempt_id="att_ffffffffffffffffffff")
        ak.execute_advance(str(project), "US-001", "reviewing", policy=_policy())
        state = json.loads(
            (
                project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
            ).read_text()
        )
        assert state["current_stories"][0]["state"] == "testing"

    def test_a_receipt_with_no_attempt_id_still_advances_and_earns_no_class(
        self, tmp_path
    ):
        """The genuinely unbound receipt, which is a different thing.

        The Claude orchestrator calls `begin_dispatch` only for `dispatch_se`,
        so QE and CR advance unbound on every ordinary story. Refusing that
        would be a different defect rather than a safer one. What makes it
        safe is that it claims nothing: `backing_evidence_class` gives it no
        class, so it buys no attempt-scoped credit either.
        """
        import story_pipeline as sp

        project = _project(tmp_path)
        _seed(project)
        _receipt(project)
        decision = ak.execute_advance(
            str(project), "US-001", "reviewing", policy=_policy()
        )
        assert decision.allowed, decision.reason
        receipt = json.loads(
            sorted(
                (project / ".synaptory" / ".orchestrator" / "receipts").glob(
                    "*.json"
                )
            )[0].read_text()
        )
        assert "attempt_id" not in receipt
        assert sp.backing_evidence_class({"passed": True}, receipt) is None, (
            "an unbound receipt earned an evidence class, so refusing the "
            "forged identity above would buy nothing"
        )

    def test_an_authorized_attempt_id_still_advances(self, tmp_path):
        """The positive control. Refusing the honest case is a different
        failure, not a safer one."""
        project = _project(tmp_path)
        _seed(project, state="queued")
        ak.execute_dispatch(
            str(project), "US-001", role="software-engineer", policy=_policy()
        )
        path = project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
        story = json.loads(path.read_text())["current_stories"][0]
        live = story["mcp_active_dispatches"]["se"]
        _receipt(
            project,
            abbrev="se",
            role="software-engineer",
            dispatch_id=live["dispatch_id"],
            attempt_id=live["attempt_id"],
        )
        decision = ak.execute_advance(
            str(project), "US-001", "testing", policy=_policy()
        )
        assert decision.allowed, decision.reason


class TestACompletedAttemptIsNotCurrentAuthority:
    """#592's re-review of #608, and the set I picked was wrong.

    #608 refused a top-level `attempt_id` the kernel had not authorized, and
    asked `_authorized_attempts`, which unions the live binding with the role's
    ENTIRE history and never retires an id once its advance pops the binding.

    The lifecycle legally returns a Work Unit to the same role stage: a
    `needs-fix` rejection sends `awaiting_acceptance -> in_progress`, and the
    blocked recovery edges do the same. On that second cycle there is no live
    binding, so a NEW receipt naming the PREVIOUS completed attempt satisfied
    the check and `backing_evidence_class` awarded it `attested`.

    Reproduced on `3806c1be` through the public path: cycle 1 advanced
    legitimately, the unit returned to `in_progress`, and a fresh receipt
    carrying the completed attempt id advanced again.

    The ledger is evidence that an attempt once ran. It is not evidence that it
    produced THIS receipt. Top-level receipt identity now answers to the live
    binding only. The nested `evidence[]` rule keeps the history union on
    purpose, because a predecessor attempt genuinely can be the producer of a
    finding an item attests to.
    """

    def _walk_one_cycle(self, project) -> str:
        """Dispatch, produce a matching receipt, advance. Returns the attempt id.

        The receipt is dated ONE SECOND AFTER the stage the dispatch just
        entered, read off the board rather than chosen. The module's `FUTURE`
        clock (2090) is fresh relative to every stage entry that will ever
        exist, so a receipt carrying it can never become last cycle's receipt.
        That is fine for a test about advance, and wrong for any test about
        what a SECOND cycle sees, which is why the rework control below could
        not tell a stale receipt from a current one.
        """
        ak.execute_dispatch(
            str(project), "US-001", role="software-engineer", policy=_policy()
        )
        path = project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
        story = json.loads(path.read_text())["current_stories"][0]
        live = story["mcp_active_dispatches"]["se"]
        entered = ak.stage_entered_at(story)
        assert entered is not None, "the dispatch left no stage entry to date against"
        _receipt(
            project,
            abbrev="se",
            role="software-engineer",
            dispatch_id=live["dispatch_id"],
            attempt_id=live["attempt_id"],
            completed_at=(entered + timedelta(seconds=1))
            .isoformat()
            .replace("+00:00", "Z"),
        )
        decision = ak.execute_advance(
            str(project), "US-001", "testing", policy=_policy()
        )
        assert decision.allowed, decision.reason
        return live["attempt_id"]

    @staticmethod
    def _minutes_apart(monkeypatch, start="2026-05-01T09:00:00+00:00"):
        """A clock that advances a minute per reading, for both writers.

        Without it this class walks two full cycles inside one wall-clock
        second, and `receipt_timestamp_is_fresh` compares SECONDS: it truncates
        microseconds so a Cursor receipt stamped `:34Z` is not called stale
        against an `entered_at` of `:34.647295Z`. Two stage entries inside the
        same second are therefore indistinguishable to it, so cycle 1's receipt
        reads as current in cycle 2 and the test measures the tolerance rather
        than the rework.

        Real rework is minutes or hours apart, so this is the realistic case,
        not a convenient one. The sub-second case is a genuine limit of the
        second-precision tolerance and is left alone deliberately: narrowing it
        would reintroduce the Cursor staleness it exists to absorb.
        """
        from datetime import datetime

        cursor = {"t": datetime.fromisoformat(start)}

        def tick():
            cursor["t"] += timedelta(minutes=1)
            return cursor["t"]

        import advance_kernel as ak_mod
        import story_pipeline as sp_mod

        monkeypatch.setattr(
            sp_mod, "_now", lambda: tick().isoformat().replace("+00:00", "Z")
        )
        monkeypatch.setattr(ak_mod, "_now_iso", lambda: tick().isoformat())

    def _reject_for_rework(self, project) -> None:
        """The supported rework path, through the verbs a PO actually calls.

        The version this replaces set `story["state"]` and dropped the binding
        by hand and called itself "the supported rework path, reduced to its
        effect on the board". The reduction removed the part that matters: a
        real `reject_story` appends a NEW `in_progress` entry to the story's
        pipeline log, and that entry's `entered_at` is exactly what
        `evaluate_dispatch` compares a leftover receipt against. Reduced away,
        the second cycle was measured against the FIRST cycle's stage entry.

        `transition_story` mutates the state dict without persisting it, so the
        write is explicit here. Omitting it silently measures the seeded board.
        """
        import story_pipeline as sp_mod

        state = sp_mod._read_state(str(project))
        for target in ("reviewing", "awaiting_acceptance"):
            state = sp_mod.transition_story(
                state, "US-001", target, reason=None, project_dir=str(project)
            )
        state = sp_mod.reject_story(
            state,
            "US-001",
            "needs-fix",
            "the reviewer asked for another pass",
            "product-owner",
            project_dir=str(project),
        )
        sp_mod._write_state(str(project), state)
        story = sp_mod.get_story(sp_mod._read_state(str(project)), "US-001")
        assert story["state"] == "in_progress", story["state"]
        assert not story.get("mcp_active_dispatches", {}).get("se"), (
            "rework left a live SE binding, so the second cycle would inherit "
            "the first one's authority instead of asking for its own"
        )

    def test_a_completed_attempt_cannot_authorize_the_next_cycle(self, tmp_path):
        """Asserts impossible: last cycle's authority carrying this cycle."""
        project = _project(tmp_path)
        _seed(project, state="queued")
        completed = self._walk_one_cycle(project)
        self._reject_for_rework(project)

        _receipt(
            project, abbrev="se", role="software-engineer", attempt_id=completed
        )
        decision = ak.execute_advance(
            str(project), "US-001", "testing", policy=_policy()
        )
        assert not decision.allowed
        assert decision.code == ak.ATTEMPT_BINDING_MISMATCH
        assert completed in decision.reason

    def test_and_the_second_cycle_does_not_move(self, tmp_path):
        """The board, not the decision object. #608's own lesson."""
        project = _project(tmp_path)
        _seed(project, state="queued")
        completed = self._walk_one_cycle(project)
        self._reject_for_rework(project)
        _receipt(
            project, abbrev="se", role="software-engineer", attempt_id=completed
        )
        ak.execute_advance(str(project), "US-001", "testing", policy=_policy())
        state = json.loads(
            (
                project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
            ).read_text()
        )
        assert state["current_stories"][0]["state"] == "in_progress"

    def test_a_reworked_cycle_obtains_its_own_live_binding(self, tmp_path, monkeypatch):
        """The positive control I claimed was impossible, and it is not.

        I first wrote this to assert that the second cycle obtains a FRESH
        dispatch, then replaced it with a weaker one and recorded the reason as
        measured fact: that `execute_dispatch` refuses a rework re-dispatch
        with `receipt_already_present` on `origin/dev` too, so a reworked unit
        can never hold a live binding. I filed #613 on the strength of it.

        That measurement was an artifact of this module's own clock. A receipt
        dated 2090 is fresh relative to every stage entry that will ever exist,
        so the freshness rule kept reporting cycle 1's receipt as current no
        matter how the unit re-entered the stage. Isolated on `fc8b4d33` with
        the walk and the state writes held constant and ONLY the receipt date
        changed:

            cycle-1 receipt dated a minute after entry -> allowed=True  ok
            cycle-1 receipt dated 2090 (this module)   -> allowed=False
                                                          receipt_already_present

        The product was right the whole time. `stage_entered_at` returns the
        LAST log entry matching the current state, so a real rework re-entry
        is the moment a leftover receipt is measured against, which is what
        #613 proposed be built and what was already there.

        So a reworked cycle CAN earn `attested` credit, on its own live
        attempt, which is what the evidence model intends. The claim that it
        structurally cannot was mine and it was wrong.
        """
        self._minutes_apart(monkeypatch)
        project = _project(tmp_path)
        _seed(project, state="queued")
        first = self._walk_one_cycle(project)
        self._reject_for_rework(project)

        decision = ak.execute_dispatch(
            str(project), "US-001", role="software-engineer", policy=_policy()
        )
        assert decision.allowed, decision.reason

        story = json.loads(
            (
                project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
            ).read_text()
        )["current_stories"][0]
        live = story["mcp_active_dispatches"]["se"]
        assert live["attempt_id"] != first, (
            "the second cycle is holding the first cycle's attempt id, which is "
            "the authority #612 closed"
        )

        # And the credit follows the binding: a receipt naming the NEW attempt
        # advances, which is the `attested` path that was reported as
        # structurally unreachable for rework.
        entered = ak.stage_entered_at(story)
        _receipt(
            project,
            abbrev="se",
            role="software-engineer",
            dispatch_id=live["dispatch_id"],
            attempt_id=live["attempt_id"],
            completed_at=(entered + timedelta(seconds=1))
            .isoformat()
            .replace("+00:00", "Z"),
        )
        second = ak.execute_advance(
            str(project), "US-001", "testing", policy=_policy()
        )
        assert second.allowed, second.reason

    def test_an_in_progress_board_with_no_ledger_is_still_refused(self, tmp_path):
        """Asserts impossible: loosening the guard into a board it cannot read.

        The rework fix answers "no binding" with the ledger. A board that has
        no ledger has not answered anything, and that is the pre-#492 shape the
        concurrency guard was written to be conservative about: a first process
        may be running work this call cannot see. It must still refuse.

        Without this, the rework fix reads as "no binding means go ahead",
        which is the opposite of what it is for.
        """
        project = _project(tmp_path)
        _seed(project, state="in_progress")
        decision = ak.execute_dispatch(
            str(project), "US-001", role="software-engineer", policy=_policy()
        )
        assert not decision.allowed
        assert decision.code == ak.DISPATCH_ALREADY_STARTED

    def test_a_ledger_that_records_no_finish_is_still_refused(self, tmp_path):
        """Asserts impossible: a record being mistaken for a finished record.

        An attempt whose record exists but carries no `advanced_at` and no
        terminal state is one nobody has shown to have stopped. The binding may
        have been cleared by hand, or the writer may have died between popping
        it and stamping the outcome. Either way the honest answer is the
        conservative one.
        """
        import advance_kernel as ak_mod

        story = {
            "id": "US-001",
            "state": "in_progress",
            ak_mod.AUTHORIZED_ATTEMPTS_KEY: {"se": [{"attempt_id": "att-1"}]},
        }
        assert ak_mod._last_attempt_terminated(story, "se") is False
        assert ak_mod._incumbent_is_live(story, "se") is True

    def test_a_ledger_that_records_a_finish_is_answered(self, tmp_path):
        """The other half, so the pair reads as one rule rather than two.

        `advanced_at` is stamped by the advance that popped the binding, so it
        is the kernel's own record that this attempt's work landed.
        """
        import advance_kernel as ak_mod

        story = {
            "id": "US-001",
            "state": "in_progress",
            ak_mod.AUTHORIZED_ATTEMPTS_KEY: {
                "se": [{"attempt_id": "att-1", "advanced_at": "2026-05-01T09:02:00Z"}]
            },
        }
        assert ak_mod._last_attempt_terminated(story, "se") is True
        assert ak_mod._incumbent_is_live(story, "se") is False

    def test_the_history_union_is_still_used_for_nested_evidence(self):
        """The rule kept separate, asserted so a later edit does not merge them.

        A predecessor attempt genuinely can be the producer of a finding an
        `evidence[]` item attests to, so that check reads the ledger. Only the
        TOP-LEVEL identity was narrowed.
        """
        import inspect

        source = inspect.getsource(ak.evaluate_advance)
        top, _, nested = source.partition("#493. The item-level half")
        assert "_authorized_attempts(" not in top, (
            "the top-level identity check consults the ledger again, so a "
            "completed attempt can authorize a new receipt"
        )
        assert "_authorized_attempts(" in nested, (
            "the nested evidence check no longer consults the ledger, so a "
            "predecessor attempt's honest attestation is refused"
        )
