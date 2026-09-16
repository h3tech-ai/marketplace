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

# An authored acceptance-case id is the KEY a verifying receipt reports its
# per-case outcome under, so it is matched literally across the manifest/receipt
# boundary. Same containment argument as the unit id, one character wider: `.`
# is allowed because tracker case keys carry it (`STAR-27492.1`).
ACCEPTANCE_CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")

SCHEMA_VERSION = "1.0"
KIND = "spq.cycle.manifest"

# ── the test-first prove path (#406, proposal 3.3, ADR-032 section 4) ────────
#
# `SP-WRK-007` requires a verifying stage to verify against criteria declared
# BEFORE any producing stage ran. V1 did the reverse: QE authored tests after
# reading SE's diff, so the test encoded the implementation rather than the
# requirement. Making that structural rather than a review habit needs the
# authored set to live in an artifact that (a) exists at COMMIT and (b) cannot
# be edited afterwards without leaving a trace.
#
# The Cycle manifest is already both of those things. It is authored at COMMIT,
# it is hash-sealed, and `revise` demands an explicit supersede. So the authored
# cases go on the Work Unit record here rather than into a side file: a side
# file would have needed its own integrity story, and a second definition of
# "the authored set" is how the two come to disagree.
#
# Three admission classifications, because migration and enforcement need
# different answers and a single boolean would have collapsed them:
#
#   TEST_FIRST_AUTHORED -- the unit carries a non-empty `acceptance_cases` list.
#                         The full contract: producer gets them as its target,
#                         prover executes them, `tests_pass` is evaluated
#                         against them first.
#   TEST_FIRST_GAP      -- the unit carries `criteria_gap_declared`. Admitted,
#                         and RECORDED as admitted without authored cases. The
#                         declared-gap vocabulary is #403's, reused rather than
#                         reinvented: a thin gate must render thin.
#   TEST_FIRST_ABSENT   -- the unit record has no `acceptance_cases` KEY at all,
#                         i.e. it predates this field. Admitted with an
#                         implicit gap recorded on its behalf, so an in-flight
#                         Cycle sealed before #406 is not bricked by a
#                         requirement its manifest could not have met.
#
# Key ABSENCE is what separates the last two from a refusal, and that is
# deliberate: `_normalize_unit` writes the key on every unit it touches, so any
# manifest built by this module states its position. An empty list is therefore
# a unit that WAS asked and authored nothing, which is refused. See the
# forgeability note on `admission_problems`.
TEST_FIRST_AUTHORED = "authored"
TEST_FIRST_GAP = "criteria_gap_declared"
TEST_FIRST_ABSENT = "absent"

#: Reason stamped on the implicit gap for a pre-#406 unit record.
LEGACY_GAP_REASON = (
    "this Work Unit record carries no `acceptance_cases` field, so it was "
    "admitted before the test-first prove path existed (#406). tests_pass for "
    "it is evaluated on the pre-#406 rules and is NOT traced to criteria "
    "declared before the producing stage ran."
)

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


# ── reading a sealed document of EITHER generation ──────────────────────────
#
# `open_cycle` now writes a `cycle_records` declaration: keyed on
# `declaration_hash`, units under `admitted_units`, hashed by that module's own
# `canonical_bytes`. This module's `manifest_hash` / `work_units` / `verify_hash`
# describe the retired `spq.cycle.manifest`.
#
# The three accessors below decide, per DOCUMENT, which generation it is and
# answer in that generation's terms. That is NOT the alias pair
# `cycle_records.HASH_FIELD` warns against: the warning is about ONE document
# answering to two names, and no document written by either sealer does. A
# reader that hardcoded the retired spelling did not fail loudly -- it read a
# missing hash as "unverifiable" and a missing unit list as "empty" -- which is
# how the DoD gate came to report `untrusted_manifest` for every Cycle the
# replacement state machine opens.


def sealed_hash(document: Dict[str, Any]) -> str:
    """The hash this sealed document claims, in the key it names it with."""
    import cycle_records

    for key in (cycle_records.HASH_FIELD, "manifest_hash"):
        value = str(document.get(key) or "").strip()
        if value:
            return value
    return ""


def verify_sealed(document: Dict[str, Any]) -> bool:
    """Does this sealed document still hash to what it claims?

    Dispatches on the key it carries, because the two generations hash
    different bodies with different canonical bytes: verifying a declaration
    with this module's `verify_hash` reads `manifest_hash` as absent and
    answers False, which is "tampered" rather than "not mine".
    """
    import cycle_records

    if str(document.get(cycle_records.HASH_FIELD) or "").strip():
        return cycle_records.verify_hash(document)
    return verify_hash(document)


