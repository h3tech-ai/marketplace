"""Layer 1 — four stages, a repeatable Acceptance, and no override (#644).

The predecessor declared seven states with `SYNC` mandatory between execution
and Checkpoint, `COMPLETE` terminal with no row at all, one global
`current_cycle` scalar, and `force=True` on both the transition and the close.
Every test here is one of those four, inverted.
"""

from __future__ import annotations

import pytest

import cycle_lifecycle as cl


def _declaration(cycle="007-aaaa1111", region=("api/",), **over):
    body = {
        "cycle_id": cycle,
        "cycle_seq": 7,
        "repository": "h3tech-ai/synaptory-v1",
        "trunk_ref": "refs/heads/dev",
        "baseline_ref": "baseline-3",
        "goal": "authentication",
        "engineering_lead": "alice@h3t.co",
        "source_region": list(region),
        "barrier_criteria": list(__import__("cycle_records").BARRIER_CRITERIA),
        "admitted_units": [{
            "id": "WU-01", "kind": "story",
            "acceptance_criteria": ["it works"],
            "path_scope": ["api/routers/"], "depends_on": [],
        }],
        "shared_path_owners": [],
    }
    body.update(over)
    return body


# ── Four stages, not seven ───────────────────────────────────────────────────

def test_there_are_four_stages_and_no_event_among_them():
    assert cl.STAGES == ("DISCOVERY", "CYCLE", "ACCEPTANCE", "COMPLETE")
    for event in ("COMMIT", "SYNC", "CHECKPOINT"):
        assert event not in cl.STAGES


def test_delivery_reaches_acceptance_without_passing_through_sync():
    """The predecessor had no `CYCLE_EXECUTION -> CHECKPOINT` edge, on purpose,
    so integration could not be skipped and also could not be reached any other
    way. Here the barrier holds that, not the topology."""
    cl.check_transition("CYCLE", "ACCEPTANCE")


def test_a_cycle_may_follow_a_cycle():
    """Closing one Cycle and opening the next is delivery continuing, not a
    stage change. Modelling it as a departure and a return would make the trunk
    look momentarily undelivered."""
    cl.check_transition("CYCLE", "CYCLE")


@pytest.mark.parametrize("current,target", [
    ("DISCOVERY", "ACCEPTANCE"),
    ("DISCOVERY", "COMPLETE"),
    ("CYCLE", "DISCOVERY"),
    ("ACCEPTANCE", "DISCOVERY"),
])
def test_an_illegal_transition_names_what_is_legal(current, target):
    with pytest.raises(cl.LifecycleError) as excinfo:
        cl.check_transition(current, target)
    assert "Legal:" in str(excinfo.value)


def test_there_is_no_override_anywhere_in_this_module():
    """`C-12`: an agent able to disable the rules it runs under has no
    guardrail. The predecessor's `force=True` moved a project from DISCOVERY to
    CHECKPOINT in one call, from any raw module call.

    Proved by inspection rather than asserted, so adding one later fails here.
    """
    cl.assert_no_force_parameter()


def test_the_refusal_says_there_is_no_override():
    """A reader who wants `force` should learn that it is gone and why, from the
    refusal itself rather than from a changelog."""
    with pytest.raises(cl.LifecycleError) as excinfo:
        cl.check_transition("DISCOVERY", "COMPLETE")
    assert "no override" in str(excinfo.value)


# ── Repeatable Acceptance ────────────────────────────────────────────────────

def test_a_nonfinal_acceptance_returns_to_delivery():
    """`C-15`. The predecessor had `ACCEPTANCE -> COMPLETE` and no row for
    COMPLETE, which is stronger than an empty row: no edge to relax, so a
    non-final go-live was unreachable by configuration."""
    assert "CYCLE" in cl.legal_targets("ACCEPTANCE")
    target = cl.acceptance_target(
        outstanding_commitments=["migrate the legacy importer"],
        handover_recorded=False,
    )
    assert target == "CYCLE"


def test_the_final_acceptance_closes_the_engagement():
    target = cl.acceptance_target(outstanding_commitments=[], handover_recorded=True)
    assert target == "COMPLETE"


def test_final_is_a_property_of_the_engagement_not_a_flag():
    """A caller-set flag would let a mid-engagement release close the
    engagement. §5.3 makes the final Acceptance the one that hands over."""
    import inspect

    params = inspect.signature(cl.acceptance_target).parameters
    assert "final" not in params and "is_final" not in params
    assert cl.is_final_acceptance(outstanding_commitments=[], handover_recorded=False) is False
    assert cl.is_final_acceptance(outstanding_commitments=["x"], handover_recorded=True) is False


