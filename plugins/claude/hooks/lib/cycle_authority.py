"""Accountable action: who may be NAMED, never what may be reached (#643).

`SC-MTH-004` and `SC-MTH-005` draw a line this repository has never had, and
the line is the whole module: **a role carries accountability and confers no
permission or reach.** Holding one grants nothing. A role resolves only at the
accountable-action check, where it decides *who may be named* for the decision.

`SC-MTH-005` is explicit that the nine legacy delivery names are retired and
shall not be reintroduced as aliases, profiles or roles. So this module is
deliberately NOT a rename of `runtime_contracts.ROLE_NAMES`. Those nine stay
exactly where they are: they are **capability composition** — which skills a
worker composes to do analysis, planning, production or proving — and they
appear in no authorization table either. Two separately typed things, and
`SC-MTH-004` says neither shall be represented as the other.

The three refusals that make the separation checkable, rather than documented:

    a role used as a permission   `permits()` does not exist, and
                                  `assert_grants_nothing` proves the absence
                                  rather than trusting it
    an actor choosing its role    `role_for` is keyed on the ACTION. A caller
                                  cannot pass one in, which is why the
                                  signature has no `role` parameter
    an actor approving its work    `decide` refuses when the deciding principal
                                  is the one that produced the subject

And the fourth, from `C-11`: a high-risk transition never continues
automatically. `decide` requires a named human principal for `HIGH`, and
`AUTOMATED_PRINCIPAL` is refused there specifically rather than in general —
routine change proceeding under monitoring is the method's own wording.

WHAT THIS MODULE DOES NOT DO, stated because the gap is load-bearing. It does
not authenticate. `principal` arrives already authenticated from the control
plane's session, and a module that both mints and checks an identity checks
nothing. `C-12` places the guardrail outside the executing agent's reach, and
a laptop-local module is not outside it — the authority of record is the
control plane, and this is the shape a decision must have before it is sent
there.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional

#: The six roles `SC-MTH-004` declares, and what each answers for. The value is
#: prose on purpose: a role is an accountability, and there is nothing else to
#: put here. A permission list would be the error this module exists to prevent.
ROLES: Dict[str, str] = {
    "engagement-lead": (
        "What gets built and whether it delivers: the requirements, the "
        "specification, the commitment including cost discipline, and the "
        "outcome. Names which kind each change is (§8)"
    ),
    "business-analyst": (
        "Requirements elicitation and analysis with stakeholders and "
        "subject-matter experts, alongside the Engagement Lead who owns them"
    ),
    "solution-architect": (
        "The technical design of the system, and in Discovery the architecture "
        "and the decisions that shape it, including an existing system's state"
    ),
    "devsecops-engineer": (
        "The agents that build the deployment environments and the CI/CD "
        "pipeline, and their security and compliance"
    ),
    "engineering-lead": (
        "One Cycle and the Crew executing it: assigning work, steering it, "
        "correcting output until it meets the bar. Clears cross-Cycle waits at Sync"
    ),
    "quality-assurance": (
        "The quality, security and compliance of the product: the acceptance "
        "criteria verified against, the verification metrics, the "
        "release-readiness judgment, and the cases the guardrails escalate"
    ),
}

#: The closed outcome set. `C-11`: a gate resolves to one of these and **never
#: to an implicit pass**. There is deliberately no `pass` and no `approve`:
#: `CONTINUE` is a decision somebody is named for, and the absence of a
#: decision is not one of the five.
OUTCOMES = ("continue", "escalate", "ask-client", "block", "rework")

#: Risk tiers. `C-11` tiers authority to risk: routine change proceeds under
#: monitoring, critical/regulated/high-impact change is decided by a named
#: accountable human.
RISK_TIERS = ("routine", "elevated", "high")

#: The sentinel for a decision no human made. Refused at `high` only.
AUTOMATED_PRINCIPAL = "system:automated"

#: Action -> the role that owns it. **Derived from the action, never chosen by
#: the actor**, which is what `C-11`'s "with the acting role derived from the
#: action and recorded" requires. An action absent here is refused rather than
#: defaulted: a default owner is how an accountable action acquires an
#: unaccountable one.
ACTION_OWNER: Dict[str, str] = {
    # Baseline and scope
    "approve-baseline": "engagement-lead",
    "name-change-kind": "engagement-lead",
    "approve-rebaseline": "engagement-lead",
    "approve-swap": "engagement-lead",
    # The Cycle
    "declare-source-region": "engineering-lead",
    "admit-work-unit": "engineering-lead",
    "cut-work-unit": "engineering-lead",
    "integrate-to-trunk": "engineering-lead",
    "close-cycle": "engineering-lead",
    # Proof and release
    "accept-work-unit": "quality-assurance",
    "decide-release-readiness": "quality-assurance",
    "escalate-guardrail-case": "quality-assurance",
    # Design and pipeline
    "approve-architecture-decision": "solution-architect",
    "approve-pipeline-change": "devsecops-engineer",
    # Requirements
    "record-requirement": "business-analyst",
}

#: Actions that never continue automatically (`C-11`). Each moves the
#: commitment, ships something, or overrides a guardrail.
HIGH_RISK_ACTIONS = frozenset({
    "approve-baseline",
    "approve-rebaseline",
    "approve-swap",
    "integrate-to-trunk",
    "decide-release-readiness",
    "escalate-guardrail-case",
})


class AuthorityError(ValueError):
    """A decision this module refuses, with the reason."""


def role_for(action: str) -> str:
    """The role that owns this action.

    No `role` parameter, by construction. If a caller could pass one, the actor
    would be choosing what it is accountable as, and `C-11` refuses "a role
    that also grants access" for the same reason it refuses this: an
    accountability the subject selects is not one.
    """
    try:
        return ACTION_OWNER[action]
    except (KeyError, TypeError):
        raise AuthorityError(
            "action %r owns no role, so no principal can be named accountable "
            "for it. Add it to ACTION_OWNER with the role that answers for it; "
            "a default owner is how an accountable action acquires an "
            "unaccountable one.\nKnown actions: %s"
            % (action, ", ".join(sorted(ACTION_OWNER)))
        )


def risk_of(action: str) -> str:
    """The tier this action decides at. Unknown actions raise via `role_for`."""
    role_for(action)
    return "high" if action in HIGH_RISK_ACTIONS else "routine"


def assert_grants_nothing(role: str) -> None:
    """Prove a role confers no reach, rather than asserting it in prose.

    A role's whole record here is a sentence of accountability. This function
    exists so a test can fail if that ever stops being true — if somebody adds
    a permission list, a scope, a capability set or a tool allowlist to a role,
    the shape check below breaks and the separation is defended by the suite
    rather than by review.
    """
    if role not in ROLES:
        raise AuthorityError(
            "unknown role %r. The six are: %s" % (role, ", ".join(sorted(ROLES)))
        )
    record = ROLES[role]
    if not isinstance(record, str):
        raise AuthorityError(
            "role %r carries a structured record (%s). A role is an "
            "accountability and nothing else: `SC-MTH-005` requires that it "
            "carry no permission, confer no reach, and appear in no "
            "authorization table. Whatever was added belongs on a capability "
            "profile, which confers no authority, or in the platform's own "
            "authorization tables, which no role appears in."
            % (role, type(record).__name__)
        )


def decide(
    *,
    action: str,
    principal: str,
    outcome: str,
    subject: str,
    subject_digest: str = "",
    produced_by: Optional[str] = None,
    rationale: str = "",
    factors: Optional[Mapping[str, Any]] = None,
    pinned_versions: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """One accountable decision, or a refusal.

    Returns the record to be appended — it does not append. Writing is the
    control plane's, because a decision stored where the deciding agent can
    edit it is a decision the agent made twice.
    """
    role = role_for(action)
    risk = risk_of(action)

    if not isinstance(principal, str) or not principal.strip():
        raise AuthorityError(
            "action %r needs a principal; an unattributed decision is not an "
            "accountable one" % action
        )
    principal = principal.strip()

    if outcome not in OUTCOMES:
        raise AuthorityError(
            "outcome %r is not one of the five a gate may resolve to (%s). "
            "There is deliberately no `pass`: `C-11` refuses an implicit one, "
            "and `continue` is a decision somebody is named for."
            % (outcome, ", ".join(OUTCOMES))
        )

    if not isinstance(subject, str) or not subject.strip():
        raise AuthorityError("a decision needs a subject to be about")

    if produced_by is not None and str(produced_by).strip() == principal:
        raise AuthorityError(
            "principal %r produced the subject it is deciding on. `C-11`: no "
            "actor approves its own work, and `C-13` makes the independence an "
            "identity boundary rather than a review convention -- if the same "
            "credentials produce and verify, the separation exists only in "
            "documentation." % principal
        )

    if risk == "high" and principal == AUTOMATED_PRINCIPAL:
        raise AuthorityError(
            "action %r is high risk and shall never continue automatically. "
            "Only a named accountable human holding %r may decide it. Routine "
            "change proceeding under monitoring is a different tier, and this "
            "is not it." % (action, role)
        )

    return {
        "schema_version": "1",
        "action": action,
        "role": role,
        "risk": risk,
        "principal": principal,
        "outcome": outcome,
        "subject": subject,
        "subject_digest": str(subject_digest or ""),
        "produced_by": str(produced_by or "") or None,
        "rationale": str(rationale or ""),
        "factors": dict(factors or {}),
        "pinned_versions": dict(pinned_versions or {}),
    }


def actions_owned_by(role: str) -> Iterable[str]:
    """Which actions this role answers for. Reporting, not authorization."""
    assert_grants_nothing(role)
    return sorted(a for a, owner in ACTION_OWNER.items() if owner == role)
