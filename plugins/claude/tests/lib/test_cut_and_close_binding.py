"""Layer 1 -- the cut is a record, and the close is bound to one commitment.

TWO CLAIMS, ONE FILE, because they are the two halves of `SC-MTH-009`'s
"cutting is the one valve": a cut must be a durable accountable record that a
later Commit can name, and the close it enables must range over the admitted
set MINUS those cuts and nothing else.

WHY THE CUT CANNOT LIVE IN THE DECLARATION, since that is the design decision
these tests pin. The declaration is hash-sealed at Commit and `path_scope` is
immutable after it, so a cut written into it would either break the seal or
require resealing -- and a resealable commitment is not a commitment. The
method wants the original admitted set AND the cut history immutable, which is
two records rather than one edited twice. The cut record is COMMITTED rather
than gitignored for a separate reason: the barrier's effective set is
*admitted minus recorded cuts*, and the barrier can run in a clone that did
not make the cut.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import cycle_barrier as barrier
import cycle_records as records
import spq_state_machine as sm
import story_pipeline as story

from _spq_fixture import CYCLE_KWARGS, unit as _fx_unit


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=False)


@pytest.fixture
def cycle(tmp_path: Path, monkeypatch):
    """A Cycle with two admitted units, opened by the product."""
    project = tmp_path / "proj"
    project.mkdir()
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "t@e.co")
    _git(project, "config", "user.name", "t")
    (project / "f.txt").write_text("x", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")
    (project / ".synaptory.yaml").write_text("build_mode: spq\n", encoding="utf-8")
    monkeypatch.delenv("SYNAPTORY_ACTIVE_SPEC", raising=False)

    sm.initialize(str(project))
    sm.approve_baseline(
        str(project),
        approved_by="lead@h3t.co",
        baseline_ref="baseline-1",
        calibration={"sample_units": 2, "measured_hours": 8},
    )
    sm.open_cycle(
        str(project),
        goal="cycle 1",
        admitted_units=[_fx_unit("WU-01"), _fx_unit("WU-02")],
        **CYCLE_KWARGS,
    )
    return project, sm.identity(str(project)).cycle_id


# ══ 1. The cut is a record, and the record is where it can be read ══════════


def test_a_cut_writes_a_committed_record(cycle):
    project, cycle_id = cycle
    assert sm.read_cuts(str(project), cycle_id) == []
    sm.cut_work_unit(
        str(project), "WU-02", "the client deferred the export view",
        cut_by="lead@h3t.co",
    )
    cuts = sm.read_cuts(str(project), cycle_id)
    assert len(cuts) == 1
    assert cuts[0]["unit_id"] == "WU-02"
    assert cuts[0]["reason"] == "the client deferred the export view"
    assert cuts[0]["readmitted"] is None


def test_the_cut_record_is_committed_not_gitignored(cycle):
    """A clone that did not make the cut must be able to read it, or it
    computes an effective set still containing work somebody withdrew."""
    import spq_paths

    project, cycle_id = cycle
    sm.cut_work_unit(str(project), "WU-02", "deferred", cut_by="lead@h3t.co")
    path = spq_paths.committed_cuts_path(str(project), cycle_id)
    assert Path(path).is_file()
    check = subprocess.run(
        ["git", "check-ignore", "-q", str(Path(path).relative_to(project))],
        cwd=str(project),
        capture_output=True,
    )
    assert check.returncode != 0, (
        "the cut record is gitignored, so a clone that did not make the cut "
        "sees the withdrawn unit as still admitted"
    )


def test_the_declaration_is_untouched_by_a_cut(cycle):
    """The seal is the commitment. A cut adds a record beside it."""
    project, cycle_id = cycle
    before = sm.read_manifest(str(project), cycle_id)
    sm.cut_work_unit(str(project), "WU-02", "deferred", cut_by="lead@h3t.co")
    after = sm.read_manifest(str(project), cycle_id)
    assert after == before, "the cut edited the sealed declaration"
    assert records.verify_hash(after), "the cut broke the seal"
    assert "cuts" not in after, (
        "the cut history landed in the hashed body, so recording one requires "
        "resealing the commitment it is measured against"
    )


def test_a_retried_cut_is_the_same_cut(cycle):
    """Two records for one withdrawal would make `SC-MTH-015`'s cut-rate
    numerator count a retry as a second cut."""
    project, cycle_id = cycle
    first = sm.cut_work_unit(
        str(project), "WU-02", "deferred", cut_by="lead@h3t.co"
    )
    second = sm.cut_work_unit(
        str(project), "WU-02", "deferred again", cut_by="lead@h3t.co"
    )
    assert first["readmission_key"] == second["readmission_key"]
    assert len(sm.read_cuts(str(project), cycle_id)) == 1


def test_a_cut_shrinks_the_barriers_subject_and_not_the_denominator(cycle):
    """Both at once, because they point opposite ways. The effective set the
    barrier ranges over loses the unit; the count taken at Commit does not --
    `SC-MTH-015`'s denominator is the ORIGINAL admission, and the
    predecessor's `declare_ready` computed it after excluding cancelled units,
    so its archived cut-rate denominator had already shrunk."""
    project, cycle_id = cycle
    result = sm.cut_work_unit(
        str(project), "WU-02", "deferred", cut_by="lead@h3t.co"
    )
    assert result["effective_set"] == ["WU-01"]
    sealed = sm.read_manifest(str(project), cycle_id)
    assert len(sealed["admitted_units"]) == 2


def test_a_cut_needs_a_reason(cycle):
    project, _ = cycle
    with pytest.raises(barrier.BarrierError, match="explicit reason"):
        sm.cut_work_unit(str(project), "WU-02", "   ", cut_by="lead@h3t.co")


def test_a_finished_unit_cannot_be_cut(cycle):
    """A cut is the valve for UNFINISHED work: cutting a proven result would
    discard it and make the Cycle's throughput unreadable."""
    project, cycle_id = cycle
    state = sm.read_state(str(project))
    next(u for u in state["current_stories"] if u["id"] == "WU-02")["state"] = "done"
    sm._write_state(str(project), state)
    with pytest.raises(barrier.BarrierError, match="UNFINISHED"):
        sm.cut_work_unit(str(project), "WU-02", "too late", cut_by="lead@h3t.co")


