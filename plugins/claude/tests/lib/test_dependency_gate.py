"""Layer 1 — the `depends_on` dispatch gate (issue #304).

`_story_deps_met` was written to fail closed on unknown dependency ids, but its
only call site was inside `_parallel_batch`, and `_attach_parallel_batch`
returns early unless `parallelism.story_parallelism` is on -- which defaults
OFF. So on a default-configured project the dependency check never executed at
all, in any lifecycle, and `queued -> dispatch_se` started work whose declared
upstream was not done.

These tests pin the three properties that fix has to hold:

1. a dep-met candidate further down the queue is dispatched instead of the
   blocked FIFO head (skip-and-scan, never starve),
2. `deps_blocked` is reached only when nothing at all is dispatchable, and
3. every unresolvable state fails CLOSED with a reason code the operator can
   act on -- waiting on a sibling, a cut upstream, a typo, and an unpushed
   workstream are four different problems.
"""

from __future__ import annotations

import pytest

from story_pipeline import (
    CONTINUE_ELIGIBLE,
    DEP_CANCELLED,
    DEP_CONDITION_UNVERIFIED,
    DEP_EXTERNAL,
    DEP_INCOMPLETE,
    DEP_LEDGER_STALE,
    DEP_OTHER_WORKSTREAM,
    DEP_OUT_OF_CYCLE,
    DEP_STALE_MANIFEST,
    DEP_UNKNOWN,
    NEXT_ACTIONS,
    _parallel_batch,
    _story_deps_met,
    dependency_gate_mode,
    next_action,
    normalize_dep,
    story_dep_status,
)


def _story(sid: str, state: str, **kw) -> dict:
    return {"id": sid, "title": f"Unit {sid}", "state": state, **kw}


def _state(*stories: dict, **kw) -> dict:
    return {
        "version": "2.0",
        "build_mode": "spq",
        "lifecycle_state": "CYCLE_EXECUTION",
        "current_cycle": 7,
        "current_stories": list(stories),
        **kw,
    }


def _codes(result: dict) -> list[str]:
    return [u["reason_code"] for u in result["unmet"]]


# ── the serial path (the actual #304 defect) ────────────────────────────────


def test_serial_queued_skips_dep_unmet_and_dispatches_next_candidate():
    """The blocked unit must not starve a dispatchable one behind it."""
    out = next_action(
        _state(
            _story("WU-02", "queued", depends_on=["WU-01"]),
            _story("WU-01", "queued"),
        )
    )
    assert out["action"] == "dispatch_se"
    assert out["story_id"] == "WU-01", "dispatched the blocked FIFO head"


def test_queued_fifo_order_preserved_among_dep_met_candidates():
    out = next_action(_state(_story("WU-05", "queued"), _story("WU-06", "queued")))
    assert out["story_id"] == "WU-05"


def test_all_queued_dep_blocked_returns_deps_blocked():
    out = next_action(_state(_story("WU-02", "queued", depends_on=["WU-01"])))
    assert out["action"] == "deps_blocked"
    assert out["human_gate_pending"] is True
    assert [h["story_id"] for h in out["dependencies_held"]] == ["WU-02"]


def test_deps_blocked_reports_every_held_unit_and_reason_code():
    out = next_action(
        _state(
            _story("WU-02", "queued", depends_on=["WU-99"]),
            _story("WU-03", "queued", depends_on=["WU-98"]),
        )
    )
    assert out["action"] == "deps_blocked"
    held = {h["story_id"]: h for h in out["dependencies_held"]}
    assert set(held) == {"WU-02", "WU-03"}
    for entry in held.values():
        assert entry["unmet"], "a held unit must say which edge held it"
        assert entry["unmet"][0]["reason_code"] == DEP_UNKNOWN
    # The reason has to be actionable on its own: an operator reading only the
    # loop transcript never sees `dependencies_held`.
    assert "WU-99" in out["reason"] or "WU-98" in out["reason"]


