"""Layer 1 -- typed evidence classes and `criteria_gap_declared` (#403).

Epic #410, capability-profile-pilot.md 3.3. Every test here is a NEGATIVE
test or the positive control for one, because the ticket exists in its
current form for one reason: `Receipt.payload` is an unrestricted client
dict, #421 credited a DoD check at whatever `evidence_class` its payload
named, and it took #432, #436 and #445 to walk that back on the read side.
This module is the producing side those three corrections had to mirror, so
the rules live here and the suite proves each one refuses something.

The three forgeries, and where each is refused:

  replayed  Untrusted input: `payload.evidence[].evidence_class` plus the
            command and exit code beside it. Forgery: a bare
            `{"evidence_class": "replayed"}`, asserting that a machine
            re-ran something while naming nothing that could be re-run.
            Refused by `runtime_contracts.validate_evidence_item`'s
            `command` / `exit_code` clauses; not credited by
            `backed_evidence_class`.

  attested  Untrusted input: the same payload key. Forgery: an attestation
            with no producing attempt identity, which belongs to no bounded
            execution and so can be minted after the fact by anyone.
            Refused by the `attempt_id` clause, shape-checked against
            `ATTEMPT_ID_RE`.

            CORRECTED BY #493, and the correction is the epic's fifth. The
            paragraph above was the whole answer this module gave, and a
            shape check refuses a MALFORMED id, never an UNAUTHORIZED one:
            `att_ffffffffffffffffffff` matches the pattern, named an
            execution that never ran, and advanced. The second forgery is
            therefore an attestation naming a WELL-FORMED attempt the kernel
            never authorized, and this module cannot refuse it, because
            authorization needs the story's dispatch record and this module
            is a dependency leaf with no project state. What it owns is the
            claim reader `attested_attempt_claims`, tested below; the
            comparison lives in `advance_kernel.evaluate_advance` and is
            tested in `test_kernel_attempt_binding.py`.

  judged    Untrusted input: the same payload key, and separately the
            `accepted_by` / `rejected_by` argument on the acceptance verbs.
            Forgery: a verdict with no named principal, no candidate digest
            and no recorded independence, which is what #421 counted as
            judged-backed. Refused by `validate_judged_verdict`; and on the
            acceptance path every discipline field except the principal is
            DERIVED by `story_pipeline.record_judged_verdict`, so a caller
            cannot choose what its verdict is a verdict about.

Plus the fourth rule, which is not a class: `criteria_gap_declared` is
produced only by `evaluate_story_dod` from the absence of evaluable
evidence. A receipt-supplied check result lands in a `self_reported` slot no
gate reads, so a forged gap changes nothing and a forged pass clears
nothing.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import receipt_validator as rv
import runtime_contracts as rc
import story_pipeline as sp
import verification_runner as vr


pytestmark = pytest.mark.unit


# ── fixtures ────────────────────────────────────────────────────────────────


def _judged(**overrides) -> dict:
    item = {
        "evidence_class": "judged",
        "principal": "alice@h3t.co",
        "candidate_digest": "sha256:" + "d" * 64,
        "verdict": "accepted",
        "recorded_at": "2026-09-02T10:15:00Z",
        "independent_of_producer": True,
    }
    item.update(overrides)
    return item


def _replayed(**overrides) -> dict:
    item = {"evidence_class": "replayed", "command": "pytest -q", "exit_code": 0}
    item.update(overrides)
    return item


def _attested(**overrides) -> dict:
    item = {"evidence_class": "attested", "attempt_id": "att_01J6EVID02"}
    item.update(overrides)
    return item


def _receipts_dir(project: Path) -> Path:
    """The canonical receipts dir, so `_resolve_receipts_dir` and the explicit
    `receipts_dir` argument name the same directory. `record_judged_verdict`
    resolves the path itself (there is no override), so a test that fed
    `evaluate_story_dod` a private directory would derive a candidate digest
    over an empty set and prove nothing."""
    return project / ".synaptory" / ".orchestrator" / "receipts"


def _project(tmp_path: Path, config: str = "project_id: t\n") -> Path:
    (tmp_path / ".synaptory.yaml").write_text(config, encoding="utf-8")
    _receipts_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _receipt(
    project: Path,
    story_id: str,
    abbrev: str,
    role: str,
    **fields,
) -> Path:
    payload = {"story_id": story_id, "role": role, "agent": role}
    payload.update(fields)
    path = _receipts_dir(project) / f"{story_id}-{abbrev}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _green_se_qe(project: Path, story_id: str, **extra) -> None:
    """SE and QE receipts with executed proof for the two early-tier checks."""
    _receipt(
        project, story_id, "se", "software-engineer",
        verification_commands=[{"command": "npm run build", "exit_code": 0}],
        metrics={"findings_critical": 0},
        **extra,
    )
    _receipt(
        project, story_id, "qe", "quality-engineer",
        verification_commands=[{"command": "pytest -q", "exit_code": 0}],
        metrics={"findings_critical": 0, "coverage_delta": "+0.4%"},
        **extra,
    )


def _dod(project: Path, story_id: str, intensity: str = "early") -> dict:
    return sp.evaluate_story_dod(
        str(project), story_id, intensity,
        receipts_dir=str(_receipts_dir(project)),
    )


# ── the replayed discipline ─────────────────────────────────────────────────


class TestReplayedDiscipline:
    def test_a_bare_class_token_backs_nothing(self):
        """The #432 defect in its purest form: a token with no subject."""
        assert rc.backed_evidence_class({"evidence_class": "replayed"}) is None

    def test_a_command_that_was_never_executed_backs_nothing(self):
        assert rc.backed_evidence_class(
            {"evidence_class": "replayed", "command": "pytest -q"}
        ) is None

    def test_an_executed_command_backs_replayed(self):
        assert rc.backed_evidence_class(_replayed()) == "replayed"

    def test_the_runner_is_the_replayed_class(self):
        """`verification_runner` semantics are untouched and tagged."""
        assert vr.EVIDENCE_CLASS == "replayed"
        assert vr.VerificationResult().to_dict()["evidence_class"] == "replayed"


