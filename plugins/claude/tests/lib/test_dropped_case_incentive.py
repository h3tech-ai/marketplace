"""Layer 1 — a dropped authored case costs at least as much as a declared gap
(#494, Epic #410, proposal 3.3).

#406 made `tests_pass` a verdict against the hash-sealed Cycle manifest, so a
case the verifying receipt does not report is a DROPPED case and the verdict is
False. #403 made a required check with nothing to evaluate a
`criteria_gap_declared`, and made THAT block the done edge. Between them they
left an inverted incentive:

    the prover that honestly declares it could not evaluate a case -> blocked
    the prover that silently omits the same case                   -> completed

Dropping the case was strictly cheaper than declaring the gap, and it needed no
forgery at all, only a missing key. That is worse than a hole. A hole leaves a
check unenforced; an inversion pays the prover to hide exactly what the check
exists to find, on the system's own quality gate.

Hypothesis: three things carry this file, and the third is the one the ticket
is actually about.

  1. **Enforcement.** A required check that FAILED against the sealed authored
     set stops the done edge. Naming the failure and enforcing it are not the
     same act; before #494 the gate did the first and not the second.
  2. **Narrowness.** It fires ONLY where `_authoritative_story` resolved the
     authored set from a manifest that passed its own hash check. Every
     project with no seal -- Scrum, Kanban, every pre-#406 Cycle -- keeps the
     behaviour `dod_requires_pass=False` exists to protect, and a legitimately
     not-applicable case still completes.
  3. **Symmetry.** Silence is no longer cheaper than honesty. Asserted
     directly, by running the two provers against the same sealed Cycle and
     comparing the outcomes, rather than inferred from (1).

Every test below reads the gate through `evaluate_story_dod` +
`dod_gate_block_reason`, which is the pair `resolve_done_edge` calls on every
host. The end-to-end host behaviour (refuse against redirect_blocked) is
certified in `conformance/test_capability_profile_contract.py`.
"""

from __future__ import annotations


import json
import subprocess
from pathlib import Path

import pytest

import spq_manifest as mf
import spq_state_machine as sm
import spq_paths as sp
from story_pipeline import (
    create_story,
    dod_gate_block_reason,
    evaluate_story_dod,
)

from _spq_fixture import CYCLE_KWARGS, unit as _fx_unit

pytestmark = pytest.mark.unit


CASES = [
    _fx_unit("AC-1", statement="an unauthenticated GET returns 401"),
    _fx_unit("AC-2", statement="a member GET returns only their own rows"),
]


# ── fixtures ──────────────────────────────────────────────────────────────────


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


def _unit(uid: str = "WU-1", **over) -> dict:
    unit = {"id": uid, "title": "work", "labels": ["ws:spine"], "kind": "story",
             "acceptance_criteria": ["it works"],
             "path_scope": ["api/" + uid.lower() + "/"]}
    unit.update(over)
    return unit


def _story(sid: str = "WU-1", state: str = "reviewing", **over) -> dict:
    story = create_story(sid, "work", acceptance_cases=CASES)
    story["state"] = state
    story["pipeline_log"] = [
        {"state": "queued", "entered_at": "2026-01-01T00:00:00Z",
         "exited_at": "2026-01-01T01:00:00Z"},
        {"state": "in_progress", "entered_at": "2026-01-01T01:00:00Z",
         "exited_at": "2026-01-01T02:00:00Z"},
        {"state": "testing", "entered_at": "2026-01-01T02:00:00Z",
         "exited_at": "2026-01-01T03:00:00Z"},
        {"state": "reviewing", "entered_at": "2026-01-01T03:00:00Z",
         "exited_at": None},
    ]
    story.update(over)
    return story


def _receipt(results, *, agent: str = "quality-engineer", exit_code: int = 0) -> dict:
    """A verifying receipt. `results=None` writes NO `authored_test_cases`."""
    body: dict = {
        "task": "verify",
        "agent": agent,
        "backend": "claude",
        "model": "sonnet",
        "artifacts": ["tests/test_api.py"],
        "completed_at": "2026-01-01T04:00:00Z",
        "verification_commands": [
            {"command": "pytest tests/ -q", "exit_code": exit_code,
             "summary": "12 passed"},
        ],
    }
    if results is not None:
        body["metrics"] = {"authored_test_cases": {
            "source": "cycle-manifest", "total": len(results), "results": results,
        }}
    return body


