"""Reconciling the board with the control plane's attempt generation (#396).

The kernel mints a LOCAL random fencing token at dispatch, because a project
with no reachable control plane still needs attempt-scoped evidence. The moment
a runner claims the attempt, the control plane issues the authoritative token,
and nothing wrote it back. Two consequences, and the first is a live break
rather than a missing feature:

  - a correctly claimed receipt carries the CP token, the board still carries
    the local one, and `evaluate_advance` refuses it as `fencing_token_stale`.
    The governed path could not land evidence at all.
  - a control-plane cancellation never reached the board, so the state stayed
    absent and the cancelled-attempt guard was skipped entirely.

These pin the verb that closes it. What still has no production caller is the
step that INVOKES it after a claim, which belongs to the host-to-adapter leg.
"""
from __future__ import annotations

import json
from pathlib import Path

from hooks.lib import advance_kernel as ak


ATTEMPT = "att_0123456789abcdef01"


def _project(
    tmp_path: Path, *, token: str = "local-random", state: str = "", attempt: str = ATTEMPT
) -> Path:
    project = tmp_path / "proj"
    (project / ".synaptory" / ".orchestrator").mkdir(parents=True)
    (project / ".synaptory.yaml").write_text("build_mode: scrum\n", encoding="utf-8")
    (project / ".synaptory" / ".orchestrator" / "pipeline-state.json").write_text(
        json.dumps({
            "build_mode": "scrum",
            "lifecycle_state": "SPRINT_EXECUTION",
            "current_stories": [{
                "id": "US-001",
                "title": "t",
                "state": "in_progress",
                "pipeline_log": [],
                "receipts": [],
                "mcp_active_dispatches": {
                    "se": dict(
                        {
                            "dispatch_id": "a" * 32,
                            "attempt_id": attempt,
                            "fencing_token": token,
                        },
                        **({"state": state} if state else {}),
                    )
                },
            }],
        }),
        encoding="utf-8",
    )
    return project


def _binding(project: Path) -> dict:
    state = json.loads(
        (project / ".synaptory" / ".orchestrator" / "pipeline-state.json")
        .read_text(encoding="utf-8")
    )
    return state["current_stories"][0]["mcp_active_dispatches"]["se"]


def test_the_authoritative_token_replaces_the_local_one(tmp_path):
    project = _project(tmp_path)
    decision = ak.reconcile_dispatch(
        str(project), "US-001", "se", attempt_id=ATTEMPT, fencing_token="000000000001"
    )
    assert decision.allowed is True, decision.reason
    assert _binding(project)["fencing_token"] == "000000000001"


def test_a_cancellation_reaches_the_board(tmp_path):
    project = _project(tmp_path)
    decision = ak.reconcile_dispatch(
        str(project), "US-001", "se", attempt_id=ATTEMPT, attempt_state="cancelled"
    )
    assert decision.allowed is True, decision.reason
    assert _binding(project)["state"] == "cancelled"


def test_the_dispatch_id_is_never_rewritten(tmp_path):
    """Reconciliation settles WHO HOLDS the attempt. Rewriting its identity
    would make it a different attempt and break the binding every other check
    is anchored on."""
    project = _project(tmp_path)
    ak.reconcile_dispatch(
        str(project), "US-001", "se",
        attempt_id=ATTEMPT, fencing_token="000000000001", attempt_state="claimed",
    )
    assert _binding(project)["dispatch_id"] == "a" * 32


def test_reconciling_an_unbound_role_is_refused(tmp_path):
    project = _project(tmp_path)
    decision = ak.reconcile_dispatch(
        str(project), "US-001", "qe", attempt_id=ATTEMPT, fencing_token="000000000001"
    )
    assert decision.allowed is False
    assert decision.code == ak.NO_BOUND_ROLE, decision.code


