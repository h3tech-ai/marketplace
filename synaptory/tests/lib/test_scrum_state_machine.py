"""Layer 1 — `plugin/hooks/lib/scrum_state_machine.py` FSM tests.

Hypothesis: lifecycle state transitions are gated by `SCRUM_TRANSITIONS`;
illegal jumps must raise `ValueError`. Initialise → walk the happy path
→ confirm history is recorded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import json
import os

from hooks.lib.scrum_state_machine import (
    SCRUM_STATES,
    SCRUM_TRANSITIONS,
    add_story,
    get_lifecycle_state,
    initialize,
    read_state,
    transition,
    transition_story,
)


@pytest.fixture
def project(tmp_path: Path) -> str:
    """A fresh project_dir with `.synaptory/` set up by initialize()."""
    p = tmp_path / "project"
    p.mkdir()
    initialize(str(p))
    return str(p)


@pytest.mark.unit
def test_states_and_transitions_locked():
    """Pin the canonical states + transitions — adding/removing is a breaking change."""
    assert SCRUM_STATES == [
        "INCEPTION",
        "SPRINT_PLANNING",
        "SPRINT_EXECUTION",
        "SPRINT_REVIEW",
        "SPRINT_RETRO",
        "SPRINT_CLOSE",
        "RELEASE",
        "COMPLETE",
    ]
    assert SCRUM_TRANSITIONS == {
        "INCEPTION": ["SPRINT_PLANNING"],
        "SPRINT_PLANNING": ["SPRINT_EXECUTION"],
        "SPRINT_EXECUTION": ["SPRINT_REVIEW"],
        "SPRINT_REVIEW": ["SPRINT_RETRO"],
        "SPRINT_RETRO": ["SPRINT_CLOSE"],
        "SPRINT_CLOSE": ["SPRINT_PLANNING", "RELEASE"],
        "RELEASE": ["COMPLETE"],
    }


@pytest.mark.unit
def test_initialize_starts_at_inception(project):
    assert get_lifecycle_state(project) == "INCEPTION"


@pytest.mark.unit
def test_full_happy_path(project):
    """Walk INCEPTION → SPRINT_PLANNING → … → RELEASE → COMPLETE."""
    sequence = [
        "SPRINT_PLANNING",
        "SPRINT_EXECUTION",
        "SPRINT_REVIEW",
        "SPRINT_RETRO",
        "SPRINT_CLOSE",
        "RELEASE",
        "COMPLETE",
    ]
    for nxt in sequence:
        # Pre-#125 follow-up, the happy path could walk straight through
        # without artifacts. Now SPRINT_REVIEW → SPRINT_RETRO is FSM-
        # gated on a TW receipt — drop one right before crossing it.
        if nxt == "SPRINT_RETRO":
            sprint_num = read_state(project).get("current_sprint") or 0
            _write_tw_receipt(project, sprint_num=sprint_num)
        transition(project, nxt)
        assert get_lifecycle_state(project) == nxt


@pytest.mark.unit
def test_loop_back_from_sprint_close(project):
    """SPRINT_CLOSE can loop back to SPRINT_PLANNING for the next sprint."""
    for nxt in ("SPRINT_PLANNING", "SPRINT_EXECUTION", "SPRINT_REVIEW",
                "SPRINT_RETRO", "SPRINT_CLOSE"):
        if nxt == "SPRINT_RETRO":
            sprint_num = read_state(project).get("current_sprint") or 0
            _write_tw_receipt(project, sprint_num=sprint_num)
        transition(project, nxt)
    transition(project, "SPRINT_PLANNING")  # next sprint
    assert get_lifecycle_state(project) == "SPRINT_PLANNING"


@pytest.mark.unit
@pytest.mark.parametrize(
    "from_state,to_state",
    [
        ("INCEPTION", "SPRINT_EXECUTION"),  # skip planning
        ("SPRINT_PLANNING", "SPRINT_REVIEW"),  # skip execution
        ("SPRINT_REVIEW", "SPRINT_CLOSE"),  # skip retro (the explicit "no-bypass" rule)
        ("SPRINT_PLANNING", "COMPLETE"),  # skip everything
    ],
)
def test_illegal_transitions_raise(tmp_path: Path, from_state: str, to_state: str):
    """Per-edge: any legal walk to `from_state` must reject the bad jump."""
    project = tmp_path / "project"
    project.mkdir()
    initialize(str(project))
    # Walk to the from_state legally first.
    walk_to_state(str(project), from_state)
    with pytest.raises(ValueError, match="Illegal transition"):
        transition(str(project), to_state)


@pytest.mark.unit
def test_complete_is_terminal(project):
    """No transition is legal once we hit COMPLETE."""
    walk_to_state(project, "COMPLETE")
    with pytest.raises(ValueError, match="COMPLETE"):
        transition(project, "RELEASE")


@pytest.mark.unit
def test_unknown_state_rejected(project):
    with pytest.raises(ValueError, match="Invalid state"):
        transition(project, "DOES_NOT_EXIST")


@pytest.mark.unit
def test_force_overrides_legality_check(project):
    """`force=True` is the operator escape hatch — illegal jumps go through."""
    transition(project, "SPRINT_PLANNING", force=True)
    transition(project, "RELEASE", force=True)
    assert get_lifecycle_state(project) == "RELEASE"


@pytest.mark.unit
def test_lifecycle_history_recorded(project):
    """Each transition appends an entry; the prior entry's exited_at is set."""
    transition(project, "SPRINT_PLANNING")
    transition(project, "SPRINT_EXECUTION")
    state = read_state(project)
    history = state["lifecycle_history"]
    # initialize() seeds the INCEPTION entry, so history has 3 rows after
    # 2 transitions.
    assert len(history) == 3
    assert history[0]["state"] == "INCEPTION"
    assert history[1]["state"] == "SPRINT_PLANNING"
    assert history[2]["state"] == "SPRINT_EXECUTION"
    # All-but-last carry exited_at; the current state's is None.
    assert history[0]["exited_at"] is not None
    assert history[1]["exited_at"] is not None
    assert history[2]["exited_at"] is None


