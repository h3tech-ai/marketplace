"""Layer 1 -- profile-keyed dispatch and advance (#402, Epic #410).

ADR-032 decision 1: dispatch and advance key on
`(stage_profile, capability_profile)` and the nine legacy role names survive as
wire aliases resolved through one table in `runtime_contracts`. These tests pin
the four properties that make that real rather than a relabelling:

1. **the keying is resolved, not restated.** `expected_stage` projects the
   edge's alias onto its pair through `profile_pair_for_role` on every call, so
   moving a role in the projection tables moves what the gate expects. The
   discriminating test monkeypatches the alias table and asserts the refusal
   follows it; a gate still keying on the bare role name passes the same
   receipt either way and fails that test.
2. **a present overlay pair cannot contradict the edge.** #342 stamped
   `stage_profile` / `capability_profile` on receipts and nothing read them.
   The control plane does (#407 ingest, gate depth on /quality), so a
   producing stage able to self-declare `capability_profile: prover` would have
   its own output ingested as verification of itself.
3. **`accountable_role` is soft-required**: validated against the alias table
   when present, an unknown value refuses, absence never does.
4. **absence is a warning HERE, on an ungoverned dispatch.** Every project in
   this module is scrum with no `runtimes:` section, so its dispatches hand
   over no envelope and its agents have nothing to copy; refusing them would
   refuse the whole pipeline. #473 tightened absence to a refusal for the
   governed case, where an envelope IS handed over, and it lives in
   `receipt_validator` rather than here -- see
   `tests/hooks/test_governed_overlay_required.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import advance_kernel as ak
import evidence_contract as ec
import runtime_contracts as rc
import story_pipeline as sp


pytestmark = pytest.mark.unit

STAGE_ENTERED = "2026-01-01T00:00:00+00:00"
FUTURE = "2099-01-01T00:00:00Z"

REPO_ROOT = Path(__file__).resolve().parents[3]


# ── fixtures ─────────────────────────────────────────────────────────────────


def _project(tmp_path: Path) -> Path:
    (tmp_path / ".synaptory" / ".orchestrator" / "receipts").mkdir(
        parents=True, exist_ok=True
    )
    (tmp_path / ".synaptory.yaml").write_text("build_mode: scrum\n", encoding="utf-8")
    return tmp_path


def _seed(
    project: Path,
    *,
    story_id: str = "US-001",
    state: str = "testing",
    sprint: int = 2,
) -> dict:
    story = {
        "id": story_id,
        "title": "Seed story",
        "state": state,
        "blocked_reason": None,
        "blocked_from": None,
        "backend": {},
        "pipeline_log": [
            {"state": state, "entered_at": STAGE_ENTERED, "exited_at": None}
        ],
        "dod": None,
        "receipts": [],
        "acceptance_criteria": [],
        "kind": "",
        "labels": [],
        "depends_on": [],
        "file_scope": [],
        "retries": {},
        "rejection_feedback": [],
    }
    payload = {
        "version": "2.0",
        "build_mode": "scrum",
        "lifecycle_state": "SPRINT_EXECUTION",
        "current_sprint": sprint,
        "cumulative_ticket_number": 1,
        "sprint_goal": "seed",
        "pipeline_log": [],
        "current_stories": [story],
    }
    path = project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def _receipt(
    project: Path,
    *,
    story_id: str = "US-001",
    role: str = "quality-engineer",
    abbrev: str = "qe",
    **overrides,
) -> Path:
    receipts = project / ".synaptory" / ".orchestrator" / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    src = project / "src" / "foo.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("x\n", encoding="utf-8")
    payload = {
        "story_id": story_id,
        "role": role,
        "backend": "claude",
        "model": "opus",
        "artifacts": ["src/foo.py"],
        "verification_commands": ["true"],
        "metrics": {"n": 1},
        "completed_at": FUTURE,
        "token_usage": {
            "input": 1,
            "output": 1,
            "cache_read": 0,
            "cache_write": 0,
            "stage": "qe-verification",
        },
        "story_dod": {
            "tests_pass": True,
            "build_succeeds": True,
            "no_critical_findings": True,
            "code_reviewed": True,
            "coverage_no_decrease": True,
        },
    }
    payload.update(overrides)
    path = receipts / ("%s-%s.json" % (story_id, abbrev))
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return path


def _policy(**kwargs) -> ak.HostPolicy:
    kwargs.setdefault("host", "test")
    kwargs.setdefault("require_next_action_match", False)
    return ak.HostPolicy(**kwargs)


def _advance(project: Path, **policy_kwargs) -> ak.Decision:
    """Evaluate the `testing -> reviewing` edge, the verifying/prover stage."""
    return ak.evaluate_advance(
        str(project), "US-001", "reviewing", policy=_policy(**policy_kwargs)
    )


# ── 1. the edge resolves to a pair, through the alias table ──────────────────


class TestEdgeProfileResolution:
    def test_every_gated_edge_projects_onto_a_pair_that_projects_back(self):
        """One table, both ways. If an edge's alias did not appear among the
        roles its own pair projects back to, the two directions would have
        drifted and the reverse lookup used for refusal messages would name a
        set the edge is not in."""
        for (from_state, to_state), (_role, abbrev) in ak.TRANSITION_RECEIPT.items():
            expected = ak.expected_stage(from_state, to_state)
            assert expected is not None, (from_state, to_state)
            assert expected.abbrev == abbrev
            assert expected.pair == rc.profile_pair_for_role(abbrev)
            assert abbrev in rc.roles_for_profile_pair(*expected.pair)

    def test_blocked_edges_resolve_too(self):
        for from_state, (_role, abbrev) in ak.BLOCKED_FROM_ROLE.items():
            expected = ak.expected_stage(from_state, "blocked")
            assert expected is not None, from_state
            assert expected.pair == rc.profile_pair_for_role(abbrev)

    def test_the_pair_is_named_alongside_the_legacy_role(self):
        expected = ak.expected_stage("testing", "reviewing")
        assert expected.as_dict() == {
            "stage_profile": "verifying",
            "capability_profile": "prover",
            "role": "quality-engineer",
            "role_abbrev": "qe",
        }

    def test_the_two_verifying_stages_share_a_pair(self):
        """Which is exactly why the alias survives and `ROLE_MISMATCH` is not
        replaced: `qe` and `cr` both project onto verifying/prover, so the pair
        names what the evidence must be and the edge names which alias
        produced it."""
        qe = ak.expected_stage("testing", "reviewing")
        cr = ak.expected_stage("reviewing", "done")
        assert qe.pair == cr.pair == ("verifying", "prover")
        assert qe.abbrev != cr.abbrev

    def test_the_bare_alias_accessor_still_answers_for_host_adapters(self):
        """Three host adapters destructure `(role, abbrev)`. Gaining a profile
        must not change the shape they already read."""
        assert ak.expected_receipt_role("testing", "reviewing") == (
            "quality-engineer",
            "qe",
        )

    def test_an_edge_with_no_receipt_role_has_no_pair(self):
        assert ak.expected_stage("queued", "done") is None

    def test_the_kernel_no_longer_keeps_its_own_role_table(self):
        """#399 pinned a byte-identical mirror with a test. #402 removed the
        mirror: the kernel re-exports the alias table, so `is` holds and
        nothing can drift."""
        assert ak.ROLE_NAMES is rc.ROLE_NAMES


# ── 2. the keying is resolved per call, not restated ─────────────────────────


class TestKeyingIsResolvedThroughTheAliasTable:
    """The discriminating tests. Each one moves a role inside
    `runtime_contracts`' projection tables and asserts the gate's expectation
    and refusal move with it. A kernel that compared the receipt's role name to
    a literal in `TRANSITION_RECEIPT` would behave identically before and
    after the patch, and would fail every test in this class."""

    def test_moving_the_role_moves_what_the_edge_expects(self, monkeypatch):
        monkeypatch.setitem(rc.DISPATCH_CAPABILITY_PROFILE, "qe", "planner")
        monkeypatch.setitem(rc.DISPATCH_STAGE_PROFILE, "qe", "planning")
        expected = ak.expected_stage("testing", "reviewing")
        assert expected.pair == ("planning", "planner")
        assert expected.role == "quality-engineer", (
            "the wire alias is unchanged; only the pair it projects onto moved"
        )

    def test_a_receipt_stamped_with_the_old_pair_is_refused_after_the_move(
        self, tmp_path, monkeypatch
    ):
        project = _project(tmp_path)
        _seed(project)
        _receipt(project, stage_profile="verifying", capability_profile="prover")
        assert _advance(project).allowed, "the unpatched pair is the edge's pair"

        monkeypatch.setitem(rc.DISPATCH_CAPABILITY_PROFILE, "qe", "planner")
        monkeypatch.setitem(rc.DISPATCH_STAGE_PROFILE, "qe", "planning")
        refused = _advance(project)
        assert not refused.allowed
        assert refused.code == ak.PROFILE_MISMATCH
        assert "planning" in refused.reason

    def test_a_receipt_stamped_with_the_new_pair_advances_after_the_move(
        self, tmp_path, monkeypatch
    ):
        """The other direction, so the test cannot pass by refusing
        everything."""
        project = _project(tmp_path)
        _seed(project)
        _receipt(project, stage_profile="planning", capability_profile="planner")
        assert not _advance(project).allowed, "unpatched, planning/planner is wrong"

        monkeypatch.setitem(rc.DISPATCH_CAPABILITY_PROFILE, "qe", "planner")
        monkeypatch.setitem(rc.DISPATCH_STAGE_PROFILE, "qe", "planning")
        assert _advance(project).allowed

    def test_an_incomplete_projection_raises_the_documented_error_type(
        self, monkeypatch
    ):
        """#399's helper indexed the projection tables directly, so a legacy
        role present in `ROLE_NAMES` but missing a projection row raised
        KeyError rather than the ValueError its own docstring promised. #402
        made the gate depend on this call, so the shape of the failure is now
        load-bearing."""
        monkeypatch.delitem(rc.DISPATCH_CAPABILITY_PROFILE, "qe")
        with pytest.raises(ValueError, match="incomplete dispatch projection"):
            rc.profile_pair_for_role("qe")

    def test_an_edge_whose_alias_leaves_the_table_fails_closed(
        self, tmp_path, monkeypatch
    ):
        """No pair means nothing to hold the receipt to. That is a table bug,
        and it must refuse rather than fall back to comparing names."""
        project = _project(tmp_path)
        _seed(project)
        _receipt(project)
        monkeypatch.delitem(rc.DISPATCH_CAPABILITY_PROFILE, "qe")
        refused = _advance(project)
        assert not refused.allowed
        assert refused.code == ak.NO_BOUND_ROLE


# ── 3. a present overlay pair cannot contradict the edge ─────────────────────


class TestOverlayPairRefusal:
    def test_a_matching_pair_advances(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        _receipt(project, stage_profile="verifying", capability_profile="prover")
        decision = _advance(project)
        assert decision.allowed, decision.reason
        assert decision.profile["capability_profile"] == "prover"

    def test_a_producer_stage_cannot_self_declare_the_prover_profile(self, tmp_path):
        """The forgery this check exists for. The control plane reads
        `capability_profile` as the kind of act a receipt attests to, so an
        `in_progress -> testing` receipt claiming prover would be ingested as
        verification of the work that produced it."""
        project = _project(tmp_path)
        _seed(project, state="in_progress")
        _receipt(
            project,
            role="software-engineer",
            abbrev="se",
            capability_profile="prover",
            stage_profile="verifying",
        )
        refused = ak.evaluate_advance(
            str(project), "US-001", "testing", policy=_policy()
        )
        assert not refused.allowed
        assert refused.code == ak.PROFILE_MISMATCH
        assert "capability_profile" in refused.reason

    def test_a_contradicting_stage_profile_alone_is_refused(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        _receipt(project, stage_profile="producing", capability_profile="prover")
        refused = _advance(project)
        assert not refused.allowed
        assert refused.code == ak.PROFILE_MISMATCH
        assert "stage_profile" in refused.reason

    def test_a_profile_outside_the_vocabulary_is_refused(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        _receipt(project, stage_profile="verifying", capability_profile="wizard")
        refused = _advance(project)
        assert not refused.allowed
        assert refused.code == ak.PROFILE_MISMATCH

    def test_a_non_string_overlay_is_refused_not_read_as_absent(self, tmp_path):
        """`{"capability_profile": {"name": "prover"}}` must not fall through
        the absence branch: an unreadable claim is not a missing one."""
        project = _project(tmp_path)
        _seed(project)
        _receipt(project, capability_profile={"name": "prover"})
        refused = _advance(project)
        assert not refused.allowed
        assert refused.code == ak.PROFILE_MISMATCH
        assert "must be a string" in refused.reason

    def test_case_and_whitespace_are_normalized_not_refused(self, tmp_path):
        """The kernel already lowercases roles at its own boundary; a stamped
        `Prover` is the same claim, not a contradiction."""
        project = _project(tmp_path)
        _seed(project)
        _receipt(project, stage_profile=" Verifying ", capability_profile="PROVER")
        assert _advance(project).allowed

    def test_an_absent_overlay_still_advances_and_warns(self, tmp_path):
        """This project is ungoverned: its dispatch handed over no envelope, so
        there was nothing to copy and refusing absence would refuse every
        producer in the field. #473 refuses it on a GOVERNED dispatch."""
        project = _project(tmp_path)
        _seed(project)
        _receipt(project)
        decision = _advance(project)
        assert decision.allowed, decision.reason
        assert any("profile_overlay_absent" in w for w in decision.warnings)

    def test_half_an_overlay_advances_and_names_the_missing_half(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        _receipt(project, capability_profile="prover")
        decision = _advance(project)
        assert decision.allowed, decision.reason
        assert any(
            "profile_overlay_absent" in w and "stage_profile" in w
            for w in decision.warnings
        )

    def test_warn_enforcement_downgrades_the_pair_check(self, tmp_path):
        """Claude's controlled mode already downgrades `ROLE_MISMATCH`. The
        pair check is the same class of statement about evidence identity, so
        promoting it to always-refuse would make one host stricter about the
        overlay than about the role beside it."""
        project = _project(tmp_path)
        _seed(project)
        _receipt(project, stage_profile="producing", capability_profile="producer")
        decision = _advance(project, enforcement="warn")
        assert decision.allowed
        assert any(ak.PROFILE_MISMATCH in w for w in decision.warnings)


# ── 4. accountable_role: soft-required, alias-validated ──────────────────────


class TestAccountableRole:
    def test_a_receipt_with_no_accountable_role_still_advances(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        _receipt(project)
        decision = _advance(project)
        assert decision.allowed, decision.reason
        assert any("accountable_role_absent" in w for w in decision.warnings)

    @pytest.mark.parametrize("value", ["qe", "quality-engineer", "QE"])
    def test_both_spellings_of_the_edge_role_are_accepted(self, tmp_path, value):
        project = _project(tmp_path)
        _seed(project)
        _receipt(project, accountable_role=value)
        assert _advance(project).allowed

    def test_an_unknown_accountable_role_is_refused(self, tmp_path):
        """`product-manager` is the retired name the #198 rename removed, so it
        is the value a stale producer is most likely to stamp. It names no role
        in the table, so it can confer accountability on nobody."""
        project = _project(tmp_path)
        _seed(project)
        _receipt(project, accountable_role="product-manager")
        refused = _advance(project)
        assert not refused.allowed
        assert refused.code == ak.ACCOUNTABLE_ROLE_INVALID
        assert "product-manager" in refused.reason

    def test_a_non_string_accountable_role_is_refused(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        _receipt(project, accountable_role=["qe"])
        refused = _advance(project)
        assert not refused.allowed
        assert refused.code == ak.ACCOUNTABLE_ROLE_INVALID

    def test_a_role_from_another_pair_is_refused(self, tmp_path):
        """`project-owner` is planning/planner. Claiming it on a
        verifying/prover stage claims the wrong kind of act, which is the same
        forgery the overlay check refuses arriving through another field."""
        project = _project(tmp_path)
        _seed(project)
        _receipt(project, accountable_role="project-owner")
        refused = _advance(project)
        assert not refused.allowed
        assert refused.code == ak.PROFILE_MISMATCH
        assert "verifying/prover" in refused.reason

    def test_a_pair_equal_alias_is_accepted_and_the_role_field_is_not(self, tmp_path):
        """The one place pair semantics differ observably from name semantics.
        `compliance-engineer` is verifying/prover, so it satisfies the pair
        this ticket keys on (ADR-032 decision 2 dissolves CE's obligations into
        checks the verifying stage runs). The receipt's own `role` -- what the
        DoD evaluator maps checks by -- is still pinned to the edge's exact
        alias, so within-pair latitude does not reach the gate."""
        project = _project(tmp_path)
        _seed(project)
        _receipt(project, accountable_role="compliance-engineer")
        assert _advance(project).allowed

        _receipt(project, role="compliance-engineer")
        refused = _advance(project)
        assert not refused.allowed
        assert refused.code == ak.ROLE_MISMATCH


# ── 5. the pair reaches the surfaces a dispatch prompt reads ─────────────────


class TestDispatchSurfaces:
    def test_next_action_names_the_pair_beside_the_role(self, tmp_path):
        project = _project(tmp_path)
        state = _seed(project, state="queued")
        action = sp.next_action(state)
        assert action["role"] == "se"
        assert action["profile"] == {
            "stage_profile": "producing",
            "capability_profile": "producer",
            "role": "se",
        }

    def test_next_action_carries_no_pair_when_nobody_is_dispatched(self):
        action = sp.next_action({"current_stories": []})
        assert action["role"] is None
        assert action["profile"] is None

    def test_next_action_survives_a_role_outside_the_alias_table(
        self, tmp_path, monkeypatch
    ):
        """A dispatcher that crashed because the profile vocabulary could not
        answer would be strictly worse than one that says nothing about
        profiles."""
        project = _project(tmp_path)
        state = _seed(project, state="queued")
        monkeypatch.delitem(rc.DISPATCH_STAGE_PROFILE, "se")
        action = sp.next_action(state)
        assert action["role"] == "se"
        assert action["profile"] is None

    def test_evaluate_dispatch_names_the_pair_it_authorizes(self, tmp_path):
        project = _project(tmp_path)
        _seed(project, state="queued")
        decision = ak.evaluate_dispatch(str(project), "US-001", policy=_policy())
        if not decision.allowed:
            pytest.skip("next_action did not select a dispatch for this fixture")
        assert decision.profile == {
            "stage_profile": "producing",
            "capability_profile": "producer",
            "role": "software-engineer",
            "role_abbrev": "se",
        }

    def test_the_decision_dict_carries_the_pair(self, tmp_path):
        project = _project(tmp_path)
        _seed(project)
        _receipt(project)
        payload = _advance(project).to_dict()
        assert payload["profile"]["stage_profile"] == "verifying"

    def test_the_execution_envelope_names_the_pair_as_a_fixed_literal(self):
        """The agent is told the two values, not merely told to copy them: an
        ungoverned dispatch hands over no envelope, so "copy the overlay" with
        no source is an invitation to guess, and a guessed pair now refuses.
        Since #473 the same line also states that the pair is REQUIRED and
        names the envelope as its source when there is one."""
        envelope = ec.render_envelope("synaptory:quality-engineer")
        assert '"stage_profile": "verifying"' in envelope
        assert '"capability_profile": "prover"' in envelope

    def test_the_backend_wrapper_states_the_pair_in_the_evidence_contract(self):
        body = (
            REPO_ROOT
            / "plugin-claude"
            / "skills"
            / "_shared"
            / "backends"
            / "claude.md"
        ).read_text(encoding="utf-8")
        contract = body.split("## Evidence Contract", 1)[1]
        assert "{PROFILE.STAGE_PROFILE}" in contract
        assert "{PROFILE.CAPABILITY_PROFILE}" in contract