def _write_receipt(project: Path, sid: str, abbrev: str, body: dict) -> None:
    d = project / ".synaptory" / ".orchestrator" / "receipts"
    d.mkdir(parents=True, exist_ok=True)
    (d / ("%s-%s.json" % (sid, abbrev))).write_text(
        json.dumps(body), encoding="utf-8"
    )


def _green_producer(project: Path, sid: str = "WU-1") -> None:
    _write_receipt(project, sid, "se", {
        "agent": "software-engineer", "completed_at": "2026-01-01T02:00:00Z",
        "verification_commands": [
            {"command": "npm run build", "exit_code": 0},
            {"command": "pytest tests/ -q", "exit_code": 0},
        ],
    })
    _write_receipt(project, sid, "cr", {
        "agent": "code-reviewer", "completed_at": "2026-01-01T03:30:00Z",
        "verification_commands": [{"command": "review", "exit_code": 0}],
    })


def _board(project: Path, story: dict) -> None:
    state = sm.read_state(str(project))
    state["current_stories"] = [story]
    sm._write_state(str(project), state)


def _sealed(repo: Path, *, cases=CASES, story=None) -> None:
    """A Cycle at CYCLE_EXECUTION whose manifest seals `cases` for WU-1."""
    sm.initialize(str(repo), workstream_id="spine")
    sm.approve_baseline(str(repo), approved_by="t", baseline_ref="baseline-1",
        calibration={"sample_units": 1, "measured_hours": 1})
    sm.open_cycle(str(repo), goal="c", admitted_units=[_unit(acceptance_cases=cases)], **CYCLE_KWARGS)
    _board(repo, story if story is not None else _story())
    _green_producer(repo)


def _gate(repo: Path, sid: str = "WU-1", tier: str = "early"):
    """`(dod, block_reason)` — the exact pair `resolve_done_edge` computes."""
    dod = evaluate_story_dod(str(repo), sid, tier)
    return dod, dod_gate_block_reason(dod)


# ══ 1. enforcement: a dropped case stops the done edge ═══════════════════════


def test_a_dropped_case_blocks_the_done_edge(repo: Path):
    """The ticket. Two of three receipts green, one authored case never
    mentioned, and before #494 the unit completed."""
    _sealed(repo)
    _write_receipt(repo, "WU-1", "qe", _receipt({"AC-1": {"status": "passed"}}))
    dod, block = _gate(repo)
    assert dod["passed"] is False
    assert block, "a dropped authored case did not stop the done edge"


def test_the_block_names_the_dropped_case(repo: Path):
    """A block an operator cannot act on is a stall, not a gate. The #406
    verdict already names the case; the block has to carry it through."""
    _sealed(repo)
    _write_receipt(repo, "WU-1", "qe", _receipt({"AC-1": {"status": "passed"}}))
    _, block = _gate(repo)
    assert "AC-2" in block, block
    assert "DoD gate:" in block, block


def test_the_block_names_a_recovery(repo: Path):
    """Blocking is recoverable, and the three routes out are the honest ones:
    report the case, mark it not-applicable WITH a reason, or supersede the
    authored set through the verb that records a supersede."""
    _sealed(repo)
    _write_receipt(repo, "WU-1", "qe", _receipt({"AC-1": {"status": "passed"}}))
    _, block = _gate(repo)
    assert "not-applicable" in block, block
    assert "revise_manifest" in block, block


def test_an_explicitly_failed_case_blocks_too(repo: Path):
    """`fail` is `fail`. A prover that reports the case and could not pass it
    is stopped, so the fix does not create a NEW incentive to report a case as
    failed rather than to drop it."""
    _sealed(repo)
    _write_receipt(repo, "WU-1", "qe", _receipt(
        {"AC-1": {"status": "passed"}, "AC-2": {"status": "failed"}}
    ))
    _, block = _gate(repo)
    assert block and "AC-2" in block, block