# ─── helpers ─────────────────────────────────────────────────────────────────


def walk_to_state(project_dir: str, target: str) -> None:
    """Perform legal transitions until lifecycle reaches `target`.

    SPRINT_REVIEW → SPRINT_RETRO requires a TW report receipt as of
    the #125 follow-up FSM guard, so transitions that cross that
    boundary first drop a synthetic TW receipt.
    """
    if target == "INCEPTION":
        return
    plan = {
        "SPRINT_PLANNING": ["SPRINT_PLANNING"],
        "SPRINT_EXECUTION": ["SPRINT_PLANNING", "SPRINT_EXECUTION"],
        "SPRINT_REVIEW": [
            "SPRINT_PLANNING", "SPRINT_EXECUTION", "SPRINT_REVIEW",
        ],
        "SPRINT_RETRO": [
            "SPRINT_PLANNING", "SPRINT_EXECUTION", "SPRINT_REVIEW",
            "SPRINT_RETRO",
        ],
        "SPRINT_CLOSE": [
            "SPRINT_PLANNING", "SPRINT_EXECUTION", "SPRINT_REVIEW",
            "SPRINT_RETRO", "SPRINT_CLOSE",
        ],
        "RELEASE": [
            "SPRINT_PLANNING", "SPRINT_EXECUTION", "SPRINT_REVIEW",
            "SPRINT_RETRO", "SPRINT_CLOSE", "RELEASE",
        ],
        "COMPLETE": [
            "SPRINT_PLANNING", "SPRINT_EXECUTION", "SPRINT_REVIEW",
            "SPRINT_RETRO", "SPRINT_CLOSE", "RELEASE", "COMPLETE",
        ],
    }
    for state in plan[target]:
        # Drop the TW receipt right before crossing the SPRINT_REVIEW
        # → SPRINT_RETRO boundary so the new FSM guard accepts it.
        if state == "SPRINT_RETRO":
            sprint_num = read_state(project_dir).get("current_sprint") or 0
            _write_tw_receipt(project_dir, sprint_num=sprint_num)
        transition(project_dir, state)


