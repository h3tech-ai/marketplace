"""Layer 1 — #487: the Compliance Engineer becomes checks declared on the gate.

The `SP-WRK-019` shape: a transition is refused until every check the workflow
declares has returned a result, and an absent, unfinished or errored result
counts as a failure rather than as a pass.

Three things this file is really guarding, in order of how much damage getting
them wrong would do:

1. **The PHI path did not weaken.** A PHI-touching Work Unit in a
   `baa_enforced` project still forces the compliance checks and still blocks
   without them. `test_removing_the_phi_promotion_breaks_this_file` makes the
   sensitivity explicit rather than hoping for it: it removes the promotion
   and asserts the fixture then passes, so a future edit that deletes the
   promotion cannot leave a green suite behind.

2. **An absent result is not a pass.** The nominal evaluator used to read
   `metrics.get("findings_critical", 0)`, so a compliance receipt that
   recorded no count at all scored a clean audit. That is the exact shape
   #403 forbids ("an unbacked claim is unbacked") and #445 forbids ("a
   member-supplied check result is self-reported, never computed").

3. **Someone can actually produce the evidence.** Removing a dispatch without
   moving its obligation leaves a gate whose remediation names a role nobody
   schedules, which is a deadlock rather than a gap. The declaration says who
   owes it, and the remediation routes there.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_HOOKS_LIB = _PLUGIN_ROOT / "hooks" / "lib"
if str(_HOOKS_LIB) not in sys.path:
    sys.path.insert(0, str(_HOOKS_LIB))

import story_pipeline as sp  # noqa: E402
import spq_state_machine as spq  # noqa: E402


_HEALTHCARE_YAML = "healthcare:\n  baa_enforced: true\n"
_PHI_ARTIFACT = "src/logger/audit.ts"
_PLAIN_ARTIFACT = "src/components/Button.tsx"


def _project(tmp_path: Path, yaml: str | None = None) -> Path:
    if yaml is not None:
        (tmp_path / ".synaptory.yaml").write_text(yaml, encoding="utf-8")
    (tmp_path / ".synaptory" / ".orchestrator" / "receipts").mkdir(
        parents=True, exist_ok=True
    )
    return tmp_path


def _receipts_dir(project: Path) -> str:
    return str(project / ".synaptory" / ".orchestrator" / "receipts")


def _write_receipt(project: Path, story_id: str, role: str, **fields) -> None:
    abbrev = sp._role_to_abbrev(role)
    payload = {"agent": role, **fields}
    Path(_receipts_dir(project), "%s-%s.json" % (story_id, abbrev)).write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _green_unit(project: Path, story_id: str, *, phi: bool) -> None:
    """SE + QE + CR receipts that clear everything EXCEPT compliance."""
    artifact = _PHI_ARTIFACT if phi else _PLAIN_ARTIFACT
    _write_receipt(
        project, story_id, "software-engineer",
        artifacts=[artifact],
        verification_commands=[{"command": "npm run build", "exit_code": 0}],
    )
    _write_receipt(
        project, story_id, "quality-engineer",
        artifacts=[artifact],
        verification_commands=[{"command": "npm test", "exit_code": 0}],
        metrics={
            "runtime_verification": {"deployed": True, "logs_inspected": True},
            "coverage_delta": "+0.4%",
        },
    )
    _write_receipt(
        project, story_id, "code-reviewer",
        artifacts=[artifact],
        status="complete",
        story_dod={"code_reviewed": True},
        verification_commands=[{"command": "npm run lint", "exit_code": 0}],
    )


def _dod(project: Path, story_id: str, intensity: str = "growing") -> dict:
    return sp.evaluate_story_dod(
        str(project), story_id, intensity, receipts_dir=_receipts_dir(project)
    )


def _check(result: dict, check_id: str = "no_critical_findings") -> dict:
    return result["checks"].get(check_id, {})


# ── 1. the obligation is DECLARED, not hard-coded at one call site ───────────


@pytest.mark.unit
def test_the_compliance_obligation_is_a_declared_set():
    """`COMPLIANCE_CHECKS` is the declaration the promotion iterates.

    Before #487 the promotion named one check inline, so "what does CE owe"
    had no single answer to read, quote to a dispatch, or extend.
    """
    assert isinstance(sp.COMPLIANCE_CHECKS, tuple)
    assert sp.COMPLIANCE_CHECKS
    for cid in sp.COMPLIANCE_CHECKS:
        assert cid in sp.DOD_CHECKS, "%s is declared but is not a DoD check" % cid


@pytest.mark.unit
def test_promotion_activates_every_declared_compliance_check():
    active = sp.active_dod_checks("early", compliance_required=True)
    for cid in sp.COMPLIANCE_CHECKS:
        assert cid in active
    quiet = sp.active_dod_checks("early")
    for cid in sp.COMPLIANCE_CHECKS:
        assert cid not in quiet


@pytest.mark.unit
def test_a_new_declared_check_is_promoted_without_touching_the_promotion():
    """The mechanism, not the current membership, is what this ticket adds."""
    original = sp.COMPLIANCE_CHECKS
    try:
        sp.COMPLIANCE_CHECKS = original + ("code_reviewed",)
        active = sp.active_dod_checks("early", compliance_required=True)
        assert "code_reviewed" in active
    finally:
        sp.COMPLIANCE_CHECKS = original


# ── 2. the PHI path, which must not weaken ───────────────────────────────────


@pytest.mark.unit
def test_phi_unit_in_a_baa_project_still_forces_the_compliance_checks(tmp_path):
    p = _project(tmp_path, _HEALTHCARE_YAML)
    _green_unit(p, "WU-1", phi=True)
    res = _dod(p, "WU-1")
    assert res["compliance_required"] is True
    for cid in sp.COMPLIANCE_CHECKS:
        assert res["checks"][cid]["required"] is True


@pytest.mark.unit
def test_phi_unit_still_blocks_without_the_compliance_result(tmp_path):
    """Green SE, QE and CR, and the unit still cannot reach done."""
    p = _project(tmp_path, _HEALTHCARE_YAML)
    _green_unit(p, "WU-2", phi=True)
    res = _dod(p, "WU-2")
    assert res["passed"] is False
    reason = sp.dod_gate_block_reason(res)
    assert reason is not None
    assert "no_critical_findings" in reason


@pytest.mark.unit
def test_removing_the_phi_promotion_breaks_this_file(tmp_path):
    """The sensitivity check the acceptance criterion asks for.

    Neutralize the promotion and the same fixture passes the gate. That is
    the whole content of "a test must fail if the promotion is removed": the
    assertions above are only load-bearing if their subject can be turned off
    and observed to matter.
    """
    p = _project(tmp_path, _HEALTHCARE_YAML)
    _green_unit(p, "WU-3", phi=True)
    assert sp.dod_gate_block_reason(_dod(p, "WU-3")) is not None

    original = sp.COMPLIANCE_CHECKS
    try:
        sp.COMPLIANCE_CHECKS = ()
        neutered = _dod(p, "WU-3")
        assert neutered["checks"]["no_critical_findings"]["required"] is False
        assert sp.dod_gate_block_reason(neutered) is None, (
            "the fixture does not actually depend on the compliance "
            "promotion, so the tests above prove nothing about it"
        )
    finally:
        sp.COMPLIANCE_CHECKS = original


@pytest.mark.unit
def test_a_non_phi_unit_in_the_same_baa_project_is_not_gated(tmp_path):
    p = _project(tmp_path, _HEALTHCARE_YAML)
    _green_unit(p, "WU-4", phi=False)
    res = _dod(p, "WU-4")
    assert res["compliance_required"] is False
    assert res["checks"]["no_critical_findings"]["required"] is False
    assert sp.dod_gate_block_reason(res) is None


@pytest.mark.unit
def test_a_phi_unit_outside_a_baa_project_is_not_gated(tmp_path):
    p = _project(tmp_path, "project:\n  name: plain\n")
    _green_unit(p, "WU-5", phi=True)
    res = _dod(p, "WU-5")
    assert res["compliance_required"] is False


# ── 3. an absent, unfinished or errored result is a failure, never a pass ────


@pytest.mark.unit
def test_a_compliance_receipt_with_no_count_is_a_gap_not_a_pass(tmp_path):
    """The vacuous pass #487 closes.

    `_evaluate_check` read `metrics.get("findings_critical", 0)`, so a
    receipt whose role name was `compliance-engineer` and whose metrics were
    empty scored a clean security audit. Two member-written strings clearing
    a security gate is precisely the forgery #445 named.
    """
    p = _project(tmp_path, _HEALTHCARE_YAML)
    _green_unit(p, "WU-6", phi=True)
    _write_receipt(p, "WU-6", "compliance-engineer", metrics={"findings_high": 3})
    res = _dod(p, "WU-6")
    entry = _check(res)
    assert entry["passed"] is not True
    assert entry["result"] == sp.CRITERIA_GAP_DECLARED
    assert sp.dod_gate_block_reason(res) is not None


@pytest.mark.unit
def test_a_compliance_receipt_with_no_metrics_at_all_is_a_gap(tmp_path):
    p = _project(tmp_path, _HEALTHCARE_YAML)
    _green_unit(p, "WU-7", phi=True)
    _write_receipt(p, "WU-7", "compliance-engineer", artifacts=["findings.md"])
    res = _dod(p, "WU-7")
    assert _check(res)["result"] == sp.CRITERIA_GAP_DECLARED


@pytest.mark.unit
def test_an_explicit_zero_still_passes(tmp_path):
    """Tightening absence must not break the honest producer."""
    p = _project(tmp_path, _HEALTHCARE_YAML)
    _green_unit(p, "WU-8", phi=True)
    _write_receipt(p, "WU-8", "compliance-engineer", metrics={"findings_critical": 0})
    res = _dod(p, "WU-8")
    assert _check(res)["passed"] is True
    assert _check(res)["result"] == sp.DOD_RESULT_PASS
    assert sp.dod_gate_block_reason(res) is None


@pytest.mark.unit
def test_a_nonzero_count_still_fails(tmp_path):
    p = _project(tmp_path, _HEALTHCARE_YAML)
    _green_unit(p, "WU-9", phi=True)
    _write_receipt(p, "WU-9", "compliance-engineer", metrics={"findings_critical": 2})
    res = _dod(p, "WU-9")
    assert _check(res)["passed"] is False
    assert _check(res)["result"] == sp.DOD_RESULT_FAIL


@pytest.mark.unit
def test_nobody_produces_it_and_the_unit_does_not_reach_done(tmp_path):
    """"What happens when nobody does" — stated, then proven.

    No compliance receipt, no findings count on the verifying receipt. The
    check declares a criteria gap, the gap blocks `reviewing -> done`, and
    the block text says what evidence is missing rather than resolving to a
    silent nothing that renders like a pass.
    """
    p = _project(tmp_path, _HEALTHCARE_YAML)
    _green_unit(p, "WU-10", phi=True)
    res = _dod(p, "WU-10")
    entry = _check(res)
    assert entry["result"] == sp.CRITERIA_GAP_DECLARED
    assert "findings_critical" in (entry.get("detail") or "")
    assert res["passed"] is False
    assert sp.dod_gate_block_reason(res) is not None


# ── 4. a self-reported result is never credited (#403 / #445) ────────────────


@pytest.mark.unit
def test_a_self_reported_compliance_pass_is_not_credited(tmp_path):
    """`Receipt.payload` is member-supplied. A claim is recorded, not counted."""
    p = _project(tmp_path, _HEALTHCARE_YAML)
    _green_unit(p, "WU-11", phi=True)
    _write_receipt(
        p, "WU-11", "compliance-engineer",
        artifacts=["findings.md"],
        dod_check_results=[
            {
                "check_id": "no_critical_findings",
                "result": "pass",
                "evidence_class": "attested",
            }
        ],
    )
    res = _dod(p, "WU-11")
    entry = _check(res)
    assert entry["result"] == sp.CRITERIA_GAP_DECLARED, (
        "a member-written result string was credited as a computed verdict"
    )
    assert entry.get("self_reported", {}).get("result") == "pass"
    assert sp.dod_gate_block_reason(res) is not None
    shipped = {
        item["check_id"]: item for item in res[sp.DOD_CHECK_RESULTS_KEY]
    }
    assert shipped["no_critical_findings"]["result"] == sp.CRITERIA_GAP_DECLARED


@pytest.mark.unit
def test_a_self_reported_claim_does_not_amplify_onto_other_checks(tmp_path):
    """#432's amplification fix, restated for the compliance claim."""
    p = _project(tmp_path, _HEALTHCARE_YAML)
    _green_unit(p, "WU-12", phi=True)
    _write_receipt(
        p, "WU-12", "compliance-engineer",
        metrics={"findings_critical": 0},
        dod_check_results=[
            {"check_id": "code_reviewed", "result": "fail"},
        ],
    )
    res = _dod(p, "WU-12")
    assert _check(res)["result"] == sp.DOD_RESULT_PASS
    assert res["checks"]["code_reviewed"]["result"] == sp.DOD_RESULT_PASS


