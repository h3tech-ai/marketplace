"""Layer 1 — LinearAdapter mapping, status resolution, and cycle handling.

Linear teams define their own workflow state names, so the adapter cannot
hardcode a status table the way the Jira adapter does. Most of these tests
exercise the three-tier resolution (`status_map` override → built-in name →
`state.type` fallback) against synthetic team state lists, including the
AWAITING_ACCEPTANCE fallback that `story_pipeline.py` contractually requires.

Transport is substituted at the boundary with a FakeTransport, matching the
convention in test_assignee_filter.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2]
    / "skills" / "_shared" / "scripts"
)
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from tracker.base import SprintInfo as SprintInfoT, Story as StoryT  # noqa: E402


# A conventional Linear workflow, matching the built-in STATUS_MAP names.
STATES_DEFAULT = [
    {"id": "s-backlog", "name": "Backlog", "type": "backlog", "position": 0},
    {"id": "s-todo", "name": "Todo", "type": "unstarted", "position": 1},
    {"id": "s-prog", "name": "In Progress", "type": "started", "position": 2},
    {"id": "s-review", "name": "In Review", "type": "started", "position": 3},
    {"id": "s-done", "name": "Done", "type": "completed", "position": 4},
    {"id": "s-cancel", "name": "Canceled", "type": "canceled", "position": 5},
]

# A fully custom workflow — no name matches STATUS_MAP. Only the `type`
# fallback can resolve this one.
STATES_CUSTOM = [
    {"id": "c-triage", "name": "Inbox", "type": "triage", "position": 0},
    {"id": "c-back", "name": "Icebox", "type": "backlog", "position": 1},
    {"id": "c-next", "name": "Up Next", "type": "unstarted", "position": 2},
    {"id": "c-build", "name": "Building", "type": "started", "position": 3},
    {"id": "c-peer", "name": "Peer Review", "type": "started", "position": 4},
    {"id": "c-ship", "name": "Shipped", "type": "completed", "position": 5},
    {"id": "c-nope", "name": "Nope", "type": "canceled", "position": 6},
]

# A team with a single started state — no distinct review column.
STATES_FLAT = [
    {"id": "f-todo", "name": "Todo", "type": "unstarted", "position": 0},
    {"id": "f-prog", "name": "In Progress", "type": "started", "position": 1},
    {"id": "f-done", "name": "Done", "type": "completed", "position": 2},
]


class FakeTransport:
    """Records mutations; serves canned reads."""

    def __init__(self, states=None, estimation="fibonacci"):
        self.states = states if states is not None else STATES_DEFAULT
        self.estimation = estimation
        self.created_issues = []
        self.updated_issues = []
        self.created_cycles = []
        self.updated_cycles = []
        self.searches = []
        self.max_results_seen = []
        self.cycles = []
        self.issues = []
        self.issues_by_id = {}
        self.resolved_projects = []
        self.project_ids = {}
        self.api_calls = 0
        self._label_ids = {}

    # reads
    def list_workflow_states(self):
        return self.states

    def issue_estimation_type(self):
        return self.estimation

    def get_or_create_label(self, name):
        return self._label_ids.setdefault(name, f"label:{name}")

    def get_issue(self, issue_id):
        return self.issues_by_id.get(issue_id)

    def search_issues(self, filter_=None, order_by="updatedAt", max_results=200):
        self.searches.append(filter_ or {})
        self.max_results_seen.append(max_results)
        self.api_calls += 1
        f = filter_ or {}
        want = ((f.get("cycle") or {}).get("id") or {}).get("in")
        if want is not None:
            out = [i for i in self.issues
                   if (i.get("cycle") or {}).get("id") in want]
        else:
            out = list(self.issues)
        # Honor the cap the way the real transport does — otherwise a
        # truncation regression cannot fail here.
        return out[:max_results]

    def list_cycles(self, max_results=100):
        self.api_calls += 1
        return [{k: v for k, v in c.items() if k != "issues"} for c in self.cycles]

    def resolve_project_id(self, name_or_id):
        self.resolved_projects.append(name_or_id)
        return self.project_ids.get(name_or_id, name_or_id)

    # writes
    def create_issue(self, input_):
        self.created_issues.append(input_)
        return {"id": "new-uuid", "identifier": "ENG-1",
                "url": "https://linear.app/acme/issue/ENG-1"}

    def update_issue(self, issue_id, input_):
        self.updated_issues.append((issue_id, input_))
        return self.issues_by_id.get(issue_id, {})

    def create_cycle(self, input_):
        self.created_cycles.append(input_)
        return {"id": "cycle-new", "number": 9}

    def update_cycle(self, cycle_id, input_):
        self.updated_cycles.append((cycle_id, input_))
        return {"id": cycle_id}


def make_adapter(tmp_path, *, states=None, spec=None, status_map=None,
                 estimation="fibonacci", manage_cycles=False, id_map=None):
    """Build a LinearAdapter with __init__ bypassed and internals injected."""
    from tracker.linear_adapter import LinearAdapter

    a = object.__new__(LinearAdapter)
    a.project_dir = tmp_path
    a.config = None
    a.spec = spec
    a.team_key = "ENG"
    a._status_map_override = dict(status_map or {})
    from tracker.transport.linear_transport import _norm_status
    a._status_reverse_override = {
        _norm_status(v): k for k, v in (status_map or {}).items() if v
    }
    a._entity_label_overrides = {}
    a._label_prefix = "hc:"
    a._manage_cycles = manage_cycles
    a._cycle_length_days = 14
    a._default_project_id = ""
    a._states = states if states is not None else STATES_DEFAULT
    a._id_map = dict(id_map or {})
    a._cycles_cache = None
    a.transport = FakeTransport(states=a._states, estimation=estimation)

    orch = tmp_path / ".synaptory" / ".orchestrator"
    a._id_map_path = orch / "tracker-id-map.json"
    a._backlog_order_path = orch / "backlog-order.json"
    a._local_cache_path = orch / "tracker-data.json"
    return a


def issue(**over):
    """Build an issue in the shape _ISSUE_FIELDS returns.

    When `label_names` is given without an explicit `description`, the
    `> synaptory:` line is derived from the labels so the two identity
    sources agree — a fixture where they disagree tests nothing real.
    """
    base = {
        "id": "i-uuid", "identifier": "ENG-7",
        "url": "https://linear.app/acme/issue/ENG-7",
        "title": "Login form", "description": "> synaptory: US-007",
        "estimate": None, "priority": 0,
        "state": {"id": "s-todo", "name": "Todo", "type": "unstarted", "position": 1},
        "labels": {"nodes": []},
        "assignee": None, "cycle": None, "project": None,
    }
    labels = over.pop("label_names", None)
    if labels is not None:
        base["labels"] = {"nodes": [{"id": f"l{i}", "name": n}
                                    for i, n in enumerate(labels)]}
        if "description" not in over:
            own = [n for n in labels
                   if n.startswith(("US-", "BUG-", "TASK-", "ENH-"))]
            if own:
                base["description"] = f"> synaptory: {own[0]}"
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Status resolution — three tiers
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("canonical,expected", [
    ("BACKLOG", "s-backlog"), ("TO_DO", "s-todo"),
    ("IN_PROGRESS", "s-prog"), ("IN_REVIEW", "s-review"),
    ("DONE", "s-done"), ("CANCELLED", "s-cancel"),
])
def test_builtin_names_resolve_on_a_conventional_workflow(tmp_path, canonical, expected):
    a = make_adapter(tmp_path)
    assert a._resolve_state(canonical)["id"] == expected


@pytest.mark.unit
@pytest.mark.parametrize("canonical,expected", [
    ("BACKLOG", "c-back"), ("TO_DO", "c-next"),
    ("IN_PROGRESS", "c-build"), ("IN_REVIEW", "c-peer"),
    ("DONE", "c-ship"), ("CANCELLED", "c-nope"),
])
def test_custom_states_resolve_by_type(tmp_path, canonical, expected):
    """The tier-3 fallback is what makes an unconfigured custom team work."""
    a = make_adapter(tmp_path, states=STATES_CUSTOM)
    assert a._resolve_state(canonical)["id"] == expected


@pytest.mark.unit
def test_status_map_override_wins_over_builtin(tmp_path):
    a = make_adapter(tmp_path, states=STATES_CUSTOM,
                     status_map={"IN_REVIEW": "Building"})
    assert a._resolve_state("IN_REVIEW")["id"] == "c-build"


@pytest.mark.unit
def test_in_review_prefers_review_named_started_state(tmp_path):
    """`Peer Review` beats position ordering among started states."""
    states = [
        {"id": "x-build", "name": "Building", "type": "started", "position": 1},
        {"id": "x-peer", "name": "Peer Review", "type": "started", "position": 2},
        {"id": "x-stage", "name": "Staging", "type": "started", "position": 3},
    ]
    a = make_adapter(tmp_path, states=states)
    assert a._resolve_state("IN_REVIEW")["id"] == "x-peer"


@pytest.mark.unit
def test_in_review_falls_back_to_last_started_state(tmp_path):
    states = [
        {"id": "y-a", "name": "Building", "type": "started", "position": 1},
        {"id": "y-b", "name": "Polishing", "type": "started", "position": 2},
    ]
    a = make_adapter(tmp_path, states=states)
    assert a._resolve_state("IN_REVIEW")["id"] == "y-b"


@pytest.mark.unit
def test_in_review_on_single_started_state_is_in_progress(tmp_path):
    a = make_adapter(tmp_path, states=STATES_FLAT)
    assert a._resolve_state("IN_REVIEW")["id"] == "f-prog"


@pytest.mark.unit
def test_awaiting_acceptance_prefers_a_dedicated_state(tmp_path):
    states = STATES_DEFAULT + [
        {"id": "s-accept", "name": "Awaiting Acceptance",
         "type": "started", "position": 4},
    ]
    a = make_adapter(tmp_path, states=states)
    assert a._resolve_state("AWAITING_ACCEPTANCE")["id"] == "s-accept"


@pytest.mark.unit
def test_awaiting_acceptance_falls_back_to_review_plus_label(tmp_path):
    """The story_pipeline.py contract: a team with no acceptance column lands
    on the review state AND gets the `awaiting-acceptance` label."""
    a = make_adapter(tmp_path, id_map={"US-007": {"linear_id": "i-uuid"}})
    a.transport.issues_by_id["i-uuid"] = issue(label_names=["US-007"])
    a._validate_status_transition = lambda *args, **kw: None
    a.get_story = lambda sid: None

    a.update_story_status("US-007", "AWAITING_ACCEPTANCE")

    _, payload = a.transport.updated_issues[0]
    assert payload["stateId"] == "s-review"
    assert "label:awaiting-acceptance" in payload["labelIds"]


@pytest.mark.unit
def test_awaiting_acceptance_label_overrides_state_on_readback(tmp_path):
    """Read-back must invert the fallback, or the two states are
    indistinguishable on a team without an acceptance column."""
    a = make_adapter(tmp_path)
    i = issue(
        state={"id": "s-review", "name": "In Review", "type": "started", "position": 3},
        label_names=["US-007", "awaiting-acceptance"],
    )
    assert a._canonical_from_issue(i) == "AWAITING_ACCEPTANCE"


@pytest.mark.unit
def test_review_state_without_label_reads_back_as_in_review(tmp_path):
    a = make_adapter(tmp_path)
    i = issue(state={"id": "s-review", "name": "In Review",
                     "type": "started", "position": 3})
    assert a._canonical_from_issue(i) == "IN_REVIEW"


@pytest.mark.unit
def test_custom_state_name_reads_back_via_type(tmp_path):
    a = make_adapter(tmp_path, states=STATES_CUSTOM)
    i = issue(state={"id": "c-build", "name": "Building",
                     "type": "started", "position": 3})
    assert a._canonical_from_issue(i) == "IN_PROGRESS"


@pytest.mark.unit
def test_cancelled_sets_canceled_state_and_label(tmp_path):
    a = make_adapter(tmp_path, id_map={"US-007": {"linear_id": "i-uuid"}})
    a.transport.issues_by_id["i-uuid"] = issue(label_names=["US-007"])
    a._validate_status_transition = lambda *args, **kw: None
    a.get_story = lambda sid: None

    a.update_story_status("US-007", "CANCELLED")

    _, payload = a.transport.updated_issues[0]
    assert payload["stateId"] == "s-cancel"
    assert "label:cancelled" in payload["labelIds"]


@pytest.mark.unit
def test_blocked_applies_label_without_state_change(tmp_path):
    """BLOCKED is an overlay, not a workflow stage."""
    a = make_adapter(tmp_path, id_map={"US-007": {"linear_id": "i-uuid"}})
    a.transport.issues_by_id["i-uuid"] = issue(label_names=["US-007"])
    a._validate_status_transition = lambda *args, **kw: None
    a.get_story = lambda sid: None

    a.update_story_status("US-007", "BLOCKED")

    _, payload = a.transport.updated_issues[0]
    assert "stateId" not in payload
    assert "label:blocked" in payload["labelIds"]


@pytest.mark.unit
def test_status_markers_are_mutually_exclusive(tmp_path):
    """Moving on from IN_REVIEW must drop its marker, or read-back sees two."""
    a = make_adapter(tmp_path, id_map={"US-007": {"linear_id": "i-uuid"}})
    a.transport.issues_by_id["i-uuid"] = issue(
        label_names=["US-007", "awaiting-acceptance"],
    )
    a._validate_status_transition = lambda *args, **kw: None
    a.get_story = lambda sid: None

    a.update_story_status("US-007", "DONE")

    _, payload = a.transport.updated_issues[0]
    assert "label:awaiting-acceptance" not in payload["labelIds"]
    assert "label:US-007" in payload["labelIds"]


@pytest.mark.unit
def test_unresolvable_state_warns_and_does_not_raise(tmp_path, capsys):
    """A raise here would wedge story_pipeline.sync_tracker_status."""
    a = make_adapter(tmp_path, states=[
        {"id": "z", "name": "Only", "type": "started", "position": 0},
    ], id_map={"US-007": {"linear_id": "i-uuid"}})
    a.transport.issues_by_id["i-uuid"] = issue(label_names=["US-007"])
    a._validate_status_transition = lambda *args, **kw: None
    a.get_story = lambda sid: None

    a.update_story_status("US-007", "BACKLOG")

    assert "no workflow state resolves" in capsys.readouterr().err


@pytest.mark.unit
def test_every_tracker_stage_has_a_status_map_entry():
    from tracker.linear_adapter import STATUS_MAP, STATUS_TYPE_FALLBACK
    from tracker.transitions import TRACKER_STAGES
    for stage in TRACKER_STAGES:
        assert stage in STATUS_MAP, f"{stage} missing from STATUS_MAP"
        assert stage in STATUS_TYPE_FALLBACK, (
            f"{stage} missing from STATUS_TYPE_FALLBACK — a custom workflow "
            "could not resolve it"
        )


@pytest.mark.unit
def test_update_story_status_calls_validate_transition(tmp_path):
    a = make_adapter(tmp_path, id_map={"US-007": {"linear_id": "i-uuid"}})
    a.transport.issues_by_id["i-uuid"] = issue(label_names=["US-007"])
    a.get_story = lambda sid: None
    seen = {}

    def fake_validate(story_id, target, allow_skip):
        seen["args"] = (story_id, target, allow_skip)

    a._validate_status_transition = fake_validate
    a.update_story_status("US-007", "IN_PROGRESS")

    assert seen["args"] == ("US-007", "IN_PROGRESS", False)


# ---------------------------------------------------------------------------
# Filters — dicts, not strings
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mine_sentinel_builds_isme_filter(tmp_path):
    from tracker.linear_adapter import LinearAdapter
    assert LinearAdapter._assignee_fragment("currentUser()") == {
        "assignee": {"isMe": {"eq": True}}
    }


@pytest.mark.unit
def test_email_assignee_builds_email_filter(tmp_path):
    from tracker.linear_adapter import LinearAdapter
    assert LinearAdapter._assignee_fragment("dev@h3t.co") == {
        "assignee": {"email": {"eq": "dev@h3t.co"}}
    }


@pytest.mark.unit
def test_non_email_assignee_builds_name_filter(tmp_path):
    from tracker.linear_adapter import LinearAdapter
    assert LinearAdapter._assignee_fragment("Trung") == {
        "assignee": {"name": {"eqIgnoreCase": "Trung"}}
    }


@pytest.mark.unit
def test_query_tickets_sends_mine_filter_to_transport(tmp_path):
    from tracker.base import QueryFilter
    a = make_adapter(tmp_path)
    a.query_tickets(QueryFilter(assignee="currentUser()"))
    assert a.transport.searches[0]["assignee"] == {"isMe": {"eq": True}}


@pytest.mark.unit
def test_unresolvable_sprint_returns_empty_not_unfiltered(tmp_path, capsys):
    """#187 — dropping the sprint filter would widen `--sprint N --mine` to
    every ticket the user owns across all sprints."""
    from tracker.base import QueryFilter
    a = make_adapter(tmp_path)
    seed_cycles(a)

    out = a.query_tickets(QueryFilter(sprint=42, assignee="currentUser()"))

    assert out == []
    assert a.transport.searches == []  # never reached the wire
    assert "returning no tickets" in capsys.readouterr().err


@pytest.mark.unit
def test_spec_label_filter_survives_collision_with_entity_label(tmp_path):
    """Both the spec filter and the entity-type filter key on `labels`. A
    plain dict merge drops one of them and silently widens the query to the
    whole team — the collision must land in Linear's `and:` array instead."""
    from tracker.config import SpecConfig, SpecFilter, LinearConfig
    spec = SpecConfig(id="alpha", linear=LinearConfig(team_key="ENG"),
                      filter=SpecFilter(type="label", value="spec-alpha"))
    a = make_adapter(tmp_path, spec=spec)

    a.get_backlog()

    sent = a.transport.searches[0]
    serialized = json.dumps(sent)
    assert "spec-alpha" in serialized, "spec scope was dropped by the merge"
    assert "hc:story" in serialized, "entity-type scope was dropped by the merge"
    assert sent.get("and"), "colliding keys should be conjoined, not overwritten"