# ── the attested discipline ─────────────────────────────────────────────────


class TestAttestedDiscipline:
    def test_an_attestation_with_no_attempt_id_backs_nothing(self):
        assert rc.backed_evidence_class(
            {"evidence_class": "attested", "finding": "looks fine"}
        ) is None

    def test_a_story_id_is_not_an_attempt_id(self):
        assert rc.backed_evidence_class(
            {"evidence_class": "attested", "attempt_id": "US-142"}
        ) is None

    def test_a_bound_attestation_backs_attested(self):
        assert rc.backed_evidence_class(_attested()) == "attested"

    def test_a_wellformed_but_unauthorized_attempt_still_backs_attested(self):
        """The line this module CANNOT hold, stated as a test so it stops
        being read as a refusal (#493).

        `att_ffffffffffffffffffff` matches ATTEMPT_ID_RE and named no
        execution that ever ran, and the shape check says yes. That is
        correct behaviour HERE and a defect only where it was mistaken for
        authorization: this module has no story to compare against.
        """
        forged = _attested(attempt_id="att_ffffffffffffffffffff")
        assert rc.validate_evidence_item(forged) == []
        assert rc.backed_evidence_class(forged) == "attested"


class TestAttestedAttemptClaims:
    """`attested_attempt_claims` is the leaf half of #493: it reports which
    executions a receipt attributes its attestations to, so the kernel can
    ask whether it ever authorized them."""

    def test_it_reports_the_index_and_the_attempt_of_each_attested_item(self):
        receipt = {
            "evidence": [
                _replayed(),
                _attested(attempt_id="att_01J6EVID02"),
                _attested(attempt_id="att_01J6EVID99"),
            ]
        }
        assert rc.attested_attempt_claims(receipt) == [
            (1, "att_01J6EVID02"),
            (2, "att_01J6EVID99"),
        ]

    def test_only_attested_items_make_a_claim(self):
        """A replayed item carrying an attempt_id is not an attestation, and
        binding it would put a rule on a class whose discipline is replay."""
        receipt = {"evidence": [_replayed(attempt_id="att_01J6EVID02")]}
        assert rc.attested_attempt_claims(receipt) == []

    def test_a_malformed_attempt_id_makes_no_claim(self):
        """Already refused by `validate_evidence_item`, and the kernel
        reaches `receipt_invalid` before the binding check, so reporting it
        again would produce two messages for one defect."""
        receipt = {"evidence": [{"evidence_class": "attested", "attempt_id": "US-142"}]}
        assert rc.attested_attempt_claims(receipt) == []
        assert rc.validate_evidence_item(receipt["evidence"][0])

    def test_a_receipt_with_no_evidence_claims_nothing(self):
        assert rc.attested_attempt_claims({}) == []
        assert rc.attested_attempt_claims({"evidence": "not a list"}) == []
        assert rc.attested_attempt_claims("not a receipt") == []

    def test_it_judges_nothing(self):
        """The reader must NOT decide authorization: it has no story. A claim
        naming an attempt that never existed is reported exactly like one
        naming the live attempt, and the difference is the kernel's to draw."""
        forged = {"evidence": [_attested(attempt_id="att_ffffffffffffffffffff")]}
        assert rc.attested_attempt_claims(forged) == [
            (0, "att_ffffffffffffffffffff")
        ]