# ── 5. who owes the result, and the remediation that names them ─────────────


@pytest.mark.unit
def test_the_accountable_role_is_the_verifying_stage_on_spq():
    assert sp.compliance_accountable_role("spq") == "cr"


@pytest.mark.unit
def test_scrum_and_kanban_keep_the_compliance_engineer_dispatch():
    """Proposal section 7: those two lifecycles are out of scope."""
    for mode in ("scrum", "kanban", "", None):
        assert sp.compliance_accountable_role(mode) == "ce"


@pytest.mark.unit
def test_the_declaration_states_the_shape_and_the_absent_result():
    decl = sp.compliance_declaration(
        baa_enforced=True, required=True, build_mode="spq"
    )
    assert decl["checks"] == list(sp.COMPLIANCE_CHECKS)
    assert decl["required"] is True
    assert decl["provisional"] is False
    assert decl["accountable_role"] == "cr"
    assert decl["absent_result"] == sp.CRITERIA_GAP_DECLARED
    assert "findings_critical" in decl["evidence"]["no_critical_findings"]


@pytest.mark.unit
def test_a_baa_project_declares_provisionally_before_the_signal_trips():
    """The obligation reaches the dispatch while it can still be acted on.

    `required` is computed from the receipts that exist so far, so on a
    BAA-enforced project it can flip false to true as the diff grows.
    Announcing the obligation only once it is true would announce it after
    the stage that had to produce the evidence already ran.
    """
    decl = sp.compliance_declaration(
        baa_enforced=True, required=False, build_mode="spq"
    )
    assert decl["provisional"] is True
    plain = sp.compliance_declaration(
        baa_enforced=False, required=False, build_mode="spq"
    )
    assert plain["provisional"] is False


