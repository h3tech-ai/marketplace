#!/usr/bin/env python3
# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
"""Record and read the intake fact where its own subject cannot rewrite it.

WHY A SECOND MODULE AND NOT A BRANCH IN `external_intake`. That module computes
a digest over bytes it read and writes a record beside the receipts. Everything
it does is correct and none of it is AUTHORITATIVE, because the record is a file
in the project and the principal who admits a candidate is the principal who
checks it. `O_EXCL` refuses a second admission and `0444` stops an accident;
neither binds the owner of the file. #495 was reopened twice over exactly that.

The fix is not a stronger local file. Nothing on the developer's machine is
secret from the developer, which is the same wall #435 hit on the gate-event
channel, so the intake identity has to live somewhere the graded principal
cannot reach. Epic #640 already built that place: `authority_decisions` is
append-only, has no update route in the router, derives the principal from the
authenticated session and the role from the action, and refuses a decision
whose `produced_by` equals the deciding principal. `admit-work-unit` is already
one of its actions. What was missing was the wire, and this module plus
`cycles decisions` is it (`docs/proposals/authoritative-external-intake.md`,
option A).

## The three answers, and why silence is one of them

`record` and `read` each return one of:

- `recorded`      -- the control plane holds the fact. It is authoritative.
- `not_connected` -- this tree addresses no control plane, so there is no
                     out-of-reach store to hold anything. The local record
                     stands, WITH its declared limit, and the verdict says so.
- `problem`       -- connected, and the store could not be reached or read.
                     Nothing was learned, and nothing may be credited.

The middle one is the one that needs defending, and it is not a loophole. The
Definition of Done must keep working offline: proposal section 3.3 makes local
delivery state canonical, and #659 Track D already declared this exact trade a
PERMANENT design limit rather than owed work, for `spq_manifest.prove`'s
`expect_hash`. Requiring connectivity to evaluate a DoD gate is the thing that
rule refuses. So an offline project keeps today's behaviour and the record says
which of the two worlds it was admitted in.

`problem` is the one that is easy to get wrong in the other direction, and the
words are `manifest_emitter.read_cycle_authority`'s because the reasoning is
identical: a connected project whose authority is unreachable has LESS evidence
than an offline one, not more, and treating unreachable as unconfigured is how a
connected project silently acquires the offline guarantee.

## Who decides that a project is offline

Not the project. That was #507's finding and it took two wrong cuts to land:
asking whenever a CLI resolved sent every offline checkout to the network, and
reading a marker directory inside the project handed the graded principal a way
to self-declare offline by deleting it.

So the CLI answers it, from its own stamp and its own session, neither of which
is in the project. `_resolve_cli() is None` is NOT read as offline here, and
that distinction is load-bearing: that single `None` also covers a stamped tree
with no candidate installed, one whose candidates fail the identity probe, a
rejected `SYNAPTORY_CLI_BIN`, and any exception on the way (#396). Only an
explicit no-stamp answer takes the offline path.

Python 3.9 compatible: this file is projected into every host package.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Dict, List, Optional, Tuple

#: The action `admit-work-unit` is owned by `engineering-lead` in
#: `cycle_authority.ACTION_OWNER`, and the SERVER re-derives that rather than
#: reading it from us. Named here only so the wire value has one definition.
ADMIT_ACTION = "admit-work-unit"

#: `cycle_authority.OUTCOMES` has no `pass`, deliberately. An admission that
#: proceeded is `continue`.
ADMIT_OUTCOME = "continue"

RECORDED = "recorded"
NOT_CONNECTED = "not_connected"
PROBLEM = "problem"

#: Longer than the emitters' 20s and shorter than the region registry's 45s.
#: An intake is on the critical path of a ceremony that cannot proceed without
#: it, so waiting beats guessing; it is not competing with another clone for a
#: lock, so it does not need the reservation ceiling.
_TIMEOUT_S = 30.0

#: TEST AND CI SEAM, and the same one-purpose rule as
#: `region_registry._CLI_ENV`: this selects WHICH BINARY answers, never WHAT it
#: answers. A recorded fact still has to come back from the store, and a
#: deployment pointing this at a yes-man would be doing what `SYNAPTORY_CLI_BIN`
#: already permits and documents.
_CLI_ENV = "SYNAPTORY_INTAKE_AUTHORITY_BIN"


def _channel_stamp() -> Tuple[str, str]:
    """`(state, whence)`: `stamped`, `unstamped`, or `unreadable`.

    Delegated to `manifest_emitter` rather than re-derived. Two copies of
    "which control plane is this tree stamped for" drift into two definitions,
    and this one has to agree with the module that already refuses a revision
    downgrade on the same signal.
    """
    try:
        import manifest_emitter

        return manifest_emitter._channel_stamp()
    except Exception as exc:  # noqa: BLE001 - an unaskable question is unknown
        return "unreadable", "the control-plane stamp could not be read (%s)" % exc


def _resolve_cli() -> Optional[str]:
    explicit = os.environ.get(_CLI_ENV)
    if explicit and os.path.exists(explicit):
        return explicit
    try:
        import gate_emitter

        return gate_emitter._resolve_cli()
    except Exception:  # noqa: BLE001
        return None


def _verdict(
    state: str, detail: str, **extra: Any
) -> Dict[str, Any]:
    out: Dict[str, Any] = {"state": state, "detail": detail, "authority": "none"}
    if state == RECORDED:
        out["authority"] = "control-plane"
    elif state == PROBLEM:
        out["authority"] = "control-plane"
    out.update(extra)
    return out


def _gate() -> Optional[Dict[str, Any]]:
    """The refusal to return before any subprocess runs, or None to proceed.

    Folded into one place because `record` and `read` ask the identical
    question and answering it twice invites the two to disagree about what
    "offline" means -- which is the defect this module's docstring is about.
    """
    state, whence = _channel_stamp()
    if state == "unreadable":
        return _verdict(
            PROBLEM,
            "this install cannot report which control plane it belongs to "
            "(%s), so whether an authoritative intake fact exists cannot be "
            "established. Repair the install (reinstall the plugin, or check "
            "that `hooks/lib/cp-url` is readable)." % (whence or "unknown cause"),
        )
    cli = _resolve_cli()
    if cli is None:
        if state != "stamped":
            return _verdict(
                NOT_CONNECTED,
                "this tree addresses no control plane, so there is no store "
                "outside the admitting principal's reach to hold the intake "
                "fact. The local record stands with its declared limit.",
            )
        return _verdict(
            PROBLEM,
            "this tree is stamped for a control plane (%s) and no usable CLI "
            "resolves, so the authoritative intake fact can neither be "
            "recorded nor read. Install or repair the CLI for this channel "
            "(`synaptory status` reports which one a binary is), or unset "
            "`SYNAPTORY_CLI_BIN` if it names a binary for a different control "
            "plane." % (whence or "unknown source"),
        )
    return None


def _run(
    cli: str, args: List[str], project_dir: Optional[str]
) -> Tuple[int, Optional[Dict[str, Any]], str]:
    """`(exit, payload, tail)` where payload carries `connected` or is None.

    THE ANSWER MUST BE SELF-IDENTIFYING. A CLI predating `cycles decisions`
    can print cobra output and exit in ways an exit code cannot distinguish
    from an answer, so a payload without `connected` is NO ANSWER rather than a
    negative one. Same contract as `telemetry cycle-authority`.
    """
    argv = [cli, "cycles", "decisions", *args]
    options: Dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "timeout": _TIMEOUT_S,
    }
    if project_dir:
        options["cwd"] = str(project_dir)
    try:
        done = subprocess.run(argv, **options)
    except Exception as exc:  # noqa: BLE001
        return 1, None, str(exc)
    payload = None
    for line in reversed((done.stdout or "").strip().splitlines()):
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict) and "connected" in candidate:
            payload = candidate
            break
    tail = (done.stderr or done.stdout or "").strip().splitlines()
    return done.returncode, payload, (tail[-1] if tail else "")


def _cli_says_disconnected(payload: Dict[str, Any]) -> bool:
    """The CLI's own answer that this project addresses no control plane.

    THE CLI IS THE AUTHORITY ON CONNECTEDNESS, not the stamp and not this
    module (#507). A tree can carry a stamp because its committed form IS the
    install while the project it is run against reports nowhere, and only the
    binary's own session can tell those apart. `telemetry cycle-authority`
    already answers this way and `read_cycle_authority` already honours it;
    not honouring it here would refuse a project the rest of the runtime
    treats as offline, which is a disagreement about what "connected" means
    rather than a stricter check.
    """
    return payload.get("connected") is False


def _skew_problem(tail: str) -> Dict[str, Any]:
    return _verdict(
        PROBLEM,
        "the CLI for this control plane did not answer in a shape this "
        "runtime recognises (%s), so the authoritative intake fact could not "
        "be reached. A stamped project still HAS a store out of the admitting "
        "principal's reach, so version skew is a reason the fact cannot be "
        "read and not evidence that there is nothing to read. Update the CLI."
        % (tail or "no parseable answer"),
    )


def record_intake(
    project_dir: Optional[str],
    unit_id: str,
    candidate_digest: str,
    *,
    produced_by: str = "",
    rationale: str = "",
) -> Dict[str, Any]:
    """Record `unit_id`'s admitted candidate digest as an authority decision.

    Never raises. The caller decides what a verdict costs, because admission
    and a later sign-off cost different things: one refuses to admit, the other
    refuses to credit.

    `produced_by` names the party that produced the artifact, which for a
    verify-only job is outside this platform. It is passed through unverified
    and the control plane uses it for one thing this module cannot do locally:
    refusing a decision whose producer IS the deciding principal.
    """
    if not unit_id or not candidate_digest:
        return _verdict(
            PROBLEM,
            "an intake fact needs both a unit id and a digest computed over "
            "bytes the platform read; refusing to record a partial one",
        )
    refusal = _gate()
    if refusal is not None:
        return refusal
    cli = _resolve_cli()
    assert cli is not None  # _gate returned None, so a CLI resolved
    args = [
        "record",
        "--action", ADMIT_ACTION,
        "--outcome", ADMIT_OUTCOME,
        "--subject", str(unit_id),
        "--subject-digest", str(candidate_digest),
    ]
    if produced_by:
        args += ["--produced-by", str(produced_by)]
    if rationale:
        args += ["--rationale", str(rationale)]
    code, payload, tail = _run(cli, args, project_dir)
    if payload is None:
        return _skew_problem(tail)
    if _cli_says_disconnected(payload):
        return _verdict(
            NOT_CONNECTED,
            "the CLI reports that this project addresses no control plane, so "
            "there is no store outside the admitting principal's reach to hold "
            "the intake fact. The local record stands with its declared limit.",
        )
    if code == 0 and payload.get("ok"):
        row = payload.get("decision") or {}
        return _verdict(
            RECORDED,
            "the control plane recorded the intake fact for %s" % unit_id,
            decision_id=str(row.get("id") or ""),
            subject_digest=str(row.get("subject_digest") or ""),
            principal=str(row.get("principal") or ""),
        )
    return _verdict(
        PROBLEM,
        "the control plane refused or could not record the intake fact: %s"
        % (payload.get("detail") or payload.get("reason") or tail or "exit %d" % code),
    )


def read_intake_fact(
    project_dir: Optional[str], unit_id: str
) -> Dict[str, Any]:
    """The digest the control plane records as admitted for `unit_id`.

    On `recorded`, `subject_digest` is the authoritative identity and a local
    record disagreeing with it is the local record being wrong.

    MOST RECENT WINS, and only among `admit-work-unit` rows for this subject.
    The store is append-only and a correction is a linked supersession, so the
    latest row is the current decision; filtering by action matters because
    the same subject also accumulates `accept-work-unit` and `cut-work-unit`
    rows whose `subject_digest` means something else.
    """
    refusal = _gate()
    if refusal is not None:
        return refusal
    cli = _resolve_cli()
    assert cli is not None
    code, payload, tail = _run(
        cli, ["list", "--subject", str(unit_id)], project_dir
    )
    if payload is None:
        return _skew_problem(tail)
    if _cli_says_disconnected(payload):
        return _verdict(
            NOT_CONNECTED,
            "the CLI reports that this project addresses no control plane, so "
            "there is no recorded intake fact to compare the local record "
            "against. The local record stands with its declared limit.",
        )
    if code != 0 or not payload.get("ok"):
        return _verdict(
            PROBLEM,
            "the recorded intake fact could not be read: %s"
            % (payload.get("detail") or payload.get("reason") or tail or "exit %d" % code),
        )
    rows = payload.get("decisions")
    if not isinstance(rows, list):
        return _skew_problem(tail)
    admits = [
        r for r in rows
        if isinstance(r, dict) and str(r.get("action") or "") == ADMIT_ACTION
    ]
    if not admits:
        # CONNECTED AND NOTHING RECORDED IS AN ANSWER, not a problem: this unit
        # was never admitted through an authoritative intake. A caller must not
        # read it as "offline" and fall back to the local record, which is why
        # the state is its own and the authority stays `control-plane`.
        return _verdict(
            RECORDED,
            "the control plane holds no admit-work-unit decision for %s, so "
            "no candidate was authoritatively admitted for it" % unit_id,
            subject_digest="",
        )
    latest = sorted(
        admits, key=lambda r: str(r.get("decided_at") or "")
    )[-1]
    return _verdict(
        RECORDED,
        "the control plane records %s as admitted for %s"
        % (latest.get("subject_digest") or "no digest", unit_id),
        decision_id=str(latest.get("id") or ""),
        subject_digest=str(latest.get("subject_digest") or ""),
        principal=str(latest.get("principal") or ""),
        produced_by=str(latest.get("produced_by") or ""),
    )
