#!/usr/bin/env python3
"""The fail-closed advance kernel: the only legal writer of AGENT-PRODUCED
story pipeline transitions.

The qualifier is load-bearing and is ADR-029's #486 amendment. Every edge whose
evidence is a receipt an agent dispatch wrote goes through this module. The
HUMAN acceptance edge `awaiting_acceptance -> done` is a declared exception:
`story_pipeline.accept_story` writes it directly, that is the path all three
hosts specify, and its discipline is `story_pipeline.record_judged_verdict`
rather than anything here. `TRANSITION_RECEIPT` still carries the row because
this module does enforce it for a caller that aims `advance` at that edge, and
because `story_pipeline._RECEIPT_GATED_EDGES` mirrors the pair so the bare
`transition_story` CLI verb refuses it. Read the amendment before assuming a
state write carries this module's guarantees: on that edge there is no
anti-replay, no dispatch binding and no `next_action` agreement.

Before this module the same gate existed three times, with three different check
sets, and the strongest version lived on the least certified host:

    check                     Cursor MCP        Codex MCP        Claude
    canonical receipt path    flat dir only     spec + symlink   hook-bound (#340)
    completed_at ordering     log[-1], skipped  state-matched    none (mtime)
    anti-replay ledger        silent reset      refuses          none
    next_action binding       none              enforced         advisory
    backend binding           none              enforced         none
    DoD on to-done            enforced          MISSING          wrappers only

Hosts now call `evaluate_advance` / `execute_advance` and get the union of every
check. Genuine per-host differences (BAA posture, readiness probes, which backend
a receipt must name) are injected through `HostPolicy` rather than forked into
divergent copies, so a difference has to be declared to exist.

Design rules:

* `evaluate_advance` is pure. It reads state and receipts and returns a verdict;
  it never writes. `execute_advance` is the only function that mutates.
* `execute_advance` performs a single state write. The MCP servers used to
  transition (write) and then append the consumed digest (write again), leaving a
  crash window in which a consumed receipt stayed replayable.
* The receipt file is read exactly once. Its bytes produce both the digest and
  the parsed payload, closing the TOCTOU window in the previous implementations.
* Everything composes existing functions from `story_pipeline`, the state
  machines, and `receipt_validator`. This module adds enforcement, not lifecycle.

Python 3.9 compatible: this file is projected into every host package.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import json
import os
import secrets
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

# The capability-profile alias surface (#399). Imported rather than copied:
# `runtime_contracts` is the dependency leaf of core/lib and owns the 9-to-4
# projection tables, so the kernel resolving a pair through it is the whole
# point of ADR-032 decision 1. It imports nothing from this module, so the
# direction cannot cycle, and every host package ships core/lib whole, so the
# import resolves on all three.
from runtime_contracts import (  # type: ignore
    ACCOUNTABLE_ROLE,
    attested_attempt_claims,
    profile_pair_for_role,
    roles_for_profile_pair,
)
from runtime_contracts import ROLE_NAMES as ROLE_NAMES  # noqa: F401  (re-export)

# ── role tables ──────────────────────────────────────────────────────────────
# `ROLE_NAMES` used to be defined here, and both MCP servers plus
# `runtime_contracts` carried byte-identical copies of it (the last of those
# policed by a Layer-1 mirror test). #402 collapsed the mirror: the alias
# module owns the table, this module re-exports it, and the hosts and tests
# that import it from the kernel keep working.
#
# The two tables below are the WIRE ALIAS view of the pipeline's edges. Since
# #402 the expectation the kernel enforces is the `(stage_profile,
# capability_profile)` pair each edge's alias resolves to through
# `profile_pair_for_role` -- see `expected_stage` -- and these names survive
# because receipts, receipt filenames and the DoD evaluator all still speak
# them (the #198 precedent: renaming a wire value is its own campaign).
#
# The pair does not replace the name, because it cannot: `qe` and `cr` both
# project onto verifying/prover, so the pair names WHAT the evidence must have
# been produced under and the edge names WHICH alias produced it. That is why
# `ROLE_MISMATCH` still compares the receipt's `role` to the edge's alias, and
# why the pair check below is an addition to it rather than a replacement.
TRANSITION_RECEIPT: Dict[Tuple[str, str], Tuple[str, str]] = {
    ("queued", "in_progress"): ("software-engineer", "se"),
    ("in_progress", "testing"): ("software-engineer", "se"),
    ("testing", "reviewing"): ("quality-engineer", "qe"),
    ("reviewing", "done"): ("code-reviewer", "cr"),
    ("reviewing", "awaiting_acceptance"): ("code-reviewer", "cr"),
    ("awaiting_acceptance", "done"): ("project-owner", "po"),
}

BLOCKED_FROM_ROLE: Dict[str, Tuple[str, str]] = {
    "in_progress": ("software-engineer", "se"),
    "testing": ("quality-engineer", "qe"),
    "reviewing": ("code-reviewer", "cr"),
    "awaiting_acceptance": ("project-owner", "po"),
}

# Transitions that land a story on the DoD gate edge.
_DONE_EDGE = ("done", "awaiting_acceptance")

_BUILD_MODES = ("scrum", "kanban", "spq")


# ── decision codes ───────────────────────────────────────────────────────────
# Machine-readable so adapters and the conformance suite can assert on a code
# rather than on prose that drifts per host.

OK = "ok"
POLICY_REFUSED = "policy_refused"
NOT_READY = "not_ready"
BAD_REQUEST = "bad_request"
STORY_NOT_FOUND = "story_not_found"
ILLEGAL_TRANSITION = "illegal_transition"
NEXT_ACTION_MISMATCH = "next_action_mismatch"
NO_BOUND_ROLE = "no_bound_role"
PATH_SYMLINK = "path_symlink"
PATH_ESCAPE = "path_escape"
PATH_NOT_CANONICAL = "path_not_canonical"
NO_RECEIPT = "no_receipt"
RECEIPT_UNREADABLE = "receipt_unreadable"
RECEIPT_INVALID = "receipt_invalid"
STORY_MISMATCH = "story_mismatch"
ROLE_MISMATCH = "role_mismatch"
# #402. The receipt's runtime-identity overlay (#342) names a
# `(stage_profile, capability_profile)` pair that is not the pair this edge
# expects. Distinct from ROLE_MISMATCH on purpose: the role name says who wrote
# the receipt, the pair says what kind of act it attests to, and the control
# plane reads the second one (#407). A stage that could self-declare the pair
# could have a producing dispatch ingested as verification evidence.
PROFILE_MISMATCH = "profile_mismatch"
# #402. `accountable_role` is outside the alias table entirely, so it names no
# role at all. Separate from PROFILE_MISMATCH because the operator fix differs:
# a typo'd or retired name (`product-manager`) is corrected at the producer,
# while a mismatched pair means the wrong stage produced the receipt.
ACCOUNTABLE_ROLE_INVALID = "accountable_role_invalid"
BACKEND_MISMATCH = "backend_mismatch"
RUNTIME_FAMILY_MISMATCH = "runtime_family_mismatch"
ENTERED_AT_MISSING = "entered_at_missing"
STALE_RECEIPT = "stale_receipt"
LEDGER_INVALID = "ledger_invalid"
REPLAY = "replay"
DOD_FAILED = "dod_failed"
DISPATCH_NOT_ELIGIBLE = "dispatch_not_eligible"
# #304. Deliberately NOT in _EVIDENCE_CODES: dependency order is correctness,
# in the same class as ILLEGAL_TRANSITION and DOD_FAILED, so it must not be
# downgraded to a warning under `enforcement="warn"`. Project-level opt-out
# lives in `resilience.dependency_gate`, where a reviewer can see it, not in a
# per-session enforcement mode.
DEPS_UNMET = "deps_unmet"
DISPATCH_ALREADY_STARTED = "dispatch_already_started"
RECEIPT_ALREADY_PRESENT = "receipt_already_present"
DISPATCH_BINDING_MISMATCH = "dispatch_binding_mismatch"
RECEIPT_DELIVERY_FAILED = "receipt_delivery_failed"
STATE_LOCK_TIMEOUT = "state_lock_timeout"
# Epic #339. The attempt is the CP-durable, fenced form of the dispatch
# binding above, not a second identity: one story carries one active binding
# per role and both ids live in it. These two codes exist because the failures
# are distinguishable and an operator needs to know which one they hit.
ATTEMPT_BINDING_MISMATCH = "attempt_binding_mismatch"
# SPQ only. A receipt whose Cycle identity disagrees with the sealed manifest
# is attesting to work under a Cycle it was not admitted to, which would fail
# Sync criterion 3 (identity closure) later at far greater cost.
MANIFEST_DISAGREEMENT = "manifest_disagreement"
# SPQ only. An open Cycle whose sealed manifest cannot be read is missing its
# admission authority, which is not the same as having none: a project with no
# Cycle open is legitimately unbound, while one mid-Cycle with a corrupt
# manifest cannot show any Work Unit is admitted. The second fails closed.
MANIFEST_UNREADABLE = "manifest_unreadable"
# SPQ only. Two Work Units whose DECLARED path scopes intersect may live in one
# Cycle -- sequentially, with distinct `execution_order` -- but never run
# concurrently (`C-07`, `SC-MTH-008`). The declaration refuses the unordered
# case at Commit; this refuses the ordered case at the moment a second
# concurrent dispatch would start.
#
# BOTH CHECKS ARE REQUIRED, and neither substitutes for the other. Admission
# cannot know when a unit is actually running, and dispatch cannot see the
# whole admitted set's shape. `SC-MTH-008` asks for the declaration and `C-07`
# asks for the concurrency, and the two answers come from different moments.
#
# NOT in `_EVIDENCE_CODES`, for the same reason `MANIFEST_DISAGREEMENT` is not:
# two agents writing one file is correctness, and a warned-past collision
# produces exactly the conflicting work the check exists to prevent -- after
# both dispatches have spent their effort.
SCOPE_COLLISION = "scope_collision"
REGION_CONTRADICTED = "region_contradicted"
# #447. The receipt echoes a fencing token that is not the one this attempt's
# binding currently holds, so it was produced by a generation that has been
# superseded. Two runners believing they hold the same attempt is the split
# brain the token exists to make refutable, so this is not evidence strength:
# see the note under `_EVIDENCE_CODES` for why neither of these two codes is
# downgradable under `enforcement="warn"`.
FENCING_TOKEN_STALE = "fencing_token_stale"
# #592. The binding holds a fencing token AND the kernel placed this attempt on
# a governed runtime, so the bridge that produced the receipt stamps the
# generation (`stampReceiptIdentity`, #607). A receipt from that path with no
# token is not an old agent that never learned to echo one: it is a receipt the
# producer would have stamped. Absence is refused separately from staleness
# because the two say different things, and a refusal that misnames what
# happened is the thing this epic keeps finding.
FENCING_TOKEN_MISSING = "fencing_token_missing"
# #447. The attempt that produced this receipt is recorded as no longer live.
# "A cancellation that only stops future work while its in-flight result still
# lands is not a cancellation" (conformance row `rf.cancelled_attempt_cannot
# _advance`), and the same holds for an expired or failed attempt.
ATTEMPT_NOT_LIVE = "attempt_not_live"

# Checks that only ever warn under `enforcement="warn"`. Policy, readiness,
# request shape, story existence, transition legality and the DoD gate always
# enforce: they are correctness, not evidence strength.
_EVIDENCE_CODES = frozenset(
    {
        NEXT_ACTION_MISMATCH,
        NO_BOUND_ROLE,
        PATH_SYMLINK,
        PATH_ESCAPE,
        PATH_NOT_CANONICAL,
        NO_RECEIPT,
        RECEIPT_UNREADABLE,
        RECEIPT_INVALID,
        STORY_MISMATCH,
        ROLE_MISMATCH,
        # #402: the same class as ROLE_MISMATCH, which is already here. Both
        # are statements about the evidence's identity, so a host that
        # downgrades one must downgrade the other; promoting the pair check to
        # always-refuse would silently make Claude's controlled mode stricter
        # than the role check it sits beside.
        PROFILE_MISMATCH,
        ACCOUNTABLE_ROLE_INVALID,
        BACKEND_MISMATCH,
        RUNTIME_FAMILY_MISMATCH,
        ENTERED_AT_MISSING,
        STALE_RECEIPT,
        LEDGER_INVALID,
        REPLAY,
        DISPATCH_BINDING_MISMATCH,
        ATTEMPT_BINDING_MISMATCH,
    }
)

# MANIFEST_DISAGREEMENT is deliberately NOT in _EVIDENCE_CODES. Which Cycle a
# Work Unit belongs to is correctness, in the same class as ILLEGAL_TRANSITION
# and DEPS_UNMET, so `enforcement="warn"` must not downgrade it: a warned-past
# disagreement produces a receipt the Sync barrier will reject anyway, after
# the Cycle has spent its integration effort.
#
# FENCING_TOKEN_MISSING, FENCING_TOKEN_STALE and ATTEMPT_NOT_LIVE are absent
# for the same reason, one
# step further out. ATTEMPT_BINDING_MISMATCH sits INSIDE the set because it is
# a statement about which execution produced the evidence, in the same class as
# ROLE_MISMATCH beside it. These two are statements about whether the execution
# was still ALLOWED to produce evidence at all: a superseded generation and a
# cancelled attempt are authority failures, and a warned-past authority failure
# is the cancellation that did not cancel. Both fire only on a value the
# coordination lane writes, so nothing that advances today is affected.

# ── dispatch envelope derivation (#447) ──────────────────────────────────────
#
# The dispatch sequence in governed-runtime-pilot.md 4.5 reads "derive
# role/capabilities/evidence -> resolve profile -> extend the existing dispatch
# binding". These two tables are that derivation, keyed on the capability
# profile rather than on the nine role names, because a policy or ceiling
# written against role names has to be rewritten every time the roster moves
# (ADR-031 decision 8) and the roster is explicitly not this pilot's business.
#
# The values are lifted from the two shipped envelope fixtures rather than
# invented: `envelope-local-qe.json` (prover) and `envelope-managed-se.json`
# (producer) are the cross-lane interface, so agreeing with them is what keeps
# the kernel's output readable by the Go bridge and the control plane. The two
# profiles no fixture covers (planner, analyst) get the read-only floor.
_CEILING_BY_CAPABILITY_PROFILE: Dict[str, Tuple[str, ...]] = {
    "producer": ("workspace.write", "process.test", "receipt.v2"),
    "prover": ("workspace.read", "process.test", "receipt.v2"),
    "planner": ("workspace.read", "receipt.v2"),
    "analyst": ("workspace.read", "receipt.v2"),
}

_EVIDENCE_BY_CAPABILITY_PROFILE: Dict[str, Tuple[str, ...]] = {
    "producer": ("build-result", "changed-files", "final-summary"),
    "prover": ("test-result", "changed-files", "final-summary"),
    "planner": ("final-summary",),
    "analyst": ("final-summary",),
}

#: How long the authority an envelope carries lasts. The bridge refuses a
#: lapsed envelope outright (`checkEnvelopeFreshness`, and MOD-06's "an expired
#: or revoked envelope denies every capability it carried"), and the same value
#: is sent to the control plane as the attempt's own `ttl_seconds` so the CP row
#: and the envelope cannot disagree about when the authority ends. One hour
#: matches the `budget.wall_clock_seconds` both shipped fixtures carry.
ATTEMPT_TTL_SECONDS = 3600

#: Attempt states from which a receipt may not advance a story. `completed` is
#: deliberately absent: an attempt that closed successfully is precisely the one
#: whose receipt SHOULD advance, and refusing it would break the only path the
#: bridge produces. The other three mean the attempt stopped without earning
#: its result.
_ATTEMPT_NOT_LIVE_STATES = frozenset({"cancelled", "expired", "failed"})


def _spq_independently_dispatchable(
    project_dir: str, story_id: str, *, role: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """A `next_action`-shaped authorization for a unit `next_action` did not
    name, or None if there is no such authorization.

    SPQ ONLY, and it returns None for every other lifecycle rather than
    raising: this is an addition to the SPQ path, not a relaxation of the
    shared one.

    IT RE-DERIVES RATHER THAN TRUSTS. The caller has already refused an unmet
    dependency and a scope collision, so this could have returned a bare True;
    it asks `story_pipeline.next_action` for the unit's own dispatch record
    instead, because the answer has to carry the role and the DoD contract the
    rest of the gate reads off `selected`. Fabricating that dict here would
    give a concurrent dispatch a different contract from a serial one, which
    is the divergence `attach_dispatch_dod_contract` exists to close.
    """
    try:
        if build_mode(project_dir) != "spq":
            return None
    except Exception:  # noqa: BLE001 - an unreadable mode is not SPQ
        return None
    sp = _sp()
    try:
        state = sp._read_state(project_dir)
        story = sp.get_story(state, story_id)
    except (ImportError, OSError, ValueError):
        return None
    if not isinstance(story, dict):
        return None
    if str(story.get("state") or "") not in ("queued", "in_progress", "testing",
                                             "reviewing", "blocked"):
        return None
    # ONE UNIT'S BOARD, so `next_action` answers about this unit rather than
    # about the Cycle's first dispatchable one. Everything else it reads --
    # config, receipts, the dependency context -- is the project's own.
    solo = dict(state)
    solo["current_stories"] = [story]
    try:
        import spq_state_machine  # noqa: PLC0415

        answer = spq_state_machine.next_action(project_dir, state=solo)
    except Exception:  # noqa: BLE001 - an unanswerable board authorizes nothing
        return None
    if not isinstance(answer, dict):
        return None
    if answer.get("story_id") != story_id:
        return None
    if not str(answer.get("action") or "").startswith("dispatch_"):
        return None
    if role:
        want = role_abbrev(str(role))
        got = role_abbrev(str(answer.get("role") or ""))
        if want and got and want != got:
            return None
    return answer



def _region_contradiction(project_dir: str) -> Optional[Dict[str, Any]]:
    """Another live reservation overlaps this Cycle's own. Refuse the dispatch.

    THE RECHECK EXISTS BECAUSE A RESERVATION IS NOT PERMANENT. `open_cycle`
    reserves the region and refuses to seal without one, but the reservation
    can be released afterwards -- by hand, by a Checkpoint in another clone,
    or by an operator recovering a stuck one -- and at that moment a second
    Cycle can legally claim the region this Cycle is still working in. Nothing
    local would notice: the sealed declaration still says the region was
    granted, which was true when it was written.

    AN UNREACHABLE REGISTRY DOES NOT REFUSE HERE, and the asymmetry with
    Commit is the point rather than an inconsistency. At Commit, "I could not
    ask" means "somebody may already hold this and I cannot see them", so
    sealing would be a guess. At dispatch, this Cycle's claim is already
    granted and no OTHER clone can be granted an overlapping one while the same
    registry is down -- they need it to say yes just as much. So an outage
    cannot create the collision this check looks for, and refusing every
    dispatch through one would strand a Cycle mid-flight to protect against a
    state the outage prevents.

    A Cycle sealed with `registry: none` addresses no registry, so there is
    nothing to recheck and nothing is asked.
    """
    binding = _sealed_cycle_binding(project_dir)
    if not isinstance(binding, dict) or binding.get("unreadable"):
        return None
    try:
        import path_scope  # noqa: PLC0415
        import region_registry  # noqa: PLC0415
        import spq_state_machine  # noqa: PLC0415

        sealed = spq_state_machine.read_manifest(
            project_dir, str(binding.get("cycle_id") or "")
        )
    except (ImportError, OSError, ValueError):
        return None
    if not isinstance(sealed, dict):
        return None
    recorded = (sealed.get("region_reservation") or {}).get("registry")
    if recorded != "control-plane":
        # A CYCLE THAT RECORDS NO GRANT WAS NEVER SEPARATED, so a dispatch
        # against it is a dispatch into a region nobody established this Cycle
        # owns. `open_cycle` refuses to produce such a declaration now, so
        # reaching this means a board sealed before the requirement was
        # enforced -- and the honest answer about it is the same as a failed
        # recheck rather than a pass. An earlier cut returned None here, which
        # made "admitted without a registry" the one state that skipped the
        # check entirely.
        return {
            "kind": "unrecorded",
            "detail": (
                "this Cycle's declaration records no control-plane region "
                "reservation (%s), so its separation from other Cycles was "
                "never established. Re-open the Cycle against a reachable "
                "registry." % (recorded or "nothing")
            ),
        }
    mine = str(sealed.get("cycle_id") or "")
    ours = [str(x) for x in (sealed.get("source_region") or ())]
    if not ours:
        return None
    # SAME EXPLICIT PROJECT `open_cycle` RESERVED UNDER (#738). `reserve` and
    # this recheck must resolve the SAME project or an accurate reservation
    # reads as vanished: `_project_slug` is the same `SYNAPTORY_PROJECT_ID` ->
    # `project_id:` reader `open_cycle` now calls before reserving, so a
    # dispatch recheck asks the registry about the identical scope the Cycle
    # was granted under, not whatever the CLI's own implicit cwd-based
    # default separately infers.
    live = region_registry.holder(
        project_dir,
        repository=str(sealed.get("repository") or ""),
        project=_project_slug(project_dir),
    )
    if not live.get("available"):
        # A RECHECK THAT CANNOT BE ANSWERED REFUSES, and the first cut of this
        # got it wrong in a way worth recording. It let a dispatch proceed
        # through an outage, reasoning that no other clone can be GRANTED an
        # overlapping claim while the same registry is down. True, and
        # insufficient: a grant made before the outage, or a release during
        # it, is exactly the state this recheck exists to notice, and an
        # outage is precisely when it cannot. Issue #643 and §5.2 say an
        # unavailable or stale registry refuses dispatch; "I could not
        # recheck" is not "the claim holds".
        return {
            "kind": "unavailable",
            "detail": (
                "the region registry could not be rechecked (%s), so nothing "
                "confirms this Cycle still holds %s"
                % (live.get("detail") or "no detail", ours)
            ),
        }
    held = {r.get("cycle_id"): r for r in live.get("reservations") or ()}
    if mine not in held:
        return {
            "kind": "released",
            "detail": (
                "this Cycle's reservation is no longer live in the registry, so "
                "another Cycle may claim %s while this one is still working in "
                "it" % (ours,)
            ),
        }
    for cycle_id, row in held.items():
        if cycle_id == mine:
            continue
        theirs = [str(x) for x in (row.get("region") or ())]
        try:
            overlap = path_scope.intersects(ours, theirs)
        except Exception:  # noqa: BLE001
            continue
        if overlap:
            return {
                "kind": "overlap",
                "cycle_id": str(cycle_id),
                "detail": (
                    "Cycle %s holds %s, which overlaps this Cycle's %s"
                    % (cycle_id, theirs, ours)
                ),
            }
    return None


def _scope_collision(
    project_dir: str, story_id: str
) -> Optional[Tuple[str, List[str]]]:
    """`(unit_id, intersecting paths)` if dispatching `story_id` would collide.

    THE SCOPES COME FROM THE SEALED DECLARATION, never from the board, and
    that is the whole security property. The board is agent-writable: a unit
    could narrow its own `file_scope` to a path nothing else claims and
    dispatch straight into another unit's files. The declaration is
    hash-sealed at Commit and `path_scope` is immutable after it, so it is the
    only statement of scope a collision check can trust.
    #
    A LIVE DISPATCH, not a board state. `in_progress` says where a unit got
    to; `mcp_active_dispatches` with an `attempt_id` says something is running
    NOW, which is what "concurrently" means. A unit sitting in `in_progress`
    with no live attempt is stalled work, not running work, and blocking on it
    would strand the Cycle.

    Returns None for every non-SPQ project and for any Cycle whose declaration
    or grammar cannot be read -- deliberately, and it is safe here only
    because it is not the last line of defence: `_sealed_cycle_binding`
    already refuses an SPQ Cycle whose manifest is unreadable, so a dispatch
    never reaches this check with an unknown admitted set. Refusing again here
    on an unreadable document would turn one failure into two codes for it.
    """
    binding = _sealed_cycle_binding(project_dir)
    if not isinstance(binding, dict) or binding.get("unreadable"):
        return None
    try:
        import path_scope  # noqa: PLC0415
        import spq_state_machine  # noqa: PLC0415

        sealed = spq_state_machine.read_manifest(
            project_dir, str(binding.get("cycle_id") or "")
        )
        board = _sp()._read_state(project_dir)
    except (ImportError, OSError, ValueError):
        # NARROW ON PURPOSE. The first cut caught bare `Exception` around
        # `_sp().read_state(...)` -- a method `story_pipeline` does not have --
        # so every collision check answered "no collision" from an
        # AttributeError, and the gate was inert while reading as implemented.
        # A broad catch around a security check reports the absence of the
        # check as the absence of a problem. These three are the failures
        # actually being tolerated: no SPQ runtime, an unreadable file, a
        # malformed one.
        return None
    declared: Dict[str, List[str]] = {}
    for unit in sealed.get("admitted_units") or []:
        if isinstance(unit, dict) and unit.get("id"):
            declared[str(unit["id"])] = [
                str(p) for p in (unit.get("path_scope") or [])
            ]
    mine = declared.get(str(story_id)) or []
    if not mine:
        return None
    for other in board.get("current_stories") or []:
        if not isinstance(other, dict):
            continue
        other_id = str(other.get("id") or "")
        if not other_id or other_id == str(story_id):
            continue
        dispatches = other.get("mcp_active_dispatches")
        if not isinstance(dispatches, dict) or not any(
            isinstance(d, dict) and str(d.get("attempt_id") or "")
            for d in dispatches.values()
        ):
            continue
        theirs = declared.get(other_id) or []
        if not theirs:
            continue
        try:
            if path_scope.intersects(mine, theirs):
                return other_id, sorted(set(mine) & set(theirs)) or sorted(theirs)
        except path_scope.PathScopeError:
            # An unreadable scope is refused rather than treated as disjoint:
            # `path_scope.intersects` raises on an absent declaration for
            # exactly this reason, and answering "no collision" to a question
            # it could not evaluate is the failure mode `SC-MTH-008` names.
            return other_id, theirs
    return None


def _sealed_cycle_binding(project_dir: str) -> Optional[dict]:
    """The current Cycle's sealed identity.

    Three outcomes, and the distinction between the last two is the point:

    * ``None`` -- not an SPQ project, or no Cycle is open. There is no manifest
      to check against, so dispatch proceeds without a Cycle binding.
    * ``{"unreadable": <reason>}`` -- an SPQ Cycle IS open and its manifest
      cannot be read or does not carry its own identity. The admission check
      cannot run, so the caller must refuse rather than proceed.
    * the binding -- cycle id, manifest hash, and the admitted unit ids.

    An earlier revision collapsed the last two, returning ``None`` on any
    failure so that "attempt identity must never be the reason a dispatch
    cannot proceed". That reasoning is right for a project with no Cycle and
    wrong for a project with one: a missing or corrupt manifest then silently
    skipped the mandatory admission check and issued an attempt carrying no
    Cycle identity at all, which is precisely the unprovable dispatch the
    check exists to stop. Unreadable Cycle authority now fails closed.
    """
    try:
        if build_mode(project_dir) != "spq":
            return None
    except Exception:
        # The mode itself is unreadable, so this is not known to be an SPQ
        # project. Every non-SPQ path is unaffected by Cycle admission.
        return None
    try:
        import spq_state_machine  # noqa: PLC0415  (optional, SPQ-only import)

        # `identity()` is the canonical resolver and the ONLY correct one here.
        # An earlier revision read `_read_state()["_cycle_id"]`, which is a
        # derived mirror that `_read_state` normalizes back to None, so the
        # admission check silently resolved no Cycle on every SPQ project and
        # never ran at all. The authoritative value lives at
        # `state["spq"]["cycle_id"]`, behind the flag/env/pointer/index order
        # `resolve_identity` implements.
        cycle_id = str(spq_state_machine.identity(str(project_dir)).cycle_id or "")
    except Exception as exc:  # noqa: BLE001
        return {"unreadable": "cycle identity is unresolvable: %s" % exc}
    if not cycle_id:
        return None  # SPQ, but no Cycle is open yet.

    # Past this point the project IS SPQ and a Cycle IS open, so every
    # remaining failure is missing authority rather than absent authority.
    # `read_manifest` is the canonical reader and resolves BOTH copies, local
    # sealed first then the committed transport. Reading only the local path
    # refused every hydrated workstream clone: `open_cycle` writes both in the
    # integration clone, but only the committed copy travels through git, and
    # `hydrate_cycle` writes a projection rather than a second sealed copy. So
    # a perfectly valid delivery clone has the committed copy alone, and the
    # first revision of this guard would have blocked all of them.
    try:
        manifest = spq_state_machine.read_manifest(str(project_dir), cycle_id)
    except Exception as exc:  # noqa: BLE001
        return {"unreadable": "sealed manifest is unreadable: %s" % exc}
    if not manifest:
        return {
            "unreadable": "no sealed manifest for Cycle %s in either the local "
            "store or the committed tree" % cycle_id
        }
    if not isinstance(manifest, dict):
        return {"unreadable": "sealed manifest is not an object"}
    binding = {
        "cycle_id": str(manifest.get("cycle_id") or ""),
        # `declaration_hash` since #644: `cycle_records` seals a DECLARATION,
        # and the old name is what let a lane topology live inside it. The
        # binding key stays `manifest_hash` for one release so a bridge built
        # against the old envelope still decodes.
        "manifest_hash": str(manifest.get("declaration_hash") or ""),
        "admitted_unit_ids": [
            str(u.get("id") or "")
            for u in (manifest.get("admitted_units") or [])
            if isinstance(u, dict) and u.get("id")
        ],
    }
    if not binding["cycle_id"] or not binding["manifest_hash"]:
        return {
            "unreadable": (
                "the sealed declaration carries no cycle_id or "
                "declaration_hash, so nothing binds a dispatch to it"
            )
        }
    if binding["cycle_id"] != cycle_id:
        # The open Cycle and the manifest on disk name different Cycles. One of
        # them is stale and this dispatch cannot tell which.
        return {
            "unreadable": "open Cycle is %s but the sealed manifest names %s"
            % (cycle_id, binding["cycle_id"])
        }
    return binding


#: Story-state key: every attempt this kernel has AUTHORIZED for the story,
#: keyed by role abbrev, in the order it minted them (#493, extended by #492).
#:
#: `mcp_active_dispatches` cannot answer the question this ledger exists for.
#: It holds one binding per role, it is OVERWRITTEN WHOLE at re-dispatch, and
#: the commit path pops the entry once the stage advances, so by the time a
#: retry's receipt lands the attempt that produced the finding it carries
#: forward has left the state file. Refusing that receipt would push a producer
#: to relabel an honest attestation with the current attempt id, which is a
#: worse outcome than the hole: a lie the check cannot see.
#:
#: #493 shipped this as a per-role list of ATTEMPT IDS and said the ordering,
#: the supersession link and the steer/retry/fresh classification belonged to
#: #492 rather than writing half a vocabulary here. #492 is that half, and it
#: EXTENDS this ledger rather than opening a third structure: an entry is now
#: a RECORD, and the questions "was this attempt authorized" (#493) and "what
#: is this attempt a response to" (#492) are answered from one list written in
#: one place. A bare-string entry is still read as `{"attempt_id": <s>}`, so a
#: story dispatched before #492 landed keeps its authorization set.
#:
#: The record, and who writes each field. Every one of them is written by this
#: module; none is accepted from a caller (see `_classify_new_attempt`):
#:
#:   attempt_id            the identity, at mint
#:   authorized_at         when this kernel minted it
#:   classification        `fresh` | `retry` | `steer`, at mint, once
#:   prior_attempt_id      the attempt this one supersedes, "" when fresh
#:   prior_failure_class   the predecessor's class, which is WHY the
#:                         classification says what it says
#:   state                 the attempt's last projected state, from
#:                         `reconcile_dispatch`
#:   failure_class         why it ended, from `reconcile_dispatch`
#:   failure_class_source  `reported` (a caller stated it) or `kernel-default`
#:                         (inferred from a bare cancellation). A class nobody
#:                         reported and one that was reported must not share a
#:                         representation.
#:   superseded_by/_at     the successor that replaced it, at the successor's
#:                         mint
#:   advanced_at           the stage this attempt ran in advanced, so its work
#:                         landed. An attempt that succeeded and one that was
#:                         abandoned must not look identical.
#:
#: Deliberately UNBOUNDED, against this ticket's own suggestion of a bounded
#: list. A trim drops attempt ids, and `_authorized_attempts` reads this list
#: to decide whether an `attested` grade names an execution that ran (#493), so
#: a cap converts a size problem nobody has measured into a correctness one:
#: honest evidence from a trimmed-out predecessor would refuse. A record is
#: about 200 bytes, per role, per Work Unit, and the worst stage measured in
#: this epic's own A/B (#521, BM-1's review stage) ran four attempts.
AUTHORIZED_ATTEMPTS_KEY = "mcp_authorized_attempts"

#: The closed set of things a new attempt can be. S1's three words.
FRESH_ATTEMPT = "fresh"
RETRY_ATTEMPT = "retry"
STEER_ATTEMPT = "steer"
ATTEMPT_CLASSIFICATIONS = (FRESH_ATTEMPT, RETRY_ATTEMPT, STEER_ATTEMPT)

#: The one failure class the steer/retry split turns on. Spelled once.
HUMAN_CANCEL_FAILURE_CLASS = "cancelled-by-human"

#: `reported` vs `kernel-default`, so a consumer counting steers can tell a
#: stated human cancellation from one this kernel inferred from a bare
#: `cancelled`. The epic's transferable rule, applied to a string instead of a
#: zero: two things that mean different things may not share a representation.
CLASS_REPORTED = "reported"
CLASS_KERNEL_DEFAULT = "kernel-default"

#: Mirrors `runtime_contracts.FAILURE_CLASSES`, used only when that module
#: cannot be imported, so a missing sibling cannot turn a closed vocabulary
#: into an open one. Same posture as `_FALLBACK_ATTEMPT_STATES`.
_FALLBACK_FAILURE_CLASSES = (
    "environment",
    "deterministic-check",
    "context-knowledge",
    "skill-rule",
    "workflow-structure",
    "agent-specialization",
    "model-policy",
    HUMAN_CANCEL_FAILURE_CLASS,
    "budget-exhausted",
    "runtime-death",
    "unclassified",
)
_UNCLASSIFIED_FAILURE = "unclassified"


def _failure_class_vocabulary() -> Tuple[str, ...]:
    try:
        import runtime_contracts as rc

        return tuple(rc.FAILURE_CLASSES)
    except Exception:  # noqa: BLE001
        return _FALLBACK_FAILURE_CLASSES


def _history_view(story: Any, abbrev: str) -> Tuple[Dict[str, Any], ...]:
    """This stage's attempt records, oldest first, as read-only copies.

    A pre-#492 bare string reads as a record carrying only its identity, which
    is exactly what it recorded. Anything else in the list is skipped rather
    than raised on: state is kernel-written, but a hand-edited or truncated
    file must degrade to "no history" inside an advance.
    """
    if not isinstance(story, dict) or not abbrev:
        return ()
    ledger = story.get(AUTHORIZED_ATTEMPTS_KEY)
    if not isinstance(ledger, dict):
        return ()
    recorded = ledger.get(abbrev)
    if not isinstance(recorded, list):
        return ()
    out: List[Dict[str, Any]] = []
    for entry in recorded:
        if isinstance(entry, str) and entry:
            out.append({"attempt_id": entry})
        elif isinstance(entry, dict):
            attempt_id = entry.get("attempt_id")
            if isinstance(attempt_id, str) and attempt_id:
                out.append(dict(entry))
    return tuple(out)


def attempt_history(story: Any, role: str) -> Tuple[Dict[str, Any], ...]:
    """Every attempt this kernel authorized for one stage of one Work Unit.

    THE SUPPORTED READER for the classification, so a consumer counting
    "retries per stage" (proposal 6 item 2, #409) does not reach into the
    state key and re-derive the field split. Oldest first; copies, so a reader
    cannot write the board through the return value.

    Accepts a role name or its abbrev, because callers hold either.
    """
    abbrev = role_abbrev(role) or str(role or "").strip().lower()
    return _history_view(story, abbrev)


def unadvanceable_attempt(story: Any, role: str) -> Optional[Dict[str, Any]]:
    """This stage's bound attempt, when its recorded state refuses its receipt.

    ONE PREDICATE, TWO READERS, and the second reader is the whole point.
    `evaluate_advance` refuses a receipt whose attempt the board records as
    cancelled, expired or failed; `story_pipeline.next_action` has to route
    AROUND that edge rather than offer it, or the operator is handed a
    transition the very next call refuses.

    #690 is what happens when only one of them can see it. `next_action` read
    that a canonical receipt file existed and selected `transition to testing
    instead of re-dispatching`; `advance` read the binding and refused
    `attempt_not_live`, saying dispatch a new attempt; `begin_dispatch` read
    the same receipt file and refused `receipt_already_present`, saying call
    advance. Three correct reads of three different facts, and a Cycle that
    could not move without an operator deleting evidence by hand. No single
    component was wrong, so reading any one of them found nothing.

    Returns the binding's identity, its recorded end state and its failure
    class -- enough for a recovery to state WHY it is one -- or None.

    NO RECORD IS NOT A DEAD ATTEMPT, the same rule `_incumbent_is_live`
    states. A stage never dispatched through the kernel has nothing whose
    death could be established, and reading absence as failure would route
    every ungoverned QE and CR receipt into a recovery it does not need.

    `attempt_state` is honoured beside `state` because `_incumbent_is_live`
    already honours both, and a liveness question answered one way by the
    concurrency guard and another way by the gate is the same class of
    disagreement this function exists to end.
    """
    abbrev = role_abbrev(role) or str(role or "").strip().lower()
    if not isinstance(story, dict) or not abbrev:
        return None
    dispatches = story.get("mcp_active_dispatches")
    if not isinstance(dispatches, dict):
        return None
    binding = dispatches.get(abbrev)
    if not isinstance(binding, dict):
        return None
    recorded = (
        str(binding.get("state") or binding.get("attempt_state") or "").strip().lower()
    )
    if recorded not in _ATTEMPT_NOT_LIVE_STATES:
        return None
    return {
        "attempt_id": str(binding.get("attempt_id") or ""),
        "attempt_state": recorded,
        "failure_class": str(binding.get("failure_class") or ""),
        "failure_class_source": str(binding.get("failure_class_source") or ""),
    }


def unadvanceable_receipt(
    project_dir: str, story: Any, role: str
) -> Optional[Dict[str, Any]]:
    """This stage's canonical receipt, when it is present but INADMISSIBLE.

    #741. `unadvanceable_attempt` above catches the attempt-liveness reason a
    fresh receipt cannot advance; this catches the other one it cannot see: a
    LIVE attempt that produced a receipt which still fails schema validation
    -- an `evidence` object where a list of typed items is required, for
    example. A completed producer attempt is not in `_ATTEMPT_NOT_LIVE_STATES`
    (deliberately: it is the case that SHOULD advance), so
    `unadvanceable_attempt` returns None for it and the caller is left
    believing the ordinary transition is safe. `evaluate_advance` refuses it
    anyway, as `RECEIPT_INVALID`, and `begin_dispatch` refuses a successor
    because the same (invalid) receipt is still on disk -- #690's deadlock
    through a different door.

    SCOPED TO A BINDING RECORDED `completed`, not merely to "any dispatch
    binding" and not to "any state `unadvanceable_attempt` calls live". Two
    reasons, and both are load-bearing:

    1. A stage with no `mcp_active_dispatches[abbrev]` binding at all was
       never dispatched through this kernel's governed retry ladder, so
       there is no successor for `begin_dispatch` to authorize and no
       predecessor for `execute_dispatch` to archive. Offering a ladder
       recovery there would only swap a direct, correct `RECEIPT_INVALID`
       refusal (from `evaluate_advance`'s own step 9) for a
       `NEXT_ACTION_MISMATCH` that names no route out, because a strict
       host's `advance` compares against `next_action`'s `transition_to`,
       which a recovery leaves unset. An ungoverned receipt written straight
       to disk (no `begin_dispatch` ever called) must keep refusing as
       `RECEIPT_INVALID`, unchanged from pre-#741 -- exactly what the
       cross-host conformance contract (`test_invalid_receipt_refused`) and
       the Cursor/Codex MCP suites already pin down.

    2. A binding recorded `pending` / `claimed` / `running` (or not yet
       state-stamped at all) names an attempt the real runtime may still be
       writing to. Archiving its receipt and minting a successor while that
       runtime is potentially still in flight would race it -- two attempts
       believing they hold the same work. `completed` is the one state that
       says the runtime is definitely finished and nothing else will ever
       correct this receipt, which is exactly the #741 scenario (CP close
       succeeded, generation reconciled, runtime ended `completed`). This is
       why the check below is an exact-match on `completed`, not
       `not in _ATTEMPT_NOT_LIVE_STATES` -- the latter would also admit
       `pending`/`claimed`/`running`, reopening the same conformance
       contract this predicate must not relitigate
       (`test_mixed_actor_producing_stage_attributes_each_actor`, whose
       negative control writes a forged, still-in-flight receipt against a
       freshly begun -- not completed -- dispatch and expects a plain
       `RECEIPT_INVALID`).

    CALLS THE SAME VALIDATOR `evaluate_advance` calls at its own step 9, on
    the same bytes it would read, via the same `canonical_receipt_path`. This
    is deliberately not a second, independent shape check: a predicate that
    disagreed with the gate about what "invalid" means would refuse a receipt
    the gate accepts, or worse, recommend recovery for one the gate would
    advance anyway.

    NO AUTO-REPAIR. This never rewrites or coerces the malformed payload --
    that is a confirmed product decision, not an oversight: an inadmissible
    receipt always requires a genuinely fresh dispatch, and its bytes are
    archived, never edited.

    Missing file, unreadable JSON, or a non-dict payload all return None --
    that is `_fresh_receipt`'s "absent" case already, and not this
    predicate's job to re-diagnose (a missing receipt already dispatches the
    stage normally). A path refusal (symlink / escape) also returns None for
    the same reason: those are `canonical_receipt_path`'s own correctness
    checks, unrelated to whether an EXISTING receipt's contents validate.

    Returns the receipt's location, its digest (so the caller can name
    exactly which bytes it is talking about, and the archive step can prove
    it archived those same bytes), and the validator's error list -- or None
    when the receipt is absent, unreadable, valid, or ungoverned.
    """
    abbrev = role_abbrev(role) or str(role or "").strip().lower()
    if not abbrev:
        return None
    if not isinstance(story, dict):
        return None
    dispatches = story.get("mcp_active_dispatches")
    if not isinstance(dispatches, dict):
        return None
    binding = dispatches.get(abbrev)
    if not isinstance(binding, dict):
        return None
    recorded = str(binding.get("state") or binding.get("attempt_state") or "").strip().lower()
    if recorded != "completed":
        return None
    story_id = str(story.get("id") or "")
    if not story_id:
        return None
    try:
        path = canonical_receipt_path(project_dir, story_id, abbrev)
    except _Refusal:
        return None
    except Exception:  # noqa: BLE001 - an unreadable path names no receipt
        return None
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        receipt = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(receipt, dict):
        return None

    from receipt_validator import validate_receipt_payload  # type: ignore

    result = validate_receipt_payload(receipt, project_dir)
    if result.valid:
        return None
    return {
        "receipt_path": str(path),
        "receipt_digest": hashlib.sha256(raw).hexdigest(),
        "errors": list(result.errors or []),
    }


def inadmissible_receipts(
    project_dir: str, state: Dict[str, Any]
) -> Dict[str, Dict[str, Any]]:
    """Every current story whose gating receipt exists, is bound to a
    recorded dispatch, but fails validation.

    #741. Mirrors `scrum_state_machine.verify_only_units`: an impure wrapper
    that does the project_dir-dependent work (reading each stage's receipt
    off disk) ONCE per `next_action` call, so the result can be threaded into
    the pure `story_pipeline.next_action` as plain data instead of that
    function reaching onto disk itself.

    Keyed by story id rather than by (story id, role): `BLOCKED_FROM_ROLE`
    names exactly one role per current stage, so one story has at most one
    gating receipt to check per call.

    `awaiting_acceptance` is skipped on purpose -- that stage's receipt is a
    human Project Owner sign-off, not a producer/prover artifact this
    predicate's retry ladder is built to recover, and is explicitly out of
    scope for #741.

    UNGOVERNED OR STILL-IN-FLIGHT RECEIPTS ARE NOT HITS HERE, by
    `unadvanceable_receipt`'s own scoping (see its docstring): a story whose
    stage was never dispatched through this kernel's `mcp_active_dispatches`
    ledger, or whose bound attempt has not been recorded `completed`, keeps
    refusing through `evaluate_advance`'s own `RECEIPT_INVALID` step exactly
    as it did before #741.
    """
    hits: Dict[str, Dict[str, Any]] = {}
    stories = state.get("current_stories") if isinstance(state, dict) else None
    if not isinstance(stories, list):
        return hits
    for story in stories:
        if not isinstance(story, dict):
            continue
        story_id = str(story.get("id") or "")
        if not story_id:
            continue
        stage = str(story.get("state") or "")
        if stage == "awaiting_acceptance":
            continue
        bound = BLOCKED_FROM_ROLE.get(stage)
        if not bound:
            continue
        _, abbrev = bound
        hit = unadvanceable_receipt(project_dir, story, abbrev)
        if hit is not None:
            hits[story_id] = hit
    return hits


def _history_for_write(story: dict, abbrev: str) -> Optional[List[Dict[str, Any]]]:
    """This stage's live record list, normalized to records, ready to mutate.

    Creates the ledger, so only the write paths may call it. The in-place
    string-to-record normalization happens here and nowhere else: a reader
    that rewrote state to answer a question would make every read a write.
    """
    if not isinstance(story, dict) or not abbrev:
        return None
    ledger = story.get(AUTHORIZED_ATTEMPTS_KEY)
    if not isinstance(ledger, dict):
        ledger = {}
        story[AUTHORIZED_ATTEMPTS_KEY] = ledger
    recorded = ledger.get(abbrev)
    if not isinstance(recorded, list):
        recorded = []
        ledger[abbrev] = recorded
    for index, entry in enumerate(recorded):
        if isinstance(entry, str) and entry:
            recorded[index] = {"attempt_id": entry}
    return recorded


def _history_record(
    story: dict, abbrev: str, attempt_id: str, *, create: bool = False
) -> Optional[Dict[str, Any]]:
    """One attempt's record, or None. `create` backfills a missing one.

    Backfilling matters for exactly one case and it is not hypothetical: an
    attempt minted before this ledger existed lives only in the live binding,
    and `_authorized_attempts` already unions that binding in, so backfilling
    adds nothing to the authorization set. What it adds is DURABILITY -- the
    outcome of that attempt would otherwise be erased by the very overwrite
    this ledger exists to survive.
    """
    if not attempt_id:
        return None
    records = _history_for_write(story, abbrev)
    if records is None:
        return None
    for record in records:
        if isinstance(record, dict) and record.get("attempt_id") == attempt_id:
            return record
    if not create:
        return None
    record = {"attempt_id": attempt_id}
    records.append(record)
    return record


def _record_authorized_attempt(
    story: dict,
    abbrev: str,
    attempt_id: str,
    *,
    classification: str = "",
    prior_attempt_id: str = "",
    prior_failure_class: str = "",
    authorized_at: str = "",
) -> Optional[Dict[str, Any]]:
    """Remember that `abbrev` on this story was authorized `attempt_id`.

    Written at MINT, the only moment the kernel knows it authorized anything,
    and never removed: outliving the binding is the whole purpose. The lineage
    fields are written here too, in the same call, because a record whose
    classification arrives later could be written by something other than the
    mint -- and the mint is the only place that can see the predecessor before
    it is overwritten.
    """
    if not isinstance(story, dict) or not abbrev or not attempt_id:
        return None
    record = _history_record(story, abbrev, attempt_id, create=True)
    if record is None:
        return None
    record.setdefault("authorized_at", authorized_at or _now_iso())
    if classification:
        # `setdefault`, not assignment: the classification is written once, at
        # the mint that established it. A second write would let a later
        # dispatch restate an earlier attempt's lineage.
        record.setdefault("classification", classification)
    if prior_attempt_id:
        record.setdefault("prior_attempt_id", prior_attempt_id)
    if prior_failure_class:
        record.setdefault("prior_failure_class", prior_failure_class)
    return record


def _predecessor(story: Any, abbrev: str) -> Optional[Dict[str, Any]]:
    """The attempt a new dispatch of this stage supersedes, or None.

    The LIVE BINDING first, and this is what makes the answer independent of
    ordering: at mint the binding is by construction the attempt immediately
    before this one, because it is the one about to be overwritten. Only when
    the stage already advanced (the commit path popped the binding) does this
    fall back to the ledger's own append order, and nothing here compares
    timestamps.
    """
    if not isinstance(story, dict) or not abbrev:
        return None
    dispatches = story.get("mcp_active_dispatches")
    if isinstance(dispatches, dict):
        live = dispatches.get(abbrev)
        if isinstance(live, dict) and str(live.get("attempt_id") or ""):
            return dict(live)
    history = _history_view(story, abbrev)
    return dict(history[-1]) if history else None


def _classify_new_attempt(story: Any, abbrev: str) -> Dict[str, str]:
    """What a dispatch about to mint an attempt for `abbrev` IS.

    THE CLASSIFICATION IS COMPUTED HERE AND ACCEPTED FROM NOBODY. There is no
    parameter for it on `execute_dispatch`, on the MCP `begin_dispatch` verbs,
    or on the receipt contract, so an agent has nothing to write: it is a
    function of state whose only writer is this module (ADR-029).

      fresh   no attempt has been authorized for this stage of this Work Unit,
              so there is nothing for this one to be a response to.
      steer   the predecessor ended `cancelled-by-human`. A human stopped the
              work and this attempt is what they asked for instead.
      retry   there is a predecessor and it did not end in a human
              cancellation, whatever else it did.

    `steer` therefore costs the predecessor its receipt: `cancelled` is
    terminal on the binding, `evaluate_advance` refuses an in-flight receipt
    from a terminal attempt as `attempt_not_live`, and `_state_regression`
    will not move it back. An attempt cannot buy the steer label without
    discarding its own work.

    The steer CONTENT is deliberately not here. `rf_supervision_privacy` is
    the precedent: the supervision transport carries a per-kind key allowlist
    because content that reaches state has no redaction path, and
    `pipeline-state.json` is committed board state with none at all. The
    classification does not need it -- a steer is established by the
    predecessor's cancellation class -- and the human's own words already have
    a home on the control plane (`cancel_reason`, `cancel_requested_by`).
    """
    predecessor = _predecessor(story, abbrev)
    if predecessor is None:
        return {
            "classification": FRESH_ATTEMPT,
            "prior_attempt_id": "",
            "prior_failure_class": "",
        }
    prior_class = str(predecessor.get("failure_class") or "")
    return {
        "classification": (
            STEER_ATTEMPT
            if prior_class == HUMAN_CANCEL_FAILURE_CLASS
            else RETRY_ATTEMPT
        ),
        "prior_attempt_id": str(predecessor.get("attempt_id") or ""),
        "prior_failure_class": prior_class,
    }


def _archive_predecessor(
    story: dict, abbrev: str, predecessor: Dict[str, Any], successor_id: str
) -> None:
    """Make the superseded attempt's record outlive the overwrite.

    `dispatches[abbrev] = binding` two frames down is a whole-value overwrite,
    so this is the last moment the predecessor's outcome exists anywhere. What
    is carried over is its identity, its last projected state and why it
    ended; what is NOT carried over is its fencing token, on purpose -- a dead
    generation duplicated onto the board is something a stale runner could be
    handed back, and nothing reads a superseded attempt's token.
    """
    prior_id = str(predecessor.get("attempt_id") or "")
    if not prior_id:
        return
    record = _history_record(story, abbrev, prior_id, create=True)
    if record is None:
        return
    for key in ("state", "failure_class", "failure_class_source"):
        value = str(predecessor.get(key) or "")
        if value and not record.get(key):
            record[key] = value
    record["superseded_by"] = successor_id
    record["superseded_at"] = _now_iso()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _authorized_attempts(story: Any, abbrev: str) -> Tuple[str, ...]:
    """Every attempt id the kernel authorized for THIS story's `abbrev` stage.

    The union of the live binding and the ledger, and the live binding is in
    there for the migration case rather than for symmetry: a story dispatched
    before this landed has a binding and no ledger, so reading only the ledger
    would refuse the attestations of every in-flight attempt on the day it
    merged.

    Scoped to one role on purpose. Proposal 3.3 says an attested item carries
    the PRODUCING attempt identity, and producer is not subject: a CR
    attestation ABOUT the SE stage is produced by the CR attempt and names it.
    So cross-role naming buys no honest case, while "any attempt on this
    story" would admit exactly the one #493 calls illegitimate -- an SE
    receipt attributing its own finding to the QE attempt, which is the
    verdict-posted-by-its-subject shape #435 is closing on the CP side.
    """
    if not isinstance(story, dict) or not abbrev:
        return ()
    ids: List[str] = []
    dispatches = story.get("mcp_active_dispatches")
    if isinstance(dispatches, dict):
        active = dispatches.get(abbrev)
        if isinstance(active, dict):
            live = active.get("attempt_id")
            if isinstance(live, str) and live:
                ids.append(live)
    for record in _history_view(story, abbrev):
        attempt_id = record.get("attempt_id")
        if isinstance(attempt_id, str) and attempt_id and attempt_id not in ids:
            ids.append(attempt_id)
    return tuple(ids)


def _mint_attempt_binding(
    project_dir: str, state: dict, story_id: str, abbrev: str
) -> Optional[dict]:
    """Mint attempt identity for one dispatch, refusing a Work Unit the sealed
    manifest never admitted (Epic #339, #344).

    Returns None when there is nothing to add (no SPQ Cycle open), a
    ``{"refusal": ...}`` mapping when the dispatch must be refused, or a
    ``{"binding": ...}`` mapping to merge into the active-dispatch record.

    The admission check is the cheap moment to catch an unadmitted unit. The
    Sync barrier catches it too, at criterion 3, but only after every
    workstream has spent its integration effort.
    """
    cycle = _sealed_cycle_binding(project_dir)
    attempt_id = "att_" + secrets.token_hex(10)
    fencing_token = secrets.token_hex(16)
    # #447. The expiry rides in the SAME binding as the two ids, because it is
    # a property of the same bounded execution: the envelope's `expires_at`,
    # the control plane's `ttl_seconds`, and this field are one deadline stated
    # three times, and reading it back off the binding is what stops a second
    # dispatch from minting a different one for the same attempt.
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=ATTEMPT_TTL_SECONDS)
    ).isoformat()
    if isinstance(cycle, dict) and cycle.get("unreadable"):
        return {
            "refusal": {
                "code": MANIFEST_UNREADABLE,
                "reason": (
                    "this Cycle's sealed manifest cannot be read, so the "
                    "admission check cannot run and %s cannot be shown to be "
                    "admitted work: %s" % (story_id, cycle["unreadable"])
                ),
            }
        }
    if cycle is None:
        return {
            "binding": {
                "attempt_id": attempt_id,
                "fencing_token": fencing_token,
                "expires_at": expires_at,
            }
        }
    admitted = cycle["admitted_unit_ids"]
    if admitted and story_id not in admitted:
        return {
            "refusal": {
                "code": MANIFEST_DISAGREEMENT,
                "reason": (
                    "%s is not admitted by the sealed manifest for Cycle %s. "
                    "Admit it in a new Cycle or re-seal; do not dispatch work "
                    "the barrier cannot close over." % (story_id, cycle["cycle_id"])
                ),
            }
        }
    # No lane to bind. `SPD-194` retires the Workstream, so a dispatch binds to
    # the Cycle and the Work Unit -- which is what it always actually
    # identified, with the lane riding along for attribution. The field stays
    # in the envelope for one release so a bridge built against the old
    # contract still decodes; it is written empty rather than dropped.
    workstream_id = ""
    return {
        "binding": {
            "attempt_id": attempt_id,
            "fencing_token": fencing_token,
            "expires_at": expires_at,
            "cycle_id": cycle["cycle_id"],
            "manifest_hash": cycle["manifest_hash"],
            "workstream_id": workstream_id,
        }
    }


def _state_lock_timeout_cls():
    """The spec_state timeout type, or a private stand-in when unavailable."""
    try:
        from spec_state import StateLockTimeout  # type: ignore

        return StateLockTimeout
    except ImportError:  # pragma: no cover
        class _Fallback(RuntimeError):
            pass

        return _Fallback


_StateLockTimeout = _state_lock_timeout_cls()


class _Refusal(Exception):
    """Internal control flow: a check refused. Carries the decision payload."""

    def __init__(self, code: str, reason: str, **extra: Any) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.extra = extra


# ── dataclass-free records (3.9-safe, no dataclasses import needed) ──────────


class HostPolicy:
    """Per-host knobs. Differences must be declared here, never forked in code.

    enforcement:
        "enforce" refuses on any failed check. "warn" downgrades evidence checks
        (see `_EVIDENCE_CODES`) to warnings and proceeds; policy, readiness,
        legality and the DoD gate still refuse. Claude's controlled mode uses
        "warn" so today's advisory behaviour is preserved.
    expected_backend:
        Receipt must name this backend. None skips the check.
    require_next_action_match:
        Refuse a transition the deterministic `next_action` did not select.
    entered_at_missing:
        "refuse" (fail closed) or "allow" (skip staleness when the stage has no
        usable entered_at). Reserve valve for pre-pipeline_log-era stories.
    gate_blocked_transitions:
        Whether `-> blocked` requires a receipt. Blocking is de-escalation, so
        Claude's CLI leaves it open while the MCP hosts keep it gated.
    dod_on_done / dod_red_behavior:
        Evaluate DoD on the done edge, and on a red result either "refuse"
        (MCP semantics) or "redirect_blocked" (Claude wrapper semantics, which
        keeps the recovery ladder alive).
    dod_requires_pass:
        False applies `dod_gate_block_reason`, the documented product policy.
        That gate blocks on the four CONDITIONAL gates (no_critical_findings,
        runtime_verified, ui_acceptance, integration_verified), on a required
        check that declared a criteria gap (#403), on a replay mismatch
        (#179 E1), and on a required check that FAILED against a hash-sealed
        authored set (#494). What still passes through is a static check
        resting on the receipt's own claim about itself.

        The original wording of this entry said static checks pass through
        "because static checks are self-attested and are covered by receipt
        replay instead". #406 falsified both halves of that for `tests_pass`
        on a Cycle carrying a seal: the verdict is derived from a manifest the
        agent cannot edit without breaking its hash, and replay re-runs
        `verification_commands`, which exit 0 whether or not a dropped case
        ever ran. #494 corrects the behaviour and this sentence together,
        because the gap between them was an inverted incentive: an honestly
        declared gap blocked and a silently dropped case did not.

        True additionally requires `dod["passed"]`, which is what the Cursor MCP
        server did on its own. Kept as a knob rather than collapsed so adopting
        the kernel does not silently RELAX a host; see #276. The two settings
        still differ: a static check that fails on the receipt's own claim
        (build_succeeds, coverage_no_decrease) blocks under True and does not
        under False.
    policy_gate / readiness_gate:
        Callables taking project_dir and returning a refusal dict or None.
    receipt_delivery_gate:
        Optional host callback invoked with ``(project_dir, receipt_path)``
        after evidence passes and while the state transaction is still held.
        It must return ``{"handed_off": true}`` before mutation may proceed.
    require_dispatch_binding:
        Issue a unique dispatch_id, persist it on the story, and refuse a
        receipt produced by any earlier or concurrent dispatch. Enabled for
        Codex and Cursor, where the MCP adapter owns every managed-agent launch.
    """

    __slots__ = (
        "host",
        "enforcement",
        "expected_backend",
        "require_next_action_match",
        "entered_at_missing",
        "gate_blocked_transitions",
        "dod_on_done",
        "dod_red_behavior",
        "dod_requires_pass",
        "policy_gate",
        "readiness_gate",
        "receipt_delivery_gate",
        "require_dispatch_binding",
    )

    def __init__(
        self,
        host: str,
        enforcement: str = "enforce",
        expected_backend: Optional[str] = None,
        require_next_action_match: bool = True,
        entered_at_missing: str = "refuse",
        gate_blocked_transitions: bool = True,
        dod_on_done: bool = True,
        dod_red_behavior: str = "refuse",
        dod_requires_pass: bool = False,
        policy_gate: Optional[Callable[[str], Optional[dict]]] = None,
        readiness_gate: Optional[Callable[[str], Optional[dict]]] = None,
        receipt_delivery_gate: Optional[Callable[[str, str], dict]] = None,
        require_dispatch_binding: bool = False,
    ) -> None:
        if enforcement not in ("enforce", "warn"):
            raise ValueError("enforcement must be 'enforce' or 'warn'")
        if entered_at_missing not in ("refuse", "allow"):
            raise ValueError("entered_at_missing must be 'refuse' or 'allow'")
        if dod_red_behavior not in ("refuse", "redirect_blocked"):
            raise ValueError("dod_red_behavior must be 'refuse' or 'redirect_blocked'")
        self.host = host
        self.enforcement = enforcement
        self.expected_backend = expected_backend
        self.require_next_action_match = require_next_action_match
        self.entered_at_missing = entered_at_missing
        self.gate_blocked_transitions = gate_blocked_transitions
        self.dod_on_done = dod_on_done
        self.dod_red_behavior = dod_red_behavior
        self.dod_requires_pass = dod_requires_pass
        self.policy_gate = policy_gate
        self.readiness_gate = readiness_gate
        self.receipt_delivery_gate = receipt_delivery_gate
        self.require_dispatch_binding = require_dispatch_binding


class Decision:
    """Verdict of an advance or dispatch evaluation."""

    __slots__ = (
        "allowed",
        "code",
        "reason",
        "warnings",
        "checks",
        "receipt_path",
        "receipt_digest",
        "effective_to_state",
        "dod",
        "story",
        "next_action",
        "role",
        # #402. The (stage_profile, capability_profile) pair this decision was
        # keyed on, named alongside the legacy role so a host adapter or a
        # dispatch prompt can state it without re-deriving it.
        "profile",
        "extra",
    )

    def __init__(
        self,
        allowed: bool,
        code: str = OK,
        reason: str = "",
        warnings: Optional[List[str]] = None,
        checks: Optional[List[dict]] = None,
        receipt_path: Optional[str] = None,
        receipt_digest: Optional[str] = None,
        effective_to_state: Optional[str] = None,
        dod: Optional[dict] = None,
        story: Optional[dict] = None,
        next_action: Optional[dict] = None,
        role: Optional[str] = None,
        profile: Optional[dict] = None,
        extra: Optional[dict] = None,
    ) -> None:
        self.allowed = allowed
        self.code = code
        self.reason = reason
        self.warnings = warnings if warnings is not None else []
        self.checks = checks if checks is not None else []
        self.receipt_path = receipt_path
        self.receipt_digest = receipt_digest
        self.effective_to_state = effective_to_state
        self.dod = dod
        self.story = story
        self.next_action = next_action
        self.role = role
        self.profile = profile
        self.extra = extra if extra is not None else {}

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "allowed": self.allowed,
            "code": self.code,
            "reason": self.reason,
            "warnings": list(self.warnings),
            "checks": list(self.checks),
        }
        for key in (
            "receipt_path",
            "receipt_digest",
            "effective_to_state",
            "dod",
            "story",
            "next_action",
            "role",
            "profile",
        ):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        payload.update(self.extra)
        return payload

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Decision(allowed=%r, code=%r, reason=%r)" % (
            self.allowed,
            self.code,
            self.reason,
        )


# ── shared runtime access ────────────────────────────────────────────────────


def _sp():
    return importlib.import_module("story_pipeline")


@contextlib.contextmanager
def _state_transaction(project_dir: str):
    """Hold the state lock across the whole read-check-mutate-write window.

    Without this, two concurrent advances both read the same state, both pass
    the replay-ledger check against that stale copy, and both write: a lost
    update and a defeated anti-replay guard. write_state's own lock covers only
    its internal re-read, not the caller's decision window.
    """
    try:
        from spec_state import state_transaction  # type: ignore
    except ImportError:  # pragma: no cover - shared runtime always ships it
        yield
        return
    with state_transaction(str(project_dir)):
        yield


def build_mode(project_dir: str) -> str:
    """Return the project's build_mode, defaulting to scrum."""
    from spec_state import read_full_state  # type: ignore

    full = read_full_state(str(project_dir)) or {}
    mode = str(full.get("build_mode") or "").lower()
    if not mode:
        # Multi-spec keeps build_mode inside the active spec slot.
        mode = str(_sp()._read_state(str(project_dir)).get("build_mode") or "scrum").lower()
    if mode not in _BUILD_MODES:
        raise ValueError("unsupported Synaptory build_mode: %s" % mode)
    return mode


def mode_module(project_dir: str):
    """Import the state machine module for this project's build mode."""
    return importlib.import_module("%s_state_machine" % build_mode(project_dir))


def next_action(project_dir: str) -> Dict[str, Any]:
    """Deterministic next action, via the project's own state machine."""
    result = mode_module(project_dir).next_action(str(project_dir))
    if not isinstance(result, dict):
        raise ValueError("state machine next_action returned a non-object")
    return result


# ── receipt helpers ──────────────────────────────────────────────────────────


def expected_receipt_role(
    from_state: str, to_state: str, intensity: Optional[str] = None
) -> Optional[Tuple[str, str]]:
    """(role_name, abbrev) bound to this edge, or None when nothing is bound.

    Early-intensity DoD omits `code_reviewed`, so `reviewing -> done` must not
    demand a CR receipt the planner will never dispatch. `intensity=None`
    keeps the static table (tests and callers that have not resolved a tier).
    """
    if to_state == "blocked":
        return BLOCKED_FROM_ROLE.get(from_state)
    bound = TRANSITION_RECEIPT.get((from_state, to_state))
    if bound and bound[1] == "cr" and intensity is not None:
        checks = _sp().DOD_TIER_CHECKS.get(intensity) or _sp().DOD_TIER_CHECKS["early"]
        if "code_reviewed" not in checks:
            return None
    return bound


class StageExpectation(NamedTuple):
    """What a receipt-gated edge expects, keyed on the capability-profile pair.

    `stage_profile` / `capability_profile` are the key (ADR-032 decision 1);
    `role` and `abbrev` are the legacy wire aliases the pair carries alongside.
    Fields are read by name, never positionally: `expected_receipt_role` keeps
    returning the bare `(role, abbrev)` tuple the host adapters already
    destructure, so nothing has to change shape to gain a profile. A
    NamedTuple rather than a dataclass because this module is projected into
    every host package and stays 3.9-safe and dataclass-free.
    """

    stage_profile: str
    capability_profile: str
    role: str
    abbrev: str

    @property
    def pair(self) -> Tuple[str, str]:
        return (self.stage_profile, self.capability_profile)

    def as_dict(self) -> Dict[str, str]:
        """The pair, named alongside the legacy role, for dispatch surfaces."""
        return {
            "stage_profile": self.stage_profile,
            "capability_profile": self.capability_profile,
            "role": self.role,
            "role_abbrev": self.abbrev,
        }


def expected_stage(
    from_state: str, to_state: str, intensity: Optional[str] = None
) -> Optional[StageExpectation]:
    """The profile pair a receipt for this edge must have been produced under.

    Resolution goes THROUGH the alias table rather than around it: the edge
    names a legacy role, `runtime_contracts.profile_pair_for_role` projects
    that name onto the pair, and the pair is what the receipt's overlay and
    `accountable_role` are then checked against. Resolved per call, not frozen
    at import, so the projection tables remain the single place the mapping
    lives: move a role there and what the kernel expects moves with it.

    Raises ValueError when the edge names a role the alias table does not
    know. That is a table inconsistency, not a caller error, and it fails
    closed at the gate (`NO_BOUND_ROLE`) rather than projecting the edge onto
    nothing.
    """
    bound = expected_receipt_role(from_state, to_state, intensity=intensity)
    if bound is None:
        return None
    role, abbrev = bound
    stage_profile, capability_profile = profile_pair_for_role(abbrev)
    return StageExpectation(stage_profile, capability_profile, role, abbrev)


def dispatch_profile(role: Any) -> Optional[Dict[str, str]]:
    """The profile pair a dispatch of `role` runs under, or None if unknown.

    The read-only surface the orchestrator and the host adapters use to state
    the pair in a dispatch prompt. Never raises: a role outside the table
    returns None, because a dispatch surface that crashed on an unrecognised
    role would be worse than one that says nothing about profiles.
    """
    try:
        stage_profile, capability_profile = profile_pair_for_role(role)
    except ValueError:
        return None
    return {
        "stage_profile": stage_profile,
        "capability_profile": capability_profile,
        "role": role_full_name(str(role)) or str(role),
        "role_abbrev": role_abbrev(str(role)) or str(role),
    }


def _overlay_value(
    receipt: Dict[str, Any], field: str
) -> Tuple[Optional[str], Optional[str]]:
    """(normalized value, problem). A present non-string is a problem, not an
    absence: `{"capability_profile": {"name": "prover"}}` must not read as
    "the agent did not stamp one"."""
    raw = receipt.get(field)
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        return None, "%s must be a string, got %r" % (field, raw)
    value = raw.strip().lower()
    if not value:
        return None, None
    return value, None


def profile_overlay_problems(
    receipt: Dict[str, Any], expected: StageExpectation
) -> Tuple[Optional[str], List[str]]:
    """Refusals and warnings for a receipt's #342 overlay pair (#402).

    Returns `(refusal or None, warnings)`.

    Hard on CONTRADICTION here, and soft on ABSENCE here only. #342 shipped
    the pair as copy-verbatim-from-the-dispatch-contract with nothing enforcing
    it, so requiring presence on every edge would refuse every agent that has
    not been taught to stamp it. Since #447 a GOVERNED dispatch does hand an
    envelope over, and #473 makes the pair required for exactly those: that
    requirement lives in `receipt_validator`, which fires at step 9 above and
    also runs on the SubagentStop path, so a governed receipt missing the pair
    never reaches this function. The warning below is therefore the
    UNGOVERNED case -- a dispatch that handed over no envelope, so there was
    nothing to copy.

    What already matters is that a PRESENT value cannot disagree. The pair is
    what the control plane reads as the stage an item of evidence was produced
    under (#407 ingest, gate depth on /quality), so a producing stage that
    could stamp `capability_profile: prover` would have its own output ingested
    as verification of itself. The refusal below is what makes that impossible;
    it does not depend on the agent being honest, only on the pair being
    checkable against the edge.
    """
    warnings: List[str] = []
    stage_value, stage_problem = _overlay_value(receipt, "stage_profile")
    capability_value, capability_problem = _overlay_value(receipt, "capability_profile")
    shape_problem = stage_problem or capability_problem
    if shape_problem:
        return shape_problem, warnings

    disagreements: List[str] = []
    if stage_value is not None and stage_value != expected.stage_profile:
        disagreements.append(
            "stage_profile %r (this edge is %r)" % (stage_value, expected.stage_profile)
        )
    if capability_value is not None and capability_value != expected.capability_profile:
        disagreements.append(
            "capability_profile %r (this edge is %r)"
            % (capability_value, expected.capability_profile)
        )
    if disagreements:
        return (
            "receipt declares %s; the runtime-identity overlay is copied from "
            "the dispatch contract, never chosen" % "; ".join(disagreements),
            warnings,
        )

    if stage_value is None and capability_value is None:
        warnings.append(
            "profile_overlay_absent: receipt carries no runtime-identity "
            "overlay; this edge is stage_profile=%s capability_profile=%s "
            "(soft-required because this dispatch handed over no envelope to "
            "copy it from; required once one does, #473)"
            % (expected.stage_profile, expected.capability_profile)
        )
    elif stage_value is None or capability_value is None:
        warnings.append(
            "profile_overlay_absent: receipt carries half the runtime-identity "
            "overlay (missing %s)"
            % ("stage_profile" if stage_value is None else "capability_profile")
        )
    return None, warnings


def accountable_role_problems(
    receipt: Dict[str, Any], expected: StageExpectation
) -> Tuple[Optional[str], Optional[str], List[str]]:
    """`(code, refusal, warnings)` for a receipt's `accountable_role` (#402).

    Soft-required, per the decision pinned during #399: validated against the
    alias table when present, absence is never a failure.

    Two distinguishable refusals. A value outside the table names no role, so
    it cannot confer accountability on anyone (`ACCOUNTABLE_ROLE_INVALID`). A
    value inside the table whose pair is not this edge's pair claims the wrong
    kind of act (`PROFILE_MISMATCH`), which is the same forgery the overlay
    check refuses, arriving through a different field.

    A pair-EQUAL alias is accepted: `compliance-engineer` on a verifying/prover
    edge passes, because the pilot dissolves CE's obligations into checks the
    verifying stage runs (ADR-032 decision 2) and the pair is what this ticket
    keys on. Within-pair attribution therefore remains self-reported, and the
    receipt's own `role` -- which the DoD evaluator maps checks by -- is still
    pinned to the edge's exact alias by `ROLE_MISMATCH`.
    """
    warnings: List[str] = []
    value = receipt.get(ACCOUNTABLE_ROLE)
    if value is None:
        warnings.append(
            "accountable_role_absent: receipt carries no %s; accountability "
            "reads from the legacy role name (soft-required during the "
            "migration window)" % ACCOUNTABLE_ROLE
        )
        return None, None, warnings
    try:
        pair = profile_pair_for_role(value)
    except ValueError as exc:
        return ACCOUNTABLE_ROLE_INVALID, str(exc), warnings
    if pair != expected.pair:
        return (
            PROFILE_MISMATCH,
            "%s %r projects onto %s; this edge expects %s (aliases: %s)"
            % (
                ACCOUNTABLE_ROLE,
                value,
                "/".join(pair),
                "/".join(expected.pair),
                ", ".join(roles_for_profile_pair(*expected.pair)) or "none",
            ),
            warnings,
        )
    return None, None, warnings


def canonical_receipt_path(project_dir: str, story_id: str, abbrev: str) -> Path:
    """Derive the one receipt path this stage may consume.

    Callers never choose the file. `_resolve_receipts_dir` is reused so the gate
    and DoD aggregation cannot disagree about where receipts live (it is already
    spec-aware, which the previous flat-dir Cursor implementation was not).
    Refuses symlinks and any path escaping the receipts directory.
    """
    sp = _sp()
    directory = Path(sp._resolve_receipts_dir(str(project_dir)))
    name = sp.get_story_receipt_path(story_id, abbrev)
    found = sp.find_story_receipt_file(str(directory), story_id, abbrev)
    if found:
        candidate = Path(found)
        directory = candidate.parent
    else:
        candidate = directory / name
    if directory.is_symlink() or candidate.is_symlink():
        raise _Refusal(PATH_SYMLINK, "canonical receipt path may not be a symlink")
    resolved_dir = directory.resolve()
    resolved_candidate = candidate.resolve()
    try:
        resolved_candidate.relative_to(resolved_dir)
    except ValueError:
        raise _Refusal(
            PATH_ESCAPE, "canonical receipt path escapes its receipt directory"
        )
    return resolved_candidate


def intended_receipts_dir(project_dir: str) -> Path:
    """Where a receipt SHOULD be written, for a dispatch contract.

    Delegates to `story_pipeline.receipts_dir_for(intended=True)`. It used to
    resolve the active spec itself, which meant two independent derivations of
    one path -- and the divergence that produced was the reason this function
    was written in the first place. With SPQ adding a third layout, a second
    copy would diverge again, so there is now exactly one resolver and the only
    difference is the `intended` flag: reading prefers a scoped directory once
    it exists, a dispatch contract resolves it whether or not it does.
    """
    return Path(_sp().receipts_dir_for(str(project_dir), intended=True))


def _scoped_path(project_dir: str, raw: str) -> Path:
    """Resolve a caller-supplied path, refusing anything outside .orchestrator."""
    root = (Path(str(project_dir)) / ".synaptory" / ".orchestrator").resolve()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = Path(str(project_dir)) / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise _Refusal(
            PATH_ESCAPE, "receipt_path escapes the project orchestrator directory"
        )
    return resolved


def parse_timestamp(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def stage_entered_at(story: Dict[str, Any]) -> Optional[datetime]:
    """entered_at of the log entry matching the story's CURRENT state.

    Cursor read `pipeline_log[-1]`, which is the wrong entry whenever anything
    appended afterwards, and skipped the staleness check when it was absent.
    """
    current = str(story.get("state") or "")
    entries = story.get("pipeline_log")
    if not isinstance(entries, list):
        return None
    for entry in reversed(entries):
        if isinstance(entry, dict) and str(entry.get("state") or "") == current:
            return parse_timestamp(entry.get("entered_at"))
    return None


# ── done-edge DoD gate ───────────────────────────────────────────────────────


def _dod_intensity(project_dir: str, state: Dict[str, Any], mode: str) -> str:
    """Reproduce each lifecycle's own intensity resolver, exactly.

    The three wrappers genuinely differed and all three are preserved:
    scrum keys off the sprint number, kanban off the cumulative ticket number,
    and spq reuses resolve_dod_tier so a Cycle has one answer rather than two.
    """
    sp = _sp()
    if mode == "scrum":
        return sp.determine_dod_intensity(sprint_number=state.get("current_sprint", 1))
    if mode == "kanban":
        return sp.determine_dod_intensity(
            ticket_number=state.get("cumulative_ticket_number", 1)
        )
    try:
        return sp.resolve_dod_tier(str(project_dir), state).get("tier") or "early"
    except Exception:  # noqa: BLE001 - tier resolution must never break a gate
        return "early"


def resolve_done_edge(
    project_dir: str,
    state: Dict[str, Any],
    story_id: str,
    to_state: str,
    reason: Optional[str],
    mode: Optional[str] = None,
) -> Tuple[str, Optional[str], Optional[dict]]:
    """Apply the DoD gate and redirects for any landing on done / acceptance.

    Returns (effective_to_state, reason, dod_result). A red DoD redirects to
    `blocked` and a live per-story-acceptance toggle redirects to
    `awaiting_acceptance` (only from `reviewing -> done`), both matching the
    existing wrapper behaviour. The compliance check is evaluated first
    because a failed gate is more fundamental than acceptance routing.
    """
    sp = _sp()
    if mode is None:
        mode = build_mode(project_dir)
    story = sp.get_story(state, story_id)
    from_state = str((story or {}).get("state") or "")
    if to_state not in _DONE_EDGE:
        return to_state, reason, None

    intensity = _dod_intensity(project_dir, state, mode)
    dod = sp.evaluate_story_dod(str(project_dir), story_id, intensity)
    block = sp.dod_gate_block_reason(dod)
    if block:
        return "blocked", block, dod
    # Kanban has no PO acceptance step, matching kanban_state_machine today.
    if (
        to_state == "done"
        and from_state == "reviewing"
        and mode != "kanban"
        and sp.is_per_story_acceptance_enabled(str(project_dir))
    ):
        return "awaiting_acceptance", reason, dod
    return to_state, reason, dod


# ── evaluation ───────────────────────────────────────────────────────────────


def _record(checks: List[dict], name: str, passed: bool, detail: str = "") -> None:
    entry = {"check": name, "passed": passed}
    if detail:
        entry["detail"] = detail
    checks.append(entry)


def evaluate_advance(
    project_dir: str,
    story_id: str,
    to_state: str,
    receipt_path: Optional[str] = None,
    reason: Optional[str] = None,
    policy: Optional[HostPolicy] = None,
    state: Optional[Dict[str, Any]] = None,
) -> Decision:
    """Decide whether this advance may proceed. Pure: never writes state.

    Check order is fail-fast and deliberate: policy and readiness first (cheap,
    and a refused project should not have its receipts inspected at all), then
    identity, then evidence, then the DoD gate last because it is the most
    expensive.
    """
    policy = policy or HostPolicy(host="unknown")
    sp = _sp()
    checks: List[dict] = []
    warnings: List[str] = []
    project_dir = str(project_dir)

    def _fail(code: str, reason_text: str, **extra: Any) -> Decision:
        """Refuse, or in warn mode downgrade an evidence check to a warning."""
        _record(checks, code, False, reason_text)
        if policy.enforcement == "warn" and code in _EVIDENCE_CODES:
            warnings.append("%s: %s" % (code, reason_text))
            return None  # type: ignore[return-value]
        return Decision(
            allowed=False,
            code=code,
            reason=reason_text,
            warnings=warnings,
            checks=checks,
            **extra,
        )

    try:
        # 1. host policy (BAA / regulated-project posture)
        if policy.policy_gate is not None:
            refusal = policy.policy_gate(project_dir)
            if refusal:
                _record(checks, POLICY_REFUSED, False)
                return Decision(
                    allowed=False,
                    code=POLICY_REFUSED,
                    reason=str(refusal.get("error") or "refused by host policy"),
                    warnings=warnings,
                    checks=checks,
                    extra=dict(refusal),
                )
        _record(checks, "policy_gate", True)

        # 2. host readiness (Codex execution readiness probe)
        if policy.readiness_gate is not None:
            refusal = policy.readiness_gate(project_dir)
            if refusal:
                _record(checks, NOT_READY, False)
                return Decision(
                    allowed=False,
                    code=NOT_READY,
                    reason=str(refusal.get("error") or "host is not ready"),
                    warnings=warnings,
                    checks=checks,
                    extra=dict(refusal),
                )
        _record(checks, "readiness_gate", True)

        # 3. request shape + story existence
        if not story_id or not to_state:
            return Decision(
                allowed=False,
                code=BAD_REQUEST,
                reason="story_id and to_state are required",
                warnings=warnings,
                checks=checks,
            )
        if state is None:
            state = sp._read_state(project_dir)
        story = sp.get_story(state, story_id)
        if not story:
            _record(checks, STORY_NOT_FOUND, False)
            return Decision(
                allowed=False,
                code=STORY_NOT_FOUND,
                reason="story not found: %s" % story_id,
                warnings=warnings,
                checks=checks,
            )
        from_state = str(story.get("state") or "")
        _record(checks, "story_exists", True)

        # Legality is pre-checked for a precise code; transition_story remains
        # the authority and re-checks on execute.
        valid_targets = sp.VALID_TRANSITIONS.get(from_state, [])
        if to_state not in valid_targets:
            _record(checks, ILLEGAL_TRANSITION, False)
            return Decision(
                allowed=False,
                code=ILLEGAL_TRANSITION,
                reason="illegal transition %s -> %s" % (from_state, to_state),
                warnings=warnings,
                checks=checks,
                story=story,
            )
        _record(checks, "transition_legal", True)

        # 4. deterministic next_action binding
        selected: Optional[Dict[str, Any]] = None
        next_action_error: Optional[str] = None
        try:
            selected = next_action(project_dir)
        except Exception as exc:  # noqa: BLE001 - refuse when the host binds strictly
            next_action_error = str(exc)
            warnings.append("next_action unavailable: %s" % exc)
        if policy.require_next_action_match:
            if next_action_error is not None:
                _record(checks, NEXT_ACTION_MISMATCH, False, next_action_error)
                return Decision(
                    allowed=False,
                    code=NEXT_ACTION_MISMATCH,
                    reason="next_action unavailable: %s" % next_action_error,
                    warnings=warnings,
                    checks=checks,
                    story=story,
                )
            if selected is not None:
                if (
                    selected.get("story_id") != story_id
                    or selected.get("transition_to") != to_state
                ):
                    bad = _fail(
                        NEXT_ACTION_MISMATCH,
                        "requested transition does not match deterministic next_action",
                        next_action=selected,
                        story=story,
                    )
                    if bad is not None:
                        return bad
                else:
                    _record(checks, "next_action_match", True)

        # 5. profile-keyed stage binding for this edge (#402)
        intensity: Optional[str] = None
        try:
            intensity = _dod_intensity(project_dir, state, build_mode(project_dir))
        except Exception:  # noqa: BLE001 - keep the static table when mode is unreadable
            intensity = None
        try:
            expected = expected_stage(from_state, to_state, intensity=intensity)
        except ValueError as exc:
            # The edge names a role the alias table does not know, so there is
            # no pair to hold the receipt to. Fail closed rather than fall back
            # to comparing names: an unprojectable edge is a table bug, and
            # advancing past it would be advancing past the whole check.
            raise _Refusal(
                NO_BOUND_ROLE,
                "edge %s -> %s does not resolve to a capability profile: %s"
                % (from_state, to_state, exc),
            )
        gated = to_state != "blocked" or policy.gate_blocked_transitions
        digest: Optional[str] = None
        path: Optional[Path] = None
        if expected is None or not gated:
            table_bound = TRANSITION_RECEIPT.get((from_state, to_state))
            if (
                expected is None
                and gated
                and to_state != "blocked"
                and table_bound is None
            ):
                bad = _fail(
                    NO_BOUND_ROLE,
                    "no bound receipt role for %s -> %s" % (from_state, to_state),
                    story=story,
                )
                if bad is not None:
                    return bad
            if expected is None and table_bound is not None:
                _record(
                    checks,
                    "receipt_required",
                    False,
                    "receipt waived for DoD intensity",
                )
            else:
                _record(checks, "receipt_required", False, "edge is not receipt-gated")
        else:
            expected_role = expected.role
            abbrev = expected.abbrev
            _record(
                checks,
                "role_bound",
                True,
                "%s (%s/%s)"
                % (expected_role, expected.stage_profile, expected.capability_profile),
            )

            # 6. canonical path derivation (caller cannot choose the file)
            path = canonical_receipt_path(project_dir, story_id, abbrev)

            # 7. a supplied path must resolve to exactly that file
            if receipt_path:
                supplied = _scoped_path(project_dir, str(receipt_path))
                if supplied != path:
                    bad = _fail(
                        PATH_NOT_CANONICAL,
                        "receipt_path must be the canonical %s for this story and stage"
                        % path.name,
                        story=story,
                    )
                    if bad is not None:
                        return bad
            _record(checks, "canonical_path", True, str(path))

            if not path.is_file():
                bad = _fail(
                    NO_RECEIPT,
                    "no receipt on disk for %s (expected %s)" % (story_id, path.name),
                    next_action=selected,
                    story=story,
                )
                if bad is not None:
                    return bad
            else:
                # 8. read once: the same bytes produce digest and payload
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                try:
                    receipt = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    bad = _fail(
                        RECEIPT_UNREADABLE,
                        "receipt is not valid JSON: %s" % exc,
                        story=story,
                    )
                    if bad is not None:
                        return bad
                    receipt = {}
                if not isinstance(receipt, dict):
                    bad = _fail(
                        RECEIPT_UNREADABLE,
                        "receipt must contain a JSON object",
                        story=story,
                    )
                    if bad is not None:
                        return bad
                    receipt = {}

                # 9. schema validation against the already-read payload
                from receipt_validator import validate_receipt_payload  # type: ignore

                result = validate_receipt_payload(receipt, project_dir)
                warnings.extend(result.warnings or [])
                if not result.valid:
                    bad = _fail(
                        RECEIPT_INVALID,
                        "receipt invalid",
                        story=story,
                        extra={"errors": result.errors},
                    )
                    if bad is not None:
                        return bad
                _record(checks, "receipt_valid", bool(result.valid))

                # 10. payload binding
                if receipt.get("story_id") != story_id:
                    bad = _fail(
                        STORY_MISMATCH,
                        "receipt story_id mismatch (expected %s)" % story_id,
                        story=story,
                    )
                    if bad is not None:
                        return bad
                if receipt.get("role") != expected_role:
                    bad = _fail(
                        ROLE_MISMATCH,
                        "expected a %s receipt" % expected_role,
                        story=story,
                    )
                    if bad is not None:
                        return bad

                # 10b. #402: the runtime-identity overlay and the
                # accountability label are held to the pair this edge's alias
                # resolves to. Both are self-declared fields the control plane
                # reads, so a present value that contradicts the edge refuses;
                # an absent one warns, because no dispatch hands the agent an
                # envelope to copy from yet (#447).
                overlay_refusal, overlay_warnings = profile_overlay_problems(
                    receipt, expected
                )
                warnings.extend(overlay_warnings)
                if overlay_refusal is not None:
                    bad = _fail(PROFILE_MISMATCH, overlay_refusal, story=story)
                    if bad is not None:
                        return bad
                accountable_code, accountable_refusal, accountable_warnings = (
                    accountable_role_problems(receipt, expected)
                )
                warnings.extend(accountable_warnings)
                if accountable_refusal is not None:
                    bad = _fail(
                        accountable_code or ACCOUNTABLE_ROLE_INVALID,
                        accountable_refusal,
                        story=story,
                    )
                    if bad is not None:
                        return bad
                _record(
                    checks,
                    "profile_bound",
                    True,
                    "%s/%s" % (expected.stage_profile, expected.capability_profile),
                )

                active_dispatches = story.get("mcp_active_dispatches") or {}
                active = (
                    active_dispatches.get(abbrev)
                    if isinstance(active_dispatches, dict)
                    else None
                )
                # THE SELECTION IS THE AUTHORITY ON WHICH EXECUTOR RAN, and it
                # is resolved BEFORE the legacy host-backend rule (#396).
                #
                # Ordering is the whole finding. The host rule requires
                # `backend == codex` on Codex and `cursor` on Cursor, so when
                # Codex selects claude-code the Claude adapter's correct
                # `backend: claude` receipt was rejected as `backend_mismatch`
                # before the family comparison ran. #346's whole point, a host
                # invoking a runtime that is not its own family, could not land
                # evidence on either real-code host: the enforcement worked
                # only in the negative direction.
                #
                # So a binding that CARRIES a selection is judged against that
                # selection, and a binding from before selection existed keeps
                # the host-backend rule.
                # ONE derivation, two readers. `receipt_validator` asks the
                # same helper whether this dispatch was governed (#473), so the
                # field that decides which executor was authorized is the field
                # that decides whether the overlay pair was handed over.
                selected_family = dispatch_runtime_authority(story, abbrev).get(
                    "runtime_family", ""
                )
                if selected_family:
                    produced_by = _receipt_runtime_family(receipt)
                    if produced_by and produced_by != selected_family:
                        bad = _fail(
                            RUNTIME_FAMILY_MISMATCH,
                            (
                                "this dispatch selected the %s runtime, so a "
                                "receipt produced by %s cannot be accepted for "
                                "it: the selection decides which executor runs, "
                                "not which one reports"
                                % (selected_family, produced_by)
                            ),
                            story=story,
                        )
                        if bad is not None:
                            return bad
                    _record(
                        checks,
                        "runtime_family_bound",
                        True,
                        "%s (selected)" % selected_family,
                    )
                elif (
                    policy.expected_backend
                    and receipt.get("backend") != policy.expected_backend
                ):
                    bad = _fail(
                        BACKEND_MISMATCH,
                        "receipt backend must be %s" % policy.expected_backend,
                        story=story,
                    )
                    if bad is not None:
                        return bad
                if not isinstance(active, dict):
                    active = {}
                if policy.require_dispatch_binding:
                    # Two different absences, and #592's audit found them
                    # collapsed into one. Reproduced on `f3df953f`: with NO
                    # dispatch ever begun, a receipt carrying an invented
                    # `dispatch_id` advanced `testing -> reviewing` under this
                    # very policy, because the comparison below ran only when
                    # the kernel already held a binding. Absence of all
                    # authority read as authorization.
                    #
                    # * The kernel HOLDS a binding: the receipt must match it.
                    #   Unchanged.
                    # * The kernel holds NONE and the receipt CLAIMS one: the
                    #   claim names an authority that was never granted, and
                    #   nothing can refute it. Refused.
                    # * The kernel holds none and the receipt claims none: an
                    #   unbound receipt, honest about having nothing. Allowed,
                    #   because the Claude orchestrator calls `begin_dispatch`
                    #   only for `dispatch_se`, so QE and CR advance this way
                    #   on every ordinary story. Failing closed here would
                    #   refuse the normal pipeline, which is a different defect
                    #   rather than a safer one.
                    dispatch_id = active.get("dispatch_id")
                    claimed_dispatch = receipt.get("dispatch_id")
                    if dispatch_id and claimed_dispatch != dispatch_id:
                        bad = _fail(
                            DISPATCH_BINDING_MISMATCH,
                            "receipt dispatch_id does not match the active dispatch",
                            story=story,
                        )
                        if bad is not None:
                            return bad
                    if not dispatch_id and claimed_dispatch:
                        bad = _fail(
                            DISPATCH_BINDING_MISMATCH,
                            (
                                "receipt claims dispatch %s, but this kernel "
                                "authorized no dispatch for %s at the %s stage. "
                                "A dispatch identity the kernel never issued "
                                "cannot be checked against anything, so it is "
                                "refused rather than credited: an unrefutable "
                                "claim of authority is not authority (#592). A "
                                "receipt that claims no dispatch is a different "
                                "thing and still advances."
                                % (claimed_dispatch, story_id, abbrev)
                            ),
                            story=story,
                        )
                        if bad is not None:
                            return bad
                # Epic #339 / #447. UNCONDITIONAL for every host, unlike the
                # dispatch-id check above, because every check below fires on
                # MISMATCH and never on absence: a receipt that omits attempt
                # identity advances exactly as it does today, and only a receipt
                # that names a DIFFERENT attempt, a superseded generation, or an
                # attempt recorded as no longer live is refused.
                #
                # These were gated on `require_dispatch_binding` until #447,
                # which is why `policy_for_claude` -- the host that ships that
                # flag off -- enforced none of them while recording every field
                # they read (recording became unconditional in #344, ADR-031
                # decision 2). Since #447 the kernel also hands the agent an
                # envelope carrying these values, so a receipt that contradicts
                # one is contradicting something it was given, on every host.
                # Absence stays unenforced: tightening presence needs a producer
                # that stamps it, which is the receipt-contract half.
                attempt_id = active.get("attempt_id")
                receipt_attempt = receipt.get("attempt_id")
                if attempt_id and receipt_attempt and receipt_attempt != attempt_id:
                    bad = _fail(
                        ATTEMPT_BINDING_MISMATCH,
                        "receipt attempt_id does not match the active attempt",
                        story=story,
                    )
                    if bad is not None:
                        return bad
                # THE SAME FORGERY ONE LEVEL IN. #606 refused an invented
                # `dispatch_id` and an invented item-level attestation when the
                # kernel had authorized nothing, and left this field, which is
                # the one that pays: `story_pipeline.backing_evidence_class`
                # credits an ordinary receipt-backed check as `attested`
                # BECAUSE this value is present. Reproduced on `514fbed7` with
                # no dispatch, no `dispatch_id`, no typed evidence and an
                # invented top-level attempt id: advanced to `reviewing`, and
                # the check classed `attested`.
                #
                # So #606's "the receipt claims none" case was not claiming
                # none. It still carried an execution identity, and it earned
                # attempt-scoped credit for it. A receipt carrying NO attempt
                # id at all is the genuinely unbound one, is still allowed for
                # the Claude QE and CR gap, and `backing_evidence_class`
                # already gives it no class.
                #
                # AND IT IS THE LIVE BINDING, NOT THE LEDGER. #608 asked
                # `_authorized_attempts`, which unions the live binding with
                # the role's whole history and never retires an id once its
                # advance pops the binding. The lifecycle legally returns a
                # unit to the same role stage (`needs-fix` sends
                # `awaiting_acceptance -> in_progress`, and the blocked
                # recovery edges do the same), and on that second cycle there
                # is no live binding, so a NEW receipt naming the PREVIOUS,
                # already-completed attempt passed and earned `attested`.
                # Reproduced on `3806c1be` through the public path.
                #
                # The ledger is evidence that an attempt once ran. It is not
                # evidence that it produced THIS receipt. Top-level receipt
                # identity therefore answers to the live binding only. The
                # nested `evidence[]` rule below keeps using the history union
                # on purpose: a predecessor attempt genuinely can be the
                # producer of a finding an item attests to.
                if not attempt_id and receipt_attempt:
                    bad = _fail(
                        ATTEMPT_BINDING_MISMATCH,
                        (
                            "receipt claims attempt %s, but this kernel holds "
                            "no live dispatch for %s at the %s stage. The "
                            "identity is what earns `attested` credit, so it "
                            "must name the execution that is running now: a "
                            "completed attempt proves something once ran, not "
                            "that it produced this receipt, and an unissued one "
                            "proves nothing at all (#592). A receipt claiming "
                            "no attempt still advances and is credited no class."
                            % (receipt_attempt, story_id, abbrev)
                        ),
                        story=story,
                    )
                    if bad is not None:
                        return bad
                # #493. The item-level half of the same question, and the half
                # nothing asked. The check above compares the receipt's
                # TOP-LEVEL attempt_id and never looks inside `evidence`, so a
                # receipt could be correctly bound at the top and still carry
                # an `attested` grade naming an execution that never ran. What
                # stood between that grade and the gate was ATTEMPT_ID_RE, and
                # a format check refuses a typo of the forgery, not the
                # forgery. The control plane then counted it as attested
                # depth, because #432 correctly derives depth from the
                # discipline the class names and this WAS the discipline.
                #
                # Absence is tolerated in exactly one place, and it is the
                # STORY, not the item: an item with no attempt_id is already
                # refused by `validate_evidence_item`, so there is no
                # migration window to protect there, but a stage the kernel
                # never authorized an attempt for has nothing to compare
                # against and cannot be made to have one by refusing. That
                # leaves a named residual rather than a silent one: on a host
                # that does not own every launch, a producer that skips
                # `begin_dispatch` faces the shape check alone. The warning
                # below says so at the moment it happens.
                claims = attested_attempt_claims(receipt)
                authorized = _authorized_attempts(story, abbrev)
                if claims and authorized:
                    unbound = [
                        (index, claimed)
                        for index, claimed in claims
                        if claimed not in authorized
                    ]
                    if unbound:
                        index, claimed = unbound[0]
                        bad = _fail(
                            ATTEMPT_BINDING_MISMATCH,
                            (
                                "evidence[%d] attests to attempt %s, which this "
                                "kernel never authorized for %s at the %s stage. "
                                "An attested item's attempt identity is the "
                                "execution that PRODUCED the finding, not a "
                                "string of the right shape; authorized here: %s"
                                % (
                                    index,
                                    claimed,
                                    story_id,
                                    abbrev,
                                    ", ".join(authorized),
                                )
                            ),
                            story=story,
                        )
                        if bad is not None:
                            return bad
                    _record(
                        checks,
                        "attested_evidence_bound",
                        True,
                        "%d attested item(s) bound to an authorized attempt"
                        % len(claims),
                    )
                elif claims:
                    # #493's acceptance criterion is that an attested item
                    # naming an attempt the kernel did not authorize cannot
                    # advance. This branch used to WARN and continue, which
                    # satisfied that criterion only when the kernel happened to
                    # hold an authorized set: with none, every invented attempt
                    # passed, and `backing_evidence_class` then credited the
                    # well-shaped id as `attested` gate depth (#592 audit,
                    # reproduced through `execute_advance`).
                    #
                    # An attested item's whole content is WHICH execution
                    # produced the finding. When the kernel authorized nothing,
                    # that identity is unrefutable, and an unrefutable claim is
                    # not evidence. A receipt carrying no attested items is
                    # unaffected: it claims no execution and is credited as
                    # `replayed` or `judged` on its own terms.
                    index, claimed = claims[0]
                    bad = _fail(
                        ATTEMPT_BINDING_MISMATCH,
                        (
                            "evidence[%d] attests to attempt %s, but this "
                            "kernel authorized no attempt for %s at the %s "
                            "stage, so the attestation is bound to nothing and "
                            "cannot be refuted. Dispatch through the kernel to "
                            "make it refutable (#493, #592)."
                            % (index, claimed, story_id, abbrev)
                        ),
                        story=story,
                    )
                    if bad is not None:
                        return bad

                bound_token = active.get("fencing_token")
                receipt_token = receipt.get("fencing_token")
                # PRESENCE, and only where presence is owed. Omitting the field
                # used to skip the comparison below, so a superseded runner had
                # a cheaper move than presenting an old token: present none.
                #
                # Requiring it unconditionally was wrong and stayed unshipped
                # for a measured reason: `execute_dispatch` mints a fencing
                # token for EVERY dispatch, including ordinary Claude SE work
                # where the agent writes its own receipt and no bridge stamps
                # it, so a blanket requirement refuses the normal pipeline.
                #
                # What was missing was a way to tell those apart, and the
                # kernel already writes one. `_record_selected_family` stamps
                # the chosen runtime family onto the binding when runtime
                # selection SELECTS a profile, which is the same branch that
                # builds the dispatch envelope the bridge runs under, and that
                # bridge stamps the generation onto the receipt (#607). So a
                # binding carrying a runtime family is one whose receipt was
                # produced by a stamping producer, and absence there is a
                # missing proof rather than an agent that never learned to
                # echo one.
                #
                # Keyed on the BINDING, never on a receipt field. The kernel
                # wrote it at dispatch, so a forger cannot opt out of the
                # requirement by omitting something; omitting is the exact
                # thing being refused.
                governed = str(active.get("runtime_family") or "").strip()
                if bound_token and governed and not receipt_token:
                    return Decision(
                        allowed=False,
                        code=FENCING_TOKEN_MISSING,
                        reason=(
                            "this attempt was placed on the %s runtime, whose "
                            "bridge stamps the lease generation onto every "
                            "receipt it produces, and this receipt carries "
                            "none. Without it the runner cannot show which "
                            "generation it holds, so a superseded one would "
                            "land its result by saying nothing" % governed
                        ),
                        warnings=warnings,
                        checks=checks,
                        story=story,
                    )
                if bound_token and receipt_token and receipt_token != bound_token:
                    # Not downgradable under warn: see the note beside
                    # `_EVIDENCE_CODES`. The token is the only refutable claim
                    # a runner can make about WHICH generation it holds, so a
                    # superseded one must not be able to land a result.
                    return Decision(
                        allowed=False,
                        code=FENCING_TOKEN_STALE,
                        reason=(
                            "receipt presents a superseded fencing token; this "
                            "attempt's lease has moved to a later generation, so "
                            "the runner holding that token no longer owns the "
                            "attempt and its result cannot be accepted"
                        ),
                        warnings=warnings,
                        checks=checks,
                        story=story,
                    )
                # THE SAME PREDICATE `next_action` ROUTES AROUND (#690). It
                # used to be an inline read of `active["state"]`, which is why
                # the deterministic planner could offer a transition this
                # refuses: nothing shared the question, so only one side asked
                # it. See `unadvanceable_attempt`.
                #
                # `-> blocked` IS EXEMPT, AND ONLY `-> blocked`. This refusal
                # exists to stop a dead attempt creating an ACCEPTED RESULT;
                # parking a Work Unit accepts nothing, which is the same
                # reason `policy_for_claude` leaves the edge ungated. Without
                # the exemption the recovery ladder re-deadlocks at its own
                # exhaustion tier on the two MCP hosts, which do gate that
                # edge: `next_action` selects `block_story` and the advance
                # that would park the unit hits this refusal again. Every
                # other check on the edge still runs, so de-escalating is not
                # a bypass.
                dead_attempt = (
                    None
                    if to_state == "blocked"
                    else unadvanceable_attempt(story, abbrev)
                )
                if dead_attempt is not None:
                    return Decision(
                        allowed=False,
                        code=ATTEMPT_NOT_LIVE,
                        reason=(
                            "the attempt that produced this receipt is recorded "
                            "as %s, so it cannot create an accepted result. "
                            "`next_action` selects the governed recovery for "
                            "this Work Unit: call begin_dispatch for the %s "
                            "stage, which archives this receipt under the "
                            "%s attempt and mints its successor"
                            % (
                                dead_attempt["attempt_state"],
                                abbrev,
                                dead_attempt["attempt_state"],
                            )
                        ),
                        warnings=warnings,
                        checks=checks,
                        story=story,
                    )
                bound_hash = active.get("manifest_hash")
                receipt_hash = receipt.get("manifest_hash")
                if bound_hash and receipt_hash and receipt_hash != bound_hash:
                    # Not downgradable under warn: see the note beside
                    # MANIFEST_DISAGREEMENT.
                    return Decision(
                        allowed=False,
                        code=MANIFEST_DISAGREEMENT,
                        reason=(
                            "receipt manifest_hash disagrees with the sealed "
                            "manifest this attempt was dispatched against"
                        ),
                        checks=checks,
                        story=story,
                    )
                _record(checks, "payload_bound", True)

                # 11. freshness against the CURRENT stage's entered_at
                entered = stage_entered_at(story)
                completed = parse_timestamp(receipt.get("completed_at"))
                if entered is None:
                    if policy.entered_at_missing == "refuse":
                        bad = _fail(
                            ENTERED_AT_MISSING,
                            "current stage has no valid entered_at timestamp",
                            story=story,
                        )
                        if bad is not None:
                            return bad
                    else:
                        warnings.append(
                            "stage has no entered_at; staleness check skipped"
                        )
                elif completed is None or not _sp().receipt_timestamp_is_fresh(
                    completed, entered
                ):
                    bad = _fail(
                        STALE_RECEIPT,
                        "receipt is stale (completed_at must be after the current "
                        "stage entered_at)",
                        story=story,
                    )
                    if bad is not None:
                        return bad
                else:
                    _record(checks, "receipt_fresh", True)

                # 12. anti-replay
                consumed = story.get("mcp_consumed_receipts", [])
                if not isinstance(consumed, list):
                    bad = _fail(
                        LEDGER_INVALID,
                        "consumed-receipt ledger is invalid",
                        story=story,
                    )
                    if bad is not None:
                        return bad
                    consumed = []
                if digest in consumed:
                    bad = _fail(
                        REPLAY, "receipt already consumed (replay)", story=story
                    )
                    if bad is not None:
                        return bad
                _record(checks, "not_replayed", True)

        # 13. DoD gate on every path that lands on done / awaiting_acceptance
        effective_to_state = to_state
        effective_reason = reason
        dod: Optional[dict] = None
        if policy.dod_on_done and to_state in _DONE_EDGE:
            effective_to_state, effective_reason, dod = resolve_done_edge(
                project_dir, state, story_id, to_state, reason
            )
            if (
                policy.dod_requires_pass
                and effective_to_state != "blocked"
                and dod is not None
                and not dod.get("passed")
            ):
                effective_to_state = "blocked"
                effective_reason = "Definition of Done did not pass"
            if effective_to_state == "blocked":
                if policy.dod_red_behavior == "refuse":
                    _record(checks, DOD_FAILED, False, str(effective_reason or ""))
                    # THE REFUSAL IS RECORDED, so the next `next_action` has
                    # something to route on (#396). `refuse` means the board
                    # does not MOVE on red evidence, and that is right; it did
                    # not mean the board should learn nothing. Without this,
                    # the unit stayed in `reviewing`, `next_action` advertised
                    # `promote_story -> done` again, and a compliant
                    # orchestrator retried an action that cannot succeed,
                    # forever.
                    #
                    # The story's STATE is untouched. What is written is the
                    # gate's own reason, which is what routes the unit to the
                    # agent that owes the missing result rather than to the SE
                    # retry ladder, which cannot clear a conditional gate.
                    _note_dod_gate_refusal(
                        project_dir, story_id, str(effective_reason or "")
                    )
                    return Decision(
                        allowed=False,
                        code=DOD_FAILED,
                        reason="Definition of Done did not pass: %s"
                        % (effective_reason or "unknown"),
                        warnings=warnings,
                        checks=checks,
                        dod=dod,
                        story=story,
                        receipt_path=str(path) if path else None,
                        receipt_digest=digest,
                    )
                # redirect_blocked: allowed, but lands on `blocked`
                _record(checks, "dod_gate", False, "redirected to blocked")
            else:
                _record(checks, "dod_gate", True)

        return Decision(
            allowed=True,
            code=OK,
            reason="",
            warnings=warnings,
            checks=checks,
            receipt_path=str(path) if path else None,
            receipt_digest=digest,
            effective_to_state=effective_to_state,
            dod=dod,
            story=story,
            next_action=selected,
            role=expected.role if expected else None,
            profile=expected.as_dict() if expected else None,
        )

    except _Refusal as refusal:
        bad = _fail(refusal.code, refusal.reason, **refusal.extra)
        if bad is not None:
            return bad
        # warn mode swallowed a path refusal; without a canonical path there is
        # nothing further to check, so allow with the warning recorded.
        return Decision(
            allowed=True,
            code=OK,
            warnings=warnings,
            checks=checks,
            effective_to_state=to_state,
        )


# ── execution ────────────────────────────────────────────────────────────────


def execute_advance(
    project_dir: str,
    story_id: str,
    to_state: str,
    receipt_path: Optional[str] = None,
    reason: Optional[str] = None,
    policy: Optional[HostPolicy] = None,
) -> Decision:
    """Evaluate, then apply the transition in a single state write.

    The previous MCP implementations transitioned (write), then appended the
    consumed digest (write again). A crash between the two left the receipt
    replayable. Here the transition, the ledger append and the DoD record are
    all staged in memory and persisted once.
    """
    policy = policy or HostPolicy(host="unknown")
    sp = _sp()
    project_dir = str(project_dir)

    try:
        with _state_transaction(project_dir):
            return _execute_advance_locked(
                project_dir, story_id, to_state, receipt_path, reason, policy, sp
            )
    except _StateLockTimeout as exc:
        return Decision(
            allowed=False,
            code=STATE_LOCK_TIMEOUT,
            reason=str(exc),
        )


def _execute_advance_locked(
    project_dir: str,
    story_id: str,
    to_state: str,
    receipt_path: Optional[str],
    reason: Optional[str],
    policy: HostPolicy,
    sp: Any,
) -> Decision:
    """Body of execute_advance, run while holding the state lock."""
    state = sp._read_state(project_dir)
    decision = evaluate_advance(
        project_dir,
        story_id,
        to_state,
        receipt_path=receipt_path,
        reason=reason,
        policy=policy,
        state=state,
    )
    if not decision.allowed:
        return decision

    # A host that promises central receipt analytics must establish durable
    # handoff before the receipt can mutate lifecycle state. The callback runs
    # under the same state lock as validation and mutation, so the shipped
    # source digest is the exact evidence digest this transition consumes.
    if decision.receipt_path and policy.receipt_delivery_gate is not None:
        try:
            delivery = policy.receipt_delivery_gate(project_dir, decision.receipt_path)
        except Exception as exc:  # noqa: BLE001 - delivery failure is a refusal
            delivery = {
                "handed_off": False,
                "error": "receipt delivery raised %s" % type(exc).__name__,
            }
        if not isinstance(delivery, dict) or delivery.get("handed_off") is not True:
            detail = (
                str(delivery.get("error") or "durable receipt handoff failed")
                if isinstance(delivery, dict)
                else "receipt delivery returned a non-object"
            )
            return Decision(
                allowed=False,
                code=RECEIPT_DELIVERY_FAILED,
                reason=detail,
                warnings=decision.warnings,
                checks=decision.checks + [
                    {"name": "control_plane_delivery", "passed": False, "detail": detail}
                ],
                receipt_path=decision.receipt_path,
                receipt_digest=decision.receipt_digest,
                story=decision.story,
                next_action=decision.next_action,
                role=decision.role,
                extra={
                    "control_plane_delivery": (
                        delivery if isinstance(delivery, dict) else {"handed_off": False}
                    )
                },
            )
        decision.checks.append(
            {"name": "control_plane_delivery", "passed": True, "detail": delivery.get("mode")}
        )
        decision.extra["control_plane_delivery"] = delivery

    target = decision.effective_to_state or to_state
    effective_reason = reason
    if target == "blocked" and not effective_reason:
        # resolve_done_edge produced the block reason during evaluation.
        _, effective_reason, _ = resolve_done_edge(
            project_dir, state, story_id, to_state, reason
        )
    if decision.receipt_path and not effective_reason:
        effective_reason = "receipt validated: %s" % Path(decision.receipt_path).name

    state = sp.transition_story(state, story_id, target, effective_reason, project_dir)

    story = sp.get_story(state, story_id)
    if story is None:
        raise ValueError("transitioned story disappeared from state: %s" % story_id)

    if decision.receipt_digest:
        ledger = story.setdefault("mcp_consumed_receipts", [])
        if not isinstance(ledger, list):
            raise ValueError("transitioned story has an invalid consumed-receipt ledger")
        ledger.append(decision.receipt_digest)
    if decision.role:
        abbrev = role_abbrev(decision.role)
        active_dispatches = story.get("mcp_active_dispatches")
        if abbrev and isinstance(active_dispatches, dict):
            popped = active_dispatches.pop(abbrev, None)
            # #492. The pop is the other way an attempt leaves the board, and
            # it is the GOOD one: its stage advanced, so its work landed. An
            # attempt that succeeded and one that was abandoned mid-flight
            # must not read the same in the history, which they did while the
            # only outcome field was written by supersession.
            if isinstance(popped, dict):
                record = _history_record(
                    story, abbrev, str(popped.get("attempt_id") or ""), create=True
                )
                if record is not None:
                    record.setdefault("advanced_at", _now_iso())
                    for key in ("state", "failure_class", "failure_class_source"):
                        value = str(popped.get(key) or "")
                        if value and not record.get(key):
                            record[key] = value

    dod = decision.dod
    # Keyed on the edge the caller ASKED for, never on where the story landed.
    # `target` was the condition, so a done edge the DoD gate REDIRECTED to
    # `blocked` recorded no verdict and shipped none: the strongest signal the
    # pipeline produces -- why the unit stopped -- was the one signal that
    # never left the machine, and the Sync barrier's `dod_evaluated` criterion
    # had nothing to read on the units it stopped.
    #
    # `scrum_state_machine` closed exactly this hole on its own path in #403.
    # The shared kernel that kanban, SPQ and both MCP hosts advance through
    # still had it, so scrum was the only mode that kept the verdict. The
    # redirect is also the case that needs it MOST: a story that reached
    # `done` can be re-evaluated from its receipts at any time, while a
    # blocked one carries the gate's reasoning and nothing else.
    #
    # `resolve_done_edge` has already computed the verdict on this path, so
    # the re-evaluation below is only ever reached when the caller landed on
    # the edge with no gate applied.
    on_done_edge = policy.dod_on_done and to_state in _DONE_EDGE
    if on_done_edge:
        if dod is None:
            mode = build_mode(project_dir)
            dod = sp.evaluate_story_dod(
                project_dir, story_id, _dod_intensity(project_dir, state, mode)
            )
        story["dod"] = dod

    sp._write_state(project_dir, state)

    # `_emit_gate_event` still no-ops for a redirect, matching the scrum
    # wrapper: #403 ships the verdict on a block, it does not emit a gate
    # decision for a transition that did not reach the gate's own edge.
    if dod is not None and on_done_edge:
        try:
            sp.ship_evaluated_dod(story_id, dod, project_dir=project_dir)
        except Exception:  # noqa: BLE001 - telemetry must never fail a transition
            pass
        _emit_gate_event(project_dir, story_id, target, dod)

    decision.story = story
    decision.dod = dod
    decision.effective_to_state = target
    try:
        decision.next_action = next_action(project_dir)
    except Exception:  # noqa: BLE001
        pass
    return decision


def _emit_gate_event(
    project_dir: str, story_id: str, to_state: str, dod: dict,
) -> None:
    """Mirror the scrum wrapper's evidence_dod gate emission (best effort)."""
    if to_state != "done":
        return
    try:
        from gate_emitter import (  # type: ignore
            emit_evidence_dod_accepted,
            emit_evidence_dod_rejected,
        )
    except ImportError:
        return
    try:
        if dod.get("passed"):
            emit_evidence_dod_accepted(
                story_id, "orchestrator", project_dir=project_dir
            )
        else:
            failed = [
                k
                for k, v in (dod.get("checks") or {}).items()
                if v.get("required") and v.get("passed") is not True
            ]
            emit_evidence_dod_rejected(
                story_id,
                "orchestrator",
                "DoD failed: %s" % (", ".join(failed) or "unknown"),
                project_dir=project_dir,
            )
    except Exception:  # noqa: BLE001
        pass


# ── dispatch ─────────────────────────────────────────────────────────────────


def evaluate_dispatch(
    project_dir: str,
    story_id: str,
    role: Optional[str] = None,
    policy: Optional[HostPolicy] = None,
) -> Decision:
    """Authorize a dispatch: only the story and role next_action selected.

    The `queued -> in_progress` edge legitimately has no receipt yet, which is
    exactly why starting work lives here and cannot be expressed through
    `execute_advance`.
    """
    policy = policy or HostPolicy(host="unknown")
    project_dir = str(project_dir)
    sp = _sp()
    checks: List[dict] = []
    warnings: List[str] = []

    if policy.policy_gate is not None:
        refusal = policy.policy_gate(project_dir)
        if refusal:
            return Decision(
                allowed=False,
                code=POLICY_REFUSED,
                reason=str(refusal.get("error") or "refused by host policy"),
                checks=checks,
                extra=dict(refusal),
            )
    if policy.readiness_gate is not None:
        refusal = policy.readiness_gate(project_dir)
        if refusal:
            return Decision(
                allowed=False,
                code=NOT_READY,
                reason=str(refusal.get("error") or "host is not ready"),
                checks=checks,
                extra=dict(refusal),
            )
    if not story_id:
        return Decision(
            allowed=False, code=BAD_REQUEST, reason="story_id is required", checks=checks
        )

    try:
        selected = next_action(project_dir)
    except Exception as exc:  # noqa: BLE001 - unreadable state refuses, never raises
        return Decision(
            allowed=False,
            code=BAD_REQUEST,
            reason="cannot determine next_action: %s" % exc,
            checks=checks,
        )
    # #304: answer the dependency question BEFORE the next_action binding.
    # Both refuse, so the gate holds either way, but the binding check reports
    # the generic `next_action_mismatch` -- which cannot distinguish "held by an
    # unmet dependency" from "you asked for the wrong story", and is what
    # next_action would return anyway once it skips the blocked candidate. That
    # made the refusal unactionable for the operator and a conformance
    # assertion on it non-discriminating. Scoped to the SE start/resume edge:
    # qe and cr run on work that is already built, and blocking there would
    # strand it. A story that is not on the board falls through to the existing
    # codes untouched.
    _dep_state = sp._read_state(project_dir)
    _dep_story = sp.get_story(_dep_state, story_id)
    if _dep_story and str(_dep_story.get("state") or "") in ("queued", "blocked"):
        requested = role_abbrev(str(role)) if role else "se"
        if requested == "se":
            dep = sp.story_dep_status(
                _dep_state,
                _dep_story,
                dep_context=sp.dep_context(project_dir, _dep_state),
            )
            if not dep["met"]:
                return Decision(
                    allowed=False,
                    code=DEPS_UNMET,
                    reason="dependencies unmet for %s: %s"
                    % (story_id, sp.format_unmet_deps(dep["unmet"])),
                    checks=checks,
                    next_action=selected,
                    extra={"unmet_dependencies": dep["unmet"]},
                )
            _record(checks, "deps_met", True)

    # PATH SCOPE, BEFORE THE BINDING, for the reason #304 gives just above:
    # both refuse, and `next_action_mismatch` cannot distinguish "this unit
    # collides with running work" from "you asked for the wrong unit". The
    # generic code made the refusal unactionable and the conformance assertion
    # non-discriminating -- `spq.intersecting_concurrent_scopes_are_refused`
    # asserts the REASON precisely because asserting `not advanced` passed on
    # the implementation that had no scope check at all.
    collision = _scope_collision(project_dir, story_id)
    if collision is not None:
        holder, paths = collision
        return Decision(
            allowed=False,
            code=SCOPE_COLLISION,
            reason=(
                "%s declares a path scope intersecting %s, which holds a live "
                "dispatch: %s. Intersecting work runs sequentially in one "
                "Cycle, never concurrently (`C-07`) -- finish or release %s "
                "first" % (story_id, holder, ", ".join(paths), holder)
            ),
            checks=checks,
            next_action=selected,
            extra={"colliding_unit": holder, "intersecting_scopes": paths},
        )
    _record(checks, "path_scopes_disjoint", True)

    contradiction = _region_contradiction(project_dir)
    if contradiction is not None:
        return Decision(
            allowed=False,
            code=REGION_CONTRADICTED,
            reason=(
                "the source region this Cycle was admitted on is no longer "
                "exclusively its own: %s. Two concurrent Cycles never declare "
                "overlapping regions (`SC-MTH-012`) -- that is what lets them "
                "run without inspecting each other's admissions. Re-reserve "
                "with `synaptory cycles regions reserve`, or close one of the "
                "two Cycles." % contradiction["detail"]
            ),
            checks=checks,
            next_action=selected,
            extra={"region_contradiction": contradiction},
        )
    _record(checks, "region_reservation_live", True)

    action = str(selected.get("action") or "")
    if selected.get("story_id") != story_id:
        # UNDER SPQ, `next_action` NAMES ONE UNIT AND AUTHORIZES NONE. That is
        # `C-07` / `SC-MTH-010`: concurrency is a consequence of declared path
        # scope, not an enablement. `next_action` is deterministic and returns
        # a single recommendation, so binding dispatch to it serialized the
        # whole Cycle -- disjoint work was refused with exactly the code
        # colliding work got, which is what made the collision risk look
        # absent rather than unguarded (`spq.disjoint_scopes_need_no_
        # enablement`).
        #
        # WHAT AUTHORIZES A SECOND DISPATCH is not this recommendation but the
        # three checks that already ran above it: the unit is admitted to the
        # sealed declaration, its dependencies are met, and its declared scope
        # is disjoint from every live dispatch. A unit that clears all three is
        # legal work by the method's own rule, and refusing it here would be
        # the parallelism switch again under another name.
        #
        # SCRUM AND KANBAN ARE UNTOUCHED. Their `next_action` binding is the
        # advisory serialization #277 shipped and they have no declared path
        # scope to substitute for it, so the alternative there is not "check
        # something stronger" but "check nothing".
        alternative = _spq_independently_dispatchable(
            project_dir, story_id, role=role
        )
        if alternative is None:
            return Decision(
                allowed=False,
                code=NEXT_ACTION_MISMATCH,
                reason="next_action did not select story %s" % story_id,
                checks=checks,
                next_action=selected,
            )
        _record(
            checks,
            "concurrent_dispatch_authorized",
            True,
            "next_action selected %s; %s is admitted, unblocked and disjoint"
            % (selected.get("story_id") or "nothing", story_id),
        )
        selected = alternative
    if not (action.startswith("dispatch_") or action == "recover_blocked"):
        return Decision(
            allowed=False,
            code=DISPATCH_NOT_ELIGIBLE,
            reason="next_action is %s, not a dispatch" % (action or "unknown"),
            checks=checks,
            next_action=selected,
        )
    raw_role = selected.get("role") or (selected.get("recovery") or {}).get("role") or ""
    selected_abbrev = role_abbrev(str(raw_role))
    if role and selected_abbrev and role_abbrev(str(role)) != selected_abbrev:
        return Decision(
            allowed=False,
            code=NEXT_ACTION_MISMATCH,
            reason="next_action selected role %s, not %s"
            % (selected_abbrev, role_abbrev(str(role)) or role),
            checks=checks,
            next_action=selected,
        )
    _record(checks, "next_action_match", True)

    state = sp._read_state(project_dir)
    story = sp.get_story(state, story_id)
    if not story:
        return Decision(
            allowed=False,
            code=STORY_NOT_FOUND,
            reason="story not found: %s" % story_id,
            checks=checks,
        )

    # A fresh successful receipt means the agent finished: advance, do not
    # re-dispatch. A verification-loop action is different: the receipt is the
    # reason the deterministic planner selected a retry, so execute_dispatch
    # archives it atomically before issuing a new dispatch contract.
    abbrev = selected_abbrev
    if abbrev and str(story.get("state") or "") != "queued":
        try:
            path = canonical_receipt_path(project_dir, story_id, abbrev)
            if path.is_file():
                entered = stage_entered_at(story)
                raw = json.loads(path.read_bytes().decode("utf-8"))
                completed = parse_timestamp(raw.get("completed_at"))
                if (
                    entered is not None
                    and completed is not None
                    and _sp().receipt_timestamp_is_fresh(completed, entered)
                    and not selected.get("recovery")
                ):
                    return Decision(
                        allowed=False,
                        code=RECEIPT_ALREADY_PRESENT,
                        reason="a fresh receipt already exists; call advance instead",
                        checks=checks,
                        next_action=selected,
                        receipt_path=str(path),
                    )
        except (_Refusal, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            pass

    # #402. The dispatch decision names the pair it is authorizing, resolved
    # through the same alias table the advance gate will hold the receipt to.
    # One resolution, two ends of the same dispatch: an orchestrator can state
    # the pair in the prompt and the agent can copy it back verbatim.
    authorized_abbrev = selected_abbrev or role_abbrev(str(role or ""))
    return Decision(
        allowed=True,
        code=OK,
        checks=checks,
        warnings=warnings,
        story=story,
        next_action=selected,
        role=role_full_name(str(raw_role)) or role_full_name(str(role or "")),
        profile=dispatch_profile(authorized_abbrev) if authorized_abbrev else None,
        extra={"role_abbrev": authorized_abbrev},
    )


def role_abbrev(role: str) -> Optional[str]:
    """Normalize a role to its abbreviation. Accepts either form."""
    if not role:
        return None
    if role in ROLE_NAMES:
        return role
    for abbrev, name in ROLE_NAMES.items():
        if name == role:
            return abbrev
    return None


def role_full_name(role: str) -> Optional[str]:
    """Normalize a role to its full name. Accepts either form."""
    abbrev = role_abbrev(role)
    return ROLE_NAMES.get(abbrev) if abbrev else None


def execute_dispatch(
    project_dir: str,
    story_id: str,
    role: Optional[str] = None,
    policy: Optional[HostPolicy] = None,
) -> Decision:
    """Authorize and start a dispatch, returning the receipt contract to honour.

    Eligibility is evaluated inside the state lock. Evaluating first and locking
    second let two processes both pass against a queued story and both launch
    an agent; the second must re-evaluate against the state the first wrote.
    """
    policy = policy or HostPolicy(host="unknown")
    project_dir = str(project_dir)

    try:
        with _state_transaction(project_dir):
            return _execute_dispatch_locked(project_dir, story_id, role, policy)
    except _StateLockTimeout as exc:
        return Decision(allowed=False, code=STATE_LOCK_TIMEOUT, reason=str(exc))


def _execute_dispatch_locked(
    project_dir: str,
    story_id: str,
    role: Optional[str],
    policy: HostPolicy,
) -> Decision:
    sp = _sp()
    decision = evaluate_dispatch(project_dir, story_id, role=role, policy=policy)
    if not decision.allowed:
        return decision

    selected = decision.next_action or {}
    decision.extra["dispatch_action"] = selected
    action = str(selected.get("action") or "")
    state = sp._read_state(project_dir)
    current = str((sp.get_story(state, story_id) or {}).get("state") or "")
    # READ BEFORE THE MINT. The concurrency guard below asks whether a previous
    # attempt is still somebody's, and the mint further down replaces the
    # binding it would have to read, so by then every incumbent looks live
    # (#396). Captured here for the same reason `_classify_new_attempt` reads
    # its predecessor before anything is overwritten.
    incumbent_live = _incumbent_is_live(sp.get_story(state, story_id), "se")

    abbrev = str(decision.extra.get("role_abbrev") or "")
    contract: Dict[str, Any] = {"role": decision.role}
    if abbrev:
        receipts_dir = intended_receipts_dir(project_dir)
        # Creating it now means _resolve_receipts_dir (read side) resolves to the
        # same directory, so dispatch and the gate cannot disagree. Canonicalise
        # BEFORE writing state: a receipts/ symlink must refuse, not hand a
        # path outside the project to the agent.
        try:
            receipts_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        try:
            contract["receipt_path"] = str(
                canonical_receipt_path(project_dir, story_id, abbrev)
            )
        except _Refusal as refusal:
            return Decision(
                allowed=False,
                code=refusal.code,
                reason=refusal.reason,
                warnings=decision.warnings,
                checks=decision.checks,
                next_action=selected,
                story=sp.get_story(state, story_id),
                extra=decision.extra,
            )
    if policy.expected_backend:
        contract["backend"] = policy.expected_backend

    recovery = selected.get("recovery")
    # #690. The predecessor the archival block below has to write its evidence
    # onto, captured here because the block that classifies the lineage is
    # scoped to `if abbrev:` and the archival block is not.
    superseded_attempt_id = ""
    ledger_story: Optional[Dict[str, Any]] = None
    # Recording is unconditional; ENFORCEMENT stays policy-gated at advance
    # (#340, #344). These were one flag, which made the binding exist only on
    # the hosts that already refused a mismatch, so Claude recorded nothing and
    # anything built on the binding there was dead code before it was written.
    # Splitting them fixed that, and the refusal branch still reads
    # `policy.require_dispatch_binding`.
    #
    # THAT SPLIT IS NOW MOOT FOR THE THREE SHIPPED HOSTS, and this comment said
    # otherwise for long enough to mislead a gap reason (#495). It read
    # "`policy_for_claude` does not set `require_dispatch_binding`", which was
    # true when written; all three host policies now set it (`policy_for_claude`,
    # `policy_for_cursor`, `policy_for_codex` below). Keep the split anyway: the
    # unconditional recording is what a host with the flag OFF would rely on,
    # and a reader deciding whether a verifier can be bound to a dispatch needs
    # the live answer, which is yes on every host.
    if abbrev:
        dispatch_id = secrets.token_hex(16)
        contract["dispatch_id"] = dispatch_id
        active_story = sp.get_story(state, story_id)
        if active_story is None:
            return Decision(
                allowed=False,
                code=STORY_NOT_FOUND,
                reason="story disappeared before dispatch binding",
            )
        dispatches = active_story.setdefault("mcp_active_dispatches", {})
        if not isinstance(dispatches, dict):
            return Decision(
                allowed=False,
                code=LEDGER_INVALID,
                reason="active-dispatch ledger is invalid",
            )
        binding = {
            "dispatch_id": dispatch_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        # Epic #339: extend the SAME binding with attempt identity rather than
        # opening a second one. `attempt_id` is the durable, fenced form of
        # `dispatch_id`; the fencing token is what a stale runner presents and
        # the coordinator refuses once the lease has moved on. Both are minted
        # locally so a project with no reachable control plane still gets
        # attempt-scoped evidence: registration with the CP is additive and is
        # the runner lane's work (#350/#352).
        # #492. WHAT THIS ATTEMPT IS, decided before anything is overwritten.
        # The predecessor exists only in `dispatches[abbrev]` until the
        # assignment at the bottom of this block replaces it, so the
        # classification and the archival both have to happen here or not at
        # all. Read before the mint so a refusing mint changes nothing.
        predecessor = _predecessor(active_story, abbrev)
        lineage = _classify_new_attempt(active_story, abbrev)
        attempt_binding = _mint_attempt_binding(project_dir, state, story_id, abbrev)
        if attempt_binding is not None:
            if attempt_binding.get("refusal"):
                return Decision(
                    allowed=False,
                    code=attempt_binding["refusal"]["code"],
                    reason=attempt_binding["refusal"]["reason"],
                )
            binding.update(attempt_binding["binding"])
            minted = attempt_binding["binding"]["attempt_id"]
            # #492. The superseded attempt's record, written BEFORE the
            # successor's so the list stays in mint order without sorting.
            if lineage["prior_attempt_id"]:
                _archive_predecessor(active_story, abbrev, predecessor or {}, minted)
                superseded_attempt_id = lineage["prior_attempt_id"]
                ledger_story = active_story
            # #493. The authorization record, written where the authorization
            # happens. `dispatches[abbrev]` below is the LIVE binding and is
            # overwritten by the next dispatch of this stage and popped when
            # the stage advances; this ledger accumulates, so an attestation
            # produced by a superseded generation still has something to bind
            # to on the retry's receipt.
            _record_authorized_attempt(
                active_story,
                abbrev,
                minted,
                classification=lineage["classification"],
                prior_attempt_id=lineage["prior_attempt_id"],
                prior_failure_class=lineage["prior_failure_class"],
            )
            # On the LIVE binding as well as in the history, because ADR-031
            # section 11's rule is stated about the new attempt ("if it
            # returns, it is a new attempt carrying `prior_attempt_id`") and a
            # reader of the active dispatch should not have to join two
            # structures to see what the attempt in front of them is.
            binding["classification"] = lineage["classification"]
            contract["classification"] = lineage["classification"]
            if lineage["prior_attempt_id"]:
                binding["prior_attempt_id"] = lineage["prior_attempt_id"]
                contract["prior_attempt_id"] = lineage["prior_attempt_id"]
            contract["attempt_id"] = attempt_binding["binding"]["attempt_id"]
            contract["fencing_token"] = attempt_binding["binding"]["fencing_token"]
            for key in ("cycle_id", "manifest_hash", "workstream_id", "expires_at"):
                if attempt_binding["binding"].get(key):
                    contract[key] = attempt_binding["binding"][key]
        dispatches[abbrev] = binding

    # Runtime selection runs HERE, before anything is committed (#396).
    #
    # It used to run after `_write_state`, so a denial returned allowed=False
    # while the Work Unit was already `in_progress` with a live dispatch and
    # attempt binding on disk. The advertised refusal CONSUMED the dispatch
    # instead of refusing it, and the retry then hit the already-started guard,
    # which is strictly worse than having allowed the run. Everything below
    # this point mutates the world: the archival block MOVES a file, the
    # transition rewrites the story, and `_write_state` persists both.
    #
    # #447: the stage the attempt will RUN in is resolved here too, because it
    # is the state this dispatch is about to move the story to and after the
    # write there is no way to tell it from the state the story was already in.
    # #464 sent `next_action.action` as the envelope's `stage`, so every
    # envelope read `dispatch_se`: an action, not a stage. The control plane
    # stores this and a worker compares its envelope against it
    # (`checkEnvelopeMatchesAttempt`), so it has to be the vocabulary the
    # fixtures use ("in_progress", "testing").
    contract["stage"] = "in_progress" if current == "queued" else current
    _attach_runtime_selection(
        project_dir, decision, contract, story_id, abbrev, binding=binding
    )
    if not decision.allowed:
        # Nothing has been written or moved yet, so the story is still exactly
        # as it was found and the dispatch can be retried once the operator
        # fixes what selection refused.
        return decision

    # Archive only after every state/ledger precondition has passed. From this
    # point the recovery path has no refusal branch before the single state
    # write below, so callers never see an archived receipt paired with a
    # rejected retry dispatch.
    if recovery and abbrev and contract.get("receipt_path"):
        failed_path = Path(str(contract["receipt_path"]))
        if failed_path.is_file():
            archive_dir = failed_path.parent / "archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            receipt_digest = hashlib.sha256(failed_path.read_bytes()).hexdigest()
            digest = receipt_digest[:16]
            # #741. The tag names WHY the predecessor could not stand, and
            # "failed" is wrong for this one: the attempt that produced this
            # receipt is not recorded dead (it may be `completed`), the
            # receipt's own bytes are what the validator refused. Every other
            # verdict keeps the existing `.failed-` tag unchanged -- no
            # existing test asserting that pattern needs to change.
            archive_tag = "invalid" if recovery.get("verdict") == RECEIPT_INVALID else "failed"
            archived = archive_dir / f"{failed_path.stem}.{archive_tag}-{digest}.json"
            suffix = 1
            while archived.exists():
                archived = archive_dir / (
                    f"{failed_path.stem}.{archive_tag}-{digest}-{suffix}.json"
                )
                suffix += 1
            shutil.move(str(failed_path), str(archived))
            failure_reason = str(selected.get("reason") or recovery.get("verdict") or "")
            retry_count = sp.record_retry(
                state, story_id, abbrev, failure_reason, project_dir=project_dir
            )
            # #690. WHERE THE EVIDENCE WENT, recorded on the attempt that
            # produced it. The filename already carried a 16-hex prefix, which
            # is a breadcrumb rather than a record: nothing on the board said
            # which attempt an archived file belonged to, so a superseded
            # receipt could only be re-associated with its attempt by reading
            # the directory and guessing. `setdefault`, like every other
            # lineage field: an attempt's evidence is fixed at the moment it
            # was superseded and a later dispatch may not restate it.
            #
            # This lands in the SAME state write as the successor's
            # authorization a few frames down, which is what makes "preserve
            # the failed attempt before authorizing its successor" true rather
            # than merely ordered -- a crash between the two cannot leave the
            # successor recorded and its predecessor's evidence unrecorded.
            if superseded_attempt_id and ledger_story is not None:
                record = _history_record(
                    ledger_story, abbrev, superseded_attempt_id, create=True
                )
                if record is not None:
                    record.setdefault("receipt_digest", receipt_digest)
                    record.setdefault("archived_receipt", str(archived))
            decision.extra["archived_receipt"] = str(archived)
            decision.extra["archived_receipt_digest"] = receipt_digest
            decision.extra["retry_count"] = retry_count

    if action == "recover_blocked":
        # UNBLOCK ONLY WHAT IS BLOCKED. `recover_blocked` covers two shapes: a
        # story the board persisted as `blocked`, which has to be restored
        # before it can be dispatched, and a GATE REMEDIATION on a story the
        # DoD gate refused while leaving it in `reviewing` (#396). The second
        # is deliberate: `dod_red_behavior: refuse` means the board does not
        # move, so there is nothing to unblock, and calling `unblock_story`
        # anyway raised `Story ... is not blocked (state: reviewing)` on the
        # very next operation a host performs after `next_action`. The
        # dead end moved rather than closing.
        if current == "blocked":
            state = sp.unblock_story(state, story_id, project_dir)
    elif current == "queued":
        state = sp.transition_story(
            state, story_id, "in_progress", None, project_dir
        )
    elif (
        current == "in_progress"
        and action == "dispatch_se"
        and not recovery
        and incumbent_live
    ):
        # Lost the queued claim: the first process already started this story.
        # Resume is an orchestrator decision against an existing in_progress
        # story, not a second begin_dispatch.
        #
        # A LIVE incumbent, specifically. The guard used to refuse every
        # `dispatch_se` against `in_progress`, which also refused the SUCCESSOR
        # of an attempt that had already terminated: cancel the producing
        # attempt through a human steer, ask for the next generation, and the
        # kernel answered `dispatch_already_started` about an attempt nobody
        # was running. That made ADR-031 section 11's steer chain impossible on
        # the producing stage, which is the one stage S1's produce-verify loop
        # is about (#396, #492).
        return Decision(
            allowed=False,
            code=DISPATCH_ALREADY_STARTED,
            reason="story %s is already in_progress; another dispatch claimed it"
            % story_id,
            checks=decision.checks,
            next_action=selected,
            story=sp.get_story(state, story_id),
        )
    sp._write_state(project_dir, state)

    story = sp.get_story(state, story_id)

    decision.story = story
    decision.extra["receipt_contract"] = contract
    try:
        decision.extra["post_dispatch_next_action"] = next_action(project_dir)
    except Exception:  # noqa: BLE001
        pass
    return decision


def _last_attempt_terminated(story: Any, abbrev: str) -> bool:
    """Whether this role's most recent LEDGER record says its attempt ended.

    Read only when there is no live binding, so it answers one question: is the
    absence of a binding the absence of a record, or the record of a finish?

    A pre-#492 bare-string record carries identity and nothing else, so it
    reports False and the caller stays conservative, which is the migration
    behaviour `_incumbent_is_live` documents.
    """
    history = _history_view(story, abbrev)
    if not history:
        return False
    last = history[-1]
    if last.get("advanced_at"):
        return True
    _, terminal = _attempt_state_vocabulary()
    recorded = str(last.get("state") or last.get("attempt_state") or "")
    return recorded in terminal


def _incumbent_is_live(story: Any, abbrev: str) -> bool:
    """Whether this role's bound attempt is still one somebody is running.

    The concurrency guard exists to stop a SECOND process starting work a first
    one already claimed. A predecessor that reached `cancelled`, `failed`,
    `completed` or `expired` is not that: nobody is running it, and its
    successor is exactly what ADR-031 section 11 asks for after a steer.

    NO BINDING AND NO LEDGER IS LIVE, deliberately. A story that reached
    `in_progress` with no recorded attempt cannot show that its incumbent
    terminated, and the guard's whole purpose is to be conservative about a
    claim it cannot see. That is also the pre-#492 shape, so a board written
    before attempt state was recorded behaves exactly as it did.

    NO BINDING BUT A TERMINATED LEDGER IS NOT LIVE. That case is not an absence
    of evidence, it is evidence: an advance pops the binding and stamps
    `advanced_at` on the ledger record precisely BECAUSE that attempt finished
    (`:2405-2412`). Treating it as live made rework unreachable, because the
    lifecycle legally returns a Work Unit to `in_progress` through
    `reject_story --reason needs-fix` and the blocked recovery edges, and every
    one of those arrives with the binding already popped. The unit could then
    never obtain a fresh dispatch, so its second cycle could never hold a live
    attempt and could never earn `attested` credit (#613).

    The distinction is the whole point: "I have no record" stays conservative,
    "I have a record and it says finished" is answered.
    """
    if not isinstance(story, dict):
        return True
    dispatches = story.get("mcp_active_dispatches")
    if not isinstance(dispatches, dict):
        return not _last_attempt_terminated(story, abbrev)
    binding = dispatches.get(abbrev)
    if not isinstance(binding, dict):
        return not _last_attempt_terminated(story, abbrev)
    _, terminal = _attempt_state_vocabulary()
    # `reconcile_dispatch` writes the projected attempt state onto the binding
    # as `state`; `attempt_state` is read too so a caller that spells it the
    # way the reconcile argument is named is not silently ignored.
    recorded = str(binding.get("state") or binding.get("attempt_state") or "")
    return recorded not in terminal


def _attach_runtime_selection(
    project_dir: str,
    decision: "Decision",
    contract: Dict[str, Any],
    story_id: str,
    abbrev: str,
    binding: Optional[Dict[str, Any]] = None,
) -> None:
    """Run runtime selection and, on a selection, build the dispatch envelope.

    THIS IS THE PRODUCTION CALLER (#396 finding 1). `select_runtime` and
    `build_envelope` existed as component shapes with no caller on the dispatch
    path, so Auto/Prefer/Pin could not affect a real dispatch, a host could not
    route to another runtime family, and the worker could never receive a
    kernel-created envelope. Every downstream story was blocked on this one
    missing call.

    Three outcomes, and the difference between them is the whole design:

      inert     the project does not govern runtime selection (not SPQ, or
                `runtimes:` absent or disabled), or NOBODY HAS PROBED this
                machine. The existing dispatch path continues untouched, with
                the reason recorded so an operator can see which it was.
      denied    the policy admits no profile that can serve this role. The
                dispatch is refused, because quietly running it on whatever is
                available is the silent degradation EP-12 forbids.
      selected  the envelope is built and returned on the contract.

    An absent probe snapshot is inert, never optimistic. Assuming a runtime is
    present because nobody looked is exactly the failure the snapshot exists to
    prevent, and denying instead would break every opted-in project the moment
    this shipped.
    """
    account: Dict[str, Any] = {}
    try:
        import runtime_selector as rsel  # type: ignore

        policy = rsel.load_policy(project_dir)
        if not policy.enabled:
            account = {
                "state": "inert",
                "reason": "; ".join(policy.problems) or "runtime policy disabled",
            }
            decision.extra["runtime_selection"] = account
            return

        availability = rsel.load_availability(project_dir)
        if availability is None:
            account = {
                "state": "inert",
                "reason": (
                    "no probe snapshot at %s; run `synaptory runtimes doctor` "
                    "so selection has a basis"
                    % "/".join(rsel.AVAILABILITY_RELPATH)
                ),
            }
            decision.extra["runtime_selection"] = account
            return

        # The ABBREV, not the full agent name. `DISPATCH_CAPABILITY_PROFILE` is
        # keyed by abbrev, which is the same choice the envelope's `role` field
        # makes, so passing `decision.role` here resolved to no capability
        # profile and denied every dispatch.
        request = rsel.SelectionRequest(
            role=abbrev or str(decision.role or ""),
            capability_profile=str(
                (decision.extra.get("profile") or {}).get("capability_profile") or ""
            ),
        )
        result = rsel.select_runtime(
            build_mode=build_mode(project_dir),
            policy=policy,
            request=request,
            profiles=rsel.load_profiles(project_dir),
            availability=availability,
        )
    except Exception as exc:  # noqa: BLE001
        # FAIL CLOSED once the project is governed (#396). Recording the error
        # and letting the dispatch proceed meant a corrupt snapshot, an
        # unreadable registry or a bug in the selector all became permission to
        # run outside the authority the project asked for, which is the silent
        # degradation EP-12 forbids. `policy.enabled` is what distinguishes
        # this from the unconfigured migration case, and that case has already
        # returned above.
        decision.allowed = False
        decision.code = PROFILE_MISMATCH
        decision.reason = (
            "runtime selection could not establish an authority for this "
            "dispatch, so it is refused rather than run outside one: %s" % exc
        )
        decision.extra["runtime_selection"] = {
            "state": "error",
            "reason": decision.reason,
        }
        return

    if result.inert:
        decision.extra["runtime_selection"] = {
            "state": "inert",
            "reason": getattr(result, "inert_reason", "") or "selection is inert",
        }
        return
    if result.denied:
        denial = result.denial
        decision.allowed = False
        decision.code = PROFILE_MISMATCH
        # `Rejection` carries `code` and `detail`, not `reason`. #464 read
        # `getattr(denial, "reason", "")`, which always missed, so every denial
        # discarded the selector's actual explanation for a generic sentence --
        # and the explanation is the selector's product surface (8.1), the one
        # thing that tells an operator WHICH requirement excluded WHICH profile.
        decision.reason = str(denial) if denial is not None else (
            "no admitted runtime profile can serve %s" % (decision.role or story_id)
        )
        decision.extra["runtime_selection"] = {
            "state": "denied",
            "reason": decision.reason,
            "explanation": result.explain(),
        }
        return

    account = {
        "state": "selected",
        "profile_id": result.selected,
        "runtime_family": result.runtime_family,
        "placement": result.placement,
        "mode": result.mode,
    }
    decision.extra["runtime_selection"] = account
    contract["runtime"] = account
    # RECORDED ON THE BINDING, not just returned (#396). A selection a host can
    # display but nothing enforces cannot change which executor actually runs:
    # Auto/Prefer/Pin were visible and inert. Persisting the chosen family is
    # what lets `evaluate_advance` refuse a receipt produced by a different
    # one, which is the difference between a recommendation and a routing
    # decision.
    if isinstance(binding, dict):
        _record_selected_family(binding, result)

    envelope, envelope_error = _build_dispatch_envelope(
        project_dir, contract, result, story_id, abbrev, decision
    )
    if envelope is None:
        # A selection with no envelope is an authority nobody can carry. It
        # used to leave the dispatch allowed and simply omit the envelope,
        # which is the same fail-open shape one level down.
        decision.allowed = False
        decision.code = PROFILE_MISMATCH
        decision.reason = (
            "profile %s was selected but its dispatch envelope could not be "
            "built, so there is no authority to run under: %s"
            % (result.selected, envelope_error)
        )
        account["state"] = "envelope_failed"
        account["reason"] = decision.reason
        return
    contract["dispatch_envelope"] = envelope
    decision.extra["dispatch_envelope"] = envelope


#: Receipt `backend` values, as hosts write them, mapped to the runtime family
#: the selector speaks. The two vocabularies exist because a receipt names the
#: host that produced it while a profile names the runtime that ran, and the
#: enforcement below has to compare like with like.
_BACKEND_RUNTIME_FAMILY = {
    "claude": "claude-code",
    "claude-code": "claude-code",
    "codex": "codex",
    "cursor": "cursor-agent",
    "cursor-agent": "cursor-agent",
}


def _note_dod_gate_refusal(project_dir: str, story_id: str, reason: str) -> None:
    """Record why the DoD gate refused, without moving the story (#396).

    A `refuse` policy leaves the unit exactly where it was, which is the point:
    red evidence does not advance a board. But the refusal was invisible to
    `next_action`, which reads the board, so the board kept advertising the
    promotion the gate had just declined and the orchestrator had no other
    action to take.

    This writes the gate's reason and nothing else. `state` is not touched, so
    `refuse` still refuses, and `redirect_blocked` hosts are unaffected because
    they never reach here. Best effort: a note that cannot be written must not
    turn a typed gate refusal into a write error, since the refusal is the
    answer the caller needs either way.
    """
    if not reason:
        return
    try:
        sp = _sp()
        with _state_transaction(project_dir):
            state = sp._read_state(project_dir)
            story = sp.get_story(state, story_id)
            if story is None:
                return
            story["dod_gate_refusal"] = {
                "reason": reason if reason.startswith("DoD gate:")
                else "DoD gate: %s" % reason,
                "at": datetime.now(timezone.utc).isoformat(),
                # WHAT THIS REFUSAL WAS ABOUT. A note that outlived its
                # evidence would leave an engineer who produced the missing
                # result stuck behind a stale routing decision with no way to
                # clear it, which is a worse failure than the loop.
                "evidence": sp.receipt_evidence_digest(
                    sp._resolve_receipts_dir(project_dir), story_id
                ),
            }
            sp._write_state(project_dir, state)
    except Exception:  # noqa: BLE001 - see the docstring
        return


def _receipt_runtime_family(receipt: Dict[str, Any]) -> str:
    """The runtime family a receipt says produced it, or "" when it does not say.

    Prefers an explicit `runtime_family` (which a bridge-produced receipt
    carries) and falls back to projecting the legacy `backend`. Unknown values
    return "" rather than a guess: refusing a dispatch because a vocabulary
    grew a value this table has not learned would be worse than not enforcing.
    """
    explicit = str(receipt.get("runtime_family") or "").strip().lower()
    if explicit:
        return explicit
    backend = str(receipt.get("backend") or "").strip().lower()
    return _BACKEND_RUNTIME_FAMILY.get(backend, "")


def dispatch_runtime_authority(story: Any, abbrev: str) -> Dict[str, str]:
    """The runtime authority this stage's LIVE dispatch binding records.

    `{}` means this stage holds no binding, or holds one that records no
    runtime selection. A NON-EMPTY result means `_attach_runtime_selection`
    reached its `selected` branch for this dispatch, and that branch is the
    only one that goes on to build a dispatch envelope: `inert` and `denied`
    both return before `_record_selected_family` runs, an unbuildable envelope
    sets `allowed=False`, and `_execute_dispatch_locked` returns before
    `_write_state` on every one of those paths. So a persisted
    `runtime_family` here is the dispatcher's own record that it handed this
    stage a validated envelope carrying `stage_profile` and
    `capability_profile`.

    This is why the governed condition is READ rather than re-derived. Asking
    `runtime_selector.load_policy` again at validation time would answer from
    the config, the probe snapshot and the profile registry as they are NOW,
    and any of the three can have moved since the dispatch: a validator that
    decided "governed" differently from the dispatcher would refuse honest
    receipts and admit dishonest ones. The binding is the decision itself, not
    a second evaluation of its inputs.

    Read by `evaluate_advance` (which executor was authorized) and by
    `receipt_validator` (whether the overlay pair is required, #473), so both
    answer from one field written in one place.
    """
    dispatches = story.get("mcp_active_dispatches") if isinstance(story, dict) else None
    if not isinstance(dispatches, dict):
        return {}
    entry = dispatches.get(abbrev)
    if not isinstance(entry, dict):
        return {}
    family = str(entry.get("runtime_family") or "").strip()
    if not family:
        return {}
    authority = {"runtime_family": family}
    profile = str(entry.get("adapter_profile_id") or "").strip()
    if profile:
        authority["adapter_profile_id"] = profile
    return authority


def governed_dispatch_authority(
    project_dir: str, story_id: str, abbrev: str
) -> Dict[str, str]:
    """`dispatch_runtime_authority`, resolved from the project's state file.

    `{}` when the state cannot be read, the story is not on the board, or the
    stage holds no governed binding. Unreadable state yields "not governed"
    rather than an error because there is nothing to compare against, and an
    agent gains nothing by removing it: every gate that consumes a receipt
    reads the same board, so a missing story refuses the advance outright.
    """
    try:
        sp = _sp()
        story = sp.get_story(sp._read_state(str(project_dir)), str(story_id))
    except Exception:  # noqa: BLE001 - a validator must not crash on state
        return {}
    return dispatch_runtime_authority(story, abbrev)


def _record_selected_family(binding: Dict[str, Any], result: Any) -> None:
    """Stamp the selected runtime family onto the active dispatch binding.

    Mutates the binding the CALLER is holding, not a fresh copy read from disk.
    The first version did its own read/write inside selection, which happens
    before the caller persists the binding, so it found no binding on disk and
    its write was then overwritten by the caller's. The family never landed and
    the enforcement below was silently inert.
    """
    family = str(getattr(result, "runtime_family", "") or "")
    if not family:
        return
    binding["runtime_family"] = family
    profile = str(getattr(result, "selected", "") or "")
    if profile:
        binding["adapter_profile_id"] = profile


def _build_dispatch_envelope(
    project_dir: str,
    contract: Dict[str, Any],
    result: Any,
    story_id: str,
    abbrev: str,
    decision: "Decision",
) -> Tuple[Optional[Dict[str, Any]], str]:
    """The kernel-minted dispatch envelope for this attempt, or the reason why not.

    Returns the reason rather than swallowing it: a caller that only sees None
    cannot tell an unbuildable envelope from an absent one, and the difference
    decides whether a governed dispatch runs. Every reason names its field, and
    the last of them is the frozen contract's own validator (#447).

    Unsigned here on purpose. The control plane signs at attempt registration
    (proposal 4.4.1, ADR-031 §12), because a kernel-held key would sit on the
    same machine as anyone who would tamper with the envelope. What the kernel
    owns is the CONTENT: it is the only place that knows the dispatch binding,
    the SPQ triple and the canonical receipt path.

    Returns `(envelope, problems)`. #464 returned the envelope or None and
    swallowed every reason, so a malformed ceiling reached the control plane and
    an unresolvable field looked the same as a project that never opted in.
    Since #447 registration ships this object, so it is validated against the
    frozen contract here and a refusal names its fields.
    """
    try:
        import runtime_contracts as rc  # type: ignore

        attempt_id = str(contract.get("attempt_id") or "")
        dispatch_id = str(contract.get("dispatch_id") or "")
        if not attempt_id or not dispatch_id:
            return None, "the dispatch carries no attempt or dispatch identity"
        project_id = _project_slug(project_dir)
        if not project_id:
            return None, (
                "project_id is unresolved: set `project_id:` in .synaptory.yaml "
                "or SYNAPTORY_PROJECT_ID, or the control plane cannot be told "
                "which project this attempt belongs to"
            )
        capability_profile = str(
            (decision.extra.get("profile") or {}).get("capability_profile")
            or rc.DISPATCH_CAPABILITY_PROFILE.get(abbrev, "")
        )
        ceiling = _capability_ceiling(result, capability_profile)
        source_revision = _source_revision(project_dir)
        # Phase D (#632). A managed envelope must be independently
        # materializable and must name its scoped credentials as cred://
        # references. The kernel used to refuse this placement outright because
        # nothing resolved a project's connector configuration; it now resolves
        # it from the project's mirror of the control plane's materialization
        # block. Still nothing is invented: an absent or unusable block refuses
        # the dispatch and says which field to set, rather than putting a runner
        # on a repository and a credential the operator never declared.
        managed: Dict[str, Any] = {}
        if str(result.placement or "") == "managed-laptop":
            import runtime_materialization as rmat  # type: ignore

            try:
                managed = rmat.managed_dispatch_context(
                    project_dir,
                    project_id=project_id,
                    source_revision=source_revision,
                    source_ref=_source_ref(project_dir),
                    adapter_profile_id=str(result.selected or ""),
                    capability_ceiling=ceiling,
                )
            except rmat.UnresolvedMaterialization as exc:
                # Returned as the reason verbatim: it already names the field
                # and the fix, and wrapping it in "envelope could not be
                # assembled" would bury the one sentence an operator acts on.
                return None, str(exc)
        envelope = rc.build_envelope(
            attempt_id=attempt_id,
            dispatch_id=dispatch_id,
            project_id=project_id,
            cycle_id=str(contract.get("cycle_id") or ""),
            manifest_hash=str(contract.get("manifest_hash") or ""),
            workstream_id=str(contract.get("workstream_id") or ""),
            story_id=story_id,
            # The story's own pipeline sub-state, which is what
            # `envelope-local-qe.json` carries ("testing") and what the control
            # plane hands a worker to compare the envelope against. #464 sent
            # `next_action.action` here, so every envelope's `stage` read
            # `dispatch_se`: an action, not a stage. SP-WRK-022 makes a stage
            # name open vocabulary resolved against the pinned workflow, so the
            # sub-state is V1's honest value. (`envelope-managed-se.json` says
            # "implementation" for the same concept; that fixture disagreement
            # is flagged in the PR rather than settled by inventing a third
            # value.)
            stage=str(contract.get("stage") or ""),
            role=abbrev,
            adapter_profile_id=str(result.selected or ""),
            runtime_family=str(result.runtime_family or ""),
            placement=str(result.placement or ""),
            source_revision=source_revision,
            # PROJECT-RELATIVE, which is what both fixtures carry. #464 sent
            # the absolute path, and since #465 the control plane delivers a
            # signed envelope to a worker that may be on another machine, where
            # this machine's absolute path names nothing. The control-plane
            # column is 512 chars, so an absolute path is also the one that
            # truncates.
            receipt_path=_envelope_receipt_path(
                project_dir, str(contract.get("receipt_path") or "")
            ),
            # The deadline the binding already minted, not a second one. #464
            # minted a fresh hour here, so the envelope's expiry, the binding
            # and the control-plane row could each answer differently; the
            # attempt has one deadline and `_mint_attempt_binding` owns it.
            expires_at=str(contract.get("expires_at") or ""),
            fencing_token=str(contract.get("fencing_token") or ""),
            capability_ceiling=ceiling,
            budget={"wall_clock_seconds": ATTEMPT_TTL_SECONDS},
            required_evidence=list(
                _EVIDENCE_BY_CAPABILITY_PROFILE.get(capability_profile, ())
            ),
            # Empty for a local placement, which is what `build_envelope`
            # already defaults to: a local attempt runs inside the checkout it
            # was dispatched from and needs none of these blocks.
            **managed
        )
    except Exception as exc:  # noqa: BLE001
        return None, "envelope could not be assembled: %s" % exc
    # VALIDATED before it leaves this function (#447). #464 never called the
    # validator, so an envelope with four required fields empty was attached to
    # the contract and, since #465, shipped to the control plane to be signed.
    # A ceiling nobody can carry is exactly what the deny above is for.
    invalid = rc.validate_envelope(envelope)
    if invalid:
        return None, "; ".join(invalid)
    return envelope, ""


def _envelope_receipt_path(project_dir: str, receipt_path: str) -> str:
    """Project-relative when it is inside the project, absolute otherwise.

    Absolute is kept only for a receipts directory that genuinely resolves
    outside the project, where a `../..` walk would be less readable and no
    more portable.
    """
    if not receipt_path:
        return ""
    try:
        return str(
            Path(receipt_path).resolve().relative_to(Path(project_dir).resolve())
        )
    except (ValueError, OSError):
        return str(receipt_path)


def _capability_ceiling(result: Any, capability_profile: str) -> List[str]:
    """What this dispatch may do, bounded by what the profile actually provides.

    #464 read `result.capabilities`, which `SelectionResult` has never had
    (`required_capabilities` is the filter, not a grant), so `getattr` always
    fell through to `()` and EVERY kernel-minted envelope carried an empty
    ceiling. Fail-closed rather than a wildcard -- the local adapter's
    `workspace.write` and `process.test` gates read the ceiling with `inList`,
    so empty denies -- but it meant a producing attempt was authorized to write
    nothing, which is not a runnable dispatch.

    The ceiling is an INTERSECTION, never a lookup: what the capability profile
    needs, kept only where the selected profile declares it. Nothing on the
    dispatch path may widen authority (governed-runtime-pilot.md 8.2,
    SP-AUT-004), and an empty intersection is the safe direction to fail in.
    """
    provided = {
        str(c)
        for c in (getattr(result, "selected_capabilities", ()) or ())
    }
    wanted = _CEILING_BY_CAPABILITY_PROFILE.get(capability_profile, ())
    if not provided:
        # The selector reported no capability list for the chosen profile, so
        # there is nothing to bound against. Grant nothing rather than
        # everything the profile might have.
        return []
    return [c for c in wanted if c in provided]


def _project_slug(project_dir: str) -> str:
    """The project id the control plane knows this checkout by.

    The CLI's own two sources, in the CLI's order (`synaptory projects
    current`): `SYNAPTORY_PROJECT_ID`, then `project_id:` in `.synaptory.yaml`.

    There is deliberately NO directory-name fallback. #464 returned
    `basename(project_dir)`, which since #447 would register an attempt against
    whatever slug the checkout happens to be named -- the #320 shape exactly: a
    locally invented project id addressed at a real control plane, accepted or
    404'd on nothing but luck. An unresolved project id now yields no envelope
    and a reason.
    """
    from_env = str(os.environ.get("SYNAPTORY_PROJECT_ID") or "").strip()
    if from_env:
        return from_env
    try:
        import re as _re

        text = (Path(project_dir) / ".synaptory.yaml").read_text(encoding="utf-8")
        # `[ \t]*`, not `\s*`. `\s` matches a newline, so #464's pattern read
        # `project_id:` with an empty value and captured the NEXT key's name:
        # a project declaring no id registered its attempts against a project
        # called "spq". An empty value must resolve to nothing.
        match = _re.search(r"^project_id:[ \t]*[\"']?([A-Za-z0-9._-]+)", text, _re.M)
        if match:
            return match.group(1)
    except OSError:
        pass
    return ""


def _source_revision(project_dir: str) -> str:
    """The commit the attempt runs against, or "" outside a git repo."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", str(project_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _source_ref(project_dir: str) -> str:
    """The full ref this checkout is on, or "" when detached or not a repo.

    `materialization.ref` is the bridge's FALLBACK when a server refuses a
    by-revision fetch, so it has to be a ref that contains `source_revision`.
    The project's configured `default_ref` is not necessarily one -- on SPQ the
    revision lives on the workstream branch, not on `main` -- so the branch this
    checkout is actually on is the better answer and the connector block's
    default is the second.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", str(project_dir), "symbolic-ref", "--quiet", "HEAD"],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def reconcile_dispatch(
    project_dir: str,
    story_id: str,
    role: str,
    *,
    attempt_id: str,
    fencing_token: str = "",
    attempt_state: str = "",
    failure_class: str = "",
) -> Decision:
    """Put the CONTROL PLANE's authoritative generation on the board.

    The kernel mints a local fencing token at dispatch because a project with
    no reachable control plane still needs attempt-scoped evidence. The moment
    a runner CLAIMS the attempt, the control plane issues the authoritative
    token, and nothing wrote it back (#396). Two consequences, and the first is
    a live break rather than a missing feature:

      - a correctly claimed receipt carries the CP token, the board still
        carries the local one, and `evaluate_advance` refuses the receipt as
        `fencing_token_stale`. The governed path could not land evidence.
      - a control-plane cancellation never reached the board, so the state
        stayed absent and the cancelled-attempt guard was skipped entirely.

    THIS VERB PROJECTS A FENCE, SO IT MUST NOT REVERSE ONE. The first version
    identified its target by story and role alone and then overwrote the token
    and state with whatever it was handed, which made three reversals possible:
    a delayed callback from a SUPERSEDED attempt could rewrite the replacement's
    binding; a delayed lease generation could roll `000000000002` back to
    `000000000001`; and any callback could move `cancelled` back to `claimed`.
    Each one hands the stale generation back its authority, which is precisely
    what `evaluate_advance` reads the binding to prevent. So:

      - `attempt_id` is REQUIRED and matched under the same transaction that
        writes, because serializing writers does not establish that the
        snapshot is current.
      - a fencing generation may repeat or move FORWARD, never backward. The
        first reconciliation adopts a CP counter over the local random token;
        after that the counter only climbs.
      - a terminal state is FINAL. Nothing moves an attempt out of
        `cancelled`, `completed`, `failed` or `expired` back into a live one.

    #492 ADDS `failure_class`, and it is the field that makes the projection
    legible rather than merely durable. `cancelled-by-human` was already a
    member of `runtime_contracts.FAILURE_CLASSES`, the control plane's own
    default on `AckCancelRequest`, and what the worker daemon passes at
    `cancel/ack` -- and there was no parameter here to carry it, so the board
    recorded THAT an attempt was cancelled and never that a human asked for
    it. A steer and a technical failure were the same event to every consumer,
    and #409 counts "retries per stage" from that board.

    Three rules on it, in order:

      - CLOSED VOCABULARY, same posture as the attempt state: only a member of
        `FAILURE_CLASSES`, and `unclassified` is refused for the reason the
        control plane refuses it at close (SP-INT-017) -- a runner that does
        not know narrows to `runtime-death`, which is a cause; `unclassified`
        is the absence of one, and it may not close an attempt.
      - IMMUTABLE ONCE SET. A second reconciliation may repeat a class and may
        not replace it. Relabelling `cancelled-by-human` as a technical
        failure would erase a steer after the fact, which is the one rewrite
        the classification exists to prevent.
      - A BARE CANCELLATION DEFAULTS TO `cancelled-by-human`, and records that
        it was defaulted. Every control-plane route into `cancelled` either
        sets that class itself (`/cancel` with no live claimant), defaults to
        it (`/cancel/ack`), or requires an explicit class (`/complete` with
        outcome `cancelled`), and only a supervisor can request a
        cancellation at all. So the inference is the control plane's own, not
        a guess -- but `failure_class_source` still says which of the two
        happened, because a reported class and an inferred one are different
        facts.

    It updates only the binding and this attempt's history record, never the
    story's own state: this reconciles who holds the attempt, and what the
    work is worth remains the advance path's decision.
    """
    sp = _sp()
    abbrev = role_abbrev(role) or str(role or "").strip().lower()
    if not abbrev:
        return Decision(
            allowed=False, code=BAD_REQUEST, reason="no role to reconcile"
        )
    if not str(attempt_id or "").strip():
        return Decision(
            allowed=False,
            code=BAD_REQUEST,
            reason=(
                "reconciliation needs the attempt id it is reporting for: "
                "story and role alone cannot tell a live attempt from one it "
                "already replaced"
            ),
        )
    stated_class = str(failure_class or "").strip()
    if not fencing_token and not attempt_state and not stated_class:
        return Decision(
            allowed=False,
            code=BAD_REQUEST,
            reason=(
                "nothing to reconcile: give a fencing token, a state, a "
                "failure class, or any combination"
            ),
        )
    if stated_class:
        problem = _failure_class_problem(stated_class)
        if problem:
            return Decision(allowed=False, code=POLICY_REFUSED, reason=problem)
    try:
        with _state_transaction(project_dir):
            state = sp._read_state(project_dir)
            story = sp.get_story(state, story_id)
            if story is None:
                return Decision(
                    allowed=False,
                    code=STORY_NOT_FOUND,
                    reason="story not found: %s" % story_id,
                )
            dispatches = story.get("mcp_active_dispatches")
            if not isinstance(dispatches, dict):
                return Decision(
                    allowed=False,
                    code=LEDGER_INVALID,
                    reason="active-dispatch ledger is invalid",
                )
            binding = dispatches.get(abbrev)
            if not isinstance(binding, dict):
                return Decision(
                    allowed=False,
                    code=NO_BOUND_ROLE,
                    reason=(
                        "%s holds no active %s dispatch to reconcile"
                        % (story_id, abbrev)
                    ),
                )
            bound_attempt = str(binding.get("attempt_id") or "")
            if not bound_attempt:
                # NO IDENTITY IS NOT A MATCH. Guarding on `bound_attempt and
                # ...` let a legacy or damaged binding fall back to story plus
                # role, which is the exact unverifiable target the required
                # match exists to remove: with no stored identity the
                # transaction cannot establish that this snapshot names the
                # attempt that owns the role (#396).
                return Decision(
                    allowed=False,
                    code=ATTEMPT_BINDING_MISMATCH,
                    reason=(
                        "%s/%s holds a dispatch with no attempt identity, so "
                        "nothing here can confirm that attempt %s is the one "
                        "that owns it. Re-dispatch rather than reconciling a "
                        "binding that cannot be matched"
                        % (story_id, abbrev, attempt_id)
                    ),
                )
            if bound_attempt != str(attempt_id):
                return Decision(
                    allowed=False,
                    code=ATTEMPT_BINDING_MISMATCH,
                    reason=(
                        "this reconciliation reports for attempt %s but %s/%s "
                        "now holds %s: a superseded attempt does not get to "
                        "rewrite the binding of the one that replaced it"
                        % (attempt_id, story_id, abbrev, bound_attempt)
                    ),
                )
            if fencing_token:
                problem = _fencing_regression(
                    str(binding.get("fencing_token") or ""), str(fencing_token)
                )
                if problem:
                    return Decision(
                        allowed=False, code=FENCING_TOKEN_STALE, reason=problem
                    )
            if attempt_state:
                problem = _state_regression(
                    str(binding.get("state") or ""), str(attempt_state)
                )
                if problem:
                    return Decision(
                        allowed=False, code=POLICY_REFUSED, reason=problem
                    )
            stored_class = str(binding.get("failure_class") or "")
            if stated_class:
                problem = _failure_class_regression(stored_class, stated_class)
                if problem:
                    return Decision(
                        allowed=False, code=POLICY_REFUSED, reason=problem
                    )
            if fencing_token:
                binding["fencing_token"] = str(fencing_token)
            if attempt_state:
                binding["state"] = str(attempt_state)
            resolved_class, class_source = _resolve_failure_class(
                stated_class,
                stored_class,
                str(binding.get("failure_class_source") or ""),
                # The state THIS CALL is reporting, not the one already on the
                # binding. The kernel may infer a class from a cancellation it
                # is being told about; inferring one from a cancellation it
                # merely found stored would classify an attempt on the
                # occasion of an unrelated token reconciliation.
                str(attempt_state or ""),
            )
            if resolved_class:
                binding["failure_class"] = resolved_class
                binding["failure_class_source"] = class_source
            # #492. The binding is overwritten whole by the next dispatch of
            # this stage, so a projection that only reached the binding is a
            # projection with an expiry date. Mirror it onto the attempt's own
            # history record, which nothing overwrites.
            record = _history_record(story, abbrev, str(attempt_id), create=True)
            if record is not None:
                for key in ("state", "failure_class", "failure_class_source"):
                    value = str(binding.get(key) or "")
                    if value:
                        record[key] = value
            sp._write_state(project_dir, state)
            return Decision(
                allowed=True,
                code=OK,
                reason="",
                story=sp.get_story(state, story_id),
                extra={"binding": dict(binding)},
            )
    except _StateLockTimeout as exc:
        return Decision(allowed=False, code=STATE_LOCK_TIMEOUT, reason=str(exc))


def _fencing_regression(current: str, proposed: str) -> str:
    """Why this generation may not replace the one on the binding, or "".

    The control plane's tokens are a zero-padded counter; the kernel's initial
    token is random hex. So the FIRST reconciliation adopts a counter over a
    non-counter, and every one after that compares numbers. A proposal that is
    not a counter never replaces one that is: that direction is how a local
    value would be handed back authority the control plane has already moved
    past.
    """
    if current == proposed:
        return ""
    try:
        proposed_n = int(proposed)
    except (TypeError, ValueError):
        if _is_counter(current):
            return (
                "refusing to replace lease generation %s with %r, which is not "
                "a control-plane generation at all" % (current, proposed)
            )
        return ""
    if not _is_counter(current):
        # Adopting the control plane's counter over the locally minted token.
        return ""
    if proposed_n < int(current):
        return (
            "refusing to roll the lease back from generation %s to %s: a "
            "delayed callback from an older generation would hand a runner "
            "the control plane has already fenced out its authority again"
            % (current, proposed)
        )
    return ""


def _is_counter(value: str) -> bool:
    try:
        int(value)
    except (TypeError, ValueError):
        return False
    return True


#: Fallbacks used only when `runtime_contracts` cannot be imported, so a
#: missing sibling module cannot turn a closed vocabulary into an open one.
_FALLBACK_ATTEMPT_STATES = (
    "pending", "claimed", "running", "completed", "failed", "cancelled", "expired",
)
_FALLBACK_TERMINAL_STATES = frozenset(
    {"completed", "failed", "cancelled", "expired"}
)


def _attempt_state_vocabulary() -> Tuple[Tuple[str, ...], frozenset]:
    try:
        import runtime_contracts as rc

        return tuple(rc.ATTEMPT_STATES), frozenset(rc.TERMINAL_ATTEMPT_STATES)
    except Exception:  # noqa: BLE001
        return _FALLBACK_ATTEMPT_STATES, _FALLBACK_TERMINAL_STATES


def _failure_class_problem(proposed: str) -> str:
    """Why this failure class may not be stored at all, or "".

    CLOSED VOCABULARY, for the same reason the attempt state's is closed: a
    class nothing downstream recognises reads as no classification, which is
    indistinguishable from a technical failure, which is exactly the
    conflation #492 exists to remove.

    `unclassified` is refused rather than stored. `runtime_contracts` already
    refuses it on a receipt and the control plane refuses it at close
    (SP-INT-017): a runner that genuinely does not know narrows to
    `runtime-death`, which is a cause, while `unclassified` is the absence of
    one. Storing it here would give a caller a way to end an attempt on the
    board without saying why, which is the hole with a name on it.
    """
    classes = _failure_class_vocabulary()
    if proposed not in classes:
        return (
            "refusing to store failure class %r: the v%s vocabulary is %s, and "
            "a class nothing downstream recognises reads as NO classification, "
            "so a human cancellation spelled wrong is a technical failure"
            % (proposed, _failure_class_version(), ", ".join(classes))
        )
    if proposed == _UNCLASSIFIED_FAILURE:
        return (
            "refusing to store failure class %r: it may be recorded locally "
            "while a runner is still deciding, but it may not CLOSE an "
            "attempt, and the board is the record of a closed one "
            "(SP-INT-017). Name the cause, or `runtime-death` when the "
            "runtime died without one" % _UNCLASSIFIED_FAILURE
        )
    return ""


def _failure_class_version() -> Any:
    try:
        import runtime_contracts as rc

        return rc.FAILURE_CLASS_VERSION
    except Exception:  # noqa: BLE001
        return 1


def _failure_class_regression(current: str, proposed: str) -> str:
    """Why this class may not replace the one already on the binding, or "".

    IMMUTABLE ONCE SET, and this is the anti-relabelling rule rather than a
    tidiness one. A late callback that could rewrite `cancelled-by-human` as
    `runtime-death` would erase a steer after the fact and turn it back into a
    retry in every count downstream; one that could rewrite a technical
    failure as a human cancellation would manufacture a steer. Repetition is
    fine, because a retried callback must be idempotent.
    """
    if not current or current == proposed:
        return ""
    return (
        "refusing to relabel this attempt's failure class from %s to %s: the "
        "class is what distinguishes a human steer from a technical failure, "
        "and a projection that can be rewritten after the fact cannot "
        "distinguish anything" % (current, proposed)
    )


def _resolve_failure_class(
    stated: str, stored: str, stored_source: str, state: str
) -> Tuple[str, str]:
    """The class to store and where it came from.

    The default is narrow on purpose: ONLY a `cancelled` attempt with no class
    anywhere, and only because every control-plane route into that state
    either sets `cancelled-by-human` itself, defaults to it, or demands an
    explicit class. A `failed` or `expired` attempt with no class stays
    unclassified on the board, because nothing here knows why it ended and
    inventing a cause would be worse than an absent field.
    """
    if stated:
        return stated, CLASS_REPORTED
    if stored:
        return stored, stored_source or CLASS_REPORTED
    if state == HUMAN_CANCEL_ATTEMPT_STATE:
        return HUMAN_CANCEL_FAILURE_CLASS, CLASS_KERNEL_DEFAULT
    return "", ""


#: The one attempt state a bare cancellation can be inferred from.
HUMAN_CANCEL_ATTEMPT_STATE = "cancelled"


def _state_regression(current: str, proposed: str) -> str:
    """Why this attempt state may not replace the one on the binding, or "".

    Two rules, and the second was missing.

    CLOSED VOCABULARY. Only the canonical states are storable. Accepting any
    string let a common misspelling through: `canceled` with one L was stored
    happily, and since the advance guard recognises only the canonical
    spellings, that malformed cancellation then read as LIVE and did not
    trigger the cancellation fence at all. A projected state nobody downstream
    understands is worse than no projection, because it looks like one (#396).

    TERMINAL IS FINAL. An attempt that reached `cancelled`, `completed`,
    `failed` or `expired` cannot be moved back to a live state, because that is
    exactly how a cancelled attempt would become able to land a result again.
    """
    states, terminal = _attempt_state_vocabulary()
    if proposed not in states:
        return (
            "refusing to store attempt state %r: the vocabulary is %s, and a "
            "state nothing downstream recognises reads as LIVE, so a "
            "misspelled cancellation would silently lift the fence"
            % (proposed, ", ".join(states))
        )
    if current == proposed or not current:
        return ""
    if current in terminal and proposed not in terminal:
        return (
            "refusing to move attempt state from %s back to %s: a terminal "
            "attempt stays terminal, and reviving one is how a cancelled "
            "attempt would land a result" % (current, proposed)
        )
    return ""


# ── host policies ────────────────────────────────────────────────────────────


def baa_refusal_gate(message: str) -> Callable[[str], Optional[dict]]:
    """Refuse regulated projects, failing CLOSED when the parser errors.

    Cursor returned False on parser failure, so a broken config silently
    permitted PHI work over MCP. Codex failed closed on the same condition. An
    unreadable healthcare config is treated as regulated.
    """

    def _gate(project_dir: str) -> Optional[dict]:
        try:
            regulated = bool(_sp().healthcare_baa_enforced(str(project_dir)))
        except Exception:  # noqa: BLE001 - unreadable config is regulated
            regulated = True
        return {"error": message} if regulated else None

    return _gate


def policy_for_claude(
    project_dir: Optional[str] = None, enforcement: Optional[str] = None
) -> HostPolicy:
    """Claude Code policy.

    No BAA refusal: Anthropic's BAA covers Claude dispatches, so healthcare
    projects escalate gates rather than being refused (unchanged behaviour).
    A red DoD redirects to `blocked` rather than refusing, which is what the
    lifecycle wrappers do today and what the recovery ladder depends on.
    `-> blocked` stays ungated because blocking is de-escalation.
    """
    mode = enforcement
    if mode is None:
        mode = "enforce"
        if project_dir:
            try:
                from mode_reader import is_autonomous  # type: ignore

                mode = "enforce" if is_autonomous(str(project_dir)) else "warn"
            except Exception:  # noqa: BLE001
                mode = "enforce"
    return HostPolicy(
        host="claude",
        enforcement=mode,
        expected_backend=None,
        # Off for now: legitimate off-script transitions exist (recovery ladders,
        # PO decisions). Promoted to enforce after telemetry (see #277).
        require_next_action_match=False,
        gate_blocked_transitions=False,
        dod_on_done=True,
        dod_red_behavior="redirect_blocked",
        require_dispatch_binding=True,
    )


def policy_for_cursor(
    baa_message: Optional[str] = None,
    expected_backend: Optional[str] = "cursor",
    readiness_gate: Optional[Callable[[str], Optional[dict]]] = None,
    receipt_delivery_gate: Optional[Callable[[str, str], dict]] = None,
) -> HostPolicy:
    """Cursor policy: refuse PHI over MCP, bind cursor receipts, fail-closed BAA."""
    return HostPolicy(
        host="cursor",
        expected_backend=expected_backend,
        require_next_action_match=True,
        dod_on_done=True,
        dod_red_behavior="refuse",
        # Cursor refused on any non-passing DoD before the kernel existed.
        # Preserved rather than relaxed to the shared gate policy.
        dod_requires_pass=True,
        policy_gate=baa_refusal_gate(
            baa_message
            or "PHI over MCP is refused for baa_enforced projects; use the local "
            "Python path (core/lib/advance_kernel.py) instead."
        ),
        readiness_gate=readiness_gate,
        receipt_delivery_gate=receipt_delivery_gate,
        require_dispatch_binding=True,
    )


def policy_for_codex(
    policy_gate: Optional[Callable[[str], Optional[dict]]] = None,
    readiness_gate: Optional[Callable[[str], Optional[dict]]] = None,
    receipt_delivery_gate: Optional[Callable[[str, str], dict]] = None,
) -> HostPolicy:
    """Codex policy: regulated refusal + readiness probe + codex backend binding.

    Gains DoD-on-done, which the Codex MCP server did not evaluate at all.
    """
    return HostPolicy(
        host="codex",
        expected_backend="codex",
        require_next_action_match=True,
        dod_on_done=True,
        dod_red_behavior="refuse",
        policy_gate=policy_gate,
        readiness_gate=readiness_gate,
        receipt_delivery_gate=receipt_delivery_gate,
        require_dispatch_binding=True,
    )


# ── CLI ──────────────────────────────────────────────────────────────────────


def _print(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, default=str))


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Fail-closed advance kernel: the only legal writer of story state."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    adv = sub.add_parser("advance", help="Validate evidence and apply a transition")
    adv.add_argument("project_dir")
    adv.add_argument("story_id")
    adv.add_argument("to_state")
    adv.add_argument("--receipt-path", default=None)
    adv.add_argument("--reason", default=None)
    adv.add_argument(
        "--dry-run", action="store_true", help="evaluate only; never write state"
    )

    dis = sub.add_parser("begin_dispatch", help="Authorize and start a dispatch")
    dis.add_argument("project_dir")
    dis.add_argument("story_id")
    dis.add_argument("--role", default=None)

    bind = sub.add_parser(
        "bind_receipt", help="Print the canonical receipt path for a story stage"
    )
    bind.add_argument("project_dir")
    bind.add_argument("story_id")
    bind.add_argument("role_abbrev")

    rec = sub.add_parser(
        "reconcile_dispatch",
        help="Put the control plane's authoritative attempt generation on the board",
    )
    rec.add_argument("project_dir")
    rec.add_argument("story_id")
    rec.add_argument("role")
    rec.add_argument("attempt_id")
    rec.add_argument("--fencing-token", default="")
    rec.add_argument("--attempt-state", default="")
    # Claude's shipped surface IS this CLI (there is no Claude MCP server), so
    # without this flag `cancelled-by-human` could not reach the board on the
    # host the pilot's only managed profile runs on.
    rec.add_argument("--failure-class", default="")

    args = parser.parse_args()

    if args.command == "reconcile_dispatch":
        decision = reconcile_dispatch(
            args.project_dir,
            args.story_id,
            args.role,
            attempt_id=args.attempt_id,
            fencing_token=args.fencing_token,
            attempt_state=args.attempt_state,
            failure_class=args.failure_class,
        )
        _print(decision.to_dict())
        return 0 if decision.allowed else 1

    if args.command == "bind_receipt":
        try:
            path = canonical_receipt_path(
                args.project_dir, args.story_id, args.role_abbrev
            )
        except _Refusal as refusal:
            _print({"allowed": False, "code": refusal.code, "reason": refusal.reason})
            return 1
        state = _sp()._read_state(args.project_dir)
        story = _sp().get_story(state, args.story_id) or {}
        entered = stage_entered_at(story)
        fresh = None
        if path.is_file():
            try:
                payload = json.loads(path.read_bytes().decode("utf-8"))
                completed = parse_timestamp(payload.get("completed_at"))
                fresh = bool(
                    entered is not None
                    and completed is not None
                    and _sp().receipt_timestamp_is_fresh(completed, entered)
                )
            except (UnicodeDecodeError, json.JSONDecodeError):
                fresh = False
        _print(
            {
                "allowed": True,
                "receipt_path": str(path),
                "exists": path.is_file(),
                "fresh": fresh,
            }
        )
        return 0

    policy = policy_for_claude(args.project_dir)

    if args.command == "advance":
        if args.dry_run:
            decision = evaluate_advance(
                args.project_dir,
                args.story_id,
                args.to_state,
                receipt_path=args.receipt_path,
                reason=args.reason,
                policy=policy,
            )
        else:
            decision = execute_advance(
                args.project_dir,
                args.story_id,
                args.to_state,
                receipt_path=args.receipt_path,
                reason=args.reason,
                policy=policy,
            )
    else:
        decision = execute_dispatch(
            args.project_dir, args.story_id, role=args.role, policy=policy
        )

    _print(decision.to_dict())
    return 0 if decision.allowed else 1


if __name__ == "__main__":
    raise SystemExit(main())
