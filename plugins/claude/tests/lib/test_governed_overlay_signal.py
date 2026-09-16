"""Layer 1 — the governed-dispatch signal the overlay requirement reads (#473).

`test_governed_overlay_required.py` (Layer 2) drives a real `execute_dispatch`
and asserts the behaviour. This file pins the signal itself: what
`advance_kernel.dispatch_runtime_authority` answers for each binding shape, and
that `receipt_validator` requires the overlay pair on exactly the shapes where
the dispatcher recorded a runtime selection.

Why the signal is a persisted field and not a re-derivation is argued in
`dispatch_runtime_authority`'s own docstring. The property these tests pin is
narrower and mechanical: ONE field, written in one place, read by both the
executor check in `evaluate_advance` and the overlay requirement in
`receipt_validator`. A second derivation of "governed" would be free to
disagree with the first, and the disagreement would refuse honest receipts in
one direction and admit dishonest ones in the other.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import advance_kernel as ak
import receipt_validator as rv


pytestmark = pytest.mark.unit

STAGE_ENTERED = "2026-01-01T00:00:00+00:00"
FUTURE = "2099-01-01T00:00:00Z"


# ── the binding shapes ───────────────────────────────────────────────────────


class TestDispatchRuntimeAuthority:
    def test_no_story_no_authority(self):
        assert ak.dispatch_runtime_authority(None, "se") == {}
        assert ak.dispatch_runtime_authority({}, "se") == {}

    def test_a_ledger_of_the_wrong_type_is_not_an_authority(self):
        assert ak.dispatch_runtime_authority({"mcp_active_dispatches": []}, "se") == {}

    def test_a_binding_for_another_stage_is_not_this_stage_s_authority(self):
        story = {"mcp_active_dispatches": {"qe": {"runtime_family": "codex"}}}
        assert ak.dispatch_runtime_authority(story, "se") == {}

    def test_a_binding_with_no_selection_is_ungoverned(self):
        """The pre-selection binding shape, and the one every unenrolled
        project still writes: a dispatch_id and an attempt, no runtime family.
        `_record_selected_family` is the only writer of that field and it runs
        only on the `selected` branch."""
        story = {
            "mcp_active_dispatches": {
                "se": {"dispatch_id": "d" * 32, "attempt_id": "att_x"}
            }
        }
        assert ak.dispatch_runtime_authority(story, "se") == {}

    def test_a_blank_family_is_ungoverned(self):
        story = {"mcp_active_dispatches": {"se": {"runtime_family": "  "}}}
        assert ak.dispatch_runtime_authority(story, "se") == {}

    def test_a_recorded_selection_is_the_authority(self):
        story = {
            "mcp_active_dispatches": {
                "se": {
                    "runtime_family": "claude-code",
                    "adapter_profile_id": "claude-local-v1",
                }
            }
        }
        assert ak.dispatch_runtime_authority(story, "se") == {
            "runtime_family": "claude-code",
            "adapter_profile_id": "claude-local-v1",
        }

    def test_the_profile_id_is_optional_but_the_family_is_not(self):
        story = {"mcp_active_dispatches": {"se": {"runtime_family": "codex"}}}
        assert ak.dispatch_runtime_authority(story, "se") == {
            "runtime_family": "codex"
        }

    def test_unreadable_state_reads_as_ungoverned_rather_than_raising(self, tmp_path):
        """A validator that crashed on an unreadable board would be worse than
        one that does not require the pair: every gate that consumes a receipt
        reads the same board, so a missing story refuses the advance anyway and
        deleting state buys an agent nothing."""
        assert ak.governed_dispatch_authority(str(tmp_path), "US-001", "se") == {}


# ── the requirement, against a seeded board ──────────────────────────────────


def _project(tmp_path: Path, *, governed: bool) -> Path:
    (tmp_path / ".synaptory" / ".orchestrator" / "receipts").mkdir(parents=True)
    (tmp_path / ".synaptory.yaml").write_text("build_mode: scrum\n", encoding="utf-8")
    binding = {"dispatch_id": "d" * 32, "attempt_id": "att_seed"}
    if governed:
        binding["runtime_family"] = "claude-code"
        binding["adapter_profile_id"] = "claude-local-v1"
    story = {
        "id": "US-001",
        "title": "Seed story",
        "state": "testing",
        "blocked_reason": None,
        "blocked_from": None,
        "backend": {},
        "pipeline_log": [
            {"state": "testing", "entered_at": STAGE_ENTERED, "exited_at": None}
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
        "mcp_active_dispatches": {"qe": binding},
    }
    (tmp_path / ".synaptory" / ".orchestrator" / "pipeline-state.json").write_text(
        json.dumps(
            {
                "version": "2.0",
                "build_mode": "scrum",
                "lifecycle_state": "SPRINT_EXECUTION",
                "current_sprint": 2,
                "cumulative_ticket_number": 1,
                "sprint_goal": "seed",
                "pipeline_log": [],
                "current_stories": [story],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return tmp_path


def _receipt(tmp_path: Path, **overrides) -> dict:
    src = tmp_path / "src" / "foo.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("x\n", encoding="utf-8")
    payload = {
        "story_id": "US-001",
        "role": "quality-engineer",
        "backend": "claude",
        "model": "opus",
        "artifacts": ["src/foo.py"],
        "verification_commands": ["true"],
        "metrics": {"n": 1},
        "completed_at": FUTURE,
    }
    payload.update(overrides)
    return payload


class TestTheRequirementFollowsTheBinding:
    def test_a_governed_binding_makes_the_pair_required(self, tmp_path):
        project = _project(tmp_path, governed=True)
        result = rv.validate_receipt_payload(_receipt(project), str(project))
        assert not result.valid
        assert any("governed dispatch" in e for e in result.errors), result.errors

    def test_the_same_receipt_only_warns_without_the_binding(self, tmp_path):
        """Same receipt, same board, one field different. This is the whole
        conditionality in one pair of assertions."""
        project = _project(tmp_path, governed=False)
        result = rv.validate_receipt_payload(_receipt(project), str(project))
        assert result.valid, result.errors
        assert any("soft-required" in w for w in result.warnings), result.warnings

    def test_the_pair_present_passes_on_a_governed_binding(self, tmp_path):
        project = _project(tmp_path, governed=True)
        result = rv.validate_receipt_payload(
            _receipt(project, stage_profile="verifying", capability_profile="prover"),
            str(project),
        )
        assert result.valid, result.errors
        assert not any("stage_profile" in w for w in result.warnings)

    def test_the_pair_is_not_checked_for_value_here(self, tmp_path):
        """`receipt_validator` answers presence only. Whether a present pair is
        the RIGHT pair is the edge's question, and only `evaluate_advance` knows
        the edge; deciding it twice is how two answers come to disagree."""
        project = _project(tmp_path, governed=True)
        result = rv.validate_receipt_payload(
            _receipt(project, stage_profile="planning", capability_profile="planner"),
            str(project),
        )
        assert result.valid, result.errors

    def test_a_receipt_naming_another_story_is_not_governed_by_this_binding(
        self, tmp_path
    ):
        """The lookup is keyed on the receipt's own (story, role) because that
        is the only pair it can name, and a receipt for work not on the board
        has no dispatch to be governed by. `STORY_MISMATCH` is what refuses it
        at the gate, not this check."""
        project = _project(tmp_path, governed=True)
        result = rv.validate_receipt_payload(
            _receipt(project, story_id="US-999"), str(project)
        )
        assert result.valid, result.errors

    def test_a_receipt_for_another_role_is_not_governed_by_this_binding(
        self, tmp_path
    ):
        project = _project(tmp_path, governed=True)
        result = rv.validate_receipt_payload(
            _receipt(project, role="software-engineer"), str(project)
        )
        assert result.valid, result.errors

    def test_a_role_outside_the_alias_table_is_never_governed(self, tmp_path):
        """No abbrev means no binding key to look up. It has to read as
        ungoverned rather than as an error, because `role` is validated
        separately and one bad field must not produce two unrelated refusals."""
        project = _project(tmp_path, governed=True)
        result = rv.validate_receipt_payload(
            _receipt(project, role="wizard"), str(project)
        )
        assert not any("governed dispatch" in e for e in result.errors), result.errors

    @pytest.mark.parametrize(
        "role", ["technical-writer", "orchestrator", "research-advisor"]
    )
    def test_a_role_no_gated_edge_binds_says_nothing_either_way(self, tmp_path, role):
        """The overlay contract is about receipt-GATED edges. A TW report or an
        inline orchestrator receipt is not one, and the reminder would be pure
        payload: this output is read by an agent under a token budget.

        Scoped off the kernel's own `TRANSITION_RECEIPT` table rather than a
        list here, so binding a new edge to a role moves the scope with it.
        """
        project = _project(tmp_path, governed=True)
        result = rv.validate_receipt_payload(
            _receipt(project, role=role), str(project)
        )
        assert result.valid, result.errors
        assert not any("runtime-identity overlay" in w for w in result.warnings)

    def test_every_gated_edge_s_role_is_in_scope(self, tmp_path):
        """The other side of the same derivation: every role a gated edge binds
        gets the reminder, `project-owner` on the acceptance edge included."""
        import advance_kernel as kernel

        project = _project(tmp_path, governed=False)
        for _role, abbrev in kernel.TRANSITION_RECEIPT.values():
            role = kernel.role_full_name(abbrev)
            result = rv.validate_receipt_payload(
                _receipt(project, role=role), str(project)
            )
            assert any(
                "runtime-identity overlay" in w for w in result.warnings
            ), (role, result.warnings)

    def test_the_error_is_a_single_line_naming_both_fields(self, tmp_path):
        """One refusal, not one per field: a receipt missing both would
        otherwise emit two errors saying the same thing, and the validator's
        output is read by an agent under a token budget."""
        project = _project(tmp_path, governed=True)
        errors = [
            e
            for e in rv.validate_receipt_payload(
                _receipt(project), str(project)
            ).errors
            if "governed dispatch" in e
        ]
        assert len(errors) == 1, errors
        assert "'stage_profile'" in errors[0] and "'capability_profile'" in errors[0]


class TestTheOverlayAbsenceRuleMatchesTheKernel:
    """`receipt_validator._overlay_absent` and
    `advance_kernel._overlay_value` both decide what "the agent did not stamp
    one" means. If they disagreed, a value would be absent for one module and
    present for the other, and the receipt would fall between the two checks.
    """

    @pytest.mark.parametrize(
        "payload",
        [{}, {"stage_profile": ""}, {"stage_profile": "   "}],
    )
    def test_both_modules_call_these_absent(self, payload):
        assert rv._overlay_absent(payload, "stage_profile") is True
        value, problem = ak._overlay_value(payload, "stage_profile")
        assert value is None and problem is None

    @pytest.mark.parametrize(
        "payload",
        [{"stage_profile": "verifying"}, {"stage_profile": " Verifying "}],
    )
    def test_both_modules_call_these_present(self, payload):
        assert rv._overlay_absent(payload, "stage_profile") is False
        value, problem = ak._overlay_value(payload, "stage_profile")
        assert value == "verifying" and problem is None

    def test_a_non_string_is_present_here_and_a_shape_problem_there(self):
        payload = {"stage_profile": {"name": "verifying"}}
        assert rv._overlay_absent(payload, "stage_profile") is False
        value, problem = ak._overlay_value(payload, "stage_profile")
        assert value is None and "must be a string" in problem
