"""Layer 1 — #31 per-story compliance gate + #30 runtime-verification gate.

Pins the behaviour that closes the two hano-app PHI-leak gaps:

  #31 — for a healthcare project (`healthcare.baa_enforced: true`), a story
        whose diff touches PHI-risk paths must clear the compliance-engineer
        gate (`no_critical_findings`) before `done`. The gate is fail-closed:
        absent a CE receipt the story cannot pass.

  #30 — those same stories (and any project that opts in via
        `quality.runtime_verification: required`) must clear `runtime_verified`,
        satisfied only by a structured QE runtime-verification proof — a
        free-text "deployed and checked" note is NOT enough.

Non-healthcare projects with no opt-in are unaffected (regression guard).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_HOOKS_LIB = _PLUGIN_ROOT / "hooks" / "lib"
if str(_HOOKS_LIB) not in sys.path:
    sys.path.insert(0, str(_HOOKS_LIB))

import story_pipeline as sp  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _project(tmp_path: Path, yaml: str | None = None) -> Path:
    if yaml is not None:
        (tmp_path / ".synaptory.yaml").write_text(yaml, encoding="utf-8")
    receipts = tmp_path / ".synaptory" / ".orchestrator" / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write_receipt(project: Path, story_id: str, role: str, **fields) -> None:
    receipts = project / ".synaptory" / ".orchestrator" / "receipts"
    abbrev = sp._role_to_abbrev(role)
    payload = {"agent": role, **fields}
    (receipts / f"{story_id}-{abbrev}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


_HEALTHCARE_YAML = "healthcare:\n  baa_enforced: true\n"


def _se_qe_pass(project: Path, story_id: str, *, phi: bool, runtime=None) -> None:
    """SE + QE receipts that satisfy tests_pass + build_succeeds."""
    artifact = "src/logger/audit.ts" if phi else "src/components/Button.tsx"
    _write_receipt(
        project, story_id, "software-engineer",
        artifacts=[artifact],
        verification_commands=[{"command": "npm run build", "exit_code": 0}],
    )
    qe_metrics = {}
    if runtime is not None:
        qe_metrics["runtime_verification"] = runtime
    _write_receipt(
        project, story_id, "quality-engineer",
        artifacts=[artifact],
        verification_commands=[{"command": "npm test", "exit_code": 0}],
        metrics=qe_metrics,
    )


def _dod(project: Path, story_id: str, intensity: str = "early") -> dict:
    receipts = str(project / ".synaptory" / ".orchestrator" / "receipts")
    return sp.evaluate_story_dod(str(project), story_id, intensity, receipts_dir=receipts)


# ---------------------------------------------------------------------------
# Config readers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_baa_enforced_reads_top_level_block(tmp_path):
    p = _project(tmp_path, _HEALTHCARE_YAML)
    assert sp.healthcare_baa_enforced(str(p)) is True


@pytest.mark.unit
def test_baa_enforced_defaults_false(tmp_path):
    assert sp.healthcare_baa_enforced(str(_project(tmp_path, "build_mode: scrum\n"))) is False
    # Absent file → false, never raises.
    assert sp.healthcare_baa_enforced(str(tmp_path / "nope")) is False


@pytest.mark.unit
def test_baa_enforced_ignores_nested_false_lookalike(tmp_path):
    # A `baa_enforced:` under a different top-level block must not match.
    p = _project(tmp_path, "other:\n  baa_enforced: true\nhealthcare:\n  baa_enforced: false\n")
    assert sp.healthcare_baa_enforced(str(p)) is False


@pytest.mark.unit
def test_runtime_verification_required_toggle(tmp_path):
    assert sp.runtime_verification_required(str(_project(tmp_path, "quality:\n  runtime_verification: required\n"))) is True
    assert sp.runtime_verification_required(str(_project(tmp_path, "quality:\n  runtime_verification: false\n"))) is False
    assert sp.runtime_verification_required(str(_project(tmp_path, "build_mode: scrum\n"))) is False


@pytest.mark.unit
def test_phi_signals_include_defaults_and_config(tmp_path):
    p = _project(tmp_path, "healthcare:\n  baa_enforced: true\n  phi_paths:\n    - src/ehr/clients\n    - estimate\n")
    signals = sp.phi_risk_signals(str(p))
    assert "logger" in signals  # default
    assert "src/ehr/clients" in signals  # config
    assert "estimate" in signals


# ---------------------------------------------------------------------------
# PHI diff detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_story_touches_phi_matches_artifact_path():
    receipts = [{"agent": "software-engineer", "artifacts": ["src/logger/request.ts"]}]
    assert sp.story_touches_phi(receipts, sp.DEFAULT_PHI_RISK_SIGNALS) is True


@pytest.mark.unit
def test_story_touches_phi_matches_summary_text():
    receipts = [{"agent": "software-engineer", "summary": "Add MRN to search visit query"}]
    assert sp.story_touches_phi(receipts, sp.DEFAULT_PHI_RISK_SIGNALS) is True


@pytest.mark.unit
def test_story_touches_phi_false_for_plain_ui():
    receipts = [{"agent": "software-engineer", "artifacts": ["src/components/Button.tsx"], "summary": "tweak button colour"}]
    assert sp.story_touches_phi(receipts, sp.DEFAULT_PHI_RISK_SIGNALS) is False


# ---------------------------------------------------------------------------
# active_dod_checks promotion
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_active_checks_unchanged_without_flags():
    assert sp.active_dod_checks("early") == ["tests_pass", "build_succeeds"]


@pytest.mark.unit
def test_active_checks_promote_compliance_and_runtime():
    checks = sp.active_dod_checks("early", compliance_required=True, runtime_required=True)
    assert "no_critical_findings" in checks
    assert "runtime_verified" in checks


# ---------------------------------------------------------------------------
# evaluate_story_dod integration
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_non_healthcare_story_unaffected(tmp_path):
    # Regression: a plain project's gate is exactly the static tier.
    p = _project(tmp_path, "build_mode: scrum\n")
    _se_qe_pass(p, "US-1", phi=True)  # PHI-looking, but baa not enforced
    res = _dod(p, "US-1")
    assert res["compliance_required"] is False
    assert res["runtime_required"] is False
    assert res["passed"] is True


@pytest.mark.unit
def test_healthcare_non_phi_story_not_gated(tmp_path):
    p = _project(tmp_path, _HEALTHCARE_YAML)
    _se_qe_pass(p, "US-2", phi=False)
    res = _dod(p, "US-2")
    assert res["baa_enforced"] is True
    assert res["compliance_required"] is False
    assert res["passed"] is True


@pytest.mark.unit
def test_healthcare_phi_story_fails_closed_without_compliance(tmp_path):
    # The exact PR #505 scenario: green SE/QE, no compliance run → must NOT pass.
    p = _project(tmp_path, _HEALTHCARE_YAML)
    _se_qe_pass(p, "US-3", phi=True)  # no CE receipt, no runtime proof
    res = _dod(p, "US-3")
    assert res["compliance_required"] is True
    assert res["runtime_required"] is True
    assert res["checks"]["no_critical_findings"]["required"] is True
    assert res["checks"]["runtime_verified"]["required"] is True
    assert res["passed"] is False  # fail-closed


@pytest.mark.unit
def test_healthcare_phi_story_passes_with_compliance_and_runtime(tmp_path):
    p = _project(tmp_path, _HEALTHCARE_YAML)
    _se_qe_pass(
        p, "US-4", phi=True,
        runtime={"deployed": True, "logs_inspected": True, "paths_exercised": ["search-visit"]},
    )
    _write_receipt(p, "US-4", "compliance-engineer", metrics={"findings_critical": 0})
    res = _dod(p, "US-4")
    assert res["passed"] is True


@pytest.mark.unit
def test_runtime_verified_note_is_not_proof(tmp_path):
    # A free-text claim must be treated as unverified (None), not pass.
    assert sp._evaluate_check("runtime_verified", {"metrics": {"runtime_verification": "deployed and checked"}}) is None
    assert sp._evaluate_check("runtime_verified", {"metrics": {"runtime_verification": {"deployed": True, "logs_inspected": True}}}) is True
    assert sp._evaluate_check("runtime_verified", {"metrics": {"runtime_verification": {"deployed": True}}}) is False


# ---------------------------------------------------------------------------
# dod_gate_block_reason — the enforcement helper used by the state machines
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_block_reason_none_when_no_conditional_gates():
    res = {"compliance_required": False, "runtime_required": False, "checks": {}}
    assert sp.dod_gate_block_reason(res) is None
    assert sp.dod_gate_block_reason(None) is None


@pytest.mark.unit
def test_block_reason_fires_on_unmet_compliance():
    res = {
        "compliance_required": True,
        "runtime_required": False,
        "checks": {"no_critical_findings": {"required": True, "passed": None}},
    }
    reason = sp.dod_gate_block_reason(res)
    assert reason and "compliance-engineer" in reason


@pytest.mark.unit
def test_block_reason_clears_when_gates_pass():
    res = {
        "compliance_required": True,
        "runtime_required": True,
        "checks": {
            "no_critical_findings": {"required": True, "passed": True},
            "runtime_verified": {"required": True, "passed": True},
        },
    }
    assert sp.dod_gate_block_reason(res) is None


# ---------------------------------------------------------------------------
# Enforcement composition — exactly what the scrum/kanban `reviewing → done`
# path computes: evaluate_story_dod() → dod_gate_block_reason(). A non-None
# reason is what redirects the story to `blocked` instead of completing.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_gate_blocks_ungated_phi_story(tmp_path, monkeypatch):
    monkeypatch.delenv("SYNAPTORY_ACTIVE_SPEC", raising=False)
    p = _project(tmp_path, _HEALTHCARE_YAML)
    _se_qe_pass(p, "US-9", phi=True)  # green SE/QE, no CE, no runtime proof
    res = _dod(p, "US-9", intensity="growing")
    reason = sp.dod_gate_block_reason(res)
    assert reason is not None  # → state machine redirects reviewing→done to blocked
    assert "compliance-engineer" in reason


@pytest.mark.unit
def test_gate_clears_after_compliance_and_runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("SYNAPTORY_ACTIVE_SPEC", raising=False)
    p = _project(tmp_path, _HEALTHCARE_YAML)
    _se_qe_pass(
        p, "US-10", phi=True,
        runtime={"deployed": True, "logs_inspected": True},
    )
    _write_receipt(p, "US-10", "compliance-engineer", metrics={"findings_critical": 0})
    res = _dod(p, "US-10", intensity="growing")
    assert sp.dod_gate_block_reason(res) is None  # → story may complete


@pytest.mark.unit
def test_gate_blocks_when_compliance_finds_critical(tmp_path, monkeypatch):
    monkeypatch.delenv("SYNAPTORY_ACTIVE_SPEC", raising=False)
    p = _project(tmp_path, _HEALTHCARE_YAML)
    _se_qe_pass(p, "US-11", phi=True, runtime={"deployed": True, "logs_inspected": True})
    # CE ran but found a critical PHI leak → still blocked.
    _write_receipt(p, "US-11", "compliance-engineer", metrics={"findings_critical": 2})
    res = _dod(p, "US-11", intensity="growing")
    assert sp.dod_gate_block_reason(res) is not None
