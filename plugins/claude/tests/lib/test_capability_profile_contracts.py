"""Layer 1 -- the capability-profile pilot's vocabulary in
`runtime_contracts` (#399, Epic #410, capability-profile-pilot.md 3.1 / 3.3).

This is the keystone the epic's lanes fan out against, so these tests pin
the properties every other lane is allowed to assume:

1. the role-alias surface is bidirectional and total over the nine legacy
   names, both spellings, and it is built ON the DISPATCH_* projection
   tables, never a second copy of them,
2. an unknown name is refused with a message that names both accepted
   spellings (actionable, not just "invalid"),
3. `accountable_role` is soft-required: validated against the alias table
   WHEN PRESENT, absence is never a validator failure,
4. every evidence class carries its own integrity discipline, and
   `independent_of_producer: false` is recorded, not refused,
5. `criteria_gap_declared` cannot be declared without naming the missing
   evidence shape (a gap with no description is a silent pass),
6. every pilot-2 fixture under core/runtime-fixtures/ validates, and the
   alias projection fixture matches the module's tables exactly, both ways.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import advance_kernel
import runtime_contracts as rc


pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[3] / "core" / "runtime-fixtures"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


class TestRoleAliasSurface:
    def test_round_trip_of_all_nine_abbrevs(self):
        for abbrev in rc.ROLE_NAMES:
            pair = rc.profile_pair_for_role(abbrev)
            assert abbrev in rc.roles_for_profile_pair(*pair), abbrev

    def test_both_spellings_project_onto_the_same_pair(self):
        for abbrev, name in rc.ROLE_NAMES.items():
            assert rc.profile_pair_for_role(abbrev) == rc.profile_pair_for_role(name), (
                abbrev,
                name,
            )

    def test_the_pair_is_stage_then_capability(self):
        """Callers destructure positionally, so the order is contract."""
        assert rc.profile_pair_for_role("se") == ("producing", "producer")
        assert rc.profile_pair_for_role("quality-engineer") == ("verifying", "prover")
        assert rc.profile_pair_for_role("ra") == ("analysing", "analyst")

    def test_the_surface_is_built_on_the_projection_tables(self):
        """One table, both ways: the alias surface must agree with the
        DISPATCH_* tables for every role, so it cannot become a divergent
        second copy."""
        for abbrev in rc.DISPATCH_CAPABILITY_PROFILE:
            assert rc.profile_pair_for_role(abbrev) == (
                rc.DISPATCH_STAGE_PROFILE[abbrev],
                rc.DISPATCH_CAPABILITY_PROFILE[abbrev],
            )

    def test_inverse_projection_partitions_the_nine_roles(self):
        """Every role appears under exactly one pair, so the inverse is a
        partition, not a lossy summary."""
        seen = []
        for stage in rc.STAGE_PROFILES:
            for capability in rc.CAPABILITY_PROFILES:
                seen.extend(rc.roles_for_profile_pair(stage, capability))
        assert sorted(seen) == sorted(rc.ROLE_NAMES)

    def test_a_valid_but_unpopulated_pair_is_empty_not_an_error(self):
        assert rc.roles_for_profile_pair("admitting", "analyst") == ()

    def test_unknown_role_name_is_refused_with_both_spellings_named(self):
        with pytest.raises(ValueError) as excinfo:
            rc.profile_pair_for_role("product-manager")
        message = str(excinfo.value)
        assert "product-manager" in message
        assert "se" in message, "the message must name the accepted abbrevs"
        assert "software-engineer" in message, (
            "the message must name the accepted full names"
        )

    @pytest.mark.parametrize("bad", ["", None, "wizard", "SE "])
    def test_unknown_names_are_refused_not_guessed(self, bad):
        if bad == "SE ":
            # Case and whitespace are normalized, not refused: the kernel
            # already lowercases roles at its own boundary.
            assert rc.profile_pair_for_role(bad) == ("producing", "producer")
        else:
            with pytest.raises(ValueError):
                rc.profile_pair_for_role(bad)

    def test_unknown_profiles_are_refused_not_emptied(self):
        """A typo'd profile silently yielding () would read as "no roles
        here" instead of "that profile does not exist"."""
        with pytest.raises(ValueError, match="stage_profile"):
            rc.roles_for_profile_pair("building", "producer")
        with pytest.raises(ValueError, match="capability_profile"):
            rc.roles_for_profile_pair("producing", "builder")

    def test_the_role_table_mirrors_the_kernel(self):
        """runtime_contracts is the dependency leaf, so it mirrors
        advance_kernel.ROLE_NAMES instead of importing the kernel. This is
        what stops the mirror from drifting."""
        assert rc.ROLE_NAMES == advance_kernel.ROLE_NAMES


