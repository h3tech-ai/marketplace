"""Layer 1 — `plugin/hooks/lib/receipt_validator.py` schema tests.

Hypothesis: a receipt's JSON shape decides whether the SubagentStop hook
accepts it. These tests pin the rules:

  * Missing required fields → invalid.
  * Bad story_id pattern → warning, still valid.
  * Unknown role / backend → warning, still valid.
  * Non-JSON / wrong type → invalid.
  * The retired `product-manager` alias WARNS and names its successor
    (#198 — the CP rejects it at ingest via a CHECK constraint).
  * Role resolution is policy UNION defaults, so a narrow published policy
    cannot make the plugin warn on roles it emits itself (#198).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hooks.lib.receipt_validator import (
    REQUIRED_FIELDS,
    VALID_BACKENDS,
    VALID_ROLES,
    ValidationResult,
    validate_receipt,
)


def _good_receipt() -> dict:
    """Minimum viable valid receipt — every required field present.

    Soft-required fields (token_usage, story_dod) are included so the helper
    produces a *clean* receipt for the strict-pass tests. Tests that exercise
    soft-required behaviour pop the field deliberately.
    """
    return {
        "story_id": "US-001",
        "role": "software-engineer",
        "backend": "claude",
        "model": "claude-sonnet-4-5",
        "artifacts": ["src/foo.py"],
        "verification_commands": ["pytest -q"],
        "metrics": {"tests_added": 3},
        "completed_at": "2026-04-29T12:00:00Z",
        "token_usage": {
            "input": 100,
            "output": 50,
            "cache_read": 200,
            "cache_write": 10,
            "stage": "se-implementation",
        },
        "story_dod": {
            "tests_pass": True,
            "build_succeeds": True,
            "no_critical_findings": True,
            "code_reviewed": False,
            "coverage_no_decrease": True,
        },
    }


def _write(tmp_path: Path, receipt: dict, name: str = "US-001-se.json") -> Path:
    """Write the receipt + create on-disk stubs for any artifacts it lists.

    The validator checks every artifact path resolves under project_dir;
    L1 tests use empty stub files so we exercise the schema branches
    without needing a real codebase.
    """
    for artifact in receipt.get("artifacts", []):
        if isinstance(artifact, str):
            full = tmp_path / artifact
            full.parent.mkdir(parents=True, exist_ok=True)
            # Validator rejects 0-byte artifacts; write a non-empty stub.
            full.write_text("# stub artifact for tests\n", encoding="utf-8")
    p = tmp_path / name
    p.write_text(json.dumps(receipt), encoding="utf-8")
    return p


@pytest.mark.unit
def test_required_fields_set_matches_documented():
    """Lock the canonical list — adding/removing a field is a breaking change."""
    assert REQUIRED_FIELDS == {
        "story_id",
        "role",
        "backend",
        "model",
        "artifacts",
        "verification_commands",
        "metrics",
        "completed_at",
    }


@pytest.mark.unit
def test_valid_receipt_passes(tmp_path: Path):
    p = _write(tmp_path, _good_receipt())
    r = validate_receipt(str(p), str(tmp_path))
    assert r.valid, f"unexpected errors: {r.errors}"
    assert not r.errors


@pytest.mark.unit
@pytest.mark.parametrize("missing", sorted(REQUIRED_FIELDS))
def test_missing_required_field_fails(tmp_path: Path, missing: str):
    receipt = _good_receipt()
    receipt.pop(missing)
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    assert not r.valid
    assert any(missing in e for e in r.errors), (
        f"expected error to mention '{missing}', got {r.errors}"
    )


@pytest.mark.unit
def test_bad_story_id_warns_but_remains_valid(tmp_path: Path):
    receipt = _good_receipt()
    receipt["story_id"] = "not-a-real-id"
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    # Schema-wise still valid; the story-id pattern is advisory.
    assert r.valid, f"errors: {r.errors}"
    assert any("story_id" in w for w in r.warnings), (
        f"expected story_id warning, got {r.warnings}"
    )


@pytest.mark.unit
def test_empty_story_id_is_an_error(tmp_path: Path):
    receipt = _good_receipt()
    receipt["story_id"] = ""
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    assert not r.valid
    assert any("story_id" in e for e in r.errors)


@pytest.mark.unit
def test_unknown_role_warns(tmp_path: Path):
    receipt = _good_receipt()
    receipt["role"] = "marketing-manager"
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    # Unknown role is warning-only; not all v3-aligned receipts will be
    # in VALID_ROLES yet.
    assert r.valid
    assert any("role" in w.lower() for w in r.warnings)


@pytest.mark.unit
def test_retired_product_manager_role_warns_and_names_successor(tmp_path: Path):
    """#198 — inverted from its original form, which asserted `product-manager`
    was *silently accepted* as an in-flight legacy alias.

    That expectation is stale and was actively harmful. ADR-019 retired the
    runtime alias, migration 20260428_0005 backfilled existing rows, and
    20260507_0008 added a CHECK constraint that REJECTS the value at insert.
    Confirmed against prod: 0 receipts carry it. Accepting it locally would
    only wave a receipt through validation so the CP could refuse it later, so
    the warning must fire AND name the successor — otherwise the writer has no
    way to connect a clean local validation to a receipt that never appeared.
    """
    receipt = _good_receipt()
    receipt["role"] = "product-manager"
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    role_warnings = [w for w in r.warnings if "role" in w.lower()]
    assert role_warnings, "a role the CP rejects at ingest must not validate silently"
    assert "project-owner" in role_warnings[0], "the warning must name the successor"
    assert "REJECTS" in role_warnings[0]


@pytest.mark.unit
def test_orchestrator_role_accepted(tmp_path: Path):
    """Inline orchestrator receipts (e.g. the Inception mockup baseline) must pass
    without a role warning — orchestrator is a valid role in receipt-protocol.md and a
    valid stage in VALID_V3_STAGES, and server-side analytics accept it."""
    receipt = _good_receipt()
    receipt["role"] = "orchestrator"
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    assert r.valid
    role_warnings = [w for w in r.warnings if "role" in w.lower()]
    assert not role_warnings, (
        f"orchestrator should be silently accepted; got {role_warnings}"
    )


@pytest.mark.unit
def test_cursor_composer_backend_accepted(tmp_path: Path):
    receipt = _good_receipt()
    receipt["backend"] = "cursor"
    receipt["model"] = "composer-2.5"
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    assert r.valid, f"unexpected errors: {r.errors}"
    assert not any("backend" in e.lower() for e in r.errors)


@pytest.mark.unit
def test_grok_model_with_claude_backend_fails(tmp_path: Path):
    receipt = _good_receipt()
    receipt["backend"] = "claude"
    receipt["model"] = "grok-4"
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    assert not r.valid
    assert any("does not match model" in e for e in r.errors)


@pytest.mark.unit
def test_non_json_file_fails(tmp_path: Path):
    p = tmp_path / "garbage.json"
    p.write_text("this is not json {", encoding="utf-8")
    r = validate_receipt(str(p), str(tmp_path))
    assert not r.valid
    assert any("json" in e.lower() for e in r.errors)


@pytest.mark.unit
def test_missing_file_fails(tmp_path: Path):
    r = validate_receipt(str(tmp_path / "nonexistent.json"), str(tmp_path))
    assert not r.valid
    assert any("not found" in e.lower() for e in r.errors)


@pytest.mark.unit
def test_top_level_must_be_object(tmp_path: Path):
    p = tmp_path / "list.json"
    p.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    r = validate_receipt(str(p), str(tmp_path))
    assert not r.valid


@pytest.mark.unit
def test_validation_result_to_dict_shape():
    r = ValidationResult()
    r.warn("foo")
    r.error("bar")
    assert r.to_dict() == {
        "valid": False,
        "errors": ["bar"],
        "warnings": ["foo"],
    }


# --- token_usage soft-required (warn-now-error-later) ----------------------
#
# Drives /cost. Receipts without it produce a warning, not an error,
# so in-flight receipts written by older plugin versions still pass. New
# receipts SHOULD include the block — the warning is the carrot for write
# sites to catch up before the field is promoted to required.


@pytest.mark.unit
def test_token_usage_missing_warns_but_remains_valid(tmp_path: Path):
    receipt = _good_receipt()
    receipt.pop("token_usage")
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    assert r.valid, f"unexpected errors: {r.errors}"
    assert any("token_usage" in w for w in r.warnings), (
        f"expected token_usage warning, got {r.warnings}"
    )


@pytest.mark.unit
def test_token_usage_present_no_warning(tmp_path: Path):
    p = _write(tmp_path, _good_receipt())
    r = validate_receipt(str(p), str(tmp_path))
    assert r.valid
    # No token_usage warning when the block is present and valid.
    token_warnings = [w for w in r.warnings if "token_usage" in w]
    assert not token_warnings, f"unexpected token_usage warnings: {token_warnings}"


@pytest.mark.unit
def test_token_usage_unknown_stage_warns_for_unknown_role(tmp_path: Path):
    """GAP-7 (#163): the stage warning survives ONLY when no canonical stage
    is derivable from the role — an unknown role can't be backfilled."""
    receipt = _good_receipt()
    receipt["role"] = "marketing-manager"
    receipt["token_usage"]["stage"] = "definitely-not-a-stage"
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    assert r.valid  # still warning-level
    assert any("token_usage.stage" in w for w in r.warnings)


@pytest.mark.unit
def test_token_usage_descriptive_stage_known_role_no_warning(tmp_path: Path):
    """GAP-7 (#163): a descriptive (non-enum) stage on a single-stage role is
    NOT warned about — the SubagentStop hook backfills the canonical stage
    via transcript_usage before shipping."""
    receipt = _good_receipt()  # role: software-engineer → se-implementation
    receipt["token_usage"]["stage"] = "implementing the auth endpoints"
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    assert r.valid
    stage_warnings = [w for w in r.warnings if "token_usage.stage" in w]
    assert not stage_warnings, f"unexpected stage warnings: {stage_warnings}"


@pytest.mark.unit
def test_token_usage_missing_stage_known_role_no_warning(tmp_path: Path):
    receipt = _good_receipt()
    receipt["role"] = "quality-engineer"
    receipt["token_usage"].pop("stage")
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    assert r.valid
    stage_warnings = [w for w in r.warnings if "token_usage.stage" in w]
    assert not stage_warnings, f"unexpected stage warnings: {stage_warnings}"


@pytest.mark.unit
def test_token_usage_missing_stage_unknown_role_still_warns(tmp_path: Path):
    receipt = _good_receipt()
    receipt["role"] = "marketing-manager"
    receipt["token_usage"].pop("stage")
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    assert r.valid
    assert any("token_usage.stage" in w for w in r.warnings)


@pytest.mark.unit
def test_token_usage_invalid_stage_multi_stage_role_still_warns(tmp_path: Path):
    """project-owner spans three pro-* stages — no deterministic backfill, so
    the warning must survive."""
    receipt = _good_receipt()
    receipt["role"] = "project-owner"
    receipt["token_usage"]["stage"] = "definitely-not-a-stage"
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    assert r.valid
    assert any("token_usage.stage" in w for w in r.warnings)


@pytest.mark.unit
def test_token_usage_valid_stage_never_warns_regardless_of_role(tmp_path: Path):
    receipt = _good_receipt()
    receipt["role"] = "marketing-manager"
    receipt["token_usage"]["stage"] = "qe-verification"
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    assert r.valid
    stage_warnings = [w for w in r.warnings if "token_usage.stage" in w]
    assert not stage_warnings


@pytest.mark.unit
def test_token_usage_negative_count_warns(tmp_path: Path):
    receipt = _good_receipt()
    receipt["token_usage"]["input"] = -5
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    assert r.valid
    assert any("token_usage.input" in w for w in r.warnings)


@pytest.mark.unit
def test_token_usage_wrong_type_warns(tmp_path: Path):
    receipt = _good_receipt()
    receipt["token_usage"] = "not a dict"
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    assert r.valid
    assert any("token_usage" in w for w in r.warnings)


# --- inferable stages are silenced, not warned (#134 GAP-7 × #158/#159) -----
#
# Mode-file dispatches have been observed inventing stages ("qe-diff-aware",
# "sa-contract-clarification"). When the canonical stage is inferable — from
# the role (evidence contract / ROLE_STAGE_FALLBACK) or an unambiguous stage
# prefix — the SubagentStop hook backfills it locally before shipping
# (transcript_usage.patch_receipt) and the CP remaps at ingest (#158), so
# the validator emits NO per-receipt warning (that was the GAP-7 noise).
# Warnings survive only when nothing can be inferred.


@pytest.mark.unit
@pytest.mark.parametrize(
    "role,invented",
    [
        ("quality-engineer", "qe-diff-aware"),
        ("solution-architect", "sa-contract-clarification"),
        ("software-engineer", "se-frontend"),
        ("research-advisor", "ra-research"),
        ("orchestrator", "orchestrator-commit-1"),
    ],
)
def test_invented_stage_with_inferable_canonical_is_silent(
    tmp_path: Path, role: str, invented: str
):
    receipt = _good_receipt()
    receipt["role"] = role
    receipt["token_usage"]["stage"] = invented
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    assert r.valid
    stage_warnings = [w for w in r.warnings if "token_usage.stage" in w]
    assert not stage_warnings, (
        f"inferable stage must be backfilled silently, got {stage_warnings}"
    )


@pytest.mark.unit
def test_invented_stage_prefix_inference_without_recognized_role_is_silent(
    tmp_path: Path,
):
    """When the role can't disambiguate, an unambiguous stage prefix still
    makes the canonical stage inferable — silent; backfill/ingest-remap own it."""
    receipt = _good_receipt()
    receipt["role"] = "product-manager"  # valid legacy role, not in ROLE_STAGE_FALLBACK
    receipt["token_usage"]["stage"] = "qe-browser-qa"
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    assert r.valid
    stage_warnings = [w for w in r.warnings if "token_usage.stage" in w]
    assert not stage_warnings, (
        f"prefix-inferable stage must be silent, got {stage_warnings}"
    )


@pytest.mark.unit
def test_missing_stage_for_single_stage_role_is_silent(tmp_path: Path):
    receipt = _good_receipt()
    receipt["role"] = "quality-engineer"
    receipt["token_usage"].pop("stage")
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    assert r.valid
    stage_warnings = [w for w in r.warnings if "token_usage.stage" in w]
    assert not stage_warnings, (
        f"role-derivable stage must be silent, got {stage_warnings}"
    )


@pytest.mark.unit
def test_ambiguous_pro_stage_gets_no_canonical_hint(tmp_path: Path):
    """project-owner spans three stages and 'pro-' is ambiguous — the warning
    must NOT guess a canonical stage."""
    receipt = _good_receipt()
    receipt["role"] = "project-owner"
    receipt["token_usage"]["stage"] = "pro-backlog-grooming"
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    assert r.valid
    stage_warnings = [w for w in r.warnings if "token_usage.stage" in w]
    assert stage_warnings, f"expected a stage warning, got {r.warnings}"
    assert all("— use '" not in w for w in stage_warnings), (
        f"warning must not guess a stage for project-owner: {stage_warnings}"
    )


# --- story_dod soft-required for SE/QE -------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("role", ["software-engineer", "quality-engineer"])
def test_story_dod_missing_warns_for_se_qe(tmp_path: Path, role: str):
    receipt = _good_receipt()
    receipt["role"] = role
    receipt.pop("story_dod")
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    assert r.valid
    assert any("story_dod" in w for w in r.warnings), (
        f"expected story_dod warning for role={role}, got {r.warnings}"
    )


@pytest.mark.unit
def test_story_dod_missing_does_not_warn_for_other_roles(tmp_path: Path):
    receipt = _good_receipt()
    receipt["role"] = "code-reviewer"
    receipt.pop("story_dod")
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    assert r.valid
    # story_dod is optional for non-SE/QE roles; no warning about it missing.
    dod_missing_warnings = [
        w for w in r.warnings if "story_dod" in w and "missing" in w
    ]
    assert not dod_missing_warnings, (
        f"unexpected story_dod missing-warning for code-reviewer: {dod_missing_warnings}"
    )


@pytest.mark.unit
def test_story_dod_non_bool_warns(tmp_path: Path):
    receipt = _good_receipt()
    receipt["story_dod"]["tests_pass"] = "yes please"
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    assert r.valid
    assert any("story_dod.tests_pass" in w for w in r.warnings)


# ─── #198: policy resolution must not depend on ambient state ────────────────


@pytest.mark.unit
def test_narrow_policy_cannot_remove_a_role_the_plugin_emits(tmp_path, monkeypatch):
    """The bug behind #198. Published policy v1 lists nine roles and omits
    `orchestrator`, which the orchestrator itself writes. Because the resolver
    used `policy or defaults` (replace), any machine that had fetched the
    policy warned on a correct receipt while CI, having no cache, did not — so
    `./synaptory test` passed in CI and failed on a logged-in laptop.

    Roles now resolve as policy UNION defaults, so a policy can widen the set
    but never narrow it below what this plugin emits.
    """
    import importlib
    cache = tmp_path / ".synaptory" / ".cache"
    cache.mkdir(parents=True)
    (cache / "policy.json").write_text(json.dumps({
        "payload": {"receipt_schema": {
            # Exactly the nine roles prod publishes: no `orchestrator`.
            "valid_roles": [
                "software-engineer", "quality-engineer", "code-reviewer",
                "compliance-engineer", "project-owner", "solution-architect",
                "platform-engineer", "technical-writer", "research-advisor",
            ],
        }},
    }))
    monkeypatch.setenv("HOME", str(tmp_path))

    from hooks.lib import receipt_validator as rv
    rv = importlib.reload(rv)
    try:
        assert "orchestrator" in rv.VALID_ROLES, (
            "a policy omitting `orchestrator` must not make the plugin warn on "
            "its own orchestrator receipts"
        )
        assert "product-manager" not in rv.VALID_ROLES, (
            "retirement happens by removing from _DEFAULT_VALID_ROLES, and must "
            "survive the union"
        )
    finally:
        # Restore module state for the rest of the session.
        monkeypatch.undo()
        importlib.reload(rv)


@pytest.mark.unit
def test_policy_can_widen_the_role_set(tmp_path, monkeypatch):
    """The union's other direction: a policy adding a role the plugin does not
    know about is honoured, which is what makes it a policy at all."""
    import importlib
    cache = tmp_path / ".synaptory" / ".cache"
    cache.mkdir(parents=True)
    (cache / "policy.json").write_text(json.dumps({
        "payload": {"receipt_schema": {"valid_roles": ["future-specialist"]}},
    }))
    monkeypatch.setenv("HOME", str(tmp_path))

    from hooks.lib import receipt_validator as rv
    rv = importlib.reload(rv)
    try:
        assert "future-specialist" in rv.VALID_ROLES
        assert "software-engineer" in rv.VALID_ROLES, "defaults must survive"
    finally:
        monkeypatch.undo()
        importlib.reload(rv)


# ─── #199: an absent contractual DoD key must be visible ─────────────────────


@pytest.mark.unit
def test_absent_dod_keys_warn_with_the_missing_names(tmp_path: Path):
    """The gap behind #199. Over an 11-day prod window, 296 receipts carried
    `story_dod` yet four of the five canonical checks appeared ZERO times —
    because only *unknown* keys and a *wholly missing* block warned. An absent
    contractual key was silent, so /quality scored one signal out of five with
    nothing flagging it."""
    receipt = _good_receipt()
    receipt["role"] = "software-engineer"
    receipt["story_dod"] = {"tests_pass": True}   # the real-world shape
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))

    assert r.valid, "absent keys are advisory, not fatal"
    dod_warnings = [w for w in r.warnings if "story_dod" in w]
    assert dod_warnings, "omitting 4 of 5 canonical checks must not be silent"
    w = dod_warnings[0]
    for missing in ("build_succeeds", "no_critical_findings",
                    "code_reviewed", "coverage_no_decrease"):
        assert missing in w, f"warning should name {missing}"
    assert "tests_pass" not in w, "the key that WAS reported must not be listed"