def test_an_unadmitted_unit_is_backlog_not_a_cut(cycle):
    project, _ = cycle
    with pytest.raises(barrier.BarrierError, match="not in this Cycle's admitted set"):
        sm.cut_work_unit(str(project), "WU-99", "never admitted", cut_by="l@h3t.co")


def _checkpoint_the_region(project: Path, cycle_id: str) -> None:
    """Release the region, as a Checkpoint does.

    A NEW COMMIT CANNOT CLAIM A REGION THE PREVIOUS CYCLE STILL HOLDS -- the
    registry refuses it, which is the whole point of `SC-MTH-012`. The tests
    below open a second Cycle without closing the first, because what they are
    about is the CUT LINK rather than the Checkpoint; this stands in for the
    close they skip. Without it they fail on a refusal that is the product
    working, and papering over that by giving the second Cycle a different
    region would hide the constraint instead of respecting it.
    """
    import region_registry

    region_registry.release(str(project), cycle_id=cycle_id)


# ══ 2. Re-admission links the cut, or it is a silent carryover ══════════════


def test_the_next_commit_links_the_cut_it_re_admits(cycle):
    project, first_cycle = cycle
    cut = sm.cut_work_unit(
        str(project), "WU-02", "deferred", cut_by="lead@h3t.co"
    )
    _checkpoint_the_region(project, first_cycle)
    sm.open_cycle(
        str(project),
        goal="cycle 2",
        admitted_units=[_fx_unit("WU-02")],
        readmits=[cut["readmission_key"]],
        **CYCLE_KWARGS,
    )
    linked = sm.read_cuts(str(project), first_cycle)
    assert len(linked) == 1, "the re-admission added a record instead of linking"
    assert linked[0]["readmitted"], "the cut is still unlinked"
    assert linked[0]["readmitted"]["cycle_id"] != first_cycle
    # The cut itself is unchanged: a mutable cut is a rewritable reason.
    assert linked[0]["reason"] == cut["reason"]
    assert linked[0]["cut_at"] == cut["cut_at"]


def test_a_readmission_citing_a_key_nobody_issued_is_refused(cycle):
    """A key nobody issued links to nothing, which is the same record as a
    silent carryover -- the state the predecessor left every cut in."""
    project, first_cycle = cycle
    _checkpoint_the_region(project, first_cycle)
    with pytest.raises(ValueError, match="no recorded cut issued it"):
        sm.open_cycle(
            str(project),
            goal="cycle 2",
            admitted_units=[_fx_unit("WU-02")],
            readmits=["cut:1-deadbeef:WU-02"],
            **CYCLE_KWARGS,
        )


def test_a_readmission_that_does_not_admit_the_unit_is_refused(cycle):
    """Naming the cut without admitting the unit records a carryover that
    never happened."""
    project, first_cycle = cycle
    cut = sm.cut_work_unit(
        str(project), "WU-02", "deferred", cut_by="lead@h3t.co"
    )
    _checkpoint_the_region(project, first_cycle)
    with pytest.raises(barrier.BarrierError, match="does not admit"):
        sm.open_cycle(
            str(project),
            goal="cycle 2",
            admitted_units=[_fx_unit("WU-07")],
            readmits=[cut["readmission_key"]],
            **CYCLE_KWARGS,
        )


# ══ 3. The close derives its verdict and proves its promotion ══════════════
#
# WHAT STOOD HERE, AND WHY IT WAS THE HOLE RATHER THAN COVERAGE. Three tests
# probed `close_cycle`'s `barrier_verdict` ARGUMENT: a verdict naming another
# Cycle, one produced against a superseded declaration, one naming no
# criteria. Each refusal was real. All three took for granted that a caller
# supplies the verdict, and built a `_green()` helper to do it -- so the file
# demonstrated that a five-field dict closes a Cycle with no barrier
# evaluation, no observed trunk and no promotion behind it. A review named
# this file as evidence of the defect, correctly.
#
# `close_cycle` now takes no verdict and no `integrated_sha`. It derives the
# verdict from `run_barrier` and `cycle_barrier.close` requires a successful
# promotion under that verdict's own operation identity. The three shape
# refusals are gone with the parameter they guarded -- there is no longer a
# shape to get wrong -- and what replaces them is the sequence itself.


