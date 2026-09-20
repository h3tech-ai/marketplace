#!/usr/bin/env python3
"""Scrum lifecycle state machine for synaptory v2.

8-state cyclic machine: INCEPTION → SPRINT_PLANNING → SPRINT_EXECUTION →
SPRINT_REVIEW → SPRINT_RETRO (adaptive) → SPRINT_CLOSE → (loop or RELEASE) → COMPLETE.

State is stored at .synaptory/.orchestrator/pipeline-state.json.
Delegates story sub-state management to story_pipeline.py.

CLI:
    python3 scrum_state_machine.py init <project_dir> [--backends '{"default":"claude","roles":{}}']
    python3 scrum_state_machine.py read <project_dir>
    python3 scrum_state_machine.py transition <project_dir> <to_state> [--force]
    python3 scrum_state_machine.py start_sprint <project_dir> <sprint_num> --goal "..." --stories '[...]'
    python3 scrum_state_machine.py complete_sprint <project_dir>
    python3 scrum_state_machine.py close_sprint <project_dir> [--proceed-to SPRINT_PLANNING|RELEASE]
    python3 scrum_state_machine.py add_story <project_dir> <story_id> [--title "..."] [--backends '{...}']
    python3 scrum_state_machine.py transition_story <project_dir> <story_id> <to_state> [--reason "..."] [--force-recovery]
    python3 scrum_state_machine.py evaluate_dod <project_dir> <story_id>
    python3 scrum_state_machine.py request_acceptance <project_dir> <story_id>
    python3 scrum_state_machine.py accept_story <project_dir> <story_id> --accepted-by '<email>'
    python3 scrum_state_machine.py reject_story <project_dir> <story_id> --reason needs-fix|redo|defer|cancel --feedback '<text>' --rejected-by '<email>' [--ac-change '<change>' ...]
    python3 scrum_state_machine.py velocity <project_dir>
    python3 scrum_state_machine.py summary <project_dir>
    python3 scrum_state_machine.py retro_check <project_dir>
    python3 scrum_state_machine.py transition_to_kanban <project_dir>
"""

from __future__ import annotations

from product_version import product_version

from state_schema import state_schema

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any

# Spec-aware state I/O (single-spec v2 and multi-spec v3, see
# docs/multi-spec-design.md). When called without an explicit spec_id, falls
# back to the SYNAPTORY_ACTIVE_SPEC env var, then to the file's `active_spec`.
import spec_state as _ss
import host_env
import advance_kernel

