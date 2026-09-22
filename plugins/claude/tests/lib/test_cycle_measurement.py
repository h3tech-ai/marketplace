"""Layer 1 — per-Cycle throughput and cut-rate, per §5.4 (#646).

Refusal-weighted, and for this module the refusals are unusually load-bearing:
a metric's failure mode is not raising, it is answering. The Pilot 2 report had
to correct measurements that were zeros on a real run because a reader found no
board and reported the emptiness as data, so most of what follows asserts that a
question this module cannot answer comes back `available: False` with a reason
rather than as a number a plan would act on.
"""

from __future__ import annotations

import pytest

import cycle_authority
import cycle_measurement as cm

pytestmark = pytest.mark.unit

QA = "bob@h3t.co"


def _credit(unit_id, accepted_by=QA, accepted_at="2026-09-08T10:00:00Z", sha="c1", **over):
    entry = {
        "unit_id": unit_id,
        "accepted_by": accepted_by,
        "accepted_at": accepted_at,
        "integrated_sha": sha,
    }
    entry.update(over)
    return entry


def _history(**over):
    record = {
        "schema_version": "1",
        "cycle_id": "007-9f2c41ab",
        "admitted_unit_ids": ["WU-01", "WU-02", "WU-03", "WU-04"],
        "admitted_count_at_commit": 4,
        "cut_unit_ids": ["WU-04"],
        # Both producers write this (`#825`), so the fixture carries it: a
        # fixture lighter than the real record is how a reader comes to be
        # tested against a shape nothing writes.
        "work_units_done": 3,
        "accepted_units": [_credit("WU-01"), _credit("WU-02"), _credit("WU-03")],
        "integrated_sha": "c1",
        "integrated_at": "2026-09-08T09:00:00Z",
        "trunk_ref": "refs/heads/dev",
        "opened_at": "2026-09-01T00:00:00Z",
        "closed_at": "2026-09-08T00:00:00Z",
    }
    record.update(over)
    return record


# ── Accepted throughput ──────────────────────────────────────────────────────


def test_a_technical_done_credits_no_accepted_throughput():
    """§5.4 credits a Work Unit only once an accountable human accepted it as
    verified, so a Cycle whose units all reported `done` and none of which was
    accepted credits NOTHING.

    The crediting rule and the availability rule are separate, and `#825` is
    about the second. Reporting `done` does not make a credit -- but a Cycle
    that delivered three units and recorded no acceptance has not measured
    zero throughput, it has failed to measure throughput, and the two must not
    share a shape. What this pins is the crediting half: nothing in the
    delivered set leaks into `credited`.
    """
    measured = cm.accepted_throughput(_history(accepted_units=[]))
    assert measured["credited"] == []
    assert measured["count"] is None


def test_delivered_and_unaccepted_is_unavailable_not_zero():
    """`#825`. Two Cycles that delivered thirteen Work Units between them
    reported `available: True, value: 0` and a rate of `0.0 per day`, because
    acceptance had never been recorded. `SC-MTH-015` nominates this number as
    what capacity is planned against, so a project following the method plans
    against zero and cannot tell why."""
    measured = cm.accepted_throughput(_history(accepted_units=[], work_units_done=6))
    assert measured["available"] is False
    assert measured["value"] is None
    assert measured["count"] is None
    assert measured["work_units_done"] == 6
    problem = " ".join(measured["problems"])
    assert "6 delivered Work Units" in problem
    # It must name where acceptance is recorded. The predecessor defect one
    # level down (`#824`) was a refusal that named the rule and not the
    # remedy, and `accept_story` does not exist on this lifecycle.
    assert "Checkpoint" in problem
    assert "accept_story" not in problem


def test_a_cycle_that_delivered_nothing_keeps_its_honest_zero():
    """The zero stays where it is a measurement. A Cycle that delivered no
    unit accepted no unit, and that is a fact about the Cycle rather than a
    gap in the record."""
    measured = cm.accepted_throughput(_history(accepted_units=[], work_units_done=0))
    assert measured["available"] is True
    assert measured["count"] == 0
    assert measured["value"] == 0