@pytest.mark.unit
def test_merge_filters_keeps_distinct_keys_flat(tmp_path):
    a = make_adapter(tmp_path)
    merged = a._merge_filters(
        {"labels": {"name": {"eq": "x"}}},
        {"cycle": {"id": {"eq": "cy-1"}}},
    )
    assert merged == {
        "labels": {"name": {"eq": "x"}},
        "cycle": {"id": {"eq": "cy-1"}},
    }
    assert "and" not in merged


@pytest.mark.unit
def test_merge_filters_conjoins_colliding_keys(tmp_path):
    a = make_adapter(tmp_path)
    merged = a._merge_filters(
        {"labels": {"name": {"eq": "spec-alpha"}}},
        {"labels": {"name": {"eq": "hc:story"}}},
        {"labels": {"name": {"eq": "EPIC-001"}}},
    )
    assert merged["labels"] == {"name": {"eq": "spec-alpha"}}
    assert merged["and"] == [
        {"labels": {"name": {"eq": "hc:story"}}},
        {"labels": {"name": {"eq": "EPIC-001"}}},
    ]


@pytest.mark.unit
def test_list_stories_by_epic_keeps_both_label_scopes(tmp_path):
    """Epic linkage is a label, so `list_stories(epic_id=...)` collides with
    the entity-type label too."""
    a = make_adapter(tmp_path)
    a.list_stories(epic_id="EPIC-001")

    serialized = json.dumps(a.transport.searches[0])
    assert "EPIC-001" in serialized
    assert "hc:story" in serialized