# Import shared story pipeline (same directory).
from story_pipeline import (
    create_story,
    transition_story as _sp_transition_story,
    unblock_story as _sp_unblock_story,
    evaluate_story_dod as _sp_evaluate_dod,
    determine_dod_intensity,
    dod_gate_block_reason,
    ship_evaluated_dod as _sp_ship_evaluated_dod,
    aggregate_sprint_dod,
    calculate_story_cycle_time,
    get_story,
    is_per_story_acceptance_enabled,
    verification_loops_active,
    list_stories_by_state,
    accept_story as _sp_accept_story,
    reject_story as _sp_reject_story,
    request_acceptance as _sp_request_acceptance,
    next_action as _sp_next_action,
    attach_dispatch_dod_contract,
    dep_context,
    dependency_gate_mode,
    parallelism_config,
    resolve_dod_tier,
    _resolve_receipts_dir,
    _cli_receipt_gated_refusal,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRUM_STATES = [
    "INCEPTION",
    "SPRINT_PLANNING",
    "SPRINT_EXECUTION",
    "SPRINT_REVIEW",
    "SPRINT_RETRO",
    "SPRINT_CLOSE",
    "RELEASE",
    "COMPLETE",
]

SCRUM_TRANSITIONS: dict[str, list[str]] = {
    "INCEPTION": ["SPRINT_PLANNING"],
    "SPRINT_PLANNING": ["SPRINT_EXECUTION"],
    "SPRINT_EXECUTION": ["SPRINT_REVIEW"],
    # SPRINT_REVIEW must always pass through SPRINT_RETRO — even when retro content
    # is skipped (e.g. Sprint 1), the node must be visited so the TW report gate and
    # process-log write are exercised. Removing the direct → SPRINT_CLOSE path closes
    # the bypass that allowed orchestrators to skip Retro + TW report in one step.
    "SPRINT_REVIEW": ["SPRINT_RETRO"],
    "SPRINT_RETRO": ["SPRINT_CLOSE"],
    "SPRINT_CLOSE": ["SPRINT_PLANNING", "RELEASE"],
    "RELEASE": ["COMPLETE"],
}

# ---------------------------------------------------------------------------
# State File I/O
# ---------------------------------------------------------------------------

def _state_path(project_dir: str) -> str:
    return _ss.state_path(project_dir)


def _resolve_spec_id(spec_id: str | None) -> str | None:
    """Resolve spec_id: explicit arg → SYNAPTORY_ACTIVE_SPEC env var → None."""
    if spec_id:
        return spec_id
    env = os.environ.get("SYNAPTORY_ACTIVE_SPEC")
    return env if env else None


def _default_state() -> dict[str, Any]:
    return {
        "version": product_version(), "state_schema": 2,
        "build_mode": "scrum",
        "lifecycle_state": "INCEPTION",
        "started_at": _now(),
        "lifecycle_history": [
            {"state": "INCEPTION", "entered_at": _now(), "exited_at": None},
        ],
        "inception": {"mode": "foundation", "completed_at": None},
        "current_sprint": 0,
        "total_sprints": None,
        "sprint_goal": None,
        "sprints_completed": [],
        "current_stories": [],
        "process_log": [],
        "agent_backends": {
            "default": host_env.default_agent_backend(),
            "roles": {},
        },
    }


def _default_spec_substate() -> dict[str, Any]:
    """Per-spec slot without envelope version, state_schema, or build_mode."""
    s = _default_state()
    s.pop("version", None)
    s.pop("state_schema", None)
    s.pop("build_mode", None)
    return s


def read_state(project_dir: str, spec_id: str | None = None) -> dict[str, Any]:
    """Read pipeline state. Raises ValueError on unrecognized format.

    Single-spec (v2): returns the flat state dict.
    Multi-spec (v3):  returns a view of the spec's sub-state with
                      `_spec_id` / `_multispec` breadcrumbs that
                      `_write_state` consumes to re-merge changes.
    """
    sid = _resolve_spec_id(spec_id)
    path = _state_path(project_dir)
    if not os.path.exists(path):
        return _default_state()

    full = _ss.read_full_state(project_dir)
    if _ss.is_multispec(full):
        # In v3 we hand back a flat per-spec view; the state machine code
        # below operates on it exactly as if it were the v2 layout.
        state = _ss.read_state(
            project_dir, spec_id=sid,
            default_state_factory=_default_spec_substate,
        )
        if "lifecycle_state" not in state:
            raise ValueError(
                "Multi-spec state slot is missing lifecycle_state. "
                "Run 'init' to seed."
            )
        return state

    # v2 flat layout
    if state_schema(full) != 2 or "lifecycle_state" not in full:
        raise ValueError(
            "Unrecognized state format — expected v2.0 with lifecycle_state. "
            "Run 'init' to start fresh."
        )
    return full


def _write_state(project_dir: str, state: dict[str, Any]) -> None:
    _ss.write_state(project_dir, state)


# ---------------------------------------------------------------------------
# Lifecycle Management
# ---------------------------------------------------------------------------

def initialize(
    project_dir: str,
    agent_backends: dict[str, Any] | None = None,
    spec_id: str | None = None,
) -> dict[str, Any]:
    """Initialize Scrum state at INCEPTION.

    Single-spec: writes a flat v2.0 file.
    Multi-spec:  initializes `specs[<spec_id>]` in the existing v3 file
                 (preserves other specs).
    """
    sid = _resolve_spec_id(spec_id)
    full = _ss.read_full_state(project_dir)

    if sid:
        # Multi-spec init: upsert this spec's slot.
        if not full:
            full = {"version": product_version(), "state_schema": 3, "build_mode": "scrum",
                    "active_spec": sid, "specs": {}}
        elif state_schema(full) == 2:
            # Refuse to silently clobber a v2 file — caller must migrate first.
            raise ValueError(
                "Cannot initialize a v3 spec slot in a v2 file. Run the "
                "migration script (migrate_to_multispec.py) first."
            )
        sub = _default_spec_substate()
        if agent_backends:
            sub["agent_backends"] = agent_backends
        full.setdefault("specs", {})[sid] = sub
        if not full.get("active_spec"):
            full["active_spec"] = sid
        _ss.write_full_state(project_dir, full)
        view = dict(sub)
        view["_spec_id"] = sid
        view["_multispec"] = True
        return view

    state = _default_state()
    if agent_backends:
        state["agent_backends"] = agent_backends
    _write_state(project_dir, state)
    return state


def transition(
    project_dir: str,
    to_state: str,
    force: bool = False,
) -> dict[str, Any]:
    """Transition lifecycle to a new state.

    Validates the transition is legal. Records history.

    Raises:
        ValueError: On illegal transition.
    """
    state = read_state(project_dir)
    current = state["lifecycle_state"]

    if current == "COMPLETE":
        raise ValueError("Cannot transition from COMPLETE — lifecycle is finished")

    if to_state not in SCRUM_STATES:
        raise ValueError(f"Invalid state: {to_state}. Must be one of {SCRUM_STATES}")

    if not force and to_state not in SCRUM_TRANSITIONS.get(current, []):
        raise ValueError(
            f"Illegal transition: {current} → {to_state}. "
            f"Valid targets: {SCRUM_TRANSITIONS.get(current, [])}"
        )

    # SPRINT_REVIEW → SPRINT_RETRO guard: the TW report is a mandatory
    # Sprint Review artifact (ceremony Step 7). Without it, the sprint
    # has no audit trail of what was demoed / metrics / stakeholder
    # feedback. Block the transition until a receipt exists so the
    # orchestrator can't silently advance past Sprint Review. `--force`
    # is the documented escape hatch (e.g. retro recovery, operator
    # backfill).
    if (not force
            and current == "SPRINT_REVIEW"
            and to_state == "SPRINT_RETRO"):
        sprint_num = state.get("current_sprint")
        if sprint_num is not None:
            # Resolve the spec-scoped receipts dir in multi-spec projects
            # (issue #24, Bug B) — the TW report lands under
            # specs/<active>/receipts/, not the shared dir.
            tw_receipt = os.path.join(
                _resolve_receipts_dir(project_dir),
                f"SPRINT-{sprint_num}-tw.json",
            )
            if not os.path.exists(tw_receipt):
                raise ValueError(
                    f"Cannot transition SPRINT_REVIEW → SPRINT_RETRO: "
                    f"TW report receipt not found at "
                    f"{os.path.relpath(tw_receipt, project_dir)}. "
                    f"Dispatch the Technical Writer agent "
                    f"(ceremonies/sprint-review.md Step 2) to generate "
                    f"the sprint report before advancing. Pass --force "
                    f"to bypass (skips Sprint Review Step 7 audit gate)."
                )

    now = _now()

    # Close current history entry.
    if state["lifecycle_history"]:
        state["lifecycle_history"][-1]["exited_at"] = now

    state["lifecycle_state"] = to_state
    state["lifecycle_history"].append(
        {"state": to_state, "entered_at": now, "exited_at": None}
    )

    _write_state(project_dir, state)

    # Emit gate events at lifecycle hand-offs (#125).
    if current == "INCEPTION" and to_state == "SPRINT_PLANNING":
        # Project Inception gate is the formal completion of the
        # inception ceremony — domain pack, schema profile, tracker
        # config all confirmed. v3 §6.1.
        try:
            from gate_emitter import emit_project_inception_approved
            # Project slug isn't stored on state — fall back to the
            # project directory name so the target_id is meaningful in
            # the ledger. The CP carries the canonical slug separately
            # via membership lookup.
            project_slug = os.path.basename(os.path.abspath(project_dir))
            emit_project_inception_approved(
                project_slug, "orchestrator", project_dir=project_dir
            )
        except ImportError:
            pass
    elif current in ("SPRINT_CLOSE", "RELEASE") and to_state == "COMPLETE":
        # Final release ceremony completed — emit the release gate so
        # the audit ledger marks the cut, even on Kanban projects that
        # don't run discrete release ceremonies.
        try:
            from gate_emitter import emit_release_approved
            release_id = str(state.get("current_sprint") or "release")
            emit_release_approved(
                release_id, "orchestrator", project_dir=project_dir
            )
        except ImportError:
            pass

    return state


def get_lifecycle_state(project_dir: str) -> str:
    """Return just the current lifecycle state string."""
    state = read_state(project_dir)
    return state["lifecycle_state"]


# ---------------------------------------------------------------------------
# Sprint Management
# ---------------------------------------------------------------------------

def start_sprint(
    project_dir: str,
    sprint_number: int,
    goal: str,
    stories: list[dict[str, Any]],
) -> dict[str, Any]:
    """Begin a new sprint. Must be in SPRINT_PLANNING.

    Args:
        sprint_number: Sprint number.
        goal: Sprint goal string.
        stories: List of {"id": "US-006", "title": "...", "backends": {...}}.
    """
    state = read_state(project_dir)
    if state["lifecycle_state"] != "SPRINT_PLANNING":
        raise ValueError(
            f"Cannot start sprint — lifecycle is {state['lifecycle_state']}, "
            "expected SPRINT_PLANNING"
        )

    state["current_sprint"] = sprint_number
    state["sprint_goal"] = goal
    state["current_stories"] = [
        create_story(
            s["id"],
            s.get("title", ""),
            s.get("backends"),
            # #44 — snapshot ACs so the DoD gate can flag UI-bearing stories.
            acceptance_criteria=s.get("acceptance_criteria"),
            # #134 — PO-declared story metadata from Sprint Planning: `kind`
            # (verification-tier scoping + ui_bearing suppression), tracker
            # labels, and the dependency/scope declarations that gate
            # parallel dispatch.
            kind=s.get("kind", ""),
            labels=s.get("labels"),
            depends_on=s.get("depends_on"),
            file_scope=s.get("file_scope"),
            project_dir=project_dir,
        )
        for s in stories
    ]

    # Transition to SPRINT_EXECUTION.
    now = _now()
    if state["lifecycle_history"]:
        state["lifecycle_history"][-1]["exited_at"] = now
    state["lifecycle_state"] = "SPRINT_EXECUTION"
    state["lifecycle_history"].append(
        {"state": "SPRINT_EXECUTION", "entered_at": now, "exited_at": None}
    )

    _write_state(project_dir, state)

    # Emit `spec_ready / opened` for each spec committed to the sprint.
    # Sprint Planning is the moment a spec becomes agent-eligible — the
    # PO approved it (Spec Ready) and the SE→QE→CR pipeline starts now.
    # See issue #125.
    try:
        from gate_emitter import emit_spec_ready_opened
        emit_spec_ready_opened(
            [s["id"] for s in stories], project_dir=project_dir
        )
    except ImportError:
        pass

    return state


def complete_sprint(project_dir: str) -> dict[str, Any]:
    """Record completed sprint, calculate velocity, transition to SPRINT_REVIEW."""
    state = read_state(project_dir)
    if state["lifecycle_state"] != "SPRINT_EXECUTION":
        raise ValueError(
            f"Cannot complete sprint — lifecycle is {state['lifecycle_state']}, "
            "expected SPRINT_EXECUTION"
        )

    stories = state.get("current_stories", [])
    planned = len(stories)
    completed = len([s for s in stories if s.get("state") == "done"])

    # Calculate DoD compliance.
    evaluated = [s for s in stories if s.get("dod")]
    dod_passed = [s for s in evaluated if s["dod"].get("passed")]
    dod_compliance = len(dod_passed) / len(evaluated) if evaluated else 0.0

    # Calculate average cycle time.
    cycle_times = []
    for s in stories:
        ct = calculate_story_cycle_time(s)
        if ct:
            cycle_times.append(ct["total_seconds"])
    avg_ct = sum(cycle_times) / len(cycle_times) if cycle_times else 0.0

    sprint_record = {
        "number": state["current_sprint"],
        "goal": state.get("sprint_goal", ""),
        "stories_planned": planned,
        "stories_completed": completed,
        "velocity": completed,
        "dod_compliance": round(dod_compliance, 2),
        "cycle_time_avg_seconds": round(avg_ct, 1),
        "completed_at": _now(),
    }
    state["sprints_completed"].append(sprint_record)

    # Transition to SPRINT_REVIEW.
    now = _now()
    if state["lifecycle_history"]:
        state["lifecycle_history"][-1]["exited_at"] = now
    state["lifecycle_state"] = "SPRINT_REVIEW"
    state["lifecycle_history"].append(
        {"state": "SPRINT_REVIEW", "entered_at": now, "exited_at": None}
    )

    _write_state(project_dir, state)
    return state


def close_sprint(
    project_dir: str,
    proceed_to: str = "SPRINT_PLANNING",
) -> dict[str, Any]:
    """Close current sprint and loop or proceed to RELEASE.

    Args:
        proceed_to: "SPRINT_PLANNING" (next sprint) or "RELEASE".
    """
    state = read_state(project_dir)
    if state["lifecycle_state"] != "SPRINT_CLOSE":
        raise ValueError(
            f"Cannot close sprint — lifecycle is {state['lifecycle_state']}, "
            "expected SPRINT_CLOSE"
        )

    if proceed_to not in ("SPRINT_PLANNING", "RELEASE"):
        raise ValueError(f"proceed_to must be SPRINT_PLANNING or RELEASE, got: {proceed_to}")

    # Clear current stories if proceeding to next sprint.
    if proceed_to == "SPRINT_PLANNING":
        state["current_stories"] = []
        state["sprint_goal"] = None

    now = _now()
    if state["lifecycle_history"]:
        state["lifecycle_history"][-1]["exited_at"] = now
    state["lifecycle_state"] = proceed_to
    state["lifecycle_history"].append(
        {"state": proceed_to, "entered_at": now, "exited_at": None}
    )

    _write_state(project_dir, state)
    return state


# ---------------------------------------------------------------------------
# Sprint Metrics
# ---------------------------------------------------------------------------

def calculate_velocity(sprints_completed: list[dict[str, Any]]) -> float:
    """Average stories completed per sprint."""
    if not sprints_completed:
        return 0.0
    total = sum(s.get("stories_completed", 0) for s in sprints_completed)
    return round(total / len(sprints_completed), 1)


def get_sprint_summary(project_dir: str) -> dict[str, Any]:
    """Dashboard data for the current sprint."""
    state = read_state(project_dir)
    stories = state.get("current_stories", [])
    by_state: dict[str, int] = {}
    for s in stories:
        st = s.get("state", "unknown")
        by_state[st] = by_state.get(st, 0) + 1

    return {
        "lifecycle_state": state["lifecycle_state"],
        "current_sprint": state.get("current_sprint", 0),
        "sprint_goal": state.get("sprint_goal"),
        "stories": by_state,
        "stories_total": len(stories),
        "velocity": calculate_velocity(state.get("sprints_completed", [])),
        "sprints_completed": len(state.get("sprints_completed", [])),
        "total_sprints": state.get("total_sprints"),
    }


def determine_retro_eligibility(project_dir: str) -> dict[str, Any]:
    """Decide whether SPRINT_RETRO should run.

    Skip conditions: Sprint 1 (not enough data), or clean sprint.
    Run conditions: DoD compliance < 80%, blocked stories, critical findings.
    """
    state = read_state(project_dir)
    sprint_num = state.get("current_sprint", 1)
    stories = state.get("current_stories", [])

    reasons: list[str] = []

    # Sprint 1: skip.
    if sprint_num <= 1:
        return {"eligible": False, "reasons": ["Sprint 1 — not enough data"], "recommendation": "skip"}

    # Check DoD compliance.
    dod_agg = aggregate_sprint_dod(state)
    if dod_agg["overall_pass_rate"] < 0.8:
        reasons.append(f"DoD pass rate {dod_agg['overall_pass_rate']:.0%} < 80%")

    # Check for blocked stories.
    blocked_entries = []
    for s in stories:
        for entry in s.get("pipeline_log", []):
            if entry.get("state") == "blocked":
                blocked_entries.append(s["id"])
                break
    if blocked_entries:
        reasons.append(f"{len(blocked_entries)} stories were blocked during sprint")

    # Check completion rate.
    planned = len(stories)
    done = len([s for s in stories if s.get("state") == "done"])
    if planned > 0 and done / planned < 0.8:
        reasons.append(f"Completion rate {done}/{planned} ({done/planned:.0%}) < 80%")

    if reasons:
        return {"eligible": True, "reasons": reasons, "recommendation": "run"}
    return {"eligible": False, "reasons": ["Sprint went smoothly"], "recommendation": "skip"}


# ---------------------------------------------------------------------------
# Story Delegation (wraps story_pipeline.py)
# ---------------------------------------------------------------------------

def add_story(
    project_dir: str,
    story_id: str,
    title: str = "",
    backends: dict[str, str] | None = None,
    acceptance_criteria: list[Any] | None = None,
) -> dict[str, Any]:
    """Add a story to current_stories."""
    state = read_state(project_dir)
    story = create_story(
        story_id,
        title,
        backends,
        acceptance_criteria=acceptance_criteria,
        project_dir=project_dir,
    )
    state.setdefault("current_stories", []).append(story)
    _write_state(project_dir, state)
    return state


def transition_story(
    project_dir: str,
    story_id: str,
    to_state: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Transition a story's sub-state.

    Two automatic behaviors:

    1. `→ done` auto-invokes DoD evaluation so `dod_compliance` is never
       0.0 just because the orchestrator forgot to call `evaluate_dod`
       (see #104).
    2. When `sprint.review.per_story_acceptance` is enabled (#116) and the
       orchestrator drives `reviewing → done` (the legacy auto-promotion
       after CR), intercept and route through `awaiting_acceptance`
       instead. The PO walks the rest at Sprint Review. DoD is evaluated
       on entry to `awaiting_acceptance` so the PO has the signal.
    """
    state = read_state(project_dir)

    # Remember the from_state before the transition mutates pipeline_log.
    # Needed below to decide whether to fire the evidence_dod gate event
    # (see "Gate emission" block at end of function).
    story_before = get_story(state, story_id)
    from_state = story_before["state"] if story_before else None

    # #31/#30: a PHI/healthcare story (or a project that opted into mandatory
    # runtime verification) must clear the conditional compliance/runtime gates
    # before it completes. On the `reviewing → done` auto-promotion, evaluate
    # the gates first and redirect to `blocked` (recoverable) if unmet — so the
    # compliance-engineer / runtime verification can't be silently skipped on
    # green SE/QE/CR (the PR #505 failure mode). Checked before the #116
    # acceptance redirect because a compliance failure is more fundamental than
    # the PO acceptance routing.
    if to_state == "done" and from_state == "reviewing":
        # Delegated to the shared kernel (#277). The gate itself is unchanged:
        # resolve_done_edge reproduces this mode's own intensity resolver
        # (sprint number) and the #116 acceptance redirect. One implementation
        # now serves scrum, kanban, spq and both MCP hosts.
        from advance_kernel import resolve_done_edge

        to_state, reason, _pre_dod = resolve_done_edge(
            project_dir, state, story_id, to_state, reason, mode="scrum"
        )
    else:
        _pre_dod = None

    state = _sp_transition_story(state, story_id, to_state, reason, project_dir)
    _write_state(project_dir, state)

    # #403 — a story the gate REDIRECTED to blocked used to record no DoD
    # verdict at all and emit nothing, because both blocks below key on
    # `done`. So the strongest signal the pipeline produces (why the story
    # stopped) was the one signal that never left the machine, and the
    # control plane saw a story that simply went quiet. `resolve_done_edge`
    # already computed the verdict; persist and ship it.
    #
    # This predates #403 for the conditional gates, but #403 makes the redirect
    # reachable for a declared criteria gap, which is a far more common shape
    # than a PHI compliance miss, so leaving the hole open was not an option.
    if to_state == "blocked" and _pre_dod is not None:
        story = get_story(state, story_id)
        if story:
            story["dod"] = _pre_dod
            _write_state(project_dir, state)
            _sp_ship_evaluated_dod(story_id, _pre_dod, project_dir=project_dir)

    if to_state in ("done", "awaiting_acceptance"):
        sprint_num = state.get("current_sprint", 1)
        intensity = determine_dod_intensity(sprint_number=sprint_num)
        result = _sp_evaluate_dod(project_dir, story_id, intensity)
        story = get_story(state, story_id)
        if story:
            story["dod"] = result
            _write_state(project_dir, state)
            _sp_ship_evaluated_dod(
                story_id, result, project_dir=project_dir
            )   # #199

    # #125 follow-up: emit `evidence_dod` gate events for the
    # toggle-OFF natural path (`reviewing → done` direct). The
    # toggle-ON path goes via `accept_story` (or `reject_story`),
    # which has its own emission, so `awaiting_acceptance → done`
    # skips this block to avoid double-emit.
    if to_state == "done" and from_state != "awaiting_acceptance":
        story_now = get_story(state, story_id) or {}
        dod = story_now.get("dod") or {}
        try:
            from gate_emitter import (
                emit_evidence_dod_accepted,
                emit_evidence_dod_rejected,
            )
            if dod.get("passed"):
                emit_evidence_dod_accepted(
                    story_id, "orchestrator", project_dir=project_dir
                )
            else:
                failed_required = [
                    k for k, v in (dod.get("checks") or {}).items()
                    if v.get("required") and v.get("passed") is not True
                ]
                emit_evidence_dod_rejected(
                    story_id, "orchestrator",
                    f"DoD failed: {', '.join(failed_required) or 'unknown'}",
                    project_dir=project_dir,
                )
        except ImportError:
            pass

    return state


def request_acceptance(project_dir: str, story_id: str) -> dict[str, Any]:
    """Move a story to `awaiting_acceptance` (#116)."""
    state = read_state(project_dir)
    state = _sp_request_acceptance(state, story_id, project_dir)
    _write_state(project_dir, state)
    return state


def accept_story(project_dir: str, story_id: str,
                 accepted_by: str) -> dict[str, Any]:
    """PO accepts a story at Sprint Review (#116).

    Also emits an ``evidence_dod / approved`` gate event (#125) so the
    Overview Gate Queue card's Evidence/DoD cell reflects PO sign-off
    counts as well as raw receipt activity.
    """
    state = read_state(project_dir)
    state = _sp_accept_story(state, story_id, accepted_by, project_dir)
    _write_state(project_dir, state)
    # Only a CREDITED acceptance reaches this line. `accept_story` refuses an
    # uncreditable one before it writes anything (#592), so the `returned`
    # emission that #599 added here became unreachable and was removed rather
    # than left looking live. Scrum and SPQ stay identical because they
    # delegate to the same `story_pipeline.accept_story` (#486).
    # Best-effort gate emission, never raises (see gate_emitter docstring).
    try:
        from gate_emitter import emit_evidence_dod_accepted
        emit_evidence_dod_accepted(
            story_id, accepted_by, project_dir=project_dir
        )
    except ImportError:
        pass
    return state


def reject_story(
    project_dir: str,
    story_id: str,
    reason_class: str,
    feedback_text: str,
    rejected_by: str,
    *,
    acceptance_criteria_change: list[str] | None = None,
) -> dict[str, Any]:
    """PO rejects a story at Sprint Review (#116).

    Also emits an ``evidence_dod / rejected`` gate event (#125) carrying
    the reason class + feedback text so the audit ledger records *why*.
    """
    state = read_state(project_dir)
    state = _sp_reject_story(
        state, story_id, reason_class, feedback_text, rejected_by,
        acceptance_criteria_change=acceptance_criteria_change,
        project_dir=project_dir,
    )
    _write_state(project_dir, state)
    try:
        from gate_emitter import emit_evidence_dod_rejected
        emit_evidence_dod_rejected(
            story_id, rejected_by,
            f"[{reason_class}] {feedback_text}",
            project_dir=project_dir,
        )
    except ImportError:
        pass
    return state


def evaluate_story_dod(project_dir: str, story_id: str) -> dict[str, Any]:
    """Evaluate per-story DoD using current sprint for intensity."""
    state = read_state(project_dir)
    sprint_num = state.get("current_sprint", 1)
    intensity = determine_dod_intensity(sprint_number=sprint_num)
    result = _sp_evaluate_dod(project_dir, story_id, intensity)

    # Store result in state.
    story = get_story(state, story_id)
    if story:
        story["dod"] = result
        _write_state(project_dir, state)
        _sp_ship_evaluated_dod(
            story_id, result, project_dir=project_dir
        )   # #199

    return result


def next_action(project_dir: str) -> dict[str, Any]:
    """Lifecycle-aware "what's next" for the sprint loop (epic #75, P1).

    Wraps story_pipeline.next_action with the scrum lifecycle context:
    outside SPRINT_EXECUTION the story board is not dispatchable, so the
    orchestrator (and the P2 Stop-hook loop engine) get `not_in_execution`
    instead of a story action. Read-only — never writes state.

    Passes the SAME argument set as story_pipeline.py's own `next_action`
    CLI, including `dod_tier_info` and `parallelism`. Both are load-bearing
    and were previously omitted here: `modes/sprint.md` drives the sprint
    loop through THIS wrapper, so a missing `parallelism` meant
    `_attach_parallel_batch` bailed out and `parallel.eligible` could never
    be true no matter what the project configured — story parallelism was
    unreachable on the primary path. A missing `dod_tier_info` likewise
    discarded a tier pinned at Sprint Planning and silently recomputed it
    (`tier_source: "computed"`). Both resolvers are pure reads, so the
    read-only contract above still holds.
    """
    state = read_state(project_dir)
    lifecycle = state.get("lifecycle_state", "INCEPTION")
    base = {
        "lifecycle_state": lifecycle,
        "current_sprint": state.get("current_sprint", 0),
        "spec_id": state.get("_spec_id"),
    }
    if lifecycle != "SPRINT_EXECUTION":
        return {
            **base,
            "action": "not_in_execution",
            "story_id": None,
            "reason": (
                f"lifecycle is {lifecycle}, not SPRINT_EXECUTION — the story "
                "board is only dispatched during sprint execution"
            ),
            "human_gate_pending": False,
        }
    result = _sp_next_action(
        state,
        per_story_acceptance=is_per_story_acceptance_enabled(project_dir),
        receipts_dir=_resolve_receipts_dir(project_dir),
        verification_loops=verification_loops_active(project_dir),
        dod_tier_info=resolve_dod_tier(project_dir, state),
        parallelism=parallelism_config(project_dir),
        dep_context=dep_context(project_dir, state),
        dependency_gate=dependency_gate_mode(project_dir),
        verify_only=verify_only_units(project_dir, state),
        inadmissible_receipts=advance_kernel.inadmissible_receipts(project_dir, state),
    )
    # #501 follow-up — swap the tier-only `dod` block for the per-story
    # contract the gate will actually enforce. Lives here, not in
    # `story_pipeline.next_action`, which has no project_dir and stays pure.
    attach_dispatch_dod_contract(
        result,
        project_dir,
        state=state,
        receipts_dir=_resolve_receipts_dir(project_dir),
    )
    # #820 -- a disabled continuation loop is named on the dispatch contract.
    # `loop_engine.attach_loop_status` is the one predicate; re-deriving the
    # kill switches per lifecycle is how one of the three would come to
    # disagree with the engine that actually stops.
    import loop_engine as _loop

    _loop.attach_loop_status(result, project_dir)
    result.update(base)
    return result


# ---------------------------------------------------------------------------
# Scrum → Kanban Transition
# ---------------------------------------------------------------------------

def transition_to_kanban(project_dir: str) -> dict[str, Any]:
    """Convert Scrum state to Kanban state.

    Archives sprint history, carries over agent backends, resets to READY.
    """
    state = read_state(project_dir)

    # Calculate cumulative ticket count from all completed sprints.
    cumulative = sum(
        s.get("stories_completed", 0) for s in state.get("sprints_completed", [])
    ) + len(state.get("current_stories", []))

    kanban_state: dict[str, Any] = {
        "version": product_version(), "state_schema": 2,
        "build_mode": "kanban",
        "lifecycle_state": "READY",
        "started_at": _now(),
        "lifecycle_history": [
            {"state": "READY", "entered_at": _now(), "exited_at": None},
        ],
        "tickets_completed": [],
        "current_stories": [],
        "cumulative_ticket_number": cumulative,
        "agent_backends": state.get("agent_backends", {"default": "claude", "roles": {}}),
        "transitioned_from": {
            "build_mode": "scrum",
            "transitioned_at": _now(),
            "final_sprint": state.get("current_sprint", 0),
            "sprints_completed": state.get("sprints_completed", []),
            "process_log": state.get("process_log", []),
        },
    }

    _write_state(project_dir, kanban_state)
    return kanban_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python3 scrum_state_machine.py <action> <project_dir> [args]", file=sys.stderr)
        sys.exit(1)

    action = sys.argv[1]
    project_dir = sys.argv[2]
    args = sys.argv[3:]

    # Multi-spec: --spec=<id> (anywhere in args) routes every read/write to
    # the named spec's slot by setting SYNAPTORY_ACTIVE_SPEC for this process.
    # The flag is consumed so subcommand parsers below don't see it.
    args, spec_id_flag = _consume_spec_flag(args)
    if spec_id_flag:
        os.environ["SYNAPTORY_ACTIVE_SPEC"] = spec_id_flag

    try:
        if action == "init":
            backends_json = _parse_flag(args, "--backends", None)
            backends = json.loads(backends_json) if backends_json else None
            result = initialize(project_dir, backends)
            print(json.dumps(result, indent=2))

        elif action == "read":
            result = read_state(project_dir)
            print(json.dumps(result, indent=2))

        elif action == "transition":
            if not args:
                _die("Usage: transition <project_dir> <to_state> [--force]")
            to_state = args[0]
            force = "--force" in args
            if force:
                # Audit trail: record forced transition in state before executing.
                # This ensures skipped ceremonies are visible in history and reports.
                state = read_state(project_dir)
                current = state["lifecycle_state"]
                state.setdefault("process_log", []).append({
                    "event": "forced_transition",
                    "from": current,
                    "to": to_state,
                    "timestamp": _now(),
                    "warning": (
                        f"DISCIPLINE VIOLATION: transition {current} → {to_state} "
                        "was forced, bypassing ceremony sequence. "
                        "Use only for disaster recovery."
                    ),
                })
                _write_state(project_dir, state)
                print(
                    f"WARNING: --force used to bypass {current} → {to_state}. "
                    "This is recorded in process_log. "
                    "Use only for disaster recovery, not ceremony shortcuts.",
                    file=__import__("sys").stderr,
                )
            result = transition(project_dir, to_state, force)
            print(json.dumps(result, indent=2))

        elif action == "start_sprint":
            if not args:
                _die("Usage: start_sprint <project_dir> <sprint_num> --goal '...' --stories '[...]'")
            sprint_num = int(args[0])
            goal = _parse_flag(args, "--goal", "")
            stories_json = _parse_flag(args, "--stories", "[]")
            stories = json.loads(stories_json)
            result = start_sprint(project_dir, sprint_num, goal, stories)
            print(json.dumps(result, indent=2))

        elif action == "complete_sprint":
            result = complete_sprint(project_dir)
            print(json.dumps(result, indent=2))

        elif action == "close_sprint":
            proceed = _parse_flag(args, "--proceed-to", "SPRINT_PLANNING")
            result = close_sprint(project_dir, proceed)
            print(json.dumps(result, indent=2))

        elif action == "add_story":
            if not args:
                _die("Usage: add_story <project_dir> <story_id> [--title '...'] [--backends '{...}'] [--acceptance-criteria '[...]']")
            story_id = args[0]
            title = _parse_flag(args, "--title", "")
            backends_json = _parse_flag(args, "--backends", None)
            backends = json.loads(backends_json) if backends_json else None
            # #44: pass ACs through so create_story can snapshot them and
            # derive `ui_bearing` — a story whose UI signal lives ONLY in its
            # acceptance criteria (title has no UI verb) must still promote the
            # ui_acceptance gate. Dropping ACs here let such stories reach
            # `done` on backend-only receipts.
            acs_json = _parse_flag(args, "--acceptance-criteria", None)
            acceptance_criteria = json.loads(acs_json) if acs_json else None
            result = add_story(
                project_dir, story_id, title, backends,
                acceptance_criteria=acceptance_criteria,
            )
            print(json.dumps(result, indent=2))

        elif action == "transition_story":
            if len(args) < 2:
                _die(
                    "Usage: transition_story <project_dir> <story_id> <to_state> "
                    "[--reason '...'] [--force-recovery]"
                )
            story_id, to_state = args[0], args[1]
            reason = _parse_flag(args, "--reason", None)
            forced = "--force-recovery" in args
            from_state = str(
                (get_story(read_state(project_dir), story_id) or {}).get("state") or ""
            )
            refused = _cli_receipt_gated_refusal(
                from_state,
                to_state,
                story_id,
                forced=forced,
                reason=reason,
                project_dir=project_dir,
            )
            if refused:
                _die(refused)
            result = transition_story(project_dir, story_id, to_state, reason)
            print(json.dumps(result, indent=2))

        elif action == "evaluate_dod":
            if not args:
                _die("Usage: evaluate_dod <project_dir> <story_id>")
            result = evaluate_story_dod(project_dir, args[0])
            print(json.dumps(result, indent=2))

        elif action == "next_action":
            # Loop Engineering P1 (epic #75): deterministic dispatch. The
            # sprint-execution loop calls this at the top of every
            # iteration and executes the returned action.
            print(json.dumps(next_action(project_dir), indent=2))

        elif action == "request_acceptance":
            if not args:
                _die("Usage: request_acceptance <project_dir> <story_id>")
            result = request_acceptance(project_dir, args[0])
            print(json.dumps(result, indent=2))

        elif action == "accept_story":
            if len(args) < 2:
                _die("Usage: accept_story <project_dir> <story_id> --accepted-by '<email>'")
            story_id = args[0]
            accepted_by = _parse_flag(args, "--accepted-by", None) or ""
            if not accepted_by:
                _die("--accepted-by is required for accept_story (audit-trail field)")
            result = accept_story(project_dir, story_id, accepted_by)
            print(json.dumps(result, indent=2))

        elif action == "reject_story":
            if len(args) < 1:
                _die(
                    "Usage: reject_story <project_dir> <story_id> "
                    "--reason needs-fix|redo|defer|cancel "
                    "--feedback '<text>' --rejected-by '<email>' "
                    "[--ac-change 'AC-01: ...' --ac-change ...]"
                )
            story_id = args[0]
            reason_class = _parse_flag(args, "--reason", None)
            feedback_text = _parse_flag(args, "--feedback", None)
            rejected_by = _parse_flag(args, "--rejected-by", None) or ""
            if not (reason_class and feedback_text and rejected_by):
                _die(
                    "reject_story requires --reason, --feedback, --rejected-by"
                )
            # Collect repeated --ac-change flags.
            ac_change: list[str] = []
            for i, a in enumerate(args):
                if a == "--ac-change" and i + 1 < len(args):
                    ac_change.append(args[i + 1])
            result = reject_story(
                project_dir, story_id, reason_class, feedback_text, rejected_by,
                acceptance_criteria_change=ac_change or None,
            )
            print(json.dumps(result, indent=2))

        elif action == "velocity":
            state = read_state(project_dir)
            v = calculate_velocity(state.get("sprints_completed", []))
            print(json.dumps({"velocity": v}))

        elif action == "summary":
            result = get_sprint_summary(project_dir)
            print(json.dumps(result, indent=2))

        elif action == "retro_check":
            result = determine_retro_eligibility(project_dir)
            print(json.dumps(result, indent=2))

        elif action == "transition_to_kanban":
            result = transition_to_kanban(project_dir)
            print(json.dumps(result, indent=2))

        else:
            _die(f"Unknown action: {action}")

    except ValueError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


def _parse_flag(args: list[str], flag: str, default: Any) -> Any:
    """Read `--flag value` or `--flag=value` from args; return string or default."""
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return default


def _consume_spec_flag(args: list[str]) -> tuple[list[str], str | None]:
    """Strip `--spec <id>` / `--spec=<id>` from args; return (clean_args, value)."""
    out: list[str] = []
    value: str | None = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--spec" and i + 1 < len(args):
            value = args[i + 1]
            i += 2
            continue
        if a.startswith("--spec="):
            value = a.split("=", 1)[1]
            i += 1
            continue
        out.append(a)
        i += 1
    return out, value


def _die(msg: str) -> None:
    print(msg, file=sys.stderr)
    sys.exit(1)

def verify_only_units(project_dir: str, state: dict) -> "frozenset[str]":
    """Queued Work Units whose deliverable came from outside and binds (#495 P2).

    HERE RATHER THAN IN `story_pipeline.next_action`, which has no project_dir
    and stays pure: recognising a verify-only unit means reading its intake
    record off disk, and this module is where the other project reads live.

    CHEAP PROBE FIRST, and deliberately. `next_action` runs on every ceremony
    turn, so a full intake read per candidate would put a file parse plus a
    possible control-plane round trip on the path that answers "what next". The
    record's absence is the common case and `os.path.exists` settles it.

    QUEUED ONLY. A unit past `queued` has a producing stage behind it whatever
    its intake record says, and re-selecting a verifier for it would contradict
    the board.

    NOT `admitted` ALONE, and not `binds` alone either. A unit whose admission
    no longer yields a candidate (substituted artifact, rewritten record, a
    producing receipt in the way) is not a verify-only job with a problem, it
    is a unit whose subject cannot be established: selecting a verifier there
    would invite a check against bytes the platform has already refused to
    name, so it falls back to `dispatch_se` and stays unbacked with intake's
    own reason. But `binds` alone deadlocks, because it requires the very
    verifying receipt this dispatch exists to produce. So the predicate is
    "binds, or is intact and simply unverified".
    """
    import os

    try:
        import external_intake as _ei
    except Exception:  # noqa: BLE001 - no intake module is no verify-only unit
        return frozenset()
    found = []
    for story in (state.get("current_stories") or []):
        if not isinstance(story, dict):
            continue
        if str(story.get("state") or story.get("status") or "") != "queued":
            continue
        unit_id = str(story.get("id") or "")
        if not unit_id:
            continue
        try:
            if not os.path.exists(_ei.record_path(str(project_dir), unit_id)):
                continue
            read = _ei.read_intake(str(project_dir), unit_id)
            # `binds` OR `awaiting_verification`. Requiring `binds` alone was a
            # deadlock: `binds` needs a verifying receipt, and the dispatch this
            # selection authorizes is what produces one, so the verifier could
            # never be dispatched precisely because it had not run yet.
            if read.binds or read.awaiting_verification:
                found.append(unit_id)
        except Exception:  # noqa: BLE001 - an unreadable intake is not verify-only
            continue
    return frozenset(found)


if __name__ == "__main__":
    main()