def test_a_record_that_cannot_say_what_it_delivered_is_not_second_guessed():
    """A pre-`#825` record carries no `work_units_done`. Inferring an omission
    from a missing field would be the same substitution this fix removes, one
    field over -- so the count stands and the record is not called broken."""
    legacy = _history(accepted_units=[])
    legacy.pop("work_units_done")
    measured = cm.accepted_throughput(legacy)
    assert measured["available"] is True
    assert measured["count"] == 0
    assert cm.delivered_count(legacy) is None


def test_a_malformed_delivered_count_reads_as_no_answer():
    """Not as zero, and not as a delivery. `True` is an `int` in Python and a
    string is what a hand-edited record carries."""
    for bad in (True, "6", -1, None, 1.5):
        assert cm.delivered_count(_history(work_units_done=bad)) is None


def test_a_retry_creates_no_duplicate_credit():
    """One unit accepted twice is one delivery. The same set membership covers
    the retry, the re-import and a second `done`."""
    measured = cm.accepted_throughput(_history(accepted_units=[
        _credit("WU-01"), _credit("WU-01", accepted_at="2026-09-09T10:00:00Z"),
    ]))
    assert measured["count"] == 1
    assert measured["duplicates_dropped"] == 1


def test_an_imported_historical_row_creates_no_duplicate_credit():
    measured = cm.accepted_throughput(_history(accepted_units=[
        _credit("WU-01"), _credit("WU-01", imported=True, sha="old-sha"),
    ]))
    assert measured["count"] == 1


def test_a_credit_missing_its_timestamp_makes_the_whole_measurement_unavailable():
    """An incomplete observation reports unavailable, never a lower count: a
    partial number is indistinguishable from a measured one afterwards."""
    measured = cm.accepted_throughput(_history(accepted_units=[
        _credit("WU-01"), _credit("WU-02", accepted_at=""),
    ]))
    assert measured["available"] is False
    assert measured["count"] is None
    assert any("accepted_at" in p for p in measured["problems"])


def test_a_credit_missing_its_integration_revision_is_unavailable():
    """Without it the credit cannot be tied to what shipped."""
    measured = cm.accepted_throughput(_history(accepted_units=[_credit("WU-01", sha="")]))
    assert measured["available"] is False


def test_the_automated_principal_cannot_accept_delivery():
    """A record naming a machine as the accepting human is not trustworthy
    rather than one credit short."""
    measured = cm.accepted_throughput(_history(accepted_units=[
        _credit("WU-01", accepted_by=cycle_authority.AUTOMATED_PRINCIPAL),
    ]))
    assert measured["available"] is False
    assert any("automated principal" in p for p in measured["problems"])


def test_crediting_a_unit_this_cycle_never_admitted_is_unavailable():
    """Otherwise delivery is attributed to the wrong Cycle, and the throughput
    of both is wrong in opposite directions."""
    measured = cm.accepted_throughput(_history(accepted_units=[_credit("WU-77")]))
    assert measured["available"] is False


def test_the_rate_is_reported_with_its_units_and_its_window():
    """§5.4 requires both the count and the elapsed-time rate with explicit
    units; a bare float is how a per-day rate is read as a per-Cycle one."""
    measured = cm.accepted_throughput(_history())
    assert measured["unit"] == cm.THROUGHPUT_UNIT
    rate = measured["rate"]
    assert rate["available"] is True
    assert rate["unit"] == cm.THROUGHPUT_RATE_UNIT
    assert rate["window"]["elapsed_days"] == pytest.approx(7.0)
    assert rate["value"] == pytest.approx(3 / 7.0)


def test_an_unrecorded_window_leaves_the_rate_unavailable_but_the_count_intact():
    """Reporting the rate as zero would invent an observation window nobody
    measured; dropping the count would discard one that was."""
    measured = cm.accepted_throughput(_history(opened_at=""))
    assert measured["available"] is True
    assert measured["count"] == 3
    assert measured["rate"]["available"] is False


