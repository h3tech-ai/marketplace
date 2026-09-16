"""Layer 1 — the ORCHESTRATOR's stated DoD contract equals the enforced one.

#501 fixed the hook path (`render_envelope` → `dispatch_dod_contract`). This
pins the other path, and it is the one every modes file drives: the
orchestrator reads `next_action`, pastes `dod.active_checks` into the dispatch
prompt as the Evidence Contract, and the agent produces evidence for exactly
what that list names.

`next_action` itself is a pure function of the board, so the block it builds
can only carry the STATIC tier list (`DOD_TIER_CHECKS`). The four CONDITIONAL
gates — `ui_acceptance`, `runtime_verified`, `no_critical_findings`,
`integration_verified` — are promoted from `.synaptory.yaml` plus the story
record, appear in no tier list, and ARE enforced by `evaluate_story_dod`. So a
prompt built from the raw block was systematically narrower than the gate for
exactly the stories that need it widest.

The property under test is equality, not containment: a stated set that is
too WIDE is its own failure (the agent produces evidence for a gate that will
never be checked, and learns the contract is noise).
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

_MATURE = {"tier": "mature", "decided_by": "po"}


def _project(
    tmp_path: Path,
    stories: list[dict],
    *,
    build_mode: str = "scrum",
    lifecycle: str = "SPRINT_EXECUTION",
    yaml: str | None = None,
    **top,
) -> Path:
    if yaml is not None:
        (tmp_path / ".synaptory.yaml").write_text(yaml, encoding="utf-8")
    orch = tmp_path / ".synaptory" / ".orchestrator"
    (orch / "receipts").mkdir(parents=True, exist_ok=True)
    state = {
        "version": "2.0",
        "build_mode": build_mode,
        "lifecycle_state": lifecycle,
        "current_sprint": 1,
        "cumulative_ticket_number": 1,
        "dod_tier": _MATURE,
        "current_stories": stories,
    }
    state.update(top)
    (orch / "pipeline-state.json").write_text(json.dumps(state), encoding="utf-8")
    return tmp_path


def _enforced_set(project: Path, story_id: str, tier: str) -> set[str]:
    """The checks `evaluate_story_dod` actually marks `required`."""
    result = sp.evaluate_story_dod(str(project), story_id, tier)
    return {
        cid
        for cid, check in (result.get("checks") or {}).items()
        if check.get("required")
    }


# ── the headline property ────────────────────────────────────────────────────

@pytest.mark.unit
def test_ui_bearing_story_stated_set_equals_enforced_set(tmp_path: Path):
    """A UI-bearing story's prompt contract must match the gate exactly.

    `ui_acceptance` is in NO tier list — it exists only as a promotion off the
    story's own record. Before this fix the orchestrator stated the four
    mature-tier checks and the gate enforced five, so the agent was blocked on
    a gate its prompt never named.
    """
    import scrum_state_machine as ssm

    project = _project(
        tmp_path,
        [{"id": "US-1", "title": "Checkout screen", "state": "queued",
          "ui_bearing": True}],
    )

    stated = set(ssm.next_action(str(project))["dod"]["active_checks"])
    enforced = _enforced_set(project, "US-1", "mature")

    assert "ui_acceptance" in stated, (
        "the promoted gate never reached the prompt — this is the #501 defect "
        "on the orchestrator path"
    )
    assert stated == enforced


@pytest.mark.unit
def test_non_ui_story_states_no_promotion_it_will_not_be_graded_on(tmp_path: Path):
    """Equality, not containment: an over-wide contract is also a failure."""
    import scrum_state_machine as ssm

    project = _project(
        tmp_path,
        [{"id": "US-2", "title": "Nightly rollup job", "state": "queued",
          "ui_bearing": False}],
    )

    stated = set(ssm.next_action(str(project))["dod"]["active_checks"])
    assert "ui_acceptance" not in stated
    assert stated == _enforced_set(project, "US-2", "mature")


@pytest.mark.unit
def test_kanban_wrapper_states_the_promoted_gate(tmp_path: Path):
    """The same property on the kanban lifecycle — one helper, every host loop."""
    import kanban_state_machine as ksm

    project = _project(
        tmp_path,
        [{"id": "T-1", "title": "Settings screen", "state": "queued",
          "ui_bearing": True}],
        build_mode="kanban",
        lifecycle="EXECUTION",
    )

    stated = set(ksm.next_action(str(project))["dod"]["active_checks"])
    assert "ui_acceptance" in stated
    assert stated == _enforced_set(project, "T-1", "mature")


@pytest.mark.unit
def test_full_verification_tier_promotes_runtime_verified(tmp_path: Path):
    """`verification_tier: full` promotes `runtime_verified` with no receipts.

    A state-derived promotion, so it is DETERMINED at dispatch — it belongs in
    `active_checks`, not in `undetermined_checks`.
    """
    import scrum_state_machine as ssm

    project = _project(
        tmp_path,
        [{"id": "US-3", "title": "Payment capture", "state": "queued"}],
        yaml="quality:\n  verification:\n    stories:\n      US-3: full\n",
    )

    dod = ssm.next_action(str(project))["dod"]
    assert "runtime_verified" in dod["active_checks"]
    assert set(dod["active_checks"]) == _enforced_set(project, "US-3", "mature")


# ── the honest reading of what is not yet knowable ───────────────────────────

@pytest.mark.unit
def test_receipt_driven_promotion_is_reported_as_undetermined(tmp_path: Path):
    """At SE dispatch the PHI verdict is unmeasured, not absent.

    Its evidence is the diff the agent has not written yet. Reporting only
    `active_checks` would render "not promoted yet" and "will never be
    promoted" as the same shorter list.
    """
    import scrum_state_machine as ssm

    project = _project(
        tmp_path,
        [{"id": "US-4", "title": "Patient chart export", "state": "queued"}],
        yaml="healthcare:\n  baa_enforced: true\n",
    )

    dod = ssm.next_action(str(project))["dod"]
    undetermined = {e["check"] for e in dod["undetermined_checks"]}

    assert "no_critical_findings" in undetermined
    assert "runtime_verified" in undetermined
    assert all(e.get("why") for e in dod["undetermined_checks"]), (
        "every undetermined check must say what would activate it"
    )


@pytest.mark.unit
def test_plain_project_does_not_report_the_phi_pair_as_undetermined(tmp_path: Path):
    """A non-BAA project can never promote the PHI pair — listing it is noise."""
    import scrum_state_machine as ssm

    project = _project(
        tmp_path, [{"id": "US-5", "title": "Rename a column", "state": "queued"}]
    )

    undetermined = {
        e["check"]
        for e in ssm.next_action(str(project))["dod"]["undetermined_checks"]
    }
    assert "no_critical_findings" not in undetermined
    assert "runtime_verified" not in undetermined
    assert undetermined == {"integration_verified"}


# ── coherence of the block's own fields ──────────────────────────────────────

@pytest.mark.unit
def test_declared_checks_stays_a_superset_of_active_checks(tmp_path: Path):
    """#487's `declared_checks` must not end up a stale SUBSET of `active`."""
    import scrum_state_machine as ssm

    project = _project(
        tmp_path,
        [{"id": "US-6", "title": "Profile screen", "state": "queued",
          "ui_bearing": True}],
    )

    dod = ssm.next_action(str(project))["dod"]
    assert set(dod["active_checks"]) <= set(dod["declared_checks"])


