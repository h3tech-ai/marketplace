#!/usr/bin/env python3
"""Receipt JSON validator for synaptory hooks.

Validates receipt files against the v2 story-scoped receipt protocol:
- Required fields: story_id, role, backend, model, artifacts,
  verification_commands, metrics, completed_at
- Artifacts exist on disk and are non-empty
- Metrics is non-empty dict
- verification_commands: list of simple command strings or command objects
- Soft-required (warn-now-error-later): token_usage. Drives /cost.
- Soft-required for SE/QE: story_dod. Drives /quality Evidence gates.
- Optional: confidence, fallback_from, design_ref + design_verified +
  design_impl_notes

CLI: python3 receipt_validator.py <receipt.json> <project_dir>
"""

import json
import os
import re
import sys
from typing import Any, Optional

try:  # pragma: no cover - import plumbing
    import transcript_usage as _transcript_usage  # type: ignore
except ImportError:  # pragma: no cover
    try:
        from hooks.lib import transcript_usage as _transcript_usage  # type: ignore
    except ImportError:
        _transcript_usage = None  # type: ignore[assignment]


def _token_usage_value(tu: dict, canonical: str):
    """Return the count for a canonical Cost key, accepting SDK aliases."""
    aliases = {
        "input": ("input", "input_tokens", "uncached_input_tokens", "prompt_tokens"),
        "output": ("output", "output_tokens", "completion_tokens"),
        "cache_read": ("cache_read", "cache_read_input_tokens", "cache_read_tokens"),
        "cache_write": (
            "cache_write",
            "cache_write_tokens",
            "cache_creation_input_tokens",
        ),
    }
    if _transcript_usage is not None:
        aliases = getattr(_transcript_usage, "TOKEN_USAGE_ALIASES", aliases)
    for k in aliases.get(canonical, (canonical,)):
        if k in tu:
            return tu[k]
    return None


# Evidence contract (issue #163) — optional sibling module. Used to map a
# receipt's role to its single canonical v3 stage so that a missing or
# descriptive `token_usage.stage` on a known single-stage role is NOT warned
# about (the SubagentStop hook backfills the canonical stage via
# transcript_usage.patch_receipt before shipping). Missing module → the
# pre-contract warning behavior applies unchanged.
try:  # pragma: no cover - import plumbing
    import evidence_contract as _evidence_contract  # type: ignore
except ImportError:  # pragma: no cover
    try:
        from hooks.lib import evidence_contract as _evidence_contract  # type: ignore
    except ImportError:
        _evidence_contract = None  # type: ignore[assignment]

# Hardcoded defaults — used when no cached policy is available.
_DEFAULT_REQUIRED_FIELDS = {
    "story_id", "role", "backend", "model",
    "artifacts", "verification_commands", "metrics", "completed_at",
}
_DEFAULT_VALID_BACKENDS = {"claude", "cursor", "codex", "gemini"}
_DEFAULT_VALID_ROLES = {
    "software-engineer", "quality-engineer", "code-reviewer",
    "compliance-engineer", "project-owner", "solution-architect",
    "platform-engineer", "technical-writer", "research-advisor",
    "orchestrator",  # top-level routing + inline orchestrator receipts (e.g. Inception
                     # mockup baseline). Already a valid role in receipt-protocol.md and a
                     # valid stage in VALID_V3_STAGES; server-side analytics accept it.
}

# `product-manager` is NOT here, and must not be re-added (#198). ADR-019
# retired the runtime alias, migration 20260428_0005 backfilled existing rows
# to `project-owner`, and 20260507_0008 added a CHECK constraint
# (`ck_receipts_no_legacy_product_manager`) that REJECTS the value at insert.
# Accepting it plugin-side would only wave a receipt through local validation
# so the control plane could refuse it later — a worse failure than warning
# here. Confirmed against prod: 0 receipts carry it.
_RETIRED_ROLES = {"product-manager": "project-owner"}

VALID_CONFIDENCE_LEVELS = {"high", "medium", "low"}

# Per-stage taxonomy — must mirror api/synaptory_api/routers/analytics.py V3_STAGES.
# Receipts that omit token_usage.stage or use an unknown value fall through
# to "unknown" server-side and don't roll up under any stage tab.
VALID_V3_STAGES = {
    "pro-discovery", "pro-brd", "pro-ux-spec",
    "sa-architecture",
    "se-implementation",
    "qe-verification",
    "cr-review",
    "ce-compliance",
    "pe-infra",
    "tw-docs",
    "orchestrator",
}

