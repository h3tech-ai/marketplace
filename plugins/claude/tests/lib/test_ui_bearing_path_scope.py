"""#758 — a declared path scope with no UI root precludes `ui_bearing`.

Hypothesis: `cycle_records.UNIT_KINDS` (SPQ's fixed vocabulary, SC-MTH-003)
and `story_pipeline._NON_UI_KINDS` (enabler/infra/backend) share no member, so
`kind` can never suppress the `story_is_ui_bearing` keyword heuristic for an
SPQ Work Unit. `path_scope_precludes_ui` closes that hole with a checkable
fact instead: a unit whose every declared path lives outside every UI root
cannot render anything, regardless of what its title/ACs say. The override
is one-directional (only suppresses; never manufactures a positive) and must
not change behaviour for Scrum/Kanban stories, which carry no file_scope.
"""

from __future__ import annotations

import pytest

from cycle_records import UNIT_KINDS
from story_pipeline import (
    _NON_UI_KINDS,
    _story_is_ui_bearing_in_state,
    create_story,
    path_scope_precludes_ui,
)

# WU-106's actual shape (ptb-assistant Cycle 1): "view" in the BRD-12-01
# sense (a database view / read model), path_scope entirely under src/ptb/api.
WU_106_TITLE = "One application service entry point and the project view"
WU_106_ACS = [
    "Every view presents one consistent project revision",
    "Requests are authorized before they reach the service",
]
WU_106_PATH_SCOPE = ["src/ptb/api/", "tests/api/", "contracts/"]


@pytest.mark.unit
def test_unit_kinds_and_non_ui_kinds_share_no_member():
    """The root cause, asserted directly so a future edit to either
    vocabulary that accidentally restores an overlap is caught here rather
    than rediscovered against a live Cycle."""
    assert set(UNIT_KINDS).isdisjoint(_NON_UI_KINDS)


@pytest.mark.unit
def test_precludes_ui_with_no_web_root():
    assert path_scope_precludes_ui(WU_106_PATH_SCOPE) is True


@pytest.mark.unit
def test_does_not_preclude_when_a_web_root_is_present():
    assert path_scope_precludes_ui(["src/ptb/api/", "web/src/shell/"]) is False


@pytest.mark.unit
def test_empty_scope_defers_rather_than_precludes():
    """No declared scope is not evidence of anything -- Scrum/Kanban stories
    carry none, and the keyword heuristic must still run for them."""
    assert path_scope_precludes_ui([]) is False
    assert path_scope_precludes_ui(None) is False


@pytest.mark.unit
def test_a_backend_style_path_containing_web_as_a_substring_is_not_a_root():
    """"webhooks/" is not "web/" -- a substring match would misfire on a
    perfectly ordinary backend directory name."""
    assert path_scope_precludes_ui(["src/webhooks/", "tests/webhooks/"]) is True


@pytest.mark.unit
def test_wu_106_no_longer_flagged_ui_bearing():
    story = create_story(
        "WU-106",
        WU_106_TITLE,
        kind="feature",  # SPQ's UNIT_KINDS -- never in _NON_UI_KINDS
        acceptance_criteria=WU_106_ACS,
        file_scope=WU_106_PATH_SCOPE,
    )
    assert story["ui_bearing"] is False


@pytest.mark.unit
def test_genuinely_ui_bearing_unit_is_unaffected():
    story = create_story(
        "WU-107",
        "Project shell renders the workbench view",
        kind="feature",
        acceptance_criteria=["The dashboard screen displays the active project"],
        file_scope=["web/src/shell/", "web/tests/shell/"],
    )
    assert story["ui_bearing"] is True


@pytest.mark.unit
def test_scrum_kind_suppression_still_works_with_no_file_scope():
    story = create_story(
        "US-1", "Backend view refresh job", kind="backend",
        acceptance_criteria=["The view materializes on schedule"],
    )
    assert story["ui_bearing"] is False


@pytest.mark.unit
def test_scrum_keyword_heuristic_still_fires_with_no_file_scope():
    story = create_story(
        "US-2", "Dashboard screen", kind="",
        acceptance_criteria=["User can view the dashboard screen"],
    )
    assert story["ui_bearing"] is True


@pytest.mark.unit
def test_live_state_rederivation_applies_the_same_override(tmp_path):
    """`_story_is_ui_bearing_in_state` re-derives from live state; it must
    reach the same answer for a record predating the snapshot fix (no
    `ui_bearing` key) but carrying the same path scope."""
    import json

    proj = tmp_path
    orchestrator = proj / ".synaptory" / ".orchestrator"
    orchestrator.mkdir(parents=True)
    (orchestrator / "pipeline-state.json").write_text(json.dumps({
        "current_stories": [{
            "id": "WU-106",
            "title": WU_106_TITLE,
            "kind": "feature",
            "acceptance_criteria": WU_106_ACS,
            "file_scope": WU_106_PATH_SCOPE,
            # no "ui_bearing" key -- simulates a pre-#758 record
        }]
    }))
    assert _story_is_ui_bearing_in_state(str(proj), "WU-106") is False
