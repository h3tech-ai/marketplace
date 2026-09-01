#!/usr/bin/env python3
# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
"""The deterministic runtime selector and its project policy (Epic #339, #343).

Source of truth: docs/proposals/governed-runtime-pilot.md 2.2, 8.1, 8.2, 9, 11.
Where this module and that document disagree, the document wins.

Given a project policy, a required capability profile, a profile registry and
an availability snapshot, `select_runtime` returns the chosen adapter profile
AND a structured explanation of why: which profiles were eligible, which were
rejected, and for what reason. The explanation is not a debugging aid, it is
the product surface described in 8.1 ("the selected profile and placement and
why it was selected").

Four properties are structural rather than documented.

1. **Determinism.** Same inputs, same selection and same explanation, always.
   That is a conformance row (11, "Selector determinism"), so it is made true
   by construction: candidates are ordered by one explicit total order
   (`(rank, profile_id)`), nothing iterates a set, and every mapping the
   policy holds is stored as a sorted tuple of pairs. There is no clock, no
   filesystem read, and no randomness on the selection path.

2. **Preference never widens authority** (8.2, `SP-AUT-004`). `allowed_profiles`
   is the authority ceiling for all three modes. A preference or a pin naming a
   profile outside it is refused, never honoured. Pin maps to the Platform's
   `runtime:request` permission; Auto and Prefer resolve under `agent:dispatch`.

3. **No silent degradation** (EP-12, success criterion in 2.4). Every path out
   of this module is one of: a selection with its reasons, an explicit denial
   with an actionable reason, or an explicit inert result. There is no fourth
   path where a requirement quietly stops applying.

4. **`capability_profile` is the policy key.** Routing rules are written
   against the four capability profiles, never against the nine role names.
   `runtime_contracts.DISPATCH_CAPABILITY_PROFILE` resolves role to profile;
   a policy that named roles would have to be rewritten when the roster moves,
   and the roster is explicitly not this pilot's business.

The selector does not dispatch, mint attempts, sign anything, or talk to the
control plane. It is a pure function over its inputs: the kernel mints and
binds (#344), the bridge executes (#345), the CP coordinates (#350). That
purity is what the determinism row needs.

Gated on `build_mode: spq`. On scrum and kanban the selector is inert and says
so; those lifecycles keep their existing dispatch path untouched (2.3).

Python 3.9 compatible: this file is projected into every host package.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import runtime_contracts as rc

# The `runtimes:` section this module understands. A project declaring a
# version we cannot read fails CLOSED (inert, with the reason named) rather
# than being misread by a parser written against an older shape.
POLICY_SCHEMA_VERSION = 1

CONFIG_SECTION = "runtimes"

# The lifecycle this pilot is scoped to. Scrum and Kanban are heading for
# retirement and the pilot must not depend on either (2.3).
GOVERNED_BUILD_MODE = "spq"

# SP-INT-019's remediation preference, adopted as fallback policy (8.2).
# `runtime_contracts.FAILURE_CLASSES` already carries the order in its first
# seven entries; restating it here would create a second copy to drift.
REMEDIATION_ORDER = rc.FAILURE_CLASSES[:7]

# "Cheaper remediations (environment, deterministic check, context) are
# considered before rerouting to another runtime" (8.2). Those three are the
# order's head; a reroute prompted by one of them is out of order and records
# a reason. The proposal names exactly these three, so the boundary is taken
# from its words rather than widened by inference.
CHEAPER_REMEDIATIONS = REMEDIATION_ORDER[:3]

# The one failure outside the remediation ladder that a reroute is the correct
# response to: the runtime itself died, so remediating in place cannot help.
# Every other class outside the ladder (cancelled-by-human, budget-exhausted,
# unclassified) reroutes out of order and must say why.
RUNTIME_ATTRIBUTABLE_FAILURES = ("runtime-death",)

# Rejection codes. Closed vocabulary so a caller can branch on the reason
# without matching on prose; the accompanying detail carries the specifics.
REJECT_NOT_ALLOWED = "not-allowed-by-policy"
REJECT_UNKNOWN_PROFILE = "unknown-profile"
REJECT_MALFORMED_PROFILE = "malformed-profile"
REJECT_CAPABILITY_PROFILE = "capability-profile-not-served"
REJECT_PLACEMENT = "placement-not-allowed"
REJECT_MISSING_CAPABILITIES = "missing-capabilities"
REJECT_UNAVAILABLE = "unavailable"

# Denial codes. A denial is the whole selection failing, not one candidate.
DENY_NO_ELIGIBLE = "no-eligible-profile"
DENY_UNKNOWN_MODE = "unknown-selection-mode"
DENY_UNKNOWN_CAPABILITY_PROFILE = "unknown-capability-profile"
DENY_PIN_UNSATISFIABLE = "pinned-profile-unsatisfiable"
DENY_PIN_MISSING = "pin-requires-a-pinned-profile"
DENY_PREFER_MISSING = "prefer-requires-a-preferred-profile"
DENY_FALLBACK_DISABLED = "preferred-ineligible-and-fallback-disabled"
DENY_REROUTE_REFUSED = "out-of-order-reroute-refused"
DENY_REROUTE_BUDGET = "reroute-budget-exhausted"

_PROFILE_ID_RE = rc.PROFILE_ID_RE

_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*)\s*:\s*(.*)$")


# ─── Explanation objects ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Rejection:
    """One reason, actionable by a human, that something was refused."""

    code: str
    detail: str

    def __str__(self) -> str:
        return "%s: %s" % (self.code, self.detail)

    def to_dict(self) -> Dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class ProfileVerdict:
    """What the selector decided about one candidate profile, and why.

    `rank` is the candidate's position in the routing order and is the primary
    key of the total order the selector walks. Rejections are collected, not
    short-circuited: a caller fixing one requirement deserves to see the other
    two rather than discovering them one deploy at a time.
    """

    profile_id: str
    rank: int
    eligible: bool
    rejections: Tuple[Rejection, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "rank": self.rank,
            "eligible": self.eligible,
            "rejections": [r.to_dict() for r in self.rejections],
        }


@dataclass(frozen=True)
class SelectionResult:
    """The chosen profile and the complete account of how it was chosen.

    Exactly one of three shapes: `inert` (this lifecycle or project does not
    govern runtime selection), `denied` (an explicit refusal carrying an
    actionable reason), or a selection. There is deliberately no shape that
    means "we quietly did something less than you asked for" (EP-12).
    """

    mode: str
    role: str
    capability_profile: str
    selected: Optional[str] = None
    placement: Optional[str] = None
    runtime_family: Optional[str] = None
    inert: bool = False
    denied: bool = False
    denial: Optional[Rejection] = None
    considered: Tuple[ProfileVerdict, ...] = ()
    required_capabilities: Tuple[str, ...] = ()
    routing_order: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()
    reroute_out_of_order: bool = False

    @property
    def eligible(self) -> Tuple[str, ...]:
        """Every eligible profile, in routing order."""
        return tuple(v.profile_id for v in self.considered if v.eligible)

    @property
    def rejected(self) -> Tuple[Tuple[str, Tuple[Rejection, ...]], ...]:
        """Every rejected profile with its reasons, in routing order."""
        return tuple(
            (v.profile_id, v.rejections) for v in self.considered if not v.eligible
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "role": self.role,
            "capability_profile": self.capability_profile,
            "selected": self.selected,
            "placement": self.placement,
            "runtime_family": self.runtime_family,
            "inert": self.inert,
            "denied": self.denied,
            "denial": self.denial.to_dict() if self.denial else None,
            "considered": [v.to_dict() for v in self.considered],
            "eligible": list(self.eligible),
            "required_capabilities": list(self.required_capabilities),
            "routing_order": list(self.routing_order),
            "notes": list(self.notes),
            "reroute_out_of_order": self.reroute_out_of_order,
        }

    def explain(self) -> str:
        """The 8.1 surface: why this profile, and what limited the choice."""
        lines: List[str] = []
        if self.inert:
            lines.append("runtime selection is inert")
        elif self.denied and self.denial is not None:
            lines.append("denied (%s): %s" % (self.denial.code, self.denial.detail))
        else:
            lines.append(
                "selected %s (%s, %s) for capability profile %r in %s mode"
                % (
                    self.selected,
                    self.runtime_family,
                    self.placement,
                    self.capability_profile,
                    self.mode,
                )
            )
        for note in self.notes:
            lines.append("  note: %s" % note)
        for profile_id, rejections in self.rejected:
            lines.append(
                "  rejected %s: %s"
                % (profile_id, "; ".join(str(r) for r in rejections))
            )
        return "\n".join(lines)


# ─── Policy ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RuntimePolicy:
    """The project's `runtimes:` policy, gated on `build_mode: spq`.

    Mappings are held as sorted tuples of pairs, not dicts, so that no code
    path can iterate them in an order that depends on how the YAML happened to
    be authored. `allowed_profiles` keeps its authored order because that order
    IS the routing order (8.2), and it is the one place authoring order is
    meaningful.
    """

    enabled: bool = False
    policy_version: int = POLICY_SCHEMA_VERSION
    allowed_profiles: Tuple[str, ...] = ()
    allowed_placements: Tuple[str, ...] = rc.PLACEMENTS
    preferences: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()
    required_capabilities: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()
    fallback_enabled: bool = True
    allow_out_of_order_reroute: bool = True
    max_reroutes: int = 2
    problems: Tuple[str, ...] = ()

    def preferred_for(self, capability_profile: str) -> Tuple[str, ...]:
        for key, value in self.preferences:
            if key == capability_profile:
                return value
        return ()

    def required_for(self, capability_profile: str) -> Tuple[str, ...]:
        for key, value in self.required_capabilities:
            if key == capability_profile:
                return value
        return ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "policy_version": self.policy_version,
            "allowed_profiles": list(self.allowed_profiles),
            "allowed_placements": list(self.allowed_placements),
            "preferences": {k: list(v) for k, v in self.preferences},
            "required_capabilities": {
                k: list(v) for k, v in self.required_capabilities
            },
            "fallback_enabled": self.fallback_enabled,
            "allow_out_of_order_reroute": self.allow_out_of_order_reroute,
            "max_reroutes": self.max_reroutes,
            "problems": list(self.problems),
        }


@dataclass(frozen=True)
class Availability:
    """One entry of the availability snapshot the bridge probes for (#345)."""

    profile_id: str
    available: bool
    detail: str = ""


@dataclass(frozen=True)
class SelectionRequest:
    """What the caller needs run, and under which selection mode.

    `role` is one of the nine; it resolves to a capability profile through the
    frozen dispatch projection. `capability_profile` overrides that resolution
    for callers that already hold one (the kernel stamps both onto the
    envelope, so it does).

    The three reroute fields are set only when this selection follows a failed
    attempt. They are what lets the selector apply SP-INT-019's ordering
    instead of treating every retry as a fresh choice.
    """

    role: str = ""
    capability_profile: str = ""
    required_capabilities: Tuple[str, ...] = ()
    mode: str = "auto"
    preferred_profile: str = ""
    pinned_profile: str = ""
    prior_profile: str = ""
    prior_failure_class: str = ""
    reroute_count: int = 0


# ─── Selection ───────────────────────────────────────────────────────────────


def capability_profile_for_role(role: str) -> str:
    """Resolve a dispatched role to its capability profile, or "" if unknown."""
    return rc.DISPATCH_CAPABILITY_PROFILE.get(str(role or "").strip().lower(), "")


def select_runtime(
    *,
    build_mode: str,
    policy: RuntimePolicy,
    request: SelectionRequest,
    profiles: Any = (),
    availability: Any = (),
) -> SelectionResult:
    """Choose an adapter profile, and account for the choice.

    `profiles` is the certified profile registry (the shape of
    `core/runtime-fixtures/profiles.json`), as a sequence of records or a
    mapping keyed by profile id. `availability` is the probe snapshot, as a
    sequence of `Availability`, a mapping of profile id to bool, or a mapping
    of profile id to a record with `available` / `detail`.

    A profile absent from the snapshot is unavailable, not assumed present:
    an unprobed runtime that turns out to be missing is exactly the silent
    degradation EP-12 forbids.
    """
    role = str(request.role or "").strip().lower()
    mode = str(request.mode or "").strip().lower()

    # Inert before anything else: on a non-SPQ lifecycle, or on a project that
    # has not opted into runtime federation, this module has no opinion and
    # says so. Callers keep their existing dispatch path.
    if str(build_mode or "").strip().lower() != GOVERNED_BUILD_MODE:
        return SelectionResult(
            mode=mode,
            role=role,
            capability_profile=request.capability_profile or capability_profile_for_role(role),
            inert=True,
            notes=(
                "build_mode is %r, not %r: runtime selection does not apply to "
                "this lifecycle" % (build_mode, GOVERNED_BUILD_MODE),
            ),
        )
    if not policy.enabled:
        return SelectionResult(
            mode=mode,
            role=role,
            capability_profile=request.capability_profile or capability_profile_for_role(role),
            inert=True,
            notes=(
                "the project declares no enabled `runtimes:` policy: runtime "
                "selection stays inert and dispatch keeps its existing path",
            )
            + policy.problems,
        )

    capability_profile = str(request.capability_profile or "").strip().lower()
    if not capability_profile:
        capability_profile = capability_profile_for_role(role)

    if mode not in rc.SELECTION_MODES:
        return _denied(
            mode=mode,
            role=role,
            capability_profile=capability_profile,
            denial=Rejection(
                DENY_UNKNOWN_MODE,
                "%r is not a selection mode; expected one of %s"
                % (request.mode, ", ".join(rc.SELECTION_MODES)),
            ),
        )

    if capability_profile not in rc.CAPABILITY_PROFILES:
        return _denied(
            mode=mode,
            role=role,
            capability_profile=capability_profile,
            denial=Rejection(
                DENY_UNKNOWN_CAPABILITY_PROFILE,
                "role %r resolves to no capability profile and none was given; "
                "expected one of %s"
                % (request.role, ", ".join(rc.CAPABILITY_PROFILES)),
            ),
        )

    registry, registry_notes = _index_profiles(profiles)
    snapshot = _index_availability(availability)
    required = tuple(
        sorted(
            {str(c).strip() for c in request.required_capabilities if str(c).strip()}
            | set(policy.required_for(capability_profile))
        )
    )

    order, order_notes = _routing_order(policy, capability_profile)
    notes: List[str] = list(registry_notes) + list(order_notes)

    candidates = _candidates(policy, order, registry, request)
    considered = tuple(
        _verdict(
            profile_id=profile_id,
            rank=rank,
            policy=policy,
            registry=registry,
            snapshot=snapshot,
            capability_profile=capability_profile,
            required=required,
        )
        for rank, profile_id in candidates
    )
    by_id = {v.profile_id: v for v in considered}
    eligible = [v.profile_id for v in considered if v.eligible]

    base = dict(
        mode=mode,
        role=role,
        capability_profile=capability_profile,
        considered=considered,
        required_capabilities=required,
        routing_order=order,
    )

    # Reroute budget is checked before mode resolution: an exhausted budget is
    # a refusal to choose at all, not a refusal of one candidate.
    if request.reroute_count and request.reroute_count >= policy.max_reroutes:
        return _denied(
            denial=Rejection(
                DENY_REROUTE_BUDGET,
                "%d reroutes already attempted and runtimes.fallback.max_reroutes "
                "is %d; classify the failure and remediate rather than rerouting "
                "again" % (request.reroute_count, policy.max_reroutes),
            ),
            notes=tuple(notes),
            **base
        )

    if mode == "pin":
        return _select_pin(request, by_id, eligible, notes, base, policy, registry)
    if mode == "prefer":
        return _select_prefer(request, by_id, eligible, notes, base, policy, registry)
    return _select_auto(request, by_id, eligible, notes, base, policy, registry)


def _select_auto(request, by_id, eligible, notes, base, policy, registry):
    if not eligible:
        return _denied(
            denial=Rejection(
                DENY_NO_ELIGIBLE,
                _no_eligible_detail(base["capability_profile"], base["considered"]),
            ),
            notes=tuple(notes),
            **base
        )
    chosen = eligible[0]
    notes.append(
        "auto selected the first eligible profile in the routing order (%s)"
        % ", ".join(base["routing_order"] or ("<empty>",))
    )
    return _finish(chosen, request, notes, base, policy, registry)


def _select_prefer(request, by_id, eligible, notes, base, policy, registry):
    preferred = str(request.preferred_profile or "").strip()
    if not preferred:
        for candidate in policy.preferred_for(base["capability_profile"]):
            if candidate in policy.allowed_profiles:
                preferred = candidate
                break
    if not preferred:
        # Quietly behaving as auto would be a mode that stops meaning anything
        # the moment the policy is incomplete, which is the shape EP-12 warns
        # about. Name the gap instead.
        return _denied(
            denial=Rejection(
                DENY_PREFER_MISSING,
                "prefer mode names no profile: pass preferred_profile, or give "
                "runtimes.preferences.%s an entry inside runtimes.allowed_profiles"
                % base["capability_profile"],
            ),
            notes=tuple(notes),
            **base
        )

    verdict = by_id.get(preferred)
    if verdict is not None and verdict.eligible:
        notes.append("preferred profile %s is eligible and was selected" % preferred)
        return _finish(preferred, request, notes, base, policy, registry)

    why = (
        "; ".join(str(r) for r in verdict.rejections)
        if verdict is not None
        else "%s: not a candidate under this policy" % REJECT_UNKNOWN_PROFILE
    )
    if not policy.fallback_enabled:
        return _denied(
            denial=Rejection(
                DENY_FALLBACK_DISABLED,
                "preferred profile %s is not eligible (%s) and "
                "runtimes.fallback.enabled is false" % (preferred, why),
            ),
            notes=tuple(notes),
            **base
        )
    if not eligible:
        return _denied(
            denial=Rejection(
                DENY_NO_ELIGIBLE,
                "preferred profile %s is not eligible (%s) and no allowed profile "
                "is either" % (preferred, why),
            ),
            notes=tuple(notes),
            **base
        )
    chosen = eligible[0]
    notes.append(
        "preferred profile %s is not eligible (%s); fell back to %s under "
        "runtimes.fallback" % (preferred, why, chosen)
    )
    return _finish(chosen, request, notes, base, policy, registry)


def _select_pin(request, by_id, eligible, notes, base, policy, registry):
    pinned = str(request.pinned_profile or "").strip()
    if not pinned:
        return _denied(
            denial=Rejection(
                DENY_PIN_MISSING,
                "pin mode names no profile: pass pinned_profile, or use auto or "
                "prefer",
            ),
            notes=tuple(notes),
            **base
        )
    verdict = by_id.get(pinned)
    if verdict is not None and verdict.eligible:
        notes.append("pinned profile %s satisfies every requirement" % pinned)
        return _finish(pinned, request, notes, base, policy, registry)
    why = (
        "; ".join(str(r) for r in verdict.rejections)
        if verdict is not None
        else "%s: not a candidate under this policy" % REJECT_UNKNOWN_PROFILE
    )
    # Pin never substitutes (8.2). Naming the alternatives is still useful to
    # the human, who can re-issue the request under another mode.
    if eligible:
        notes.append(
            "pin does not substitute; %s would be eligible under auto"
            % ", ".join(eligible)
        )
    return _denied(
        denial=Rejection(
            DENY_PIN_UNSATISFIABLE,
            "pinned profile %s does not satisfy every requirement (%s)"
            % (pinned, why),
        ),
        notes=tuple(notes),
        **base
    )


def _finish(chosen, request, notes, base, policy, registry):
    record = (registry or {}).get(chosen, {})
    out_of_order, reason = _reroute_verdict(request, chosen)
    if out_of_order:
        if not policy.allow_out_of_order_reroute:
            return _denied(
                denial=Rejection(DENY_REROUTE_REFUSED, reason),
                notes=tuple(notes),
                **base
            )
        notes.append(reason)
    return SelectionResult(
        selected=chosen,
        placement=str(record.get("placement") or "") or None,
        runtime_family=str(record.get("runtime_family") or "") or None,
        notes=tuple(notes),
        reroute_out_of_order=out_of_order,
        **base
    )


def _reroute_verdict(request: SelectionRequest, chosen: str) -> Tuple[bool, str]:
    """Is rerouting to `chosen` in SP-INT-019 order, and if not, why not?

    Only a change of runtime counts as a reroute. Re-selecting the same profile
    after a failure is a retry in place, which is what the cheap remediations
    are FOR, so it is never out of order.
    """
    prior = str(request.prior_profile or "").strip()
    failure = str(request.prior_failure_class or "").strip()
    if not prior or not failure or prior == chosen:
        return False, ""
    if failure in CHEAPER_REMEDIATIONS:
        return True, (
            "out-of-order reroute: the prior attempt failed with %r, which "
            "SP-INT-019 orders ahead of rerouting (%s come first); remediate "
            "in place on %s before moving to %s"
            % (failure, ", ".join(CHEAPER_REMEDIATIONS), prior, chosen)
        )
    if failure in REMEDIATION_ORDER or failure in RUNTIME_ATTRIBUTABLE_FAILURES:
        return False, ""
    return True, (
        "out-of-order reroute: the prior attempt's failure class %r is outside "
        "the v%d remediation order, so rerouting from %s to %s is not a ranked "
        "remediation" % (failure, rc.FAILURE_CLASS_VERSION, prior, chosen)
    )


def _denied(*, mode, role, capability_profile, denial, considered=(), notes=(),
            required_capabilities=(), routing_order=()) -> SelectionResult:
    return SelectionResult(
        mode=mode,
        role=role,
        capability_profile=capability_profile,
        denied=True,
        denial=denial,
        considered=tuple(considered),
        required_capabilities=tuple(required_capabilities),
        routing_order=tuple(routing_order),
        notes=tuple(notes),
    )


def _no_eligible_detail(capability_profile: str, considered) -> str:
    if not considered:
        return (
            "no profile is allowed for capability profile %r; add one to "
            "runtimes.allowed_profiles" % capability_profile
        )
    return "no allowed profile is eligible for capability profile %r: %s" % (
        capability_profile,
        "; ".join(
            "%s (%s)" % (v.profile_id, ", ".join(r.code for r in v.rejections))
            for v in considered
        ),
    )


def _verdict(
    *,
    profile_id: str,
    rank: int,
    policy: RuntimePolicy,
    registry: Mapping[str, Mapping[str, Any]],
    snapshot: Mapping[str, Availability],
    capability_profile: str,
    required: Tuple[str, ...],
) -> ProfileVerdict:
    """Every reason this candidate cannot serve this dispatch. Empty = eligible.

    Rules are evaluated in one fixed sequence and their reasons collected, so
    the same candidate always produces the same rejection list in the same
    order. An unknown profile short-circuits because the remaining rules have
    nothing to read.
    """
    rejections: List[Rejection] = []

    if profile_id not in policy.allowed_profiles:
        rejections.append(
            Rejection(
                REJECT_NOT_ALLOWED,
                "not listed in runtimes.allowed_profiles (%s)"
                % (", ".join(policy.allowed_profiles) or "<empty>"),
            )
        )

    record = registry.get(profile_id)
    if record is None:
        rejections.append(
            Rejection(
                REJECT_UNKNOWN_PROFILE,
                "no certified profile record with this id is registered",
            )
        )
        return ProfileVerdict(profile_id, rank, False, tuple(rejections))

    problems = rc.validate_profile(record)
    if problems:
        rejections.append(
            Rejection(REJECT_MALFORMED_PROFILE, "; ".join(problems))
        )
        return ProfileVerdict(profile_id, rank, False, tuple(rejections))

    served = tuple(record.get("capability_profiles") or ())
    if capability_profile not in served:
        rejections.append(
            Rejection(
                REJECT_CAPABILITY_PROFILE,
                "serves %s, not %r"
                % (", ".join(sorted(served)) or "<none>", capability_profile),
            )
        )

    placement = str(record.get("placement") or "")
    if placement not in policy.allowed_placements:
        rejections.append(
            Rejection(
                REJECT_PLACEMENT,
                "placement %r is not in runtimes.allowed_placements (%s)"
                % (placement, ", ".join(policy.allowed_placements) or "<empty>"),
            )
        )

    capabilities = {str(c) for c in (record.get("capabilities") or ())}
    missing = tuple(c for c in required if c not in capabilities)
    if missing:
        rejections.append(
            Rejection(
                REJECT_MISSING_CAPABILITIES,
                "does not provide %s" % ", ".join(missing),
            )
        )

    probe = snapshot.get(profile_id)
    if probe is None:
        rejections.append(
            Rejection(
                REJECT_UNAVAILABLE,
                "absent from the availability snapshot; an unprobed runtime is "
                "treated as unavailable rather than assumed present",
            )
        )
    elif not probe.available:
        rejections.append(
            Rejection(
                REJECT_UNAVAILABLE,
                probe.detail or "reported unavailable by the availability probe",
            )
        )

    return ProfileVerdict(profile_id, rank, not rejections, tuple(rejections))


def _routing_order(
    policy: RuntimePolicy, capability_profile: str
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """The order Auto walks, and any notes about preferences that were ignored.

    Per-capability-profile preferences reorder within `allowed_profiles`; they
    cannot add to it. A preference naming a profile outside the allowlist is
    dropped with a note, because honouring it would let a preference widen
    authority (8.2).
    """
    order: List[str] = []
    notes: List[str] = []
    allowed = policy.allowed_profiles
    for candidate in policy.preferred_for(capability_profile):
        if candidate not in allowed:
            notes.append(
                "runtimes.preferences.%s names %s, which is not in "
                "runtimes.allowed_profiles; ignored, because a preference cannot "
                "widen authority" % (capability_profile, candidate)
            )
            continue
        if candidate not in order:
            order.append(candidate)
    for candidate in allowed:
        if candidate not in order:
            order.append(candidate)
    return tuple(order), tuple(notes)


def _candidates(
    policy: RuntimePolicy,
    order: Tuple[str, ...],
    registry: Mapping[str, Mapping[str, Any]],
    request: SelectionRequest,
) -> Tuple[Tuple[int, str], ...]:
    """Every profile worth a verdict, under one explicit total order.

    The key is `(rank, profile_id)`: rank from the routing order, profile id as
    the tie-break for candidates the policy never ranked. Registered but
    unallowed profiles are still considered so that a pin or preference naming
    one gets "not allowed by policy" rather than a bare "unknown profile", and
    so the explanation shows what the allowlist excluded.
    """
    ranks = {profile_id: index for index, profile_id in enumerate(order)}
    unranked = len(order)
    names = set(order) | set(registry)
    for extra in (request.preferred_profile, request.pinned_profile, request.prior_profile):
        extra = str(extra or "").strip()
        if extra:
            names.add(extra)
    return tuple(
        sorted(
            ((ranks.get(name, unranked), name) for name in names),
            key=lambda pair: (pair[0], pair[1]),
        )
    )


def _index_profiles(profiles: Any) -> Tuple[Dict[str, Mapping[str, Any]], Tuple[str, ...]]:
    """Normalize the registry to `{profile_id: record}`, first record wins."""
    records: List[Mapping[str, Any]] = []
    if isinstance(profiles, Mapping):
        for key in sorted(profiles):
            value = profiles[key]
            if isinstance(value, Mapping):
                merged = dict(value)
                merged.setdefault("profile_id", key)
                records.append(merged)
    else:
        for value in profiles or ():
            if isinstance(value, Mapping):
                records.append(value)
    index: Dict[str, Mapping[str, Any]] = {}
    notes: List[str] = []
    for record in records:
        profile_id = str(record.get("profile_id") or "").strip()
        if not profile_id:
            notes.append("a profile record carries no profile_id and was ignored")
            continue
        if profile_id in index:
            notes.append(
                "profile %s is registered more than once; the first record wins"
                % profile_id
            )
            continue
        index[profile_id] = record
    return index, tuple(notes)


def _index_availability(availability: Any) -> Dict[str, Availability]:
    """Normalize the probe snapshot to `{profile_id: Availability}`."""
    index: Dict[str, Availability] = {}
    if isinstance(availability, Mapping):
        for key in sorted(availability):
            value = availability[key]
            if isinstance(value, Availability):
                index[str(key)] = value
            elif isinstance(value, Mapping):
                index[str(key)] = Availability(
                    profile_id=str(key),
                    available=bool(value.get("available")),
                    detail=str(value.get("detail") or ""),
                )
            else:
                index[str(key)] = Availability(str(key), bool(value))
        return index
    for entry in availability or ():
        if isinstance(entry, Availability):
            index[entry.profile_id] = entry
        elif isinstance(entry, Mapping):
            profile_id = str(entry.get("profile_id") or "").strip()
            if profile_id:
                index[profile_id] = Availability(
                    profile_id=profile_id,
                    available=bool(entry.get("available")),
                    detail=str(entry.get("detail") or ""),
                )
        elif isinstance(entry, str):
            index[entry] = Availability(entry, True)
    return index


# ─── Config: the `runtimes:` section of .synaptory.yaml ───────────────────────


def read_build_mode(project_dir: Any) -> str:
    """The project's declared build_mode, or "scrum" when unset (the default)."""
    return _build_mode_of(_read_config(project_dir))


def load_policy(project_dir: Any) -> RuntimePolicy:
    """Read the project's runtime policy from `.synaptory.yaml`.

    Returns a disabled policy (never an exception) when the file is absent, the
    lifecycle is not SPQ, the section is missing, or the section declares a
    version this module cannot read. Every one of those carries its reason in
    `problems`, and a disabled policy makes `select_runtime` inert rather than
    denying: an existing SPQ project that never opted in must keep working
    (objective 10).
    """
    return parse_policy(_read_config(project_dir))


def parse_policy(text: str) -> RuntimePolicy:
    """Parse a `.synaptory.yaml` body into a `RuntimePolicy`."""
    build_mode = _build_mode_of(text)
    if build_mode != GOVERNED_BUILD_MODE:
        return RuntimePolicy(
            problems=(
                "build_mode is %r, not %r: the `runtimes:` section is not read"
                % (build_mode, GOVERNED_BUILD_MODE),
            )
        )

    section, problems = _parse_section(text, CONFIG_SECTION)
    if section is None:
        return RuntimePolicy(
            problems=tuple(problems)
            + ("no `runtimes:` section in .synaptory.yaml",)
        )

    version = section.get("version", POLICY_SCHEMA_VERSION)
    if not isinstance(version, int) or isinstance(version, bool):
        problems.append("runtimes.version: expected an integer, got %r" % (version,))
        version = -1
    if version != POLICY_SCHEMA_VERSION:
        # Fail closed: a section written against a shape we do not know is not
        # a section we may half-apply.
        return RuntimePolicy(
            policy_version=version if isinstance(version, int) else -1,
            problems=tuple(problems)
            + (
                "runtimes.version %r is not supported (this runtime reads v%d); "
                "runtime selection stays inert"
                % (section.get("version"), POLICY_SCHEMA_VERSION),
            ),
        )

    allowed_profiles: List[str] = []
    for value in _as_list(section.get("allowed_profiles")):
        name = str(value).strip()
        if not _PROFILE_ID_RE.match(name):
            problems.append(
                "runtimes.allowed_profiles: %r is not a valid profile id" % value
            )
            continue
        if name not in allowed_profiles:
            allowed_profiles.append(name)

    raw_placements = _as_list(section.get("allowed_placements"))
    if raw_placements:
        allowed_placements: List[str] = []
        for value in raw_placements:
            name = str(value).strip()
            if name not in rc.PLACEMENTS:
                problems.append(
                    "runtimes.allowed_placements: %r not in %s"
                    % (value, ", ".join(rc.PLACEMENTS))
                )
                continue
            if name not in allowed_placements:
                allowed_placements.append(name)
        placements = tuple(allowed_placements)
    else:
        placements = rc.PLACEMENTS

    preferences = _capability_map(
        section.get("preferences"), "runtimes.preferences", problems
    )
    required = _capability_map(
        section.get("required_capabilities"),
        "runtimes.required_capabilities",
        problems,
    )

    fallback = section.get("fallback")
    fallback = fallback if isinstance(fallback, dict) else {}
    fallback_enabled = _as_bool(fallback.get("enabled"), True)
    allow_out_of_order = _as_bool(fallback.get("allow_out_of_order_reroute"), True)
    max_reroutes = fallback.get("max_reroutes", 2)
    if not isinstance(max_reroutes, int) or isinstance(max_reroutes, bool) or max_reroutes < 0:
        problems.append(
            "runtimes.fallback.max_reroutes: expected a non-negative integer, "
            "got %r; using 2" % (max_reroutes,)
        )
        max_reroutes = 2

    enabled = _as_bool(section.get("enabled"), True)
    if enabled and not allowed_profiles:
        problems.append(
            "runtimes.allowed_profiles is empty; runtime selection stays inert "
            "and dispatch keeps its existing path"
        )
        enabled = False

    return RuntimePolicy(
        enabled=enabled,
        policy_version=POLICY_SCHEMA_VERSION,
        allowed_profiles=tuple(allowed_profiles),
        allowed_placements=placements,
        preferences=preferences,
        required_capabilities=required,
        fallback_enabled=fallback_enabled,
        allow_out_of_order_reroute=allow_out_of_order,
        max_reroutes=max_reroutes,
        problems=tuple(problems),
    )


def _capability_map(
    raw: Any, label: str, problems: List[str]
) -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    """`{capability_profile: [values]}`, sorted, with unknown keys refused.

    Routing rules are written against the four capability profiles, never the
    nine role names (r4 amendment). A key that is a role name is the mistake
    this refusal is for, so the message names the profile it should have been.
    """
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        problems.append("%s: expected a mapping, got %r" % (label, raw))
        return ()
    out: List[Tuple[str, Tuple[str, ...]]] = []
    for key in sorted(raw):
        name = str(key).strip().lower()
        if name not in rc.CAPABILITY_PROFILES:
            hint = rc.DISPATCH_CAPABILITY_PROFILE.get(name, "")
            problems.append(
                "%s.%s: %r is not a capability profile%s"
                % (
                    label,
                    key,
                    key,
                    (" (role %r dispatches as %r)" % (name, hint)) if hint else "",
                )
            )
            continue
        values = tuple(
            str(v).strip() for v in _as_list(raw[key]) if str(v).strip()
        )
        if values:
            out.append((name, values))
    return tuple(out)


def _build_mode_of(text: str) -> str:
    for line in (text or "").splitlines():
        match = re.match(r"^build_mode\s*:\s*(.+)$", line)
        if match:
            return str(_scalar(_strip_comment(match.group(1)).strip())).lower()
    return "scrum"


def _read_config(project_dir: Any) -> str:
    try:
        return (Path(project_dir) / ".synaptory.yaml").read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError, TypeError):
        return ""


def _parse_section(text: str, section: str) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Parse one top-level block of `.synaptory.yaml` into nested dicts/lists.

    A dedicated parser rather than a shared one: `core/lib` carries no PyYAML
    dependency (the tracker's `_parse_yaml_lite` lives under `core/scripts` and
    handles neither sequences nor three-level nesting), and the layouts differ
    per host, so a cross-package import would be the fragile half of this.
    Scope is exactly what `runtimes:` needs: nested mappings and sequences of
    scalars, block or inline. A sequence of mappings is refused with a problem
    rather than half-read, following the SPQ block's warn-do-not-honour rule.
    """
    problems: List[str] = []
    lines: List[Tuple[int, str]] = []
    inside = False
    found = False
    for raw in (text or "").splitlines():
        body = _strip_comment(raw)
        if not body.strip():
            continue
        indent = len(body) - len(body.lstrip())
        content = body.strip()
        if indent == 0:
            if inside:
                break
            match = _KEY_RE.match(content)
            if match and match.group(1) == section:
                inside = True
                found = True
            continue
        if inside:
            lines.append((indent, content))
    if not found:
        return None, problems
    if not lines:
        return {}, problems
    value, _ = _parse_at(lines, 0, lines[0][0], problems, section)
    return (value if isinstance(value, dict) else {}), problems


def _parse_at(
    lines: List[Tuple[int, str]], index: int, indent: int, problems: List[str], path: str
) -> Tuple[Any, int]:
    if lines[index][1].startswith("-"):
        return _parse_sequence(lines, index, indent, problems, path)
    return _parse_mapping(lines, index, indent, problems, path)


def _parse_mapping(
    lines: List[Tuple[int, str]], index: int, indent: int, problems: List[str], path: str
) -> Tuple[Dict[str, Any], int]:
    out: Dict[str, Any] = {}
    while index < len(lines) and lines[index][0] >= indent:
        line_indent, content = lines[index]
        if line_indent > indent:
            problems.append("%s: unexpected indentation at %r" % (path, content))
            index += 1
            continue
        match = _KEY_RE.match(content)
        if not match:
            problems.append("%s: cannot read %r as `key: value`" % (path, content))
            index += 1
            continue
        key, rest = match.group(1), match.group(2).strip()
        child_path = "%s.%s" % (path, key)
        if rest:
            out[key] = _scalar_or_flow(rest)
            index += 1
            continue
        index += 1
        if index < len(lines) and lines[index][0] > line_indent:
            out[key], index = _parse_at(
                lines, index, lines[index][0], problems, child_path
            )
        else:
            out[key] = {}
    return out, index


def _parse_sequence(
    lines: List[Tuple[int, str]], index: int, indent: int, problems: List[str], path: str
) -> Tuple[List[Any], int]:
    out: List[Any] = []
    while index < len(lines) and lines[index][0] == indent:
        content = lines[index][1]
        if not content.startswith("-"):
            break
        item = content[1:].strip()
        index += 1
        if not item:
            problems.append("%s: empty sequence entry" % path)
            continue
        if _KEY_RE.match(item):
            # A sequence of mappings would be half-read by this parser, which
            # would look configured without being configured. Refuse it.
            problems.append(
                "%s: sequences of mappings are not supported here (%r); use a "
                "sequence of scalars" % (path, item)
            )
            while index < len(lines) and lines[index][0] > indent:
                index += 1
            continue
        out.append(_scalar_or_flow(item))
    return out, index


def _strip_comment(line: str) -> str:
    out: List[str] = []
    quote = ""
    for char in line:
        if quote:
            out.append(char)
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
            out.append(char)
            continue
        if char == "#":
            break
        out.append(char)
    return "".join(out).rstrip()


def _scalar_or_flow(raw: str) -> Any:
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part.strip()) for part in inner.split(",") if part.strip()]
    return _scalar(raw)


def _scalar(raw: str) -> Any:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    lowered = value.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "~", ""):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _as_list(raw: Any) -> List[Any]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, (str, int, float, bool)):
        return [raw]
    return []


def _as_bool(raw: Any, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("true", "yes", "on", "1")
