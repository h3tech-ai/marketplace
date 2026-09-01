#!/usr/bin/env python3
"""The Cycle manifest: the authoritative admitted set for an SPQ Cycle (#303).

Before this, `open_cycle` and `hydrate_cycle` accepted arbitrary caller-supplied
Work Unit JSON. Every workstream clone therefore reconstructed its own view of
the Cycle from whatever the prompt happened to pass, so two clones could
disagree about what was admitted, who owned it, and what depended on what -- and
nothing could detect the disagreement until the barrier, if then.

The manifest replaces that with one sealed, hashed document. A workstream
hydrates a validated PROJECTION of it and refuses stale, mismatched, foreign or
tampered input.

## The hash rule, defined once

    manifest_hash = "sha256:" + sha256(canonical_bytes(body))

where `body` is the manifest with `manifest_hash` removed and `canonical_bytes`
is `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)`
encoded UTF-8. One definition, both sides -- the same discipline design §8.3
demanded of the shared-digest script, and for the same reason: two spellings of
"the hash" is a barrier that quietly stops being a barrier.

`schema_version` is INSIDE the hash. A schema bump therefore changes the hash,
which is correct: no cross-version hash compatibility is owed, and pretending
otherwise would let a v1 reader accept a v2 document it cannot fully validate.

## What the manifest is not

It is not execution state. Work Unit sub-states, retries and receipts live in
each workstream's own execution state; the manifest records what was ADMITTED
and who OWNS it. The distinction is what lets a workstream be handed a
projection containing other workstreams' unit identities without their mutable
state -- the anti-pattern #303 explicitly forbids.

Python 3.9 compatible: this file is projected into every host package.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# A Work Unit id becomes a path segment (the ledger event filename) and a
# receipt filename, so the character set is a containment control, not
# cosmetics. Deliberately narrower than the receipt-id regex: no separators, no
# dots, no leading dash.
WORK_UNIT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

SCHEMA_VERSION = "1.0"
KIND = "spq.cycle.manifest"

# Conditions a dependency edge may declare, mirroring
# `story_pipeline.DEP_CONDITIONS`. Kept as a literal here rather than imported
# so the manifest can be validated by a tool that does not carry the pipeline.
DEP_CONDITIONS = (
    "done",
    "contract_published",
    "artifact_published",
    "integrated",
    "environment_ready",
    "cycle_integrated",
)

# Conditions a CHILD may declare on a cross-Cycle (`external`) edge. A strict
# subset of the above: `done` is excluded because another clone's board is not
# visible from here, so a bare cross-Cycle `done` is a claim nothing can check --
# precisely the edge the epic's "prefer the smallest sufficient condition" is
# telling a PO to replace with a verifiable one.
#
# `cycle_accepted` from the epic's example list is deliberately NOT here. It
# would be a second claim-only condition that nothing can verify and that fails
# closed by default, i.e. vocabulary with no behaviour -- and the parent pins a
# `selected_sha`, so a child's ACCEPTANCE state is not what a release gates on.
# `cycle_integrated` already expresses "this child's increment is in".
CROSS_DECLARABLE_CONDITIONS = (
    "integrated",
    "contract_published",
    "artifact_published",
    "cycle_integrated",
    "environment_ready",
)

# Strength order for satisfying a declared condition, declared EXPLICITLY rather
# than left as an implicit ranking. `integrated` implies the code is on the
# integration ref, which implies it was published, which implies it was built.
# An implicit ordering is how a weaker event comes to satisfy a stronger
# requirement without anyone deciding that it should.
CONDITION_RANK = {
    "done": 1,
    "contract_published": 2,
    "artifact_published": 2,
    "integrated": 3,
    # Never satisfiable from inside one Cycle; ranked so comparisons are total.
    "environment_ready": 0,
    "cycle_integrated": 99,
}


class ManifestError(ValueError):
    """The manifest is malformed, tampered with, or does not admit this input."""


# ── canonicalization and hashing ────────────────────────────────────────────


def canonical_bytes(manifest: Dict[str, Any]) -> bytes:
    """Deterministic bytes for hashing: sorted keys, no whitespace.

    `manifest_hash` is excluded, since a document cannot contain its own hash.
    """
    body = {k: v for k, v in manifest.items() if k != "manifest_hash"}
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def compute_hash(manifest: Dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(manifest)).hexdigest()


def verify_hash(manifest: Dict[str, Any]) -> bool:
    recorded = manifest.get("manifest_hash")
    return bool(recorded) and recorded == compute_hash(manifest)


# ── construction ────────────────────────────────────────────────────────────


def cross_ref_key(spec: Dict[str, Any]) -> str:
    """The canonical address of a cross-Cycle dependency target (#305).

    Accepts EITHER a `depends_on[].external` spec or a coordination event, so a
    producer and a consumer cannot spell the same address two ways -- the same
    discipline the manifest hash rule states above, applied to addressing.

        `<cycle_id>|unit:<unit_id>`        a specific Work Unit in that Cycle
        `<cycle_id>|out:<kind>:<id>`       a named output, whoever produces it
        `<cycle_id>|cycle`                 the Cycle as a whole

    Unit beats output when both are present: it is the narrower claim, and
    "prefer the smallest sufficient condition" applies to the address too.
    """
    cycle_id = str(spec.get("cycle_id") or "")
    unit_id = str(spec.get("unit_id") or "")
    if unit_id:
        return "%s|unit:%s" % (cycle_id, unit_id)
    output = spec.get("output")
    if isinstance(output, dict) and (output.get("id") or output.get("kind")):
        return "%s|out:%s:%s" % (
            cycle_id,
            str(output.get("kind") or ""),
            str(output.get("id") or ""),
        )
    return "%s|cycle" % cycle_id


def normalize_dep(entry: Any) -> Dict[str, Any]:
    """Normalize one dependency edge. Mirrors `story_pipeline.normalize_dep`."""
    if isinstance(entry, dict):
        external = entry.get("external")
        if isinstance(external, dict):
            # `unit_id` is a Work Unit id or NOTHING. It used to fall back to
            # `external["output"]`, which in the epic's own output-addressed form
            # is a DICT -- so the id became the literal string
            # "{'kind': 'contract', 'id': 'contracts/auth'}". That never crashed:
            # it produced an unaddressable edge, put garbage in the operator's
            # message, and got compared against `unit_ids` in `validate`.
            # Output-addressed edges are addressed by `cross_ref_key`, not by
            # stringifying a mapping.
            return {
                "unit_id": str(external.get("unit_id") or ""),
                "condition": str(external.get("condition") or "cycle_integrated"),
                "output": external.get("output"),
                "external": dict(external),
            }
        return {
            "unit_id": str(entry.get("unit_id") or entry.get("id") or ""),
            "condition": str(entry.get("condition") or "done"),
            "output": entry.get("output"),
            "external": None,
        }
    return {"unit_id": str(entry), "condition": "done", "external": None}


def _normalize_unit(raw: Dict[str, Any], owner_default: str = "") -> Dict[str, Any]:
    return {
        "id": str(raw.get("id") or ""),
        "title": str(raw.get("title") or ""),
        "owner_workstream": str(raw.get("owner_workstream") or owner_default or ""),
        "kind": str(raw.get("kind") or ""),
        "labels": list(raw.get("labels") or []),
        "acceptance_criteria": list(raw.get("acceptance_criteria") or []),
        "file_scope": list(raw.get("file_scope") or []),
        "source_spec_refs": list(raw.get("source_spec_refs") or []),
        "verification_tier": str(raw.get("verification_tier") or ""),
        "outputs": [dict(o) for o in (raw.get("outputs") or []) if isinstance(o, dict)],
        "depends_on": [normalize_dep(d) for d in (raw.get("depends_on") or [])],
    }


def build(
    *,
    cycle_id: str,
    cycle_seq: int,
    goal: str,
    baseline_sha: str,
    integration_ref: str,
    workstreams: Sequence[Dict[str, Any]],
    work_units: Sequence[Dict[str, Any]],
    acceptance_criteria: Optional[Sequence[str]] = None,
    shared_surfaces: Optional[Sequence[Dict[str, Any]]] = None,
    verification: Optional[Dict[str, Any]] = None,
    integration_policy: Optional[Dict[str, Any]] = None,
    source_spec_refs: Optional[Sequence[str]] = None,
    coordination_cycle_id: Optional[str] = None,
    tracker_binding: Optional[Dict[str, Any]] = None,
    project: str = "",
    created_at: str = "",
    created_by: str = "",
    runner_id: str = "",
    manifest_revision: int = 1,
    supersedes: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble an UNSEALED manifest. Call `seal` to hash it."""
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "cycle_id": str(cycle_id),
        "cycle_seq": int(cycle_seq),
        "manifest_revision": int(manifest_revision),
        "supersedes": supersedes,
        "coordination_cycle_id": coordination_cycle_id,
        "project": str(project),
        "goal": str(goal),
        "acceptance_criteria": list(acceptance_criteria or []),
        "baseline_sha": str(baseline_sha),
        "integration_ref": str(integration_ref),
        "created_at": str(created_at),
        "created_by": str(created_by),
        "runner_id": str(runner_id),
        # Traceability ONLY. Nothing in this module or in spq_paths may use it
        # to select a path, a cache key or a command (#303 AC).
        "source_spec_refs": list(source_spec_refs or []),
        "tracker_binding": dict(tracker_binding) if tracker_binding else None,
        "workstreams": [
            {
                "id": str(w.get("id") or ""),
                "branch": str(w.get("branch") or ""),
                "shared_owner": bool(w.get("shared_owner")),
                "integration": bool(w.get("integration")),
                "file_scope": list(w.get("file_scope") or []),
                "source_spec_refs": list(w.get("source_spec_refs") or []),
            }
            for w in workstreams
        ],
        "work_units": [_normalize_unit(u) for u in work_units],
        "shared_surfaces": [dict(s) for s in (shared_surfaces or [])],
        "verification": dict(verification or {}),
        "integration_policy": dict(
            integration_policy or {"mode": "at_sync", "merge_requires": []}
        ),
    }


