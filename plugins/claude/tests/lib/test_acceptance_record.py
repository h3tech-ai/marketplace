"""Layer 1 — repeatable Acceptance and the four change dispositions (#646).

Refusal-weighted. The positive case here is short and uninteresting -- a
package compiles, a decision is made -- while the value is in what cannot
happen: a release closing an engagement that still owes work, an approval from
last week standing for this week's candidate, a proof written for the release
rather than by the work, and a re-baseline wearing an absorb's name.
"""

from __future__ import annotations

import pytest

import acceptance_record as ar
import cycle_authority
import cycle_lifecycle

pytestmark = pytest.mark.unit

QA = "quinn@h3t.co"
LEAD = "erin@h3t.co"
OPENED = "2026-09-08T09:00:00Z"


def _close(cycle_id="007-9f2c41ab", sha="c1", **over):
    record = {
        "cycle_id": cycle_id,
        "integrated_sha": sha,
        "trunk_ref": "refs/heads/dev",
        "closed_at": "2026-09-07T00:00:00Z",
    }
    record.update(over)
    return record


def _evidence(rid="EV-1", recorded_at="2026-09-05T00:00:00Z", **over):
    record = {"id": rid, "recorded_at": recorded_at, "subject_digest": "sha256:abc"}
    record.update(over)
    return record


def _package(**over):
    kwargs = {
        "acceptance_id": "REL-2026-09-08",
        "trunk_digest": "sha256:trunk-1",
        "opened_at": OPENED,
        "cycle_closes": [_close()],
        "integrated_shas_on_trunk": ["c1"],
        "evidence": [_evidence()],
    }
    kwargs.update(over)
    return ar.compile_evidence(**kwargs)


def _handover(**over):
    handover = {item: "recorded" for item in ar.HANDOVER_ITEMS}
    handover.update(over)
    return handover


# ── The package ──────────────────────────────────────────────────────────────


def test_a_package_is_keyed_on_the_acceptance_identity_and_the_trunk_digest():
    """Independent of any one Cycle: a long engagement ships several times, and
    each go-live's package must be tellable from the last."""
    a = _package(acceptance_id="REL-A")
    b = _package(acceptance_id="REL-B")
    assert a["package_digest"] != b["package_digest"]


def test_the_same_release_from_a_different_trunk_is_a_different_package():
    """Otherwise a package could be re-presented for another candidate."""
    a = _package(trunk_digest="sha256:trunk-1")
    b = _package(trunk_digest="sha256:trunk-2")
    assert a["package_digest"] != b["package_digest"]


def test_an_acceptance_without_its_own_identity_is_refused():
    with pytest.raises(ar.AcceptanceError):
        _package(acceptance_id="")


def test_an_acceptance_that_pins_no_trunk_revision_is_refused():
    """An Acceptance evaluates a PINNED integrated release candidate; without
    one its approval is about whatever the trunk happens to be later."""
    with pytest.raises(ar.AcceptanceError):
        _package(trunk_digest="")


def test_an_unintegrated_candidate_is_refused():
    """The staging branch must never become the demonstrated final result, and a
    release compiled from one would be exactly that."""
    with pytest.raises(ar.AcceptanceError) as excinfo:
        _package(cycle_closes=[_close(sha="")])
    assert "UNINTEGRATED" in str(excinfo.value)


def test_a_cycle_whose_work_is_not_on_this_trunk_is_refused():
    with pytest.raises(ar.AcceptanceError) as excinfo:
        _package(cycle_closes=[_close(sha="c9")], integrated_shas_on_trunk=["c1"])
    assert "not observed on the trunk" in str(excinfo.value)


def test_a_release_may_compile_several_closed_cycles():
    """Nothing here requires a particular Cycle, count, or that no other Cycle
    is open -- which is what `a release does not wait for an unrelated open
    Cycle` means at this level."""
    package = _package(
        cycle_closes=[_close("007-a", "c1"), _close("008-b", "c2")],
        integrated_shas_on_trunk=["c1", "c2"],
    )
    assert [c["cycle_id"] for c in package["cycles"]] == ["007-a", "008-b"]


def test_a_release_citing_no_closed_cycle_has_nothing_to_ship():
    with pytest.raises(ar.AcceptanceError):
        _package(cycle_closes=[])


