"""Layer 1 — #44 phase 5: the per-story integration-claim gate.

Pins the behaviour that closes the "stubbed integration counted as wired" gap
(the hano-app / ngaythobet failure mode, also called out in issue #44):

  When a story CLAIMS an external service is wired / connected / integrated
  (Entra SSO, a third-party API, a webhook), a green SE/QE suite is not enough.
  The `integration_verified` gate is promoted and is fail-closed: it passes only
  when every claimed integration appears in a receipt's `integrations[]` as
  `status: "live"` backed by an executed smoke verification_command
  (exit_code 0). A stubbed / placeholder-cred integration does not satisfy the
  AC, so the story is held out of `done`.

Stories that make no integration claim are unaffected (regression guard) — even
when they carry an honest, un-claimed stub.
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


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _project(tmp_path: Path) -> Path:
    receipts = tmp_path / ".synaptory" / ".orchestrator" / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write_receipt(project: Path, story_id: str, role: str, **fields) -> None:
    receipts = project / ".synaptory" / ".orchestrator" / "receipts"
    abbrev = sp._role_to_abbrev(role)
    payload = {"agent": role, **fields}
    (receipts / f"{story_id}-{abbrev}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _se_qe_pass(project: Path, story_id: str, *, se_fields=None, qe_fields=None) -> None:
    """SE + QE receipts that satisfy the static tests_pass + build_succeeds tier.

    `se_fields` / `qe_fields` are merged over the defaults, so a caller can
    override `verification_commands` (e.g. to add a live smoke) without a
    duplicate-keyword clash.
    """
    se = {
        "artifacts": ["src/auth/entra.ts"],
        "verification_commands": [{"command": "npm run build", "exit_code": 0}],
        **(se_fields or {}),
    }
    qe = {
        "artifacts": ["src/auth/entra.test.ts"],
        "verification_commands": [{"command": "npm test", "exit_code": 0}],
        **(qe_fields or {}),
    }
    _write_receipt(project, story_id, "software-engineer", **se)
    _write_receipt(project, story_id, "quality-engineer", **qe)


def _write_story_spec(project: Path, story_id: str, *, title: str,
                      acceptance_criteria: list[str]) -> None:
    """Seed pipeline state so the gate can read the story's title and ACs.

    `evaluate_story_dod` resolves the spec through `_story_claims_integration_
    in_state`, which reads pipeline-state.json — a spec-level claim cannot be
    passed in as an argument.
    """
    orch = project / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True, exist_ok=True)
    (orch / "pipeline-state.json").write_text(
        json.dumps({"current_stories": [{
            "id": story_id, "title": title, "state": "reviewing",
            "acceptance_criteria": acceptance_criteria,
        }]}),
        encoding="utf-8",
    )


def _dod(project: Path, story_id: str, intensity: str = "early") -> dict:
    receipts = str(project / ".synaptory" / ".orchestrator" / "receipts")
    return sp.evaluate_story_dod(str(project), story_id, intensity, receipts_dir=receipts)


# ---------------------------------------------------------------------------
# Claim detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_claim_detected_in_receipt_summary_when_an_integration_is_declared():
    # Prose + a declaration anywhere in the receipt set still promotes the gate.
    receipts = [
        {"agent": "software-engineer", "summary": "Wire up Entra SSO login",
         "integrations": [{"name": "entra-sso", "status": "stubbed"}]},
    ]
    assert sp.story_claims_integration(receipts) is True


@pytest.mark.unit
def test_claim_corroboration_may_come_from_another_receipt():
    # Splitting claim and declaration across roles is normal: the SE says it,
    # the QE proves it. Pairing them per-receipt would fail such a story closed
    # for a bookkeeping reason.
    receipts = [
        {"agent": "software-engineer", "summary": "Wire up Entra SSO login"},
        {"agent": "quality-engineer",
         "integrations": [{"name": "entra-sso", "status": "live"}]},
    ]
    assert sp.story_claims_integration(receipts) is True


@pytest.mark.unit
def test_receipt_prose_alone_does_not_promote_the_gate():
    """#235: prose with no declaration anywhere must NOT promote the gate.

    This replaces an earlier assertion that it *did*. The old behaviour was
    unrecoverable rather than strict: `evaluate_integration_claims` returns None
    when nothing is declared, a required check that is not True blocks, and
    `integration_verified` is deliberately excluded from _WAIVABLE_DOD_CHECKS.
    So this combination could only ever block and never pass, leaving two ways
    out — a PO acceptance, or deleting the offending word from the receipt.

    It also fired constantly on ordinary English for purely internal work.
    Discovered when a pure-library story ("filter_by_tags() ... wired into
    TaskStore.list") was hard-blocked with no external service anywhere near it.
    Teaching agents which verbs to avoid is the opposite of what receipts are
    for, so the claim now needs a declaration to corroborate it.
    """
    receipts = [{"agent": "software-engineer",
                 "summary": "Wire up Entra SSO login"}]  # nothing declared
    assert sp.story_claims_integration(receipts) is False


@pytest.mark.unit
def test_internal_wiring_prose_does_not_promote_the_gate():
    # The #235 reproduction, verbatim in shape: a local-only change whose
    # receipt describes internal call wiring.
    receipts = [{
        "agent": "software-engineer",
        "task": ("BM-1 Added conjunctive tag filtering: filter_by_tags() in "
                 "todo/query.py, wired into TaskStore.list(tags=[...])"),
        "artifacts": ["todo/query.py", "todo/store.py"],
    }]
    assert sp.story_claims_integration(
        receipts, title="Add tag filtering to TaskStore.list"
    ) is False


@pytest.mark.unit
def test_spec_level_claim_still_promotes_without_any_declaration():
    # The spec is authoritative and needs no corroboration: an AC that promises
    # a third-party service is wired keeps failing closed, which is where the
    # #44 contract actually belongs.
    assert sp.story_claims_integration(
        [], title="Auth",
        acceptance_criteria=["Entra SSO is wired to the real tenant"],
    ) is True


@pytest.mark.unit
def test_claim_detected_in_acceptance_criteria():
    assert sp.story_claims_integration(
        [], title="Auth", acceptance_criteria=["User can log in once Entra SSO is wired to the real tenant"]
    ) is True


@pytest.mark.unit
def test_no_claim_for_plain_backend_story():
    receipts = [{"agent": "software-engineer", "summary": "Add a sort helper to the list util"}]
    assert sp.story_claims_integration(receipts, title="Sort helper") is False


@pytest.mark.unit
@pytest.mark.parametrize("text", [
    "Add a webhook endpoint",          # 'webhook' is a noun, not a claim
    "Add integration tests for the API",  # 'integration tests' must not fire
    "Parse the OAuth token in middleware",
    "Add SSO config schema",
])
def test_service_nouns_do_not_trigger_claim(text):
    # Regression: the gate keys on claim VERBS, not service nouns — ordinary
    # backend work must not be force-gated.
    assert sp.story_claims_integration([], title=text) is False


# ---------------------------------------------------------------------------
# evaluate_integration_claims — the across-receipts evaluator
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_eval_none_when_no_integrations_declared():
    # Claimed (requiredness handled upstream) but never declared → unverified.
    assert sp.evaluate_integration_claims([{"agent": "software-engineer"}]) is None


@pytest.mark.unit
def test_eval_false_for_stubbed():
    receipts = [{
        "agent": "software-engineer",
        "integrations": [{"name": "entra-sso", "status": "stubbed", "evidence": "placeholder creds"}],
        "verification_commands": [{"command": "npm test", "exit_code": 0}],
    }]
    assert sp.evaluate_integration_claims(receipts) is False


@pytest.mark.unit
def test_eval_false_for_live_without_smoke():
    receipts = [{
        "agent": "software-engineer",
        "integrations": [{"name": "entra-sso", "status": "live", "evidence": "claims it works"}],
        "verification_commands": [{"command": "npm run build", "exit_code": 0}],
    }]
    # build passing is not a smoke that reached the service — but our contract
    # requires *a* passing executed command on the carrying receipt. Use a
    # receipt with NO passing command to prove the live-without-proof failure:
    no_smoke = [{
        "agent": "software-engineer",
        "integrations": [{"name": "entra-sso", "status": "live"}],
        "verification_commands": [{"command": "curl ...", "exit_code": 7}],
    }]
    assert sp.evaluate_integration_claims(no_smoke) is False


@pytest.mark.unit
def test_eval_true_for_live_with_passing_smoke():
    receipts = [{
        "agent": "quality-engineer",
        "integrations": [{
            "name": "football-data.org", "status": "live",
            "evidence": "GET /v4/competitions → 200, see verification_commands[0]",
        }],
        "verification_commands": [{"command": "curl -fsS https://api.football-data.org/v4/competitions", "exit_code": 0}],
    }]
    assert sp.evaluate_integration_claims(receipts) is True


# ---------------------------------------------------------------------------
# evaluate_story_dod integration + the enforcement helper the state machines use
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_claim_story_unaffected(tmp_path):
    # Regression: a plain backend story's gate is exactly the static tier, even
    # when it carries an honest un-claimed stub.
    p = _project(tmp_path)
    _se_qe_pass(
        p, "US-1",
        se_fields={
            "summary": "Add list sort helper",
            "integrations": [{"name": "future-payments", "status": "stubbed"}],
        },
    )
    res = _dod(p, "US-1")
    assert res["integration_required"] is False
    assert res["checks"]["integration_verified"]["required"] is False
    assert res["passed"] is True
    assert sp.dod_gate_block_reason(res) is None


@pytest.mark.unit
def test_prose_claim_without_declaration_no_longer_traps_the_story(tmp_path):
    """#235: replaces a test that asserted this combination fails closed.

    It did fail closed, but with no way back open — see
    `test_receipt_prose_alone_does_not_promote_the_gate` for why that was a trap
    rather than strictness. The story now proceeds on its static tier.
    """
    p = _project(tmp_path)
    _se_qe_pass(p, "US-2", se_fields={"summary": "Wire up Entra SSO"})  # no integrations[]
    res = _dod(p, "US-2")
    assert res["integration_required"] is False
    assert res["checks"]["integration_verified"]["required"] is False
    assert res["passed"] is True
    assert sp.dod_gate_block_reason(res) is None


@pytest.mark.unit
def test_spec_claim_without_declaration_still_fails_closed(tmp_path):
    """The fail-closed behaviour survives where the claim belongs: the spec.

    This is the half of `test_claim_without_declaration_fails_closed` worth
    keeping. An AC that promises a live third-party service, with nothing
    declared to back it, still blocks — so the #44 contract is not weakened,
    only moved off incidental receipt prose.
    """
    p = _project(tmp_path)
    _se_qe_pass(p, "US-2b", se_fields={"summary": "Add the auth module"})
    # evaluate_story_dod reads the title/ACs from pipeline state, so the spec
    # claim has to live there rather than being passed in.
    _write_story_spec(
        p, "US-2b", title="Auth",
        acceptance_criteria=["Entra SSO is wired to the real tenant"],
    )
    res = _dod(p, "US-2b")
    assert res["integration_required"] is True
    assert res["checks"]["integration_verified"]["required"] is True
    assert res["passed"] is False
    reason = sp.dod_gate_block_reason(res)
    assert reason and "integration_verified" in reason


@pytest.mark.unit
def test_claim_stubbed_blocks(tmp_path):
    p = _project(tmp_path)
    _se_qe_pass(
        p, "US-3",
        se_fields={
            "summary": "Integrate Entra SSO",
            "integrations": [{"name": "entra-sso", "status": "stubbed", "evidence": "pending tenant creds"}],
        },
    )
    res = _dod(p, "US-3")
    assert res["passed"] is False
    assert sp.dod_gate_block_reason(res) is not None


@pytest.mark.unit
def test_claim_live_with_smoke_passes(tmp_path):
    p = _project(tmp_path)
    _se_qe_pass(
        p, "US-4",
        se_fields={"summary": "Wire up Entra SSO"},
        qe_fields={
            "integrations": [{"name": "entra-sso", "status": "live", "evidence": "token exchange 200"}],
            "verification_commands": [
                {"command": "npm test", "exit_code": 0},
                {"command": "curl -fsS https://login.microsoftonline.com/.../token", "exit_code": 0},
            ],
        },
    )
    res = _dod(p, "US-4")
    assert res["integration_required"] is True
    assert res["checks"]["integration_verified"]["passed"] is True
    assert res["passed"] is True
    assert sp.dod_gate_block_reason(res) is None


@pytest.mark.unit
def test_active_checks_promote_integration():
    checks = sp.active_dod_checks("early", integration_required=True)
    assert "integration_verified" in checks
    # Unchanged without the flag.
    assert "integration_verified" not in sp.active_dod_checks("early")
