"""Layer 1 — `plugin/hooks/lib/story_pipeline.py` DoD + tier tests.

Hypothesis: the adaptive DoD intensity tier is a pure function of
sprint number / ticket number / release flag. Each tier maps to a
specific check-set (DOD_TIER_CHECKS). The mapping is the contract that
gates story closure — pin it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import story_pipeline as sp
from story_pipeline import (
    STORY_STATES,
    determine_dod_intensity,
    evaluate_story_dod,
)


@pytest.mark.unit
def test_story_states_locked():
    # #116 added `awaiting_acceptance` and `cancelled`. The list is
    # ordered roughly by lifecycle progression; pin the full set so any
    # future addition is a deliberate review item.
    assert STORY_STATES == [
        "queued",
        "in_progress",
        "testing",
        "reviewing",
        "awaiting_acceptance",
        "done",
        "cancelled",
        "blocked",
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "sprint,ticket,is_release,expected",
    [
        (1, None, False, "early"),
        (None, 1, False, "early"),
        (2, None, False, "growing"),
        (3, None, False, "growing"),
        (None, 2, False, "growing"),
        (None, 3, False, "growing"),
        (4, None, False, "mature"),
        (5, None, False, "mature"),
        (None, 4, False, "mature"),
        (None, 100, False, "mature"),
        # is_release forces the max tier regardless of count.
        (1, None, True, "release"),
        (5, None, True, "release"),
        (None, 1, True, "release"),
    ],
)
def test_dod_intensity_tier(sprint, ticket, is_release, expected):
    """Adaptive tier: early (1) → growing (2-3) → mature (4+); release overrides."""
    assert determine_dod_intensity(sprint, ticket, is_release) == expected


@pytest.mark.unit
def test_dod_intensity_default_kanban_treats_unset_as_one():
    """Kanban default (no sprint, no ticket) maps to early tier."""
    assert determine_dod_intensity(None, None, False) == "early"


@pytest.mark.unit
def test_dod_tier_checks_are_subsets_in_growth_order():
    """Lower-tier checks should be a subset of higher-tier ones."""
    early = set(sp.DOD_TIER_CHECKS["early"])
    growing = set(sp.DOD_TIER_CHECKS["growing"])
    mature = set(sp.DOD_TIER_CHECKS["mature"])
    release = set(sp.DOD_TIER_CHECKS["release"])
    assert early <= growing, "early checks should be ⊆ growing"
    assert growing <= mature, "growing checks should be ⊆ mature"
    assert mature <= release, "mature checks should be ⊆ release"


@pytest.mark.unit
def test_role_to_abbrev_canonical():
    """Receipt filenames use abbreviated role tags — pin the mapping."""
    assert sp._role_to_abbrev("software-engineer") == "se"
    assert sp._role_to_abbrev("quality-engineer") == "qe"
    assert sp._role_to_abbrev("code-reviewer") == "cr"
    assert sp._role_to_abbrev("project-owner") == "po"
    assert sp._role_to_abbrev("solution-architect") == "sa"
    # Unknown role → None (write site decides whether to skip or error).
    assert sp._role_to_abbrev("doesnt-exist") is None


@pytest.mark.unit
def test_get_story_receipt_path_naming_convention():
    """`{story_id}-{role_abbrev}.json` is the canonical receipt filename."""
    assert sp.get_story_receipt_path("US-042", "se") == "US-042-se.json"
    assert sp.get_story_receipt_path("INFRA-007", "qe") == "INFRA-007-qe.json"


# ---------------------------------------------------------------------------
# evaluate_story_dod — receipt resolution and check evaluation (#105/#106/#107)
# ---------------------------------------------------------------------------

def _write_receipt(receipts_dir: Path, story_id: str, abbrev: str, body: dict) -> None:
    receipts_dir.mkdir(parents=True, exist_ok=True)
    (receipts_dir / f"{story_id}-{abbrev}.json").write_text(json.dumps(body))


@pytest.fixture
def receipts_dir(tmp_path: Path) -> Path:
    return tmp_path / "receipts"


@pytest.mark.unit
def test_dod_uses_agent_field_for_role_grouping(tmp_path: Path, receipts_dir: Path):
    """#105 — receipts written with `agent` (not `role`) must still resolve."""
    _write_receipt(
        receipts_dir, "US-1", "se",
        {
            "agent": "software-engineer",
            "verification_commands": [{"command": "pytest", "exit_code": 0}],
        },
    )
    _write_receipt(
        receipts_dir, "US-1", "qe",
        {
            "agent": "quality-engineer",
            "verification_commands": [{"command": "pytest", "exit_code": 0}],
        },
    )
    result = evaluate_story_dod(
        str(tmp_path), "US-1", "early", receipts_dir=str(receipts_dir)
    )
    assert result["checks"]["tests_pass"]["passed"] is True
    assert result["checks"]["tests_pass"]["source"] == "US-1-qe.json"
    assert result["checks"]["build_succeeds"]["passed"] is True
    assert result["passed"] is True