# ── the judged discipline ───────────────────────────────────────────────────


class TestJudgedDiscipline:
    def test_the_421_payload_backs_nothing(self):
        """The exact shape #421 credited: a class claim and nothing else."""
        assert rc.backed_evidence_class({"evidence_class": "judged"}) is None

    @pytest.mark.parametrize(
        "field",
        [
            "principal",
            "candidate_digest",
            "verdict",
            "recorded_at",
            "independent_of_producer",
        ],
    )
    def test_each_missing_discipline_field_costs_the_class(self, field):
        item = _judged()
        del item[field]
        assert rc.backed_evidence_class(item) is None, field
        assert any(
            p.startswith(field + ":") for p in rc.validate_evidence_item(item)
        ), field

    def test_non_independence_is_recorded_never_refused(self):
        """The decision pinned during #399: false is a valid, recorded value."""
        item = _judged(independent_of_producer=False)
        assert rc.validate_evidence_item(item) == []
        assert rc.backed_evidence_class(item) == "judged"

    def test_a_missing_independence_field_is_not_the_same_as_false(self):
        item = _judged()
        del item["independent_of_producer"]
        assert rc.backed_evidence_class(item) is None


# ── per-check linkage ───────────────────────────────────────────────────────


class TestEvidenceCheckLinkage:
    def test_an_item_naming_no_check_backs_no_check(self):
        """One well-shaped item must not credit five checks (#432)."""
        assert rc.evidence_class_by_check([_replayed()]) == {}

    def test_an_item_backs_only_the_check_it_names(self):
        backed = rc.evidence_class_by_check([_replayed(check_id="tests_pass")])
        assert backed == {"tests_pass": ("replayed",)}

    def test_both_spellings_of_a_check_id_normalize(self):
        backed = rc.evidence_class_by_check(
            [_attested(check_id="coverage-no-decrease")]
        )
        assert backed == {"coverage_no_decrease": ("attested",)}

    def test_an_unbacked_item_credits_its_named_check_with_nothing(self):
        backed = rc.evidence_class_by_check(
            [{"evidence_class": "judged", "check_id": "code_reviewed"}]
        )
        assert backed == {}

    def test_an_empty_check_id_is_refused_by_name(self):
        problems = rc.validate_evidence_item(_replayed(check_id="   "))
        assert any(p.startswith("check_id:") for p in problems)


# ── the receipt boundary ────────────────────────────────────────────────────