@pytest.mark.unit
def test_the_block_reason_names_a_stage_that_is_actually_dispatched(tmp_path):
    """The deadlock this ticket had to avoid.

    Removing the per-unit compliance-engineer dispatch while the gate still
    said "dispatch the compliance-engineer" would leave a blocked unit whose
    only named recovery cannot be performed. The reason now names the stage
    the declaration says owes the result.
    """
    p = _project(tmp_path, _HEALTHCARE_YAML)
    (p / ".synaptory" / ".orchestrator").mkdir(parents=True, exist_ok=True)
    (p / ".synaptory" / ".orchestrator" / "pipeline-state.json").write_text(
        json.dumps({"build_mode": "spq", "current_stories": []}), encoding="utf-8"
    )
    _green_unit(p, "WU-13", phi=True)
    res = _dod(p, "WU-13")
    assert res["compliance"]["accountable_role"] == "cr"
    reason = sp.dod_gate_block_reason(res)
    assert "code-reviewer" in reason
    assert "-cr.json" in reason


@pytest.mark.unit
def test_the_block_reason_is_unchanged_in_substance_off_spq(tmp_path):
    p = _project(tmp_path, _HEALTHCARE_YAML)
    _green_unit(p, "WU-14", phi=True)
    res = _dod(p, "WU-14")
    assert res["compliance"]["accountable_role"] == "ce"
    reason = sp.dod_gate_block_reason(res)
    assert "compliance-engineer" in reason
    assert "-ce.json" in reason