@pytest.mark.unit
def test_spec_project_filter_uses_name_or_uuid(tmp_path):
    from tracker.config import SpecConfig, SpecFilter, LinearConfig
    by_name = make_adapter(tmp_path, spec=SpecConfig(
        id="a", linear=LinearConfig(team_key="ENG"),
        filter=SpecFilter(type="project", value="Platform"),
    ))
    assert by_name._spec_filter_fragment() == {
        "project": {"name": {"eq": "Platform"}}
    }

    uuid = "0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9"
    by_id = make_adapter(tmp_path, spec=SpecConfig(
        id="b", linear=LinearConfig(team_key="ENG"),
        filter=SpecFilter(type="project", value=uuid),
    ))
    assert by_id._spec_filter_fragment() == {"project": {"id": {"eq": uuid}}}


@pytest.mark.unit
def test_spec_write_side_effect_appends_label(tmp_path):
    from tracker.config import SpecConfig, SpecFilter, LinearConfig
    spec = SpecConfig(id="alpha", linear=LinearConfig(team_key="ENG"),
                      filter=SpecFilter(type="label", value="spec-alpha"))
    a = make_adapter(tmp_path, spec=spec)
    labels, extra = a._apply_spec_write_side_effect(labels=["US-1"])
    assert "spec-alpha" in labels
    assert "projectId" not in extra