class TestReceiptValidatorRefusals:
    def _base(self, tmp_path: Path) -> dict:
        (tmp_path / "src").mkdir(exist_ok=True)
        (tmp_path / "src" / "a.py").write_text("x\n", encoding="utf-8")
        return {
            "story_id": "US-001",
            "role": "software-engineer",
            "backend": "claude",
            "model": "claude-sonnet",
            "artifacts": ["src/a.py"],
            "verification_commands": [
                {"command": "pytest -q", "exit_code": 0, "summary": "ok"}
            ],
            "metrics": {"n": 1},
            "completed_at": "2026-09-02T10:00:00Z",
        }

    def test_a_receipt_with_no_evidence_block_is_unaffected(self, tmp_path):
        """The block is opt-in: absence must not turn a valid receipt bad."""
        result = rv.validate_receipt_payload(self._base(tmp_path), str(tmp_path))
        assert result.valid, result.errors

    def test_a_well_formed_evidence_block_validates(self, tmp_path):
        payload = self._base(tmp_path)
        payload["evidence"] = [_replayed(check_id="tests_pass"), _attested()]
        result = rv.validate_receipt_payload(payload, str(tmp_path))
        assert result.valid, result.errors

    def test_a_forged_judged_claim_invalidates_the_receipt(self, tmp_path):
        payload = self._base(tmp_path)
        payload["evidence"] = [{"evidence_class": "judged"}]
        result = rv.validate_receipt_payload(payload, str(tmp_path))
        assert not result.valid
        joined = " ".join(result.errors)
        for field in ("principal", "candidate_digest", "verdict", "recorded_at"):
            assert field in joined, field
        assert "evidence[0]" in joined

    def test_an_unbound_attestation_invalidates_the_receipt(self, tmp_path):
        payload = self._base(tmp_path)
        payload["evidence"] = [{"evidence_class": "attested"}]
        result = rv.validate_receipt_payload(payload, str(tmp_path))
        assert not result.valid
        assert any("attempt_id" in e for e in result.errors)

    def test_a_replay_claim_with_no_execution_invalidates_the_receipt(self, tmp_path):
        payload = self._base(tmp_path)
        payload["evidence"] = [{"evidence_class": "replayed", "command": "pytest"}]
        result = rv.validate_receipt_payload(payload, str(tmp_path))
        assert not result.valid
        assert any("exit_code" in e for e in result.errors)

    def test_a_non_list_evidence_block_is_refused(self, tmp_path):
        payload = self._base(tmp_path)
        payload["evidence"] = {"evidence_class": "replayed"}
        result = rv.validate_receipt_payload(payload, str(tmp_path))
        assert not result.valid

    def test_a_gap_with_no_description_is_refused(self, tmp_path):
        """A gap that does not say what is missing is a silent pass wearing
        a different label."""
        payload = self._base(tmp_path)
        payload[rv.DOD_CHECK_RESULTS_KEY] = [
            {"check_id": "tests_pass", "result": "criteria_gap_declared"}
        ]
        result = rv.validate_receipt_payload(payload, str(tmp_path))
        assert not result.valid
        assert any("detail" in e for e in result.errors)

    def test_a_typed_result_is_announced_as_self_reported(self, tmp_path):
        payload = self._base(tmp_path)
        payload[rv.DOD_CHECK_RESULTS_KEY] = [
            {"check_id": "tests_pass", "result": "pass"}
        ]
        result = rv.validate_receipt_payload(payload, str(tmp_path))
        assert result.valid, result.errors
        assert any("SELF-REPORTED" in w for w in result.warnings)

    def test_the_seam_key_matches_the_pipeline(self):
        """#407 named one payload key for this ticket; two spellings would
        mean the control plane reads a key nothing writes."""
        assert rv.DOD_CHECK_RESULTS_KEY == sp.DOD_CHECK_RESULTS_KEY == "dod_check_results"


# ── criteria_gap_declared ───────────────────────────────────────────────────