@pytest.mark.unit
def test_tier_is_pinned_to_the_one_next_action_routed_on(tmp_path: Path):
    """The contract may not require a CR the same output did not schedule.

    At `early` the tier carries no `code_reviewed`, so a contract that
    re-resolved its own tier could state one while `next_action` routed
    straight from testing to done.
    """
    import scrum_state_machine as ssm

    project = _project(
        tmp_path,
        [{"id": "US-7", "title": "Checkout screen", "state": "queued",
          "ui_bearing": True}],
        dod_tier={"tier": "early", "decided_by": "po"},
    )

    dod = ssm.next_action(str(project))["dod"]
    assert dod["tier"] == "early"
    assert dod["tier_source"] == "planned"
    assert "code_reviewed" not in dod["active_checks"]
    assert "ui_acceptance" in dod["active_checks"]


@pytest.mark.unit
def test_tier_source_computed_survives_the_upgrade(tmp_path: Path):
    """`tier_source: computed` is the silent-promotion tell the modes files act
    on — resolving the contract must not overwrite it with a bookkeeping value.
    """
    import scrum_state_machine as ssm

    project = _project(
        tmp_path,
        [{"id": "US-8", "title": "Checkout screen", "state": "queued",
          "ui_bearing": True}],
        dod_tier=None,
    )

    assert ssm.next_action(str(project))["dod"]["tier_source"] == "computed"


