"""Layer 1 — the Checkpoint barrier and the integration transaction (#645).

Refusal-weighted. A barrier that goes green on a good candidate proves almost
nothing: every barrier does that, including the one that never looked. What is
worth testing is what it refuses -- a criterion nobody evaluated, a set that
matches by count and not by identity, a candidate carrying cut code, a trunk
that moved under a green verdict, a second promotion of the same Cycle, and a
close over a promotion that never landed.
"""

from __future__ import annotations

import pytest

import cycle_authority
import cycle_barrier as cb
import cycle_records as cr
import path_scope

pytestmark = pytest.mark.unit

LEAD = "alice@h3t.co"
QA = "bob@h3t.co"


def _unit(uid, scope, **over):
    unit = {
        "id": uid,
        "kind": "story",
        "acceptance_criteria": ["AC-1"],
        "path_scope": list(scope),
        "depends_on": [],
    }
    unit.update(over)
    return unit


def _declaration(**over):
    body = {
        "cycle_id": "007-9f2c41ab",
        "repository": "h3tech-ai/synaptory-v1",
        "trunk_ref": "refs/heads/dev",
        "baseline_ref": "baseline-3",
        "goal": "authentication",
        "engineering_lead": LEAD,
        "crew": ["agent:se-01"],
        "source_region": ["api/"],
        "barrier_criteria": list(cr.BARRIER_CRITERIA),
        "admitted_units": [
            _unit("WU-01", ["api/routers/"]),
            _unit("WU-02", ["api/models/"]),
        ],
        "shared_path_owners": [],
    }
    body.update(over)
    return cr.seal(body)


def _unit_result(passed=True, criteria=("AC-1",), accepted_by=QA, **over):
    result = {
        "schema_version": cb.RESULT_SCHEMA_VERSION,
        "acceptance_criteria": {c: {"passed": passed} for c in criteria},
    }
    if accepted_by:
        result["acceptance"] = {
            "accepted_by": accepted_by,
            "accepted_at": "2026-09-08T10:00:00Z",
        }
    result.update(over)
    return result


def _trunk(promoted=False, observed="t0", current=None, candidate="c1", ref="refs/heads/dev"):
    return {
        "trunk_ref": ref,
        "observed_sha": observed,
        "current_sha": current if current is not None else observed,
        "candidate_sha": candidate,
        "candidate_is_ancestor_of_trunk": promoted,
    }


def _evaluate(declaration=None, *, unit_results=None, cuts=(), trunk=None,
              proofs=None, changed_paths=(), promoted=True, opened_at="2026-09-01T00:00:00Z"):
    declaration = declaration or _declaration()
    if unit_results is None:
        unit_results = {
            uid: _unit_result()
            for uid in [u["id"] for u in declaration["admitted_units"]]
        }
    return cb.evaluate(
        declaration=declaration,
        unit_results=unit_results,
        cuts=cuts,
        proofs=proofs if proofs is not None else {"regression": {"passed": True}},
        trunk=trunk if trunk is not None else _trunk(promoted=promoted),
        changed_paths=changed_paths,
        opened_at=opened_at,
    )


# ── The criteria are published, not restated ─────────────────────────────────


def test_the_criteria_are_the_records_list_and_not_a_second_copy():
    """Three different Sync-criteria counts shipped in one product because the
    list was restated in a prompt, a renderer and two hosts."""
    assert cb.CRITERIA is cr.BARRIER_CRITERIA


def test_an_unsealed_declaration_publishes_nothing():
    """A tampered declaration's criteria are not the criteria the Cycle sized
    against, so reading them would certify the tamper."""
    declaration = dict(_declaration())
    declaration["goal"] = "something else"
    with pytest.raises(cb.BarrierError):
        cb.publish_criteria(declaration)


def test_a_declaration_that_dropped_a_method_criterion_is_refused():
    """A project may add a criterion or raise a threshold; removing one the
    method declares must not be a way to pass."""
    sealed = _declaration()
    tampered = dict(sealed)
    tampered["barrier_criteria"] = [
        c for c in cr.BARRIER_CRITERIA if c != "trunk_integrated"
    ]
    tampered.pop("declaration_hash", None)
    tampered["declaration_hash"] = cr.compute_hash(tampered)
    with pytest.raises(cb.BarrierError) as excinfo:
        cb.publish_criteria(tampered)
    assert "trunk_integrated" in str(excinfo.value)


# ── Fails closed ─────────────────────────────────────────────────────────────