def seal(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Stamp the hash. Idempotent for an unmodified manifest."""
    sealed = dict(manifest)
    sealed.pop("manifest_hash", None)
    sealed["manifest_hash"] = compute_hash(sealed)
    return sealed


def revise(
    sealed: Dict[str, Any], changes: Dict[str, Any], *, revised_by: str = ""
) -> Dict[str, Any]:
    """Produce the next revision, recording what it supersedes.

    `supersedes` carries the PRIOR HASH, not the prior revision number. A
    workstream holding an older projection can then be told, precisely, that the
    manifest it is on was superseded by this one -- rather than being asked to
    trust a number that any writer could have bumped.
    """
    if not sealed.get("manifest_hash"):
        raise ManifestError("cannot revise an unsealed manifest")
    nxt = dict(sealed)
    prior = sealed["manifest_hash"]
    nxt.update(changes)
    nxt["manifest_revision"] = int(sealed.get("manifest_revision") or 1) + 1
    nxt["supersedes"] = prior
    nxt["created_by"] = revised_by or sealed.get("created_by", "")
    return seal(nxt)


# ── validation ──────────────────────────────────────────────────────────────


def topo_order(manifest: Dict[str, Any]) -> List[str]:
    """Admitted unit ids in dependency order. Raises on a cycle.

    A dependency cycle is unsatisfiable by construction: every unit in it waits
    for another unit in it. Detecting it at Commit is the difference between a
    Cycle that cannot start and a Cycle that appears to start and then reports
    `deps_blocked` forever with no indication why.
    """
    units = {u["id"]: u for u in manifest.get("work_units") or []}
    state: Dict[str, int] = {}
    order: List[str] = []

    def visit(uid: str, trail: List[str]) -> None:
        mark = state.get(uid, 0)
        if mark == 2:
            return
        if mark == 1:
            raise ManifestError(
                "dependency cycle: %s" % " -> ".join(trail + [uid])
            )
        state[uid] = 1
        for dep in units[uid].get("depends_on") or []:
            if dep.get("external"):
                continue
            target = dep.get("unit_id")
            if target in units:
                visit(target, trail + [uid])
        state[uid] = 2
        order.append(uid)

    for uid in units:
        visit(uid, [])
    return order


def _external_problems(
    uid: Any,
    dep: Dict[str, Any],
    external: Dict[str, Any],
    unit_ids: "set[str] | frozenset[str]",
) -> List[str]:
    """Shape-check one cross-Cycle edge (#305).

    Before this, `validate` skipped every external edge entirely -- no target
    check, no self-dependency check, and `topo_order` skips them too. So
    `{"external": {"unit_id": "WU-LOCAL"}}` validated clean with nothing looked
    at at all. That was inert only while `story_dep_status` held every external
    edge unconditionally. The moment #305 makes the branch resolvable, an edge
    declared cross-Cycle would be satisfied by a LOCAL event -- and it takes a
    typo, not malice, to get there.
    """
    problems: List[str] = []
    cycle_id = str(external.get("cycle_id") or "")
    target = str(external.get("unit_id") or "")
    output = external.get("output")
    output_addressed = isinstance(output, dict) and (
        output.get("id") or output.get("kind")
    )
    if not cycle_id:
        # An OUTPUT-addressed edge needs no producer identity: the Coordination
        # Cycle's declared edges already say who supplies it, and the consumer
        # only has to name what it waits on. Requiring one here forced the
        # producing child to open its Cycle FIRST -- a hard ordering dependency
        # between Cycles this epic calls independent, and it made the
        # output-addressed form the epic itself prefers unusable.
        #
        # A UNIT-addressed edge still needs it, because a bare unit id has no
        # other way to say which Cycle it belongs to -- and that is the form the
        # local-unit smuggle takes.
        if not output_addressed:
            problems.append(
                "work unit %r declares an external dependency with neither a "
                "cycle_id nor an output. A cross-Cycle edge that names neither "
                "its Cycle nor what it waits on cannot be reconciled against "
                "the Coordination Cycle's declared edges, so it can never "
                "resolve." % (uid,)
            )
    else:
        try:
            import spq_paths

            spq_paths.valid_cycle_id(cycle_id)
        except ImportError:  # validating without the runtime on the path
            pass
        except Exception as exc:  # noqa: BLE001 - IdentityError and friends
            problems.append(
                "work unit %r declares an external dependency on cycle_id %r, "
                "which is not a valid Cycle identity: %s" % (uid, cycle_id, exc)
            )

    if target:
        if not WORK_UNIT_ID_RE.match(target):
            problems.append(
                "work unit %r declares an external dependency on %r, which is "
                "not a single safe path segment (expected %s)"
                % (uid, target, WORK_UNIT_ID_RE.pattern)
            )
        if target in unit_ids:
            problems.append(
                "work unit %r declares an EXTERNAL dependency on %r, which is "
                "admitted to THIS Cycle. An edge is either cross-Cycle or local; "
                "declaring a local unit external would let its own local event "
                "satisfy an edge that claims to wait on another Cycle."
                % (uid, target)
            )
    elif not output_addressed:
        problems.append(
            "work unit %r declares an external dependency that names neither a "
            "unit_id nor an output. There is nothing for a Coordination Cycle "
            "event to be about." % (uid,)
        )

    condition = str(dep.get("condition") or "")
    if condition and condition not in CROSS_DECLARABLE_CONDITIONS:
        problems.append(
            "work unit %r declares an external dependency with condition %r. A "
            "cross-Cycle edge must name a condition another Cycle can publish "
            "(known: %s)." % (uid, condition, ", ".join(CROSS_DECLARABLE_CONDITIONS))
        )
    return problems


def validate(
    manifest: Dict[str, Any],
    *,
    project_dir: Optional[str] = None,
    require_baseline: bool = True,
) -> List[str]:
    """Return a list of problems; empty means valid.

    Returns rather than raises so a caller can report every problem at once: an
    operator fixing a manifest one refusal at a time is a bad loop.

    `require_baseline=False` is for a project with no git repository. SPQ's whole
    integration model is git-based -- branches, ancestry, the barrier's
    `is_ancestor` check -- so such a project cannot reach Sync regardless, and
    demanding a baseline it cannot have would block Cycle open for no
    protection. The field is still recorded as "" so its absence is visible
    rather than fabricated.
    """
    problems: List[str] = []

    if manifest.get("kind") != KIND:
        problems.append("kind must be %r, got %r" % (KIND, manifest.get("kind")))
    if manifest.get("schema_version") != SCHEMA_VERSION:
        problems.append(
            "schema_version must be %r, got %r"
            % (SCHEMA_VERSION, manifest.get("schema_version"))
        )

    cycle_id = manifest.get("cycle_id")
    cycle_seq = manifest.get("cycle_seq")
    try:
        import spq_paths

        spq_paths.valid_cycle_id(str(cycle_id))
        if int(cycle_seq) != spq_paths.seq_of(str(cycle_id)):
            problems.append(
                "cycle_seq %r disagrees with cycle_id %r; the seq is the "
                "receipt-facing projection of the id, so a mismatch makes "
                "CYCLE-{seq} receipts name a different Cycle than the storage"
                % (cycle_seq, cycle_id)
            )
    except Exception as exc:  # noqa: BLE001
        problems.append("cycle_id %r is invalid: %s" % (cycle_id, exc))

    if require_baseline and not manifest.get("baseline_sha"):
        problems.append("baseline_sha is required: a Cycle without a baseline "
                        "cannot prove what it changed")
    if not manifest.get("integration_ref"):
        problems.append("integration_ref is required")

    # `coordination_cycle_id` has been an accepted, HASHED field since #303 with
    # nothing checking its shape. It becomes a path segment the moment a parent
    # resolves it, so it is validated at the identity layer here -- the same
    # posture `_validate` takes in `spq_paths`, and the same class of defence the
    # Work Unit id check above already applies.
    ccid = manifest.get("coordination_cycle_id")
    if ccid is not None:
        try:
            import spq_paths

            spq_paths.valid_coordination_cycle_id(str(ccid))
        except (ImportError, AttributeError):  # runtime not on the path
            pass
        except Exception as exc:  # noqa: BLE001 - IdentityError and friends
            problems.append(
                "coordination_cycle_id %r is invalid: %s" % (ccid, exc)
            )

    policy = manifest.get("integration_policy") or {}
    policy_mode = policy.get("mode")
    merge_requires = policy.get("merge_requires")
    if policy_mode not in ("at_sync", "incremental"):
        problems.append(
            "integration_policy.mode must be 'at_sync' or 'incremental', got %r"
            % policy_mode
        )
    if not isinstance(merge_requires, list) or not all(
        isinstance(item, str) and item for item in merge_requires
    ):
        problems.append("integration_policy.merge_requires must be a list of names")
        merge_requires = []
    verification = manifest.get("verification") or {}
    if (
        policy_mode == "incremental"
        and verification.get("require_regression") is True
        and "regression_green" not in merge_requires
    ):
        problems.append(
            "incremental integration with require_regression: true must include "
            "regression_green in integration_policy.merge_requires"
        )

    ws_ids: Set[str] = set()
    shared_owners = 0
    for entry in manifest.get("workstreams") or []:
        wid = entry.get("id")
        if not wid:
            problems.append("a workstream entry has no id")
            continue
        if wid in ws_ids:
            problems.append("duplicate workstream id %r" % wid)
        ws_ids.add(wid)
        if entry.get("shared_owner"):
            shared_owners += 1
    if not ws_ids:
        problems.append("workstreams[] is empty")
    if shared_owners > 1:
        problems.append(
            "%d workstreams claim shared_owner; exactly one owns the shared "
            "surfaces for a Cycle (§12), or adjudication has no address"
            % shared_owners
        )

    unit_ids: Set[str] = set()
    units = manifest.get("work_units") or []
    if not units:
        problems.append(
            "work_units[] is empty: an empty Cycle lets a workstream declare "
            "readiness having delivered nothing"
        )
    for unit in units:
        uid = unit.get("id")
        if not uid:
            problems.append("a work unit has no id")
            continue
        # Review finding P1(4): a Work Unit id is interpolated into an event
        # FILENAME, so any id the manifest admits becomes a path segment. An id
        # containing a separator or `..` escaped the events directory entirely.
        # Validated here, at the boundary that decides what is admitted, so a
        # malformed id can never reach the ledger in the first place.
        if not WORK_UNIT_ID_RE.match(str(uid)):
            problems.append(
                "work unit id %r is not a single safe path segment: ids become "
                "event filenames, so they must match %s"
                % (uid, WORK_UNIT_ID_RE.pattern)
            )
        if uid in unit_ids:
            problems.append("duplicate work unit id %r" % uid)
        unit_ids.add(uid)
        owner = unit.get("owner_workstream")
        if not owner:
            problems.append("work unit %r has no owner_workstream" % uid)
        elif owner not in ws_ids:
            problems.append(
                "work unit %r is owned by %r, which is not in workstreams[]"
                % (uid, owner)
            )

    for unit in units:
        uid = unit.get("id")
        for dep in unit.get("depends_on") or []:
            condition = dep.get("condition")
            if condition not in DEP_CONDITIONS:
                problems.append(
                    "work unit %r declares unknown condition %r (known: %s)"
                    % (uid, condition, ", ".join(DEP_CONDITIONS))
                )
            external = dep.get("external")
            if external:
                problems.extend(_external_problems(uid, dep, external, unit_ids))
                continue
            target = dep.get("unit_id")
            if not target:
                problems.append("work unit %r has a dependency with no unit_id" % uid)
            elif target not in unit_ids:
                problems.append(
                    "work unit %r depends on %r, which is neither admitted to "
                    "this Cycle nor declared external. An edge that can never "
                    "resolve blocks the unit forever." % (uid, target)
                )
            elif target == uid:
                problems.append("work unit %r depends on itself" % uid)

    if not problems:
        try:
            topo_order(manifest)
        except ManifestError as exc:
            problems.append(str(exc))

    for surface in manifest.get("shared_surfaces") or []:
        path = surface.get("path")
        owner = surface.get("owner_workstream")
        if not path:
            problems.append("a shared surface has no path")
        if not owner:
            problems.append("shared surface %r has no owner_workstream" % path)
        elif owner not in ws_ids:
            problems.append(
                "shared surface %r is owned by %r, which is not a workstream"
                % (path, owner)
            )

    if project_dir and manifest.get("baseline_sha"):
        if not _sha_exists(project_dir, str(manifest["baseline_sha"])):
            problems.append(
                "baseline_sha %s is not reachable in this checkout; a manifest "
                "pinned to an unknown baseline cannot be verified here"
                % manifest["baseline_sha"]
            )

    return problems


def _sha_exists(project_dir: str, sha: str) -> bool:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "cat-file", "-e", "%s^{commit}" % sha],
            cwd=project_dir,
            capture_output=True,
            timeout=15,
        )
        return result.returncode == 0
    except Exception:  # noqa: BLE001 - no git, no verdict
        return True


# ── projection ──────────────────────────────────────────────────────────────


def project(manifest: Dict[str, Any], workstream_id: str) -> Dict[str, Any]:
    """The validated view one workstream is allowed to hydrate.

    `work_units` carries the FULL records this workstream owns. `foreign_units`
    carries identity only for everything else: id, owner, and declared outputs,
    with NO mutable state.

    That asymmetry is the load-bearing design decision. It is what lets the
    #304 dependency gate answer "owned by workstream spine, no
    contract_published event yet" instead of the useless "unknown id", WITHOUT
    reconstructing another workstream's board -- which is the anti-pattern #303
    forbids, and which would also be a lie, since the other clone's state is not
    visible from here.
    """
    if workstream_id not in {w.get("id") for w in manifest.get("workstreams") or []}:
        raise ManifestError(
            "workstream %r is not in this Cycle's manifest (have: %s)"
            % (
                workstream_id,
                ", ".join(sorted(str(w.get("id")) for w in manifest.get("workstreams") or []))
                or "none",
            )
        )

    owned: List[Dict[str, Any]] = []
    foreign: List[Dict[str, Any]] = []
    for unit in manifest.get("work_units") or []:
        if unit.get("owner_workstream") == workstream_id:
            owned.append(dict(unit))
        else:
            foreign.append(
                {
                    "id": unit.get("id"),
                    "owner_workstream": unit.get("owner_workstream"),
                    "outputs": [dict(o) for o in (unit.get("outputs") or [])],
                }
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "spq.cycle.projection",
        "cycle_id": manifest.get("cycle_id"),
        "cycle_seq": manifest.get("cycle_seq"),
        "manifest_hash": manifest.get("manifest_hash"),
        "manifest_revision": manifest.get("manifest_revision"),
        "workstream_id": workstream_id,
        "goal": manifest.get("goal"),
        "acceptance_criteria": list(manifest.get("acceptance_criteria") or []),
        "baseline_sha": manifest.get("baseline_sha"),
        "integration_ref": manifest.get("integration_ref"),
        "verification": dict(manifest.get("verification") or {}),
        "integration_policy": dict(manifest.get("integration_policy") or {}),
        "shared_surfaces": [dict(s) for s in (manifest.get("shared_surfaces") or [])],
        "work_units": owned,
        "foreign_units": foreign,
    }


def owners(manifest: Dict[str, Any]) -> Dict[str, str]:
    """unit_id -> owning workstream, for the dependency gate's `dep_context`."""
    return {
        str(u.get("id")): str(u.get("owner_workstream") or "")
        for u in manifest.get("work_units") or []
        if u.get("id")
    }


def admitted_ids(manifest: Dict[str, Any]) -> List[str]:
    return [str(u.get("id")) for u in manifest.get("work_units") or [] if u.get("id")]


def satisfies(declared: str, observed: str) -> bool:
    """Does an `observed` condition meet-or-exceed a `declared` one?

    Uses the explicit `CONDITION_RANK`. `environment_ready` and
    `cycle_integrated` only ever satisfy themselves: the first is an out-of-band
    assertion that nothing else implies, and the second is cross-Cycle and owned
    by #305.
    """
    if declared not in CONDITION_RANK or observed not in CONDITION_RANK:
        return False
    if declared in ("environment_ready", "cycle_integrated"):
        return observed == declared
    if observed in ("environment_ready", "cycle_integrated"):
        return observed == declared
    return CONDITION_RANK[observed] >= CONDITION_RANK[declared]