@pytest.mark.unit
def test_spec_write_side_effect_sets_project_id(tmp_path):
    from tracker.config import SpecConfig, SpecFilter, LinearConfig
    spec = SpecConfig(id="beta", linear=LinearConfig(team_key="ENG"),
                      filter=SpecFilter(type="project", value="proj-uuid"))
    a = make_adapter(tmp_path, spec=spec)
    labels, extra = a._apply_spec_write_side_effect(labels=["US-1"])
    assert extra["projectId"] == "proj-uuid"
    assert labels == ["US-1"]


# ---------------------------------------------------------------------------
# Mapping + ID handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_extract_synaptory_id_from_label(tmp_path):
    a = make_adapter(tmp_path)
    i = issue(label_names=["hc:story", "US-042"], description="")
    assert a._extract_synaptory_id(i) == "US-042"


@pytest.mark.unit
def test_extract_synaptory_id_from_description_metadata_line(tmp_path):
    """Authoritative on Linear: we write it at create time with the entity's
    own id, and Linear stores descriptions as raw Markdown."""
    a = make_adapter(tmp_path)
    i = issue(label_names=["hc:story"], description="> synaptory: US-099\n\n## Story")
    assert a._extract_synaptory_id(i) == "US-099"


@pytest.mark.unit
def test_story_does_not_identify_itself_as_its_epic(tmp_path):
    """Label-only epic linkage means a story carries TWO id-shaped labels.
    Returning the first match makes the story identify as its epic — and
    Linear does not guarantee label order, so it would fail intermittently."""
    a = make_adapter(tmp_path)
    # Epic label deliberately first, description line absent.
    i = issue(label_names=["EPIC-001", "US-001", "hc:story"], description="")
    assert a._extract_synaptory_id(i) == "US-001"


@pytest.mark.unit
def test_epic_still_identifies_as_its_epic_id(tmp_path):
    a = make_adapter(tmp_path)
    i = issue(label_names=["EPIC-001", "hc:epic"], description="")
    assert a._extract_synaptory_id(i) == "EPIC-001"


@pytest.mark.unit
def test_story_with_epic_and_feature_labels_picks_its_own_id(tmp_path):
    a = make_adapter(tmp_path)
    i = issue(label_names=["FEAT-003", "EPIC-001", "US-001", "hc:story"],
              description="")
    assert a._extract_synaptory_id(i) == "US-001"


@pytest.mark.unit
def test_description_line_wins_over_ambiguous_labels(tmp_path):
    a = make_adapter(tmp_path)
    i = issue(label_names=["EPIC-001", "US-001", "hc:story"],
              description="> synaptory: US-001 | Feature: FEAT-003")
    assert a._extract_synaptory_id(i) == "US-001"


@pytest.mark.unit
def test_epic_and_feature_derived_from_labels(tmp_path):
    """Epic linkage is label-only — not parentId, not a Linear Project."""
    a = make_adapter(tmp_path)
    story = a._issue_to_story(issue(
        label_names=["US-007", "EPIC-001", "FEAT-003", "hc:story"],
    ))
    assert story.epic == "EPIC-001"
    assert story.feature == "FEAT-003"


@pytest.mark.unit
def test_issue_to_story_maps_core_fields(tmp_path):
    a = make_adapter(tmp_path)
    story = a._issue_to_story(issue(
        label_names=["US-007"], priority=2, estimate=3,
        assignee={"name": "Trung", "email": "dev@h3t.co"},
        state={"id": "s-prog", "name": "In Progress",
               "type": "started", "position": 2},
    ))
    assert story.id == "US-007"
    assert story.status == "IN_PROGRESS"
    assert story.priority == "Should"
    assert story.size == "M"
    assert story.assignee == "dev@h3t.co"
    assert story.tracker_id == "ENG-7"


@pytest.mark.unit
def test_id_map_stores_uuid_and_identifier(tmp_path):
    a = make_adapter(tmp_path)
    a._save_id_mapping("US-007", "uuid-1", "ENG-7", "https://x/ENG-7")

    saved = json.loads(a._id_map_path.read_text(encoding="utf-8"))
    assert saved["US-007"]["linear_id"] == "uuid-1"
    assert saved["US-007"]["linear_identifier"] == "ENG-7"


@pytest.mark.unit
def test_resolve_id_returns_uuid_for_mutations(tmp_path):
    a = make_adapter(tmp_path, id_map={
        "US-007": {"linear_id": "uuid-1", "linear_identifier": "ENG-7"},
    })
    assert a._resolve_id("US-007") == "uuid-1"