def _drive_to_done(project: Path, unit_id: str) -> None:
    """Take one Work Unit through the receipt-gated walk to `done`.

    Through `execute_advance`, not by writing `state: done` on the board. The
    barrier reads per-criterion results out of the DoD gate, and a unit set
    `done` by hand has a gate that never ran -- which would make every
    assertion below a statement about the fixture.
    """
    import advance_kernel as ak

    policy = ak.HostPolicy(host="test", require_next_action_match=False)
    walk = (
        ("in_progress", "software-engineer", "se", "se-implementation", 1),
        ("testing", "software-engineer", "se", "se-implementation", 2),
        ("reviewing", "quality-engineer", "qe", "qe-verification", 1),
        ("done", "code-reviewer", "cr", "cr-review", 1),
    )
    receipts = Path(story.receipts_dir_for(str(project), intended=True))
    receipts.mkdir(parents=True, exist_ok=True)
    for target, role, abbrev, stage, nth in walk:
        artifact = project / "api" / unit_id.lower() / "impl.py"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("# %s %d\n" % (abbrev, nth), encoding="utf-8")
        (receipts / ("%s-%s.json" % (unit_id, abbrev))).write_text(
            json.dumps({
                "story_id": unit_id,
                "role": role,
                "backend": "claude",
                "model": "test-fixture",
                "task": "%s for %s" % (stage, unit_id),
                "artifacts": ["api/%s/impl.py" % unit_id.lower()],
                "verification_commands": [
                    {"command": "pytest -q", "exit_code": 0, "summary": "ok"}
                ],
                "metrics": {"n": nth, "tests_passed": 1, "tests_failed": 0},
                # `status: complete` is what `code_reviewed` READS
                # (`DOD_CHECKS["code_reviewed"]["receipt_field"]`), and tier
                # `growing` requires that check. Without it the CR receipt
                # exists, the edge passes on its presence, and the DoD then
                # reports `code_reviewed: false` -- so the barrier's
                # `acceptance_criteria_met` is unmet and the sequence below
                # cannot reach a close. Written for every role rather than
                # only `cr` because it is the receipt template's own field and
                # a role-specific fixture would encode a rule the product does
                # not have.
                "status": "complete",
                "completed_at": _now_iso(),
            }, indent=1),
            encoding="utf-8",
        )
        decision = ak.execute_advance(str(project), unit_id, target, policy=policy)
        assert decision.allowed, (target, decision.code, decision.reason)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def promotable(tmp_path: Path, monkeypatch):
    """A Cycle whose barrier can go green: one unit proved, a regression that
    passes, and a trunk the candidate is ahead of."""
    project = tmp_path / "proj"
    project.mkdir()
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "t@e.co")
    _git(project, "config", "user.name", "t")
    (project / "reg.sh").write_text(
        "#!/bin/bash\necho '412 passed'\nexit 0\n", encoding="utf-8"
    )
    (project / ".synaptory.yaml").write_text(
        "build_mode: spq\n"
        "quality:\n  dod_tier: growing\n"
        "spq:\n  regression_script: \"reg.sh\"\n",
        encoding="utf-8",
    )
    # THE LAYOUT THE RUNBOOK SPECIFIES, and the fixture needs it for a reason
    # the runbook does not state: without it `git add -A` commits the
    # orchestrator tree, so checking out a branch MOVES THE BOARD. The barrier
    # then read one Cycle's state and git another's, and `candidate_sha` came
    # back as a revision from a branch nobody was on.
    #
    # `.synaptory/*` and not `.synaptory/`: git cannot re-include a child of a
    # directory excluded as a whole. `!.synaptory/cycles/` keeps the sealed
    # declaration, the cut record and the barrier ledger visible across
    # clones, which is what `open_cycle` refuses to seal without.
    (project / ".gitignore").write_text(
        ".synaptory/*\n!.synaptory/cycles/\n", encoding="utf-8"
    )
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")
    _git(project, "branch", "-M", "dev")
    monkeypatch.delenv("SYNAPTORY_ACTIVE_SPEC", raising=False)

    sm.initialize(str(project))
    sm.approve_baseline(
        str(project), approved_by="lead@h3t.co", baseline_ref="baseline-1",
        calibration={"sample_units": 2, "measured_hours": 8},
    )
    sm.open_cycle(
        str(project), goal="cycle 1", admitted_units=[_fx_unit("WU-01")],
        **CYCLE_KWARGS,
    )
    cycle_id = sm.identity(str(project)).cycle_id
    _drive_to_done(project, "WU-01")
    # The candidate: the proved work committed, ahead of the trunk. Nothing is
    # merged yet, so `trunk_integrated` is legitimately unmet -- which is the
    # state `promote` expects and `close` refuses.
    _git(project, "checkout", "-q", "-b", "candidate")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "WU-01")
    return project, cycle_id


def test_a_close_before_the_promotion_is_refused(promotable):
    """The keystone. Nothing supplies a verdict, so the only way to close is
    to have actually promoted -- and `trunk_integrated` is unmet until the
    merge lands, which is the correct pre-promotion state rather than a
    failure."""
    project, _ = promotable
    with pytest.raises(barrier.BarrierError) as refusal:
        sm.close_cycle(str(project), principal="lead@h3t.co")
    assert "not met" in str(refusal.value) or "promotion" in str(refusal.value)


