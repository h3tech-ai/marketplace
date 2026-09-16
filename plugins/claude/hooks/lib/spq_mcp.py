#!/usr/bin/env python3
"""The SPQ tool surface, defined once for every MCP host (#303 host parity).

Cursor and Codex each expose SPQ through their own server, and "both servers
implement the same contract" enforced only by code review is how `story_id`
becomes `work_unit_id` on one host. The tool bodies therefore live here and each
server registers them, passing its own gates in. A new argument, a changed
refusal, or a new tool reaches both hosts or neither.

## What is on the boundary, and what deliberately is not

A verb belongs here iff it mutates durable Cycle state, produces evidence that
crosses a clone boundary, or the ceremony cannot proceed without it. Everything
else stays CLI-only so the model's reachable surface stays small.

    reads       spq_get_cycle, spq_dependency_ledger, spq_manifest_validate
    mutations   spq_publish_event, spq_refresh_ledger, spq_cut_work_unit

The CEREMONY operations -- initialize, approve_baseline, open_cycle,
hydrate_cycle, close_cycle, record_sync, complete -- are NOT here. Each host
already exposes them (`spq_lifecycle` on Codex, the lifecycle tools on Cursor),
and a second path to the same operation is worse than either path alone: two
gates to keep in step, and a model free to pick whichever one refuses less.
This module adds only the manifest and ledger surface, which neither host had.

NOT exposed, on purpose: `init`, the raw `transition` edge, `approve_baseline`,
`open_cycle`, and the acceptance verbs. Each is either already covered by
`advance` / `begin_dispatch` or is an operator recovery hatch that must not be
model-reachable.

Nor is the BARRIER. `cycle_barrier.evaluate` takes facts and derives verdicts,
and its close requires a promotion recorded under the same operation identity;
putting either on the model's surface would offer a second authority beside the
one that actually decides whether a Cycle integrates. `close_cycle` records a
verdict the barrier produced -- it does not produce one.

## Identities only

Every schema carries identities -- `cycle_id` and `work_unit_id` -- and never
a path. There is therefore no `manifest_path` or `record_path` for a
caller to substitute, so the containment check that guards the receipt path has
nothing to guard here: the attack surface is removed by schema design rather
than validated away. Every path is derived server-side under
`.synaptory/.orchestrator/spq/`.

`cycle_id` is the NATIVE SPQ identity. It is never `SYNAPTORY_ACTIVE_SPEC`,
which remains the Scrum/Kanban Multi-Spec variable that SPQ ignores.

Python 3.9 compatible: this file is projected into every host package.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

import mcp_transport

# Tools that change durable state or publish evidence. These run the host's
# policy AND readiness gates; the reads run only the policy gate, because
# refusing to let an operator LOOK at why their Cycle is blocked helps nobody.
MUTATING = frozenset(
    {
        "spq_publish_event",
        "spq_cut_work_unit",
        "spq_external_intake_admit",
    }
)

# The two `refresh_*` verbs are DELIBERATELY not in that set, though they do
# write a file. What they write is a local cache both modules' own docstrings
# describe as "never authoritative and can be deleted safely" -- rebuilt from
# git, publishing nothing, relied on by nobody else.
#
# Gating them on execution readiness made the fail-closed dependency gate
# self-defeating: the gate holds a Work Unit with `dep_ledger_stale` and tells
# the operator to run refresh, and refresh then refuses because the clone has
# not installed nine managed agent profiles. A clone could not find out whether
# its own dependency was satisfied without provisioning to dispatch agents it
# was never going to dispatch. Observed on Codex with a real agent, which
# correctly reported the Work Unit blocked and could not get past it.
#
# The policy gate still applies to them, so a regulated project still refuses.


def _project(args: Dict[str, Any]) -> str:
    return str(args.get("project_dir") or os.getcwd())


def _refused(code: str, message: str, **extra: Any) -> Dict[str, Any]:
    payload = {"ok": False, "code": code, "error": message}
    payload.update(extra)
    return payload


# ── tool bodies ─────────────────────────────────────────────────────────────


def _get_cycle(args: Dict[str, Any]) -> Dict[str, Any]:
    import cycle_records
    import spq_manifest
    import spq_state_machine as spq

    project = _project(args)
    ident = spq.identity(project, cycle_id=args.get("cycle_id"))
    manifest = spq.read_manifest(project, ident.cycle_id) if ident.cycle_id else {}
    state = {}
    try:
        state = spq.read_state(project)
    except Exception as exc:  # noqa: BLE001 - report, do not raise, on a read
        return _refused("state_unreadable", str(exc))
    return {
        "ok": True,
        "cycle_id": ident.cycle_id,
        "cycle_seq": ident.cycle_seq,
        "runner_id": ident.runner_id,
        "lifecycle_state": state.get("lifecycle_state"),
        "cycle_goal": state.get("cycle_goal"),
        "declaration_hash": spq_manifest.sealed_hash(manifest),
        "manifest_revision": manifest.get("manifest_revision"),
        "manifest_valid": bool(manifest) and spq_manifest.verify_sealed(manifest),
        "admitted": cycle_records.admitted_ids(manifest) if manifest else [],
        # SHARED PATHS, not units. The retired reader answered unit -> lane;
        # with the lane gone a unit has no owner, and the ownership the method
        # declares is of a path that more than one unit would otherwise touch
        # (`C-04`'s `shared_paths_owned`).
        "shared_path_owners": (
            cycle_records.shared_path_owners(manifest) if manifest else {}
        ),
        "work_units": [
            {
                "id": u.get("id"),
                "state": u.get("state"),
                "unit_status": spq.integration_label(u),
            }
            for u in state.get("current_stories") or []
        ],
        "sync": state.get("sync"),
    }


def _dependency_ledger(args: Dict[str, Any]) -> Dict[str, Any]:
    """Read-only. #304's "an unknown dependency is observable to the operator"."""
    import spq_state_machine as spq

    return {"ok": True, **spq.dep_status(_project(args), args.get("work_unit_id"))}


