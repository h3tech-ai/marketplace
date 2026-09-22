"""#809 — the tier's required stages travel on the dispatch contract.

Two places decided the pipeline's shape and they disagreed: `DOD_TIER_CHECKS`
omits `code_reviewed` at `early`, while the orchestration prose described the
pipeline as SE -> QE -> CR unconditionally. The one with no code in it won,
silently — an Engineering Lead dispatched a reviewer because the document said
three stages, and paid 20m30s for a stage the Definition of Done never required.

Worse than redundant: a `needs-work` at `early` blocks a unit the DoD would
otherwise have closed, because `code_reviewed` is not in the tier.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks" / "lib"))

import story_pipeline as sp  # noqa: E402

pytestmark = pytest.mark.unit


def _action(tier: str):
    state = {
        "current_stories": [
            {"id": "WU-1", "state": "queued", "pipeline_log": [], "retries": {}}
        ]
    }
    return sp.next_action(state, dod_tier_info={"tier": tier, "tier_source": "planned"})


def test_early_says_it_asks_for_no_code_review():
    """The fact an Engineering Lead had no way to see mid-Cycle."""
    dod = _action("early")["dod"]

    assert dod["cr_required"] is False
    assert dod["stages_required"] == ["se", "qe"]
    assert "cr" not in dod["stages_required"]


@pytest.mark.parametrize("tier", ["growing", "mature", "release"])
def test_a_tier_that_requires_review_says_so(tier):
    dod = _action(tier)["dod"]

    assert dod["cr_required"] is True
    assert dod["stages_required"] == ["se", "qe", "cr"]


def test_the_contract_agrees_with_the_tier_table_it_is_derived_from():
    """Two sources of the same fact is what produced #809; this pins them."""
    for tier, checks in sp.DOD_TIER_CHECKS.items():
        dod = _action(tier)["dod"]
        assert dod["cr_required"] == ("code_reviewed" in checks), tier
        assert ("cr" in dod["stages_required"]) == ("code_reviewed" in checks), tier


def test_early_never_returns_a_cr_dispatch():
    """The machine's side was always right; this guards it staying right."""
    state = {
        "current_stories": [
            {"id": "WU-1", "state": "reviewing", "pipeline_log": [], "retries": {}}
        ]
    }
    action = sp.next_action(
        state, dod_tier_info={"tier": "early", "tier_source": "planned"}
    )
    assert action["action"] != "dispatch_cr"