def test_the_documented_sequence_reaches_a_close(promotable):
    """evaluate -> promote -> merge -> re-evaluate -> close, on a real repo.

    THE REVIEW REPRODUCED THIS FAILING. `operation_id` included
    `observed_sha`, which is `merge-base(candidate, trunk)`: the base before
    the merge, and the candidate itself after it. So the re-evaluation at the
    last step computed a different identity and `close` could not find its own
    promotion, ending in "no successful promotion is recorded". A verdict
    identity that changes because the operation succeeded is not an identity.
    """
    project, cycle_id = promotable

    before = sm.run_barrier(str(project))
    assert before["green"] is False
    assert before["unmet"] == ["trunk_integrated"], before["unmet"]

    promotion = sm.promote_cycle(str(project), principal="lead@h3t.co")
    assert promotion["outcome"] == "promoted"

    # The authorized human-controlled mechanism. A fast-forward is the
    # smallest thing that is genuinely a trunk update.
    _git(project, "checkout", "-q", "dev")
    _git(project, "merge", "-q", "--ff-only", "candidate")

    after = sm.run_barrier(str(project))
    assert after["green"] is True, after["unmet"]
    assert after["operation_id"] == before["operation_id"], (
        "the operation identity moved across the promotion, so the close "
        "cannot find the promotion it just authorized"
    )

    closed = sm.close_cycle(str(project), principal="lead@h3t.co")
    assert closed["ok"] is True
    recorded = sm.read_state(str(project))["cycles_completed"][-1]
    assert recorded["integrated_sha"], recorded
    ledger = sm.read_barrier_ledger(str(project), cycle_id)
    assert [r["kind"] for r in ledger] == ["promotion", "close"], ledger


def test_a_merge_commit_checkpoint_keeps_the_operation_identity(promotable):
    """The same close, merged with `--no-ff`. This is how most teams merge.

    The fast-forward arm above passed while this one could not: `candidate_sha`
    came from HEAD, so a merge commit moved the candidate identity, the
    re-evaluation computed a different `operation_id`, and the close ended in
    "no successful promotion is recorded". An identity that changes because the
    operation succeeded is not an identity -- the review's own words about
    `observed_sha`, surviving one field over.

    NO FIXTURE BRANCH SETUP, and that is the point of this version. The first
    fix read the candidate from `cycle/<id>/integration`, which nothing in the
    shipped ceremony creates -- so the real path still fell back to HEAD and
    only a test that made the ref itself passed. The candidate is now pinned
    from the promotion record, which the product writes on its own, so this
    test walks exactly the sequence `checkpoint.md` documents.
    """
    project, cycle_id = promotable

    before = sm.run_barrier(str(project))
    assert before["trunk"]["candidate_from"] == "HEAD", before["trunk"]
    assert before["unmet"] == ["trunk_integrated"], before["unmet"]
    sm.promote_cycle(str(project), principal="lead@h3t.co")

    # A merge commit, and the operator standing on the trunk afterwards --
    # which is where `git merge` leaves them.
    _git(project, "checkout", "-q", "dev")
    _git(project, "merge", "-q", "--no-ff", "-m", "checkpoint", "candidate")

    after = sm.run_barrier(str(project))
    assert after["trunk"]["candidate_from"] == "promotion", after["trunk"]
    assert after["green"] is True, after["unmet"]
    assert after["operation_id"] == before["operation_id"], (
        "the merge commit moved the operation identity, so the close cannot "
        "find the promotion it just authorized"
    )
    assert sm.close_cycle(str(project), principal="lead@h3t.co")["ok"] is True