def test_a_zero_length_window_has_no_rate():
    measured = cm.accepted_throughput(
        _history(opened_at="2026-09-08T00:00:00Z", closed_at="2026-09-08T00:00:00Z")
    )
    assert measured["rate"]["available"] is False


def test_a_record_missing_a_required_field_is_unavailable_not_zero():
    measured = cm.accepted_throughput(_history(admitted_unit_ids=None))
    assert measured["available"] is False
    assert measured["count"] is None


# ── Cut-rate ─────────────────────────────────────────────────────────────────


def test_the_cut_rate_denominator_is_the_original_commit_admitted_count():
    measured = cm.cut_rate(_history())
    assert measured["denominator"] == 4
    assert measured["numerator"] == 1
    assert measured["value"] == pytest.approx(0.25)


def test_the_denominator_does_not_shrink_after_a_cut():
    """The one way this rate can be made to lie: remove the cut units from the
    admitted list and a Cycle that cut half its scope reports zero over the half
    that remained."""
    measured = cm.cut_rate(_history(
        admitted_unit_ids=["WU-01", "WU-02", "WU-03"], cut_unit_ids=["WU-04"],
    ))
    assert measured["available"] is False
    assert any("does not shrink" in p for p in measured["problems"])


def test_an_empty_admission_is_refused_rather_than_rated():
    """Every cut-rate over zero admitted units is undefined, or 0/0 reported as
    0%, and a Cycle that admitted nothing has no delivery to rate."""
    with pytest.raises(cm.MeasurementError) as excinfo:
        cm.cut_rate(_history(admitted_unit_ids=[], admitted_count_at_commit=0,
                             cut_unit_ids=[]))
    assert "Empty" in str(excinfo.value) or "empty" in str(excinfo.value)


def test_a_cut_unit_the_cycle_never_admitted_is_unavailable():
    """The numerator would count work the denominator does not."""
    measured = cm.cut_rate(_history(cut_unit_ids=["WU-99"]))
    assert measured["available"] is False


def test_the_same_unit_cut_twice_counts_once():
    measured = cm.cut_rate(_history(cut_unit_ids=["WU-04", "WU-04"]))
    assert measured["numerator"] == 1


def test_a_cycle_that_cut_nothing_measures_zero_rather_than_unavailable():
    """The distinction runs both ways: a real zero must be a measurement."""
    measured = cm.cut_rate(_history(cut_unit_ids=[]))
    assert measured["available"] is True
    assert measured["value"] == 0.0


# ── Lead time ────────────────────────────────────────────────────────────────


def test_lead_time_ends_at_observed_trunk_integration_not_at_the_close():
    """Using the close would fold the time between the merge and the
    bookkeeping into the delivery measurement."""
    measured = cm.lead_time(
        _history(), specification_approved_at="2026-09-01T09:00:00Z",
        specification_revision="SPEC-4@r2",
    )
    assert measured["available"] is True
    assert measured["value"] == pytest.approx(7.0)
    assert measured["integration"]["integrated_at"] == "2026-09-08T09:00:00Z"


def test_lead_time_keeps_human_acceptance_in_a_separate_block():
    """A Cycle can be on the trunk and unaccepted; one number covering both
    would report the unaccepted case as delivered."""
    measured = cm.lead_time(
        _history(), specification_approved_at="2026-09-01T09:00:00Z",
        specification_revision="SPEC-4@r2",
    )
    assert measured["acceptance"]["available"] is True
    assert measured["acceptance"]["value"] != measured["value"]


def test_an_integrated_cycle_with_no_acceptance_reports_acceptance_unavailable():
    measured = cm.lead_time(
        _history(accepted_units=[]),
        specification_approved_at="2026-09-01T09:00:00Z",
        specification_revision="SPEC-4@r2",
    )
    assert measured["available"] is True
    assert measured["acceptance"]["available"] is False