@pytest.mark.unit
def test_gate_remediation_routes_to_the_declared_owner():
    story = {
        "id": "WU-15",
        "blocked_reason": "DoD gate: the declared compliance check "
                          "no_critical_findings has not returned a passing result",
    }
    assert sp._gate_remediation(story)["role"] == "ce"
    routed = sp._gate_remediation(story, accountable={"no_critical_findings": "cr"})
    assert routed["role"] == "cr"
    assert routed["gate"] == "no_critical_findings"


# ── 6. the declaration reaches the dispatch contract ────────────────────────


def _board(story_id: str, state: str) -> dict:
    return {
        "current_sprint": 2,
        "current_stories": [
            {"id": story_id, "title": "t", "state": state, "acceptance_criteria": []}
        ],
    }


@pytest.mark.unit
def test_next_action_declares_the_compliance_obligation(tmp_path):
    p = _project(tmp_path, _HEALTHCARE_YAML)
    _write_receipt(
        p, "WU-16", "software-engineer",
        artifacts=[_PHI_ARTIFACT],
        verification_commands=[{"command": "npm run build", "exit_code": 0}],
    )
    out = sp.next_action(
        _board("WU-16", "testing"),
        receipts_dir=_receipts_dir(p),
        compliance={
            "baa_enforced": True,
            "phi_signals": sp.DEFAULT_PHI_RISK_SIGNALS,
            "build_mode": "spq",
        },
    )
    decl = out["dod"]["compliance"]
    assert decl is not None
    assert decl["required"] is True
    assert decl["accountable_role"] == "cr"
    assert "no_critical_findings" in out["dod"]["declared_checks"]


