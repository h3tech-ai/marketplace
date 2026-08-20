"""Layer 1 — #44 user-facing acceptance gate (ui_acceptance).

Pins the behaviour that closes the "100% DoD with zero UI" hole: a story
whose acceptance criteria describe a user-facing screen cannot reach `done`
on a green backend (SE/QE API) suite alone. The gate is fail-closed —

  - UI-bearing is detected from the story's title + ACs (UI verbs), snapshotted
    onto the story record at `create_story` time.
  - For a UI-bearing story, `ui_acceptance` is promoted into the active DoD
    checks and BLOCKS `reviewing → done` unless the QE receipt carries a
    structured `metrics.ui_verification` proof (or the legacy browser-qa
    metric shape) showing the screen was actually exercised.

Backend-only stories (no UI verbs) are unaffected — regression guard.
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


def _project(tmp_path: Path) -> Path:
    (tmp_path / ".synaptory" / ".orchestrator" / "receipts").mkdir(
        parents=True, exist_ok=True
    )
    return tmp_path


def _write_receipt(project: Path, story_id: str, role: str, **fields) -> None:
    receipts = project / ".synaptory" / ".orchestrator" / "receipts"
    abbrev = sp._role_to_abbrev(role)
    (receipts / f"{story_id}-{abbrev}.json").write_text(
        json.dumps({"agent": role, **fields}), encoding="utf-8"
    )


def _green_backend_receipts(project: Path, story_id: str, qe_metrics=None) -> None:
    """SE + QE receipts that satisfy tests_pass + build_succeeds — the exact
    shape an API-only build produces for a UI-implying story (the #44 bug)."""
    _write_receipt(
        project, story_id, "software-engineer",
        artifacts=["services/api/routes.ts"],
        verification_commands=[{"command": "npm run build", "exit_code": 0}],
    )
    _write_receipt(
        project, story_id, "quality-engineer",
        artifacts=["tests/api/routes.spec.ts"],
        verification_commands=[{"command": "npm test", "exit_code": 0}],
        metrics=qe_metrics or {},
    )


def _seed_story(project: Path, story_id: str, title: str, acs) -> None:
    story = sp.create_story(story_id, title, acceptance_criteria=acs)
    state = {
        "current_stories": [story],
        "current_sprint": 1,
        "lifecycle_state": "SPRINT_EXECUTION",
    }
    sp._write_state(str(project), state)


def _dod(project: Path, story_id: str, intensity: str = "early") -> dict:
    receipts = str(project / ".synaptory" / ".orchestrator" / "receipts")
    return sp.evaluate_story_dod(
        str(project), story_id, intensity, receipts_dir=receipts
    )


# ---------------------------------------------------------------------------
# story_is_ui_bearing — UI-verb detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("text", [
    "User can open the dashboard",
    "When they open the group view the page renders",
    "The console displays the report",
    "Click the submit button on the form",
    "The UI renders in VI/EN across every primary flow",
])
def test_ui_verbs_detected(text):
    assert sp.story_is_ui_bearing(text) is True


@pytest.mark.unit
@pytest.mark.parametrize("text", [
    "Expose a REST endpoint that returns JSON",
    "Persist the order to Postgres with an idempotency key",
    "Publish a Kafka event on payment settlement",
    "",
])
def test_backend_only_not_ui_bearing(text):
    assert sp.story_is_ui_bearing(text) is False


@pytest.mark.unit
def test_whole_word_match_no_false_positives():
    # "review" must not fire "view"; "perform" must not fire "form".
    assert sp.story_is_ui_bearing("Reviewer can perform a backend audit") is False


@pytest.mark.unit
def test_detects_in_tracker_dict_acs():
    acs = [{"id": "AC-1", "given": "logged in", "when": "they open the dashboard",
            "then": "stats are shown", "text": ""}]
    assert sp.story_is_ui_bearing("Stats story", acs) is True


# ---------------------------------------------------------------------------
# create_story — AC snapshot
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_create_story_snapshots_acs_and_ui_flag():
    s = sp.create_story("US-1", "Open the dashboard", acceptance_criteria=["UI renders"])
    assert s["acceptance_criteria"] == ["UI renders"]
    assert s["ui_bearing"] is True


@pytest.mark.unit
def test_create_story_backend_only_flag_false():
    s = sp.create_story("US-2", "Add a webhook endpoint", acceptance_criteria=["returns 200"])
    assert s["ui_bearing"] is False


