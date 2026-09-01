#!/usr/bin/env python3
"""The fail-closed advance kernel: the only legal writer of story pipeline state.

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# ── role tables ──────────────────────────────────────────────────────────────
# Single source of truth. Both MCP servers carried byte-identical copies.

ROLE_NAMES: Dict[str, str] = {
    "se": "software-engineer",
    "qe": "quality-engineer",
    "cr": "code-reviewer",
    "ce": "compliance-engineer",
    "pe": "platform-engineer",
    "po": "project-owner",
    "sa": "solution-architect",
    "tw": "technical-writer",
    "ra": "research-advisor",
}

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
BACKEND_MISMATCH = "backend_mismatch"
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
        BACKEND_MISMATCH,
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


def _sealed_cycle_binding(project_dir: str) -> Optional[dict]:
    """The current Cycle's sealed identity, or None when this is not an SPQ
    project or no Cycle is open.

    Read defensively and treated as absent on any failure: attempt identity
    must never be the reason a delivery-critical dispatch cannot proceed, and
    a project mid-migration between layouts is a normal state rather than an
    error. What the absence costs is the manifest check below, which then
    simply does not run.
    """
    try:
        if build_mode(project_dir) != "spq":
            return None
    except Exception:
        return None
    try:
        import json as _json  # noqa: PLC0415
        import spq_paths  # noqa: PLC0415  (optional, SPQ-only import)

        cycle_id = str(_sp()._read_state(str(project_dir)).get("_cycle_id") or "")
        if not cycle_id:
            return None
        path = spq_paths.manifest_path(str(project_dir), cycle_id)
        with open(path, "r", encoding="utf-8") as handle:
            manifest = _json.load(handle)
    except Exception:
        return None
    if not isinstance(manifest, dict):
        return None
    binding = {
        "cycle_id": str(manifest.get("cycle_id") or ""),
        "manifest_hash": str(manifest.get("manifest_hash") or ""),
        "admitted_unit_ids": [
            str(u.get("id") or "")
            for u in (manifest.get("work_units") or [])
            if isinstance(u, dict) and u.get("id")
        ],
    }
    return binding if binding["cycle_id"] and binding["manifest_hash"] else None


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
    if cycle is None:
        return {
            "binding": {"attempt_id": attempt_id, "fencing_token": fencing_token}
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
    workstream_id = ""
    try:
        import spq_paths  # noqa: PLC0415

        workstream_id = str(spq_paths.read_pin(str(project_dir)) or "")
    except Exception:
        workstream_id = ""
    return {
        "binding": {
            "attempt_id": attempt_id,
            "fencing_token": fencing_token,
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
        False applies `dod_gate_block_reason`, the documented product policy: only
        the CONDITIONAL gates (no_critical_findings, runtime_verified,
        ui_acceptance, integration_verified) and a replay mismatch block, because
        static checks are self-attested and are covered by receipt replay instead.
        True additionally requires `dod["passed"]`, which is what the Cursor MCP
        server did on its own. Kept as a knob rather than collapsed so adopting
        the kernel does not silently RELAX a host; see #276.
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

        # 5. role binding for this edge
        intensity: Optional[str] = None
        try:
            intensity = _dod_intensity(project_dir, state, build_mode(project_dir))
        except Exception:  # noqa: BLE001 - keep the static table when mode is unreadable
            intensity = None
        expected = expected_receipt_role(from_state, to_state, intensity=intensity)
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
            expected_role, abbrev = expected
            _record(checks, "role_bound", True, expected_role)

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
                if (
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
                if policy.require_dispatch_binding:
                    active_dispatches = story.get("mcp_active_dispatches") or {}
                    active = (
                        active_dispatches.get(abbrev)
                        if isinstance(active_dispatches, dict)
                        else None
                    )
                    dispatch_id = (
                        active.get("dispatch_id") if isinstance(active, dict) else None
                    )
                    if dispatch_id and receipt.get("dispatch_id") != dispatch_id:
                        bad = _fail(
                            DISPATCH_BINDING_MISMATCH,
                            "receipt dispatch_id does not match the active dispatch",
                            story=story,
                        )
                        if bad is not None:
                            return bad
                    # Epic #339. Enforced on MISMATCH, not on absence. Every
                    # binding now carries an attempt id, so requiring the
                    # receipt to echo one would refuse every agent that has
                    # not yet been taught to stamp it, which is all of them
                    # until the evidence contract ships (#342) and each host
                    # picks it up. A receipt that omits the field falls back
                    # to the dispatch_id binding checked just above, so this
                    # window loses no protection that exists today; it only
                    # declines to add new protection before the producers can
                    # satisfy it. Requiring presence is a deliberate later
                    # tightening, and it belongs with the managed runner
                    # (#352/#355), where the runner is handed the envelope and
                    # echoing it is the whole point of the fencing token.
                    attempt_id = (
                        active.get("attempt_id") if isinstance(active, dict) else None
                    )
                    receipt_attempt = receipt.get("attempt_id")
                    if attempt_id and receipt_attempt and receipt_attempt != attempt_id:
                        bad = _fail(
                            ATTEMPT_BINDING_MISMATCH,
                            "receipt attempt_id does not match the active attempt",
                            story=story,
                        )
                        if bad is not None:
                            return bad
                    bound_hash = (
                        active.get("manifest_hash") if isinstance(active, dict) else None
                    )
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
            role=expected[0] if expected else None,
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
            active_dispatches.pop(abbrev, None)

    dod = decision.dod
    if policy.dod_on_done and target in _DONE_EDGE:
        if dod is None:
            mode = build_mode(project_dir)
            dod = sp.evaluate_story_dod(
                project_dir, story_id, _dod_intensity(project_dir, state, mode)
            )
        story["dod"] = dod

    sp._write_state(project_dir, state)

    if dod is not None and target in _DONE_EDGE:
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

    action = str(selected.get("action") or "")
    if selected.get("story_id") != story_id:
        return Decision(
            allowed=False,
            code=NEXT_ACTION_MISMATCH,
            reason="next_action did not select story %s" % story_id,
            checks=checks,
            next_action=selected,
        )
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

    return Decision(
        allowed=True,
        code=OK,
        checks=checks,
        warnings=warnings,
        story=story,
        next_action=selected,
        role=role_full_name(str(raw_role)) or role_full_name(str(role or "")),
        extra={"role_abbrev": selected_abbrev or role_abbrev(str(role or ""))},
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
    # Recording is unconditional; ENFORCEMENT stays policy-gated at advance
    # (#340, #344). These were one flag, which made the binding exist only on
    # the hosts that already refused a mismatch: `policy_for_claude` does not
    # set `require_dispatch_binding`, so Claude recorded nothing, and anything
    # built on the binding there was dead code before it was written. Cursor
    # and Codex behaviour is unchanged, because the refusal branch still reads
    # `policy.require_dispatch_binding`.
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
        attempt_binding = _mint_attempt_binding(project_dir, state, story_id, abbrev)
        if attempt_binding is not None:
            if attempt_binding.get("refusal"):
                return Decision(
                    allowed=False,
                    code=attempt_binding["refusal"]["code"],
                    reason=attempt_binding["refusal"]["reason"],
                )
            binding.update(attempt_binding["binding"])
            contract["attempt_id"] = attempt_binding["binding"]["attempt_id"]
            contract["fencing_token"] = attempt_binding["binding"]["fencing_token"]
            for key in ("cycle_id", "manifest_hash", "workstream_id"):
                if attempt_binding["binding"].get(key):
                    contract[key] = attempt_binding["binding"][key]
        dispatches[abbrev] = binding

    # Archive only after every state/ledger precondition has passed. From this
    # point the recovery path has no refusal branch before the single state
    # write below, so callers never see an archived receipt paired with a
    # rejected retry dispatch.
    if recovery and abbrev and contract.get("receipt_path"):
        failed_path = Path(str(contract["receipt_path"]))
        if failed_path.is_file():
            archive_dir = failed_path.parent / "archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(failed_path.read_bytes()).hexdigest()[:16]
            archived = archive_dir / f"{failed_path.stem}.failed-{digest}.json"
            suffix = 1
            while archived.exists():
                archived = archive_dir / (
                    f"{failed_path.stem}.failed-{digest}-{suffix}.json"
                )
                suffix += 1
            shutil.move(str(failed_path), str(archived))
            failure_reason = str(selected.get("reason") or recovery.get("verdict") or "")
            retry_count = sp.record_retry(
                state, story_id, abbrev, failure_reason, project_dir=project_dir
            )
            decision.extra["archived_receipt"] = str(archived)
            decision.extra["retry_count"] = retry_count

    if action == "recover_blocked":
        state = sp.unblock_story(state, story_id, project_dir)
    elif current == "queued":
        state = sp.transition_story(
            state, story_id, "in_progress", None, project_dir
        )
    elif current == "in_progress" and action == "dispatch_se" and not recovery:
        # Lost the queued claim: the first process already started this story.
        # Resume is an orchestrator decision against an existing in_progress
        # story, not a second begin_dispatch.
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

    args = parser.parse_args()

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
