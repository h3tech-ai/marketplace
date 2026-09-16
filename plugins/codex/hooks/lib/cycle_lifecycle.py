"""The four-stage lifecycle, and the three events that are not stages (#644).

    DISCOVERY -> CYCLE -> ACCEPTANCE -> COMPLETE
                   ^          |
                   +----------+   a non-final go-live returns to delivery

Four stages, because `C-02` says Commit, Sync and Checkpoint are **method
events, never stages**, and the predecessor made all three states with `SYNC`
mandatory between execution and Checkpoint. They live in `method_events` now,
which records and cannot decide.

WHAT CHANGED, AND WHAT DID NOT

`ACCEPTANCE -> CYCLE` is the edge that makes `C-15` true. The predecessor had
`ACCEPTANCE -> COMPLETE` and **no row for `COMPLETE` at all** -- which is a
stronger statement than an empty row, because there was no edge to relax and a
non-final go-live was unreachable by configuration. A long engagement ships
several times; only the last Acceptance closes the engagement.

`COMPLETE` stays terminal, and that is not an inconsistency with the above. §5.3:
Acceptance repeats, and the **final** one additionally ends the engagement by
handing over the codebase, the documentation and the operating knowledge. A
lifecycle where `COMPLETE` had an outgoing edge would have no way to say that.

ENGAGEMENT STATE AND CYCLE STATE ARE DIFFERENT THINGS, and conflating them is
what forced the predecessor's single global `current_cycle` scalar. An
engagement is `discovering` / `active` / `closed`; a Cycle is `draft` /
`executing` / `closed`; and **many Cycles and many Acceptance records may exist
while an engagement is active**. That is the whole point of `SC-MTH-012`: N
Engineering Leads means N concurrent Cycles. A project-global pointer cannot
select among them, so every function here takes the Cycle it acts on.

WHAT THIS MODULE REFUSES TO DO

It does not read or write a state file, and it holds no `force` parameter. The
predecessor's `transition(..., force=True)` moved a project from `DISCOVERY` to
`CHECKPOINT` in one call -- skipping Commit, execution and the barrier -- and it
was reachable from any raw module call, which `C-12` refuses as an executor
escape hatch. There is no equivalent here: a transition that cannot be taken is
refused, and recovery is an accountable action through `cycle_authority`, whose
record the deciding agent cannot write. `assert_no_force_parameter` proves the
absence rather than asserting it in prose.
"""

from __future__ import annotations

import inspect
from typing import Any, Dict, List, Mapping, Optional, Sequence

import cycle_authority
import cycle_records

#: The four stages. `CYCLE` is one stage that repeats, not a family of them:
#: Plan -> Build -> Prove happens inside it, continuously, and giving each a
#: stage would put a gate between them that §6 does not sanction.
STAGES = ("DISCOVERY", "CYCLE", "ACCEPTANCE", "COMPLETE")

#: The edges. `CYCLE -> CYCLE` is a legal self-transition: closing one Cycle and
#: opening the next is delivery continuing, not a stage change, and modelling it
#: as a departure and a return would make the trunk look momentarily undelivered.
TRANSITIONS: Dict[str, tuple] = {
    "DISCOVERY": ("CYCLE",),
    "CYCLE": ("CYCLE", "ACCEPTANCE"),
    "ACCEPTANCE": ("CYCLE", "COMPLETE"),
    "COMPLETE": (),
}

ENGAGEMENT_STATES = ("discovering", "active", "closed")
CYCLE_STATES = ("draft", "executing", "closed")


class LifecycleError(ValueError):
    """A transition or a Cycle operation this module refuses."""


def legal_targets(stage: str) -> tuple:
    if stage not in TRANSITIONS:
        raise LifecycleError(
            "%r is not a stage. The four are %s." % (stage, ", ".join(STAGES))
        )
    return TRANSITIONS[stage]


def check_transition(current: str, target: str) -> None:
    """Refuse an illegal stage change, naming what is legal instead.

    No `force`. See the module docstring: the predecessor's escape hatch was
    reachable from any raw call, and `C-12` places a guardrail outside the
    executing agent's reach -- an argument is not outside it.
    """
    targets = legal_targets(current)
    if target not in targets:
        if not targets:
            raise LifecycleError(
                "%s is terminal: the final Acceptance ended the engagement by "
                "handing over the codebase, the documentation and the "
                "operating knowledge, and there is nothing to return to."
                % current
            )
        raise LifecycleError(
            "%s -> %s is not a legal transition. Legal: %s. There is no "
            "override: a transition that cannot be taken is refused, and "
            "recovery is an accountable action recorded where the deciding "
            "agent cannot write it."
            % (current, target, ", ".join(targets))
        )


def is_final_acceptance(*, outstanding_commitments: Sequence[Any], handover_recorded: bool) -> bool:
    """Does this Acceptance close the engagement?

    Not a flag the caller sets. §5.3 makes the final Acceptance the one that
    hands over, so "final" is a property of the state of the engagement rather
    than a claim about intent -- a caller-set flag would let a mid-engagement
    release close it.
    """
    return not list(outstanding_commitments) and bool(handover_recorded)