def test_criteria_all_returned_separates_evaluated_nothing_from_passed():
    """Without this criterion, `the barrier evaluated nothing` and `the barrier
    evaluated everything and passed` are both an empty list of failures."""
    assert "criteria_all_returned" in cr.BARRIER_CRITERIA
    verdict = _evaluate()
    assert verdict["criteria"]["criteria_all_returned"]["passed"] is True


def test_a_published_criterion_with_no_evaluator_is_unmet_not_skipped():
    """Adding a criterion the barrier cannot evaluate must not be cheaper than
    meeting it."""
    base = _declaration()
    extended = dict(base)
    extended["barrier_criteria"] = list(cr.BARRIER_CRITERIA) + ["project_smoke_green"]
    extended.pop("declaration_hash", None)
    extended["declaration_hash"] = cr.compute_hash(extended)
    verdict = _evaluate(extended)
    assert verdict["green"] is False
    assert "project_smoke_green" in verdict["unmet"]
    assert verdict["criteria"]["criteria_all_returned"]["passed"] is False, (
        "an unevaluated criterion must also make the meta-criterion fail"
    )


def test_an_errored_criterion_counts_as_unmet(monkeypatch):
    """An exception inside a check is not a pass, and must not take the barrier
    down either -- a crashed evaluator that raised out of `evaluate` would leave
    the caller with no verdict at all to act on."""
    def boom(_ctx):
        raise RuntimeError("regression runner vanished")

    monkeypatch.setitem(cb._EVALUATORS, "regression_green", boom)
    verdict = _evaluate()
    assert verdict["criteria"]["regression_green"]["errored"] is True
    assert verdict["criteria"]["regression_green"]["passed"] is False
    assert "regression_green" in verdict["criteria"]["criteria_all_returned"]["errored_criteria"]


def test_a_truthy_result_is_not_a_pass(monkeypatch):
    """`1` and `"yes"` are what a caller produces by accident; treating them as
    a pass is how an unevaluated criterion becomes a met one."""
    monkeypatch.setitem(
        cb._EVALUATORS, "regression_green", lambda _ctx: {"passed": 1}
    )
    verdict = _evaluate()
    assert verdict["criteria"]["regression_green"]["passed"] is False
    assert "not the boolean True" in verdict["criteria"]["regression_green"]["detail"]


def test_a_non_mapping_result_is_unmet_and_reported_as_not_returned(monkeypatch):
    monkeypatch.setitem(cb._EVALUATORS, "regression_green", lambda _ctx: "green")
    verdict = _evaluate()
    assert verdict["criteria"]["regression_green"]["returned"] is False
    assert "regression_green" in verdict["unmet"]


# ── Closure by identity, never by count ──────────────────────────────────────


def test_the_same_number_of_units_with_different_identities_fails_closure():
    """Three lanes each declaring `11 admitted` satisfied the old barrier even
    when they had admitted three different sets of eleven (#303)."""
    verdict = _evaluate(unit_results={
        "WU-01": _unit_result(),
        "WU-99": _unit_result(),
    })
    closed = verdict["criteria"]["admitted_set_closed"]
    assert closed["passed"] is False
    assert closed["missing"] == ["WU-02"]
    assert closed["extra"] == ["WU-99"]


def test_a_missing_unit_result_blocks_the_whole_candidate():
    verdict = _evaluate(unit_results={"WU-01": _unit_result()})
    assert verdict["green"] is False
    assert "admitted_set_closed" in verdict["unmet"]


def test_one_failing_retained_unit_promotes_nothing():
    """All-or-nothing at the level the work is judged: there is no per-unit
    promotion to fall back to."""
    verdict = _evaluate(unit_results={
        "WU-01": _unit_result(),
        "WU-02": _unit_result(passed=False),
    })
    assert "acceptance_criteria_met" in verdict["unmet"]
    with pytest.raises(cb.BarrierError) as excinfo:
        cb.promote(verdict=verdict, ledger=[], principal=LEAD)
    assert "All or nothing" in str(excinfo.value)


def test_a_unit_that_reported_done_while_proving_nothing_is_unmet():
    """An absent criterion result is unmet, so `done` with no evidence blocks
    the barrier instead of sliding through it."""
    verdict = _evaluate(unit_results={
        "WU-01": _unit_result(),
        "WU-02": {"schema_version": cb.RESULT_SCHEMA_VERSION, "state": "done"},
    })
    assert "acceptance_criteria_met" in verdict["unmet"]


