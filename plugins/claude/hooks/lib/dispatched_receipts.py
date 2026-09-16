"""Canonical receipts for the story dispatches that are currently active.

The SubagentStop verify hook used to pick the receipt to VALIDATE by newest
mtime, so a receipt belonging to another story or role could satisfy the
gate (#340; the shipping half of the same mtime race was #287/#290). The
kernel's `execute_dispatch` records `mcp_active_dispatches[role_abbrev]` on
the story it authorizes, which is exactly the (story, role) set a
SubagentStop can be closing. This helper reads that binding and resolves
each active (story, role) through the kernel's own canonical resolver, so
the hook validates the receipt the dispatch contract named instead of
whichever file happens to be newest on disk.

Output contract, one line each, consumed by synaptory-verify-receipt.sh:

    status=bound|scoped|none
    <absolute receipt path>     (existing canonical receipts, newest first)

`status=bound` means at least one story carries an active dispatch. With no
paths it means the dispatch's canonical receipt is not on disk: the hook
treats the receipt as MISSING rather than validating an unrelated file.

`status=scoped` means no dispatch is bound, but the board names a story or a
ceremony this SubagentStop can be closing, so selection is the canonical
receipts for THAT work across its roles. This is the path Claude actually
takes for QE and CR, because its orchestrator calls `begin_dispatch` only
for `dispatch_se` from `queued`, so those stages carry no binding at all.

`status=none` means the board names nothing resolvable, or this helper
failed. The hook then validates NOTHING and says so. It no longer falls back
to newest-by-mtime: #340's acceptance criterion is that no workflow chooses
a receipt by file modification time, and a fallback that reads "whichever
file is newest" reintroduces the original defect on exactly the workflows
that lack a binding (#396). Refusing to select is the honest answer, because
a hook that validates an arbitrary file reports a result about work nobody
asked it about.

Pure logic + filesystem reads. No subprocess, no network. Layer-1 testable.
Python 3.9 compatible: this file is projected into every host package.
"""
from __future__ import annotations

import os
import sys
from typing import List, Optional, Tuple


def _canonical(project_dir: str, story_id: str, abbrev: str) -> Optional[str]:
    """The one path this (story, role) may satisfy, or None.

    `advance_kernel.canonical_receipt_path` is the single resolver the
    advance gate itself uses (spec- and SPQ-aware, refuses symlinks and
    escapes), so selection and consumption cannot disagree. A refusal or
    resolver error yields None: the binding still counts as existing, and
    the hook treats the receipt as missing rather than falling back to
    whichever file is newest.
    """
    try:
        import advance_kernel as ak

        return str(ak.canonical_receipt_path(str(project_dir), story_id, abbrev))
    except Exception:  # noqa: BLE001 - selection must never crash the hook
        return None


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


#: Recorded on the marker when SubagentStart cannot name one assignment. It is
#: deliberately not a story id: selection must be able to tell "no correlation
#: was available" from "the correlation is that several are possible", because
#: the second is the parallel case and must refuse rather than pick.
_AMBIGUOUS = "?ambiguous"

#: The board states the fallback below reads as "in flight", and the role that
#: HOLDS a story in each. Mirrors `advance_kernel.BLOCKED_FROM_ROLE` for the
#: same three states; duplicated rather than imported so this module stays
#: loadable on a partial install, and `test_dispatched_receipts_scope` asserts
#: the two never drift.
#:
#: It is what makes the fallback a fallback for the roles it was written for
#: rather than for every role: a story sitting in `testing` is evidence about
#: a QE dispatch and about nothing else.
_IN_FLIGHT_HOLDER: dict = {
    "in_progress": "se",
    "testing": "qe",
    "reviewing": "cr",
}