def test_complete_stays_terminal_and_says_why():
    assert cl.legal_targets("COMPLETE") == ()
    with pytest.raises(cl.LifecycleError) as excinfo:
        cl.check_transition("COMPLETE", "CYCLE")
    assert "handing over" in str(excinfo.value)


# ── Many Cycles, none of them global ─────────────────────────────────────────

def test_engagement_state_and_cycle_state_are_different_vocabularies():
    assert cl.ENGAGEMENT_STATES == ("discovering", "active", "closed")
    assert cl.CYCLE_STATES == ("draft", "executing", "closed")


def test_a_cycle_must_be_named_because_there_is_no_current_one():
    """N Engineering Leads means N concurrent Cycles, so a global pointer would
    have to pick one arbitrarily."""
    open_cycles = [{"cycle_id": "007-aaaa1111"}, {"cycle_id": "008-bbbb2222"}]
    with pytest.raises(cl.LifecycleError) as excinfo:
        cl.select_cycle(open_cycles, "")
    assert "no current Cycle" in str(excinfo.value)
    assert "007-aaaa1111" in str(excinfo.value)


def test_selecting_a_cycle_that_is_not_open_lists_the_ones_that_are():
    with pytest.raises(cl.LifecycleError) as excinfo:
        cl.select_cycle([{"cycle_id": "007-aaaa1111"}], "009-cccc3333")
    assert "007-aaaa1111" in str(excinfo.value)


def test_opening_a_cycle_seals_its_declaration():
    cycle = cl.open_cycle(_declaration())
    assert cycle["state"] == "executing"
    assert cycle["declaration"]["declaration_hash"]


def test_a_cycle_cannot_open_on_an_invalid_declaration():
    import cycle_records

    with pytest.raises(cycle_records.DeclarationError):
        cl.open_cycle(_declaration(source_region=[]))


def test_the_admitted_set_is_read_from_the_declaration_not_a_board():
    """A board changes as work progresses; the admitted set does not. A barrier
    ranging over the board would range over whatever survived rather than over
    what was promised."""
    cycle = cl.open_cycle(_declaration())
    assert cl.admitted_ids(cycle) == ["WU-01"]


# ── Regions: declared, and the local answer is not the authority ─────────────

def test_an_overlapping_region_names_the_cycle_it_would_collide_with():
    first = cl.open_cycle(_declaration("007-aaaa1111", region=("api/",)))
    assert cl.concurrent_regions_conflict([first], ["api/routers/"]) == "007-aaaa1111"
    assert cl.concurrent_regions_conflict([first], ["web/"]) is None


def test_a_closed_cycle_no_longer_holds_its_region():
    first = cl.open_cycle(_declaration("007-aaaa1111", region=("api/",)))
    first = dict(first, state="closed")
    assert cl.concurrent_regions_conflict([first], ["api/"]) is None


def test_an_empty_local_view_reports_no_conflict_which_is_why_it_is_not_authority():
    """Documented here because it is the trap: a clone that has seen nothing
    answers `None`, and a caller must treat an unreadable registry as a
    refusal rather than as an absence of conflict. §5.2 -- availability is
    never permission to invent ownership."""
    assert cl.concurrent_regions_conflict([], ["api/"]) is None


# ── Recovery replaces force ──────────────────────────────────────────────────

def test_recovery_is_a_decision_with_a_rationale_not_an_argument():
    record = cl.recovery_decision(
        principal="alice@h3t.co", subject="007-aaaa1111",
        rationale="barrier evaluated against a stale candidate after a force push",
    )
    assert record["role"] == "engineering-lead"
    assert record["outcome"] == "escalate"


def test_a_recovery_without_a_rationale_is_refused():
    """`force=True` needed nothing, which is why nothing explains any of the
    transitions it took."""
    with pytest.raises(cl.LifecycleError) as excinfo:
        cl.recovery_decision(principal="alice@h3t.co", subject="007-aaaa1111",
                             rationale="  ")
    assert "rationale" in str(excinfo.value)


def test_a_recovery_cannot_be_unattributed():
    import cycle_authority

    with pytest.raises(cycle_authority.AuthorityError):
        cl.recovery_decision(principal="", subject="007-aaaa1111", rationale="x")