def test_a_red_runner_under_a_seal_blocks(repo: Path):
    """#406 keeps the runner verdict as a second requirement under a seal. A
    red suite with every authored case reported passing is still a `fail`
    derived against the seal, so it blocks with the rest."""
    _sealed(repo)
    _write_receipt(repo, "WU-1", "qe", _receipt(
        {"AC-1": {"status": "passed"}, "AC-2": {"status": "passed"}},
        exit_code=1,
    ))
    dod, block = _gate(repo)
    assert dod["checks"]["tests_pass"]["passed"] is False
    assert block, "a red runner under a seal did not stop the done edge"


def test_a_claim_of_coverage_with_no_results_blocks(repo: Path):
    """`{"results": {}}` claims authored coverage and records none. #406 calls
    that a fail rather than a gap, and it now blocks."""
    _sealed(repo)
    _write_receipt(repo, "WU-1", "qe", _receipt({}))
    _, block = _gate(repo)
    assert block, "a coverage claim with no per-case results did not block"


# ══ 2. the incentive: silence is not cheaper than honesty ════════════════════


def _declared_gap_arm(repo: Path):
    _sealed(repo)
    _write_receipt(repo, "WU-1", "qe", _receipt(None))
    return _gate(repo)


def _dropped_case_arm(repo: Path):
    _sealed(repo)
    _write_receipt(repo, "WU-1", "qe", _receipt({"AC-1": {"status": "passed"}}))
    return _gate(repo)


def test_the_declared_gap_still_blocks(repo: Path):
    """The half that already worked, asserted so the symmetry below cannot be
    satisfied by breaking it. #403's behaviour is unchanged."""
    dod, block = _declared_gap_arm(repo)
    results = {i["check_id"]: i["result"] for i in dod["dod_check_results"]}
    assert results["tests_pass"] == "criteria_gap_declared"
    assert block, block


def test_dropping_a_case_is_not_cheaper_than_declaring_a_gap(tmp_path, monkeypatch):
    """THE assertion of this ticket, made directly rather than by implication.

    Same sealed Cycle, same authored pair, two provers that both fail to prove
    AC-2. One says so by carrying no per-case object; one says nothing at all
    about it. Before #494 the first was blocked and the second completed, so
    the cheapest route past the gate was to report less. Both are blocked now.
    """
    arms = {}
    for label, arm in (("declared", _declared_gap_arm), ("dropped", _dropped_case_arm)):
        project = tmp_path / label
        project.mkdir()
        _git(project, "init", "-q")
        _git(project, "config", "user.email", "t@e.co")
        _git(project, "config", "user.name", "t")
        (project / "f.txt").write_text("x", encoding="utf-8")
        _git(project, "add", "-A")
        _git(project, "commit", "-qm", "init")
        (project / ".synaptory.yaml").write_text(
            "build_mode: spq\nspq:\n  workstreams:\n"
            '    - id: "spine"\n      shared_owner: true\n',
            encoding="utf-8",
        )
        arms[label] = arm(project)

    (declared_dod, declared_block) = arms["declared"]
    (dropped_dod, dropped_block) = arms["dropped"]

    # The premise: two DIFFERENT paths, not the same one run twice.
    assert {i["check_id"]: i["result"] for i in declared_dod["dod_check_results"]}[
        "tests_pass"
    ] == "criteria_gap_declared"
    assert {i["check_id"]: i["result"] for i in dropped_dod["dod_check_results"]}[
        "tests_pass"
    ] == "fail"

    # The claim: the same cost.
    assert declared_block, "the honest declaration stopped costing anything"
    assert dropped_block, (
        "silence is still cheaper than honesty: declaring the gap blocks and "
        "dropping the case does not"
    )

    # And the causes stay distinguishable, so equalising the cost did not
    # equalise the diagnosis. An operator can still tell which happened.
    assert "criteria gap" in declared_block
    assert "criteria gap" not in dropped_block
    assert "AC-2" in dropped_block