@pytest.mark.unit
def test_the_declaration_agrees_with_the_gate_on_the_same_receipts(tmp_path):
    """One predicate, two readers.

    A declaration that could disagree with the gate would be worse than none:
    the dispatch would produce evidence for a check the gate never required
    and skip one it did.
    """
    p = _project(tmp_path, _HEALTHCARE_YAML)
    for story_id, phi in (("WU-17", True), ("WU-18", False)):
        _green_unit(p, story_id, phi=phi)
        out = sp.next_action(
            _board(story_id, "reviewing"),
            receipts_dir=_receipts_dir(p),
            compliance={
                "baa_enforced": True,
                "phi_signals": sp.DEFAULT_PHI_RISK_SIGNALS,
                "build_mode": "spq",
            },
        )
        gate = _dod(p, story_id)
        assert out["dod"]["compliance"]["required"] == gate["compliance_required"], (
            "%s: the dispatch declaration and the gate disagree" % story_id
        )


@pytest.mark.unit
def test_the_static_tier_list_is_not_quietly_redefined(tmp_path):
    """`active_checks` keeps meaning the tier set; `declared_checks` is new."""
    p = _project(tmp_path, _HEALTHCARE_YAML)
    _green_unit(p, "WU-19", phi=True)
    out = sp.next_action(
        _board("WU-19", "reviewing"),
        receipts_dir=_receipts_dir(p),
        compliance={
            "baa_enforced": True,
            "phi_signals": sp.DEFAULT_PHI_RISK_SIGNALS,
            "build_mode": "spq",
        },
    )
    assert "no_critical_findings" not in out["dod"]["active_checks"]
    assert "no_critical_findings" in out["dod"]["declared_checks"]