def test_reconciling_nothing_is_refused(tmp_path):
    """A call that names neither a token nor a state would silently succeed
    while changing nothing, which reads as reconciliation having happened."""
    project = _project(tmp_path)
    decision = ak.reconcile_dispatch(str(project), "US-001", "se", attempt_id=ATTEMPT)
    assert decision.allowed is False
    assert decision.code == ak.BAD_REQUEST, decision.code


def test_an_unknown_story_is_refused(tmp_path):
    project = _project(tmp_path)
    decision = ak.reconcile_dispatch(
        str(project), "US-404", "se", attempt_id=ATTEMPT, fencing_token="000000000001"
    )
    assert decision.allowed is False
    assert decision.code == ak.STORY_NOT_FOUND, decision.code


# ─── this verb projects a fence, so it must not reverse one ──────────────────
#
# The first version identified its target by story and role alone and then
# overwrote token and state with whatever it was handed. Three reversals were
# possible, and each one hands a stale generation back the authority
# `evaluate_advance` reads the binding to deny (#396).


def test_a_superseded_attempt_cannot_rewrite_its_replacement(tmp_path):
    """A delayed callback from an attempt that has already been replaced.

    Serializing writers does not establish that the CP snapshot is current, so
    the attempt id is matched under the same transaction that writes.
    """
    project = _project(tmp_path, attempt="att_replacement00001")
    decision = ak.reconcile_dispatch(
        str(project), "US-001", "se",
        attempt_id="att_superseded000001", fencing_token="000000000009",
    )
    assert decision.allowed is False, decision.reason
    assert decision.code == ak.ATTEMPT_BINDING_MISMATCH, decision.code
    assert _binding(project)["fencing_token"] == "local-random", _binding(project)


def test_a_lease_generation_cannot_roll_backward(tmp_path):
    """A delayed callback carrying an older generation would hand a runner the
    control plane has already fenced out its authority again."""
    project = _project(tmp_path, token="000000000002")
    decision = ak.reconcile_dispatch(
        str(project), "US-001", "se",
        attempt_id=ATTEMPT, fencing_token="000000000001",
    )
    assert decision.allowed is False, decision.reason
    assert decision.code == ak.FENCING_TOKEN_STALE, decision.code
    assert _binding(project)["fencing_token"] == "000000000002"


def test_a_terminal_attempt_cannot_be_revived(tmp_path):
    """Reviving a cancelled attempt is how it would land a result."""
    project = _project(tmp_path, token="000000000002", state="cancelled")
    decision = ak.reconcile_dispatch(
        str(project), "US-001", "se",
        attempt_id=ATTEMPT, attempt_state="claimed",
    )
    assert decision.allowed is False, decision.reason
    assert _binding(project)["state"] == "cancelled"


def test_the_reviewers_combined_reproduction(tmp_path):
    """Token 2 / cancelled, handed token 1 / claimed. It returned allowed=true
    and persisted the rollback, removing both guards at once."""
    project = _project(tmp_path, token="000000000002", state="cancelled")
    decision = ak.reconcile_dispatch(
        str(project), "US-001", "se",
        attempt_id=ATTEMPT, fencing_token="000000000001", attempt_state="claimed",
    )
    assert decision.allowed is False, decision.reason
    binding = _binding(project)
    assert binding["fencing_token"] == "000000000002", binding
    assert binding["state"] == "cancelled", binding


def test_the_same_generation_is_idempotent(tmp_path):
    """A repeated callback is normal and must not be an error: the control
    plane is allowed to tell us the same thing twice."""
    project = _project(tmp_path, token="000000000002")
    decision = ak.reconcile_dispatch(
        str(project), "US-001", "se",
        attempt_id=ATTEMPT, fencing_token="000000000002",
    )
    assert decision.allowed is True, decision.reason


def test_a_forward_generation_is_accepted(tmp_path):
    """The control that keeps the guard from being a blanket refusal."""
    project = _project(tmp_path, token="000000000002")
    decision = ak.reconcile_dispatch(
        str(project), "US-001", "se",
        attempt_id=ATTEMPT, fencing_token="000000000003",
    )
    assert decision.allowed is True, decision.reason
    assert _binding(project)["fencing_token"] == "000000000003"