# ══ 3. narrowness: an honest absence still completes ═════════════════════════


def test_a_not_applicable_case_with_a_reason_still_completes(repo: Path):
    """The flow the fix must not break, and the reason this is not simply
    "every absence fails". A case that legitimately does not apply is reported
    as such WITH a sentence, `_case_result_is_settled` counts it, the verdict
    is True, and there is no fail for the gate to block on."""
    _sealed(repo)
    _write_receipt(repo, "WU-1", "qe", _receipt({
        "AC-1": {"status": "passed"},
        "AC-2": {
            "status": "not-applicable",
            "reason": "the member-scoping path was descoped at COMMIT+2; "
                      "there is no multi-tenant row to scope in this Cycle",
        },
    }))
    dod, block = _gate(repo)
    assert dod["checks"]["tests_pass"]["passed"] is True
    assert block is None, block


def test_a_bare_not_applicable_does_not_buy_the_same_exemption(repo: Path):
    """The cheapest forgery on the whole path, and it stays refused. Without a
    reason `not-applicable` is a status a prover can write for every case it
    cannot pass, so it is not settled, the verdict is a fail, and it blocks."""
    _sealed(repo)
    _write_receipt(repo, "WU-1", "qe", _receipt({
        "AC-1": {"status": "passed"},
        "AC-2": {"status": "not-applicable"},
    }))
    _, block = _gate(repo)
    assert block and "AC-2" in block, block


def test_every_case_reported_passing_completes(repo: Path):
    """The control. Without it every assertion above would read identically
    against a gate that simply blocks everything."""
    _sealed(repo)
    _write_receipt(repo, "WU-1", "qe", _receipt(
        {"AC-1": {"status": "passed"}, "AC-2": {"status": "passed"}}
    ))
    dod, block = _gate(repo)
    assert dod["checks"]["tests_pass"]["passed"] is True
    assert block is None, block


def test_a_project_with_no_sealed_authored_set_is_unchanged(repo: Path):
    """Acceptance criterion 2, and the whole reason the clause keys on the
    seal. A pre-#406 Cycle carries no `acceptance_cases` on its unit record,
    so `manifest_authored_cases` returns None, `authored_source` is `board`,
    and `tests_pass` is evaluated on the pre-#406 rules. A receipt-internal
    authored-case failure fails the CHECK, exactly as it did, and does not
    block the done edge, exactly as it did."""
    sm.initialize(str(repo), workstream_id="spine")
    sm.approve_baseline(str(repo), approved_by="t", baseline_ref="baseline-1",
        calibration={"sample_units": 1, "measured_hours": 1})
    sm.open_cycle(str(repo), goal="c", admitted_units=[_unit()], **CYCLE_KWARGS)  # no acceptance_cases key
    story = _story(acceptance_cases=[])
    story["criteria_gap_declared"] = {
        "unit_id": "WU-1", "declared": False, "reason": mf.LEGACY_GAP_REASON,
    }
    _board(repo, story)
    _green_producer(repo)
    _write_receipt(repo, "WU-1", "qe", _receipt({
        "AC-1": {"status": "passed"}, "AC-2": {"status": "failed"},
    }))
    dod, block = _gate(repo)
    assert dod["checks"]["tests_pass"]["test_first"]["authored_source"] == "board"
    assert dod["checks"]["tests_pass"]["passed"] is False
    assert block is None, (
        "a project with no sealed authored set changed behaviour: %s" % block
    )


def test_a_scrum_story_is_untouched(tmp_path: Path):
    """The blast radius, stated as a test. The pilot is scoped to SPQ
    (ADR-032 section 6) and this clause must not reach a Scrum board, which
    has no manifest at all."""
    import scrum_state_machine as scrum

    project = tmp_path / "scrum"
    project.mkdir()
    (project / ".synaptory.yaml").write_text("build_mode: scrum\n", encoding="utf-8")
    state = scrum.initialize(str(project), "p")
    state["current_stories"] = [_story("US-1")]
    scrum._write_state(str(project), state)
    _green_producer(project, "US-1")
    _write_receipt(project, "US-1", "qe", _receipt({
        "AC-1": {"status": "passed"}, "AC-2": {"status": "failed"},
    }))
    dod = evaluate_story_dod(str(project), "US-1", "early")
    assert dod["checks"]["tests_pass"]["passed"] is False
    assert dod_gate_block_reason(dod) is None


