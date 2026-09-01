#!/usr/bin/env python3
"""The Coordination Cycle: one release composed of several independent Cycles (#305).

A project can decompose one master specification into several child scopes that
run **independent SPQ Cycles in parallel** against one shared codebase, one git
remote and one release date. Platform Cycle 12, Contract Mastery Cycle 3 and EHR
Cycle 5 progress at different numbers and different cadences. EHR needs a
contract Contract Mastery publishes. Platform needs nothing from either and must
never wait on them.

## What this module owns, and what it deliberately does not

It owns the parent manifest (build / validate / seal / revise), the cross-Cycle
event ledger, and the cross-Cycle condition vocabulary. It does NOT own child
Work Unit dispatch: the parent coordinates declared dependency edges and the
chosen release contents, and nothing else. A parent that could dispatch into a
child would be a second scheduler for state the child's own barrier owns.

## Why the condition vocabulary is duplicated rather than shared

`spq_manifest.satisfies` special-cases `cycle_integrated` to satisfy only itself,
and `spq_ledger` puts it in `CLAIM_ONLY`. Both are on the intra-Cycle dispatch
path for every Cycle already running. Relaxing either to make a cross-Cycle edge
resolve would change how EVERY Cycle gates -- and the one knob that would let a
claim-only condition through, `accept_unverified_events`, is a single
manifest-wide boolean that would simultaneously let every bare `done` claim
satisfy an edge, which is exactly the guarantee #303 exists to provide.

So cross-Cycle strength is a different question answered in a different module.
`spq_ledger.publish`'s refusal of `cycle_integrated` stays exactly as written:
cross-Cycle publication is this module writing to a different tree, so there is
no bypass to build and no intra-Cycle behaviour to change.

## An event is a claim, not proof -- the same posture, one level up

    contract_published / artifact_published  verifiable: recompute the digest
                                             with the SAME `digest_script` the
                                             child barrier runs
    integrated / cycle_integrated            verifiable: ancestry against the
                                             child's integration ref, plus that
                                             child's own green barrier receipt
    environment_ready                        claim only -- asserted out of band

## Hashing and storage

The hash rule is `spq_manifest`'s, called not copied: one definition, both
documents. Local state lives under the gitignored
`.synaptory/.orchestrator/spq/coordination-cycles/<ccid>/`; the cross-clone
transport is the committed `.synaptory/coordination-cycles/<ccid>/`.

Python 3.9 compatible: this file is projected into every host package.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA_VERSION = "1.0"
KIND = "spq.coordination.manifest"
EVENT_KIND = "spq.coordination.event"
CHILD_STATES = ("included", "dropped")

# Conditions a cross-Cycle edge may declare. A superset of what a child may
# declare on its own `external` edge (`spq_manifest.CROSS_DECLARABLE_CONDITIONS`)
# is NOT wanted here: the two must agree, or a child edge can never match a
# parent edge. This is the same tuple, named locally so the parent manifest can
# be validated by a tool that does not carry the pipeline.
CROSS_CONDITIONS = (
    "integrated",
    "contract_published",
    "artifact_published",
    "cycle_integrated",
    "environment_ready",
)

# Strength order, declared EXPLICITLY. `cycle_integrated` outranks everything a
# single Work Unit can claim, because it is a statement about the whole child
# increment. `environment_ready` is ranked 0 and satisfies only itself: it has no
# verifiable definition anywhere in the epic, so it exists for forward
# compatibility without silently licensing dispatch.
# Mirrors `spq_manifest.CONDITION_RANK`'s ordering for the conditions they
# share: `integrated` implies the code is on the integration ref, which implies
# it was published, which implies it was built. An implicit ordering is how a
# weaker event comes to satisfy a stronger requirement without anyone deciding
# that it should.
CROSS_RANK = {
    "environment_ready": 0,
    "contract_published": 2,
    "artifact_published": 2,
    "integrated": 3,
    "cycle_integrated": 99,
}

VERIFIABLE = frozenset(
    {"contract_published", "artifact_published", "integrated", "cycle_integrated"}
)
CLAIM_ONLY = frozenset({"environment_ready"})

DEFAULT_MAX_LEDGER_AGE_S = 900


class CoordinationError(ValueError):
    """The coordination manifest is malformed, tampered with, or inconsistent."""


# ── hashing: `spq_manifest`'s rule, called not copied ───────────────────────


def canonical_bytes(manifest: Dict[str, Any]) -> bytes:
    import spq_manifest

    return spq_manifest.canonical_bytes(manifest)


def compute_hash(manifest: Dict[str, Any]) -> str:
    import spq_manifest

    return spq_manifest.compute_hash(manifest)


def verify_hash(manifest: Dict[str, Any]) -> bool:
    import spq_manifest

    return spq_manifest.verify_hash(manifest)


def seal(manifest: Dict[str, Any]) -> Dict[str, Any]:
    import spq_manifest

    return spq_manifest.seal(manifest)


def revise(
    sealed: Dict[str, Any], changes: Dict[str, Any], *, revised_by: str = ""
) -> Dict[str, Any]:
    """The next revision, recording the PRIOR HASH in `supersedes`.

    Delegates to `spq_manifest.revise` so "what a revision is" has one
    definition. Dropping a late child is exactly this and nothing more.
    """
    import spq_manifest

    return spq_manifest.revise(sealed, changes, revised_by=revised_by)


# ── construction ────────────────────────────────────────────────────────────


def _normalize_child(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "cycle_id": str(raw.get("cycle_id") or ""),
        # Informational. NEVER a key: cycle numbers are allocated per clone, so
        # two children can legitimately both be "Cycle 12".
        "cycle_seq": raw.get("cycle_seq"),
        "goal": str(raw.get("goal") or ""),
        "manifest_hash": str(raw.get("manifest_hash") or ""),
        "manifest_revision": raw.get("manifest_revision"),
        "integration_ref": str(raw.get("integration_ref") or ""),
        "selected_sha": str(raw.get("selected_sha") or ""),
        "state": str(raw.get("state") or "included"),
        "dropped_reason": str(raw.get("dropped_reason") or ""),
        "dropped_at": str(raw.get("dropped_at") or ""),
        "source_spec_refs": list(raw.get("source_spec_refs") or []),
        "outputs": [dict(o) for o in (raw.get("outputs") or [])],
    }


def _normalize_edge(raw: Dict[str, Any], index: int = 0) -> Dict[str, Any]:
    return {
        "id": str(raw.get("id") or "EDGE-%d" % (index + 1)),
        # An edge names DEPENDENCY DIRECTION, not data flow: the waiter is held
        # until the producer publishes. The earlier `from_`/`to_` spelling read
        # as flow and was described backwards on first contact, so the roles are
        # now in the field names themselves.
        "waiter_cycle_id": str(raw.get("waiter_cycle_id") or ""),
        "waiter_unit_id": str(raw.get("waiter_unit_id") or ""),
        "producer_cycle_id": str(raw.get("producer_cycle_id") or ""),
        "producer_unit_id": str(raw.get("producer_unit_id") or ""),
        "output": dict(raw["output"]) if isinstance(raw.get("output"), dict) else None,
        "condition": str(raw.get("condition") or "cycle_integrated"),
    }


def build(
    *,
    coordination_cycle_id: str,
    coordination_seq: int,
    release_goal: str,
    baseline_sha: str,
    integration_ref: str,
    children: Sequence[Dict[str, Any]],
    dependency_edges: Optional[Sequence[Dict[str, Any]]] = None,
    acceptance_criteria: Optional[Sequence[str]] = None,
    required_outputs: Optional[Sequence[Dict[str, Any]]] = None,
    verification: Optional[Dict[str, Any]] = None,
    release_policy: Optional[Dict[str, Any]] = None,
    source_spec_refs: Optional[Sequence[str]] = None,
    project: str = "",
    created_at: str = "",
    created_by: str = "",
    runner_id: str = "",
    manifest_revision: int = 1,
    supersedes: Optional[str] = None,
    revision_reason: str = "",
) -> Dict[str, Any]:
    """Assemble an UNSEALED coordination manifest. Call `seal` to hash it."""
    import spq_paths

    # Derived defensively: `validate` is what REPORTS a malformed identity, and
    # it has to be able to inspect a document read off a git ref that this clone
    # did not author. Raising here would turn "your release manifest is invalid"
    # into a traceback at read time.
    try:
        rid = spq_paths.release_id(str(coordination_cycle_id))
    except Exception:  # noqa: BLE001
        rid = ""

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "coordination_cycle_id": str(coordination_cycle_id),
        "coordination_seq": int(coordination_seq),
        # Recorded rather than derived at read time, so the id -> receipt-id
        # mapping is auditable from the document itself.
        "release_id": rid,
        "manifest_revision": int(manifest_revision),
        "supersedes": supersedes,
        "revision_reason": str(revision_reason),
        "project": str(project),
        "release_goal": str(release_goal),
        "acceptance_criteria": list(acceptance_criteria or []),
        "baseline_sha": str(baseline_sha),
        "integration_ref": str(integration_ref),
        "created_at": str(created_at),
        "created_by": str(created_by),
        "runner_id": str(runner_id),
        # Traceability ONLY. Nothing here or in `spq_paths` may use it to select
        # a path, a cache key or a command (#305 AC).
        "source_spec_refs": list(source_spec_refs or []),
        "children": [_normalize_child(c) for c in children],
        "dependency_edges": [
            _normalize_edge(e, i) for i, e in enumerate(dependency_edges or [])
        ],
        "required_outputs": [dict(r) for r in (required_outputs or [])],
        "verification": dict(
            verification
            or {
                "require_composition_regression": True,
                "composition_regression_script": "scripts/coordination-regression.sh",
                "digest_script": "scripts/shared-digest.sh",
            }
        ),
        "release_policy": dict(
            release_policy
            or {
                "allow_drop": True,
                "drop_requires_revision": True,
                "accept_unverified_events": False,
            }
        ),
    }


# ── reading ─────────────────────────────────────────────────────────────────


def children_index(manifest: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        str(c.get("cycle_id")): c
        for c in (manifest.get("children") or [])
        if c.get("cycle_id")
    }


def included_children(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        c for c in (manifest.get("children") or [])
        if str(c.get("state") or "included") == "included"
    ]


def edges(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(manifest.get("dependency_edges") or [])


def gating_edges(manifest: Dict[str, Any], cycle_id: str) -> List[Dict[str, Any]]:
    """Edges on which `cycle_id` WAITS.

    A child with no gating edge is gated by nothing, by construction. This is the
    function that makes "unrelated child Cycles are not forced through a single
    global readiness barrier" a property of the data rather than an emergent
    behaviour nobody can point at.
    """
    return [e for e in edges(manifest) if str(e.get("waiter_cycle_id")) == str(cycle_id)]


def downstream_edges(manifest: Dict[str, Any], cycle_id: str) -> List[Dict[str, Any]]:
    """Edges for which `cycle_id` is the PRODUCER."""
    return [e for e in edges(manifest) if str(e.get("producer_cycle_id")) == str(cycle_id)]


def edge_key(edge: Dict[str, Any]) -> str:
    """The address an event must carry to satisfy this edge."""
    import spq_manifest

    return spq_manifest.cross_ref_key(
        {
            "cycle_id": edge.get("producer_cycle_id"),
            "unit_id": edge.get("producer_unit_id"),
            "output": edge.get("output"),
        }
    )


def declared_keys(manifest: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Address -> the edges that declare it.

    An event whose address is NOT here must never resolve anything: the parent
    has not admitted that coordination, and admitting one is a parent-manifest
    revision, not an event.
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    for edge in edges(manifest):
        out.setdefault(edge_key(edge), []).append(edge)
    for req in manifest.get("required_outputs") or []:
        import spq_manifest

        key = spq_manifest.cross_ref_key(req)
        out.setdefault(key, [])
    return out


def cross_satisfies(declared: str, observed: str) -> bool:
    """Does an `observed` condition meet a `declared` one, cross-Cycle?

    Separate from `spq_manifest.satisfies` on purpose -- see the module
    docstring. `environment_ready` and `cycle_integrated` satisfy only
    themselves; the rest compare by explicit rank.
    """
    if declared not in CROSS_RANK or observed not in CROSS_RANK:
        return False
    if declared in ("environment_ready", "cycle_integrated"):
        return declared == observed
    if observed in ("environment_ready",):
        return declared == observed
    return CROSS_RANK[observed] >= CROSS_RANK[declared]


def satisfies_set(observed: str) -> List[str]:
    """Every condition `observed` would satisfy.

    Precomputed here so `story_pipeline.story_dep_status` can do MEMBERSHIP
    rather than ranking, and therefore needs no import from this module and stays
    a pure function of its arguments.
    """
    return sorted(c for c in CROSS_CONDITIONS if cross_satisfies(c, observed))


def read_manifest(project_dir: str, coordination_cycle_id: str) -> Dict[str, Any]:
    """The sealed parent manifest: local copy first, then the committed one.

    The committed fallback is the point of the transport. A clone that has never
    opened this Coordination Cycle -- a child workstream clone, say -- has no
    local copy and no index entry, so the committed manifest is the only place
    the parent's declared edges travel.
    """
    import spq_paths
    import state_store

    for path in (
        spq_paths.coordination_manifest_path(project_dir, coordination_cycle_id),
        spq_paths.committed_coordination_manifest_path(
            project_dir, coordination_cycle_id
        ),
    ):
        found = state_store.read_json(path)
        if found:
            return found
    return {}


# ── validation ──────────────────────────────────────────────────────────────


def validate(
    manifest: Dict[str, Any],
    *,
    project_dir: Optional[str] = None,
    require_baseline: bool = True,
) -> List[str]:
    """Return a list of problems; empty means valid.

    Returns rather than raises, matching `spq_manifest.validate`: an operator
    fixing a release manifest one refusal at a time is a bad loop.
    """
    import spq_paths

    problems: List[str] = []

    if manifest.get("kind") != KIND:
        problems.append("kind must be %r, got %r" % (KIND, manifest.get("kind")))
    if manifest.get("schema_version") != SCHEMA_VERSION:
        problems.append(
            "schema_version must be %r, got %r"
            % (SCHEMA_VERSION, manifest.get("schema_version"))
        )

    ccid = str(manifest.get("coordination_cycle_id") or "")
    try:
        spq_paths.valid_coordination_cycle_id(ccid)
    except Exception as exc:  # noqa: BLE001
        problems.append("coordination_cycle_id %r is invalid: %s" % (ccid, exc))
    else:
        declared_seq = manifest.get("coordination_seq")
        actual = spq_paths.coordination_seq_of(ccid)
        if declared_seq is not None and int(declared_seq) != actual:
            problems.append(
                "coordination_seq %r disagrees with coordination_cycle_id %r. "
                "The seq is the receipt-facing projection (RELEASE-%d), so a "
                "mismatch means the receipts name a different release than the "
                "storage." % (declared_seq, ccid, actual)
            )

    if require_baseline and not manifest.get("baseline_sha"):
        problems.append(
            "baseline_sha is required: a release without a baseline cannot prove "
            "what it composed"
        )
    if not manifest.get("integration_ref"):
        problems.append("integration_ref is required")

    children = manifest.get("children") or []
    if not children:
        problems.append(
            "children[] is empty: a Coordination Cycle with no child Cycles "
            "coordinates nothing"
        )

    seen: set = set()
    for child in children:
        cid = str(child.get("cycle_id") or "")
        if not cid:
            problems.append("a child entry has no cycle_id")
            continue
        try:
            spq_paths.valid_cycle_id(cid)
        except Exception as exc:  # noqa: BLE001
            problems.append("child cycle_id %r is invalid: %s" % (cid, exc))
            continue
        if cid in seen:
            problems.append("duplicate child cycle_id %r" % cid)
        seen.add(cid)

        state = str(child.get("state") or "")
        if state not in CHILD_STATES:
            problems.append(
                "child %r has state %r; expected one of %s"
                % (cid, state, ", ".join(CHILD_STATES))
            )
        if state == "dropped" and not child.get("dropped_reason"):
            problems.append(
                "child %r is dropped with no reason. Dropping a child changes "
                "what ships, so the record has to say why." % cid
            )
        if state == "included":
            if not str(child.get("manifest_hash") or "").startswith("sha256:"):
                problems.append(
                    "child %r has no sealed manifest_hash. Pinning a child by "
                    "number rather than by hash is what makes 'the release "
                    "contains exactly this' unprovable." % cid
                )
            if not child.get("selected_sha"):
                problems.append(
                    "child %r is included with no selected_sha: there is no "
                    "exact increment to compose." % cid
                )
            if not child.get("integration_ref"):
                problems.append("child %r has no integration_ref" % cid)

    edge_ids: set = set()
    for edge in edges(manifest):
        eid = str(edge.get("id") or "")
        if not eid:
            problems.append("a dependency edge has no id")
        elif eid in edge_ids:
            problems.append("duplicate dependency edge id %r" % eid)
        edge_ids.add(eid)

        waiter = str(edge.get("waiter_cycle_id") or "")
        producer_id = str(edge.get("producer_cycle_id") or "")
        for role, cid in (("waiter_cycle_id", waiter), ("producer_cycle_id", producer_id)):
            if not cid:
                problems.append("edge %r has no %s" % (eid, role))
            elif cid not in seen:
                problems.append(
                    "edge %r names %s %r, which is not a child of this "
                    "Coordination Cycle. An edge that references a Cycle the "
                    "release does not contain can never resolve."
                    % (eid, role, cid)
                )
        if waiter and producer_id and waiter == producer_id:
            problems.append(
                "edge %r has the same Cycle on both ends; an intra-Cycle "
                "dependency belongs in that Cycle's own manifest" % eid
            )

        condition = str(edge.get("condition") or "")
        if condition not in CROSS_CONDITIONS:
            problems.append(
                "edge %r declares unknown condition %r (known: %s)"
                % (eid, condition, ", ".join(CROSS_CONDITIONS))
            )
        if not edge.get("producer_unit_id") and not isinstance(edge.get("output"), dict):
            problems.append(
                "edge %r names neither a producing unit nor an output, so there "
                "is nothing for an event to be about" % eid
            )

        by_id = children_index(manifest)
        consumer = by_id.get(waiter) or {}
        producer = by_id.get(producer_id) or {}
        # Only when someone is still WAITING. A dropped consumer no longer waits
        # on anything, so its edges are dead rather than violated -- and
        # `drop_child` prunes them. Enforcing the producer side against a dropped
        # consumer would make "drop the consumer, then drop the producer"
        # impossible, which is the ordinary way a whole branch of a release is
        # descoped.
        if (
            producer
            and str(producer.get("state")) == "dropped"
            and str(consumer.get("state") or "included") == "included"
        ):
            problems.append(
                "edge %r depends on child %r, which is dropped from this "
                "release. Dropping a producer while a consumer still waits on "
                "it makes the consumer unsatisfiable." % (eid, producer_id)
            )

    if not problems:
        try:
            topo_order(manifest)
        except CoordinationError as exc:
            problems.append(str(exc))

    if project_dir and require_baseline:
        sha = str(manifest.get("baseline_sha") or "")
        if sha and not _sha_exists(project_dir, sha):
            problems.append(
                "baseline_sha %s is not reachable in this checkout; fetch the "
                "release baseline before composing against it" % sha[:12]
            )

    return problems


def topo_order(manifest: Dict[str, Any]) -> List[str]:
    """Child cycle ids in dependency order. Raises on a cross-Cycle cycle.

    A cross-Cycle dependency loop is unsatisfiable by construction, so it is
    caught when the release is opened rather than discovered at the barrier
    after everyone has built against it.
    """
    by_id = children_index(manifest)
    waits_on: Dict[str, List[str]] = {cid: [] for cid in by_id}
    for edge in edges(manifest):
        waiter = str(edge.get("waiter_cycle_id"))
        producer = str(edge.get("producer_cycle_id"))
        if waiter in waits_on and producer in by_id:
            waits_on[waiter].append(producer)

    state: Dict[str, int] = {}
    order: List[str] = []

    def visit(cid: str, trail: List[str]) -> None:
        mark = state.get(cid, 0)
        if mark == 2:
            return
        if mark == 1:
            raise CoordinationError(
                "cross-Cycle dependency loop: %s" % " -> ".join(trail + [cid])
            )
        state[cid] = 1
        for target in waits_on.get(cid, []):
            visit(target, trail + [cid])
        state[cid] = 2
        order.append(cid)

    for cid in by_id:
        visit(cid, [])
    return order


def _sha_exists(project_dir: str, sha: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "cat-file", "-e", "%s^{commit}" % sha],
            cwd=str(project_dir),
            capture_output=True,
            timeout=15,
        )
    except Exception:  # noqa: BLE001 - no git, no verdict
        return True
    return result.returncode == 0


# ── lifecycle ───────────────────────────────────────────────────────────────


def _transport_ignored(project_dir: str, relpath: str) -> bool:
    import spq_state_machine

    return spq_state_machine.transport_ignored(project_dir, relpath)


def open_coordination(
    project_dir: str,
    *,
    release_goal: str,
    children: Sequence[Dict[str, Any]],
    dependency_edges: Optional[Sequence[Dict[str, Any]]] = None,
    coordination_seq: Optional[int] = None,
    baseline_sha: str = "",
    integration_ref: str = "",
    created_by: str = "",
    runner_id: str = "",
    project: str = "",
    **extra: Any,
) -> Dict[str, Any]:
    """Seal and publish a Coordination Cycle manifest.

    One atomic step: the manifest, the index entry and the pin are written
    together, so a crash cannot leave a clone pointing at a release whose
    manifest was never sealed.
    """
    import spq_paths
    import state_store

    if coordination_seq is None:
        coordination_seq = spq_paths.next_coordination_seq(project_dir)
    created_at = state_store.now_iso()
    ccid = spq_paths.new_coordination_cycle_id(
        int(coordination_seq),
        baseline_sha=baseline_sha,
        goal=release_goal,
        created_at=created_at,
        project_slug=project,
    )
    if not integration_ref:
        integration_ref = spq_paths.coordination_integration_branch(ccid)

    manifest = seal(
        build(
            coordination_cycle_id=ccid,
            coordination_seq=int(coordination_seq),
            release_goal=release_goal,
            baseline_sha=baseline_sha,
            integration_ref=integration_ref,
            children=children,
            dependency_edges=dependency_edges,
            created_at=created_at,
            created_by=created_by,
            runner_id=runner_id,
            project=project,
            **extra,
        )
    )
    problems = validate(manifest, project_dir=project_dir, require_baseline=bool(baseline_sha))
    if problems:
        raise CoordinationError(
            "refusing to open Coordination Cycle %s: the manifest is invalid.\n  - %s"
            % (ccid, "\n  - ".join(problems))
        )

    # The committed copy is the ONLY way this reaches a child clone. Refusing an
    # ignored transport here rather than discovering it at the barrier: a release
    # manifest that writes cleanly and is invisible everywhere else is the worst
    # possible failure mode, because the parent looks correct locally.
    published = spq_paths.committed_coordination_manifest_path(project_dir, ccid)
    relpath = os.path.relpath(published, project_dir)
    if _transport_ignored(project_dir, relpath):
        raise CoordinationError(
            "refusing to open Coordination Cycle %s: %s is excluded by "
            ".gitignore, so the release manifest would never reach a child "
            "clone. Add the narrow un-ignore:\n\n    .synaptory/*\n"
            "    !.synaptory/sync/\n    !.synaptory/cycles/\n"
            "    !.synaptory/coordination-cycles/\n\nThe trailing `/*` is "
            "load-bearing: git cannot re-include a path whose parent directory "
            "is excluded." % (ccid, relpath)
        )

    local = spq_paths.coordination_manifest_path(project_dir, ccid)
    os.makedirs(os.path.dirname(local), exist_ok=True)
    state_store.write_json_atomic(local, manifest, mode=0o444)
    # The committed copy is load-bearing, not best-effort: the `_transport_ignored`
    # check above refuses to open a release whose transport is merely gitignored,
    # so swallowing an actual write failure here would wave through the identical
    # end state by a different route -- a release that is active in the index,
    # pinned, and reported to the CP, which no child clone can ever read.
    # Fail before any of that is advanced, and take the orphan local manifest
    # with us so a retry starts clean.
    try:
        os.makedirs(os.path.dirname(published), exist_ok=True)
        state_store.write_json_atomic(published, manifest)
    except OSError as exc:
        # Roll the local write back COMPLETELY. Removing the manifest but
        # leaving its directory behind still leaves a `cc-…` entry on disk for
        # a release that was never opened, which reads as half-open state to
        # anyone (or any test) listing the store.
        try:
            os.chmod(local, 0o644)
            os.remove(local)
        except OSError:
            pass
        try:
            os.rmdir(os.path.dirname(local))
        except OSError:
            pass  # not empty -- something else lives there, leave it alone
        raise CoordinationError(
            "refusing to open Coordination Cycle %s: the committed release "
            "manifest could not be written to %s (%s). That file is the only "
            "way this release reaches a child clone, so the Coordination Cycle "
            "has NOT been opened -- no index entry, no pin, nothing reported. "
            "Fix the write error and re-run." % (ccid, relpath, exc)
        )

    spq_paths.record_coordination_cycle(
        project_dir, ccid, seq=int(coordination_seq),
        release_goal=release_goal, opened_at=created_at,
    )
    spq_paths.write_coordination_pin(project_dir, ccid)
    _observe(manifest, project_dir)
    return manifest


def _observe(manifest: Dict[str, Any], project_dir: str) -> None:
    """Report the release to the control plane. Never raises, never blocks.

    Git is authoritative for a release; this is the observer copy, and a
    failure to ship it is a reporting gap rather than a delivery one.

    Reports the document in hand, THEN retries anything an earlier attempt could
    not ship (#334 G8). Both halves are needed: `_rewrite_sealed` overwrites the
    sealed file, so a revision that never shipped cannot be recovered from disk
    afterwards — the direct emission is its only chance — while the sweep is what
    turns a transient failure into a delay instead of a permanent loss.
    """
    try:
        import manifest_emitter

        manifest_emitter.emit_coordination_manifest(
            manifest, project_dir=project_dir
        )
        manifest_emitter.resend_pending(project_dir)
    except Exception:  # noqa: BLE001
        pass


def _rewrite_sealed(project_dir: str, ccid: str, revised: Dict[str, Any]) -> Dict[str, Any]:
    import spq_paths
    import state_store

    local = spq_paths.coordination_manifest_path(project_dir, ccid)
    prior = None
    if os.path.exists(local):
        try:
            with open(local, encoding="utf-8") as fh:
                prior = fh.read()
        except OSError:
            prior = None
        os.chmod(local, 0o644)
    state_store.write_json_atomic(local, revised, mode=0o444)
    # Same argument as the open path, one step sharper: if the committed copy
    # keeps the PREVIOUS revision while the local copy advances, every child
    # reads a release the parent no longer believes in -- including one that
    # still contains a child the revision just dropped. Restore the local copy
    # and refuse, rather than leaving the two out of step.
    try:
        published = spq_paths.committed_coordination_manifest_path(project_dir, ccid)
        os.makedirs(os.path.dirname(published), exist_ok=True)
        state_store.write_json_atomic(published, revised)
    except OSError as exc:
        # Two rollback shapes, and the second one is easy to miss.
        # `read_manifest` deliberately supports a clone whose ONLY copy is the
        # committed transport, so `prior` is None there -- and skipping the
        # rollback in that case left a brand-new local revision 2 beside a
        # committed revision 1. Reads prefer the local copy, so that clone then
        # believes in a release no child can see (#305 review, P1).
        rollback_failed = ""
        try:
            os.chmod(local, 0o644)
            if prior is None:
                os.remove(local)
            else:
                with open(local, "w", encoding="utf-8") as fh:
                    fh.write(prior)
                os.chmod(local, 0o444)
        except OSError as rb:
            # Never silent: a failed rollback is precisely the split state this
            # block exists to prevent, and the operator has to be told which
            # file to inspect.
            rollback_failed = (
                " ROLLBACK ALSO FAILED (%s): %s may now hold a revision the "
                "committed transport does not. Inspect it before continuing."
                % (rb, local)
            )
        raise CoordinationError(
            "refusing to revise Coordination Cycle %s: the committed release "
            "manifest could not be written (%s). The local manifest has been "
            "rolled back; nothing was reported. Fix the write error and "
            "re-run.%s" % (ccid, exc, rollback_failed)
        )
    # A REVISION is a new row, not an update -- dropping a late child has to be
    # visible as its own record, or the CP cannot say what the release contained
    # before the drop.
    _observe(revised, project_dir)
    return revised


def drop_child(
    project_dir: str,
    *,
    coordination_cycle_id: str,
    cycle_id: str,
    reason: str,
    revised_by: str = "",
) -> Dict[str, Any]:
    """Remove a late child from the release. A PARENT-SIDE EDIT ONLY.

    Nothing in this function opens the child's manifest, execution state or
    receipts. That is the whole requirement: "a late child can be removed from
    the parent manifest when release policy permits, without corrupting other
    child Cycle state." The child keeps running its own Cycle; it simply is not
    in this release.
    """
    import state_store

    if not reason:
        raise CoordinationError(
            "dropping a child changes what ships; --reason is required so the "
            "release record says why"
        )
    manifest = read_manifest(project_dir, coordination_cycle_id)
    if not manifest:
        raise CoordinationError(
            "no manifest for Coordination Cycle %s" % coordination_cycle_id
        )
    if not verify_hash(manifest):
        raise CoordinationError(
            "refusing to revise Coordination Cycle %s: the manifest does not "
            "match its own hash" % coordination_cycle_id
        )
    policy = manifest.get("release_policy") or {}
    if not policy.get("allow_drop", True):
        raise CoordinationError(
            "release policy forbids dropping a child from Coordination Cycle %s"
            % coordination_cycle_id
        )

    by_id = children_index(manifest)
    if str(cycle_id) not in by_id:
        raise CoordinationError(
            "%s is not a child of Coordination Cycle %s"
            % (cycle_id, coordination_cycle_id)
        )
    if str(by_id[str(cycle_id)].get("state")) == "dropped":
        return manifest

    dropped_at = state_store.now_iso()
    children = []
    for child in manifest.get("children") or []:
        entry = dict(child)
        if str(entry.get("cycle_id")) == str(cycle_id):
            entry["state"] = "dropped"
            entry["dropped_reason"] = reason
            entry["dropped_at"] = dropped_at
            entry["selected_sha"] = ""
        children.append(entry)

    # A dropped child no longer waits on anything, so its OUTBOUND edges go with
    # it. Leaving them would keep the release gated on a dependency nobody has.
    # Its INBOUND edges (where it was the producer) are left for `validate` to
    # refuse, because someone else is still waiting on output that will not ship.
    surviving = [
        e for e in edges(manifest)
        if str(e.get("waiter_cycle_id")) != str(cycle_id)
    ]
    pruned = len(edges(manifest)) - len(surviving)

    revised = revise(
        manifest,
        {
            "children": children,
            "dependency_edges": surviving,
            "revision_reason": "dropped %s: %s%s"
            % (cycle_id, reason, " (%d edge(s) pruned)" % pruned if pruned else ""),
        },
        revised_by=revised_by,
    )
    problems = validate(revised, require_baseline=False)
    if problems:
        raise CoordinationError(
            "refusing to drop %s from %s: the resulting manifest is invalid.\n"
            "  - %s\n\nA consumer still waiting on this child is the usual "
            "cause: drop the consumer too, or re-point its edge."
            % (cycle_id, coordination_cycle_id, "\n  - ".join(problems))
        )
    return _rewrite_sealed(project_dir, coordination_cycle_id, revised)


def status(
    project_dir: str, coordination_cycle_id: Optional[str] = None
) -> Dict[str, Any]:
    """What this clone knows about the release, with no network access."""
    import spq_paths

    ccid = spq_paths.resolve_coordination_id(
        project_dir, coordination_cycle_id=coordination_cycle_id
    )
    if not ccid:
        return {
            "coordination_cycle_id": None,
            "detail": (
                "this clone is not part of a Coordination Cycle. Open one with "
                "open_coordination_cycle, or pass --coordination-cycle-id."
            ),
        }
    manifest = read_manifest(project_dir, ccid)
    if not manifest:
        return {
            "coordination_cycle_id": ccid,
            "manifest_present": False,
            "detail": (
                "Coordination Cycle %s is pinned here but no manifest is "
                "readable. Pull the release branch: the committed manifest is "
                "the only copy that travels." % ccid
            ),
        }
    return {
        "coordination_cycle_id": ccid,
        "coordination_seq": manifest.get("coordination_seq"),
        "release_id": manifest.get("release_id"),
        "release_goal": manifest.get("release_goal"),
        "manifest_present": True,
        "manifest_hash": manifest.get("manifest_hash"),
        "manifest_revision": manifest.get("manifest_revision"),
        "manifest_hash_verified": verify_hash(manifest),
        "integration_ref": manifest.get("integration_ref"),
        "children": [
            {
                "cycle_id": c.get("cycle_id"),
                "cycle_seq": c.get("cycle_seq"),
                "goal": c.get("goal"),
                "state": c.get("state"),
                "manifest_hash": c.get("manifest_hash"),
                "selected_sha": c.get("selected_sha"),
                "gating_edges": [e["id"] for e in gating_edges(manifest, str(c.get("cycle_id")))],
            }
            for c in manifest.get("children") or []
        ],
        "dependency_edges": edges(manifest),
    }


# ── the cross-Cycle event ledger ────────────────────────────────────────────
#
# The committed `.synaptory/coordination-cycles/<ccid>/events/<child-cycle-id>/`
# files are the SOURCE OF TRUTH; `cross-cycle-ledger.json` in the orchestrator
# tree is a local materialized cache with a `refreshed_at`. The cache is never
# authoritative and can be deleted safely.
#
# Propagation is human-gated by the project's own rules: `modes/init.md` forbids
# agents committing or pushing, so every cross-Cycle unblock has a mandatory
# human push in the middle. `publish` therefore prints the exact command, and
# `local_only_children` exists so "written but not pushed" never looks like
# "nothing to see yet".


def event_id(event: Dict[str, Any]) -> str:
    """Content-addressed, excluding the id itself.

    Sixteen hex characters, matching `spq_ledger.event_id`, so both ledgers map
    onto the control plane's `client_event_id` idempotency index the same way.
    """
    import hashlib

    body = {k: v for k, v in event.items() if k != "event_id"}
    raw = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def build_event(
    *,
    coordination_cycle_id: str,
    coordination_manifest_hash: str,
    cycle_id: str,
    cycle_manifest_hash: str,
    condition: str,
    cycle_seq: Optional[int] = None,
    unit_id: str = "",
    output: Optional[Dict[str, Any]] = None,
    commit_sha: str = "",
    integration_ref: str = "",
    workstream_id: str = "",
    verification: Optional[Dict[str, Any]] = None,
    published_at: str = "",
    published_by: str = "",
    runner_id: str = "",
    seq: int = 0,
) -> Dict[str, Any]:
    event = {
        "schema_version": SCHEMA_VERSION,
        "kind": EVENT_KIND,
        "seq": int(seq),
        "coordination_cycle_id": str(coordination_cycle_id),
        # The PARENT revision this was published against. An event recorded
        # against a superseded parent may describe a child that has since been
        # dropped, so it must not silently satisfy an edge in a later revision.
        "coordination_manifest_hash": str(coordination_manifest_hash),
        "cycle_id": str(cycle_id),
        "cycle_seq": cycle_seq,
        "cycle_manifest_hash": str(cycle_manifest_hash),
        "workstream_id": str(workstream_id),
        "runner_id": str(runner_id),
        "unit_id": str(unit_id),
        "condition": str(condition),
        "output": dict(output) if output else None,
        "commit_sha": str(commit_sha),
        "integration_ref": str(integration_ref),
        "verification": dict(verification) if verification else None,
        "published_at": str(published_at),
        "published_by": str(published_by),
    }
    event["event_id"] = event_id(event)
    return event


def _git(project_dir: str, *args: str) -> Tuple[int, str, str]:
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(project_dir), capture_output=True,
            text=True, timeout=120,
        )
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)
    return result.returncode, result.stdout, result.stderr


# ── the child's own Sync evidence, re-read (never re-run) ───────────────────
#
# ADR-030 says `cycle_integrated` is proven by "ancestry plus the child's own
# green SYNC-{seq}-barrier.json". That artifact is a RECEIPT, written by
# `sync_barrier._write_barrier_receipt` into `spq_paths.receipts_dir`, which
# resolves under `.synaptory/.orchestrator/spq/` -- the gitignored local tree.
# It never travels, so a parent clone cannot read it and the ADR's literal
# wording is not implementable. What does travel is the per-workstream
# readiness records the child commits to `.synaptory/cycles/<cid>/sync/`, and
# they carry the evidence the barrier itself judged.
#
# So the parent re-reads THOSE, and only the criteria that survive the trip:
#
#   records_present     at least one committed record
#   manifest_agreement  every record names the pinned child manifest hash
#   manifest_closure    every admitted Work Unit is done or cut
#   dependency_closure  no record reports an unsatisfied dependency
#   regression_green    no record reports a regression that ran and failed
#
# Deliberately NOT re-read: `branches_merged`, `journey_green` and
# `digests_match` are computed by executing scripts against an integrated tree.
# Re-running them here would make the parent re-run the child's barrier, which
# is exactly what this module must not do -- and the merge question is already
# answered independently by `selected_sha_on_child_integration`, which proves
# the pinned SHA is an ancestor of the child's integration ref.
SYNC_EVIDENCE_CRITERIA = (
    "manifest_sealed",
    "records_present",
    "manifest_agreement",
    "manifest_closure",
    "dependency_closure",
    "regression_green",
)


def sync_evidence_verdict(
    records: Dict[str, Any],
    *,
    child_manifest: Optional[Dict[str, Any]] = None,
    pinned_manifest_hash: str = "",
) -> Dict[str, Any]:
    """Judge a child's committed readiness records. PURE -- no git, no disk.

    Returns `{"verdict": "green"|"blocked", "blocking": [...], ...}`. A caller
    that could not READ the records passes `{}` and gets `blocked` with
    `records_present` blocking: absence of evidence is not evidence, which is
    the rule `sync_barrier` already applies to a skipped journey.

    `child_manifest` is the child's SEALED manifest, and it is REQUIRED for a
    green verdict. Without it the two questions that matter cannot be asked at
    all: which workstreams owed a record, and which Work Units the Cycle
    admitted. An earlier cut of this function took only the records, so
    `records_present` degraded to "at least one record exists" and
    `manifest_closure` compared each record's admitted set against its own
    done/cut set -- self-consistent by construction. A two-workstream Cycle
    with one committed record read as "green across spine" (#305 review, P1).
    """
    blocking: List[str] = []
    detail: List[str] = []

    if not records:
        return {
            "verdict": "blocked",
            "blocking": ["records_present"],
            "workstreams": [],
            "expected_workstreams": [],
            "manifest_hashes": [],
            "detail": ["no committed readiness record for this Cycle"],
            "criteria_read": list(SYNC_EVIDENCE_CRITERIA),
        }

    # ── the quorum ─────────────────────────────────────────────────────────
    # `sync_barrier` expects a record from every workstream EXCEPT the
    # integration one (see its `_declaring_workstreams`). The parent re-reads
    # that same rule off the sealed manifest rather than inventing a weaker one.
    expected: List[str] = []
    if child_manifest is None:
        blocking.extend(["manifest_sealed", "records_present"])
        detail.append(
            "the child's sealed manifest is unavailable, so the expected "
            "workstream quorum cannot be established; a record count proves "
            "nothing on its own"
        )
    else:
        # RECOMPUTE the seal. Comparing the document's own `manifest_hash`
        # field against the pin only proves the file still CLAIMS that hash --
        # editing the workstream list while leaving the field alone kept the
        # claim intact and shrank the quorum to whatever remained (#305 review).
        import spq_manifest as _mf

        if not _mf.verify_hash(child_manifest):
            blocking.append("manifest_sealed")
            detail.append(
                "the child's manifest does not match its own hash: it has been "
                "modified since it was sealed, so nothing in it can be trusted "
                "to define the quorum or the admitted set"
            )

        expected = sorted(
            str(w.get("id") or "")
            for w in (child_manifest.get("workstreams") or [])
            if not w.get("integration") and w.get("id")
        )
        if not expected:
            # `sync_barrier.collect` refuses this outright -- "an empty quorum
            # is never present". A manifest declaring only an integration
            # workstream cannot be green, and treating it as vacuously
            # satisfied let a stray record carry a whole release.
            blocking.append("records_present")
            detail.append(
                "the child manifest declares no delivery workstream, so there "
                "is no quorum to satisfy; the child's own barrier refuses this "
                "shape rather than passing it"
            )
        else:
            # EXACT equality, not "nothing missing". A record from a workstream
            # the manifest does not declare is evidence about a Cycle other
            # than the one pinned, and must not count toward this one.
            missing = [ws for ws in expected if ws not in records]
            unexpected = sorted(ws for ws in records if ws not in expected)
            if missing or unexpected:
                blocking.append("records_present")
                if missing:
                    detail.append(
                        "no readiness record from %s -- the child's own barrier "
                        "would block on the same absence" % ", ".join(missing)
                    )
                if unexpected:
                    detail.append(
                        "readiness record(s) from %s, which the child manifest "
                        "does not declare as a delivery workstream"
                        % ", ".join(unexpected)
                    )

    # ── manifest agreement ─────────────────────────────────────────────────
    hashes = sorted(
        {str(r.get("manifest_hash") or "") for r in records.values() if r.get("manifest_hash")}
    )
    if pinned_manifest_hash:
        mismatched = sorted(
            ws for ws, r in records.items()
            if str(r.get("manifest_hash") or "") != str(pinned_manifest_hash)
        )
        if mismatched:
            blocking.append("manifest_agreement")
            detail.append(
                "readiness records from %s do not name the pinned manifest %s"
                % (", ".join(mismatched), str(pinned_manifest_hash)[:19])
            )
        if child_manifest is not None and str(
            child_manifest.get("manifest_hash") or ""
        ) != str(pinned_manifest_hash):
            blocking.append("manifest_agreement")
            detail.append(
                "the child's sealed manifest is %s, not the pinned %s"
                % (str(child_manifest.get("manifest_hash") or "")[:19],
                   str(pinned_manifest_hash)[:19])
            )
    elif len(hashes) > 1:
        blocking.append("manifest_agreement")
        detail.append("readiness records disagree on the manifest hash: %s" % ", ".join(hashes))

    # ── closure against the SEALED admitted set, by owner ──────────────────
    # Comparing a record's admitted list against its own done/cut list is
    # self-consistent by construction and proves nothing. The admitted set is
    # the manifest's, and each Work Unit is settled by the workstream that
    # OWNS it -- a unit cannot be signed off by a sibling.
    if child_manifest is not None:
        for unit in child_manifest.get("work_units") or []:
            uid = str(unit.get("id") or "")
            if not uid:
                continue
            owner = str(unit.get("owner_workstream") or "")
            rec = records.get(owner)
            if rec is None:
                # Previously skipped on the assumption that `records_present`
                # had already caught it. It has not when the owner is not a
                # DECLARED delivery workstream at all -- an unowned unit, or one
                # assigned to the integration workstream, owes no record and so
                # could never be settled by anyone. That is unclosable, not
                # closed.
                if "manifest_closure" not in blocking:
                    blocking.append("manifest_closure")
                detail.append(
                    "%s is admitted but owned by %r, which owes no readiness "
                    "record, so nothing can ever report it done or cut"
                    % (uid, owner or "(unowned)")
                )
                continue
            ids = rec.get("work_unit_ids") or {}
            if uid not in set(ids.get("done") or []) | set(ids.get("cut") or []):
                if "manifest_closure" not in blocking:
                    blocking.append("manifest_closure")
                detail.append(
                    "%s is admitted by the sealed manifest and owned by %s, "
                    "which reports it neither done nor cut" % (uid, owner or "?")
                )

    for ws in sorted(records):
        rec = records[ws]
        unsatisfied = list((rec.get("dependency_closure") or {}).get("unsatisfied") or [])
        if unsatisfied:
            if "dependency_closure" not in blocking:
                blocking.append("dependency_closure")
            detail.append(
                "%s reports %d unsatisfied dependency(ies)" % (ws, len(unsatisfied))
            )

        reg = rec.get("regression") or {}
        # A skipped regression is the child's own waiver decision, recorded at
        # declare-ready time; `declare_ready` refuses outright when the config
        # required one and it failed. A record that RAN a regression and failed
        # is unambiguous, and is the case worth blocking on here.
        if not reg.get("skipped") and reg.get("exit_code") not in (0, None):
            if "regression_green" not in blocking:
                blocking.append("regression_green")
            detail.append(
                "%s reports a failed regression (exit %s)" % (ws, reg.get("exit_code"))
            )

    return {
        "verdict": "green" if not blocking else "blocked",
        "blocking": blocking,
        "workstreams": sorted(records),
        "expected_workstreams": expected,
        "manifest_hashes": hashes,
        "detail": detail,
        "criteria_read": list(SYNC_EVIDENCE_CRITERIA),
    }


def read_child_manifest(
    project_dir: str, ref: str, cycle_id: str
) -> Optional[Dict[str, Any]]:
    """The child's SEALED manifest at `ref`, or None if unreadable."""
    import spq_paths

    rel = os.path.relpath(
        spq_paths.committed_manifest_path(project_dir, str(cycle_id)),
        project_dir,
    )
    code, blob, _ = _git(project_dir, "show", "%s:%s" % (ref, rel))
    if code != 0:
        return None
    try:
        doc = json.loads(blob)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def read_child_sync_records(
    project_dir: str, ref: str, cycle_id: str
) -> Optional[Dict[str, Any]]:
    """The child's committed readiness records at `ref`, or None if unreadable.

    `None` means "could not read" and is distinct from `{}` ("read, and there
    are none") -- the caller must not treat an unreachable ref as an empty one.
    """
    import spq_paths

    rel_dir = os.path.relpath(
        os.path.join(spq_paths.committed_cycle_dir(project_dir, str(cycle_id)), "sync"),
        project_dir,
    )
    code, listing, _ = _git(
        project_dir, "ls-tree", "-r", "--name-only", ref, "--", rel_dir
    )
    if code != 0:
        return None

    records: Dict[str, Any] = {}
    for path in listing.splitlines():
        path = path.strip()
        if not path.endswith(".json"):
            continue
        code, blob, _ = _git(project_dir, "show", "%s:%s" % (ref, path))
        if code != 0:
            continue
        try:
            record = json.loads(blob)
        except ValueError:
            continue
        if isinstance(record, dict):
            records[str(record.get("workstream") or os.path.basename(path))] = record
    return records


def _is_ancestor(project_dir: str, sha: str, of: str) -> bool:
    if not sha or not of:
        return False
    code, _, _ = _git(project_dir, "merge-base", "--is-ancestor", sha, of)
    return code == 0


def _ref_head(project_dir: str, ref: str) -> str:
    if not ref:
        return ""
    code, out, _ = _git(project_dir, "rev-parse", "--verify", "%s^{commit}" % ref)
    return out.strip() if code == 0 else ""


def verify_cross_condition(
    project_dir: str,
    *,
    manifest: Dict[str, Any],
    child: Dict[str, Any],
    condition: str,
    output: Optional[Dict[str, Any]] = None,
    commit_sha: str = "",
    remote: str = "origin",
) -> Dict[str, Any]:
    """Can this clone CHECK the claim, and does it hold?

    `{"verified": True|False|None, "detail": str}`. `None` means claim-only --
    not a failure, but not proof either, and the gate treats it as unproven
    unless the release explicitly opts in.
    """
    if condition in CLAIM_ONLY:
        return {
            "verified": None,
            "detail": (
                "%s is asserted out of band; nothing in this repository can "
                "confirm it" % condition
            ),
        }

    ref = str(child.get("integration_ref") or "")
    if condition in ("integrated", "cycle_integrated"):
        if not commit_sha:
            return {"verified": False,
                    "detail": "%s requires --sha: without it the event names no "
                              "commit" % condition}
        # Prefer the remote-tracking ref: the producing child pushes to it, and
        # this clone's local branch of that name (if any) is its own business.
        candidates = [r for r in ("%s/%s" % (remote, ref), ref) if ref]
        target = next((r for r in candidates if _ref_head(project_dir, r)), "")
        if not target:
            return {
                "verified": False,
                "detail": (
                    "child integration ref %r is unavailable here; fetch it "
                    "before publishing a %s event about it" % (ref, condition)
                ),
            }
        if not _is_ancestor(project_dir, commit_sha, target):
            return {
                "verified": False,
                "detail": (
                    "%s is not an ancestor of %s, so that work is not on the "
                    "child's integration ref" % (commit_sha[:12], target)
                ),
            }
        if condition == "cycle_integrated":
            pinned = str(child.get("selected_sha") or "")
            if pinned and pinned != commit_sha:
                return {
                    "verified": False,
                    "detail": (
                        "cycle_integrated names %s but the release pins %s for "
                        "Cycle %s. A whole-Cycle claim has to be about the "
                        "increment the release actually selected."
                        % (commit_sha[:12], pinned[:12], child.get("cycle_id"))
                    ),
                }
            # ADR-030: cycle_integrated is ancestry PLUS the child's own green
            # Sync evidence. Ancestry alone only says "this commit is on that
            # branch" -- it says nothing about whether the Cycle that produced
            # it ever passed its own barrier, so without this a dependent child
            # could be unblocked by a whole-Cycle claim nobody verified.
            records = read_child_sync_records(
                project_dir, target, str(child.get("cycle_id") or "")
            )
            if records is None:
                return {
                    "verified": False,
                    "detail": (
                        "cannot read Cycle %s's readiness records at %s, so its "
                        "Sync evidence is unavailable here; fetch the ref before "
                        "publishing a cycle_integrated event about it"
                        % (child.get("cycle_id"), target)
                    ),
                }
            sync = sync_evidence_verdict(
                records,
                child_manifest=read_child_manifest(
                    project_dir, target, str(child.get("cycle_id") or "")
                ),
                pinned_manifest_hash=str(child.get("manifest_hash") or ""),
            )
            if sync["verdict"] != "green":
                return {
                    "verified": False,
                    "detail": (
                        "Cycle %s's own Sync evidence is not green (%s)%s"
                        % (
                            child.get("cycle_id"),
                            ", ".join(sync["blocking"]),
                            (": " + "; ".join(sync["detail"][:3])) if sync["detail"] else "",
                        )
                    ),
                    "sync_evidence": sync,
                }
            return {
                "verified": True,
                "detail": (
                    "%s is an ancestor of %s, and Cycle %s's readiness records "
                    "are green across %s"
                    % (commit_sha[:12], target, child.get("cycle_id"),
                       ", ".join(sync["workstreams"]))
                ),
                "command": "git merge-base --is-ancestor %s %s" % (commit_sha, target),
                "sync_evidence": sync,
            }
        return {
            "verified": True,
            "detail": "%s is an ancestor of %s" % (commit_sha[:12], target),
            "command": "git merge-base --is-ancestor %s %s" % (commit_sha, target),
        }

    # contract_published / artifact_published: recompute the digest with the SAME
    # script the child barrier runs. Two spellings of "the digest" is a check
    # that quietly stops checking.
    declared = (output or {}).get("digest")
    if not declared:
        return {
            "verified": False,
            "detail": (
                "%s requires an output digest: without one the event asserts "
                "that something was published but not WHAT" % condition
            ),
        }
    script = str((manifest.get("verification") or {}).get("digest_script") or "")
    path = (output or {}).get("id") or ""
    if not script or not os.path.exists(os.path.join(project_dir, script)):
        return {
            "verified": None,
            "detail": (
                "no digest_script configured on the release, so the declared "
                "digest %s cannot be recomputed here" % str(declared)[:16]
            ),
        }
    try:
        result = subprocess.run(
            ["bash", script, str(path)], cwd=str(project_dir),
            capture_output=True, text=True, timeout=120,
        )
    except Exception as exc:  # noqa: BLE001
        return {"verified": False, "detail": "digest script failed: %s" % exc}
    if result.returncode != 0:
        return {
            "verified": False,
            "detail": "digest script exited %d: %s"
            % (result.returncode, result.stderr.strip()[:200]),
        }
    recomputed = result.stdout.strip().split()[-1] if result.stdout.strip() else ""
    if recomputed != str(declared):
        return {
            "verified": False,
            "detail": (
                "declared digest %s does not match the recomputed %s for %s"
                % (str(declared)[:16], recomputed[:16], path)
            ),
        }
    return {
        "verified": True,
        "detail": "digest %s matches for %s" % (recomputed[:16], path),
        "command": "bash %s %s" % (script, path),
    }


def publish(
    project_dir: str,
    *,
    coordination_cycle_id: str,
    manifest: Dict[str, Any],
    cycle_id: str,
    condition: str,
    cycle_manifest_hash: str = "",
    unit_id: str = "",
    output: Optional[Dict[str, Any]] = None,
    commit_sha: str = "",
    workstream_id: str = "",
    published_by: str = "",
    runner_id: str = "",
    remote: str = "origin",
    verify: bool = True,
) -> Dict[str, Any]:
    """Record that a child Cycle reached a condition another child depends on.

    Verification happens HERE, on the producing side, where the refs and the
    digest script exist. A consumer re-checking later can only re-run the cheap
    parts, so an event that was never checked here is an event nobody checks.
    """
    import spq_manifest
    import spq_paths
    import state_store

    if condition not in CROSS_CONDITIONS:
        raise CoordinationError(
            "unknown cross-Cycle condition %r (known: %s)"
            % (condition, ", ".join(CROSS_CONDITIONS))
        )
    if not verify_hash(manifest):
        raise CoordinationError(
            "refusing to publish against Coordination Cycle %s: the manifest "
            "does not match its own hash" % coordination_cycle_id
        )

    by_id = children_index(manifest)
    child = by_id.get(str(cycle_id))
    if child is None:
        raise CoordinationError(
            "Cycle %s is not a child of Coordination Cycle %s, so no event "
            "about it can be meaningful" % (cycle_id, coordination_cycle_id)
        )
    if str(child.get("state")) == "dropped":
        raise CoordinationError(
            "Cycle %s is dropped from this release; publishing an event about "
            "it would let a dropped child unblock something that ships"
            % cycle_id
        )

    pinned_hash = str(child.get("manifest_hash") or "")
    if cycle_manifest_hash and pinned_hash and cycle_manifest_hash != pinned_hash:
        raise CoordinationError(
            "Cycle %s is pinned at manifest %s but this event declares %s. The "
            "release selected an exact child increment; an event about a "
            "different revision is about a different admitted set."
            % (cycle_id, pinned_hash[:20], cycle_manifest_hash[:20])
        )

    # The address must be one the PARENT declared. Admitting a new coordination
    # is a parent-manifest revision, not an event -- otherwise any child could
    # invent a dependency the release never agreed to.
    key = spq_manifest.cross_ref_key(
        {"cycle_id": cycle_id, "unit_id": unit_id, "output": output}
    )
    known = declared_keys(manifest)
    if key not in known:
        raise CoordinationError(
            "no edge or required output in Coordination Cycle %s names %s. "
            "Declaring a cross-Cycle dependency is a parent-manifest revision, "
            "not an event.\nDeclared addresses: %s"
            % (coordination_cycle_id, key, ", ".join(sorted(known)) or "(none)")
        )

    verification = None
    if verify:
        verification = verify_cross_condition(
            project_dir, manifest=manifest, child=child, condition=condition,
            output=output, commit_sha=commit_sha, remote=remote,
        )
        if verification.get("verified") is False:
            raise CoordinationError(
                "refusing to publish %s for Cycle %s: %s"
                % (condition, cycle_id, verification.get("detail"))
            )

    events_dir = spq_paths.committed_coordination_events_dir(
        project_dir, coordination_cycle_id, str(cycle_id)
    )
    os.makedirs(events_dir, exist_ok=True)
    seq = len([n for n in os.listdir(events_dir) if n.endswith(".json")]) + 1

    event = build_event(
        coordination_cycle_id=coordination_cycle_id,
        coordination_manifest_hash=str(manifest.get("manifest_hash") or ""),
        cycle_id=str(cycle_id),
        cycle_seq=child.get("cycle_seq"),
        cycle_manifest_hash=cycle_manifest_hash or pinned_hash,
        condition=condition,
        unit_id=unit_id,
        output=output,
        commit_sha=commit_sha,
        integration_ref=str(child.get("integration_ref") or ""),
        workstream_id=workstream_id,
        verification=verification,
        published_at=state_store.now_iso(),
        published_by=published_by,
        runner_id=runner_id,
        seq=seq,
    )

    slug = unit_id or str((output or {}).get("id") or "cycle")
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in slug)[:64]
    name = "%04d-%s-%s.json" % (seq, condition, slug or "cycle")
    path = os.path.join(events_dir, name)
    if os.path.realpath(os.path.dirname(path)) != os.path.realpath(events_dir):
        raise CoordinationError(
            "refusing to write a coordination event outside %s" % events_dir
        )
    state_store.write_json_atomic(path, event)

    # Same reporting gap one level up: without this the CP can show a release
    # and its children but never why a child was gated or released.
    try:
        from gate_emitter import emit_dependency_published

        emit_dependency_published(
            str(cycle_id),
            target_type="cycle",
            published_by=published_by,
            reason="%s for %s (release %s, event %s)"
            % (condition, key, coordination_cycle_id, event["event_id"]),
            project_dir=project_dir,
        )
    except Exception:  # noqa: BLE001 - observation must never block delivery
        pass

    rel = os.path.relpath(path, project_dir)
    return {
        "event": event,
        "path": path,
        "key": key,
        # Agents may not commit or push (`modes/init.md`), so the operator has
        # to. Printing the exact command is what keeps the mandatory human step
        # from looking like a bug.
        "push_command": (
            "git add %s && git commit -m 'spq: %s %s' && git push"
            % (rel, condition, cycle_id)
        ),
    }


def _local_events(project_dir: str, coordination_cycle_id: str) -> List[Dict[str, Any]]:
    import spq_paths

    root = spq_paths.committed_coordination_events_dir(
        project_dir, coordination_cycle_id
    )
    found: List[Dict[str, Any]] = []
    for base, _dirs, files in os.walk(root):
        for name in sorted(files):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(base, name), "r", encoding="utf-8") as fh:
                    event = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(event, dict) and event.get("event_id"):
                found.append(event)
    return found


