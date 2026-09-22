"""Per-Cycle throughput and cut-rate, with the §5.4 definitions pinned (#646).

`C-16` requires concurrency to be planned against **recorded history** rather
than against what a team believes it can hold. Neither rate existed, while both
sibling lifecycles compute their equivalent (`kanban_state_machine.
calculate_throughput`, `scrum_state_machine.calculate_velocity`).

THE DEFINITIONS ARE PINNED HERE BECAUSE A METRIC WITH TWO READINGS IS NOT ONE.
§5.4 asks for them to be fixed before any benchmark, so each one is implemented
exactly as it is worded there, and the wording that constrains the code is
quoted at the function that carries it.

THE ONE PROPERTY EVERY FUNCTION HERE DEFENDS: **absent is not zero.** The Pilot
2 report had to correct measurements that were zeros -- 0 work units, 0
dispatches, mean gate depth 0.0 on a real run -- because a reader opened the
mode pointer and found no board. Nothing here ever answers `0` for a question
it could not measure: an incomplete observation reports `available: False` with
the reason, and the two cases a plan must never confuse -- "no history" and "a
history of zero" -- have different shapes.

    a measurement       {"available": True,  "value": 0.0, ...}
    an absence          {"available": False, "value": None, "problems": [...]}

AND THE SECOND PROPERTY: **a technical `done` credits nothing.** §5.4 credits a
Work Unit only when an accountable human accepted it as verified. The filter is
applied at the WRITER -- `cycle_barrier.close` reads acceptance off the unit
results and drops an entry naming the automated principal -- and re-asserted
here, because a record that arrived from somewhere else must not be able to
credit itself.

WHERE THE RECORDS COME FROM. `cycle_barrier.close` returns a `history` block per
Cycle; a consumer reaches the recorded ones through
`pipeline_board.read_board(...).completed_cycles()`, which is the one sanctioned
reader. This module takes the records and never opens a file, so it cannot
acquire the reader defect it exists downstream of.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set

import cycle_authority

SCHEMA_VERSION = "1"

#: What a Cycle history record must carry to be measurable at all. A record
#: missing any of these is reported unavailable rather than measured partially:
#: a rate computed over an incomplete record is a number nobody can reproduce.
HISTORY_REQUIRED_FIELDS = (
    "cycle_id",
    "admitted_unit_ids",
    "admitted_count_at_commit",
    "cut_unit_ids",
    "accepted_units",
)

#: What one acceptance credit must carry. §5.4: "retain the acceptance timestamp
#: and integration revision" -- a credit without them cannot be placed in an
#: observation window or tied to what shipped.
ACCEPTANCE_REQUIRED_FIELDS = ("unit_id", "accepted_by", "accepted_at", "integrated_sha")

#: Units, spelled out, because §5.4 requires them explicit and a bare float is
#: how a per-day rate gets read as a per-Cycle one.
THROUGHPUT_UNIT = "accepted work units"
THROUGHPUT_RATE_UNIT = "accepted work units per day"
CUT_RATE_UNIT = "cut work units per admitted work unit"


class MeasurementError(ValueError):
    """A measurement or a plan this module refuses outright.

    Distinct from an *unavailable* report, and the distinction is the point. An
    unavailable report says "this could not be measured"; this exception says
    "this must not be measured" -- an empty admission, or a plan citing history
    that was never recorded. Returning a number for either would be worse than
    failing.
    """


def _unavailable(problems: Sequence[str], **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"available": False, "value": None, "problems": list(problems)}
    out.update(extra)
    return out


def _parse_ts(raw: object) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def record_problems(record: Mapping[str, Any]) -> List[str]:
    """Why this history record is not measurable. Empty means it is."""
    if not isinstance(record, Mapping):
        return ["the history entry is not a record"]
    found = [
        "the record carries no %s" % field
        for field in HISTORY_REQUIRED_FIELDS
        if record.get(field) is None
    ]
    admitted = record.get("admitted_unit_ids")
    if admitted is not None and not isinstance(admitted, (list, tuple)):
        found.append("admitted_unit_ids is not a list of identities")
    return found


def delivered_count(record: Mapping[str, Any]) -> Optional[int]:
    """How many Work Units this Cycle delivered, or None if it cannot say.

    `None` IS A THIRD ANSWER and the reason this is a function. A record that
    does not carry the field has not told us it delivered nothing -- it has
    told us nothing -- and the caller must not read the absence either way.

    `work_units_done` is written by both producers: `cycle_barrier.close` (from
    the effective set, which a green verdict has already proved complete) and
    the `cycles_completed` archive entry (from the board). A record predating
    `#825` carries neither, and answers `None`.
    """
    raw = record.get("work_units_done") if isinstance(record, Mapping) else None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return None
    return raw


# ─── Accepted throughput ─────────────────────────────────────────────────────


def accepted_throughput(record: Mapping[str, Any]) -> Dict[str, Any]:
    """§5.4: "count distinct verified Work Units credited by accountable human
    acceptance to that Cycle; retain the acceptance timestamp and integration
    revision. Report both count and elapsed-time rate with explicit units.
    Technical `done`, retries and imported historical rows do not create
    duplicate credit."

    DISTINCT is what makes the last sentence hold, and it holds for all three
    cases at once: a retry, a re-import and a second `done` all arrive as
    another entry for a unit id already credited, and a set does not care which
    of the three it was.
    """
    problems = record_problems(record)
    if problems:
        return _unavailable(problems, count=None, credited=[])

    entries = record.get("accepted_units") or []
    if not isinstance(entries, (list, tuple)):
        return _unavailable(["accepted_units is not a list of credits"], count=None, credited=[])

    admitted: Set[str] = {str(u) for u in record.get("admitted_unit_ids") or ()}
    credited: Dict[str, Dict[str, Any]] = {}
    duplicates = 0
    bad: List[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            bad.append("credit %d is not a record" % index)
            continue
        missing = [f for f in ACCEPTANCE_REQUIRED_FIELDS if not str(entry.get(f) or "").strip()]
        if missing:
            bad.append(
                "credit %s is missing %s, so it cannot be placed in an "
                "observation window or tied to what shipped"
                % (entry.get("unit_id") or index, ", ".join(missing))
            )
            continue
        unit_id = str(entry["unit_id"])
        if str(entry["accepted_by"]) == cycle_authority.AUTOMATED_PRINCIPAL:
            bad.append(
                "credit %s names the automated principal as the accepting "
                "human. A change counts as delivered only once an accountable "
                "human has accepted it as verified, so this record is not "
                "trustworthy rather than one credit short" % unit_id
            )
            continue
        if unit_id not in admitted:
            bad.append(
                "credit %s was not admitted to this Cycle, so crediting it here "
                "would attribute delivery to the wrong Cycle" % unit_id
            )
            continue
        if unit_id in credited:
            duplicates += 1
            continue
        credited[unit_id] = {
            "unit_id": unit_id,
            "accepted_by": str(entry["accepted_by"]),
            "accepted_at": str(entry["accepted_at"]),
            "integrated_sha": str(entry["integrated_sha"]),
        }
    if bad:
        # An incomplete observation reports unavailable, never a lower count: a
        # partial number is indistinguishable from a measured one afterwards.
        return _unavailable(bad, count=None, credited=[])

    # `#825`: A CYCLE THAT NEVER RECORDED ACCEPTANCE IS NOT A CYCLE THAT
    # ACCEPTED NOTHING. Both arrive here with an empty `credited`, and this
    # function answered `available: True, value: 0` to both -- the exact shape
    # this module's header forbids, on the one number `SC-MTH-015` nominates as
    # the basis for capacity planning. Two Cycles that delivered thirteen Work
    # Units between them read `0.0 accepted work units per day`, and a project
    # following the method plans its next Cycle against that.
    #
    # THE ZERO IS KEPT WHERE IT IS HONEST. A Cycle that delivered nothing
    # accepted nothing, and that is a measurement. A record that cannot say
    # what it delivered (`delivered_count` is None) is not second-guessed
    # either -- inferring an omission from a missing field would be the same
    # substitution one field over.
    delivered = delivered_count(record)
    if not credited and delivered:
        return _unavailable(
            [
                "no acceptance is recorded for %d delivered Work Unit%s, so "
                "throughput cannot be measured. Acceptance is credited at "
                "Checkpoint, per unit, by an accountable human; until it is "
                "recorded this Cycle has no accepted count -- which is not the "
                "same as an accepted count of zero" % (delivered, "" if delivered == 1 else "s")
            ],
            count=None,
            credited=[],
            work_units_done=delivered,
        )

    return {
        "available": True,
        "count": len(credited),
        "value": len(credited),
        "unit": THROUGHPUT_UNIT,
        "duplicates_dropped": duplicates,
        "credited": [credited[k] for k in sorted(credited)],
        "rate": _throughput_rate(record, len(credited)),
        "problems": [],
    }


def _throughput_rate(record: Mapping[str, Any], count: int) -> Dict[str, Any]:
    """The elapsed-time half, unavailable when the window is not observable.

    Kept separate from the count on purpose: a Cycle whose open time was never
    recorded still has a countable throughput, and reporting the rate as zero
    (or as the count, per an implied one-day window) would invent an
    observation window nobody measured.
    """
    opened = _parse_ts(record.get("opened_at"))
    closed = _parse_ts(record.get("closed_at"))
    if opened is None or closed is None:
        return _unavailable(
            ["the observation window is not recorded (opened_at=%r, closed_at=%r)"
             % (record.get("opened_at"), record.get("closed_at"))],
            unit=THROUGHPUT_RATE_UNIT,
        )
    elapsed_days = (closed - opened).total_seconds() / 86400.0
    if elapsed_days <= 0:
        return _unavailable(
            ["the recorded window is %s days long, which has no rate"
             % round(elapsed_days, 6)],
            unit=THROUGHPUT_RATE_UNIT,
            window={"from": str(record.get("opened_at")), "to": str(record.get("closed_at")),
                    "elapsed_days": elapsed_days},
        )
    return {
        "available": True,
        "value": count / elapsed_days,
        "unit": THROUGHPUT_RATE_UNIT,
        "window": {
            "from": str(record.get("opened_at")),
            "to": str(record.get("closed_at")),
            "elapsed_days": elapsed_days,
        },
        "problems": [],
    }


# ─── Cut-rate ────────────────────────────────────────────────────────────────


def cut_rate(record: Mapping[str, Any]) -> Dict[str, Any]:
    """§5.4: "distinct Work Units explicitly cut divided by the original
    Commit-admitted count. The denominator does not shrink after cuts. Empty
    admission is refused; incomplete observations report unavailable instead of
    zero."

    THE DENOMINATOR IS CHECKED, not trusted. A writer that removed cut units
    from `admitted_unit_ids` would leave a record whose count and list disagree,
    and that is the one way this rate can be made to lie -- a Cycle that cut
    half its scope would report a cut-rate of zero over the half that remained.
    So the recorded Commit count and the recorded identities are compared, and a
    disagreement is unavailable rather than resolved in either direction.
    """
    problems = record_problems(record)
    if problems:
        return _unavailable(problems, numerator=None, denominator=None)

    denominator = record.get("admitted_count_at_commit")
    if not isinstance(denominator, int) or denominator <= 0:
        raise MeasurementError(
            "Cycle %s records %r admitted Work Units at Commit. An empty "
            "admission is refused rather than measured: every cut-rate over "
            "zero admitted units is either undefined or 0/0 reported as 0%%, "
            "and a Cycle that admitted nothing has no delivery to rate."
            % (record.get("cycle_id"), denominator)
        )

    admitted = [str(u) for u in record.get("admitted_unit_ids") or ()]
    distinct_admitted = set(admitted)
    if len(distinct_admitted) != denominator:
        return _unavailable(
            ["the record names %d distinct admitted Work Units but its "
             "Commit-admitted count is %d. The denominator does not shrink "
             "after cuts, so a record where the two disagree has had its "
             "admitted set edited and cannot be rated"
             % (len(distinct_admitted), denominator)],
            numerator=None, denominator=denominator,
        )

    cut = [str(u) for u in record.get("cut_unit_ids") or ()]
    foreign = sorted(set(cut) - distinct_admitted)
    if foreign:
        return _unavailable(
            ["cut unit(s) %s were never admitted to this Cycle, so the "
             "numerator counts work the denominator does not"
             % ", ".join(foreign)],
            numerator=None, denominator=denominator,
        )
    numerator = len(set(cut))
    return {
        "available": True,
        "value": numerator / float(denominator),
        "unit": CUT_RATE_UNIT,
        "numerator": numerator,
        "denominator": denominator,
        "cut_unit_ids": sorted(set(cut)),
        "problems": [],
    }


# ─── Lead time ───────────────────────────────────────────────────────────────


def lead_time(
    record: Mapping[str, Any],
    *,
    specification_approved_at: str,
    specification_revision: str,
) -> Dict[str, Any]:
    """§5.4: "approved specification timestamp to observed trunk integration,
    with specification revision and Cycle references. Record human acceptance
    separately so integration is never silently treated as accepted delivery."

    The two blocks below never merge, and that is the whole requirement. A Cycle
    can be on the trunk and unaccepted -- that is the normal state between the
    merge and the go-live -- and a single number covering both would report the
    unaccepted case as delivered.
    """
    problems = record_problems(record)
    approved = _parse_ts(specification_approved_at)
    integrated = _parse_ts(record.get("integrated_at"))
    if not str(specification_revision or "").strip():
        problems.append(
            "no specification revision is named, so this lead time cannot be "
            "traced to what was approved"
        )
    if approved is None:
        problems.append(
            "the approved specification timestamp is missing or unparseable (%r)"
            % specification_approved_at
        )
    if integrated is None:
        problems.append(
            "no observed trunk integration time is recorded, so the interval "
            "has no end. A close timestamp is not an integration timestamp"
        )
    if problems:
        return _unavailable(
            problems,
            cycle_id=str(record.get("cycle_id") or ""),
            specification_revision=str(specification_revision or ""),
            acceptance=_unavailable(["not measured: the integration half is unavailable"]),
        )
    return {
        "available": True,
        "value": (integrated - approved).total_seconds() / 86400.0,
        "unit": "days from approved specification to observed trunk integration",
        "cycle_id": str(record.get("cycle_id") or ""),
        "specification_revision": str(specification_revision),
        "integration": {
            "integrated_at": str(record.get("integrated_at")),
            "integrated_sha": str(record.get("integrated_sha") or ""),
            "trunk_ref": str(record.get("trunk_ref") or ""),
        },
        # Separate, always. Integration is not acceptance.
        "acceptance": _acceptance_window(record, approved),
        "problems": [],
    }


def _acceptance_window(record: Mapping[str, Any], approved: datetime) -> Dict[str, Any]:
    accepted_at = [
        _parse_ts(e.get("accepted_at"))
        for e in record.get("accepted_units") or ()
        if isinstance(e, Mapping)
    ]
    usable = [t for t in accepted_at if t is not None]
    if not usable:
        return _unavailable(
            ["no accountable human acceptance is recorded for this Cycle. The "
             "work may be integrated and is not thereby delivered"]
        )
    latest = max(usable)
    return {
        "available": True,
        "value": (latest - approved).total_seconds() / 86400.0,
        "unit": "days from approved specification to the last accountable human acceptance",
        "last_accepted_at": latest.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "problems": [],
    }


# ─── One Cycle, and the history a plan reads ─────────────────────────────────


def measure_cycle(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Both rates for one closed Cycle, with the identity they belong to.

    `cut_rate`'s refusal of an empty admission is caught here and reported as a
    problem rather than raised, because one unmeasurable Cycle must not make the
    whole history unreadable -- and a history reporting one Cycle as
    unmeasurable is exactly the honest answer a plan needs.
    """
    identity = {
        "cycle_id": str(record.get("cycle_id") or "") if isinstance(record, Mapping) else "",
        "trunk_ref": str(record.get("trunk_ref") or "") if isinstance(record, Mapping) else "",
        "integrated_sha": str(record.get("integrated_sha") or "") if isinstance(record, Mapping) else "",
    }
    throughput = accepted_throughput(record)
    try:
        cuts = cut_rate(record)
    except MeasurementError as exc:
        cuts = _unavailable([str(exc)], numerator=None, denominator=None)
    return dict(
        identity,
        available=bool(throughput.get("available") and cuts.get("available")),
        throughput=throughput,
        cut_rate=cuts,
        problems=list(throughput.get("problems") or []) + list(cuts.get("problems") or []),
    )


