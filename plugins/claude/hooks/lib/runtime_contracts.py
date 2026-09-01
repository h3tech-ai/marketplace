#!/usr/bin/env python3
# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
"""Runtime-federation contracts: the dispatch envelope, attempt identity,
supervision events, and adapter profiles (Epic #339, ticket #341).

Source of truth: docs/proposals/governed-runtime-pilot.md r3+ (schemas in
section 4, vocabulary notes in 4.4). Where this module and that document
disagree, the document wins and this module gets corrected.

This lives in core/lib rather than the proposal's provisional `core/runtime/`
package because composition already ships core/lib to every host (Claude via
symlink, Cursor and Codex via compose.py); a new top-level core directory
would need compose and build changes on two hosts before the first consumer
existed. The proposal's change map is amended to match (r4).

Validation is hand-rolled in the spq_manifest.py idiom: no jsonschema
dependency, defensive coercion, and every rule returns a message naming the
field so a refusal is actionable. Validators return a list of problems; an
empty list is a pass. Nothing here signs or stores anything: the kernel mints
and binds (ticket #344), the CP coordinates (#350), the bridge executes
(#345). This module only defines what a well-formed object IS.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

SCHEMA_VERSION = 1

ENVELOPE_KIND = "synaptory.dispatch.envelope"

# The pilot's placements. "managed-server" and "provider-managed" are Platform
# waves, deliberately absent: an envelope naming one must be refused, not
# forwarded (proposal 3.4: managed-laptop is a V1-only placement).
PLACEMENTS = ("local", "managed-laptop")

RUNTIME_FAMILIES = ("claude-code", "codex", "cursor-agent")

SELECTION_MODES = ("auto", "prefer", "pin")

# Platform vocabulary carried alongside V1's roster (spec 0.13.0). Capability
# profiles are intent labels for skill composition and grant nothing
# (SC-MTH-004); stage profiles are where Platform obligations attach
# (SP-WRK-022). V1 dispatches roles; these two fields are the evidence overlay
# that lets the same receipt be read in Platform terms.
CAPABILITY_PROFILES = ("analyst", "planner", "producer", "prover")

STAGE_PROFILES = (
    "admitting",
    "analysing",
    "planning",
    "producing",
    "verifying",
    "releasing",
    "operating",
)

# Dispatch-time projection of the nine-agent roster onto the four profiles.
# Three agents straddle profiles in the SPQ design (6.3): po spans analyst and
# planner, pe spans planner and producer, qe spans planner and prover. A
# dispatch is one bounded act, so the straddle resolves to the profile of the
# act being dispatched (a qe dispatch verifies; its planning half happens
# inside po/sa ceremonies). The straddles themselves are research output and
# are recorded as MethodSignals by the ceremonies, not here.
DISPATCH_CAPABILITY_PROFILE = {
    "se": "producer",
    "qe": "prover",
    "cr": "prover",
    "ce": "prover",
    "pe": "producer",
    "tw": "producer",
    "po": "planner",
    "sa": "planner",
    "ra": "analyst",
}

DISPATCH_STAGE_PROFILE = {
    "se": "producing",
    "qe": "verifying",
    "cr": "verifying",
    "ce": "verifying",
    "pe": "producing",
    "tw": "producing",
    "po": "planning",
    "sa": "planning",
    "ra": "analysing",
}

# Attempt lifecycle. An attempt never re-enters a prior state; the CP enforces
# the transition table (#350), this module only names the vocabulary.
ATTEMPT_STATES = (
    "pending",
    "claimed",
    "running",
    "completed",
    "failed",
    "cancelled",
    "expired",
)

TERMINAL_ATTEMPT_STATES = frozenset({"completed", "failed", "cancelled", "expired"})

# att_ + 6..32 url-safe chars. Deliberately looser than a ULID so the minting
# side can evolve without a lockstep validator change; uniqueness is the CP's
# job, shape is ours.
ATTEMPT_ID_RE = re.compile(r"^att_[A-Za-z0-9_-]{6,32}$")

# secrets.token_hex(16) from the kernel's existing dispatch binding.
DISPATCH_ID_RE = re.compile(r"^[0-9a-f]{32}$")

PROFILE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")

# The seven supervision sub-kinds (proposal 4.2), with a payload-key allowlist
# per kind. The allowlist is the privacy posture in code form (SP-SEC-037):
# an unknown key is refused rather than forwarded, so a leaky adapter fails
# fast in tests instead of shipping content to the control plane.
SUPERVISION_EVENT_KINDS = {
    "attempt/started": frozenset({"profile", "placement", "model_id"}),
    "attempt/status": frozenset({"status"}),
    "tool/call": frozenset({"tool", "summary", "duration_ms"}),
    "file/write": frozenset({"path", "size_bytes"}),
    "step/complete": frozenset({"label", "result", "digest"}),
    "budget/update": frozenset({"budget_kind", "consumed", "remaining"}),
    "runtime/error": frozenset({"error_kind", "diagnostic_level"}),
}

# Sized so a "summary" stays a summary. tool/call summaries and step labels
# are the two fields a lazy adapter would stuff raw output into.
MAX_EVENT_TEXT_LEN = 200

# Failure classification (SP-INT-017 design input). Versioned closed
# vocabulary; extend by appending and bumping FAILURE_CLASS_VERSION, never by
# ad-hoc strings in receipts.
FAILURE_CLASS_VERSION = 1
FAILURE_CLASSES = (
    "environment",
    "deterministic-check",
    "context-knowledge",
    "skill-rule",
    "workflow-structure",
    "agent-specialization",
    "model-policy",
    "cancelled-by-human",
    "budget-exhausted",
    "runtime-death",
    "unclassified",
)

_ENVELOPE_REQUIRED_STR = (
    "attempt_id",
    "dispatch_id",
    "project_id",
    "cycle_id",
    "manifest_hash",
    "workstream_id",
    "story_id",
    "stage",
    "stage_profile",
    "role",
    "capability_profile",
    "adapter_profile_id",
    "runtime_family",
    "placement",
    "source_revision",
    "receipt_path",
    "expires_at",
    "fencing_token",
)


def build_envelope(
    *,
    attempt_id: str,
    dispatch_id: str,
    project_id: str,
    cycle_id: str,
    manifest_hash: str,
    workstream_id: str,
    story_id: str,
    stage: str,
    role: str,
    adapter_profile_id: str,
    runtime_family: str,
    placement: str,
    source_revision: str,
    receipt_path: str,
    expires_at: str,
    fencing_token: str,
    model_route: Optional[Dict[str, Any]] = None,
    materialization: Optional[Dict[str, Any]] = None,
    connector_refs: Optional[Dict[str, Any]] = None,
    config_refs: Optional[Dict[str, Any]] = None,
    workspace: Optional[Dict[str, Any]] = None,
    capability_ceiling: Optional[Sequence[str]] = None,
    network_allowlist: str = "project-default",
    budget: Optional[Dict[str, Any]] = None,
    required_evidence: Optional[Sequence[str]] = None,
    stage_profile: str = "",
    capability_profile: str = "",
) -> Dict[str, Any]:
    """Assemble a dispatch envelope. The kernel signs and persists the
    binding; this only shapes the object. stage_profile and capability_profile
    default from the role's dispatch-time projection so a caller cannot ship
    an envelope with the Platform overlay silently absent."""
    role_key = str(role or "").strip().lower()
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": ENVELOPE_KIND,
        "attempt_id": str(attempt_id),
        "dispatch_id": str(dispatch_id),
        "project_id": str(project_id),
        "cycle_id": str(cycle_id),
        "manifest_hash": str(manifest_hash),
        "workstream_id": str(workstream_id),
        "story_id": str(story_id),
        "stage": str(stage),
        "stage_profile": str(
            stage_profile or DISPATCH_STAGE_PROFILE.get(role_key, "")
        ),
        "role": role_key,
        "capability_profile": str(
            capability_profile or DISPATCH_CAPABILITY_PROFILE.get(role_key, "")
        ),
        "adapter_profile_id": str(adapter_profile_id),
        "runtime_family": str(runtime_family),
        "placement": str(placement),
        "model_route": dict(model_route or {"requested": "policy-default"}),
        "source_revision": str(source_revision),
        "materialization": dict(materialization) if materialization else None,
        "connector_refs": dict(connector_refs) if connector_refs else None,
        "config_refs": dict(config_refs) if config_refs else None,
        "workspace": dict(workspace or {"mode": "existing-locked-worktree"}),
        "capability_ceiling": list(capability_ceiling or []),
        "network_allowlist": str(network_allowlist),
        "budget": dict(budget or {}),
        "receipt_path": str(receipt_path),
        "expires_at": str(expires_at),
        "fencing_token": str(fencing_token),
        "required_evidence": list(required_evidence or []),
    }


def validate_envelope(env: Any) -> List[str]:
    """Every reason this is not a well-formed dispatch envelope. Empty = pass.

    Secrets are refused structurally: connector_refs values must be cred://
    references, never inline material, because the envelope is an authority
    ceiling that crosses process boundaries and gets logged.
    """
    problems: List[str] = []
    if not isinstance(env, dict):
        return ["envelope: not an object"]
    if env.get("schema_version") != SCHEMA_VERSION:
        problems.append(
            "schema_version: expected %d, got %r"
            % (SCHEMA_VERSION, env.get("schema_version"))
        )
    if env.get("kind") != ENVELOPE_KIND:
        problems.append("kind: expected %r, got %r" % (ENVELOPE_KIND, env.get("kind")))
    for field in _ENVELOPE_REQUIRED_STR:
        value = env.get(field)
        if not isinstance(value, str) or not value.strip():
            problems.append("%s: required non-empty string" % field)
    attempt_id = env.get("attempt_id")
    if isinstance(attempt_id, str) and attempt_id and not ATTEMPT_ID_RE.match(attempt_id):
        problems.append("attempt_id: %r does not match %s" % (attempt_id, ATTEMPT_ID_RE.pattern))
    dispatch_id = env.get("dispatch_id")
    if isinstance(dispatch_id, str) and dispatch_id and not DISPATCH_ID_RE.match(dispatch_id):
        problems.append("dispatch_id: not a 32-char lowercase hex token")
    profile_id = env.get("adapter_profile_id")
    if isinstance(profile_id, str) and profile_id and not PROFILE_ID_RE.match(profile_id):
        problems.append("adapter_profile_id: %r is not a valid profile id" % profile_id)
    placement = env.get("placement")
    if placement not in PLACEMENTS:
        problems.append("placement: %r not in %r" % (placement, PLACEMENTS))
    if env.get("runtime_family") not in RUNTIME_FAMILIES:
        problems.append(
            "runtime_family: %r not in %r" % (env.get("runtime_family"), RUNTIME_FAMILIES)
        )
    if env.get("stage_profile") not in STAGE_PROFILES:
        problems.append(
            "stage_profile: %r not in %r" % (env.get("stage_profile"), STAGE_PROFILES)
        )
    if env.get("capability_profile") not in CAPABILITY_PROFILES:
        problems.append(
            "capability_profile: %r not in %r"
            % (env.get("capability_profile"), CAPABILITY_PROFILES)
        )
    # Managed placement must be independently materializable: the runner has
    # no host environment to fall back to (proposal 4.5, 6.4). Local
    # placements may omit both blocks and use the host's own checkout.
    if placement == "managed-laptop":
        mat = env.get("materialization")
        if not isinstance(mat, dict):
            problems.append("materialization: required for managed placement")
        else:
            for field in ("repo_url", "ref", "revision"):
                if not str(mat.get(field) or "").strip():
                    problems.append("materialization.%s: required non-empty" % field)
        if not isinstance(env.get("connector_refs"), dict):
            problems.append("connector_refs: required for managed placement")
    refs = env.get("connector_refs")
    if isinstance(refs, dict):
        for name, ref in refs.items():
            if ref is None:
                continue
            if not isinstance(ref, str) or not ref.startswith("cred://"):
                problems.append(
                    "connector_refs.%s: must be a cred:// reference, never inline material"
                    % name
                )
    ceiling = env.get("capability_ceiling")
    if not isinstance(ceiling, list) or not all(isinstance(c, str) for c in ceiling or []):
        problems.append("capability_ceiling: must be a list of strings")
    budget = env.get("budget")
    if not isinstance(budget, dict):
        problems.append("budget: must be an object")
    return problems


def validate_supervision_event(event: Any) -> List[str]:
    """Every reason this is not a well-formed, privacy-safe supervision event.

    The payload allowlist is deliberately closed: content the table in
    proposal 3.5 says never leaves the runtime has no field to travel in.
    """
    problems: List[str] = []
    if not isinstance(event, dict):
        return ["event: not an object"]
    seq = event.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        problems.append("seq: required integer >= 1")
    if not str(event.get("ts") or "").strip():
        problems.append("ts: required non-empty timestamp string")
    attempt_id = event.get("attempt_id")
    if not isinstance(attempt_id, str) or not ATTEMPT_ID_RE.match(attempt_id or ""):
        problems.append("attempt_id: required, matching %s" % ATTEMPT_ID_RE.pattern)
    if event.get("privacy_safe") is not True:
        problems.append("privacy_safe: must be literal true")
    kind = event.get("kind")
    allowed = SUPERVISION_EVENT_KINDS.get(kind or "")
    if allowed is None:
        problems.append(
            "kind: %r not in %r" % (kind, sorted(SUPERVISION_EVENT_KINDS))
        )
        return problems
    payload = event.get("payload")
    if not isinstance(payload, dict):
        problems.append("payload: required object")
        return problems
    extra = set(payload) - allowed
    if extra:
        problems.append(
            "payload: keys %r not allowed for kind %r (allowlist: %r)"
            % (sorted(extra), kind, sorted(allowed))
        )
    for key, value in payload.items():
        if isinstance(value, str) and len(value) > MAX_EVENT_TEXT_LEN:
            problems.append(
                "payload.%s: %d chars exceeds the %d-char summary cap"
                % (key, len(value), MAX_EVENT_TEXT_LEN)
            )
        if isinstance(value, (dict, list)):
            problems.append("payload.%s: nested structures are not allowed" % key)
    return problems


def validate_profile(record: Any) -> List[str]:
    """Every reason this is not a well-formed adapter-profile record.

    A profile is the immutable certified combination of adapter, runtime,
    placement, and capabilities (proposal 3.1). capability_profiles is the
    Platform overlay: which of the four profiles this runtime may serve, the
    selector's policy key (r4 amendment)."""
    problems: List[str] = []
    if not isinstance(record, dict):
        return ["profile: not an object"]
    profile_id = record.get("profile_id")
    if not isinstance(profile_id, str) or not PROFILE_ID_RE.match(profile_id or ""):
        problems.append("profile_id: required, matching %s" % PROFILE_ID_RE.pattern)
    if record.get("runtime_family") not in RUNTIME_FAMILIES:
        problems.append(
            "runtime_family: %r not in %r"
            % (record.get("runtime_family"), RUNTIME_FAMILIES)
        )
    if record.get("placement") not in PLACEMENTS:
        problems.append(
            "placement: %r not in %r" % (record.get("placement"), PLACEMENTS)
        )
    if not str(record.get("pinned_version") or "").strip():
        problems.append("pinned_version: required non-empty string")
    served = record.get("capability_profiles")
    if (
        not isinstance(served, list)
        or not served
        or not set(served) <= set(CAPABILITY_PROFILES)
    ):
        problems.append(
            "capability_profiles: required non-empty subset of %r"
            % (CAPABILITY_PROFILES,)
        )
    caps = record.get("capabilities")
    if not isinstance(caps, list) or not all(isinstance(c, str) for c in caps or []):
        problems.append("capabilities: must be a list of strings")
    return problems


def validate_receipt_attempt_binding(receipt: Any) -> List[str]:
    """The attempt-identity fields a pilot receipt payload must carry
    (proposal 4.4: identity lives in the payload, never the filename).
    Enforcement at advance is the kernel's job (#342/#344); this names the
    shape so hosts and fixtures agree on it."""
    problems: List[str] = []
    if not isinstance(receipt, dict):
        return ["receipt: not an object"]
    attempt_id = receipt.get("attempt_id")
    if not isinstance(attempt_id, str) or not ATTEMPT_ID_RE.match(attempt_id or ""):
        problems.append("attempt_id: required, matching %s" % ATTEMPT_ID_RE.pattern)
    dispatch_id = receipt.get("dispatch_id")
    if not isinstance(dispatch_id, str) or not DISPATCH_ID_RE.match(dispatch_id or ""):
        problems.append("dispatch_id: required 32-char lowercase hex token")
    failure_class = receipt.get("failure_class")
    if failure_class is not None and failure_class not in FAILURE_CLASSES:
        problems.append(
            "failure_class: %r not in the v%d vocabulary %r"
            % (failure_class, FAILURE_CLASS_VERSION, FAILURE_CLASSES)
        )
    return problems
