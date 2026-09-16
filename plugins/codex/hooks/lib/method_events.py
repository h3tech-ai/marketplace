"""Commit, Sync and Checkpoint: recorded, and blocking nothing (#644).

`C-02` is the hardest thing in the replacement to get right, because the
predecessor got it wrong in the most reasonable-looking way. It made all three
events **lifecycle states**, with `SYNC` sitting between execution and
Checkpoint as a mandatory one and the `CYCLE_EXECUTION -> CHECKPOINT` edge
deliberately absent. Reading Checkpoint as a gate is, in the method's own words,
*"the most common way to get SPQ wrong, because it invites a second approval
path beside the one that actually authorises release."*

So this module records and refuses to decide. It has no transition function, it
is imported by nothing that gates, and the one property every test here defends
is that **appending an event changes no outcome**:

    a Commit event does not open a Cycle -- the sealed declaration does
    a Sync event does not unblock a Work Unit -- a satisfied dependency does
    a Checkpoint event does not close a Cycle -- the barrier does

THE ONE THAT SURPRISES PEOPLE, and the largest behavioural change in the epic:
**the absence of a Sync event blocks nothing.** In the predecessor, `SYNC` was
the human gate -- `next_action` returned `await_sync` and the loop engine was
forbidden to drive past it. Here, a real unresolved dependency blocks the
affected Work Unit and nothing else does. §6: *"Sync is not a ceremony with a
slot; it is the answer to a dependency."* An implementation that made Sync
optional-but-still-a-state would keep the second approval path and lose only the
ordering, which is the half that mattered least.

WHY `M-03`'S READING IS NOT A LOOPHOLE. §7.3 requires a mandatory integration
barrier while `C-02` forbids an event gate, and those look contradictory until
you separate the two things a close does. The barrier is a **mechanical
transition condition** -- it evaluates published criteria and fails closed. The
Checkpoint event **records** that the close happened, with what it integrated.
The test for whether an implementation kept them apart is the one this module's
docstring can be checked against: if deleting the event record would change
whether a Cycle may close, the event has become a gate.

APPEND-ONLY, AND WHY THE ORDER IS NOT AN INDEX. Events carry `recorded_at` and
sit in a list, but nothing reads them positionally: a Cycle's second Sync is not
"index 1", it is the Sync naming a particular dependency. Positional identity is
how a replayed or re-ingested event silently becomes a different one.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

SCHEMA_VERSION = "1"

#: The three method events, and nothing else. A fourth would be a stage in
#: disguise -- the method names three and §1.1 says a term not listed there is
#: not part of the method.
EVENT_KINDS = ("commit", "sync", "checkpoint")

#: What each event must carry to be worth recording. Deliberately small: an
#: event that required a decision outcome would be a gate with extra steps.
REQUIRED_FIELDS: Dict[str, tuple] = {
    # The set this Commit fixed, by identity, and the declaration that holds it.
    "commit": ("cycle_id", "declaration_hash", "admitted_unit_ids"),
    # Which Cycle waited on what, from which Cycle, and how it cleared. A Sync
    # that named no dependency would be a ceremony, which §6 says it is not.
    "sync": ("cycle_id", "waiting_unit_id", "producing_cycle_id", "resolution"),
    # What was demonstrated, and the trunk revision it integrated to. The
    # integration FACT lives here; whether it was permitted is the barrier's.
    "checkpoint": ("cycle_id", "integrated_sha", "trunk_ref"),
}

#: How a Sync was resolved. Closed, because "it cleared somehow" is not a record
#: anyone can act on later.
SYNC_RESOLUTIONS = ("published", "cut", "co-admitted", "waived")


class MethodEventError(ValueError):
    """An event this module refuses to record."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build(kind: str, **fields: Any) -> Dict[str, Any]:
    """One event record, or a refusal.

    Returns the record; it does not append. The caller owns where events live,
    which keeps this module free of a state file and therefore free of any way
    to affect one.
    """
    if kind not in EVENT_KINDS:
        raise MethodEventError(
            "%r is not a method event. The three are %s, and a fourth would be "
            "a stage in disguise -- §1.1: a term not listed in the method's "
            "vocabulary is not part of the method."
            % (kind, ", ".join(EVENT_KINDS))
        )
    missing = [f for f in REQUIRED_FIELDS[kind] if not fields.get(f)]
    if missing:
        raise MethodEventError(
            "a %s event needs %s; missing %s. An event that records less than "
            "this is not evidence of anything, and the point of the three is "
            "that they leave a record rather than a decision."
            % (kind, ", ".join(REQUIRED_FIELDS[kind]), ", ".join(missing))
        )
    if kind == "sync":
        resolution = str(fields.get("resolution"))
        if resolution not in SYNC_RESOLUTIONS:
            raise MethodEventError(
                "sync resolution %r is not one of %s. `it cleared somehow` is "
                "not a record anyone can act on later."
                % (resolution, ", ".join(SYNC_RESOLUTIONS))
            )
    record = {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "recorded_at": _now(),
    }
    record.update({k: v for k, v in fields.items() if v is not None})
    record["event_digest"] = _digest(record)
    return record


def _digest(record: Mapping[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k != "event_digest"}
    return "sha256:" + hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def append(ledger: Sequence[Mapping[str, Any]], record: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Add an event. Append-only: nothing here can edit or remove one.

    A duplicate digest is refused rather than deduplicated, because two
    identical events are either a replay or a caller that lost track of what it
    already recorded, and both are worth surfacing.
    """
    if not record.get("event_digest"):
        raise MethodEventError("an event must carry its digest before it is appended")
    seen = {str(e.get("event_digest")) for e in ledger}
    if str(record["event_digest"]) in seen:
        raise MethodEventError(
            "this event is already in the ledger. A duplicate is a replay or a "
            "caller that lost track of what it recorded, and deduplicating it "
            "silently would hide both."
        )
    return list(ledger) + [dict(record)]


def of_kind(ledger: Sequence[Mapping[str, Any]], kind: str, cycle_id: str = "") -> List[Dict[str, Any]]:
    """Every event of one kind, optionally for one Cycle.

    Filtered by identity rather than by position: a Cycle's second Sync is the
    one naming a particular dependency, not "index 1", and positional identity
    is how a replayed event silently becomes a different one.
    """
    if kind not in EVENT_KINDS:
        raise MethodEventError("%r is not a method event" % kind)
    return [
        dict(e) for e in ledger
        if str(e.get("kind")) == kind
        and (not cycle_id or str(e.get("cycle_id")) == cycle_id)
    ]


def latest(ledger: Sequence[Mapping[str, Any]], kind: str, cycle_id: str = "") -> Optional[Dict[str, Any]]:
    """The most recently recorded event of a kind, or None.

    `None` is a real answer and callers must treat it as one: a Cycle with no
    Sync event is the common case, not a broken one.
    """
    matching = of_kind(ledger, kind, cycle_id)
    if not matching:
        return None
    return sorted(matching, key=lambda e: str(e.get("recorded_at")))[-1]


def sync_count(ledger: Sequence[Mapping[str, Any]], cycle_id: str = "") -> int:
    """How many times this Cycle coordinated. Zero is correct and common.

    Exposed as a count because `SC-MTH-015`'s measurement wants it, and stated
    here because a reader who finds zero must not read it as a missing step:
    §1.1 -- Sync "happens as needed, several times or not at all".
    """
    return len(of_kind(ledger, "sync", cycle_id))