def test_a_unit_result_on_an_unknown_schema_is_unproven_not_passing():
    """The never-pass-on-legacy posture: treating absence of evidence as
    evidence is how a barrier quietly stops being one."""
    verdict = _evaluate(unit_results={
        "WU-01": _unit_result(),
        "WU-02": _unit_result(schema_version="0.9"),
    })
    unmet = verdict["criteria"]["acceptance_criteria_met"]["unmet"]
    assert any("schema 0.9" in u for u in unmet), unmet


# ── Path scope and shared ownership, re-derived at the barrier ───────────────


def test_a_declaration_sealed_under_another_path_grammar_is_refused():
    """A scope re-read under different rules is a different scope."""
    sealed = dict(_declaration())
    sealed["path_grammar"] = "99"
    sealed.pop("declaration_hash", None)
    sealed["declaration_hash"] = cr.compute_hash(sealed)
    verdict = _evaluate(sealed)
    assert verdict["criteria"]["path_scopes_disjoint"]["code"] == "grammar_mismatch"
    assert verdict["green"] is False


def test_cutting_the_owner_of_a_shared_path_leaves_it_unowned():
    """A consequence the declaration cannot foresee: with no owner in the
    effective set, whoever merges last decides what the file says."""
    declaration = _declaration(
        admitted_units=[
            _unit("WU-01", ["api/routers/"]),
            _unit("WU-02", ["web/lib/flags.ts"]),
        ],
        shared_path_owners=[{"path": "web/lib/flags.ts", "owning_unit_id": "WU-02"}],
    )
    cut = cb.record_cut(
        declaration=declaration, unit_id="WU-02", reason="descoped by the client",
        principal=LEAD, unit_state="in_progress",
        baseline_digest_before="b3", baseline_digest_after="b3",
    )
    verdict = _evaluate(
        declaration, unit_results={"WU-01": _unit_result()}, cuts=[cut]
    )
    owned = verdict["criteria"]["shared_paths_owned"]
    assert owned["passed"] is False
    assert any("no longer in the effective set" in p for p in owned["problems"])


# ── Cutting ──────────────────────────────────────────────────────────────────


def test_a_cut_needs_an_explicit_reason():
    """The predecessor supplied a default one, so every cut it recorded says the
    same thing and none explains anything."""
    with pytest.raises(cb.BarrierError) as excinfo:
        cb.record_cut(
            declaration=_declaration(), unit_id="WU-01", reason="   ",
            principal=LEAD, unit_state="queued",
            baseline_digest_before="b3", baseline_digest_after="b3",
        )
    assert "explicit reason" in str(excinfo.value)


@pytest.mark.parametrize("state", ["done", "accepted"])
def test_cutting_finished_work_is_refused(state):
    """A cut is the valve for UNFINISHED work; cutting a verified result would
    make the Cycle's throughput unreadable."""
    with pytest.raises(cb.BarrierError) as excinfo:
        cb.record_cut(
            declaration=_declaration(), unit_id="WU-01", reason="out of time",
            principal=LEAD, unit_state=state,
            baseline_digest_before="b3", baseline_digest_after="b3",
        )
    assert "UNFINISHED" in str(excinfo.value)


def test_cutting_a_unit_this_cycle_never_admitted_is_refused():
    with pytest.raises(cb.BarrierError):
        cb.record_cut(
            declaration=_declaration(), unit_id="WU-77", reason="out of time",
            principal=LEAD, unit_state="queued",
            baseline_digest_before="b3", baseline_digest_after="b3",
        )


def test_a_cut_that_moved_the_baseline_is_refused():
    """Different records, authorities and invariants: naming a re-baseline as a
    cut is how a commitment quietly stops being one."""
    with pytest.raises(cb.BarrierError) as excinfo:
        cb.record_cut(
            declaration=_declaration(), unit_id="WU-01", reason="out of time",
            principal=LEAD, unit_state="queued",
            baseline_digest_before="b3", baseline_digest_after="b4",
        )
    assert "re-baseline" in str(excinfo.value)


def test_a_cut_returns_the_unit_to_the_backlog_with_a_nameable_link():
    cut = cb.record_cut(
        declaration=_declaration(), unit_id="WU-01", reason="out of time",
        principal=LEAD, unit_state="in_progress",
        baseline_digest_before="b3", baseline_digest_after="b3",
    )
    assert cut["backlog_ref"] == "backlog:WU-01"
    assert cut["readmission_key"] == "cut:007-9f2c41ab:WU-01"
    assert cut["decision"]["role"] == "engineering-lead"


