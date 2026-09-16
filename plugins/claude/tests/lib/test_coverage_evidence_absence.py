# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
"""Layer 1 - #540: an absent coverage measurement is a gap, never a pass.

This is the SECOND half of the defect #487 closed. That one read
`metrics.get("findings_critical", 0)`, so an empty-metrics receipt scored a
clean security audit. Its neighbour defaulted the delta to `""`, failed to
parse it, and returned True with the comment "cannot determine, assume pass",
which is the same lenient default one branch away.

Three things had already written down that this was wrong, which is what makes
it worth a module rather than a line:

  * `_MISSING_EVIDENCE_SHAPE["coverage_no_decrease"]` says there is nothing to
    compare against, in the text handed to the retry prompt.
  * `_evaluate_fallback_proof` has always returned None for an absent delta.
  * The fallback evaluator's docstring named this exact pair, coverage
    included, as the danger.

Only the nominal path disagreed, and `evidence-contract.json` had codified its
disagreement as intended behaviour, which is how #487's half survived review
in the first place.

So the last test here is the one that matters most: it asks the question of
every check at once, so the next lenient default fails on arrival instead of
waiting for a third ticket.
"""

from __future__ import annotations


import ast
import copy
import inspect
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_HOOKS_LIB = _PLUGIN_ROOT / "hooks" / "lib"
if str(_HOOKS_LIB) not in sys.path:
    sys.path.insert(0, str(_HOOKS_LIB))

import story_pipeline as sp  # noqa: E402

from _spq_fixture import CYCLE_KWARGS, unit as _fx_unit


def _project(tmp_path: Path) -> Path:
    (tmp_path / ".synaptory" / ".orchestrator" / "receipts").mkdir(
        parents=True, exist_ok=True
    )
    return tmp_path


def _receipts_dir(project: Path) -> str:
    return str(project / ".synaptory" / ".orchestrator" / "receipts")


def _write_receipt(project: Path, story_id: str, role: str, **fields) -> None:
    abbrev = sp._role_to_abbrev(role)
    Path(_receipts_dir(project), "%s-%s.json" % (story_id, abbrev)).write_text(
        json.dumps({"agent": role, **fields}), encoding="utf-8"
    )


def _unit(project: Path, story_id: str, **qe_metrics) -> None:
    """SE + QE + CR receipts that clear everything EXCEPT coverage.

    `metrics` is passed through verbatim so a caller can omit it entirely,
    which is the shape under test.
    """
    artifact = "src/components/Button.tsx"
    _write_receipt(
        project, story_id, "software-engineer",
        artifacts=[artifact],
        verification_commands=[{"command": "npm run build", "exit_code": 0}],
    )
    _write_receipt(
        project, story_id, "quality-engineer",
        artifacts=[artifact],
        verification_commands=[{"command": "npm test", "exit_code": 0}],
        **qe_metrics,
    )
    _write_receipt(
        project, story_id, "code-reviewer",
        artifacts=[artifact],
        status="complete",
        story_dod={"code_reviewed": True},
        verification_commands=[{"command": "npm run lint", "exit_code": 0}],
    )


def _dod(project: Path, story_id: str) -> dict:
    # `mature` is the first tier that activates coverage. The result comes from
    # the evaluator itself rather than a hand-written dict, so the shape under
    # assertion is the shape the gate reads (#509).
    return sp.evaluate_story_dod(
        str(project), story_id, "mature", receipts_dir=_receipts_dir(project)
    )


def _coverage(result: dict) -> dict:
    checks = result.get("checks") or {}
    entry = checks.get("coverage_no_decrease")
    assert isinstance(entry, dict), (
        "no coverage entry in the mature-tier result: %r" % sorted(checks)
    )
    return entry


@pytest.mark.unit
def test_a_unit_with_no_metrics_at_all_gaps_on_coverage(tmp_path):
    """Nothing was measured, so there is nothing to have passed."""
    project = _project(tmp_path)
    _unit(project, "WU-1")
    entry = _coverage(_dod(project, "WU-1"))
    assert entry["passed"] is not True, entry
    assert entry["result"] == sp.CRITERIA_GAP_DECLARED, entry