class TestAccountableRole:
    def _receipt(self, **overrides):
        base = {
            "attempt_id": "att_01J6ABCDEF",
            "dispatch_id": "e" * 32,
            "adapter_profile_id": "claude-local-v1",
            "placement": "local",
            "fencing_token": "b" * 32,
            "source_revision": "c" * 40,
        }
        base.update(overrides)
        return base

    def test_absence_is_never_a_validator_failure(self):
        """Soft-required during the migration window (the #342 overlay
        pattern): absence is a kernel-side warning, not a refusal here."""
        assert rc.validate_receipt_attempt_binding(self._receipt()) == []

    @pytest.mark.parametrize("value", ["qe", "quality-engineer"])
    def test_both_spellings_validate_when_present(self, value):
        receipt = self._receipt(accountable_role=value)
        assert rc.validate_receipt_attempt_binding(receipt) == []

    def test_an_unknown_value_is_refused_with_the_table_named(self):
        problems = rc.validate_receipt_attempt_binding(
            self._receipt(accountable_role="product-manager")
        )
        assert len(problems) == 1
        assert problems[0].startswith("accountable_role:")
        assert "product-manager" in problems[0]
        assert "soft-required" in problems[0]

    def test_a_non_string_value_is_refused(self):
        problems = rc.validate_receipt_attempt_binding(
            self._receipt(accountable_role=["qe"])
        )
        assert any(p.startswith("accountable_role:") for p in problems)

    def test_the_envelope_carries_the_same_vocabulary(self):
        env = dict(
            attempt_id="att_01J6ABCDEF",
            dispatch_id="a" * 32,
            project_id="taskflow",
            cycle_id="003-9f2c41ab",
            manifest_hash="sha256:" + "b" * 64,
            workstream_id="api",
            story_id="WU-142",
            stage="testing",
            role="qe",
            adapter_profile_id="codex-local-v1",
            runtime_family="codex",
            placement="local",
            source_revision="c" * 40,
            receipt_path="receipts/WU-142-qe.json",
            expires_at="2026-09-01T13:00:00Z",
            fencing_token="tok_01J6ABC",
        )
        built = rc.build_envelope(**env)
        assert rc.validate_envelope(built) == []
        built[rc.ACCOUNTABLE_ROLE] = "quality-engineer"
        assert rc.validate_envelope(built) == []
        built[rc.ACCOUNTABLE_ROLE] = "wizard"
        assert any(
            p.startswith("accountable_role:") for p in rc.validate_envelope(built)
        )


class TestEvidenceClasses:
    def _judged(self, **overrides):
        base = {
            "evidence_class": "judged",
            "principal": "alice@h3t.co",
            "candidate_digest": "sha256:" + "d" * 64,
            "verdict": "accepted",
            "recorded_at": "2026-09-02T10:15:00Z",
            "independent_of_producer": True,
        }
        base.update(overrides)
        return base

    def test_the_vocabulary_is_the_proposal_triplet(self):
        assert rc.EVIDENCE_CLASSES == ("replayed", "attested", "judged")

    def test_a_bare_replayed_class_token_is_refused(self):
        """#403 CORRECTS #399, which pinned the opposite.

        The original assertion here was
        `validate_evidence_item({"evidence_class": "replayed"}) == []`:
        a replayed item needed nothing beyond its class, on the reasoning that
        the replay itself is verification_runner.py's job. That leaves a bare
        class token asserting that something reproduced while naming nothing
        that could be re-run, which is the same defect #432 fixed on the read
        side ("a bare {"evidence_class": "replayed"} credited every check that
        named replayed"). #403's negative-test list names it directly: a
        replayed claim whose command was never executed must be refused.

        The runner's semantics are untouched. What moved is the shape an item
        must have before a reader may credit it as replayed, and it is the
        executed-object shape the runner and `_evaluate_check` already demand.
        """
        problems = rc.validate_evidence_item({"evidence_class": "replayed"})
        assert any(p.startswith("command:") for p in problems)
        assert any(p.startswith("exit_code:") for p in problems)

    def test_a_declared_command_with_no_exit_code_was_never_executed(self):
        problems = rc.validate_evidence_item(
            {"evidence_class": "replayed", "command": "pytest -q"}
        )
        assert [p for p in problems if p.startswith("exit_code:")]
        assert not [p for p in problems if p.startswith("command:")]

    def test_a_replayed_item_naming_an_executed_command_validates(self):
        assert rc.validate_evidence_item(
            {"evidence_class": "replayed", "command": "pytest -q", "exit_code": 0}
        ) == []

    def test_a_nonzero_exit_code_is_still_well_formed_evidence(self):
        """Shape, not verdict. A command that failed is evidence that it
        failed, and refusing it here would push writers toward omitting the
        runs that did not go well."""
        assert rc.validate_evidence_item(
            {"evidence_class": "replayed", "command": "pytest -q", "exit_code": 1}
        ) == []

    def test_a_boolean_exit_code_is_not_an_exit_code(self):
        problems = rc.validate_evidence_item(
            {"evidence_class": "replayed", "command": "pytest -q", "exit_code": True}
        )
        assert any(p.startswith("exit_code:") for p in problems)

    def test_an_untyped_item_is_refused(self):
        problems = rc.validate_evidence_item({"finding": "looks fine"})
        assert len(problems) == 1
        assert problems[0].startswith("evidence_class:")
        assert "integrity discipline" in problems[0]

    def test_an_unknown_class_is_refused(self):
        problems = rc.validate_evidence_item({"evidence_class": "vibes"})
        assert any(p.startswith("evidence_class:") for p in problems)

    def test_not_an_object_is_refused(self):
        assert rc.validate_evidence_item("replayed") == ["evidence: not an object"]

    def test_an_attestation_must_be_bound_to_one_bounded_execution(self):
        unbound = {"evidence_class": "attested", "finding": "x"}
        problems = rc.validate_evidence_item(unbound)
        assert any(p.startswith("attempt_id:") for p in problems)
        assert any("bounded execution" in p for p in problems)
        bound = dict(unbound, attempt_id="att_01J6EVID02")
        assert rc.validate_evidence_item(bound) == []

    def test_an_attested_attempt_id_is_shape_checked(self):
        bad = {"evidence_class": "attested", "attempt_id": "US-142"}
        assert any(
            p.startswith("attempt_id:") for p in rc.validate_evidence_item(bad)
        )

    def test_a_judged_item_carries_the_full_discipline(self):
        assert rc.validate_evidence_item(self._judged()) == []

    @pytest.mark.parametrize(
        "field", ["principal", "candidate_digest", "verdict", "recorded_at"]
    )
    def test_each_judged_discipline_field_is_required_by_name(self, field):
        item = self._judged()
        del item[field]
        problems = rc.validate_evidence_item(item)
        assert any(p.startswith(field + ":") for p in problems), (field, problems)