class TestCriteriaGapDeclared:
    def test_no_receipts_at_all_declares_a_gap_on_every_required_check(self, tmp_path):
        project = _project(tmp_path)
        dod = _dod(project, "US-100")
        for cid in ("tests_pass", "build_succeeds"):
            check = dod["checks"][cid]
            assert check["required"] is True
            assert check["result"] == sp.CRITERIA_GAP_DECLARED
            assert check["passed"] is None
            assert "Missing evidence" in check["detail"]

    def test_a_gap_never_reads_as_a_pass(self, tmp_path):
        project = _project(tmp_path)
        dod = _dod(project, "US-100")
        assert dod["passed"] is False
        assert dod["critical_passed"] is False

    def test_a_gap_blocks_the_dod_gate(self, tmp_path):
        project = _project(tmp_path)
        reason = sp.dod_gate_block_reason(_dod(project, "US-100"))
        assert reason is not None
        assert "criteria gap" in reason
        assert "tests_pass" in reason

    def test_the_block_routes_to_a_role_that_can_produce_the_evidence(self, tmp_path):
        project = _project(tmp_path)
        reason = sp.dod_gate_block_reason(_dod(project, "US-100"))
        remediation = sp._gate_remediation({"blocked_reason": reason})
        assert remediation["tier"] == "gate_remediation"
        assert remediation["role"] in ("qe", "se")

    def test_a_receipt_with_only_replay_instructions_declares_a_gap(self, tmp_path):
        """#106's case. A plain string is an instruction, not evidence."""
        project = _project(tmp_path)
        _receipt(
            project, "US-101", "qe", "quality-engineer",
            verification_commands=["pytest -q"],
        )
        check = _dod(project, "US-101")["checks"]["tests_pass"]
        assert check["result"] == sp.CRITERIA_GAP_DECLARED
        assert "exit_code" in check["detail"]

    def test_evidence_clears_the_gap(self, tmp_path):
        project = _project(tmp_path)
        _green_se_qe(project, "US-102")
        dod = _dod(project, "US-102")
        assert dod["checks"]["tests_pass"]["result"] == "pass"
        assert dod["checks"]["build_succeeds"]["result"] == "pass"
        assert dod["passed"] is True
        assert sp.dod_gate_block_reason(dod) is None

    def test_a_check_not_required_at_this_tier_gets_no_result(self, tmp_path):
        """A gap is the absence of evidence for something REQUIRED. Inventing
        one for a check the tier never asked about would be the same silent
        nothing in the other direction."""
        project = _project(tmp_path)
        _green_se_qe(project, "US-103")
        dod = _dod(project, "US-103", intensity="early")
        assert "result" not in dod["checks"]["code_reviewed"]
        assert dod["checks"]["code_reviewed"]["required"] is False
        ids = [item["check_id"] for item in dod[sp.DOD_CHECK_RESULTS_KEY]]
        assert "code_reviewed" not in ids

    def test_a_definitive_negative_is_a_fail_not_a_gap(self, tmp_path):
        """#187's authored-case veto. Evidence exists and it says no, which
        is a different fact from having nothing to evaluate."""
        project = _project(tmp_path)
        _receipt(
            project, "US-104", "se", "software-engineer",
            verification_commands=[{"command": "npm run build", "exit_code": 0}],
        )
        _receipt(
            project, "US-104", "qe", "quality-engineer",
            verification_commands=["pytest -q"],
            metrics={
                "authored_test_cases": {
                    "total": 1,
                    "results": {"AC-1": {"status": "failed"}},
                }
            },
        )
        check = _dod(project, "US-104")["checks"]["tests_pass"]
        assert check["result"] == "fail"
        assert "authored test case" in check["detail"]

    def test_the_typed_result_list_is_the_control_plane_shape(self, tmp_path):
        project = _project(tmp_path)
        dod = _dod(project, "US-105")
        items = dod[sp.DOD_CHECK_RESULTS_KEY]
        assert items
        for item in items:
            assert rc.validate_dod_check_result(item) == [], item


# ── a forged criteria_gap_declared, and a forged pass ───────────────────────