@pytest.mark.unit
def test_create_story_no_acs_defaults_empty():
    s = sp.create_story("US-3", "Some task")
    assert s["acceptance_criteria"] == []
    assert s["ui_bearing"] is False


# ---------------------------------------------------------------------------
# active_dod_checks — conditional promotion
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_active_checks_promote_ui_acceptance():
    checks = sp.active_dod_checks("early", ui_required=True)
    assert "ui_acceptance" in checks


@pytest.mark.unit
def test_active_checks_omit_ui_acceptance_by_default():
    assert "ui_acceptance" not in sp.active_dod_checks("early")


# ---------------------------------------------------------------------------
# The core bug — UI story passes backend gate but is BLOCKED on ui_acceptance
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ui_story_with_only_backend_receipts_is_blocked(tmp_path):
    p = _project(tmp_path)
    _seed_story(p, "US-1", "User can open the dashboard",
                ["Given login, when they open the dashboard, the UI renders"])
    _green_backend_receipts(p, "US-1")  # tests_pass + build_succeeds GREEN

    dod = _dod(p, "US-1")
    assert dod["ui_required"] is True
    # tests_pass / build_succeeds are green...
    assert dod["checks"]["tests_pass"]["passed"] is True
    assert dod["checks"]["build_succeeds"]["passed"] is True
    # ...but ui_acceptance is required and NOT satisfied → gate blocks.
    assert dod["checks"]["ui_acceptance"]["required"] is True
    assert dod["checks"]["ui_acceptance"]["passed"] is not True
    block = sp.dod_gate_block_reason(dod)
    assert block is not None and "ui_acceptance" in block


@pytest.mark.unit
def test_ui_story_with_structured_proof_passes(tmp_path):
    p = _project(tmp_path)
    _seed_story(p, "US-1", "User can open the dashboard", ["the dashboard renders"])
    _green_backend_receipts(p, "US-1", qe_metrics={
        "ui_verification": {"rendered": True, "routes_tested": 3, "flows_failed": 0},
    })
    dod = _dod(p, "US-1")
    assert dod["checks"]["ui_acceptance"]["passed"] is True
    assert sp.dod_gate_block_reason(dod) is None


@pytest.mark.unit
def test_ui_story_legacy_browser_qa_metrics_pass(tmp_path):
    p = _project(tmp_path)
    _seed_story(p, "US-1", "Open the group view", ["the group view page renders"])
    _green_backend_receipts(p, "US-1", qe_metrics={"routes_tested": 5, "flows_failed": 0})
    dod = _dod(p, "US-1")
    assert dod["checks"]["ui_acceptance"]["passed"] is True
    assert sp.dod_gate_block_reason(dod) is None


@pytest.mark.unit
def test_ui_story_with_failing_flows_is_blocked(tmp_path):
    p = _project(tmp_path)
    _seed_story(p, "US-1", "Open the dashboard", ["the dashboard renders"])
    _green_backend_receipts(p, "US-1", qe_metrics={
        "ui_verification": {"rendered": True, "routes_tested": 3, "flows_failed": 2},
    })
    dod = _dod(p, "US-1")
    assert dod["checks"]["ui_acceptance"]["passed"] is False
    assert sp.dod_gate_block_reason(dod) is not None


# ---------------------------------------------------------------------------
# Regression guard — backend-only story is unaffected
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_backend_story_not_gated_on_ui(tmp_path):
    p = _project(tmp_path)
    _seed_story(p, "US-9", "Add a webhook endpoint", ["POST /hooks returns 202"])
    _green_backend_receipts(p, "US-9")
    dod = _dod(p, "US-9")
    assert dod["ui_required"] is False
    assert dod["checks"]["ui_acceptance"]["required"] is False
    assert dod["passed"] is True
    assert sp.dod_gate_block_reason(dod) is None


@pytest.mark.unit
def test_older_record_without_flag_derives_from_acs(tmp_path):
    # Records written before #44 have no ui_bearing flag — derive from ACs.
    p = _project(tmp_path)
    story = sp.create_story("US-1", "Open the dashboard", acceptance_criteria=["renders"])
    del story["ui_bearing"]
    sp._write_state(str(p), {"current_stories": [story], "current_sprint": 1,
                             "lifecycle_state": "SPRINT_EXECUTION"})
    _green_backend_receipts(p, "US-1")
    dod = _dod(p, "US-1")
    assert dod["ui_required"] is True
    assert sp.dod_gate_block_reason(dod) is not None