def acceptance_target(
    *, outstanding_commitments: Sequence[Any], handover_recorded: bool
) -> str:
    """Where an Acceptance goes next: back to delivery, or to COMPLETE."""
    if is_final_acceptance(
        outstanding_commitments=outstanding_commitments,
        handover_recorded=handover_recorded,
    ):
        return "COMPLETE"
    return "CYCLE"


# ─── Cycles, plural, and none of them global ─────────────────────────────────


def open_cycle(declaration: Mapping[str, Any]) -> Dict[str, Any]:
    """Move one Cycle `draft -> executing` by sealing its declaration.

    The Commit EVENT records that this happened; it does not cause it. Sealing
    is what fixes the admitted set, which is why `C-03`'s "no work joins a Cycle
    after Commit" is a property of the sealed document rather than of an event
    anybody could omit.
    """
    sealed = cycle_records.seal(declaration)
    return {
        "cycle_id": sealed["cycle_id"],
        "state": "executing",
        "declaration": sealed,
    }


def admitted_ids(cycle: Mapping[str, Any]) -> List[str]:
    """The set the barrier will range over, read from the sealed declaration.

    Read from the DECLARATION rather than from a board, deliberately. A board
    changes as work progresses; the admitted set does not, and a barrier that
    ranged over the board would range over whatever survived rather than over
    what was promised.
    """
    declaration = cycle.get("declaration") or {}
    return [str(u.get("id")) for u in declaration.get("admitted_units") or [] if u.get("id")]


def select_cycle(cycles: Sequence[Mapping[str, Any]], cycle_id: str) -> Mapping[str, Any]:
    """The Cycle a caller named, or a refusal that lists what is open.

    Explicit selection, always. The predecessor kept one global
    `current_cycle` scalar, which cannot select among N concurrent Cycles -- and
    a UI convenience selection is never authority.
    """
    if not cycle_id:
        raise LifecycleError(
            "name the Cycle to act on. There is no current Cycle: N Engineering "
            "Leads means N concurrent Cycles (`SC-MTH-012`), so a global "
            "pointer would have to pick one of them arbitrarily. Open: %s"
            % (", ".join(str(c.get("cycle_id")) for c in cycles) or "none")
        )
    for cycle in cycles:
        if str(cycle.get("cycle_id")) == cycle_id:
            return cycle
    raise LifecycleError(
        "no Cycle %r. Open: %s"
        % (cycle_id, ", ".join(str(c.get("cycle_id")) for c in cycles) or "none")
    )


def concurrent_regions_conflict(
    cycles: Sequence[Mapping[str, Any]], candidate_region: Sequence[object]
) -> Optional[str]:
    """Which open Cycle's region a candidate would overlap, if any.

    The local half of a check whose authority is the control plane. This
    answers from what the clone can see, and a clone that has seen nothing
    would answer `None` -- which is why the caller must treat an unreadable
    registry as a refusal rather than as an absence of conflict. §5.2: unknown
    registry state refuses new admission, because availability is never
    permission to invent ownership.
    """
    for cycle in cycles:
        if str(cycle.get("state")) == "closed":
            continue
        region = (cycle.get("declaration") or {}).get("source_region") or []
        if not region:
            continue
        if cycle_records.regions_overlap(region, candidate_region):
            return str(cycle.get("cycle_id"))
    return None


# ─── The absences, proved rather than asserted ───────────────────────────────


def assert_no_force_parameter() -> None:
    """No function here takes a `force`, and no test has to trust that.

    The predecessor's `transition(..., force=True)` moved a project from
    `DISCOVERY` straight to `CHECKPOINT`, skipping Commit, execution and the
    barrier, from any raw module call. `C-12` refuses an agent able to disable
    the rules it runs under, and an argument is inside the agent's reach.
    """
    offenders = []
    for name, obj in list(globals().items()):
        if name.startswith("_") or not inspect.isfunction(obj):
            continue
        if obj.__module__ != __name__:
            continue
        params = inspect.signature(obj).parameters
        for bad in ("force", "skip_checks", "override", "no_verify"):
            if bad in params:
                offenders.append("%s(%s=...)" % (name, bad))
    if offenders:
        raise LifecycleError(
            "these functions accept an override, which `C-12` refuses as an "
            "executor escape hatch: %s. Recovery is an accountable action "
            "through `cycle_authority`, recorded where the deciding agent "
            "cannot write it." % ", ".join(offenders)
        )


def recovery_decision(*, principal: str, subject: str, rationale: str) -> Dict[str, Any]:
    """The replacement for `force`: a decision, not an argument.

    Deliberately routed through `cycle_authority.decide`, which refuses an
    unattributed principal and an actor deciding on its own work, and which
    returns a record for the control plane to store rather than storing it
    here. A local write would put the record inside the reach of whatever
    needed recovering.
    """
    if not str(rationale or "").strip():
        raise LifecycleError(
            "a recovery needs a rationale. The predecessor's `force=True` "
            "needed nothing, which is why nothing explains any of the "
            "transitions it took."
        )
    return cycle_authority.decide(
        action="close-cycle",
        principal=principal,
        outcome="escalate",
        subject=subject,
        rationale=rationale,
    )
