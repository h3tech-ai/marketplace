"""The sealed Cycle declaration, and every refusal that makes it worth sealing.

One document per Cycle, fixed at Commit. `SC-MTH-007` requires the admitted set
to be **closed at Commit** and the barrier's criteria **published in advance**;
`SC-MTH-008` requires a path scope per Work Unit and exactly one owner for a
shared path; `SC-MTH-012` requires a declared source region and one Engineering
Lead; `SC-MTH-013` requires one repository. All of those are properties of this
record, so validating it is where they hold or do not.

WHY VALIDATION IS A LIST AND NOT AN EXCEPTION. A caller sealing a Cycle wants
every problem at once. Raising on the first means an author fixes six
declarations in six round trips, and the sixth is when they stop reading the
messages. `problems()` returns all of them; `seal()` raises with the whole list.

THE ORDER OF THE PATH CHECKS IS LOAD-BEARING, and it is the P0 finding made
mechanical. An ABSENT `path_scope` is refused BEFORE any scope is compared,
because `path_scope.intersects` raises on an absent side rather than answering
-- the predecessor's `_scopes_disjoint([], [...])` returned True, disjoint from
everything, and promoting that comparator to an admission condition unchanged
would have admitted a scope-less unit as concurrent-safe with the whole
repository.

DEPENDENCY SATISFACTION AND PATH DISJOINTNESS ARE EVALUATED SEPARATELY. §7.2:
"independence in the dependency graph is not path disjointness." Two Work Units
with no edge between them may still address one file, and a dependency check
cannot see it. So `problems()` runs both and never derives either from the
other -- and a pair whose scopes intersect is not refused outright, it is
required to declare an `execution_order`, which is `M-04`'s reading: the
refusal is admission to a CONCURRENT execution set, and sequential work inside
one Cycle may intersect.

A MUTUAL PAIR IS NOT A LOOP TO REJECT, IT IS WORK TO CO-ADMIT. `SC-MTH-012`:
work that cannot be ordered cannot be split, so it belongs in one Cycle. Two
units that depend on each other inside this declaration are therefore FINE --
they are the atomic case, already co-admitted. What is refused is a dependency
naming a unit this Cycle did not admit, because that is the split.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Mapping, Sequence

import cycle_authority
import path_scope

SCHEMA_VERSION = "1"
KIND = "spq.cycle.declaration"

#: A Work Unit id is interpolated into filenames -- `spq_ledger` writes one
#: event per unit as `<seq>-<condition>-<unit_id>.json`, and receipts are
#: `<unit_id>-<role>.json` -- so any id this document admits becomes a path
#: segment. Refused HERE, at the boundary that decides what is admitted,
#: because the write-time containment check in `spq_ledger.publish` must not be
#: the only one: containment is defence in depth for a caller reaching the write
#: directly, and admission is where a traversing id is supposed to be rejected.
UNIT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

#: `SC-MTH-003`: each specification declares a kind, the kind constrains which
#: fields are required, and **an unrecognised kind is rejected rather than
#: defaulted**.
UNIT_KINDS = ("feature", "story", "bugfix", "task", "release")

#: What a dependency edge may require of its producer. An edge names a
#: VERIFIABLE condition rather than merely "done", because `done` is the
#: producer's own claim about itself while the other three are facts another
#: unit can check -- which is what lets `spq_ledger` resolve an edge without
#: asking the producer.
#:
#: `cycle_integrated` is gone with the Coordination Cycle (`SPD-194`): it was
#: the cross-Cycle condition, and there is no second Cycle to wait on now that
#: unorderable work is co-admitted to one.
DEP_CONDITIONS = (
    "done",
    "contract_published",
    "artifact_published",
    "integrated",
    "environment_ready",
)


#: The criteria a Cycle barrier publishes at Commit. Declared here, in one
#: place, because three different Sync-criteria counts shipped in one product
#: when the list was restated in prompts, in a renderer and in two hosts.
BARRIER_CRITERIA = (
    "admitted_set_closed",
    "path_scopes_disjoint",
    "shared_paths_owned",
    "criteria_all_returned",
    "acceptance_criteria_met",
    "regression_green",
    "trunk_integrated",
)


class DeclarationError(ValueError):
    """A declaration that cannot be sealed, carrying every problem found."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__(
            "the Cycle declaration cannot be sealed:\n  - "
            + "\n  - ".join(self.problems)
        )


