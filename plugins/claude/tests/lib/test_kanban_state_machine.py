"""Layer 1 — `plugin-claude/hooks/lib/kanban_state_machine.py` FSM tests.

Symmetric to test_scrum_state_machine.py. Hypothesis: lifecycle
transitions are gated by `KANBAN_TRANSITIONS`; illegal jumps raise
`ValueError`. The REVIEW→READY edge is the lane re-queue case for
stories that didn't land first time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import json

from hooks.lib.kanban_state_machine import (
    KANBAN_STATES,
    KANBAN_TRANSITIONS,
    get_lifecycle_state,
    initialize,
    pull_ticket,
    read_state,
    transition,
    transition_story,
)


@pytest.fixture
def project(tmp_path: Path) -> str:
    p = tmp_path / "project"
    p.mkdir()
    initialize(str(p))
    return str(p)


@pytest.mark.unit
def test_states_and_transitions_locked():
    assert KANBAN_STATES == [
        "DISCOVER",
        "READY",
        "EXECUTION",
        "REVIEW",
        "RELEASE",
        "COMPLETE",
    ]
    assert KANBAN_TRANSITIONS == {
        "DISCOVER": ["READY"],
        "READY": ["EXECUTION"],
        "EXECUTION": ["REVIEW"],
        "REVIEW": ["READY", "RELEASE"],
        "RELEASE": ["COMPLETE"],
    }


@pytest.mark.unit
def test_initialize_starts_at_discover(project):
    assert get_lifecycle_state(project) == "DISCOVER"


@pytest.mark.unit
def test_full_happy_path(project):
    for nxt in ("READY", "EXECUTION", "REVIEW", "RELEASE", "COMPLETE"):
        transition(project, nxt)
        assert get_lifecycle_state(project) == nxt


@pytest.mark.unit
def test_review_can_loop_back_to_ready(project):
    """REVIEW→READY is the re-queue path for stories that didn't land."""
    for nxt in ("READY", "EXECUTION", "REVIEW", "READY"):
        transition(project, nxt)
    assert get_lifecycle_state(project) == "READY"


@pytest.mark.unit
@pytest.mark.parametrize(
    "from_state,to_state",
    [
        ("DISCOVER", "EXECUTION"),  # skip READY
        ("READY", "REVIEW"),  # skip EXECUTION
        ("EXECUTION", "RELEASE"),  # skip REVIEW
        ("DISCOVER", "COMPLETE"),  # skip everything
    ],
)
def test_illegal_transitions_raise(tmp_path: Path, from_state: str, to_state: str):
    project = tmp_path / "project"
    project.mkdir()
    initialize(str(project))
    walk_to_state(str(project), from_state)
    with pytest.raises(ValueError, match="Illegal transition"):
        transition(str(project), to_state)


@pytest.mark.unit
def test_complete_is_terminal(project):
    walk_to_state(project, "COMPLETE")
    with pytest.raises(ValueError, match="COMPLETE"):
        transition(project, "RELEASE")


def walk_to_state(project_dir: str, target: str) -> None:
    if target == "DISCOVER":
        return
    plan = {
        "READY": ["READY"],
        "EXECUTION": ["READY", "EXECUTION"],
        "REVIEW": ["READY", "EXECUTION", "REVIEW"],
        "RELEASE": ["READY", "EXECUTION", "REVIEW", "RELEASE"],
        "COMPLETE": ["READY", "EXECUTION", "REVIEW", "RELEASE", "COMPLETE"],
    }
    for state in plan[target]:
        transition(project_dir, state)


def _write_receipt(project: str, ticket_id: str, role: str, **fields) -> None:
    import story_pipeline as sp

    receipts = Path(project) / ".synaptory" / ".orchestrator" / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    abbrev = sp._role_to_abbrev(role)
    (receipts / f"{ticket_id}-{abbrev}.json").write_text(
        json.dumps({"agent": role, **fields}), encoding="utf-8"
    )