def test_lead_time_without_an_observed_integration_has_no_end():
    measured = cm.lead_time(
        _history(integrated_at=""),
        specification_approved_at="2026-09-01T09:00:00Z",
        specification_revision="SPEC-4@r2",
    )
    assert measured["available"] is False
    assert any("no end" in p for p in measured["problems"])


def test_lead_time_without_a_specification_revision_cannot_be_traced():
    measured = cm.lead_time(
        _history(), specification_approved_at="2026-09-01T09:00:00Z",
        specification_revision="",
    )
    assert measured["available"] is False


# ── The history a plan reads ─────────────────────────────────────────────────


def test_no_closed_cycle_is_an_absence_and_not_a_history_of_zero():
    """The distinction a concurrency plan rests on, and the one a metric reader
    loses first."""
    report = cm.history_report([])
    assert report["available"] is False
    assert report["throughput"]["available"] is False
    assert report["throughput"]["value"] is None
    assert report["cut_rate"]["value"] is None
    assert report["problems"]


def test_the_report_always_carries_a_throughput_key_even_when_absent():
    """A consumer that has to branch on the key's presence learns nothing about
    availability from it, which is how absence gets read as zero."""
    assert "throughput" in cm.history_report([])
    assert "throughput" in cm.history_report([_history()])


def test_one_unmeasurable_cycle_does_not_make_the_whole_history_unreadable():
    report = cm.history_report([_history(), _history(cycle_id="008", accepted_units=[
        _credit("WU-01", accepted_at="")])])
    assert report["available"] is True
    assert report["sample_size"] == 1
    assert report["cycle_ids"] == ["007-9f2c41ab"]


def test_a_history_where_every_cycle_is_unmeasurable_reports_unavailable():
    report = cm.history_report([_history(admitted_count_at_commit=None)])
    assert report["available"] is False
    assert any("none is measurable" in p for p in report["problems"])


def test_the_report_names_its_observation_window():
    report = cm.history_report([_history(), _history(
        cycle_id="008-aabbccdd", closed_at="2026-09-15T00:00:00Z")])
    assert report["observation_window"]["from"] == "2026-09-08T00:00:00Z"
    assert report["observation_window"]["to"] == "2026-09-15T00:00:00Z"


# ── Planning ─────────────────────────────────────────────────────────────────


def _cites(report, **over):
    cites = {
        "cycle_ids": list(report["cycle_ids"]),
        "sample_size": report["sample_size"],
        "throughput": report["throughput"]["value"],
        "cut_rate": report["cut_rate"]["value"],
        "observation_window": report["observation_window"],
        "comparable_work": "api routers, same verification depth",
        "verification_depth": "replayed evidence, mature DoD tier",
    }
    cites.update(over)
    return cites


def test_a_plan_asserted_without_recorded_history_is_refused():
    """`C-16`: concurrency is planned against recorded throughput and cut-rate,
    never against what a team believes it can hold."""
    with pytest.raises(cm.MeasurementError) as excinfo:
        cm.assert_plan_supported(
            plan={"concurrent_cycles": 3}, history=cm.history_report([]),
        )
    assert "invented capacity" in str(excinfo.value)


def test_a_first_ever_cycle_may_bootstrap_but_must_say_so_explicitly():
    cm.assert_plan_supported(
        plan={
            "concurrent_cycles": 1,
            "bootstrap": {
                "calibration_sample": "Discovery: 3 units, 2 days, replayed evidence",
                "assumption": "one Cycle at a time until two Cycles are recorded",
            },
        },
        history=cm.history_report([]),
    )


def test_a_bootstrap_without_an_explicit_assumption_is_refused():
    """An unstated assumption reads as a measurement later."""
    with pytest.raises(cm.MeasurementError):
        cm.assert_plan_supported(
            plan={"concurrent_cycles": 1,
                  "bootstrap": {"calibration_sample": "Discovery: 3 units"}},
            history=cm.history_report([]),
        )