def test_a_retrospective_proof_is_refused():
    """§5.3 compiles the package from records the work already produced AS IT
    RAN; a proof produced afterwards proves the release, not the work."""
    with pytest.raises(ar.AcceptanceError) as excinfo:
        _package(evidence=[_evidence(recorded_at="2026-09-08T10:00:00Z")])
    assert "retrospective" in str(excinfo.value)


def test_evidence_that_cannot_be_dated_is_refused():
    """It is indistinguishable from evidence written for the release."""
    with pytest.raises(ar.AcceptanceError):
        _package(evidence=[_evidence(recorded_at="")])


def test_an_acceptance_with_no_open_time_cannot_date_its_evidence():
    with pytest.raises(ar.AcceptanceError):
        _package(opened_at="")


# ── The readiness decision ───────────────────────────────────────────────────


def test_release_readiness_is_quality_assurances_action_and_high_risk():
    """The role is derived from the action, never chosen by the caller."""
    decision = ar.readiness_decision(
        package=_package(), principal=QA, outcome="continue", rationale="all green",
    )
    assert decision["role"] == "quality-assurance"
    assert decision["risk"] == "high"


def test_a_release_never_continues_automatically():
    with pytest.raises(cycle_authority.AuthorityError):
        ar.readiness_decision(
            package=_package(),
            principal=cycle_authority.AUTOMATED_PRINCIPAL,
            outcome="continue",
        )


def test_the_producer_of_the_candidate_cannot_decide_its_release():
    with pytest.raises(cycle_authority.AuthorityError):
        ar.readiness_decision(
            package=_package(), principal=QA, outcome="continue", produced_by=QA,
        )


def test_a_readiness_decision_may_block_rather_than_pass():
    """There is deliberately no implicit pass; `block` is a decision somebody is
    named for, exactly like `continue`."""
    decision = ar.readiness_decision(
        package=_package(), principal=QA, outcome="block", rationale="open finding",
    )
    assert decision["outcome"] == "block"


def test_an_approval_reused_for_another_digest_is_refused():
    """A release slips, the candidate is rebuilt, and last week's approval is
    still sitting there looking like an approval. It approved a different
    revision."""
    old = ar.readiness_decision(
        package=_package(trunk_digest="sha256:trunk-1"), principal=QA,
        outcome="continue",
    )
    rebuilt = _package(trunk_digest="sha256:trunk-2")
    with pytest.raises(ar.AcceptanceError) as excinfo:
        ar.assert_decision_binds(old, package=rebuilt)
    assert "another digest" in str(excinfo.value)


def test_an_approval_from_another_go_live_is_refused():
    old = ar.readiness_decision(
        package=_package(acceptance_id="REL-A"), principal=QA, outcome="continue",
    )
    with pytest.raises(ar.AcceptanceError):
        ar.assert_decision_binds(old, package=_package(acceptance_id="REL-B"))


def test_a_decision_bound_to_its_own_package_is_accepted():
    package = _package()
    ar.assert_decision_binds(
        ar.readiness_decision(package=package, principal=QA, outcome="continue"),
        package=package,
    )


# ── Final versus non-final ───────────────────────────────────────────────────


def test_a_release_with_work_outstanding_returns_to_delivery():
    """`C-15`: release A, then continue Cycles, then release B."""
    assert ar.acceptance_target(
        outstanding_commitments=["EPIC-9"], handover=_handover()
    ) == "CYCLE"


def test_only_the_final_acceptance_closes_the_engagement():
    assert ar.acceptance_target(
        outstanding_commitments=[], handover=_handover()
    ) == "COMPLETE"


def test_a_forced_terminal_close_for_a_nonfinal_release_is_refused():
    """The predecessor's lifecycle made this the only reachable outcome, so the
    refusal is the change."""
    with pytest.raises(ar.AcceptanceError) as excinfo:
        ar.assert_close_permitted(
            requested_target="COMPLETE",
            outstanding_commitments=["EPIC-9"],
            handover=_handover(),
        )
    assert "EPIC-9" in str(excinfo.value)


def test_a_nonfinal_release_returning_to_delivery_is_permitted():
    ar.assert_close_permitted(
        requested_target="CYCLE", outstanding_commitments=["EPIC-9"],
        handover=_handover(),
    )