class TestJudgedVerdict:
    def _verdict(self, **overrides):
        base = {
            "principal": "alice@h3t.co",
            "candidate_digest": "sha256:" + "e" * 64,
            "verdict": "rejected",
            "recorded_at": "2026-09-02T10:20:00Z",
            "independent_of_producer": True,
        }
        base.update(overrides)
        return base

    def test_a_well_formed_verdict_validates(self):
        assert rc.validate_judged_verdict(self._verdict()) == []

    def test_not_an_object_is_refused(self):
        assert rc.validate_judged_verdict(None) == ["judged verdict: not an object"]

    def test_the_verdict_vocabulary_is_closed(self):
        problems = rc.validate_judged_verdict(self._verdict(verdict="maybe"))
        assert any(p.startswith("verdict:") for p in problems)

    def test_the_principal_must_be_named(self):
        problems = rc.validate_judged_verdict(self._verdict(principal="   "))
        assert any(
            p.startswith("principal:") and "naming the human" in p for p in problems
        )

    def test_the_verdict_is_bound_to_an_exact_candidate(self):
        problems = rc.validate_judged_verdict(self._verdict(candidate_digest=""))
        assert any(
            p.startswith("candidate_digest:") and "exact candidate" in p
            for p in problems
        )

    @pytest.mark.parametrize(
        "bad", ["yesterday", "2026-09-02", "2026-09-02 10:20:00", 1725270000, None]
    )
    def test_recorded_at_must_be_an_iso_timestamp(self, bad):
        problems = rc.validate_judged_verdict(self._verdict(recorded_at=bad))
        assert any(
            p.startswith("recorded_at:") and "ISO-8601" in p for p in problems
        ), bad

    def test_recorded_at_accepts_offset_and_fractional_forms(self):
        for ok in ("2026-09-02T10:20:00Z", "2026-09-02T10:20:00.123Z", "2026-09-02T17:20:00+07:00"):
            assert rc.validate_judged_verdict(self._verdict(recorded_at=ok)) == [], ok

    def test_non_independence_is_recorded_not_refused(self):
        """`independent_of_producer: false` is a VALID value: the discipline
        requires independence to be recorded, and any refusal rule is a later
        policy decision, not vocabulary."""
        assert (
            rc.validate_judged_verdict(self._verdict(independent_of_producer=False))
            == []
        )

    @pytest.mark.parametrize("bad", [None, "true", 1, 0])
    def test_independence_must_be_a_real_boolean(self, bad):
        problems = rc.validate_judged_verdict(
            self._verdict(independent_of_producer=bad)
        )
        assert any(
            p.startswith("independent_of_producer:") and "recorded, never assumed" in p
            for p in problems
        ), bad