def test_deps_blocked_never_returned_when_another_stage_has_work():
    out = next_action(
        _state(
            _story("WU-02", "queued", depends_on=["WU-01"]),
            _story("WU-01", "testing"),
        )
    )
    assert out["action"] == "dispatch_qe"
    assert out["story_id"] == "WU-01"


def test_awaiting_acceptance_preempts_deps_blocked():
    """Accepting a unit is precisely what unblocks its dependents."""
    out = next_action(
        _state(
            _story("WU-02", "queued", depends_on=["WU-01"]),
            _story("WU-01", "awaiting_acceptance"),
        )
    )
    assert out["action"] == "await_acceptance"


def test_blocked_recovery_preempts_deps_blocked():
    out = next_action(
        _state(
            _story("WU-02", "queued", depends_on=["WU-01"]),
            _story("WU-01", "blocked", blocked_from="in_progress"),
        )
    )
    assert out["action"] in ("recover_blocked", "all_blocked")


def test_in_progress_and_reviewing_stages_are_not_dep_gated():
    """Retro-blocking started work would strand it, and gating `reviewing`
    would deadlock `reviewing -> done` behind a merge agents may not perform."""
    # `reviewing` dispatches CR rather than promoting, because Cycle 7 computes
    # the `mature` tier and `code_reviewed` is required there. The point of this
    # test is only that the stage is reached at all, not which role it picks.
    for stage, expected in (
        ("in_progress", "dispatch_se"),
        ("testing", "dispatch_qe"),
        ("reviewing", "dispatch_cr"),
    ):
        out = next_action(_state(_story("WU-01", stage, depends_on=["WU-MISSING"])))
        assert out["action"] == expected, stage
        assert out["story_id"] == "WU-01"


def test_met_deps_dispatch_normally():
    out = next_action(
        _state(
            _story("WU-01", "done"),
            _story("WU-02", "queued", depends_on=["WU-01"]),
        )
    )
    assert out["action"] == "dispatch_se"
    assert out["story_id"] == "WU-02"


# ── reason codes: each one names a different operator action ────────────────


def test_unknown_dep_id_fails_closed_as_dep_unknown():
    result = story_dep_status(_state(), _story("WU-02", "queued", depends_on=["typo"]))
    assert result["met"] is False
    assert _codes(result) == [DEP_UNKNOWN]


def test_cancelled_dep_reports_dep_cancelled_not_dep_incomplete():
    """A cut upstream can NEVER satisfy the edge, so waiting is the wrong
    advice. Before this the board reported `dep_incomplete` and stalled
    forever with no indication that no amount of waiting would help."""
    state = _state(
        _story("WU-01", "cancelled"),
        _story("WU-02", "queued", depends_on=["WU-01"]),
    )
    result = story_dep_status(state, state["current_stories"][1])
    assert _codes(result) == [DEP_CANCELLED]
    assert "re-admit" in result["unmet"][0]["detail"]


def test_incomplete_dep_names_the_current_substate():
    state = _state(
        _story("WU-01", "testing"),
        _story("WU-02", "queued", depends_on=["WU-01"]),
    )
    result = story_dep_status(state, state["current_stories"][1])
    assert _codes(result) == [DEP_INCOMPLETE]
    assert "testing" in result["unmet"][0]["detail"]


def test_dep_from_another_cycle_cannot_satisfy_the_edge():
    """#304 AC: a matching Work Unit id from another Cycle must not satisfy."""
    state = _state(
        _story("WU-01", "done", cycle_id="3"),
        _story("WU-02", "queued", depends_on=["WU-01"]),
    )
    result = story_dep_status(
        state, state["current_stories"][1], dep_context={"cycle_id": "7"}
    )
    assert result["met"] is False
    assert _codes(result) == [DEP_OUT_OF_CYCLE]


def test_same_cycle_dep_still_satisfies():
    state = _state(
        _story("WU-01", "done", cycle_id="7"),
        _story("WU-02", "queued", depends_on=["WU-01"]),
    )
    result = story_dep_status(
        state, state["current_stories"][1], dep_context={"cycle_id": "7"}
    )
    assert result["met"] is True