def sealed_units(document: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The admitted Work Unit records, from whichever key holds them."""
    import cycle_records

    for key in (cycle_records.UNITS_FIELD, "work_units"):
        units = document.get(key)
        if isinstance(units, (list, tuple)):
            return [u for u in units if isinstance(u, dict)]
    return []


# ── where the seal can be read from (#507) ──────────────────────────────────
#
# A sealed manifest is written to TWO places by `_seal_manifest`: the local
# store under the gitignored orchestrator tree, and the committed
# cross-clone transport under `.synaptory/cycles/`. Every reader that
# consulted only the first one treated the loss of ONE COPY as the loss of
# the seal -- which is how `rm` of a single file downgraded #406's gate to
# the pre-#406 rules while an identical copy sat on disk beside it (#507).
#
# So "read the seal" is defined here, once, with its provenance reported,
# rather than spelled per-caller. Two spellings of where the seal lives is a
# seal that quietly stops being one -- the same argument the hash rule at the
# top of this module makes about two spellings of the hash.

#: The local sealed copy, `.synaptory/.orchestrator/spq/cycles/<id>/manifest.json`.
#: Written 0444 and authoritative for THIS clone.
SEAL_LOCAL = "local"

#: The committed transport, `.synaptory/cycles/<id>/manifest.json`. A different
#: file with a different lifetime, git-TRACKABLE (the documented narrow
#: un-ignore), and the only copy that reaches another clone.
SEAL_COMMITTED = "committed"

#: The committed transport as `HEAD` holds it. Outside the working tree
#: entirely: removing the file does not remove the blob, so this rung answers
#: after both on-disk copies are gone.
SEAL_GIT = "git"

#: Ordered most-local first. The local copy wins so a clone that has both
#: keeps answering from the document its own verbs maintain, and so tampering
#: with the local copy still reads as tampering rather than being papered over
#: by a pristine sibling.
#: A copy was found and it is not the revision the board is executing. A
#: DIFFERENT answer from "no seal": the operator action is to commit or
#: restore the CURRENT revision, not to restore a deleted one, and consuming
#: the older document would let a superseded, easier authored set finish the
#: unit (#396).
#:
#: Named for the condition rather than the rung, because it is not the git
#: rung's alone: the first cut of this compared only there, so restoring an
#: old revision into either on-disk path walked straight past the check.
SEAL_STALE = "stale"

SEAL_ORIGINS = (SEAL_LOCAL, SEAL_COMMITTED, SEAL_GIT, SEAL_STALE)

_GIT_TIMEOUT_S = 20


def _git(project_dir: str, *args: str) -> Tuple[int, str]:
    """`(returncode, stdout)`. Never raises: no git is not a verdict."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 - no git, no repo, or a hang
        return 1, ""
    return result.returncode, result.stdout


def committed_relpath(project_dir: str, cycle_id: str) -> str:
    """The committed manifest's repo-relative path, in git's own spelling."""
    import os

    import spq_paths

    absolute = spq_paths.committed_manifest_path(project_dir, cycle_id)
    return os.path.relpath(absolute, str(project_dir)).replace(os.sep, "/")


def seal_paths(project_dir: str, cycle_id: str) -> List[Tuple[str, str]]:
    """`[(origin, path)]` for the on-disk copies, most-local first.

    Empty for an identity `spq_paths` refuses. The Cycle id reaching here comes
    from the agent-writable board pointer, so a malformed one has to answer
    "there is no such seal" rather than raise an `IdentityError` inside a gate
    evaluation.
    """
    import spq_paths

    try:
        return [
            (SEAL_LOCAL, spq_paths.manifest_path(project_dir, cycle_id)),
            (SEAL_COMMITTED,
             spq_paths.committed_manifest_path(project_dir, cycle_id)),
        ]
    except Exception:  # noqa: BLE001 - IdentityError and friends
        return []


def read_sealed(
    project_dir: str,
    cycle_id: str,
    *,
    allow_git: bool = True,
    expect_hash: str = "",
) -> Tuple[Optional[Dict[str, Any]], str]:
    """`(manifest, origin)` for `cycle_id`, or `(None, "")` if no copy reads.

    The hash is NOT checked here: `verify_hash` is the caller's decision,
    because a document that fails its own hash is a different answer from no
    document at all and this function must not collapse the two.

    `allow_git=False` for a caller that must stay free of subprocesses. The
    git rung is reached only when both on-disk copies are unreadable, which on
    a healthy project is never.

    `expect_hash` IS THE REVISION THE BOARD IS EXECUTING, and the git rung is
    the only one that needs it (#396). `revise_manifest` writes revision N+1
    to both working-tree copies without committing it, so `HEAD` still holds
    N. Recovering N there and verifying only that it hashes to itself made
    deleting the current revision cheaper than honouring it: the unit finishes
    under the superseded, easier authored set and the case the revision added
    is silently gone. Self-consistency answers "is this a real document", not
    "is this the document in force".

    Given a hash, a document from ANY rung that is not that revision is
    reported as `SEAL_STALE` with no document, so a caller blocks on a stale
    seal rather than consuming an older one. Without a hash every rung behaves
    as before, because a caller that knows of no current revision has nothing
    to be stale against.

    WHAT `expect_hash` IS WORTH, stated exactly. It comes from the board, which
    the graded principal can write, so it raises the cost of the bypass without
    closing it: rewriting `sealed_manifest_hash` to a superseded revision and
    then restoring that revision defeats this comparison. Between a revision
    and the commit that carries it there is NO record of the current revision
    outside the working tree, so nothing offline can do better.

    A PERMANENT DESIGN LIMIT FOR AN OFFLINE PROJECT, not a gap awaiting an
    owner (proposal §12.17, "a decision rather than an oversight"). The two
    closures that exist -- a signature by a key the graded principal does not
    hold, or the control plane's append-only `cycle_manifests` row -- both
    require connectivity AT THE MOMENT OF REVISION, and requiring either would
    make the Definition of Done gate depend on the control plane, which §3.3
    refuses: local delivery state is canonical and an offline project must
    still be able to evaluate its own DoD. So the residual is a property of
    the situation rather than of this implementation, and closing it is a
    methodology decision about §3.3, not a defect to fix here.

    For a CONNECTED project the same finding IS closed, and elsewhere:
    `manifest_emitter.read_cycle_authority` asks the recorded row, the
    connected/offline answer comes from the CLI's own stamp and session --
    neither of which is in the project, so deleting project files cannot fake
    offline -- and unreachable fails closed. #507 recorded both halves and is
    closed for what it delivered; the offline half was declared, not deferred.
    The tripwire is
    `test_seal_absence.py::test_an_offline_project_keeps_the_local_marker_and_its_limit`.
    """
    # EVERY RUNG COMPARES, not just the last one. The first cut checked only
    # the git rung, so restoring an old revision into either on-disk path was
    # a shorter route to the same resurrection: the loop returned before any
    # comparison ran. A stale copy is skipped rather than returned, so a
    # current copy further down the order still answers.
    stale = False
    for origin, path in seal_paths(project_dir, cycle_id):
        try:
            with open(path, encoding="utf-8") as handle:
                found = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(found, dict) and found:
            if expect_hash and sealed_hash(found) != str(expect_hash):
                stale = True
                continue
            return found, origin
    if not allow_git:
        return None, SEAL_STALE if stale else ""
    relpath = ""
    try:
        relpath = committed_relpath(project_dir, cycle_id)
    except Exception:  # noqa: BLE001 - an invalid id has no committed path
        return None, SEAL_STALE if stale else ""
    # `HEAD:./<path>` and not `HEAD:<path>`: the second form is relative to the
    # REPOSITORY ROOT, so it would read the wrong file (or nothing) whenever the
    # project directory is not the root -- a nested SPQ project inside a larger
    # repository, which is a shape the worktree flow produces routinely.
    code, out = _git(project_dir, "show", "HEAD:./%s" % relpath)
    if code != 0 or not out.strip():
        return None, SEAL_STALE if stale else ""
    try:
        found = json.loads(out)
    except ValueError:
        return None, SEAL_STALE if stale else ""
    if isinstance(found, dict) and found:
        if expect_hash and sealed_hash(found) != str(expect_hash):
            return None, SEAL_STALE
        return found, SEAL_GIT
    return None, SEAL_STALE if stale else ""


def sealed_in_history(project_dir: str, cycle_id: str) -> bool:
    """Did any commit ever carry this Cycle's committed manifest?

    The one record of a seal that the subject of the gate cannot reach by
    writing files: removing the path from `HEAD` takes a commit, and the commit
    that removed it is itself the evidence. Consulted only after `read_sealed`
    has come back empty, so the cost lands on the anomalous path.
    """
    try:
        relpath = committed_relpath(project_dir, cycle_id)
    except Exception:  # noqa: BLE001
        return False
    code, out = _git(
        project_dir, "rev-list", "--max-count=1", "HEAD", "--", relpath
    )
    return code == 0 and bool(out.strip())


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


def normalize_case(entry: Any, index: int) -> Dict[str, Any]:
    """One authored acceptance case, normalized (#406).

    Accepts a dict (`{id, statement, criterion_ref}`) or a bare string, which
    becomes the statement with a positional id. Positional ids are stable
    precisely because the manifest is immutable once sealed: a `revise` that
    reorders the list produces a new hash and a recorded supersede, so a
    receipt reporting `AC-2` can always be resolved against the revision it
    was dispatched under. Lowering the authoring bar matters here -- a PO who
    must invent ids for five cases authors fewer cases.
    """
    if not isinstance(entry, dict):
        return {
            "id": "AC-%d" % (index + 1),
            "statement": str(entry or "").strip(),
            "criterion_ref": "",
        }
    return {
        "id": str(entry.get("id") or "AC-%d" % (index + 1)).strip(),
        "statement": str(entry.get("statement") or entry.get("text") or "").strip(),
        # Which declared acceptance criterion this case proves. Traceability
        # only: nothing selects a path or a command from it.
        "criterion_ref": str(entry.get("criterion_ref") or "").strip(),
    }


def normalize_cases(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, (list, tuple)):
        return []
    return [normalize_case(entry, i) for i, entry in enumerate(raw)]


def normalize_case_gap(raw: Any) -> Optional[Dict[str, Any]]:
    """A `criteria_gap_declared` block on a Work Unit, normalized (#406).

    A gap is only a gap when someone declared it: an empty dict, or one with
    no reason, is not a declaration and normalizes to None so admission
    refuses it. "Absence of a reason" being accepted as a reason is exactly
    the silent-nothing this ticket exists to remove.
    """
    if not isinstance(raw, dict):
        return None
    reason = str(raw.get("reason") or "").strip()
    if not reason:
        return None
    return {
        "reason": reason,
        "declared_by": str(raw.get("declared_by") or "").strip(),
        "declared_at": str(raw.get("declared_at") or "").strip(),
    }


def _normalize_unit(raw: Dict[str, Any], owner_default: str = "") -> Dict[str, Any]:
    unit = {
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
    # #406. Written only when the input STATES a position, because key absence
    # is the migration signal `classify_unit` reads: normalizing an absent key
    # into `[]` here would turn every pre-#406 unit into "asked and authored
    # nothing", which admission refuses -- bricking exactly the in-flight
    # Cycles the declared-gap path exists to carry.
    if "acceptance_cases" in raw:
        unit["acceptance_cases"] = normalize_cases(raw.get("acceptance_cases"))
    gap = normalize_case_gap(raw.get("criteria_gap_declared"))
    if gap is not None:
        unit["criteria_gap_declared"] = gap
    return unit


def classify_unit(unit: Dict[str, Any]) -> str:
    """This unit's test-first admission class (#406).

    Reads the RAW record, so `acceptance_cases` key absence still means
    "predates the requirement" rather than "authored nothing". Authored cases
    win over a declared gap: a unit that carries both has the artifact, and
    the gap declaration is then stale rather than load-bearing.
    """
    cases = unit.get("acceptance_cases")
    if isinstance(cases, (list, tuple)) and normalize_cases(cases):
        return TEST_FIRST_AUTHORED
    if normalize_case_gap(unit.get("criteria_gap_declared")) is not None:
        return TEST_FIRST_GAP
    if "acceptance_cases" not in unit:
        return TEST_FIRST_ABSENT
    return ""  # asked, authored nothing, declared no gap -- refused


def authored_case_ids(unit: Dict[str, Any]) -> List[str]:
    """The authored case ids for one Work Unit, deduplicated in order."""
    seen: Set[str] = set()
    out: List[str] = []
    for case in normalize_cases(unit.get("acceptance_cases")):
        cid = case["id"]
        if cid and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


def unit_case_problems(unit: Dict[str, Any]) -> List[str]:
    """Shape problems in one unit's authored-case artifact (#406).

    Shape is checked on every manifest, whether or not admission requires the
    artifact to be present: a malformed case list is a defect in a document
    that IS trying to declare something, and letting it through would put an
    unmatchable id in a hash-sealed manifest.
    """
    uid = unit.get("id") or "<unnamed>"
    problems: List[str] = []
    raw = unit.get("acceptance_cases", None)
    if raw is not None and not isinstance(raw, (list, tuple)):
        problems.append(
            "work unit %r has acceptance_cases of type %s; it must be a list"
            % (uid, type(raw).__name__)
        )
        return problems
    seen: Set[str] = set()
    for case in normalize_cases(raw):
        cid = case["id"]
        if not ACCEPTANCE_CASE_ID_RE.match(cid):
            problems.append(
                "work unit %r declares acceptance case id %r, which must match "
                "%s: a verifying receipt reports its per-case outcome under "
                "this exact key, so an id the two sides cannot spell "
                "identically is a case nothing can prove"
                % (uid, cid, ACCEPTANCE_CASE_ID_RE.pattern)
            )
        if cid in seen:
            problems.append(
                "work unit %r declares acceptance case id %r twice; per-case "
                "outcomes are keyed by id, so a duplicate makes one of the two "
                "unprovable" % (uid, cid)
            )
        seen.add(cid)
        if not case["statement"]:
            problems.append(
                "work unit %r acceptance case %r has no statement: an id with "
                "no criterion behind it is a case a producer cannot target and "
                "a prover can trivially mark passed" % (uid, cid)
            )
    gap_raw = unit.get("criteria_gap_declared")
    if gap_raw is not None and normalize_case_gap(gap_raw) is None:
        problems.append(
            "work unit %r declares criteria_gap_declared with no `reason`. A "
            "gap is a statement about what is missing; without one it is "
            "indistinguishable from having skipped the question" % uid
        )
    return problems


def admission_problems(units: Sequence[Dict[str, Any]]) -> List[str]:
    """Why these Work Units may not be admitted at COMMIT (#406).

    The admission requirement of proposal 3.3: a Work Unit reaches
    CYCLE_EXECUTION either with authored acceptance cases or with an explicit
    declared gap. Called on the RAW caller-supplied records, before
    `_normalize_unit` states a position on their behalf.

    FORGEABILITY, stated plainly. The caller-supplied unit list is untrusted
    input and this function is not a wall against a determined author:

    - An author who omits the `acceptance_cases` key entirely lands on
      `TEST_FIRST_ABSENT` and is admitted with an implicit gap. That is not
      closable while migration is a requirement, because "no opinion" is
      exactly the shape a pre-#406 sealed manifest has and refusing it bricks
      in-flight Cycles. What is closed is SILENCE: the gap is recorded on the
      unit, on the Cycle state, on every dispatch payload, and on the DoD
      result, so a Cycle running without test-first is visibly running without
      it rather than indistinguishable from one that is not.
    - An author can declare five trivially-true cases and clear its own gate
      later. Nothing structural stops that either, and the statement
      requirement above only makes it cost a sentence. What the sealed
      manifest DOES buy is that the trivial cases are the ones on the record
      at COMMIT, before the producer ran, so a reviewer reads them against the
      acceptance criteria rather than against the diff that satisfied them.
      Adversarial case authorship is #408's subject, not this function's.

    What IS refused here is the case a review cannot catch by reading: a unit
    that states a position (`acceptance_cases: []`) and declares nothing.
    """
    problems: List[str] = []
    for index, unit in enumerate(units or []):
        if not isinstance(unit, dict):
            # A problem string, not a `continue`. Skipping let a non-dict entry
            # escape the case requirement entirely and then raise
            # `AttributeError` deeper in `_normalize_unit`, which is a worse
            # message from a worse place -- in a function whose entire contract
            # is "say why these units may not be admitted".
            problems.append(
                "work unit at position %d is a %s, not an object; it can "
                "declare neither an id nor acceptance cases"
                % (index, type(unit).__name__)
            )
            continue
        problems.extend(unit_case_problems(unit))
        if classify_unit(unit):
            continue
        problems.append(
            "work unit %r declares `acceptance_cases: []` and no "
            "`criteria_gap_declared`. SP-WRK-007 requires the verifying stage "
            "to verify against criteria declared BEFORE any producing stage "
            "ran, so the authored set is admitted at COMMIT or its absence is "
            "declared. Author the cases, or record why they do not exist with "
            "`criteria_gap_declared: {\"reason\": \"...\"}`."
            % (unit.get("id") or "<unnamed>")
        )
    return problems


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
        # #406 -- SHAPE only. Whether the artifact must be PRESENT is an
        # admission question (`admission_problems`), asked once at COMMIT
        # against the caller's raw records; asking it here would re-ask it of
        # every already-sealed manifest on every hydration and refuse the ones
        # sealed before the field existed.
        problems.extend(unit_case_problems(unit))
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