def _write_tw_receipt(project_dir: str, sprint_num: int) -> str:
    """Drop a minimal TW receipt file so the SPRINT_REVIEW → SPRINT_RETRO
    guard sees it. Returns the receipt path so tests can also remove it
    to exercise the failure path."""
    receipts_dir = os.path.join(
        project_dir, ".synaptory", ".orchestrator", "receipts",
    )
    os.makedirs(receipts_dir, exist_ok=True)
    path = os.path.join(receipts_dir, f"SPRINT-{sprint_num}-tw.json")
    with open(path, "w") as f:
        json.dump({
            "role": "technical-writer",
            "story_id": f"SPRINT-{sprint_num}",
            "verification_commands": ["tw_report_emitted"],
        }, f)
    return path


def _walk_to_sprint_review(project_dir: str) -> None:
    """Walk the lifecycle from INCEPTION to SPRINT_REVIEW for tests below.

    Mirrors what the orchestrator does naturally: INCEPTION → SPRINT_PLANNING
    → (start_sprint sets SPRINT_EXECUTION) → manual transition to
    SPRINT_REVIEW.
    """
    from hooks.lib.scrum_state_machine import start_sprint
    transition(project_dir, "SPRINT_PLANNING")
    start_sprint(project_dir, 1, "Test sprint",
                 [{"id": "US-1", "title": "t"}])
    transition(project_dir, "SPRINT_REVIEW")


@pytest.mark.unit
def test_sprint_review_to_retro_blocked_without_tw_receipt(project: str):
    """#125 follow-up: the FSM must enforce Sprint Review Step 7 (TW
    report mandatory). Markdown-only guards have already been observed
    to be skipped by the orchestrator in the wild."""
    _walk_to_sprint_review(project)

    with pytest.raises(ValueError, match="TW report receipt not found"):
        transition(project, "SPRINT_RETRO")


@pytest.mark.unit
def test_sprint_review_to_retro_allowed_with_tw_receipt(project: str):
    """Happy path: writing the TW receipt unblocks the transition."""
    _walk_to_sprint_review(project)
    _write_tw_receipt(project, sprint_num=1)

    state = transition(project, "SPRINT_RETRO")
    assert state["lifecycle_state"] == "SPRINT_RETRO"


@pytest.mark.unit
def test_sprint_review_to_retro_force_bypasses_tw_guard(project: str):
    """Documented escape hatch: `force=True` skips the guard. Used by
    operators recovering from a stuck retro."""
    _walk_to_sprint_review(project)

    state = transition(project, "SPRINT_RETRO", force=True)
    assert state["lifecycle_state"] == "SPRINT_RETRO"


@pytest.mark.unit
def test_transition_story_to_done_emits_evidence_dod_natural_path(
    project: str, monkeypatch
):
    """#125 follow-up: the toggle-off path (default config) must emit
    `evidence_dod` so the Overview Gate Queue card shows real lifecycle
    data for the 95%+ of projects that don't opt into the per-story
    acceptance toggle. Capture the emitter to confirm it fires."""
    calls: list[tuple[str, str]] = []
    import gate_emitter
    monkeypatch.setattr(
        gate_emitter, "emit_evidence_dod_accepted",
        lambda story_id, who: calls.append(("approved", story_id)) or True,
    )
    monkeypatch.setattr(
        gate_emitter, "emit_evidence_dod_rejected",
        lambda story_id, who, reason: calls.append(("rejected", story_id)) or True,
    )

    add_story(project, "US-200", title="ev-dod natural path")
    transition_story(project, "US-200", "in_progress")
    transition_story(project, "US-200", "testing")
    transition_story(project, "US-200", "reviewing")
    transition_story(project, "US-200", "done")

    # Exactly one evidence_dod event fires per story completion. The
    # rejection vs approval depends on the DoD result; here DoD has no
    # receipts to evaluate, so all checks return passed=None (not True),
    # which lands on the rejected branch — and that's the right
    # behaviour for "no evidence at all".
    assert len(calls) == 1
    assert calls[0][1] == "US-200"