def test_a_promotion_of_one_candidate_does_not_bless_another(promotable):
    """Pinning from the promotion cannot be circular, and this is why.

    The candidate is pinned so the re-evaluation is about the commit somebody
    authorized. If that made `trunk_integrated` true by construction the pin
    would be a rubber stamp -- so: promote X, merge something else, and X is
    still not an ancestor of the trunk.
    """
    project, _cycle_id = promotable
    sm.run_barrier(str(project))
    promotion = sm.promote_cycle(str(project), principal="lead@h3t.co")
    promoted = promotion["trunk"]["candidate_sha"]

    # The trunk moves, but not to this candidate.
    _git(project, "checkout", "-q", "dev")
    (project / "unrelated.txt").write_text("elsewhere\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "something else entirely")

    after = sm.run_barrier(str(project))
    assert after["trunk"]["candidate_sha"] == promoted, after["trunk"]
    assert "trunk_integrated" in after["unmet"], after["unmet"]


def test_a_revision_does_not_inherit_the_previous_promotion(promotable):
    """The pin is matched on the declaration hash, not only the Cycle id.

    A revision changes the admitted set the barrier ranges over, so a
    promotion of the previous revision authorized a different commitment.
    Carrying its candidate into the new one would let the earlier
    authorization stand in for one nobody gave.
    """
    project, cycle_id = promotable
    sm.run_barrier(str(project))
    sm.promote_cycle(str(project), principal="lead@h3t.co")
    sealed = sm.read_manifest(str(project), cycle_id)

    stale = dict(sealed)
    stale[records.HASH_FIELD] = "sha256:" + "0" * 64
    assert sm._promoted_candidate(str(project), stale) == ""
    # And the real declaration still resolves its own promotion.
    assert sm._promoted_candidate(str(project), sealed)


def test_a_second_close_reconciles_rather_than_closing_twice(promotable):
    """§4.3 step 7: a crash between the promotion and the local record must
    reconcile the SAME operation. A retry that closed again would record two
    closes for one integration."""
    project, cycle_id = promotable
    sm.promote_cycle(str(project), principal="lead@h3t.co")
    _git(project, "checkout", "-q", "dev")
    _git(project, "merge", "-q", "--ff-only", "candidate")
    first = sm.close_cycle(str(project), principal="lead@h3t.co")
    assert not first.get("reconciled"), first
    again = sm.close_cycle(str(project), principal="lead@h3t.co")
    assert again["reconciled"] is True, again
    ledger = sm.read_barrier_ledger(str(project), cycle_id)
    assert [r["kind"] for r in ledger].count("close") == 1, ledger
    # THE ARCHIVE TOO, not just the ledger. `cycles_completed` is what
    # `SC-MTH-015` measures throughput from, and a retry that appended a
    # second entry would credit one integration twice.
    completed = sm.read_state(str(project))["cycles_completed"]
    assert len(completed) == 1, completed
    events = [
        e for e in sm.read_state(str(project)).get("method_events") or []
        if e.get("kind") == "checkpoint"
    ]
    assert len(events) == 1, events


def test_close_cycle_accepts_no_verdict_and_no_revision(promotable):
    """The absence is the security property, so it is asserted rather than
    left to a reader. A parameter here would put the caller back in the
    position of stating whether the barrier passed and what reached the
    trunk -- and on the MCP surface that caller is the agent being graded."""
    import inspect

    signature = inspect.signature(sm.close_cycle)
    assert "barrier_verdict" not in signature.parameters, signature
    assert "integrated_sha" not in signature.parameters, signature
    assert set(signature.parameters) == {
        "project_dir", "principal", "rationale", "cycle_id"
    }, signature


def test_a_close_records_the_cut_from_the_record_not_the_board(promotable):
    """The board's `cancelled` state is agent-writable, so a unit could be set
    `cancelled` with no accountable cut ever recorded -- crediting the Cycle
    with a withdrawal nobody authorized.

    Driven through the real sequence now: with the verdict derived, reaching a
    close at all requires the promotion, so this can no longer be asserted
    against a hand-built green."""
    project, cycle_id = promotable
    sm.promote_cycle(str(project), principal="lead@h3t.co")
    _git(project, "checkout", "-q", "dev")
    _git(project, "merge", "-q", "--ff-only", "candidate")
    sm.close_cycle(str(project), principal="lead@h3t.co")
    recorded = sm.read_state(str(project))["cycles_completed"][-1]
    assert recorded["work_units_cut"] == 0, recorded
    assert recorded["admitted_at_commit"] == 1


# ══ 4. The verdict has a producer, and it derives its own facts ═════════════
#
# `close_cycle` requires a `barrier_verdict` and `cycle_barrier.evaluate` was
# reachable from no CLI verb and no MCP tool, so a Cycle could be opened,
# executed and proved and then closed by nothing. Same class as
# `revise_manifest`'s missing ingress, one step further along: here the
# missing route was the only way the lifecycle ends.


def _regression(project: Path, *, exit_code: int = 0) -> None:
    (project / "reg.sh").write_text(
        "#!/bin/bash\necho '412 passed'\nexit %d\n" % exit_code, encoding="utf-8"
    )
    (project / ".synaptory.yaml").write_text(
        'build_mode: spq\nspq:\n  regression_script: "reg.sh"\n', encoding="utf-8"
    )


def test_run_barrier_returns_a_verdict_over_the_effective_set(cycle):
    project, _ = cycle
    _regression(project)
    verdict = sm.run_barrier(str(project))
    assert verdict["published_criteria"] == list(records.BARRIER_CRITERIA)
    assert set(verdict["criteria"]) >= set(records.BARRIER_CRITERIA[:3])


def test_run_barrier_executes_the_regression_rather_than_believing_one(cycle):
    """A proof the caller supplies is a proof the graded principal wrote. This
    verb takes no facts at all, so the only way `regression_green` passes is
    for the configured script to exit 0."""
    project, _ = cycle
    _regression(project, exit_code=1)
    verdict = sm.run_barrier(str(project))
    assert verdict["criteria"]["regression_green"]["passed"] is False
    _regression(project, exit_code=0)
    assert sm.run_barrier(str(project))["criteria"]["regression_green"]["passed"] is True


def test_an_unconfigured_regression_is_skipped_and_a_skip_is_not_a_pass(cycle):
    project, _ = cycle
    (project / ".synaptory.yaml").write_text("build_mode: spq\n", encoding="utf-8")
    result = sm.run_barrier(str(project))["criteria"]["regression_green"]
    assert result["passed"] is False
    assert "skipped" in result["detail"] or "regression" in result["detail"]


def test_run_barrier_observes_the_trunk_rather_than_taking_an_observation(cycle):
    """`candidate_is_ancestor_of_trunk` is the whole integration claim, and a
    caller able to set it is a caller able to claim an integration."""
    project, _ = cycle
    _regression(project)
    import inspect

    signature = inspect.signature(sm.run_barrier)
    assert set(signature.parameters) == {"project_dir", "cycle_id"}, (
        "`run_barrier` grew a parameter. Every fact must be derived inside it: "
        "a verb accepting a trunk observation, a regression proof or a unit "
        "result pushes the caller-supplied-verdict hole one layer out of the "
        "barrier and into the CLI: %s" % signature
    )
    detail = sm.run_barrier(str(project))["criteria"]["trunk_integrated"]["detail"]
    assert "ancestor" in detail


def test_run_barrier_refuses_a_project_with_no_cycle(cycle, tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / ".synaptory.yaml").write_text("build_mode: spq\n", encoding="utf-8")
    sm.initialize(str(bare))
    with pytest.raises(ValueError, match="no admitted set"):
        sm.run_barrier(str(bare))


# ══ 5. Only the FINAL Acceptance ends the engagement ═══════════════════════
#
# `check_transition` allows `ACCEPTANCE -> COMPLETE` because the final
# Acceptance is exactly what that edge is for. Nothing asked whether THIS
# Acceptance is the final one, so any Acceptance could end the engagement with
# commitments outstanding and nothing handed over. The shipped ceremony ran the
# check in prose, which is advice: every host reads a different prompt and none
# of them is a gate.
#
# The guard and its write path land together on purpose. Consulting
# `assert_close_permitted` without a verb that can record a handover would have
# traded a premature close for an engagement that can never end -- and both
# halves being missing is why the prose version looked adequate.


@pytest.fixture
def at_acceptance(promotable):
    project, cycle_id = promotable
    state = sm.read_state(str(project))
    for unit in state["current_stories"]:
        unit["state"] = "done"
    sm._write_state(str(project), state)
    sm.transition(str(project), "ACCEPTANCE")
    return project, cycle_id


def _record_handover(project: Path) -> None:
    sm.record_handover(
        str(project),
        recorded_by="lead@h3t.co",
        codebase="rev abc1234",
        documentation="docs/runbook.md",
        operating_knowledge="docs/operations.md",
    )


def test_an_acceptance_with_no_handover_cannot_close_the_engagement(at_acceptance):
    import acceptance_record

    project, _ = at_acceptance
    with pytest.raises(acceptance_record.AcceptanceError) as refusal:
        sm.transition(str(project), "COMPLETE")
    # The refusal NAMES what is missing, because "not permitted" leaves an
    # operator guessing between three different remedies.
    message = str(refusal.value)
    for item in acceptance_record.HANDOVER_ITEMS:
        assert item in message, message


def test_a_recorded_handover_makes_the_final_close_reachable(at_acceptance):
    """The other direction, and it is the one that proves the guard is a gate
    rather than a wall."""
    project, cycle_id = at_acceptance
    _record_handover(project)
    _checkpoint_the_region(project, cycle_id)
    assert sm.transition(str(project), "COMPLETE")["lifecycle_state"] == "COMPLETE"


def test_a_partial_handover_is_not_a_handover(at_acceptance):
    """An AND, not a count. An engagement closed without the operating
    knowledge has handed over a codebase nobody can run."""
    project, _ = at_acceptance
    with pytest.raises(ValueError, match="operating_knowledge"):
        sm.record_handover(
            str(project),
            recorded_by="lead@h3t.co",
            codebase="rev abc1234",
            documentation="docs/runbook.md",
            operating_knowledge="   ",
        )


def test_a_handover_names_the_human_who_made_it(at_acceptance):
    project, _ = at_acceptance
    with pytest.raises(ValueError, match="attributable to nobody"):
        sm.record_handover(
            str(project),
            recorded_by="",
            codebase="rev abc1234",
            documentation="docs/runbook.md",
            operating_knowledge="docs/operations.md",
        )


def test_the_handover_lives_on_the_engagement_not_a_cycle_board(at_acceptance):
    """It outlives every Cycle, and `open_cycle` replaces a board. Storing it
    there would put the engagement's terminal condition inside a record the
    next Commit overwrites."""
    project, cycle_id = at_acceptance
    _record_handover(project)
    _checkpoint_the_region(project, cycle_id)
    assert sm.read_engagement(str(project))["handover"]["recorded_by"] == "lead@h3t.co"
    sm.open_cycle(
        str(project), goal="cycle 2", admitted_units=[_fx_unit("WU-09")],
        **CYCLE_KWARGS,
    )
    assert sm.read_engagement(str(project))["handover"]["recorded_by"] == "lead@h3t.co", (
        "opening a Cycle lost the handover, so the engagement's terminal "
        "condition is stored somewhere a Commit can erase"
    )


# ══ 6. A condition stronger than `done` has a verifier, or is not sealed ════
#
# `spq_ledger.verify_condition` recomputes a `contract_published` /
# `artifact_published` digest with `manifest["verification"]["digest_script"]`.
# No declaration could carry that block -- `cycle_records` had no `verification`
# concept at all -- so the recompute branch returned `verified: None` for every
# project, forever, and the consumer sat at `dep_condition_unverified` with
# nothing anyone could do about it. Every dependency condition stronger than
# `done` was decorative.
#
# The two halves land together for the reason they always do here: sealing the
# script without refusing a declaration that omits it leaves the old silent
# failure reachable by simply not passing it, and refusing without a way to
# seal one makes the strong conditions unusable instead of merely inert.


def _digest_project(tmp_path: Path, monkeypatch, *, script: bool = True) -> Path:
    monkeypatch.chdir(tmp_path)
    project = tmp_path / "proj"
    (project / "contracts").mkdir(parents=True)
    (project / "contracts" / "auth").write_text("v1\n", encoding="utf-8")
    (project / ".synaptory.yaml").write_text("build_mode: spq\n", encoding="utf-8")
    (project / ".gitignore").write_text(
        ".synaptory/*\n!.synaptory/cycles/\n", encoding="utf-8"
    )
    if script:
        digest = project / "digest.sh"
        digest.write_text(
            '#!/bin/bash\nshasum -a 256 "$1" | cut -d" " -f1\n', encoding="utf-8"
        )
        digest.chmod(0o755)
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "t@h3t.co")
    _git(project, "config", "user.name", "T")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "initial")
    sm.initialize(str(project))
    sm.approve_baseline(
        str(project),
        approved_by="lead@h3t.co",
        baseline_ref="baseline-1",
        calibration={"sample_units": 1, "measured_hours": 1},
    )
    return project