# Unambiguous role → v3 stage map, mirrored by the server-side cost fallback
# (api/synaptory_api/routers/analytics.py ROLE_STAGE_FALLBACK). project-owner
# is deliberately absent — it spans three stages (pro-discovery / pro-brd /
# pro-ux-spec), so the validator cannot pick one for it.
ROLE_STAGE_FALLBACK = {
    "solution-architect": "sa-architecture",
    "software-engineer": "se-implementation",
    "quality-engineer": "qe-verification",
    "code-reviewer": "cr-review",
    "compliance-engineer": "ce-compliance",
    "platform-engineer": "pe-infra",
    "technical-writer": "tw-docs",
    # Documented single-stage roles too: RA's receipt protocol fixes its
    # stage to "orchestrator", and the orchestrator's inline receipts are
    # the orchestrator stage by definition.
    "research-advisor": "orchestrator",
    "orchestrator": "orchestrator",
}

# Invented-but-prefixed stages (observed in the field: "qe-diff-aware",
# "sa-contract-clarification") normalize to their canonical stage. "pro-" is
# absent — ambiguous across the three project-owner stages.
STAGE_PREFIX_FALLBACK = {
    "sa-": "sa-architecture",
    "se-": "se-implementation",
    "qe-": "qe-verification",
    "cr-": "cr-review",
    "ce-": "ce-compliance",
    "pe-": "pe-infra",
    "tw-": "tw-docs",
}


def _canonical_stage_hint(stage, role):
    """Best-effort canonical v3 stage for a missing/invalid ``stage`` value.

    Prefers the receipt's role (authoritative for single-stage roles), then an
    unambiguous stage prefix. Returns None when nothing can be inferred.
    """
    if isinstance(role, str) and role in ROLE_STAGE_FALLBACK:
        return ROLE_STAGE_FALLBACK[role]
    if isinstance(stage, str):
        for prefix, canonical in STAGE_PREFIX_FALLBACK.items():
            if stage.startswith(prefix):
                return canonical
    return None


# Roles that must populate `story_dod` so the Evidence/DoD gate scoreboard
# at /quality has a signal. Other roles MAY populate it; only these
# trigger a warning when it's missing.
ROLES_REQUIRING_STORY_DOD = {"software-engineer", "quality-engineer"}

# Story-DoD checks recognised by the API rollup at
# /v1/admin/analytics/evidence-gates. The validator only warns on unknown
# keys — it does not enforce a closed set, since teams may add custom checks.
KNOWN_STORY_DOD_KEYS = {
    "tests_pass", "build_succeeds", "no_critical_findings",
    "code_reviewed", "coverage_no_decrease",
}


def _load_policy_schema() -> tuple[set, set, set]:
    """Load receipt_schema from the cached policy.json if available.

    Returns (required_fields, valid_roles, valid_backends) — falling back to
    hardcoded defaults when the cache is absent or missing the receipt_schema key.

    #198: ``valid_roles`` is the UNION of the policy's list and the built-in
    defaults, not a replacement. The policy may widen the accepted set; it may
    not narrow it below the roles this plugin itself emits.

    Two reasons. First, published policy version 1 lists nine roles and omits
    `orchestrator`, which the orchestrator legitimately writes (4 such receipts
    in prod) and which server-side analytics accept as a stage — so every
    machine that had fetched the policy warned on a correct receipt, while CI,
    having no cache, did not. That made ``./synaptory test`` pass in CI and fail
    on a logged-in laptop: a test suite whose result depends on login state is
    worse than the warning itself.

    Second, ``VALID_ROLES`` drives a *warning*, never a rejection, so widening
    costs nothing while narrowing produces false alarms. The trade-off is
    explicit: a future policy cannot retire a role by omission alone. Retiring
    one means removing it from ``_DEFAULT_VALID_ROLES`` too — which is exactly
    how `product-manager` was retired (see ``_RETIRED_ROLES``).
    """
    from host_env import state_dir

    cache_path = os.path.join(state_dir(), ".cache", "policy.json")
    try:
        with open(cache_path, "r") as f:
            cached = json.load(f)
        schema = cached.get("payload", {}).get("receipt_schema", {})
        required = set(schema.get("required_fields", [])) or _DEFAULT_REQUIRED_FIELDS
        roles = set(schema.get("valid_roles", [])) | _DEFAULT_VALID_ROLES
        # Union, not replace — a published policy that only lists `claude`
        # must not make Cursor-host receipts (`cursor`/`codex`/`gemini`) warn.
        backends = set(schema.get("valid_backends", [])) | _DEFAULT_VALID_BACKENDS
        return required, roles, backends
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return _DEFAULT_REQUIRED_FIELDS, _DEFAULT_VALID_ROLES, _DEFAULT_VALID_BACKENDS