@pytest.mark.unit
def test_transition_story_to_done_skips_emit_via_awaiting_acceptance(
    project: str, monkeypatch
):
    """When the `per_story_acceptance` toggle is on, stories pass
    through `awaiting_acceptance` and `accept_story` does the emit.
    The natural-path emit in transition_story must skip to avoid
    double-counting."""
    # Enable the toggle.
    (Path(project) / ".synaptory.yaml").write_text(
        "sprint:\n  review:\n    per_story_acceptance: true\n",
        encoding="utf-8",
    )

    calls: list[tuple[str, str]] = []
    import gate_emitter
    monkeypatch.setattr(
        gate_emitter, "emit_evidence_dod_accepted",
        lambda story_id, who: calls.append(("approved", story_id)) or True,
    )
    monkeypatch.setattr(
        gate_emitter, "emit_evidence_dod_rejected",
        lambda story_id, who, reason: calls.append(("rejected", story_id)) or True,
    )

    add_story(project, "US-201", title="ev-dod via awaiting")
    transition_story(project, "US-201", "in_progress")
    transition_story(project, "US-201", "testing")
    transition_story(project, "US-201", "reviewing")
    # Toggle ON → reviewing → done is redirected to awaiting_acceptance.
    state = transition_story(project, "US-201", "done")
    story = next(s for s in state["current_stories"] if s["id"] == "US-201")
    assert story["state"] == "awaiting_acceptance"
    # Now walk awaiting_acceptance → done. accept_story is the canonical
    # path; transition_story("done") from awaiting_acceptance must
    # SKIP the natural-path emit (accept_story owns the emission).
    transition_story(project, "US-201", "done")
    assert calls == [], (
        f"Toggle-on path must NOT double-emit from transition_story; "
        f"got {calls}"
    )


@pytest.mark.unit
def test_transition_story_to_done_auto_evaluates_dod(project: str):
    """Regression for #104: `→ done` must populate `story["dod"]` automatically.

    Before the fix, the orchestrator had to remember to call evaluate_dod
    separately; when it didn't (and the missing protocol body meant it
    usually didn't), complete_sprint computed dod_compliance = 0.0 over
    zero evaluated stories.
    """
    add_story(project, "US-104", title="Regression cover", backends={})
    transition_story(project, "US-104", "in_progress")
    transition_story(project, "US-104", "testing")
    transition_story(project, "US-104", "reviewing")
    state = transition_story(project, "US-104", "done")

    story = next(s for s in state["current_stories"] if s["id"] == "US-104")
    assert story["dod"] is not None, "auto-eval should populate dod on → done"
    assert "passed" in story["dod"]
    assert "checks" in story["dod"]


def _write_receipt(project: str, story_id: str, role: str, **fields) -> None:
    """Write a story receipt into the shared receipts dir the FSM reads."""
    import story_pipeline as sp

    receipts = Path(project) / ".synaptory" / ".orchestrator" / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    abbrev = sp._role_to_abbrev(role)
    (receipts / f"{story_id}-{abbrev}.json").write_text(
        json.dumps({"agent": role, **fields}), encoding="utf-8"
    )