@pytest.mark.parametrize("missing", list(ar.HANDOVER_ITEMS))
def test_the_final_close_needs_all_three_handover_items(missing):
    """An engagement closed without the operating knowledge has handed over a
    codebase nobody can run, which is why this is an AND rather than a count."""
    handover = _handover(**{missing: ""})
    assert ar.handover_recorded(handover) is False
    with pytest.raises(ar.AcceptanceError) as excinfo:
        ar.assert_close_permitted(
            requested_target="COMPLETE", outstanding_commitments=[],
            handover=handover,
        )
    assert missing in str(excinfo.value)


def test_an_absent_handover_is_not_a_recorded_one():
    assert ar.handover_recorded(None) is False


def test_finality_is_decided_by_the_lifecycle_and_not_restated_here():
    """Two answers to `does this go-live end the engagement` is how a
    mid-engagement release closes one."""
    assert ar.acceptance_target(
        outstanding_commitments=[], handover=_handover()
    ) == cycle_lifecycle.acceptance_target(
        outstanding_commitments=[], handover_recorded=True
    )


# ── Change dispositions ─────────────────────────────────────────────────────


def _named(disposition, **over):
    kwargs = {
        "disposition": disposition,
        "principal": LEAD,
        "subject": "CHG-1",
        "rationale": "agreed with the client on the call",
        "baseline_digest_before": "b3",
        "baseline_digest_after": "b3",
    }
    kwargs.update(over)
    return ar.name_change(**kwargs)


def test_a_cut_is_not_a_change_disposition():
    """A cut removes unfinished work inside the baseline; a re-baseline moves
    the baseline. Naming one as the other is how a commitment quietly stops
    being one."""
    with pytest.raises(ar.AcceptanceError) as excinfo:
        _named("cut", severity="defect")
    assert "not a change disposition" in str(excinfo.value)


def test_an_unrecognised_disposition_is_refused_rather_than_defaulted():
    with pytest.raises(ar.AcceptanceError):
        _named("descope")


def test_every_disposition_is_the_engagement_leads_action():
    for disposition, extra in (
        ("correction", {"severity": "defect"}),
        ("absorb", {}),
        ("swap", {"sizing_basis": "3 points each, estimated at planning",
                  "scope_added": [{"unit_id": "WU-N", "size": 3}],
                  "scope_removed": [{"unit_id": "WU-O", "size": 3}]}),
        ("re-baseline", {"client_agreed_at": "2026-09-08T09:00:00Z",
                         "baseline_digest_after": "b4"}),
    ):
        record = _named(disposition, **extra)
        assert record["decision"]["role"] == "engagement-lead", disposition


def test_naming_a_change_without_a_rationale_is_refused():
    """The Engagement Lead carries the disposition to the client, and `it
    changed` is not a disposition."""
    with pytest.raises(ar.AcceptanceError):
        _named("absorb", rationale="")


def test_the_two_dispositions_that_move_commitment_are_high_risk():
    swap = _named(
        "swap", sizing_basis="points", scope_added=[{"unit_id": "A", "size": 2}],
        scope_removed=[{"unit_id": "B", "size": 5}],
    )
    rebaseline = _named(
        "re-baseline", client_agreed_at="2026-09-08T09:00:00Z",
        baseline_digest_after="b4",
    )
    assert swap["decision"]["risk"] == "high"
    assert rebaseline["decision"]["risk"] == "high"


# ── Correction ──────────────────────────────────────────────────────────────


def test_a_critical_correction_interrupts_rather_than_queueing():
    record = _named("correction", severity="production-down")
    assert record["details"]["routing"] == "interrupt"


def test_a_routine_correction_plans_into_an_upcoming_cycle():
    record = _named("correction", severity="defect")
    assert record["details"]["routing"] == "next-cycle"


def test_a_correction_without_a_severity_is_refused():
    """The severity is what decides whether a production outage waits for a
    Cycle, so it is refused rather than defaulted."""
    with pytest.raises(ar.AcceptanceError) as excinfo:
        _named("correction")
    assert "severity" in str(excinfo.value)


def test_a_correction_may_not_move_the_baseline():
    with pytest.raises(Exception) as excinfo:
        _named("correction", severity="defect", baseline_digest_after="b4")
    assert "re-baseline" in str(excinfo.value)


# ── Absorb ──────────────────────────────────────────────────────────────────


def test_an_absorb_holds_the_commitment():
    """A refinement inside the agreed intent, so the baseline is asserted
    unchanged rather than assumed to be."""
    record = _named("absorb")
    assert record["baseline_digest_before"] == record["baseline_digest_after"]