class TestForgedTypedResults:
    def test_a_forged_gap_cannot_dodge_a_check_that_has_evidence(self, tmp_path):
        """The ticket's own question. A member writes
        `dod_check_results: [{tests_pass, criteria_gap_declared}]` onto a
        story whose evidence proves tests_pass. The computed verdict stands;
        the claim is recorded as self-reported and reads nowhere else."""
        project = _project(tmp_path)
        _green_se_qe(
            project, "US-200",
            dod_check_results=[{
                "check_id": "tests_pass",
                "result": "criteria_gap_declared",
                "detail": "I would rather this were not evaluated",
            }],
        )
        dod = _dod(project, "US-200")
        check = dod["checks"]["tests_pass"]
        assert check["result"] == "pass"
        assert check["self_reported"]["result"] == "criteria_gap_declared"
        assert sp.dod_gate_block_reason(dod) is None
        ids = {
            item["check_id"]: item["result"]
            for item in dod[sp.DOD_CHECK_RESULTS_KEY]
        }
        assert ids["tests_pass"] == "pass"

    def test_a_forged_pass_cannot_clear_a_real_gap(self, tmp_path):
        project = _project(tmp_path)
        _receipt(
            project, "US-201", "qe", "quality-engineer",
            verification_commands=["pytest -q"],
            dod_check_results=[
                {"check_id": "tests_pass", "result": "pass"},
                {"check_id": "build_succeeds", "result": "pass"},
            ],
        )
        dod = _dod(project, "US-201")
        assert dod["checks"]["tests_pass"]["result"] == sp.CRITERIA_GAP_DECLARED
        assert dod["checks"]["build_succeeds"]["result"] == sp.CRITERIA_GAP_DECLARED
        assert dod["passed"] is False
        assert sp.dod_gate_block_reason(dod) is not None

    def test_a_typed_result_refines_per_check_and_suppresses_nothing(self, tmp_path):
        """The #432 amplification, restated locally: a claim about ONE check
        must leave every other computed verdict exactly as it was."""
        project = _project(tmp_path)
        _green_se_qe(
            project, "US-202",
            dod_check_results=[{"check_id": "tests_pass", "result": "fail"}],
        )
        _receipt(project, "US-202", "cr", "code-reviewer", status="complete")
        dod = _dod(project, "US-202", intensity="mature")
        computed = {
            cid: dod["checks"][cid]["result"]
            for cid in (
                "tests_pass", "build_succeeds", "code_reviewed",
                "coverage_no_decrease",
            )
        }
        assert computed == {
            "tests_pass": "pass",
            "build_succeeds": "pass",
            "code_reviewed": "pass",
            "coverage_no_decrease": "pass",
        }
        # Only the claimed check carries a self_reported slot.
        assert "self_reported" in dod["checks"]["tests_pass"]
        for cid in ("build_succeeds", "code_reviewed", "coverage_no_decrease"):
            assert "self_reported" not in dod["checks"][cid], cid

    def test_a_malformed_typed_claim_is_dropped_not_adopted(self, tmp_path):
        project = _project(tmp_path)
        _green_se_qe(
            project, "US-203",
            dod_check_results=[{"check_id": "tests_pass", "result": "green"}],
        )
        dod = _dod(project, "US-203")
        assert dod["checks"]["tests_pass"]["result"] == "pass"
        assert "self_reported" not in dod["checks"]["tests_pass"]


# ── derived evidence class per check ────────────────────────────────────────


class TestDerivedEvidenceClass:
    def test_a_receipt_with_no_attempt_identity_backs_no_depth(self, tmp_path):
        """Zero depth shows as zero. It does not round up to the weakest
        class that would fit."""
        project = _project(tmp_path)
        _green_se_qe(project, "US-300")
        dod = _dod(project, "US-300")
        assert dod["checks"]["tests_pass"]["evidence_class"] is None

    def test_an_attempt_bound_receipt_backs_attested(self, tmp_path):
        project = _project(tmp_path)
        _green_se_qe(project, "US-301", attempt_id="att_01J6EVID01")
        dod = _dod(project, "US-301")
        assert dod["checks"]["tests_pass"]["evidence_class"] == "attested"
        item = next(
            i for i in dod[sp.DOD_CHECK_RESULTS_KEY]
            if i["check_id"] == "tests_pass"
        )
        assert item["evidence_class"] == "attested"

    def test_a_payload_cannot_name_its_own_depth(self, tmp_path):
        """The #421 forgery at the producing side: a receipt declaring
        `evidence_class` on its own check result gets no depth from it."""
        project = _project(tmp_path)
        _green_se_qe(
            project, "US-302",
            dod_check_results=[{
                "check_id": "tests_pass",
                "result": "pass",
                "evidence_class": "judged",
            }],
        )
        dod = _dod(project, "US-302")
        assert dod["checks"]["tests_pass"]["evidence_class"] is None

    def test_a_gap_is_backed_by_nothing(self, tmp_path):
        project = _project(tmp_path)
        _receipt(
            project, "US-303", "qe", "quality-engineer",
            attempt_id="att_01J6EVID01",
            verification_commands=["pytest -q"],
        )
        dod = _dod(project, "US-303")
        assert dod["checks"]["tests_pass"]["result"] == sp.CRITERIA_GAP_DECLARED
        assert dod["checks"]["tests_pass"]["evidence_class"] is None


