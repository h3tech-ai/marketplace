"""Layer 1 — the test-first prove path (#406, proposal 3.3, ADR-032 section 4).

`SP-WRK-007` requires a verifying stage to verify against criteria declared
BEFORE any producing stage ran. V1 did the reverse: QE authored tests after
reading SE's diff, which is the bias where the test encodes the implementation
instead of the requirement.

Hypothesis: three things carry this file, and each is a negative.

  1. **Admission.** A Work Unit that states a position on acceptance cases and
     declares none cannot be admitted at COMMIT. The migration path (a
     declared gap, or a unit record predating the field) is admitted and
     RECORDED, never silently equated with having authored cases.
  2. **Primacy.** A green runner exit code does not clear `tests_pass` when an
     authored case is failed, blocked or DROPPED. Dropped is the new one: the
     authored set lives in the sealed manifest, so a case the verifying
     receipt does not report is a case the prover removed, not one that never
     existed.
  3. **Order.** Evidence whose execution pre-dates the producing stage's
     latest entry is refused. That is the ordering SP-WRK-007 asks for, read
     off the clock the kernel already keeps.

Context for (2): the SPQ design's section 13.3 measured 4 of 5 DoD checks
emitting zero evidence across 296 production receipts while the Evidence gate
rendered green, with `tests_pass` the only one that worked (238/296). So the
check being made structural here is also the only one with a track record, and
every assertion below is written against that scrutiny.
"""

from __future__ import annotations


import json
import subprocess
import ast
from pathlib import Path

import pytest

import spq_manifest as mf
import spq_state_machine as sm
import spq_paths as sp
import cycle_records
from story_pipeline import (
    _evaluate_check,
    _evaluate_fallback_proof,
    authored_cases_target,
    authored_cases_verdict,
    authored_execution_after_producer,
    create_story,
    evaluate_story_dod,
    evaluate_tests_pass,
    next_action,
    story_authored_case_ids,
)

from _spq_fixture import CYCLE_KWARGS, unit as _fx_unit

pytestmark = pytest.mark.unit

#: The authored Claude plugin tree, for the prompt and agent-body assertions.
PLUGIN = Path(__file__).resolve().parents[2]


# ── fixtures ──────────────────────────────────────────────────────────────────