@pytest.mark.unit
def test_create_ticket_labels_id_entity_and_epic(tmp_path):
    from tracker.base import Story
    a = make_adapter(tmp_path)
    a.create_ticket(Story(id="US-007", title="US-007 — Login", epic="EPIC-001"))

    sent = a.transport.created_issues[0]
    assert sent["title"] == "Login"
    assert set(sent["labelIds"]) >= {
        "label:US-007", "label:hc:story", "label:EPIC-001",
    }
    assert sent["description"].startswith("> synaptory: US-007")


# ---------------------------------------------------------------------------
# Estimates
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_estimate_omitted_when_estimation_not_used(tmp_path):
    from tracker.base import Story
    a = make_adapter(tmp_path, estimation="notUsed")
    a.create_ticket(Story(id="US-1", title="X", size="M"))
    assert "estimate" not in a.transport.created_issues[0]


@pytest.mark.unit
@pytest.mark.parametrize("scale,size,expected", [
    ("fibonacci", "L", 5),
    ("exponential", "L", 4),
    ("tShirt", "L", 3),
])
def test_estimate_scale_selected_by_issue_estimation_type(tmp_path, scale, size, expected):
    from tracker.base import Story
    a = make_adapter(tmp_path, estimation=scale)
    a.create_ticket(Story(id="US-1", title="X", size=size))
    assert a.transport.created_issues[0]["estimate"] == expected


# ---------------------------------------------------------------------------
# Cycles
# ---------------------------------------------------------------------------


def _cycle(cid, number, issues=None, **over):
    """Build a cycle. `issues` are stamped with the cycle ref so the fake
    transport's filtered `issues` query returns them — matching the real
    two-call shape (cycle scalars, then one flat filtered issues query)."""
    c = {
        "id": cid, "number": number, "name": f"Cycle {number}",
        "description": "", "startsAt": "", "endsAt": "", "completedAt": None,
        "issues": {"nodes": issues or []},
    }
    c.update(over)
    for i in (issues or []):
        # `issue()` seeds `cycle: None`, so setdefault would not fire.
        if not i.get("cycle"):
            i["cycle"] = {"id": cid, "number": number}
    return c


def seed_cycles(adapter, *cycles):
    """Load cycles into the fake transport the way Linear serves them."""
    adapter.transport.cycles = list(cycles)
    adapter.transport.issues = [
        i for c in cycles for i in (c.get("issues") or {}).get("nodes", [])
    ]
    adapter._cycles_cache = None


@pytest.mark.unit
def test_create_sprint_is_idempotent_when_cycle_exists(tmp_path):
    from tracker.base import SprintInfo
    a = make_adapter(tmp_path, manage_cycles=True)
    seed_cycles(a, _cycle("cy-3", 3))

    a.create_sprint(SprintInfo(number=3, goal="Ship auth"))

    assert a.transport.created_cycles == []          # no cycleCreate
    assert a.transport.updated_cycles[0][0] == "cy-3"  # goal synced


@pytest.mark.unit
def test_create_sprint_refuses_when_manage_cycles_false(tmp_path):
    from tracker.base import SprintInfo
    from tracker.base import AdapterError
    a = make_adapter(tmp_path, manage_cycles=False)
    seed_cycles(a)

    with pytest.raises(AdapterError) as exc:
        a.create_sprint(SprintInfo(number=3, goal="Ship auth"))
    assert "manage_cycles" in str(exc.value)


@pytest.mark.unit
def test_create_sprint_creates_when_manage_cycles_true(tmp_path):
    from tracker.base import SprintInfo
    a = make_adapter(tmp_path, manage_cycles=True)
    seed_cycles(a)

    a.create_sprint(SprintInfo(number=3, goal="Ship auth"))

    assert a.transport.created_cycles[0]["name"] == "Sprint 3"


@pytest.mark.unit
def test_sprint_number_resolves_via_id_map_before_cycle_number(tmp_path):
    """A team already at cycle 47 has no cycle 1 for Sprint 1 to bind to."""
    a = make_adapter(tmp_path, id_map={
        "SPRINT-1": {"linear_id": "cy-47", "linear_number": 47},
    })
    seed_cycles(a, _cycle("cy-47", 47), _cycle("cy-1", 1))

    assert a._resolve_cycle(1)["id"] == "cy-47"


@pytest.mark.unit
def test_sprint_falls_back_to_cycle_number_on_a_fresh_team(tmp_path):
    a = make_adapter(tmp_path)
    seed_cycles(a, _cycle("cy-1", 1), _cycle("cy-2", 2))
    assert a._resolve_cycle(2)["id"] == "cy-2"


@pytest.mark.unit
def test_close_sprint_does_not_raise_on_already_complete(tmp_path):
    a = make_adapter(tmp_path, manage_cycles=True)
    seed_cycles(a, _cycle("cy-3", 3, completedAt="2026-01-01T00:00:00Z"))

    info = a.close_sprint(3)

    assert info.number == 3
    assert a.transport.updated_cycles == []


@pytest.mark.unit
def test_close_sprint_on_unknown_cycle_returns_placeholder(tmp_path):
    a = make_adapter(tmp_path)
    seed_cycles(a)
    assert a.close_sprint(9).number == 9


@pytest.mark.unit
def test_move_incomplete_skips_completed_and_canceled(tmp_path):
    done = issue(id="i-done", label_names=["US-1"],
                 state={"id": "s-done", "name": "Done",
                        "type": "completed", "position": 4})
    killed = issue(id="i-cancel", label_names=["US-2"],
                   state={"id": "s-cancel", "name": "Canceled",
                          "type": "canceled", "position": 5})
    open_ = issue(id="i-open", label_names=["US-3"],
                  state={"id": "s-prog", "name": "In Progress",
                         "type": "started", "position": 2})
    a = make_adapter(tmp_path)
    seed_cycles(a, _cycle("cy-1", 1, [done, killed, open_]), _cycle("cy-2", 2))

    moved = a.move_incomplete_to_next(1, 2)

    assert [m.id for m in moved] == ["US-3"]
    assert a.transport.updated_issues == [("i-open", {"cycleId": "cy-2"})]


@pytest.mark.unit
def test_move_incomplete_is_noop_for_already_rolled_over_issues(tmp_path):
    """Linear may roll issues over natively before we get there."""
    already = issue(id="i-open", label_names=["US-3"],
                    state={"id": "s-prog", "name": "In Progress",
                           "type": "started", "position": 2},
                    cycle={"id": "cy-2", "number": 2})
    a = make_adapter(tmp_path)
    seed_cycles(a, _cycle("cy-1", 1, [already]), _cycle("cy-2", 2))

    assert a.move_incomplete_to_next(1, 2) == []
    assert a.transport.updated_issues == []


