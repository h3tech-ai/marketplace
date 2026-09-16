"""Where SPQ keeps its Cycles, and the one identity that names them (#644).

One identity, not three. The predecessor resolved a `cycle_id`, a
`workstream_id` and a `runner_id`, plus a parallel `cc-` namespace for the
Coordination Cycle -- five id shapes for a lifecycle the method describes with
one noun. `SPD-194` retires the Workstream and the Coordination Cycle, so what
remains is the Cycle.

THE BOARD MOVED UP ONE LEVEL, and that is the whole storage change:

    before  spq/cycles/<cycle-id>/workstreams/<ws>/execution-state.json
    after   spq/cycles/<cycle-id>/execution-state.json

The lane directory existed because N clones each held a partial board and
something downstream had to gather them. With one Engineering Lead and one Crew
per Cycle (`SC-MTH-012`) there is one board, and the level that held the parts
has nothing to hold.

WHAT WENT WITH IT, listed because a reader arriving from an older prompt or an
older runbook needs to find out what happened rather than that a name is
unknown:

    workstream_root / projection_path      a lane's slice of the board
    tracker_data_path(.., workstream_id)   per-lane tracker cache
    committed_sync_path                    the per-lane readiness record; the
                                           barrier is at Checkpoint now and
                                           evaluates the admitted set, not a
                                           quorum of lane records
    workstream_branch                      `cycle/<id>/ws/<ws>`; a Cycle has
                                           one integration ref
    pin_path / read_pin / write_pin        the lane pin. A checkout no longer
                                           needs to know which lane it is,
                                           because there is one
    the whole `cc-` namespace              index, pin, root, manifest, ledger,
                                           receipts, branch, discovery and
                                           resolution -- 15 functions

`runner_id` survives, because it is not a lane: it names WHICH MACHINE is
executing, which runtime federation needs and the method says nothing about.

WHY `SYNAPTORY_ACTIVE_SPEC` IS STILL NAMED HERE. It is the Multi-Spec env var,
and SPQ deliberately ignores it. The constant exists so a grep for the name
finds the comment saying so, rather than finding nothing and leaving the reader
to conclude it is honoured.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import socket
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import state_store as _store

#: Everything SPQ keeps for a project lives under here.
SPQ_RELDIR = os.path.join(".synaptory", ".orchestrator", "spq")

#: The runtime's own committed transport, named once so a consumer does not
#: rebuild the prefix. `committed_cycle_dir` lives under it, and the barrier
#: exempts it when checking that a candidate stayed inside the declaration:
#: those files are written by the runtime recording the Cycle, not by an
#: agent's work, so requiring every declaration to claim them would be
#: requiring the operator to grant the runtime permission to keep records.
COMMITTED_RELDIR = os.path.join(".synaptory", "cycles")

#: `<seq>-<8 hex>`. The sequence is human-facing and the hash is what makes the
#: id collision-safe: two clones may both open "Cycle 7" (they are different
#: Cycles with the same number), so the number alone cannot identify one.
CYCLE_ID_RE = re.compile(r"^[0-9]+-[0-9a-f]{8}$")

#: `runner_id` names a machine, not a lane. Kept for runtime federation.
RUNNER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

ENV_CYCLE_ID = "SYNAPTORY_CYCLE_ID"
ENV_RUNNER_ID = "SYNAPTORY_RUNNER_ID"

#: Named so a grep finds this comment. SPQ ignores it: it is the Multi-Spec
#: selector, and honouring it here would let a Multi-Spec export silently
#: redirect an SPQ read.
_MULTISPEC_ENV_IGNORED = "SYNAPTORY_ACTIVE_SPEC"


class IdentityError(ValueError):
    """An id this module refuses, or an identity it cannot resolve."""


@dataclass(frozen=True)
class Identity:
    """Which Cycle this checkout is on, and which machine is running it."""

    cycle_id: str = ""
    cycle_seq: int = 0
    runner_id: str = ""

    @property
    def resolved(self) -> bool:
        return bool(self.cycle_id)


def _validate(kind: str, value: str, pattern: "re.Pattern[str]") -> str:
    text = str(value or "").strip()
    if not pattern.match(text):
        raise IdentityError(
            "%s %r is not well formed (expected %s)" % (kind, value, pattern.pattern)
        )
    return text


def valid_cycle_id(value: str) -> str:
    return _validate("cycle id", value, CYCLE_ID_RE)


def valid_runner_id(value: str) -> str:
    return _validate("runner id", value, RUNNER_ID_RE)


def new_cycle_id(seq: int, *, entropy: str = "") -> str:
    """A fresh Cycle id for sequence `seq`.

    The hash is over the sequence, the clock and the host, so two clones opening
    Cycle 7 at the same moment get different ids. It is identity, not secrecy:
    a predictable id would still be a correct id, and using a cryptographic
    source here would only make the value unreproducible in a test.
    """
    if int(seq) < 0:
        raise IdentityError("a Cycle sequence is not negative")
    seed = entropy or "%s|%s|%s" % (seq, time.time_ns(), socket.gethostname())
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]
    return "%d-%s" % (int(seq), digest)


def seq_of(cycle_id: str) -> int:
    """The sequence a Cycle id carries.

    Used for receipt ids -- `receipt_validator` enforces `^[A-Z][A-Z0-9]*-\\d+$`,
    so `CYCLE-7` is a legal task id and `CYCLE-7-9f2c41ab` is not.
    """
    return int(valid_cycle_id(cycle_id).split("-", 1)[0])


def default_runner_id() -> str:
    """This machine, in a form an id pattern accepts."""
    raw = (socket.gethostname() or platform.node() or "runner").lower()
    cleaned = re.sub(r"[^a-z0-9._-]", "-", raw).strip("-._") or "runner"
    return cleaned[:64]


# ─── Paths ───────────────────────────────────────────────────────────────────


def spq_root(project_dir: str) -> str:
    return os.path.join(str(project_dir), SPQ_RELDIR)


def index_path(project_dir: str) -> str:
    return os.path.join(spq_root(project_dir), "index.json")


def cycle_root(project_dir: str, cycle_id: str) -> str:
    return os.path.join(spq_root(project_dir), "cycles", valid_cycle_id(cycle_id))


def manifest_path(project_dir: str, cycle_id: str) -> str:
    """The sealed declaration. Written `0o444`: see `cycle_records.seal`."""
    return os.path.join(cycle_root(project_dir, cycle_id), "manifest.json")


def ledger_path(project_dir: str, cycle_id: str) -> str:
    """The dependency-ledger cache. Rebuildable from git, so never authority."""
    return os.path.join(cycle_root(project_dir, cycle_id), "dependency-ledger.json")


def events_path(project_dir: str, cycle_id: str) -> str:
    """The method-event ledger: Commit, Sync and Checkpoint, append-only."""
    return os.path.join(cycle_root(project_dir, cycle_id), "method-events.json")


def execution_state_path(project_dir: str, cycle_id: str) -> str:
    """THE BOARD. One per Cycle, not one per lane.

    `pipeline-state.json` is a mode + identity pointer and deliberately not
    this: keeping the lifecycle fields in both would make one a stale copy of
    the other, and then which one wins is a coin toss.
    """
    return os.path.join(cycle_root(project_dir, cycle_id), "execution-state.json")


def tracker_data_path(project_dir: str, cycle_id: str) -> str:
    return os.path.join(cycle_root(project_dir, cycle_id), "tracker-data.json")


def receipts_dir(project_dir: str, cycle_id: str) -> str:
    """Where this Cycle's receipts live.

    One home, where there were three (per-lane, Cycle-level, and the
    Coordination Cycle's). `core/lib/receipt-paths.sh` mirrors this for the
    hooks and exists because the glob was once duplicated in six places with
    the SPQ layout missing from every copy.
    """
    return os.path.join(cycle_root(project_dir, cycle_id), "receipts")


def lock_for(path: str) -> str:
    return str(path) + ".lock"


# ─── Committed transport ─────────────────────────────────────────────────────
#
# The gitignored tree above is local. These paths are COMMITTED, because they
# are how one clone tells another what a Cycle admitted -- and `open_cycle`
# refuses when they are gitignored, since a manifest that writes cleanly and is
# invisible to every other clone is the worst of the available failures.


def committed_cycle_dir(project_dir: str, cycle_id: str) -> str:
    return os.path.join(
        str(project_dir), ".synaptory", "cycles", valid_cycle_id(cycle_id)
    )


def committed_manifest_path(project_dir: str, cycle_id: str) -> str:
    return os.path.join(committed_cycle_dir(project_dir, cycle_id), "manifest.json")


def committed_events_dir(project_dir: str, cycle_id: str) -> str:
    """Dependency events, committed so another clone can verify them.

    One directory per Cycle. The predecessor split it per lane, which only
    mattered because a lane was the thing publishing.
    """
    return os.path.join(committed_cycle_dir(project_dir, cycle_id), "events")


def committed_cuts_path(project_dir: str, cycle_id: str) -> str:
    """The Cycle's append-only cut record, committed rather than gitignored.

    COMMITTED, AND NOT IN THE DECLARATION. Both halves are the design.

    A cut cannot live in the declaration: that document is hash-sealed at
    Commit and `path_scope` is immutable after it, so writing a cut into it
    would either break the seal or require resealing -- and a resealable
    commitment is not a commitment. `SC-MTH-009` wants the original admitted
    set AND the cut history to stay immutable, which is two records, not one
    edited twice.

    It must be committed because the barrier's effective set is *admitted minus
    recorded cuts*, and the barrier can run in a clone that did not make the
    cut. A gitignored cut record would let a clone compute an effective set
    that still contains work another clone withdrew -- which is the whole
    admitted set disagreeing across checkouts, the failure `#507` is about one
    file over.
    """
    return os.path.join(committed_cycle_dir(project_dir, cycle_id), "cuts.json")


def committed_barrier_path(project_dir: str, cycle_id: str) -> str:
    """The Cycle's append-only barrier ledger: verdicts, promotions, closes.

    COMMITTED, for the same reason the cut record is. `cycle_barrier.promote`
    and `close` are idempotent under an operation identity, and that only works
    if the record of the earlier attempt survives the crash the idempotence
    exists for. A gitignored ledger would let a retry in another clone promote
    a candidate that was already merged.

    AND IT IS WHY `close_cycle` CAN STOP TAKING A VERDICT. With no durable
    ledger there was nowhere for a promotion record to live, so the close had
    to be handed one -- which made the caller the source of the verdict it was
    supposed to be recording.
    """
    return os.path.join(committed_cycle_dir(project_dir, cycle_id), "barrier.json")


def integration_branch(cycle_id: str) -> str:
    """The Cycle's one integration ref.

    `cycle/<cycle-id>/ws/<ws>` is gone with the lane. What a Cycle has is this,
    and a trunk it integrates into at Checkpoint (`SC-MTH-007`).
    """
    return "cycle/%s/integration" % valid_cycle_id(cycle_id)


# ─── Identity resolution ─────────────────────────────────────────────────────


def resolve_identity(
    project_dir: str,
    *,
    cycle_id: Optional[str] = None,
    state: Optional[Dict[str, Any]] = None,
    require_cycle: bool = False,
) -> Identity:
    """Which Cycle to act on: the flag, the environment, then the pointer.

    Precedence is explicit-beats-ambient, and there is no fourth source. The
    predecessor had a lane pin ranked ABOVE the environment specifically so an
    ambient export could not silently re-attribute a clone's receipts -- that
    hazard leaves with the lane, because a checkout no longer has a lane to be
    wrong about.

    Never defaults a Cycle. An unresolved identity is an answer
    (`resolved=False`), and a caller that needs one says `require_cycle=True`
    and gets a refusal naming what to pass.
    """
    resolved = ""
    for candidate in (
        cycle_id,
        os.environ.get(ENV_CYCLE_ID),
        ((state or {}).get("spq") or {}).get("cycle_id") if state else None,
        (state or {}).get("_cycle_id") if state else None,
    ):
        text = str(candidate or "").strip()
        if text:
            resolved = valid_cycle_id(text)
            break
    if not resolved:
        resolved = _index_current(project_dir) or ""

    if require_cycle and not resolved:
        raise IdentityError(
            "no Cycle resolved. Pass --cycle-id, set %s, or open a Cycle "
            "first. There is no default: N Engineering Leads means N "
            "concurrent Cycles (`SC-MTH-012`), so a default would pick one of "
            "them arbitrarily." % ENV_CYCLE_ID
        )

    runner = str(os.environ.get(ENV_RUNNER_ID) or "").strip()
    return Identity(
        cycle_id=resolved,
        cycle_seq=seq_of(resolved) if resolved else 0,
        runner_id=valid_runner_id(runner) if runner else default_runner_id(),
    )


# ─── The Cycle index ─────────────────────────────────────────────────────────


def _index_current(project_dir: str) -> Optional[str]:
    index = read_index(project_dir)
    current = str(index.get("current_cycle_id") or "").strip()
    return current or None


def read_index(project_dir: str) -> Dict[str, Any]:
    return _store.read_json(index_path(project_dir))


def record_cycle(
    project_dir: str, cycle_id: str, *, seq: int, goal: str = ""
) -> Dict[str, Any]:
    """Add a Cycle to the index and make it the current one for this checkout.

    `current_cycle_id` is a CONVENIENCE for a single-Cycle checkout, and never
    authority: every operation takes the Cycle it acts on, and a caller that
    relies on this to pick among several concurrent Cycles has picked
    arbitrarily. Stated here because the predecessor's one global pointer is
    exactly what made N concurrent Cycles unrepresentable.
    """
    valid = valid_cycle_id(cycle_id)
    path = index_path(project_dir)
    with _store.transaction(lock_for(path)):
        index = _store.read_json(path)
        cycles = index.get("cycles")
        if not isinstance(cycles, list):
            cycles = []
        if not any(str(c.get("cycle_id")) == valid for c in cycles):
            cycles.append({"cycle_id": valid, "cycle_seq": int(seq), "goal": goal})
        index["cycles"] = cycles
        index["cycle_seq_high"] = max(
            int(index.get("cycle_seq_high") or 0), int(seq)
        )
        index["current_cycle_id"] = valid
        _store.write_json_atomic(path, index)
        return dict(index)


def list_cycles(project_dir: str) -> List[Dict[str, Any]]:
    cycles = read_index(project_dir).get("cycles")
    return list(cycles) if isinstance(cycles, list) else []


def next_seq(project_dir: str) -> int:
    return int(read_index(project_dir).get("cycle_seq_high") or 0) + 1