def test_a_bootstrap_is_refused_once_history_exists():
    """Using one where history exists plans against belief next to the data
    that contradicts it."""
    history = cm.history_report([_history()])
    with pytest.raises(cm.MeasurementError) as excinfo:
        cm.assert_plan_supported(
            plan={"concurrent_cycles": 2,
                  "bootstrap": {"calibration_sample": "x", "assumption": "y"}},
            history=history,
        )
    assert "first-ever" in str(excinfo.value)


def test_a_plan_that_cites_the_record_is_accepted():
    history = cm.history_report([_history()])
    cm.assert_plan_supported(
        plan={"concurrent_cycles": 2, "cites": _cites(history)}, history=history,
    )


@pytest.mark.parametrize("dropped", list(cm.PLAN_REQUIRED_CITATIONS))
def test_every_required_citation_is_required(dropped):
    """§5.4's capacity-input list is the whole list; a plan citing only a number
    cannot be checked against the record it came from."""
    history = cm.history_report([_history()])
    with pytest.raises(cm.MeasurementError) as excinfo:
        cm.assert_plan_supported(
            plan={"concurrent_cycles": 2, "cites": _cites(history, **{dropped: None})},
            history=history,
        )
    assert dropped in str(excinfo.value)


def test_a_plan_citing_a_cycle_nobody_recorded_is_refused():
    """Fabricated Cycle history: a citation names a record."""
    history = cm.history_report([_history()])
    with pytest.raises(cm.MeasurementError) as excinfo:
        cm.assert_plan_supported(
            plan={"concurrent_cycles": 2,
                  "cites": _cites(history, cycle_ids=["007-9f2c41ab", "099-deadbeef"])},
            history=history,
        )
    assert "099-deadbeef" in str(excinfo.value)


def test_a_plan_citing_a_larger_sample_than_was_recorded_is_refused():
    history = cm.history_report([_history()])
    with pytest.raises(cm.MeasurementError):
        cm.assert_plan_supported(
            plan={"concurrent_cycles": 2, "cites": _cites(history, sample_size=6)},
            history=history,
        )


def test_a_plan_citing_a_throughput_the_record_does_not_measure_is_refused():
    """Whichever number is right, the plan is not built on what was measured."""
    history = cm.history_report([_history()])
    with pytest.raises(cm.MeasurementError) as excinfo:
        cm.assert_plan_supported(
            plan={"concurrent_cycles": 2, "cites": _cites(history, throughput=11.0)},
            history=history,
        )
    assert "claim about the record" in str(excinfo.value)


def test_a_plan_citing_a_cut_rate_the_record_does_not_measure_is_refused():
    history = cm.history_report([_history()])
    with pytest.raises(cm.MeasurementError):
        cm.assert_plan_supported(
            plan={"concurrent_cycles": 2, "cites": _cites(history, cut_rate=0.0)},
            history=history,
        )


def test_a_plan_that_names_no_concurrency_is_refused():
    with pytest.raises(cm.MeasurementError):
        cm.assert_plan_supported(plan={}, history=cm.history_report([_history()]))


# ── The barrier's own close record is measurable ─────────────────────────────