@pytest.mark.unit
def test_list_sprints_is_constant_call_count_not_per_cycle(tmp_path):
    """Two flat calls regardless of cycle count: cycle scalars, then one
    filtered issues query. Copying Jira's per-sprint N+1 would burn the
    1500 req/hour budget, and a nested cycles{issues} connection would blow
    Linear's 10,000-point complexity ceiling."""
    a = make_adapter(tmp_path)
    seed_cycles(a, *[
        _cycle(f"cy-{n}", n, [issue(id=f"i{n}", label_names=[f"US-{n}"])])
        for n in range(1, 21)
    ])

    sprints = a.list_sprints()

    assert len(sprints) == 20
    assert a.transport.api_calls == 2, "must not scale with cycle count"


@pytest.mark.unit
def test_velocity_data_reuses_the_same_single_call(tmp_path):
    a = make_adapter(tmp_path)
    done = issue(id="i1", label_names=["US-1"],
                 state={"id": "s-done", "name": "Done",
                        "type": "completed", "position": 4})
    todo = issue(id="i2", label_names=["US-2"])
    seed_cycles(a, _cycle("cy-1", 1, [done, todo]))

    data = a.get_velocity_data()

    assert data == [{"sprint": 1, "planned": 2, "completed": 1}]
    assert a.transport.api_calls == 2, "must not scale with cycle count"


@pytest.mark.unit
def test_get_sprint_backlog_reads_issues_off_the_cycle(tmp_path):
    a = make_adapter(tmp_path)
    seed_cycles(a, _cycle("cy-1", 1, [issue(label_names=["US-5"])]))
    assert [s.id for s in a.get_sprint_backlog(1)] == ["US-5"]


@pytest.mark.unit
def test_remove_from_sprint_nulls_the_cycle(tmp_path):
    a = make_adapter(tmp_path, id_map={"US-1": {"linear_id": "i-uuid"}})
    a.get_story = lambda sid: None
    a.remove_from_sprint("US-1", 1)
    assert a.transport.updated_issues == [("i-uuid", {"cycleId": None})]


# ---------------------------------------------------------------------------
# Acceptance criteria
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_update_acceptance_criteria_rewrites_the_checkbox(tmp_path):
    """Implementable on Linear (raw Markdown) where Jira punts on it."""
    a = make_adapter(tmp_path, id_map={"US-1": {"linear_id": "i-uuid"}})
    a.transport.issues_by_id["i-uuid"] = issue(
        description="> synaptory: US-1\n\n- [ ] AC-1: logs in\n- [ ] AC-2: fails shut\n",
    )

    ac = a.update_acceptance_criteria("US-1", "AC-1", True)

    _, payload = a.transport.updated_issues[0]
    assert "- [x] AC-1: logs in" in payload["description"]
    assert "- [ ] AC-2: fails shut" in payload["description"]
    assert ac.met is True


# ---------------------------------------------------------------------------
# Local cache
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sync_bootstraps_cache_on_a_fresh_workspace(tmp_path):
    """#108 — `sync` must not demand `migrate` on a fresh project."""
    a = make_adapter(tmp_path)
    a.list_stories = lambda: []

    stats = a.sync_to_local_cache()

    assert stats["errors"] == []
    assert a._local_cache_path.exists()
    assert json.loads(a._local_cache_path.read_text())["stories"] == []


# ---------------------------------------------------------------------------
# PR #191 review findings — regressions
# ---------------------------------------------------------------------------


def _spec(spec_id, ftype, value):
    from tracker.config import SpecConfig, SpecFilter, LinearConfig
    return SpecConfig(id=spec_id, linear=LinearConfig(team_key="ENG"),
                      filter=SpecFilter(type=ftype, value=value))


@pytest.mark.unit
def test_cycle_reads_are_scoped_to_the_active_spec(tmp_path):
    """Finding 2: cycle-backed reads bypassed the spec filter entirely, so a
    spec-alpha sprint returned spec-beta's stories."""
    a = make_adapter(tmp_path, spec=_spec("alpha", "label", "spec-alpha"))
    seed_cycles(a, _cycle("cy-1", 1, [issue(id="i1", label_names=["US-1"])]))

    a.get_sprint_backlog(1)

    # The issues query for cycle contents must carry the spec label.
    cycle_queries = [s for s in a.transport.searches if "cycle" in s]
    assert cycle_queries, "no cycle-scoped issues query was issued"
    assert "spec-alpha" in json.dumps(cycle_queries[0]), (
        f"cycle read is not spec-scoped: {cycle_queries[0]}"
    )


@pytest.mark.unit
def test_sprint_backlog_excludes_epics(tmp_path):
    """Finding 2: an epic is a container, not sprint work — it only looks like
    an issue because Linear has no separate epic entity."""
    story = issue(id="i1", label_names=["US-1", "hc:story"])
    epic = issue(id="i2", label_names=["EPIC-001", "hc:epic"])
    a = make_adapter(tmp_path)
    seed_cycles(a, _cycle("cy-1", 1, [story, epic]))

    assert [s.id for s in a.get_sprint_backlog(1)] == ["US-1"]


@pytest.mark.unit
def test_move_incomplete_cannot_touch_another_specs_issues(tmp_path):
    """Finding 2: rollover iterated the same unfiltered collection, so it
    could reassign issues belonging to a different spec."""
    a = make_adapter(tmp_path, spec=_spec("alpha", "label", "spec-alpha"))
    seed_cycles(a, _cycle("cy-1", 1), _cycle("cy-2", 2))

    a.move_incomplete_to_next(1, 2)

    cycle_queries = [s for s in a.transport.searches if "cycle" in s]
    assert cycle_queries
    assert "spec-alpha" in json.dumps(cycle_queries[0])


@pytest.mark.unit
def test_create_sprint_binds_first_sprint_on_an_offset_team(tmp_path):
    """Finding 3: with an empty ID map and a team at cycle 47, Sprint 1 could
    never be bound — _resolve_cycle missed and create_sprint raised before
    _save_sprint_mapping was reachable. No pre-seeded ID map here."""
    a = make_adapter(tmp_path, manage_cycles=False)
    seed_cycles(a, _cycle("cy-47", 47), _cycle("cy-48", 48))

    info = a.create_sprint(SprintInfoT(number=1, goal="Ship auth"))

    assert info.number == 1
    saved = json.loads(a._id_map_path.read_text(encoding="utf-8"))
    assert saved["SPRINT-1"]["linear_id"] == "cy-47"
    assert saved["SPRINT-1"]["linear_number"] == 47
    assert a.transport.created_cycles == [], "must bind, not create"