@pytest.mark.unit
def test_dod_back_compat_with_role_field(tmp_path: Path, receipts_dir: Path):
    """#105 — legacy receipts written with `role` (not `agent`) still resolve."""
    _write_receipt(
        receipts_dir, "US-2", "se",
        {
            "role": "software-engineer",
            "verification_commands": [{"command": "pytest", "exit_code": 0}],
        },
    )
    _write_receipt(
        receipts_dir, "US-2", "qe",
        {
            "role": "quality-engineer",
            "verification_commands": [{"command": "pytest", "exit_code": 0}],
        },
    )
    result = evaluate_story_dod(
        str(tmp_path), "US-2", "early", receipts_dir=str(receipts_dir)
    )
    assert result["checks"]["tests_pass"]["passed"] is True
    assert result["passed"] is True


@pytest.mark.unit
def test_tests_pass_string_only_is_unverified(tmp_path: Path, receipts_dir: Path):
    """#106 — string-only verification_commands ⇒ unverified (None), not vacuous True."""
    _write_receipt(
        receipts_dir, "US-3", "se",
        {"agent": "software-engineer", "verification_commands": ["npm run build"]},
    )
    _write_receipt(
        receipts_dir, "US-3", "qe",
        {"agent": "quality-engineer", "verification_commands": ["pytest -q"]},
    )
    result = evaluate_story_dod(
        str(tmp_path), "US-3", "early", receipts_dir=str(receipts_dir)
    )
    assert result["checks"]["tests_pass"]["passed"] is None
    assert result["checks"]["build_succeeds"]["passed"] is None
    assert result["passed"] is False
    assert result["critical_passed"] is False


@pytest.mark.unit
def test_tests_pass_dict_with_zero_exit_passes(tmp_path: Path, receipts_dir: Path):
    """#106 — dict with exit_code=0 ⇒ passed=True."""
    _write_receipt(
        receipts_dir, "US-4", "qe",
        {
            "agent": "quality-engineer",
            "verification_commands": [{"command": "pytest", "exit_code": 0}],
        },
    )
    result = evaluate_story_dod(
        str(tmp_path), "US-4", "early", receipts_dir=str(receipts_dir)
    )
    assert result["checks"]["tests_pass"]["passed"] is True


@pytest.mark.unit
def test_tests_pass_dict_nonzero_fails(tmp_path: Path, receipts_dir: Path):
    """#106 — dict with exit_code != 0 ⇒ passed=False."""
    _write_receipt(
        receipts_dir, "US-5", "qe",
        {
            "agent": "quality-engineer",
            "verification_commands": [{"command": "pytest", "exit_code": 1}],
        },
    )
    result = evaluate_story_dod(
        str(tmp_path), "US-5", "early", receipts_dir=str(receipts_dir)
    )
    assert result["checks"]["tests_pass"]["passed"] is False
    assert result["passed"] is False


@pytest.mark.unit
def test_tests_pass_mixed_strings_and_dicts_uses_dicts(tmp_path: Path, receipts_dir: Path):
    """#106 — strings are skipped; verified dicts drive the result."""
    _write_receipt(
        receipts_dir, "US-6", "qe",
        {
            "agent": "quality-engineer",
            "verification_commands": [
                "pytest -q",  # intent
                {"command": "pytest tests/unit", "exit_code": 0},  # executed
            ],
        },
    )
    result = evaluate_story_dod(
        str(tmp_path), "US-6", "early", receipts_dir=str(receipts_dir)
    )
    assert result["checks"]["tests_pass"]["passed"] is True


@pytest.mark.unit
def test_early_tier_no_longer_requires_ce(tmp_path: Path, receipts_dir: Path):
    """#107 — early-tier story must close without a CE receipt."""
    _write_receipt(
        receipts_dir, "US-7", "se",
        {"agent": "software-engineer",
         "verification_commands": [{"command": "build", "exit_code": 0}]},
    )
    _write_receipt(
        receipts_dir, "US-7", "qe",
        {"agent": "quality-engineer",
         "verification_commands": [{"command": "pytest", "exit_code": 0}]},
    )
    result = evaluate_story_dod(
        str(tmp_path), "US-7", "early", receipts_dir=str(receipts_dir)
    )
    assert result["checks"]["no_critical_findings"]["required"] is False
    assert result["passed"] is True
    assert result["critical_passed"] is True


@pytest.mark.unit
def test_growing_tier_excludes_ce(tmp_path: Path, receipts_dir: Path):
    """#107 — growing-tier story must close on SE+QE+CR without CE."""
    _write_receipt(
        receipts_dir, "US-8", "se",
        {"agent": "software-engineer",
         "verification_commands": [{"command": "build", "exit_code": 0}]},
    )
    _write_receipt(
        receipts_dir, "US-8", "qe",
        {"agent": "quality-engineer",
         "verification_commands": [{"command": "pytest", "exit_code": 0}]},
    )
    _write_receipt(
        receipts_dir, "US-8", "cr",
        {"agent": "code-reviewer", "status": "complete"},
    )
    result = evaluate_story_dod(
        str(tmp_path), "US-8", "growing", receipts_dir=str(receipts_dir)
    )
    assert result["checks"]["no_critical_findings"]["required"] is False
    assert result["passed"] is True