REQUIRED_FIELDS, VALID_ROLES, VALID_BACKENDS = _load_policy_schema()

# story_id pattern: US-NNN, TICKET-NNN, INFRA-NNN, BUG-NNN, etc.
STORY_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")


def infer_backend_from_model(model: str) -> Optional[str]:
    """Map a model id to a receipt `backend` without guessing `claude`.

    Returns None when the id is empty or unrecognized (omit rather than invent).
    """
    m = (model or "").strip().lower()
    if not m:
        return None
    if "claude" in m or m.startswith("anthropic"):
        return "claude"
    if "gemini" in m:
        return "gemini"
    if (
        "codex" in m
        or m.startswith("gpt-")
        or m.startswith("o1")
        or m.startswith("o3")
        or m.startswith("o4")
    ):
        return "codex"
    if "composer" in m or "grok" in m or "cursor" in m:
        return "cursor"
    return None


class ValidationResult:
    """Result of receipt validation."""

    def __init__(self) -> None:
        self.valid = True
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)
        self.valid = False

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def validate_receipt(receipt_path: str, project_dir: str) -> ValidationResult:
    """Validate a receipt JSON file against the v2 receipt protocol schema.

    Args:
        receipt_path: Absolute path to receipt JSON file.
        project_dir: Project root directory (for resolving artifact paths).

    Returns:
        ValidationResult with valid flag, errors list, and warnings list.
    """
    result = ValidationResult()

    try:
        with open(receipt_path, "r") as f:
            receipt = json.load(f)
    except FileNotFoundError:
        result.error(f"Receipt file not found: {receipt_path}")
        return result
    except json.JSONDecodeError as e:
        result.error(f"Invalid JSON in receipt: {e}")
        return result

    return validate_receipt_payload(receipt, project_dir)