_PRODUCER = _fx_unit("WU-UP", outputs=[{"kind": "contract", "id": "contracts/auth"}])
_CONSUMER = _fx_unit(
    "WU-DOWN",
    depends_on=[{"unit_id": "WU-UP", "condition": "contract_published"}],
)


def _digest_of(project: Path, path: str) -> str:
    done = subprocess.run(
        ["bash", "digest.sh", path],
        cwd=str(project),
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def test_a_digest_verified_edge_without_a_verifier_is_refused_at_commit(
    tmp_path, monkeypatch
):
    project = _digest_project(tmp_path, monkeypatch)
    with pytest.raises(ValueError) as caught:
        sm.open_cycle(
            str(project),
            goal="ship auth",
            admitted_units=[_PRODUCER, _CONSUMER],
            **CYCLE_KWARGS,
        )
    assert "digest_script" in str(caught.value)


def test_a_verifier_naming_a_script_outside_the_project_is_refused(
    tmp_path, monkeypatch
):
    project = _digest_project(tmp_path, monkeypatch)
    with pytest.raises(ValueError) as caught:
        sm.open_cycle(
            str(project),
            goal="ship auth",
            admitted_units=[_PRODUCER, _CONSUMER],
            verification={"digest_script": "../digest.sh"},
            **CYCLE_KWARGS,
        )
    assert "escap" in str(caught.value).lower() or "outside" in str(caught.value)


def test_an_edge_no_stronger_than_done_needs_no_verifier(tmp_path, monkeypatch):
    project = _digest_project(tmp_path, monkeypatch)
    sm.open_cycle(
        str(project),
        goal="ship auth",
        admitted_units=[
            _fx_unit("WU-UP"),
            _fx_unit(
                "WU-DOWN", depends_on=[{"unit_id": "WU-UP", "condition": "done"}]
            ),
        ],
        **CYCLE_KWARGS,
    )


def test_the_sealed_verifier_is_inside_the_declaration_hash(tmp_path, monkeypatch):
    project = _digest_project(tmp_path, monkeypatch)
    sm.open_cycle(
        str(project),
        goal="ship auth",
        admitted_units=[_PRODUCER, _CONSUMER],
        verification={"digest_script": "digest.sh"},
        **CYCLE_KWARGS,
    )
    cycle_id = sm.identity(str(project)).cycle_id
    manifest = sm.read_manifest(str(project), cycle_id)
    assert manifest["verification"] == {"digest_script": "digest.sh"}
    tampered = json.loads(json.dumps(manifest))
    tampered["verification"] = {"digest_script": "lies.sh"}
    assert records.compute_hash(tampered) != records.compute_hash(manifest)


def test_a_published_digest_is_recomputed_and_the_consumer_unblocks(
    tmp_path, monkeypatch
):
    project = _digest_project(tmp_path, monkeypatch)
    sm.open_cycle(
        str(project),
        goal="ship auth",
        admitted_units=[_PRODUCER, _CONSUMER],
        verification={"digest_script": "digest.sh"},
        **CYCLE_KWARGS,
    )
    published = sm.publish_event(
        str(project),
        unit_id="WU-UP",
        condition="contract_published",
        output={
            "kind": "contract",
            "id": "contracts/auth",
            "digest": _digest_of(project, "contracts/auth"),
        },
    )
    event = published.get("event") or published
    assert event["verification"]["verified"] is True

    # The event exists but is untracked, so no other clone can see it: the
    # honest answer is "not pushed", not "not published".
    blocked = _consumer(sm.dep_status(str(project), unit_id="WU-DOWN"))
    assert blocked["dependencies_met"] is False
    assert blocked["unmet"][0]["reason_code"] == "dep_event_local_only"

    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "publish the contract event")
    sm.refresh_ledger(str(project))
    cleared = _consumer(sm.dep_status(str(project), unit_id="WU-DOWN"))
    assert cleared["dependencies_met"] is True, cleared.get("unmet")