def _unit_problems(units: Sequence[Mapping[str, Any]]) -> List[str]:
    found: List[str] = []
    seen: Dict[str, int] = {}
    for index, unit in enumerate(units):
        label = str(unit.get("id") or "<unit %d>" % index)
        if not unit.get("id"):
            found.append("unit %d declares no id" % index)
        elif label in seen:
            found.append(
                "unit %s is admitted twice (positions %d and %d); an admitted "
                "set with a duplicate has no single owner for that work"
                % (label, seen[label], index)
            )
        else:
            seen[label] = index

        if unit.get("id") and not UNIT_ID_RE.match(str(unit.get("id"))):
            found.append(
                "unit %s declares an id that is not a safe path segment (%s). "
                "Ids become event and receipt FILENAMES, so an id carrying `/` "
                "or `..` would place a Cycle's records outside the Cycle's own "
                "directory" % (label, UNIT_ID_RE.pattern)
            )

        kind = unit.get("kind")
        if kind not in UNIT_KINDS:
            found.append(
                "unit %s declares kind %r, which is not one of %s. An "
                "unrecognised kind is rejected rather than defaulted, because "
                "the kind decides which fields are required"
                % (label, kind, ", ".join(UNIT_KINDS))
            )

        criteria = unit.get("acceptance_criteria")
        if not isinstance(criteria, (list, tuple)) or not criteria:
            found.append(
                "unit %s declares no acceptance criteria, so nothing can judge "
                "it and the barrier has nothing to evaluate" % label
            )
        elif not all(str(c).strip() for c in criteria):
            found.append("unit %s declares a blank acceptance criterion" % label)

        scope = unit.get("path_scope")
        if not isinstance(scope, (list, tuple)) or not scope:
            found.append(
                "unit %s declares no path scope. Refused here rather than "
                "treated as disjoint: an absent scope compared against "
                "anything would read as `addresses nothing`, which is how a "
                "unit becomes concurrent-safe with the whole repository" % label
            )
        else:
            try:
                path_scope.normalize_all(scope)
            except path_scope.PathScopeError as exc:
                found.append("unit %s: %s" % (label, exc))
    return found


def _concurrency_problems(units: Sequence[Mapping[str, Any]]) -> List[str]:
    """Intersecting scopes need an explicit order; disjoint ones need nothing.

    `M-04`: the refusal is admission to a CONCURRENT set. So this does not
    refuse an intersection, it refuses an intersection that nobody sequenced.
    """
    found: List[str] = []
    usable = [
        u for u in units
        if u.get("id") and isinstance(u.get("path_scope"), (list, tuple)) and u["path_scope"]
    ]
    for i, left in enumerate(usable):
        for right in usable[i + 1:]:
            try:
                overlaps = path_scope.intersects(left["path_scope"], right["path_scope"])
            except path_scope.PathScopeError:
                continue  # already reported by _unit_problems
            if not overlaps:
                continue
            lo, ro = left.get("execution_order"), right.get("execution_order")
            if not isinstance(lo, int) or not isinstance(ro, int) or lo == ro:
                found.append(
                    "units %s and %s declare intersecting path scopes and no "
                    "distinct execution_order. Intersecting work may run in one "
                    "Cycle -- sequentially -- but never concurrently, so each "
                    "side needs an order the other does not share"
                    % (left["id"], right["id"])
                )
    return found


def dep_target(dep: Any) -> str:
    """The Work Unit an edge points at, whichever shape the edge takes.

    Two shapes are live, and both are legitimate. A bare id is the simple case;
    `{"unit_id": ..., "condition": ...}` is an edge that names what it waits
    FOR, which is the shape `spq_ledger` resolves. This function exists because
    the first version of `_dependency_problems` compared `str(dep)` against the
    admitted ids -- which silently never matches a dict, so every conditioned
    edge read as "this Cycle did not admit it" and refused a valid declaration.
    Found by fixtures whose subject was the ledger, not the declaration.
    """
    if isinstance(dep, Mapping):
        return str(dep.get("unit_id") or dep.get("id") or "")
    return str(dep or "")


def dep_condition(dep: Any) -> str:
    """What an edge waits for. A bare id waits for `done`, which is the
    weakest condition and therefore the safe default to read into silence."""
    if isinstance(dep, Mapping):
        return str(dep.get("condition") or "done")
    return "done"