def test_the_barriers_close_record_measures_without_translation():
    """The writer and the reader are one contract. If `close` produced a record
    this module had to be taught to read, the two would drift and the metric
    would be measuring an older shape than the one being written."""
    import cycle_barrier as cb
    import cycle_records as cr

    declaration = cr.seal({
        "cycle_id": "007-9f2c41ab",
        "repository": "h3tech-ai/synaptory-v1",
        "trunk_ref": "refs/heads/dev",
        "baseline_ref": "baseline-3",
        "goal": "authentication",
        "engineering_lead": "alice@h3t.co",
        "source_region": ["api/"],
        "barrier_criteria": list(cr.BARRIER_CRITERIA),
        "admitted_units": [{
            "id": "WU-01", "kind": "story", "acceptance_criteria": ["AC-1"],
            "path_scope": ["api/routers/"], "depends_on": [],
        }],
        "shared_path_owners": [],
    })
    trunk = {
        "trunk_ref": "refs/heads/dev", "observed_sha": "t0", "current_sha": "t0",
        "candidate_sha": "c1", "candidate_is_ancestor_of_trunk": True,
    }
    verdict = cb.evaluate(
        declaration=declaration,
        unit_results={"WU-01": {
            "schema_version": cb.RESULT_SCHEMA_VERSION,
            "acceptance_criteria": {"AC-1": {"passed": True}},
            "acceptance": {"accepted_by": QA, "accepted_at": "2026-09-08T10:00:00Z"},
        }},
        proofs={"regression": {"passed": True}},
        trunk=trunk,
        opened_at="2026-09-01T00:00:00Z",
    )
    promotion = cb.promote(verdict=verdict, ledger=[], principal="alice@h3t.co")
    closed = cb.close(verdict=verdict, ledger=[promotion], principal="alice@h3t.co")

    measured = cm.measure_cycle(closed["history"])
    assert measured["available"] is True, measured["problems"]
    assert measured["throughput"]["count"] == 1
    assert measured["cut_rate"]["denominator"] == 1
    assert cm.history_report([closed["history"]])["available"] is True
    # The writer's half of `#825`. Asserted on the real close record rather
    # than on a fixture, because a reader proved only against a fixture is a
    # reader proved against a shape nothing writes.
    assert closed["history"]["work_units_done"] == 1


def test_a_real_close_with_no_acceptance_measures_unavailable():
    """`#825` end to end, through the producer. A green barrier does not
    require a recorded acceptance -- the reporting engagement closed three
    Cycles this way -- so the close record must carry enough for the measurer
    to tell this apart from a Cycle that accepted nothing."""
    import cycle_barrier as cb
    import cycle_records as cr

    declaration = cr.seal({
        "cycle_id": "008-1a2b3c4d",
        "repository": "h3tech-ai/synaptory-v1",
        "trunk_ref": "refs/heads/dev",
        "baseline_ref": "baseline-3",
        "goal": "storage",
        "engineering_lead": "alice@h3t.co",
        "source_region": ["api/"],
        "barrier_criteria": list(cr.BARRIER_CRITERIA),
        "admitted_units": [{
            "id": "WU-01", "kind": "story", "acceptance_criteria": ["AC-1"],
            "path_scope": ["api/routers/"], "depends_on": [],
        }],
        "shared_path_owners": [],
    })
    verdict = cb.evaluate(
        declaration=declaration,
        # Every criterion met, and NO `acceptance` block -- the shape that
        # read as `0.0 accepted work units per day`.
        unit_results={"WU-01": {
            "schema_version": cb.RESULT_SCHEMA_VERSION,
            "acceptance_criteria": {"AC-1": {"passed": True}},
        }},
        proofs={"regression": {"passed": True}},
        trunk={
            "trunk_ref": "refs/heads/dev", "observed_sha": "t0", "current_sha": "t0",
            "candidate_sha": "c1", "candidate_is_ancestor_of_trunk": True,
        },
        opened_at="2026-09-01T00:00:00Z",
    )
    assert verdict["green"] is True, verdict["unmet"]
    assert verdict["accepted_units"] == []
    promotion = cb.promote(verdict=verdict, ledger=[], principal="alice@h3t.co")
    closed = cb.close(verdict=verdict, ledger=[promotion], principal="alice@h3t.co")

    assert closed["history"]["work_units_done"] == 1
    measured = cm.measure_cycle(closed["history"])
    assert measured["available"] is False
    assert measured["throughput"]["value"] is None
    assert "1 delivered Work Unit," in " ".join(measured["problems"])
    # And a history made only of such Cycles is an absence, not a history of
    # zero throughput -- which is the number capacity would be planned against.
    report = cm.history_report([closed["history"]])
    assert report["available"] is False
    assert report["throughput"]["value"] is None