@pytest.mark.unit
def test_a_caller_that_passes_no_compliance_inputs_sees_no_declaration(tmp_path):
    p = _project(tmp_path, _HEALTHCARE_YAML)
    _green_unit(p, "WU-20", phi=True)
    out = sp.next_action(_board("WU-20", "reviewing"), receipts_dir=_receipts_dir(p))
    assert out["dod"]["compliance"] is None
    assert out["dod"]["declared_checks"] == out["dod"]["active_checks"]


@pytest.mark.unit
def test_a_blocked_phi_unit_routes_recovery_to_the_declared_owner(tmp_path):
    p = _project(tmp_path, _HEALTHCARE_YAML)
    _green_unit(p, "WU-21", phi=True)
    board = {
        "current_sprint": 2,
        "current_stories": [
            {
                "id": "WU-21",
                "title": "t",
                "state": "blocked",
                "blocked_reason": "DoD gate: the declared compliance check "
                                  "no_critical_findings has not returned a "
                                  "passing result",
            }
        ],
    }
    spq_out = sp.next_action(
        board,
        receipts_dir=_receipts_dir(p),
        compliance={
            "baa_enforced": True,
            "phi_signals": sp.DEFAULT_PHI_RISK_SIGNALS,
            "build_mode": "spq",
        },
    )
    assert spq_out["action"] == "recover_blocked"
    assert spq_out["role"] == "cr"

    legacy = sp.next_action(board, receipts_dir=_receipts_dir(p))
    assert legacy["role"] == "ce"


# ── 7. what did NOT dissolve ────────────────────────────────────────────────


@pytest.mark.unit
def test_ce_is_still_a_dispatch_identity_at_the_spq_acceptance_gate():
    """The scoping finding, pinned so it cannot be "tidied up" later.

    #487 removes the per-Work-Unit compliance dispatch. It does not remove CE
    from `ACCEPTANCE_ROLES`, which binds `ACCEPTANCE -> COMPLETE` to a real
    `ACCEPTANCE-{N}-ce.json`. A role named by a receipt-gated edge is still a
    dispatch identity there, by exactly the rule #405 used.
    """
    assert spq.ACCEPTANCE_ROLES["ce"] == "compliance-engineer"


@pytest.mark.unit
def test_the_acceptance_gate_still_refuses_without_the_ce_receipt(tmp_path):
    # `acceptance_readiness` reads the board itself now and reports the roles
    # still owed as `missing`, rather than taking a state dict and returning a
    # `blocking` list (#644). So the Cycle has to exist -- receipts are named
    # `ACCEPTANCE-{seq}-{abbrev}.json` under the Cycle's own receipts dir, and
    # a synthetic state dict could not say where that is. The guarantee this
    # test was written for is unchanged and asserted at the new spelling: the
    # gate is not ready, and the Compliance Engineer is one of the reasons.
    project = _project(tmp_path, "build_mode: spq\n")
    spq.initialize(str(project))
    spq.approve_baseline(str(project), approved_by="t", baseline_ref="b-1",
                         calibration={"sample_units": 1, "measured_hours": 1})
    spq.open_cycle(
        str(project), goal="c", repository="h3tech-ai/synaptory-v1",
        trunk_ref="refs/heads/dev", source_region=["api/"],
        engineering_lead="lead@h3t.co",
        admitted_units=[{
            "id": "WU-1", "title": "t", "kind": "story",
            "acceptance_criteria": ["it works"], "path_scope": ["api/wu-1/"],
        }],
    )
    readiness = spq.acceptance_readiness(str(project))
    assert readiness["ready"] is False
    assert spq.ACCEPTANCE_ROLES["ce"] in readiness["missing"], readiness