def test_the_cut_decision_refuses_an_actor_cutting_its_own_work():
    """`C-11`: no actor approves its own work, and a cut is a decision about
    somebody's output."""
    with pytest.raises(cycle_authority.AuthorityError):
        cb.record_cut(
            declaration=_declaration(), unit_id="WU-01", reason="not viable",
            principal=LEAD, unit_state="in_progress", produced_by=LEAD,
            baseline_digest_before="b3", baseline_digest_after="b3",
        )


def test_a_readmission_citing_a_key_nobody_issued_links_to_nothing():
    """Without the link, `cut and re-admitted` and `cut and forgotten` are the
    same record."""
    cut = cb.record_cut(
        declaration=_declaration(), unit_id="WU-01", reason="out of time",
        principal=LEAD, unit_state="queued",
        baseline_digest_before="b3", baseline_digest_after="b3",
    )
    later = _declaration(cycle_id="008-1a2b3c4d")
    with pytest.raises(cb.BarrierError):
        cb.link_readmission(
            cut=cut, readmission_key="cut:whatever:WU-01",
            declaration=later, principal=LEAD, baseline_digest="b3",
        )


def test_a_readmission_that_does_not_admit_the_unit_is_refused():
    cut = cb.record_cut(
        declaration=_declaration(), unit_id="WU-01", reason="out of time",
        principal=LEAD, unit_state="queued",
        baseline_digest_before="b3", baseline_digest_after="b3",
    )
    later = _declaration(
        cycle_id="008-1a2b3c4d", admitted_units=[_unit("WU-09", ["web/"])]
    )
    with pytest.raises(cb.BarrierError) as excinfo:
        cb.link_readmission(
            cut=cut, readmission_key=cut["readmission_key"],
            declaration=later, principal=LEAD, baseline_digest="b3",
        )
    assert "does not admit" in str(excinfo.value)


def test_a_readmission_may_not_move_the_baseline_either():
    cut = cb.record_cut(
        declaration=_declaration(), unit_id="WU-01", reason="out of time",
        principal=LEAD, unit_state="queued",
        baseline_digest_before="b3", baseline_digest_after="b3",
    )
    later = _declaration(cycle_id="008-1a2b3c4d")
    with pytest.raises(cb.BarrierError):
        cb.link_readmission(
            cut=cut, readmission_key=cut["readmission_key"],
            declaration=later, principal=LEAD, baseline_digest="b4",
        )


def test_a_linked_readmission_leaves_the_cut_record_untouched():
    """The cut is immutable: a re-admission adds a fact rather than editing one."""
    cut = cb.record_cut(
        declaration=_declaration(), unit_id="WU-01", reason="out of time",
        principal=LEAD, unit_state="queued",
        baseline_digest_before="b3", baseline_digest_after="b3",
    )
    later = _declaration(cycle_id="008-1a2b3c4d")
    linked = cb.link_readmission(
        cut=cut, readmission_key=cut["readmission_key"],
        declaration=later, principal=LEAD, baseline_digest="b3",
    )
    assert cut["readmitted"] is None
    assert linked["readmitted"]["cycle_id"] == "008-1a2b3c4d"


def test_already_produced_code_for_a_cut_unit_is_excluded_from_the_candidate():
    """§4.3 step 3 says INCLUDING partial code already produced, which is the
    half a state change to `cancelled` cannot do."""
    declaration = _declaration()
    cut = cb.record_cut(
        declaration=declaration, unit_id="WU-02", reason="out of time",
        principal=LEAD, unit_state="in_progress",
        baseline_digest_before="b3", baseline_digest_after="b3",
    )
    verdict = _evaluate(
        declaration,
        unit_results={"WU-01": _unit_result()},
        cuts=[cut],
        changed_paths=["api/routers/auth.py", "api/models/user.py"],
    )
    closed = verdict["criteria"]["admitted_set_closed"]
    assert closed["cut_paths_in_candidate"] == ["api/models/user.py"]
    assert closed["passed"] is False


def test_a_path_a_retained_unit_also_owns_is_not_cut_code():
    """Over-refusing here would make a cut block work the retained set answers
    for, which is the opposite of what cutting is for."""
    declaration = _declaration(admitted_units=[
        _unit("WU-01", ["api/"], execution_order=1),
        _unit("WU-02", ["api/models/"], execution_order=2),
    ])
    cut = cb.record_cut(
        declaration=declaration, unit_id="WU-02", reason="out of time",
        principal=LEAD, unit_state="queued",
        baseline_digest_before="b3", baseline_digest_after="b3",
    )
    verdict = _evaluate(
        declaration, unit_results={"WU-01": _unit_result()}, cuts=[cut],
        changed_paths=["api/models/user.py"],
    )
    assert verdict["criteria"]["admitted_set_closed"]["cut_paths_in_candidate"] == []