@pytest.mark.unit
def test_metrics_without_the_delta_gaps(tmp_path):
    """A receipt that measured OTHER things still measured no coverage."""
    project = _project(tmp_path)
    _unit(project, "WU-2", metrics={"tests_added": 4})
    entry = _coverage(_dod(project, "WU-2"))
    assert entry["result"] == sp.CRITERIA_GAP_DECLARED, entry


@pytest.mark.unit
@pytest.mark.parametrize(
    "delta", ["", "n/a", "unknown", "+", "%", True],
    ids=["empty", "n-a", "unknown", "bare-sign", "bare-percent", "boolean"],
)
def test_an_unparseable_delta_gaps_rather_than_passing(tmp_path, delta):
    """`True` is in this list on purpose: a bool is an int in Python, so
    `coverage_delta: true` would otherwise compare `>= 0` and pass. It is a
    claim, not a measurement."""
    project = _project(tmp_path)
    _unit(project, "WU-3", metrics={"coverage_delta": delta})
    entry = _coverage(_dod(project, "WU-3"))
    assert entry["passed"] is not True, entry
    assert entry["result"] == sp.CRITERIA_GAP_DECLARED, entry


@pytest.mark.unit
@pytest.mark.parametrize("delta", ["+3.2%", "0%", " +0.4% ", 1.5, 0])
def test_an_honest_measurement_still_passes(tmp_path, delta):
    """Tightening absence must not break the producer that did the work."""
    project = _project(tmp_path)
    _unit(project, "WU-4", metrics={"coverage_delta": delta})
    entry = _coverage(_dod(project, "WU-4"))
    assert entry["passed"] is True, entry
    assert entry["result"] == sp.DOD_RESULT_PASS, entry


@pytest.mark.unit
@pytest.mark.parametrize("delta", ["-1.5%", -0.1])
def test_a_measured_decrease_still_fails_rather_than_gapping(tmp_path, delta):
    """A gap and a failure are different answers and must not merge.

    "Coverage dropped" is a measured verdict a human can act on; "nobody
    measured" routes to whoever owes the measurement. Returning None for a real
    decrease would lose the first, which is why the fix distinguishes absence
    from a parsed negative rather than gapping on anything non-positive.
    """
    project = _project(tmp_path)
    _unit(project, "WU-5", metrics={"coverage_delta": delta})
    entry = _coverage(_dod(project, "WU-5"))
    assert entry["passed"] is False, entry
    assert entry["result"] == sp.DOD_RESULT_FAIL, entry


@pytest.mark.unit
def test_the_contract_no_longer_documents_a_lenient_default():
    """#487's lesson: the contract file had codified the defect.

    Fixing the evaluator while `evidence-contract.json` still tells an author
    that absence passes leaves the next reader with a written promise the code
    does not keep, and leaves the defect one revert away from returning with
    documentation on its side.
    """
    import evidence_contract

    contract = evidence_contract.load_contract()
    spec = contract["dod_checks"]["coverage_no_decrease"]
    predicate = spec["evidence"]["predicate"]
    assert "assumed to pass" not in predicate, predicate
    assert "never a pass" in predicate, predicate


# ══ the class: no DoD check may read an absence as a satisfied check ════════
#
# The first version of this census (merged with the evaluator fix) asked the
# question of `_EVIDENCE_GATE_KEYS`, which is FIVE of the eight checks in
# `DOD_CHECKS` by its own comment: the four conditional gates have no column
# on /quality and were left out. The scope choice hid a live third instance.
# `ui_acceptance` read `int(uv.get("flows_failed", 0) or 0)`, the same
# `.get(key, 0)` shape #487 removed from `findings_critical`, and ignored
# `routes_tested` at the `full` tier entirely, so `{"rendered": true}` scored
# the gate whose own missing-evidence text demands three recorded fields.
#
# So the census is enumerated from the REGISTRY, and the registry is checked
# for completeness against the evaluator's own branches. A census that names
# its own subset closes the class only for the part someone already looked at,
# which is the failure mode it exists to prevent.


