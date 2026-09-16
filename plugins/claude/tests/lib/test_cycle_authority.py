"""Layer 1 — accountable action: who may be named, never what may be reached (#643).

`SC-MTH-005` is the row this file defends: a role gates only which principal may
be **named** for an action it owns, carries no permission, confers no reach, and
appears in no authorization table. Every test here is that separation, because
collapsing it produces a system where holding a job title grants access — the
failure the separation exists to prevent.
"""

from __future__ import annotations

import pytest

import cycle_authority as ca


def test_the_six_roles_are_the_six_the_method_declares():
    assert set(ca.ROLES) == {
        "engagement-lead", "business-analyst", "solution-architect",
        "devsecops-engineer", "engineering-lead", "quality-assurance",
    }


def test_the_nine_legacy_delivery_names_are_not_roles():
    """`SC-MTH-005`: retired, and not reintroduced as aliases, profiles or roles.

    The nine stay in `runtime_contracts.ROLE_NAMES` as capability composition,
    which confers no authority either. Two separately typed things.
    """
    for legacy in ("software-engineer", "quality-engineer", "code-reviewer",
                   "compliance-engineer", "platform-engineer", "project-owner",
                   "technical-writer", "research-advisor"):
        assert legacy not in ca.ROLES, legacy
        with pytest.raises(ca.AuthorityError):
            ca.assert_grants_nothing(legacy)


def test_a_role_carries_accountability_and_nothing_structured():
    """The mechanical form of "confers no reach".

    If somebody adds a permission list, a scope, a capability set or a tool
    allowlist to a role, this fails — so the separation is defended by the
    suite rather than by review.
    """
    for role in ca.ROLES:
        ca.assert_grants_nothing(role)
        assert isinstance(ca.ROLES[role], str)


def test_there_is_no_permits_function_to_call():
    """A role that could answer "may this actor reach X" would be a permission.

    Asserted as an absence because the absence is the design: `C-11` refuses
    "a role that also grants access", and the cheapest way to hold that is to
    give the module no such verb.
    """
    assert not hasattr(ca, "permits")
    assert not hasattr(ca, "can")
    assert not hasattr(ca, "authorize")


def test_the_role_is_derived_from_the_action_not_passed_in():
    """`decide` has no `role` parameter, by construction.

    An accountability the subject selects is not one, so a caller cannot offer
    a role — it is looked up from what is being decided.
    """
    import inspect

    assert "role" not in inspect.signature(ca.decide).parameters
    assert ca.role_for("integrate-to-trunk") == "engineering-lead"
    assert ca.role_for("approve-baseline") == "engagement-lead"


def test_an_unknown_action_is_refused_rather_than_defaulted():
    """A default owner is how an accountable action acquires an unaccountable one."""
    with pytest.raises(ca.AuthorityError) as excinfo:
        ca.role_for("ship-it")
    assert "owns no role" in str(excinfo.value)
    assert "ACTION_OWNER" in str(excinfo.value)


def test_a_decision_records_the_role_it_acted_under():
    record = ca.decide(
        action="cut-work-unit", principal="alice@h3t.co", outcome="continue",
        subject="WU-07", rationale="not finishable this Cycle",
    )
    assert record["role"] == "engineering-lead"
    assert record["risk"] == "routine"
    assert record["outcome"] == "continue"


@pytest.mark.parametrize("outcome", list(ca.OUTCOMES))
def test_every_declared_outcome_is_accepted(outcome):
    record = ca.decide(action="cut-work-unit", principal="alice@h3t.co",
                       outcome=outcome, subject="WU-07")
    assert record["outcome"] == outcome


@pytest.mark.parametrize("outcome", ["pass", "approve", "ok", "", None, "PASS"])
def test_an_implicit_pass_is_not_one_of_the_five(outcome):
    """`C-11`: a gate resolves to one of a closed set and never to an implicit
    pass. There is deliberately no `pass` — `continue` is a decision somebody
    is named for."""
    with pytest.raises(ca.AuthorityError) as excinfo:
        ca.decide(action="cut-work-unit", principal="alice@h3t.co",
                  outcome=outcome, subject="WU-07")
    assert "implicit" in str(excinfo.value)


def test_no_actor_approves_its_own_work():
    """`C-11` and `C-13`: the independence is an identity boundary, not a
    review convention."""
    with pytest.raises(ca.AuthorityError) as excinfo:
        ca.decide(action="accept-work-unit", principal="agent:se-01",
                  outcome="continue", subject="WU-07", produced_by="agent:se-01")
    assert "produced the subject" in str(excinfo.value)


def test_a_different_principal_may_decide_on_produced_work():
    record = ca.decide(action="accept-work-unit", principal="alice@h3t.co",
                       outcome="continue", subject="WU-07",
                       produced_by="agent:se-01")
    assert record["produced_by"] == "agent:se-01"


@pytest.mark.parametrize("action", sorted(ca.HIGH_RISK_ACTIONS))
def test_a_high_risk_action_never_continues_automatically(action):
    with pytest.raises(ca.AuthorityError) as excinfo:
        ca.decide(action=action, principal=ca.AUTOMATED_PRINCIPAL,
                  outcome="continue", subject="RELEASE-3")
    assert "high risk" in str(excinfo.value)
    assert ca.role_for(action) in str(excinfo.value)


def test_routine_change_may_proceed_under_monitoring():
    """The other half of the tier, which the method states explicitly: routine
    change proceeds under monitoring. Refusing the automated principal in
    general would make the tiering meaningless."""
    record = ca.decide(action="admit-work-unit", principal=ca.AUTOMATED_PRINCIPAL,
                       outcome="continue", subject="WU-07")
    assert record["risk"] == "routine"


def test_an_unattributed_decision_is_refused():
    for principal in ("", "   ", None):
        with pytest.raises(ca.AuthorityError):
            ca.decide(action="cut-work-unit", principal=principal,
                      outcome="continue", subject="WU-07")


def test_every_owned_action_names_one_of_the_six():
    """A typo in ACTION_OWNER would make an action unaccountable at runtime."""
    for action, role in ca.ACTION_OWNER.items():
        assert role in ca.ROLES, "%s -> %s" % (action, role)


def test_every_high_risk_action_is_an_action():
    for action in ca.HIGH_RISK_ACTIONS:
        assert action in ca.ACTION_OWNER, action
