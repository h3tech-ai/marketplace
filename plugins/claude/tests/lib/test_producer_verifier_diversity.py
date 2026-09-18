"""#753 — producer/verifier backend and model diversity is a recorded fact.

Hypothesis: `diversity_signal` derives `backend_diversity` from each receipt's
own `backend` field (a fact the orchestrator already knows), and derives
`model_attribution` from `model_identity.read_identity`'s typed classification
rather than from raw string equality — so a same-backend pair where either
side's model is unreported (Codex) or merely a legacy string is `unattributed`,
never upgraded to `model-same`. The signal never affects `passed`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from producer_verifier_diversity import diversity_signal
from story_pipeline import evaluate_story_dod

CLAUDE_OPUS = {
    "model_identity_version": 1,
    "runtime_family": "claude-code",
    "runtime_version": "2.1.200",
    "model_route": {"requested": "opus"},
    "model": {
        "kind": "vendor_id",
        "source": "claude-code.system.init.model",
        "evidence_class": "runtime-reported-vendor-id",
        "value": "claude-opus-5",
    },
}
CLAUDE_SONNET = {
    "model_identity_version": 1,
    "runtime_family": "claude-code",
    "runtime_version": "2.1.200",
    "model_route": {"requested": "sonnet"},
    "model": {
        "kind": "vendor_id",
        "source": "claude-code.system.init.model",
        "evidence_class": "runtime-reported-vendor-id",
        "value": "claude-sonnet-5",
    },
}
CODEX_UNREPORTED = {
    "model_identity_version": 1,
    "runtime_family": "codex",
    "runtime_version": "0.153.4",
    "model_route": {"requested": "policy-default"},
    "model": {
        "kind": "unreported",
        "source": "codex.stream",
        "evidence_class": "runtime-only",
    },
}


def _receipt(backend: str, model_fields: dict | None = None, model: object = None) -> dict:
    r: dict = {"backend": backend}
    if model_fields is not None:
        r.update(model_fields)
    else:
        r["model"] = model
    return r


@pytest.mark.unit
def test_missing_side_answers_none():
    assert diversity_signal(None, _receipt("claude", model="claude-opus-5")) is None
    assert diversity_signal(_receipt("claude", model="claude-opus-5"), {}) is None


@pytest.mark.unit
def test_same_backend_same_attested_model_is_model_same():
    se = _receipt("claude", CLAUDE_OPUS)
    cr = _receipt("claude", CLAUDE_OPUS)
    result = diversity_signal(se, cr)
    assert result["backend_diversity"] == "single-agent"
    assert result["model_attribution"] == "model-same"


@pytest.mark.unit
def test_same_backend_different_attested_model_is_model_distinct():
    """Today's actual default routing: se runs sonnet, cr runs opus, both
    backend "claude" -- diversity exists today without any cross-host wiring."""
    se = _receipt("claude", CLAUDE_SONNET)
    cr = _receipt("claude", CLAUDE_OPUS)
    result = diversity_signal(se, cr)
    assert result["backend_diversity"] == "single-agent"
    assert result["model_attribution"] == "model-distinct"


@pytest.mark.unit
def test_codex_side_is_unattributed_not_model_same():
    """The exact defect #753 exists to reduce: Codex's --json stream carries
    no model field, so a same-backend-looking round must never be reported
    as model-same. (Here the backends differ too, but attribution stays
    unattributed on its own axis regardless.)"""
    se = _receipt("claude", CLAUDE_SONNET)
    cr = _receipt("codex", CODEX_UNREPORTED)
    result = diversity_signal(se, cr)
    assert result["backend_diversity"] == "backend-distinct"
    assert result["model_attribution"] == "unattributed"
    assert result["verifier_model"]["attested"] is False


@pytest.mark.unit
def test_legacy_string_model_is_unattributed_even_same_backend():
    """Most receipts on disk today still carry a bare string `model` (pre-#705
    typed identity). A legacy string is read as `legacy_untyped`, not
    `vendor_id` -- so it must not be treated as attested just because the
    two strings happen to match."""
    se = _receipt("claude", model="claude-opus-5")
    cr = _receipt("claude", model="claude-opus-5")
    result = diversity_signal(se, cr)
    assert result["backend_diversity"] == "single-agent"
    assert result["model_attribution"] == "unattributed"


@pytest.mark.unit
def test_missing_backend_field_is_single_agent_not_a_crash():
    se = {"model": "claude-opus-5"}
    cr = {"model": "claude-opus-5"}
    result = diversity_signal(se, cr)
    assert result["producer_backend"] is None
    assert result["verifier_backend"] is None
    assert result["backend_diversity"] == "single-agent"


def _project(tmp_path: Path) -> Path:
    (tmp_path / ".synaptory.yaml").write_text("project_id: t\n")
    (tmp_path / "receipts").mkdir()
    return tmp_path


def _write_receipt(proj: Path, sid: str, role_abbrev: str, role: str, **extra) -> None:
    payload = {
        "task": "t",
        "agent": role,
        "verification_commands": [{"command": "true", "exit_code": 0, "summary": "s"}],
        "artifacts": ["x"],
        **extra,
    }
    (proj / "receipts" / f"{sid}-{role_abbrev}.json").write_text(json.dumps(payload))


@pytest.mark.unit
def test_evaluate_story_dod_surfaces_diversity_without_touching_passed(tmp_path: Path):
    proj = _project(tmp_path)
    _write_receipt(proj, "US-1", "se", "software-engineer", backend="claude", **CLAUDE_SONNET)
    _write_receipt(proj, "US-1", "qe", "quality-engineer", backend="claude", **CLAUDE_OPUS)
    _write_receipt(proj, "US-1", "cr", "code-reviewer", backend="codex", **CODEX_UNREPORTED)

    result = evaluate_story_dod(
        str(proj), "US-1", "early", receipts_dir=str(proj / "receipts")
    )

    diversity = result["producer_verifier_diversity"]
    assert diversity["se_vs_qe"]["backend_diversity"] == "single-agent"
    assert diversity["se_vs_qe"]["model_attribution"] == "model-distinct"
    assert diversity["se_vs_cr"]["backend_diversity"] == "backend-distinct"
    assert diversity["se_vs_cr"]["model_attribution"] == "unattributed"
    # A recorded fact, not a gate: the new field must not move the verdict.
    assert result["passed"] is True
    assert result["checks"]["tests_pass"]["passed"] is True
    assert result["checks"]["build_succeeds"]["passed"] is True


@pytest.mark.unit
def test_evaluate_story_dod_diversity_is_none_before_a_verifier_exists(tmp_path: Path):
    proj = _project(tmp_path)
    _write_receipt(proj, "US-2", "se", "software-engineer", backend="claude", **CLAUDE_SONNET)

    result = evaluate_story_dod(
        str(proj), "US-2", "early", receipts_dir=str(proj / "receipts")
    )

    diversity = result["producer_verifier_diversity"]
    assert diversity["se_vs_qe"] is None
    assert diversity["se_vs_cr"] is None