def refresh(
    project_dir: str,
    coordination_cycle_id: str,
    *,
    manifest: Optional[Dict[str, Any]] = None,
    remote: str = "origin",
    fetch: bool = True,
) -> Dict[str, Any]:
    """Rebuild the local cache from git. NEVER merges.

    Reads each child's events off its integration ref with `git show`, the same
    no-merge trick `sync_barrier.collect` uses one level down: refreshing must
    never mutate the working tree, or reading a dependency would be a
    side-effecting operation.
    """
    import spq_paths
    import state_store

    fetch_ok, fetch_detail = True, "skipped"
    if fetch:
        code, _, err = _git(project_dir, "fetch", remote, "--quiet")
        fetch_ok = code == 0
        fetch_detail = "ok" if fetch_ok else err.strip()[:200]

    events: Dict[str, Dict[str, Any]] = {}
    unreadable: List[Dict[str, str]] = []
    for event in _local_events(project_dir, coordination_cycle_id):
        events[str(event.get("event_id"))] = event

    if manifest:
        rel_root = os.path.relpath(
            spq_paths.committed_coordination_events_dir(
                project_dir, coordination_cycle_id
            ),
            project_dir,
        )
        for child in manifest.get("children") or []:
            cid = str(child.get("cycle_id") or "")
            branch = str(child.get("integration_ref") or "")
            if not cid or not branch:
                continue
            ref = "%s/%s" % (remote, branch)
            code, listing, err = _git(
                project_dir, "ls-tree", "-r", "--name-only", ref, "--",
                os.path.join(rel_root, cid),
            )
            if code != 0:
                # Absent is not unreadable. A child that has not pushed its
                # integration branch yet is the normal early state; a ref that
                # RESOLVES and still cannot be listed is a real fault, and at
                # release scale silence is indistinguishable from "that child
                # declares nothing" unless it is recorded.
                if _ref_head(project_dir, ref):
                    unreadable.append(
                        {"cycle_id": cid, "ref": ref, "error": err.strip()[:200]}
                    )
                continue
            for relpath in listing.splitlines():
                relpath = relpath.strip()
                if not relpath.endswith(".json"):
                    continue
                code, blob, _ = _git(project_dir, "show", "%s:%s" % (ref, relpath))
                if code != 0:
                    continue
                try:
                    event = json.loads(blob)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("event_id"):
                    events[str(event["event_id"])] = event

    if unreadable and fetch_ok:
        fetch_detail = "unreadable child refs: %s" % ", ".join(
            "%s (%s)" % (u["cycle_id"], u["ref"]) for u in unreadable
        )
    cache = {
        "schema_version": SCHEMA_VERSION,
        "coordination_cycle_id": coordination_cycle_id,
        "refreshed_at": state_store.now_iso(),
        "refresh_ok": fetch_ok and not unreadable,
        "refresh_detail": fetch_detail,
        "unreadable_refs": unreadable,
        "events": sorted(events.values(), key=lambda e: (e.get("seq") or 0)),
    }
    state_store.write_json_atomic(
        spq_paths.coordination_ledger_path(project_dir, coordination_cycle_id), cache
    )
    return cache