CASES = [
    _fx_unit("AC-1", statement="an unauthenticated GET returns 401"),
    _fx_unit("AC-2", statement="a member GET returns only their own rows"),
]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=False)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    """A real git repo with one commit, so a Cycle baseline exists."""
    project = tmp_path / "proj"
    project.mkdir()
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "t@e.co")
    _git(project, "config", "user.name", "t")
    (project / "f.txt").write_text("x", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")
    (project / ".synaptory.yaml").write_text(
        "build_mode: spq\n"
        "spq:\n"
        "  workstreams:\n"
        '    - id: "spine"\n'
        "      shared_owner: true\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("SYNAPTORY_ACTIVE_SPEC", raising=False)
    return project


def _at_commit(project: Path) -> None:
    sm.initialize(str(project), workstream_id="spine")
    sm.approve_baseline(str(project), approved_by="t", baseline_ref="baseline-1",
        calibration={"sample_units": 1, "measured_hours": 1})


def _open(project: Path, **kwargs) -> dict:
    """`open_cycle`, then the board it produced.

    `open_cycle` returns a Commit SUMMARY now (`cycle_id`,
    `declaration_hash`, the admitted ids) rather than the board, because the
    board is per-Cycle and there is no project-global one to hand back. The
    assertions here are about the board, so they read it -- and reading it
    back through `read_state` is stronger than inspecting a return value,
    since it proves what was PERSISTED rather than what was computed.
    """
    kwargs.setdefault("goal", "cycle 1")
    sm.open_cycle(str(project), **dict(CYCLE_KWARGS, **kwargs))
    return sm.read_state(str(project))


def _unit(uid: str = "WU-1", **over) -> dict:
    unit = {"id": uid, "title": "work", "labels": ["ws:spine"], "kind": "story",
             "acceptance_criteria": ["it works"],
             "path_scope": ["api/" + uid.lower() + "/"]}
    unit.update(over)
    return unit


def _story(sid: str = "WU-1", state: str = "testing", **over) -> dict:
    """A board record with a producing-stage entry, so ordering is answerable."""
    story = create_story(sid, "work", acceptance_cases=CASES)
    story["state"] = state
    story["pipeline_log"] = [
        {"state": "queued", "entered_at": "2026-01-01T00:00:00Z",
         "exited_at": "2026-01-01T01:00:00Z"},
        {"state": "in_progress", "entered_at": "2026-01-01T01:00:00Z",
         "exited_at": "2026-01-01T02:00:00Z"},
        {"state": "testing", "entered_at": "2026-01-01T02:00:00Z",
         "exited_at": None},
    ]
    story.update(over)
    return story


def _receipt(results: dict | None = None, exit_code: int = 0,
             completed_at: str = "2026-01-01T03:00:00Z", **over) -> dict:
    """A QE receipt: a green suite run plus per-case outcomes."""
    body: dict = {
        "task": "verify",
        "agent": "quality-engineer",
        "backend": "claude",
        "model": "sonnet",
        "artifacts": ["tests/test_api.py"],
        "completed_at": completed_at,
        "verification_commands": [
            {"command": "pytest tests/ -q", "exit_code": exit_code,
             "summary": "12 passed"},
        ],
    }
    if results is not None:
        body["metrics"] = {"authored_test_cases": {
            "source": "cycle-manifest", "total": len(results), "results": results,
        }}
    body.update(over)
    return body


def _all_passed() -> dict:
    return {cid: {"status": "passed"} for cid in ("AC-1", "AC-2")}


def _write_receipt(project: Path, sid: str, abbrev: str, body: dict) -> Path:
    d = project / ".synaptory" / ".orchestrator" / "receipts"
    d.mkdir(parents=True, exist_ok=True)
    path = d / ("%s-%s.json" % (sid, abbrev))
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _board(project: Path, story: dict) -> None:
    """Put one story on an existing project's board."""
    state = sm.read_state(str(project))
    state["current_stories"] = [story]
    sm._write_state(str(project), state)


# ══ 1. admission at COMMIT ═══════════════════════════════════════════════════


def test_a_unit_with_authored_cases_is_admitted(repo: Path):
    _at_commit(repo)
    state = _open(repo, admitted_units=[_unit(acceptance_cases=CASES)])
    assert state["lifecycle_state"] == "CYCLE"
    assert state["test_first"]["units_with_authored_cases"] == 1
    assert state["test_first"]["criteria_gaps"] == []
    assert story_authored_case_ids(state["current_stories"][0]) == ["AC-1", "AC-2"]


def test_admission_is_refused_without_authored_cases(repo: Path):
    """The keystone negative: a unit that states a position and declares none.

    Refused at COMMIT, because no later check can restore an ordering the
    Cycle never had.
    """
    _at_commit(repo)
    with pytest.raises(sm.HydrationRefusal) as exc:
        sm.open_cycle(str(repo), goal="cycle 1", admitted_units=[_unit(acceptance_cases=[])], **CYCLE_KWARGS)
    assert exc.value.code == "authored_cases_absent"
    assert "SP-WRK-007" in str(exc.value)


def test_a_refused_admission_leaves_commit_untouched(repo: Path):
    """A refusal must be re-runnable, not a half-opened Cycle to recover."""
    _at_commit(repo)
    with pytest.raises(sm.HydrationRefusal):
        sm.open_cycle(str(repo), goal="cycle 1", admitted_units=[_unit(acceptance_cases=[])], **CYCLE_KWARGS)
    state = sm.read_state(str(repo))
    # `DISCOVERY`, not `COMMIT`: Commit is a recorded method event now, never a
    # stage (`C-02`), so the stage a refused admission leaves the project on is
    # the one it was already on. The guarantee is unchanged and is the next
    # three assertions -- no Cycle, no board, no posture block.
    assert state["lifecycle_state"] == "DISCOVERY"
    assert not state.get("_cycle_id")
    assert not state.get("current_stories")
    assert not state.get("test_first")
    # And the PO can author the cases and re-run the same verb.
    assert _open(repo, admitted_units=[_unit(acceptance_cases=CASES)])[
        "lifecycle_state"] == "CYCLE"


def test_one_unauthored_unit_refuses_the_whole_admission(repo: Path):
    """Per-unit, and the Cycle is the unit of admission: a mixed set is
    refused, and the message names the offending unit rather than the batch."""
    _at_commit(repo)
    with pytest.raises(sm.HydrationRefusal) as exc:
        sm.open_cycle(str(repo), goal="c", admitted_units=[
            _unit("WU-1", acceptance_cases=CASES),
            _unit("WU-2", acceptance_cases=[]),
        ], **CYCLE_KWARGS)
    assert "WU-2" in str(exc.value)
    assert "'WU-1'" not in str(exc.value)


def test_a_declared_gap_admits_and_is_recorded(repo: Path):
    """The migration path is EXPLICIT and VISIBLE, not silent."""
    _at_commit(repo)
    state = _open(repo, goal="c", admitted_units=[_unit(
        acceptance_cases=[],
        criteria_gap_declared={
            "reason": "spike; the criteria are the output, not the input",
            "declared_by": "po@h3t.co",
        },
    )])
    tf = state["test_first"]
    assert tf["units_admitted"] == 1
    assert tf["units_with_authored_cases"] == 0
    assert len(tf["criteria_gaps"]) == 1
    gap = tf["criteria_gaps"][0]
    assert gap["unit_id"] == "WU-1"
    assert gap["declared"] is True
    assert "spike" in gap["reason"]
    # And it travels with the WORK, not just the Cycle summary.
    assert state["current_stories"][0]["criteria_gap_declared"]["declared"] is True


def test_a_gap_with_no_reason_is_not_a_declaration(repo: Path):
    """"Absence of a reason" accepted as a reason is the silent-nothing this
    ticket removes, so an empty gap block refuses like no gap at all."""
    _at_commit(repo)
    with pytest.raises(sm.HydrationRefusal) as exc:
        sm.open_cycle(str(repo), goal="c", admitted_units=[_unit(
            acceptance_cases=[], criteria_gap_declared={"declared_by": "po"},
        )], **CYCLE_KWARGS)
    assert "no `reason`" in str(exc.value)


def test_a_pre_406_unit_record_is_admitted_with_an_implicit_gap(repo: Path):
    """An in-flight Cycle is not bricked, and the difference is recorded.

    Key ABSENCE is the migration signal. A unit that never heard of the field
    is admitted, and the gap it gets is marked `declared: False` so an auditor
    can tell "nobody declared this" from "the PO declared this".
    """
    _at_commit(repo)
    state = _open(repo, goal="c", admitted_units=[_unit()])  # no acceptance_cases key
    gap = state["test_first"]["criteria_gaps"][0]
    assert gap["declared"] is False
    assert gap["reason"] == mf.LEGACY_GAP_REASON
    assert "pre-#406" in gap["reason"] or "before the test-first" in gap["reason"]
    assert state["test_first"]["units_with_authored_cases"] == 0


def test_the_sealed_manifest_carries_the_authored_set(repo: Path):
    """The artifact lives in the hash-sealed COMMIT document, so it cannot be
    edited afterwards without a recorded supersede."""
    _at_commit(repo)
    _open(repo, goal="c", admitted_units=[_unit(acceptance_cases=CASES)])
    cycle_id = sm.identity(str(repo)).cycle_id
    body = json.loads(
        Path(sp.manifest_path(str(repo), cycle_id)).read_text(encoding="utf-8")
    )
    # `cycle_records` seals the document and names the digest
    # `declaration_hash` and the set `admitted_units` (#644). The guarantee is
    # the same one and is asserted at its new home; `authored_case_ids` still
    # reads the unit, because `acceptance_cases` is the unit's own key on
    # either document.
    assert cycle_records.verify_hash(body)
    assert mf.authored_case_ids(body["admitted_units"][0]) == ["AC-1", "AC-2"]
    # And the cases are INSIDE the hash: editing one invalidates the seal.
    body["admitted_units"][0]["acceptance_cases"][0]["statement"] = "trivially true"
    assert not cycle_records.verify_hash(body)


# ══ 2. the authored-case artifact's shape ════════════════════════════════════


@pytest.mark.parametrize("bad_id", ["has space", "../escape", "-leading", "a" * 120])
def test_an_unmatchable_case_id_is_refused(bad_id: str):
    problems = mf.admission_problems([
        _unit(acceptance_cases=[_fx_unit(bad_id, statement="s")])
    ])
    assert any("acceptance case id" in p for p in problems), problems


def test_a_duplicate_case_id_is_refused():
    problems = mf.admission_problems([_unit(acceptance_cases=[
        _fx_unit("AC-1", statement="a"), _fx_unit("AC-1", statement="b"),
    ])])
    assert any("twice" in p for p in problems), problems


def test_a_case_with_no_statement_is_refused():
    """An id with no criterion behind it is a case the producer cannot target
    and the prover can trivially mark passed."""
    problems = mf.admission_problems([
        _unit(acceptance_cases=[_fx_unit("AC-1")])
    ])
    assert any("no statement" in p for p in problems), problems


def test_a_bare_string_case_gets_a_positional_id():
    """Lowering the authoring bar: a PO who must invent ids authors fewer
    cases. Positional ids are stable because the manifest is immutable."""
    problems = mf.admission_problems([_unit(acceptance_cases=["returns 401"])])
    assert problems == []
    assert mf.authored_case_ids(
        mf._normalize_unit(_unit(acceptance_cases=["returns 401"]))
    ) == ["AC-1"]


def test_case_shape_is_validated_on_every_manifest():
    """Shape is checked by `validate`, not only at admission: a malformed id
    in a hash-sealed document is unmatchable forever."""
    manifest = mf.build(
        cycle_id="1-abcdef12", cycle_seq=1, goal="g", baseline_sha="abc",
        integration_ref="ref", workstreams=[_fx_unit("spine", shared_owner=True)],
        work_units=[_unit(owner_workstream="spine",
                          acceptance_cases=[_fx_unit("bad id", statement="s")])],
    )
    problems = mf.validate(manifest, require_baseline=False)
    assert any("acceptance case id" in p for p in problems), problems


def test_validate_does_not_demand_presence():
    """Presence is an ADMISSION question, asked once at COMMIT. Asking it in
    `validate` would re-ask it of every sealed manifest on every hydration and
    refuse the ones sealed before the field existed."""
    manifest = mf.build(
        cycle_id="1-abcdef12", cycle_seq=1, goal="g", baseline_sha="abc",
        integration_ref="ref", workstreams=[_fx_unit("spine", shared_owner=True)],
        work_units=[_unit(owner_workstream="spine")],
    )
    assert mf.validate(manifest, require_baseline=False) == []


def test_normalize_does_not_invent_a_position():
    """`_normalize_unit` must not turn an absent key into `[]`: that would
    make every pre-#406 unit read as "asked and authored nothing", which
    admission refuses, bricking exactly the Cycles migration must carry."""
    assert "acceptance_cases" not in mf._normalize_unit(_unit())
    assert mf._normalize_unit(_unit(acceptance_cases=[]))["acceptance_cases"] == []


def test_authored_cases_outrank_a_stale_gap_declaration():
    """A unit carrying both has the artifact; the gap declaration is stale."""
    unit = _unit(acceptance_cases=CASES,
                 criteria_gap_declared={"reason": "was a spike"})
    assert mf.classify_unit(unit) == mf.TEST_FIRST_AUTHORED


# ══ 3. tests_pass: authored cases are the PRIMARY evaluation ═════════════════


def test_a_green_suite_and_every_case_passed_clears_the_check():
    verdict, detail = evaluate_tests_pass(_receipt(_all_passed()), _story())
    assert verdict is True
    assert detail is None


@pytest.mark.parametrize("status", ["failed", "blocked", "in-progress", ""])
def test_a_green_exit_code_cannot_clear_a_non_passing_case(status: str):
    """The primacy assertion. Exit code 0 on the suite, one authored case not
    passing, and the check is False."""
    results = {"AC-1": {"status": "passed"}, "AC-2": {"status": status}}
    verdict, detail = evaluate_tests_pass(_receipt(results), _story())
    assert verdict is False
    assert "AC-2" in (detail or "")


def test_a_green_exit_code_cannot_clear_a_DROPPED_case():
    """The rule the #187 veto could not express.

    That veto compared the receipt against ITSELF (`total` vs `len(results)`),
    so a receipt declaring two cases and reporting two passing ones satisfied
    it even when the Cycle authored more. Comparing against the sealed
    manifest's ids is what makes dropping a case you cannot pass a refusal.
    """
    results = {"AC-1": {"status": "passed"}}  # AC-2 simply not mentioned
    verdict, detail = evaluate_tests_pass(_receipt(results), _story())
    assert verdict is False
    assert "AC-2" in (detail or "")
    assert "DROPPED" in (detail or "")


def test_the_pre_406_veto_could_not_see_a_dropped_case():
    """Characterizes the gap this ticket closes, so the fix cannot be
    reverted without this failing: the receipt is internally consistent."""
    from story_pipeline import _authored_cases_pass

    metrics = {"authored_test_cases": {
        "total": 1, "results": {"AC-1": {"status": "passed"}},
    }}
    assert _authored_cases_pass(metrics) is True          # self-consistent
    assert evaluate_tests_pass(
        _receipt({"AC-1": {"status": "passed"}}), _story()
    )[0] is False                                          # against the manifest


def test_no_per_case_record_is_a_gap_and_never_a_pass():
    """A green suite with an authored set and no per-case outcomes has not
    answered the check. It renders as a gap, which never reads as a pass."""
    verdict, detail = evaluate_tests_pass(_receipt(results=None), _story())
    assert verdict is None
    assert "no `metrics.authored_test_cases`" in (detail or "")
    assert "AC-1" in (detail or "")


def test_a_claim_of_coverage_with_no_results_fails():
    receipt = _receipt(_all_passed())
    receipt["metrics"]["authored_test_cases"]["results"] = {}
    assert evaluate_tests_pass(receipt, _story())[0] is False


def test_a_red_suite_still_fails_even_with_every_case_passed():
    """Both are required; neither substitutes for the other."""
    assert evaluate_tests_pass(
        _receipt(_all_passed(), exit_code=1), _story()
    )[0] is False


def test_string_commands_are_still_not_proof_with_cases_passed():
    """#106 unchanged: a plain-string command is a replay instruction. With
    the authored cases green and no executed object, the check is unverified,
    not passed."""
    receipt = _receipt(_all_passed())
    receipt["verification_commands"] = ["pytest tests/ -q"]
    assert evaluate_tests_pass(receipt, _story())[0] is None


def test_a_non_passing_case_beats_a_missing_runner_verdict():
    """A definitive authored-case negative wins outright, whether or not the
    runner reported anything at all."""
    receipt = _receipt({"AC-1": {"status": "failed"}, "AC-2": {"status": "passed"}})
    receipt["verification_commands"] = []
    assert evaluate_tests_pass(receipt, _story())[0] is False


def test_not_applicable_with_a_reason_counts_as_a_settled_case():
    results = {"AC-1": {"status": "passed"},
               "AC-2": {"status": "not-applicable", "reason": "data migration"}}
    assert evaluate_tests_pass(_receipt(results), _story())[0] is True


def test_a_bare_not_applicable_is_not_a_settled_case():
    """The cheapest forgery on this whole path, closed by the cheapest floor.

    A prover that cannot pass a case writes `{"status": "not-applicable"}` for
    it, or for every case, and clears the gate having proven nothing. A
    required `reason` does not make the claim true; it makes it a claim
    someone wrote and a reviewer can read, which is the standard
    `unit_case_problems` already holds an authored case's `statement` to.
    """
    results = {"AC-1": {"status": "passed"}, "AC-2": {"status": "not-applicable"}}
    verdict, detail = evaluate_tests_pass(_receipt(results), _story())
    assert verdict is False
    assert "AC-2" in (detail or "") and "reason" in (detail or "")


def test_every_case_not_applicable_with_no_reason_is_refused():
    results = {cid: {"status": "not-applicable"} for cid in ("AC-1", "AC-2")}
    assert evaluate_tests_pass(_receipt(results), _story())[0] is False


def test_a_failed_case_outside_the_authored_set_still_fails():
    """The authored path must not be LOOSER than the veto it replaces.

    The #187 predicate read `all(results.values())`, so a case QE added
    itself and reported `failed` failed the gate. Iterating only the authored
    ids would have silently stopped doing that under a key the manifest does
    not name, which is a bypass created by enabling the feature.
    """
    results = {"AC-1": {"status": "passed"}, "AC-2": {"status": "passed"},
               "QE-EXTRA-1": {"status": "failed"}}
    verdict, detail = evaluate_tests_pass(_receipt(results), _story())
    assert verdict is False
    assert "QE-EXTRA-1" in (detail or "")


# ══ 4. order: proving must follow producing ══════════════════════════════════


def test_execution_predating_the_producing_stage_is_refused():
    """The SP-WRK-007 ordering assertion. Every authored case passed, a green
    suite, and the work happened BEFORE the current candidate was produced."""
    receipt = _receipt(_all_passed(), completed_at="2026-01-01T00:30:00Z")
    verdict, detail = evaluate_tests_pass(receipt, _story())
    assert verdict is False
    assert "pre-dates" in (detail or "")
    assert "producing stage" in (detail or "")


def test_a_re_entered_producing_stage_invalidates_the_prior_proof():
    """The realistic shape: QE proved the cases, SE was re-dispatched, and the
    old receipt is now about a candidate that has been rebuilt."""
    story = _story()
    story["pipeline_log"].append(
        {"state": "in_progress", "entered_at": "2026-01-01T04:00:00Z",
         "exited_at": None}
    )
    receipt = _receipt(_all_passed(), completed_at="2026-01-01T03:00:00Z")
    assert evaluate_tests_pass(receipt, story)[0] is False


def test_a_same_second_receipt_is_not_stale():
    """Cursor emits second-precision `completed_at` while stage entry carries
    microseconds; strict `>` would read a same-second receipt as stale. Reuses
    `receipt_timestamp_is_fresh` rather than a second clock."""
    story = _story()
    story["pipeline_log"][1]["entered_at"] = "2026-01-01T01:00:00.647295Z"
    receipt = _receipt(_all_passed(), completed_at="2026-01-01T01:00:00Z")
    assert authored_execution_after_producer(story, receipt) is True


def test_unestablishable_order_is_a_gap_not_a_verdict():
    """Unknowable resolves to a criteria gap, in both directions: no producing
    stage entry, and no parseable `completed_at`."""
    no_log = _story()
    no_log["pipeline_log"] = []
    assert authored_execution_after_producer(no_log, _receipt(_all_passed())) is None
    verdict, detail = evaluate_tests_pass(_receipt(_all_passed()), no_log)
    assert verdict is None
    assert "cannot be ordered" in (detail or "")

    no_ts = _receipt(_all_passed())
    no_ts.pop("completed_at")
    assert authored_execution_after_producer(_story(), no_ts) is None


def test_a_forged_future_completed_at_does_not_satisfy_the_ordering():
    """The ordering is the one structural guarantee #406 claims over #187, and
    `completed_at` is a field in an agent-written file. Without an upper bound
    `"2099-01-01T00:00:00Z"` satisfied it unconditionally, from any receipt,
    forever. A wrong clock makes the ordering unestablishable rather than
    disproven, so it is a gap."""
    receipt = _receipt(_all_passed(), completed_at="2099-01-01T00:00:00Z")
    assert authored_execution_after_producer(_story(), receipt) is None
    verdict, detail = evaluate_tests_pass(receipt, _story())
    assert verdict is None
    assert "cannot be ordered" in (detail or "")


def test_real_clock_skew_is_tolerated():
    """The bound must be generous against host skew: the cost of a false gap
    is a blocked story, and it should not be paid by a CI runner a few minutes
    fast. A story entered `in_progress` long ago, so only the future bound is
    under test here."""
    from datetime import datetime, timedelta, timezone
    from story_pipeline import _FUTURE_TOLERANCE_S

    skewed = (
        datetime.now(timezone.utc) + timedelta(seconds=_FUTURE_TOLERANCE_S - 60)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    story = _story()
    story["pipeline_log"][1]["entered_at"] = "2026-01-01T01:00:00Z"
    assert authored_execution_after_producer(
        story, _receipt(_all_passed(), completed_at=skewed)
    ) is True


def test_the_order_check_uses_the_kernel_clock():
    """`_parse_iso_timestamp` is the advance kernel's parser, so offset and Z
    forms are read identically on both sides of the boundary."""
    receipt = _receipt(_all_passed(), completed_at="2026-01-01T03:00:00+00:00")
    assert authored_execution_after_producer(_story(), receipt) is True


# ══ 5. no authored set: the pre-#406 rules are exact ═════════════════════════


def test_without_an_authored_set_the_old_rules_apply():
    """A declared-gap unit, a legacy unit, and every Scrum / Kanban story go
    down the pre-#406 path untouched."""
    legacy = _story(acceptance_cases=[])
    assert story_authored_case_ids(legacy) == []
    assert evaluate_tests_pass(_receipt(results=None), legacy)[0] is True
    assert evaluate_tests_pass(_receipt(results=None), legacy)[1] is None


def test_the_187_receipt_internal_veto_survives():
    """With no authored set, a receipt whose own `total` disagrees with its
    results is still vetoed, exactly as #187 left it."""
    legacy = _story(acceptance_cases=[])
    receipt = _receipt({"X-1": {"status": "passed"}})
    receipt["metrics"]["authored_test_cases"]["total"] = 3
    assert evaluate_tests_pass(receipt, legacy)[0] is False


# Every shape a `tests_pass` receipt can take, crossed. Written as a table
# because the divergence this pins was found by crossing them exhaustively and
# not by reading the code: applying the authored-case veto unconditionally
# moved `passed` from None to False for a receipt with a definitive negative
# and NO executed proof object, which would have tightened every Scrum story,
# every Kanban story and every migrated SPQ unit as a side effect of a pilot
# scoped to SPQ. The values below are the pre-#406 verdicts.
_LEGACY_MATRIX = [
    # (commands, authored_test_cases, expected verdict)
    ([], None, False),
    ([], {"total": 2, "results": {"a": {"status": "failed"}}}, False),
    (["pytest -q"], None, None),
    (["pytest -q"], {"total": 2, "results": {"a": {"status": "failed"}}}, None),
    (["pytest -q"], {"results": {}}, None),
    ([{"command": "pytest -q"}], {"total": 3, "results": {"a": {"status": "passed"}}}, None),
    ([{"command": "pytest -q", "exit_code": 0}], None, True),
    ([{"command": "pytest -q", "exit_code": 0}], {"results": {}}, False),
    ([{"command": "pytest -q", "exit_code": 0}],
     {"total": 3, "results": {"a": {"status": "passed"}}}, False),
    ([{"command": "pytest -q", "exit_code": 0}],
     {"total": 1, "results": {"a": {"status": "passed"}}}, True),
    ([{"command": "pytest -q", "exit_code": 1}], None, False),
    ([{"command": "pytest -q", "exit_code": 1}],
     {"total": 1, "results": {"a": {"status": "passed"}}}, False),
    (["pytest -q", {"command": "npm t", "exit_code": 0}], None, True),
]


@pytest.mark.parametrize("cmds,atc,expected", _LEGACY_MATRIX)
@pytest.mark.parametrize(
    "story",
    [None, {}, {"acceptance_cases": []},
     {"acceptance_cases": [], "criteria_gap_declared": {"reason": "spike"}}],
)
def test_the_legacy_verdict_is_unchanged_shape_by_shape(cmds, atc, expected, story):
    receipt: dict = {"verification_commands": cmds}
    if atc is not None:
        receipt["metrics"] = {"authored_test_cases": atc}
    assert _evaluate_check("tests_pass", receipt, story=story) is expected


def test_a_story_argument_is_optional():
    """Many callers evaluate a receipt out of board context; they must keep
    working unchanged."""
    assert _evaluate_check("tests_pass", _receipt(results=None)) is True
    assert evaluate_tests_pass(_receipt(results=None))[0] is True


def test_build_succeeds_is_untouched():
    """The authored-case path is scoped to tests_pass. Nothing else moved."""
    se = {"verification_commands": [
        {"command": "npm run build", "exit_code": 0}]}
    assert _evaluate_check("build_succeeds", se, story=_story()) is True
    assert _evaluate_check("build_succeeds", {"verification_commands": []}) is False


# ══ 6. the fallback path cannot re-open the bypass ═══════════════════════════


def test_a_foreign_green_pytest_cannot_satisfy_an_authored_story():
    """#134 GAP-10's rule is "role is a preference, structured evidence is the
    requirement". Once cases are authored, the requirement is the per-case
    evidence, so an SE receipt with a recognisable green test command is no
    longer enough."""
    se = {"verification_commands": [
        {"command": "pytest tests/ -q", "exit_code": 0}]}
    assert _evaluate_fallback_proof("tests_pass", se) is True          # pre-#406
    assert _evaluate_fallback_proof(
        "tests_pass", se, story=_story()
    ) is not True                                                      # post-#406


def test_a_stray_partial_report_cannot_block_a_satisfied_check(repo: Path):
    """#445's other direction, and it matters as much as the obvious one.

    Letting an untrusted payload CLEAR a check is the obvious forgery. Letting
    one BLOCK a check hands any receipt writer a way to stall another
    workstream's story, and "it only ever makes things stricter" is not an
    argument for trusting untrusted input.

    The case that matters is the one where the check is satisfied by a
    NON-nominal receipt, because that is where the veto scan actually runs.
    An adversarial review found the first version of this test only exercised
    the branch the `passed is not True` guard already protects, and that the
    property was false in the branch it did not exercise: a code-reviewer
    receipt noting the two cases it happened to look at turned a `tests_pass`
    the SE receipt legitimately satisfied into a definitive FAIL. The veto is
    now narrowed to an EXPLICIT non-passing status: a subset report is silence
    about the rest, and silence is not a negative.
    """
    _at_commit(repo)
    sm.open_cycle(str(repo), goal="c", admitted_units=[_unit(acceptance_cases=CASES)], **CYCLE_KWARGS)
    story = _story()
    _board(repo, story)
    # tests_pass satisfied by the SE receipt through the evidence fallback,
    # with NO qe receipt, so the nominal verdict is not already True.
    _write_receipt(repo, "WU-1", "se", _receipt(
        _all_passed(), agent="software-engineer",
    ))
    _write_receipt(repo, "WU-1", "cr", {
        "agent": "code-reviewer", "status": "complete",
        "completed_at": "2026-01-01T04:00:00Z",
        "story_dod": {"code_reviewed": True},
        "metrics": {"findings_critical": 0, "authored_test_cases": {
            "total": 1, "results": {"AC-1": {"status": "passed"}}}},
    })
    dod = evaluate_story_dod(str(repo), "WU-1", "early")
    tp = dod["checks"]["tests_pass"]
    assert tp["passed"] is True, tp.get("detail")
    assert not tp.get("definitive_negative")


def test_an_explicit_negative_from_any_receipt_still_vetoes(repo: Path):
    """The clearing direction, unweakened. A subset report is not a negative,
    but a case explicitly reported `failed` is, whoever reported it."""
    _at_commit(repo)
    sm.open_cycle(str(repo), goal="c", admitted_units=[_unit(acceptance_cases=CASES)], **CYCLE_KWARGS)
    _board(repo, _story())
    _write_receipt(repo, "WU-1", "se", _receipt(
        _all_passed(), agent="software-engineer",
    ))
    _write_receipt(repo, "WU-1", "cr", {
        "agent": "code-reviewer", "status": "complete",
        "completed_at": "2026-01-01T04:00:00Z",
        "story_dod": {"code_reviewed": True},
        "metrics": {"findings_critical": 0, "authored_test_cases": {
            "results": {"AC-1": {"status": "failed"}}}},
    })
    dod = evaluate_story_dod(str(repo), "WU-1", "early")
    assert dod["checks"]["tests_pass"]["passed"] is not True
    assert dod["checks"]["tests_pass"]["definitive_negative"] is True


def test_the_gap_detail_names_the_nominal_receipt(repo: Path):
    """The retry prompt teaches from this text, so it must describe the
    receipt at fault. Scanning in collection order let an SE receipt's "no
    per-case object" be reported while the QE receipt was the real problem,
    and the orchestrator would re-dispatch the wrong role."""
    _at_commit(repo)
    sm.open_cycle(str(repo), goal="c", admitted_units=[_unit(acceptance_cases=CASES)], **CYCLE_KWARGS)
    _board(repo, _story())
    _write_receipt(repo, "WU-1", "se", {
        "agent": "software-engineer", "completed_at": "2026-01-01T02:00:00Z",
        "verification_commands": [{"command": "npm run build", "exit_code": 0}],
    })
    # The QE receipt reports AC-1 and drops AC-2: that is the fault to name.
    _write_receipt(repo, "WU-1", "qe", _receipt({"AC-1": {"status": "passed"}}))
    detail = evaluate_story_dod(str(repo), "WU-1", "early")["checks"]["tests_pass"]["detail"]
    assert "AC-2" in detail and "DROPPED" in detail


def test_a_foreign_receipt_with_full_case_evidence_still_counts():
    """Evidence is the requirement, not the role: a non-QE receipt that
    carries the authored-case outcomes and post-dates the producer satisfies
    it."""
    other = _receipt(_all_passed(), agent="software-engineer")
    assert _evaluate_fallback_proof(
        "tests_pass", other, story=_story()
    ) is True


# ══ 7. the dispatch payload carries the authored cases as the target ═════════


@pytest.mark.parametrize(
    "state,role", [("queued", "se"), ("testing", "qe"), ("reviewing", "cr")]
)
def test_every_pipeline_dispatch_receives_the_authored_cases(state, role, tmp_path):
    board = {"current_stories": [_story(state=state)],
             "current_cycle": 4, "cumulative_ticket_number": 12}
    action = next_action(board, receipts_dir=str(tmp_path))
    assert action["role"] == role, action["reason"]
    block = action["authored_cases"]
    assert block["authored_at_commit"] is True
    assert block["count"] == 2
    assert [c["id"] for c in block["cases"]] == ["AC-1", "AC-2"]
    assert block["cases"][0]["statement"].startswith("an unauthenticated")


def test_a_dispatch_under_the_migration_path_says_so(tmp_path):
    """A dispatch never has to infer which regime it is running under."""
    story = _story(state="queued", acceptance_cases=[])
    story["criteria_gap_declared"] = {"reason": "spike", "declared": True}
    action = next_action({"current_stories": [story]}, receipts_dir=str(tmp_path))
    block = action["authored_cases"]
    assert block["authored_at_commit"] is False
    assert block["count"] == 0
    assert block["criteria_gap_declared"]["reason"] == "spike"


def test_non_dispatch_actions_carry_no_authored_block(tmp_path):
    action = next_action({"current_stories": []}, receipts_dir=str(tmp_path))
    assert action["action"] == "no_stories"
    assert action["authored_cases"] is None


def test_the_block_survives_the_spq_wrapper(repo: Path):
    """The SPQ wrapper is the actual orchestrator path, and it rebuilds the
    output dict from `base`. A block the shared pipeline attaches is worthless
    if the lifecycle wrapper drops it -- which is exactly how #192 made story
    parallelism unreachable for months."""
    _at_commit(repo)
    sm.open_cycle(str(repo), goal="c", admitted_units=[_unit(acceptance_cases=CASES)], **CYCLE_KWARGS)
    action = sm.next_action(str(repo))
    assert action["action"] == "dispatch_se"
    assert [c["id"] for c in action["authored_cases"]["cases"]] == ["AC-1", "AC-2"]


def test_the_orchestrator_is_instructed_to_relay_the_block():
    """A JSON field no consumer is told to read is not a target.

    An adversarial review found the first version of this work populated
    `next_action()["authored_cases"]` and asserted only that it was populated.
    Nothing relayed it: `modes/spq.md` has an explicit "state the tier in
    every dispatch prompt" line for the `dod` block and had no equivalent
    here, so the ticket's "the producing dispatch receives them as its target"
    was implemented as a field in a dict. This asserts the instruction exists,
    and names all three roles, because all three need it and none of them can
    obtain the authored set itself.
    """
    spq = (PLUGIN / "skills" / "synaptory" / "modes" / "spq.md").read_text(
        encoding="utf-8"
    )
    assert "`authored_cases` block" in spq
    assert "state it verbatim in the dispatch prompt" in spq
    for role in ("dispatch_se", "dispatch_qe", "dispatch_cr"):
        assert role in spq
    # And the QE agent body describes the inverted order.
    qe = (PLUGIN / "agents" / "quality-engineer" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "authored_at_commit" in qe
    assert "You do not author the acceptance set" in qe


def test_the_commit_prompt_authors_the_artifact():
    """Admission refuses a unit with no cases, so the prompt that admits units
    has to tell the PO to author them. A refusal nobody was warned about is a
    broken lifecycle, not a gate."""
    commit = (PLUGIN / "skills" / "synaptory" / "spq" / "commit.md").read_text(
        encoding="utf-8"
    )
    assert "acceptance_cases" in commit
    assert "criteria_gap_declared" in commit
    assert "refuses admission" in commit


def test_the_block_is_additive_and_the_target_is_referenced_not_inlined():
    """#404 cut the SE fixed instruction payload to just under the enforced
    4,000-word ceiling. The authored cases therefore ride the PER-DISPATCH
    payload, and nothing was added to the rendered Execution Envelope."""
    import evidence_contract as ec

    envelope = ec.render_envelope("synaptory:software-engineer") or ""
    assert "acceptance_cases" not in envelope
    block = authored_cases_target(_story())
    assert set(block) == {
        "authored_at_commit", "count", "cases", "criteria_gap_declared"
    }


# ══ 8. the DoD gate, end to end ══════════════════════════════════════════════


def test_the_gate_refuses_a_dropped_case_end_to_end(repo: Path):
    _at_commit(repo)
    sm.open_cycle(str(repo), goal="c", admitted_units=[_unit(acceptance_cases=CASES)], **CYCLE_KWARGS)
    _board(repo, _story())
    _write_receipt(repo, "WU-1", "qe", _receipt({"AC-1": {"status": "passed"}}))
    _write_receipt(repo, "WU-1", "se", {
        "agent": "software-engineer", "completed_at": "2026-01-01T02:00:00Z",
        "verification_commands": [
            {"command": "npm run build", "exit_code": 0},
            {"command": "pytest tests/ -q", "exit_code": 0},
        ],
    })
    dod = evaluate_story_dod(str(repo), "WU-1", "early")
    tp = dod["checks"]["tests_pass"]
    assert tp["passed"] is not True
    assert tp["definitive_negative"] is True
    assert "AC-2" in tp["detail"]
    typed = {i["check_id"]: i["result"] for i in dod["dod_check_results"]}
    assert typed["tests_pass"] == "fail"
    assert dod["passed"] is False


def test_the_gate_reports_a_missing_per_case_record_as_a_gap(repo: Path):
    """"Nothing here to evaluate" and "something here, and it says no" stay
    distinct results (#403). A green suite with no per-case record is the
    former."""
    _at_commit(repo)
    sm.open_cycle(str(repo), goal="c", admitted_units=[_unit(acceptance_cases=CASES)], **CYCLE_KWARGS)
    _board(repo, _story())
    _write_receipt(repo, "WU-1", "qe", _receipt(results=None))
    dod = evaluate_story_dod(str(repo), "WU-1", "early")
    tp = dod["checks"]["tests_pass"]
    assert tp["passed"] is None
    assert not tp.get("definitive_negative")
    typed = {i["check_id"]: i["result"] for i in dod["dod_check_results"]}
    assert typed["tests_pass"] == "criteria_gap_declared"


def test_the_gate_stamps_the_test_first_posture(repo: Path):
    """The fourth visibility surface. 13.3's failure was not a wrong verdict,
    it was checks emitting nothing while the gate rendered green, so a gate
    evaluated WITHOUT test-first has to say so on the record."""
    _at_commit(repo)
    sm.open_cycle(str(repo), goal="c", admitted_units=[_unit(acceptance_cases=CASES)], **CYCLE_KWARGS)
    _board(repo, _story())
    _write_receipt(repo, "WU-1", "qe", _receipt(_all_passed()))
    dod = evaluate_story_dod(str(repo), "WU-1", "early")
    tf = dod["checks"]["tests_pass"]["test_first"]
    assert tf["authored_at_commit"] is True
    assert tf["authored_case_ids"] == ["AC-1", "AC-2"]
    assert tf["criteria_gap_declared"] is None
    assert tf["producing_stage_entered_at"] == "2026-01-01T01:00:00Z"


def test_the_gate_stamps_the_migration_path_visibly(repo: Path):
    """A Cycle running without test-first is visibly running without it,
    rather than indistinguishable from one that is."""
    _at_commit(repo)
    sm.open_cycle(str(repo), goal="c", admitted_units=[_unit()], **CYCLE_KWARGS)  # pre-#406 record
    story = _story(acceptance_cases=[])
    story["criteria_gap_declared"] = {
        "unit_id": "WU-1", "declared": False, "reason": mf.LEGACY_GAP_REASON,
    }
    _board(repo, story)
    _write_receipt(repo, "WU-1", "qe", _receipt(results=None))
    dod = evaluate_story_dod(str(repo), "WU-1", "early")
    tf = dod["checks"]["tests_pass"]["test_first"]
    assert tf["authored_at_commit"] is False
    assert tf["criteria_gap_declared"]["declared"] is False
    # ...and the story is NOT blocked by the migration: old rules, old verdict.
    assert dod["checks"]["tests_pass"]["passed"] is True


# ══ 9. the SEAL is the authority, not the board ══════════════════════════════
#
# Every test below was written from an adversarial review finding. The board
# (`pipeline-state.json`) is agent-writable, so a board-only authority would
# have made the authored set exactly as forgeable as the receipt it exists to
# check, and the sealed-manifest argument in the design comments would have
# been decoration.


def test_dropping_a_case_from_the_board_does_not_shrink_the_gate(repo: Path):
    """The forgery: an agent edits the state file to remove the case it cannot
    pass, reports the rest green, and the gate clears. It does not."""
    _at_commit(repo)
    three = CASES + [_fx_unit("AC-3", statement="an admin GET returns all rows")]
    sm.open_cycle(str(repo), goal="c", admitted_units=[_unit(acceptance_cases=three)], **CYCLE_KWARGS)
    story = _story()
    story["acceptance_cases"] = list(CASES)  # AC-3 quietly removed
    _board(repo, story)
    _write_receipt(repo, "WU-1", "qe", _receipt(_all_passed()))
    dod = evaluate_story_dod(str(repo), "WU-1", "early")
    tp = dod["checks"]["tests_pass"]
    assert tp["passed"] is not True
    assert "AC-3" in tp["detail"]
    tf = tp["test_first"]
    assert tf["authored_case_ids"] == ["AC-1", "AC-2", "AC-3"]
    assert tf["authored_source"] == "manifest"
    assert tf["authored_disagreement"] == {
        "board": ["AC-1", "AC-2"], "manifest": ["AC-1", "AC-2", "AC-3"],
    }


def test_emptying_the_board_set_does_not_revert_to_the_old_rules(repo: Path):
    """The same forgery in its simplest form: delete the authored set from the
    board and inherit the pre-#406 rules with no refusal."""
    _at_commit(repo)
    sm.open_cycle(str(repo), goal="c", admitted_units=[_unit(acceptance_cases=CASES)], **CYCLE_KWARGS)
    story = _story()
    story["acceptance_cases"] = []
    _board(repo, story)
    _write_receipt(repo, "WU-1", "qe", _receipt(results=None))
    tp = evaluate_story_dod(str(repo), "WU-1", "early")["checks"]["tests_pass"]
    assert tp["passed"] is None
    assert tp["test_first"]["authored_at_commit"] is True


def test_an_unreadable_board_does_not_disable_the_check(repo: Path):
    """The fail-open a review found and I had documented rather than closed.

    `evaluate_story_dod` resolves the story inside a bare `except`, so an
    unreadable `pipeline-state.json` used to yield `story_rec = None` and
    silently revert an authored unit to the pre-#406 rules -- rendering
    identically to a legitimate migration, which defeats the whole
    four-surfaces remedy. The Cycle identity now also resolves from
    `spq_paths`' index, a different file with a different writer.
    """
    _at_commit(repo)
    sm.open_cycle(str(repo), goal="c", admitted_units=[_unit(acceptance_cases=CASES)], **CYCLE_KWARGS)
    _write_receipt(repo, "WU-1", "qe", _receipt(results=None))
    (repo / ".synaptory" / ".orchestrator" / "pipeline-state.json").write_text(
        "{ this is not json", encoding="utf-8"
    )
    tp = evaluate_story_dod(str(repo), "WU-1", "early")["checks"]["tests_pass"]
    assert tp["passed"] is None, "a green suite must not clear an authored story"
    assert tp["test_first"]["authored_at_commit"] is True
    assert tp["test_first"]["authored_case_ids"] == ["AC-1", "AC-2"]


def test_a_story_off_the_board_still_carries_its_authored_set(repo: Path):
    """`_find_story` searches `current_stories` only, so a unit that has left
    the board resolved to None and took the authored set with it."""
    _at_commit(repo)
    sm.open_cycle(str(repo), goal="c", admitted_units=[_unit(acceptance_cases=CASES)], **CYCLE_KWARGS)
    state = sm.read_state(str(repo))
    state["current_stories"] = []
    sm._write_state(str(repo), state)
    _write_receipt(repo, "WU-1", "qe", _receipt(results=None))
    tp = evaluate_story_dod(str(repo), "WU-1", "early")["checks"]["tests_pass"]
    assert tp["passed"] is None
    assert tp["test_first"]["authored_case_ids"] == ["AC-1", "AC-2"]


def test_a_tampered_manifest_is_refused_rather_than_fallen_back_from(repo: Path):
    """An unverifiable seal makes the authored set unestablishable, which is a
    gap. Falling back to the board here would reward tampering with the very
    document that makes the set tamper-evident."""
    _at_commit(repo)
    sm.open_cycle(str(repo), goal="c", admitted_units=[_unit(acceptance_cases=CASES)], **CYCLE_KWARGS)
    cycle_id = sm.identity(str(repo)).cycle_id
    path = Path(sp.manifest_path(str(repo), cycle_id))
    path.chmod(0o644)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["admitted_units"][0]["acceptance_cases"] = [
        _fx_unit("AC-1", statement="trivially true", criterion_ref="")
    ]
    path.write_text(json.dumps(body), encoding="utf-8")  # hash NOT recomputed
    _board(repo, _story())
    _write_receipt(repo, "WU-1", "qe", _receipt({"AC-1": {"status": "passed"}}))
    tp = evaluate_story_dod(str(repo), "WU-1", "early")["checks"]["tests_pass"]
    assert tp["passed"] is None
    assert "does not match its own hash" in tp["detail"]
    assert tp["test_first"]["authored_source"] == "untrusted_manifest"


def test_a_non_spq_story_never_reaches_the_manifest_path(repo: Path):
    """No Cycle, no manifest, no change. Scrum and Kanban are out of scope
    (ADR-032 section 6) and must not acquire a new dependency."""
    from story_pipeline import manifest_authored_cases

    assert manifest_authored_cases(str(repo), "US-1") is None


def test_a_pre_406_sealed_unit_is_not_given_a_position(repo: Path):
    """Key absence survives the round trip through the seal: reporting `[]`
    for a unit sealed before #406 would be the reader inventing a position the
    document does not hold, and would refuse the Cycle at the gate instead."""
    from story_pipeline import manifest_authored_cases

    _at_commit(repo)
    sm.open_cycle(str(repo), goal="c", admitted_units=[_unit()], **CYCLE_KWARGS)
    assert manifest_authored_cases(str(repo), "WU-1") is None


# ══ 10. revise_manifest admits, so it clears the same gate ═══════════════════


def test_revise_manifest_cannot_admit_a_unit_with_no_criteria(repo: Path):
    """A revision goes through admission just as surely as `open_cycle` does.

    The bypass this pins moved shape with #644 and got WORSE. A revision can
    no longer ADD a unit at all -- `C-03` closes the set at Commit, so
    `revise_manifest` refuses a new id outright -- but it can rewrite one that
    was admitted, and emptying an admitted unit's `acceptance_cases` bought
    the pre-#406 rules back for the whole Cycle: the seal is the gate's
    authority, so a seal saying the unit authored nothing is a green runner
    exit code clearing `tests_pass` again. It was also the same brick: my
    `hydrate_cycle` re-runs admission on the sealed units, so every fresh
    clone would refuse permanently and the remedy would need another revise.
    """
    _at_commit(repo)
    _open(repo, goal="c", admitted_units=[_unit(acceptance_cases=CASES)])
    cycle_id = sm.identity(str(repo)).cycle_id
    # Adding an unauthored unit: refused for being an ADDITION, before the
    # authored set is even looked at. Asserted because it is the outer wall.
    with pytest.raises(ValueError, match="cannot admit WU-9"):
        sm.revise_manifest(
            str(repo), cycle_id=cycle_id, reason="scope change",
            admitted_units=[_unit(acceptance_cases=CASES),
                            _unit("WU-9", acceptance_cases=[])],
        )
    # Stripping an ADMITTED unit's authored set: the route that survived the
    # addition wall, and the one this test exists for.
    with pytest.raises(sm.HydrationRefusal) as exc:
        sm.revise_manifest(
            str(repo), cycle_id=cycle_id, reason="scope change",
            admitted_units=[_unit(acceptance_cases=[])],
        )
    assert exc.value.code == "authored_cases_absent"
    assert "SP-WRK-007" in str(exc.value)


def test_revise_manifest_accepts_authored_or_declared_units(repo: Path):
    """The legitimate revision still works, on both admission classes."""
    _at_commit(repo)
    _open(repo, goal="c", admitted_units=[
        _unit("WU-1", acceptance_cases=CASES),
        _unit("WU-2", acceptance_cases=CASES),
    ])
    cycle_id = sm.identity(str(repo)).cycle_id
    revised = sm.revise_manifest(
        str(repo), cycle_id=cycle_id, reason="scope change",
        admitted_units=[
            _unit("WU-1", acceptance_cases=[_fx_unit("AC-9", statement="s")]),
            _unit("WU-2", acceptance_cases=[],
                  criteria_gap_declared={"reason": "spike"}),
        ],
    )
    sealed = sm.read_manifest(str(repo), cycle_id)
    assert cycle_records.verify_hash(sealed)
    assert sealed[cycle_records.HASH_FIELD] == revised["declaration_hash"]
    assert mf.admission_problems(sealed["admitted_units"]) == []
    assert mf.authored_case_ids(sealed["admitted_units"][0]) == ["AC-9"]
    # And a clone can still hydrate it: admission on the sealed set passes, so
    # the revision did not brick every checkout that has not seen it yet.
    board = Path(sp.execution_state_path(str(repo), cycle_id))
    board.unlink()
    sm.hydrate_cycle(str(repo), cycle_id=cycle_id)
    hydrated = sm.read_state(str(repo))
    assert story_authored_case_ids(hydrated["current_stories"][0]) == ["AC-9"]
    assert hydrated["test_first"]["criteria_gaps"][0]["unit_id"] == "WU-2"


# ══ 11. the strict-mode loop and the gate agree on "failed" ══════════════════


def test_the_verification_loop_sees_what_the_gate_sees(tmp_path: Path):
    """`_receipt_verification_verdict`'s docstring promises the loop and the
    gate agree on what "failed" means. Without the story threaded through,
    a QE receipt with a green suite and no per-case outcomes read as VERIFIED
    to the loop (so the story advanced out of `testing`) and as a gap to the
    gate (so it could never reach `done`). The loop exists to catch at the
    stage what the gate would reject later."""
    from story_pipeline import _receipt_verification_verdict

    rd = tmp_path / "receipts"
    rd.mkdir()
    (rd / "WU-1-qe.json").write_text(json.dumps(_receipt(results=None)))
    assert _receipt_verification_verdict(str(rd), "WU-1", "qe") is None
    assert _receipt_verification_verdict(
        str(rd), "WU-1", "qe", story=_story()
    ) == "unverified"


def test_the_loop_re_dispatches_instead_of_advancing(tmp_path: Path):
    """The observable consequence at the mature tier: the story loops rather
    than advancing on evidence the gate will refuse."""
    _write_receipt(tmp_path, "WU-1", "qe", _receipt(results=None))
    board = {"current_stories": [_story(state="testing")], "current_cycle": 6}
    action = next_action(
        board,
        receipts_dir=str(tmp_path / ".synaptory" / ".orchestrator" / "receipts"),
        verification_loops=True,
        dod_tier_info={"tier": "mature", "tier_source": "planned"},
    )
    assert action["action"] == "dispatch_qe"
    assert action["recovery"]["verdict"] == "unverified"


# ══ 12. the degraded runtime path must not disable the check ═════════════════


def test_the_fallback_normalizer_produces_the_same_ids():
    """A tree without `spq_manifest` must resolve the SAME case ids, including
    the positional fallback. The first version omitted it, so every case in
    the low-friction form `commit.md` documents (a bare string, or a dict with
    no id) normalized to an empty id, got skipped, and silently reverted the
    unit to the pre-#406 rules. A degraded path that disables the check on the
    exact shape the docs encourage is worse than none."""
    from story_pipeline import _fallback_normalize_cases

    for raw in (
        [_fx_unit("AC-1", statement="a"), _fx_unit("AC-2", statement="b")],
        [{"statement": "a"}, {"statement": "b"}],
        ["a", "b"],
    ):
        assert (
            [c["id"] for c in _fallback_normalize_cases(raw)]
            == [c["id"] for c in mf.normalize_cases(raw)]
        ), raw


# ══ 13. Sync and Checkpoint are out of scope, and stay that way ══════════════


def test_the_barrier_criteria_are_unchanged():
    """#406 touches the per-unit prove path and must not move the barrier by
    one name or one position.

    Re-pointed from `sync_barrier.CRITERIA` to `cycle_records.BARRIER_CRITERIA`
    when the barrier moved from Sync to Checkpoint (#640, `ADR-035`). Same
    guarantee, new owner: the criteria are published at Commit from ONE
    constant, because three different Sync-criteria counts shipped in one
    product while the list was restated in a prompt, a renderer and two hosts.
    """
    assert cycle_records.BARRIER_CRITERIA == (
        "admitted_set_closed",
        "path_scopes_disjoint",
        "shared_paths_owned",
        "criteria_all_returned",
        "acceptance_criteria_met",
        "regression_green",
        "trunk_integrated",
    )


def test_the_barrier_does_not_read_the_authored_set():
    """Its inputs are the admitted set and the integrated tree. An
    authored-case field on a Work Unit is not one of them, so the barrier
    cannot start depending on it by accident.

    CODE, NOT PROSE. The first cut of this grepped the raw source, so it also
    forbade explaining the boundary it enforces -- and duly failed on a
    comment naming `resolve_authored_cases` as the reader on the OTHER side of
    it (#644). A guard that cannot be documented gets deleted rather than
    understood. Identifiers, string literals and keyword arguments are the
    surface a read can actually happen through; comments and docstrings are
    not, so they stay free to name what this module must not touch.
    """
    tree = ast.parse(Path(cycle_records.__file__).read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    surface: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            surface.append(node.id)
        elif isinstance(node, ast.Attribute):
            surface.append(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            surface.append(node.arg)
        elif isinstance(node, ast.arg):
            surface.append(node.arg)
        elif isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            surface.append(node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                surface.append(node.value)

    for forbidden in ("acceptance_cases", "authored_case"):
        offenders = [name for name in surface if forbidden in name]
        assert not offenders, (
            f"`cycle_records` reaches the authored set through {offenders}; "
            "the barrier's inputs are the admitted set and the integrated tree"
        )
