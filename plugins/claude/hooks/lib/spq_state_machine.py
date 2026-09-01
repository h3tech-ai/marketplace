#!/usr/bin/env python3
# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
"""SPQ lifecycle state machine (docs/spq-lifecycle-design.md §4).

7-state cyclic machine: DISCOVERY → COMMIT → CYCLE_EXECUTION → SYNC →
CHECKPOINT → (loop to COMMIT, or ACCEPTANCE) → COMPLETE.

SPQ is v2's default delivery method; this implements it as a third V1 lifecycle
alongside `scrum` and `kanban`, adding the one thing neither has: a cross-
workstream integration barrier (**Sync**). State lives at
`.synaptory/.orchestrator/pipeline-state.json` and story sub-state management is
delegated to `story_pipeline.py` unchanged (§7.1).

THE FILENAME IS LOAD-BEARING. `loop_engine.py` builds the state-machine path as
`f"{build_mode}_state_machine.py"` and embeds it in the continuation reason it
hands the orchestrator, so `build_mode: spq` requires exactly this filename AND
a `main()` CLI with verb parity to `scrum_state_machine.py`.

TWO INVARIANTS THIS FILE EXISTS TO HOLD:

1. `CYCLE_EXECUTION → CHECKPOINT` is illegal. Integration must precede
   demonstration (§4.1): a Checkpoint that demos from an un-integrated branch
   shows 1/N of the system and defers the integration debt. The only path from
   execution to demonstration runs through SYNC.

2. `sprint_complete` becomes `await_sync`. When every admitted Work Unit goes
   terminal, `story_pipeline.next_action` returns `sprint_complete`; passing
   that through would close the Cycle WITHOUT integrating (§10.2). This is the
   single most important behaviour in the file.

Vocabulary note (§7.1): the internal state field names are deliberately
`current_stories`, `depends_on`, `file_scope` etc. — the same names
`story_pipeline.next_action` reads. Renaming them to Cycle/Work-Unit vocabulary
would fork 144 KB of proven logic for cosmetics, so SPQ vocabulary is applied at
the orchestrator/prompt layer only.

CLI:
    python3 spq_state_machine.py init <project_dir> [--backends '{...}']
    python3 spq_state_machine.py read <project_dir>
    python3 spq_state_machine.py transition <project_dir> <to_state> [--force]
    python3 spq_state_machine.py open_cycle <project_dir> <n> --goal "..." --work-units '[...]' [--workstream ID]
    python3 spq_state_machine.py approve_baseline <project_dir> [--approved-by EMAIL]
    python3 spq_state_machine.py hydrate_cycle <project_dir> <n> --work-units '[...]' [--goal "..."]
    python3 spq_state_machine.py close_cycle <project_dir> [--proceed-to COMMIT|ACCEPTANCE] [--force]
    python3 spq_state_machine.py declare_sync_ready <project_dir> <n> [--workstream ID]
    python3 spq_state_machine.py evaluate_sync <project_dir> <n>
    python3 spq_state_machine.py clear_sync <project_dir> <n> [--cleared-by EMAIL]
    python3 spq_state_machine.py cut_work_unit <project_dir> <id> [--reason "..."]
    python3 spq_state_machine.py publish_event <project_dir> <id> --condition C [--sha SHA] [--evaluation JSON]
    python3 spq_state_machine.py add_story <project_dir> <story_id> [--title "..."]
    python3 spq_state_machine.py transition_story <project_dir> <story_id> <to_state> [--reason '...'] [--force-recovery]
    python3 spq_state_machine.py evaluate_dod <project_dir> <story_id>
    python3 spq_state_machine.py request_acceptance <project_dir> <story_id>
    python3 spq_state_machine.py accept_story <project_dir> <story_id> --accepted-by '<email>'
    python3 spq_state_machine.py reject_story <project_dir> <story_id> --reason <r> --feedback '<t>' --rejected-by '<email>'
    python3 spq_state_machine.py record_method_signal <project_dir> <kind> --summary '...' [--data '{...}']
    python3 spq_state_machine.py receipts_dir <project_dir>
    python3 spq_state_machine.py next_action <project_dir>
    python3 spq_state_machine.py summary <project_dir>
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import spq_manifest as _mf
import spq_paths as _paths
import spec_state as _ss
import state_store as _store
import sync_barrier as _barrier
import host_env

from story_pipeline import (
    create_story,
    transition_story as _sp_transition_story,
    unblock_story as _sp_unblock_story,
    evaluate_story_dod as _sp_evaluate_dod,
    ship_evaluated_dod as _sp_ship_evaluated_dod,
    dod_gate_block_reason,
    aggregate_sprint_dod,
    get_story,
    is_per_story_acceptance_enabled,
    verification_loops_active,
    list_stories_by_state,
    accept_story as _sp_accept_story,
    reject_story as _sp_reject_story,
    request_acceptance as _sp_request_acceptance,
    next_action as _sp_next_action,
    dep_context,
    dep_context as _sp_dep_context,
    _normalize_ac_texts as _sp_normalize_ac,
    dependency_gate_mode,
    story_dep_status as _sp_story_dep_status,
    parallelism_config,
    resolve_dod_tier,
    _resolve_receipts_dir,
    _cli_receipt_gated_refusal,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SPQ_STATES = [
    "DISCOVERY",
    "COMMIT",
    "CYCLE_EXECUTION",
    "SYNC",
    "CHECKPOINT",
    "ACCEPTANCE",
    "COMPLETE",
]

# There is deliberately NO CYCLE_EXECUTION → CHECKPOINT edge (§4.1). Retro is
# also deliberately absent as a state: SPQ has no retro event, so process
# learning folds into CHECKPOINT as MethodSignals (§12.2) rather than adding a
# state SYN-LIF-013 does not sanction.
SPQ_TRANSITIONS: dict[str, list[str]] = {
    "DISCOVERY": ["COMMIT"],
    "COMMIT": ["CYCLE_EXECUTION"],
    "CYCLE_EXECUTION": ["SYNC"],
    "SYNC": ["CHECKPOINT"],
    "CHECKPOINT": ["COMMIT", "ACCEPTANCE"],
    "ACCEPTANCE": ["COMPLETE"],
}

# The pseudo Work Unit ids §6.2 assigns to non-Work-Unit states, so their
# receipts satisfy `^[A-Z][A-Z0-9]*-\d+$` (receipt_validator.py:155).
PSEUDO_STORY_IDS = {
    "DISCOVERY": "DISCOVERY-0",
    "COMMIT": "CYCLE-{n}",
    "SYNC": "SYNC-{n}",
    "CHECKPOINT": "CHECKPOINT-{n}",
    "ACCEPTANCE": "ACCEPTANCE-{n}",
}

ACCEPTANCE_ROLES = {
    "qe": "quality-engineer",
    "ce": "compliance-engineer",
    "pe": "platform-engineer",
    "tw": "technical-writer",
    "cr": "code-reviewer",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# State File I/O  (mirrors scrum_state_machine)
# ---------------------------------------------------------------------------

def _pointer_path(project_dir: str) -> str:
    """`pipeline-state.json`, which SPQ keeps as a mode + identity POINTER.

    Deliberately retained rather than replaced. `advance_kernel.build_mode()`
    and `loop_engine._lifecycle_next_action()` both discover the lifecycle by
    reading `build_mode` out of this file, and `build_mode()`'s multi-spec
    fallback must keep working for scrum/kanban. A top-level
    `build_mode: spq` satisfies the primary read, so neither fail-closed
    dispatcher changes at all -- which is the last place to take risk.
    """
    return os.path.join(
        project_dir, ".synaptory", ".orchestrator", "pipeline-state.json"
    )


def read_pointer(project_dir: str) -> dict[str, Any]:
    return _store.read_json(_pointer_path(project_dir))


def _write_pointer(project_dir: str, identity: dict[str, Any]) -> None:
    """Record mode + identity, preserving anything else already in the file."""
    path = _pointer_path(project_dir)
    with _store.transaction(_paths.lock_for(path)):
        pointer = _store.read_json(path)
        pointer["version"] = "2.0"
        pointer["build_mode"] = "spq"
        spq = pointer.get("spq")
        if not isinstance(spq, dict):
            spq = {}
        spq.update({k: v for k, v in identity.items() if v is not None})
        pointer["spq"] = spq
        _store.write_json_atomic(path, pointer)


def identity(
    project_dir: str,
    *,
    cycle_id: str | None = None,
    workstream_id: str | None = None,
    require_workstream: bool = False,
    require_cycle: bool = False,
) -> _paths.Identity:
    """Resolve native SPQ identity. Never reads `SYNAPTORY_ACTIVE_SPEC`."""
    return _paths.resolve_identity(
        project_dir,
        cycle_id=cycle_id,
        workstream_id=workstream_id,
        state=read_pointer(project_dir),
        require_workstream=require_workstream,
        require_cycle=require_cycle,
    )


def _default_state() -> dict[str, Any]:
    return {
        "version": "2.0",
        "build_mode": "spq",
        "lifecycle_state": "DISCOVERY",
        "started_at": _now(),
        "lifecycle_history": [
            {"state": "DISCOVERY", "entered_at": _now(), "exited_at": None},
        ],
        "discovery": {"completed_at": None, "baseline_approved": False},
        # Cycle bookkeeping replaces sprint bookkeeping. `current_cycle` is the
        # single owner of N (§8.3) — everything derived from it (CYCLE-{N} /
        # SYNC-{N} receipt ids, .synaptory/sync/cycle-<N>/, the integration
        # branch) therefore has one upstream.
        "current_cycle": 0,
        "cycle_goal": None,
        "cycles_completed": [],
        "cycle_tracker_binding": None,
        "sync": None,
        # Field names preserved verbatim for story_pipeline (§7.1).
        "current_stories": [],
        "process_log": [],
        "method_signals": [],
        "agent_backends": {
            "default": host_env.default_agent_backend(),
            "roles": {},
        },
    }


# ---------------------------------------------------------------------------
# Legacy vocabulary migration (SPD-173)
# ---------------------------------------------------------------------------
# The SPQ delivery unit was called a "Slice" through v1.1.x. SPD-173 renamed it
# to "Cycle" because "slice" is reserved for the Synaptory Platform concept (a
# discipline plugged in through the public contract). State files, tracker
# bindings and lifecycle history written by an older plugin still carry the old
# spelling, so every read upgrades them in memory. Writes are always new-form,
# so a state file self-heals on the first transition after the upgrade.
#
# This is a read shim, not an accepted alias: nothing emits the old names.
# Receipt ids need no shim — `SLICE-{N}-{role}.json` receipts already ingested
# by the control plane stay as historical rows under their original id (there is
# no CHECK constraint on task ids, unlike the product-manager rename), so only
# forward-going ids become `CYCLE-{N}`. Remove this shim once no supported
# version writes the old state vocabulary.

_LEGACY_STATE = "SLICE_EXECUTION"
_LEGACY_KEYS = {
    "current_slice": "current_cycle",
    "slice_goal": "cycle_goal",
    "slices_completed": "cycles_completed",
    "slice_tracker_binding": "cycle_tracker_binding",
}


def _migrate_legacy_vocabulary(state: dict[str, Any]) -> dict[str, Any]:
    """Upgrade a pre-SPD-173 state dict in place: Slice -> Cycle.

    Returns the same dict. Safe to call on already-migrated state and on the
    multispec per-spec substates, which carry the same key names.
    """
    if not isinstance(state, dict):
        return state

    for old, new in _LEGACY_KEYS.items():
        if old in state:
            # A new-form key already present wins: never clobber current data
            # with a stale legacy copy left behind by a partial upgrade.
            state.setdefault(new, state[old])
            del state[old]

    if state.get("lifecycle_state") == _LEGACY_STATE:
        state["lifecycle_state"] = "CYCLE_EXECUTION"

    history = state.get("lifecycle_history")
    if isinstance(history, list):
        for entry in history:
            if isinstance(entry, dict) and entry.get("state") == _LEGACY_STATE:
                entry["state"] = "CYCLE_EXECUTION"

    return state


# The refusal a v3-backed SPQ project meets. It names the migration rather than
# reporting a generic "unrecognized format", because that is the difference
# between a project that is blocked with a runnable next step and one whose
# operator has to guess.
_V3_REFUSAL = (
    "This SPQ project is on the retired Multi-Spec (v3.0) state layout. SPQ "
    "now owns its own store with native cycle/workstream identity "
    "(#303/#304/#305). Migrate once, forward only:\n"
    "    python3 <plugin>/skills/_shared/scripts/migrate_spq_native.py "
    "--project-dir .\n"
    "Nothing is lost: Work Unit state, receipts and the anti-replay ledger are "
    "moved, and a snapshot is written to .synaptory/.migrations/ first."
)


# The lane a SINGLE-clone project stores its board under. Storage and
# attribution are separated deliberately: `resolve_identity` still refuses to
# default a workstream, because defaulting ATTRIBUTION would credit one lane's
# receipts and barrier records to another. But STORAGE must always have exactly
# one home, or the board splits between the pointer and an execution state with
# ambiguous precedence -- which is how a hydrated Cycle came to read back as
# "Cycle 0, lifecycle COMMIT".
SOLO_WORKSTREAM = "default"


def storage_workstream(ident: _paths.Identity) -> str:
    return ident.workstream_id or SOLO_WORKSTREAM


def _execution_state_path(project_dir: str, ident: _paths.Identity) -> str | None:
    if not ident.cycle_id:
        return None
    return _paths.execution_state_path(
        project_dir, ident.cycle_id, storage_workstream(ident)
    )


def read_state(project_dir: str, spec_id: str | None = None) -> dict[str, Any]:
    """Read SPQ Cycle state. Raises ValueError if this is not an SPQ project.

    `spec_id` is accepted and IGNORED. Kept in the signature for one minor so
    an in-field caller passing it gets its work done rather than a TypeError;
    it no longer selects anything, which is the #303 requirement.
    """
    _ = spec_id
    pointer = read_pointer(project_dir)

    # A v3 envelope means this project has not been migrated. Refuse with the
    # command rather than advancing on a state file we would half-understand.
    if pointer.get("version") == "3.0" or isinstance(pointer.get("specs"), dict):
        raise ValueError(_V3_REFUSAL)

    if not pointer:
        return _default_state()

    mode = str(pointer.get("build_mode") or "unknown")
    if mode != "spq":
        raise ValueError(
            f"State is not SPQ format (found: {mode}). Use "
            "scrum_state_machine.py or kanban_state_machine.py instead."
        )

    ident = _paths.resolve_identity(project_dir, state=pointer)
    path = _execution_state_path(project_dir, ident)
    if path:
        on_disk = _store.read_json(path)
        if on_disk:
            state = _migrate_legacy_vocabulary(on_disk)
            state["_cycle_id"] = ident.cycle_id
            state["_workstream_id"] = ident.workstream_id
            return state

    # Pointer exists but no Cycle is hydrated in this checkout yet. The
    # pre-execution lifecycle (DISCOVERY, COMMIT) lives in the pointer itself,
    # so an un-hydrated clone still has a readable state instead of looking
    # like a fresh project.
    state = _migrate_legacy_vocabulary(dict(pointer))
    state.pop("spq", None)
    for key, value in _default_state().items():
        state.setdefault(key, value)
    state["_cycle_id"] = ident.cycle_id
    state["_workstream_id"] = ident.workstream_id
    return state


def _write_state(project_dir: str, state: dict[str, Any]) -> None:
    """Persist Cycle state, plus the mode/identity pointer.

    Two writes rather than one because they answer different questions: the
    pointer answers "what lifecycle is this checkout on, and which Cycle and
    workstream", which the fail-closed dispatchers read; the execution state
    answers "what is on the board", which only SPQ reads.
    """
    clean = {k: v for k, v in state.items() if not k.startswith("_")}
    cycle_id = state.get("_cycle_id")
    workstream_id = state.get("_workstream_id")
    if not cycle_id:
        ident = _paths.resolve_identity(project_dir, state=read_pointer(project_dir))
        cycle_id, workstream_id = ident.cycle_id, ident.workstream_id

    _write_pointer(
        project_dir,
        {
            "cycle_id": cycle_id,
            "cycle_seq": clean.get("current_cycle"),
            "workstream_id": workstream_id,
        },
    )

    if cycle_id:
        path = _paths.execution_state_path(
            project_dir, cycle_id, workstream_id or SOLO_WORKSTREAM
        )
        with _store.transaction(_paths.lock_for(path)):
            _store.write_json_atomic(path, clean)
        # Strip the lifecycle fields the pre-COMMIT phase left in the pointer.
        # Leaving them makes the pointer a second, stale copy of the board, and
        # then which one wins is a coin toss.
        pointer_path = _pointer_path(project_dir)
        with _store.transaction(_paths.lock_for(pointer_path)):
            pointer = _store.read_json(pointer_path)
            for key in list(pointer):
                if key not in ("version", "build_mode", "spq",
                               "migrated_from_multispec", "migrated_at"):
                    pointer.pop(key)
            _store.write_json_atomic(pointer_path, pointer)
        return

    # No Cycle yet (pre-COMMIT). The pointer carries the lifecycle until
    # `open_cycle` establishes a Cycle identity.
    path = _pointer_path(project_dir)
    with _store.transaction(_paths.lock_for(path)):
        pointer = _store.read_json(path)
        pointer.update(clean)
        pointer["version"] = "2.0"
        pointer["build_mode"] = "spq"
        _store.write_json_atomic(path, pointer)


def _is_declared_integration(project_dir: str, workstream_id: str | None) -> bool:
    """Is this id declared `integration: true` in `.synaptory.yaml`?

    Config, not state. `_is_integration_seat` resolves through the execution
    state, which during hydration is exactly the thing not written yet -- so the
    seat could not be recognised at the moment recognition mattered.

    Why the exemption exists at all: the SPQ lifecycle ALREADY treats an empty
    board on this seat as legitimate everywhere else. `transition` to SYNC
    permits it in as many words ("An empty board here is the ready-to-barrier
    state, not `nothing was done`"), `open_cycle` gives the seat
    `_integration_cycle_units` and an empty `current_stories`, `next_action`
    returns `await_sync` for it, and `_integration_readiness_rollup` exists to
    describe it. `hydrate_cycle` was the one path that refused, and because it
    refused the seat never reached CYCLE_EXECUTION, therefore never SYNC --
    while `spq/sync.md` and `clear_sync` both require SYNC. The barrier seat
    could `evaluate` and never `clear`, so completing a Cycle meant
    transitioning a DELIVERY clone to SYNC and clearing from there: the exact
    inverse of why the seat exists, since no single delivery lane should be able
    to clear its own integration (#330 G7).

    The refusal's own rationale does not apply here. It warns that an empty
    Cycle "would make next_action return await_sync immediately and let this
    workstream declare readiness having delivered nothing" -- but `await_sync`
    IS the correct next action for this seat, and it declares no delivery
    readiness. It evaluates the quorum and clears the barrier.
    """
    if not workstream_id:
        return False
    try:
        for stream in _config_workstreams(project_dir):
            if str(stream.get("id")) == str(workstream_id):
                return bool(stream.get("integration"))
    except Exception:  # noqa: BLE001 - an unreadable config is not an integration seat
        return False
    return False


def _is_integration_seat(
    project_dir: str, state: dict[str, Any] | None = None
) -> bool:
    """True when this checkout is the barrier seat (`integration: true`)."""
    ws_id = None
    try:
        import spq_paths as _paths

        ident = _paths.resolve_identity(project_dir, state=state)
        ws_id = ident.workstream_id
    except Exception:
        if state:
            ws_id = state.get("_workstream_id") or state.get("_spec_id")
    if not ws_id:
        return False
    try:
        cfg = _barrier.load_spq_config(project_dir)
    except Exception:
        return False
    for stream in cfg.get("workstreams") or []:
        if str(stream.get("id")) == str(ws_id) and stream.get("integration"):
            return True
    return False


def _rollup_stories(
    project_dir: str, state: dict[str, Any]
) -> list[dict[str, Any]]:
    """Stories on this board plus delivery-workstream boards (integration rollup)."""
    stories = list(state.get("current_stories") or [])
    seen = {s.get("id") for s in stories if s.get("id")}

    def _absorb(extra: Any) -> None:
        if not isinstance(extra, list):
            return
        for story in extra:
            if not isinstance(story, dict):
                continue
            sid = story.get("id")
            if sid and sid not in seen:
                stories.append(story)
                seen.add(sid)

    try:
        full = _ss.read_full_state(project_dir) or {}
    except Exception:
        full = {}
    for sub in (full.get("specs") or {}).values():
        if isinstance(sub, dict):
            _absorb(sub.get("current_stories"))

    try:
        ident = _paths.resolve_identity(project_dir, state=state)
        if ident.cycle_id:
            ws_root = os.path.join(
                _paths.cycle_root(project_dir, ident.cycle_id), "workstreams"
            )
            if os.path.isdir(ws_root):
                for ws in os.listdir(ws_root):
                    path = _paths.execution_state_path(
                        project_dir, ident.cycle_id, ws
                    )
                    if os.path.isfile(path):
                        data = _store.read_json(path) or {}
                        _absorb(data.get("current_stories"))
    except Exception:
        pass

    # Integration never admits units onto its own board. After a merge the
    # barrier records (and `_integration_cycle_units`) are the honest count.
    if not stories:
        for sid in state.get("_integration_cycle_units") or []:
            if sid and sid not in seen:
                stories.append({"id": sid, "state": "done"})
                seen.add(sid)
    return stories


#: What a work-unit count is counted OVER (#334 G15).
#:
#: The Checkpoint rollup printed `work_units_reported: 6` (summed across every
#: workstream in the manifest) beside `cycles_completed: 2/2` (this seat only),
#: with nothing in the output saying they were different scopes. A reader cannot
#: tell that from two numbers side by side, so the scope is now recorded next to
#: the counts instead of living in the reader's head.
SCOPE_MANIFEST = "manifest"   # summed across the Cycle's workstreams
SCOPE_SEAT = "seat"           # this clone's board only


def _barrier_work_unit_tally(
    project_dir: str, state: dict[str, Any]
) -> dict[str, int] | None:
    """Sum admitted/done/cut from merged Sync readiness records.

    Integration's local board is empty by design, so Checkpoint rollup must
    read the barrier files the workstreams published — not the empty board.
    Used when records omit a cycle field (older barrier files).
    """
    cycle_n = state.get("current_cycle")
    if cycle_n is None:
        return None
    try:
        cfg = _barrier.load_spq_config(project_dir)
    except Exception:
        return None
    admitted = done = cut = 0
    found = False
    for stream in cfg.get("workstreams") or []:
        if stream.get("integration"):
            continue
        ws_id = stream.get("id")
        if not ws_id:
            continue
        rels = (
            _barrier.record_relpath(cfg, int(cycle_n), str(ws_id)),
            _barrier.record_relpath_legacy(cfg, int(cycle_n), str(ws_id)),
        )
        path = next(
            (os.path.join(project_dir, rel) for rel in rels if os.path.isfile(
                os.path.join(project_dir, rel)
            )),
            None,
        )
        if not path:
            continue
        try:
            rec = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        wu = rec.get("work_units") or {}
        admitted += int(wu.get("admitted") or 0)
        done += int(wu.get("done") or 0)
        cut += int(wu.get("cut") or 0)
        found = True
    if not found:
        return None
    return {"total": admitted + cut, "done": done, "cut": cut}


def _integration_readiness_rollup(
    project_dir: str, state: dict[str, Any]
) -> dict[str, Any] | None:
    """Summarize delivery boards from readiness records merged at Sync.

    The integration clone intentionally has an empty board, while every
    delivery clone owns a different native Cycle id and gitignored execution
    state. The merged readiness records are therefore the barrier-validated
    cross-clone source for admitted/done/cut and DoD summary counts.
    """
    cycle_n = int(state.get("current_cycle") or 0)
    if not cycle_n or not _is_integration_seat(project_dir, state):
        return None
    try:
        cfg = _barrier.load_spq_config(project_dir)
        workstreams = _barrier.quorum_workstreams(cfg)
    except Exception:
        return None

    admitted = done = cut = evaluated = passed = 0
    failed_stories: list[str] = []
    records = 0
    for stream in workstreams:
        ws_id = str(stream.get("id") or "")
        if not ws_id:
            continue
        path = Path(project_dir) / _barrier.record_relpath(cfg, cycle_n, ws_id)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if int(record.get("cycle") or 0) != cycle_n:
            continue
        records += 1
        units = record.get("work_units") or {}
        admitted += int(units.get("admitted") or 0)
        done += int(units.get("done") or 0)
        cut += int(units.get("cut") or 0)
        dod = record.get("dod") or {}
        evaluated += int(dod.get("stories_evaluated") or 0)
        passed += int(dod.get("stories_passed") or 0)
        failed = dod.get("checks_failed") or {}
        if isinstance(failed, dict):
            failed_stories.extend(str(key) for key in failed)

    if not records:
        return None
    failed_count = max(evaluated - passed, 0)
    rate = round(passed / evaluated, 4) if evaluated else 0.0
    return {
        "work_units_total": admitted,
        "work_units_done": done,
        "work_units_cut": cut,
        "dod": {
            "total_stories": admitted,
            "stories_evaluated": evaluated,
            "stories_passed": passed,
            "stories_failed": failed_count,
            "critical_pass_rate": rate,
            "overall_pass_rate": rate,
            "failed_stories": sorted(set(failed_stories)),
        },
    }


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def initialize(
    project_dir: str,
    agent_backends: dict[str, Any] | None = None,
    spec_id: str | None = None,
    workstream_id: str | None = None,
) -> dict[str, Any]:
    """Initialize SPQ state at DISCOVERY, in SPQ's own store.

    Was multi-spec-shaped: it authored a `{"version":"3.0", "specs":{}}`
    envelope itself and hard-failed a v2 file with "Run migrate_to_multispec.py
    first" -- a migration demand produced by following the documented flow. SPQ
    now owns `.synaptory/.orchestrator/spq/` and `pipeline-state.json` is a
    two-key mode + identity pointer, so there is no envelope to author and no
    version to collide with.

    `spec_id` is accepted and IGNORED (one minor, so an in-field caller does not
    get a TypeError). Pass `workstream_id` to pin this checkout's lane; the pin
    file is written so no shell has to re-export anything.
    """
    _ = spec_id
    state = _default_state()
    if agent_backends:
        state["agent_backends"] = agent_backends
    # Persist whatever identity resolved, however it arrived. Same rule as
    # `hydrate_cycle`: resolving without persisting leaves identity living in
    # one shell's environment, which is precisely how `SYNAPTORY_ACTIVE_SPEC`
    # produced permanently unattributed work.
    resolved = workstream_id or _paths.resolve_identity(
        project_dir, workstream_id=workstream_id
    ).workstream_id
    if resolved:
        _paths.write_pin(project_dir, resolved)
    _write_state(project_dir, state)
    ident = identity(project_dir)
    state["_cycle_id"] = ident.cycle_id
    state["_workstream_id"] = ident.workstream_id
    return state


def transition(
    project_dir: str,
    to_state: str,
    force: bool = False,
    approved_by: str | None = None,
) -> dict[str, Any]:
    """Transition the lifecycle. Raises ValueError on an illegal transition.

    SYNC → CHECKPOINT carries a hard gate, deliberately mirroring scrum's
    SPRINT_REVIEW → SPRINT_RETRO TW-report guard: the barrier must have
    recorded a GREEN verdict for the current Cycle. Without this the state
    machine would let an orchestrator walk past the barrier and demo from an
    unintegrated tree, which is the entire failure SPQ exists to prevent.
    `--force` is the documented operator escape hatch.
    """
    state = read_state(project_dir)
    current = state["lifecycle_state"]

    if current == "COMPLETE":
        raise ValueError("Cannot transition from COMPLETE — lifecycle is finished")
    if to_state not in SPQ_STATES:
        raise ValueError(f"Invalid state: {to_state}. Must be one of {SPQ_STATES}")
    if not force and to_state not in SPQ_TRANSITIONS.get(current, []):
        extra = ""
        if current == "CYCLE_EXECUTION" and to_state == "CHECKPOINT":
            extra = (
                " — integration must precede demonstration (§4.1); the only "
                "path from execution to Checkpoint runs through SYNC"
            )
        raise ValueError(
            f"Illegal transition: {current} → {to_state}. "
            f"Valid targets: {SPQ_TRANSITIONS.get(current, [])}{extra}"
        )

    if not force and current == "SYNC" and to_state == "CHECKPOINT":
        sync = state.get("sync") or {}
        cycle_n = state.get("current_cycle")
        if sync.get("verdict") != "green" or sync.get("cycle") != cycle_n:
            raise ValueError(
                f"Cannot leave SYNC for Cycle {cycle_n}: no green barrier "
                "verdict recorded. Run `sync_barrier.py evaluate` and then "
                "`clear_sync`. The barrier is all-or-nothing (§8.6) — a "
                "workstream that cannot make it cuts scope rather than "
                "merging late. Use --force only to recover a stuck barrier."
            )

    if not force and current == "CYCLE_EXECUTION" and to_state == "SYNC":
        stories = state.get("current_stories") or []
        unfinished = [
            story
            for story in stories
            if story.get("state") not in {"done", "cancelled"}
        ]
        # Integration never admits units locally (they live on delivery
        # workstreams). An empty board here is the ready-to-barrier state,
        # not "nothing was done".
        if unfinished or (
            not stories and not _is_integration_seat(project_dir, state)
        ):
            detail = ", ".join(
                f"{story.get('id')}={story.get('state')}" for story in unfinished[:6]
            ) or "no admitted Work Units"
            raise ValueError(
                "Cannot enter SYNC until every admitted Work Unit is done or "
                f"explicitly cut: {detail}"
            )

    if not force and current == "ACCEPTANCE" and to_state == "COMPLETE":
        readiness = acceptance_readiness(project_dir, state=state)
        if not readiness["ready"]:
            raise ValueError(
                "Cannot complete SPQ release: Acceptance evidence is blocked: "
                + "; ".join(readiness["blocking"])
            )

    now = _now()
    if state.get("lifecycle_history"):
        state["lifecycle_history"][-1]["exited_at"] = now
    state["lifecycle_state"] = to_state
    state.setdefault("lifecycle_history", []).append(
        {"state": to_state, "entered_at": now, "exited_at": None}
    )
    if to_state == "COMPLETE" and current == "ACCEPTANCE" and approved_by:
        state["release_approval"] = {
            "approved_by": approved_by,
            "approved_at": now,
        }
        state.setdefault("process_log", []).append(
            {
                "at": now,
                "event": "release_approved",
                "approved_by": approved_by,
            }
        )
    _write_state(project_dir, state)

    # §13.2 maps all four v3 gates onto SPQ transitions. Emitting only
    # spec_ready would leave three of the four permanently silent, so the
    # /overview Gate Queue would show an SPQ project as having never passed
    # Inception or shipped a release.
    if to_state == "COMMIT" and current == "DISCOVERY":
        _emit_gate("project_inception_approved", state, project_dir)
    if to_state == "COMMIT":
        _emit_gate("spec_ready_opened", state)
    elif to_state == "COMPLETE" and current == "ACCEPTANCE":
        _emit_gate("release_approved", state, project_dir)
    return state


def acceptance_readiness(
    project_dir: str, state: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Validate the fail-closed release evidence required by Acceptance.

    Acceptance is the final release gate, not a ceremonial state label.  A
    legal adjacency alone must never authorize ``ACCEPTANCE -> COMPLETE``:
    every closed Cycle must carry its green Sync verdict and the five release
    roles must have produced valid, non-failing receipts for the current
    ``ACCEPTANCE-{N}`` pseudo Work Unit.
    """
    state = state or read_state(project_dir)
    cycle_n = int(state.get("current_cycle") or 0)
    story_id = f"ACCEPTANCE-{cycle_n}"
    blocking: list[str] = []
    warnings: list[str] = []
    receipt_status: dict[str, dict[str, Any]] = {}

    def _metric_int(metrics: dict[str, Any], key: str) -> int:
        value = metrics.get(key, 0)
        if isinstance(value, bool):
            return 0
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    cycles = state.get("cycles_completed") or []
    if not cycles:
        blocking.append("no closed Cycles are recorded")
    for cycle in cycles:
        if not isinstance(cycle, dict):
            blocking.append("cycles_completed contains an invalid entry")
            continue
        number = cycle.get("cycle")
        if (cycle.get("sync") or {}).get("verdict") != "green":
            blocking.append(f"Cycle {number} has no recorded green Sync verdict")

    receipts_dir = Path(_resolve_receipts_dir(project_dir))
    from receipt_validator import validate_receipt  # type: ignore

    for abbrev, expected_role in ACCEPTANCE_ROLES.items():
        path = receipts_dir / f"{story_id}-{abbrev}.json"
        item: dict[str, Any] = {
            "role": expected_role,
            "receipt_path": str(path),
            "present": path.is_file(),
            "valid": False,
        }
        receipt_status[abbrev] = item
        if not path.is_file():
            blocking.append(f"missing {path.name}")
            continue
        validation = validate_receipt(str(path), project_dir)
        item["valid"] = validation.valid
        item["errors"] = list(validation.errors or [])
        item["warnings"] = list(validation.warnings or [])
        if not validation.valid:
            blocking.append(f"{path.name} is invalid: {validation.errors[0]}")
            continue
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            blocking.append(f"{path.name} is unreadable: {exc}")
            continue
        if receipt.get("story_id") != story_id:
            blocking.append(f"{path.name} story_id must be {story_id}")
        if receipt.get("role") != expected_role:
            blocking.append(f"{path.name} role must be {expected_role}")
        dispatches = state.get("lifecycle_dispatches") or {}
        active = (
            dispatches.get(f"{story_id}:{abbrev}")
            if isinstance(dispatches, dict)
            else None
        )
        expected_dispatch = (
            active.get("dispatch_id") if isinstance(active, dict) else None
        )
        if expected_dispatch and receipt.get("dispatch_id") != expected_dispatch:
            blocking.append(f"{path.name} does not match its active lifecycle dispatch")

        failed_commands = [
            command
            for command in receipt.get("verification_commands") or []
            if isinstance(command, dict) and command.get("exit_code") != 0
        ]
        if failed_commands:
            blocking.append(
                f"{path.name} has {len(failed_commands)} failed verification command(s)"
            )

        status = str(receipt.get("status") or "").strip().lower()
        verdict = str(receipt.get("verdict") or "").strip().lower()
        if status in {"blocked", "failed", "needs-work", "needs_work"}:
            blocking.append(f"{path.name} reports status {status}")
        if verdict in {"blocked", "failed", "reject", "rejected", "needs-work", "needs_work"}:
            blocking.append(f"{path.name} reports verdict {verdict}")

        metrics = receipt.get("metrics") or {}
        dod = receipt.get("story_dod") or {}
        if abbrev == "qe":
            if _metric_int(metrics, "tests_failed") > 0:
                blocking.append(f"{path.name} reports failing release tests")
            # Acceptance dispatches the five release roles sequentially.  A QE
            # receipt therefore cannot honestly attest checks owned by roles
            # that have not run yet (build/platform, compliance, and review).
            # Treat only QE-owned test evidence as a QE blocker; the remaining
            # checks are enforced by their role receipts below.  Otherwise the
            # first receipt deadlocks Acceptance before CE can be authorized.
            if dod.get("tests_pass") is False:
                blocking.append(
                    f"{path.name} reports failed release DoD check: tests_pass"
                )
        elif abbrev == "ce":
            if _metric_int(metrics, "findings_critical") > 0:
                blocking.append(f"{path.name} reports critical security findings")
            if dod.get("no_critical_findings") is False:
                blocking.append(
                    f"{path.name} reports failed release DoD check: no_critical_findings"
                )
        elif abbrev == "pe":
            if dod.get("build_succeeds") is False:
                blocking.append(
                    f"{path.name} reports failed release DoD check: build_succeeds"
                )
        elif abbrev == "cr":
            review = receipt.get("code_review") or {}
            nested_verdict = (
                str(review.get("verdict") or "").strip().lower()
                if isinstance(review, dict)
                else ""
            )
            review_verdict = verdict or nested_verdict
            approved_verdicts = {
                "approve",
                "approved",
                "pass",
                "passed",
                "green",
                "complete",
                "completed",
            }
            rejected_verdicts = {
                "blocked",
                "failed",
                "reject",
                "rejected",
                "needs-work",
                "needs_work",
            }
            if review_verdict in rejected_verdicts:
                blocking.append(
                    f"{path.name} reports review verdict {review_verdict}"
                )
            elif status != "complete" and review_verdict not in approved_verdicts:
                blocking.append(
                    f"{path.name} must report status complete or an approved review verdict"
                )
            if dod.get("code_reviewed") is False:
                blocking.append(
                    f"{path.name} reports failed release DoD check: code_reviewed"
                )

        warnings.extend(f"{path.name}: {warning}" for warning in validation.warnings or [])

    return {
        "ready": not blocking,
        "lifecycle_state": state.get("lifecycle_state"),
        "cycle": cycle_n,
        "story_id": story_id,
        "blocking": blocking,
        "warnings": warnings,
        "receipts": receipt_status,
    }


def checkpoint_readiness(project_dir: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate the current Cycle's required Technical Writer report."""
    state = state or read_state(project_dir)
    cycle_n = int(state.get("current_cycle") or 0)
    story_id = f"CHECKPOINT-{cycle_n}"
    path = Path(_resolve_receipts_dir(project_dir)) / f"{story_id}-tw.json"
    blocking: list[str] = []
    if not path.is_file():
        blocking.append(f"missing {path.name}")
    else:
        from receipt_validator import validate_receipt  # type: ignore

        validation = validate_receipt(str(path), project_dir)
        if not validation.valid:
            blocking.append(f"{path.name} is invalid: {validation.errors[0]}")
        else:
            receipt = json.loads(path.read_text(encoding="utf-8"))
            if receipt.get("story_id") != story_id:
                blocking.append(f"{path.name} story_id must be {story_id}")
            if receipt.get("role") != "technical-writer":
                blocking.append(f"{path.name} role must be technical-writer")
            failed = [
                command
                for command in receipt.get("verification_commands") or []
                if isinstance(command, dict) and command.get("exit_code") != 0
            ]
            if failed:
                blocking.append(f"{path.name} has failed verification commands")
            dispatches = state.get("lifecycle_dispatches") or {}
            active = (
                dispatches.get(f"{story_id}:tw")
                if isinstance(dispatches, dict)
                else None
            )
            expected_dispatch = (
                active.get("dispatch_id") if isinstance(active, dict) else None
            )
            if expected_dispatch and receipt.get("dispatch_id") != expected_dispatch:
                blocking.append(f"{path.name} does not match its active lifecycle dispatch")
    return {
        "ready": not blocking,
        "cycle": cycle_n,
        "story_id": story_id,
        "receipt_path": str(path),
        "blocking": blocking,
    }


def get_lifecycle_state(project_dir: str) -> str:
    return read_state(project_dir).get("lifecycle_state", "DISCOVERY")


def _project_slug(project_dir: str | None = None) -> str:
    """Best-effort project identity for project-scoped gates."""
    return (
        os.environ.get("SYNAPTORY_PROJECT_ID")
        or os.path.basename(os.path.abspath(project_dir or os.getcwd()))
        or "unknown"
    )


def _emit_gate(
    kind: str, state: dict[str, Any], project_dir: str | None = None
) -> None:
    """Best-effort gate emission. §13.2: SPQ adds no NEW gate types, it maps
    its events onto the existing four. Never raises — a lifecycle transition
    must not depend on the control plane being reachable."""
    who = os.environ.get("SYNAPTORY_UPN") or ""
    try:
        if kind == "spec_ready_opened":
            from gate_emitter import emit_spec_ready_opened

            cycle_n = state.get("current_cycle", 0)
            emit_spec_ready_opened([f"CYCLE-{cycle_n}"], project_dir=project_dir)
        elif kind == "project_inception_approved":
            from gate_emitter import emit_project_inception_approved

            emit_project_inception_approved(
                _project_slug(project_dir), who, project_dir=project_dir
            )
        elif kind == "release_approved":
            from gate_emitter import emit_release_approved

            cycle_n = state.get("current_cycle", 0)
            emit_release_approved(
                f"ACCEPTANCE-{cycle_n}", who, project_dir=project_dir
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Cycle management
# ---------------------------------------------------------------------------

def open_cycle(
    project_dir: str,
    cycle_number: int | None,
    goal: str,
    work_units: list[dict[str, Any]],
    tracker_cycle: int | None = None,
    manifest: dict[str, Any] | None = None,
    cycle_id: str | None = None,
    workstream_id: str | None = None,
) -> dict[str, Any]:
    """Admit Work Units and open a Cycle. Must be in COMMIT.

    §8.3: the opening clone's state owns N, allocated here and monotonic.
    `tracker_cycle` records the Cycle ↔ Linear-cycle binding at admission time
    so a cycle renamed or renumbered in the tracker cannot silently repoint an
    existing Cycle — the tracker is a mirror, never the source of truth.

    Also allocates the native `cycle_id` (#303). `N` remains the human- and
    receipt-facing number; the id adds a hash because N collides across clones
    by design -- two clones can both open a Cycle 7 -- so an N match is not an
    identity match. Storage, branches and (from #305) parent manifests key on
    the id; `CYCLE-{N}` receipt ids keep keying on N, since
    `receipt_validator` will not accept a hashed suffix.
    """
    state = read_state(project_dir)
    if state["lifecycle_state"] != "COMMIT":
        raise ValueError(
            f"Cannot open a Cycle — lifecycle is {state['lifecycle_state']}, "
            "expected COMMIT"
        )

    previous = int(state.get("current_cycle") or 0)
    if cycle_number is None:
        cycle_number = previous + 1
    if cycle_number <= previous:
        raise ValueError(
            f"Cycle numbers are monotonic and owned by the integration state "
            f"(§8.3): current is {previous}, refusing to open {cycle_number}"
        )

    # A hydrating clone JOINS an existing Cycle, so its identity is supplied
    # rather than allocated. Allocating a fresh one here made the clone diverge
    # from the very Cycle it had just discovered and projected from -- the
    # manifest hashes then disagreed and the barrier correctly refused, which
    # looked like a barrier bug rather than an identity bug.
    cycle_id = cycle_id or state.get("_cycle_id")
    if not cycle_id or _paths.seq_of(cycle_id) != cycle_number:
        # A Cycle with this sequence may already exist in the repository -- the
        # integration clone opening "Cycle 1" is JOINING the Cycle the manifest
        # already describes, not starting a rival one. Two Cycle 1s in one
        # repository is not a state the barrier can reconcile: the records would
        # be filed under one identity and the manifest under the other.
        try:
            cycle_id = _cycle_id_for_seq(project_dir, cycle_number)
        except HydrationRefusal:
            cycle_id = None
    if not cycle_id or _paths.seq_of(cycle_id) != cycle_number:
        cycle_id = _paths.new_cycle_id(
            cycle_number,
            baseline_sha=str((state.get("discovery") or {}).get("baseline_sha") or ""),
            goal=goal,
            created_at=_now(),
            project_slug=os.path.basename(os.path.abspath(project_dir)),
        )
    # `workstream_id=` is the CALLER'S explicit lane and must win. Resolving
    # bare `identity(project_dir)` here let `SYNAPTORY_WORKSTREAM` beat an
    # explicit `--workstream`: `hydrate_cycle` wrote the pin from the flag at
    # :2048 and this line overwrote it from the environment, so the clone
    # believed it was a different lane from the work it held. `declare-ready`
    # without `--workstream` then declared as the wrong lane and the quorum
    # never saw the real one (#328 G1). `resolve_identity` already ranks
    # flag > env > pin > state; the bug was never passing the flag.
    ident = identity(project_dir, workstream_id=workstream_id)
    if ident.workstream_id:
        _paths.write_pin(project_dir, ident.workstream_id)
    state["_cycle_id"] = cycle_id
    state["_workstream_id"] = ident.workstream_id
    state["current_cycle"] = cycle_number
    state["cycle_goal"] = goal
    state["sync"] = None  # a new Cycle invalidates the previous verdict
    state["cycle_tracker_binding"] = {
        "cycle": cycle_number,
        "tracker_cycle": tracker_cycle,
        "bound_at": _now(),
    }
    if _is_integration_seat(project_dir, state):
        # Units live on delivery workstream boards. Putting them here made
        # next_action dispatch_se on the integration clone.
        state["current_stories"] = []
        state["_integration_cycle_units"] = [
            wu.get("id") for wu in work_units if wu.get("id")
        ]
    else:
        state["current_stories"] = [
            create_story(
                wu["id"],
                wu.get("title", ""),
                wu.get("backends"),
                acceptance_criteria=wu.get("acceptance_criteria"),
                kind=wu.get("kind", ""),
                labels=wu.get("labels"),
                depends_on=wu.get("depends_on"),
                file_scope=wu.get("file_scope"),
            )
            for wu in work_units
        ]

    now = _now()
    if state.get("lifecycle_history"):
        state["lifecycle_history"][-1]["exited_at"] = now
    state["lifecycle_state"] = "CYCLE_EXECUTION"
    state.setdefault("lifecycle_history", []).append(
        {"state": "CYCLE_EXECUTION", "entered_at": now, "exited_at": None}
    )
    # Index BEFORE the state write, so a crash between the two leaves a
    # recorded-but-unhydrated Cycle (recoverable) rather than a hydrated Cycle
    # the index has never heard of (which `resolve_identity` could not find).
    _paths.record_cycle(
        project_dir,
        cycle_id,
        seq=cycle_number,
        goal=goal,
        opened_at=now,
    )

    # A hydrating workstream passes the ALREADY-SEALED manifest through rather
    # than letting one be authored from its projection: the projection holds
    # only this lane's units, so re-authoring would produce a different hash and
    # collide with the manifest every other lane is already on.
    manifest = seal_manifest(
        project_dir,
        cycle_id=cycle_id,
        cycle_seq=cycle_number,
        goal=goal,
        work_units=work_units,
        baseline_sha=str((state.get("discovery") or {}).get("baseline_sha") or ""),
        created_at=now,
        tracker_cycle=tracker_cycle,
        manifest=manifest,
        workstream_id=ident.workstream_id,
    )
    if manifest:
        state["manifest_hash"] = manifest.get("manifest_hash")
        state["manifest_revision"] = manifest.get("manifest_revision")

    _write_state(project_dir, state)

    try:
        from gate_emitter import emit_spec_ready_opened

        emit_spec_ready_opened(
            [wu["id"] for wu in work_units], project_dir=project_dir
        )
    except Exception:
        pass
    return state


def _is_git_repo(project_dir: str) -> bool:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=project_dir,
            capture_output=True,
            timeout=15,
        )
    except Exception:  # noqa: BLE001
        return False
    return result.returncode == 0


def transport_ignored(project_dir: str, relpath: str) -> bool:
    """Is this committed-transport path excluded by .gitignore?

    The cross-clone transport only works if it is TRACKED. The un-ignore rule
    (`.synaptory/*` plus `!.synaptory/cycles/`) lives in a runbook, three prompts
    and some fixtures -- nothing has ever checked it. A project that skipped that
    line gets a manifest that writes cleanly, reads back cleanly, and is invisible
    to every other clone: the parent looks fine locally while the children see
    nothing. That is the same class of failure as "a fresh clone could not
    discover the Cycle at all", which #303 found by hitting it.

    Returns False when git cannot answer, so a non-repo or a broken git is never
    read as a verdict.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", "--", relpath],
            cwd=project_dir,
            capture_output=True,
            timeout=15,
        )
    except Exception:  # noqa: BLE001
        return False
    # 0 = ignored, 1 = not ignored, 128 = not a repo / other error.
    return result.returncode == 0


def _git_head(project_dir: str) -> str:
    """The current commit, or "" outside a repo / before the first commit."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:  # noqa: BLE001 - no git binary
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _config_workstreams(project_dir: str) -> list[dict[str, Any]]:
    """Workstream topology from `.synaptory.yaml`, via the barrier's parser.

    One parser, not two: `sync_barrier.load_spq_config` already reads this block
    and the barrier's branch derivation depends on it, so a second reader here
    would be a second place for the topology to be understood differently.
    """
    try:
        import sync_barrier

        return list(sync_barrier.load_spq_config(project_dir).get("workstreams") or [])
    except Exception:  # noqa: BLE001
        return []


def _owner_from_labels(unit: dict[str, Any], known: set[str]) -> str:
    """Recover a unit's owning workstream from the shipped `ws:<id>` label.

    `spq/commit.md` already tells the PO to label admitted units `ws:<id>`, so
    this reads the convention that exists rather than inventing a second one and
    asking every project to re-annotate its backlog.
    """
    explicit = str(unit.get("owner_workstream") or "")
    if explicit:
        return explicit
    for label in unit.get("labels") or []:
        text = str(label)
        if text.startswith("ws:"):
            candidate = text[3:]
            if not known or candidate in known:
                return candidate
    return ""


def _observe_manifest(project_dir: str, manifest: dict[str, Any] | None) -> None:
    """Report a Cycle manifest to the control plane, and retry earlier failures.

    Two steps, because they cover different losses (#334 G8):

    - The direct emission reports the document IN HAND. It has to happen here
      and now for a revision, because `revise_manifest` overwrites the sealed
      file: once superseded, a revision that never shipped cannot be recovered
      from disk by any later sweep.
    - `resend_pending` then ships anything an earlier attempt could not. That is
      the retry the previous one-shot `try/except: pass` never had, and it also
      catches `_seal_manifest`'s two early returns, which never reached the old
      emission at all — so a hydrated workstream shipped nothing, despite every
      workstream holding the same seal and being meant to ship it.

    Never raises. Git holds the seal and the barrier reads it there; a reporting
    gap must not become a delivery one.
    """
    try:
        import manifest_emitter

        if manifest and manifest.get("manifest_hash"):
            manifest_emitter.emit_cycle_manifest(manifest, project_dir=project_dir)
        manifest_emitter.resend_pending(project_dir)
    except Exception:  # noqa: BLE001 - observation must never block delivery
        pass


def seal_manifest(
    project_dir: str,
    *,
    cycle_id: str,
    cycle_seq: int,
    goal: str,
    work_units: list[dict[str, Any]],
    baseline_sha: str = "",
    created_at: str = "",
    tracker_cycle: int | None = None,
    manifest: dict[str, Any] | None = None,
    workstream_id: str | None = None,
) -> dict[str, Any]:
    """Seal the manifest, then report it and anything still undelivered.

    A thin wrapper over `_seal_manifest`, whose signature it mirrors: the
    observation moved out of that function's final return path so it also covers
    the earlier `return manifest` (hydration) and `return existing` (re-open)
    paths, which previously shipped nothing (#334 G8). Reporting from three
    return sites would have been three chances to forget one.

    It runs after the seal and outside any try/finally: a refusal from
    `_seal_manifest` means there is no manifest to report, and that exception
    should propagate untouched.
    """
    sealed = _seal_manifest(
        project_dir,
        cycle_id=cycle_id,
        cycle_seq=cycle_seq,
        goal=goal,
        work_units=work_units,
        baseline_sha=baseline_sha,
        created_at=created_at,
        tracker_cycle=tracker_cycle,
        manifest=manifest,
        workstream_id=workstream_id,
    )
    _observe_manifest(project_dir, sealed)
    return sealed


def _seal_manifest(
    project_dir: str,
    *,
    cycle_id: str,
    cycle_seq: int,
    goal: str,
    work_units: list[dict[str, Any]],
    baseline_sha: str = "",
    created_at: str = "",
    tracker_cycle: int | None = None,
    manifest: dict[str, Any] | None = None,
    workstream_id: str | None = None,
) -> dict[str, Any]:
    """Author, validate, seal and publish the Cycle manifest (#303).

    One atomic step inside `open_cycle` rather than a separate verb, so the
    manifest and the execution state cannot diverge -- the same argument
    `approve_baseline` already makes for its own single write.

    `manifest=` accepts a pre-authored document (the richer Discovery/Commit
    output). Otherwise one is generated from the admitted unit JSON plus the
    configured workstream topology, which keeps the shipped `--work-units` path
    working with no verb rename and no prompt drift.

    Sealed 0o444: the hash is what makes tampering detectable, and a read-only
    file makes accidental tampering unlikely as well.
    """
    # A Cycle's baseline is the commit it started from. Resolve it rather than
    # requiring the caller to thread it through: the integration clone records
    # one at `approve_baseline`, but a workstream clone INHERITS the baseline
    # (it does not re-approve it), so on the shipped hydration path the only
    # place the fact exists is git.
    if not baseline_sha:
        baseline_sha = _git_head(project_dir)

    if not work_units and manifest is None:
        # An empty Cycle gets no manifest. `open_cycle`'s contract has always
        # permitted an empty admitted set (the integration clone executes no
        # units), and `validate` rightly refuses an empty manifest -- so the
        # resolution is that there is nothing to seal, not that the validator
        # bends. Emptiness is refused where it actually matters, in
        # `hydrate_cycle`, which is the path a workstream takes before it can
        # declare readiness.
        return {}

    ws_config = _config_workstreams(project_dir)
    known = {str(w.get("id")) for w in ws_config if w.get("id")}
    if not ws_config:
        # A single-clone Cycle still gets a manifest. Naming the workstream
        # after the resolved identity keeps ownership explicit rather than
        # blank, which `validate` would reject.
        solo = (
            identity(project_dir, workstream_id=workstream_id).workstream_id
            or SOLO_WORKSTREAM
        )
        ws_config = [{"id": solo, "shared_owner": True}]
        known = {solo}

    # Ownership, most explicit first: a declared `owner_workstream`, then the
    # shipped `ws:<id>` label convention, then THIS clone's own lane. The last
    # fallback is the contract the prompts already describe -- `hydrate_cycle`
    # is documented as taking "this workstream's admitted Work Units" -- so a
    # unit admitted here with no declared owner belongs to the lane admitting
    # it. Leaving it blank instead would make every unlabelled backlog fail
    # manifest validation, which is a correct rule enforced at the wrong place.
    ident = identity(project_dir, workstream_id=workstream_id)
    default_owner = (
        ident.workstream_id
        or (next(iter(known)) if len(known) == 1 else "")
    )
    # `default_owner` may legitimately be empty here: a Cycle whose every unit
    # declares `owner_workstream` (or carries a `ws:<id>` label) never needs a
    # fallback, and demanding an identity from such a caller would refuse a
    # perfectly well-specified manifest. So the check belongs at the point of
    # USE, below, where the offending unit can be named.
    if default_owner and default_owner not in known:
        # Reachable ONLY when the project declares a topology and this clone's
        # identity is not in it: the solo branch above sets `known` FROM the
        # identity, so there the two always agree. So this used to append an
        # undeclared lane to the HASH-SEALED manifest every single time it ran.
        #
        # `default` was merely the case #323 observed, via the solo sentinel.
        # The general bug is wider and was found by re-running the scaffold with
        # a hostile `SYNAPTORY_WORKSTREAM=wrong-lane` export, which sealed
        # `[api, web, cli, integration, wrong-lane]`. Sealed means permanent,
        # and a lane no clone will ever claim owns whatever was assigned to it.
        raise HydrationRefusal(
            "unknown_workstream",
            "this clone's workstream is %r, which the project does not declare "
            "(declared: %s). Opening a Cycle from here would seal %r into the "
            "manifest as a workstream, permanently, and no clone would ever "
            "claim it. Fix the pin with --workstream, or declare %r under "
            "spq.workstreams in .synaptory.yaml."
            % (default_owner, ", ".join(sorted(known)), default_owner,
               default_owner),
        )
    units = []
    for unit in work_units:
        record = dict(unit)
        owner = _owner_from_labels(unit, known) or default_owner
        if not owner:
            # The old code fell through to SOLO_WORKSTREAM and appended it as a
            # lane, injecting a phantom `default` workstream into the
            # HASH-SEALED manifest -- observed as `Cycle declares 5 workstreams
            # (api, cli, default, integration, web)` (#328 G12). Sealed means
            # permanent for that Cycle, and this unit was then owned by a lane
            # no clone would ever claim, so no one delivered it and
            # `manifest_closure` could not be satisfied.
            #
            # `SOLO_WORKSTREAM` is the right answer for a genuinely single-clone
            # Cycle -- handled above, where no topology is declared at all --
            # and never the right answer alongside real lanes.
            raise HydrationRefusal(
                "unresolved_workstream",
                "Work Unit %r has no owner: it declares no `owner_workstream`, "
                "carries no `ws:<id>` label, and this clone has no workstream "
                "identity to fall back on while the project declares %d "
                "workstreams (%s). Pass --workstream, export %s, run "
                "hydrate_cycle to write the pin, or label the unit. Defaulting "
                "would put a phantom lane in a hash-sealed manifest."
                % (unit.get("id") or "<unnamed>", len(known),
                   ", ".join(sorted(known)), _paths.ENV_WORKSTREAM),
            )
        record["owner_workstream"] = owner
        units.append(record)

    if manifest is None:
        cfg: dict[str, Any] = {}
        try:
            import sync_barrier

            cfg = sync_barrier.load_spq_config(project_dir)
        except Exception:  # noqa: BLE001
            cfg = {}
        integration_mode = str(cfg.get("integration_mode") or "at_sync")
        require_regression = bool(cfg.get("require_regression", True))
        # Incremental integration publishes an `integrated` event before the
        # final Sync barrier. When regression is enabled, that event must only
        # be publishable from a regression-green integration result. Leaving
        # this list empty makes a failed regression informational and therefore
        # turns the pre-merge quality gate into a fail-open path.
        merge_requires = (
            ["regression_green"]
            if integration_mode == "incremental" and require_regression
            else []
        )
        manifest = _mf.build(
            cycle_id=cycle_id,
            cycle_seq=cycle_seq,
            goal=goal,
            baseline_sha=baseline_sha,
            integration_ref=_barrier.integration_branch(
                cfg, cycle_seq, cycle_id=cycle_id
            ),
            workstreams=[
                {
                    "id": str(w.get("id")),
                    "branch": _barrier.branch_for(
                        cfg,
                        w,
                        cycle_id=cycle_id,
                        cycle_n=cycle_seq,
                    ),
                    "shared_owner": bool(w.get("shared_owner")),
                    "integration": bool(w.get("integration")),
                }
                for w in ws_config
                if w.get("id")
            ],
            work_units=units,
            shared_surfaces=[
                {
                    "path": str(path),
                    "owner_workstream": next(
                        (str(w.get("id")) for w in ws_config if w.get("shared_owner")),
                        "",
                    ),
                }
                for path in (cfg.get("shared_digest_paths") or [])
            ],
            verification={
                "require_regression": require_regression,
                "require_journey": bool(cfg.get("require_journey", True)),
                "regression_script": cfg.get("regression_script"),
                "journey_script": cfg.get("journey_script"),
                "digest_script": cfg.get("digest_script"),
            },
            # `at_sync` is the SHIPPED flow: workstream branches meet at the
            # human merge in §8.5 step 2, and there is no per-unit integration
            # event to demand. Incremental integration is the NEW capability
            # #303 §4 adds, so it is opt-in -- defaulting to it would make every
            # existing Cycle block at Sync on evidence it was never going to
            # produce.
            integration_policy={
                "mode": integration_mode,
                "merge_requires": merge_requires,
                "accept_unverified_events": bool(
                    cfg.get("accept_unverified_events")
                ),
            },
            tracker_binding={"cycle": cycle_seq, "tracker_cycle": tracker_cycle},
            project=os.path.basename(os.path.abspath(project_dir)),
            created_at=created_at or _now(),
            runner_id=identity(project_dir).runner_id,
        )

    # Require a baseline only where one can exist. Outside a git repo the
    # barrier is unreachable anyway (it works on branches and ancestry), so the
    # requirement would block Cycle open for no protection.
    # An already-sealed manifest handed in by hydration is authority, not a
    # proposal: validate it, but do not re-hash or re-publish it.
    if manifest.get("manifest_hash"):
        problems = _mf.validate(manifest, require_baseline=_is_git_repo(project_dir))
        if problems:
            raise ValueError(
                "refusing to open Cycle %s: the manifest is invalid.\n  - %s"
                % (cycle_id, "\n  - ".join(problems))
            )
        if not _mf.verify_hash(manifest):
            raise ValueError(
                "refusing to open Cycle %s from a manifest that does not match "
                "its own hash" % cycle_id
            )
        return manifest

    problems = _mf.validate(manifest, require_baseline=_is_git_repo(project_dir))
    if problems:
        raise ValueError(
            "refusing to open Cycle %s: the manifest is invalid.\n  - %s"
            % (cycle_id, "\n  - ".join(problems))
        )
    sealed = _mf.seal(manifest)

    local = _paths.manifest_path(project_dir, cycle_id)
    if os.path.exists(local):
        existing = _store.read_json(local)
        if existing.get("manifest_hash") != sealed.get("manifest_hash"):
            raise ValueError(
                "a sealed manifest already exists for Cycle %s with a different "
                "hash. Use revise_manifest to supersede it explicitly rather "
                "than overwriting: a silent replacement would leave every "
                "hydrated workstream on a manifest that no longer exists."
                % cycle_id
            )
        return existing
    _store.write_json_atomic(local, sealed, mode=0o444)

    # The committed copy is the cross-clone transport. Best-effort: the sealed
    # local copy is authoritative for THIS clone, and a read-only filesystem or
    # missing subtree must not fail the Cycle open.
    try:
        published = _paths.committed_manifest_path(project_dir, cycle_id)
        relpath = os.path.relpath(published, project_dir)
        # Only when the transport actually MATTERS. A single-lane Cycle never
        # reads the committed copy -- the local sealed manifest is authoritative
        # for the only clone there is -- so refusing there would break solo
        # projects to protect them from a problem they cannot have.
        multi_clone = len(sealed.get("workstreams") or []) > 1
        if multi_clone and transport_ignored(project_dir, relpath):
            raise ValueError(
                "refusing to open Cycle %s: %s is excluded by .gitignore, so the "
                "sealed manifest would never reach another clone. Every workstream "
                "would fail to discover the Cycle and the barrier would report "
                "missing records with no visible cause. Add the narrow un-ignore "
                "to .gitignore:\n\n    .synaptory/*\n    !.synaptory/sync/\n"
                "    !.synaptory/cycles/\n\nThe trailing `/*` is load-bearing: "
                "git cannot re-include a path whose parent directory is excluded."
                % (cycle_id, relpath)
            )
        os.makedirs(os.path.dirname(published), exist_ok=True)
        _store.write_json_atomic(published, sealed)
    except OSError:
        pass

    # Telling the control plane is `seal_manifest`'s job, not this function's:
    # every return path above reaches the caller, and only this one used to
    # reach the emission (#334 G8).
    return sealed


class HydrationRefusal(ValueError):
    """A refusal the operator must resolve, carrying a machine-readable code.

    Codes rather than prose because the operator action differs completely
    between them: re-hydrate, re-pull, fix an id, or stop and talk to the
    workstream that owns the unit.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _cycle_id_for_seq(project_dir: str, cycle_number: int) -> str | None:
    """The Cycle identity for a sequence number.

    Checks the local index first, then the resolved identity, then the COMMITTED
    transport. That last lookup is the point of the transport: a freshly cloned
    workstream has no index and no pointer -- both live under the gitignored
    orchestrator tree -- so the only way it can discover which Cycle it is
    joining is the manifest that travelled with the repository.
    """
    for entry in _paths.list_cycles(project_dir):
        if int(entry.get("seq") or -1) == int(cycle_number):
            return str(entry.get("cycle_id"))
    ident = identity(project_dir)
    if ident.cycle_id and ident.cycle_seq == int(cycle_number):
        return ident.cycle_id

    committed = os.path.join(project_dir, _paths.COMMITTED_RELDIR)
    if os.path.isdir(committed):
        candidates = []
        for name in sorted(os.listdir(committed)):
            try:
                if _paths.seq_of(name) != int(cycle_number):
                    continue
            except Exception:  # noqa: BLE001 - not a cycle id
                continue
            manifest = _store.read_json(
                os.path.join(committed, name, "manifest.json")
            )
            if manifest.get("cycle_id") == name:
                candidates.append((manifest.get("created_at") or "", name))
        if len(candidates) > 1:
            # Two Cycles with the same sequence number is precisely what the
            # hashed identity exists to make visible. Refusing beats guessing:
            # hydrating the wrong one admits another Cycle's Work Units.
            raise HydrationRefusal(
                "cycle_mismatch",
                "%d Cycles in this repository share sequence %d (%s). Pass "
                "--cycle-id to say which one this clone is joining."
                % (len(candidates), cycle_number,
                   ", ".join(sorted(c[1] for c in candidates))),
            )
        if candidates:
            return candidates[0][1]
    return None


def _projection_for(
    project_dir: str, cycle_number: int, workstream_id: str | None
) -> dict[str, Any] | None:
    """This workstream's validated projection, or None when no manifest exists.

    Every refusal below is a case where hydrating would produce a board that
    disagrees with the Cycle. Failing closed matters more here than anywhere
    else in SPQ: a workstream that hydrates a wrong admitted set will pass its
    own DoD, declare readiness, and only be caught at the barrier -- after the
    work is done.
    """
    cycle_id = _cycle_id_for_seq(project_dir, cycle_number)
    if not cycle_id:
        return None
    manifest = read_manifest(project_dir, cycle_id)
    if not manifest:
        return None

    if not _mf.verify_hash(manifest):
        raise HydrationRefusal(
            "manifest_tampered",
            "the manifest for Cycle %s does not match its own hash. Do not "
            "hydrate from it: a modified admitted set could license work the "
            "Cycle never admitted. Re-pull the Cycle's committed manifest."
            % cycle_id,
        )

    problems = _mf.validate(manifest, require_baseline=_is_git_repo(project_dir))
    if problems:
        raise HydrationRefusal(
            "manifest_invalid",
            "the manifest for Cycle %s is invalid:\n  - %s"
            % (cycle_id, "\n  - ".join(problems)),
        )

    if str(manifest.get("cycle_id")) != cycle_id:
        raise HydrationRefusal(
            "cycle_mismatch",
            "manifest declares Cycle %s but was found under %s"
            % (manifest.get("cycle_id"), cycle_id),
        )

    if not workstream_id:
        # With exactly one declared workstream there is no other lane whose
        # units could be admitted here, so the ambiguity the refusal guards
        # against cannot arise. Demanding an explicit id there would break the
        # single-clone flow for no protection.
        declared = [str(w.get("id")) for w in manifest.get("workstreams") or []]
        if len(declared) == 1:
            workstream_id = declared[0]
            _paths.write_pin(project_dir, workstream_id)
        else:
            raise HydrationRefusal(
                "unknown_workstream",
                "Cycle %s declares %d workstreams (%s), so hydration needs an "
                "explicit one: pass --workstream or export %s. Guessing would "
                "admit another lane's Work Units into this clone."
                % (cycle_id, len(declared), ", ".join(sorted(declared)),
                   _paths.ENV_WORKSTREAM),
            )

    try:
        projection = _mf.project(manifest, workstream_id)
    except _mf.ManifestError as exc:
        raise HydrationRefusal("unknown_workstream", str(exc))

    if not projection["work_units"] and not _is_declared_integration(
        project_dir, workstream_id
    ):
        raise HydrationRefusal(
            "empty_projection",
            "workstream %r owns no admitted Work Unit in Cycle %s. Opening an "
            "empty Cycle would make next_action return await_sync immediately "
            "and let this workstream declare readiness having delivered "
            "nothing." % (workstream_id, cycle_id),
        )

    # A local board already on a DIFFERENT manifest revision is stale. Adopting
    # a superseding revision is allowed and recorded; adopting an unrelated one
    # is refused, because it would silently swap the admitted set under work
    # that is already in flight.
    existing = _store.read_json(
        _paths.execution_state_path(project_dir, cycle_id, workstream_id)
    )
    local_hash = existing.get("manifest_hash")
    manifest_hash = manifest.get("manifest_hash")
    if local_hash and manifest_hash and local_hash != manifest_hash:
        if manifest.get("supersedes") != local_hash:
            raise HydrationRefusal(
                "stale_projection",
                "this clone is on manifest %s but Cycle %s now has %s, which "
                "does not supersede it. Two unrelated revisions cannot be "
                "reconciled automatically: re-pull the Cycle, or re-open it."
                % (local_hash, cycle_id, manifest_hash),
            )

    projection["_cycle_id"] = cycle_id
    _store.write_json_atomic(
        _paths.projection_path(project_dir, cycle_id, workstream_id), projection
    )
    return projection


def revise_manifest(
    project_dir: str,
    *,
    cycle_id: str | None = None,
    reason: str = "",
    drop_units: list[str] | None = None,
    add_units: list[dict[str, Any]] | None = None,
    revised_by: str = "",
) -> dict[str, Any]:
    """Supersede a sealed manifest with an explicit new revision.

    A separate verb, never an overwrite. `open_cycle` refuses to replace a
    sealed manifest with a different hash precisely because a silent
    replacement would leave every already-hydrated workstream on a manifest
    that no longer exists, with no way to discover that. Going through here
    records `supersedes: <prior hash>`, which is what lets a stale clone be
    told exactly what replaced its projection.
    """
    if not reason:
        raise ValueError(
            "revise_manifest requires --reason: a Cycle's admitted set changing "
            "mid-flight is a decision, and the audit trail should say whose."
        )
    cycle_id = cycle_id or identity(project_dir, require_cycle=True).cycle_id
    current = read_manifest(project_dir, cycle_id)
    if not current:
        raise ValueError(f"no sealed manifest to revise for Cycle {cycle_id}")
    if not _mf.verify_hash(current):
        raise ValueError(
            f"refusing to revise Cycle {cycle_id}: the current manifest does "
            "not match its own hash, so what would be superseded is unknown"
        )

    dropped = set(drop_units or [])
    units = [u for u in current.get("work_units") or [] if u.get("id") not in dropped]
    known = {str(w.get("id")) for w in current.get("workstreams") or []}
    for extra in add_units or []:
        record = dict(extra)
        record["owner_workstream"] = _owner_from_labels(extra, known) or (
            identity(project_dir).workstream_id or SOLO_WORKSTREAM
        )
        record["depends_on"] = [
            _mf.normalize_dep(d) for d in (record.get("depends_on") or [])
        ]
        units.append(record)

    revised = _mf.revise(
        current,
        {"work_units": units, "revision_reason": reason},
        revised_by=revised_by or identity(project_dir).runner_id,
    )
    problems = _mf.validate(revised, require_baseline=_is_git_repo(project_dir))
    if problems:
        raise ValueError(
            "refusing to revise Cycle %s: the result is invalid.\n  - %s"
            % (cycle_id, "\n  - ".join(problems))
        )

    local = _paths.manifest_path(project_dir, cycle_id)
    if os.path.exists(local):
        os.chmod(local, 0o644)
    _store.write_json_atomic(local, revised, mode=0o444)
    try:
        published = _paths.committed_manifest_path(project_dir, cycle_id)
        os.makedirs(os.path.dirname(published), exist_ok=True)
        _store.write_json_atomic(published, revised)
    except OSError:
        pass
    # A revision had NO producer at all: `seal_manifest` was the only emitter,
    # and this verb never called it. `cycle_manifests` is append-only precisely
    # so a revision is its own row, and none was ever written. Report it before
    # the next revision overwrites the file it would have to be recovered from.
    _observe_manifest(project_dir, revised)
    return revised


def publish_event(
    project_dir: str,
    *,
    unit_id: str,
    condition: str,
    cycle_id: str | None = None,
    output: dict[str, Any] | None = None,
    evaluation: dict[str, Any] | None = None,
    commit_sha: str = "",
    published_by: str = "",
) -> dict[str, Any]:
    """Record that a Work Unit reached a condition another lane can depend on.

    One verb, one write, three effects: verify the condition, write the
    committed event, and stamp `story["integration"]` for units this workstream
    owns. Splitting them would let an event exist for work the board does not
    show as integrated, or the reverse.
    """
    import spq_ledger

    ident = identity(project_dir, cycle_id=cycle_id, require_workstream=True,
                     require_cycle=True)
    manifest = read_manifest(project_dir, ident.cycle_id)
    if not manifest:
        raise ValueError(
            f"Cycle {ident.cycle_id} has no sealed manifest, so there is no "
            "admitted set an event could be about"
        )
    if not _mf.verify_hash(manifest):
        raise ValueError(
            f"refusing to publish against Cycle {ident.cycle_id}: the manifest "
            "does not match its own hash"
        )

    # An integration ref proves where a commit landed, not that the Work Unit
    # passed its Definition of Done. Without this producer-side lifecycle
    # check, the Cycle baseline itself can be published as ``integrated`` for a
    # queued unit and safely-looking evidence will unblock another workstream.
    if condition == "integrated":
        state = read_state(project_dir)
        unit = next(
            (
                story
                for story in (state.get("current_stories") or [])
                if str(story.get("id") or "") == str(unit_id)
            ),
            None,
        )
        if unit is None:
            raise ValueError(
                f"refusing to publish integrated for {unit_id}: the owned Work "
                "Unit is not present in this workstream's hydrated board"
            )
        if str(unit.get("state") or "") != "done":
            raise ValueError(
                f"refusing to publish integrated for {unit_id}: the Work Unit "
                f"must be done, currently {unit.get('state') or 'unknown'}"
            )

    result = spq_ledger.publish(
        project_dir,
        cycle_id=ident.cycle_id,
        manifest=manifest,
        workstream_id=ident.workstream_id,
        unit_id=unit_id,
        condition=condition,
        output=output,
        evaluation=evaluation,
        commit_sha=commit_sha,
        published_by=published_by,
        runner_id=ident.runner_id,
    )

    # `integrated` is the one condition that also changes what the local board
    # says about the unit. Modelled as an ORTHOGONAL sub-record rather than a
    # new STORY_STATES member: `done` is the DoD verdict, integration is a
    # separate axis, and a unit can legitimately be done-and-unintegrated or
    # integrated-then-reopened by a PO rejection. Folding an orthogonal axis
    # into a linear enum forces either a state explosion or lossy transitions,
    # and would break every `state == "done"` consumer in all three lifecycles.
    if condition == "integrated":
        state = read_state(project_dir)
        for story in state.get("current_stories") or []:
            if str(story.get("id")) == str(unit_id):
                story["integration"] = {
                    "status": "integrated",
                    "commit_sha": commit_sha,
                    "integration_ref": manifest.get("integration_ref"),
                    "event_id": result["event"]["event_id"],
                    "integrated_at": result["event"]["published_at"],
                }
                _write_state(project_dir, state)
                break
    return result


def integration_label(story: dict[str, Any]) -> str:
    """`done` | `integration_pending` | `integrated`, derived not stored.

    #303 §3 asks for state that "distinguishes implementation completion from
    integration completion, for example done, integration_pending, and
    integrated". Read literally as new `STORY_STATES` members that breaks every
    `state == "done"` consumer across all three lifecycles -- counts, terminal
    checks, `_STAGE_PRIORITY`, the DoD gate, `declare_ready`'s unfinished check,
    `cut_work_unit`, `state_drift`, the tracker transitions. Derived here
    instead: the requirement is met, only the literal enum members differ.
    """
    if str(story.get("state") or "") != "done":
        return str(story.get("state") or "")
    integration = story.get("integration") or {}
    status = str(integration.get("status") or "")
    if status == "integrated":
        return "integrated"
    return "integration_pending"


def dep_status(project_dir: str, unit_id: str | None = None) -> dict[str, Any]:
    """Operator view of every Work Unit's dependency resolution.

    #304's AC asks that an unknown dependency be "observable to the operator".
    A `deps_blocked` stop tells you the loop halted; this tells you which edge,
    why, who owns the upstream, and whether the event exists but has not been
    pushed yet -- which is the difference between waiting and acting.
    """
    import spq_ledger

    state = read_state(project_dir)
    context = _sp_dep_context(project_dir, state)
    ident = identity(project_dir)

    pending_push: list[str] = []
    if ident.cycle_id and ident.workstream_id:
        try:
            pending_push = spq_ledger.local_only_units(
                project_dir, ident.cycle_id, ident.workstream_id
            )
        except Exception:  # noqa: BLE001
            pending_push = []

    units = []
    for story in state.get("current_stories") or []:
        verdict = _sp_story_dep_status(state, story, dep_context=context)
        unmet = verdict["unmet"]
        # Producer side vs consumer side. Without the distinction, "I published
        # it" and "nobody can see it" look identical and the mandatory human
        # push reads as a failure.
        for entry in unmet:
            dep_unit = entry["dep"].get("unit_id")
            if dep_unit in pending_push:
                entry["reason_code"] = "dep_event_local_only"
                entry["detail"] = (
                    "the event for %s exists in this clone but is not pushed "
                    "yet, so no other workstream can see it. Commit and push "
                    "%s." % (dep_unit, ".synaptory/cycles/")
                )
        units.append({
            "unit_id": story.get("id"),
            "state": story.get("state"),
            "unit_status": integration_label(story),
            "dependencies_met": verdict["met"],
            "unmet": unmet,
        })

    if unit_id:
        units = [u for u in units if str(u["unit_id"]) == str(unit_id)]

    return {
        "cycle_id": ident.cycle_id,
        "workstream_id": ident.workstream_id,
        "manifest_present": context.get("manifest_present"),
        "manifest_hash": context.get("manifest_hash"),
        "ledger_fresh": context.get("ledger_fresh"),
        "events_pending_push": pending_push,
        "units": units,
    }


def refresh_ledger(
    project_dir: str, *, cycle_id: str | None = None, fetch: bool = True
) -> dict[str, Any]:
    """Rebuild the local dependency-ledger cache from git."""
    import spq_ledger

    ident = identity(project_dir, cycle_id=cycle_id, require_cycle=True)
    manifest = read_manifest(project_dir, ident.cycle_id)
    cache = spq_ledger.refresh(
        project_dir, ident.cycle_id, manifest=manifest or None, fetch=fetch
    )
    return {
        "cycle_id": ident.cycle_id,
        "refreshed_at": cache.get("refreshed_at"),
        "refresh_ok": cache.get("refresh_ok"),
        "refresh_detail": cache.get("refresh_detail"),
        "events": len(cache.get("events") or []),
    }


def read_manifest(project_dir: str, cycle_id: str) -> dict[str, Any]:
    """The sealed manifest, preferring the local copy then the committed one."""
    for path in (
        _paths.manifest_path(project_dir, cycle_id),
        _paths.committed_manifest_path(project_dir, cycle_id),
    ):
        found = _store.read_json(path)
        if found:
            return found
    return {}


def approve_baseline(
    project_dir: str, approved_by: str | None = None
) -> dict[str, Any]:
    """Record Discovery's baseline approval AND transition to COMMIT, atomically.

    Review finding P1 on #226: `spq/discovery.md` told the orchestrator to Edit
    `pipeline-state.json` to set `discovery.baseline_approved`, which boundary
    guard G2 DENIES in structured mode (and `test_g2_still_denies_pipeline_state_writes`
    pins that denial). The prompt's next step then ran `transition COMMIT`, which
    succeeds on its own — so the documented flow could land in COMMIT with
    `baseline_approved` still false. An approval gate that records nothing is not
    a gate.

    One command, one write, so the approval and the transition cannot diverge.
    """
    state = read_state(project_dir)
    if state["lifecycle_state"] != "DISCOVERY":
        raise ValueError(
            f"Cannot approve the baseline — lifecycle is "
            f"{state['lifecycle_state']}, expected DISCOVERY"
        )

    now = _now()
    discovery = dict(state.get("discovery") or {})
    discovery["baseline_approved"] = True
    discovery["completed_at"] = now
    discovery["approved_by"] = approved_by or os.environ.get("SYNAPTORY_UPN") or ""
    state["discovery"] = discovery

    if state.get("lifecycle_history"):
        state["lifecycle_history"][-1]["exited_at"] = now
    state["lifecycle_state"] = "COMMIT"
    state.setdefault("lifecycle_history", []).append(
        {"state": "COMMIT", "entered_at": now, "exited_at": None}
    )
    state.setdefault("process_log", []).append(
        {"at": now, "event": "baseline_approved",
         "approved_by": discovery["approved_by"]}
    )
    _write_state(project_dir, state)

    _emit_gate("project_inception_approved", state, project_dir)
    _emit_gate("spec_ready_opened", state)
    return state


def hydrate_cycle(
    project_dir: str,
    cycle_number: int,
    work_units: list[dict[str, Any]],
    goal: str = "",
    tracker_cycle: int | None = None,
    workstream_id: str | None = None,
) -> dict[str, Any]:
    """Bring a WORKSTREAM clone into Cycle execution in one guarded call.

    Review finding P1 on #226: each workstream clone has its own gitignored
    state, and provisioning created branches and discriminators but never
    initialised or opened local Cycle state. So the shipped prompt fetched the
    tracker backlog and went straight to `next_action`, which returned
    `not_in_execution` and dispatched nothing. The end-to-end fixture hid the
    gap by calling `init`, `transition COMMIT` and `open_cycle` by hand in every
    clone -- i.e. it tested a path the prompt never took.

    Idempotent, so re-running after a partial setup converges instead of
    erroring: seeds state if absent, walks DISCOVERY / CHECKPOINT / a finished
    previous Cycle (CYCLE_EXECUTION or SYNC) to COMMIT, and opens the Cycle
    only when it is not already open. Users must not pass --force for Cycle
    N+1; this verb does the walk internally.

    Also writes the workstream PIN, so identity survives the shell that ran
    this. The predecessor derived identity from `SYNAPTORY_ACTIVE_SPEC`, which
    meant an un-exported variable produced work that was unattributed forever.
    """
    # Persist whatever identity resolved, however it was supplied. Resolving
    # without persisting would leave the identity living in one shell's
    # environment -- which is exactly the failure mode `SYNAPTORY_ACTIVE_SPEC`
    # had: forget the export and the work is unattributed forever.
    resolved_ws = workstream_id or _paths.resolve_identity(
        project_dir, workstream_id=workstream_id
    ).workstream_id
    if resolved_ws:
        _paths.write_pin(project_dir, resolved_ws)
    workstream_id = resolved_ws

    # #303 problem 2: "each workstream can hydrate local state from
    # caller-supplied Work Unit JSON, allowing stale or inconsistent views of
    # the Cycle." When a sealed manifest is reachable it becomes AUTHORITATIVE
    # and the caller's JSON is discarded, not merged -- merging would let a
    # stale prompt re-admit a unit the Cycle cut, which is precisely the
    # inconsistency the manifest exists to prevent.
    #
    # Absent a manifest, the caller's units are still accepted: a single-clone
    # project that has never opened a Cycle has nothing to project from, and
    # refusing there would break the shipped flow for the majority case that
    # has no cross-workstream risk at all.
    sealed_manifest: dict[str, Any] | None = None
    projection = _projection_for(project_dir, cycle_number, workstream_id)
    if projection is not None:
        work_units = projection["work_units"]
        goal = goal or str(projection.get("goal") or "")
        sealed_manifest = read_manifest(project_dir, projection["_cycle_id"])

    # Seed on the absence of a POINTER, not on state that "looks empty".
    # `read_state` returns `_default_state()` when nothing is on disk, and that
    # default already carries lifecycle_state="DISCOVERY" -- so a
    # `not state.get("lifecycle_state")` guard never fires on a clean clone and
    # `initialize()` would be skipped.
    if not read_pointer(project_dir):
        initialize(project_dir, workstream_id=workstream_id)

    try:
        state = read_state(project_dir)
    except ValueError:
        state = {}

    if not state or not state.get("lifecycle_state"):
        initialize(project_dir)
        state = read_state(project_dir)

    current = state.get("lifecycle_state")
    previous_cycle = int(state.get("current_cycle") or 0)

    # Already executing this Cycle. Usually nothing to do -- but re-project when
    # this clone is not on the AUTHORITATIVE manifest revision.
    #
    # Review finding P1(1): comparing Work Unit ID SETS treated matching ids as
    # proof the projection was current. A superseding manifest that keeps the
    # same ids while changing `depends_on`, `file_scope` or acceptance criteria
    # returned `already executing`, so the clone kept stale unit data AND the
    # old manifest hash -- and then declared readiness against a revision the
    # Cycle had moved past. The hash is the only thing that actually answers
    # "am I current", so compare that.
    if current == "CYCLE_EXECUTION" and previous_cycle == int(cycle_number):
        # THE ADMITTED SET is the second thing that can be stale, and only the
        # manifest hash was checked. `open_cycle` leaves the opening clone
        # holding EVERY lane's units; `hydrate_cycle --workstream api` is how it
        # narrows to its own. Both calls carry the same manifest, so the hash
        # matched, hydrate returned `already executing`, and the board kept all
        # six units. That clone's readiness record then reported
        # `admitted=6, done=2`, `manifest_closure` failed, and the Cycle could
        # not go green through the natural flow -- the only recovery was
        # deleting .orchestrator and hydrating fresh (#328 G2).
        #
        # The hash answers "has the manifest moved on". It cannot answer "is
        # this MY lane's slice of it", because one manifest projects to a
        # different admitted set per workstream. Both questions have to be
        # asked, so the id comparison the hash check replaced comes back
        # ALONGSIDE it rather than instead of it.
        on_board = sorted(
            str(u.get("id")) for u in state.get("current_stories") or []
        )
        requested = sorted(str(u.get("id")) for u in work_units)
        board_is_this_lane = on_board == requested

        if projection is None and board_is_this_lane:
            return {**state, "hydrated": False,
                    "reason": f"already executing Cycle {cycle_number}"}
        authoritative = (sealed_manifest or {}).get("manifest_hash")
        if (authoritative and state.get("manifest_hash") == authoritative
                and board_is_this_lane):
            return {**state, "hydrated": False,
                    "reason": f"already executing Cycle {cycle_number}"}
        if projection is None:
            # The board holds a different set from the one requested and there
            # is no manifest to project from, so there is no authoritative
            # admitted set to narrow to. Refuse rather than keep a board that
            # belongs to another lane: continuing silently is what produced the
            # `admitted=6` record in the first place.
            raise HydrationRefusal(
                "stale_projection",
                "this clone's board carries %d Work Unit(s) (%s) but %d were "
                "requested for workstream %r, and no sealed manifest is "
                "reachable to re-project from. Re-pull the Cycle branch so the "
                "manifest is present, or hydrate a clean clone."
                % (len(on_board), ", ".join(on_board) or "none",
                   len(requested), resolved_ws or "<unresolved>"),
            )
        # Re-projecting replaces the admitted set, so it must not silently
        # discard work in flight. Units that are not queued have started, and
        # dropping them would strand their receipts.
        projected = sorted(str(u.get("id")) for u in work_units)
        started = [
            u for u in state.get("current_stories") or []
            if str(u.get("state")) != "queued" and str(u.get("id")) not in projected
        ]
        if started:
            raise HydrationRefusal(
                "foreign_work_unit",
                "this clone's board carries %d Work Unit(s) that workstream %r "
                "does not own and that are already in progress (%s). "
                "Re-projecting would strand them. Resolve them, or hydrate a "
                "clean clone."
                % (len(started), workstream_id,
                   ", ".join(str(u.get("id")) for u in started)),
            )
        # Units already in flight keep their state and receipts; only their
        # manifest-owned FIELDS are refreshed. Rebuilding them with
        # create_story would reset a testing unit to queued and orphan its work.
        existing = {str(u.get("id")): u for u in state.get("current_stories") or []}
        rebuilt = []
        for wu in work_units:
            unit_id = str(wu["id"])
            projected = create_story(
                unit_id,
                wu.get("title", ""),
                wu.get("backends"),
                acceptance_criteria=wu.get("acceptance_criteria"),
                kind=wu.get("kind", ""),
                labels=wu.get("labels"),
                depends_on=wu.get("depends_on"),
                file_scope=wu.get("file_scope"),
            )
            prior = existing.get(unit_id)
            if prior is None:
                rebuilt.append(projected)
                continue
            refreshed = dict(prior)
            for field in (
                "title",
                "acceptance_criteria",
                "kind",
                "labels",
                "depends_on",
                "file_scope",
                "ui_bearing",
            ):
                refreshed[field] = projected[field]
            rebuilt.append(refreshed)
        state["current_stories"] = rebuilt
        state["manifest_hash"] = authoritative
        _write_state(project_dir, state)
        return {**state, "hydrated": True, "reprojected": True,
                "work_units_admitted": len(work_units),
                "reason": "re-projected onto manifest %s" % (authoritative or "")}

    # A finished previous Cycle must not strand the workstream in
    # CYCLE_EXECUTION or SYNC. There is no workstream Checkpoint / close_cycle;
    # hydrate of N+1 is the documented next step. Walk internally (force) so
    # the user never needs --force.
    if previous_cycle and previous_cycle < int(cycle_number):
        if current == "CYCLE_EXECUTION":
            unfinished = [
                story
                for story in (state.get("current_stories") or [])
                if story.get("state") not in {"done", "cancelled"}
            ]
            if unfinished:
                detail = ", ".join(
                    f"{story.get('id')}={story.get('state')}"
                    for story in unfinished[:6]
                )
                raise ValueError(
                    f"Cannot hydrate Cycle {cycle_number} — Cycle {previous_cycle} "
                    f"still has unfinished Work Units: {detail}. Finish or cut "
                    "them, then hydrate the next Cycle."
                )
            state = transition(project_dir, "COMMIT", force=True)
            current = "COMMIT"
        elif current == "SYNC":
            state = transition(project_dir, "COMMIT", force=True)
            current = "COMMIT"

    if current == "DISCOVERY":
        # A workstream clone inherits an approved baseline from the integration
        # clone; it does not re-approve it.
        state = transition(project_dir, "COMMIT")
        current = "COMMIT"
    elif current == "CHECKPOINT":
        state = transition(project_dir, "COMMIT")
        current = "COMMIT"

    if current != "COMMIT":
        raise ValueError(
            f"Cannot hydrate Cycle {cycle_number} — lifecycle is {current}. "
            "Hydration walks DISCOVERY, CHECKPOINT, or a finished previous "
            "Cycle (CYCLE_EXECUTION/SYNC) to COMMIT and then opens the Cycle."
        )

    if not work_units and not _is_declared_integration(project_dir, workstream_id):
        raise ValueError(
            "Cannot hydrate an empty Cycle: pass this workstream's admitted "
            "Work Units from `tracker_cli.py get-sprint-backlog <N>`, filtered "
            "by this workstream's discriminator. Opening an empty Cycle would "
            "make next_action return await_sync immediately and let the "
            "workstream declare readiness having delivered nothing."
        )

    out = open_cycle(
        project_dir, cycle_number, goal or f"Cycle {cycle_number}",
        work_units, tracker_cycle=tracker_cycle, manifest=sealed_manifest,
        cycle_id=(projection or {}).get("_cycle_id"),
        # Hand over the lane hydrate resolved. Without this `open_cycle`
        # re-resolved from the environment and overwrote the pin hydrate had
        # just written from the flag (#328 G1).
        workstream_id=workstream_id,
    )
    return {**out, "hydrated": True, "work_units_admitted": len(work_units)}


def cut_work_unit(
    project_dir: str, story_id: str, reason: str = ""
) -> dict[str, Any]:
    """Cut a Work Unit from the current Cycle (§8.6, the release valve).

    A scope-defined Cycle ends when its admitted Work Units are done, so a
    workstream that cannot make the barrier cuts scope rather than merging
    late. Cut units are marked `cancelled` — which `aggregate_sprint_dod`
    already excludes from quality counts — and returned to the backlog, so the
    honest `work_units.cut` number reaches the readiness record.
    """
    state = read_state(project_dir)
    story = get_story(state, story_id)
    if story is None:
        raise ValueError(f"Work Unit {story_id} is not on the current Cycle")
    if story.get("state") == "done":
        raise ValueError(
            f"{story_id} is already done — cutting it would misreport the "
            "Cycle. Cut only unfinished Work Units."
        )
    story["state"] = "cancelled"
    story["cut_reason"] = reason or "cut at Sync to make the barrier (§8.6)"
    story["cut_at"] = _now()
    state.setdefault("process_log", []).append({
        "at": _now(), "event": "work_unit_cut",
        "story_id": story_id, "reason": story["cut_reason"],
    })
    _write_state(project_dir, state)
    return {"story_id": story_id, "state": "cancelled",
            "reason": story["cut_reason"]}


def close_cycle(
    project_dir: str, proceed_to: str = "COMMIT", force: bool = False
) -> dict[str, Any]:
    """Close the Cycle at CHECKPOINT and loop or proceed to ACCEPTANCE.

    Carries the same TW-report guard scrum puts on SPRINT_REVIEW →
    SPRINT_RETRO: without a Technical Writer receipt the Cycle has no audit
    trail of what was demonstrated, and a Checkpoint whose report is merely
    "expected" is one that gets skipped in a busy week. `--force` is the
    documented escape hatch for operator backfill.
    """
    state = read_state(project_dir)
    if state["lifecycle_state"] != "CHECKPOINT":
        raise ValueError(
            f"Cannot close a Cycle — lifecycle is {state['lifecycle_state']}, "
            "expected CHECKPOINT"
        )
    if proceed_to not in ("COMMIT", "ACCEPTANCE"):
        raise ValueError(
            f"proceed_to must be COMMIT or ACCEPTANCE, got {proceed_to!r}"
        )

    cycle_n = state.get("current_cycle")
    if not force and cycle_n is not None:
        readiness = checkpoint_readiness(project_dir, state=state)
        if not readiness["ready"]:
            raise ValueError(
                f"Cannot close Cycle {cycle_n}: Technical Writer receipt gate "
                f"failed: {'; '.join(readiness['blocking'])}. Dispatch `tw` per "
                "spq/checkpoint.md, or pass --force for operator backfill."
            )

    stories = _rollup_stories(project_dir, state)
    rollup_state = dict(state)
    rollup_state["current_stories"] = stories
    agg = aggregate_sprint_dod(rollup_state)
    work_units_total = len(stories)
    work_units_done = len([s for s in stories if s.get("state") == "done"])
    work_units_cut = len([s for s in stories if s.get("state") == "cancelled"])
    # Three producers, two scopes. Which one answered is recorded, because the
    # same three keys mean different things depending on it (#334 G15).
    work_units_scope = SCOPE_SEAT
    work_units_source = "local_board"
    readiness_rollup = _integration_readiness_rollup(project_dir, state)
    if readiness_rollup is not None:
        work_units_total = readiness_rollup["work_units_total"]
        work_units_done = readiness_rollup["work_units_done"]
        work_units_cut = readiness_rollup["work_units_cut"]
        agg = readiness_rollup["dod"]
        work_units_scope = SCOPE_MANIFEST
        work_units_source = "sync_readiness_records"
    else:
        tally = _barrier_work_unit_tally(project_dir, state)
        if tally:
            work_units_total = tally["total"]
            work_units_done = tally["done"]
            work_units_cut = tally["cut"]
            work_units_scope = SCOPE_MANIFEST
            work_units_source = "barrier_tally"
    state.setdefault("cycles_completed", []).append({
        "cycle": state.get("current_cycle"),
        "goal": state.get("cycle_goal"),
        "work_units_total": work_units_total,
        "work_units_done": work_units_done,
        "work_units_cut": work_units_cut,
        # `work_units_*` above are counted over `work_units_scope`, which is
        # `manifest` on the integration seat and `seat` on a delivery clone.
        # Everything else in this record — and `cycles_completed` itself — is
        # always seat-local.
        "work_units_scope": work_units_scope,
        "work_units_source": work_units_source,
        "dod": agg,
        "sync": state.get("sync"),
        "closed_at": _now(),
    })
    state["current_stories"] = []
    state["cycle_goal"] = None
    state["sync"] = None
    _write_state(project_dir, state)
    return transition(project_dir, proceed_to)


def record_method_signal(
    project_dir: str, kind: str, summary: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Append a MethodSignal at CHECKPOINT (§12.2).

    Mirrors into `signals.py`'s existing append-only store rather than adding a
    parallel file — that module already implements exactly this shape and is
    harvested by the planning loop.
    """
    state = read_state(project_dir)
    record = {"at": _now(), "kind": kind, "summary": summary,
              "data": data or {}, "cycle": state.get("current_cycle")}
    state.setdefault("method_signals", []).append(record)
    _write_state(project_dir, state)
    try:
        from signals import write_signal

        write_signal(
            project_dir, kind="method_signal", summary=summary,
            data={"spq_kind": kind, **(data or {})}, source="spq/checkpoint",
        )
    except Exception:
        pass
    return record


# ---------------------------------------------------------------------------
# Sync barrier delegation
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Coordination Cycle (#305) -- thin wrappers; the logic is in coordination_cycle
# ---------------------------------------------------------------------------
#
# Verbs live HERE rather than on a fourth `*_state_machine.py` because a
# Coordination Cycle is not a `build_mode`: `advance_kernel._BUILD_MODES` is a
# closed tuple that raises on anything else, and `loop_engine` builds
# `f"{build_mode}_state_machine.py"` by string interpolation. Both are
# fail-closed on that value, so a fourth mode would drag both dispatchers plus
# `state_machine.py` and `state_drift` into scope for a layer that dispatches no
# Work Units at all.


def open_coordination_cycle(
    project_dir: str,
    *,
    release_goal: str,
    children: list[dict[str, Any]],
    dependency_edges: list[dict[str, Any]] | None = None,
    coordination_seq: int | None = None,
    baseline_sha: str = "",
    created_by: str = "",
) -> dict[str, Any]:
    import coordination_cycle as _cc

    ident = identity(project_dir)
    return _cc.open_coordination(
        project_dir,
        release_goal=release_goal,
        children=children,
        dependency_edges=dependency_edges,
        coordination_seq=coordination_seq,
        baseline_sha=baseline_sha or _git_head(project_dir),
        created_by=created_by or os.environ.get("SYNAPTORY_UPN") or "",
        runner_id=ident.runner_id,
        project=_project_slug(project_dir),
    )


def coordination_status(
    project_dir: str, *, coordination_cycle_id: str | None = None
) -> dict[str, Any]:
    import coordination_cycle as _cc

    return _cc.status(project_dir, coordination_cycle_id)


def revise_coordination_manifest(
    project_dir: str,
    *,
    coordination_cycle_id: str | None = None,
    drop_child: str = "",
    reason: str = "",
    revised_by: str = "",
) -> dict[str, Any]:
    """Revise the release. Today the only revision is dropping a late child.

    A peer of `revise_manifest` one level up, and it exists for the same reason:
    without it, "a late child can be removed from the parent manifest" has no
    address, and `open_coordination_cycle` correctly refuses to overwrite a
    sealed manifest rather than silently re-opening the release.
    """
    import coordination_cycle as _cc
    import spq_paths as _p

    ccid = _p.resolve_coordination_id(
        project_dir, coordination_cycle_id=coordination_cycle_id, require=True
    )
    if not drop_child:
        raise ValueError(
            "revise_coordination_manifest needs something to change: pass "
            "--drop-child <cycle-id> --reason '...'"
        )
    return _cc.drop_child(
        project_dir,
        coordination_cycle_id=ccid,
        cycle_id=drop_child,
        reason=reason,
        revised_by=revised_by or os.environ.get("SYNAPTORY_UPN") or "",
    )


def publish_cross_cycle_event(
    project_dir: str,
    *,
    cycle_id: str | None = None,
    condition: str = "cycle_integrated",
    coordination_cycle_id: str | None = None,
    unit_id: str = "",
    output: dict[str, Any] | None = None,
    commit_sha: str = "",
    published_by: str = "",
) -> dict[str, Any]:
    """Record that a child Cycle reached a condition another child depends on.

    The producer side. `spq_ledger.publish` still refuses `cycle_integrated`
    outright and that refusal is unchanged: a cross-Cycle claim is written HERE,
    to a different tree, by a caller that holds the release manifest -- so there
    is no bypass of the intra-Cycle rule to build.
    """
    import coordination_cycle as _cc
    import spq_paths as _p

    ident = identity(project_dir)
    child_cycle = cycle_id or ident.cycle_id
    if not child_cycle:
        raise ValueError(
            "publish_cross_cycle_event needs a child Cycle: pass --cycle-id, or "
            "run it from a clone that has one hydrated"
        )

    child_manifest = read_manifest(project_dir, child_cycle) or {}
    ccid = _p.resolve_coordination_id(
        project_dir,
        coordination_cycle_id=coordination_cycle_id,
        manifest=child_manifest,
        require=True,
    )
    parent = _cc.read_manifest(project_dir, ccid)
    if not parent:
        raise ValueError(
            f"no manifest for Coordination Cycle {ccid}; pull the release branch"
        )

    result = _cc.publish(
        project_dir,
        coordination_cycle_id=ccid,
        manifest=parent,
        cycle_id=child_cycle,
        condition=condition,
        cycle_manifest_hash=str(child_manifest.get("manifest_hash") or ""),
        unit_id=unit_id,
        output=output,
        commit_sha=commit_sha,
        workstream_id=ident.workstream_id or "",
        published_by=published_by or os.environ.get("SYNAPTORY_UPN") or "",
        runner_id=ident.runner_id,
    )
    return result


def refresh_coordination(
    project_dir: str,
    *,
    coordination_cycle_id: str | None = None,
    fetch: bool = True,
) -> dict[str, Any]:
    """Rebuild the cross-Cycle ledger cache from git. Never merges.

    A separate verb rather than a flag on `coordination_status`, for the same
    reason `refresh_ledger` is separate one level down: `dep_context` reads the
    cache on every `next_action`, so a read must never become a network
    operation.
    """
    import coordination_cycle as _cc
    import spq_paths as _p

    ccid = _p.resolve_coordination_id(
        project_dir, coordination_cycle_id=coordination_cycle_id, require=True
    )
    parent = _cc.read_manifest(project_dir, ccid)
    cache = _cc.refresh(project_dir, ccid, manifest=parent or None, fetch=fetch)
    return {
        "coordination_cycle_id": ccid,
        "events": len(cache.get("events") or []),
        "refresh_ok": cache.get("refresh_ok"),
        "refresh_detail": cache.get("refresh_detail"),
        "unreadable_refs": cache.get("unreadable_refs") or [],
        "refreshed_at": cache.get("refreshed_at"),
        "pending_push": _cc.local_only_children(project_dir, ccid),
    }


def release_readiness(
    project_dir: str,
    *,
    coordination_cycle_id: str | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """The release report: exact children, hashes, SHAs, closure, evidence."""
    import coordination_barrier as _cb
    import spq_paths as _p

    ccid = _p.resolve_coordination_id(
        project_dir, coordination_cycle_id=coordination_cycle_id, require=True
    )
    return _cb.release_readiness(project_dir, ccid, use_cache=use_cache)


def clear_release(
    project_dir: str,
    *,
    coordination_cycle_id: str | None = None,
    cleared_by: str | None = None,
) -> dict[str, Any]:
    """Clear a green release. Re-evaluates fail-closed, like `clear_sync`."""
    import coordination_barrier as _cb
    import spq_paths as _p

    ccid = _p.resolve_coordination_id(
        project_dir, coordination_cycle_id=coordination_cycle_id, require=True
    )
    return _cb.clear(project_dir, ccid, cleared_by=cleared_by)


def declare_sync_ready(
    project_dir: str, cycle_n: int | None = None, **kw: Any
) -> dict[str, Any]:
    """Workstream-clone verb: write this workstream's readiness record."""
    state = read_state(project_dir)
    n = cycle_n if cycle_n is not None else int(state.get("current_cycle") or 0)
    return _barrier.declare_ready(project_dir, n, **kw)


def evaluate_sync(
    project_dir: str, cycle_n: int | None = None, use_cache: bool = True
) -> dict[str, Any]:
    """Integration-clone verb: evaluate and remember the current verdict.

    The barrier remains the authority and ``clear_sync`` always re-evaluates
    fail-closed.  Persisting the tree-bound summary lets host adapters expose
    the human clearance gate after a green evaluation instead of selecting
    ``evaluate_sync`` forever.
    """
    state = read_state(project_dir)
    n = cycle_n if cycle_n is not None else int(state.get("current_cycle") or 0)
    verdict = _barrier.evaluate(project_dir, n, use_cache=use_cache)
    try:
        import verification_cache

        tree_state = verification_cache.tree_state(project_dir)
    except Exception:
        tree_state = None
    state["sync_evaluation"] = {
        "cycle": n,
        "verdict": verdict.get("verdict"),
        "blocking": list(verdict.get("blocking") or []),
        "evaluated_at": verdict.get("evaluated_at") or _now(),
        "tree_state": tree_state,
    }
    _write_state(project_dir, state)
    return verdict


def clear_sync(
    project_dir: str, cycle_n: int | None = None, cleared_by: str | None = None
) -> dict[str, Any]:
    """Clear the barrier and transition SYNC → CHECKPOINT.

    `sync_barrier.clear` records the verdict and emits the gate but never
    touches `lifecycle_state` — there is exactly one writer of that, here, so
    the barrier stays unit-testable without a state machine.
    """
    state = read_state(project_dir)
    n = cycle_n if cycle_n is not None else int(state.get("current_cycle") or 0)
    if state["lifecycle_state"] != "SYNC":
        raise ValueError(
            f"Cannot clear Sync — lifecycle is {state['lifecycle_state']}, "
            "expected SYNC"
        )
    verdict = _barrier.clear(project_dir, n, cleared_by=cleared_by)
    transition(project_dir, "CHECKPOINT")
    return verdict


# ---------------------------------------------------------------------------
# Work Unit (story) delegation — unchanged semantics (§7.1)
# ---------------------------------------------------------------------------

def add_story(
    project_dir: str,
    story_id: str,
    title: str = "",
    backends: dict[str, Any] | None = None,
    **kw: Any,
) -> dict[str, Any]:
    state = read_state(project_dir)
    story = create_story(story_id, title, backends, **kw)
    state.setdefault("current_stories", []).append(story)
    _write_state(project_dir, state)
    return story


def _dod_intensity(project_dir: str, state: dict[str, Any]) -> str:
    """The DoD tier for this Cycle.

    Scrum derives intensity from the sprint number via
    `determine_dod_intensity`. SPQ has Cycles, not sprints, and already
    resolves the tier through `resolve_dod_tier` for `next_action` (config
    override first, else the Cycle number). Reusing that resolver keeps one
    answer per Cycle rather than two that can disagree.
    """
    try:
        return resolve_dod_tier(project_dir, state).get("tier") or "early"
    except Exception:  # noqa: BLE001 — never let tier resolution break a transition
        return "early"


def transition_story(
    project_dir: str, story_id: str, to_state: str, reason: str = ""
) -> dict[str, Any]:
    """Move a Work Unit between sub-states, applying the DoD gate (#238).

    This was a bare passthrough, which silently dropped FOUR behaviours the
    scrum wrapper has. The consequences were not cosmetic:

    1. No pre-transition DoD gate on `reviewing -> done`, so a Work Unit whose
       DoD gate was red completed anyway. Scrum redirects it to `blocked`.
    2. No #116 per-story-acceptance redirect, so the toggle was silently
       inert in SPQ even though `next_action` honours it — the two disagreed.
    3. No post-transition DoD evaluation, so `story["dod"]` was never written.
       That is what made `sync_barrier.declare_ready`'s "no DoD check is
       failing" invariant vacuous: `derive_dod` reads that key via
       `aggregate_sprint_dod`, so with nothing ever written `checks_failed`
       was always empty and the barrier could not see a red gate. Measured in
       the SPQ pilot: three workstreams declared readiness with
       `stories_evaluated: 0`.
    4. No `ship_evaluated_dod`, so no Evidence/DoD verdict reached the control
       plane and `/quality` had no SPQ data at all.
    """
    state = read_state(project_dir)
    story_before = get_story(state, story_id)
    from_state = story_before["state"] if story_before else None

    # The gate runs on the `reviewing -> done` auto-promotion, mirroring
    # scrum: a red DoD redirects to `blocked` (recoverable) rather than
    # completing. Checked before the acceptance redirect because a failed gate
    # is more fundamental than the acceptance routing.
    if to_state == "done" and from_state == "reviewing":
        # Delegated to the shared kernel (#277). resolve_done_edge reproduces
        # this mode's resolve_dod_tier-based intensity and the acceptance
        # redirect.
        from advance_kernel import resolve_done_edge

        to_state, reason, pre_dod = resolve_done_edge(
            project_dir, state, story_id, to_state, reason, mode="spq"
        )

    result = _sp_transition_story(
        state, story_id, to_state, reason=reason, project_dir=project_dir
    )
    _write_state(project_dir, state)

    if to_state in ("done", "awaiting_acceptance"):
        dod = _sp_evaluate_dod(project_dir, story_id, _dod_intensity(project_dir, state))
        story = get_story(state, story_id)
        if story:
            story["dod"] = dod
            _write_state(project_dir, state)
            _sp_ship_evaluated_dod(
                story_id, dod, project_dir=project_dir
            )   # #199
    return result


def unblock_story(project_dir: str, story_id: str, reason: str = "") -> dict[str, Any]:
    state = read_state(project_dir)
    result = _sp_unblock_story(state, story_id, reason=reason)
    _write_state(project_dir, state)
    return result


def evaluate_story_dod(project_dir: str, story_id: str) -> dict[str, Any]:
    """Evaluate a Work Unit's DoD, persist the verdict, ship it (#238).

    Was a two-line passthrough that omitted the required `intensity`
    argument, so the `evaluate_dod` verb raised TypeError on every single
    invocation. It also never persisted the result or shipped it, which the
    scrum wrapper does.
    """
    state = read_state(project_dir)
    result = _sp_evaluate_dod(project_dir, story_id, _dod_intensity(project_dir, state))
    story = get_story(state, story_id)
    if story:
        story["dod"] = result
        _write_state(project_dir, state)
        _sp_ship_evaluated_dod(
            story_id, result, project_dir=project_dir
        )   # #199
    return result


def request_acceptance(project_dir: str, story_id: str) -> dict[str, Any]:
    state = read_state(project_dir)
    result = _sp_request_acceptance(state, story_id)
    _write_state(project_dir, state)
    return result


def accept_story(project_dir: str, story_id: str, accepted_by: str) -> dict[str, Any]:
    """PO accepts a Work Unit (#116).

    Scrum's wrapper emits ``evidence_dod / approved`` so the Quality Gate
    Queue counts PO sign-off. SPQ used to skip that emit, so Pulse Notes
    (per-story acceptance) never posted an approved Evidence/DoD row even
    though ``request_acceptance`` had already shipped the evaluated checks.
    """
    state = read_state(project_dir)
    result = _sp_accept_story(state, story_id, accepted_by, project_dir)
    _write_state(project_dir, state)
    try:
        from gate_emitter import emit_evidence_dod_accepted

        emit_evidence_dod_accepted(story_id, accepted_by)
    except ImportError:
        pass
    return result


def reject_story(
    project_dir: str,
    story_id: str,
    reason: str,
    feedback: str,
    rejected_by: str,
    ac_changes: list[str] | None = None,
) -> dict[str, Any]:
    """PO rejects a Work Unit (#116).

    The previous call passed `ac_changes` as a SIXTH POSITIONAL argument, but
    `story_pipeline.reject_story` takes five positionals and then `*`, so this
    raised `TypeError: reject_story() takes 5 positional arguments but 6 were
    given` on every rejection (#238). It also never forwarded `project_dir`,
    which the AC-change handling needs, and never emitted the `evidence_dod /
    rejected` gate event, so the audit ledger recorded no reason for the
    rejection.
    """
    state = read_state(project_dir)
    result = _sp_reject_story(
        state, story_id, reason, feedback, rejected_by,
        acceptance_criteria_change=ac_changes or None,
        project_dir=project_dir,
    )
    _write_state(project_dir, state)
    try:
        from gate_emitter import emit_evidence_dod_rejected

        emit_evidence_dod_rejected(
            story_id,
            rejected_by,
            f"[{reason}] {feedback}",
            project_dir=project_dir,
        )
    except ImportError:
        pass
    return result


# ---------------------------------------------------------------------------
# next_action
# ---------------------------------------------------------------------------

def next_action(project_dir: str) -> dict[str, Any]:
    """Lifecycle-aware "what's next" for the SPQ loop. Read-only.

    Passes the SAME argument set as `story_pipeline.py`'s own `next_action`
    CLI, INCLUDING `dod_tier_info` and `parallelism`. This is not optional
    polish: both lifecycle wrappers shipped for months omitting exactly these
    two, which made story parallelism unreachable on the primary orchestrator
    path (#192) because `_attach_parallel_batch` bails out on a falsy
    `parallelism`. This file is specified as a mirror of that wrapper, so it is
    one copy-paste away from inheriting the same defect.

    THE TRANSLATION (§10.2): all Work Units terminal returns
    `sprint_complete` from the shared pipeline. Passing that through closes the
    Cycle without integrating, so it becomes `await_sync` — a stop action, and
    the barrier's whole value. Gate detection needs nothing else:
    `loop_engine` stops on `action not in CONTINUE_ELIGIBLE`, so a new action
    is a stop action by default.
    """
    state = read_state(project_dir)
    lifecycle = state.get("lifecycle_state", "DISCOVERY")
    base = {
        "lifecycle_state": lifecycle,
        "current_cycle": state.get("current_cycle", 0),
        "cycle_id": state.get("_cycle_id"),
        "cycle_seq": state.get("current_cycle"),
        "workstream_id": state.get("_workstream_id"),
        # Deprecated mirror of workstream_id, one minor only. #304 requires
        # "existing SPQ command/wrapper output remains backward compatible"
        # while #303 requires no compatibility dependency on Multi-Spec; both
        # hold if the key survives as an alias and then goes.
        "spec_id": state.get("_workstream_id"),
    }
    if lifecycle != "CYCLE_EXECUTION":
        return {
            **base,
            "action": "not_in_execution",
            "story_id": None,
            "reason": (
                f"lifecycle is {lifecycle}, not CYCLE_EXECUTION — Work Units "
                "are only dispatched during Cycle execution"
            ),
            "human_gate_pending": False,
        }

    if _is_integration_seat(project_dir, state):
        return {
            **base,
            "action": "await_sync",
            "story_id": None,
            "reason": (
                "this clone is the integration seat (`integration: true`) — "
                "Work Units are dispatched on delivery workstreams, not here. "
                "Wait at the barrier; never dispatch_se from this checkout."
            ),
            "human_gate_pending": True,
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
    )

    if result.get("action") == "sprint_complete":
        cycle_n = state.get("current_cycle", 0)
        result["action"] = "await_sync"
        result["human_gate_pending"] = True
        result["reason"] = (
            f"every admitted Work Unit in Cycle {cycle_n} is terminal, but the "
            "Cycle does not close until the cross-workstream barrier clears. "
            "Run `sync_barrier.py declare-ready` in this clone, then the "
            "integration clone runs collect → (human merge) → evaluate → "
            "clear_sync. STOP here: this is a human gate."
        )

    result.update(base)
    return result


def summary(project_dir: str) -> dict[str, Any]:
    state = read_state(project_dir)
    stories = state.get("current_stories", [])
    return {
        "lifecycle_state": state.get("lifecycle_state"),
        "current_cycle": state.get("current_cycle"),
        "cycle_goal": state.get("cycle_goal"),
        "cycles_completed": len(state.get("cycles_completed", [])),
        "work_units": {
            st: len(list_stories_by_state(state, st))
            for st in ("queued", "in_progress", "testing", "reviewing",
                       "done", "blocked", "cancelled")
        },
        # Both counts above are this clone's own: `cycles_completed` is the
        # Cycles THIS seat closed, and `work_units` is THIS seat's board. Said
        # out loud because the archived per-Cycle records carry manifest-wide
        # counts under the same `work_units_*` names, and a reader comparing the
        # two would otherwise have no way to know (#334 G15).
        "scope": {"cycles_completed": SCOPE_SEAT, "work_units": SCOPE_SEAT},
        "sync": state.get("sync"),
        "cycle_tracker_binding": state.get("cycle_tracker_binding"),
        "dod": aggregate_sprint_dod(state),
        "method_signals": len(state.get("method_signals", [])),
    }


def _parse_flag(args: list[str], flag: str, default: Any = None) -> Any:
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1]
    return default


def _die(msg: str) -> None:
    print(json.dumps({"error": msg}), file=sys.stderr)
    sys.exit(1)


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    action, project_dir = sys.argv[1], sys.argv[2]
    args = sys.argv[3:]

    def _emit(obj: Any) -> None:
        print(json.dumps(obj, indent=2, default=str))

    def _first_int() -> int | None:
        for a in args:
            if a.isdigit():
                return int(a)
        return None

    try:
        if action == "init":
            backends = _parse_flag(args, "--backends")
            _emit(initialize(
                project_dir, json.loads(backends) if backends else None))
        elif action == "read":
            _emit(read_state(project_dir))
        elif action == "transition":
            if not args:
                _die("transition requires a target state")
            _emit(transition(project_dir, args[0], force="--force" in args))
        elif action == "open_cycle":
            wu = _parse_flag(args, "--work-units", "[]")
            _emit(open_cycle(
                project_dir, _first_int(),
                _parse_flag(args, "--goal", "") or "",
                json.loads(wu),
                tracker_cycle=(
                    int(_parse_flag(args, "--tracker-cycle"))
                    if _parse_flag(args, "--tracker-cycle") else None
                ),
                # `--workstream` was accepted by hydrate_cycle but not here, so
                # the one verb that WRITES the pin could not be told which lane
                # to write (#328 G1).
                workstream_id=_parse_flag(args, "--workstream"),
            ))
        elif action == "close_cycle":
            _emit(close_cycle(
                project_dir, _parse_flag(args, "--proceed-to", "COMMIT"),
                force="--force" in args))
        elif action == "approve_baseline":
            _emit(approve_baseline(
                project_dir, _parse_flag(args, "--approved-by")))
        elif action == "hydrate_cycle":
            wu = _parse_flag(args, "--work-units", "[]")
            _emit(hydrate_cycle(
                project_dir, _first_int() or 1, json.loads(wu),
                goal=_parse_flag(args, "--goal", "") or "",
                tracker_cycle=(
                    int(_parse_flag(args, "--tracker-cycle"))
                    if _parse_flag(args, "--tracker-cycle") else None
                ),
                workstream_id=_parse_flag(args, "--workstream"),
            ))
        elif action == "publish_event":
            if not args:
                _die("publish_event requires a Work Unit id")
            _emit(publish_event(
                project_dir,
                unit_id=args[0],
                condition=_parse_flag(args, "--condition", "integrated") or "integrated",
                cycle_id=_parse_flag(args, "--cycle-id"),
                output=json.loads(_parse_flag(args, "--output", "null") or "null"),
                evaluation=json.loads(
                    _parse_flag(args, "--evaluation", "null") or "null"
                ),
                commit_sha=_parse_flag(args, "--sha", "") or "",
                published_by=_parse_flag(args, "--published-by", "") or "",
            ))
        elif action == "dep_status":
            _emit(dep_status(project_dir, args[0] if args else None))
        elif action == "refresh_ledger":
            _emit(refresh_ledger(
                project_dir,
                cycle_id=_parse_flag(args, "--cycle-id"),
                fetch="--no-fetch" not in args,
            ))
        elif action == "identity":
            ident = identity(project_dir, cycle_id=_parse_flag(args, "--cycle-id"))
            _emit({
                "cycle_id": ident.cycle_id,
                "cycle_seq": ident.cycle_seq,
                "workstream_id": ident.workstream_id,
                "runner_id": ident.runner_id,
                # Which mechanism answered. `/status` and `/doctor` need this to
                # tell "pinned" from "guessed by the only-workstream fallback",
                # which is the difference between attributed and unattributed
                # work.
                "source": ident.source,
            })
        elif action == "verify_manifest":
            cid = _parse_flag(args, "--cycle-id") or identity(
                project_dir, require_cycle=True
            ).cycle_id
            found = read_manifest(project_dir, cid)
            if not found:
                _die(f"no manifest found for Cycle {cid}")
            _emit({
                "cycle_id": cid,
                "manifest_hash": found.get("manifest_hash"),
                "manifest_revision": found.get("manifest_revision"),
                "integration_ref": found.get("integration_ref"),
                "hash_verified": _mf.verify_hash(found),
                "problems": _mf.validate(
                    found, require_baseline=_is_git_repo(project_dir)
                ),
                "admitted": _mf.admitted_ids(found),
                "owners": _mf.owners(found),
            })
        elif action == "revise_manifest":
            _emit(revise_manifest(
                project_dir,
                cycle_id=_parse_flag(args, "--cycle-id"),
                reason=_parse_flag(args, "--reason", "") or "",
                drop_units=[
                    u for u in (_parse_flag(args, "--drop-unit", "") or "").split(",")
                    if u
                ],
                add_units=json.loads(_parse_flag(args, "--add-units", "[]") or "[]"),
                revised_by=_parse_flag(args, "--revised-by", "") or "",
            ))
        elif action == "cut_work_unit":
            if not args:
                _die("cut_work_unit requires a story id")
            _emit(cut_work_unit(
                project_dir, args[0], _parse_flag(args, "--reason", "") or ""))
        elif action == "open_coordination_cycle":
            _emit(open_coordination_cycle(
                project_dir,
                release_goal=_parse_flag(args, "--goal", "") or "",
                children=json.loads(_parse_flag(args, "--children", "[]") or "[]"),
                dependency_edges=json.loads(
                    _parse_flag(args, "--edges", "[]") or "[]"
                ),
                coordination_seq=(
                    int(_parse_flag(args, "--coordination-seq"))
                    if _parse_flag(args, "--coordination-seq") else None
                ),
                baseline_sha=_parse_flag(args, "--baseline-sha", "") or "",
                created_by=_parse_flag(args, "--created-by", "") or "",
            ))
        elif action == "coordination_status":
            _emit(coordination_status(
                project_dir,
                coordination_cycle_id=_parse_flag(args, "--coordination-cycle-id"),
            ))
        elif action == "revise_coordination_manifest":
            _emit(revise_coordination_manifest(
                project_dir,
                coordination_cycle_id=_parse_flag(args, "--coordination-cycle-id"),
                drop_child=_parse_flag(args, "--drop-child", "") or "",
                reason=_parse_flag(args, "--reason", "") or "",
                revised_by=_parse_flag(args, "--revised-by", "") or "",
            ))
        elif action == "publish_cross_cycle_event":
            _emit(publish_cross_cycle_event(
                project_dir,
                cycle_id=_parse_flag(args, "--cycle-id"),
                condition=(
                    _parse_flag(args, "--condition", "cycle_integrated")
                    or "cycle_integrated"
                ),
                coordination_cycle_id=_parse_flag(args, "--coordination-cycle-id"),
                unit_id=_parse_flag(args, "--unit-id", "") or "",
                output=json.loads(_parse_flag(args, "--output", "null") or "null"),
                commit_sha=_parse_flag(args, "--sha", "") or "",
                published_by=_parse_flag(args, "--published-by", "") or "",
            ))
        elif action == "refresh_coordination":
            _emit(refresh_coordination(
                project_dir,
                coordination_cycle_id=_parse_flag(args, "--coordination-cycle-id"),
                fetch="--no-fetch" not in args,
            ))
        elif action == "release_readiness":
            out = release_readiness(
                project_dir,
                coordination_cycle_id=_parse_flag(args, "--coordination-cycle-id"),
                use_cache="--no-cache" not in args,
            )
            _emit(out)
            sys.exit(0 if out.get("verdict") == "green" else 3)
        elif action == "clear_release":
            _emit(clear_release(
                project_dir,
                coordination_cycle_id=_parse_flag(args, "--coordination-cycle-id"),
                cleared_by=_parse_flag(args, "--cleared-by"),
            ))
        elif action == "declare_sync_ready":
            _emit(declare_sync_ready(
                project_dir, _first_int(),
                workstream=_parse_flag(args, "--workstream"),
                declared_by=_parse_flag(args, "--declared-by"),
                run_regression="--no-regression" not in args,
            ))
        elif action == "evaluate_sync":
            out = evaluate_sync(
                project_dir, _first_int(), use_cache="--no-cache" not in args)
            _emit(out)
            sys.exit(0 if out.get("verdict") == "green" else 3)
        elif action == "clear_sync":
            _emit(clear_sync(
                project_dir, _first_int(),
                cleared_by=_parse_flag(args, "--cleared-by")))
        elif action == "add_story":
            if not args:
                _die("add_story requires a story id")
            _emit(add_story(
                project_dir, args[0], _parse_flag(args, "--title", "") or ""))
        elif action == "transition_story":
            if len(args) < 2:
                _die(
                    "transition_story requires <story_id> <to_state> "
                    "[--reason '...'] [--force-recovery]"
                )
            reason = _parse_flag(args, "--reason", "") or ""
            forced = "--force-recovery" in args
            from_state = str(
                (get_story(read_state(project_dir), args[0]) or {}).get("state") or ""
            )
            refused = _cli_receipt_gated_refusal(
                from_state, args[1], args[0], forced=forced, reason=reason or None
            )
            if refused:
                _die(refused)
            _emit(transition_story(project_dir, args[0], args[1], reason))
        elif action == "unblock_story":
            if not args:
                _die("unblock_story requires a story id")
            _emit(unblock_story(
                project_dir, args[0], _parse_flag(args, "--reason", "") or ""))
        elif action == "evaluate_dod":
            if not args:
                _die("evaluate_dod requires a story id")
            _emit(evaluate_story_dod(project_dir, args[0]))
        elif action == "request_acceptance":
            if not args:
                _die("request_acceptance requires a story id")
            _emit(request_acceptance(project_dir, args[0]))
        elif action == "accept_story":
            if not args:
                _die("accept_story requires a story id")
            _emit(accept_story(
                project_dir, args[0],
                _parse_flag(args, "--accepted-by", "") or ""))
        elif action == "reject_story":
            if not args:
                _die("reject_story requires a story id")
            # --ac-change is repeatable, matching scrum_state_machine. It was
            # previously unparsed, so AC changes passed on the command line
            # were silently dropped — the caller had no way to tell.
            ac_changes = [
                args[i + 1] for i, a in enumerate(args)
                if a == "--ac-change" and i + 1 < len(args)
            ]
            _emit(reject_story(
                project_dir, args[0],
                _parse_flag(args, "--reason", "needs-fix") or "needs-fix",
                _parse_flag(args, "--feedback", "") or "",
                _parse_flag(args, "--rejected-by", "") or "",
                ac_changes=ac_changes,
            ))
        elif action == "record_method_signal":
            if not args:
                _die("record_method_signal requires a kind")
            raw_data = _parse_flag(args, "--data")
            _emit(record_method_signal(
                project_dir, args[0],
                _parse_flag(args, "--summary", "") or "",
                json.loads(raw_data) if raw_data else None))
        elif action == "receipts_dir":
            # The ONE path a ceremony may name when it tells an agent where to
            # write. Prose used to spell `.orchestrator/receipts/` directly,
            # while `checkpoint_readiness`, `acceptance_readiness` and
            # `sync_barrier` all read `_resolve_receipts_dir` -- so a TW that
            # followed the doc wrote where no gate looked and `close_cycle`
            # refused with `missing CHECKPOINT-{N}-tw.json` (#327).
            #
            # Bare stdout, not `_emit`'s JSON: the only caller is `$(...)` in a
            # ceremony's bash block, and making every such block parse JSON to
            # recover one string is how a doc ends up hardcoding it again.
            #
            # `intended=True`: the ceremony names the path BEFORE any receipt
            # exists, which is exactly the read/write split that made
            # `advance_kernel.intended_receipts_dir` necessary. Reading with
            # `intended=False` here would hand back the flat dir whenever the
            # scoped one had not been created yet.
            from story_pipeline import receipts_dir_for

            print(receipts_dir_for(project_dir, intended=True))
        elif action == "next_action":
            _emit(next_action(project_dir))
        elif action == "summary":
            _emit(summary(project_dir))
        else:
            _die(f"Unknown action: {action}")
    except (ValueError, _barrier.BarrierError) as e:
        _die(str(e))
    except Exception as e:  # noqa: BLE001
        # `CoordinationError` subclasses ValueError, so it is already covered;
        # this is the belt for anything the coordination modules raise that is
        # not a ValueError. A traceback on stdout would be parsed as JSON by the
        # prompt that called this.
        _die("%s: %s" % (type(e).__name__, e))


if __name__ == "__main__":
    main()