def history_report(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """The recorded per-Cycle history, in the shape a concurrency plan cites.

    `available` is False when NOTHING is measurable, and that is not a
    formality: a fresh project has no history, which is the correct state, and
    the distinction a plan rests on is between that and a history of zero
    throughput. Both would otherwise arrive as an empty list.
    """
    entries = [measure_cycle(r) for r in records or ()]
    measurable = [e for e in entries if e.get("available")]
    problems: List[str] = []
    if not entries:
        problems.append(
            "no Cycle has closed, so there is no recorded throughput or "
            "cut-rate. This is an absence, not a measurement of zero: a plan "
            "must cite a bootstrap assumption rather than read capacity from it"
        )
    elif not measurable:
        problems.append(
            "%d closed Cycle(s) are recorded and none is measurable: %s"
            % (len(entries), "; ".join(p for e in entries for p in e["problems"])[:600])
        )
    throughput_values = [e["throughput"]["count"] for e in measurable]
    cut_values = [e["cut_rate"]["value"] for e in measurable]
    window = _history_window(records or ())
    return {
        "schema_version": SCHEMA_VERSION,
        "available": bool(measurable),
        "cycles": entries,
        "cycle_ids": [e["cycle_id"] for e in measurable],
        "sample_size": len(measurable),
        "throughput": (
            {
                "available": True,
                "value": sum(throughput_values) / float(len(throughput_values)),
                "unit": THROUGHPUT_UNIT + " per Cycle (mean)",
                "observations": throughput_values,
                "problems": [],
            }
            if measurable else _unavailable(problems, unit=THROUGHPUT_UNIT)
        ),
        "cut_rate": (
            {
                "available": True,
                "value": sum(cut_values) / float(len(cut_values)),
                "unit": CUT_RATE_UNIT + " (mean)",
                "observations": cut_values,
                "problems": [],
            }
            if measurable else _unavailable(problems, unit=CUT_RATE_UNIT)
        ),
        "observation_window": window,
        "problems": problems,
    }


def _history_window(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    stamps = [
        _parse_ts((r or {}).get("closed_at"))
        for r in records if isinstance(r, Mapping)
    ]
    usable = sorted(t for t in stamps if t is not None)
    if not usable:
        return _unavailable(["no closed Cycle carries a usable timestamp"])
    return {
        "available": True,
        "from": usable[0].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": usable[-1].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "problems": [],
    }


# ─── Planning against recorded history ──────────────────────────────────────


#: What a plan must name. §5.4's "Capacity input" list, verbatim in intent: the
#: historical Cycles, comparable work and verification depth, sample size,
#: throughput, cut-rate and observation window.
PLAN_REQUIRED_CITATIONS = (
    "cycle_ids",
    "sample_size",
    "throughput",
    "cut_rate",
    "observation_window",
    "comparable_work",
    "verification_depth",
)


def assert_plan_supported(
    *, plan: Mapping[str, Any], history: Mapping[str, Any]
) -> None:
    """Refuse a concurrency plan that recorded history does not support.

    `C-16`: concurrency is planned against recorded throughput and cut-rate.
    Four refusals, and the last two are the ones that matter after the first
    Cycle closes -- once history exists, the tempting failure is not planning
    without data, it is citing data that says something else.

        no history and no bootstrap   nothing to plan against
        a bootstrap where history
        exists                        ignoring what was measured
        a cited Cycle nobody
        recorded                      fabricated Cycle history
        a cited rate that disagrees
        with the measured one         a citation is a claim about the record
    """
    concurrency = plan.get("concurrent_cycles")
    if not isinstance(concurrency, int) or concurrency < 1:
        raise MeasurementError(
            "a concurrency plan must state how many Cycles it plans to run "
            "concurrently; %r is not a number of Cycles" % (concurrency,)
        )
    bootstrap = plan.get("bootstrap")
    recorded = bool(history.get("available"))

    if bootstrap is not None:
        if recorded:
            raise MeasurementError(
                "this plan cites a bootstrap assumption while %d measurable "
                "Cycle(s) are recorded (%s). A bootstrap is for a first-ever "
                "Cycle; using one where history exists plans against belief "
                "next to the data that contradicts it."
                % (history.get("sample_size"), ", ".join(history.get("cycle_ids") or ()))
            )
        if not isinstance(bootstrap, Mapping):
            raise MeasurementError("bootstrap must name its calibration sample and its assumption")
        missing = [
            f for f in ("calibration_sample", "assumption")
            if not str(bootstrap.get(f) or "").strip()
        ]
        if missing:
            raise MeasurementError(
                "a bootstrap plan must name %s. A first-ever Cycle uses the "
                "measured Discovery calibration sample and an EXPLICIT "
                "assumption -- never fabricated Cycle history, and never an "
                "unstated one that reads as measurement later."
                % ", ".join(missing)
            )
        return

    if not recorded:
        raise MeasurementError(
            "no measurable Cycle history is recorded, and this plan cites no "
            "bootstrap assumption. %s Planning %d concurrent Cycles from an "
            "absence would report an invented capacity as a measured one."
            % ("; ".join(history.get("problems") or ()) or "", concurrency)
        )

    cites = plan.get("cites")
    if not isinstance(cites, Mapping):
        raise MeasurementError(
            "a plan built on history must cite it: %s"
            % ", ".join(PLAN_REQUIRED_CITATIONS)
        )
    missing = [f for f in PLAN_REQUIRED_CITATIONS if cites.get(f) in (None, "", [], {})]
    if missing:
        raise MeasurementError(
            "the plan does not cite %s. §5.4 requires the actual observation "
            "window and sample size to be named, because a plan that cites "
            "only a number cannot be checked against the record it came from."
            % ", ".join(missing)
        )

    recorded_ids = set(history.get("cycle_ids") or ())
    fabricated = sorted({str(c) for c in cites.get("cycle_ids") or ()} - recorded_ids)
    if fabricated:
        raise MeasurementError(
            "the plan cites Cycle(s) %s, which are not in the measurable "
            "recorded history (%s). A citation names a record; naming one that "
            "does not exist is fabricated Cycle history."
            % (", ".join(fabricated), ", ".join(sorted(recorded_ids)) or "none")
        )
    if int(cites.get("sample_size") or 0) != int(history.get("sample_size") or 0):
        raise MeasurementError(
            "the plan cites a sample size of %s and %d Cycle(s) are measurable. "
            "The sample size is a property of the record, not of the plan."
            % (cites.get("sample_size"), history.get("sample_size"))
        )
    for field in ("throughput", "cut_rate"):
        claimed = cites.get(field)
        measured = (history.get(field) or {}).get("value")
        if measured is None or abs(float(claimed) - float(measured)) > 1e-9:
            raise MeasurementError(
                "the plan cites %s %r and the record measures %r. A citation is "
                "a claim about the record, so a disagreement is refused rather "
                "than reconciled: whichever is right, the plan is not built on "
                "what was measured." % (field, claimed, measured)
            )