def test_a_failing_build_under_a_seal_does_not_block(repo: Path):
    """The clause is not "any failing static check". `build_succeeds` rests on
    the receipt's own exit code -- there is no sealed document behind it -- so
    it carries no `test_first` stanza and passes through as it always has.
    `dod_requires_pass=True` is still the knob that tightens that, and the two
    settings still differ."""
    _sealed(repo)
    _write_receipt(repo, "WU-1", "se", {
        "agent": "software-engineer", "completed_at": "2026-01-01T02:00:00Z",
        "verification_commands": [{"command": "npm run build", "exit_code": 1}],
    })
    _write_receipt(repo, "WU-1", "qe", _receipt(
        {"AC-1": {"status": "passed"}, "AC-2": {"status": "passed"}}
    ))
    dod, block = _gate(repo)
    assert dod["checks"]["build_succeeds"]["passed"] is False
    assert block is None, block


# ══ 4. forgeability: what the clause reads, and what it refuses ══════════════


def test_the_board_cannot_claim_the_seal_it_does_not_have(repo: Path):
    """`_authored_source` is DERIVED on every read, never adopted.

    Untrusted input: `pipeline-state.json`, which the agent under the gate can
    write. Forgery: stamp `_authored_source: "board"` onto the unit record so
    the gate treats a sealed Cycle as an unsealed one and lets the dropped case
    through. Refused by `_authoritative_story`, which assigns
    `story["_authored_source"]` in all three of its branches after consulting
    the manifest, so a board-supplied value is overwritten before the gate sees
    it. That refuses the FORGERY and not merely a malformed spelling of it:
    the field is not read from the board at any point.
    """
    _sealed(repo, story=_story(_authored_source="board"))
    _write_receipt(repo, "WU-1", "qe", _receipt({"AC-1": {"status": "passed"}}))
    dod, block = _gate(repo)
    assert dod["checks"]["tests_pass"]["test_first"]["authored_source"] == "manifest"
    assert block, "a board-supplied `_authored_source` bought a way past the gate"


def test_shrinking_the_boards_authored_set_does_not_shrink_the_block(repo: Path):
    """The #406 forgery, re-asserted at the ENFORCEMENT layer.

    Dropping AC-2 from the board's own copy is the cheapest way to make the
    dropped case look absent rather than dropped. The gate reads the seal, so
    the block still fires and still names AC-2. #406 proved the VERDICT
    survives this; what is new is that the verdict now stops the edge.
    """
    _sealed(repo, story=_story(acceptance_cases=[CASES[0]]))
    _write_receipt(repo, "WU-1", "qe", _receipt({"AC-1": {"status": "passed"}}))
    dod, block = _gate(repo)
    assert dod["checks"]["tests_pass"]["test_first"]["authored_disagreement"], dod
    assert block and "AC-2" in block, block


def test_a_tampered_manifest_blocks_through_the_gap_route(repo: Path):
    """Editing the seal instead breaks its hash. The authored set is then
    unknowable rather than disproven, which is a gap, and #403's clause blocks
    on it. Unchanged by this ticket and asserted so the two routes cannot
    collapse into one."""
    _sealed(repo)
    _write_receipt(repo, "WU-1", "qe", _receipt(
        {"AC-1": {"status": "passed"}, "AC-2": {"status": "passed"}}
    ))
    path = Path(sp.manifest_path(str(repo), sm.identity(str(repo)).cycle_id))
    path.chmod(0o644)  # sealed manifests are written read-only
    manifest = json.loads(path.read_text(encoding="utf-8"))
    # `admitted_units` is the sealed set's key (`cycle_records`, #644). The
    # tamper is the same one: empty the authored set in place, without
    # recomputing the digest.
    manifest["admitted_units"][0]["acceptance_cases"] = []
    path.write_text(json.dumps(manifest), encoding="utf-8")

    dod, block = _gate(repo)
    tf = dod["checks"]["tests_pass"]["test_first"]
    assert tf["authored_source"] == "untrusted_manifest", tf
    results = {i["check_id"]: i["result"] for i in dod["dod_check_results"]}
    assert results["tests_pass"] == "criteria_gap_declared"
    assert block, block