# ── parallel dispatch: N prompts, N stories, N contracts ─────────────────────

@pytest.mark.unit
def test_each_parallel_batch_member_carries_its_own_contract(tmp_path: Path):
    """A batch is N dispatches for N stories; `ui_bearing` is per story.

    One shared block would hand the lead's contract to every member, which is
    the same defect this file exists to close, re-opened one level down.
    """
    import scrum_state_machine as ssm

    project = _project(
        tmp_path,
        [
            {"id": "US-A", "title": "Rename a column", "state": "queued",
             "ui_bearing": False, "file_scope": ["db/"]},
            {"id": "US-B", "title": "Checkout screen", "state": "queued",
             "ui_bearing": True, "file_scope": ["web/"]},
        ],
        yaml=(
            "parallelism:\n"
            "  story_parallelism: enabled\n"
            "  isolation: shared\n"
            "  max_concurrent: 3\n"
        ),
    )

    out = ssm.next_action(str(project))
    parallel = out.get("parallel") or {}
    batch = parallel.get("batch") or []
    by_id = {e["story_id"]: e for e in batch if isinstance(e, dict)}

    assert {"US-A", "US-B"} <= set(by_id), (
        "fixture no longer produces a two-member batch: %r" % (parallel,)
    )
    assert "ui_acceptance" not in by_id["US-A"]["dod"]["active_checks"]
    assert "ui_acceptance" in by_id["US-B"]["dod"]["active_checks"]
    for sid, entry in by_id.items():
        assert set(entry["dod"]["active_checks"]) == _enforced_set(
            project, sid, "mature"
        )


# ── the helper's own failure posture ─────────────────────────────────────────

@pytest.mark.unit
def test_unresolvable_project_leaves_the_tier_block_intact(tmp_path: Path):
    """A dispatch that states a narrow contract is a bug; one that cannot be
    issued at all is worse. The helper degrades to what `next_action` built.
    """
    out = {
        "action": "dispatch_se",
        "story_id": "US-1",
        "dod": {
            "tier": "mature",
            "tier_source": "planned",
            "active_checks": list(sp.DOD_TIER_CHECKS["mature"]),
        },
    }
    sp.attach_dispatch_dod_contract(out, str(tmp_path / "no-such-project"))

    assert out["dod"]["tier"] == "mature"
    assert "tests_pass" in out["dod"]["active_checks"]


@pytest.mark.unit
def test_no_story_id_is_a_no_op(tmp_path: Path):
    """Ceremony and terminal actions name no story — nothing to resolve."""
    out = {
        "action": "sprint_complete",
        "story_id": None,
        "dod": {"tier": "early", "tier_source": "computed",
                "active_checks": list(sp.DOD_TIER_CHECKS["early"])},
    }
    before = json.dumps(out, sort_keys=True)
    sp.attach_dispatch_dod_contract(out, str(tmp_path))
    assert json.dumps(out, sort_keys=True) == before
