"""#705: uniform source semantics, immutable legacy reads and consumer behavior."""

import copy
import json
from pathlib import Path
import pytest
from model_identity import identity_problems, read_identity
from receipt_validator import validate_receipt_payload
from summary.receipts import normalize_receipt

ROOT = Path(__file__).resolve().parents[3]
CASES = json.loads(
    (ROOT / "core/runtime-fixtures/model-identity-cases.json").read_text()
)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_cross_language_identity_contract(case):
    before = copy.deepcopy(case["receipt"])
    assert (not identity_problems(case["receipt"])) == case["valid"]
    assert case["receipt"] == before


@pytest.mark.parametrize(
    "case", CASES[:1] + [c for c in CASES if c["name"] in {"cursor-agent", "codex"}]
)
def test_typed_receipt_passes_shared_validator(case, tmp_path):
    (tmp_path / "report.md").write_text("Verified.\n")
    receipt = dict(
        case["receipt"],
        story_id="US-705",
        role="software-engineer",
        backend={"claude-code": "claude", "cursor-agent": "cursor", "codex": "codex"}[
            case["receipt"]["runtime_family"]
        ],
        artifacts=["report.md"],
        verification_commands=["python3 -m pytest"],
        metrics={"tests_run": 1},
        completed_at="2026-09-15T00:00:00Z",
    )
    result = validate_receipt_payload(receipt, str(tmp_path))
    assert not result.errors
    normalized = normalize_receipt(receipt)
    assert normalized["model_identity"] == receipt["model"]
    assert normalized["runtime_version"] == receipt["runtime_version"]
    assert isinstance(normalized["model"], str)


@pytest.mark.parametrize(
    "model",
    ["claude-opus-5", "Claude Sonnet 4.6 200K Medium", "codex-runtime-unattributed"],
)
def test_legacy_never_becomes_vendor_id(model):
    assert read_identity(model)["kind"] == "legacy_untyped"
    assert (
        normalize_receipt({"model": model})["model_identity"]["kind"]
        == "legacy_untyped"
    )


@pytest.mark.parametrize("backend", [[], {}])
def test_malformed_backend_is_reported_without_crashing(backend, tmp_path):
    result = validate_receipt_payload({"backend": backend, "model": "claude-opus-5"}, str(tmp_path))
    assert "'backend' must be a string" in result.errors


@pytest.mark.parametrize("identity,display", [
    ({"value": "claude-opus-5"}, "claude-opus-5"),
    ({}, "invalid"),
    ({"kind": "vendor_id", "value": {"unexpected": "object"}}, "invalid"),
])
def test_incomplete_identity_does_not_abort_project_summary(tmp_path, identity, display):
    from build_summary import generate

    receipts_dir = tmp_path / ".synaptory" / ".orchestrator" / "receipts"
    receipts_dir.mkdir(parents=True)
    path = receipts_dir / "US-705-se.json"
    receipt = {"story_id": "US-705", "role": "software-engineer", "model": identity}
    path.write_text(json.dumps(receipt))
    original = path.read_bytes()
    summary_path = generate(tmp_path)
    summary = json.loads(summary_path.read_text())
    normalized = summary["receipts_normalized"][0]
    assert normalized["model"] == display
    assert normalized["model_identity"] == identity
    assert path.read_bytes() == original