def test_cutting_every_unit_leaves_a_vacuous_barrier_and_is_refused():
    declaration = _declaration()
    cuts = [
        cb.record_cut(
            declaration=declaration, unit_id=uid, reason="descoped",
            principal=LEAD, unit_state="queued",
            baseline_digest_before="b3", baseline_digest_after="b3",
        )
        for uid in ("WU-01", "WU-02")
    ]
    with pytest.raises(cb.BarrierError) as excinfo:
        cb.effective_set(declaration, cuts)
    assert "vacuously" in str(excinfo.value)


def test_a_cut_from_another_declaration_cannot_shrink_this_barriers_subject():
    declaration = _declaration()
    foreign = cb.record_cut(
        declaration=_declaration(cycle_id="008-1a2b3c4d"), unit_id="WU-01",
        reason="descoped", principal=LEAD, unit_state="queued",
        baseline_digest_before="b3", baseline_digest_after="b3",
    )
    with pytest.raises(cb.BarrierError) as excinfo:
        cb.effective_set(declaration, [foreign])
    assert "bound to the declaration" in str(excinfo.value)


def test_the_original_admitted_set_survives_every_cut():
    """The cut-rate denominator does not shrink, which needs the original set to
    stay on the record the metric reads."""
    declaration = _declaration()
    cut = cb.record_cut(
        declaration=declaration, unit_id="WU-02", reason="descoped",
        principal=LEAD, unit_state="queued",
        baseline_digest_before="b3", baseline_digest_after="b3",
    )
    verdict = _evaluate(
        declaration, unit_results={"WU-01": _unit_result()}, cuts=[cut]
    )
    assert verdict["admitted_unit_ids"] == ["WU-01", "WU-02"]
    assert verdict["effective_unit_ids"] == ["WU-01"]
    assert verdict["cut_unit_ids"] == ["WU-02"]


# ── Trunk integration ───────────────────────────────────────────────────────


def test_a_candidate_that_is_not_on_the_trunk_cannot_close():
    """The old barrier verified ancestry into a Cycle-scoped integration branch
    and left promotion to `dev` as `a human PR`, so a Cycle closed having
    integrated nothing anyone else could see."""
    verdict = _evaluate(promoted=False)
    assert verdict["criteria"]["trunk_integrated"]["code"] == "not_promoted"
    with pytest.raises(cb.BarrierError):
        cb.close(verdict=verdict, ledger=[], principal=LEAD)


def test_integration_into_a_different_ref_is_not_integration():
    verdict = _evaluate(trunk=_trunk(promoted=True, ref="refs/heads/cycle/007/integration"))
    assert verdict["criteria"]["trunk_integrated"]["code"] == "wrong_trunk"


def test_an_absent_trunk_observation_leaves_integration_unproven():
    verdict = _evaluate(trunk={})
    assert verdict["criteria"]["trunk_integrated"]["passed"] is False


def test_a_moved_trunk_forces_rebuild_rather_than_reusing_a_green_verdict():
    """Disjoint paths do not eliminate behavioural integration failures, so a
    verdict bound to one trunk revision is not evidence about another."""
    verdict = _evaluate(trunk=_trunk(promoted=False, observed="t0", current="t1"))
    with pytest.raises(cb.BarrierError) as excinfo:
        cb.promote(verdict=verdict, ledger=[], principal=LEAD)
    assert "trunk moved" in str(excinfo.value)


def test_promotion_needs_both_trunk_revisions_to_see_a_move_at_all():
    verdict = _evaluate(trunk={
        "trunk_ref": "refs/heads/dev", "candidate_sha": "c1",
        "candidate_is_ancestor_of_trunk": False,
    })
    with pytest.raises(cb.BarrierError) as excinfo:
        cb.promote(verdict=verdict, ledger=[], principal=LEAD)
    assert "unobservable" in str(excinfo.value)


def test_promotion_is_high_risk_and_never_continues_automatically():
    verdict = _evaluate(promoted=False)
    with pytest.raises(cycle_authority.AuthorityError):
        cb.promote(
            verdict=verdict, ledger=[],
            principal=cycle_authority.AUTOMATED_PRINCIPAL,
        )


def test_integration_after_the_barrier_closed_is_refused():
    """`No closed Cycle can publish additional code later.`"""
    verdict = _evaluate(promoted=True)
    promotion = cb.promote(verdict=verdict, ledger=[], principal=LEAD)
    closed = cb.close(verdict=verdict, ledger=[promotion], principal=LEAD)
    with pytest.raises(cb.BarrierError) as excinfo:
        cb.promote(verdict=verdict, ledger=[promotion, closed], principal=LEAD)
    assert "already closed" in str(excinfo.value)