def test_cross_workstream_dep_without_manifest_is_explicitly_blocked():
    """#304: 'until that shared ledger exists, an unknown dependency must fail
    closed rather than dispatch'. The detail must name the cause so the
    operator is not left thinking they typo'd an id."""
    result = story_dep_status(
        _state(), _story("WU-02", "queued", depends_on=["WU-UPSTREAM"])
    )
    assert _codes(result) == [DEP_UNKNOWN]
    assert "another workstream" in result["unmet"][0]["detail"]
    assert "#303" in result["unmet"][0]["detail"]


def test_cross_workstream_dep_with_manifest_names_the_owner():
    ctx = {
        "cycle_id": "7",
        "workstream_id": "frame",
        "manifest_present": True,
        "admitted": ["WU-01", "WU-02"],
        "owners": {"WU-01": "spine", "WU-02": "frame"},
        "satisfied": {},
    }
    result = story_dep_status(
        _state(), _story("WU-02", "queued", depends_on=["WU-01"]), dep_context=ctx
    )
    assert _codes(result) == [DEP_OTHER_WORKSTREAM]
    assert result["unmet"][0]["owner_workstream"] == "spine"


def test_dep_not_in_the_admitted_set_is_dep_unknown_even_with_a_manifest():
    ctx = {
        "cycle_id": "7",
        "workstream_id": "frame",
        "manifest_present": True,
        "admitted": ["WU-02"],
        "owners": {},
        "satisfied": {},
    }
    result = story_dep_status(
        _state(), _story("WU-02", "queued", depends_on=["WU-77"]), dep_context=ctx
    )
    assert _codes(result) == [DEP_UNKNOWN]


def test_a_satisfying_event_survives_a_stale_ledger():
    """Freshness is checked AFTER satisfaction, not before.

    An event is an append-only FACT, so its age does not invalidate it: a stale
    cache can only be MISSING newer events, and "not satisfied" is already the
    fail-closed direction. This test asserted the opposite when `dep_context`
    was still a stub with no real ledger behind it; blocking here would strand
    a workstream whose upstream genuinely did integrate.
    """
    ctx = {
        "cycle_id": "7",
        "workstream_id": "frame",
        "manifest_present": True,
        "admitted": ["WU-01"],
        "owners": {"WU-01": "spine"},
        "satisfied": {"WU-01": {"condition": "done", "verified": True}},
        "ledger_fresh": False,
    }
    result = story_dep_status(
        _state(), _story("WU-02", "queued", depends_on=["WU-01"]), dep_context=ctx
    )
    assert result["met"] is True


def test_a_stale_ledger_with_no_event_still_names_the_owner():
    """When the edge is NOT satisfied and the cache is behind, the operator
    needs both facts: who owns the upstream, and that a refresh might change
    the answer."""
    ctx = {
        "cycle_id": "7",
        "workstream_id": "frame",
        "manifest_present": True,
        "admitted": ["WU-01"],
        "owners": {"WU-01": "spine"},
        "satisfied": {},
        "ledger_fresh": False,
    }
    result = story_dep_status(
        _state(), _story("WU-02", "queued", depends_on=["WU-01"]), dep_context=ctx
    )
    assert _codes(result) == [DEP_LEDGER_STALE]
    assert result["unmet"][0]["owner_workstream"] == "spine"
    assert "refresh_ledger" in result["unmet"][0]["detail"]


def test_stale_manifest_hash_short_circuits_every_dep():
    """One reason, not one per edge: a projection on a superseded manifest
    cannot be trusted about ANY edge, and re-hydration is the single fix."""
    story = _story("WU-03", "queued", depends_on=["WU-01", "WU-02"], manifest_hash="old")
    result = story_dep_status(
        _state(), story, dep_context={"manifest_hash": "new", "cycle_id": "7"}
    )
    assert _codes(result) == [DEP_STALE_MANIFEST, DEP_STALE_MANIFEST]
    assert "re-hydrate" in result["unmet"][0]["detail"]