def _manifest_validate(args: Dict[str, Any]) -> Dict[str, Any]:
    import spq_manifest
    import spq_state_machine as spq

    project = _project(args)
    ident = spq.identity(project, cycle_id=args.get("cycle_id"))
    if not ident.cycle_id:
        return _refused("no_cycle", "no Cycle identity resolves for this project")
    manifest = spq.read_manifest(project, ident.cycle_id)
    if not manifest:
        return _refused("no_manifest", "Cycle %s has no sealed manifest" % ident.cycle_id)
    return {
        "ok": True,
        "cycle_id": ident.cycle_id,
        "manifest_hash": manifest.get("manifest_hash"),
        "manifest_revision": manifest.get("manifest_revision"),
        "integration_ref": manifest.get("integration_ref"),
        "hash_verified": spq_manifest.verify_hash(manifest),
        "problems": spq_manifest.validate(manifest),
    }


def hydrate_cycle(args: Dict[str, Any]) -> Dict[str, Any]:
    """Join this clone to a Cycle. PUBLIC, and not in `_BODIES`.

    Every other body here is registered as an `spq_*` tool. This one is called
    by each host's own ceremony tool instead -- `hydrate_cycle` on Cursor,
    `spq_lifecycle(operation="hydrate_cycle")` on Codex -- because those two
    tool shapes cannot be produced from one registry entry, while the BODY they
    run has to be the same one or the manifest-hash refusal below exists on
    whichever host was edited last.

    It was previously `_hydrate_cycle`: private, unregistered, and reachable
    from nothing but one unit test, while both hosts hand-rolled their own
    hydration against a signature the rewrite had already changed.
    """
    # `spq_manifest` IS USED BELOW, and its absence here was a live defect: the
    # `declared_hash` guard raised `NameError`, the blanket `except Exception`
    # caught it, and every hydration that named the manifest it believed it was
    # joining came back `hydrate_failed: name 'spq_manifest' is not defined`.
    # The check that exists to refuse a stale prompt refused every caller
    # instead, and the one that passed a hash was the only caller it refused.
    import spq_manifest
    import spq_state_machine as spq

    project = _project(args)
    cycle_id = args.get("cycle_id")
    declared_hash = args.get("manifest_hash")
    try:
        ident = spq.identity(project, cycle_id=cycle_id)
        manifest = spq.read_manifest(project, ident.cycle_id) if ident.cycle_id else {}
        # The caller states which manifest it believes it is joining. Refusing a
        # mismatch here means a stale prompt cannot hydrate a clone onto a
        # revision the Cycle has moved past without anyone noticing.
        if declared_hash and spq_manifest.sealed_hash(manifest) != declared_hash:
            # Also fires when NO manifest is reachable. A caller cannot be on
            # the manifest it names if there is none -- and letting that through
            # was the stale-prompt case this check exists for, passing silently
            # because the guard required a manifest to compare against.
            return _refused(
                "manifest_hash_mismatch",
                "caller expected manifest %s but Cycle %s is on %s"
                % (
                    declared_hash,
                    ident.cycle_id or "<unresolved>",
                    spq_manifest.sealed_hash(manifest) or "no sealed manifest",
                ),
            )
        # BY CYCLE ID, and with no lane slice to take. `hydrate_cycle` used
        # to take a sequence plus a lane and project that lane's units; it now
        # takes the Cycle and projects the whole admitted set, because the
        # admitted set IS the Cycle's work (`C-01`).
        result = spq.hydrate_cycle(project, cycle_id=ident.cycle_id)
    except spq.HydrationRefusal as exc:
        return _refused(exc.code, str(exc))
    except Exception as exc:  # noqa: BLE001
        return _refused("hydrate_failed", str(exc))
    # PASSED THROUGH, not re-described. The previous mapping read `hydrated`,
    # `reason` and `current_stories` off the result; `hydrate_cycle` returns
    # none of the three, so every successful hydration reported
    # `hydrated: null` with an empty unit list -- a tool answering "nothing
    # happened" about work it had just done.
    board = spq.read_state(project, cycle_id=result["cycle_id"])
    return {
        "ok": True,
        "cycle_id": result["cycle_id"],
        "units": result["units"],
        "reprojected": result["reprojected"],
        "declaration_hash": result["declaration_hash"],
        "work_units": [u.get("id") for u in board.get("current_stories") or []],
    }