def _evaluated_check_ids() -> set:
    """The check ids `_evaluate_check` actually branches on, read from source.

    Enumerating from a hand-maintained tuple is what let the first census miss
    three checks. This reads the branches themselves, so a new `check_id ==
    "..."` arm that is never registered in `DOD_CHECKS` fails here rather than
    escaping every probe below.

    What it cannot see, stated rather than left for a reader to discover: a
    branch dispatched through a dict or a helper instead of a literal
    comparison, and anything `evaluate_story_dod` decides ABOVE this function
    (the cross-role fallback, the authored-case veto). Those are covered by
    the gate-level tests, not by this.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(sp._evaluate_check)))
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not (isinstance(node.left, ast.Name) and node.left.id == "check_id"):
            continue
        for operand in node.comparators:
            if isinstance(operand, ast.Constant) and isinstance(operand.value, str):
                found.add(operand.value)
            elif isinstance(operand, (ast.Tuple, ast.List, ast.Set)):
                for element in operand.elts:
                    if isinstance(element, ast.Constant) and isinstance(
                        element.value, str
                    ):
                        found.add(element.value)
    return found


@pytest.mark.unit
def test_the_registry_names_every_check_the_evaluator_answers():
    """A new check cannot land outside the census by skipping the registry."""
    assert _evaluated_check_ids() == set(sp.DOD_CHECKS), (
        "`_evaluate_check` branches on %r; `DOD_CHECKS` registers %r"
        % (sorted(_evaluated_check_ids()), sorted(sp.DOD_CHECKS))
    )


@pytest.mark.unit
def test_every_registered_check_can_say_what_evidence_it_is_missing():
    """A gap that cannot name its missing shape is the silent nothing #403
    replaced, so the two tables move together."""
    assert set(sp._MISSING_EVIDENCE_SHAPE) == set(sp.DOD_CHECKS)


@pytest.mark.unit
def test_the_five_gate_keys_are_a_subset_and_not_the_census(check_ids=None):
    """The scope note that made the first census miss three checks, asserted
    so the relationship is visible rather than inferred."""
    assert set(sp._EVIDENCE_GATE_KEYS) < set(sp.DOD_CHECKS)


@pytest.mark.unit
@pytest.mark.parametrize("check_id", sorted(sp.DOD_CHECKS))
@pytest.mark.parametrize("tier", ["full", "standard"])
def test_no_dod_check_treats_an_empty_receipt_as_satisfied(check_id, tier):
    """The class, not the instance (#528's shape, applied to #540).

    This is the second lenient default found on this evaluator, and both were
    found by someone reading the code rather than by a test. A per-case test
    would catch the third one only if somebody thought to write it. This asks
    the question of every check at once, so a new check that reads an absent
    metric as satisfied fails the moment it lands.

    An empty receipt is the strongest form of the question: nothing was
    verified, nothing was counted, nothing was reviewed. `False` and `None`
    are both acceptable answers, and they mean different things. `True` is
    not an answer to a receipt that says nothing.

    Asked at both tiers because a tier is a way for one branch to be lenient
    while its neighbour is strict, which is this class one axis over.
    """
    receipt = {"agent": "quality-engineer"}
    assert sp._evaluate_check(check_id, receipt, verification_tier=tier) is not True, (
        "%s reads an empty receipt as satisfied at the %s tier, which is the "
        "lenient default #487 removed from no_critical_findings and #540 from "
        "coverage_no_decrease" % (check_id, tier)
    )
    assert (
        sp._evaluate_fallback_proof(check_id, receipt, verification_tier=tier)
        is not True
    ), (
        "%s satisfies the FALLBACK matcher on an empty receipt at the %s tier; "
        "that path is documented as the strict one" % (check_id, tier)
    )


#: Per check: a receipt the check accepts, the tier it is accepted at, and
#: every evidence field whose ABSENCE must not leave the pass standing.
#:
#: The empty-receipt probe above cannot see a check that reads SOME evidence
#: and defaults the rest, which is exactly what `ui_acceptance` did. This one
#: starts from a receipt that passes and removes one field at a time, so a
#: lenient default has to survive the removal of the field it defaults.
_WITNESSES: dict = {
    "tests_pass": (
        "full",
        {"verification_commands": [{"command": "pytest -q", "exit_code": 0}]},
        ["verification_commands.0.exit_code"],
    ),
    "build_succeeds": (
        "full",
        {"verification_commands": [{"command": "npm run build", "exit_code": 0}]},
        ["verification_commands.0.exit_code"],
    ),
    "no_critical_findings": (
        "full",
        {"metrics": {"findings_critical": 0}},
        ["metrics.findings_critical"],
    ),
    "code_reviewed": (
        "full",
        {"status": "complete", "story_dod": {"code_reviewed": True}},
        [],  # either half is sufficient by contract (#134 GAP-1b), so neither
    ),      # is an absence the check defaults; the empty probe covers it.
    "coverage_no_decrease": (
        "full",
        {"metrics": {"coverage_delta": "+1.2%"}},
        ["metrics.coverage_delta"],
    ),
    "runtime_verified": (
        "full",
        {"metrics": {"runtime_verification": {
            "deployed": True, "logs_inspected": True,
        }}},
        ["metrics.runtime_verification.deployed",
         "metrics.runtime_verification.logs_inspected"],
    ),
    "ui_acceptance": (
        "full",
        {"metrics": {"ui_verification": {
            "rendered": True, "routes_tested": 2, "flows_failed": 0,
        }}},
        ["metrics.ui_verification.rendered",
         "metrics.ui_verification.routes_tested",
         "metrics.ui_verification.flows_failed"],
    ),
    "integration_verified": (
        "full",
        {"integrations": [{"name": "stripe", "status": "live"}],
         "verification_commands": [{"command": "curl -f $STRIPE", "exit_code": 0}]},
        ["integrations.0.status", "verification_commands.0.exit_code"],
    ),
}


def _without(receipt: dict, path: str) -> dict:
    """`receipt` with the dotted `path` removed. List steps are indices."""
    out = copy.deepcopy(receipt)
    node = out
    steps = path.split(".")
    for step in steps[:-1]:
        node = node[int(step)] if isinstance(node, list) else node[step]
    last = steps[-1]
    if isinstance(node, list):
        del node[int(last)]
    else:
        del node[last]
    return out


@pytest.mark.unit
def test_every_registered_check_declares_a_witness():
    """A check with no witness has not been asked the question at all, so a
    new one cannot join the registry and skip the leave-one-out probe."""
    assert set(_WITNESSES) == set(sp.DOD_CHECKS)


@pytest.mark.unit
@pytest.mark.parametrize("check_id", sorted(_WITNESSES))
def test_each_witness_is_a_receipt_the_check_actually_accepts(check_id):
    """Without this the probe below could pass by testing nothing."""
    tier, receipt, _ = _WITNESSES[check_id]
    assert sp._evaluate_check(check_id, receipt, verification_tier=tier) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "check_id,path",
    [(cid, p) for cid, (_, _, paths) in sorted(_WITNESSES.items()) for p in paths],
)
def test_removing_a_recorded_measurement_does_not_leave_a_pass(check_id, path):
    """The lenient-default probe, per field.

    `metrics.get(field, <benign>)` is the whole class in one expression: it
    turns "nobody measured this" into "it was measured and it was fine". The
    only way to catch it generically is to take a receipt that passes and
    delete the field, because a check that defaults is indistinguishable from
    one that reads, right up until the field is gone.
    """
    tier, receipt, _ = _WITNESSES[check_id]
    verdict = sp._evaluate_check(
        check_id, _without(receipt, path), verification_tier=tier
    )
    assert verdict is not True, (
        "%s still passes with `%s` removed, so an absent measurement is being "
        "credited as a satisfied one" % (check_id, path)
    )


@pytest.mark.unit
def test_the_legacy_ui_shape_also_needs_its_flow_count():
    """The back-compat branch is a second reader of the same evidence and had
    the same default, so fixing only the structured object would have left the
    absence reachable one shape away."""
    legacy = {"metrics": {"routes_tested": 3, "flows_failed": 0}}
    assert sp._evaluate_check("ui_acceptance", legacy) is True
    assert sp._evaluate_check("ui_acceptance", _without(
        legacy, "metrics.flows_failed")) is None


@pytest.mark.unit
@pytest.mark.parametrize("count", ["n/a", "", True, None, [], {"n": 1}])
def test_an_unreadable_ui_count_gaps_rather_than_passing_or_crashing(count):
    """#540's unparseable-delta rule, and a crash this ticket would otherwise
    have introduced: `routes_tested` is load-bearing at the `full` tier now,
    and `int("n/a")` raises. A bool is in the list for #540's reason, that a
    claim is not a count."""
    receipt = {"metrics": {"ui_verification": {
        "rendered": True, "routes_tested": count, "flows_failed": 0,
    }}}
    assert sp._evaluate_check("ui_acceptance", receipt) is None


@pytest.mark.unit
def test_the_standard_tier_keeps_the_relaxation_it_declares():
    """#134 GAP-3 waives the FLOW count at this tier, in writing, and a waiver
    is not the class this ticket closes: the tier says out loud that it does
    not need the measurement. A recorded failure still fails, and the route
    count the tier does require is still required.

    This is also the direction the fix had to be careful about. Before it,
    `full` was strictly WEAKER than its own relaxation, because only
    `standard` insisted a route had been exercised.
    """
    render_only = {"metrics": {"ui_verification": {
        "rendered": True, "routes_tested": 1,
    }}}
    assert sp._evaluate_check(
        "ui_acceptance", render_only, verification_tier="standard") is True
    assert sp._evaluate_check(
        "ui_acceptance", render_only, verification_tier="full") is None
    failed = {"metrics": {"ui_verification": {
        "rendered": True, "routes_tested": 1, "flows_failed": 2,
    }}}
    assert sp._evaluate_check(
        "ui_acceptance", failed, verification_tier="standard") is False


@pytest.mark.unit
def test_the_contract_documents_the_ui_counts_it_now_requires():
    """#487's lesson again: the contract file had codified the defect, which
    is how the first half survived review. A predicate that still promised
    `rendered true AND flows_failed == 0` would leave this fix one revert away
    from returning with documentation on its side."""
    import evidence_contract

    predicate = evidence_contract.load_contract()[
        "dod_checks"]["ui_acceptance"]["evidence"]["predicate"]
    assert "routes_tested >= 1" in predicate, predicate
    assert "never a pass" in predicate, predicate


# ══ the gap BLOCKS, on a board the state machine wrote ═══════════════════════
#
# The acceptance criterion is "a gap, not a pass, AND the gap blocks", and the
# tests above establish only the first half: `criteria_gap_declared` is what
# `_evaluate_check` produces, not what the done edge does with it. The pair
# every host actually calls is `evaluate_story_dod` + `dod_gate_block_reason`,
# so that pair is what these ask.
#
# The Cycle is opened with `open_cycle` rather than hand-written, per #509: a
# fixture writing a shape the lifecycle never produces cannot reach a layout
# defect, and under `build_mode: spq` the board is not `pipeline-state.json`
# at all.


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=False)


@pytest.fixture
def spq_repo(tmp_path: Path, monkeypatch) -> Path:
    """A real git repo with one commit, so a Cycle baseline exists."""
    import spq_paths

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


def _open_cycle_with_unit(project: Path, story: dict) -> None:
    """Open a real Cycle, then put `story` on the board the machine wrote."""
    import spq_state_machine as sm

    sm.initialize(str(project), workstream_id="spine")
    sm.approve_baseline(str(project), approved_by="t", baseline_ref="baseline-1",
        calibration={"sample_units": 1, "measured_hours": 1})
    sm.open_cycle(str(project), goal="c", admitted_units=[
        _fx_unit(story["id"], title=story.get("title") or "work"),
    ], **CYCLE_KWARGS)
    state = sm.read_state(str(project))
    state["current_stories"] = [story]
    sm._write_state(str(project), state)


def _green_unit(project: Path, story_id: str, qe_metrics: dict) -> None:
    """SE + QE + CR receipts that clear everything except what is omitted."""
    _project(project)
    _write_receipt(
        project, story_id, "software-engineer",
        artifacts=["src/app.py"],
        verification_commands=[{"command": "npm run build", "exit_code": 0}],
    )
    _write_receipt(
        project, story_id, "quality-engineer",
        artifacts=["tests/test_app.py"],
        verification_commands=[{"command": "pytest -q", "exit_code": 0}],
        metrics=qe_metrics,
    )
    _write_receipt(
        project, story_id, "code-reviewer",
        artifacts=["src/app.py"],
        status="complete",
        story_dod={"code_reviewed": True},
        verification_commands=[{"command": "review", "exit_code": 0}],
    )


def _gate(project: Path, story_id: str, tier: str = "mature"):
    """The exact pair the done edge computes on every host."""
    dod = sp.evaluate_story_dod(str(project), story_id, tier)
    return dod, sp.dod_gate_block_reason(dod)


@pytest.mark.unit
def test_an_unmeasured_coverage_gap_stops_the_done_edge(spq_repo: Path):
    """The ticket's own acceptance criterion, end to end.

    Everything else on the unit is green. Nothing recorded a coverage delta,
    and before the evaluator fix this unit completed.
    """
    _open_cycle_with_unit(spq_repo, sp.create_story("WU-1", "work"))
    _green_unit(spq_repo, "WU-1", {"tests_added": 4})
    dod, block = _gate(spq_repo, "WU-1")
    assert dod["checks"]["coverage_no_decrease"]["result"] == sp.CRITERIA_GAP_DECLARED
    assert dod["passed"] is not True
    assert block and "coverage_no_decrease" in block, block


@pytest.mark.unit
def test_the_block_names_the_measurement_that_is_missing(spq_repo: Path):
    """A block an operator cannot act on is a stall. The retry prompt's text
    is `_MISSING_EVIDENCE_SHAPE`, and this is where the two ends of #540, the
    text and the evaluator, have to meet on the same unit."""
    _open_cycle_with_unit(spq_repo, sp.create_story("WU-1", "work"))
    _green_unit(spq_repo, "WU-1", {})
    _, block = _gate(spq_repo, "WU-1")
    assert "coverage_delta" in block, block


@pytest.mark.unit
def test_the_same_unit_completes_once_the_delta_is_recorded(spq_repo: Path):
    """The gap is recoverable by doing the measurement, which is the only
    remedy the block names. Without this the test above would pass just as
    well if the gate never let anything through."""
    _open_cycle_with_unit(spq_repo, sp.create_story("WU-1", "work"))
    _green_unit(spq_repo, "WU-1", {"coverage_delta": "+0.4%"})
    dod, block = _gate(spq_repo, "WU-1")
    assert dod["checks"]["coverage_no_decrease"]["passed"] is True
    assert block is None, block


@pytest.mark.unit
def test_an_unmeasured_ui_count_stops_the_done_edge_at_the_full_tier(
    spq_repo: Path,
):
    """The third instance, at the gate rather than in the evaluator.

    `{"rendered": true}` is a claim that a screen appeared, with no route
    exercised and no flow outcome recorded, and it used to carry a user-facing
    unit to done at the tier that demands the most evidence.
    """
    (spq_repo / ".synaptory.yaml").write_text(
        "build_mode: spq\n"
        "quality:\n"
        "  verification:\n"
        "    default: full\n"
        "spq:\n"
        "  workstreams:\n"
        '    - id: "spine"\n'
        "      shared_owner: true\n",
        encoding="utf-8",
    )
    story = sp.create_story(
        "WU-1", "Revenue dashboard",
        acceptance_criteria=["The dashboard page renders the revenue chart"],
    )
    _open_cycle_with_unit(spq_repo, story)
    _green_unit(spq_repo, "WU-1", {
        "coverage_delta": "+0.1%",
        "runtime_verification": {"deployed": True, "logs_inspected": True},
        "ui_verification": {"rendered": True},
    })
    dod, block = _gate(spq_repo, "WU-1")
    assert dod["ui_required"] is True, dod
    assert dod["checks"]["ui_acceptance"]["result"] == sp.CRITERIA_GAP_DECLARED
    assert block and "ui_acceptance" in block, block


# ── the count parser is total, and a count is a whole number (#396) ──────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "count",
    ["Infinity", "-Infinity", "NaN", "inf", "nan",
     float("inf"), float("-inf"), float("nan")],
    ids=["str-inf", "str-neg-inf", "str-nan", "str-inf-short", "str-nan-short",
         "inf", "neg-inf", "nan"],
)
def test_a_non_finite_count_gaps_instead_of_crashing_the_gate(count):
    """THE CRASH (#396). `_recorded_count` caught `ValueError` around
    `int(float(...))` and nothing else, so a valid JSON string like
    "Infinity" parsed to a non-finite float and `int()` raised
    `OverflowError`.

    `receipt_validator` only requires `metrics` to be a non-empty object, so
    this reached the real gate: `evaluate_story_dod` raised instead of
    returning a typed result, and an untrusted receipt took down the shared
    done-edge evaluation for every host. A parser whose whole job is making an
    untrusted value safe is the one place that cannot raise.
    """
    receipt = {"metrics": {"ui_verification": {
        "rendered": True, "routes_tested": count, "flows_failed": 0,
    }}}
    assert sp._evaluate_check("ui_acceptance", receipt) is None
    receipt = {"metrics": {"ui_verification": {
        "rendered": True, "routes_tested": 1, "flows_failed": count,
    }}}
    assert sp._evaluate_check("ui_acceptance", receipt) is None


@pytest.mark.unit
@pytest.mark.parametrize("count", ["1.5", 1.5, "0.5", 2.75])
def test_a_fractional_count_is_not_a_count(count):
    """`"1.5"` truncated to 1 and satisfied `routes_tested >= 1`, so a value
    the published shape calls an `int` was credited by rounding it into one. A
    count that was not recorded as a whole number was not recorded."""
    receipt = {"metrics": {"ui_verification": {
        "rendered": True, "routes_tested": count, "flows_failed": 0,
    }}}
    assert sp._evaluate_check("ui_acceptance", receipt) is None


@pytest.mark.unit
@pytest.mark.parametrize("count", [3, 3.0, "3", "  3  ", 0])
def test_a_whole_count_is_still_read(count):
    """The positive arm, so totality did not become refusal. `3.0` and `"3"`
    are whole numbers written differently, and a receipt is JSON."""
    assert sp._recorded_count(count) == int(count)


@pytest.mark.unit
def test_a_gate_evaluation_survives_a_non_finite_count(tmp_path):
    """End to end, because the finding was that the GATE raised rather than
    that a helper did. The typed result must come back, and it must be a gap.
    """
    project = _project(tmp_path)
    _unit(project, "WU-INF", metrics={
        "coverage_delta": "+1.0%",
        "ui_verification": {
            "rendered": True, "routes_tested": 1, "flows_failed": "Infinity",
        },
    })
    result = _dod(project, "WU-INF")
    assert isinstance(result, dict), result
    entry = result["checks"].get("ui_acceptance") or {}
    assert entry.get("passed") is not True, entry


@pytest.mark.unit
def test_an_absent_standard_tier_route_count_is_a_gap_not_a_failure():
    """UNRECORDED IS A GAP, NOT A FAILURE (#396).

    The standard tier returned False for an absent `routes_tested`, which
    asserts that a measurement was taken and came back bad. The same absence
    gaps at the `full` tier, and the contract text says an absent count is
    UNEVALUABLE, so the two tiers disagreed about the same missing evidence
    and the standard one named the wrong cause. Both block; only one sends the
    operator to the stage that owes the count.
    """
    absent = {"metrics": {"ui_verification": {"rendered": True}}}
    assert sp._evaluate_check(
        "ui_acceptance", absent, verification_tier="standard") is None
    assert sp._evaluate_check(
        "ui_acceptance", absent, verification_tier="full") is None
    # A recorded zero is the real failure: the stage measured, and found none.
    measured_zero = {"metrics": {"ui_verification": {
        "rendered": True, "routes_tested": 0,
    }}}
    assert sp._evaluate_check(
        "ui_acceptance", measured_zero, verification_tier="standard") is False