@pytest.mark.unit
def test_transition_story_ui_bearing_blocks_without_browser_proof(
    project: str, monkeypatch
):
    """#44 end-to-end through the REAL scrum state machine: a UI-bearing story
    with green backend-only receipts must redirect `reviewing → done` to
    `blocked` (not `done`), with the `ui_acceptance` gate in the reason.

    This is the path the orchestrator follows — it is the regression the L1
    `evaluate_story_dod` tests can't pin on their own."""
    monkeypatch.delenv("SYNAPTORY_ACTIVE_SPEC", raising=False)

    # UI-bearing by its own title (verb "view" + "dashboard"/"screen").
    add_story(project, "US-300", title="View the analytics dashboard screen", backends={})
    # Green SE/QE — build + tests pass, but NO metrics.ui_verification.
    _write_receipt(
        project, "US-300", "software-engineer",
        artifacts=["src/api/analytics.ts"],
        verification_commands=[{"command": "npm run build", "exit_code": 0}],
    )
    _write_receipt(
        project, "US-300", "quality-engineer",
        artifacts=["src/api/analytics.test.ts"],
        verification_commands=[{"command": "npm test", "exit_code": 0}],
        metrics={},  # no ui_verification → ui_acceptance unverified
    )

    transition_story(project, "US-300", "in_progress")
    transition_story(project, "US-300", "testing")
    transition_story(project, "US-300", "reviewing")
    state = transition_story(project, "US-300", "done")

    story = next(s for s in state["current_stories"] if s["id"] == "US-300")
    assert story["state"] == "blocked", "UI story with no browser proof must fail-closed"
    assert "ui_acceptance" in (story.get("blocked_reason") or "")


@pytest.mark.unit
def test_transition_story_ui_bearing_passes_with_browser_proof(
    project: str, monkeypatch
):
    """Counterpart: the same UI story DOES reach `done` once QE records a
    structured `metrics.ui_verification` proof."""
    monkeypatch.delenv("SYNAPTORY_ACTIVE_SPEC", raising=False)

    add_story(project, "US-301", title="View the analytics dashboard screen", backends={})
    _write_receipt(
        project, "US-301", "software-engineer",
        artifacts=["src/pages/Analytics.tsx"],
        verification_commands=[{"command": "npm run build", "exit_code": 0}],
    )
    _write_receipt(
        project, "US-301", "quality-engineer",
        artifacts=["e2e/analytics.spec.ts"],
        verification_commands=[{"command": "npm run e2e", "exit_code": 0}],
        metrics={"ui_verification": {"rendered": True, "routes_tested": 2, "flows_failed": 0}},
    )

    transition_story(project, "US-301", "in_progress")
    transition_story(project, "US-301", "testing")
    transition_story(project, "US-301", "reviewing")
    state = transition_story(project, "US-301", "done")

    story = next(s for s in state["current_stories"] if s["id"] == "US-301")
    assert story["state"] == "done"


@pytest.mark.unit
def test_add_story_cli_passes_acceptance_criteria(project: str, monkeypatch):
    """#44 P1: the `add_story` CLI must forward `--acceptance-criteria` so a
    story whose UI signal lives ONLY in its ACs (non-UI title) still gets
    `ui_bearing=True`. Before the fix the CLI dropped ACs and such a story
    could reach `done` on backend-only receipts."""
    import scrum_state_machine as ssm

    # Non-UI title; the UI verb ("opens"/"renders") is only in the AC.
    argv = [
        "scrum_state_machine.py", "add_story", project, "US-400",
        "--title", "Persist user preferences",
        "--acceptance-criteria",
        json.dumps(["When the user opens the dashboard, the preferences panel renders"]),
    ]
    monkeypatch.setattr("sys.argv", argv)
    ssm.main()

    story = next(s for s in read_state(project)["current_stories"] if s["id"] == "US-400")
    assert story["ui_bearing"] is True
    assert story["acceptance_criteria"]  # ACs were persisted, not dropped