@pytest.mark.unit
def test_complete_dod_block_warns_nothing(tmp_path: Path):
    """All five present (any booleans) → no story_dod warning at all, so the
    new check cannot become background noise agents learn to ignore."""
    receipt = _good_receipt()
    receipt["role"] = "quality-engineer"
    receipt["story_dod"] = {
        "tests_pass": True, "build_succeeds": True, "no_critical_findings": True,
        "code_reviewed": False, "coverage_no_decrease": True,
    }
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    assert not [w for w in r.warnings if "story_dod" in w], r.warnings


@pytest.mark.unit
def test_absent_dod_keys_only_warn_for_roles_that_owe_them(tmp_path: Path):
    """A partial block from a role outside ROLES_REQUIRING_STORY_DOD is not a
    contract breach — a code-reviewer legitimately reports only code_reviewed,
    and warning there would punish correct behaviour."""
    receipt = _good_receipt()
    receipt["role"] = "code-reviewer"
    receipt["story_dod"] = {"code_reviewed": True}
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    assert not [w for w in r.warnings if "omits" in w], r.warnings


@pytest.mark.unit
def test_off_contract_keys_still_warn_alongside_absent_ones(tmp_path: Path):
    """Both #199 symptoms in one receipt — the shape prod actually shows:
    invented keys present AND contractual keys missing. Each gets its own
    warning so neither masks the other."""
    receipt = _good_receipt()
    receipt["role"] = "software-engineer"
    receipt["story_dod"] = {"tests_pass": True, "runtime_verified": True}
    p = _write(tmp_path, receipt)
    r = validate_receipt(str(p), str(tmp_path))
    assert any("runtime_verified" in w and "canonical" in w for w in r.warnings), \
        "an off-contract key must still be flagged"
    assert any("omits" in w for w in r.warnings), \
        "absent contractual keys must still be flagged"