def snapshot(
    project_dir: str,
    coordination_cycle_id: str,
    *,
    max_age_s: int = DEFAULT_MAX_LEDGER_AGE_S,
    allow_stale: bool = False,
) -> Dict[str, Any]:
    """The cached ledger, with an honest freshness verdict.

    `fresh: False` is not an error; it is the fact the dependency gate needs so a
    cross-Cycle edge can fail closed rather than resolve against a snapshot that
    may predate the very event it is waiting for.
    """
    import datetime as _dt

    import spq_paths
    import state_store

    cache = state_store.read_json(
        spq_paths.coordination_ledger_path(project_dir, coordination_cycle_id)
    )
    if not cache:
        return {
            "coordination_cycle_id": coordination_cycle_id,
            "events": [],
            "fresh": False,
            "detail": "no local coordination ledger cache; run refresh_coordination",
        }

    fresh, detail = True, "cache is current"
    stamp = cache.get("refreshed_at")
    if not cache.get("refresh_ok"):
        fresh = False
        detail = "last refresh failed: %s" % cache.get("refresh_detail")
    elif stamp:
        try:
            when = _dt.datetime.fromisoformat(str(stamp))
            age = (_dt.datetime.now(_dt.timezone.utc) - when).total_seconds()
            if age > max_age_s:
                fresh = False
                detail = "cache is %ds old (ceiling %ds)" % (int(age), max_age_s)
        except ValueError:
            fresh = False
            detail = "unparseable refreshed_at"

    if allow_stale:
        fresh = True
        detail += " (staleness waived)"
    return {
        "coordination_cycle_id": coordination_cycle_id,
        "events": list(cache.get("events") or []),
        "fresh": fresh,
        "detail": detail,
        "refreshed_at": stamp,
        "unreadable_refs": list(cache.get("unreadable_refs") or []),
    }