def test_state_level_stale_manifest_blocks_a_story_with_no_old_edges():
    """The hydrated projection hash lives on state. A newly added dependency
    is absent from the stale story, but the projection must still fail closed."""
    story = _story("WU-03", "queued", depends_on=[])
    result = story_dep_status(
        _state(),
        story,
        dep_context={
            "story_manifest_hash": "old",
            "manifest_hash": "new",
            "cycle_id": "7",
        },
    )
    assert result["met"] is False
    assert _codes(result) == [DEP_STALE_MANIFEST]
    assert result["unmet"][0]["dep"]["condition"] == "manifest_current"


def test_projection_hash_with_no_readable_current_manifest_fails_closed():
    result = story_dep_status(
        _state(),
        _story("WU-03", "queued", depends_on=[]),
        dep_context={"story_manifest_hash": "old", "manifest_hash": None},
    )
    assert result["met"] is False
    assert _codes(result) == [DEP_STALE_MANIFEST]
    assert "<unavailable>" in result["unmet"][0]["detail"]


def test_external_cross_cycle_dep_is_dep_external_not_dep_unknown():
    """A cross-Cycle edge is owned by a Coordination Cycle. Reporting it as
    `dep_unknown` would send the operator hunting for a typo that does not exist.

    With no coordination context this still HOLDS -- #305 made these resolvable,
    not automatic. A Cycle that is not part of a release has nothing that could
    satisfy such an edge, and the detail says so rather than implying a bug.
    """
    story = _story(
        "WU-02",
        "queued",
        depends_on=[{"external": {"cycle_id": "3-abc", "output": "contract:billing.v2"}}],
    )
    result = story_dep_status(_state(), story)
    assert _codes(result) == [DEP_EXTERNAL]
    detail = result["unmet"][0]["detail"]
    assert "Coordination Cycle" in detail
    assert "none was resolved from this clone" in detail
    # The hold must say what to DO. "This Cycle is not part of a Coordination
    # Cycle" read as a fact when it was really a failure to discover one, and
    # sent an operator looking for the wrong problem.
    assert "coordination-cycles" in detail


def test_cycle_integrated_condition_is_external_even_without_the_external_key():
    story = _story(
        "WU-02", "queued",
        depends_on=[{"unit_id": "WU-01", "condition": "cycle_integrated"}],
    )
    result = story_dep_status(_state(), story)
    assert _codes(result) == [DEP_EXTERNAL]


def test_stronger_condition_on_a_local_unit_needs_a_ledger_event():
    """`done` is a board fact; `contract_published` is a ledger fact. A done
    story does not prove its contract was published at a pinned digest."""
    state = _state(
        _story("WU-01", "done"),
        _story(
            "WU-02", "queued",
            depends_on=[{"unit_id": "WU-01", "condition": "contract_published"}],
        ),
    )
    result = story_dep_status(state, state["current_stories"][1])
    assert _codes(result) == [DEP_CONDITION_UNVERIFIED]


def test_unverified_event_refused_by_default_and_accepted_when_configured():
    ctx = {
        "cycle_id": "7",
        "workstream_id": "frame",
        "manifest_present": True,
        "admitted": ["WU-01"],
        "owners": {"WU-01": "spine"},
        # The recorded condition matters now that the strength comparison is
        # real: an event must meet-or-exceed the DECLARED condition before its
        # verification status is even considered.
        "satisfied": {"WU-01": {"condition": "done", "verified": False}},
    }
    story = _story("WU-02", "queued", depends_on=["WU-01"])
    assert _codes(story_dep_status(_state(), story, dep_context=ctx)) == [
        DEP_CONDITION_UNVERIFIED
    ]
    ctx_open = dict(ctx, accept_unverified=True)
    assert story_dep_status(_state(), story, dep_context=ctx_open)["met"] is True


def test_dep_entry_naming_no_unit_is_dep_unknown():
    story = _story("WU-02", "queued", depends_on=[{"condition": "done"}])
    assert _codes(story_dep_status(_state(), story)) == [DEP_UNKNOWN]


