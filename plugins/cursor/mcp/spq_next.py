#!/usr/bin/env python3
"""SPQ ceremony next_action enrichment for the Cursor MCP adapter.

Ported from plugin-codex so Cursor's `next_action` names the same ceremony
verbs (approve_baseline, declare_ready, dispatch_tw, …) instead of returning
raw `not_in_execution` / `await_sync`.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any


def configured_build_mode(project: Path) -> str:
    path = project / ".synaptory.yaml"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.split("#", 1)[0].strip()
            if stripped.startswith("build_mode:"):
                return stripped.split(":", 1)[1].strip().strip("\"'") or "scrum"
    except OSError:
        pass
    return "scrum"


def sync_boundary_next_action(
    project: Path, selected: dict[str, Any], flat_state: dict[str, Any]
) -> dict[str, Any]:
    """Expose readiness publication before entering Sync."""
    barrier = importlib.import_module("sync_barrier")
    config = barrier.load_spq_config(str(project))
    workstreams = [item for item in config.get("workstreams", []) if isinstance(item, dict)]
    current = barrier.current_branch(str(project))
    workstream = str(os.environ.get("SYNAPTORY_ACTIVE_SPEC") or "").strip()
    if not workstream:
        workstream = str(flat_state.get("_workstream_id") or "").strip()
    if not workstream and current:
        match = next(
            (str(item.get("id") or "") for item in workstreams if item.get("branch") == current),
            "",
        )
        workstream = match
    if not workstream and len(workstreams) == 1:
        workstream = str(workstreams[0].get("id") or "")
    if not workstream:
        return {
            **selected,
            "action": "sync_workstream_unknown",
            "operation": None,
            "human_gate_pending": True,
            "reason": (
                "all Work Units are terminal, but the current Sync workstream "
                "cannot be resolved from SYNAPTORY_ACTIVE_SPEC or the branch"
            ),
        }
    cycle = int(flat_state.get("current_cycle") or 0)
    workstream_config = next(
        (item for item in workstreams if str(item.get("id") or "") == workstream),
        {},
    )
    if workstream_config.get("integration"):
        readiness = barrier.status(str(project), cycle)
        summary = {
            "workstream": workstream,
            "ready_count": int(readiness.get("ready_count") or 0),
            "quorum": int(readiness.get("quorum") or 0),
        }
        if not readiness.get("records_present"):
            return {
                **selected,
                **summary,
                "action": "await_sync",
                "operation": None,
                "human_gate_pending": True,
                "reason": (
                    "integration seat is waiting for Cycle %s delivery "
                    "readiness (%s/%s)"
                    % (cycle, summary["ready_count"], summary["quorum"])
                ),
            }
        return {
            **selected,
            **summary,
            "action": "enter_sync",
            "operation": "enter_sync",
            "human_gate_pending": True,
            "reason": (
                "all delivery readiness records are present; after a human "
                "confirms the integration branch and workstream heads, enter "
                "the guarded Sync ceremony"
            ),
        }
    record = project / barrier.record_relpath(config, cycle, workstream)
    if not record.is_file():
        return {
            **selected,
            "action": "declare_ready",
            "operation": "declare_ready",
            "workstream": workstream,
            "human_gate_pending": False,
            "reason": (
                "all admitted Work Units are terminal; derive the Cycle %s "
                "readiness record for workstream %s through declare_sync_ready"
                % (cycle, workstream)
            ),
        }
    return {
        **selected,
        "action": "enter_sync",
        "operation": "enter_sync",
        "workstream": workstream,
        "readiness_path": str(record.relative_to(project)),
        "human_gate_pending": True,
        "reason": (
            "readiness exists; after a human commits and pushes the workstream "
            "head and readiness record, enter the guarded Sync ceremony"
        ),
    }


def lifecycle_next_action(project: Path) -> dict[str, Any]:
    """Deterministic ceremony action outside Work Unit execution."""
    module = importlib.import_module("spq_state_machine")
    state = module.read_state(str(project))
    lifecycle = str(state.get("lifecycle_state") or "DISCOVERY")
    cycle = int(state.get("current_cycle") or 0)
    base = {"lifecycle_state": lifecycle, "current_cycle": cycle, "story_id": None}
    if lifecycle == "DISCOVERY":
        return {
            **base,
            "action": "approve_baseline",
            "operation": "approve_baseline",
            "human_gate_pending": True,
            "reason": "Discovery baseline needs explicit delivery-owner approval",
        }
    if lifecycle == "COMMIT":
        return {
            **base,
            "action": "open_cycle",
            "operation": "open_cycle",
            "human_gate_pending": True,
            "reason": "admit the approved Cycle goal and Work Units",
        }
    if lifecycle == "SYNC":
        evaluation = state.get("sync_evaluation")
        current_tree_state = None
        try:
            import verification_cache

            current_tree_state = verification_cache.tree_state(str(project))
        except Exception:
            pass
        evaluation_is_current = bool(
            isinstance(evaluation, dict)
            and evaluation.get("cycle") == cycle
            and evaluation.get("tree_state")
            and evaluation.get("tree_state") == current_tree_state
        )
        if evaluation_is_current and evaluation.get("verdict") == "green":
            return {
                **base,
                "action": "clear_sync",
                "operation": "clear_sync",
                "human_gate_pending": True,
                "reason": (
                    "the current integration tree has a green five-criterion "
                    "Sync verdict; explicit human clearance is required"
                ),
            }
        previous = (
            "; blocking: " + ", ".join(evaluation.get("blocking") or [])
            if evaluation_is_current and isinstance(evaluation, dict)
            else ""
        )
        return {
            **base,
            "action": "evaluate_sync",
            "operation": "evaluate_sync",
            "human_gate_pending": False,
            "reason": (
                "collect merged workstreams and evaluate all five Sync criteria"
                + previous
            ),
        }
    if lifecycle == "CHECKPOINT":
        readiness = module.checkpoint_readiness(str(project), state=state)
        if not readiness["ready"]:
            return {
                **base,
                "action": "dispatch_tw",
                "role": "tw",
                "story_id": readiness["story_id"],
                "receipt_path": readiness["receipt_path"],
                "human_gate_pending": False,
                "reason": "; ".join(readiness["blocking"]),
            }
        return {
            **base,
            "action": "close_cycle",
            "operation": "close_cycle",
            "human_gate_pending": True,
            "reason": "Checkpoint evidence is valid; choose another Cycle or Acceptance",
        }
    if lifecycle == "ACCEPTANCE":
        readiness = module.acceptance_readiness(str(project), state=state)
        for abbrev in ("qe", "ce", "pe", "tw", "cr"):
            filename = "%s-%s.json" % (readiness["story_id"], abbrev)
            item = readiness["receipts"][abbrev]
            role_blocked = not item["present"] or not item["valid"] or any(
                filename in reason for reason in readiness["blocking"]
            )
            if role_blocked:
                return {
                    **base,
                    "action": "dispatch_%s" % abbrev,
                    "role": abbrev,
                    "story_id": readiness["story_id"],
                    "receipt_path": item["receipt_path"],
                    "human_gate_pending": False,
                    "reason": next(
                        (
                            reason
                            for reason in readiness["blocking"]
                            if filename in reason
                        ),
                        "missing valid %s" % filename,
                    ),
                }
        if not readiness["ready"]:
            return {
                **base,
                "action": "acceptance_blocked",
                "human_gate_pending": True,
                "reason": "; ".join(readiness["blocking"]),
                "acceptance": readiness,
            }
        return {
            **base,
            "action": "complete_release",
            "operation": "complete",
            "human_gate_pending": True,
            "reason": "all five Acceptance receipts and Cycle barriers are green",
            "acceptance": readiness,
        }
    if lifecycle == "COMPLETE":
        return {
            **base,
            "action": "complete",
            "human_gate_pending": False,
            "reason": "SPQ lifecycle is terminal",
        }
    return {**base, "action": "not_in_execution", "human_gate_pending": False}