def test_an_absorb_that_moved_the_baseline_is_refused():
    """This is the exact mislabel §8's accountable naming exists to catch."""
    with pytest.raises(Exception):
        _named("absorb", baseline_digest_after="b4")


# ── Swap ────────────────────────────────────────────────────────────────────


def test_a_swap_without_a_recorded_sizing_basis_is_refused():
    """Without one, `equal or smaller` is an assertion rather than a comparison
    anyone can check afterwards."""
    with pytest.raises(ar.AcceptanceError) as excinfo:
        _named("swap", scope_added=[{"unit_id": "A", "size": 3}],
               scope_removed=[{"unit_id": "B", "size": 3}])
    assert "sizing basis" in str(excinfo.value)


def test_a_swap_for_larger_committed_work_is_refused():
    """Exchanging for larger work grows the commitment while calling it a swap,
    which is a re-baseline the client never agreed to."""
    with pytest.raises(ar.AcceptanceError) as excinfo:
        _named("swap", sizing_basis="points",
               scope_added=[{"unit_id": "A", "size": 8}],
               scope_removed=[{"unit_id": "B", "size": 3}])
    assert "EQUAL OR SMALLER" in str(excinfo.value)


def test_a_swap_for_equal_work_is_accepted():
    record = _named("swap", sizing_basis="points",
                    scope_added=[{"unit_id": "A", "size": 3}],
                    scope_removed=[{"unit_id": "B", "size": 3}])
    assert record["details"]["added_size"] == record["details"]["removed_size"]


def test_a_swap_needs_both_sides():
    """One side alone is an absorb or a cut."""
    with pytest.raises(ar.AcceptanceError):
        _named("swap", sizing_basis="points",
               scope_added=[{"unit_id": "A", "size": 3}], scope_removed=[])


def test_an_unsized_swap_side_makes_the_comparison_unfalsifiable():
    with pytest.raises(ar.AcceptanceError) as excinfo:
        _named("swap", sizing_basis="points",
               scope_added=[{"unit_id": "A"}],
               scope_removed=[{"unit_id": "B", "size": 3}])
    assert "unfalsifiable" in str(excinfo.value)


def test_a_zero_sized_swap_side_is_refused():
    with pytest.raises(ar.AcceptanceError):
        _named("swap", sizing_basis="points",
               scope_added=[{"unit_id": "A", "size": 0}],
               scope_removed=[{"unit_id": "B", "size": 3}])


# ── Re-baseline ─────────────────────────────────────────────────────────────


def test_rebaseline_work_started_before_client_agreement_is_refused():
    """Work already underway makes the negotiation a formality."""
    with pytest.raises(ar.AcceptanceError) as excinfo:
        _named("re-baseline", client_agreed_at="2026-09-08T09:00:00Z",
               work_started_at="2026-09-07T09:00:00Z",
               baseline_digest_after="b4")
    assert "cannot begin before client agreement" in str(excinfo.value)


def test_a_rebaseline_without_client_agreement_is_refused():
    """It is the only thing that distinguishes an agreement from a bill."""
    with pytest.raises(ar.AcceptanceError):
        _named("re-baseline", baseline_digest_after="b4")


def test_a_rebaseline_that_leaves_the_baseline_where_it_was_is_refused():
    """One that does not move the baseline is an absorb, and naming it a
    re-baseline records a renegotiation that did not happen."""
    with pytest.raises(ar.AcceptanceError) as excinfo:
        _named("re-baseline", client_agreed_at="2026-09-08T09:00:00Z")
    assert "MOVES the baseline" in str(excinfo.value)


def test_authorized_work_continues_during_a_negotiation():
    """§8: existing authorized work continues while a new baseline is
    negotiated."""
    record = _named(
        "re-baseline", client_agreed_at="2026-09-08T09:00:00Z",
        baseline_digest_after="b4", new_scope_unit_ids=["WU-NEW"],
        authorized_unit_ids=["WU-01", "WU-02"],
    )
    assert ar.authorized_work_continues(
        change=record, authorized_unit_ids=["WU-01", "WU-02"]
    ) == ["WU-01", "WU-02"]