#: Conditions a consumer verifies by RECOMPUTING a digest, which needs a script
#: the declaration seals. `done` is board state and `integrated` is git
#: ancestry, so neither needs one; `environment_ready` is claim-only by design.
#:
#: `spq_ledger.VERIFIABLE` minus `integrated`, and kept as its own name rather
#: than derived, because the question here is narrower: not "can a consumer
#: check this" but "does checking it need something Commit must provide".
NEEDS_DIGEST_SCRIPT = ("contract_published", "artifact_published")


def _verification_problems(declaration: Mapping[str, Any]) -> List[str]:
    """A condition nobody can verify is refused AT COMMIT, not left to hang.

    `spq_ledger.verify_condition` reads
    `manifest["verification"]["digest_script"]` to recompute a published
    digest, and `cycle_records` had no `verification` concept at all -- so
    `open_cycle` could not seal one and the check returned `verified: None`
    forever. The consumer then sat at `dep_condition_unverified` permanently:
    admitted, and unable to proceed, with nothing in the product able to
    unblock it.
    #
    Failing closed at admission is the same shape as every other refusal here.
    A Cycle that declares an edge it cannot check is not a Cycle that waits --
    it is a Cycle that cannot run, and Commit is where that is cheap to say.
    """
    found: List[str] = []
    block = declaration.get("verification")
    if block is not None and not isinstance(block, Mapping):
        return ["verification must be an object of named verifiers"]
    script = str((block or {}).get("digest_script") or "").strip()

    needing: List[str] = []
    for unit in declaration.get(UNITS_FIELD) or ():
        if not isinstance(unit, Mapping):
            continue
        for dep in unit.get("depends_on") or ():
            condition = dep_condition(dep)
            if condition in NEEDS_DIGEST_SCRIPT:
                needing.append("%s -> %s (%s)" % (
                    unit.get("id"), dep_target(dep), condition))
    if needing and not script:
        found.append(
            "these edges declare a condition verified by recomputing a digest, "
            "and this declaration seals no `verification.digest_script` to "
            "recompute it with: %s. Without one the consumer's check answers "
            "`unverified` forever, so the edge would be admitted and "
            "permanently unsatisfiable. Seal a script, or use a condition the "
            "consumer can check without one (`done`, `integrated`)."
            % "; ".join(needing[:6])
        )
    if script and (script.startswith("/") or ".." in script.split("/")):
        found.append(
            "verification.digest_script %r must be a repository-relative path "
            "inside the project: the consumer runs it, and a path that escapes "
            "the repository is a script the declaration cannot vouch for"
            % script
        )
    return found


def _dependency_problems(units: Sequence[Mapping[str, Any]]) -> List[str]:
    """Evaluated separately from path scope, and never derived from it.

    A dependency on a unit this Cycle did not admit is the split `SC-MTH-012`
    refuses. A mutual pair inside the Cycle is not: that is the atomic case,
    already co-admitted, which is exactly what the method asks for.
    """
    found: List[str] = []
    admitted = {str(u.get("id")) for u in units if u.get("id")}
    for unit in units:
        for dep in unit.get("depends_on") or []:
            target = dep_target(dep)
            if not target:
                found.append(
                    "unit %s declares a dependency naming no Work Unit (%r). An "
                    "edge that points nowhere cannot be satisfied and cannot be "
                    "refused, so it would hold the unit forever"
                    % (unit.get("id"), dep)
                )
                continue
            if target not in admitted:
                found.append(
                    "unit %s depends on %s, which this Cycle did not admit. "
                    "Work that cannot be ordered cannot be split: co-admit the "
                    "pair to one Cycle, or consume a published version of it at "
                    "this Cycle's own Commit" % (unit.get("id"), target)
                )
            condition = dep_condition(dep)
            if condition not in DEP_CONDITIONS:
                found.append(
                    "unit %s waits on %s for %r, which is not a condition "
                    "anything can verify (%s). `cycle_integrated` retired with "
                    "the Coordination Cycle"
                    % (unit.get("id"), target, condition,
                       ", ".join(DEP_CONDITIONS))
                )
    return found