# ── the legacy contract must not shift ──────────────────────────────────────


def test_legacy_string_depends_on_still_means_condition_done():
    assert normalize_dep("WU-7") == {
        "unit_id": "WU-7",
        "condition": "done",
        "external": None,
    }


def test_story_deps_met_wrapper_is_behaviour_preserving():
    """The boolean face still exists so `_parallel_batch`'s historical call
    site and issue #134's tests are untouched."""
    state = _state(
        _story("WU-01", "done"),
        _story("WU-02", "queued", depends_on=["WU-01"]),
        _story("WU-03", "queued", depends_on=["nope"]),
    )
    assert _story_deps_met(state, state["current_stories"][1]) is True
    assert _story_deps_met(state, state["current_stories"][2]) is False


def test_no_depends_on_is_always_met():
    assert story_dep_status(_state(), _story("WU-01", "queued"))["met"] is True


# ── parallel batch: the LEAD was never checked ──────────────────────────────


def test_parallel_batch_refuses_a_dep_blocked_lead():
    """Only additional members were gated; `batch[0]` never was."""
    state = _state(
        _story("WU-02", "queued", depends_on=["WU-01"]),
        _story("WU-01", "queued"),
    )
    serial = {"action": "dispatch_se", "role": "se", "story_id": "WU-02"}
    block = _parallel_batch(
        state, None, serial, {"story_parallelism": True, "max_concurrent": 3}
    )
    assert block is not None, "returning None would hide the condition"
    assert block["eligible"] is False
    assert block["lead_dependencies_held"]
    # batch[0] must stay the serial action so an orchestrator that ignores the
    # block behaves exactly as before.
    assert [m["story_id"] for m in block["batch"]] == ["WU-02"]


def test_parallel_batch_still_holds_back_dep_unmet_members_with_a_reason():
    state = _state(
        _story("WU-01", "queued"),
        _story("WU-02", "queued", depends_on=["WU-99"]),
    )
    serial = {"action": "dispatch_se", "role": "se", "story_id": "WU-01"}
    block = _parallel_batch(
        state, None, serial, {"story_parallelism": True, "max_concurrent": 3}
    )
    assert [m["story_id"] for m in block["batch"]] == ["WU-01"]
    assert "WU-02" in block["reason"]
    assert "unmet depends_on" in block["reason"]


# ── the action contract ─────────────────────────────────────────────────────


def test_deps_blocked_is_a_declared_stop_action():
    assert "deps_blocked" in NEXT_ACTIONS
    assert "deps_blocked" not in CONTINUE_ELIGIBLE, (
        "next_action is pure, so a continue-eligible deps_blocked would re-drive "
        "to an identical digest until the loop guard tripped"
    )


# ── the enforcement valve ───────────────────────────────────────────────────


def test_warn_mode_dispatches_and_annotates(tmp_path):
    out = next_action(
        _state(_story("WU-02", "queued", depends_on=["WU-01"])),
        dependency_gate="warn",
    )
    assert out["action"] == "dispatch_se"
    assert out["story_id"] == "WU-02"
    assert out["deps_warning"], "warn mode must still surface the unmet edges"
    assert "UNMET" in out["reason"]


def test_dependency_gate_mode_defaults_to_enforce(tmp_path):
    assert dependency_gate_mode(tmp_path) == "enforce"
    (tmp_path / ".synaptory.yaml").write_text(
        "build_mode: spq\nresilience:\n  dependency_gate: warn\n", encoding="utf-8"
    )
    assert dependency_gate_mode(tmp_path) == "warn"


@pytest.mark.parametrize("value", ["enforce", "", "nonsense", "true"])
def test_dependency_gate_mode_only_warn_disables(tmp_path, value):
    """Anything other than an explicit warn spelling enforces. A typo must not
    silently disable a correctness gate."""
    (tmp_path / ".synaptory.yaml").write_text(
        f"build_mode: spq\nresilience:\n  dependency_gate: {value}\n", encoding="utf-8"
    )
    assert dependency_gate_mode(tmp_path) == "enforce"