@pytest.mark.unit
def test_successive_sprints_take_successive_cycles(tmp_path):
    a = make_adapter(tmp_path)
    seed_cycles(a, _cycle("cy-47", 47), _cycle("cy-48", 48))

    a.create_sprint(SprintInfoT(number=1, goal="one"))
    a.create_sprint(SprintInfoT(number=2, goal="two"))

    saved = json.loads(a._id_map_path.read_text(encoding="utf-8"))
    assert saved["SPRINT-1"]["linear_id"] == "cy-47"
    assert saved["SPRINT-2"]["linear_id"] == "cy-48"


@pytest.mark.unit
def test_create_sprint_skips_completed_cycles_when_binding(tmp_path):
    a = make_adapter(tmp_path)
    seed_cycles(a,
                _cycle("cy-47", 47, completedAt="2026-01-01T00:00:00Z"),
                _cycle("cy-48", 48))

    a.create_sprint(SprintInfoT(number=1, goal="one"))

    saved = json.loads(a._id_map_path.read_text(encoding="utf-8"))
    assert saved["SPRINT-1"]["linear_id"] == "cy-48"


@pytest.mark.unit
def test_create_sprint_honors_explicit_tracker_id(tmp_path):
    a = make_adapter(tmp_path)
    seed_cycles(a, _cycle("cy-47", 47), _cycle("cy-48", 48))

    a.create_sprint(SprintInfoT(number=1, goal="one", tracker_id="cy-48"))

    saved = json.loads(a._id_map_path.read_text(encoding="utf-8"))
    assert saved["SPRINT-1"]["linear_id"] == "cy-48"


@pytest.mark.unit
def test_create_sprint_rejects_unknown_tracker_id(tmp_path):
    from tracker.base import AdapterError
    a = make_adapter(tmp_path)
    seed_cycles(a, _cycle("cy-47", 47))

    with pytest.raises(AdapterError, match="matches no cycle"):
        a.create_sprint(SprintInfoT(number=1, goal="x", tracker_id="cy-999"))


@pytest.mark.unit
def test_cycle_create_always_sends_both_dates(tmp_path):
    """Finding 4: CycleCreateInput.startsAt/endsAt are non-null in the live
    schema, so omitting them fails GraphQL validation."""
    a = make_adapter(tmp_path, manage_cycles=True)
    seed_cycles(a)  # nothing to bind to -> must create

    a.create_sprint(SprintInfoT(number=1, goal="Ship auth"))

    sent = a.transport.created_cycles[0]
    assert sent["startsAt"] and sent["endsAt"], sent
    assert sent["startsAt"] < sent["endsAt"]


@pytest.mark.unit
def test_cycle_create_uses_supplied_dates(tmp_path):
    a = make_adapter(tmp_path, manage_cycles=True)
    seed_cycles(a)

    a.create_sprint(SprintInfoT(number=1, goal="x",
                                dates="2026-08-01 to 2026-08-15"))

    sent = a.transport.created_cycles[0]
    assert sent["startsAt"].startswith("2026-08-01")
    assert sent["endsAt"].startswith("2026-08-15")