def _ownership_problems(
    units: Sequence[Mapping[str, Any]], shared: Sequence[Mapping[str, Any]]
) -> List[str]:
    """Exactly one owner per shared path -- never none, never two."""
    found: List[str] = []
    owners = [
        (u["id"], u["path_scope"]) for u in units
        if u.get("id") and isinstance(u.get("path_scope"), (list, tuple)) and u["path_scope"]
    ]
    for entry in shared:
        raw_path = entry.get("path")
        declared = entry.get("owning_unit_id")
        try:
            claimed = path_scope.owners_of(raw_path, owners)
        except path_scope.PathScopeError as exc:
            found.append("shared path %r: %s" % (raw_path, exc))
            continue
        if not declared:
            found.append(
                "shared path %r declares no owning unit. A path outside every "
                "region needs exactly one owner; with none, whoever merges last "
                "decides what it says" % raw_path
            )
        elif str(declared) not in claimed:
            found.append(
                "shared path %r names %s as its owner, and %s does not declare "
                "a scope covering it (%s do)"
                % (raw_path, declared, declared,
                   ", ".join(sorted(claimed)) or "no units")
            )
        if len(claimed) > 1:
            found.append(
                "shared path %r is covered by %d units (%s). Exactly one owns "
                "it: two owners is the same unrecoverable merge as none"
                % (raw_path, len(claimed), ", ".join(sorted(claimed)))
            )
    return found


def problems(declaration: Mapping[str, Any]) -> List[str]:
    """Every reason this declaration cannot be sealed. Empty means it can."""
    found: List[str] = []

    for field, why in (
        ("cycle_id", "a Cycle needs an identity its records can be bound to"),
        ("repository", "a Cycle addresses one repository (`SC-MTH-013`), and a "
                       "declaration that names none cannot be refused for "
                       "spanning two"),
        ("trunk_ref", "closing integrates to one shared trunk (`SC-MTH-007`); "
                      "without a named trunk there is nothing to integrate into"),
        ("baseline_ref", "a cut must not move the approved baseline, which "
                         "needs the baseline to be named"),
        ("goal", "a Cycle is demonstrated against its goal"),
        ("engineering_lead", "exactly one Engineering Lead owns a Cycle "
                             "(`SC-MTH-012`)"),
    ):
        if not str(declaration.get(field) or "").strip():
            found.append("%s is required: %s" % (field, why))

    region = declaration.get("source_region")
    if not isinstance(region, (list, tuple)) or not region:
        found.append(
            "source_region is required: two concurrent Cycles are kept apart by "
            "declaration rather than by coordination (`SC-MTH-012`), so a Cycle "
            "that declares no region cannot be checked against another's"
        )
    else:
        try:
            path_scope.normalize_all(region)
        except path_scope.PathScopeError as exc:
            found.append("source_region: %s" % exc)

    crew = declaration.get("crew")
    if crew is not None and not isinstance(crew, (list, tuple)):
        found.append("crew is a list of names; it grants nothing and is not a table")

    units = declaration.get("admitted_units")
    if not isinstance(units, (list, tuple)) or not units:
        found.append(
            "admitted_units is required and non-empty: the barrier ranges over "
            "the set Commit fixed, and an empty set makes every barrier vacuous"
        )
        units = []
    else:
        found.extend(_unit_problems(units))
        found.extend(_concurrency_problems(units))
        found.extend(_dependency_problems(units))

    found.extend(_verification_problems(declaration))
    shared = declaration.get("shared_path_owners") or []
    if not isinstance(shared, (list, tuple)):
        found.append("shared_path_owners is a list of {path, owning_unit_id}")
    else:
        found.extend(_ownership_problems(units, shared))

    criteria = declaration.get("barrier_criteria")
    if not isinstance(criteria, (list, tuple)) or not criteria:
        found.append(
            "barrier_criteria is required: `SC-MTH-007` publishes them in "
            "advance so the Cycle sizes against them at Commit instead of "
            "discovering them at close"
        )
    else:
        missing = [c for c in BARRIER_CRITERIA if c not in criteria]
        if missing:
            found.append(
                "barrier_criteria omits %s. A project may ADD a criterion or "
                "raise a threshold; it may not remove one the method declares "
                "(`SC-MTH-020`'s shape, and §3.4's rule)" % ", ".join(missing)
            )

    lead = declaration.get("engineering_lead")
    if lead and declaration.get("lead_role"):
        try:
            cycle_authority.assert_grants_nothing(str(declaration["lead_role"]))
        except cycle_authority.AuthorityError as exc:
            found.append("lead_role: %s" % exc)

    return found


