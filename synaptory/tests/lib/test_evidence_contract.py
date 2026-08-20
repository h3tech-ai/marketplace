"""Layer 1 — `plugin/hooks/lib/evidence_contract.py` contract tests.

Hypothesis: the Evidence Contract (receipt-schema/evidence-contract.json +
EMBEDDED_DEFAULTS) is the single source of truth for receipt / DoD / replay
rules, and it must stay behavior-faithful to the modules it was extracted
from. These tests pin:

  * JSON artifact parses and is content-identical to EMBEDDED_DEFAULTS.
  * Drift guards against the (not-yet-refactored) ground-truth modules:
    receipt_validator.VALID_V3_STAGES, story_pipeline.DOD_CHECKS /
    DOD_TIER_CHECKS, verification_runner.VERIFICATION_ALLOWLIST (contract
    is a superset — #163 expands it deliberately).
  * role_stage round-trips; project-owner (multi-stage) → None.
  * render_envelope is role-scoped and fail-safe ("" for unknown roles).
  * explain_rejection covers every code with a distinct, actionable string.
  * The signed policy.json overlay wins over the on-disk contract.
  * CLI entry point works via subprocess.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from hooks.lib import evidence_contract as ec
from hooks.lib import receipt_validator
from hooks.lib import story_pipeline
from hooks.lib import verification_runner

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = PLUGIN_ROOT / "receipt-schema" / "evidence-contract.json"
MODULE_PATH = PLUGIN_ROOT / "hooks" / "lib" / "evidence_contract.py"

REJECTION_CODES = [
    "intent_not_proof",
    "unreplayable_pipe",
    "not_allowlisted",
    "env_prefix",
    "command_substitution",
    "interpreter_inline",
    "find_forbidden_flag",
    "missing_status_complete",
    "missing_verification_commands",
]


# ─── 1. JSON artifact ↔ embedded defaults ─────────────────────────────────────


@pytest.mark.unit
def test_contract_json_parses_and_matches_embedded_defaults():
    assert CONTRACT_PATH.is_file(), f"contract missing at {CONTRACT_PATH}"
    with open(CONTRACT_PATH) as f:
        on_disk = json.load(f)
    assert on_disk == ec.EMBEDDED_DEFAULTS


@pytest.mark.unit
def test_contract_top_level_shape():
    for key in (
        "contract_version",
        "receipt",
        "stages",
        "verification_commands",
        "dod_checks",
        "dod_tiers",
    ):
        assert key in ec.EMBEDDED_DEFAULTS, f"missing top-level key {key!r}"
    assert ec.EMBEDDED_DEFAULTS["contract_version"] == 1


# ─── 2. Ground-truth drift guards ─────────────────────────────────────────────


@pytest.mark.unit
def test_stages_match_receipt_validator():
    contract_stages = set(ec.EMBEDDED_DEFAULTS["stages"]["valid"])
    assert contract_stages == receipt_validator.VALID_V3_STAGES


@pytest.mark.unit
def test_dod_checks_cover_story_pipeline_with_same_metadata():
    contract_checks = ec.EMBEDDED_DEFAULTS["dod_checks"]
    for check_id, check_def in story_pipeline.DOD_CHECKS.items():
        assert check_id in contract_checks, f"contract missing DoD check {check_id!r}"
        entry = contract_checks[check_id]
        assert entry["category"] == check_def["category"], check_id
        assert entry["label"] == check_def["label"], check_id
        assert entry["receipt_role"] == check_def["receipt_role"], check_id
        assert entry["receipt_field"] == check_def["receipt_field"], check_id


@pytest.mark.unit
def test_dod_tiers_match_story_pipeline():
    assert ec.EMBEDDED_DEFAULTS["dod_tiers"]["tiers"] == story_pipeline.DOD_TIER_CHECKS


@pytest.mark.unit
def test_allowlist_is_superset_of_verification_runner():
    contract_allowlist = set(
        ec.EMBEDDED_DEFAULTS["verification_commands"]["replayability"]["allowlist"]
    )
    assert contract_allowlist >= verification_runner.VERIFICATION_ALLOWLIST
    # #163 deliberately expands the allowlist with read-only inspectors.
    for prog in ("test", "grep", "ls", "wc", "diff", "jq", "stat", "head", "tail", "find"):
        assert prog in contract_allowlist, f"expanded program {prog!r} missing"


@pytest.mark.unit
def test_forbidden_tokens_cover_runner_shell_control_tokens():
    tokens = set(
        ec.EMBEDDED_DEFAULTS["verification_commands"]["replayability"]["forbidden_tokens"]
    )
    assert tokens >= verification_runner._SHELL_CONTROL_TOKENS


@pytest.mark.unit
def test_timeouts_match_verification_runner():
    replay = ec.EMBEDDED_DEFAULTS["verification_commands"]["replayability"]
    assert replay["per_command_timeout_s"] == verification_runner.PER_COMMAND_TIMEOUT
    assert replay["total_timeout_s"] == verification_runner.TOTAL_TIMEOUT


@pytest.mark.unit
def test_receipt_section_matches_receipt_validator_defaults():
    receipt = ec.EMBEDDED_DEFAULTS["receipt"]
    assert set(receipt["required_fields"]) == receipt_validator._DEFAULT_REQUIRED_FIELDS
    assert set(receipt["valid_roles"]) == receipt_validator._DEFAULT_VALID_ROLES
    assert set(receipt["valid_backends"]) == receipt_validator._DEFAULT_VALID_BACKENDS
    assert receipt["story_id_pattern"] == receipt_validator.STORY_ID_PATTERN.pattern


@pytest.mark.unit
def test_waivable_matches_story_pipeline():
    waivable = {
        cid
        for cid, entry in ec.EMBEDDED_DEFAULTS["dod_checks"].items()
        if entry.get("waivable")
    }
    assert waivable == story_pipeline._WAIVABLE_DOD_CHECKS


@pytest.mark.unit
def test_role_abbrevs_match_story_pipeline():
    for role, abbrev in ec.EMBEDDED_DEFAULTS["receipt"]["role_abbrevs"].items():
        assert story_pipeline._role_to_abbrev(role) == abbrev, role
    # The legacy alias resolves through role_aliases to the same abbrev.
    contract = ec.load_contract()
    assert ec.role_abbrev("product-manager", contract) == "po"


# ─── 3. role_stage ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_role_stage_round_trips_single_stage_roles():
    contract = ec.load_contract()
    valid_stages = set(contract["stages"]["valid"])
    for role in contract["stages"]["role_to_stage"]:
        stage = ec.role_stage(role, contract)
        assert stage in valid_stages, f"{role} → {stage!r} not a valid stage"


@pytest.mark.unit
def test_role_stage_multi_stage_and_unknown_roles_return_none():
    contract = ec.load_contract()
    assert ec.role_stage("project-owner", contract) is None
    assert ec.role_stage("product-manager", contract) is None  # alias → project-owner
    assert ec.role_stage("research-advisor", contract) is None
    assert ec.role_stage("not-a-role", contract) is None


# ─── 4. render_envelope ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_envelope_se_mentions_build_proof_and_executed_object():
    env = ec.render_envelope("synaptory:software-engineer")
    assert env.startswith("## Execution Envelope (evidence contract v1)")
    assert "US-042-se.json" in env
    assert '"role": "software-engineer"' in env
    assert "se-implementation" in env
    assert "build_succeeds" in env
    assert '"exit_code": 0' in env
    assert "npm run build 2>&1 | tail -5" in env  # the BAD example


@pytest.mark.unit
def test_envelope_qe_mentions_runtime_and_ui_shapes():
    env = ec.render_envelope("synaptory:quality-engineer")
    assert "US-042-qe.json" in env
    assert "qe-verification" in env
    assert "runtime_verification" in env
    assert "logs_inspected" in env
    assert "ui_verification" in env
    assert "flows_failed" in env


@pytest.mark.unit
def test_envelope_cr_mentions_status_complete():
    env = ec.render_envelope("synaptory:code-reviewer")
    assert "US-042-cr.json" in env
    assert '"status": "complete"' in env
    assert "code_reviewed" in env


@pytest.mark.unit
def test_envelope_ce_and_pe_obligations():
    ce_env = ec.render_envelope("synaptory:compliance-engineer")
    assert "findings_critical" in ce_env
    pe_env = ec.render_envelope("synaptory:platform-engineer")
    assert "cross-role" in pe_env


@pytest.mark.unit
def test_envelope_unknown_and_non_synaptory_return_empty():
    assert ec.render_envelope("general-purpose") == ""
    assert ec.render_envelope("not-a-role") == ""
    assert ec.render_envelope("other-plugin:software-engineer") == ""
    assert ec.render_envelope("") == ""
    assert ec.render_envelope("synaptory:orchestrator") == ""  # no receipt abbrev


@pytest.mark.unit
def test_envelope_bare_role_and_active_checks():
    env = ec.render_envelope(
        "software-engineer",
        active_checks=["tests_pass", "build_succeeds", "code_reviewed"],
        tier="growing",
    )
    assert "active DoD checks" in env
    assert "code_reviewed" in env
    assert "growing" in env


@pytest.mark.unit
def test_envelope_tier_only_expands_tier_checks():
    env = ec.render_envelope("synaptory:software-engineer", tier="mature")
    assert "coverage_no_decrease" in env


@pytest.mark.unit
def test_envelope_stays_terse():
    for role in ("software-engineer", "quality-engineer", "code-reviewer"):
        env = ec.render_envelope(f"synaptory:{role}", tier="growing")
        assert 30 <= len(env.splitlines()) <= 80, f"{role} envelope size drifted"


# ─── 5. explain_rejection ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_explain_rejection_all_codes_distinct_and_actionable():
    seen = {}
    for code in REJECTION_CODES:
        msg = ec.explain_rejection(code)
        assert msg and msg.strip(), code
        assert "Fix:" in msg, f"{code} lacks an actionable fix"
        seen[code] = msg
    assert len(set(seen.values())) == len(REJECTION_CODES), "duplicate explanations"


@pytest.mark.unit
def test_explain_rejection_interpolates_context():
    assert "'rsync'" in ec.explain_rejection("not_allowlisted", program="rsync")
    assert "'-delete'" in ec.explain_rejection("find_forbidden_flag", flag="-delete")
    assert "'npm test'" in ec.explain_rejection("intent_not_proof", command="npm test")


@pytest.mark.unit
def test_explain_rejection_unknown_code_fail_safe():
    msg = ec.explain_rejection("no_such_code")
    assert msg and "Fix:" in msg


# ─── 6. Policy overlay ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_policy_overlay_overrides_receipt_schema(tmp_path, monkeypatch):
    # The autouse _isolate_synaptory_state fixture already points HOME at
    # tmp_path; write a fake signed-policy cache under it.
    monkeypatch.setenv("HOME", str(tmp_path))
    cache_dir = tmp_path / ".synaptory" / ".cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "policy.json").write_text(
        json.dumps(
            {
                "payload": {
                    "receipt_schema": {
                        "required_fields": ["story_id", "role", "custom_field"],
                        "valid_backends": ["claude", "codex"],
                    }
                }
            }
        )
    )
    contract = ec.load_contract()
    assert contract["receipt"]["required_fields"] == ["story_id", "role", "custom_field"]
    assert contract["receipt"]["valid_backends"] == ["claude", "codex"]
    # Keys the policy omits keep contract values.
    assert set(contract["receipt"]["valid_roles"]) == set(
        ec.EMBEDDED_DEFAULTS["receipt"]["valid_roles"]
    )
    # EMBEDDED_DEFAULTS itself must never be mutated by the overlay.
    assert "custom_field" not in ec.EMBEDDED_DEFAULTS["receipt"]["required_fields"]


@pytest.mark.unit
def test_load_contract_falls_back_to_embedded_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # no policy cache
    contract = ec.load_contract(plugin_root=str(tmp_path / "nonexistent"))
    assert contract == ec.EMBEDDED_DEFAULTS
    assert contract is not ec.EMBEDDED_DEFAULTS  # deep copy, safe to mutate


@pytest.mark.unit
def test_load_contract_reads_on_disk_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "plugroot"
    (root / "receipt-schema").mkdir(parents=True)
    custom = json.loads(json.dumps(ec.EMBEDDED_DEFAULTS))
    custom["contract_version"] = 99
    (root / "receipt-schema" / "evidence-contract.json").write_text(json.dumps(custom))
    contract = ec.load_contract(plugin_root=str(root))
    assert contract["contract_version"] == 99


# ─── 7. CLI ───────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_cli_envelope_subprocess():
    result = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "envelope",
            "--agent-type",
            "synaptory:quality-engineer",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "Execution Envelope" in result.stdout
    assert "qe-verification" in result.stdout


@pytest.mark.unit
def test_cli_envelope_unknown_role_exits_zero_empty():
    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), "envelope", "--agent-type", "mystery-role"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


@pytest.mark.unit
def test_cli_explain_and_dump():
    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), "explain", "unreplayable_pipe"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "Fix:" in result.stdout

    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), "dump"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    dumped = json.loads(result.stdout)
    assert dumped["contract_version"] == ec.EMBEDDED_DEFAULTS["contract_version"]