def active_dispatch_for(project_dir: str, role: str) -> Tuple[str, str]:
    """The one (story_id, dispatch_id) this role currently holds, or ("", "").

    Called at SubagentStart, where the role is the only thing the host tells
    us, so the correlation has to be established while it is still
    unambiguous. Returns nothing when zero or MORE THAN ONE story holds an
    active dispatch for the role: under parallel execution of the same role
    there is no way to tell from the role alone which one a later
    SubagentStop belongs to, and guessing is what this whole area keeps
    getting wrong.
    """
    abbrev = _abbrev(role)
    if not abbrev:
        return ("", "")
    try:
        import story_pipeline as sp

        state = sp._read_state(str(project_dir))
    except Exception:  # noqa: BLE001
        return ("", "")
    found: List[Tuple[str, str]] = []
    for story in state.get("current_stories") or []:
        if not isinstance(story, dict):
            continue
        dispatches = story.get("mcp_active_dispatches")
        if not isinstance(dispatches, dict):
            continue
        entry = dispatches.get(abbrev)
        if not isinstance(entry, dict):
            continue
        story_id = str(story.get("id") or "")
        if story_id:
            found.append((story_id, str(entry.get("dispatch_id") or "")))
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        # Two stories hold this role at once. The role cannot tell them apart
        # and neither can anything else the host gives SubagentStop, so the
        # ambiguity is RECORDED rather than resolved.
        return (_AMBIGUOUS, "")

    # No binding for this role, which is the ordinary Claude case for QE and
    # CR: its orchestrator calls `begin_dispatch` only for `dispatch_se` from
    # `queued`. Fall back to the board's in-flight work. One story in flight
    # is an unambiguous assignment; two are not, and a scoped selection that
    # accepted either would let an overlapping agent's receipt answer this
    # stop. Zero means ceremony work, which carries its own ids and is left
    # to the receipt's own declaration.
    #
    # ONLY for a role the per-story pipeline actually runs through those
    # states. `in_progress`, `testing` and `reviewing` are held by SE, QE and
    # CR; they say nothing about a Project Owner, Solution Architect or
    # Technical Writer dispatch, which is not on the story pipeline at all.
    # Reading them as evidence about EVERY role made the in-flight COUNT
    # decide a question it has no bearing on: with one story in flight a PO
    # stop was assigned to that story, with two it was called ambiguous and
    # selection then refused, and with none it resolved correctly by the
    # receipt's own declared id. Same agent, same receipt, three different
    # answers chosen by unrelated board state -- and the middle one reported
    # `receipt_missing` and blocked the session with exit 2 over a receipt
    # sitting in the directory the error message named. Sprint planning, which
    # dispatches the PO onto a board of several in-flight stories, hits it
    # every time.
    #
    # An off-pipeline role falls through to the same answer a quiet board
    # already gave it: nothing recorded, resolved by declaration.
    #
    # And a pipeline role reads only the states IT holds. Counting all three
    # was the same defect one level in: the map named a holder per state and
    # then the candidate list used only its keys, so an SE story in
    # `in_progress` made a QE stop on the single story in `testing`
    # `?ambiguous` and selection refused a receipt that was on disk under its
    # canonical name. That board is the ordinary pipeline, not an overlap:
    # SE on one story while QE verifies another is what the pipeline is FOR.
    # Ambiguity is two stories in the SAME state, which is the parallel case
    # the refusal was written for.
    held_states = {
        board_state
        for board_state, holder in _IN_FLIGHT_HOLDER.items()
        if holder == abbrev
    }
    if not held_states:
        return ("", "")
    in_flight = [
        str(story.get("id") or "")
        for story in (state.get("current_stories") or [])
        if isinstance(story, dict)
        and story.get("state") in held_states
        and story.get("id")
    ]
    if len(in_flight) == 1:
        return (in_flight[0], "")
    if len(in_flight) > 1:
        return (_AMBIGUOUS, "")
    return ("", "")


