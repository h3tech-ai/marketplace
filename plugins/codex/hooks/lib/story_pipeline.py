#!/usr/bin/env python3
"""Shared story-level pipeline for synaptory v2.

Manages story sub-states (queued → in_progress → testing → reviewing → done),
per-story DoD evaluation with adaptive intensity, and cycle time calculation.

Used by both the Scrum and Kanban state machines. Functions are pure — they
accept and return state dicts. The CLI wrapper handles file I/O for standalone use.

CLI:
    python3 story_pipeline.py create_story <project_dir> <story_id> [--title "..."]
    python3 story_pipeline.py transition <project_dir> <story_id> <to_state> [--reason "..."]
    python3 story_pipeline.py unblock <project_dir> <story_id>
    python3 story_pipeline.py get_story <project_dir> <story_id>
    python3 story_pipeline.py list_stories <project_dir> <state>
    python3 story_pipeline.py evaluate_dod <project_dir> <story_id> [--sprint N] [--ticket-number N] [--release]
    python3 story_pipeline.py aggregate_dod <project_dir>
    python3 story_pipeline.py cycle_time <project_dir> <story_id>
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _host_env():
    """Lazy import so a partial install cannot break module load."""
    import host_env

    return host_env


def _profile_pair_for_role(role: Any) -> dict[str, str] | None:
    """The `(stage_profile, capability_profile)` pair a dispatch role runs
    under, as a dict, or None when the role does not project onto one (#402).

    Lazy import for the same reason as `_host_env`: a partial install must not
    break module load, and a dispatcher that crashed because the profile
    vocabulary was absent would be strictly worse than one that says nothing
    about profiles.
    """
    if not role:
        return None
    try:
        from runtime_contracts import profile_pair_for_role

        stage_profile, capability_profile = profile_pair_for_role(role)
    except (ImportError, ValueError):
        return None
    return {
        "stage_profile": stage_profile,
        "capability_profile": capability_profile,
        "role": str(role),
    }


try:
    from synaptory_logger import emit as _log_emit
except ImportError:  # synaptory_logger may not be on sys.path in some invocations
    def _log_emit(event: str, project_dir: str | None = None, **payload: Any) -> None:
        return None


def _emit_project_event(
    event: str, project_dir: str | os.PathLike[str] | None, **payload: Any
) -> None:
    """Log a project event, or drop it when the project is not known.

    ONE PLACE DECIDES, and it never falls back to the working directory.
    Handed `project_dir=None`, `synaptory_logger.emit` addresses
    `host_env.project_dir()`, which ends at `os.getcwd()` -- so an unaddressed
    event is written into whatever directory the process happened to be in.
    Most call sites in this module pass the project; `story_create` and the
    `forced_transition` override did not, and `transition_story` takes it
    OPTIONALLY, so any caller that omits it silently redirects the write.

    What that cost: running `./synaptory test` grew a
    `.synaptory/.orchestrator/{events.jsonl,chain-id}` in the checkout under
    test, worktree or not (#380). `.synaptory/*` is gitignored, so `git status`
    stayed clean and nothing showed -- the same invisibility that made #502 and
    #568 read as mysterious corruption instead of as a test leak.

    Dropping rather than redirecting is not a new best-effort: `emit`'s own
    contract already drops events it cannot write ("missing workspace,
    permission denied") so hooks stay non-blocking, and an event with no
    project is exactly that. Dropping is also the only remedy that reaches
    callers this module does not own -- the host adapters and the tests that
    build a record without naming a project.
    """
    if project_dir is None:
        return
    _log_emit(event, project_dir=project_dir, **payload)


def is_per_story_acceptance_enabled(project_dir: str | os.PathLike) -> bool:
    """Read the `sprint.review.per_story_acceptance` toggle (#116).

    Returns the configured value when the key is present. When the key (or the
    whole `.synaptory.yaml`) is absent, returns `false` — the back-compat
    runtime default for existing projects that never set it. Note this is the
    *runtime absent-key* default; the project template
    (`templates/synaptory.yaml.tmpl`) ships the toggle `true`, so newly
    scaffolded projects opt into per-story PO acceptance by default.

    Read directly from `.synaptory.yaml` with a minimal regex sweep — we don't
    pull in a full YAML parser at this layer just to read one bool, and the
    surrounding code already uses the homegrown `_parse_yaml_lite` in
    tracker/config.py for the same reason.
    """
    cfg_path = Path(project_dir) / ".synaptory.yaml"
    if not cfg_path.exists():
        return False
    try:
        text = cfg_path.read_text(encoding="utf-8")
    except OSError:
        return False

    # Look for the key under a `sprint:` → `review:` nesting. We scan for
    # the literal key line because the toggle is documented as a fixed
    # location in the template.
    in_sprint = False
    in_review = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line:
            continue
        indent = len(line) - len(line.lstrip())
        s = line.strip()
        if indent == 0 and s.startswith("sprint:"):
            in_sprint, in_review = True, False
            continue
        if indent == 0 and not s.startswith("sprint:"):
            in_sprint, in_review = False, False
            continue
        if in_sprint and indent == 2 and s.startswith("review:"):
            in_review = True
            continue
        if in_sprint and indent == 2 and not s.startswith("review:"):
            in_review = False
            continue
        if in_review and indent >= 4 and s.startswith("per_story_acceptance:"):
            val = s.split(":", 1)[1].strip().strip('"').strip("'").lower()
            return val in ("true", "yes", "1", "on")
    return False


def _read_config_text(project_dir: str | os.PathLike) -> str:
    """Return `.synaptory.yaml` text, or "" if absent/unreadable."""
    cfg_path = Path(project_dir) / ".synaptory.yaml"
    try:
        return cfg_path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _truthy(val: str) -> bool:
    return val.strip().strip('"').strip("'").lower() in ("true", "yes", "1", "on")


def healthcare_baa_enforced(project_dir: str | os.PathLike) -> bool:
    """Read `healthcare.baa_enforced` from `.synaptory.yaml` (#31).

    Default `false`. Same minimal-scan rationale as
    `is_per_story_acceptance_enabled` — one bool, no YAML dependency at this
    layer. Matches the top-level `healthcare:` block → `baa_enforced:` key.
    """
    in_healthcare = False
    for raw in _read_config_text(project_dir).splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line:
            continue
        indent = len(line) - len(line.lstrip())
        s = line.strip()
        if indent == 0:
            in_healthcare = s.startswith("healthcare:")
            continue
        if in_healthcare and indent >= 2 and s.startswith("baa_enforced:"):
            return _truthy(s.split(":", 1)[1])
    return False


def runtime_verification_required(project_dir: str | os.PathLike) -> bool:
    """Read `quality.runtime_verification` from `.synaptory.yaml` (#30).

    A project opts every user-facing story into the mandatory runtime-
    verification gate by setting `quality.runtime_verification: required`
    (or `true`). Default `false` — healthcare PHI stories still get the gate
    via `active_dod_checks` regardless of this toggle.
    """
    in_quality = False
    for raw in _read_config_text(project_dir).splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line:
            continue
        indent = len(line) - len(line.lstrip())
        s = line.strip()
        if indent == 0:
            in_quality = s.startswith("quality:")
            continue
        if in_quality and indent >= 2 and s.startswith("runtime_verification:"):
            val = s.split(":", 1)[1].strip().strip('"').strip("'").lower()
            return val in ("required", "true", "yes", "1", "on")
    return False


# #134 (GAP-3/GAP-4) — per-story verification-intensity tiers. Resolution is
# most-specific-wins, mirroring the v2 §12 runtime-policy precedence:
#   quality.verification.stories[<id>]  >  .by_kind[<kind>]  >  .default
#   >  legacy `runtime_verification: required` (⇒ full)  >  "standard".
VERIFICATION_TIERS: tuple[str, ...] = ("minimal", "standard", "full")

# Story kinds that never describe a user-facing screen. A story tagged with
# one of these suppresses the `ui_bearing` keyword heuristic outright — the
# regex over-flags enabler work ("define color tokens" whose AC says a
# component *renders* them) into full browser QA (#134 GAP-4).
_NON_UI_KINDS: frozenset[str] = frozenset({"enabler", "infra", "backend"})


def _parse_verification_config(project_dir: str | os.PathLike) -> dict[str, Any]:
    """Parse the `quality.verification` block from `.synaptory.yaml`.

    Returns {"default": str|None, "by_kind": {kind: tier}, "stories": {id: tier}}.
    Unknown tier values are ignored (fail toward the coarser default). Same
    lite line-scanner used by the sibling config readers.
    """
    out: dict[str, Any] = {"default": None, "by_kind": {}, "stories": {}}
    in_quality = False
    in_verification = False
    submap: str | None = None  # "by_kind" | "stories" when inside one
    for raw in _read_config_text(project_dir).splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line:
            continue
        indent = len(line) - len(line.lstrip())
        s = line.strip()
        if indent == 0:
            in_quality = s.startswith("quality:")
            in_verification = False
            submap = None
            continue
        if not in_quality:
            continue
        if indent == 2:
            in_verification = s.startswith("verification:")
            submap = None
            continue
        if not in_verification:
            continue
        if indent == 4:
            if s.startswith("default:"):
                val = s.split(":", 1)[1].strip().strip('"').strip("'").lower()
                if val in VERIFICATION_TIERS:
                    out["default"] = val
                submap = None
            elif s.startswith("by_kind:"):
                submap = "by_kind"
            elif s.startswith("stories:"):
                submap = "stories"
            else:
                submap = None
            continue
        if submap and indent >= 6 and ":" in s and not s.startswith("-"):
            key, _, val = s.partition(":")
            val = val.strip().strip('"').strip("'").lower()
            key = key.strip().strip('"').strip("'")
            if key and val in VERIFICATION_TIERS:
                out[submap][key] = val
    return out


def resolve_verification_tier(
    project_dir: str | os.PathLike, story: dict[str, Any] | None
) -> str:
    """Resolve a story's verification tier (#134 GAP-3).

    Most-specific-wins; PHI enforcement is applied by the CALLER
    (`evaluate_story_dod` forces `full` for PHI-touching stories) so the tier
    system can never weaken the compliance path.
    """
    cfg = _parse_verification_config(project_dir)
    story = story or {}
    sid = str(story.get("id") or "").strip()
    if sid and sid in cfg["stories"]:
        return cfg["stories"][sid]
    kind = str(story.get("kind") or "").strip().lower()
    if kind and kind in cfg["by_kind"]:
        return cfg["by_kind"][kind]
    if cfg["default"]:
        return cfg["default"]
    # Legacy project-wide toggle (#30) maps to the strictest tier so existing
    # projects keep exactly their current gate behaviour.
    if runtime_verification_required(project_dir):
        return "full"
    return "standard"


def parallelism_config(project_dir: str | os.PathLike) -> dict[str, Any]:
    """Parse the `parallelism` block (#134 GAP-6/GAP-8).

    Runtime absent-key defaults keep story parallelism OFF (same back-compat
    posture as `per_story_acceptance`): the template ships it enabled for new
    projects, existing projects opt in.
    """
    out: dict[str, Any] = {
        "max_concurrent": 3,
        "story_parallelism": False,
        "isolation": "worktree",
    }
    in_par = False
    for raw in _read_config_text(project_dir).splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line:
            continue
        indent = len(line) - len(line.lstrip())
        s = line.strip()
        if indent == 0:
            in_par = s.startswith("parallelism:")
            continue
        if not in_par or indent < 2:
            continue
        if s.startswith("max_concurrent_subagents:"):
            try:
                out["max_concurrent"] = max(1, int(s.split(":", 1)[1].strip()))
            except ValueError:
                pass
        elif s.startswith("story_parallelism:"):
            val = s.split(":", 1)[1].strip().strip('"').strip("'").lower()
            out["story_parallelism"] = val in ("enabled", "true", "yes", "1", "on")
        elif s.startswith("isolation:"):
            val = s.split(":", 1)[1].strip().strip('"').strip("'").lower()
            if val in ("worktree", "shared"):
                out["isolation"] = val
    return out


def concurrency_policy(project_dir: str | os.PathLike) -> dict[str, Any]:
    """THE effective concurrency policy, which is not the same block on every
    lifecycle.

    Scrum and Kanban keep `parallelism_config` unchanged -- `story_parallelism`
    and `isolation` are theirs, they are in supported scope, and removing them
    would be a capability regression for every project running several
    label-filtered boards.

    SPQ HAS NO SWITCH, and that is `C-07` / `SC-MTH-010`: concurrency is a
    CONSEQUENCE of declared path scope, not an enablement a project sets. The
    predecessor made it a switch twice over -- `story_parallelism` defaulted to
    False so a project that never edited its config got no concurrency at all,
    and `isolation: worktree` permitted file overlap BY DESIGN, so declared
    scopes were compared only under `shared`. Two Work Units addressing one
    path were both authorized, and the risk looked absent rather than
    unguarded.

    What survives for SPQ is `max_concurrent`, a resource ceiling. The
    distinction is worth stating because it is the one this function exists to
    keep: a ceiling limits how much runs at once and can never make colliding
    work legal, while a switch decided whether the collision was looked for.
    `advance_kernel._scope_collision` is what decides, and it consults the
    sealed declaration rather than anything here.
    """
    block = parallelism_config(project_dir)
    # `_project_build_mode` rather than a local read: it already mirrors
    # `advance_kernel.build_mode`'s two lookups and returns "" rather than
    # raising on an unreadable board, and one more copy of that logic is how
    # the two would drift.
    if _project_build_mode(project_dir) != "spq":
        return block
    return {
        "max_concurrent": block["max_concurrent"],
        # Named rather than merely absent, so a reader learns WHAT governs
        # concurrency instead of only that a key they expected is gone.
        "governed_by": "declared path scope (C-07)",
    }


def _config_dod_tier(project_dir: str | os.PathLike) -> str | None:
    """Read an explicit `quality.dod_tier` override from `.synaptory.yaml`."""
    in_quality = False
    for raw in _read_config_text(project_dir).splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line:
            continue
        indent = len(line) - len(line.lstrip())
        s = line.strip()
        if indent == 0:
            in_quality = s.startswith("quality:")
            continue
        if in_quality and indent == 2 and s.startswith("dod_tier:"):
            val = s.split(":", 1)[1].strip().strip('"').strip("'").lower()
            if val in DOD_TIER_CHECKS:
                return val
    return None


def phi_risk_signals(project_dir: str | os.PathLike) -> tuple[str, ...]:
    """Default PHI-risk signals plus any `healthcare.phi_paths` list entries.

    The config list is parsed as a YAML block sequence (`- value` lines) or a
    single inline value under the top-level `healthcare:` block.
    """
    extra: list[str] = []
    in_healthcare = False
    in_phi_paths = False
    for raw in _read_config_text(project_dir).splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line:
            continue
        indent = len(line) - len(line.lstrip())
        s = line.strip()
        if indent == 0:
            in_healthcare = s.startswith("healthcare:")
            in_phi_paths = False
            continue
        if not in_healthcare:
            continue
        if indent >= 2 and s.startswith("phi_paths:"):
            in_phi_paths = True
            inline = s.split(":", 1)[1].strip()
            if inline and not inline.startswith("["):
                extra.append(inline.strip('"').strip("'"))
            continue
        if in_phi_paths and s.startswith("- "):
            extra.append(s[2:].strip().strip('"').strip("'"))
            continue
        if indent >= 2 and not s.startswith("-"):
            in_phi_paths = False
    return DEFAULT_PHI_RISK_SIGNALS + tuple(e for e in extra if e)


def story_touches_phi(
    receipts: list[dict[str, Any]], signals: tuple[str, ...]
) -> bool:
    """True if any receipt references a PHI-risk signal (#31).

    Scans changed-file artifacts and the task/summary text (case-insensitive
    substring). This is the diff-signal trigger the issue asks for: PHI module
    paths, `logger.*`, `phi_access_log`/audit writes, FHIR clients, etc.
    """
    haystack: list[str] = []
    for r in receipts:
        for art in r.get("artifacts") or []:
            if isinstance(art, str):
                haystack.append(art.lower())
            elif isinstance(art, dict):
                haystack.append(json.dumps(art).lower())
        for key in ("task", "summary"):
            val = r.get(key)
            if isinstance(val, str):
                haystack.append(val.lower())
    blob = "\n".join(haystack)
    return any(sig.lower() in blob for sig in signals if sig)


#: The compliance obligations the compliance-engineer used to discharge by
#: being dispatched per story (#31). #487 makes them DECLARED CHECKS on the
#: gate instead (proposal 3.1, the `SP-WRK-019` shape): the gate names them
#: before the verifying stage runs, refuses the transition until each has
#: returned a result, and reads an absent, unfinished or errored result as a
#: failure rather than as a pass.
#:
#: One id today, and the tuple rather than its length is the point: the
#: promotion in `active_dod_checks` iterates this declaration instead of
#: naming a check, so a second compliance obligation joins by being added
#: here. Adding one is deliberately NOT done in this ticket, because a new
#: check id is also a new key in `receipt_validator.KNOWN_STORY_DOD_KEYS`, a
#: new entry in `receipt-schema/evidence-contract.json`, and a new column on
#: the control plane's Evidence gate — surfaces owned by other tickets in this
#: epic (#407 landed the ingest, #408 holds `conformance/contract.json` as a
#: single writer). The mechanism lands here; widening the set is a separate,
#: cross-surface change.
COMPLIANCE_CHECKS: tuple[str, ...] = ("no_critical_findings",)

#: Receipt abbreviation to full role name, for the gate messages that have to
#: name a stage a human can dispatch. Deliberately a local literal rather than
#: an import of `runtime_contracts.ROLE_NAMES`: every other cross-module reach
#: in this file is lazy for the same reason (a partial host install must not
#: break module load), and a block reason must never fail to render.
_ROLE_ABBREV_TO_NAME: dict[str, str] = {
    "se": "software-engineer",
    "qe": "quality-engineer",
    "cr": "code-reviewer",
    "ce": "compliance-engineer",
}


def active_dod_checks(
    intensity: str,
    *,
    compliance_required: bool = False,
    runtime_required: bool = False,
    ui_required: bool = False,
    integration_required: bool = False,
) -> list[str]:
    """The active DoD check ids for a story.

    Starts from the static intensity tier (`DOD_TIER_CHECKS`) and promotes the
    conditional gates: `no_critical_findings` (compliance-engineer, #31),
    `runtime_verified` (live deployment, #30), `ui_acceptance` (user-facing
    screen verified through the UI, #44), and `integration_verified` (external
    service proven live, #44 phase 5). Promotion is what makes those gates
    satisfiable — the orchestrator dispatches the matching agent/mode only when
    the gate is active, so the two stay in lockstep.

    #487 changes WHO satisfies the compliance half, not whether it is
    promoted. On SPQ the promotion no longer implies a compliance-engineer
    dispatch (there never was one on the Work Unit path); it declares the
    obligation on the gate, and `compliance_accountable_role` names the
    verifying stage that owes the result. Scrum and Kanban are unchanged.
    """
    checks = list(DOD_TIER_CHECKS.get(intensity, DOD_TIER_CHECKS["early"]))
    if compliance_required:
        # #487 — promote the DECLARED compliance set rather than one named
        # check. The behaviour is identical while `COMPLIANCE_CHECKS` holds a
        # single id; what changes is that the obligation now has one place to
        # be read from, which is what lets `next_action` state it to a
        # dispatch and what makes a second obligation a one-line addition
        # instead of a hunt through four call sites.
        for compliance_check in COMPLIANCE_CHECKS:
            if compliance_check not in checks:
                checks.append(compliance_check)
    if runtime_required and "runtime_verified" not in checks:
        checks.append("runtime_verified")
    if ui_required and "ui_acceptance" not in checks:
        checks.append("ui_acceptance")
    if integration_required and "integration_verified" not in checks:
        checks.append("integration_verified")
    return checks


#: The five canonical Evidence-gate checks, mirroring
#: api/synaptory_api/routers/analytics.py EVIDENCE_GATE_KEYS and
#: receipt_validator.KNOWN_STORY_DOD_KEYS. Only these are shipped as the
#: computed verdict — the conditional gates (compliance, runtime, ui,
#: integration) are pipeline-internal and have no column on /quality.
_EVIDENCE_GATE_KEYS = (
    "tests_pass", "build_succeeds", "no_critical_findings",
    "code_reviewed", "coverage_no_decrease",
)

# ─── Typed DoD check results (#403, capability-profile-pilot.md 3.3) ─────────

#: The three results a required check can land on. `criteria_gap_declared` is
#: its own state: never a pass, never a fail, and never folded into either.
#: Mirrors runtime_contracts.DOD_CHECK_RESULTS (imported lazily below, in the
#: `_host_env` idiom, so a partial install cannot break module load).
DOD_RESULT_PASS = "pass"
DOD_RESULT_FAIL = "fail"
CRITERIA_GAP_DECLARED = "criteria_gap_declared"

#: Payload key for the typed results this module SHIPS, and the key a receipt
#: may carry a SELF-REPORTED result under. Same key on both sides because the
#: control plane reads exactly one (`analytics.DOD_CHECK_RESULTS_KEY`, the
#: seam #407 named for this ticket) and mirrors it, not two.
DOD_CHECK_RESULTS_KEY = "dod_check_results"

#: Per check, the evidence shape whose ABSENCE makes the check unevaluable.
#: This is the text a `criteria_gap_declared` result carries, and the reason
#: the result exists: a gate that cannot be evaluated must say what is missing
#: so the orchestrator's retry prompt can teach the agent, instead of
#: resolving to a silent nothing that renders like a pass (the 13.3 shape).
#: Extends the self-explaining `detail` idiom #134 GAP-1 established for
#: tests_pass / build_succeeds to every check that can be required.
_MISSING_EVIDENCE_SHAPE: dict[str, str] = {
    "tests_pass": (
        "`verification_commands` contains no executed proof objects. "
        "Plain-string commands are replay instructions, not evidence — "
        'record each command you actually ran as {"command": "...", '
        '"exit_code": 0, "summary": "..."}.'
    ),
    "build_succeeds": (
        "`verification_commands` contains no executed proof objects. "
        "Plain-string commands are replay instructions, not evidence — "
        'record each command you actually ran as {"command": "...", '
        '"exit_code": 0, "summary": "..."}.'
    ),
    "no_critical_findings": (
        "no review receipt records a critical-findings count. A "
        "compliance-engineer or code-reviewer receipt must carry "
        "`metrics.findings_critical` as an explicit number; a security "
        "verdict cannot be inferred from its absence."
    ),
    "code_reviewed": (
        "no code-reviewer receipt exists for this story. A review is the "
        'CR\'s own act: it must carry top-level `"status": "complete"` (or '
        "`story_dod.code_reviewed: true`), and no other role's receipt can "
        "stand in for it."
    ),
    "coverage_no_decrease": (
        "no receipt records `metrics.coverage_delta`, so there is nothing to "
        "compare against the prior revision. Record the delta as a signed "
        'percentage (e.g. "+3.2%") or a number.'
    ),
    "runtime_verified": (
        "no receipt carries a structured `metrics.runtime_verification` "
        "object. A note saying the stack was checked is intent; the object "
        "recording {deployed, logs_inspected} is the evidence."
    ),
    "ui_acceptance": (
        "no receipt carries a structured `metrics.ui_verification` object. A "
        "green API or unit suite is not evidence a screen renders — QE "
        "browser-qa (or e2e) must record {rendered, routes_tested, "
        "flows_failed}."
    ),
    "integration_verified": (
        "no receipt declares an `integrations[]` entry with a backing "
        "executed smoke command, so the story's claim that an external "
        "service is wired has no live proof."
    ),
}


def criteria_gap_detail(check_id: str, *, source: str | None = None) -> str:
    """The missing-evidence description a `criteria_gap_declared` carries.

    Names the check, then the evidence shape that is absent, then whether any
    receipt was even found for it — the last distinction matters because "the
    receipt is the wrong shape" and "there is no receipt" need different
    fixes, and a reader who cannot tell them apart re-dispatches the wrong
    role.
    """
    shape = _MISSING_EVIDENCE_SHAPE.get(
        check_id, "no receipt carries evidence this check can be evaluated from."
    )
    where = (
        "a receipt was found but carries no evaluable evidence"
        if source
        else "no receipt was found that could source it"
    )
    return "%s is unverified: %s. Missing evidence: %s" % (check_id, where, shape)


# ─── the compliance obligation, declared rather than dispatched (#487) ───────


def compliance_accountable_role(build_mode: str | None) -> str:
    """The receipt abbreviation that OWES the declared compliance result.

    SPQ dispatches no compliance-engineer on the Work Unit path any more
    (#487), so the obligation lands on the stage that verifies the unit:
    `cr`, the prover that already owns the `reviewing -> done` edge, is
    already excluded from sourcing its own build evidence, and already
    records `metrics.findings_critical/high/medium/low` per its evidence
    obligations. `qe` is deliberately NOT the answer: `no_critical_findings`
    carries `fallback: review_roles_only` precisely so a security verdict
    comes from a review role, and widening that to the test-writing stage
    would be a weakening dressed as a re-assignment.

    Scrum and Kanban are out of scope for this pilot (proposal 7). They keep
    the compliance-engineer dispatch they have, so this returns `ce` for them
    and nothing about those two lifecycles changes.

    CE is NOT dissolved as a dispatch identity everywhere, and that is a
    finding rather than an omission: `spq_state_machine.ACCEPTANCE_ROLES`
    binds `ACCEPTANCE -> COMPLETE` to a real `ACCEPTANCE-{N}-ce.json`, so CE
    keeps dispatching at the SPQ Acceptance gate and in `modes/secure.md`.
    What this ticket removes is the per-Work-Unit dispatch nobody schedules.
    """
    return "cr" if str(build_mode or "").lower() == "spq" else "ce"


def compliance_declaration(
    *,
    baa_enforced: bool,
    required: bool,
    build_mode: str | None = None,
) -> dict[str, Any]:
    """The compliance check set declared on the gate, in dispatch-readable form.

    This is the surface the `SP-WRK-019` shape needs and the pipeline did not
    have: before #487, `no_critical_findings` was promoted at GATE time and
    announced to nobody at DISPATCH time. `next_action`'s `dod` block carried
    only the static tier list (`DOD_TIER_CHECKS`), and the SubagentStart
    envelope is rendered with neither `--active-checks` nor `--tier`, so the
    conditional compliance obligation reached no agent before it blocked one.
    A check a dispatch was never told about is a check the dispatch cannot
    have produced evidence for.

    `required` is the verdict computed from the receipts that exist SO FAR,
    so it can flip from false to true as a unit's diff grows. `provisional`
    says exactly that, and it is why a BAA project's dispatch is told about
    the obligation even while `required` is still false: the alternative is
    telling the verifying stage only once it is too late to have produced
    the evidence.
    """
    return {
        "checks": list(COMPLIANCE_CHECKS),
        "baa_enforced": bool(baa_enforced),
        "required": bool(required),
        # True when the project is BAA-enforced and the PHI signal has not
        # (yet) tripped. Not "false": a later receipt can still trip it.
        "provisional": bool(baa_enforced) and not bool(required),
        "accountable_role": compliance_accountable_role(build_mode),
        "evidence": {
            cid: _MISSING_EVIDENCE_SHAPE.get(
                cid, "no receipt carries evidence this check can be evaluated from."
            )
            for cid in COMPLIANCE_CHECKS
        },
        # Stated rather than implied: an absent result is not a pass. It is
        # this, and `dod_gate_block_reason` refuses the done edge on it.
        "absent_result": CRITERIA_GAP_DECLARED,
    }


def ship_evaluated_dod(
    story_id: str,
    dod_result: dict[str, Any] | None,
    *,
    project_dir: str | None = None,
) -> None:
    """Ship a computed DoD verdict to the CP (#199). Best-effort, never raises.

    Called wherever an evaluation is PERSISTED onto a story, which is what
    makes it authoritative; read-only evaluations (e.g. the pre-transition gate
    probe) deliberately do not emit. Without this the computed verdict lives
    only in local pipeline state and `/quality` is left scoring agent
    self-assessment, which in practice reports one of the five checks.

    #435 widens WHAT this ships, not when. The control plane stopped
    crediting `payload.dod_check_results` because a receipt payload is
    member-supplied and its subject writes it, which left this — the
    pipeline's own evaluation — as the only channel a verdict is credited
    from. Two facts the payload could carry and this could not therefore move
    here, or they would have been deleted along with the channel that carried
    them: the typed `result` (so `criteria_gap_declared` stays its own state,
    #407) and the DERIVED `evidence_class` (`backing_evidence_class`, never a
    class a payload named for itself, #432).
    """
    if not isinstance(dod_result, dict):
        return
    checks = dod_result.get("checks")
    if not isinstance(checks, dict):
        return
    try:
        from gate_emitter import emit_evidence_dod_evaluated

        entries = {
            k: (checks.get(k) or {}) for k in _EVIDENCE_GATE_KEYS if k in checks
        }
        emit_evidence_dod_evaluated(
            story_id,
            # The typed result where the evaluation produced one, else the
            # boolean. A gap has `passed is None`, so shipping `passed` alone
            # made a declared gap indistinguishable from a check the tier
            # never required.
            checks={
                k: (e["result"] if isinstance(e.get("result"), str) else e.get("passed"))
                for k, e in entries.items()
            },
            classes={
                k: e["evidence_class"]
                for k, e in entries.items()
                if isinstance(e.get("evidence_class"), str)
            },
            passed=bool(dod_result.get("passed")),
            tier=dod_result.get("intensity") or dod_result.get("verification_tier"),
            project_dir=project_dir,
        )
    except Exception:
        pass  # telemetry must never affect a DoD verdict


def dod_gate_block_reason(dod_result: dict[str, Any] | None) -> str | None:
    """Return a block reason when a story's CONDITIONAL gates are active but
    not satisfied, else None.

    The state machines call this on the `reviewing → done` transition: rather
    than let a story silently complete on green SE/QE/CR while a required
    higher-assurance gate was never satisfied, the story is redirected to
    `blocked` with this reason. Blocking is recoverable — dispatch the missing
    agent/mode (compliance-engineer, runtime verification, browser-qa) or get
    PO acceptance, then unblock.

    The blocking set is OPEN, not closed (#494 question 3). It began as four
    conditional gates, #403 added a fifth entry deliberately, and #494 adds a
    sixth. The rule that governs additions is the one all six satisfy: a
    condition blocks here when the gate can establish, from evidence the
    subject of the gate did not author, that a required check is not answered.
    What does NOT belong here is a check resting on the receipt's own claim
    about itself.

      - `no_critical_findings`  — PHI-touching healthcare story (#31)
      - `runtime_verified`      — live-deployment verification (#30)
      - `ui_acceptance`         — user-facing AC verified through the UI (#44)
      - `integration_verified`  — external service proven live (#44 phase 5)
      - any required check whose `result` is `criteria_gap_declared` (#403)
      - a static check that passed on attested evidence but failed DoD-time
        evidence replay (#179 E1): an exit code that does not reproduce is not
        proof
      - a required check that FAILED against the hash-sealed authored set
        (#494): the verdict is derived from a document its subject cannot edit

    The remaining static tier checks (build_succeeds, coverage, …) keep their
    existing handling, so this never changes behaviour for backend-only,
    non-healthcare, non-opt-in projects, nor for any project whose Cycle
    carries no sealed authored set. Routed to the producing role by
    `_gate_remediation`.
    """
    if not dod_result:
        return None
    checks = dod_result.get("checks", {})
    problems: list[str] = []
    # #403 — a required check that declared a criteria gap blocks. This was the
    # first STATIC-check addition to this gate, and it is deliberate: a gap is
    # not a fail, it is the absence of anything to evaluate, and the failure
    # mode it replaces was a required gate resolving to a silent nothing that
    # rendered indistinguishably from a pass (proposal 3.3, the 13.3 fix at
    # the contract level: thin gates must look thin).
    #
    # Blocking is recoverable and self-explaining: `detail` names the missing
    # evidence shape, and `_gate_remediation` routes the block to the role
    # that can produce it. A caller cannot use this to dodge a required check
    # either, because a gap never clears one: `passed` stays None, so the DoD
    # verdict is still not-passed, AND the done edge now stops.
    for cid, c in checks.items():
        if c.get("required") and c.get("result") == CRITERIA_GAP_DECLARED:
            problems.append(
                "%s declared a criteria gap: %s"
                % (cid, c.get("detail") or criteria_gap_detail(cid))
            )
    # #494 — a required check whose FAIL was derived from the hash-sealed
    # authored set blocks, exactly as a declared gap does.
    #
    # This exists because the gate had an INVERTED incentive, which is worse
    # than a hole. `criteria_gap_declared` blocks, so a prover that honestly
    # reports a case it could not evaluate is stopped; a `fail` did not block,
    # so a prover that silently omits the same case from
    # `metrics.authored_test_cases.results` was not. Dropping a case was
    # strictly cheaper than declaring a gap, and it needed no forgery at all,
    # only a missing key. A gate that pays better for silence than for honesty
    # is not measuring what it claims to measure.
    #
    # The condition is narrow on purpose, and the narrowness is what makes it
    # safe. It fires only where `_authoritative_story` resolved the authored
    # set from a manifest that passed `spq_manifest.verify_hash`, so the
    # verdict is derived from a document the subject of the gate cannot edit
    # without breaking the seal. That is why the standing rationale for
    # letting static checks through ("static checks are self-attested and are
    # covered by receipt replay instead", `HostPolicy.dod_requires_pass`)
    # does not reach it: the authored-case verdict is not self-attested, and
    # replay does not cover it either, because replay re-runs
    # `verification_commands` and a suite that never ran the dropped case
    # exits 0. Where there is no seal (`authored_source` `board` or absent:
    # Scrum, Kanban, every pre-#406 Cycle) nothing changes.
    #
    # `missing_manifest` (#507) deliberately does NOT reach this clause. A
    # deleted seal makes the authored set unknowable rather than disproven, so
    # it blocks through #403's criteria-gap route with a remedy about restoring
    # a document — which is a different instruction from "report every case",
    # and handing an operator the wrong one of the two is how a block becomes
    # a stall.
    #
    # A legitimately not-applicable case is NOT caught by this. It is settled
    # by `_case_result_is_settled` when it carries a reason, so the verdict is
    # True and there is no fail to block on. Turning every absence into a hard
    # failure would have broken honest flows, which is a different failure
    # rather than a safer one; what this closes is the case reported as
    # nothing at all.
    for cid, c in checks.items():
        if not c.get("required") or c.get("result") != DOD_RESULT_FAIL:
            continue
        if c.get("replay_mismatch"):
            continue  # already named, with a more specific remedy, below
        test_first = c.get("test_first")
        if not isinstance(test_first, dict):
            continue
        if test_first.get("authored_source") != "manifest":
            continue
        problems.append(
            "%s failed against the acceptance cases sealed in this Cycle's "
            "manifest, and a case the verifying receipt does not report is "
            "not cheaper than one it reports honestly — both stop the done "
            "edge. Re-dispatch the verifying stage with an outcome for every "
            "authored case, mark a case `not-applicable` WITH a reason, or "
            "supersede the authored set with `revise_manifest --reason`. %s"
            % (cid, c.get("detail") or "No detail was recorded.")
        )
    for cid, c in checks.items():
        if c.get("replay_mismatch"):
            problems.append(
                f"{cid} failed evidence replay — the receipt attests a passing "
                "exit code that does not reproduce; re-dispatch the producing "
                "agent so the evidence is real (or have the PO accept explicitly)"
            )
    if dod_result.get("compliance_required"):
        # #487 — the remedy names whoever OWES the declared result on this
        # lifecycle, not a dispatch identity that may no longer exist. On SPQ
        # the compliance-engineer is not dispatched per Work Unit, so telling
        # the orchestrator to dispatch it was an instruction with no
        # executable meaning: the unit blocked and the named recovery could
        # not be performed. Scrum and Kanban resolve to `ce` and read exactly
        # as they did.
        _decl = dod_result.get("compliance") or {}
        _owner = _decl.get("accountable_role") or "ce"
        _owner_name = _ROLE_ABBREV_TO_NAME.get(_owner, "compliance-engineer")
        for cid in dod_result.get("compliance_checks") or list(COMPLIANCE_CHECKS):
            c = checks.get(cid, {})
            if c.get("required") and c.get("passed") is not True:
                problems.append(
                    "the declared compliance check %s has not returned a "
                    "passing result — this PHI-touching story's compliance "
                    "evaluation is owed by the %s stage, which must record "
                    "metrics.findings_critical == 0 on its {story}-%s.json "
                    "receipt. An absent count is not a clean audit: it is "
                    "%s, and it blocks here."
                    % (cid, _owner_name, _owner, CRITERIA_GAP_DECLARED)
                )
    if dod_result.get("runtime_required"):
        c = checks.get("runtime_verified", {})
        if c.get("required") and c.get("passed") is not True:
            problems.append(
                "runtime verification (runtime_verified) has not passed — QE "
                "must deploy the stack, exercise the story, inspect live "
                "logs/URLs/audit, and record metrics.runtime_verification"
            )
    if dod_result.get("ui_required"):
        c = checks.get("ui_acceptance", {})
        if c.get("required") and c.get("passed") is not True:
            problems.append(
                "user-facing acceptance (ui_acceptance) has not passed — this "
                "story's AC describes a screen, so a passing API/unit suite is "
                "not enough. Dispatch QE in browser-qa mode (or run e2e) and "
                "record a {story}-qe.json receipt with metrics.ui_verification "
                "{rendered: true, flows_failed: 0}, or have the PO accept the "
                "AC explicitly at Sprint Review"
            )
    if dod_result.get("integration_required"):
        c = checks.get("integration_verified", {})
        if c.get("required") and c.get("passed") is not True:
            problems.append(
                "external integration (integration_verified) is claimed but not "
                "proven live — every service the story claims as wired/connected "
                "must carry an `integrations[].status: \"live\"` receipt entry "
                "backed by an executed smoke verification_command (exit_code 0) "
                "that reached the real service; a stubbed / placeholder-cred "
                "integration does not satisfy its AC. Run the live smoke and "
                "record it, or have the PO accept/defer the AC at Sprint Review"
            )
    if problems:
        return "DoD gate: " + "; ".join(problems)
    return None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STORY_STATES = [
    "queued",
    "in_progress",
    "testing",
    "reviewing",
    # Per #116: when sprint.review.per_story_acceptance=true, stories enter
    # `awaiting_acceptance` after CR completes and wait for a PO Accept /
    # Reject decision at Sprint Review. The state is unused (and not entered)
    # when the toggle is false — back-compat default keeps today's
    # `reviewing → done` direct promotion.
    "awaiting_acceptance",
    "done",
    # First-class terminal state for stories the PO cancels at Sprint Review.
    # Excluded from velocity and DoD aggregation.
    "cancelled",
    "blocked",
]

# Rejection reason classes (#116). Each maps to a different recovery target
# via `reject_story`. See the table in the issue for semantics.
REJECTION_REASONS: tuple[str, ...] = ("needs-fix", "redo", "defer", "cancel")

# Default retry cap for the H3-F1 recovery ladder. The Orchestrator re-dispatches
# up to this many times per role before marking the story blocked. Overridable
# via .synaptory.yaml → resilience.story_retry_cap.
DEFAULT_RETRY_CAP = 2

# Recovery ladder tiers. Returned by recommend_recovery_action() so the
# Orchestrator can build an augmented prompt or stop retrying.
RETRY_TIER_SAME_PROMPT = "retry_same_prompt"
RETRY_TIER_AUGMENTED_PROMPT = "retry_augmented_prompt"
RETRY_TIER_BLOCK = "block"

VALID_TRANSITIONS: dict[str, list[str]] = {
    "queued": ["in_progress"],
    "in_progress": ["testing", "blocked"],
    "testing": ["reviewing", "blocked"],
    # Per #116: `reviewing → done` stays legal for back-compat when the
    # `per_story_acceptance` toggle is false. With the toggle on, the
    # orchestrator drives `reviewing → awaiting_acceptance` instead and
    # the PO walks `awaiting_acceptance → done` at Sprint Review.
    "reviewing": ["awaiting_acceptance", "done", "blocked"],
    # PO acceptance fork — Accept goes to `done`, the four reject classes
    # land back in the pipeline at the right stage (see reject_story).
    "awaiting_acceptance": [
        "done", "in_progress", "queued", "cancelled", "blocked",
    ],
    "blocked": [
        "queued", "in_progress", "testing", "reviewing",
        "awaiting_acceptance",
    ],
    # Terminal states have no outgoing transitions.
    "done": [],
    "cancelled": [],
}

# Per-story DoD checks. Each check has an id, label, category, and optional agent.
#
# #134 (GAP-10 generalization of #144): `preferred_roles` + `fallback` make
# evidence-based sourcing explicit per check. The first preferred role is the
# nominal receipt slot (unchanged primary behaviour); when it is missing or
# unresolved, `fallback` says which other receipts may satisfy the check:
#   "any_with_proof"    — any same-story receipt carrying the check's
#                         STRUCTURED proof (strict matcher, see
#                         _evaluate_fallback_proof) may source it.
#   "review_roles_only" — only the listed review roles (a security verdict
#                         can't come from the builder's receipt).
#   "none"              — role-bound act (a review IS the CR's act; letting
#                         SE self-attest would gut evidence-over-assertion).
# These values are mirrored in receipt-schema/evidence-contract.json; the
# test_evidence_contract drift guard keeps the two in sync.
DOD_CHECKS: dict[str, dict[str, Any]] = {
    "tests_pass": {
        "label": "All acceptance criteria tests passing",
        "category": "critical",
        "receipt_role": "qe",
        "receipt_field": "verification_commands",
        "preferred_roles": ["qe", "se"],
        "fallback": "any_with_proof",
    },
    "build_succeeds": {
        # #44 — this attests the code COMPILES and the dev server BOOTS. It is
        # deliberately NOT "the feature is delivered" / "the AC renders": a
        # user-facing AC is judged by the `ui_acceptance` gate, not this one.
        "label": "Build compiles and dev server boots (not proof a feature renders)",
        "category": "critical",
        "receipt_role": "se",
        "receipt_field": "verification_commands",
        "preferred_roles": ["se", "pe"],
        "fallback": "any_with_proof",
    },
    "no_critical_findings": {
        "label": "Zero critical security findings",
        "category": "critical",
        "receipt_role": "ce",
        "receipt_field": "metrics",
        "preferred_roles": ["ce", "cr"],
        "fallback": "review_roles_only",
    },
    "code_reviewed": {
        "label": "Story code reviewed",
        "category": "adaptive",
        "receipt_role": "cr",
        "receipt_field": "status",
        "preferred_roles": ["cr"],
        "fallback": "none",
    },
    "coverage_no_decrease": {
        "label": "Test coverage did not decrease",
        "category": "adaptive",
        "receipt_role": "qe",
        "receipt_field": "metrics",
        "preferred_roles": ["qe", "se"],
        "fallback": "any_with_proof",
    },
    # #30: live-deployment + end-to-end + log/URL/audit inspection. Static
    # tests cannot see PHI-in-logs, PHI-in-URLs, broken workflows, or
    # empty-data states — only running the app does. Satisfied by a QE
    # receipt carrying a structured `metrics.runtime_verification` proof.
    "runtime_verified": {
        "label": "Deployed live and exercised end-to-end (logs/URLs/audit inspected)",
        "category": "critical",
        "receipt_role": "qe",
        "receipt_field": "metrics",
        "preferred_roles": ["qe"],
        "fallback": "any_with_proof",
    },
    # #44: a story whose AC describes a user-facing screen must be verified
    # THROUGH the UI — a green API/unit suite is not evidence the screen
    # renders. Satisfied by a QE receipt carrying a structured
    # `metrics.ui_verification` proof (browser-qa / e2e), or accepted
    # explicitly by the PO at Sprint Review.
    "ui_acceptance": {
        "label": "User-facing AC verified through the UI (browser-qa / e2e)",
        "category": "critical",
        "receipt_role": "qe",
        "receipt_field": "metrics",
        "preferred_roles": ["qe"],
        "fallback": "any_with_proof",
    },
    # #44 phase 5: a story that CLAIMS an external service is wired/connected
    # must prove it live. Satisfied only when every declared `integrations[]`
    # entry is `status: "live"` with a backing executed smoke verification
    # command (exit_code 0). Evaluated across ALL story receipts (the array can
    # live on the SE or QE receipt), so its `receipt_role` is nominal — see the
    # `integration_verified` branch in `evaluate_story_dod`.
    "integration_verified": {
        "label": "External integration proven live (smoke check reached the real service)",
        "category": "critical",
        "receipt_role": "se",
        "receipt_field": "integrations",
        "preferred_roles": ["se", "qe"],
        "fallback": "any_with_proof",
    },
}

# Checks active at each intensity tier. `no_critical_findings` and
# `runtime_verified` are NOT in the static tier lists: per #107 the per-story
# SE→QE→CR pipeline historically never invoked the compliance-engineer, so
# requiring either here would make the gate structurally unsatisfiable.
#
# #31 / #30 close that gap *conditionally*: `active_dod_checks()` promotes
# `no_critical_findings` (compliance-engineer must run per-story) and
# `runtime_verified` (QE must deploy + exercise the story) into the active
# set when a healthcare project (`healthcare.baa_enforced: true`) ships a
# PHI-touching story — or when `quality.runtime_verification: required` is
# set. For those stories Scrum and Kanban dispatch CE per-story so the gate
# is satisfiable; for every other project the behaviour is unchanged.
#
# #487: SPQ never had that per-Work-Unit CE dispatch. No SPQ ceremony file
# describes one, so the promotion produced a gate whose only documented
# remedy pointed at `modes/sprint.md`, which is Scrum's answer. The
# compliance obligation is therefore DECLARED on the SPQ gate and owed by
# the verifying stage (`compliance_accountable_role`), which is what makes
# it satisfiable on that lifecycle rather than a silent dead end.
DOD_TIER_CHECKS: dict[str, list[str]] = {
    "early": ["tests_pass", "build_succeeds"],
    "growing": ["tests_pass", "build_succeeds", "code_reviewed"],
    "mature": ["tests_pass", "build_succeeds", "code_reviewed", "coverage_no_decrease"],
    "release": ["tests_pass", "build_succeeds", "code_reviewed", "coverage_no_decrease"],
}

# Default PHI-risk signals (#31). A story whose receipts reference any of
# these (changed-file paths, verification commands, summary) is treated as
# PHI-sensitive. Generic on purpose — projects extend the list with their own
# module paths via `.synaptory.yaml: healthcare.phi_paths: [...]`. Matching is
# case-insensitive substring against receipt artifacts + task/summary text.
DEFAULT_PHI_RISK_SIGNALS: tuple[str, ...] = (
    "logger",
    "logging",
    "audit",
    "phi",
    "phi_access_log",
    "fhir",
    "patient",
    "mrn",
    "ehr",
    "medical",
    "clinical",
)


# Story sub-state → tracker workflow stage mapping.
# The tracker CLI accepts these status strings across all backends
# (local, GitHub, Jira, Teamwork, Linear).
TRACKER_STATUS_MAP: dict[str, str] = {
    "queued": "TO_DO",
    "in_progress": "IN_PROGRESS",
    "testing": "IN_PROGRESS",
    "reviewing": "IN_REVIEW",
    # #116: maps to AWAITING_ACCEPTANCE on trackers that have that column;
    # adapters fall back to IN_REVIEW + an `awaiting-acceptance` tag when
    # the workflow doesn't define one (matches the existing IN_REVIEW
    # fallback pattern in teamwork_adapter.update_story_status).
    "awaiting_acceptance": "AWAITING_ACCEPTANCE",
    "done": "DONE",
    # PO cancel → tracker column is DONE (work is off the board) plus
    # the `cancelled` tag so the audit trail records that it wasn't
    # accepted. Velocity / DoD aggregation excludes cancelled stories.
    "cancelled": "DONE",
    "blocked": "IN_PROGRESS",  # blocked is a label, not a workflow stage
}


# ---------------------------------------------------------------------------
# Tracker Sync
# ---------------------------------------------------------------------------

# The shared scripts directory sits at a different place in every layout, and a
# wrong guess here fails SILENTLY: sync_tracker_status just returns, so pipeline
# state advances while the tracker never hears about it.
#
#   core source tree   <core>/scripts/tracker/                 (after the core/ extraction)
#   Claude / Cursor    <pkg>/skills/_shared/scripts/tracker/
#   Codex package      <pkg>/runtime/scripts/tracker/          (_copy_shared_runtime renames it)
#
# Probe every layout rather than assume one. The Codex arrangements were both
# broken before this: the composed package has never matched the hard-coded
# skills/_shared path, so Codex advances silently skipped tracker sync.
_TRACKER_RELPATHS = (
    ("scripts", "tracker", "tracker_cli.py"),
    ("skills", "_shared", "scripts", "tracker", "tracker_cli.py"),
    ("runtime", "scripts", "tracker", "tracker_cli.py"),
)


def _resolve_tracker_cli() -> str | None:
    """Return the tracker CLI path for whichever layout is installed, or None."""
    roots: list[str] = []

    # Via host_env so core/ never names a host-specific variable itself (#285).
    configured = _host_env().plugin_root().strip()
    if configured:
        roots.append(configured)

    # Relative to this module: <core>/lib/ -> <core>/, and <pkg>/hooks/lib/ -> <pkg>/.
    this_dir = os.path.dirname(os.path.abspath(__file__))
    roots.append(os.path.dirname(this_dir))
    roots.append(os.path.dirname(os.path.dirname(this_dir)))

    for root in roots:
        for parts in _TRACKER_RELPATHS:
            candidate = os.path.normpath(os.path.join(root, *parts))
            if os.path.exists(candidate):
                return candidate
    return None


def sync_tracker_status(
    project_dir: str,
    story_id: str,
    to_state: str,
) -> None:
    """Sync story sub-state to the tracker's workflow stage.

    Calls tracker_cli.py update-status to keep the external tracker
    (local/GitHub/Jira/Teamwork) in sync with the pipeline sub-state.

    Failures are logged but do not block the pipeline — tracker sync
    is best-effort. The pipeline state in pipeline-state.json is the
    source of truth.
    """
    tracker_status = TRACKER_STATUS_MAP.get(to_state)
    if not tracker_status:
        return

    tracker_cli = _resolve_tracker_cli()
    if not tracker_cli:
        return  # Tracker CLI not available — skip silently.

    try:
        result = subprocess.run(
            [sys.executable, tracker_cli, "--project-dir", project_dir,
             "update-status", story_id, tracker_status, "--allow-skip"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            # Best-effort but no longer silent (#115): if the tracker rejects
            # the transition (e.g. the #111 audit-gate guard, or an offline
            # remote), surface a single-line warning so the agent transcript
            # shows the drift instead of board state silently lagging.
            err = (result.stderr or result.stdout or "").strip().splitlines()
            tail = err[-1] if err else f"exit {result.returncode}"
            print(
                f"synaptory: tracker sync drift — {story_id} → {tracker_status} "
                f"failed ({tail}); pipeline state is canonical, tracker may lag.",
                file=sys.stderr,
            )
    except (subprocess.TimeoutExpired, OSError) as e:
        # Best-effort — don't block pipeline on tracker sync failure. But do
        # log so the drift is visible.
        print(
            f"synaptory: tracker sync drift — {story_id} → {tracker_status} "
            f"raised {type(e).__name__}; pipeline state is canonical.",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Story Creation
# ---------------------------------------------------------------------------

# User-facing AC verbs (#44). A story whose title or acceptance criteria
# mention any of these describes a screen the user actually sees — so it
# cannot be considered `done` on API/unit exit codes alone; the UI must be
# exercised (browser-qa / e2e) or the AC explicitly accepted by the PO.
# Matched as whole words, case-insensitive, so "view" does not fire on
# "review" and "form" does not fire on "perform".
UI_AC_VERBS: tuple[str, ...] = (
    "view", "views", "viewing",
    "open", "opens", "opening",
    "render", "renders", "rendered", "rendering",
    "display", "displays", "displayed",
    "screen", "screens", "page", "pages",
    "dashboard", "console", "ui", "interface",
    "click", "clicks", "button", "buttons",
    "form", "forms", "modal", "dialog",
    "navigate", "navigates", "navigation",
    "layout", "responsive", "frontend",
)


def _normalize_ac_texts(acceptance_criteria: Any) -> list[str]:
    """Coerce acceptance criteria into a flat list of strings.

    Accepts the two shapes seen in the field: a plain list of strings, or the
    tracker's list of `{id, text, given, when, then, met}` dicts (see
    tracker/base.py). Given/When/Then are folded into the text so UI verbs in
    any clause are detected.
    """
    out: list[str] = []
    for ac in acceptance_criteria or []:
        if isinstance(ac, str):
            if ac.strip():
                out.append(ac.strip())
        elif isinstance(ac, dict):
            parts = [str(ac.get(k, "")) for k in ("text", "given", "when", "then")]
            joined = " ".join(p for p in parts if p and p.strip())
            if joined.strip():
                out.append(joined.strip())
    return out


def story_is_ui_bearing(title: str = "", acceptance_criteria: Any = None) -> bool:
    """True when the story's title/ACs describe a user-facing screen (#44).

    Whole-word, case-insensitive match against `UI_AC_VERBS`. Used to promote
    the `ui_acceptance` DoD gate so a UI-implying story cannot reach `done`
    on backend exit codes alone.
    """
    texts = [title or ""] + _normalize_ac_texts(acceptance_criteria)
    blob = " ".join(texts).lower()
    if not blob.strip():
        return False
    return any(re.search(rf"\b{re.escape(v)}\b", blob) for v in UI_AC_VERBS)


# #44 phase 5 — verbs that mark an acceptance criterion as *claiming* an
# external service is wired/connected/integrated. Deliberately limited to the
# claim VERBS named in the receipt-protocol "Integration claims" section — not
# the service nouns (sso/oauth/webhook) or the bare noun "integration", which
# fire on perfectly ordinary backend work ("add a webhook endpoint", "add
# integration tests") and would false-block it. A match promotes the
# fail-closed `integration_verified` gate — recoverable, like the UI/PHI gates
# — so a story can't report an integration as delivered while the receipt only
# shows a stub against placeholder credentials.
INTEGRATION_CLAIM_TERMS: tuple[str, ...] = (
    "wired", "wire up", "wiring",
    "connect", "connects", "connected", "connecting",
    "integrate", "integrates", "integrated", "integrating",
)


def _text_claims_integration(texts: list[Any]) -> bool:
    """Whole-word, case-insensitive `INTEGRATION_CLAIM_TERMS` match over texts."""
    blob = " ".join(t for t in texts if isinstance(t, str) and t.strip()).lower()
    if not blob.strip():
        return False
    return any(re.search(rf"\b{re.escape(t)}\b", blob) for t in INTEGRATION_CLAIM_TERMS)


def story_claims_integration(
    receipts: list[dict[str, Any]] | None = None,
    title: str = "",
    acceptance_criteria: Any = None,
) -> bool:
    """True when the story claims an external integration is wired (#44 phase 5).

    Mirrors the receipt-protocol "Integration claims" contract: an AC that says
    a third-party service is wired/connected/integrated is NOT met by code that
    merely compiles against placeholder credentials.

    Two signals, deliberately asymmetric (#235):

    - The story's own specification (title + ACs) promotes the gate on its own.
      That is the contract this check exists for, and the spec is where an
      integration is actually promised.
    - A receipt's free-text `task`/`summary` promotes it only when that same
      receipt also declares a non-empty ``integrations[]`` array.

    Requiredness is still driven by the *claim* rather than by the array alone,
    so an honest, un-claimed stub ("scaffold Entra for next sprint") is never
    force-blocked.
    """
    # The spec is authoritative and needs no corroboration.
    if _text_claims_integration([title] + _normalize_ac_texts(acceptance_criteria)):
        return True

    # Receipt prose alone used to be enough, and that made the gate fire on
    # ordinary English for purely internal work: "wired the helper into
    # TaskStore.list", "connected the reducer to the store", "integrated the
    # new step into the pipeline" — none of which involve an external service.
    #
    # That was not merely noisy, it was unrecoverable. evaluate_integration_
    # claims returns None when no integrations[] entries are declared, a
    # required check that is not True blocks, and `integration_verified` is
    # deliberately excluded from _WAIVABLE_DOD_CHECKS. So a prose match with no
    # array could only ever block and never pass, leaving two escapes: a PO
    # acceptance, or deleting the offending word from the receipt. The second
    # trains agents to write vaguer receipts in order to dodge a gate, which is
    # the opposite of what receipts are for.
    #
    # Requiring a declaration as corroboration keeps the case the gate was built
    # for — a story that claims an integration AND declares one must prove it
    # live instead of shipping a stub against placeholder credentials — while a
    # deliberate `integrations: []` ("none here") no longer traps the story.
    #
    # Corroboration is looked for across the whole receipt SET, not per-receipt.
    # Splitting the two across roles is normal and legitimate: the SE writes
    # "wire up Entra SSO" in its summary and the QE receipt carries the
    # integrations[] entry with the live smoke that proves it. Pairing them on a
    # single receipt would fail that story closed for a bookkeeping reason.
    receipts = receipts or []
    declared_anywhere = any(
        isinstance(r.get("integrations"), list) and r.get("integrations")
        for r in receipts
    )
    if not declared_anywhere:
        return False
    return any(
        _text_claims_integration([r.get("task"), r.get("summary")]) for r in receipts
    )


def evaluate_integration_claims(
    receipts: list[dict[str, Any]] | None,
) -> bool | None:
    """Evaluate the `integration_verified` gate across a story's receipts (#44).

    Fail-closed, mirroring `runtime_verified`:

      - No `integrations[]` entry on any receipt → ``None`` (claimed but never
        declared — unverified, so the gate cannot pass).
      - Any declared integration with ``status != "live"`` (stubbed / placeholder
        creds / unknown) → ``False``: a stub does not satisfy a "wired" AC.
      - A ``status == "live"`` entry whose owning receipt carries no executed
        smoke check (a `verification_commands` object with ``exit_code == 0``)
        → ``False``: "live" requires a passing smoke that reached the real service.
      - Every declared integration is ``live`` AND backed by a passing smoke on
        its own receipt → ``True``.
    """
    declared = 0
    for r in receipts or []:
        entries = r.get("integrations")
        if not isinstance(entries, list):
            continue
        has_passing_smoke = any(
            isinstance(vc, dict) and vc.get("exit_code") == 0
            for vc in (r.get("verification_commands") or [])
        )
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            declared += 1
            if str(entry.get("status", "")).strip().lower() != "live":
                return False  # stubbed / unknown → AC not met
            if not has_passing_smoke:
                return False  # "live" with no executed smoke → false claim
    if declared == 0:
        return None  # claimed in AC but no honest integrations[] declaration
    return True


def create_story(
    story_id: str,
    title: str = "",
    backends: dict[str, str] | None = None,
    acceptance_criteria: list[Any] | None = None,
    kind: str = "",
    labels: list[str] | None = None,
    depends_on: list[str] | None = None,
    file_scope: list[str] | None = None,
    acceptance_cases: list[Any] | None = None,
    criteria_gap_declared: dict[str, Any] | None = None,
    project_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Create a new story record in 'queued' state.

    Args:
        story_id: Unique identifier (e.g., "US-042", "TICKET-007").
        title: Story title.
        backends: Deprecated — accepted for API compatibility but ignored.
                  In plugin-claude every role runs on Claude.
        acceptance_criteria: The story's ACs (list of strings, or tracker
                  `{text,given,when,then}` dicts). Snapshotted at intake so the
                  DoD gate can tell UI-bearing stories from backend-only ones
                  without re-querying the tracker (#44).
        kind: PO-assigned story kind (#134 GAP-4): one of
                  enabler|infra|backend|api|ui|mixed (free-form tolerated).
                  Kinds in `_NON_UI_KINDS` suppress the ui_bearing keyword
                  heuristic; kinds also key `quality.verification.by_kind`
                  tier overrides.
        labels: Tracker labels snapshotted at intake (informational).
        depends_on: Story ids that must be terminal (`done`) before this
                  story may join a parallel dispatch batch (#134 GAP-6).
        file_scope: Declared glob/dir scopes this story touches; required
                  for shared-workspace parallelism (#134 GAP-8).
        acceptance_cases: The acceptance cases AUTHORED AT COMMIT (#406), from
                  the sealed Cycle manifest. These are the producer's target
                  and the prover's executable set, and the fact they were
                  declared before any producing dispatch is what satisfies
                  `SP-WRK-007` by construction rather than by review.
        criteria_gap_declared: Recorded when this unit was admitted WITHOUT an
                  authored set (`{reason, declared, declared_by, declared_at}`
                  from `spq_state_machine.admit_authored_cases`). Present on
                  the story so the absence travels with the work instead of
                  living only in a Cycle-level summary nobody reads.
        project_dir: The project this story belongs to. REQUIRED FOR THE EVENT
                  TO LAND IN THE PROJECT: `synaptory_logger.emit` falls back to
                  `host_env.project_dir()`, which falls back to `os.getcwd()`,
                  so omitting it writes `story_create` and the shared
                  `chain-id` into whatever directory the process happened to be
                  in rather than into the project it was handed (#380). Every
                  other `_log_emit` in this module already passes it; this one
                  and `_cli_receipt_gated_refusal` did not, which is why a
                  `./synaptory test` run grew a `.synaptory/.orchestrator/` in
                  the checkout under test -- invisible, because `.synaptory/*`
                  is gitignored.

                  When it is None the event is DROPPED rather than addressed
                  to the cwd -- see `_emit_project_event`, which is where that
                  decision is made once for every event this module logs.

    Returns:
        Story record dict ready to insert into current_stories.
    """
    _ = backends  # retained for API compat; plugin-claude is Claude-only
    now = _now()
    _emit_project_event(
        "story_create", project_dir=project_dir, story_id=story_id, title=title
    )
    ac_texts = _normalize_ac_texts(acceptance_criteria)
    kind_norm = (kind or "").strip().lower()
    return {
        "id": story_id,
        "title": title,
        "state": "queued",
        "blocked_reason": None,
        "blocked_from": None,
        "backend": {},
        "pipeline_log": [
            {"state": "queued", "entered_at": now, "exited_at": None},
        ],
        "dod": None,
        "receipts": [],
        # #44: AC snapshot (normalized to strings) captured at intake, plus
        # whether any AC describes a user-facing screen. `ui_bearing` promotes
        # the `ui_acceptance` gate in evaluate_story_dod so the story can't
        # reach `done` without browser/e2e proof (or explicit PO acceptance).
        # #134 GAP-4: a PO-declared non-UI kind beats the keyword heuristic.
        "acceptance_criteria": ac_texts,
        # #406 — the authored set and, where there is none, the recorded gap.
        # Both are always present as keys (list / None) so a reader never has
        # to distinguish "no cases" from "this record predates the field": the
        # board is rebuilt from the manifest on every projection, so the
        # manifest is the only place where key absence carries meaning.
        "acceptance_cases": _normalize_authored_cases(acceptance_cases),
        "criteria_gap_declared": (
            dict(criteria_gap_declared)
            if isinstance(criteria_gap_declared, dict) and criteria_gap_declared
            else None
        ),
        "kind": kind_norm,
        "labels": list(labels or []),
        "depends_on": list(depends_on or []),
        "file_scope": list(file_scope or []),
        "ui_bearing": (
            False if kind_norm in _NON_UI_KINDS
            else story_is_ui_bearing(title, acceptance_criteria)
        ),
        # Per-role retry counter for the H3-F1 recovery ladder:
        #   tier 1 (retry #1): re-dispatch with same prompt
        #   tier 2 (retry #2): re-dispatch with augmented prompt citing the failure
        #   tier 3 (retry #3+): transition to blocked with reason
        "retries": {},
        # PO rejection feedback (#116) — populated by `reject_story`. Read by
        # the next SE dispatch so the agent knows why the prior cut was
        # rejected. List so multiple rejections across re-work cycles are
        # preserved in order.
        "rejection_feedback": [],
    }


# ---------------------------------------------------------------------------
# State Transitions
# ---------------------------------------------------------------------------

def transition_story(
    state: dict[str, Any],
    story_id: str,
    to_state: str,
    reason: str | None = None,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Transition a story to a new sub-state.

    Args:
        state: Full pipeline state dict (caller owns I/O).
        story_id: Story to transition.
        to_state: Target state.
        reason: Required when to_state is "blocked".
        project_dir: If provided, syncs the transition to the tracker backend.

    Returns:
        Updated state dict.

    Raises:
        ValueError: On illegal transition, missing story, or missing reason.
    """
    story = _find_story(state, story_id)
    if story is None:
        raise ValueError(f"Story not found: {story_id}")

    from_state = story["state"]

    if to_state not in STORY_STATES:
        raise ValueError(f"Invalid state: {to_state}. Must be one of {STORY_STATES}")

    if to_state not in VALID_TRANSITIONS.get(from_state, []):
        raise ValueError(
            f"Illegal transition: {from_state} → {to_state} for {story_id}. "
            f"Valid targets: {VALID_TRANSITIONS.get(from_state, [])}"
        )

    if to_state == "blocked" and not reason:
        raise ValueError("Reason is required when transitioning to 'blocked'")

    now = _now()

    # Close current pipeline_log entry.
    if story["pipeline_log"]:
        story["pipeline_log"][-1]["exited_at"] = now

    # Record blocked metadata.
    if to_state == "blocked":
        story["blocked_reason"] = reason
        story["blocked_from"] = from_state
    else:
        story["blocked_reason"] = None
        story["blocked_from"] = None

    story["state"] = to_state
    story["pipeline_log"].append(
        {"state": to_state, "entered_at": now, "exited_at": None}
    )

    _emit_project_event(
        "transition",
        project_dir=project_dir,
        story_id=story_id,
        from_state=from_state,
        to_state=to_state,
        reason=reason,
    )

    # Sync to tracker backend (best-effort).
    if project_dir:
        sync_tracker_status(project_dir, story_id, to_state)

    return state


def unblock_story(
    state: dict[str, Any],
    story_id: str,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Unblock a story, restoring it to its blocked_from state.

    Args:
        state: Full pipeline state dict.
        story_id: Story to unblock.
        project_dir: If provided, syncs the transition to the tracker backend.

    Returns:
        Updated state dict.

    Raises:
        ValueError: If story is not in 'blocked' state.
    """
    story = _find_story(state, story_id)
    if story is None:
        raise ValueError(f"Story not found: {story_id}")

    if story["state"] != "blocked":
        raise ValueError(f"Story {story_id} is not blocked (state: {story['state']})")

    restore_to = story.get("blocked_from") or "queued"
    now = _now()

    if story["pipeline_log"]:
        story["pipeline_log"][-1]["exited_at"] = now

    story["state"] = restore_to
    story["blocked_reason"] = None
    story["blocked_from"] = None
    story["pipeline_log"].append(
        {"state": restore_to, "entered_at": now, "exited_at": None}
    )

    _emit_project_event(
        "unblock",
        project_dir=project_dir,
        story_id=story_id,
        restored_to=restore_to,
    )

    # Sync to tracker backend (best-effort).
    if project_dir:
        sync_tracker_status(project_dir, story_id, restore_to)

    return state


# ---------------------------------------------------------------------------
# PO Acceptance (#116) — per-story Sprint Review gate
# ---------------------------------------------------------------------------

# Each rejection reason maps to a recovery state in the FSM. See the
# table in #116 for the semantics.
_REJECTION_NEXT_STATE: dict[str, str] = {
    "needs-fix": "in_progress",   # works but needs incremental fix
    "redo": "queued",             # approach wrong, redesign
    "defer": "queued",            # punt to next sprint (caller carries over)
    "cancel": "cancelled",        # kill the story
}


#: Where a story keeps the human verdicts recorded against it. Append-only:
#: a recorded verdict is never rewritten, which is what "immutable once
#: recorded" means in a state file nothing else guards (#403).
JUDGED_VERDICTS_KEY = "judged_verdicts"

#: Receipt fields that name a HUMAN principal. `agent` / `role` are not here
#: on purpose: they name an agent role, and comparing a human UPN against a
#: role name would always differ and so would always "prove" independence.
_PRODUCER_PRINCIPAL_KEYS = ("principal", "produced_by", "dispatched_by")


#: What a judged verdict's candidate identity was derived FROM. On the item so
#: no reader can mistake a verdict bound to an artifact the platform did not
#: produce for one bound to the receipts it did (#495).
CANDIDATE_SOURCE_INTAKE = "external_intake"
CANDIDATE_SOURCE_LEDGER = "consumed_receipt_ledger"
CANDIDATE_SOURCE_RECEIPT_BYTES = "receipt_bytes"

#: "the caller did not pre-read the intake", distinct from `None`, which means
#: "consulted, and there is no intake path to consult". Two facts, two
#: representations (#521's rule).
_UNREAD = object()


def _read_external_intake(project_dir: str | None, story_id: str):
    """`external_intake.read_intake`, or None when the module is unavailable.

    Imported lazily because `external_intake` resolves the receipts directory
    through this module, so a module-level import either way is a cycle. None
    means "the intake path could not be consulted at all", which the caller
    treats as "no admission" -- the pre-#495 behaviour, unchanged.
    """
    if not project_dir:
        return None
    try:
        import external_intake
    except ImportError:  # pragma: no cover - composed trees always carry it
        return None
    try:
        return external_intake.read_intake(str(project_dir), story_id)
    except Exception as exc:  # noqa: BLE001 - a verdict must not raise here
        # FAILS CLOSED. If the intake path could not be consulted we do not
        # know whether an admission exists, and returning None here would fall
        # through to the receipt derivations and credit a verdict that might
        # have had an external subject all along.
        return external_intake.IntakeRead(
            admitted=True,
            problems=[
                "the intake record for this unit could not be consulted (%s), "
                "so whether an external candidate was admitted is unknown" % exc
            ],
        )


def _unauthorized_verifier(
    story: dict[str, Any],
    story_id: str,
    project_dir: str | None,
    intake: Any,
) -> str | None:
    """Why the verifying receipt is not proof a verifying stage RAN, or None.

    #495 P2. Everything before this establishes that a valid receipt declaring
    a verifying role names the admitted bytes. That is matching by SHAPE, and a
    document written by hand has the same shape as one a dispatched verifier
    wrote -- which is what #553's review answered: epic #339 built the identity
    that settles it (`attempt_id`, the durable fenced form of `dispatch_id`),
    so the vocabulary exists and this path simply did not use it.

    HERE RATHER THAN IN `external_intake`, because "was this stage authorized"
    is a question about the Work Unit's dispatch history, which lives on the
    story. That module names the receipts (`verified_by`) and this one holds
    the story, so neither reaches into the other's data.

    `advance_kernel.attempt_history` is the SUPPORTED reader for the attempts a
    kernel authorized, so this does not re-derive the state key. A receipt
    matches when it names an attempt the kernel issued for that stage, or the
    dispatch currently active for it; `attempt_id` is preferred because it
    survives the active binding being replaced.

    WHY THIS IS NOT THE PIPELINE-WIDE RULE. On an ordinary story the Claude
    orchestrator calls `begin_dispatch` only for `dispatch_se`, so QE and CR
    receipts legitimately carry no dispatch identity, and `advance_kernel`
    deliberately allows an unbound receipt there rather than refusing the
    normal pipeline (#592). This does not change that. A verify-only Work Unit
    has no producing stage, its verifying stage is the ONLY kernel-visible work
    in the whole job, and `next_action` selects that dispatch for it -- so here
    an unauthorized verifier is the difference between a checked candidate and
    an unchecked one, not a normal receipt missing an optional field.
    """
    names = list(getattr(intake, "verified_by", None) or [])
    if not names:
        # `binds` is already False without a verifier, so reaching here with no
        # names would mean the two disagreed. Refuse rather than credit.
        return (
            "the admitted candidate has no named verifying receipt, so there "
            "is nothing whose authorization could be established"
        )
    try:
        import advance_kernel as _ak
    except Exception:  # noqa: BLE001
        # NO KERNEL IS NOT AUTHORIZATION. A runtime that cannot ask which
        # attempts were authorized cannot answer that one was, and crediting
        # `judged` here would turn an import failure into a bypass.
        return (
            "this runtime cannot read the dispatch history that would show a "
            "verifying stage was authorized, so the verification on record "
            "cannot be distinguished from a document written by hand"
        )
    active = story.get("mcp_active_dispatches")
    active = active if isinstance(active, dict) else {}
    for name in names:
        receipt = _read_receipt_for_binding(project_dir, story_id, name)
        if receipt is None:
            continue
        abbrev = str(receipt.get("agent_abbrev") or "").strip().lower()
        if not abbrev:
            abbrev = _abbrev_from_receipt_name(name)
        if not abbrev:
            continue
        authorized = {
            str(rec.get("attempt_id") or "")
            for rec in _ak.attempt_history(story, abbrev)
            if isinstance(rec, dict) and rec.get("attempt_id")
        }
        claimed_attempt = str(receipt.get("attempt_id") or "").strip()
        if claimed_attempt and claimed_attempt in authorized:
            return None
        binding = active.get(abbrev)
        live = ""
        if isinstance(binding, dict):
            live = str(binding.get("dispatch_id") or "").strip()
        if live and str(receipt.get("dispatch_id") or "").strip() == live:
            return None
    return (
        "the verifying receipt for the admitted candidate (%s) names no "
        "attempt or dispatch this kernel authorized, so what is on record is a "
        "document with the right shape rather than evidence that a verifying "
        "stage ran. Dispatch the verifying stage through the kernel so the "
        "receipt carries an attempt identity that can be checked."
        % ", ".join(sorted(names))
    )


def _abbrev_from_receipt_name(name: str) -> str:
    """`US-901-qe.json` -> `qe`. The filename is the fallback, never the source.

    `_declared_abbrev` on the receipt body is what `external_intake` matched
    on, so the body is preferred above; this covers a receipt whose body omits
    the field, where refusing outright would be stricter than the match that
    got us here.
    """
    stem = str(name or "").rsplit(".", 1)[0]
    return stem.rsplit("-", 1)[-1].strip().lower() if "-" in stem else ""


def _read_receipt_for_binding(
    project_dir: str | None, story_id: str, name: str
) -> dict | None:
    """One receipt body by filename, through `external_intake`'s own resolver.

    Reusing `_story_receipt_names` rather than joining a path: the hosts differ
    in receipt layout (flat versus nested), and a second definition of where a
    receipt lives is the drift that makes a check pass on one host and silently
    find nothing on another.
    """
    if not project_dir:
        return None
    import json as _json
    import os as _os

    try:
        import external_intake as _ei

        paths = _ei._story_receipt_names(str(project_dir), str(story_id))
    except Exception:  # noqa: BLE001
        return None
    for path in paths:
        if _os.path.basename(path) != name:
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                body = _json.load(handle)
        except (OSError, ValueError):
            return None
        return body if isinstance(body, dict) else None
    return None


def _story_candidate_digest(
    story: dict[str, Any],
    story_id: str,
    project_dir: str | None,
    intake: Any = _UNREAD,
) -> tuple[str | None, str | None, str | None]:
    """The candidate a human verdict binds to: `(digest, why_absent, source)`.

    A judged verdict has to name the exact thing judged, or it is an opinion
    with no subject: accepting "the story" leaves nothing to detect when the
    story changes afterwards. Three derivations, in precedence order:

    0. An external candidate admitted at intake (`external_intake.read_intake`).
       First in precedence because it is the only derivation that can answer at
       all for a verify-only job -- the platform produced nothing, so there are
       no receipts to digest (#495, proposal section 5 S4). The digest is
       re-derived on every read from the artifact bytes the record names, so the
       record's own digest field is a claim that is checked rather than trusted.
       **An admission that exists but does not yield a candidate STOPS here**
       rather than falling through to 1 and 2: a substituted artifact must leave
       the verdict unbacked, never quietly re-bound to receipt bytes, which
       would read as backed.
    1. The advance kernel's consumed-receipt ledger
       (`story["mcp_consumed_receipts"]`). Each entry is a sha256 over the
       exact receipt bytes a transition consumed, computed by the kernel from
       the bytes it read. Where it exists it is the best candidate identity
       in the tree, so the last entry is the candidate.
    2. A digest over the story's receipt files on disk, filename and bytes,
       in sorted order. Weaker (it is computed now rather than at the moment
       of consumption) but still binds the verdict to content that changes
       when the work changes.

    All three are DERIVED. None reads a digest a caller supplied at the moment
    of the verdict, which is why a forged verdict cannot pick its own subject.
    What derivation 0 changes is WHEN and BY WHOM the subject is fixed -- at
    intake, before any checking ran -- and not whether the verdict may choose
    it. `external_intake`'s docstring states what that does and does not buy.

    Returns `(None, reason, None)` when no derivation has anything to work
    with, and the reason is recorded on the verdict. That case is honest and
    it is not rare: a story with no receipts has produced no candidate, and
    saying so beats inventing a digest over the story's own id.
    """
    if intake is _UNREAD:
        intake = _read_external_intake(project_dir, story_id)
    if intake is not None and intake.admitted:
        if intake.binds:
            unauthorized = _unauthorized_verifier(
                story, story_id, project_dir, intake
            )
            if unauthorized is not None:
                return None, unauthorized, None
            return intake.candidate_digest, None, CANDIDATE_SOURCE_INTAKE
        return None, intake.why_absent, None
    ledger = story.get("mcp_consumed_receipts")
    if isinstance(ledger, list):
        recorded = [d for d in ledger if isinstance(d, str) and d.strip()]
        if recorded:
            return (
                "sha256:%s" % recorded[-1].strip(),
                None,
                CANDIDATE_SOURCE_LEDGER,
            )
    if not project_dir:
        return None, (
            "no consumed-receipt ledger on the story and no project directory "
            "to read receipts from, so no candidate identity could be derived"
        ), None
    try:
        receipts_dir = _resolve_receipts_dir(str(project_dir))
        paths = sorted(
            p for p in Path(receipts_dir).glob("%s-*.json" % story_id) if p.is_file()
        )
    except OSError:
        paths = []
    if not paths:
        return None, (
            "no receipts exist for this story and no external candidate was "
            "admitted at intake, so there is no candidate for a verdict to "
            "bind to"
        ), None
    import hashlib

    h = hashlib.sha256()
    for path in paths:
        try:
            h.update(path.name.encode("utf-8"))
            h.update(path.read_bytes())
        except OSError:
            return None, (
                "a receipt for this story could not be read, so the candidate "
                "digest would describe a different set of bytes than the one "
                "judged"
            ), None
    return "sha256:%s" % h.hexdigest(), None, CANDIDATE_SOURCE_RECEIPT_BYTES


def _producer_principals(story_id: str, project_dir: str | None) -> set[str]:
    """The HUMAN principals recorded as having produced this story's work.

    Empty is the normal case today: receipts record `agent` and `role`, not a
    human, so there is usually nothing here to compare a judge against. That
    emptiness is exactly why `independent_of_producer` fails closed below
    rather than defaulting to true. Giving receipts a human principal is what
    would make independence derivable, and that is #435's provenance work,
    not something this function can assume.
    """
    if not project_dir:
        return set()
    try:
        receipts = collect_story_receipts(
            _resolve_receipts_dir(str(project_dir)), story_id
        )
    except Exception:  # noqa: BLE001 - a verdict must not fail on a bad receipt
        return set()
    out: set[str] = set()
    for receipt in receipts:
        if not isinstance(receipt, dict):
            continue
        for key in _PRODUCER_PRINCIPAL_KEYS:
            value = receipt.get(key)
            if isinstance(value, str) and value.strip():
                out.add(value.strip().lower())
    return out




def _uncreditable_for_digest(
    story: dict[str, Any], digest: "str | None", why_absent: "str | None"
) -> "tuple[str | None, str | None]":
    """Why a verdict over an ALREADY derived candidate could not be credited.

    Private, and it stays private. #604 exposed a public `uncreditable_verdict`
    that derived its own candidate, and #592's re-review found it answered
    differently from the record path: its optional `intake` argument defaulted
    to `None`, which in `_story_candidate_digest` means "intake was already
    consulted and there is none" rather than "read it now". So it skipped an
    admitted external candidate and fell through to receipt bytes, and reported
    a SUBSTITUTED artifact as creditable while the record path correctly
    refused it.

    The fix was not a better default. A caller wanting to know whether a
    verdict can be credited asks `record_judged_verdict(..., commit=False)`,
    which cannot disagree with the record path because it IS the record path.
    Two derivations of one fact drift; that is the whole lesson of this thread,
    and a second answer that merely happens to be right today is still a second
    answer.
    """
    if digest is None:
        return "no_candidate", why_absent
    existing = story.get(JUDGED_VERDICTS_KEY)
    if isinstance(existing, list) and any(
        isinstance(prior, dict) and prior.get("candidate_digest") == digest
        for prior in existing
    ):
        return "already_judged", (
            "a verdict is already recorded against candidate %s; a judged "
            "verdict is immutable once recorded, so re-judging requires a new "
            "candidate (produce new evidence, then judge that)" % digest
        )
    return None, None


def record_judged_verdict(
    story: dict[str, Any],
    story_id: str,
    *,
    verdict: str,
    principal: str,
    project_dir: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Record a human verdict on a story as `judged` evidence (#403).

    `commit=False` PREPARES the item and writes nothing, so a caller can
    inspect the exact verdict it is about to record and decline. That exists
    because deriving the candidate twice, once to check and once to record,
    let the two describe different moments (#592 re-review).

    The ONE place `accept_story` and `reject_story` both go through, so there
    is a single acceptance path rather than two shapes of the same decision.
    The item is the judged-verdict shape from `runtime_contracts`: a named
    principal, the verdict bound to the exact candidate identity, the
    recording instant, and independence from the producing principal
    RECORDED rather than assumed.

    Every discipline field is DERIVED here, and that is the whole forgeability
    answer for this class. `accepted_by` / `rejected_by` is the only caller
    input, and it is the principal, which is the one field a verdict is
    supposed to be a claim by. The candidate digest comes from the kernel's
    consumed-receipt ledger or from the receipt bytes on disk, never from an
    argument, so a verdict cannot choose what it is a verdict about.
    `independent_of_producer` is computed against the recorded producer set
    and FAILS CLOSED: false unless a human producer is on record and it is
    someone else. False is valid and recorded, never refused (the decision
    pinned during #399), so failing closed costs nothing except an
    unearned claim of independence.

    **When the candidate came from intake (#495) the producer is outside the
    platform, and `independent_of_producer` is then FALSE by decision, not by
    computation.** #495 asked what the field means in that case, and the answer
    it forbids is the tempting one: the in-platform producer set for a
    verify-only unit contains at most the CHECKER's receipts, so a judge who is
    not the checker would compute `true` and the record would claim
    independence from a producer nobody ever recorded. `independence_basis`
    names the outside producer, and names the intake principal so the case
    where the judge admitted their own candidate is visible rather than
    inferred.

    Immutability is enforced by refusing to CREDIT a second verdict against
    the same candidate: re-judging requires a new candidate, so a second
    verdict on unchanged evidence is appended as unbacked, with the reason,
    and the first recording is left exactly as it was. Returns the appended
    item, whose `evidence_class` is None whenever the discipline could not be
    met -- an unbacked claim is unbacked, never credited at its class.
    """
    recorded_at = _now()
    # Read the intake ONCE and hand it to the derivation: two reads would let
    # the digest come from one snapshot and the intake principal from another.
    intake = _read_external_intake(project_dir, story_id)
    digest, _why_absent, source = _story_candidate_digest(
        story, story_id, project_dir, intake
    )
    producers = _producer_principals(story_id, project_dir)
    principal_key = str(principal or "").strip().lower()
    external = source == CANDIDATE_SOURCE_INTAKE
    independent = (
        False if external else (bool(producers) and principal_key not in producers)
    )

    item: dict[str, Any] = {
        "evidence_class": "judged",
        "principal": str(principal or ""),
        "candidate_digest": digest,
        "verdict": verdict,
        "recorded_at": recorded_at,
        "independent_of_producer": independent,
        "check_id": "po_acceptance",
    }
    if source:
        item["candidate_source"] = source
    if external:
        admitted_by = str((intake.record if intake else {}).get("admitted_by") or "")
        item["intake_principal"] = admitted_by
        item["candidate_artifact"] = str(
            (intake.record if intake else {}).get("artifact_path") or ""
        )
        same_hand = bool(admitted_by) and admitted_by.strip().lower() == principal_key
        item["independence_basis"] = (
            "the candidate was produced outside this platform, so independence "
            "from the producing principal is not derivable here and is recorded "
            "false rather than computed against a producer set that holds only "
            "this platform's checkers; the candidate was admitted by %s%s"
            % (
                admitted_by or "an unrecorded principal",
                ", who is also the judge" if same_hand else "",
            )
        )
    elif not producers:
        item["independence_basis"] = (
            "no receipt for this story records a human principal, so "
            "independence from the producing principal could not be "
            "established; recorded false rather than assumed true"
        )
    else:
        item["independence_basis"] = (
            "compared against the human principals recorded on this story's "
            "receipts"
        )

    # WHY a verdict is uncredited decides what a caller may do about it, so the
    # two cases carry a machine-readable code and not only prose (#592
    # re-review). They are not degrees of the same thing:
    #
    # * `no_candidate` -- nothing was produced to bind to. The verdict backs
    #   nothing, which is why it is uncredited, but it overrules nothing
    #   either. `TestOrdinaryJobsAreUnaffected` pins this as an ordinary
    #   outcome, and it must stay one.
    # * `already_judged` -- a verdict IS recorded against this exact candidate.
    #   Crediting a second one would let it overrule the first, which is what
    #   the immutability rule exists to refuse.
    # Judged against the digest THIS call derived, not against a fresh read.
    # Re-deriving here is what let the check and the record describe different
    # moments (#592 re-review), so the snapshot taken above is the only one
    # this verdict is ever judged against.
    unbacked_code, unbacked_reason = _uncreditable_for_digest(
        story, digest, _why_absent
    )
    if unbacked_reason:
        item["evidence_class"] = None
        item["unbacked_reason"] = unbacked_reason
        item["unbacked_code"] = unbacked_code

    if not commit:
        # PREPARED ONLY. Nothing has been written, not even the verdict list:
        # `setdefault` would add an empty key to the story, which is a mutation
        # a refused acceptance must not leave behind (#592 re-review).
        return item
    return commit_judged_verdict(story, item, story_id, project_dir=project_dir)


def commit_judged_verdict(
    story: dict[str, Any],
    item: dict[str, Any],
    story_id: str,
    project_dir: "str | None" = None,
) -> dict[str, Any]:
    """Append and log THIS prepared verdict. The only writer of the ledger.

    Separated from preparation so a caller can inspect the exact item it is
    about to record and decline to record it. `accept_story` does that: the
    verdict it judges and the verdict it stores are the same object, derived
    from one candidate snapshot, so nothing can change underneath between the
    decision and the record (#592 re-review).
    """
    existing = story.setdefault(JUDGED_VERDICTS_KEY, [])
    if not isinstance(existing, list):  # a corrupt slot must not lose the verdict
        existing = []
        story[JUDGED_VERDICTS_KEY] = existing
    existing.append(item)
    _emit_project_event(
        "story_judged",
        project_dir=project_dir,
        story_id=story_id,
        verdict=item.get("verdict"),
        principal=item["principal"],
        candidate_digest=item.get("candidate_digest"),
        independent_of_producer=item.get("independent_of_producer"),
        evidence_class=item["evidence_class"],
    )
    return item


def request_acceptance(
    state: dict[str, Any],
    story_id: str,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Move a story from `reviewing` to `awaiting_acceptance` (#116).

    Called by the orchestrator when CR finishes and the PO acceptance
    toggle is on. The story sits in `awaiting_acceptance` until Sprint
    Review walks it via `accept_story` or `reject_story`.
    """
    return transition_story(
        state, story_id, "awaiting_acceptance", reason=None,
        project_dir=project_dir,
    )





def accept_story(
    state: dict[str, Any],
    story_id: str,
    accepted_by: str,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """PO accepts a story at Sprint Review (#116). awaiting_acceptance → done.

    **This is ADR-029's declared exception (#486): the one forward edge written
    outside the advance kernel, and the path all three hosts specify.** The
    discipline here is `record_judged_verdict`, not the kernel's — no
    anti-replay, no dispatch binding, no `next_action` agreement, and
    `accepted_by` is authenticated by nothing.

    **An acceptance whose verdict cannot be credited is refused (#592).** The
    controlling proposal says a judged verdict binds to the exact candidate and
    is immutable once recorded; `record_judged_verdict` applies that rule and
    returns the answer; this function reads it. Both uncredited cases are
    refused, because both leave the board saying `done` while the evidence says
    the acceptance was unbacked, and a state and its signal disagreeing is the
    split this epic exists to remove:

    * `already_judged` — a verdict is recorded against this exact candidate, so
      crediting a second one lets it overrule the first.
    * `no_candidate` — nothing was produced to bind to, so the verdict names no
      subject and there is no evidence for `done` to mean.

    Only a CREDITED acceptance reaches an emission, so an approval row always
    describes an acceptance that had evidence bound to it. The
    `evidence_dod / returned` path #599 added for uncredited verdicts became
    unreachable once refusal moved ahead of the write, and was removed.

    What is NOT provided, and belongs to #435: an authenticated verdict channel.
    `accepted_by` remains a caller-supplied string, so a credited verdict proves
    that evidence existed and was bound, never who judged it.
    """
    story = _find_story(state, story_id)
    if story is None:
        raise ValueError(f"Story not found: {story_id}")
    if story["state"] != "awaiting_acceptance":
        raise ValueError(
            f"Story {story_id} is not awaiting acceptance "
            f"(state: {story['state']})"
        )
    # ONE candidate snapshot decides and is recorded. #603 asked the question
    # with a separate read and then let `record_judged_verdict` take its own,
    # so a receipt removed between the two passed the check and stored an
    # uncredited verdict while the unit advanced to `done` (#592 re-review,
    # reproduced: three derivations, `evidence_class: null`, `state: done`).
    #
    # The verdict is PREPARED without writing, judged as the exact object it
    # is, and then either refused or committed unchanged. Nothing re-reads the
    # candidate in between, so there is no window to change it in.
    #
    # A refusal writes nothing at all, not even the empty verdict list, which
    # is why preparation must not `setdefault` (#592, previous round).
    item = record_judged_verdict(
        story,
        story_id,
        verdict="accepted",
        principal=accepted_by,
        project_dir=project_dir,
        commit=False,
    )
    if item.get("unbacked_code"):
        raise ValueError(
            "Story %s cannot be accepted: the acceptance verdict could not be "
            "credited (%s). Acceptance binds to a candidate, so completing the "
            "unit requires evidence to judge, not a sign-off alone."
            % (story_id, item.get("unbacked_reason") or "no reason recorded")
        )
    story["accepted_by"] = accepted_by
    story["accepted_at"] = _now()
    # #403 — the acceptance IS a judged verdict, so it is stored as one rather
    # than as two loose fields no reader can bind to a candidate. This commits
    # the very item judged above.
    commit_judged_verdict(story, item, story_id, project_dir=project_dir)
    _emit_project_event(
        "story_accept",
        project_dir=project_dir,
        story_id=story_id,
        accepted_by=accepted_by,
    )
    return transition_story(
        state, story_id, "done", reason=None, project_dir=project_dir,
    )


def reject_story(
    state: dict[str, Any],
    story_id: str,
    reason_class: str,
    feedback_text: str,
    rejected_by: str,
    *,
    acceptance_criteria_change: list[str] | None = None,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """PO rejects a story at Sprint Review (#116).

    Four reason classes route to different recovery states. See
    `_REJECTION_NEXT_STATE` and the table in the issue.

    The rejection is recorded as a first-class `rejection_feedback` entry
    on the story so the next SE dispatch reads it.
    """
    if reason_class not in REJECTION_REASONS:
        raise ValueError(
            f"Invalid reason_class {reason_class!r}; expected one of "
            f"{REJECTION_REASONS}"
        )

    story = _find_story(state, story_id)
    if story is None:
        raise ValueError(f"Story not found: {story_id}")
    if story["state"] != "awaiting_acceptance":
        raise ValueError(
            f"Story {story_id} is not awaiting acceptance "
            f"(state: {story['state']})"
        )

    feedback_entry = {
        "reason_class": reason_class,
        "feedback_text": feedback_text,
        "acceptance_criteria_change": acceptance_criteria_change or [],
        "rejected_by": rejected_by,
        "rejected_at": _now(),
    }
    story.setdefault("rejection_feedback", []).append(feedback_entry)
    # #403 — a rejection is a judged verdict on the same candidate an
    # acceptance would have bound to. Recorded through the same function, so
    # neither direction of the decision gets its own private shape.
    record_judged_verdict(
        story,
        story_id,
        verdict="rejected",
        principal=rejected_by,
        project_dir=project_dir,
    )

    if reason_class == "defer":
        # Tag for the next sprint's carry-over picker. Caller decides
        # which sprint to land in; we just mark it.
        story["carry_over"] = True

    _emit_project_event(
        "story_reject",
        project_dir=project_dir,
        story_id=story_id,
        reason_class=reason_class,
        rejected_by=rejected_by,
    )

    next_state = _REJECTION_NEXT_STATE[reason_class]
    return transition_story(
        state, story_id, next_state, reason=None, project_dir=project_dir,
    )


# ---------------------------------------------------------------------------
# H3-F1 Recovery Ladder — per-role retry tracking
# ---------------------------------------------------------------------------


def record_retry(
    state: dict[str, Any],
    story_id: str,
    role_abbrev: str,
    failure_reason: str,
    project_dir: str | None = None,
) -> int:
    """Increment the retry counter for (story, role). Returns the new count.

    Called by the Orchestrator when a receipt validation or verification fails
    before it decides to re-dispatch. The counter drives
    recommend_recovery_action(); the Orchestrator uses the tier to choose
    between same-prompt retry, augmented prompt, or transitioning to blocked.

    Also records a hash of failure_reason for degenerate-loop detection
    (BEA5-F1). If the same hash shows up twice across retries of the same
    role, recommend_recovery_action() will escalate to BLOCK even if the
    retry cap has not been reached — repeating the exact same failure
    means the generator is not learning.
    """
    import hashlib

    story = _find_story(state, story_id)
    if story is None:
        raise ValueError(f"Story not found: {story_id}")
    retries = story.setdefault("retries", {})
    retries[role_abbrev] = retries.get(role_abbrev, 0) + 1
    new_count = retries[role_abbrev]

    # Track failure-reason hashes per role to detect degenerate loops.
    failure_hashes = story.setdefault("retry_failure_hashes", {})
    reason_hash = hashlib.sha256(failure_reason.encode("utf-8", errors="replace")).hexdigest()[:16]
    role_hashes = failure_hashes.setdefault(role_abbrev, [])
    role_hashes.append(reason_hash)

    _emit_project_event(
        "retry_recorded",
        project_dir=project_dir,
        story_id=story_id,
        role=role_abbrev,
        retry_count=new_count,
        reason=failure_reason,
        reason_hash=reason_hash,
        degenerate=role_hashes.count(reason_hash) >= 2,
    )
    return new_count


def recommend_recovery_action(
    state: dict[str, Any],
    story_id: str,
    role_abbrev: str,
    retry_cap: int = DEFAULT_RETRY_CAP,
) -> dict[str, Any]:
    """Recommend the next recovery step for a failing (story, role).

    Returns a dict with keys:
        tier:  RETRY_TIER_SAME_PROMPT | RETRY_TIER_AUGMENTED_PROMPT | RETRY_TIER_BLOCK
        retry_count: current retry count for this role
        remaining: retries left before the block tier fires
        reason: human-readable explanation

    The Orchestrator inspects `tier` to decide:
      RETRY_TIER_SAME_PROMPT     → re-dispatch with unchanged prompt
      RETRY_TIER_AUGMENTED_PROMPT → re-dispatch with prompt augmented to cite the failure
      RETRY_TIER_BLOCK           → call transition_story(..., "blocked", reason=...)
    """
    story = _find_story(state, story_id)
    if story is None:
        raise ValueError(f"Story not found: {story_id}")
    retries = story.get("retries", {})
    count = retries.get(role_abbrev, 0)

    # BEA5-F1: degenerate-loop detection. If the same failure_reason_hash
    # appears twice in this role's history, force BLOCK regardless of
    # retry_count. Repeating the same failure means prompt augmentation
    # isn't helping — the generator is stuck.
    failure_hashes = story.get("retry_failure_hashes", {}).get(role_abbrev, [])
    degenerate = any(failure_hashes.count(h) >= 2 for h in set(failure_hashes))

    if degenerate:
        tier = RETRY_TIER_BLOCK
        reason = "degenerate loop — identical failure hash repeated"
    elif count == 0:
        tier = RETRY_TIER_SAME_PROMPT
        reason = "first failure — retry with same prompt"
    elif count < retry_cap:
        tier = RETRY_TIER_AUGMENTED_PROMPT
        reason = f"retry {count}/{retry_cap} — augment prompt with failure context"
    else:
        tier = RETRY_TIER_BLOCK
        reason = f"retry cap reached ({count} ≥ {retry_cap}) — block story"
    return {
        "tier": tier,
        "retry_count": count,
        "remaining": max(0, retry_cap - count),
        "reason": reason,
        "degenerate": degenerate,
    }


def reset_retries(
    state: dict[str, Any],
    story_id: str,
    role_abbrev: str | None = None,
) -> None:
    """Clear retry counter for a role (or all roles). Called on successful completion."""
    story = _find_story(state, story_id)
    if story is None:
        return
    retries = story.get("retries")
    if not retries:
        return
    if role_abbrev is None:
        story["retries"] = {}
    else:
        retries.pop(role_abbrev, None)


# ---------------------------------------------------------------------------
# Story Queries
# ---------------------------------------------------------------------------

def get_story(state: dict[str, Any], story_id: str) -> dict[str, Any] | None:
    """Retrieve a story record from current_stories by ID."""
    return _find_story(state, story_id)


def list_stories_by_state(
    state: dict[str, Any], target_state: str
) -> list[dict[str, Any]]:
    """Return all stories in a given state."""
    stories = state.get("current_stories", [])
    return [s for s in stories if s.get("state") == target_state]


# ---------------------------------------------------------------------------
# Deterministic dispatch — next_action (Loop Engineering P1, epic #75)
# ---------------------------------------------------------------------------

# The full action contract (issue #77). The Stop-hook loop engine (P2)
# treats the CONTINUE_ELIGIBLE set as continue-eligible; every other action
# allows the session to stop. Keep in sync with loop_engine.py when it lands.
# `not_in_execution` is returned only by the lifecycle wrappers
# (scrum/kanban_state_machine.next_action) when the board isn't in an
# execution state — it belongs to the shared contract so wrapper consumers
# and contract tests recognise it.
CONTINUE_ELIGIBLE = (
    "dispatch_se",
    "dispatch_qe",
    "dispatch_cr",
    "promote_story",
    "request_acceptance",
    "recover_blocked",
    # P5: a verification loop whose H3-F1 ladder is exhausted transitions the
    # failed story to blocked and moves on to the next dispatchable work — a
    # board-changing step, so the loop engine keeps driving (the next
    # next_action call picks the next story or surfaces all_blocked).
    "block_story",
)
_STOP_ACTIONS = (
    "await_acceptance",
    # #304: every queued Work Unit is held by an unmet `depends_on` edge and
    # nothing else is dispatchable. A STOP action, not continue-eligible:
    # `next_action` is pure, so re-driving it would produce an identical
    # progress digest until the loop guard tripped, and a guard trip is a
    # diagnostic-grade failure rather than a clean stop. Reachable only after
    # the whole stage scan, the blocked-recovery block and the
    # awaiting_acceptance gate have all found nothing.
    "deps_blocked",
    "sprint_complete",
    # SPQ only: every admitted Work Unit is terminal but the Cycle cannot close
    # until the cross-workstream barrier clears (docs/spq-lifecycle-design.md
    # §10.2). Emitted by spq_state_machine.next_action, never by this module —
    # it lives here because NEXT_ACTIONS is the action contract every consumer
    # keys off, and a wrapper action outside it fails the contract test.
    "await_sync",
    "all_blocked",
    "no_stories",
    "not_in_execution",
)
NEXT_ACTIONS = CONTINUE_ELIGIBLE + _STOP_ACTIONS

# In-flight stages are drained furthest-along-first so one story finishes
# before the next is pulled — minimises WIP and keeps receipts/story state
# converging instead of fanning out.
_STAGE_PRIORITY = ("reviewing", "testing", "in_progress", "queued")

# stage → (dispatch action, receipt role whose presence means the stage's
# agent already finished, state the orchestrator should transition to when
# that receipt exists).
_STAGE_DISPATCH = {
    "in_progress": ("dispatch_se", "se", "testing"),
    "testing": ("dispatch_qe", "qe", "reviewing"),
    # "reviewing" is special-cased (CR adaptivity + acceptance fork).
}


def progress_digest(state: dict[str, Any]) -> str:
    """16-hex digest of the story board's observable progress.

    Consumed by the P2 loop-guard: two consecutive session stops with the
    same digest mean no story advanced — the continuation loop is spinning
    without progress and must trip. Inputs are exactly the fields that
    change when real work lands: story id, sub-state, per-role retry
    counts, and the DoD verdict. Ordering-independent (sorted) and stable
    across json/dict key order.

    The DoD verdict is captured as a tri-state (none / pass / fail), NOT
    bool(passed): a story going from never-evaluated (`dod` absent) to
    evaluated-and-failed (`{passed: false}`) IS progress — a gate ran and
    produced a verdict — but `bool(None) == bool(False)`, so a bool cast
    would hide it and let the guard trip on real work.
    """
    import hashlib

    def _verdict(s: dict[str, Any]) -> str:
        dod = s.get("dod") or {}
        if "passed" not in dod:
            return "none"
        return "pass" if dod.get("passed") else "fail"

    items = sorted(
        (
            str(s.get("id", "")),
            str(s.get("state", "")),
            json.dumps(s.get("retries", {}), sort_keys=True),
            _verdict(s),
        )
        for s in state.get("current_stories", [])
    )
    return hashlib.sha256(repr(items).encode("utf-8")).hexdigest()[:16]


def _stage_entered_at(story: dict[str, Any], stage: str) -> str | None:
    """The most recent `entered_at` timestamp for `stage` from the story's
    pipeline_log, or None if the story never (re-)entered it."""
    for entry in reversed(story.get("pipeline_log", []) or []):
        if entry.get("state") == stage:
            return entry.get("entered_at")
    return None


def _parse_iso_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp the same way the advance kernel does.

    next_action and evaluate_dispatch must share one freshness clock
    (`completed_at` vs stage `entered_at`). mtime is not that clock: a
    copied or restored receipt can have a new mtime and a stale
    `completed_at`, or the reverse.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def receipt_timestamp_is_fresh(
    completed_dt: datetime | None, entered_dt: datetime | None
) -> bool:
    """True when the receipt is contemporaneous with or after stage entry.

    Agents (especially Cursor) often emit second-precision `completed_at`
    (`…:34Z`) while stage `entered_at` is written with microseconds
    (`…:34.647295Z`). Strict `>` then treats a same-second receipt as stale,
    so next_action asks for a dispatch that already finished. A leftover from
    a prior attempt is dated an earlier second and still fails this check.
    """
    if completed_dt is None or entered_dt is None:
        return False
    if completed_dt > entered_dt:
        return True
    completed_utc = completed_dt.astimezone(timezone.utc).replace(microsecond=0)
    entered_utc = entered_dt.astimezone(timezone.utc).replace(microsecond=0)
    return completed_utc == entered_utc


def _fresh_receipt(
    receipts_dir: str | None,
    story_id: str,
    role: str,
    story: dict[str, Any],
    stage: str,
) -> bool:
    """True when {story_id}-{role}.json exists, PARSES as a JSON object, AND
    its `completed_at` post-dates the story's entry into `stage`.

    Three ways a receipt fails to count as "stage complete":

    - **Missing** — nothing to advance on.
    - **Stale** — a receipt from a PRIOR attempt, or one left behind when
      `reject_story` needs-fix reset an accepted story back to an earlier
      stage. Advancing on it silently skips the (re-)work the current stage
      exists to do. Detected by comparing receipt `completed_at` against the
      current stage-entry timestamp — the same clock `evaluate_dispatch`
      uses for RECEIPT_ALREADY_PRESENT.
    - **Malformed** — a receipt that isn't valid JSON. `collect_story_receipts`
      (which DoD reads) skips unparseable files, so treating a malformed
      receipt as "present" here would make next_action advance the story on
      a receipt DoD will never see — the exact dispatch/DoD disagreement this
      whole path exists to avoid. Treated as absent → re-dispatch.

    receipts_dir=None or a missing stage timestamp fail safe toward
    re-dispatch / existence respectively (dispatch is idempotent).
    """
    if not receipts_dir:
        return False
    receipt = None
    for directory in companion_receipt_dirs(receipts_dir):
        path = os.path.join(directory, get_story_receipt_path(story_id, role))
        try:
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                receipt = loaded
                break
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    if receipt is None:
        return False
    entered = _stage_entered_at(story, stage)
    if not entered:
        return True  # no stage timestamp to compare against — fall back to existence
    entered_dt = _parse_iso_timestamp(entered)
    if entered_dt is None:
        return True
    completed_dt = _parse_iso_timestamp(receipt.get("completed_at"))
    if completed_dt is None:
        return False
    return receipt_timestamp_is_fresh(completed_dt, entered_dt)


def _blocked_recovery_role(story: dict[str, Any]) -> str:
    """Pick the role whose retry ladder blocked the story (highest retry
    count), defaulting to the builder when nothing was recorded.

    Ties break toward the furthest-along stage (cr > qe > se) so a story
    that failed at review recovers at review rather than bouncing back to
    the build stage."""
    retries = story.get("retries") or {}
    if not retries:
        return "se"
    _stage_rank = {"cr": 3, "qe": 2, "se": 1}
    return max(
        retries.items(), key=lambda kv: (kv[1], _stage_rank.get(kv[0], 0))
    )[0]


# Remediation role per conditional DoD gate: the agent/mode that can
# actually clear it. A gate-blocked story MUST route here, never to the SE
# retry ladder (which can never satisfy a compliance/runtime/ui/integration
# gate — see dod_gate_block_reason). Kept in lockstep with the gate ids in
# active_dod_checks / dod_gate_block_reason.
_GATE_REMEDIATION_ROLE = {
    "no_critical_findings": "ce",   # compliance-engineer audit (#31)
    "runtime_verified": "qe",       # QE live-deployment verification (#30)
    "ui_acceptance": "qe",          # QE browser-qa (#44)
    "integration_verified": "qe",   # QE live integration smoke (#44 phase 5)
    # #179 E1 — evidence-replay mismatch on a static check routes to the role
    # that produced the receipt (its attested exit code did not reproduce).
    # Conditional gates above are matched first, so these only fire for a
    # replay-mismatch block reason (which names the static check id).
    "tests_pass": "qe",             # QE test evidence did not reproduce
    "build_succeeds": "se",         # SE build evidence did not reproduce
    # #403 — the two remaining static checks, reachable here only through a
    # `criteria_gap_declared` block. Appended last so the conditional gates
    # above keep their matching priority, per the note at the top.
    "code_reviewed": "cr",          # a review is the CR's own act
    "coverage_no_decrease": "qe",   # QE records metrics.coverage_delta
}


def _attach_compliance_declaration(
    out: dict[str, Any],
    compliance: dict[str, Any] | None,
    receipts_dir: str | None,
    story: dict[str, Any],
) -> None:
    """Fill `out["dod"]["compliance"]` and extend `declared_checks` (#487).

    The PHI verdict is recomputed from the unit's receipts on disk, using the
    same `story_touches_phi` predicate `evaluate_story_dod` will use at the
    gate. Two evaluators reading one predicate is the point: a declaration
    that could disagree with the gate would be worse than no declaration,
    because a dispatch would produce evidence for a check the gate never
    required and skip one it did.

    No-op without compliance inputs, which is every Scrum and Kanban caller
    (proposal 7) and every caller written before this ticket.
    """
    if not isinstance(compliance, dict):
        return
    baa_enforced = bool(compliance.get("baa_enforced"))
    required = False
    if baa_enforced and receipts_dir:
        signals = tuple(compliance.get("phi_signals") or DEFAULT_PHI_RISK_SIGNALS)
        try:
            receipts = collect_story_receipts(receipts_dir, str(story.get("id", "")))
        except Exception:
            receipts = []
        required = story_touches_phi(receipts, signals)
    declaration = compliance_declaration(
        baa_enforced=baa_enforced,
        required=required,
        build_mode=compliance.get("build_mode"),
    )
    out["dod"]["compliance"] = declaration
    if required:
        declared = out["dod"].get("declared_checks") or []
        for cid in declaration["checks"]:
            if cid not in declared:
                declared.append(cid)
        out["dod"]["declared_checks"] = declared


def _gate_remediation(
    story: dict[str, Any],
    *,
    accountable: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """If the story was blocked by an unmet conditional DoD gate, return
    {tier:'gate_remediation', role, gate, reason}; else None.

    A gate block is recorded by the state-machine wrapper as a
    `blocked_reason` beginning 'DoD gate:' (see dod_gate_block_reason) and
    carries NO retry-ladder entries — so the ladder would read it as a
    fresh SE failure (count 0 → retry_same_prompt) and re-dispatch SE
    forever without ever touching the gate. Detect the marker and route to
    the gate's remediation agent instead.
    """
    reason = story.get("blocked_reason") or ""
    if not reason.startswith("DoD gate:"):
        return None
    for gate, role in _GATE_REMEDIATION_ROLE.items():
        if gate in reason:
            # #487 — a declared check's remediation role is whoever the gate
            # said OWES it on this lifecycle, which for the compliance set on
            # SPQ is the verifying stage rather than a per-unit
            # compliance-engineer dispatch that no orchestrator schedules.
            # Absent an override (Scrum, Kanban, any caller that does not
            # pass one) the table below answers exactly as it always did.
            role = (accountable or {}).get(gate, role)
            return {"tier": "gate_remediation", "role": role, "gate": gate, "reason": reason}
    return {"tier": "gate_remediation", "role": "qe", "gate": "unknown", "reason": reason}


# DoD check whose pass/fail tells us a stage's receipt actually verified.
_ROLE_VERIFY_CHECK = {"se": "build_succeeds", "qe": "tests_pass", "cr": "code_reviewed"}

#: The verdict `next_action` stamps on a recovery it selected because the
#: stage's bound attempt is no longer live (#690). The literal IS
#: `advance_kernel.ATTEMPT_NOT_LIVE`, deliberately: the planner's reason for
#: routing around the edge and the kernel's reason for refusing it are one
#: fact, and spelling them differently is how two components came to be each
#: correct and jointly stuck. `test_failed_attempt_recovery` asserts they
#: still agree.
ATTEMPT_NOT_LIVE_VERDICT = "attempt_not_live"


def _unadvanceable_attempt(
    story: dict[str, Any], role: str
) -> dict[str, Any] | None:
    """`advance_kernel.unadvanceable_attempt`, asked safely from the planner.

    NO KERNEL, NO REFUSAL. A runtime that cannot import the kernel cannot hit
    the refusal this routes around either, so answering "not a recovery" is
    exactly right rather than conservative. Contrast `_unauthorized_verifier`,
    which refuses on the same import failure: there the kernel's absence would
    have CREDITED something, here it would only decline to reroute.
    """
    try:
        import advance_kernel as _ak
    except Exception:  # noqa: BLE001
        return None
    try:
        return _ak.unadvanceable_attempt(story, role)
    except Exception:  # noqa: BLE001
        return None


def _receipt_verification_verdict(
    receipts_dir: str | None,
    story_id: str,
    role: str,
    story: dict[str, Any] | None = None,
) -> str | None:
    """'failed' when the role's receipt is present but its verification/status
    is DEFINITIVELY failed, 'unverified' when the receipt is present but its
    evidence is unverifiable (`_evaluate_check` → None), None otherwise
    (absent/unreadable receipt, or verification passed).

    Drives strict-mode verification loops (P5): when on, next_action
    re-dispatches a failed stage instead of advancing on its receipt. Uses
    the same `_evaluate_check` the DoD gate uses, so the loop and the gate
    agree on what "failed" means. 'failed' always loops; 'unverified' loops
    only at the mature/release tiers (issue #179 E6a) — at those tiers the
    DoD gate would reject the unverifiable evidence anyway, so catching it
    at the stage saves a full stage-cycle.

    `story` (#406) is what keeps that agreement true. Omit it and `tests_pass`
    is evaluated on the pre-#406 rules here while the gate evaluates it
    against the authored set, so a QE receipt with a green suite and no
    per-case outcomes reads as VERIFIED to the loop (the story advances out of
    `testing`) and as a criteria gap to the gate (it cannot reach `done`). The
    loop exists to catch at the stage what the gate would reject later; two
    different definitions of "failed" is precisely the divergence that stops
    it doing so."""
    if not receipts_dir:
        return None
    check = _ROLE_VERIFY_CHECK.get(role)
    if not check:
        return None
    path = find_story_receipt_file(receipts_dir, story_id, role)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            receipt = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(receipt, dict):
        return None
    verdict = _evaluate_check(check, receipt, story=story)
    if verdict is False:
        return "failed"
    if verdict is None:
        return "unverified"
    return None


def _resilience_setting(project_dir: str | os.PathLike, key: str) -> str | None:
    """The raw (lowercased, unquoted) value of `resilience.<key>` from
    `.synaptory.yaml`, or None when the key / file is absent."""
    cfg = Path(project_dir) / ".synaptory.yaml"
    try:
        text = cfg.read_text(encoding="utf-8")
    except OSError:
        return None
    in_resilience = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line:
            continue
        indent = len(line) - len(line.lstrip())
        s = line.strip()
        if indent == 0:
            in_resilience = s.startswith("resilience:")
            continue
        if in_resilience and s.startswith(f"{key}:"):
            return s.split(":", 1)[1].strip().strip('"').strip("'").lower()
    return None


def verification_loops_active(project_dir: str | os.PathLike) -> bool:
    """P5 gate: strict quality enforcement AND verification loops resolved on.

    Resolution of `resilience.verification_loops` (issue #179 E2 — the
    autonomy/verification coupling): an autonomous loop must not be MORE
    autonomous than its verifier is strong, so when loop-continuation is on
    (its default) verification loops default on too instead of off.

    - explicitly enabled  → True (strict mode permitting)
    - explicitly disabled → False (the token-cost opt-out is respected)
    - absent / ``auto``   → follows loop-continuation: True unless
      `resilience.loop_continuation` is explicitly disabled
    """
    try:
        from mode_reader import get_quality_enforcement

        if get_quality_enforcement(str(project_dir)) != "strict":
            return False
    except Exception:
        return False
    val = _resilience_setting(project_dir, "verification_loops")
    if val in ("enabled", "true", "on", "yes", "1"):
        return True
    if val in ("disabled", "false", "off", "no", "0"):
        return False
    # Absent or `auto` → couple to loop-continuation (same value set as
    # loop_engine._config_disabled: absent key means continuation is ON).
    lc = _resilience_setting(project_dir, "loop_continuation")
    return lc not in ("disabled", "false", "off", "no", "0")


def dependency_gate_mode(project_dir: str | os.PathLike) -> str:
    """`resilience.dependency_gate`: "enforce" (default) or "warn".

    The gate itself is unconditional across all three lifecycles, because
    `depends_on` is one contract and #304's own framing objects to serial and
    parallel dispatch disagreeing about it. What is configurable is ENFORCEMENT,
    and only because the shipped prompt text created the exposure:
    `ceremonies/sprint-planning.md` documented `depends_on` as a
    batch-membership filter, so a project may hold aspirational or stale edges
    (including cut or typo'd upstreams) that would now stall a sprint. `warn`
    annotates and dispatches. One-release escape valve, visible in config where
    a reviewer sees it.
    """
    val = _resilience_setting(project_dir, "dependency_gate")
    return "warn" if val in ("warn", "warning", "annotate") else "enforce"


def dep_context(
    project_dir: str | os.PathLike, state: dict[str, Any]
) -> dict[str, Any]:
    """Build the external-knowledge bundle `story_dep_status` resolves against.

    Impure by design: `story_dep_status` stays a pure function and this is the
    single place that reads identity off state (and, from #303, the manifest and
    the dependency ledger off disk).

    Today there is no Cycle manifest, so `manifest_present` is False and any
    dependency that is not on the local board resolves `dep_unknown` with a
    detail naming the cause. That is #304's requirement verbatim: "until that
    shared ledger exists, an unknown dependency must fail closed rather than
    dispatch."

    `cycle_id` is synthesized from `current_cycle` until #303 lands a native
    one. That is enough for the AC it serves -- a Work Unit id from another
    Cycle cannot satisfy an edge -- because the local board holds exactly one
    Cycle, and the ledger interface it consults returns nothing and fails
    closed.
    """
    spq = state.get("spq") if isinstance(state.get("spq"), dict) else {}
    cycle_id = state.get("_cycle_id") or spq.get("cycle_id")
    if not cycle_id:
        cycle = state.get("current_cycle")
        cycle_id = str(cycle) if cycle not in (None, "") else None

    context: dict[str, Any] = {
        "cycle_id": cycle_id,
        # The Cycle records the declaration it hydrated from on state, not on
        # every individual unit. Keep both hashes in the context so dispatch
        # can reject a superseded one.
        "story_manifest_hash": state.get("manifest_hash"),
        "manifest_hash": None,
        "manifest_present": False,
        "owners": {},
        "admitted": None,
        "satisfied": {},
        "ledger_fresh": True,
        # ALWAYS FALSE, AND NOT A SETTING. It was documented as
        # `spq.sync.accept_unverified_events` and named in two refusals, and
        # nothing ever read a config value into it -- so the toggle was
        # advertised and inert. Two ways to fix that, and this is the stricter
        # one: an unverified claim never unblocks downstream work, and the
        # method asks for no toggle. Wiring it would have added a documented
        # route to weaken an evidence gate in order to make a docs page true.
        #
        # Kept as a field rather than inlined, because `story_dep_status` reads
        # it at three sites and a caller-supplied context is how a test pins
        # the OPEN behaviour it must never have.
        "accept_unverified": False,
    }

    # With a sealed manifest, a cross-workstream edge stops being an
    # indistinguishable "unknown id" and becomes
    # `dep_other_workstream_unresolved` NAMING the owner. That upgrade is the
    # whole reason #304's resolver takes a context: the operator's next step
    # differs completely between "you typo'd an id" and "spine has not
    # published its contract yet".
    if not cycle_id or str(state.get("build_mode") or "").lower() != "spq":
        return context
    try:
        import cycle_records
        import spq_state_machine

        manifest = spq_state_machine.read_manifest(project_dir, cycle_id)
        if not manifest:
            return context
        # A declaration whose hash does not verify is treated as ABSENT, not as
        # authority. Trusting a tampered admitted set would be worse than
        # having none: it could license a dispatch the real one forbids.
        if not cycle_records.verify_hash(manifest):
            return context
        context["manifest_present"] = True
        context["manifest_hash"] = manifest.get(cycle_records.HASH_FIELD)
        # No owner map. `SPD-194` retires the Workstream, so a unit is owned by
        # the Cycle that admitted it and there is no second owner to resolve
        # against -- which is why `owners` is empty rather than absent: a
        # resolver reading it gets "no other owner", not "unknown".
        context["owners"] = {}
        context["admitted"] = [
            str(u.get("id")) for u in manifest.get("admitted_units") or []
            if u.get("id")
        ]
    except Exception:  # noqa: BLE001 - no manifest means local-board-only
        return context

    # The ledger is what actually resolves a cross-workstream edge. Read the
    # CACHE only -- never refresh here. `next_action` is called on every loop
    # iteration and must stay cheap and side-effect-free; a git fetch inside it
    # would make reading the board a network operation. Staleness is reported
    # rather than repaired, and the gate fails closed on it.
    try:
        import spq_ledger

        snap = spq_ledger.snapshot(project_dir, cycle_id)
        context["ledger_fresh"] = bool(snap.get("fresh"))
        context["satisfied"] = spq_ledger.satisfied_map(snap, manifest)
    except Exception:  # noqa: BLE001 - no ledger means nothing is satisfied
        context["ledger_fresh"] = False

    # There is no parent. `SPD-194` retires the Coordination Cycle
    # (`SC-MTH-011`): composition existed only because integration was
    # deferred, and with trunk integration at Checkpoint there is nothing left
    # to compose. A change that must land atomically with its consumer cannot
    # be ORDERED, so it cannot be SPLIT -- it is admitted to one Cycle
    # (`SC-MTH-012`), which is why there is no cross-Cycle resolver here and
    # `cycle_records` refuses a dependency naming a unit this Cycle did not
    # admit. Coupling across separately released repositories goes through
    # published, versioned artifacts consumed at the consumer's own next Commit
    # (`SC-MTH-013`).
    return context


# ---------------------------------------------------------------------------
# Parallel dispatch (#134 GAP-6/GAP-8)
# ---------------------------------------------------------------------------

# Conditions a dependency edge may declare. A bare string `depends_on` entry
# means `done` — today's exact semantics, preserved so existing boards keep
# working. The richer conditions are resolved against the Cycle ledger (#303);
# until that ledger exists they are unresolvable and therefore fail closed,
# which is what #304 asks for ("an unknown dependency must fail closed rather
# than dispatch").
DEP_CONDITIONS = (
    "done",
    "contract_published",
    "artifact_published",
    "integrated",
    "environment_ready",
    "cycle_integrated",
)

# Why each edge is unmet. Distinguishable because the operator action differs:
# waiting is not the same as fixing a typo, and neither is the same as pushing a
# branch from another workstream.
DEP_INCOMPLETE = "dep_incomplete"
DEP_CANCELLED = "dep_cancelled"
DEP_UNKNOWN = "dep_unknown"
DEP_OTHER_WORKSTREAM = "dep_other_workstream_unresolved"
DEP_OUT_OF_CYCLE = "dep_out_of_cycle"
DEP_EXTERNAL = "dep_external"
DEP_STALE_MANIFEST = "dep_stale_manifest"
DEP_LEDGER_STALE = "dep_ledger_stale"
DEP_CONDITION_UNVERIFIED = "dep_condition_unverified"
# A cross-Cycle edge the PARENT never declared (#305). Its own code because the
# operator action is categorically different from every other hold: revise the
# Coordination Cycle manifest. Not the child, not the ledger, not a typo.
DEP_CROSS_CYCLE_UNDECLARED = "dep_cross_cycle_undeclared"


def normalize_dep(entry: Any) -> dict[str, Any]:
    """Normalize one `depends_on` entry to `{unit_id, condition, external}`.

    Accepts the legacy string form (`"WU-7"`), the #303 dict form
    (`{"unit_id": "WU-7", "condition": "contract_published"}`), and the
    cross-Cycle form (`{"external": {...}}`) which is owned by #305 and is
    never satisfiable from inside one Cycle.
    """
    if isinstance(entry, dict):
        external = entry.get("external")
        if isinstance(external, dict):
            # `unit_id` is a Work Unit id or NOTHING. It used to fall back to
            # `external["output"]`, which in the output-addressed form is a DICT
            # -- so the id became the literal string
            # "{'kind': 'contract', 'id': 'contracts/auth'}". That never crashed:
            # it produced an unaddressable edge and put a stringified mapping in
            # the operator's "why is this blocked" message. Output-addressed
            # edges are addressed by `spq_manifest.cross_ref_key`.
            #
            # `dict(external)` rather than the caller's object: the returned dep
            # is surfaced in `unmet[].dep` and must not alias live board state.
            return {
                "unit_id": str(external.get("unit_id") or ""),
                "condition": str(external.get("condition") or "cycle_integrated"),
                "output": external.get("output"),
                "external": dict(external),
            }
        condition = str(entry.get("condition") or "done")
        return {
            "unit_id": str(entry.get("unit_id") or entry.get("id") or ""),
            "condition": condition,
            "external": None,
        }
    return {"unit_id": str(entry), "condition": "done", "external": None}


def _condition_satisfies(declared: str, event: dict[str, Any]) -> bool:
    """Does a recorded event meet-or-exceed the DECLARED condition?

    One comparator for both the locally-owned and the cross-workstream branch.
    They had separate logic and drifted: the cross-workstream side compared
    strength while the local side accepted any event at all (review finding
    P1(2)).
    """
    recorded = str(event.get("condition") or "")
    try:
        import spq_manifest

        return bool(spq_manifest.satisfies(declared, recorded))
    except Exception:  # noqa: BLE001 - no manifest module means exact match only
        return recorded == declared


def cross_ref_key(cycle_id: Any, unit_id: Any, output: Any) -> str:
    """The cross-Cycle address, spelled identically to `spq_manifest.cross_ref_key`.

    Duplicated deliberately rather than imported: `story_dep_status` is pure and
    on the `next_action` hot path, and `spq_manifest` is not otherwise needed
    there. A test asserts the two spellings agree for every shape, because two
    spellings of one address is how a producer and a consumer stop matching.
    """
    cid = str(cycle_id or "")
    uid = str(unit_id or "")
    if uid:
        return "%s|unit:%s" % (cid, uid)
    if isinstance(output, dict) and (output.get("id") or output.get("kind")):
        return "%s|out:%s:%s" % (
            cid, str(output.get("kind") or ""), str(output.get("id") or "")
        )
    return "%s|cycle" % cid


def story_dep_status(
    state: dict[str, Any],
    story: dict[str, Any],
    *,
    dep_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve a story's `depends_on` edges. Pure: no file, git or env access.

    Returns `{"met": bool, "unmet": [{dep, reason_code, detail,
    owner_workstream}]}`. All external knowledge (cycle identity, the manifest's
    admitted set and owner map, the ledger's satisfied map) arrives through
    `dep_context`, so this stays a function of its arguments and is trivially
    testable. `dep_context=None` means "local board only", which is the
    pre-#303 world.

    Every unresolvable state fails CLOSED. A dependency that cannot be proven
    satisfied is treated as unsatisfied, because the alternative is starting
    implementation against a contract that does not exist yet.
    """
    ctx = dep_context or {}
    cycle_id = ctx.get("cycle_id")
    self_ws = ctx.get("workstream_id")
    manifest_present = bool(ctx.get("manifest_present"))
    owners: dict[str, Any] = ctx.get("owners") or {}
    admitted = ctx.get("admitted")
    satisfied: dict[str, Any] = ctx.get("satisfied") or {}
    accept_unverified = bool(ctx.get("accept_unverified"))
    ledger_fresh = ctx.get("ledger_fresh", True)

    cross_cycle: dict[str, Any] = ctx.get("cross_cycle") or {}
    declared_cross = set(ctx.get("declared_cross_keys") or ())
    coordination_id = ctx.get("coordination_cycle_id")
    coordination_fresh = ctx.get("coordination_fresh", False)
    coordination_detail = ctx.get("coordination_detail") or ""

    unmet: list[dict[str, Any]] = []

    def _hold(dep, code, detail, owner=None):
        unmet.append(
            {
                "dep": dep,
                "reason_code": code,
                "detail": detail,
                "owner_workstream": owner,
            }
        )

    def _hold_cross(dep, condition):
        """Resolve a cross-Cycle edge, or hold it with a code that names the fix.

        Stays PURE: every fact comes from `dep_context`. The condition strength
        comparison is a MEMBERSHIP test against a precomputed `satisfies` list,
        so this needs no import from `coordination_cycle` and no knowledge of
        cross-Cycle ranking.

        Every unresolvable state has its own code, because the operator action
        differs completely between them: revise the parent, chase a producer,
        refresh a cache, or ask for a verifiable condition.
        """
        external = dep.get("external") if isinstance(dep.get("external"), dict) else {}
        target_cycle = str(external.get("cycle_id") or "")
        output = dep.get("output") if isinstance(dep.get("output"), dict) else None
        key = cross_ref_key(target_cycle, dep["unit_id"], output)
        named = (
            dep["unit_id"]
            or (output or {}).get("id")
            or target_cycle
        )

        # An OUTPUT-addressed edge may omit the producer: the release already
        # declares who supplies it, and requiring the consumer to know would
        # force the producing Cycle to open first. Resolve it against the
        # declared set by matching the output half of the address.
        #
        # Ambiguity fails CLOSED rather than picking one: two children declared
        # as supplying the same output is a release-manifest problem, and
        # guessing would satisfy the edge from the wrong Cycle.
        if not target_cycle and declared_cross:
            suffix = key                      # "|out:kind:id"
            hits = [k for k in declared_cross if k.endswith(suffix)]
            if len(hits) == 1:
                key = hits[0]
            elif len(hits) > 1:
                _hold(
                    dep,
                    DEP_CROSS_CYCLE_UNDECLARED,
                    "Coordination Cycle %s declares %d producers for %s (%s). "
                    "An output-addressed edge that could be satisfied by more "
                    "than one Cycle is ambiguous: name the producing cycle_id "
                    "on the edge, or fix the release manifest."
                    % (coordination_id, len(hits), named, ", ".join(sorted(hits))),
                )
                return

        if not coordination_id:
            _hold(
                dep,
                DEP_EXTERNAL,
                "cross-Cycle dependency on %r (%s) is owned by a Coordination "
                "Cycle, and %s. It is never satisfied by a local Work Unit. If "
                "a release DOES include this Cycle, its sealed manifest has to "
                "reach this clone: pull the branch carrying "
                "`.synaptory/coordination-cycles/`."
                % (
                    named or "<unnamed>",
                    condition,
                    coordination_detail
                    or "none was resolved from this clone",
                ),
            )
            return
        if key not in declared_cross:
            _hold(
                dep,
                DEP_CROSS_CYCLE_UNDECLARED,
                "Coordination Cycle %s declares no edge for %s. A child cannot "
                "invent a cross-Cycle dependency the release never agreed to -- "
                "add the edge to the parent manifest, then re-hydrate."
                % (coordination_id, key),
            )
            return

        record = cross_cycle.get(key)
        if record is None:
            stale = "" if coordination_fresh else (
                " (the coordination ledger cache is behind: %s -- run "
                "refresh_coordination to be sure)" % coordination_detail
            )
            _hold(
                dep,
                DEP_EXTERNAL if coordination_fresh else DEP_LEDGER_STALE,
                "no %s event is recorded yet for %s in Coordination Cycle %s%s"
                % (condition, key, coordination_id, stale),
            )
            return
        if condition not in (record.get("satisfies") or []):
            _hold(
                dep,
                DEP_EXTERNAL,
                "%s has a %r event, which does not meet the declared condition "
                "%r" % (key, record.get("condition"), condition),
            )
            return
        if not record.get("verified") and not accept_unverified:
            _hold(
                dep,
                DEP_CONDITION_UNVERIFIED,
                "the %s event for %s is a claim, not proof, and this release "
                "does not accept unverified cross-Cycle events"
                % (record.get("condition"), key),
            )
            return
        # Satisfied.


    # A projection hydrated from a superseded manifest cannot be trusted about
    # ANY edge, so this short-circuits rather than adding one reason per dep.
    local_hash = story.get("manifest_hash") or ctx.get("story_manifest_hash")
    ctx_hash = ctx.get("manifest_hash")
    if local_hash and local_hash != ctx_hash:
        entries = story.get("depends_on") or [
            {"unit_id": story.get("id") or "<current>", "condition": "manifest_current"}
        ]
        current = ctx_hash or "<unavailable>"
        for entry in entries:
            _hold(
                normalize_dep(entry),
                DEP_STALE_MANIFEST,
                "workstream projection is on manifest %s but the Cycle is on "
                "%s — re-hydrate before dispatching" % (local_hash, current),
            )
        return {"met": False, "unmet": unmet}

    for entry in story.get("depends_on") or []:
        dep = normalize_dep(entry)
        unit_id = dep["unit_id"]
        condition = dep["condition"]

        if dep["external"] or condition == "cycle_integrated":
            _hold_cross(dep, condition)
            continue

        if not unit_id:
            _hold(dep, DEP_UNKNOWN, "dependency entry names no Work Unit id")
            continue

        local = _find_story(state, unit_id)

        # An id admitted to a DIFFERENT Cycle must not satisfy this edge, even
        # when the id matches. Checked before the local board so a stale story
        # record cannot launder a foreign id.
        if local is not None and cycle_id:
            local_cycle = local.get("cycle_id")
            if local_cycle and str(local_cycle) != str(cycle_id):
                _hold(
                    dep,
                    DEP_OUT_OF_CYCLE,
                    "%s resolves under Cycle %s, not %s — a matching Work Unit "
                    "id from another Cycle cannot satisfy this edge"
                    % (unit_id, local_cycle, cycle_id),
                )
                continue

        owner = owners.get(unit_id)
        owned_elsewhere = bool(owner and self_ws and owner != self_ws)

        if local is not None and not owned_elsewhere:
            st = local.get("state")
            if condition != "done":
                # A stronger condition than "built and verified" is a ledger
                # fact, not a board fact, so it cannot be read off the local
                # story even for a unit this workstream owns.
                #
                # Review finding P1(2): this checked only that SOME event
                # existed. A weaker event (`done`) or an unverified one
                # therefore satisfied a stronger declared condition
                # (`integrated`) and licensed downstream dispatch -- the exact
                # hole the cross-workstream branch was written to close, left
                # open for locally owned units. Ownership changes who can
                # publish the event, not what the event has to prove.
                event = satisfied.get(unit_id)
                if event is None:
                    # TWO UNSATISFIED STATES, AND THE DISTINCTION IS THE POINT.
                    # "We have never looked" is not "we looked and there is
                    # nothing": only the second means chasing the producer
                    # rather than running a refresh. Both fail closed, so this
                    # costs no safety and is the whole difference between a
                    # hold an operator can act on and one they cannot.
                    #
                    # It used to live only on the non-local branch, where a
                    # cross-lane edge was resolved. `SPD-194` puts every
                    # admitted unit on one board, so that branch became
                    # unreachable and the distinction was lost with it -- the
                    # gate reported "no such event is recorded" for a cache it
                    # had never read.
                    _hold(
                        dep,
                        DEP_CONDITION_UNVERIFIED if ledger_fresh else DEP_LEDGER_STALE,
                        "%s declares condition %r, which is proven by a Cycle "
                        "ledger event rather than by board state; no such event "
                        "is recorded%s"
                        % (
                            unit_id,
                            condition,
                            "" if ledger_fresh else
                            " and the local ledger cache is behind, so run "
                            "refresh_ledger before believing it",
                        ),
                        owner,
                    )
                    continue
                if not _condition_satisfies(condition, event):
                    _hold(
                        dep,
                        DEP_OTHER_WORKSTREAM,
                        "%s has a %r event, which does not meet the declared "
                        "condition %r" % (unit_id, event.get("condition"), condition),
                        owner,
                    )
                    continue
                if not event.get("verified") and not accept_unverified:
                    _hold(
                        dep,
                        DEP_CONDITION_UNVERIFIED,
                        "the %s event for %s is a claim, not proof, and "
                        "an unverified claim never unblocks downstream "
                        "work: name a condition the consumer can check"
                        % (condition, unit_id),
                        owner,
                    )
                    continue
                if st == "cancelled":
                    _hold(
                        dep,
                        DEP_CANCELLED,
                        "%s was cut from the Cycle, so this edge can never be "
                        "satisfied — re-admit it or drop the dependency"
                        % unit_id,
                        owner,
                    )
                    continue
                # A SATISFYING VERIFIED EVENT IS THE ANSWER, and the producer's
                # board state is not additionally required. Declaring a
                # condition SMALLER than `done` exists precisely to unblock
                # earlier: `contract_published` is published while the producer
                # is still in progress, which is #303's "prefer the smallest
                # sufficient condition".
                #
                # Falling through to the `done` check below made every
                # conditioned edge wait for the producer to finish anyway, so
                # the mechanism collapsed into "wait for done, plus extra
                # proof". It was invisible while a lane owned the upstream,
                # because the cross-lane branch never consulted board state;
                # with the Workstream retired every unit is local and the
                # fall-through applies to every edge.
                continue
            if st == "done":
                continue
            if st == "cancelled":
                _hold(
                    dep,
                    DEP_CANCELLED,
                    "%s was cut from the Cycle, so this edge can never be "
                    "satisfied — re-admit it or drop the dependency"
                    % unit_id,
                    owner,
                )
                continue
            _hold(
                dep,
                DEP_INCOMPLETE,
                "%s is %s, not done" % (unit_id, st or "in an unknown state"),
                owner,
            )
            continue

        # Not on this board (or owned by another workstream).
        if manifest_present and isinstance(admitted, (list, tuple, set)):
            if unit_id not in admitted:
                _hold(
                    dep,
                    DEP_UNKNOWN,
                    "%s is not in the Cycle manifest's admitted set — a typo, "
                    "or a unit belonging to another Cycle" % unit_id,
                )
                continue
            # Freshness is checked AFTER satisfaction, not before. An event is
            # an append-only fact, so its age does not invalidate it: a stale
            # cache can only be MISSING newer events, and "not satisfied" is
            # already the fail-closed direction. Checking staleness first
            # masked the far more useful answer -- which workstream owns the
            # upstream -- behind a code that says nothing about who to chase.
            event = satisfied.get(unit_id)
            if event is None:
                stale_note = (
                    "" if ledger_fresh else
                    " (the local ledger cache is behind; run refresh_ledger to "
                    "be sure)"
                )
                _hold(
                    dep,
                    DEP_LEDGER_STALE if not ledger_fresh else DEP_OTHER_WORKSTREAM,
                    "%s is owned by workstream %r and no %s event is recorded "
                    "for it yet%s"
                    % (unit_id, owner or "unknown", condition, stale_note),
                    owner,
                )
                continue
            if not _condition_satisfies(condition, event):
                _hold(
                    dep,
                    DEP_OTHER_WORKSTREAM,
                    "%s has a %r event, which does not meet the declared "
                    "condition %r" % (unit_id, event.get("condition"), condition),
                    owner,
                )
                continue
            if not event.get("verified") and not accept_unverified:
                _hold(
                    dep,
                    DEP_CONDITION_UNVERIFIED,
                    "the %s event for %s is a claim, not proof, and "
                    "an unverified claim never unblocks downstream "
                    "work: name a condition the consumer can check"
                    % (condition, unit_id),
                    owner,
                )
                continue
            continue

        # No manifest: cross-workstream edges are unresolvable by construction.
        _hold(
            dep,
            DEP_UNKNOWN,
            "%s is not on this board and no Cycle manifest is available. If it "
            "is owned by another workstream, resolving it requires the Cycle "
            "manifest and dependency ledger (#303)." % unit_id,
            owner,
        )

    return {"met": not unmet, "unmet": unmet}


def _story_deps_met(state: dict[str, Any], story: dict[str, Any]) -> bool:
    """True when every `depends_on` story is terminal (`done`).

    Unknown dependency ids fail closed (not met) — a typo'd dependency must
    not silently license parallel dispatch. Retained as the boolean face of
    `story_dep_status` so existing call sites are untouched."""
    return story_dep_status(state, story)["met"]


def format_unmet_deps(unmet: list[dict[str, Any]], limit: int = 2) -> str:
    """One-line rendering of unmet edges for an action `reason`."""
    parts = [
        "%s (%s: %s)"
        % (u["dep"]["unit_id"] or "<unnamed>", u["reason_code"], u["detail"])
        for u in unmet[:limit]
    ]
    if len(unmet) > limit:
        parts.append("+%d more" % (len(unmet) - limit))
    return "; ".join(parts)


def _scope_prefix(glob_entry: Any) -> str:
    """Normalize a file_scope glob to its literal directory/path prefix."""
    return str(glob_entry).split("*", 1)[0].rstrip("/")


def _scopes_disjoint(a: list[Any], b: list[Any]) -> bool:
    """Conservative disjointness for declared file scopes.

    Two scope sets overlap when any normalized prefix of one is a path
    prefix of the other. Empty/blank entries fail closed (overlap)."""
    for x in a:
        for y in b:
            nx, ny = _scope_prefix(x), _scope_prefix(y)
            if not nx or not ny:
                return False
            if nx == ny or nx.startswith(ny + "/") or ny.startswith(nx + "/"):
                return False
    return True


def _parallel_batch(
    state: dict[str, Any],
    receipts_dir: str | None,
    serial: dict[str, Any],
    parallelism: dict[str, Any],
    dep_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Compute the parallelizable dispatch batch for a serial action (#134 GAP-6).

    `batch[0]` is ALWAYS the serial action, so an orchestrator that ignores
    the `parallel` block behaves exactly as today. (`attach_dispatch_dod_contract`
    later adds a per-member `dod` block to each entry, the lead included; that
    is additive and leaves the dispatch identity untouched — a batch is N
    dispatches for N stories, and the promoted gates are a per-story fact.)
    Additional members are
    SE-stage dispatches only (queued stories, or in_progress stories whose SE
    receipt is absent): the wall-clock lives in the build stage, and scoping
    parallelism to SE keeps QE's dev-server/browser work serialized on the
    merged main workspace (GAP-8's race surface).

    A story joins the batch iff:
      1. its own next step is `dispatch_se` with no fresh receipt,
      2. every `depends_on` story is `done` (fail-closed on unknown ids),
      3. isolation is feasible: `worktree` allows file overlap (conflicts
         surface at merge); `shared` requires every member — lead included —
         to declare a non-empty `file_scope` pairwise-disjoint from all
         other members' scopes,
      4. the batch is below `max_concurrent`.

    Batches are built ONLY when the serial action is `dispatch_se` (#170
    review). QE runs serially post-merge on the merged main workspace — it
    shares one dev server and browser session, exactly the race surface this
    work package removes — so a `dispatch_qe` serial action must never seed a
    concurrent batch.
    """
    action = serial.get("action")
    if action != "dispatch_se":
        return None
    if serial.get("recovery") or serial.get("receipt_present"):
        return None  # verification-loop / crash-recovery paths stay serial
    isolation = parallelism.get("isolation", "worktree")
    cap = max(1, int(parallelism.get("max_concurrent", 3)))
    lead_id = str(serial.get("story_id") or "")
    lead_story = _find_story(state, lead_id) or {}
    lead_scope = lead_story.get("file_scope") or []
    batch: list[dict[str, Any]] = [
        {"story_id": lead_id, "action": action, "role": serial.get("role")}
    ]
    batch_scopes: list[list[Any]] = [lead_scope]
    skipped: list[str] = []

    # #304: the LEAD was never dependency-checked, only the additional members.
    # Return the block (never None) so the condition is visible rather than
    # silently absent, and keep `batch == [lead]` so the
    # `batch[0] == serial action` invariant holds for an orchestrator that
    # ignores the block -- which is what makes this safe under warn mode and
    # for the in_progress lead the serial gate deliberately does not cover.
    lead_dep = story_dep_status(state, lead_story, dep_context=dep_context)
    if not lead_dep["met"]:
        return {
            "eligible": False,
            "batch": batch,
            "isolation": isolation,
            "max_concurrent": cap,
            "reason": "lead %s has unmet dependencies: %s"
            % (lead_id, format_unmet_deps(lead_dep["unmet"])),
            "lead_dependencies_held": lead_dep["unmet"],
        }

    for s in state.get("current_stories", []):
        if len(batch) >= cap:
            break
        sid = str(s.get("id", ""))
        if not sid or sid == lead_id:
            continue
        st = s.get("state")
        if st == "queued":
            pass
        elif st == "in_progress" and not _fresh_receipt(
            receipts_dir, sid, "se", s, "in_progress"
        ):
            pass
        else:
            continue
        member_dep = story_dep_status(state, s, dep_context=dep_context)
        if not member_dep["met"]:
            skipped.append(
                "%s (unmet depends_on: %s)"
                % (sid, format_unmet_deps(member_dep["unmet"], limit=1))
            )
            continue
        if isolation == "shared":
            scope = s.get("file_scope") or []
            if not scope or not lead_scope:
                skipped.append(f"{sid} (shared isolation requires declared file_scope)")
                continue
            if not all(_scopes_disjoint(scope, other) for other in batch_scopes):
                skipped.append(f"{sid} (file_scope overlaps batch)")
                continue
            batch_scopes.append(scope)
        batch.append({"story_id": sid, "action": "dispatch_se", "role": "se"})

    eligible = len(batch) > 1
    reason = (
        f"{len(batch)} dispatchable stories with no unmet dependencies"
        if eligible
        else "no additional story is independently dispatchable"
    )
    if skipped:
        reason += f"; held back: {', '.join(skipped[:5])}"
    return {
        "eligible": eligible,
        "batch": batch,
        "isolation": isolation,
        "max_concurrent": cap,
        "reason": reason,
    }


def _attach_parallel_batch(
    out: dict[str, Any],
    state: dict[str, Any],
    *,
    receipts_dir: str | None,
    parallelism: dict[str, Any] | None,
    dep_context: dict[str, Any] | None = None,
) -> None:
    """Attach the additive `parallel` block to a next_action output."""
    if not parallelism or not parallelism.get("story_parallelism"):
        return
    block = _parallel_batch(state, receipts_dir, out, parallelism, dep_context)
    if block is not None:
        out["parallel"] = block


# ---------------------------------------------------------------------------
# Interrupted-dispatch detection (#134 GAP-12)
# ---------------------------------------------------------------------------

def detect_interrupted_dispatch(
    project_dir: str, story_id: str
) -> dict[str, Any] | None:
    """Evidence that a prior agent run for `story_id` was interrupted mid-work.

    Called by the CLI wrapper when next_action chose a dispatch with no fresh
    receipt. Returns an evidence dict (or None) the orchestrator uses for the
    tier-0 RESUME step of the recovery ladder: re-dispatch the SAME role with
    a resume preamble instead of restarting from scratch (and instead of
    recording a retry).

    Signals, most precise first:
      - a per-story worktree (`.synaptory/.worktrees/<id>`) with uncommitted
        changes or commits ahead of the main HEAD;
      - uncommitted changes in the shared workspace (heuristic — the
        orchestrator judges whether they belong to this story before resuming).

    Best-effort: any git/subprocess failure returns None (no resume hint).
    """
    def _git(args: list[str], cwd: str) -> str | None:
        try:
            res = subprocess.run(
                ["git", *args], cwd=cwd, capture_output=True, text=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        return res.stdout.strip() if res.returncode == 0 else None

    evidence: dict[str, Any] = {}
    wt_path = os.path.join(project_dir, ".synaptory", ".worktrees", story_id)
    if os.path.isdir(wt_path):
        dirty = _git(["status", "--porcelain"], wt_path)
        ahead_raw = _git(
            ["rev-list", "--count", f"HEAD..synaptory/{story_id}"], project_dir
        )
        try:
            ahead = int(ahead_raw) if ahead_raw is not None else 0
        except ValueError:
            ahead = 0
        if dirty or ahead > 0:
            evidence["worktree"] = {
                "path": wt_path,
                "branch": f"synaptory/{story_id}",
                "dirty_files": (dirty or "").splitlines()[:20],
                "commits_ahead": ahead,
            }
    else:
        dirty = _git(["status", "--porcelain"], project_dir)
        if dirty:
            evidence["workspace"] = {
                "path": project_dir,
                "dirty_files": dirty.splitlines()[:20],
                "note": (
                    "uncommitted changes in the shared workspace — judge "
                    "whether they belong to this story before resuming"
                ),
            }
    return evidence or None


def gate_evidence_digest(
    receipts_dir: str | None,
    stories: list[dict[str, Any]],
    *,
    max_commands: int = 5,
    max_artifacts: int = 10,
) -> list[dict[str, Any]]:
    """Per-story evidence bundle for human gates (issue #179 E5).

    "Attach run data to all human-reviewed output": when next_action stops at
    a human gate (`await_acceptance`, `all_blocked`), the reviewer should see
    WHAT was proven, not just the board state. Assembled read-only from the
    stories' receipts: executed verification commands + exit codes, produced
    artifacts, structured runtime/UI verification objects, and the story's
    last DoD verdict. Bounded (`max_commands`/`max_artifacts` per receipt,
    commands truncated) so the digest never dominates a prompt.
    """
    digest: list[dict[str, Any]] = []
    for story in stories:
        sid = str(story.get("id", ""))
        entry: dict[str, Any] = {
            "story_id": sid,
            "title": story.get("title") or None,
            "state": story.get("state"),
        }
        if story.get("blocked_reason"):
            entry["blocked_reason"] = story["blocked_reason"]
        dod = story.get("dod")
        if isinstance(dod, dict) and "passed" in dod:
            entry["dod_passed"] = bool(dod.get("passed"))
            failed = [
                k for k, v in (dod.get("checks") or {}).items()
                if v.get("required") and v.get("passed") is not True
            ]
            if failed:
                entry["dod_failed_checks"] = failed
        receipts_evidence: list[dict[str, Any]] = []
        if receipts_dir:
            for receipt in collect_story_receipts(receipts_dir, sid):
                role = receipt.get("agent") or receipt.get("role") or "unknown"
                r_entry: dict[str, Any] = {"role": role}
                commands = [
                    {
                        "command": str(c.get("command", ""))[:200],
                        "exit_code": c.get("exit_code"),
                    }
                    for c in receipt.get("verification_commands", [])
                    if isinstance(c, dict) and "exit_code" in c
                ]
                if commands:
                    r_entry["commands"] = commands[:max_commands]
                artifacts = receipt.get("artifacts")
                if isinstance(artifacts, list) and artifacts:
                    r_entry["artifacts"] = [str(a) for a in artifacts[:max_artifacts]]
                metrics = receipt.get("metrics")
                if isinstance(metrics, dict):
                    for proof_key in ("runtime_verification", "ui_verification"):
                        if isinstance(metrics.get(proof_key), dict):
                            r_entry[proof_key] = metrics[proof_key]
                if len(r_entry) > 1:
                    receipts_evidence.append(r_entry)
        if receipts_evidence:
            entry["receipts"] = receipts_evidence
        digest.append(entry)
    return digest


def next_action(
    state: dict[str, Any],
    *,
    per_story_acceptance: bool = False,
    receipts_dir: str | None = None,
    verification_loops: bool = False,
    dod_tier_info: dict[str, Any] | None = None,
    parallelism: dict[str, Any] | None = None,
    dep_context: dict[str, Any] | None = None,
    dependency_gate: str = "enforce",
    compliance: dict[str, Any] | None = None,
    verify_only: "set[str] | frozenset[str] | None" = None,
) -> dict[str, Any]:
    """Deterministically choose the orchestrator's next pipeline step.

    `verify_only` names the Work Units whose deliverable was produced outside
    the platform and admitted at intake (#495 P2). It is passed IN as data
    rather than derived here, and that is not a preference: this function has
    no `project_dir` and stays pure, which is the same reason
    `attach_dispatch_dod_contract` lives in the state machines. Recognising a
    verify-only unit means reading its intake record off disk, so the read
    belongs where the other project reads already are.

    Omitting it gives the pre-#495 selection for every unit, which is the
    honest degradation: a verify-only unit then selects `dispatch_se` and its
    sign-off stays unbacked with intake's own reason, rather than silently
    crediting a verifier nobody authorized.

    NOTE on the ``role`` field in the returned action (#204): it is an
    **abbreviation** (``se`` / ``qe`` / ``cr``), matching the receipt *filename*
    convention — NOT the value a receipt's ``role`` field takes, which is the
    full name (``software-engineer``). The two share a field name and mean
    different things, and prod receipts show the abbreviation being copied
    straight through into the analytics grouping key, where it became a phantom
    agent alongside the real one. The CP now canonicalizes at ingest, but do not
    pass this value into a receipt: see
    ``skills/_shared/protocols/receipt-protocol.md``.

    The prompt loop (modes/sprint.md, modes/kanban.md) calls this at the
    top of every iteration instead of trusting model memory for "what's
    next"; the P2 Stop-hook loop engine renders the same output as its
    continuation reason. Pure function of (state, toggles, receipt files)
    — no state writes, no environment reads. Config that the result depends
    on is resolved by the caller and passed in (`dod_tier_info`,
    `parallelism`, `compliance`); receipt files are read (#487's PHI verdict
    reads them here, and the freshness checks always have).

    Consequence for the `dod` block: it can only carry the STATIC tier list,
    because the four CONDITIONAL gates are promoted from `.synaptory.yaml`
    plus the story record. Every impure caller must therefore run the result
    through `attach_dispatch_dod_contract`, which replaces `active_checks`
    with the set the gate will actually enforce. Without it the prompt states
    a narrower contract than the gate applies — see that helper's docstring.

    Selection policy: finish the furthest-along in-flight story first
    (reviewing → testing → in_progress), then pull queued stories FIFO,
    then recover blocked stories (H3-F1 ladder permitting), then surface
    the human gates / terminal outcomes.

    verification_loops (P5): when True, a stage whose fresh receipt is
    present but FAILED verification — or, at the mature/release tiers,
    carries unverifiable evidence (#179 E6a) — is re-dispatched (H3-F1
    ladder) instead of advanced — catching the failure at the stage rather
    than one DoD-gate cycle later. Ladder-exhausted falls through to the
    normal advance so the DoD gate still blocks it. Gated on strict mode +
    config by the callers (`verification_loops_active`; since #179 E2 the
    absent-key default couples to loop_continuation); pure here.

    dod_tier_info (#134 GAP-11, default None): the `resolve_dod_tier` output.
    When provided, its tier drives CR adaptivity and is surfaced on the
    output's `dod` block so every dispatch carries the gate expectations;
    when absent, the tier is computed exactly as before (tier_source
    "computed" — the silent-promotion tell).

    parallelism (#134 GAP-6/GAP-8, default None): the `parallelism_config`
    output. When story parallelism is enabled and the chosen action is a
    dispatch, the output gains an ADDITIVE `parallel` block whose
    `batch[0]` always equals the serial action — an orchestrator that
    ignores it behaves exactly as before.

    compliance (#487, default None): the caller's project-level compliance
    inputs, `{"baa_enforced": bool, "phi_signals": (...), "build_mode": str}`.
    Passed in rather than read here because this function is a pure function
    of (state, toggles, receipt files) and reads no environment; the caller
    owns `.synaptory.yaml`. When provided, the output's `dod` block gains a
    `compliance` declaration naming the checks, who owes them, the evidence
    shape, and what an absent result becomes — the `SP-WRK-019` surface the
    pipeline previously had nowhere to state. When absent (Scrum, Kanban, and
    every pre-existing caller) the block is None and nothing changes.
    """
    stories = state.get("current_stories", [])
    counts = {st: 0 for st in STORY_STATES}
    for s in stories:
        st = s.get("state", "")
        if st in counts:
            counts[st] += 1

    if dod_tier_info and dod_tier_info.get("tier") in DOD_TIER_CHECKS:
        intensity = dod_tier_info["tier"]
        tier_source = dod_tier_info.get("tier_source", "planned")
    else:
        intensity = determine_dod_intensity(
            # SPQ counts Cycles, never sprints, so reading `current_sprint`
            # alone handed `determine_dod_intensity` two Nones and pinned every
            # SPQ project to the `early` tier forever -- contradicting
            # `spq_state_machine._dod_intensity`'s own docstring. Scrum and
            # kanban never set `current_cycle`, so this is inert for them.
            state.get("current_sprint") or state.get("current_cycle"),
            state.get("cumulative_ticket_number"),
            False,
        )
        tier_source = "computed"

    out: dict[str, Any] = {
        "action": "no_stories",
        "story_id": None,
        "story_title": None,
        "role": None,
        "reason": "no stories on the board",
        "counts": counts,
        "human_gate_pending": False,
        "receipt_present": False,
        "transition_to": None,
        "recovery": None,
        # #402 — the capability-profile pair this dispatch runs under, named
        # alongside the legacy `role` above so a dispatch prompt can state it
        # and the agent can copy it back onto its receipt verbatim (the pair
        # the advance kernel will hold that receipt to). Additive: None
        # wherever the action dispatches nobody.
        "profile": None,
        # #406 — the acceptance cases this Cycle authored at COMMIT. The
        # PRODUCING dispatch reads them as its target; the VERIFYING dispatch
        # executes them and reports a per-case outcome for each. Additive:
        # None wherever the action dispatches nobody, and carrying
        # `authored_at_commit: false` plus the recorded reason on the
        # migration path so a dispatch never has to infer which regime it is
        # running under.
        "authored_cases": None,
        # #134 GAP-11 — every next_action output carries the active DoD tier
        # and its base check set so dispatch prompts can state the gate
        # expectations instead of agents discovering them at gate time.
        "dod": {
            "tier": intensity,
            "tier_source": tier_source,
            "active_checks": list(
                DOD_TIER_CHECKS.get(intensity, DOD_TIER_CHECKS["early"])
            ),
            # #487 — the checks this dispatch is actually held to, tier set
            # PLUS the conditional promotions that are already determinable.
            # Both lists start as the STATIC tier list here because this
            # function is pure and cannot resolve a promotion. Every
            # orchestrator entry point then runs the result through
            # `attach_dispatch_dod_contract`, which fills `active_checks` with
            # the set the gate will enforce and unions it into this one. Read
            # neither field as final until that has run.
            "declared_checks": list(
                DOD_TIER_CHECKS.get(intensity, DOD_TIER_CHECKS["early"])
            ),
            # The compliance declaration, per story, filled by `_fill`. None
            # for every caller that passes no compliance inputs.
            "compliance": None,
        },
    }
    if not stories:
        return out

    cr_required = "code_reviewed" in DOD_TIER_CHECKS.get(
        intensity, DOD_TIER_CHECKS["early"]
    )

    def _fill(story: dict[str, Any], **kw: Any) -> dict[str, Any]:
        out.update(
            story_id=story.get("id"),
            story_title=story.get("title") or None,
            **kw,
        )
        out["profile"] = _profile_pair_for_role(out.get("role"))
        # #406 — attached for every action that names a pipeline role. CR gets
        # it too: it reviews the diff against the same declared criteria, and
        # a reviewer who cannot see the target is reviewing the diff against
        # itself, which is the bias this ticket exists to remove.
        out["authored_cases"] = (
            authored_cases_target(story)
            if out.get("role") in ("se", "qe", "cr")
            else None
        )
        # #487 — declare the compliance obligation on the dispatch contract.
        # Attached for every action that names a pipeline role, including SE:
        # the promotion is driven by what the DIFF touches, so the producing
        # stage is the first one able to know its unit is heading for the
        # gate, and telling only the verifier is telling it too late to have
        # scoped the work.
        _attach_compliance_declaration(out, compliance, receipts_dir, story)
        _attach_parallel_batch(
            out,
            state,
            receipts_dir=receipts_dir,
            parallelism=parallelism,
            dep_context=dep_context,
        )
        return out

    # #304: queued Work Units whose `depends_on` is unmet, collected across the
    # stage scan and surfaced only if nothing at all turns out to be
    # dispatchable. Skip-and-keep-scanning rather than refusing on the FIFO
    # head: refusing there would starve dispatchable work behind one blocked
    # unit, which reads as a hang.
    deps_held: list[dict[str, Any]] = []

    for stage in _STAGE_PRIORITY:
        candidates = [s for s in stories if s.get("state") == stage]
        if not candidates:
            continue
        story = candidates[0]  # FIFO within a stage
        sid = str(story.get("id", ""))

        if stage == "queued":
            # Gated at `queued -> dispatch_se` ONLY. A unit already in
            # in_progress/testing/reviewing has started, and retro-blocking it
            # would strand built work and deadlock `reviewing -> done` behind a
            # merge agents are forbidden to perform.
            for cand in candidates:
                cand_id = str(cand.get("id", ""))
                dep = story_dep_status(state, cand, dep_context=dep_context)
                if dep["met"]:
                    # A VERIFY-ONLY WORK UNIT HAS NO PRODUCING STAGE (#495 P2,
                    # proposal section 5 S4). Its deliverable was produced
                    # outside the platform and admitted at intake, so selecting
                    # the software-engineer here would dispatch a producer to
                    # produce what already exists -- and, because
                    # `evaluate_dispatch` authorizes only the role
                    # `next_action` selected, it would leave the VERIFYING
                    # stage unable to be authorized at all. That is what made a
                    # hand-written verifier receipt indistinguishable from a
                    # dispatched one: there was no authorized attempt for it to
                    # name.
                    #
                    # GATED ON AN ADMITTED CANDIDATE, so no unit that exists
                    # today changes behaviour: a queued unit with no intake
                    # record still selects `dispatch_se`. And this is a
                    # SELECTION change only -- it adds no transition, so a
                    # queued unit still cannot reach a later state without the
                    # receipt its edge already demands. A verify-only unit
                    # skipping production is a property of what was admitted,
                    # never of an edge anyone else can take.
                    if cand_id in (verify_only or ()):
                        return _fill(
                            cand,
                            action="dispatch_qe",
                            role="qe",
                            reason=(
                                f"{cand_id} is queued with an external "
                                "candidate admitted at intake and no producing "
                                "stage — dispatch the quality-engineer to "
                                "verify the admitted bytes"
                            ),
                        )
                    return _fill(
                        cand,
                        action="dispatch_se",
                        role="se",
                        reason=(
                            f"{cand_id} is queued — dispatch the "
                            "software-engineer to start it"
                        ),
                    )
                if dependency_gate == "warn":
                    filled = _fill(
                        cand,
                        action="dispatch_se",
                        role="se",
                        reason=(
                            f"{cand_id} is queued with UNMET dependencies "
                            f"({format_unmet_deps(dep['unmet'])}) but "
                            "resilience.dependency_gate is 'warn' — dispatching "
                            "anyway"
                        ),
                    )
                    filled["deps_warning"] = dep["unmet"]
                    return filled
                deps_held.append({"story_id": cand_id, "unmet": dep["unmet"]})
            continue  # every queued candidate is held; try the next stage

        # P5 verification loop: a fresh receipt that FAILED verification is
        # re-dispatched (ladder permitting) rather than advanced on. When the
        # H3-F1 ladder is exhausted (tier block), the story is BLOCKED and the
        # loop moves on (#81) — NOT advanced, which would waste a CR/DoD cycle
        # on a known-failed receipt. At the mature/release tiers an
        # UNVERIFIABLE receipt (evidence present but not proof — #106 string
        # commands) loops too (issue #179 E6a): the DoD gate would reject it
        # anyway, so catching it at the stage saves a full stage-cycle.
        # Returns the recovery dict (tier may be block) or None when no
        # verification loop applies.
        def _verify_loop(role: str) -> dict[str, Any] | None:
            if not verification_loops:
                return None
            # #406 — pass the story so the loop's `tests_pass` definition is
            # the gate's. See `_receipt_verification_verdict`'s docstring.
            verdict = _receipt_verification_verdict(
                receipts_dir, sid, role, story=story
            )
            if verdict is None:
                return None
            if verdict == "unverified" and intensity not in ("mature", "release"):
                return None
            return {
                **recommend_recovery_action(state, sid, role),
                "role": role,
                "verdict": verdict,
            }

        # #690. THE OTHER REASON A FRESH RECEIPT IS NOT ADVANCEABLE, and the
        # one nothing here could see. A canonical receipt whose bound attempt
        # the board records as cancelled, expired or failed is refused by
        # `advance_kernel.evaluate_advance` as `attempt_not_live`. Offering
        # the ordinary transition anyway is what deadlocked the governed loop:
        # the refusal asks for a new attempt, and `begin_dispatch` refuses to
        # mint one while the receipt is still there.
        #
        # It routes through the SAME H3-F1 ladder the verification loop uses
        # -- the issue says "according to the retry ladder" and this is the
        # one that exists -- because that is what makes the successor
        # reachable: `evaluate_dispatch` waives `receipt_already_present` for
        # a recovery, and `execute_dispatch` archives the receipt under the
        # dead attempt before minting the next one. A second recovery concept
        # would have been a second ladder.
        #
        # NOT gated on `resilience.verification_loops`. That toggle decides
        # whether a receipt's own VERDICT re-dispatches a stage; this is not a
        # verdict about the work at all, it is the board reporting that the
        # execution which produced it is over. With the toggle off there is
        # still no legal transition, so skipping the recovery would restore
        # the dead end rather than preserve a behaviour.
        def _attempt_loop(role: str) -> dict[str, Any] | None:
            dead = _unadvanceable_attempt(story, role)
            if dead is None:
                return None
            return {
                **recommend_recovery_action(state, sid, role),
                "role": role,
                "verdict": ATTEMPT_NOT_LIVE_VERDICT,
                **dead,
            }

        def _verify_action(role: str, vl: dict[str, Any], stage_label: str) -> dict[str, Any]:
            agent = {"se": "software-engineer", "qe": "quality-engineer",
                     "cr": "code-reviewer"}[role]
            verdict = vl.get("verdict")
            if verdict == ATTEMPT_NOT_LIVE_VERDICT:
                problem = (
                    "is bound to attempt %s, which the board records as %s (%s), "
                    "so it cannot be advanced on"
                    % (
                        vl.get("attempt_id") or "an unnamed attempt",
                        vl.get("attempt_state") or "not live",
                        vl.get("failure_class") or "no failure class recorded",
                    )
                )
                loop = "attempt recovery; the failed attempt and this receipt are kept"
            elif verdict == "failed":
                problem = "failed verification"
                loop = "verification loop"
            else:
                problem = "carries unverifiable evidence (intent, not proof)"
                loop = "verification loop"
            if vl["tier"] == RETRY_TIER_BLOCK:
                return _fill(
                    story,
                    action="block_story",
                    role=role,
                    receipt_present=True,
                    transition_to="blocked",
                    recovery=vl,
                    reason=(
                        f"{sid} {stage_label} receipt {problem} and the "
                        f"H3-F1 ladder is exhausted — transition to blocked and move on"
                    ),
                )
            return _fill(
                story,
                action=f"dispatch_{role}",
                role=role,
                receipt_present=True,
                recovery=vl,
                reason=(
                    f"{sid} {stage_label} receipt {problem} — re-dispatch "
                    f"the {agent} ({vl['tier']}); {loop}"
                ),
            )

        if stage == "reviewing":
            has_cr = _fresh_receipt(receipts_dir, sid, "cr", story, "reviewing")
            if cr_required and not has_cr:
                return _fill(
                    story,
                    action="dispatch_cr",
                    role="cr",
                    reason=(
                        f"{sid} is in reviewing; code_reviewed is required at "
                        f"intensity '{intensity}' and no {sid}-cr.json receipt found"
                    ),
                )
            if has_cr:
                # Attempt liveness FIRST. A dead attempt's receipt cannot
                # advance whatever its verdict says, so asking the verdict
                # first would report the second-order problem and route the
                # operator into an edge that is refused either way.
                _al = _attempt_loop("cr")
                if _al:
                    return _verify_action("cr", _al, "review")
                _vl = _verify_loop("cr")
                if _vl:
                    return _verify_action("cr", _vl, "review")
            if per_story_acceptance:
                return _fill(
                    story,
                    action="request_acceptance",
                    role=None,
                    receipt_present=has_cr,
                    transition_to="awaiting_acceptance",
                    reason=(
                        f"{sid} finished review; per-story acceptance is on — "
                        "transition to awaiting_acceptance for the PO walk"
                    ),
                )
            # A GATE THAT ALREADY REFUSED THIS PROMOTION (#396). Under a
            # `refuse` policy the unit stays in `reviewing`, so without this
            # the board advertised `promote_story -> done` again and a
            # compliant orchestrator retried an action that cannot succeed,
            # forever. The FIRST pass still advertises the promotion: the DoD
            # gate is what should refuse it, with its own typed reason, and it
            # cannot do that if the board never offers the edge.
            _refused = _dod_gate_refusal(
                story, receipt_evidence_digest(receipts_dir, sid)
            )
            if _refused:
                _owed: dict[str, str] = {}
                if isinstance(compliance, dict):
                    _who = compliance_accountable_role(compliance.get("build_mode"))
                    _owed = {cid: _who for cid in COMPLIANCE_CHECKS}
                _gate = _gate_remediation(
                    {"blocked_reason": _refused}, accountable=_owed
                )
                if _gate:
                    return _fill(
                        story,
                        action="recover_blocked",
                        role=_gate["role"],
                        recovery=_gate,
                        receipt_present=has_cr,
                        reason=(
                            f"{sid} cannot be promoted: DoD gate "
                            f"'{_gate['gate']}' refused it, so the transition "
                            f"would be refused again. Dispatch {_gate['role']} "
                            "per the gate reason, then advance (NOT an SE "
                            f"retry). {_refused}"
                        ),
                    )
            return _fill(
                story,
                action="promote_story",
                role=None,
                receipt_present=has_cr,
                transition_to="done",
                reason=(
                    f"{sid} finished review — transition to done "
                    "(DoD evaluation auto-fires on the transition)"
                ),
            )

        # in_progress / testing
        action, role, next_state = _STAGE_DISPATCH[stage]
        if _fresh_receipt(receipts_dir, sid, role, story, stage):
            # #690: a receipt whose bound attempt is no longer live is not
            # advanceable at all, so that is asked BEFORE the verdict. The
            # stage this branch used to end in -- "transition instead of
            # re-dispatching" -- is precisely the recommendation `advance`
            # then refused.
            _al = _attempt_loop(role)
            if _al:
                return _verify_action(role, _al, stage)
            # P5: if the fresh receipt FAILED verification, re-dispatch this
            # stage (ladder permitting) or block it (ladder exhausted) instead
            # of advancing on a bad receipt.
            _vl = _verify_loop(role)
            if _vl:
                return _verify_action(role, _vl, stage)
            # The stage's agent already delivered a receipt POST-DATING this
            # stage entry but the transition never landed (crash mid-
            # iteration). Advancing is the recovery, not another dispatch. A
            # stale receipt (older than the stage entry — e.g. a needs-fix
            # reset) is treated as absent so the re-work actually re-runs.
            return _fill(
                story,
                action=action,
                role=role,
                receipt_present=True,
                transition_to=next_state,
                reason=(
                    f"{sid} is in {stage} and {sid}-{role}.json already exists — "
                    f"transition to {next_state} instead of re-dispatching"
                ),
            )
        agent = {"se": "software-engineer", "qe": "quality-engineer"}[role]
        return _fill(
            story,
            action=action,
            role=role,
            reason=f"{sid} is in {stage}; no {sid}-{role}.json receipt — dispatch the {agent}",
        )

    # No dispatchable work. Blocked stories next.
    blocked = list_stories_by_state(state, "blocked")
    if blocked:
        # 1. Gate-blocked stories route to the gate's remediation agent
        #    (compliance-engineer / QE runtime / QE browser-qa) — never the
        #    SE retry ladder, which can't clear a conditional gate and would
        #    loop. Highest priority: the reason names the exact next step.
        # #487 — the compliance gate's remediation role follows the
        # declaration, so a PHI-blocked SPQ unit routes to the stage that
        # owes the result rather than to a per-unit compliance-engineer
        # dispatch SPQ does not schedule. Empty for every caller that passes
        # no compliance inputs, which leaves `_GATE_REMEDIATION_ROLE`
        # answering unchanged.
        _gate_accountable: dict[str, str] = {}
        if isinstance(compliance, dict):
            _owner = compliance_accountable_role(compliance.get("build_mode"))
            _gate_accountable = {cid: _owner for cid in COMPLIANCE_CHECKS}
        for s in blocked:
            gate = _gate_remediation(s, accountable=_gate_accountable)
            if gate:
                return _fill(
                    s,
                    action="recover_blocked",
                    role=gate["role"],
                    recovery=gate,
                    reason=(
                        f"{s.get('id')} is blocked on DoD gate '{gate['gate']}' — "
                        f"unblock and dispatch {gate['role']} per the gate reason "
                        "(NOT an SE retry)"
                    ),
                )
        # 2. Retry-ladder-blocked stories the H3-F1 ladder can still recover.
        recoverable = []
        for s in blocked:
            role = _blocked_recovery_role(s)
            rec = recommend_recovery_action(state, str(s.get("id", "")), role)
            if rec["tier"] != RETRY_TIER_BLOCK:
                recoverable.append((s, role, rec))
        if recoverable:
            story, role, rec = recoverable[0]
            return _fill(
                story,
                action="recover_blocked",
                role=role,
                recovery={**rec, "role": role},
                reason=(
                    f"{story.get('id')} is blocked and the recovery ladder allows "
                    f"{rec['tier']} for role '{role}' — unblock and re-dispatch"
                ),
            )
        if counts["awaiting_acceptance"] == 0 and (
            counts["done"] + counts["cancelled"] + counts["blocked"] == len(stories)
        ):
            out.update(
                action="all_blocked",
                reason=(
                    f"{counts['blocked']} blocked stor"
                    f"{'y has' if counts['blocked'] == 1 else 'ies have'} exhausted "
                    "the recovery ladder — human decision needed"
                ),
                human_gate_pending=True,
                # #179 E5 — human gates carry the run data behind the verdict.
                evidence=gate_evidence_digest(receipts_dir, blocked),
            )
            return out

    if counts["awaiting_acceptance"] > 0:
        awaiting = list_stories_by_state(state, "awaiting_acceptance")
        return _fill(
            awaiting[0],
            action="await_acceptance",
            reason=(
                f"{counts['awaiting_acceptance']} stor"
                f"{'y awaits' if counts['awaiting_acceptance'] == 1 else 'ies await'} "
                "PO acceptance — human gate, do not auto-continue"
            ),
            human_gate_pending=True,
            # #179 E5 — the PO walk sees what each story actually proved
            # (commands + exit codes, artifacts, runtime/UI verification).
            evidence=gate_evidence_digest(receipts_dir, awaiting),
        )

    # #304. Ordered after `awaiting_acceptance` because accepting a unit is
    # precisely what unblocks its dependents, and before `sprint_complete`
    # (which cannot be true with queued units present, but stating the order
    # explicitly keeps it correct under future edits).
    if deps_held:
        out.update(
            action="deps_blocked",
            reason=(
                "%d queued Work Unit%s cannot start: %s"
                % (
                    len(deps_held),
                    "" if len(deps_held) == 1 else "s",
                    "; ".join(
                        "%s <- %s"
                        % (h["story_id"], format_unmet_deps(h["unmet"], limit=1))
                        for h in deps_held[:3]
                    ),
                )
            ),
            dependencies_held=deps_held,
            human_gate_pending=True,
            # Every unmet edge is one of: waiting on a sibling, a cut upstream,
            # a typo, or a workstream that has not pushed. All four are human
            # decisions, so this carries the same run data as the other gates.
            evidence=gate_evidence_digest(
                receipts_dir,
                [
                    s
                    for s in (
                        get_story(state, h["story_id"]) for h in deps_held
                    )
                    if s
                ],
            ),
        )
        return out

    if counts["done"] + counts["cancelled"] == len(stories):
        out.update(
            action="sprint_complete",
            reason=(
                f"all {len(stories)} stories terminal "
                f"({counts['done']} done, {counts['cancelled']} cancelled)"
            ),
        )
        return out

    # Unreachable with a well-formed board, but a story in an unknown
    # sub-state must not crash the dispatcher.
    unknown = [s for s in stories if s.get("state") not in STORY_STATES]
    out.update(
        action="all_blocked",
        reason=(
            "board is in an inconsistent state "
            f"(unknown sub-states: {[s.get('state') for s in unknown]}) — "
            "human review needed"
        ),
        human_gate_pending=True,
        # #179 E5 — every all_blocked human gate carries run data, this
        # inconsistent-board fallback included.
        evidence=gate_evidence_digest(receipts_dir, unknown),
    )
    return out


# ---------------------------------------------------------------------------
# DoD Evaluation
# ---------------------------------------------------------------------------

def determine_dod_intensity(
    sprint_number: int | None = None,
    ticket_number: int | None = None,
    is_release: bool = False,
) -> str:
    """Determine the DoD intensity tier.

    Args:
        sprint_number: Current sprint (Scrum). None for Kanban.
        ticket_number: Cumulative ticket count (Kanban or cross-sprint).
        is_release: True forces max depth.

    Returns:
        Tier name: "early" | "growing" | "mature" | "release"
    """
    if is_release:
        return "release"

    # Use sprint number for Scrum, ticket number for Kanban.
    n = sprint_number if sprint_number is not None else (ticket_number or 1)

    if n <= 1:
        return "early"
    elif n <= 3:
        return "growing"
    else:
        return "mature"


def resolve_dod_tier(
    project_dir: str,
    state: dict[str, Any],
    is_release: bool = False,
) -> dict[str, Any]:
    """Resolve the sprint's DoD tier with explicit-decision precedence (#134 GAP-11).

    Order: recorded planning decision (`state["dod_tier"]`, written by
    `set_dod_tier` at Sprint Planning) → `.synaptory.yaml quality.dod_tier`
    override → computed `determine_dod_intensity` (unchanged pure function).

    `tier_source: "computed"` on the output is the silent-promotion tell —
    Sprint Planning is instructed to record the decision, so a computed
    source mid-sprint means the tier was never announced.
    """
    if is_release:
        return {
            "tier": "release",
            "tier_source": "computed",
            "active_base_checks": list(DOD_TIER_CHECKS["release"]),
        }
    rec = state.get("dod_tier")
    if isinstance(rec, dict) and rec.get("tier") in DOD_TIER_CHECKS:
        return {
            "tier": rec["tier"],
            "tier_source": "planned",
            "decided_by": rec.get("decided_by"),
            "decided_at": rec.get("decided_at"),
            "sprint": rec.get("sprint"),
            "active_base_checks": list(DOD_TIER_CHECKS[rec["tier"]]),
        }
    cfg_tier = _config_dod_tier(project_dir)
    if cfg_tier:
        return {
            "tier": cfg_tier,
            "tier_source": "config",
            "active_base_checks": list(DOD_TIER_CHECKS[cfg_tier]),
        }
    tier = determine_dod_intensity(
        # See the note in next_action: SPQ has no `current_sprint`.
        state.get("current_sprint") or state.get("current_cycle"),
        state.get("cumulative_ticket_number"),
        False,
    )
    return {
        "tier": tier,
        "tier_source": "computed",
        "active_base_checks": list(DOD_TIER_CHECKS[tier]),
    }


def set_dod_tier(
    state: dict[str, Any],
    tier: str,
    decided_by: str,
    reason: str = "",
    sprint: int | None = None,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Record the sprint's DoD tier as an explicit planning decision (#134 GAP-11).

    Writes the decision record onto the state (caller persists) and emits a
    best-effort gate event through the existing `gate_emitter` channel so the
    decision is recorded with requester/decider/reason (REQ-G-005 shape; a
    dedicated `dod_tier` gate_type is a CP coordination item, so this rides
    the `evidence_dod / opened` event against the project target).
    """
    if tier not in DOD_TIER_CHECKS:
        raise ValueError(
            f"Invalid DoD tier {tier!r}; expected one of {sorted(DOD_TIER_CHECKS)}"
        )
    record = {
        "tier": tier,
        "decided_by": decided_by,
        "reason": reason or "",
        "sprint": sprint if sprint is not None else state.get("current_sprint"),
        "decided_at": _now(),
    }
    state["dod_tier"] = record
    _emit_project_event(
        "dod_tier_set",
        project_dir=project_dir,
        tier=tier,
        decided_by=decided_by,
        sprint=record["sprint"],
    )
    try:
        from gate_emitter import emit_gate_event

        emit_gate_event(
            gate_type="evidence_dod",
            target_type="project",
            target_id=os.path.basename(os.path.abspath(project_dir or ".")),
            state="opened",
            decided_by=decided_by,
            decision_role="delivery_owner",
            reason=(
                f"DoD tier for sprint {record['sprint']}: {tier} "
                f"(checks: {', '.join(DOD_TIER_CHECKS[tier])})"
                + (f" — {reason}" if reason else "")
            ),
            project_dir=project_dir,
        )
    except Exception:
        pass  # gate emission is best-effort, never blocks planning
    return record


def _story_is_ui_bearing_in_state(project_dir: str, story_id: str) -> bool:
    """Resolve whether a story is UI-bearing from the pipeline state (#44).

    Prefers the `ui_bearing` flag snapshotted by `create_story`. For older
    records written before #44 (no flag), derives it from any stored
    `acceptance_criteria` / `title`. Defensive: any failure → False so the
    gate never crashes a transition for non-UI work.
    """
    try:
        state = _read_state(project_dir)
        story = _find_story(state, story_id)
    except Exception:
        return False
    if not story:
        return False
    # #134 GAP-4: a PO-declared non-UI kind (enabler/infra/backend) suppresses
    # the keyword heuristic AND any stale snapshot flag — checked first so a
    # kind assigned after story creation still wins.
    if str(story.get("kind") or "").strip().lower() in _NON_UI_KINDS:
        return False
    if "ui_bearing" in story:
        return bool(story["ui_bearing"])
    return story_is_ui_bearing(
        story.get("title", ""), story.get("acceptance_criteria")
    )


def _story_claims_integration_in_state(
    project_dir: str, story_id: str, receipts: list[dict[str, Any]]
) -> bool:
    """Resolve whether a story claims an external integration (#44 phase 5).

    Reads the story's title/ACs from pipeline state and folds in the receipts'
    task/summary text, then delegates to `story_claims_integration`. Defensive:
    any state-read failure falls back to receipt text only, so the gate never
    crashes a transition.
    """
    title, acs = "", None
    try:
        story = _find_story(_read_state(project_dir), story_id)
        if story:
            title = story.get("title", "")
            acs = story.get("acceptance_criteria")
    except Exception:
        pass
    return story_claims_integration(receipts, title, acs)


# #144 — gates a story may explicitly mark not-applicable via a
# `dod_not_applicable` list on its state record. Deliberately narrow: only the
# build gate is waivable, so the mechanism can never be used to dodge a
# security / runtime / UI / integration gate. Fail-closed — absent the flag
# every gate stays required exactly as before.
_WAIVABLE_DOD_CHECKS = {"build_succeeds"}


def _dod_check_waived(project_dir: str, story_id: str, check_id: str) -> bool:
    """True if the story explicitly marks ``check_id`` not-applicable (#144).

    A genuinely code-free story (e.g. a manual audit that produces only a
    markdown doc) can list waivable gate ids in its `dod_not_applicable`
    state field so the gate resolves to N/A instead of failing as an
    unsourceable required check. Opt-in and fail-closed: only
    ``_WAIVABLE_DOD_CHECKS`` can be waived, and any read failure / absent
    list leaves every gate required.
    """
    if check_id not in _WAIVABLE_DOD_CHECKS:
        return False
    try:
        story = _find_story(_read_state(project_dir), story_id)
    except Exception:
        return False
    if not story:
        return False
    waived = story.get("dod_not_applicable") or []
    return isinstance(waived, list) and check_id in waived


def receipt_evidence_digest(receipts_dir: str | None, story_id: str) -> str:
    """A cheap fingerprint of the evidence a story currently has.

    Name, size and mtime of each of the story's receipts. It changes whenever a
    receipt is written, which is exactly when a previous DoD verdict stops
    being about the evidence on disk. Deliberately not a content hash: this
    runs on every `next_action` and only has to answer "is this the same
    evidence", not "what does it say".
    """
    if not receipts_dir:
        return ""
    try:
        base = Path(receipts_dir)
        parts = []
        for path in sorted(base.glob("%s-*.json" % story_id)):
            st = path.stat()
            parts.append("%s:%d:%d" % (path.name, st.st_size, st.st_mtime_ns))
    except OSError:
        return ""
    if not parts:
        return ""
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def _dod_gate_refusal(story: Any, evidence: str) -> str:
    """The reason a DoD gate refused THIS evidence, or "".

    Written by the advance kernel when its policy is `refuse`: the story does
    not move, and the board learns why so it can route to the agent that owes
    the missing result instead of re-offering the promotion (#396).

    THE NOTE IS SCOPED TO THE EVIDENCE IT JUDGED. A refusal that outlived the
    condition it described would be worse than the loop it fixes: an engineer
    who dispatched the remediation, produced the missing result and came back
    would find the board still routing to remediation and the advance refused
    as `next_action_mismatch`, with no way to clear it. So the note records the
    evidence fingerprint it was computed from, and a note about evidence that
    has since changed is ignored rather than obeyed. The Cursor MCP suite
    caught exactly this: fix the coverage, re-advance, and the stale note
    refused the legitimate retry.
    """
    if not isinstance(story, dict):
        return ""
    note = story.get("dod_gate_refusal")
    if not isinstance(note, dict):
        return ""
    recorded = str(note.get("evidence") or "")
    if recorded and evidence and recorded != evidence:
        return ""
    return str(note.get("reason") or "")


def story_gate_promotions(
    project_dir: str,
    story_id: str,
    receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    """The conditional-gate promotion flags for a story, given the receipts so far.

    Extracted from `evaluate_story_dod` so the DISPATCH side (#501) and the
    GATE side compute promotion from one body of code. Two copies of this
    logic would let a dispatch prompt state a check set the gate does not
    use, which is a worse failure than stating none at all: the agent would
    be told a contract, satisfy it, and still be blocked.

    `receipts` is the evidence available AT THE MOMENT OF THE CALL. At the
    gate that is every receipt for the story; at dispatch it is whatever the
    earlier stages already wrote (nothing, for an SE dispatch). Two of the
    flags read receipts, so the result is monotonic rather than final — see
    `dispatch_dod_contract`, which reports that difference instead of hiding
    it.
    """
    # #31/#30 — promote the conditional gates for PHI-sensitive healthcare
    # stories (compliance-engineer must run; story must be exercised live) and
    # for any project that opts into mandatory runtime verification.
    baa_enforced = healthcare_baa_enforced(project_dir)
    phi_touching = baa_enforced and story_touches_phi(
        receipts, phi_risk_signals(project_dir)
    )
    # #134 GAP-3 — per-story verification tier. PHI always forces `full`:
    # scoping may reduce intensity, never the compliance class.
    story_rec: dict[str, Any] | None = None
    _state_for_manifest: dict[str, Any] | None = None
    try:
        _state_for_manifest = _read_state(project_dir)
        story_rec = _find_story(_state_for_manifest, story_id)
    except Exception:
        story_rec = None
    # #406 — the authored set comes from the hash-SEALED Cycle manifest, not
    # from the board. `pipeline-state.json` is agent-writable, so a board-only
    # authority would have made the authored set exactly as forgeable as the
    # receipt it exists to check. This also means an unreadable board or a
    # story that has left it no longer silently reverts to the pre-#406 rules,
    # since `manifest_authored_cases` can resolve the Cycle from the index.
    story_rec = _authoritative_story(
        project_dir, story_rec or {"id": story_id}, _state_for_manifest
    )
    verification_tier = resolve_verification_tier(project_dir, story_rec)
    if phi_touching:
        verification_tier = "full"
    return {
        "baa_enforced": baa_enforced,
        # The manifest-authoritative story record (#406). Returned rather than
        # recomputed by the caller: `_authoritative_story` can resolve a story
        # that has left the board, so a second lookup would not be the same
        # record, and `evaluate_story_dod` keys the authored-case checks off it.
        "story_rec": story_rec,
        "verification_tier": verification_tier,
        "compliance_required": phi_touching,
        "runtime_required": phi_touching or verification_tier == "full",
        # #44 — promote the UI-acceptance gate for stories whose AC describes
        # a user-facing screen, so a green backend suite can't carry it to
        # `done`. #134: `minimal`-tier stories suppress the gate entirely (PHI
        # can't be minimal — forced to full above).
        "ui_required": (
            verification_tier != "minimal"
            and _story_is_ui_bearing_in_state(project_dir, story_id)
        ),
        # #44 phase 5 — promote the integration gate when the story CLAIMS an
        # external service is wired/connected, so a stub against placeholder
        # creds can't be reported as a delivered integration.
        "integration_required": _story_claims_integration_in_state(
            project_dir, story_id, receipts
        ),
    }


#: Conditional gates whose promotion depends on receipt evidence, mapped to the
#: sentence that says what would still activate them. A gate listed here and NOT
#: in `active_checks` at dispatch time is undetermined, not inactive — see
#: `dispatch_dod_contract`.
#: Kept to short fragments on purpose: this text is rendered into every
#: dispatch, and the fixed-payload budget (#404) leaves single-digit words of
#: headroom. The reason has to fit in a clause or it does not ship at all.
_RECEIPT_DEPENDENT_PROMOTIONS: dict[str, str] = {
    "no_critical_findings": "if a receipt shows PHI",
    "runtime_verified": "if a receipt shows PHI",
    "integration_verified": "if you claim a service is live",
}


def dispatch_dod_contract(
    project_dir: str,
    story_id: str,
    *,
    receipts_dir: str | None = None,
    state: dict[str, Any] | None = None,
    tier: str | None = None,
) -> dict[str, Any]:
    """The DoD block a dispatch prompt should state for `story_id` (#501).

    Returns `{tier, tier_source, active_checks, undetermined_checks}`.

    `active_checks` is the check set the gate would use if it ran right now:
    the tier's base checks plus every conditional gate already promoted by
    the story record and the receipts written so far, computed by the SAME
    `story_gate_promotions` the gate itself calls.

    `undetermined_checks` exists because two promotions read receipts that do
    not exist yet at dispatch — PHI detection (`no_critical_findings`,
    `runtime_verified`) and the integration claim (`integration_verified`).
    Shipping only `active_checks` would present "not promoted yet" and "will
    never be promoted" as the same fact, and the agent would read a shorter
    list as a lighter contract. Each entry is `{check, why}`: what activates
    it, so the agent can tell an absent check from an unmeasured one.

    `tier` (optional) pins the base-check tier to one the caller already
    resolved, so the contract cannot state a check the caller's own routing
    did not plan for. `tier_source` is unaffected — see the comment below.

    Never raises: a dispatch prompt losing its DoD block is bad, a dispatch
    that cannot be issued is worse. On failure the tier falls back to the
    computed default and `undetermined_checks` says the promotions were not
    evaluated — which is the honest reading, not an empty list.
    """
    try:
        if state is None:
            state = _read_state(project_dir)
    except Exception:
        state = {}
    try:
        tier_info = resolve_dod_tier(project_dir, state or {})
    except Exception:
        tier_info = {"tier": "early", "tier_source": "computed"}
    # `tier` pins WHICH TIER'S base checks are selected, to the tier the caller
    # already routed on (`attach_dispatch_dod_contract` passes `next_action`'s).
    # Resolving it a second time here would let the stated contract carry
    # `code_reviewed` while the same output's routing never asked for a CR --
    # the two must move together or the prompt describes a pipeline the
    # orchestrator is not running. It does NOT override `tier_source`, which
    # answers a different question (was the tier a recorded planning decision,
    # or silently computed?) and which the modes files act on.
    if tier not in DOD_TIER_CHECKS:
        tier = str(tier_info.get("tier") or "early")
    out: dict[str, Any] = {
        "tier": tier,
        "tier_source": str(tier_info.get("tier_source") or "computed"),
        "active_checks": list(DOD_TIER_CHECKS.get(tier, DOD_TIER_CHECKS["early"])),
        "undetermined_checks": [],
    }
    try:
        if receipts_dir is None:
            receipts_dir = _resolve_receipts_dir(project_dir)
        receipts = collect_story_receipts(receipts_dir, story_id)
        promotions = story_gate_promotions(project_dir, story_id, receipts)
    except Exception:
        out["undetermined_checks"] = [
            {"check": check, "why": "promotion could not be evaluated"}
            for check in sorted(_RECEIPT_DEPENDENT_PROMOTIONS)
        ]
        return out

    active = active_dod_checks(
        tier,
        compliance_required=promotions["compliance_required"],
        runtime_required=promotions["runtime_required"],
        ui_required=promotions["ui_required"],
        integration_required=promotions["integration_required"],
    )
    out["active_checks"] = list(active)
    out["verification_tier"] = promotions["verification_tier"]

    undetermined: list[dict[str, str]] = []
    for check, why in _RECEIPT_DEPENDENT_PROMOTIONS.items():
        if check in active:
            continue
        # A non-healthcare project can never promote the PHI pair, so those
        # two are DETERMINED absent there. Listing them anyway would turn the
        # honest signal into noise on every ordinary project.
        if check != "integration_verified" and not promotions["baa_enforced"]:
            continue
        undetermined.append({"check": check, "why": why})
    out["undetermined_checks"] = undetermined
    return out


#: The keys `attach_dispatch_dod_contract` OVERWRITES on a `next_action` `dod`
#: block. `declared_checks` is unioned rather than overwritten (it must stay a
#: superset), and `tier` / `tier_source` / the SPQ `compliance` stanza (#487)
#: are left alone because they answer questions the per-story contract does not.
_DISPATCH_DOD_KEYS = ("active_checks", "undetermined_checks", "verification_tier")


def attach_dispatch_dod_contract(
    out: dict[str, Any],
    project_dir: str,
    *,
    state: dict[str, Any] | None = None,
    receipts_dir: str | None = None,
) -> dict[str, Any]:
    """Upgrade a `next_action` output's `dod` block to the PER-STORY contract.

    `next_action` is a pure function of the board, so the `dod` block it builds
    can only carry the STATIC tier list (`DOD_TIER_CHECKS`). The gate does not
    stop there: `active_dod_checks` promotes four CONDITIONAL gates
    (`ui_acceptance`, `runtime_verified`, `no_critical_findings`,
    `integration_verified`) from `.synaptory.yaml` and the receipts, and
    `evaluate_story_dod` enforces the promoted set. Every modes file and the
    backend wrapper tell the orchestrator to paste `dod.active_checks` into the
    dispatch prompt as the Evidence Contract, so the tier-only block made the
    STATED contract systematically narrower than the ENFORCED one for exactly
    the stories that need it widest: UI-bearing ones, `verification_tier: full`
    ones, PHI-touching ones on a `baa_enforced` project, and any story claiming
    an external integration.

    The fix belongs HERE rather than inside `next_action` because this is the
    seam where a `project_dir` exists. All four orchestrator entry points --
    `story_pipeline.py next_action`, and the `next_action(project_dir)` wrapper
    in each of the scrum / kanban / spq state machines -- are already impure and
    already enrich the result (`spec_id`, `resume_candidate`, the compliance
    declaration). Routing through one helper keeps them from drifting: a
    lifecycle that forgot to call this would silently keep the narrow contract,
    which is the bug rather than a smaller version of it.

    The tier is PINNED to the one `next_action` routed on, so the stated
    contract can never require a `code_reviewed` the same output's routing did
    not schedule a CR for.

    Parallel batch members (#134 GAP-6) each get their OWN `dod` block: a batch
    is N dispatches for N different stories, and `ui_bearing` is a per-story
    fact, so one shared block would hand the lead's contract to every member.

    Best-effort and in-place: any failure leaves the tier-only block exactly as
    `next_action` built it. A dispatch that states a narrow contract is a bug; a
    dispatch that cannot be issued at all is worse.
    """
    dod = out.get("dod")
    if not isinstance(dod, dict):
        return out
    tier = dod.get("tier") if isinstance(dod.get("tier"), str) else None

    cache: dict[str, dict[str, Any]] = {}

    def _contract(story_id: str) -> dict[str, Any] | None:
        if story_id in cache:
            return cache[story_id]
        try:
            resolved = dispatch_dod_contract(
                project_dir,
                story_id,
                receipts_dir=receipts_dir,
                state=state,
                tier=tier,
            )
        except Exception:
            return None
        cache[story_id] = resolved
        return resolved

    story_id = out.get("story_id")
    if isinstance(story_id, str) and story_id:
        resolved = _contract(story_id)
        if resolved is not None:
            for key in _DISPATCH_DOD_KEYS:
                if key in resolved:
                    dod[key] = resolved[key]
            # #487 built `declared_checks` as "the tier set PLUS the
            # determinable promotions" but only ever extended it with the
            # compliance check. Union it with the resolved set so the two stay
            # ordered the way their names claim -- declared is a superset of
            # active, never a stale subset of it.
            declared = list(dod.get("declared_checks") or [])
            for cid in resolved.get("active_checks") or []:
                if cid not in declared:
                    declared.append(cid)
            dod["declared_checks"] = declared

    parallel = out.get("parallel")
    if isinstance(parallel, dict):
        for entry in parallel.get("batch") or []:
            if not isinstance(entry, dict):
                continue
            member_id = entry.get("story_id")
            if not isinstance(member_id, str) or not member_id:
                continue
            resolved = _contract(member_id)
            if resolved is None:
                continue
            entry["dod"] = {
                "tier": resolved.get("tier", tier),
                "tier_source": dod.get("tier_source"),
                **{k: resolved[k] for k in _DISPATCH_DOD_KEYS if k in resolved},
            }
    return out


def evaluate_story_dod(
    project_dir: str,
    story_id: str,
    intensity: str,
    receipts_dir: str | None = None,
) -> dict[str, Any]:
    """Evaluate DoD checks for a completed story.

    Reads the story's receipts and evaluates each applicable DoD check.

    Returns:
        DoD result dict with per-check results and overall pass/fail.
    """
    if receipts_dir is None:
        receipts_dir = _resolve_receipts_dir(project_dir)

    receipts = collect_story_receipts(receipts_dir, story_id)

    promotions = story_gate_promotions(project_dir, story_id, receipts)
    phi_touching = promotions["compliance_required"]
    runtime_required = promotions["runtime_required"]
    ui_required = promotions["ui_required"]
    integration_required = promotions["integration_required"]
    verification_tier = promotions["verification_tier"]
    story_rec = promotions["story_rec"]
    baa_enforced = promotions["baa_enforced"]
    active_checks = active_dod_checks(
        intensity,
        compliance_required=phi_touching,
        runtime_required=runtime_required,
        ui_required=ui_required,
        integration_required=integration_required,
    )

    receipt_by_role: dict[str, dict] = {}
    for r in receipts:
        # Agent backends in v2.6.3 write `agent`, not `role` (#105). Accept
        # either field for backward compatibility with both shapes.
        role = r.get("agent") or r.get("role") or ""
        abbrev = _role_to_abbrev(role)
        if abbrev:
            receipt_by_role[abbrev] = r

    # #134 GAP-10 (generalizes #144): evidence-based sourcing. The nominal
    # role's receipt is tried first (unchanged primary behaviour). When it is
    # missing or unresolved, the check's `fallback` policy decides which other
    # same-story receipts may satisfy it — with STRICT proof matchers
    # (_evaluate_fallback_proof), so role is a preference and structured
    # evidence is the requirement.
    checks: dict[str, dict[str, Any]] = {}
    for check_id in DOD_CHECKS:
        check_def = DOD_CHECKS[check_id]
        required = check_id in active_checks
        role_abbrev = check_def["receipt_role"]
        receipt = receipt_by_role.get(role_abbrev)

        passed: bool | None = None
        source: str | None = None
        # Re-initialized per check on purpose: the detail branch below reads it
        # and a value left over from the previous check would be read as this
        # check's verdict.
        authored_veto = False

        if required and receipt:
            source = _receipt_source_path(story_id, role_abbrev, receipt)
            passed = _evaluate_check(
                check_id,
                receipt,
                verification_tier=verification_tier,
                story=story_rec,
            )
        elif not required:
            passed = None  # Not evaluated at this intensity.

        if required and passed is not True:
            fallback = check_def.get("fallback", "none")
            # #187 — a definitive authored test-case failure is a hard veto on
            # tests_pass: a green runner command carried by ANOTHER same-story
            # receipt (e.g. an SE pytest run) must not override a failed/blocked
            # or dropped authored case recorded by QE. Without this guard the
            # `any_with_proof` fallback re-opens the exact bypass the nominal
            # `_evaluate_check` closes.
            #
            # #406 asks the same question of the set authored at COMMIT rather
            # than of the receipt's own `total`. `authored_case_negative` is
            # deliberately NARROWER than the primary verdict: only an explicit
            # non-passing status vetoes across receipts, because a receipt
            # reporting a subset is silent about the rest and silence must not
            # let one receipt writer fail another's satisfied check (#445's
            # blocking direction). With no authored set it is the #187
            # predicate exactly, so one call covers both regimes.
            authored_veto = (
                check_id == "tests_pass"
                and any(authored_case_negative(story_rec or {}, r) for r in receipts)
            )
            if authored_veto:
                fallback = "none"
            if fallback != "none":
                if fallback == "review_roles_only":
                    candidates = [
                        ab for ab in check_def.get("preferred_roles", [])
                        if ab != role_abbrev
                    ]
                else:  # any_with_proof
                    preferred = check_def.get("preferred_roles", [role_abbrev])
                    candidates = [
                        *(ab for ab in preferred if ab != role_abbrev),
                        *(ab for ab in receipt_by_role
                          if ab != role_abbrev and ab not in preferred),
                    ]
                for ab in candidates:
                    r = receipt_by_role.get(ab)
                    if r is None:
                        continue
                    if _evaluate_fallback_proof(
                        check_id,
                        r,
                        verification_tier=verification_tier,
                        story=story_rec,
                    ) is True:
                        passed = True
                        source = _receipt_source_path(story_id, ab, r)
                        break

        entry: dict[str, Any] = {
            "required": required,
            "passed": passed,
            "source": source,
        }
        # Self-explaining gate verdicts (#134 GAP-1): when the check is
        # required but unresolvable, say exactly what evidence shape is
        # missing so the orchestrator's retry prompt can teach the agent.
        #
        # #403 moved the unresolvable case to the typed classification pass
        # below, which produces the same text through `criteria_gap_detail`
        # for EVERY required check rather than for these two, and stamps the
        # `criteria_gap_declared` result beside it. The two remaining branches
        # are definitive negatives, which are fails and not gaps: evidence
        # exists and it says no.
        if required and passed is False and check_id == "code_reviewed":
            entry["detail"] = (
                "code_reviewed failed: the CR receipt must carry top-level "
                '`"status": "complete"` (or `story_dod.code_reviewed: true`).'
            )
        elif required and passed is not True and check_id == "tests_pass":
            # #406 — say WHICH of the three things is wrong (a failed case, a
            # dropped case, or an execution that pre-dates the producing
            # stage), because the retry prompt teaches from this text.
            #
            # The NOMINAL receipt is asked first and its answer wins. Scanning
            # in `collect_story_receipts` order would let an SE or CE receipt's
            # "carries no `metrics.authored_test_cases` object" be reported as
            # the reason tests_pass is unresolved while the QE receipt is the
            # one at fault, and the orchestrator would then re-dispatch the
            # wrong role.
            _ordered = [receipt] if receipt else []
            _ordered += [_r for _r in receipts if _r is not receipt]
            _neg_detail: str | None = None
            _gap_detail: str | None = None
            for _r in _ordered:
                _v, _d = authored_cases_verdict(story_rec or {}, _r)
                if _v is False and _d and _neg_detail is None:
                    _neg_detail = _d
                elif _v is None and _d and _gap_detail is None:
                    _gap_detail = _d
            # A cross-receipt veto with no detail of its own still has to say
            # something, and the #187 wording is what the legacy path says.
            if _neg_detail is None and authored_veto:
                _neg_detail = (
                    "tests_pass failed: an authored test case is failed/blocked "
                    "or dropped in `metrics.authored_test_cases`. A green "
                    "runner exit code from any receipt cannot clear this — "
                    "every authored case must end `passed` or "
                    "`not-applicable`."
                )
            if _neg_detail is not None:
                entry["detail"] = _neg_detail
                # #403 — a definitively failed authored case is negative
                # EVIDENCE, so this check is a `fail` and not a
                # `criteria_gap_declared`, even though `passed` is None
                # because no executed object was recorded. A gap says
                # "nothing here to evaluate"; this says "something here, and
                # it says no". Keeping them distinct is why the gap result is
                # worth having.
                entry["definitive_negative"] = True
            elif _gap_detail is not None:
                entry["detail"] = _gap_detail
        checks[check_id] = entry

    # #44 phase 5 — `integration_verified` spans the whole receipt set (the
    # `integrations[]` array may sit on the SE or the QE receipt), so evaluate
    # it across all receipts rather than the single nominal-role receipt the
    # loop picked.
    if checks.get("integration_verified", {}).get("required"):
        checks["integration_verified"]["passed"] = evaluate_integration_claims(receipts)
        _src_role = next(
            (
                ab for ab, r in receipt_by_role.items()
                if isinstance(r.get("integrations"), list) and r.get("integrations")
            ),
            None,
        )
        checks["integration_verified"]["source"] = (
            get_story_receipt_path(story_id, _src_role) if _src_role else None
        )

    # #144 — a genuinely code-free story (e.g. a manual audit producing only a
    # doc) has no build to prove. Honour an explicit per-story waiver so the
    # gate resolves to N/A instead of a hard unsourceable fail. (The #144
    # cross-role sourcing became the generic evidence fallback above — #134.)
    bs = checks.get("build_succeeds")
    if bs and bs["required"] and _dod_check_waived(
        project_dir, story_id, "build_succeeds"
    ):
        bs["required"] = False
        bs["passed"] = None
        bs["source"] = None
        bs["not_applicable"] = True
        bs.pop("detail", None)

    # #179 E1 — DoD-time evidence replay. The SubagentStop hook already
    # replays receipts through verification_runner.py (#163/#164/#167/#168:
    # allowlist, no shell, replay cache, worktree cwd) and BLOCKS in
    # structured mode — but the DoD gate itself still promoted on attested
    # exit codes: crash-recovery advances (`receipt_present`), interactive
    # mode (warn-only hook), and receipts written outside the hook path all
    # reach promotion unreplayed. With `resilience.evidence_replay: enabled`
    # the gate re-runs the satisfying receipt through the SAME runner (cache
    # makes this near-free when the tree hasn't changed since the hook's
    # replay) and a non-reproducing command flips the check to failed.
    # Unreplayable commands keep their attested verdict (REQ-E-003 — marked,
    # not silently accepted): replay narrows trust, it never widens it.
    _replayable = [
        cid for cid in ("tests_pass", "build_succeeds")
        if checks.get(cid, {}).get("required")
        and checks[cid].get("passed") is True
    ]
    if _replayable and _resilience_setting(project_dir, "evidence_replay") in (
        "enabled", "true", "on", "yes", "1"
    ):
        try:
            from verification_runner import run_verifications
        except ImportError:
            try:
                from hooks.lib.verification_runner import run_verifications
            except Exception:
                run_verifications = None  # partial install → keep attested verdicts
        except Exception:
            run_verifications = None
        if run_verifications is not None:
            _replay_by_src: dict[str, dict[str, Any]] = {}
            for cid in _replayable:
                entry = checks[cid]
                src = entry.get("source")
                if not src:
                    continue
                if src not in _replay_by_src:
                    _replay_by_src[src] = run_verifications(
                        os.path.join(receipts_dir, os.path.basename(src)),
                        str(project_dir),
                    ).to_dict()
                replay = _replay_by_src[src]
                entry["replay"] = {
                    "passed": replay["passed"],
                    "skipped": replay["skipped"],
                    "total": replay["total"],
                    "failures": replay["failures"],
                }
                if not replay["skipped"] and not replay["passed"]:
                    entry["passed"] = False
                    # Marker read by dod_gate_block_reason: a static check
                    # that PASSED on attested evidence but FAILED replay must
                    # BLOCK the reviewing→done transition, not merely record a
                    # false verdict (the transition only blocks on gates, so
                    # without this the story completes anyway).
                    entry["replay_mismatch"] = True
                    first_bad = replay["failures"][0] if replay["failures"] else {}
                    entry["detail"] = (
                        f"{cid} failed evidence replay: "
                        f"`{first_bad.get('command', '?')}` attested exit "
                        f"{first_bad.get('expected_exit_code')} but replayed "
                        f"exit {first_bad.get('actual_exit_code')} — the "
                        "attested evidence does not reproduce."
                    )
                    _emit_project_event(
                        "evidence_replay_mismatch",
                        project_dir=project_dir,
                        story_id=story_id,
                        check=cid,
                        source=src,
                        command=first_bad.get("command"),
                        attested_exit_code=first_bad.get("expected_exit_code"),
                        replayed_exit_code=first_bad.get("actual_exit_code"),
                    )
                    # #181 — the _log_emit above lands in
                    # .synaptory/.orchestrator/events.jsonl, which never
                    # leaves the machine that wrote it. Mirror the mismatch
                    # to the CP as `evidence_dod / returned` so E1 activity
                    # is observable centrally; without this, #181's gating
                    # precondition cannot be evaluated from production at
                    # all. Best-effort, exactly like every other gate
                    # emission: a DoD verdict must never depend on the CP
                    # being reachable.
                    try:
                        from gate_emitter import (
                            emit_evidence_dod_replay_mismatch,
                        )

                        emit_evidence_dod_replay_mismatch(
                            story_id,
                            check_id=cid,
                            command=first_bad.get("command"),
                            attested_exit_code=first_bad.get(
                                "expected_exit_code"),
                            replayed_exit_code=first_bad.get(
                                "actual_exit_code"),
                            project_dir=project_dir,
                        )
                    except Exception:
                        pass  # never block the gate on telemetry

    # #403 — typed classification. Runs LAST, after the waiver has demoted
    # build_succeeds and after the #179 E1 replay has had its chance to flip a
    # verdict, so `result` describes the check's final standing and never a
    # midway one.
    # Both spellings a `source` can take, so the class derivation can find the
    # receipt that actually sourced a check: `_receipt_source_path` prefers the
    # loaded filename, while the integration branch above uses the canonical
    # name. A source that resolves to no receipt simply yields no class.
    receipt_by_source: dict[str, dict[str, Any]] = {}
    for _ab, _r in receipt_by_role.items():
        receipt_by_source[_receipt_source_path(story_id, _ab, _r)] = _r
        receipt_by_source.setdefault(get_story_receipt_path(story_id, _ab), _r)
    # #406 — the test-first posture, stamped on the check it governs. This is
    # the fourth of the four surfaces the migration path is visible on (the
    # others: the story record, the Cycle's `test_first` state block, and the
    # `authored_cases` block on every dispatch). 13.3's failure was not a
    # wrong verdict, it was four of five checks emitting nothing while the
    # gate rendered green, so a gate evaluated WITHOUT test-first has to say
    # so on the record a reader actually reads.
    if "tests_pass" in checks:
        _authored_ids = story_authored_case_ids(story_rec or {})
        _gap = (story_rec or {}).get("criteria_gap_declared")
        _disagreement = (story_rec or {}).get("_authored_disagreement")
        checks["tests_pass"]["test_first"] = {
            "authored_at_commit": bool(_authored_ids),
            "authored_case_ids": _authored_ids,
            "criteria_gap_declared": (
                dict(_gap) if isinstance(_gap, dict) and _gap else None
            ),
            "producing_stage_entered_at": _producing_stage_entered_at(
                story_rec or {}
            ),
            # WHERE the authored set came from: `manifest` (the seal),
            # `untrusted_manifest` (a seal that failed its own hash),
            # `missing_manifest` (#507: a unit under a seal no copy of which
            # can be read), or `board` (no manifest to consult -- Scrum,
            # Kanban, pre-manifest). Reported because "the gate trusted the
            # board" and "the gate trusted the seal" are different guarantees
            # and a reader cannot otherwise tell which one they got.
            "authored_source": (story_rec or {}).get("_authored_source"),
            # #507 — WHICH copy of the seal answered (`local`, `committed`,
            # `git`), and, when none did, what still said one existed. "The
            # gate read the local copy" and "the gate had to recover the seal
            # from HEAD because the working tree lost it" are different facts
            # about the same verdict, so they do not share a representation.
            "authored_seal_origin": (story_rec or {}).get("_authored_seal_origin"),
            "authored_seal_witness": (
                (story_rec or {}).get("_authored_seal_witness") or None
            ),
            # Present ONLY when the board and the seal name different sets.
            # The board diverges from a hash-sealed document by being edited,
            # so it is surfaced rather than silently corrected.
            "authored_disagreement": (
                dict(_disagreement) if isinstance(_disagreement, dict) else None
            ),
        }

    typed_results = _classify_dod_checks(checks, receipts, receipt_by_source)

    required_checks = {k: v for k, v in checks.items() if v["required"]}
    all_required_passed = all(
        v["passed"] is True for v in required_checks.values()
    )
    critical_checks = {
        k: v for k, v in checks.items()
        if v["required"] and DOD_CHECKS[k]["category"] == "critical"
    }
    critical_passed = all(
        v["passed"] is True for v in critical_checks.values()
    )

    result = {
        "story_id": story_id,
        "intensity": intensity,
        # #134 GAP-3 — the resolved per-story verification tier
        # (minimal|standard|full); PHI-touching stories are always `full`.
        "verification_tier": verification_tier,
        "checks": checks,
        "passed": all_required_passed,
        "critical_passed": critical_passed,
        # #31/#30 — surface why the conditional gates are active so the
        # orchestrator knows to dispatch the compliance-engineer / runtime
        # verifier for this story before it can reach `done`.
        "baa_enforced": baa_enforced,
        "compliance_required": phi_touching,
        # #487 — the declared compliance check set, and who owes its result
        # on THIS lifecycle. `compliance_required` above stays exactly what it
        # was so every existing reader is unaffected; these two are additive
        # and are what lets the block reason name a stage that can actually be
        # dispatched, instead of a per-unit compliance-engineer dispatch that
        # SPQ no longer schedules.
        "compliance_checks": list(COMPLIANCE_CHECKS),
        "compliance": compliance_declaration(
            baa_enforced=baa_enforced,
            required=phi_touching,
            build_mode=_project_build_mode(project_dir),
        ),
        "runtime_required": runtime_required,
        # #44 — true when this story's AC describes a user-facing screen, so
        # the orchestrator knows to dispatch QE browser-qa (or get PO
        # acceptance) before the story can reach `done`.
        "ui_required": ui_required,
        # #44 phase 5 — true when the story claims an external service is
        # wired/connected, so a stubbed integration can't be reported as done
        # without a live smoke proof (or PO acceptance/deferral).
        "integration_required": integration_required,
        # #403 — the typed per-check results, in the shape the control plane
        # reads (`analytics.DOD_CHECK_RESULTS_KEY`). One entry per REQUIRED
        # check, carrying pass | fail | criteria_gap_declared, the
        # missing-evidence description for a gap, and the evidence class that
        # actually backed the check (never one a payload claimed).
        DOD_CHECK_RESULTS_KEY: typed_results,
        "evaluated_at": _now(),
    }
    _emit_project_event(
        "dod_evaluated",
        project_dir=project_dir,
        story_id=story_id,
        intensity=intensity,
        passed=all_required_passed,
        critical_passed=critical_passed,
        failed_required=[k for k, v in required_checks.items() if v["passed"] is not True],
    )
    return result


def aggregate_sprint_dod(state: dict[str, Any]) -> dict[str, Any]:
    """Aggregate DoD results across all stories in the current sprint.

    Cancelled stories (#116) are excluded from every count — they're work
    the PO decided not to ship, not work that failed quality. They still
    appear in `current_stories` for audit-trail purposes.
    """
    stories = [
        s for s in state.get("current_stories", [])
        if s.get("state") != "cancelled"
    ]
    evaluated = [s for s in stories if s.get("dod") is not None]
    passed = [s for s in evaluated if s["dod"].get("passed")]
    failed = [s for s in evaluated if not s["dod"].get("passed")]

    failed_details = []
    for s in failed:
        failed_checks = [
            k for k, v in s["dod"].get("checks", {}).items()
            if v.get("required") and not v.get("passed")
        ]
        failed_details.append({"id": s["id"], "failed_checks": failed_checks})

    total = len(stories)
    return {
        "total_stories": total,
        "stories_evaluated": len(evaluated),
        "stories_passed": len(passed),
        "stories_failed": len(failed),
        "critical_pass_rate": (
            sum(1 for s in evaluated if s["dod"].get("critical_passed")) / len(evaluated)
            if evaluated else 0.0
        ),
        "overall_pass_rate": len(passed) / len(evaluated) if evaluated else 0.0,
        "failed_stories": failed_details,
    }


# ---------------------------------------------------------------------------
# Cycle Time
# ---------------------------------------------------------------------------

def calculate_story_cycle_time(story: dict[str, Any]) -> dict[str, Any] | None:
    """Calculate cycle time for a completed story from pipeline_log.

    Returns timing breakdown or None if story is not 'done'.
    """
    if story.get("state") != "done":
        return None

    log = story.get("pipeline_log", [])
    if not log:
        return None

    totals: dict[str, float] = {}
    for entry in log:
        s = entry.get("state", "")
        entered = entry.get("entered_at")
        exited = entry.get("exited_at")
        if entered and exited:
            try:
                t_enter = datetime.fromisoformat(entered.replace("Z", "+00:00"))
                t_exit = datetime.fromisoformat(exited.replace("Z", "+00:00"))
                seconds = (t_exit - t_enter).total_seconds()
                key = f"{s}_seconds"
                totals[key] = totals.get(key, 0.0) + seconds
            except (ValueError, TypeError):
                pass

    total = sum(totals.values())
    return {"total_seconds": total, **totals}


# ---------------------------------------------------------------------------
# Receipt Helpers
# ---------------------------------------------------------------------------

def companion_receipt_dirs(receipts_dir: str) -> list[str]:
    """Read-side search: scoped receipts AND the flat orchestrator dir.

    `begin_dispatch` names a scoped path (spec or SPQ workstream). Cursor
    `agents/*.md` historically told the model to write the flat
    `.orchestrator/receipts/` path. If the scoped dir exists (even empty),
    `receipts_dir_for` returns it and a flat file would be invisible.
    Both locations count.
    """
    if not receipts_dir:
        return []
    dirs = [receipts_dir]
    abs_dir = os.path.abspath(receipts_dir)
    cur = abs_dir
    orch = None
    for _ in range(8):
        if os.path.basename(cur) == ".orchestrator":
            orch = cur
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    if orch is None:
        return dirs
    flat = os.path.join(orch, "receipts")
    if flat not in dirs:
        dirs.append(flat)
    if os.path.abspath(abs_dir) == os.path.abspath(flat):
        specs = os.path.join(orch, "specs")
        if os.path.isdir(specs):
            try:
                names = os.listdir(specs)
            except OSError:
                names = []
            for name in names:
                cand = os.path.join(specs, name, "receipts")
                if cand not in dirs:
                    dirs.append(cand)
        cycles = os.path.join(orch, "spq", "cycles")
        if os.path.isdir(cycles):
            try:
                cycle_names = os.listdir(cycles)
            except OSError:
                cycle_names = []
            for cycle in cycle_names:
                cycle_receipts = os.path.join(cycles, cycle, "receipts")
                if cycle_receipts not in dirs:
                    dirs.append(cycle_receipts)
                ws_root = os.path.join(cycles, cycle, "workstreams")
                if not os.path.isdir(ws_root):
                    continue
                try:
                    streams = os.listdir(ws_root)
                except OSError:
                    streams = []
                for ws in streams:
                    cand = os.path.join(ws_root, ws, "receipts")
                    if cand not in dirs:
                        dirs.append(cand)
    return dirs


def find_story_receipt_file(
    receipts_dir: str, story_id: str, abbrev: str
) -> str | None:
    """First existing `{story_id}-{abbrev}.json` across companion dirs."""
    name = get_story_receipt_path(story_id, abbrev)
    for directory in companion_receipt_dirs(receipts_dir):
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            return path
    return None


def receipts_dir_for(project_dir: str, *, intended: bool = False) -> str:
    """THE receipts directory for this project. One definition, three layouts.

    Three layouts coexist and each is selected by a different mechanism:

        SPQ            `spq/cycles/<cycle-id>/receipts` from NATIVE identity
                       (#303/#304/#305, flattened by #644 with the lane).
                       Never resolved through `active_spec` or
                       `SYNAPTORY_ACTIVE_SPEC`, and never falling through to
                       the branch that would.
        multi-spec     `specs/<active>/receipts` for scrum/kanban, unchanged.
        flat           `receipts`.

    `intended=False` (reading) keeps the historical "prefer the scoped dir only
    once it exists" posture, so a project mid-migration still finds its old
    receipts. `intended=True` (issuing a dispatch contract) resolves the scoped
    dir whether or not it exists yet, because the contract names a path before
    any receipt has been written.

    This is the single authoritative resolver on purpose. There were previously
    TWO -- this one and `advance_kernel.intended_receipts_dir` -- and the whole
    reason that function exists is documented in its own docstring: the
    dispatch-contract path and the gate-read path had diverged. Five
    independent derivations of one path (add the three shell hooks) is how that
    happens, so the shell now calls this too.
    """
    orch = os.path.join(project_dir, ".synaptory", ".orchestrator")

    # SPQ FIRST, AND SPQ NEVER FALLS THROUGH. The Multi-Spec branch below
    # resolves a directory from `SYNAPTORY_ACTIVE_SPEC`, so an SPQ receipt
    # reaching it lands in a spec slot chosen by whatever happened to be
    # exported -- #303/#304 forbid that in as many words.
    #
    # IT HAD BEEN HAPPENING ON EVERY SPQ PROJECT SINCE THE REWRITE, and this
    # handler is both load-bearing and where the bug lived. The block branched
    # on `ident.workstream_id` before choosing its call; `SPD-194` retires the
    # Workstream so `Identity` carries no such attribute; the AttributeError
    # was swallowed by a bare `except` whose comment described the
    # fall-through as intentional. The guard against the fall-through had
    # become the fall-through.
    #
    # WHAT THAT COST, in the words of this function's own docstring: there
    # were previously TWO resolvers and the whole reason the second exists is
    # that "the dispatch-contract path and the gate-read path had diverged."
    # The gates calling `spq_paths.receipts_dir` directly --
    # `acceptance_readiness`, and `close_cycle`'s per-unit DoD aggregation --
    # kept reading `spq/cycles/<cycle-id>/receipts`, so a receipt filed
    # through this resolver was invisible to the gate that required it.
    #
    # TWO LESSONS TAKEN HERE. The `except` names the failure it tolerates:
    # `IdentityError` is "no Cycle open", the one legitimate reason resolution
    # fails, and a broad catch around a branch whose whole job is to refuse a
    # fall-through will always be able to cause one. And the branch RETURNS
    # rather than dropping out, so nothing below it can be reached by an SPQ
    # project regardless of what fails.
    #
    # A dead COORDINATION block sat above this one, calling
    # `spq_paths.read_coordination_pin` and `coordination_receipts_dir`, both
    # retired with the Coordination Cycle. It could only ever raise into a
    # handler. Deleted rather than left swallowed: an exception handler is no
    # place to keep dead code, because it hides the live failure standing next
    # to it -- which is how the `workstream_id` line survived at all.
    _state = _read_state(project_dir) or {}
    if str(_state.get("build_mode") or "").lower() == "spq":
        import spq_paths

        try:
            ident = spq_paths.resolve_identity(project_dir, state=_state)
        except spq_paths.IdentityError:
            # No Cycle open yet -- DISCOVERY, or a clone that has not
            # hydrated. A legitimate state with a legitimate answer, and the
            # answer is the flat directory. An unresolvable identity is not
            # permission to consult `active_spec`.
            ident = None
        if ident is not None and ident.cycle_id:
            candidate = spq_paths.receipts_dir(project_dir, ident.cycle_id)
            if intended or os.path.isdir(candidate):
                return candidate
        return os.path.join(orch, "receipts")

    # Multi-spec, for scrum and kanban. Retained: #303/#304/#305 scope the
    # decoupling to SPQ, and removing this would be a capability regression for
    # every scrum project running several label-filtered boards.
    sid = os.environ.get("SYNAPTORY_ACTIVE_SPEC") or None
    if not sid:
        try:
            from spec_state import get_active_spec
            sid = get_active_spec(project_dir)
        except Exception:
            sid = None
    if sid:
        spec_receipts = os.path.join(orch, "specs", sid, "receipts")
        if intended or os.path.isdir(spec_receipts):
            return spec_receipts
    return os.path.join(orch, "receipts")


def _resolve_receipts_dir(project_dir: str) -> str:
    """Read-side receipts directory. Historical name, kept for its callers."""
    return receipts_dir_for(project_dir, intended=False)


# Forward edges that require a validated receipt. Kept here (rather than imported
# from advance_kernel) so the refusal cannot be defeated by an import failure --
# this guard must hold even when the kernel is unavailable.
_RECEIPT_GATED_EDGES = frozenset(
    {
        ("queued", "in_progress"),
        ("in_progress", "testing"),
        ("testing", "reviewing"),
        ("reviewing", "done"),
        ("reviewing", "awaiting_acceptance"),
        ("awaiting_acceptance", "done"),
    }
)


def _state_transaction(project_dir: str):
    """Serialize a read-decide-write window against other processes.

    `spec_state.write_state` locks only its own internal re-read, so every
    caller's decision window is unprotected: two concurrent callers can both
    read the same state, both decide against that stale copy, and both write.
    Mutating CLI verbs wrap their whole window in this.
    """
    try:
        from spec_state import state_transaction

        return state_transaction(project_dir)
    except ImportError:  # pragma: no cover - shared runtime always ships it
        import contextlib

        return contextlib.nullcontext()


def _is_receipt_gated_edge(from_state: str, to_state: str) -> bool:
    """True when this transition needs the kernel's evidence checks.

    Backward and de-escalating edges (-> blocked, unblock, rejection) stay open:
    parking a failing story is not promotion, and the recovery ladder depends on
    being able to do it without a receipt.
    """
    return (from_state, to_state) in _RECEIPT_GATED_EDGES


_RECEIPT_GATED_CLI_REFUSAL = (
    "Refused: %s -> %s is receipt-gated and must go through the "
    "advance kernel, which validates the receipt before writing:\n"
    "    python3 hooks/lib/advance_kernel.py advance <project_dir> "
    "%s %s\n"
    "This verb writes state with no receipt, DoD or replay check, so "
    "it is reserved for operator recovery. If you are repairing a "
    "crashed or corrupt board, re-run with --force-recovery and "
    "--reason '<why>'; the override is recorded as a signal."
)


def _cli_receipt_gated_refusal(
    from_state: str,
    to_state: str,
    story_id: str,
    *,
    forced: bool,
    reason: str | None,
    project_dir: str | os.PathLike[str] | None = None,
) -> str | None:
    """Return a CLI refusal message, or None if the transition may proceed.

    Shared by `story_pipeline.py transition` and the Scrum/Kanban/SPQ
    `transition_story` verbs so a public CLI cannot promote without a receipt.
    A `--force-recovery` override is recorded as a `forced_transition` signal.

    `project_dir` is where that signal is recorded. Without it the override
    lands in the process's cwd instead of the project (#380) -- and an operator
    override nobody can find in the project's own event log is the one signal
    that must not go missing.
    """
    if _is_receipt_gated_edge(from_state, to_state) and not forced:
        return _RECEIPT_GATED_CLI_REFUSAL % (
            from_state or "?",
            to_state,
            story_id,
            to_state,
        )
    if forced:
        if not reason:
            return "--force-recovery requires --reason '<why>'"
        _emit_project_event(
            "forced_transition",
            project_dir=project_dir,
            story_id=story_id,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
        )
    return None


def get_story_receipt_path(story_id: str, role_abbrev: str) -> str:
    """Generate the v2 story-scoped receipt filename."""
    return f"{story_id}-{role_abbrev}.json"


def _receipt_source_path(story_id: str, role_abbrev: str, receipt: dict[str, Any]) -> str:
    """Return the loaded receipt filename, falling back to canonical naming."""
    filename = receipt.get("_filename")
    if isinstance(filename, str) and filename:
        return filename
    return get_story_receipt_path(story_id, role_abbrev)


def collect_story_receipts(
    receipts_dir: str, story_id: str
) -> list[dict[str, Any]]:
    """Collect all receipt files matching {story_id}-*.json.

    Searches companion dirs (spec-scoped + flat) so a receipt written to
    either layout is visible to DoD. First filename wins (primary dir first).
    """
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    prefix = f"{story_id}-"
    for directory in companion_receipt_dirs(receipts_dir):
        if not os.path.isdir(directory):
            continue
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for fname in names:
            if not (fname.startswith(prefix) and fname.endswith(".json")):
                continue
            if fname in seen:
                continue
            fpath = os.path.join(directory, fname)
            try:
                with open(fpath) as f:
                    receipt = json.load(f)
                    if isinstance(receipt, dict):
                        receipt.setdefault("_filename", fname)
                    results.append(receipt)
                    seen.add(fname)
            except (json.JSONDecodeError, OSError):
                pass
    return results


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------

def _find_story(
    state: dict[str, Any], story_id: str
) -> dict[str, Any] | None:
    """Find a story by ID in current_stories."""
    for story in state.get("current_stories", []):
        if story.get("id") == story_id:
            return story
    return None


def _now() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _role_to_abbrev(role_name: str) -> str | None:
    """Map a role name to its abbreviation for receipt filenames."""
    mapping = {
        "software-engineer": "se",
        "quality-engineer": "qe",
        "code-reviewer": "cr",
        "compliance-engineer": "ce",
        "solution-architect": "sa",
        "platform-engineer": "pe",
        "project-owner": "po",
        "product-manager": "po",  # legacy alias for in-flight receipts
        "technical-writer": "tw",
        "research-advisor": "ra",
    }
    return mapping.get(role_name) or mapping.get(role_name.replace("_", "-"))


def _command_is_build_proof(command: Any) -> bool:
    """True when a verification command is build/dev-server evidence."""
    if not isinstance(command, str):
        return False
    normalized = command.lower()
    build_terms = (
        "build",
        "compile",
        "tsc",
        "typecheck",
        "type-check",
        "dev server",
        "npm run dev",
        "pnpm dev",
        "yarn dev",
        "next dev",
    )
    return any(term in normalized for term in build_terms)


def _evaluate_build_proof(receipt: dict[str, Any]) -> bool | None:
    """Evaluate build_succeeds from receipts outside the nominal SE slot.

    The cross-role fallback for #144 must find actual build/dev-server proof,
    not any successful executed command. A QE-only pytest receipt proves tests,
    but it must not satisfy the build gate.
    """
    cmds = receipt.get("verification_commands", [])
    if not cmds:
        return False
    build_cmds = [
        c for c in cmds
        if (
            isinstance(c, dict)
            and "exit_code" in c
            and _command_is_build_proof(c.get("command", ""))
        )
    ]
    if not build_cmds:
        return None
    return all(c.get("exit_code") == 0 for c in build_cmds)


def _command_is_test_proof(command: Any) -> bool:
    """True when a verification command is test-suite evidence."""
    if not isinstance(command, str):
        return False
    normalized = command.lower()
    test_terms = (
        "test",
        "pytest",
        "vitest",
        "jest",
        "spec",
        "rspec",
        "ctest",
    )
    return any(term in normalized for term in test_terms)


def _evaluate_test_proof(receipt: dict[str, Any]) -> bool | None:
    """Evaluate tests_pass from receipts outside the nominal QE slot (#134).

    Strict analog of `_evaluate_build_proof`: only executed command objects
    whose text is recognisably a test run count; a receipt with no test
    commands proves nothing about tests (None, never vacuous True).
    """
    cmds = receipt.get("verification_commands", [])
    if not cmds:
        return None
    test_cmds = [
        c for c in cmds
        if (
            isinstance(c, dict)
            and "exit_code" in c
            and _command_is_test_proof(c.get("command", ""))
        )
    ]
    if not test_cmds:
        return None
    return all(c.get("exit_code") == 0 for c in test_cmds)


def _evaluate_fallback_proof(
    check_id: str,
    receipt: dict[str, Any],
    verification_tier: str = "full",
    story: dict[str, Any] | None = None,
) -> bool | None:
    """Strict per-check proof matcher for evidence-based fallback (#134 GAP-10).

    Deliberately STRICTER than `_evaluate_check`: a non-nominal receipt only
    satisfies a check when it carries the check's explicit structured proof.
    Lenient defaults that are safe on the nominal receipt (e.g.
    `findings_critical` defaulting to 0, coverage "assume pass") would let a
    stray receipt vacuously satisfy a gate it knows nothing about.

    `story` (#406): once a Cycle has authored acceptance cases, a recognisable
    test command is no longer sufficient proof of `tests_pass` from ANY
    receipt. The fallback then demands the same authored-case evidence the
    nominal path demands, so "role is a preference, structured evidence is the
    requirement" still holds -- the requirement simply got stricter. Without
    it the `any_with_proof` fallback re-opens the bypass on the primary path,
    which is #187's finding restated one layer up.
    """
    if check_id == "build_succeeds":
        return _evaluate_build_proof(receipt)
    if check_id == "tests_pass":
        # `under_authored_regime` rather than the board's ids alone (#507): a
        # recognisable test command from a stray receipt must not satisfy
        # tests_pass for a unit whose seal is unreadable either. That was the
        # live half of the bypass -- the nominal path already refused, and this
        # fallback then promoted an SE receipt's green `pytest` back to a pass.
        if under_authored_regime(story):
            return evaluate_tests_pass(receipt, story)[0]
        return _evaluate_test_proof(receipt)
    if check_id == "no_critical_findings":
        metrics = receipt.get("metrics", {})
        if "findings_critical" not in metrics:
            return None  # explicit verdict required from a non-nominal role
        return metrics.get("findings_critical") == 0
    if check_id == "coverage_no_decrease":
        metrics = receipt.get("metrics", {})
        if "coverage_delta" not in metrics:
            return None  # no vacuous pass from a receipt without the metric
        return _evaluate_check("coverage_no_decrease", receipt)
    if check_id in ("runtime_verified", "ui_acceptance", "integration_verified"):
        # These already require explicit structured evidence in
        # `_evaluate_check` (None without it) — safe to reuse directly.
        return _evaluate_check(check_id, receipt, verification_tier=verification_tier)
    return None


# ─── the test-first prove path (#406, proposal 3.3, ADR-032 section 4) ───────


def _fallback_normalize_cases(raw: Any) -> list[dict[str, Any]]:
    """`spq_manifest.normalize_cases` for a tree without `spq_manifest`.

    Must produce the SAME ids, positional fallback included. The first version
    of this omitted the positional `AC-%d`, so every case authored in the
    low-friction form `commit.md` documents (a bare string, or a dict with no
    explicit id) normalized to an empty id, got skipped by
    `story_authored_case_ids`, and silently reverted the unit to the pre-#406
    rules. A degraded path that disables the check on exactly the shape the
    docs encourage is worse than no degraded path.
    """
    out: list[dict[str, Any]] = []
    for index, entry in enumerate(raw):
        positional = "AC-%d" % (index + 1)
        if isinstance(entry, dict):
            out.append({
                "id": str(entry.get("id") or positional).strip(),
                "statement": str(
                    entry.get("statement") or entry.get("text") or ""
                ).strip(),
                "criterion_ref": str(entry.get("criterion_ref") or "").strip(),
            })
        else:
            out.append({
                "id": positional,
                "statement": str(entry or "").strip(),
                "criterion_ref": "",
            })
    return out


try:
    from spq_manifest import normalize_cases as _normalize_cases_impl
except ImportError:  # pragma: no cover - partial install
    _normalize_cases_impl = _fallback_normalize_cases


def _normalize_authored_cases(raw: Any) -> list[dict[str, Any]]:
    """The authored acceptance cases for a story, normalized.

    Delegates to `spq_manifest.normalize_cases` so the manifest and the board
    spell a case exactly one way. A second spelling here would be a second
    place for a case id to be understood differently, and the id is what the
    manifest/receipt match is performed on. Resolved once at import rather
    than per call: this runs per receipt inside the gate's veto scan, its
    detail loop and its fallback matcher, and a module-level binding also
    makes the degraded path reachable from a test.
    """
    if not isinstance(raw, (list, tuple)) or not raw:
        return []
    return _normalize_cases_impl(raw)


def story_authored_case_ids(story: dict[str, Any]) -> list[str]:
    """The case ids this story's Cycle authored at COMMIT, in order.

    Empty for a story admitted with a declared (or implicit) criteria gap, and
    empty for every Scrum / Kanban story: those lifecycles have no COMMIT
    event to author against, and this pilot is SPQ-only (ADR-032 section 6).
    Emptiness therefore means "the pre-#406 rules apply here", which is a
    different statement from "the authored set is satisfied".
    """
    seen: set[str] = set()
    out: list[str] = []
    for case in _normalize_authored_cases(story.get("acceptance_cases")):
        cid = case.get("id") or ""
        if cid and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


#: `_authored_source` values meaning "a seal governs this unit and no copy of
#: it could be read". Spelled once because two consumers switch on it and a
#: third would otherwise be added without them.
SEAL_UNREADABLE_SOURCES = ("untrusted_manifest", "missing_manifest")


def seal_authority_unreadable(story: dict[str, Any] | None) -> bool:
    """Is this unit's seal governing but unreadable (tampered, or gone)?"""
    return (story or {}).get("_authored_source") in SEAL_UNREADABLE_SOURCES


def under_authored_regime(story: dict[str, Any] | None) -> bool:
    """Do the #406 rules govern `tests_pass` for this story?

    The board's own authored ids, OR a seal the gate could not read. The
    second clause is #507's correction and the reason this predicate exists at
    all: both consumers used to ask `story_authored_case_ids` directly, so the
    BOARD decided whether the strict rules applied — and the board is the
    agent-writable file the seal exists to distrust. Emptying
    `acceptance_cases` therefore bought the pre-#406 rules back, on a Cycle
    whose seal had just been tampered with or deleted, which made the refusal
    conditional on the cooperation of the party it refuses.
    """
    return bool(story_authored_case_ids(story or {})) or seal_authority_unreadable(
        story
    )


def authored_cases_target(story: dict[str, Any]) -> dict[str, Any]:
    """The authored-case block a dispatch carries (#406).

    This is how the producer receives the authored cases as its TARGET and the
    prover receives them as the set to execute. Deliberately a per-dispatch
    payload and not a line in the rendered Execution Envelope: #404 cut the
    SE fixed instruction payload to 3,987 words against a 4,000-word enforced
    ceiling, and spending the last thirteen on a stanza that repeats per-task
    data would have bought a fragile budget for no new information.

    `authored_at_commit` is the field a reader should branch on. When it is
    false the block still says so and carries the recorded reason, so a
    dispatch under the migration path announces that it is under the migration
    path.
    """
    cases = _normalize_authored_cases(story.get("acceptance_cases"))
    gap = story.get("criteria_gap_declared")
    return {
        "authored_at_commit": bool(cases),
        "count": len(cases),
        "cases": cases,
        "criteria_gap_declared": dict(gap) if isinstance(gap, dict) and gap else None,
    }


#: Sentinel for "this Cycle's sealed manifest exists but cannot be trusted",
#: distinct from None ("there is no manifest to consult"). The difference is
#: load-bearing: no manifest means a Scrum / Kanban / pre-manifest story and
#: the board is the only record there is, while an unverifiable manifest means
#: the authored set is unestablishable and must resolve to a criteria gap.
#: Falling back to the board in the second case would reward tampering with
#: the very document that makes the authored set tamper-evident.
_MANIFEST_UNTRUSTED = object()

#: Sentinel for "this unit believes it is under a seal, and no copy of that
#: seal can be read" (#507). A THIRD answer, deliberately: deleting the
#: manifest and never having had one are different facts, and while they
#: shared the `None` representation one `rm` reverted a sealed unit to the
#: pre-#406 rules -- cheaper than the dishonesty #494 had just made expensive,
#: and against the document that makes the authored set tamper-evident.
#:
#: It is not "the manifest file is absent". Absence is correct and common for
#: Scrum, for Kanban and for every Cycle predating #406, so this fires only
#: where a record OTHER than the missing file says a manifest was sealed for
#: the Cycle this story is being graded against (`_seal_witness`).
_MANIFEST_MISSING = object()

#: How far ahead of now a `completed_at` may sit before the ordering check
#: treats the clock as wrong. Fifteen minutes: wide enough for real host clock
#: skew on a developer laptop or a CI runner, narrow enough that a receipt
#: dated next year cannot satisfy "the proving followed the producing".
_FUTURE_TOLERANCE_S = 900


def _resolved_cycle_id(project_dir: str, state: dict[str, Any] | None) -> str:
    """Which Cycle is this checkout grading against?

    The state pointer FIRST and `spq_paths`' own cycle index second. The second
    lookup is not redundancy for its own sake: `evaluate_story_dod` reaches the
    manifest with `story_rec = None` whenever the state file cannot be read or
    the unit has left the board, and resolving the identity from the index
    means an unreadable board no longer silently reverts an authored unit to
    the pre-#406 rules. The index is a different file with a different writer,
    so one being lost does not take the other with it.
    """
    try:
        import spq_paths
    except ImportError:  # pragma: no cover - partial install
        return ""
    try:
        if state is None:
            state = _read_state(project_dir)
        cycle_id = str((state or {}).get("_cycle_id") or "")
    except Exception:  # noqa: BLE001 - an unreadable board is not the end of it
        cycle_id = ""
    if cycle_id:
        return cycle_id
    try:
        rows = [
            r for r in spq_paths.list_cycles(project_dir)
            if isinstance(r, dict) and _index_row_cycle_id(r)
        ]
        return _index_row_cycle_id(max(rows, key=lambda r: int(r.get("seq") or 0)))
    except Exception:  # noqa: BLE001 - no index, no Cycle, no verdict
        return ""


def _index_row_cycle_id(row: dict[str, Any]) -> str:
    """`record_cycle` writes the key as `cycle_id`; `id` is accepted so a
    future index shape does not silently disable the lookup, which is exactly
    how the first version of it failed (it filtered on `id` alone and matched
    nothing)."""
    return str(row.get("cycle_id") or row.get("id") or "")


def _seal_witness(
    project_dir: str, cycle_id: str, state: dict[str, Any] | None
) -> str:
    """What, other than the missing file, says this Cycle sealed a manifest?

    The whole of #507 is this question, because the fix cannot be "block when
    the manifest is absent". Absence is the correct and common state for
    Scrum, for Kanban and for every Cycle predating #406, and blocking on it
    would brick all three. What distinguishes deletion from never-had-one is
    whether some OTHER record still says a seal existed for the Cycle this
    story is being graded against.

    Returns a phrase for the operator, or `""` for "nothing here ever sealed a
    manifest", which is the answer that leaves the unsealed lifecycles alone.

    Ordered by how far the record sits from the gate's own subject, and the
    ordering is the honest part of this function:

      1. **git history for the committed transport.** The only rung an agent
         cannot reach by writing files: dropping the path from `HEAD` takes a
         commit, and that commit is itself the record. Everything below is on
         the same filesystem as the agent, so each is one more deliberate act
         rather than an impossibility -- see this function's caller docstring
         for the residual that follows from that.
      2. **another Cycle's readable seal.** This project demonstrably seals
         manifests; the one for THIS Cycle is the copy that is gone.
      3. **the committed cycle directory**, surviving without its manifest.
      4. **the cycle index**, written by `record_cycle` -- a different file
         with a different writer. Naming a DIFFERENT Cycle counts too: that is
         the `_cycle_id` repoint, where the pointer selects a Cycle whose seal
         this checkout has never held while the index still names the real one.
      5. **the board's own path.** The SPQ board lives under
         `spq/cycles/<cycle-id>/workstreams/<ws>/`, and `open_cycle` seals the
         manifest before writing it, so a board under a Cycle's directory
         records that this checkout hydrated that Cycle from a seal. An edit
         to the file cannot change where the file is.
      6. **the board's own hydration stamp** (`manifest_hash`,
         `manifest_revision`) and #406's `test_first` block. A self-witness,
         named as such: it lives in the same file as the story record, so it
         raises the price of the forgery by one edit and proves nothing on its
         own.
    """
    try:
        import spq_manifest
        import spq_paths
    except ImportError:  # pragma: no cover - partial install
        return ""

    if spq_manifest.sealed_in_history(project_dir, cycle_id):
        return (
            "git history carries this Cycle's committed manifest at %s, so the "
            "seal existed and this working tree no longer has it"
            % spq_manifest.committed_relpath(project_dir, cycle_id)
        )

    try:
        cycles_root = os.path.dirname(spq_paths.cycle_root(project_dir, cycle_id))
        siblings = sorted(os.listdir(cycles_root))
    except Exception:  # noqa: BLE001 - no store, no siblings
        siblings = []
    for other in siblings:
        if other == cycle_id:
            continue
        try:
            found, _origin = spq_manifest.read_sealed(
                project_dir, other, allow_git=False
            )
        except Exception:  # noqa: BLE001 - an unreadable sibling witnesses nothing
            continue
        if found:
            return (
                "Cycle %s in this same store has a readable sealed manifest, so "
                "this project seals them and %s's is the copy that is gone"
                % (other, cycle_id)
            )

    try:
        if os.path.isdir(spq_paths.committed_cycle_dir(project_dir, cycle_id)):
            return (
                "the committed transport directory %s survives without its "
                "manifest.json"
                % os.path.relpath(
                    spq_paths.committed_cycle_dir(project_dir, cycle_id),
                    str(project_dir),
                )
            )
    except Exception:  # noqa: BLE001
        pass

    try:
        rows = [
            _index_row_cycle_id(r) for r in spq_paths.list_cycles(project_dir)
            if isinstance(r, dict) and _index_row_cycle_id(r)
        ]
    except Exception:  # noqa: BLE001
        rows = []
    if cycle_id in rows:
        return (
            "this Cycle is recorded in the cycle index (%s), which `open_cycle` "
            "writes alongside the seal"
            % os.path.relpath(spq_paths.index_path(project_dir), str(project_dir))
        )
    if rows:
        return (
            "the cycle index names %s, not %s, so the identity this board is "
            "pointed at is one whose seal this checkout has never held"
            % (", ".join(rows), cycle_id)
        )

    # The board's own LOCATION, which an in-place edit cannot change. Under SPQ
    # the board is `spq/cycles/<cycle-id>/execution-state.json`, and
    # `open_cycle` seals the declaration before it writes that file, so a board
    # sitting under a Cycle's directory is a record that this checkout hydrated
    # that Cycle from a seal. Moving it is possible and changes which board the
    # runtime reads at all, which is why this outranks the stamps INSIDE the
    # file.
    #
    # ONE BOARD PER CYCLE. This rung used to scan
    # `<cycle-root>/workstreams/<ws>/execution-state.json`, a directory
    # `spq_paths` no longer produces -- so it always found nothing, and the
    # ladder fell through to the self-witnessing stamps in rung 6. A rung that
    # cannot answer is worse than an absent one: it reads as "no such record"
    # when the record is right there under a different name.
    try:
        board = spq_paths.execution_state_path(project_dir, cycle_id)
    except Exception:  # noqa: BLE001 - a malformed identity has no board path
        board = ""
    if board and os.path.exists(board):
        return (
            "this checkout has a hydrated board for Cycle %s at %s, and "
            "`open_cycle` seals the declaration before it writes that file"
            % (cycle_id, os.path.relpath(board, str(project_dir)))
        )

    stamped = state if isinstance(state, dict) else {}
    if stamped.get("manifest_hash"):
        return (
            "this board records being hydrated from manifest %s, and no copy "
            "of that document can be read" % stamped.get("manifest_hash")
        )
    test_first = stamped.get("test_first")
    if isinstance(test_first, dict) and test_first.get("cycle_id"):
        return (
            "this board's `test_first` block records %d unit(s) admitted under "
            "Cycle %s's seal"
            % (
                int(test_first.get("units_admitted") or 0),
                test_first.get("cycle_id"),
            )
        )
    return ""


def _recorded_revision(project_dir: str, cycle_id: str) -> tuple[str, str]:
    """`(manifest_hash, problem)` from the control plane, or `("", "")`.

    Wrapped so the gate has ONE place that decides what an unavailable
    authority means, and so a tree without the emitter reads as unconfigured
    rather than as unreachable: a partial install is not a connected project
    whose record is missing.
    """
    try:
        import manifest_emitter
    except ImportError:  # pragma: no cover - partial host install
        return "", ""
    reader = getattr(manifest_emitter, "read_cycle_authority", None)
    if reader is None:  # pragma: no cover - older composed runtime
        return "", ""
    try:
        recorded, problem = reader(project_dir, cycle_id)
    except Exception as exc:  # noqa: BLE001 - an unreadable authority is a problem
        return "", "the recorded revision could not be read (%s)" % exc
    return str(recorded or ""), str(problem or "")


def resolve_authored_cases(
    project_dir: str, story_id: str, state: dict[str, Any] | None = None
) -> tuple[Any, str, str]:
    """`(cases, seal_origin, witness)` for `story_id` (#406, #507).

    `cases` is the case records, `None` when there is no seal to consult,
    `_MANIFEST_UNTRUSTED` when a copy exists and does not match its own hash,
    or `_MANIFEST_MISSING` when no copy can be read and something else still
    says one was sealed. `seal_origin` names which copy answered
    (`spq_manifest.SEAL_*`); `witness` is `_seal_witness`'s phrase, non-empty
    only on the `_MANIFEST_MISSING` path.

    Why the gate reads the seal at all, and it is the answer to a forgery it
    could not otherwise refuse. The DoD gate reads the STORY record, because
    `next_action` has to stay a pure function of state and the board is what it
    can see. But the board is agent-writable, so an agent can drop a case it
    cannot pass, report the remainder green, and clear the gate. Comparing the
    receipt against the board would have made the authored set exactly as
    forgeable as the receipt it is meant to check.

    So the gate consults the hash-sealed manifest and the board is only a cache
    of it. #507 is what was left: the seal is written to TWO places and this
    function read ONE, so `rm` of the local copy resolved to "no manifest to
    consult" -- the same answer a Scrum project gives -- while an identical
    committed copy sat on disk beside it. That made deleting the seal cheaper
    than honouring it and cheaper than the omission #494 had just priced.

    Both halves of the fix are here:

    - **Recovery.** `spq_manifest.read_sealed` consults every copy of the seal
      (local, committed, `HEAD`), so removing one is not cheaper, it is
      ineffective. This is the better half: no block is needed, because the
      authored set still binds.
    - **Detection.** When no copy reads and `_seal_witness` still names one,
      the answer is `_MANIFEST_MISSING`, which the verdict turns into a
      criteria gap. Unknowable, not satisfied -- and a gap blocks the done
      edge, so a complete deletion costs at least what declaring the gap does.

    **What this does NOT close, stated plainly.** `manifest_hash` is an
    UNKEYED checksum: it proves a document is internally consistent, not that
    this system authored it. An agent that writes a whole replacement manifest
    -- units, cases, recomputed hash -- is not refused by anything here, and no
    rung above can refuse it, because every local record is on the filesystem
    the agent writes to. The only record of a seal genuinely beyond its reach
    is the control plane's `cycle_manifests` row (shipped by
    `manifest_emitter` at `open_cycle`) plus git history for a project that
    tracks `.synaptory/cycles/`, and the gate consults the second but not the
    first: an offline project must still be able to evaluate its own DoD. So
    what changed is the PRICE -- one `rm` bought a downgrade, and now a
    downgrade takes authoring a counterfeit document -- not the guarantee.
    Signing the seal is what would close it, and it is a separate ticket.
    """
    try:
        import spq_manifest
    except ImportError:  # pragma: no cover - partial install
        return None, "", ""
    cycle_id = _resolved_cycle_id(project_dir, state)
    if not cycle_id:
        return None, "", ""
    # THE REVISION THE BOARD IS EXECUTING, handed to the reader so its git rung
    # can tell "the current seal, recovered" from "an older one, found" (#396).
    # `open_cycle` and `revise_manifest` both stamp it, and it is the only
    # local record of which revision is in force once both working-tree copies
    # are gone.
    # `sealed_manifest_hash` is the revision IN FORCE; `manifest_hash` is the
    # one this board was projected from, and the two differ exactly between a
    # revision and the reprojection that follows it. The fallback keeps a board
    # written before this field existed working, where the two are the same.
    expect_hash = ""
    if isinstance(state, dict):
        expect_hash = str(
            state.get("sealed_manifest_hash") or state.get("manifest_hash") or ""
        )
    # A CONNECTED PROJECT ASKS THE CONTROL PLANE INSTEAD (#507). The board is a
    # file the graded principal writes, so on its own it raises the price of a
    # downgrade without closing it: rewrite `sealed_manifest_hash` to a
    # superseded revision, restore that revision, and every rung agrees again.
    # `cycle_manifests` is append-only and written by a different process, so
    # for a project configured for a control plane it is the authority the
    # working tree cannot be. An offline project keeps the local marker and its
    # declared limit, which §3.3 requires: local delivery state is canonical
    # and an offline project must still evaluate its own DoD.
    recorded, authority_problem = _recorded_revision(project_dir, cycle_id)
    if authority_problem:
        # Connected, and the record could not be read. FAIL CLOSED: a connected
        # project whose authority is unreachable has less evidence than an
        # offline one, not more, and answering from the local marker here would
        # hand it the offline guarantee without saying so.
        return _MANIFEST_MISSING, "", (
            "this project reports to a control plane, and the revision it "
            "records for Cycle %s could not be read, so which revision is in "
            "force cannot be established here: %s" % (cycle_id, authority_problem)
        )
    if recorded:
        expect_hash = recorded
    try:
        manifest, origin = spq_manifest.read_sealed(
            project_dir, cycle_id, expect_hash=expect_hash
        )
    except Exception:  # noqa: BLE001 - a broken read is not a verdict
        manifest, origin = None, ""
    if not isinstance(manifest, dict) or not manifest:
        witness = ""
        try:
            witness = _seal_witness(project_dir, cycle_id, state)
        except Exception:  # noqa: BLE001 - a witness that cannot be read is none
            witness = ""
        if origin == getattr(spq_manifest, "SEAL_STALE", "stale"):
            # A copy exists, and it is not this revision. Blocking, like a
            # missing seal, and for the same reason: what the gate would
            # otherwise consume is not the authored set in force. The witness
            # names the revision the board expects so the remedy is obvious
            # (commit or restore the current one), rather than reading as a
            # deletion.
            return _MANIFEST_MISSING, "", (
                witness
                or "every readable copy of this Cycle's seal is an earlier "
                "revision than the one this board is executing (%s), so the "
                "current revision was never committed and its working-tree "
                "copies are gone" % expect_hash
            )
        if witness:
            return _MANIFEST_MISSING, "", witness
        return None, "", ""
    try:
        # `verify_sealed` and `sealed_units`, not `verify_hash` and
        # `work_units`. `open_cycle` writes a `cycle_records` declaration keyed
        # on `declaration_hash` with its units under `admitted_units`, and the
        # retired spellings did not fail loudly against it: the hash read as
        # ABSENT, so every Cycle the replacement state machine opens graded as
        # `untrusted_manifest` and no Work Unit could ever reach done.
        if not spq_manifest.verify_sealed(manifest):
            return _MANIFEST_UNTRUSTED, origin, ""
        for unit in spq_manifest.sealed_units(manifest):
            if isinstance(unit, dict) and str(unit.get("id") or "") == str(story_id):
                # Key ABSENCE survives the round trip: a unit sealed before
                # #406 carries no `acceptance_cases`, and reporting `[]` for it
                # would be this module inventing a position the document does
                # not hold. `None` sends it down the board path, where
                # `open_cycle` already recorded the migration gap.
                if "acceptance_cases" not in unit:
                    return None, origin, ""
                return (
                    spq_manifest.normalize_cases(unit.get("acceptance_cases")),
                    origin,
                    "",
                )
        return None, origin, ""  # not a unit of the current Cycle
    except Exception:  # noqa: BLE001 - a broken manifest is not a verdict
        return None, origin, ""


def manifest_authored_cases(
    project_dir: str, story_id: str, state: dict[str, Any] | None = None
) -> Any:
    """The authored cases for `story_id` from the SEALED Cycle manifest (#406).

    The `cases` half of `resolve_authored_cases`, kept as the name every
    caller and test already uses. Read that function's docstring for what each
    return value means and for the residual none of them closes.
    """
    return resolve_authored_cases(project_dir, story_id, state)[0]


def _authoritative_story(
    project_dir: str, story: dict[str, Any] | None, state: dict[str, Any] | None = None
) -> dict[str, Any]:
    """`story` with its authored set replaced by the sealed manifest's (#406).

    Adds read-only markers the gate surfaces on the `test_first` stanza:
    `_authored_source` (`manifest` / `board` / `untrusted_manifest` /
    `missing_manifest`), `_authored_seal_origin` naming WHICH copy of the seal
    answered (#507), `_authored_seal_witness` carrying what said a seal
    existed when none could be read, and `_authored_disagreement` when the
    board and the manifest name different sets. A disagreement is reported
    rather than silently corrected, because the board only diverges from a
    hash-sealed document by being edited, and that is worth seeing.
    """
    story = dict(story or {})
    cases, origin, witness = resolve_authored_cases(
        project_dir, str(story.get("id") or ""), state
    )
    if cases is _MANIFEST_UNTRUSTED:
        # Refuse rather than fall back. `story_authored_case_ids` still reports
        # the board's ids so the gate can say WHICH set it could not trust,
        # and the marker turns tests_pass into a gap below.
        story["_authored_source"] = "untrusted_manifest"
        story["_authored_seal_origin"] = origin
        return story
    if cases is _MANIFEST_MISSING:
        # #507 — no copy of the seal reads, and `witness` names what still
        # says one was sealed. Distinct from `untrusted_manifest` because the
        # operator action differs (restore a deleted document vs. explain a
        # document that no longer matches its hash) and because a deletion and
        # a tamper must not share a representation.
        story["_authored_source"] = "missing_manifest"
        story["_authored_seal_witness"] = witness
        return story
    if cases is None:
        story["_authored_source"] = "board"
        if origin:
            # A seal WAS read; it just holds no position for this unit (a
            # pre-#406 record, or a unit this Cycle does not admit).
            story["_authored_seal_origin"] = origin
        return story
    board_ids = story_authored_case_ids(story)
    sealed_ids = [c["id"] for c in cases if c.get("id")]
    story["acceptance_cases"] = cases
    story["_authored_source"] = "manifest"
    story["_authored_seal_origin"] = origin
    if board_ids != sealed_ids:
        story["_authored_disagreement"] = {
            "board": board_ids,
            "manifest": sealed_ids,
        }
    return story


def _producing_stage_entered_at(story: dict[str, Any]) -> str | None:
    """When this story most recently entered the PRODUCING stage.

    `in_progress` is the SE stage in the shared SE->QE->CR pipeline, so its
    latest `entered_at` is the moment the current candidate started being
    produced. Read through `_stage_entered_at` rather than a second scan of
    `pipeline_log`, because next_action, `evaluate_dispatch` and the advance
    kernel already share that one clock and a second one would drift
    (`receipt_timestamp_is_fresh`'s docstring says why: mtime is not it).
    """
    return _stage_entered_at(story, "in_progress")


def authored_execution_after_producer(
    story: dict[str, Any], receipt: dict[str, Any]
) -> bool | None:
    """Did this receipt's work happen AFTER the producing stage's latest entry?

    True  — the verifying work is contemporaneous with or later than the
            current candidate, so its per-case outcomes are about the code
            that exists now.
    False — the receipt PRE-DATES the producing stage's latest entry. That is
            a definitive negative, not a gap: the evidence exists and it is
            about a candidate that has since been rebuilt. This is the same
            staleness `_fresh_receipt` refuses at dispatch time, applied at
            the gate, because a story can be advanced by a host that never
            consulted next_action.
    None  — the ordering cannot be established (no producing-stage entry, no
            parseable `completed_at`, or a `completed_at` implausibly far in
            the FUTURE). Unknowable, so it resolves to a criteria gap rather
            than to either verdict.

    The future bound is not decoration. `completed_at` is a field in an
    agent-written file and this ordering is the one structural guarantee #406
    claims over #187, so `"completed_at": "2099-01-01T00:00:00Z"` would
    otherwise satisfy it unconditionally, from any receipt, forever. A
    future-dated receipt makes the ordering unestablishable rather than
    disproven, hence a gap: the clock is wrong, which is not the same claim as
    the work being stale. `_FUTURE_TOLERANCE_S` is deliberately generous
    against real clock skew, because the cost of a false gap is a blocked
    story and the cost of no bound at all is a bypass.

    A naive `completed_at` is read as UTC, exactly as the advance kernel reads
    it (`_parse_iso_timestamp`). The receipt protocol requires UTC; an agent
    writing local naive time east of Greenwich will read as EARLIER than
    reality and can therefore be scored stale. That is a real sharp edge and
    it is the receipt contract's to fix, not this function's: guessing a zone
    here would mean two clocks again.

    Note what this does NOT claim. It fixes the ORDER of two events on one
    story's own clock. It does not bind the execution to a content digest of
    the candidate, because no per-story candidate digest exists in the kernel
    today -- `selected_sha` is coordination-level (`coordination_barrier.py`)
    and names a whole child Cycle's increment. A producer that edits the tree
    after writing its receipt, without re-entering `in_progress`, is not
    caught here. Naming that gap is more useful than a check that implies a
    guarantee the state cannot support.
    """
    entered = _parse_iso_timestamp(_producing_stage_entered_at(story))
    if entered is None:
        return None
    completed = _parse_iso_timestamp(receipt.get("completed_at"))
    if completed is None:
        return None
    if (completed - datetime.now(timezone.utc)).total_seconds() > _FUTURE_TOLERANCE_S:
        return None  # the clock is wrong; the ordering is unestablishable
    return receipt_timestamp_is_fresh(completed, entered)


def _case_result_is_settled(result: Any) -> bool:
    """Is one per-case result an honest `passed` or `not-applicable`?

    `not-applicable` requires a non-empty `reason`. Without one it is the
    cheapest possible forgery on this whole path: a prover that cannot pass a
    case writes `{"status": "not-applicable"}` for it, or for all of them, and
    clears the gate having proven nothing. Demanding a sentence does not make
    the claim true, but it makes it a claim someone wrote and a reviewer can
    read, which is the same standard `unit_case_problems` holds an authored
    case's `statement` to. The QE contract already asks for the reason; this
    is the line that enforces it.
    """
    if not isinstance(result, dict):
        return False
    status = result.get("status")
    if status == "passed":
        return True
    if status != "not-applicable":
        return False
    return bool(str(result.get("reason") or "").strip())


def authored_case_negative(story: dict[str, Any], receipt: dict[str, Any]) -> bool:
    """Does THIS receipt carry a negative no other receipt may override?

    The cross-receipt veto, and it is deliberately narrower than
    `authored_cases_verdict`. Under the authored regime the veto fires only on
    an EXPLICIT non-passing status for a case the receipt actually reports. A
    receipt reporting a SUBSET is silent about the rest, and silence is not a
    negative.

    That distinction is the #445 blocking direction, which matters as much as
    the clearing one. `authored_cases_verdict`'s dropped-case rule is correct
    for the receipt CLAIMING to satisfy the check, and wrong as a veto: a
    code-reviewer receipt that happened to note the two cases it looked at
    would otherwise turn a `tests_pass` another receipt legitimately satisfied
    into a definitive FAIL, handing any receipt writer a way to stall another
    workstream's story. "It only ever makes things stricter" is not an
    argument for trusting untrusted input.

    With no authored set this is exactly the #187 predicate, unchanged.
    """
    metrics = receipt.get("metrics") or {}
    if not isinstance(metrics, dict):
        metrics = {}
    if not story_authored_case_ids(story or {}):
        return _authored_cases_pass(metrics) is False
    atc = metrics.get("authored_test_cases")
    if not isinstance(atc, dict):
        return False
    results = atc.get("results")
    if not isinstance(results, dict):
        return False
    return any(
        isinstance(r, dict)
        and r.get("status") is not None
        and not _case_result_is_settled(r)
        for r in results.values()
    )


def authored_cases_verdict(
    story: dict[str, Any], receipt: dict[str, Any]
) -> tuple[bool | None, str | None]:
    """The authored-case verdict for one receipt: `(verdict, detail)`.

    This is the PRIMARY evaluation of `tests_pass` (#406, promoting the #187
    fallback veto). Where an authored set exists it is the authority, and the
    runner's exit code becomes a second requirement rather than the first:

    - a case reported `failed` / `blocked` / unknown          -> False
    - an authored case MISSING from the receipt's results     -> False
    - a receipt claiming coverage with no per-case results    -> False
    - execution that pre-dates the producing stage's entry    -> False
    - no `metrics.authored_test_cases` object at all          -> None (gap)
    - ordering unestablishable                                -> None (gap)

    The missing-case rule is the one the #187 veto could not express. That
    veto compared the receipt against ITSELF (`total` vs `len(results)`), so a
    receipt declaring `total: 2` with two passing results satisfied it even
    when the Cycle had authored five cases. Comparing against the sealed
    manifest's ids instead is what makes dropping a case you cannot pass a
    refusal rather than an omission nobody can see.

    With no authored set (declared gap, implicit gap, or a Scrum / Kanban
    story) this falls back to `_authored_cases_pass` unchanged, so the pre-#406
    receipt-internal veto keeps working exactly as it did.
    """
    metrics = receipt.get("metrics") or {}
    if not isinstance(metrics, dict):
        metrics = {}
    story = story or {}
    if story.get("_authored_source") == "untrusted_manifest":
        # The Cycle's sealed manifest does not match its own hash, so what was
        # authored at COMMIT cannot be established. That is unknowable, not
        # disproven: a gap, which still blocks the done edge.
        return None, (
            "tests_pass is unverified: this Cycle's sealed manifest does not "
            "match its own hash, so the acceptance cases authored at COMMIT "
            "cannot be established. The board's own copy is not authority for "
            "this: it is agent-writable, which is why the seal exists. Restore "
            "the manifest, or supersede it with `revise_manifest --reason`."
        )
    if story.get("_authored_source") == "missing_manifest":
        # #507 — no copy of this Cycle's seal can be read, and something other
        # than the missing file says one was sealed. Deleting the document is
        # therefore not the same fact as never having had one, and it resolves
        # the same way tampering does: unknowable, which is a gap, which blocks.
        #
        # Note what this branch is checked BEFORE: `story_authored_case_ids`.
        # A deletion is usually accompanied by an empty board set (the two
        # edits cost one action together), and keying on the board's ids would
        # have made the second edit sufficient to escape the first refusal.
        return None, (
            "tests_pass is unverified: no copy of this Cycle's sealed manifest "
            "can be read -- not the local store, not the committed transport "
            "under `.synaptory/cycles/`, not HEAD -- while %s. A seal that is "
            "MISSING is not a Cycle that never had one, so the acceptance cases "
            "authored at COMMIT cannot be established. Restore it (`git "
            "checkout -- .synaptory/cycles/`, or re-pull the Cycle branch), or "
            "supersede the authored set with `revise_manifest --reason`."
            % (story.get("_authored_seal_witness") or "another record names it")
        )
    authored = story_authored_case_ids(story)
    if not authored:
        return _authored_cases_pass(metrics), None

    atc = metrics.get("authored_test_cases")
    if not isinstance(atc, dict):
        return None, (
            "tests_pass is unverified: this Cycle authored %d acceptance case%s "
            "at COMMIT (%s) and the receipt carries no "
            "`metrics.authored_test_cases` object, so no per-case outcome was "
            "recorded. A suite exit code is not evidence the authored cases "
            "ran." % (
                len(authored),
                "" if len(authored) == 1 else "s",
                ", ".join(authored),
            )
        )
    results = atc.get("results")
    if not isinstance(results, dict) or not results:
        return False, (
            "tests_pass failed: the receipt claims authored-case coverage but "
            "records no per-case results for the %d case%s authored at COMMIT."
            % (len(authored), "" if len(authored) == 1 else "s")
        )

    missing = [cid for cid in authored if cid not in results]
    if missing:
        return False, (
            "tests_pass failed: authored case%s %s %s no recorded outcome. The "
            "authored set is fixed in the sealed Cycle manifest, so a case the "
            "verifying receipt does not report is a DROPPED case, not an "
            "absent one." % (
                "" if len(missing) == 1 else "s",
                ", ".join(missing),
                "has" if len(missing) == 1 else "have",
            )
        )

    # EVERY reported case, not only the authored ids. Iterating `authored`
    # alone would have made the authored path LOOSER than the #187 veto it
    # replaces: that veto read `all(results.values())`, so a case QE added
    # itself and reported `failed` used to fail the gate, and would silently
    # stop doing so under a key the manifest does not name.
    bad = [
        cid for cid in sorted(set(authored) | set(results))
        if not _case_result_is_settled(results.get(cid))
    ]
    if bad:
        return False, (
            "tests_pass failed: case%s %s did not end `passed`, or ended "
            "`not-applicable` with no `reason`. A green runner exit code "
            "cannot clear this. Cases outside the authored set count too: a "
            "case the prover added and could not pass is still a failure."
            % ("" if len(bad) == 1 else "s", ", ".join(bad))
        )

    ordered = authored_execution_after_producer(story or {}, receipt)
    if ordered is False:
        return False, (
            "tests_pass failed: the verifying receipt's `completed_at` (%s) "
            "pre-dates this story's latest entry into the producing stage "
            "(%s), so the authored cases were executed against a candidate "
            "that has since been rebuilt. SP-WRK-007 wants the authored "
            "criteria proven on the candidate under review." % (
                receipt.get("completed_at") or "absent",
                _producing_stage_entered_at(story or {}) or "absent",
            )
        )
    if ordered is None:
        return None, (
            "tests_pass is unverified: the authored cases passed, but the "
            "execution cannot be ordered against the producing stage. Needs "
            "the story's `in_progress` entry timestamp and the receipt's "
            "`completed_at`; one of the two is absent or unparseable, so "
            "whether proving followed producing is unknown."
        )
    return True, None


def _runner_test_verdict(receipt: dict[str, Any]) -> bool | None:
    """The pre-#406 `tests_pass` runner verdict, unchanged.

    False when the receipt records no `verification_commands` at all, None
    when none of them is an executed proof object (#106: a plain string is a
    replay instruction, not evidence), else whether every executed command
    exited 0.
    """
    cmds = receipt.get("verification_commands", [])
    if not cmds:
        return False
    executed = [c for c in cmds if isinstance(c, dict) and "exit_code" in c]
    if not executed:
        return None
    return all(c.get("exit_code") == 0 for c in executed)


def evaluate_tests_pass(
    receipt: dict[str, Any], story: dict[str, Any] | None = None
) -> tuple[bool | None, str | None]:
    """The `tests_pass` verdict, authored cases FIRST (#406).

    Order of authority WHERE CASES WERE AUTHORED, and the ticket is entirely
    about this order:

    1. A definitive authored-case negative wins outright. A failed, blocked or
       DROPPED case fails the check whatever the runner reported, and whether
       or not the runner reported anything at all.
    2. An absent per-case record is a criteria gap. A green exit code does not
       get promoted into a pass just because the receipt carried one: the
       check is "the authored cases pass", and a receipt silent about them has
       not answered it.
    3. Then, and only then, the runner verdict still has to be green. Both are
       required; neither substitutes for the other.

    With NO authored set the pre-#406 behaviour is exact, down to the position
    of the veto. That precision is not fussiness: the veto used to be reachable
    only when the runner verdict was already True, so a receipt with a failed
    authored case and no executed proof object resolved to None (a gap) rather
    than False. Applying the veto unconditionally here would have tightened
    every legacy SPQ unit, every Scrum story and every Kanban story as a side
    effect of a pilot scoped to SPQ (ADR-032 section 6), which is exactly the
    kind of quiet blast radius the epic's forgeability rule is aimed at. An
    exhaustive 56-shape comparison against `origin/dev` found this one
    divergence and it is the reason for the branch below.

    **#507, and it is a correction to the above.** The "no authored set means
    the pre-#406 path" rule was keyed on `story_authored_case_ids`, which reads
    the BOARD. That is fine for a story with no seal and wrong for a story
    whose seal cannot be read, because the board is the agent-writable file the
    seal exists to distrust. Measured consequence on `origin/dev`: tamper with
    the manifest AND empty the board's `acceptance_cases`, and
    `authored_cases_verdict` correctly answers "this Cycle's sealed manifest
    does not match its own hash" while this function DISCARDED that answer and
    returned the runner's green exit code. So the tamper refusal #406 shipped,
    and #505 pinned, was escapable by a second edit to the same file the first
    forgery already required. The seal-derived sentinels are therefore resolved
    FIRST, before anything consults the board.
    """
    story = story or {}
    verdict, detail = authored_cases_verdict(story, receipt)
    runner = _runner_test_verdict(receipt)
    if story.get("_authored_source") in ("untrusted_manifest", "missing_manifest"):
        # Unknowable, whatever the board says and whatever the runner says. The
        # board cannot vote on whether its own authority is readable.
        return None, detail
    if story_authored_case_ids(story):
        if verdict is False:
            return False, detail
        if verdict is None:
            return None, detail
        if runner is not True:
            return runner, None
        return True, None
    # Pre-#406 path: the runner verdict, with the receipt-internal veto folded
    # in exactly where `_evaluate_check` folded it in before — under `if ok`,
    # so a definitive negative on a receipt carrying no executed proof stays
    # the gap it has always been.
    if runner is True and verdict is False:
        return False, None
    return runner, None


def _authored_cases_pass(metrics: dict[str, Any]) -> bool | None:
    """Authored test-case evidence gate (quality.test_cases; receipt protocol
    §Authored test-case evidence).

    Returns None when the receipt carries no `authored_test_cases` object —
    the project has no authored source configured, nothing to enforce.
    Otherwise the object must be internally consistent and every case must
    end `passed` or `not-applicable`:

    - missing/empty `results` while claiming authored coverage ⇒ False
      (claim without per-case evidence);
    - `total` disagreeing with len(results) ⇒ False (silently dropped cases);
    - any case with status `failed`/`blocked`/unknown ⇒ False.
    """
    atc = metrics.get("authored_test_cases")
    if not isinstance(atc, dict):
        return None
    results = atc.get("results")
    if not isinstance(results, dict) or not results:
        return False
    total = atc.get("total")
    if isinstance(total, int) and total != len(results):
        return False
    return all(
        isinstance(r, dict) and r.get("status") in ("passed", "not-applicable")
        for r in results.values()
    )


def _recorded_count(value: Any) -> int | None:
    """A count a receipt actually RECORDED, or None when it recorded no count.

    #540's rule for `coverage_delta`, reused for the two counts on
    `metrics.ui_verification`: a value that cannot be read as a number is not
    a measurement, and neither is a bool. `flows_failed: true` is an `int` in
    Python and would compare `== 0` as "one flow failed" or, worse,
    `routes_tested: true` would satisfy `>= 1`, crediting a claim as a count.
    Returning None keeps the caller's absence branch and its gap.

    It also removes a crash: `int("n/a")` raises, and this ticket makes
    `routes_tested` load-bearing at the `full` tier where it was previously
    ignored, so an unparseable value that used to be inert would otherwise
    take the DoD gate down instead of gapping it.

    TOTAL OVER ARBITRARY JSON, which the first cut was not (#396). It caught
    `ValueError` around `int(float(...))` and nothing else, so `"Infinity"`
    parsed to a non-finite float and `int()` raised `OverflowError`: a valid
    JSON string in a receipt whose `metrics` the validator accepts took
    `evaluate_story_dod` down for every host rather than gapping. A parser
    whose whole job is to make an untrusted value safe has to be the one place
    that cannot raise.

    NON-INTEGRAL IS NOT A COUNT EITHER. `"1.5"` truncated to 1 and satisfied
    `routes_tested >= 1`, so a value the published shape calls an `int` was
    credited by rounding it into one. A count that was not recorded as a whole
    number was not recorded.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return _whole(value)
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except (TypeError, ValueError):
            return None
        return _whole(parsed)
    return None


def _whole(value: float) -> int | None:
    """`value` as an int when it is a finite whole number, else None.

    `math.isfinite` before `is_integer` because `float("nan").is_integer()` is
    False but `float("inf").is_integer()` is False too on some builds and True
    on none of them worth relying on; and `int()` on either raises. One guard,
    stated once, so the two callers cannot drift.
    """
    if not math.isfinite(value) or not float(value).is_integer():
        return None
    try:
        return int(value)
    except (OverflowError, ValueError):  # pragma: no cover - guarded above
        return None


def _evaluate_check(
    check_id: str,
    receipt: dict[str, Any],
    verification_tier: str = "full",
    story: dict[str, Any] | None = None,
) -> bool | None:
    """Evaluate a single DoD check against a receipt.

    Returns True/False when the receipt carries evidence, or None when the
    check is unverifiable from the receipt's shape (#106: agents may write
    `verification_commands` as plain strings = "intent declared, not
    executed". Returning None lets the gate treat the check as unverified
    rather than vacuously passed).

    `verification_tier` (#134 GAP-3) relaxes `ui_acceptance` at the
    `standard` tier to a render assertion; every other check ignores it.

    `story` (#406) is the story's state record, carrying the acceptance cases
    authored at COMMIT. `tests_pass` is evaluated against them FIRST when they
    exist (`evaluate_tests_pass`); absent, the pre-#406 behaviour is exact.
    Optional so the many callers that evaluate a receipt out of board context
    keep working unchanged.
    """
    if check_id == "tests_pass":
        # #406 — authored cases are the primary evaluation, not a veto layered
        # over a runner verdict. `evaluate_tests_pass` owns the whole order.
        return evaluate_tests_pass(receipt, story)[0]

    if check_id == "build_succeeds":
        cmds = receipt.get("verification_commands", [])
        if not cmds:
            return False
        executed = [c for c in cmds if isinstance(c, dict) and "exit_code" in c]
        if not executed:
            return None
        return all(c.get("exit_code") == 0 for c in executed)

    elif check_id == "no_critical_findings":
        # #487 — an ABSENT count is not a clean audit. This branch used to
        # read `metrics.get("findings_critical", 0)`, so a nominal-role
        # receipt that recorded no count at all scored a pass: two member-
        # written strings (`role: compliance-engineer`) satisfying a security
        # gate with no security finding in them. `_MISSING_EVIDENCE_SHAPE`
        # already said the opposite in the text it hands the retry prompt
        # ("a security verdict cannot be inferred from its absence"), and the
        # strict cross-role matcher in `_evaluate_fallback_proof` has always
        # returned None here. The lenient default was the odd one out, and
        # the ticket that turns this into a DECLARED check cannot leave a
        # branch that reads an absent result as a pass (#403's rule: an
        # unbacked claim is unbacked; #445's: a member-supplied result is
        # self-reported, never computed).
        #
        # None, not False: nothing was evaluated. `_classify_dod_checks`
        # stamps `criteria_gap_declared` and `dod_gate_block_reason` refuses
        # the done edge, so the story blocks with the missing-evidence text
        # instead of completing silently.
        metrics = receipt.get("metrics", {})
        if "findings_critical" not in metrics:
            return None
        return metrics.get("findings_critical") == 0

    elif check_id == "runtime_verified":
        # #30 — fail-closed like tests_pass: a string note ("deployed and
        # checked") is intent, not proof. Require a structured
        # `metrics.runtime_verification` object that records the stack was
        # actually deployed and live logs/URLs/audit were inspected.
        metrics = receipt.get("metrics", {})
        rv = metrics.get("runtime_verification")
        if not isinstance(rv, dict):
            return None  # not performed / unverifiable
        return bool(rv.get("deployed")) and bool(rv.get("logs_inspected"))

    elif check_id == "ui_acceptance":
        # #44 — fail-closed like runtime_verified: a passing API/unit suite is
        # NOT evidence the user-facing screen renders. Require the QE receipt
        # to carry proof a browser actually exercised the screen(s).
        metrics = receipt.get("metrics", {})
        uv = metrics.get("ui_verification")
        # Preferred contract: an explicit structured proof object written by
        # QE browser-qa — {rendered: true, routes_tested: N, flows_failed: 0}.
        if isinstance(uv, dict):
            if verification_tier == "standard":
                # #134 GAP-3 — render assertion: the screen was served and at
                # least one route exercised; full browser-qa flows are not
                # demanded at this tier (but a recorded failed flow still fails).
                # The absent flow count is WAIVED here, by the tier, in writing.
                # That is what makes it different from the absences below: an
                # absence a tier declares it does not need is not an absence
                # the check is crediting to itself. A flow count that IS
                # recorded and cannot be read is neither, so it gaps.
                if "routes_tested" not in uv:
                    # UNRECORDED IS A GAP, NOT A FAILURE (#396). This returned
                    # False, which asserts that a measurement was taken and
                    # came back bad. The same absence gaps at the `full` tier,
                    # and the contract text says an absent count is
                    # UNEVALUABLE, so the two tiers disagreed about the same
                    # missing evidence and the standard one named the wrong
                    # cause. Both outcomes block; only one sends the operator
                    # to the stage that owes the count. A recorded
                    # `routes_tested: 0` is the real False, below.
                    return None
                routes = _recorded_count(uv.get("routes_tested"))
                flows_failed = (
                    _recorded_count(uv.get("flows_failed"))
                    if "flows_failed" in uv
                    else 0  # waived by this tier, in writing
                )
                if routes is None or flows_failed is None:
                    return None  # recorded and unreadable is not a measurement
                return bool(uv.get("rendered")) and routes >= 1 and flows_failed == 0
            # #540's census, applied to the tier that demands the MOST. The
            # `full` tier read `rendered and flows_failed == 0` with
            # `flows_failed` defaulting to 0, which is the same
            # `.get(key, 0)` shape #487 removed from `findings_critical`: a
            # receipt that recorded no flow outcomes was credited with zero
            # failed flows. It ignored `routes_tested` outright, so
            # `{"rendered": true}` alone scored the gate whose own
            # `_MISSING_EVIDENCE_SHAPE` text demands
            # {rendered, routes_tested, flows_failed}. Two consequences, both
            # live on `dev` before this: the check disagreed with its own
            # documented shape, and `full` was strictly WEAKER than the
            # `standard` relaxation of it, since only `standard` insisted a
            # route was exercised.
            #
            # None, not False, and for #540's reason: nothing was counted, so
            # this is a criteria gap that routes to QE rather than an assertion
            # that a flow failed. A recorded failure still fails.
            routes = _recorded_count(uv.get("routes_tested"))
            flows_failed = _recorded_count(uv.get("flows_failed"))
            if routes is None or flows_failed is None:
                return None
            return bool(uv.get("rendered")) and routes >= 1 and flows_failed == 0
        # Back-compat: the legacy browser-qa metric shape (routes_tested /
        # flows_passed / flows_failed) is also accepted as browser evidence.
        # `flows_failed` is required here at every tier: `standard` waived the
        # flow count on the STRUCTURED object #134 defined, and a legacy
        # browser-qa run that reports routes and omits its flow outcomes is
        # the plain absence again, one shape away.
        routes = metrics.get("routes_tested")
        if isinstance(routes, (int, float)) and not isinstance(routes, bool) and routes > 0:
            flows_failed = _recorded_count(metrics.get("flows_failed"))
            if flows_failed is None:
                return None
            return flows_failed == 0
        return None  # no browser/e2e verification recorded → unverified

    elif check_id == "integration_verified":
        # #44 phase 5 — evaluate this single receipt's `integrations[]`. The
        # story-level path (`evaluate_story_dod`) aggregates across all receipts;
        # this single-receipt form keeps `_evaluate_check` uniform for callers
        # that pass the carrying receipt directly.
        return evaluate_integration_claims([receipt])

    elif check_id == "code_reviewed":
        # #134 GAP-1b — accept EITHER the top-level status contract OR the
        # story_dod boolean the CR template has always taught. The two-sided
        # fix: templates now also write `status: "complete"`.
        if receipt.get("status") == "complete":
            return True
        story_dod = receipt.get("story_dod")
        if isinstance(story_dod, dict) and story_dod.get("code_reviewed") is True:
            return True
        return False

    elif check_id == "coverage_no_decrease":
        # #540. The same defect #487 fixed on `no_critical_findings`, left
        # standing on the neighbour. This branch defaulted the delta to `""`,
        # failed to parse it, and returned True with the comment "cannot
        # determine, assume pass". So a receipt that measured nothing scored a
        # coverage gate, and `_MISSING_EVIDENCE_SHAPE` said the opposite in the
        # text it hands the retry prompt. `evidence-contract.json` had codified
        # the lenient default as intended behaviour, which is precisely how
        # #487's half survived review.
        #
        # None, not False: nothing was evaluated, so this is a criteria gap
        # rather than a measured regression. `_classify_dod_checks` stamps
        # `criteria_gap_declared` and the done edge is refused, which is what
        # #403 and #494 require of an unbacked claim. False would say coverage
        # dropped, which is a different and unsupported assertion.
        metrics = receipt.get("metrics")
        if not isinstance(metrics, dict) or "coverage_delta" not in metrics:
            return None
        delta = metrics["coverage_delta"]
        # A bool is an int in Python, and `coverage_delta: true` is a claim
        # rather than a measurement.
        if isinstance(delta, bool):
            return None
        if isinstance(delta, (int, float)):
            return delta >= 0
        if isinstance(delta, str):
            # "+3.2%" or "-1.5%". An unparseable string is not a measurement
            # either, so it gaps rather than passing.
            try:
                return float(delta.strip().replace("%", "")) >= 0
            except ValueError:
                return None
        return None

    return False


# ---------------------------------------------------------------------------
# Typed evidence classification (#403)
# ---------------------------------------------------------------------------


def _attempt_id_of(receipt: Any) -> str | None:
    """The producing attempt identity on a receipt, or None.

    Shape-checked against the vocabulary's own pattern rather than a local
    copy of it, so the kernel and the validator agree on what an attempt id
    looks like. Lazy import in the `_host_env` idiom: a partial install must
    degrade to "no attempt identity", which under-credits, never over-credits.
    """
    if not isinstance(receipt, dict):
        return None
    attempt_id = receipt.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        return None
    try:
        from runtime_contracts import ATTEMPT_ID_RE
    except ImportError:  # pragma: no cover - partial install
        return None
    return attempt_id if ATTEMPT_ID_RE.match(attempt_id) else None


def backing_evidence_class(
    entry: dict[str, Any], receipt: dict[str, Any] | None
) -> str | None:
    """The evidence class that ACTUALLY backs a DoD check entry, or None.

    Derived from what the pipeline observed, never read from a payload. This
    is the local half of the #432 rule: a class is credited because something
    in this process produced evidence of that class, not because a receipt
    named one.

      `replayed` — the #179 E1 DoD-time replay re-executed this check's
                   commands through `verification_runner.py` and they
                   reproduced. That runner IS the replayed class, so a
                   verdict it confirmed is the one case where a machine can
                   re-derive the answer.
      `attested` — the check resolved from a receipt that carries its
                   producing attempt identity, so the attestation is bound to
                   one bounded execution (the proposal's own example of
                   attested evidence is a review verdict).
      None       — everything else, including a check that resolved from a
                   receipt with no attempt id. Depth of zero shows as zero
                   (proposal 3.3 / the 13.3 shape); it does not round up to
                   the weakest class that would fit.

    A check that was never evaluated has no class either: `passed is None`
    with no replay and no attempt id is a gap, and a gap is backed by
    nothing by definition.
    """
    replay = entry.get("replay")
    if isinstance(replay, dict) and replay.get("skipped") is False:
        if replay.get("passed") is True and entry.get("passed") is True:
            return "replayed"
    if entry.get("passed") is None and not entry.get("definitive_negative"):
        return None
    return "attested" if _attempt_id_of(receipt) else None


def _self_reported_check_results(receipts: list[dict[str, Any]]) -> dict[str, dict]:
    """Typed DoD check results the RECEIPTS claim, keyed by check id.

    A member-supplied check result is self-reported and never computed
    (#445). It is parsed here so the record shows what was claimed, and it is
    written to a `self_reported` slot that no gate reads: it can neither clear
    a check nor block one. Both directions matter. Letting a claim clear a
    check is the obvious forgery; letting one BLOCK a check would hand any
    writer of an unrestricted payload a way to stall another workstream's
    story, and "it only ever makes things stricter" is not an argument for
    trusting an untrusted input.

    Malformed entries are dropped rather than reported: `receipt_validator`
    already refuses them at the receipt boundary with a message naming the
    field, and a second, quieter refusal here would only mean the same
    payload was described two ways.
    """
    out: dict[str, dict] = {}
    try:
        from runtime_contracts import validate_dod_check_result
    except ImportError:  # pragma: no cover - partial install
        return {}
    for receipt in receipts:
        if not isinstance(receipt, dict):
            continue
        items = receipt.get(DOD_CHECK_RESULTS_KEY)
        if not isinstance(items, list):
            continue
        for item in items:
            if validate_dod_check_result(item):
                continue
            key = str(item.get("check_id", "")).strip().lower().replace("-", "_")
            if key not in DOD_CHECKS:
                continue
            claim: dict[str, Any] = {"result": item["result"]}
            if isinstance(item.get("detail"), str) and item["detail"].strip():
                claim["detail"] = item["detail"]
            out[key] = claim
    return out


def _classify_dod_checks(
    checks: dict[str, dict[str, Any]],
    receipts: list[dict[str, Any]],
    receipt_by_source: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Stamp `result` on every check and return the typed result list (#403).

    Three results, and the third is the point of the ticket:

      pass                   — required, and the evidence says yes.
      fail                   — required, and the evidence says no. Includes a
                               required check whose `passed` is None because
                               of a definitive negative (a failed authored
                               test case): evidence exists, and it refuses.
      criteria_gap_declared  — required, and there is no evidence this check
                               can be evaluated from at all. Carries the
                               missing-evidence description, and NEVER reads
                               as a pass: `passed` stays None, so every
                               existing consumer still sees "not passed", and
                               `dod_gate_block_reason` blocks the done edge
                               on it.

    A check that is not required at this tier (or was waived to N/A) gets no
    result and no entry: it was not evaluated, and inventing a result for it
    would be the same silent-nothing this result exists to remove.

    The typed result list is derived HERE, from the pipeline's own verdicts.
    A receipt-supplied claim is merged per check into a `self_reported` slot
    and cannot change `result` in either direction (#436: computed verdicts
    outrank typed claims; #445: a member-supplied result is self-reported).
    Merging per check rather than per story is the #432 amplification fix
    restated locally: a claim about one check must leave the other four
    computed verdicts exactly as they were.
    """
    receipt_by_source = receipt_by_source or {}
    claimed = _self_reported_check_results(receipts)
    typed: list[dict[str, Any]] = []
    for check_id, entry in checks.items():
        claim = claimed.get(check_id)
        if claim is not None:
            # Recorded beside the computed verdict, never instead of it.
            entry["self_reported"] = dict(claim)
        if not entry.get("required"):
            entry.pop("result", None)
            entry.pop("evidence_class", None)
            continue
        passed = entry.get("passed")
        source = entry.get("source")
        if passed is True:
            result = DOD_RESULT_PASS
        elif passed is False or entry.get("definitive_negative"):
            result = DOD_RESULT_FAIL
        else:
            result = CRITERIA_GAP_DECLARED
            if not entry.get("detail"):
                entry["detail"] = criteria_gap_detail(check_id, source=source)
        entry["result"] = result
        entry["evidence_class"] = backing_evidence_class(
            entry, receipt_by_source.get(source or "")
        )
        item: dict[str, Any] = {"check_id": check_id, "result": result}
        if entry.get("detail"):
            item["detail"] = entry["detail"]
        if entry.get("evidence_class"):
            item["evidence_class"] = entry["evidence_class"]
        typed.append(item)
    return typed


# ---------------------------------------------------------------------------
# State File I/O (for standalone CLI use)
# ---------------------------------------------------------------------------

def _state_path(project_dir: str) -> str:
    # Delegated to spec_state so the path stays consistent with the other
    # state machines (also supports multi-spec layout — see spec_state.py).
    from spec_state import state_path
    return state_path(project_dir)


def _is_spq(full: dict[str, Any]) -> bool:
    return str(full.get("build_mode") or "").lower() == "spq"


def _project_build_mode(project_dir: str | os.PathLike) -> str:
    """The project's lifecycle, or "" when it cannot be read.

    Mirrors `advance_kernel.build_mode`'s two lookups (the top-level pointer,
    then the active spec slot) without its ValueError on an unknown value:
    the caller here is `evaluate_story_dod`, and a DoD evaluation must not
    fail because the board is unreadable. An empty string resolves the
    compliance owner to `ce`, which is the pre-#487 behaviour, so the
    degraded path is the old path rather than a new one.
    """
    try:
        from spec_state import read_full_state

        full = read_full_state(str(project_dir)) or {}
        mode = str(full.get("build_mode") or "").lower()
        if mode:
            return mode
    except Exception:
        pass
    try:
        return str(_read_state(str(project_dir)).get("build_mode") or "").lower()
    except Exception:
        return ""


def _read_state(project_dir: str) -> dict[str, Any]:
    """Return the flat board view, whichever store owns it.

    Three stores, and every consumer in this module (DoD aggregation, the
    barrier's local view, the CLI verbs) has to see the SAME one, or they
    disagree about what is on the board:

        SPQ            the native SPQ store, keyed on cycle+workstream
                       identity (#303/#304/#305). Never `active_spec`.
        multi-spec     the spec named by `SYNAPTORY_ACTIVE_SPEC` or the file's
                       `active_spec`. Unchanged for scrum/kanban.
        flat           the top-level dict.

    Routing lives here rather than in each caller because `sync_barrier` read
    the board through this function while SPQ wrote it to the native store, and
    the result was the barrier reporting "local state is on Cycle 0" for a
    hydrated Cycle -- a divergence that looks like data loss.
    """
    from spec_state import read_full_state, read_state as ss_read, is_multispec
    full = read_full_state(project_dir)
    if not full:
        return {"current_stories": []}
    if _is_spq(full):
        try:
            import spq_state_machine

            return spq_state_machine.read_state(project_dir)
        except Exception:  # noqa: BLE001 - fall back to the pointer contents
            return full
    if is_multispec(full):
        sid = os.environ.get("SYNAPTORY_ACTIVE_SPEC") or None
        return ss_read(
            project_dir, spec_id=sid,
            default_state_factory=lambda: {"current_stories": []},
        )
    return full


def _write_state(project_dir: str, state: dict[str, Any]) -> None:
    from spec_state import read_full_state, write_state as ss_write

    if _is_spq(read_full_state(project_dir)) or _is_spq(state):
        import spq_state_machine

        spq_state_machine._write_state(project_dir, state)
        return
    ss_write(project_dir, state)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python3 story_pipeline.py <action> <project_dir> [args]", file=sys.stderr)
        sys.exit(1)

    action = sys.argv[1]
    project_dir = sys.argv[2]
    args = sys.argv[3:]

    try:
        if action == "create_story":
            if not args:
                _die(
                    "Usage: create_story <project_dir> <story_id> "
                    "[--title '...'] [--acceptance-criteria '[\"...\"]'] "
                    "[--kind enabler|infra|backend|api|ui|mixed] "
                    "[--labels '[\"...\"]'] [--depends-on '[\"US-001\"]'] "
                    "[--file-scope '[\"app/api/\"]']"
                )
            story_id = args[0]
            title = _parse_flag(args, "--title", "")
            acceptance_criteria = _parse_json_flag(args, "--acceptance-criteria")
            with _state_transaction(project_dir):
                state = _read_state(project_dir)
                story = create_story(
                    story_id,
                    title,
                    acceptance_criteria=acceptance_criteria,
                    kind=_parse_flag(args, "--kind", ""),
                    labels=_parse_json_flag(args, "--labels"),
                    depends_on=_parse_json_flag(args, "--depends-on"),
                    file_scope=_parse_json_flag(args, "--file-scope"),
                    project_dir=project_dir,
                )
                state.setdefault("current_stories", []).append(story)
                _write_state(project_dir, state)
            print(json.dumps(story, indent=2))

        elif action == "is_ui_bearing":
            # #44 — deterministic signal for the orchestrator's per-story
            # dispatch: prints "true" when the story's AC describes a
            # user-facing screen (→ SE frontend mode + QE browser-qa), else
            # "false". Reads the ui_bearing flag snapshotted at create_story.
            if not args:
                _die("Usage: is_ui_bearing <project_dir> <story_id>")
            print("true" if _story_is_ui_bearing_in_state(project_dir, args[0]) else "false")

        elif action == "transition":
            if len(args) < 2:
                _die("Usage: transition <project_dir> <story_id> <to_state> [--reason '...']")
            story_id, to_state = args[0], args[1]
            reason = _parse_flag(args, "--reason", None)
            forced = "--force-recovery" in args

            with _state_transaction(project_dir):
                state = _read_state(project_dir)
                story_now = get_story(state, story_id)
                from_state = str((story_now or {}).get("state") or "")
                refused = _cli_receipt_gated_refusal(
                    from_state,
                    to_state,
                    story_id,
                    forced=forced,
                    reason=reason,
                    project_dir=project_dir,
                )
                if refused:
                    _die(refused)

                state = transition_story(state, story_id, to_state, reason, project_dir)
                _write_state(project_dir, state)
                story = get_story(state, story_id)
            print(json.dumps(story, indent=2))

        elif action == "unblock":
            if not args:
                _die("Usage: unblock <project_dir> <story_id>")
            story_id = args[0]

            with _state_transaction(project_dir):
                state = _read_state(project_dir)
                state = unblock_story(state, story_id, project_dir)
                _write_state(project_dir, state)
            story = get_story(state, story_id)
            print(json.dumps(story, indent=2))

        elif action == "record_retry":
            # Loop Engineering P1 (epic #75): the recover_blocked step in
            # modes/sprint.md must record a retry before re-dispatching so the
            # H3-F1 ladder actually escalates (retry_same_prompt →
            # retry_augmented_prompt → block) instead of reading count 0
            # forever. Exposes the record_retry() function as a CLI verb.
            if len(args) < 3:
                _die(
                    "Usage: record_retry <project_dir> <story_id> <role_abbrev> "
                    "<failure_reason>"
                )
            story_id, role_abbrev = args[0], args[1]
            failure_reason = args[2]
            with _state_transaction(project_dir):
                state = _read_state(project_dir)
                new_count = record_retry(
                    state, story_id, role_abbrev, failure_reason, project_dir
                )
                _write_state(project_dir, state)
            rec = recommend_recovery_action(state, story_id, role_abbrev)
            print(json.dumps({"story_id": story_id, "role": role_abbrev,
                              "retry_count": new_count, "recovery": rec}, indent=2))

        elif action == "get_story":
            if not args:
                _die("Usage: get_story <project_dir> <story_id>")
            story_id = args[0]

            state = _read_state(project_dir)
            story = get_story(state, story_id)
            if story is None:
                print(json.dumps({"error": f"Story not found: {story_id}"}))
                sys.exit(1)
            print(json.dumps(story, indent=2))

        elif action == "list_stories":
            if not args:
                _die("Usage: list_stories <project_dir> <state>")
            target = args[0]

            state = _read_state(project_dir)
            stories = list_stories_by_state(state, target)
            print(json.dumps(stories, indent=2))

        elif action == "evaluate_dod":
            if not args:
                _die("Usage: evaluate_dod <project_dir> <story_id> [--sprint N] [--ticket-number N] [--release]")
            story_id = args[0]
            sprint_str = _parse_flag(args, "--sprint", None)
            ticket_str = _parse_flag(args, "--ticket-number", None)
            is_release = "--release" in args

            sprint_num = int(sprint_str) if sprint_str else None
            ticket_num = int(ticket_str) if ticket_str else None
            intensity = determine_dod_intensity(sprint_num, ticket_num, is_release)

            with _state_transaction(project_dir):
                result = evaluate_story_dod(project_dir, story_id, intensity)

                # Store result in state.
                state = _read_state(project_dir)
                story = _find_story(state, story_id)
                if story:
                    story["dod"] = result
                    _write_state(project_dir, state)
                    # #199 — the verdict is now persisted, so it is authoritative;
                    # ship it or /quality keeps scoring self-assessment only.
                    ship_evaluated_dod(story_id, result, project_dir=project_dir)

            print(json.dumps(result, indent=2))

        elif action == "aggregate_dod":
            state = _read_state(project_dir)
            result = aggregate_sprint_dod(state)
            print(json.dumps(result, indent=2))

        elif action == "next_action":
            # Loop Engineering P1 (epic #75): deterministic "what's next".
            # Spec-aware via _read_state; receipts resolved the same way DoD
            # evaluation resolves them so the two can never disagree.
            state = _read_state(project_dir)
            result = next_action(
                state,
                per_story_acceptance=is_per_story_acceptance_enabled(project_dir),
                receipts_dir=_resolve_receipts_dir(project_dir),
                verification_loops=verification_loops_active(project_dir),
                dod_tier_info=resolve_dod_tier(project_dir, state),
                parallelism=parallelism_config(project_dir),
            )
            # #501 follow-up — replace the tier-only `dod` block with the
            # per-story contract the gate will actually enforce. Done here (and
            # in each state-machine wrapper) rather than inside `next_action`,
            # which has no project_dir and stays pure.
            attach_dispatch_dod_contract(
                result,
                project_dir,
                state=state,
                receipts_dir=_resolve_receipts_dir(project_dir),
            )
            result["spec_id"] = state.get("_spec_id")
            # #134 GAP-12 — resume-first: a dispatch with no receipt but
            # partial deliverables on disk means a prior run was interrupted.
            # Tier 0 of the recovery ladder resumes the same role instead of
            # re-dispatching from scratch (and records NO retry).
            if (
                str(result.get("action", "")).startswith("dispatch_")
                and not result.get("receipt_present")
                and result.get("story_id")
            ):
                evidence = detect_interrupted_dispatch(
                    project_dir, str(result["story_id"])
                )
                if evidence:
                    result["resume_candidate"] = True
                    result["resume_evidence"] = evidence
            print(json.dumps(result, indent=2))

        elif action == "set_dod_tier":
            # #134 GAP-11 — record the sprint's DoD tier as an explicit
            # planning decision (Sprint Planning announces it; this persists
            # it and emits the gate event).
            if not args:
                _die(
                    "Usage: set_dod_tier <project_dir> <tier> "
                    "--decided-by <who> [--reason '...'] [--sprint N]"
                )
            tier = args[0]
            decided_by = _parse_flag(args, "--decided-by", "orchestrator")
            reason = _parse_flag(args, "--reason", "")
            sprint_str = _parse_flag(args, "--sprint", None)
            with _state_transaction(project_dir):
                state = _read_state(project_dir)
                record = set_dod_tier(
                    state, tier, decided_by, reason,
                    sprint=int(sprint_str) if sprint_str else None,
                    project_dir=project_dir,
                )
                _write_state(project_dir, state)
            print(json.dumps(record, indent=2))

        elif action == "dod_tier":
            # #134 GAP-11 — resolved tier + source + active base checks.
            # `tier_source: "computed"` means no planning decision was
            # recorded — the silent-promotion tell.
            state = _read_state(project_dir)
            print(json.dumps(resolve_dod_tier(project_dir, state), indent=2))

        elif action == "progress_digest":
            # Loop Engineering P1: story-board progress hash for the P2
            # Stop-hook runaway guard (same digest across two stops = spin).
            state = _read_state(project_dir)
            print(progress_digest(state))

        elif action == "cycle_time":
            if not args:
                _die("Usage: cycle_time <project_dir> <story_id>")
            story_id = args[0]

            state = _read_state(project_dir)
            story = get_story(state, story_id)
            if story is None:
                print(json.dumps({"error": f"Story not found: {story_id}"}))
                sys.exit(1)
            result = calculate_story_cycle_time(story)
            print(json.dumps(result, indent=2))

        else:
            _die(f"Unknown action: {action}")

    except ValueError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


def _parse_flag(args: list[str], flag: str, default: Any) -> Any:
    """Parse a --flag value from args list."""
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
    return default


def _parse_json_flag(args: list[str], flag: str) -> Any:
    """Parse a --flag whose value is a JSON document; dies on invalid JSON."""
    raw = _parse_flag(args, flag, None)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        _die(f"{flag} must be valid JSON")


def _die(msg: str) -> None:
    print(msg, file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