def satisfied_map(
    snap: Dict[str, Any], manifest: Dict[str, Any]
) -> Dict[str, Dict[str, Any]]:
    """address -> the STRONGEST recorded condition, for the dependency gate.

    Keyed on `spq_manifest.cross_ref_key`, which embeds the `cycle_id`. It is
    NEVER merged into the intra-Cycle `satisfied` map, which is keyed on a bare
    `unit_id` -- two child Cycles in one codebase routinely share Work Unit ids,
    so flattening them would let Platform's `WU-01` satisfy an edge waiting on
    EHR's `WU-01`.

    Every filter below is a fail-closed gate, in this order:
      1. a different release,
      2. a SUPERSEDED parent revision (it may describe a since-dropped child),
      3. a child that is not `included`, or pinned at a different manifest hash,
      4. an address the parent never declared,
      5. weaker than something already recorded.
    """
    import spq_manifest

    ccid = str(manifest.get("coordination_cycle_id") or "")
    parent_hash = str(manifest.get("manifest_hash") or "")
    by_id = children_index(manifest)
    known = declared_keys(manifest)

    best: Dict[str, Dict[str, Any]] = {}
    for event in snap.get("events") or []:
        if str(event.get("coordination_cycle_id") or "") != ccid:
            continue
        if parent_hash and str(
            event.get("coordination_manifest_hash") or ""
        ) != parent_hash:
            continue
        cid = str(event.get("cycle_id") or "")
        child = by_id.get(cid)
        if child is None or str(child.get("state")) != "included":
            continue
        pinned = str(child.get("manifest_hash") or "")
        if pinned and str(event.get("cycle_manifest_hash") or "") != pinned:
            continue
        condition = str(event.get("condition") or "")
        if condition not in CROSS_RANK:
            continue
        key = spq_manifest.cross_ref_key(
            {
                "cycle_id": cid,
                "unit_id": event.get("unit_id"),
                "output": event.get("output"),
            }
        )
        if key not in known:
            continue
        current = best.get(key)
        if current and not cross_satisfies(str(current["condition"]), condition):
            continue
        verification = event.get("verification") or {}
        best[key] = {
            "condition": condition,
            # Precomputed so the PURE dependency gate does membership rather
            # than ranking, and therefore needs no import from this module.
            "satisfies": satisfies_set(condition),
            "verified": verification.get("verified") is True,
            "sha": event.get("commit_sha"),
            "event_id": event.get("event_id"),
            "cycle_id": cid,
            "coordination_cycle_id": ccid,
            "published_at": event.get("published_at"),
        }
    return best


def local_only_children(
    project_dir: str, coordination_cycle_id: str
) -> List[str]:
    """Children whose events are written here but not yet tracked by git.

    Without this, "I published it" and "nobody can see it" look identical, and
    the mandatory human push looks like a bug.
    """
    import spq_paths

    root = spq_paths.committed_coordination_events_dir(
        project_dir, coordination_cycle_id
    )
    if not os.path.isdir(root):
        return []
    rel = os.path.relpath(root, project_dir)
    code, out, _ = _git(project_dir, "ls-files", "--", rel)
    tracked = {line.strip() for line in out.splitlines() if line.strip()}
    pending: set = set()
    for base, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith(".json"):
                continue
            relpath = os.path.relpath(os.path.join(base, name), project_dir)
            if relpath not in tracked:
                pending.add(os.path.basename(base))
    return sorted(pending)