def test_promotion_is_idempotent_under_the_same_operation_identity():
    """A crash between promote and close-record must reconcile the same
    operation rather than merge the candidate twice."""
    verdict = _evaluate(promoted=True)
    first = cb.promote(verdict=verdict, ledger=[], principal=LEAD)
    again = cb.promote(verdict=verdict, ledger=[first], principal=LEAD)
    assert again["reconciled"] is True
    assert again["operation_id"] == first["operation_id"]
    assert again["promoted_at"] == first["promoted_at"], (
        "a reconciled promotion must return the recorded one, not a new one"
    )


def test_the_operation_identity_is_derived_and_not_generated():
    """A generated id would make every retry a different operation, which is how
    a candidate gets merged twice."""
    verdict = _evaluate(promoted=True)
    again = _evaluate(promoted=True)
    assert verdict["operation_id"] == again["operation_id"]


def test_the_operation_identity_changes_with_the_candidate():
    a = _evaluate(promoted=True, trunk=_trunk(promoted=True, candidate="c1"))
    b = _evaluate(promoted=True, trunk=_trunk(promoted=True, candidate="c2"))
    assert a["operation_id"] != b["operation_id"]


def test_a_second_promotion_of_a_different_candidate_is_refused():
    first = cb.promote(
        verdict=_evaluate(promoted=True, trunk=_trunk(promoted=True, candidate="c1")),
        ledger=[], principal=LEAD,
    )
    second = _evaluate(promoted=True, trunk=_trunk(promoted=True, candidate="c2"))
    with pytest.raises(cb.BarrierError) as excinfo:
        cb.promote(verdict=second, ledger=[first], principal=LEAD)
    assert "already promoted" in str(excinfo.value)


# ── The close ────────────────────────────────────────────────────────────────


def test_a_close_without_a_recorded_promotion_is_refused():
    """If promotion fails, no successful Checkpoint or close is recorded."""
    verdict = _evaluate(promoted=True)
    with pytest.raises(cb.BarrierError) as excinfo:
        cb.close(verdict=verdict, ledger=[], principal=LEAD)
    assert "no successful promotion" in str(excinfo.value)


def test_a_failed_promotion_is_not_the_promotion_a_close_needs():
    verdict = _evaluate(promoted=True)
    failure = cb.record_promotion_failure(verdict=verdict, reason="merge conflict on dev")
    with pytest.raises(cb.BarrierError):
        cb.close(verdict=verdict, ledger=[failure], principal=LEAD)


def test_a_failed_promotion_needs_a_reason():
    with pytest.raises(cb.BarrierError):
        cb.record_promotion_failure(verdict=_evaluate(promoted=True), reason="")


def test_a_close_records_the_integrated_revision_and_the_cycle_history():
    verdict = _evaluate(promoted=True)
    promotion = cb.promote(verdict=verdict, ledger=[], principal=LEAD)
    record = cb.close(verdict=verdict, ledger=[promotion], principal=LEAD)
    assert record["integrated_sha"] == "c1"
    assert record["trunk_ref"] == "refs/heads/dev"
    history = record["history"]
    assert history["admitted_count_at_commit"] == 2
    assert sorted(u["unit_id"] for u in history["accepted_units"]) == ["WU-01", "WU-02"]


def test_a_second_close_reconciles_rather_than_stranding_a_promoted_cycle():
    verdict = _evaluate(promoted=True)
    promotion = cb.promote(verdict=verdict, ledger=[], principal=LEAD)
    first = cb.close(verdict=verdict, ledger=[promotion], principal=LEAD)
    again = cb.close(verdict=verdict, ledger=[promotion, first], principal=LEAD)
    assert again["reconciled"] is True
    assert again["closed_at"] == first["closed_at"]


# ── Acceptance credit is read, never invented ────────────────────────────────


def test_an_agent_reporting_done_credits_no_accepted_throughput():
    """`SPQ-R16`: a change counts as delivered only once an accountable human has
    accepted it as verified."""
    verdict = _evaluate(unit_results={
        "WU-01": _unit_result(accepted_by=None),
        "WU-02": _unit_result(accepted_by=None),
    })
    assert verdict["green"] is True, verdict["unmet"]
    assert verdict["accepted_units"] == []