def test_the_first_reconciliation_adopts_the_control_plane_counter(tmp_path):
    """The kernel's initial token is random hex and the control plane's is a
    counter, so the first reconciliation is not a comparison between two
    generations. It must still be allowed."""
    project = _project(tmp_path, token="9f2c41ab9f2c41ab9f2c41ab9f2c41ab")
    decision = ak.reconcile_dispatch(
        str(project), "US-001", "se",
        attempt_id=ATTEMPT, fencing_token="000000000001",
    )
    assert decision.allowed is True, decision.reason
    assert _binding(project)["fencing_token"] == "000000000001"


def test_a_non_counter_never_replaces_a_counter(tmp_path):
    """The reverse direction of the adoption above: once the control plane's
    generation is on the board, a local value must not take it back."""
    project = _project(tmp_path, token="000000000002")
    decision = ak.reconcile_dispatch(
        str(project), "US-001", "se",
        attempt_id=ATTEMPT, fencing_token="9f2c41ab9f2c41ab",
    )
    assert decision.allowed is False, decision.reason
    assert _binding(project)["fencing_token"] == "000000000002"


def test_terminal_to_terminal_is_allowed(tmp_path):
    """`cancelled` to `expired` is reconciliation catching up, not a revival."""
    project = _project(tmp_path, state="cancelled")
    decision = ak.reconcile_dispatch(
        str(project), "US-001", "se",
        attempt_id=ATTEMPT, attempt_state="expired",
    )
    assert decision.allowed is True, decision.reason
    assert _binding(project)["state"] == "expired"


def test_a_binding_with_no_attempt_identity_cannot_be_reconciled(tmp_path):
    """No identity is not a match.

    Guarding on "bound id is truthy AND differs" let a legacy or damaged
    binding fall back to story plus role, which is the exact unverifiable
    target the required match exists to remove: with nothing stored, the
    transaction cannot establish that this snapshot names the attempt that owns
    the role (#396).
    """
    project = _project(tmp_path, attempt="")
    decision = ak.reconcile_dispatch(
        str(project), "US-001", "se",
        attempt_id=ATTEMPT, fencing_token="000000000001", attempt_state="claimed",
    )
    assert decision.allowed is False, decision.reason
    assert decision.code == ak.ATTEMPT_BINDING_MISMATCH, decision.code
    binding = _binding(project)
    assert binding["fencing_token"] == "local-random", binding
    assert "state" not in binding, binding


def test_a_misspelled_cancellation_cannot_be_stored(tmp_path):
    """`canceled` with one L is not `cancelled`.

    It was stored happily, and since the advance guard recognises only the
    canonical spellings, that malformed cancellation then read as LIVE and did
    not trigger the fence at all. A projected state nothing downstream
    understands is worse than no projection, because it looks like one.
    """
    project = _project(tmp_path)
    decision = ak.reconcile_dispatch(
        str(project), "US-001", "se", attempt_id=ATTEMPT, attempt_state="canceled"
    )
    assert decision.allowed is False, decision.reason
    assert "state" not in _binding(project), _binding(project)


def test_the_stored_state_must_be_in_the_shared_vocabulary(tmp_path):
    """Every canonical state is storable, and nothing else is."""
    import runtime_contracts as rc

    for state in rc.ATTEMPT_STATES:
        project = _project(tmp_path / state)
        decision = ak.reconcile_dispatch(
            str(project), "US-001", "se", attempt_id=ATTEMPT, attempt_state=state
        )
        assert decision.allowed is True, (state, decision.reason)
        assert _binding(project)["state"] == state

    project = _project(tmp_path / "invented")
    decision = ak.reconcile_dispatch(
        str(project), "US-001", "se", attempt_id=ATTEMPT, attempt_state="in-flight"
    )
    assert decision.allowed is False, decision.reason