def test_a_digest_that_does_not_match_the_artifact_is_refused_at_publish(
    tmp_path, monkeypatch
):
    import spq_ledger

    project = _digest_project(tmp_path, monkeypatch)
    sm.open_cycle(
        str(project),
        goal="ship auth",
        admitted_units=[_PRODUCER, _CONSUMER],
        verification={"digest_script": "digest.sh"},
        **CYCLE_KWARGS,
    )
    with pytest.raises(spq_ledger.LedgerError) as caught:
        sm.publish_event(
            str(project),
            unit_id="WU-UP",
            condition="contract_published",
            output={"kind": "contract", "id": "contracts/auth", "digest": "0" * 64},
        )
    assert "does not match the recomputed" in str(caught.value)
    # And the consumer is no better off than before the false claim.
    blocked = _consumer(sm.dep_status(str(project), unit_id="WU-DOWN"))
    assert blocked["dependencies_met"] is False


def _consumer(status: dict) -> dict:
    for unit in status.get("units") or ():
        if unit.get("unit_id") == "WU-DOWN":
            return unit
    raise AssertionError("dep_status reported no WU-DOWN: %r" % (status,))


# ══ 7. The retired close arguments refuse by name, not in silence ═══════════
#
# `close_cycle` derives the verdict and observes the trunk, so neither
# `--barrier-verdict` nor `--integrated-sha` has anywhere to go. Dropping an
# unknown flag in silence closes the Cycle and leaves the operator believing
# the verdict they assembled was the one recorded -- so the next time the
# barrier disagrees with their own notes, the product looks wrong. Someone
# passing these is following the retired sequence and needs to be told once.