def test_the_automated_principal_cannot_be_the_accepting_human():
    verdict = _evaluate(unit_results={
        "WU-01": _unit_result(accepted_by=cycle_authority.AUTOMATED_PRINCIPAL),
        "WU-02": _unit_result(accepted_by=QA),
    })
    assert [u["unit_id"] for u in verdict["accepted_units"]] == ["WU-02"]


def test_acceptance_credit_retains_the_timestamp_and_the_integration_revision():
    """§5.4 requires both, because a credit without them cannot be placed in an
    observation window or tied to what shipped."""
    verdict = _evaluate(promoted=True)
    entry = verdict["accepted_units"][0]
    assert entry["accepted_at"] == "2026-09-08T10:00:00Z"
    assert entry["integrated_sha"] == "c1"


# ── The regression proof ─────────────────────────────────────────────────────


def test_a_skipped_regression_is_not_a_passed_one():
    """`--no-regression cannot bypass require_regression` was a review finding
    on the old barrier; the shape of the mistake survives the move."""
    verdict = _evaluate(proofs={"regression": {"skipped": True, "reason": "no script"}})
    assert verdict["criteria"]["regression_green"]["passed"] is False


def test_an_absent_regression_proof_is_unmet():
    verdict = _evaluate(proofs={})
    assert "regression_green" in verdict["unmet"]


# ── Reconciliation ──────────────────────────────────────────────────────────


def test_reconcile_after_a_crash_before_promotion_says_promote():
    verdict = _evaluate(promoted=False)
    assert cb.reconcile(verdict=verdict, ledger=[])["action"] == "promote"


def test_reconcile_after_a_crash_between_promote_and_close_says_close():
    """The case the derived operation identity exists for."""
    verdict = _evaluate(promoted=True)
    promotion = cb.promote(verdict=verdict, ledger=[], principal=LEAD)
    assert cb.reconcile(verdict=verdict, ledger=[promotion])["action"] == "close"


def test_reconcile_after_the_close_says_complete_and_never_promote_again():
    verdict = _evaluate(promoted=True)
    promotion = cb.promote(verdict=verdict, ledger=[], principal=LEAD)
    closed = cb.close(verdict=verdict, ledger=[promotion], principal=LEAD)
    assert cb.reconcile(verdict=verdict, ledger=[promotion, closed])["action"] == "complete"


def test_a_promotion_recorded_but_not_landed_forces_a_rebuild():
    """The half a resumed process cannot know from its own memory: the record
    says promoted and the trunk says otherwise."""
    verdict = _evaluate(promoted=False)
    promotion = dict(
        cb.promote(
            verdict=_evaluate(promoted=True), ledger=[], principal=LEAD,
        )
    )
    assert cb.reconcile(verdict=verdict, ledger=[promotion])["action"] == "rebuild"


def test_reconcile_returns_only_closed_set_actions():
    verdict = _evaluate(promoted=False)
    assert cb.reconcile(verdict=verdict, ledger=[])["action"] in cb.RECONCILIATIONS


# ── The absences, proved rather than asserted ───────────────────────────────


def test_no_function_accepts_its_own_evidence_as_an_argument():
    """The lift of `declare_ready`'s refusal to be handed `dod`, `replay`,
    `work_units` or `shared_digests`."""
    cb.assert_no_caller_supplied_verdicts()


def test_no_function_accepts_a_partial_admission_mode():
    """The predecessor spelled partial admission as a config value naming the
    retired lane, so the refusal has to catch a setting rather than a branch."""
    cb.assert_no_partial_admission_mode()


def test_the_absence_guards_are_not_vacuous(monkeypatch):
    """A guard that would pass over an offending signature defends nothing, so
    both are exercised against one."""
    def offender(*, results=None, mode="all"):  # pragma: no cover - a probe
        return results, mode

    offender.__module__ = cb.__name__
    monkeypatch.setitem(cb.__dict__, "offender", offender)
    with pytest.raises(cb.BarrierError):
        cb.assert_no_caller_supplied_verdicts()
    with pytest.raises(cb.BarrierError):
        cb.assert_no_partial_admission_mode()


def test_the_grammar_the_barrier_reads_is_the_one_it_publishes():
    """A drifted grammar version would make every declaration a mismatch, so the
    pairing is asserted rather than assumed."""
    assert _declaration()["path_grammar"] == path_scope.GRAMMAR_VERSION


# ── #817 — a passing criterion must say what it ranged over ──────────────────