def canonical_bytes(body: Mapping[str, Any]) -> bytes:
    """The bytes the hash is over. Sorted and separator-fixed, so two callers
    that agree on the content agree on the digest."""
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_hash(body: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(body)).hexdigest()


#: What the sealed hash is called. Consumers written against the retired
#: `spq_manifest` read `manifest_hash`; this module names it
#: `declaration_hash`, because what is sealed is a DECLARATION and calling it a
#: manifest is what let a lane topology live inside it. Exported so a consumer
#: reads the name from here rather than hardcoding either spelling -- and so
#: there is exactly ONE key, not an alias pair, which is the drift this epic
#: exists to remove.
HASH_FIELD = "declaration_hash"

#: Where the admitted set lives, exported for the same reason and read the same
#: way. Consumers written against the retired `spq_manifest` read `work_units`,
#: and a consumer that hardcoded that spelling did not fail -- it silently read
#: an EMPTY set, which is how `spq_ledger.publish` came to refuse every Work
#: Unit its own Cycle had admitted, and how `resolve_authored_cases` came to
#: find no authored cases in a document that carried them.
UNITS_FIELD = "admitted_units"


def seal(declaration: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate, stamp and hash a declaration, or raise with every problem.

    `schema_version`, `kind` and `path_grammar` are inside the hash: a document
    re-read under different rules is a different document, and a hash that did
    not cover the rules would let one be swapped for the other.
    """
    found = problems(declaration)
    if found:
        raise DeclarationError(found)
    body = dict(declaration)
    body["schema_version"] = SCHEMA_VERSION
    body["kind"] = KIND
    body["path_grammar"] = path_scope.GRAMMAR_VERSION
    # ALWAYS PRESENT, EVEN EMPTY, and that is the same discipline the metrics
    # run on: absent and zero mean opposite things. An omitted map cannot be
    # told apart from a forgotten one, so "this Cycle declares no shared path"
    # would read identically to "nobody wrote the section" -- and `C-04`'s
    # `shared_paths_owned` criterion would then be evaluating a field it
    # cannot distinguish from a gap. An empty list is a stated position; a
    # missing key is not. Inside the hash, so the position cannot be added
    # later without resealing.
    body["shared_path_owners"] = list(declaration.get("shared_path_owners") or [])
    body.pop("declaration_hash", None)
    sealed = dict(body)
    sealed["declaration_hash"] = compute_hash(body)
    return sealed


def verify_hash(sealed: Mapping[str, Any]) -> bool:
    """Does this document still hash to what it claims?"""
    claimed = str(sealed.get("declaration_hash") or "")
    if not claimed:
        return False
    body = {k: v for k, v in sealed.items() if k != "declaration_hash"}
    return compute_hash(body) == claimed


def shared_path_owners(declaration: Mapping[str, Any]) -> Dict[str, str]:
    """path -> the one unit that owns it, from a sealed declaration.

    The reader for `C-04`'s `shared_paths_owned` criterion and for anything
    that needs to answer "who may touch this file". It replaces the retired
    `spq_manifest.owners`, whose answer was unit -> LANE: with the lane gone
    there is no owner of a unit, and the ownership the method actually
    declares is of a shared PATH.
    """
    owners: Dict[str, str] = {}
    for entry in declaration.get("shared_path_owners") or []:
        if not isinstance(entry, Mapping):
            continue
        path = str(entry.get("path") or "").strip()
        unit = str(entry.get("owning_unit_id") or "").strip()
        if path:
            owners[path] = unit
    return owners


def admitted_ids(declaration: Mapping[str, Any]) -> List[str]:
    """The ids of the admitted set, in declaration order."""
    return [
        str(unit.get("id"))
        for unit in declaration.get(UNITS_FIELD) or []
        if isinstance(unit, Mapping) and unit.get("id")
    ]


def regions_overlap(left: Sequence[object], right: Sequence[object]) -> bool:
    """Do two Cycles' declared regions intersect?

    The only cross-Cycle check in the system, and it runs at DECLARATION rather
    than at admission -- which is what lets concurrent Cycles stay independent:
    no Cycle inspects what another is admitting.
    """
    return path_scope.intersects(left, right)