@pytest.mark.unit
def test_release_tier_excludes_no_critical_findings():
    """#107 — `no_critical_findings` is no longer in any per-story tier."""
    for tier in ("early", "growing", "mature", "release"):
        assert "no_critical_findings" not in sp.DOD_TIER_CHECKS[tier], (
            f"per-story tier {tier!r} must not require no_critical_findings — "
            f"CE is dispatched at Release only (see #107)"
        )


# ---------------------------------------------------------------------------
# evaluate_story_dod — build_succeeds sourcing + code-free waiver (#144)
# ---------------------------------------------------------------------------

def _write_state(project_dir: Path, stories: list[dict]) -> None:
    """Write a minimal single-spec pipeline-state.json for the story readers."""
    d = project_dir / ".synaptory" / ".orchestrator"
    d.mkdir(parents=True, exist_ok=True)
    (d / "pipeline-state.json").write_text(json.dumps({"current_stories": stories}))


@pytest.mark.unit
def test_build_succeeds_sourced_from_platform_engineer_receipt(
    tmp_path: Path, receipts_dir: Path
):
    """#144 — a CI/infra story's build proof authored by the platform-engineer
    (internal role `pe`, even in the `-se` filename slot) must satisfy
    build_succeeds, not read as source: null / passed: null."""
    # SE-slot filename, but the internal role is platform-engineer.
    _write_receipt(
        receipts_dir, "US-12", "se",
        {
            "agent": "platform-engineer",
            "verification_commands": [{"command": "pnpm build", "exit_code": 0}],
        },
    )
    _write_receipt(
        receipts_dir, "US-12", "qe",
        {"agent": "quality-engineer", "verification_commands": [{"command": "pytest", "exit_code": 0}]},
    )
    result = evaluate_story_dod(
        str(tmp_path), "US-12", "early", receipts_dir=str(receipts_dir)
    )
    bs = result["checks"]["build_succeeds"]
    assert bs["required"] is True
    assert bs["passed"] is True
    assert bs["source"] == "US-12-se.json"
    assert result["passed"] is True


@pytest.mark.unit
def test_build_succeeds_still_fails_when_pe_build_errored(
    tmp_path: Path, receipts_dir: Path
):
    """A pe-authored build proof with a non-zero exit must NOT pass the gate —
    the cross-role sourcing widens who can supply proof, not what counts."""
    _write_receipt(
        receipts_dir, "US-12b", "se",
        {"agent": "platform-engineer", "verification_commands": [{"command": "pnpm build", "exit_code": 1}]},
    )
    result = evaluate_story_dod(
        str(tmp_path), "US-12b", "early", receipts_dir=str(receipts_dir)
    )
    assert result["checks"]["build_succeeds"]["passed"] is not True


@pytest.mark.unit
def test_build_succeeds_fallback_ignores_qe_only_test_receipt(
    tmp_path: Path, receipts_dir: Path
):
    """The cross-role fallback must find build proof, not any green command."""
    _write_receipt(
        receipts_dir, "US-12c", "qe",
        {"agent": "quality-engineer", "verification_commands": [{"command": "pytest", "exit_code": 0}]},
    )
    result = evaluate_story_dod(
        str(tmp_path), "US-12c", "early", receipts_dir=str(receipts_dir)
    )
    assert result["checks"]["tests_pass"]["passed"] is True
    assert result["checks"]["build_succeeds"]["passed"] is None
    assert result["passed"] is False


@pytest.mark.unit
def test_code_free_story_can_waive_build_succeeds(tmp_path: Path, receipts_dir: Path):
    """#144 — a genuinely code-free story (audit/docs) marks build_succeeds
    not-applicable via `dod_not_applicable`, resolving it to N/A instead of a
    hard unsourceable fail."""
    _write_state(
        tmp_path,
        [{"id": "US-13", "title": "Manual accessibility audit", "dod_not_applicable": ["build_succeeds"]}],
    )
    # Audit produced only a QE findings receipt; no build was run by design.
    _write_receipt(
        receipts_dir, "US-13", "qe",
        {"agent": "quality-engineer", "verification_commands": [{"command": "audit", "exit_code": 0}]},
    )
    result = evaluate_story_dod(
        str(tmp_path), "US-13", "early", receipts_dir=str(receipts_dir)
    )
    bs = result["checks"]["build_succeeds"]
    assert bs["required"] is False
    assert bs["not_applicable"] is True
    assert bs["passed"] is None


@pytest.mark.unit
def test_waiver_cannot_dodge_a_non_waivable_gate(tmp_path: Path, receipts_dir: Path):
    """The waiver allowlist is narrow: listing a non-waivable gate (tests_pass)
    is ignored, so it stays required and can't be used as a bypass."""
    _write_state(
        tmp_path,
        [{"id": "US-14", "title": "Some story", "dod_not_applicable": ["tests_pass"]}],
    )
    result = evaluate_story_dod(
        str(tmp_path), "US-14", "early", receipts_dir=str(receipts_dir)
    )
    assert result["checks"]["tests_pass"]["required"] is True