@pytest.mark.unit
def test_transition_story_ui_bearing_blocks_without_browser_proof(project, monkeypatch):
    """#44 symmetric to the scrum case: a UI-bearing ticket with green
    backend-only receipts must fail-closed to `blocked` at `reviewing → done`
    through the real kanban state machine."""
    monkeypatch.delenv("SYNAPTORY_ACTIVE_SPEC", raising=False)

    pull_ticket(project, "TICK-1", title="Open the settings page and render the form")
    _write_receipt(
        project, "TICK-1", "software-engineer",
        artifacts=["src/settings.ts"],
        verification_commands=[{"command": "npm run build", "exit_code": 0}],
    )
    _write_receipt(
        project, "TICK-1", "quality-engineer",
        artifacts=["src/settings.test.ts"],
        verification_commands=[{"command": "npm test", "exit_code": 0}],
        metrics={},  # no ui_verification
    )

    transition_story(project, "TICK-1", "in_progress")
    transition_story(project, "TICK-1", "testing")
    transition_story(project, "TICK-1", "reviewing")
    state = transition_story(project, "TICK-1", "done")

    ticket = next(s for s in state["current_stories"] if s["id"] == "TICK-1")
    assert ticket["state"] == "blocked"
    assert "ui_acceptance" in (ticket.get("blocked_reason") or "")


@pytest.mark.unit
def test_pull_ticket_cli_passes_acceptance_criteria(project, monkeypatch):
    """#44 P1: the `pull_ticket` CLI must forward `--acceptance-criteria` so a
    ticket whose UI signal lives ONLY in its ACs (non-UI title) still gets
    `ui_bearing=True`. Before the fix the CLI dropped ACs at line 477."""
    import kanban_state_machine as ksm

    argv = [
        "kanban_state_machine.py", "pull_ticket", project, "TICK-9",
        "--title", "Store the audit row",
        "--acceptance-criteria",
        json.dumps(["When the user opens the dashboard, the UI renders the audit feed"]),
    ]
    monkeypatch.setattr("sys.argv", argv)
    ksm.main()

    ticket = next(s for s in read_state(project)["current_stories"] if s["id"] == "TICK-9")
    assert ticket["ui_bearing"] is True
    assert ticket["acceptance_criteria"]


# ── #304: the serial path now gates `depends_on` in this lifecycle too ─────


def test_serial_dispatch_now_gates_depends_on():
    """An intentional behaviour change, asserted here so nobody reverts it.

    `create_story` populates `depends_on` for all three lifecycles (#134
    GAP-6), and with story parallelism defaulting off the serial path was the
    ONLY path -- and it was ungated, so a queued story could start against an
    unfinished upstream. The gate is deliberately not conditioned on
    build_mode: #304 objects to serial and parallel dispatch disagreeing about
    one contract. A project holding aspirational or stale edges (the shipped
    prompts described `depends_on` as a batch filter) can set
    `resilience.dependency_gate: warn` for one release.
    """
    from story_pipeline import next_action

    def _unit(sid, state, **kw):
        return {"id": sid, "title": sid, "state": state, **kw}

    board = {
        "version": "2.0",
        "build_mode": "kanban",
        "lifecycle_state": "EXECUTION",
        "cumulative_ticket_number": 2,
        "current_stories": [
            _unit("TK-02", "queued", depends_on=["TK-01"]),
        ],
    }
    out = next_action(board)
    assert out["action"] == "deps_blocked", out
    assert out["dependencies_held"][0]["story_id"] == "TK-02"


def test_serial_dispatch_skips_a_blocked_unit_instead_of_starving_the_queue():
    from story_pipeline import next_action

    def _unit(sid, state, **kw):
        return {"id": sid, "title": sid, "state": state, **kw}

    board = {
        "version": "2.0",
        "build_mode": "kanban",
        "lifecycle_state": "EXECUTION",
        "cumulative_ticket_number": 2,
        "current_stories": [
            _unit("TK-02", "queued", depends_on=["TK-01"]),
            _unit("TK-01", "queued"),
        ],
    }
    out = next_action(board)
    assert out["action"] == "dispatch_se"
    assert out["story_id"] == "TK-01", "blocked FIFO head starved the queue"