def _require_declared_identity(args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Refuse a mutation whose declared identity is not the current one.

    Review finding P1(3): both mutations took `cycle_id` and `manifest_hash` in
    their SCHEMA and then ignored them, so a caller naming Cycle 3 mutated
    whichever Cycle happened to be current. A required argument the body
    discards is worse than no argument at all -- the caller believes it bound
    the operation to an identity, and nothing did.
    """
    import spq_state_machine as spq

    project = _project(args)
    declared_cycle = args.get("cycle_id")
    declared_hash = args.get("manifest_hash")
    missing = [
        name
        for name, value in (
            ("cycle_id", declared_cycle),
            ("manifest_hash", declared_hash),
        )
        if not isinstance(value, str) or not value.strip()
    ]
    if missing:
        return _refused(
            "identity_required",
            "mutation requires non-empty %s" % " and ".join(missing),
        )
    try:
        ident = spq.identity(project)
    except Exception as exc:  # noqa: BLE001
        return _refused("no_identity", str(exc))

    if str(declared_cycle) != str(ident.cycle_id or ""):
        return _refused(
            "cycle_mismatch",
            "caller declared Cycle %s but this clone is on %s. Mutating the "
            "current Cycle instead would apply the change to a Cycle the caller "
            "never named." % (declared_cycle, ident.cycle_id or "<none>"),
        )
    manifest = spq.read_manifest(project, ident.cycle_id) if ident.cycle_id else {}
    actual = manifest.get("manifest_hash")
    if actual != declared_hash:
        return _refused(
            "manifest_hash_mismatch",
            "caller declared manifest %s but Cycle %s is on %s"
            % (declared_hash, ident.cycle_id or "<none>",
               actual or "no sealed manifest"),
        )
    return None


def _publish_event(args: Dict[str, Any]) -> Dict[str, Any]:
    import spq_ledger
    import spq_state_machine as spq

    refusal = _require_declared_identity(args)
    if refusal:
        return refusal
    output = args.get("output")
    if output is not None and not isinstance(output, dict):
        return _refused(
            "invalid_output",
            "output evidence must be an object containing id and digest",
        )
    evaluation = args.get("evaluation")
    if evaluation is not None and not isinstance(evaluation, dict):
        return _refused(
            "invalid_evaluation",
            "evaluation attestation must be the object returned by "
            "evaluate-incremental",
        )
    try:
        result = spq.publish_event(
            _project(args),
            unit_id=str(args.get("work_unit_id") or ""),
            condition=str(args.get("condition") or ""),
            cycle_id=args.get("cycle_id"),
            commit_sha=str(args.get("commit_sha") or ""),
            output=output,
            evaluation=evaluation,
        )
    except spq_ledger.LedgerError as exc:
        return _refused("event_refused", str(exc))
    except Exception as exc:  # noqa: BLE001
        return _refused("publish_failed", str(exc))
    return {
        "ok": True,
        "event_id": result["event"]["event_id"],
        "condition": result["event"]["condition"],
        # Propagation is a human step: the shipped git rules forbid agents
        # pushing. Returning the command is what stops the latency reading as a
        # failure.
        "push_command": result["push_command"],
    }


def _refresh_ledger(args: Dict[str, Any]) -> Dict[str, Any]:
    import spq_state_machine as spq

    try:
        return {"ok": True, **spq.refresh_ledger(
            _project(args), cycle_id=args.get("cycle_id")
        )}
    except Exception as exc:  # noqa: BLE001
        return _refused("refresh_failed", str(exc))


def _cut_work_unit(args: Dict[str, Any]) -> Dict[str, Any]:
    import spq_state_machine as spq

    refusal = _require_declared_identity(args)
    if refusal:
        return refusal
    try:
        spq.cut_work_unit(
            _project(args),
            str(args.get("work_unit_id") or ""),
            str(args.get("reason") or ""),
        )
    except Exception as exc:  # noqa: BLE001
        return _refused("cut_refused", str(exc))
    return {"ok": True, "work_unit_id": args.get("work_unit_id")}


def _external_intake_admit(args: Dict[str, Any]) -> Dict[str, Any]:
    """Admit an artifact the platform did not produce (#495 P1).

    ONE REGISTRATION, THREE HOSTS. Before this, intake existed only as a module
    CLI and the conformance scenario reached it by importing `core/lib`, while
    `contract.json` claimed all three hosts provided it -- #517's lesson one
    level up, where the machine-readable claim and the prose beside it disagree
    and the report publishes the claim. Registering it here rather than in each
    server is what makes "this host provides intake" mean the surface a project
    installs, and it inherits each host's own `policy_gate` for free: Codex
    refuses a regulated project and Cursor refuses a BAA one WITHOUT intake
    having to restate either rule.

    It is in `MUTATING`, so it also inherits the execution-readiness gate. That
    is deliberate and unlike the two `refresh_*` verbs: this writes a record a
    judged sign-off will later bind to, which is the opposite of a cache that
    "can be deleted safely".

    WHAT THIS DOES NOT MAKE AUTHORITATIVE. Reaching intake through the host
    boundary does not put the intake fact beyond the admitting principal's
    reach: the record is still a file in the project, and the same principal
    can rewrite it and replace the artifact together after checking. That half
    is #495's remaining declared gap and its resolution is
    `docs/proposals/authoritative-external-intake.md` option A. Closing the
    reachability half without saying so would recreate the overclaim #549
    corrected.
    """
    import external_intake

    project = _project(args)
    # RESOLVED AGAINST THE PROJECT, NOT THE CWD. `admit_external_candidate`
    # resolves a relative artifact against the process working directory, which
    # is right for its module CLI and meaningless through an MCP boundary: a
    # server's cwd is wherever the host launched it. Resolving here keeps the
    # shared module's CLI semantics unchanged and makes `inbox/brief.md` mean
    # the same thing to every host. Containment is NOT weakened by this: the
    # join happens before `admit_external_candidate`'s own realpath check, so a
    # `../../..` still lands outside the project and is still refused.
    artifact = str(args.get("artifact_path") or "")
    if artifact and not os.path.isabs(artifact):
        artifact = os.path.join(project, artifact)

    try:
        record = external_intake.admit_external_candidate(
            project,
            str(args.get("work_unit_id") or ""),
            artifact,
            admitted_by=str(args.get("admitted_by") or ""),
            source_note=(
                str(args["source_note"]) if args.get("source_note") else None
            ),
        )
    except external_intake.IntakeRefused as exc:
        return _refused(exc.code, exc.message)
    return {"ok": True, "record": record}


# ── the host ceremony next_action, defined once ─────────────────────────────


#: `next_action` results a host drives through a ceremony tool rather than
#: through `begin_dispatch`, mapped to the operation name that tool takes.
#: Derived from the action rather than restated per stage: the host copies this
#: replaces hard-coded one `operation` per stage branch, and the branch and the
#: operation drifted apart the moment the stage list changed.
CEREMONY_OPERATIONS = {
    "approve_baseline": "approve_baseline",
    "open_cycle": "open_cycle",
    "close_cycle": "close_cycle",
    "complete_release": "complete",
}


def lifecycle_next_action(project_dir: str) -> Dict[str, Any]:
    """The Cycle's next step, in the shape both MCP hosts' contracts expect.

    HERE, AND NOT IN EITHER HOST. Cursor's `spq_next.lifecycle_next_action` and
    Codex's `_spq_lifecycle_next_action` were a line-for-line pair, each
    branching on seven lifecycle states and each restating a barrier-criteria
    count in prose. Three encodings of one machine is the mechanism that
    shipped three different criteria counts in one product (`#640`), and two
    identical copies is the same mechanism one edit away.

    This function names NO stage. `DISCOVERY -> CYCLE -> ACCEPTANCE ->
    COMPLETE` lives in `cycle_lifecycle.TRANSITIONS` and the per-stage decision
    lives in `spq_state_machine.next_action`; branching on the stage again here
    would be the second encoding, one stage list shorter.

    What it adds is host contract, not method:

      `operation`       which ceremony tool to call, when the action is one
      `role`            the abbrev, for a `dispatch_<abbrev>` action
      `receipt_path`    where that dispatch's receipt must land
      `current_cycle`   the sequence a host tool response reports
      defaults          `story_id` and `human_gate_pending` are read
                        unconditionally by both hosts' `next_action` consumers

    `receipt_path` IS DERIVED FROM `spq_paths`, not rebuilt. Both hosts'
    `begin_lifecycle_dispatch` requires it and the kernel does not supply it;
    the previous copies each got it from a `checkpoint_readiness` that no longer
    exists, so each host was one derivation away from binding a dispatch to a
    path the gate does not read.
    """
    import spq_paths as _paths
    import spq_state_machine as spq

    state = spq.read_state(project_dir)
    action = spq.next_action(project_dir)
    name = str(action.get("action") or "")

    out = dict(action)
    out.setdefault("lifecycle_state", state.get("lifecycle_state") or "DISCOVERY")
    out["current_cycle"] = int(state.get("current_cycle") or 0)
    out.setdefault("story_id", None)
    out.setdefault("human_gate_pending", False)

    operation = CEREMONY_OPERATIONS.get(name)
    if operation:
        out["operation"] = operation

    if name.startswith("dispatch_"):
        # The kernel puts `role` on the action where it knows it. Only fill in
        # from the action name, never overwrite: re-deriving would disagree
        # with the kernel the first time an action name grows a suffix.
        out.setdefault("role", name[len("dispatch_"):])
        unit = str(out.get("story_id") or "")
        if unit and not out.get("receipt_path"):
            out["receipt_path"] = os.path.join(
                _paths.receipts_dir(project_dir, str(state.get("_cycle_id") or "")),
                "%s-%s.json" % (unit, out["role"]),
            )
    return out


# ── registry ────────────────────────────────────────────────────────────────

_BODIES: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "spq_get_cycle": _get_cycle,
    "spq_dependency_ledger": _dependency_ledger,
    "spq_manifest_validate": _manifest_validate,
    "spq_publish_event": _publish_event,
    "spq_refresh_ledger": _refresh_ledger,
    "spq_cut_work_unit": _cut_work_unit,
    "spq_external_intake_admit": _external_intake_admit,
}

_DESCRIPTIONS = {
    "spq_get_cycle": "Cycle identity, sealed-declaration state, and the board.",
    "spq_dependency_ledger": "Per-Work-Unit dependency resolution with reasons.",
    "spq_manifest_validate": "Verify the sealed Cycle manifest's hash and shape.",
    "spq_publish_event": "Record that a Work Unit reached a condition others depend on.",
    "spq_refresh_ledger": "Rebuild the local dependency-ledger cache from git.",
    "spq_cut_work_unit": "Cut a Work Unit from the Cycle (the §8.6 release valve).",
    "spq_external_intake_admit": (
        "Admit an artifact the platform did not produce as a Work Unit's "
        "candidate, fixing its digest over bytes read at intake."
    ),
}

_SCHEMAS = {
    "spq_get_cycle": mcp_transport.SPQ_CYCLE_SCHEMA,
    "spq_dependency_ledger": mcp_transport.SPQ_DEP_STATUS_SCHEMA,
    "spq_manifest_validate": mcp_transport.SPQ_CYCLE_SCHEMA,
    "spq_publish_event": mcp_transport.SPQ_LEDGER_APPEND_SCHEMA,
    "spq_refresh_ledger": mcp_transport.SPQ_CYCLE_SCHEMA,
    "spq_cut_work_unit": mcp_transport.SPQ_CUT_SCHEMA,
    "spq_external_intake_admit": mcp_transport.SPQ_EXTERNAL_INTAKE_SCHEMA,
}


def tools(
    *,
    policy_gate: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
    readiness_gate: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
) -> Dict[str, Dict[str, Any]]:
    """The registry a host merges into its own TOOLS map.

    Gates are passed in rather than imported, because they are the one genuinely
    host-specific part: Codex refuses regulated projects outright and re-checks
    execution readiness server-side, Cursor refuses BAA projects. Everything
    else is identical by construction.
    """

    def _wrap(name: str, body: Callable[[Dict[str, Any]], Dict[str, Any]]):
        def _run(args: Dict[str, Any]) -> Dict[str, Any]:
            project = _project(args)
            if policy_gate is not None:
                refusal = policy_gate(project)
                if refusal:
                    return dict(refusal, ok=False)
            if name in MUTATING and readiness_gate is not None:
                refusal = readiness_gate(project)
                if refusal:
                    return dict(refusal, ok=False)
            try:
                return body(args)
            except Exception as exc:  # noqa: BLE001 - a tool refuses, never raises
                return _refused("tool_error", "%s: %s" % (name, exc))

        return _run

    return {
        name: {
            "fn": _wrap(name, body),
            "description": _DESCRIPTIONS[name],
            "schema": _SCHEMAS[name],
        }
        for name, body in _BODIES.items()
    }
