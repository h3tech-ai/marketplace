"""Layer 1 — the DoD block a dispatch prompt states (#501).

`render_envelope` used to accept `--active-checks` and `--tier`, and the only
caller passed neither, so agents were graded on a contract they were never
shown. The fix moves the computation behind a story handle the caller can
actually supply. These tests pin the two properties that make that honest:

  1. The dispatch side and the GATE side compute promotion from one body of
     code (`story_gate_promotions`), so a prompt can never state a check set
     the gate does not use.
  2. A check that is "not promoted yet" is reported separately from one that
     "will never be promoted". Two of the promotions read receipts that do
     not exist at SE dispatch, so collapsing them into a single
     `active_checks` list would present an unmeasured gate as an absent one.
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

_HEALTHCARE_YAML = "healthcare:\n  baa_enforced: true\n"


def _project(tmp_path: Path, stories: list[dict], *, yaml: str | None = None,
             **top) -> Path:
    if yaml is not None:
        (tmp_path / ".synaptory.yaml").write_text(yaml, encoding="utf-8")
    orch = tmp_path / ".synaptory" / ".orchestrator"
    (orch / "receipts").mkdir(parents=True, exist_ok=True)
    state = {"current_stories": stories}
    state.update(top)
    (orch / "pipeline-state.json").write_text(json.dumps(state), encoding="utf-8")
    return tmp_path


@pytest.mark.unit
def test_dispatch_states_the_promoted_gate_not_just_the_tier_list(tmp_path):
    """A promoted gate must reach the prompt, or the agent is graded blind.

    `ui_acceptance` is in NO tier list — it exists only as a promotion. A
    dispatch block that reported the tier's static checks would omit it and
    look complete while doing so.
    """
    project = _project(
        tmp_path,
        [{"id": "US-1", "title": "Checkout screen", "state": "in_progress",
          "ui_bearing": True}],
        dod_tier={"tier": "mature", "decided_by": "po"},
    )
    dod = sp.dispatch_dod_contract(str(project), "US-1")

    assert dod["tier"] == "mature"
    assert dod["tier_source"] == "planned"
    assert "coverage_no_decrease" in dod["active_checks"]
    assert "ui_acceptance" in dod["active_checks"]


@pytest.mark.unit
def test_dispatch_and_gate_agree_on_promotion(tmp_path):
    """One computation, two readers.

    What this asserts is impossible: a dispatch prompt stating a check set the
    gate would not use. The two used to be separate code paths only because
    the dispatch side did not exist; a second copy is the drift this repo
    keeps paying for elsewhere.
    """
    project = _project(
        tmp_path,
        [{"id": "US-1", "title": "Settings page", "state": "in_progress",
          "ui_bearing": True}],
    )
    receipts_dir = str(project / ".synaptory" / ".orchestrator" / "receipts")
    receipts = sp.collect_story_receipts(receipts_dir, "US-1")

    promotions = sp.story_gate_promotions(str(project), "US-1", receipts)
    dod = sp.dispatch_dod_contract(str(project), "US-1")
    expected = sp.active_dod_checks(
        dod["tier"],
        compliance_required=promotions["compliance_required"],
        runtime_required=promotions["runtime_required"],
        ui_required=promotions["ui_required"],
        integration_required=promotions["integration_required"],
    )
    assert dod["active_checks"] == expected

    # And the gate itself reports the same promotion flags for the story.
    evaluated = sp.evaluate_story_dod(str(project), "US-1", dod["tier"])
    for flag in ("compliance_required", "runtime_required", "ui_required",
                 "integration_required"):
        assert evaluated[flag] == promotions[flag], flag


@pytest.mark.unit
def test_unmeasurable_phi_promotion_is_undetermined_not_absent(tmp_path):
    """On a BAA project the PHI gates are unmeasured at SE dispatch, not off.

    `no_critical_findings` and `runtime_verified` are promoted by PHI detected
    IN THE RECEIPTS — which, at an SE dispatch, do not exist yet. Reporting
    only `active_checks` would tell the SE those gates do not apply, using the
    same text it would use for a story where they genuinely never will.
    """
    project = _project(
        tmp_path,
        [{"id": "US-1", "title": "Patient record sync", "state": "in_progress",
          "kind": "backend"}],
        yaml=_HEALTHCARE_YAML,
    )
    dod = sp.dispatch_dod_contract(str(project), "US-1")

    assert "no_critical_findings" not in dod["active_checks"]
    undetermined = {e["check"] for e in dod["undetermined_checks"]}
    assert "no_critical_findings" in undetermined
    assert "runtime_verified" in undetermined
    # And it must say what would activate them, not merely list them.
    for entry in dod["undetermined_checks"]:
        assert entry["why"].strip(), entry


@pytest.mark.unit
def test_non_healthcare_project_does_not_report_phi_gates_as_unmeasured(tmp_path):
    """The other half of the zero rule.

    On a project without `baa_enforced` the PHI gates are DETERMINED absent —
    no future receipt can promote them. Listing them as undetermined would
    make the honest signal indistinguishable from noise, and a signal that
    fires everywhere is read nowhere.
    """
    project = _project(
        tmp_path,
        [{"id": "US-1", "title": "Rename a column", "state": "in_progress",
          "kind": "backend"}],
    )
    dod = sp.dispatch_dod_contract(str(project), "US-1")

    undetermined = {e["check"] for e in dod["undetermined_checks"]}
    assert "no_critical_findings" not in undetermined
    assert "runtime_verified" not in undetermined
    # The integration gate stays undetermined everywhere: any receipt can
    # claim an external service is wired, and that claim is what promotes it.
    assert "integration_verified" in undetermined


@pytest.mark.unit
def test_promotion_failure_reports_unmeasured_rather_than_empty(
    tmp_path, monkeypatch
):
    """A promotion it could not evaluate must not come back as "no gates".

    The failure path is where the zero rule is easiest to break: the obvious
    `except: return out` leaves `undetermined_checks` empty, which reads as a
    measurement that found nothing. Every receipt-dependent gate has to be
    named as unevaluated instead.
    """
    project = _project(
        tmp_path, [{"id": "US-1", "title": "X", "state": "in_progress"}]
    )

    def _boom(*_a, **_k):
        raise RuntimeError("state store unavailable")

    monkeypatch.setattr(sp, "story_gate_promotions", _boom)
    dod = sp.dispatch_dod_contract(str(project), "US-1")

    assert dod["tier"]  # a tier is always stated
    undetermined = {e["check"] for e in dod["undetermined_checks"]}
    assert {"no_critical_findings", "runtime_verified",
            "integration_verified"} <= undetermined
    assert all(
        "could not be evaluated" in e["why"] for e in dod["undetermined_checks"]
    )


@pytest.mark.unit
def test_absent_project_config_determines_phi_gates_absent(tmp_path):
    """A missing `.synaptory.yaml` is a config default, not a failed read.

    `healthcare.baa_enforced` absent means the project is not regulated, which
    is a DETERMINATION. It must not be laundered into "unmeasured" — that
    would make the undetermined list fire on every ordinary project, which is
    the same failure as never firing.
    """
    dod = sp.dispatch_dod_contract(str(tmp_path / "no-such-project"), "US-1")

    undetermined = {e["check"] for e in dod["undetermined_checks"]}
    assert "no_critical_findings" not in undetermined
    assert undetermined == {"integration_verified"}