def validate_receipt_payload(receipt: Any, project_dir: str) -> ValidationResult:
    """Validate an already-parsed receipt object.

    Callers that have read the file once (the advance kernel hashes those
    same bytes) must validate this payload rather than reopening the path,
    otherwise the digest and the validated receipt can describe different
    on-disk content.
    """
    result = ValidationResult()
    if not isinstance(receipt, dict):
        result.error("Receipt must be a JSON object")
        return result

    # Check required fields
    missing = REQUIRED_FIELDS - set(receipt.keys())
    if missing:
        result.error(f"Missing required fields: {', '.join(sorted(missing))}")

    # Validate story_id
    if "story_id" in receipt:
        story_id = receipt["story_id"]
        if not isinstance(story_id, str) or not story_id.strip():
            result.error("'story_id' must be a non-empty string")
        elif not STORY_ID_PATTERN.match(story_id):
            result.warn(
                f"'story_id' format '{story_id}' does not match expected pattern "
                "(e.g., US-001, TICKET-042, INFRA-003)"
            )

    # Validate role
    if "role" in receipt:
        role = receipt["role"]
        if not isinstance(role, str):
            result.error("'role' must be a string")
        elif role in _RETIRED_ROLES:
            # Naming the successor matters here: this value is rejected by a DB
            # CHECK constraint at ingest, so a generic "unrecognized" warning
            # would leave the writer guessing why the receipt later vanished.
            result.warn(
                f"Retired role: '{role}' — use "
                f"'{_RETIRED_ROLES[role]}' instead. The control plane REJECTS "
                f"'{role}' at ingest (ADR-019; constraint "
                "ck_receipts_no_legacy_product_manager), so this receipt will "
                "not be recorded."
            )
        elif role not in VALID_ROLES:
            result.warn(
                f"Unrecognized role: '{role}' (expected one of {sorted(VALID_ROLES)})"
            )

    # Validate backend
    if "backend" in receipt:
        backend = receipt["backend"]
        if not isinstance(backend, str):
            result.error("'backend' must be a string")
        elif backend not in VALID_BACKENDS:
            result.warn(
                f"Unrecognized backend: '{backend}' (expected one of {sorted(VALID_BACKENDS)})"
            )

    # Validate model
    if "model" in receipt:
        model = receipt["model"]
        if not isinstance(model, str):
            result.error("'model' must be a string")
        elif not model.strip():
            result.warn("'model' is empty — should identify the specific model used")

    # Honest backend↔model mapping (Cursor Router + multi-host receipts).
    # `composer-*` / `grok-*` with `backend: claude` is a lie — fail closed.
    backend_val = receipt.get("backend")
    model_val = receipt.get("model")
    if isinstance(backend_val, str) and isinstance(model_val, str) and model_val.strip():
        inferred = infer_backend_from_model(model_val)
        if inferred and inferred != backend_val:
            result.error(
                f"backend '{backend_val}' does not match model '{model_val}' "
                f"(inferred backend '{inferred}'). Do not invent backend: claude."
            )

    # Validate artifacts
    if "artifacts" in receipt:
        artifacts = receipt["artifacts"]
        if not isinstance(artifacts, list):
            result.error("'artifacts' must be a list")
        elif len(artifacts) == 0:
            result.error("'artifacts' must be non-empty — list every file the agent produced")
        else:
            for artifact_path in artifacts:
                if not isinstance(artifact_path, str):
                    result.error(f"Artifact path must be string, got: {type(artifact_path).__name__}")
                    continue
                full_path = os.path.join(project_dir, artifact_path)
                if not os.path.exists(full_path):
                    result.error(f"Artifact not found on disk: {artifact_path}")
                elif os.path.isfile(full_path) and os.path.getsize(full_path) == 0:
                    result.error(f"Artifact is empty (0 bytes): {artifact_path}")

    # Validate metrics
    if "metrics" in receipt:
        metrics = receipt["metrics"]
        if not isinstance(metrics, dict):
            result.error("'metrics' must be a dict")
        elif len(metrics) == 0:
            result.error("'metrics' must be non-empty — include at least one concrete number")

    # Required: verification_commands — every agent must provide machine-verifiable proof
    if "verification_commands" in receipt:
        cmds = receipt["verification_commands"]
        if not isinstance(cmds, list):
            result.error("'verification_commands' must be a list")
        elif len(cmds) == 0:
            result.error(
                "'verification_commands' must contain at least one command. "
                "Include commands that prove your work (e.g., test runner, build, file checks)."
            )
        else:
            for i, cmd in enumerate(cmds):
                if isinstance(cmd, str):
                    # v2 simple string format — accepted
                    if not cmd.strip():
                        result.error(f"verification_commands[{i}] is an empty string")
                elif isinstance(cmd, dict):
                    # Legacy dict format — still accepted for transition
                    cmd_missing = {"command", "exit_code", "summary"} - set(cmd.keys())
                    if cmd_missing:
                        result.error(
                            f"verification_commands[{i}] (dict) missing fields: {', '.join(sorted(cmd_missing))}"
                        )
                else:
                    result.error(
                        f"verification_commands[{i}] must be a string or dict, "
                        f"got: {type(cmd).__name__}"
                    )

    # Validate completed_at
    if "completed_at" in receipt:
        completed_at = receipt["completed_at"]
        if not isinstance(completed_at, str) or not completed_at.strip():
            result.error("'completed_at' must be a non-empty ISO 8601 timestamp string")

    # Optional: confidence (recommended for controlled mode)
    if "confidence" in receipt:
        confidence = receipt["confidence"]
        if not isinstance(confidence, dict):
            result.warn("'confidence' should be a dict")
        else:
            if "level" not in confidence:
                result.warn("'confidence' missing 'level' field")
            elif confidence["level"] not in VALID_CONFIDENCE_LEVELS:
                result.warn(
                    f"'confidence.level' should be one of {VALID_CONFIDENCE_LEVELS}, "
                    f"got: '{confidence['level']}'"
                )
            if "reasoning" not in confidence:
                result.warn("'confidence' missing 'reasoning' field")
            if "what_i_cannot_verify" not in confidence:
                result.warn("'confidence' missing 'what_i_cannot_verify' field")

    # Soft-required: story_dod for SE/QE. Without it the Evidence/DoD gate
    # scoreboard at /quality has no signal for that story.
    role_for_dod = receipt.get("role") if isinstance(receipt.get("role"), str) else None
    if "story_dod" in receipt:
        story_dod = receipt["story_dod"]
        if not isinstance(story_dod, dict):
            result.warn("'story_dod' should be a dict mapping DoD check IDs to booleans")
        else:
            for k, v in story_dod.items():
                if not isinstance(v, bool):
                    result.warn(f"'story_dod.{k}' should be a boolean, got: {type(v).__name__}")
                if k not in KNOWN_STORY_DOD_KEYS:
                    result.warn(
                        f"'story_dod.{k}' is not one of the canonical DoD checks "
                        f"({sorted(KNOWN_STORY_DOD_KEYS)}); it will be ignored by the Evidence-gate rollup"
                    )
            # #199: an ABSENT contractual key was previously invisible — only
            # unknown keys and a wholly missing block warned. Across a real
            # 11-day prod window (296 receipts carrying story_dod) that let
            # four of the five canonical checks reach ZERO occurrences while
            # agents invented their own keys, so /quality's Evidence gate was
            # scoring one signal out of five and nothing said so.
            #
            # Reported as one warning rather than one per key: a receipt with
            # only `tests_pass` would otherwise emit four, drowning the rest of
            # the validation output.
            if role_for_dod in ROLES_REQUIRING_STORY_DOD:
                absent = sorted(KNOWN_STORY_DOD_KEYS - set(story_dod))
                if absent:
                    result.warn(
                        f"'story_dod' omits {len(absent)} of the "
                        f"{len(KNOWN_STORY_DOD_KEYS)} canonical DoD checks: "
                        f"{absent}. The /quality Evidence gate has no signal for "
                        "these, so the story scores as unevaluated on them rather "
                        "than passing — report each one explicitly, including "
                        "`false`."
                    )
    elif role_for_dod in ROLES_REQUIRING_STORY_DOD:
        result.warn(
            f"'story_dod' missing — role '{role_for_dod}' should populate it so "
            "/quality Evidence-gate scoreboard has a signal for this story"
        )

    # Soft-required: token_usage. Without it the Cost dashboard at
    # /cost shows $0 for this row even when work was done.
    # Warn-now-error-later: in-flight receipts written by older plugin
    # versions still pass; new receipts SHOULD include the block.
    if "token_usage" in receipt:
        tu = receipt["token_usage"]
        if not isinstance(tu, dict):
            result.warn(
                "'token_usage' should be a dict like "
                "{input, output, cache_read, cache_write, stage}"
            )
        else:
            for k in ("input", "output", "cache_read", "cache_write"):
                v = _token_usage_value(tu, k)
                if v is None:
                    result.warn(f"'token_usage.{k}' missing — defaults to 0 in cost rollup")
                    continue
                if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                    result.warn(
                        f"'token_usage.{k}' should be a non-negative int, got: {v!r}"
                    )
            stage = tu.get("stage")
            stage_valid = (
                isinstance(stage, str) and stage.strip() and stage in VALID_V3_STAGES
            )
            if not stage_valid:
                # GAP-7 (#163) × #158/#159: when a canonical v3 stage can be
                # inferred — from the receipt's role (evidence contract, then
                # ROLE_STAGE_FALLBACK) or an unambiguous stage prefix — the
                # SubagentStop hook backfills it locally before shipping
                # (transcript_usage.patch_receipt) and the CP remaps at
                # ingest as belt-and-braces, so warning here would just be
                # per-receipt noise. The warning is kept only when nothing
                # can be inferred (unknown or multi-stage roles like
                # project-owner).
                canonical_stage = None
                role_for_stage = receipt.get("role")
                if _evidence_contract is not None and isinstance(role_for_stage, str):
                    try:
                        canonical_stage = _evidence_contract.role_stage(role_for_stage)
                    except Exception:
                        canonical_stage = None
                if not canonical_stage:
                    canonical_stage = _canonical_stage_hint(stage, role_for_dod)
                if not canonical_stage:
                    if not isinstance(stage, str) or not stage.strip():
                        result.warn(
                            "'token_usage.stage' missing — row will roll up under 'unknown' "
                            "in /cost by-stage"
                        )
                    else:
                        result.warn(
                            f"'token_usage.stage' '{stage}' is not a v3 stage "
                            f"(expected one of {sorted(VALID_V3_STAGES)})"
                        )
    else:
        result.warn(
            "'token_usage' missing — /cost will show $0 for this receipt. "
            "Populate {input, output, cache_read, cache_write, stage} so spend rolls up."
        )

    # Optional: fallback_from (backend fallback tracing)
    if "fallback_from" in receipt:
        fallback = receipt["fallback_from"]
        if not isinstance(fallback, str):
            result.warn("'fallback_from' should be a string (original backend name)")

    return result


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <receipt.json> <project_dir>", file=sys.stderr)
        sys.exit(1)

    receipt_path = sys.argv[1]
    project_dir = sys.argv[2]
    result = validate_receipt(receipt_path, project_dir)
    print(json.dumps(result.to_dict(), indent=2))
    sys.exit(0 if result.valid else 1)