def bound_receipts(
    project_dir: str, role: str = "", story: str = "", dispatch_id: str = ""
) -> Tuple[bool, List[str]]:
    """Return (binding_exists, existing canonical receipt paths, newest first).

    Scoped to the dispatch THIS SubagentStop is closing, not to every active
    dispatch on the board (#396). An earlier revision took only `project_dir`
    and emitted the canonical receipt for every entry of every story's
    `mcp_active_dispatches`, and the CLI returned `status=bound` before the
    role was even read. So on a board with an active SE binding and a QE stop,
    the SE receipt answered for QE: `receipt_missing` never fired, and an
    invalid SE receipt could block a valid QE stop. The role marker existed
    and this path ignored it.

    `binding_exists` is now True only when a binding exists FOR THIS pair,
    whether or not its receipt is on disk yet. A stop whose role holds no
    binding falls through to the scoped path rather than borrowing another
    role's.

    `story` and `dispatch_id` come from the marker when SubagentStart could
    resolve them unambiguously, and they are what disambiguates two
    simultaneous same-role dispatches. With an empty `role` the behaviour is
    the pre-#396 board-wide one, which only legacy markers reach.
    """
    import story_pipeline as sp

    abbrev = _abbrev(role)
    state = sp._read_state(str(project_dir))
    binding_exists = False
    paths: List[str] = []
    for story_rec in state.get("current_stories") or []:
        if not isinstance(story_rec, dict):
            continue
        dispatches = story_rec.get("mcp_active_dispatches")
        if not isinstance(dispatches, dict) or not dispatches:
            continue
        story_id = str(story_rec.get("id") or "")
        if not story_id:
            continue
        if story and story_id != story:
            continue
        for entry_abbrev, entry in dispatches.items():
            if abbrev and str(entry_abbrev) != abbrev:
                continue
            if dispatch_id:
                held = entry.get("dispatch_id") if isinstance(entry, dict) else None
                if str(held or "") != dispatch_id:
                    continue
            binding_exists = True
            path = _canonical(project_dir, story_id, str(entry_abbrev))
            if path and os.path.isfile(path) and path not in paths:
                paths.append(path)
    paths.sort(key=_mtime, reverse=True)
    return binding_exists, paths


#: Full role name to receipt abbreviation. Mirrors `receipt_recovery.ROLE_ABBREV`,
#: which is the map the recovery path already uses.
_ROLE_ABBREV = {
    "software-engineer": "se",
    "quality-engineer": "qe",
    "code-reviewer": "cr",
    "solution-architect": "sa",
    "project-owner": "po",
    "product-manager": "po",  # legacy alias
    "compliance-engineer": "ce",
    "platform-engineer": "pe",
    "technical-writer": "tw",
    "research-advisor": "ra",
}
_ABBREVS = frozenset(_ROLE_ABBREV.values())


def _abbrev(role: str) -> str:
    """The receipt abbreviation for a role name, an abbreviation, or "".

    Accepts both spellings because the marker records whatever Claude Code
    reported, and a namespaced `synaptory:` prefix is stripped before this.
    """
    token = str(role or "").strip().lower()
    if not token:
        return ""
    if token in _ABBREVS:
        return token
    return _ROLE_ABBREV.get(token, "")


def _iter_receipt_files(project_dir: str):
    """Every receipt JSON under the orchestrator dir, any layout.

    Mirrors `receipt-paths.sh`'s sweep: flat, multi-spec and the three SPQ
    homes. Globbing rather than enumerating known id schemes is the point,
    because the id scheme is the receipt's business, not this module's.
    """
    import glob

    orch = os.path.join(str(project_dir), ".synaptory", ".orchestrator")
    patterns = (
        os.path.join(orch, "receipts", "*.json"),
        os.path.join(orch, "specs", "*", "receipts", "*.json"),
        os.path.join(orch, "spq", "**", "receipts", "*.json"),
    )
    for pattern in patterns:
        for path in glob.glob(pattern, recursive=True):
            if os.path.isfile(path):
                yield path