def test_the_closure_verdict_says_how_many_paths_it_ranged_over():
    """A verified path guarantee and a vacuous one must not read alike."""
    verdict = _evaluate(
        changed_paths=["api/routers/auth.py", "api/models/user.py"],
    )
    closed = verdict["criteria"]["admitted_set_closed"]

    assert closed["passed"] is True
    assert closed["changed_paths_observed"] == 2
    assert "2 changed paths" in closed["detail"]


def test_a_vacuous_path_lane_does_not_claim_the_guarantee():
    """#817: the detail asserted `every changed path is inside the
    declaration` having ranged over nothing."""
    verdict = _evaluate(changed_paths=[])
    closed = verdict["criteria"]["admitted_set_closed"]

    assert closed["passed"] is True
    assert closed["changed_paths_observed"] == 0
    # The identity lane DID run and may still be claimed.
    assert "by identity" in closed["detail"]
    # The two path lanes did not, and the verdict must not say otherwise.
    assert "NOT evaluated here" in closed["detail"]
    assert "every changed path is inside the declaration (" not in closed["detail"]


def test_an_integrated_candidate_is_named_as_the_reason_the_diff_is_empty():
    """The operator needs to know this is by construction, not a near miss."""
    verdict = _evaluate(trunk=_trunk(promoted=True), changed_paths=[])
    detail = verdict["criteria"]["admitted_set_closed"]["detail"]

    assert "already an ancestor of the trunk" in detail
    assert "PROMOTION" in detail


def test_integration_and_an_empty_diff_are_the_same_fact():
    """Why #817's stronger remedy is refused, pinned as an executable fact.

    `changed_paths` is `git diff merge-base(candidate, trunk)..candidate`, and
    `_eval_trunk_integrated` passes only when the candidate is an ancestor of
    the trunk -- at which point that merge-base IS the candidate and the diff
    is empty. So EVERY close has an empty diff. Returning a non-`True` verdict
    for a vacuous lane would put `admitted_set_closed` in `unmet` on every
    close and no Cycle could ever close again.
    """
    verdict = _evaluate(trunk=_trunk(promoted=True), changed_paths=[])

    assert verdict["criteria"]["trunk_integrated"]["passed"] is True
    assert verdict["criteria"]["admitted_set_closed"]["changed_paths_observed"] == 0
    # The barrier's own unmet rule is `passed is not True`, so a vacuous lane
    # reporting anything other than True would strand this Cycle.
    assert "admitted_set_closed" not in verdict["unmet"]
    assert verdict["unmet"] == []


def test_a_real_violation_still_refuses_when_paths_are_observed():
    """The honesty fix must not soften the lane it is honest about."""
    verdict = _evaluate(changed_paths=["docs/unclaimed.md"])
    closed = verdict["criteria"]["admitted_set_closed"]

    assert closed["passed"] is False
    assert closed["paths_outside_the_declaration"] == ["docs/unclaimed.md"]
    assert closed["changed_paths_observed"] == 1


# ── #814 — the same state before and after a promotion means opposite things ──


def test_before_a_promotion_a_non_ancestor_candidate_is_the_expected_state():
    verdict = _evaluate(trunk=_trunk(promoted=False, candidate="c1"))
    integrated = verdict["criteria"]["trunk_integrated"]

    assert integrated["passed"] is False
    assert integrated["code"] == "not_promoted"
    assert "Before promotion this is the correct state" in integrated["detail"]


def test_a_promoted_candidate_missing_from_the_trunk_names_squash_and_the_repair():
    """#814: the old message said `not_promoted` AFTER a successful promotion,
    and an engagement lost two days reading it as `you have not promoted`."""
    trunk = _trunk(promoted=False, candidate="b9c5c5417c89", current="c51a069deadbe")
    trunk["candidate_from"] = "promotion"
    integrated = _evaluate(trunk=trunk)["criteria"]["trunk_integrated"]

    assert integrated["passed"] is False
    assert integrated["code"] == "promoted_candidate_not_in_trunk"
    assert "not_promoted" != integrated["code"]
    # It must name what happened, the cause, and the repair.
    assert "AUTHORIZED by a promotion" in integrated["detail"]
    assert "squash or rebase" in integrated["detail"]
    assert "git merge -s ours b9c5c5417c89" in integrated["detail"]
    # And must NOT keep telling the operator this is the pre-promotion state.
    assert "Before promotion this is the correct state" not in integrated["detail"]


def test_an_integrated_promoted_candidate_still_passes():
    trunk = _trunk(promoted=True, candidate="c1")
    trunk["candidate_from"] = "promotion"
    assert _evaluate(trunk=trunk)["criteria"]["trunk_integrated"]["passed"] is True