@pytest.mark.parametrize(
    "flag, marker",
    [("--barrier-verdict", "derives it"), ("--integrated-sha", "observed")],
)
def test_a_retired_close_argument_is_refused_by_name(tmp_path, flag, marker):
    import subprocess as sp
    import sys

    done = sp.run(
        [sys.executable, "core/lib/spq_state_machine.py", "close_cycle",
         str(tmp_path), flag, "x"],
        cwd=str(Path(__file__).resolve().parents[3]),
        capture_output=True,
        text=True,
    )
    # The refusal goes to STDERR with a non-zero status, like every other
    # CLI-level refusal here: stdout is the verb's result channel, and a
    # refusal printed there is a result.
    assert done.returncode != 0, done.stdout
    body = json.loads(done.stderr)
    assert body["ok"] is False
    assert flag in body["error"] and marker in body["error"], body


# ══ 8. A coherent declaration is not a candidate that respected it ══════════
#
# §5.2 asks for the ACTUAL changed paths to be validated against the
# declaration before promotion. Every other path check here is a statement
# about the declaration -- scopes disjoint, shared paths owned, cut code
# excluded -- and a declaration can be perfectly coherent while the candidate
# wandered outside every region in it. The region registry cannot catch it
# either: the registry keeps two CYCLES apart and says nothing about whether
# this one stayed inside the region it was granted.


def _verdict_over(project: Path, cycle_id: str, changed: list[str]) -> dict:
    """The barrier's own verdict, with `changed_paths` substituted.

    Through `cycle_barrier.evaluate` with the real declaration and real unit
    results, so what varies between the two arms below is only what the
    candidate touched.
    """
    sealed = sm.read_manifest(str(project), cycle_id)
    results = sm._unit_results(str(project), sm.read_state(str(project)))
    return barrier.evaluate(
        declaration=sealed,
        cuts=[],
        unit_results=results,
        proofs={"regression": {"passed": True, "detail": "stub"}},
        trunk={"trunk_ref": "dev", "observed_sha": "a" * 40,
               "current_sha": "a" * 40, "candidate_sha": "b" * 40,
               "candidate_is_ancestor_of_trunk": True},
        changed_paths=changed,
    )


def test_a_candidate_inside_the_declaration_closes_the_criterion(promotable):
    project, cycle_id = promotable
    verdict = _verdict_over(project, cycle_id, ["api/wu-01/impl.py"])
    assert "admitted_set_closed" not in (verdict.get("unmet") or ()), verdict


def test_a_candidate_outside_every_declared_path_is_unmet(promotable):
    project, cycle_id = promotable
    verdict = _verdict_over(project, cycle_id, ["web/src/rogue.ts"])
    assert "admitted_set_closed" in (verdict.get("unmet") or ()), verdict
    detail = verdict["criteria"]["admitted_set_closed"]["detail"]
    assert "never claimed" in detail and "web/src/rogue.ts" in detail, detail


def test_the_runtime_s_own_committed_records_are_not_a_stray_path(promotable):
    """Every Checkpoint commits the Cycle's records, and no declaration claims
    them. Without the exemption the barrier would fail for the runtime's own
    bookkeeping -- and the first declaration to forget `.synaptory/` would be
    the one that noticed."""
    project, cycle_id = promotable
    verdict = _verdict_over(
        project, cycle_id,
        [".synaptory/cycles/%s/events/WU-01.json" % cycle_id, "api/wu-01/impl.py"],
    )
    assert "admitted_set_closed" not in (verdict.get("unmet") or ()), verdict


def test_an_unobserved_candidate_is_not_reported_as_clean(promotable):
    """An empty `changed_paths` reports nothing rather than passing loudly:
    it is the absence of an observation, not evidence of a clean candidate."""
    project, cycle_id = promotable
    verdict = _verdict_over(project, cycle_id, [])
    result = verdict["criteria"]["admitted_set_closed"]
    assert result["paths_outside_the_declaration"] == []