def test_rescoping_already_authorized_work_as_new_scope_is_a_swap():
    """Refused here so a re-baseline cannot quietly stop committed work that the
    client is still owed."""
    with pytest.raises(ar.AcceptanceError) as excinfo:
        _named("re-baseline", client_agreed_at="2026-09-08T09:00:00Z",
               baseline_digest_after="b4", new_scope_unit_ids=["WU-01"],
               authorized_unit_ids=["WU-01"])
    assert "is a swap" in str(excinfo.value)


def test_new_scope_with_no_agreed_rebaseline_may_not_begin():
    """The same failure arriving from the other side: `name_change` refuses a
    record written after the work began, and this refuses the work beginning
    with no record."""
    with pytest.raises(ar.AcceptanceError) as excinfo:
        ar.assert_new_scope_may_begin(unit_id="WU-NEW", change=None)
    assert "before that work begins" in str(excinfo.value)


def test_new_scope_outside_the_agreed_rebaseline_may_not_begin():
    record = _named(
        "re-baseline", client_agreed_at="2026-09-08T09:00:00Z",
        baseline_digest_after="b4", new_scope_unit_ids=["WU-NEW"],
    )
    with pytest.raises(ar.AcceptanceError):
        ar.assert_new_scope_may_begin(unit_id="WU-OTHER", change=record)


def test_new_scope_the_client_agreed_to_may_begin():
    record = _named(
        "re-baseline", client_agreed_at="2026-09-08T09:00:00Z",
        baseline_digest_after="b4", new_scope_unit_ids=["WU-NEW"],
    )
    ar.assert_new_scope_may_begin(unit_id="WU-NEW", change=record)


def test_an_absorb_is_not_a_licence_for_new_scope_to_begin():
    with pytest.raises(ar.AcceptanceError):
        ar.assert_new_scope_may_begin(unit_id="WU-NEW", change=_named("absorb"))


# ── The absences, proved rather than asserted ───────────────────────────────


def test_acceptance_enumerates_no_roles():
    """The predecessor validated exactly five role receipts, so a five-name
    tuple is the shape that would come back first."""
    ar.assert_no_fixed_role_fanout()


def test_the_role_fanout_guard_catches_a_reintroduced_role_list(monkeypatch):
    monkeypatch.setitem(
        ar.__dict__, "ACCEPTANCE_ROLES",
        ("engagement-lead", "quality-assurance", "solution-architect"),
    )
    with pytest.raises(ar.AcceptanceError) as excinfo:
        ar.assert_no_fixed_role_fanout()
    assert "ACCEPTANCE_ROLES" in str(excinfo.value)


def test_the_role_fanout_guard_catches_a_caller_set_final(monkeypatch):
    """`final` is a property of the engagement's state; a flag would let a
    mid-engagement release close it."""
    def offender(*, final=False):  # pragma: no cover - a probe
        return final

    offender.__module__ = ar.__name__
    monkeypatch.setitem(ar.__dict__, "offender", offender)
    with pytest.raises(ar.AcceptanceError) as excinfo:
        ar.assert_no_fixed_role_fanout()
    assert "final" in str(excinfo.value)


# ── Two go-lives with a Cycle between them ──────────────────────────────────


def test_release_a_then_a_cycle_then_release_b():
    """`C-15`'s own scenario, end to end at this layer: each go-live compiles
    its own package and its own decision, the engagement stays open through the
    first, and only the second closes it."""
    first = _package(
        acceptance_id="REL-A", trunk_digest="sha256:trunk-1",
        cycle_closes=[_close("007-a", "c1")], integrated_shas_on_trunk=["c1"],
    )
    decision_a = ar.readiness_decision(
        package=first, principal=QA, outcome="continue", rationale="go",
    )
    ar.assert_decision_binds(decision_a, package=first)
    assert ar.acceptance_target(
        outstanding_commitments=["EPIC-9"], handover=_handover()
    ) == "CYCLE"

    second = _package(
        acceptance_id="REL-B", trunk_digest="sha256:trunk-2", opened_at=OPENED,
        cycle_closes=[_close("008-b", "c2")], integrated_shas_on_trunk=["c2"],
    )
    decision_b = ar.readiness_decision(
        package=second, principal=QA, outcome="continue", rationale="go",
    )
    ar.assert_decision_binds(decision_b, package=second)

    assert first["package_digest"] != second["package_digest"]
    with pytest.raises(ar.AcceptanceError):
        ar.assert_decision_binds(decision_a, package=second)
    assert ar.acceptance_target(
        outstanding_commitments=[], handover=_handover()
    ) == "COMPLETE"