class TestDodCheckResult:
    def _result(self, **overrides):
        base = {"check_id": "tests-pass", "result": "pass"}
        base.update(overrides)
        return base

    def test_pass_and_fail_validate_without_detail(self):
        assert rc.validate_dod_check_result(self._result()) == []
        assert rc.validate_dod_check_result(self._result(result="fail")) == []

    def test_not_an_object_is_refused(self):
        assert rc.validate_dod_check_result([]) == ["dod check result: not an object"]

    def test_the_result_vocabulary_is_closed(self):
        problems = rc.validate_dod_check_result(self._result(result="skipped"))
        assert any(p.startswith("result:") for p in problems)

    def test_the_check_must_be_identified(self):
        problems = rc.validate_dod_check_result(self._result(check_id=""))
        assert any(p.startswith("check_id:") for p in problems)

    def test_a_gap_must_name_the_missing_evidence_shape(self):
        """A criteria gap with no description is a silent pass wearing a
        different label, which is exactly what the result exists to end."""
        for absent in ({}, {"detail": ""}, {"detail": "   "}):
            result = self._result(result="criteria_gap_declared", **absent)
            problems = rc.validate_dod_check_result(result)
            assert any(
                p.startswith("detail:") and "missing evidence shape" in p
                for p in problems
            ), absent

    def test_a_described_gap_validates(self):
        result = self._result(
            result="criteria_gap_declared",
            detail="no coverage baseline exists; a replayed coverage report at the prior revision is missing",
        )
        assert rc.validate_dod_check_result(result) == []

    def test_a_non_string_detail_is_refused(self):
        problems = rc.validate_dod_check_result(self._result(detail={"text": "x"}))
        assert any(p.startswith("detail:") for p in problems)


class TestPilot2Fixtures:
    """The fixtures are the interface the epic's lanes fan out against, so
    each one must validate under the helpers it exists to exercise."""

    def test_the_alias_fixture_matches_the_module_both_ways(self):
        rows = _load("role-profile-aliases.json")
        assert {row["role"]: row["agent"] for row in rows} == rc.ROLE_NAMES
        for row in rows:
            expected = (row["stage_profile"], row["capability_profile"])
            assert rc.profile_pair_for_role(row["role"]) == expected, row
            assert rc.profile_pair_for_role(row["agent"]) == expected, row
            assert row["role"] in rc.roles_for_profile_pair(*expected), row

    @pytest.mark.parametrize(
        "name,evidence_class",
        [
            ("receipt-evidence-replayed.json", "replayed"),
            ("receipt-evidence-attested.json", "attested"),
            ("receipt-evidence-judged.json", "judged"),
        ],
    )
    def test_each_evidence_class_has_a_valid_receipt_fixture(
        self, name, evidence_class
    ):
        receipt = _load(name)
        assert rc.validate_receipt_attempt_binding(receipt) == [], name
        items = receipt["evidence"]
        assert [item["evidence_class"] for item in items] == [evidence_class]
        for item in items:
            assert rc.validate_evidence_item(item) == [], (name, item)

    def test_the_receipt_fixtures_carry_a_valid_accountable_role(self):
        for name in (
            "receipt-evidence-replayed.json",
            "receipt-evidence-attested.json",
            "receipt-evidence-judged.json",
        ):
            receipt = _load(name)
            role = receipt[rc.ACCOUNTABLE_ROLE]
            assert receipt["role"] == role, (
                "the fixture models the wire-compatibility rule: agent stays "
                "on the wire and accountable_role rides alongside it"
            )
            assert rc.profile_pair_for_role(role) == (
                receipt["stage_profile"],
                receipt["capability_profile"],
            ), name

    def test_the_attested_fixture_is_bound_to_its_own_attempt(self):
        receipt = _load("receipt-evidence-attested.json")
        assert receipt["evidence"][0]["attempt_id"] == receipt["attempt_id"], (
            "the attestation is produced by this attempt, so the binding "
            "must point at the receipt's own bounded execution"
        )

    def test_the_judged_verdict_fixture_validates_standalone(self):
        verdict = _load("judged-verdict.json")
        assert rc.validate_judged_verdict(verdict) == []
        assert verdict["independent_of_producer"] is False, (
            "the fixture must model the recorded-not-refused case, so a "
            "consumer that refuses non-independence fails here rather than "
            "in a ceremony"
        )

    def test_the_criteria_gap_fixture_validates_and_names_the_gap(self):
        result = _load("dod-criteria-gap.json")
        assert rc.validate_dod_check_result(result) == []
        assert result["result"] == "criteria_gap_declared"
        assert result["detail"].strip()