@pytest.mark.unit
def test_cycle_create_window_follows_the_last_cycle(tmp_path):
    """A derived window must start after the latest in-flight cycle — Linear
    rejects overlapping cycles. Dates are relative to now so the test does not
    rot; a cycle that already ended must not push the window backwards."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    running_end = (now + timedelta(days=10)).isoformat().replace("+00:00", "Z")
    stale_end = (now - timedelta(days=90)).isoformat().replace("+00:00", "Z")

    a = make_adapter(tmp_path, manage_cycles=True)
    seed_cycles(
        a,
        _cycle("cy-0", 0, endsAt=stale_end),      # long finished — ignored
        _cycle("cy-1", 1, endsAt=running_end),    # in flight — must clear it
    )
    a._id_map = {
        "SPRINT-0": {"linear_id": "cy-0", "linear_number": 0},
        "SPRINT-1": {"linear_id": "cy-1", "linear_number": 1},
    }

    a.create_sprint(SprintInfoT(number=2, goal="next"))

    sent = a.transport.created_cycles[0]
    assert sent["startsAt"] == running_end, sent
    assert sent["endsAt"] > sent["startsAt"]


@pytest.mark.unit
def test_query_tickets_blocked_uses_the_label_axis(tmp_path):
    """Finding 5: BLOCKED resolves to no workflow state, so the whole status
    filter was dropped and the query returned every ticket."""
    from tracker.base import QueryFilter
    a = make_adapter(tmp_path)

    a.query_tickets(QueryFilter(status=["BLOCKED"]))

    sent = a.transport.searches[0]
    assert "blocked" in json.dumps(sent), f"status filter was dropped: {sent}"


@pytest.mark.unit
def test_query_tickets_mixed_state_and_label_statuses(tmp_path):
    from tracker.base import QueryFilter
    a = make_adapter(tmp_path)

    a.query_tickets(QueryFilter(status=["IN_PROGRESS", "BLOCKED"]))

    sent = json.dumps(a.transport.searches[0])
    assert "blocked" in sent and "s-prog" in sent
    assert '"or"' in sent, "a multi-status request is a disjunction"


@pytest.mark.unit
def test_query_tickets_returns_empty_when_no_status_resolves(tmp_path, capsys):
    """Never silently widen: an unresolvable status has no tickets."""
    from tracker.base import QueryFilter
    a = make_adapter(tmp_path, states=[])

    out = a.query_tickets(QueryFilter(status=["IN_PROGRESS"]))

    assert out == []
    assert a.transport.searches == []
    assert "returning no tickets" in capsys.readouterr().err


@pytest.mark.unit
def test_blocked_issue_reads_back_as_blocked(tmp_path):
    """Finding 5: read-back returned IN_PROGRESS, so blocked metrics were
    always zero. teamwork_adapter and github_adapter both report BLOCKED."""
    a = make_adapter(tmp_path)
    i = issue(label_names=["US-1", "blocked"],
              state={"id": "s-prog", "name": "In Progress",
                     "type": "started", "position": 2})

    assert a._canonical_from_issue(i) == "BLOCKED"


@pytest.mark.unit
def test_blocked_metrics_are_non_zero(tmp_path):
    a = make_adapter(tmp_path)
    blocked = issue(id="i1", label_names=["US-1", "hc:story", "blocked"],
                    state={"id": "s-prog", "name": "In Progress",
                           "type": "started", "position": 2})
    seed_cycles(a, _cycle("cy-1", 1, [blocked]))

    assert a.get_sprint_metrics(1).blocked == 1


@pytest.mark.unit
def test_done_issue_ignores_a_stale_blocked_label(tmp_path):
    """A finished issue is DONE regardless of a leftover marker."""
    a = make_adapter(tmp_path)
    i = issue(label_names=["US-1", "blocked"],
              state={"id": "s-done", "name": "Done",
                     "type": "completed", "position": 4})

    assert a._canonical_from_issue(i) == "DONE"


@pytest.mark.unit
def test_project_spec_write_resolves_name_to_uuid(tmp_path):
    """Finding 6: reads translate a project name correctly, but writes copied
    the raw name into IssueCreateInput.projectId."""
    a = make_adapter(tmp_path, spec=_spec("beta", "project", "Mobile Q3"))
    a.transport.project_ids = {"Mobile Q3": "proj-uuid"}

    _, extra = a._apply_spec_write_side_effect(labels=["US-1"])

    assert extra["projectId"] == "proj-uuid"
    assert a.transport.resolved_projects == ["Mobile Q3"]


@pytest.mark.unit
def test_create_ticket_sends_resolved_project_uuid(tmp_path):
    a = make_adapter(tmp_path, spec=_spec("beta", "project", "Mobile Q3"))
    a.transport.project_ids = {"Mobile Q3": "proj-uuid"}

    a.create_ticket(StoryT(id="US-1", title="X"))

    assert a.transport.created_issues[0]["projectId"] == "proj-uuid"


# ---------------------------------------------------------------------------
# PR #191 re-review — second round
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_first_binding_ignores_completed_historical_cycle(tmp_path):
    """Re-review finding: a team at cycle 47 whose cycle 1 completed long ago.
    Going through _resolve_cycle matched the dead cycle 1 by number, so
    Sprint 1 bound to history instead of the live cycle."""
    a = make_adapter(tmp_path)
    seed_cycles(a,
                _cycle("cy-1", 1, completedAt="2025-03-01T00:00:00Z"),
                _cycle("cy-47", 47))

    a.create_sprint(SprintInfoT(number=1, goal="Ship auth"))

    saved = json.loads(a._id_map_path.read_text(encoding="utf-8"))
    assert saved["SPRINT-1"]["linear_id"] == "cy-47", (
        "bound to a completed historical cycle instead of the live one"
    )


@pytest.mark.unit
def test_explicit_tracker_id_wins_over_a_numeric_match(tmp_path):
    """Re-review finding: once the numeric match succeeded, an explicit
    tracker_id never got a say."""
    a = make_adapter(tmp_path)
    seed_cycles(a, _cycle("cy-1", 1), _cycle("cy-47", 47))

    a.create_sprint(SprintInfoT(number=1, goal="x", tracker_id="cy-47"))

    saved = json.loads(a._id_map_path.read_text(encoding="utf-8"))
    assert saved["SPRINT-1"]["linear_id"] == "cy-47"


@pytest.mark.unit
def test_numeric_match_is_skipped_when_that_cycle_is_already_bound(tmp_path):
    a = make_adapter(tmp_path, id_map={
        "SPRINT-9": {"linear_id": "cy-1", "linear_number": 1},
    })
    seed_cycles(a, _cycle("cy-1", 1), _cycle("cy-2", 2))

    a.create_sprint(SprintInfoT(number=1, goal="x"))

    saved = json.loads(a._id_map_path.read_text(encoding="utf-8"))
    assert saved["SPRINT-1"]["linear_id"] == "cy-2", "stole an already-bound cycle"


@pytest.mark.unit
def test_id_map_stays_authoritative_over_a_numeric_match(tmp_path):
    a = make_adapter(tmp_path, id_map={
        "SPRINT-1": {"linear_id": "cy-47", "linear_number": 47},
    })
    seed_cycles(a, _cycle("cy-1", 1), _cycle("cy-47", 47))

    a.create_sprint(SprintInfoT(number=1, goal="x"))

    saved = json.loads(a._id_map_path.read_text(encoding="utf-8"))
    assert saved["SPRINT-1"]["linear_id"] == "cy-47"


@pytest.mark.unit
def test_numeric_match_still_works_on_a_fresh_team(tmp_path):
    """The fallback must not regress the ordinary fresh-team case."""
    a = make_adapter(tmp_path)
    seed_cycles(a, _cycle("cy-1", 1), _cycle("cy-2", 2))

    a.create_sprint(SprintInfoT(number=2, goal="x"))

    saved = json.loads(a._id_map_path.read_text(encoding="utf-8"))
    assert saved["SPRINT-2"]["linear_id"] == "cy-2"


@pytest.mark.unit
def test_cycle_issue_query_budget_scales_with_cycle_count(tmp_path):
    """Re-review finding: search_issues defaults to 200 rows, but this one
    call covers every cycle — a fixed cap silently drops older sprints."""
    a = make_adapter(tmp_path)
    seed_cycles(a, *[_cycle(f"cy-{n}", n) for n in range(1, 11)])

    a.list_sprints()

    assert a.transport.max_results_seen[0] >= 10 * 250, (
        f"cap {a.transport.max_results_seen[0]} does not scale with 10 cycles"
    )


@pytest.mark.unit
def test_more_than_200_cycle_issues_are_not_truncated(tmp_path):
    """The 200-row default would empty older sprint backlogs and undercount
    velocity. 260 issues spread across two cycles must all come back."""
    per_cycle = 130
    c1 = _cycle("cy-1", 1, [
        issue(id=f"a{n}", label_names=[f"US-a{n}", "hc:story"])
        for n in range(per_cycle)
    ])
    c2 = _cycle("cy-2", 2, [
        issue(id=f"b{n}", label_names=[f"US-b{n}", "hc:story"])
        for n in range(per_cycle)
    ])
    a = make_adapter(tmp_path)
    seed_cycles(a, c1, c2)

    assert len(a.get_sprint_backlog(1)) == per_cycle
    assert len(a.get_sprint_backlog(2)) == per_cycle
    assert a.get_sprint_metrics(2).planned == per_cycle
    assert [d["planned"] for d in a.get_velocity_data()] == [per_cycle, per_cycle]