# ── judged verdicts on the acceptance path ─────────────────────────────────


def _awaiting(story_id: str = "US-400") -> dict:
    return {
        "current_stories": [{
            "id": story_id,
            "title": "t",
            "state": "awaiting_acceptance",
            "pipeline_log": [
                {"state": "awaiting_acceptance", "entered_at": "t0", "exited_at": None}
            ],
            "receipts": [],
            "retries": {},
        }],
    }


class TestJudgedVerdictsOnAcceptance:
    def test_accept_records_a_judged_verdict_bound_to_the_candidate(self, tmp_path):
        project = _project(tmp_path)
        _green_se_qe(project, "US-400")
        state = _awaiting()
        sp.accept_story(state, "US-400", "po@h3t.co", project_dir=str(project))
        story = sp._find_story(state, "US-400")
        verdicts = story[sp.JUDGED_VERDICTS_KEY]
        assert len(verdicts) == 1
        item = verdicts[0]
        assert item["evidence_class"] == "judged"
        assert item["verdict"] == "accepted"
        assert item["principal"] == "po@h3t.co"
        assert item["candidate_digest"].startswith("sha256:")
        assert rc.validate_evidence_item(item) == []

    def test_reject_records_the_other_verdict_through_the_same_path(self, tmp_path):
        project = _project(tmp_path)
        _green_se_qe(project, "US-400")
        state = _awaiting()
        sp.reject_story(
            state, "US-400", "needs-fix", "add validation", "po@h3t.co",
            project_dir=str(project),
        )
        item = sp._find_story(state, "US-400")[sp.JUDGED_VERDICTS_KEY][0]
        assert item["verdict"] == "rejected"
        assert item["evidence_class"] == "judged"
        assert rc.validate_evidence_item(item) == []

    def test_the_candidate_digest_is_derived_from_the_receipt_bytes(self, tmp_path):
        """Not supplied. There is no parameter for it, and its value is the
        digest of what is actually on disk, so it changes when the work
        changes."""
        project = _project(tmp_path)
        _green_se_qe(project, "US-400")
        expected = hashlib.sha256()
        for path in sorted(_receipts_dir(project).glob("US-400-*.json")):
            expected.update(path.name.encode("utf-8"))
            expected.update(path.read_bytes())
        state = _awaiting()
        sp.accept_story(state, "US-400", "po@h3t.co", project_dir=str(project))
        item = sp._find_story(state, "US-400")[sp.JUDGED_VERDICTS_KEY][0]
        assert item["candidate_digest"] == "sha256:" + expected.hexdigest()

    def test_changing_the_evidence_changes_the_candidate(self, tmp_path):
        project = _project(tmp_path)
        _green_se_qe(project, "US-400")
        first = sp._story_candidate_digest({}, "US-400", str(project))[0]
        _receipt(
            project, "US-400", "cr", "code-reviewer", status="complete",
        )
        second = sp._story_candidate_digest({}, "US-400", str(project))[0]
        assert first is not None and second is not None
        assert first != second

    def test_the_kernel_ledger_wins_over_the_on_disk_digest(self, tmp_path):
        """Where the kernel recorded the digest of the bytes it consumed, that
        is the candidate: it was computed at the moment of consumption."""
        project = _project(tmp_path)
        _green_se_qe(project, "US-400")
        story = {"mcp_consumed_receipts": ["a" * 64, "b" * 64]}
        digest, why, source = sp._story_candidate_digest(
            story, "US-400", str(project)
        )
        assert digest == "sha256:" + "b" * 64
        assert why is None
        assert source == sp.CANDIDATE_SOURCE_LEDGER

    def test_a_story_with_no_candidate_records_the_verdict_unbacked(self, tmp_path):
        """A verdict with no subject is an opinion. It is recorded, and it is
        not credited as judged evidence."""
        project = _project(tmp_path)
        state = _awaiting()
        story = sp._find_story(state, "US-400")
        # Recorded directly, because this asks what the VERDICT is, not whether
        # the unit may advance on it. Since #592 `accept_story` refuses an
        # uncreditable acceptance, which is the very item this test builds.
        item = sp.record_judged_verdict(
            story, "US-400", verdict="accepted", principal="po@h3t.co",
            project_dir=str(project),
        )
        assert item["evidence_class"] is None
        assert item["candidate_digest"] is None
        assert "no candidate" in item["unbacked_reason"]
        assert rc.validate_evidence_item(item) != []

    def test_re_judging_the_same_candidate_is_not_credited(self, tmp_path):
        """Immutable once recorded: the first entry survives untouched and the
        second is appended unbacked, naming what a re-judge requires."""
        project = _project(tmp_path)
        _green_se_qe(project, "US-400")
        story = sp._find_story(_awaiting(), "US-400")
        first = sp.record_judged_verdict(
            story, "US-400", verdict="rejected", principal="po@h3t.co",
            project_dir=str(project),
        )
        snapshot = dict(first)
        second = sp.record_judged_verdict(
            story, "US-400", verdict="accepted", principal="po@h3t.co",
            project_dir=str(project),
        )
        assert first == snapshot, "a recorded verdict was mutated"
        assert second["evidence_class"] is None
        assert "new candidate" in second["unbacked_reason"]
        assert len(story[sp.JUDGED_VERDICTS_KEY]) == 2

    def test_new_evidence_makes_a_new_candidate_judgeable(self, tmp_path):
        project = _project(tmp_path)
        _green_se_qe(project, "US-400")
        story = sp._find_story(_awaiting(), "US-400")
        sp.record_judged_verdict(
            story, "US-400", verdict="rejected", principal="po@h3t.co",
            project_dir=str(project),
        )
        _receipt(project, "US-400", "cr", "code-reviewer", status="complete")
        again = sp.record_judged_verdict(
            story, "US-400", verdict="accepted", principal="po@h3t.co",
            project_dir=str(project),
        )
        assert again["evidence_class"] == "judged"

    def test_independence_fails_closed_when_no_producer_is_on_record(self, tmp_path):
        """Receipts record `agent`, not a human, so there is usually nothing
        to compare a judge against. Recording false is honest; recording true
        would be assuming the thing the field exists to state."""
        project = _project(tmp_path)
        _green_se_qe(project, "US-400")
        story = sp._find_story(_awaiting(), "US-400")
        item = sp.record_judged_verdict(
            story, "US-400", verdict="accepted", principal="po@h3t.co",
            project_dir=str(project),
        )
        assert item["independent_of_producer"] is False
        assert "could not be established" in item["independence_basis"]

    def test_independence_is_true_when_a_different_human_produced(self, tmp_path):
        project = _project(tmp_path)
        _green_se_qe(project, "US-400", principal="dev@h3t.co")
        story = sp._find_story(_awaiting(), "US-400")
        item = sp.record_judged_verdict(
            story, "US-400", verdict="accepted", principal="po@h3t.co",
            project_dir=str(project),
        )
        assert item["independent_of_producer"] is True

    def test_a_judge_who_produced_the_candidate_is_not_independent(self, tmp_path):
        """And it is recorded, not refused (#399)."""
        project = _project(tmp_path)
        _green_se_qe(project, "US-400", principal="po@h3t.co")
        story = sp._find_story(_awaiting(), "US-400")
        item = sp.record_judged_verdict(
            story, "US-400", verdict="accepted", principal="PO@h3t.co",
            project_dir=str(project),
        )
        assert item["independent_of_producer"] is False
        assert item["evidence_class"] == "judged"