def test_deleting_the_seal_does_not_downgrade_the_gate(repo: Path):
    """The residual this file used to pin, CLOSED by #507.

    It was pinned here as `test_deleting_the_seal_still_downgrades_the_gate`,
    asserting that removing the manifest reverted the unit to the pre-#406
    board rules -- one `rm` cheaper than the dropped case #494 had just made
    expensive. The reason turned out to be smaller and worse than the note
    guessed: the seal is written to TWO places (`spq/cycles/<id>/manifest.json`
    and the committed transport `.synaptory/cycles/<id>/manifest.json`) and the
    gate read ONE, so this deletion never removed the seal at all. An
    identical copy was on disk beside it the whole time.

    So the fix is recovery before refusal: `spq_manifest.read_sealed` consults
    every copy, this arm resolves from the committed one, and the dropped case
    still stops the done edge. The full ladder -- both copies gone, the index
    gone, the `_cycle_id` repointed, and the counterfeit-manifest residual that
    remains -- lives in `test_seal_absence.py`.
    """
    _sealed(repo, story=_story(acceptance_cases=[]))
    _write_receipt(repo, "WU-1", "qe", _receipt({"AC-1": {"status": "passed"}}))
    Path(sp.manifest_path(str(repo), sm.identity(str(repo)).cycle_id)).unlink()

    dod, block = _gate(repo)
    tf = dod["checks"]["tests_pass"]["test_first"]
    assert tf["authored_source"] == "manifest", tf
    assert tf["authored_seal_origin"] == "committed", tf
    assert block and "AC-2" in block, block


# ══ 5. the block is named once, and routed ═══════════════════════════════════


def test_a_replay_mismatch_is_reported_once_not_twice(repo: Path):
    """A replay-mismatched `tests_pass` under a seal satisfies both this
    ticket's clause and #179 E1's. It is named once, by the more specific of
    the two, so an operator gets one remedy rather than two contradictory
    ones."""
    _sealed(repo)
    _write_receipt(repo, "WU-1", "qe", _receipt(
        {"AC-1": {"status": "passed"}, "AC-2": {"status": "passed"}}
    ))
    dod, _ = _gate(repo)
    dod["checks"]["tests_pass"]["passed"] = False
    dod["checks"]["tests_pass"]["result"] = "fail"
    dod["checks"]["tests_pass"]["replay_mismatch"] = True
    block = dod_gate_block_reason(dod)
    assert block.count("tests_pass") == 1, block
    assert "evidence replay" in block, block


def test_the_block_routes_to_the_verifying_stage(repo: Path):
    """`_gate_remediation` already maps `tests_pass` to `qe`, so the block
    reaches the stage that can produce the missing outcome instead of feeding
    the retry ladder a fresh SE failure."""
    from story_pipeline import _gate_remediation

    _sealed(repo)
    _write_receipt(repo, "WU-1", "qe", _receipt({"AC-1": {"status": "passed"}}))
    _, block = _gate(repo)
    action = _gate_remediation({"blocked_reason": block})
    assert action == {
        "tier": "gate_remediation", "role": "qe",
        "gate": "tests_pass", "reason": block,
    }


def test_a_non_required_check_does_not_block(repo: Path):
    """The clause reads `required`, so a check the tier does not require
    cannot stop the edge on a fail it was never asked to answer."""
    _sealed(repo)
    _write_receipt(repo, "WU-1", "qe", _receipt({"AC-1": {"status": "passed"}}))
    dod, block = _gate(repo)
    assert block
    dod["checks"]["tests_pass"]["required"] = False
    assert dod_gate_block_reason(dod) is None