def _declared(path: str) -> Tuple[str, str]:
    """(story_id, role_abbrev) as the receipt declares them, or ("", "")."""
    import json

    try:
        with open(path, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
    except Exception:  # noqa: BLE001
        return ("", "")
    if not isinstance(doc, dict):
        return ("", "")
    return (str(doc.get("story_id") or ""), _abbrev(str(doc.get("role") or "")))


def scoped_receipts(
    project_dir: str, role: str = "", since: float = 0.0, story: str = ""
) -> List[str]:
    """The receipt(s) this SubagentStop owes, identified rather than guessed.

    Three facts decide it, and none of them is a list of id schemes:

    1. `role`, from the marker SubagentStart wrote. The receipt's own declared
       role must match it. This is what closes #340's "another story OR ROLE".
    2. The receipt must sit at the canonical path for the story IT declares.
       A receipt that names `INCEPTION-PO-1` is checked against the canonical
       path for `INCEPTION-PO-1`, so ceremony, sprint and Work Unit schemes
       all work without this module knowing any of them.
    3. `since`, the moment this subagent started. A receipt written before it
       started cannot be its output. This is a causal filter, not a
       newest-wins heuristic: it excludes, it never ranks.

    An earlier revision inferred the work from the board instead, using a
    hard-coded ceremony list and three story states. That both accepted the
    wrong role and REJECTED valid supported flows: Inception writes
    `INCEPTION-<ROLE>-1` and scrum planning writes `SPRINT-{N}`, neither of
    which the list contained, so the hook reported `receipt_missing` for
    receipts that were sitting right there. Enumerating plausible receipts
    cannot identify the one a finished subagent owes.

    Empty `role` returns nothing: with no identified role there is no pair to
    resolve, and falling back to every role reinstates the substitution.
    """
    abbrev = _abbrev(role)
    if not abbrev:
        return []
    if story == _AMBIGUOUS:
        # Overlapping same-role work. Role, self-declared identity and a time
        # window together still cannot say which story this stop belongs to:
        # a receipt written by the OTHER agent after this one started passes
        # every one of those filters. Selecting nothing is the honest answer,
        # and the one that cannot substitute. The real fix is for the
        # orchestrator to bind the dispatch, which is Epic #339 finding 1.
        return []

    out: List[str] = []
    for path in _iter_receipt_files(project_dir):
        story_id, declared_abbrev = _declared(path)
        if not story_id or declared_abbrev != abbrev:
            continue
        if story and story_id != story:
            # An assignment was recorded, so a receipt naming other work is
            # not this stop's, however recent and however well formed.
            continue
        canonical = _canonical(project_dir, story_id, declared_abbrev)
        if not canonical or os.path.realpath(canonical) != os.path.realpath(path):
            # Declared identity and location disagree, so the file is not the
            # canonical receipt for what it claims to be.
            continue
        if since and _mtime(path) + 1.0 < since:
            continue
        if path not in out:
            out.append(path)

    out.sort(key=_mtime, reverse=True)
    return out


def _cli() -> int:  # pragma: no cover - thin CLI wrapper
    """Entry point for the SubagentStop hook:

        dispatched_receipts.py <project_dir> [<role>] [<started_at_epoch>]
                               [<story>] [<dispatch_id>]
        dispatched_receipts.py --resolve-dispatch <project_dir> <role>

    Always exits 0. On any internal failure it reports `status=none`, so its
    own bugs make the hook validate nothing rather than block a session. That
    is a deliberate trade: a missed validation is visible in the log, while a
    hook that crashes a session is not recoverable by the person hitting it.
    """
    if len(sys.argv) < 2:
        print("status=none")
        return 0
    # `--resolve-dispatch <project_dir> <role>` is the SubagentStart side: it
    # prints "story\tdispatch_id" so the marker can carry the correlation
    # while it is still unambiguous.
    if sys.argv[1] == "--resolve-dispatch":
        if len(sys.argv) < 4:
            print("\t")
            return 0
        try:
            story, dispatch = active_dispatch_for(sys.argv[2], sys.argv[3])
        except Exception:  # noqa: BLE001
            story, dispatch = "", ""
        print("%s\t%s" % (story, dispatch))
        return 0
    role = sys.argv[2] if len(sys.argv) > 2 else ""
    marker_story = sys.argv[4] if len(sys.argv) > 4 else ""
    marker_dispatch = sys.argv[5] if len(sys.argv) > 5 else ""
    try:
        binding_exists, paths = bound_receipts(
            sys.argv[1], role, marker_story, marker_dispatch
        )
    except Exception:  # noqa: BLE001 - selection must never crash the hook
        binding_exists, paths = False, []
    if binding_exists:
        print("status=bound")
        for path in paths:
            print(path)
        return 0
    try:
        since = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
    except ValueError:
        since = 0.0
    try:
        scoped = scoped_receipts(sys.argv[1], role, since, marker_story)
    except Exception:  # noqa: BLE001
        scoped = []
    print("status=scoped" if scoped else "status=none")
    for path in scoped:
        print(path)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
