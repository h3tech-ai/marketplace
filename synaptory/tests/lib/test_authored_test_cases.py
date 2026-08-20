"""Layer-1 tests for the authored test-case evidence gate.

quality.test_cases (receipt protocol §Authored test-case evidence): when the
QE receipt carries metrics.authored_test_cases, per-case results become part
of the tests_pass DoD gate — a failed/blocked/dropped authored case fails the
story even when every runner exit code is 0.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from story_pipeline import _authored_cases_pass, evaluate_story_dod


def _write_receipt(receipts_dir: Path, story_id: str, abbrev: str, body: dict) -> None:
    (receipts_dir / f"{story_id}-{abbrev}.json").write_text(json.dumps(body))


@pytest.fixture
def receipts_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".synaptory" / ".orchestrator" / "receipts"
    d.mkdir(parents=True)
    return d


def _atc(results: dict, total: int | None = None) -> dict:
    obj: dict = {"source": "tracker-linked", "results": results}
    if total is not None:
        obj["total"] = total
    return obj


@pytest.mark.unit
def test_absent_object_is_not_enforced():
    # No authored source configured — helper must stay out of the way.
    assert _authored_cases_pass({}) is None
    assert _authored_cases_pass({"test_coverage": 87}) is None


@pytest.mark.unit
def test_all_passed_or_na_passes():
    metrics = {"authored_test_cases": _atc({
        "STAR-27492": {"status": "passed", "test": "tests/e2e/speaker.e2e.ts"},
        "STAR-27493": {"status": "not-applicable", "reason": "data migration"},
    }, total=2)}
    assert _authored_cases_pass(metrics) is True


@pytest.mark.unit
@pytest.mark.parametrize("status", ["failed", "blocked", "in-progress", ""])
def test_non_passed_status_fails(status):
    metrics = {"authored_test_cases": _atc({
        "STAR-27492": {"status": "passed"},
        "STAR-27493": {"status": status, "reason": "x"},
    }, total=2)}
    assert _authored_cases_pass(metrics) is False


@pytest.mark.unit
def test_claim_without_per_case_results_fails():
    # "6 passed" with no per-case evidence is a claim, not proof.
    assert _authored_cases_pass(
        {"authored_test_cases": {"source": "ticket", "total": 6, "passed": 6}}
    ) is False
    assert _authored_cases_pass(
        {"authored_test_cases": _atc({}, total=0)}
    ) is False


@pytest.mark.unit
def test_dropped_cases_fail():
    # total says 3, results carry 2 — one authored case silently vanished.
    metrics = {"authored_test_cases": _atc({
        "STAR-27492": {"status": "passed"},
        "STAR-27493": {"status": "passed"},
    }, total=3)}
    assert _authored_cases_pass(metrics) is False


@pytest.mark.unit
def test_tests_pass_gate_blocks_on_failed_authored_case(
    tmp_path: Path, receipts_dir: Path  # noqa: F811
):
    # Green exit codes + one failed authored case ⇒ tests_pass is False.
    _write_receipt(
        receipts_dir, "US-9", "qe",
        {
            "agent": "quality-engineer",
            "verification_commands": [{"command": "playwright test", "exit_code": 0}],
            "metrics": {"authored_test_cases": _atc({
                "STAR-27492": {"status": "passed"},
                "STAR-27493": {"status": "failed", "reason": "validation missing"},
            }, total=2)},
        },
    )
    result = evaluate_story_dod(
        str(tmp_path), "US-9", "early", receipts_dir=str(receipts_dir)
    )
    assert result["checks"]["tests_pass"]["passed"] is False
    assert result["passed"] is False


@pytest.mark.unit
def test_tests_pass_gate_passes_with_green_authored_evidence(
    tmp_path: Path, receipts_dir: Path  # noqa: F811
):
    _write_receipt(
        receipts_dir, "US-10", "qe",
        {
            "agent": "quality-engineer",
            "verification_commands": [{"command": "playwright test", "exit_code": 0}],
            "metrics": {"authored_test_cases": _atc({
                "STAR-27492": {"status": "passed"},
            }, total=1)},
        },
    )
    result = evaluate_story_dod(
        str(tmp_path), "US-10", "early", receipts_dir=str(receipts_dir)
    )
    assert result["checks"]["tests_pass"]["passed"] is True


@pytest.mark.unit
def test_failed_authored_case_not_rescued_by_other_receipt(
    tmp_path: Path, receipts_dir: Path  # noqa: F811
):
    # #187 — a failed authored case on the QE receipt must NOT be overridden by
    # a green test command on another same-story receipt (e.g. an SE pytest
    # run) via the `any_with_proof` fallback.
    _write_receipt(
        receipts_dir, "US-12", "qe",
        {
            "agent": "quality-engineer",
            "verification_commands": [{"command": "playwright test", "exit_code": 0}],
            "metrics": {"authored_test_cases": _atc({
                "STAR-1": {"status": "passed"},
                "STAR-2": {"status": "failed", "reason": "validation missing"},
            }, total=2)},
        },
    )
    _write_receipt(
        receipts_dir, "US-12", "se",
        {
            "agent": "software-engineer",
            "verification_commands": [{"command": "pytest -q", "exit_code": 0}],
        },
    )
    result = evaluate_story_dod(
        str(tmp_path), "US-12", "early", receipts_dir=str(receipts_dir)
    )
    assert result["checks"]["tests_pass"]["passed"] is False
    assert result["passed"] is False
    assert "authored test case" in result["checks"]["tests_pass"].get("detail", "")


@pytest.mark.unit
def test_receipts_without_authored_object_unchanged(
    tmp_path: Path, receipts_dir: Path  # noqa: F811
):
    # Back-compat: projects without quality.test_cases behave exactly as before.
    _write_receipt(
        receipts_dir, "US-11", "qe",
        {
            "agent": "quality-engineer",
            "verification_commands": [{"command": "pytest", "exit_code": 0}],
        },
    )
    result = evaluate_story_dod(
        str(tmp_path), "US-11", "early", receipts_dir=str(receipts_dir)
    )
    assert result["checks"]["tests_pass"]["passed"] is True