@pytest.mark.unit
def test_the_compliance_engineer_agent_stub_still_ships():
    """Skill-ification is not deletion: the host still needs the entry point."""
    agent_dir = _PLUGIN_ROOT / "agents" / "compliance-engineer"
    assert (agent_dir / "agent.md").is_file()
    assert (agent_dir / "SKILL.md").is_file()
    assert (agent_dir / "guides" / "security-playbook.md").is_file()


# ── 8. the method the accountable stage has to fetch ────────────────────────


def _cp_name_mapper():
    """The control plane's real `_skill_name_for`, lifted from seed.py's AST.

    Same technique as `test_agent_skill_catalog`, and for the same reason: a
    Layer-1 test must not import sqlalchemy, and restating the mapper's rule
    in a second place is how a name the SKILL offers and the CLI does not
    serve slips through.
    """
    import ast

    source = (
        _PLUGIN_ROOT.parent / "api" / "synaptory_api" / "seed.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    module = ast.Module(body=[], type_ignores=[])
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_skill_name_for":
            module.body.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id.isupper() for t in node.targets
        ):
            try:
                ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                continue
            module.body.append(node)
    ns: dict = {"Path": Path}
    exec(compile(module, "<seed>", "exec"), ns)
    return ns["_skill_name_for"]


#: Names the code-reviewer is told to fetch when the gate declares the
#: compliance check. They live under ANOTHER role's directory, and the
#: catalog guards in `test_agent_skill_catalog` skip cross-role names
#: (`if owner != role: continue`), so nothing there would have caught a 404.
CROSS_ROLE_COMPLIANCE_NAMES = (
    "compliance-engineer/guides/security-playbook",
    "compliance-engineer/modes/healthcare",
)


@pytest.mark.unit
def test_the_reviewer_is_pointed_at_the_compliance_method():
    text = (_PLUGIN_ROOT / "agents" / "code-reviewer" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "compliance-engineer/guides/security-playbook" in text, (
        "the code-reviewer owes the declared compliance result and has no "
        "route to the method that produces it"
    )


@pytest.mark.unit
@pytest.mark.parametrize("name", CROSS_ROLE_COMPLIANCE_NAMES)
def test_the_cross_role_compliance_names_resolve_on_both_routes(name: str):
    body = _PLUGIN_ROOT / "agents" / (name + ".md")
    assert body.is_file(), "%s has no body on disk (the {{path:}} fallback 404s)" % name
    served = _cp_name_mapper()(body, _PLUGIN_ROOT)
    assert served == name, (
        "the control plane serves this body as %r, so `synaptory skills get "
        "%s` 404s" % (served, name)
    )


@pytest.mark.unit
def test_the_playbook_carries_the_audit_method_not_just_a_pointer():
    playbook = (
        _PLUGIN_ROOT / "agents" / "compliance-engineer" / "guides"
        / "security-playbook.md"
    ).read_text(encoding="utf-8")
    for marker in ("STRIDE", "OWASP", "Severity Classification", "issues.json"):
        assert marker in playbook, "the playbook lost %r in the move" % marker


@pytest.mark.unit
def test_the_skill_states_who_owes_the_declared_result():
    text = (
        _PLUGIN_ROOT / "agents" / "compliance-engineer" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "declared check" in text.lower()
    assert "ACCEPTANCE_ROLES" in text, (
        "the SKILL must say where CE still dispatches, or the next reader "
        "concludes the role was retired outright"
    )


@pytest.mark.unit
def test_the_reviewer_envelope_declares_the_compliance_obligation():
    """The obligation reaches the agent through the injected envelope too."""
    import evidence_contract as ec

    rendered = ec.render_envelope("synaptory:code-reviewer")
    assert "no_critical_findings" in rendered
    assert "findings_critical" in rendered


@pytest.mark.unit
def test_the_contract_no_longer_documents_the_absent_key_default():
    """The predicate text was the documented form of the vacuous pass."""
    import evidence_contract as ec

    spec = ec.evidence_spec("no_critical_findings")
    assert "absent key defaults to 0" not in spec["predicate"]
    assert "never a pass" in spec["predicate"]
