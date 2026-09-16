"""Layer 2 -- SubagentStop receipt SELECTION binds to the active dispatch (#340).

`synaptory-verify-receipt.sh` used to pick the receipt to validate by newest
mtime, so a receipt belonging to another story or role could satisfy the hook.
The shipping half of that race was fixed by per-receipt sentinels (#287, which
closed #290); this suite covers the successor: validation now binds to the
(story, role) pairs the kernel recorded in `mcp_active_dispatches`, resolved
through the canonical receipt resolver by `dispatched_receipts.py`, and falls
back to newest-by-mtime only when no binding exists.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest


@pytest.fixture
def verify_hook(plugin_root: Path) -> Path:
    return plugin_root / "hooks" / "synaptory-verify-receipt.sh"


# Fixed agent id used across the stdin payload and the SubagentStart marker
# so the two stay in sync (see _make_workspace / Issue #130).
_TEST_AGENT_ID = "test-agent-340"


def _subagent_stop_stdin(
    description: str = "software-engineer", agent_id: str = _TEST_AGENT_ID
) -> str:
    """Minimal SubagentStop JSON payload the hook reads from stdin."""
    return json.dumps(
        {
            "description": description,
            "agent_id": agent_id,
            "session_id": "test-session-340",
            "hook_event_name": "SubagentStop",
        }
    )


def _make_workspace(
    project_dir: Path,
    engagement: str = "structured",
    quality_enforcement: str = "strict",
    marker_role: str = "software-engineer",
    marker_story: str = "",
    marker_dispatch: str = "",
) -> Path:
    """Create the minimal .synaptory/ workspace structure.

    Mirrors test_verify_receipt.py: quality_enforcement='lenient' selects the
    controlled/warn path (autonomous=False), 'strict' the blocking path.
    """
    suite = project_dir / ".synaptory"
    orch = suite / ".orchestrator"
    (orch / "receipts").mkdir(parents=True, exist_ok=True)
    (orch / "settings.md").write_text(
        f"Engagement: {engagement}\nQuality-Enforcement: {quality_enforcement}\n",
        encoding="utf-8",
    )
    markers = orch / "subagent-markers"
    markers.mkdir(parents=True, exist_ok=True)
    # SubagentStart writes the namespaced role here (#396); SubagentStop reads
    # it to bind selection to one (work, role) pair. An empty marker is the
    # pre-#396 shape and now means "role unknown".
    if marker_role:
        link = "%s\t%s" % (marker_story, marker_dispatch) if marker_story else ""
        body = "synaptory:%s\n%s\n" % (marker_role, link)
    else:
        body = ""
    (markers / _TEST_AGENT_ID).write_text(body, encoding="utf-8")
    return suite


def _write_receipt(
    receipts_dir: Path,
    project_dir: Path,
    name: str,
    *,
    story_id: str,
    role: str,
    valid: bool = True,
    mtime: float | None = None,
) -> Path:
    """A receipt that passes (or, with valid=False, fails) receipt_validator.py."""
    artifact_rel = "api/routes/auth.py"
    artifact_abs = project_dir / artifact_rel
    artifact_abs.parent.mkdir(parents=True, exist_ok=True)
    artifact_abs.write_text("# stub\n", encoding="utf-8")

    payload: dict = {
        "story_id": story_id,
        "role": role,
        "backend": "claude",
        "model": "claude-sonnet-4-5",
        "artifacts": [artifact_rel],
        "verification_commands": [
            {"command": "echo ok", "exit_code": 0, "summary": "smoke check passes"}
        ],
        "metrics": {"tests_added": 1, "coverage_delta": 0.0},
        "completed_at": "2026-05-20T12:00:00Z",
    }
    if not valid:
        # Drop required fields so the validator errors on this file. If the
        # hook picks it, the run warns (lenient) or blocks (strict).
        del payload["verification_commands"]
        del payload["completed_at"]
    receipts_dir.mkdir(parents=True, exist_ok=True)
    receipt = receipts_dir / name
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    if mtime is not None:
        os.utime(receipt, (mtime, mtime))
    return receipt


def _write_flat_state(
    orch: Path, *, bind_se_on: str | None, state: str = "in_progress"
) -> None:
    """A flat scrum board; bind_se_on names the story carrying an active
    se dispatch, None writes the same board with no binding at all.

    `state` is the story's pipeline state, and it is the role's assignment:
    the board fallback reads `in_progress` as SE's, `testing` as QE's and
    `reviewing` as CR's. A QE test therefore needs `testing`, which is where
    the pipeline actually puts a story QE is verifying.
    """
    story: dict = {"id": "US-042", "state": state}
    if bind_se_on:
        story["mcp_active_dispatches"] = {
            "se": {"dispatch_id": "d" * 32, "started_at": "2026-05-20T11:00:00Z"}
        }
    (orch / "pipeline-state.json").write_text(
        json.dumps({"build_mode": "scrum", "current_stories": [story]}),
        encoding="utf-8",
    )


# ── binding present: the canonical receipt wins over a newer decoy ────────────


@pytest.mark.hook
def test_binding_selects_canonical_over_newer_wrong_receipt(
    verify_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """The #340 defect: a NEWER receipt for the wrong story/role is on disk,
    schema-invalid, while the dispatched story's canonical receipt is valid
    and older. Under mtime selection the hook validated the decoy and warned;
    under dispatch binding it must validate the canonical receipt and stay
    silent."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(project_dir, quality_enforcement="lenient")
    orch = suite / ".orchestrator"
    receipts = orch / "receipts"
    _write_flat_state(orch, bind_se_on="US-042")

    now = time.time()
    _write_receipt(
        receipts, project_dir, "US-042-se.json",
        story_id="US-042", role="software-engineer", mtime=now - 1000,
    )
    _write_receipt(
        receipts, project_dir, "US-099-qe.json",
        story_id="US-099", role="quality-engineer", valid=False, mtime=now,
    )

    result = run_hook(verify_hook, env=hook_env, stdin=_subagent_stop_stdin())
    combined = result.stderr + result.stdout
    assert result.returncode == 0, (
        f"bound canonical receipt is valid, hook must pass; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "Receipt validation issues" not in combined, (
        f"hook validated the newer wrong-story receipt instead of the bound "
        f"canonical one; combined={combined!r}"
    )


@pytest.mark.hook
def test_binding_with_missing_canonical_blocks_despite_newer_receipt(
    verify_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """When a dispatch is active and its canonical receipt is absent, the
    hook must report receipt_missing (blocking in autonomous mode) even
    though a newer, perfectly VALID receipt for another story/role sits on
    disk. That decoy passing the gate was exactly the #340 defect."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(project_dir, engagement="structured")
    orch = suite / ".orchestrator"
    receipts = orch / "receipts"
    _write_flat_state(orch, bind_se_on="US-042")

    _write_receipt(
        receipts, project_dir, "US-099-qe.json",
        story_id="US-099", role="quality-engineer",
    )

    result = run_hook(verify_hook, env=hook_env, stdin=_subagent_stop_stdin())
    assert result.returncode == 2, (
        f"bound dispatch without its canonical receipt must block; "
        f"got {result.returncode}; stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    combined = result.stderr + result.stdout
    assert "without writing a receipt" in combined, (
        f"expected the receipt_missing error; combined={combined!r}"
    )


# ── no binding: newest-by-mtime fallback is preserved ─────────────────────────


@pytest.mark.hook
def test_no_binding_scopes_to_the_board_not_to_mtime(
    verify_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """The inversion of what this test used to pin (#396).

    It previously asserted that WITHOUT a binding the hook validated the
    newest file on disk, and called that fallback something legacy sessions
    depend on. That is #340's original defect surviving on exactly the
    workflows that lack a binding, which on Claude is QE and CR: its
    orchestrator calls `begin_dispatch` only for `dispatch_se` from `queued`,
    so those stages never carry one.

    Selection is now scoped to the story the board says is in flight. The
    decoy here belongs to US-099, is newer, and is invalid; the board names
    US-042. The decoy must not be selected, so no validation warning appears.
    """
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(project_dir, quality_enforcement="lenient")
    orch = suite / ".orchestrator"
    receipts = orch / "receipts"
    _write_flat_state(orch, bind_se_on=None)

    now = time.time()
    _write_receipt(
        receipts, project_dir, "US-042-se.json",
        story_id="US-042", role="software-engineer", mtime=now - 1000,
    )
    _write_receipt(
        receipts, project_dir, "US-099-qe.json",
        story_id="US-099", role="quality-engineer", valid=False, mtime=now,
    )

    result = run_hook(verify_hook, env=hook_env, stdin=_subagent_stop_stdin())
    combined = result.stderr + result.stdout
    assert result.returncode == 0, f"lenient never blocks; stderr={result.stderr!r}"
    assert "Receipt validation issues" not in combined, (
        f"a newer receipt from an unrelated story was selected, which is the "
        f"defect #340 was filed for; combined={combined!r}"
    )


@pytest.mark.hook
def test_an_unresolvable_board_validates_nothing(
    verify_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """When the board names nothing this SubagentStop could be closing, the
    honest answer is to validate nothing.

    Reaching for the newest file here is what let an unrelated receipt report
    a result about work nobody asked about. The hook still exits 0: selection
    finding nothing is not a reason to break a session.
    """
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(project_dir, quality_enforcement="lenient")
    orch = suite / ".orchestrator"
    receipts = orch / "receipts"
    (orch / "pipeline-state.json").write_text(
        json.dumps({"build_mode": "scrum", "current_stories": []}), encoding="utf-8"
    )

    _write_receipt(
        receipts, project_dir, "US-099-qe.json",
        story_id="US-099", role="quality-engineer", valid=False, mtime=time.time(),
    )

    result = run_hook(verify_hook, env=hook_env, stdin=_subagent_stop_stdin())
    combined = result.stderr + result.stdout
    assert result.returncode == 0
    assert "Receipt validation issues" not in combined, (
        f"an unrelated receipt was validated against an empty board; "
        f"combined={combined!r}"
    )


# ── SPQ layout: the binding resolves the workstream receipts dir ──────────────


@pytest.mark.hook
def test_spq_binding_resolves_the_cycles_receipt(
    verify_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """SPQ Work Unit receipts live under `spq/cycles/<cycle-id>/receipts/`,
    never the flat dir. With an active dispatch recorded on the Cycle's
    execution state, the hook must validate the canonical Work Unit receipt
    there and ignore a newer invalid decoy in the flat directory.

    THE LANE IS GONE, THE PROPERTY IS NOT. This read
    `spq/cycles/<id>/workstreams/<ws>/receipts` and pinned a lane; `SPD-194`
    retires both, so the path loses one level and the pin loses its reason.
    What is still worth asserting is the part the flat decoy tests: selection
    must be by BINDING, not by mtime. A newer file in the wrong directory is
    the cheapest way for a hook to report a receipt for work this stop did
    not do.
    """
    import spq_paths

    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(project_dir, quality_enforcement="lenient")
    orch = suite / ".orchestrator"
    cycle_id = "1-abcdef12"

    (orch / "pipeline-state.json").write_text(
        json.dumps({"build_mode": "spq",
                    "lifecycle_state": "CYCLE_EXECUTION", "current_cycle": 1}),
        encoding="utf-8",
    )
    (orch / "spq").mkdir(parents=True, exist_ok=True)
    (orch / "spq" / "index.json").write_text(
        json.dumps({"cycle_seq_high": 1, "current_cycle_id": cycle_id,
                    "cycles": [{"cycle_id": cycle_id}]}),
        encoding="utf-8",
    )
    exec_state = Path(spq_paths.execution_state_path(str(project_dir), cycle_id))
    exec_state.parent.mkdir(parents=True, exist_ok=True)
    exec_state.write_text(
        json.dumps({
            "build_mode": "spq",
            "lifecycle_state": "CYCLE_EXECUTION",
            "current_stories": [{
                "id": "WU-API-1",
                "state": "in_progress",
                "mcp_active_dispatches": {
                    "se": {"dispatch_id": "e" * 32,
                           "started_at": "2026-05-20T11:00:00Z"}
                },
            }],
        }),
        encoding="utf-8",
    )

    cycle_receipts = Path(spq_paths.receipts_dir(str(project_dir), cycle_id))
    now = time.time()
    _write_receipt(
        cycle_receipts, project_dir, "WU-API-1-se.json",
        story_id="WU-API-1", role="software-engineer", mtime=now - 1000,
    )
    # Newer invalid decoy in the FLAT dir: mtime selection would pick it.
    _write_receipt(
        orch / "receipts", project_dir, "US-099-qe.json",
        story_id="US-099", role="quality-engineer", valid=False, mtime=now,
    )

    result = run_hook(verify_hook, env=hook_env, stdin=_subagent_stop_stdin())
    combined = result.stderr + result.stdout
    assert result.returncode == 0, (
        f"the bound Cycle receipt is valid, so the hook must pass; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "Receipt validation issues" not in combined, (
        f"the hook validated the flat-dir decoy instead of the Cycle's own "
        f"receipt; combined={combined!r}"
    )


# ── role substitution: the other half of #340's "another story OR ROLE" ──────


@pytest.mark.hook
def test_a_qe_stop_is_not_satisfied_by_the_se_receipt(
    verify_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """#396. Scoping to "current story, any role" fixed the story half and
    left the role half open.

    US-042 has a valid SE receipt. A QE SubagentStop for the same story wrote
    no QE receipt. Selection must not answer with the SE file: that reports a
    receipt for work this stop did not do, and `receipt_missing` never fires.
    """
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(
        project_dir, quality_enforcement="lenient", marker_role="quality-engineer"
    )
    orch = suite / ".orchestrator"
    _write_flat_state(orch, bind_se_on=None, state="testing")
    _write_receipt(
        orch / "receipts", project_dir, "US-042-se.json",
        story_id="US-042", role="software-engineer", mtime=time.time(),
    )

    result = run_hook(
        verify_hook, env=hook_env,
        stdin=_subagent_stop_stdin(description="quality-engineer"),
    )
    combined = result.stderr + result.stdout
    assert "US-042-se.json" not in combined, (
        f"the SE receipt answered a QE stop; combined={combined!r}"
    )


@pytest.mark.hook
def test_a_qe_stop_validates_its_own_receipt_and_ignores_a_bad_se_one(
    verify_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """The inverse, which the same bug caused in the other direction: an
    invalid receipt from another role could block an agent whose own receipt
    was fine. QE's receipt is valid, SE's is not, and only QE's is this stop's
    business."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(
        project_dir, quality_enforcement="strict", marker_role="quality-engineer"
    )
    orch = suite / ".orchestrator"
    _write_flat_state(orch, bind_se_on=None, state="testing")
    # No backdating. Ordering used to matter because selection ranked by
    # mtime; it no longer does, and a receipt written BEFORE this subagent
    # started is now excluded as impossible output, so backdating here would
    # be testing the causal filter rather than role binding.
    now = time.time()
    _write_receipt(
        orch / "receipts", project_dir, "US-042-qe.json",
        story_id="US-042", role="quality-engineer", mtime=now,
    )
    _write_receipt(
        orch / "receipts", project_dir, "US-042-se.json",
        story_id="US-042", role="software-engineer", valid=False, mtime=now,
    )

    result = run_hook(
        verify_hook, env=hook_env,
        stdin=_subagent_stop_stdin(description="quality-engineer"),
    )
    combined = result.stderr + result.stdout
    assert result.returncode == 0, (
        f"another role's invalid receipt blocked a valid QE stop; "
        f"stderr={result.stderr!r}"
    )
    assert "US-042-se.json" not in combined


@pytest.mark.hook
def test_a_qe_stop_is_not_blocked_by_a_story_another_role_holds(
    verify_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """The ordinary pipeline board must not read as ambiguous (#396).

    The board fallback maps each in-flight state to the role that HOLDS it,
    then counted stories across ALL of them. So SE building US-SE while QE
    verifies US-QE made the QE stop `?ambiguous`, selection returned nothing,
    and this hook reported `receipt_missing` against a valid QE receipt
    sitting at its canonical path. SE on one story while QE verifies another
    is what the per-story pipeline is for, so this fired on the normal case.
    """
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(
        project_dir, quality_enforcement="strict", marker_role="quality-engineer"
    )
    orch = suite / ".orchestrator"
    (orch / "pipeline-state.json").write_text(
        json.dumps({
            "build_mode": "scrum",
            "current_stories": [
                {"id": "US-SE", "state": "in_progress"},
                {"id": "US-QE", "state": "testing"},
            ],
        }),
        encoding="utf-8",
    )
    _write_receipt(
        orch / "receipts", project_dir, "US-QE-qe.json",
        story_id="US-QE", role="quality-engineer", mtime=time.time(),
    )

    result = run_hook(
        verify_hook, env=hook_env,
        stdin=_subagent_stop_stdin(description="quality-engineer"),
    )
    combined = result.stderr + result.stdout
    assert result.returncode == 0, (
        f"a story held by another role blocked a valid QE stop; "
        f"stderr={result.stderr!r}"
    )
    assert "receipt_missing" not in combined, (
        f"QE's own canonical receipt was on disk; combined={combined!r}"
    )


@pytest.mark.hook
def test_an_unknown_role_selects_nothing(
    verify_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """A marker with no role (the pre-#396 shape, or a non-synaptory agent)
    resolves no (work, role) pair. Falling back to all roles would reinstate
    the substitution above, so selection returns nothing."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(
        project_dir, quality_enforcement="lenient", marker_role=""
    )
    orch = suite / ".orchestrator"
    _write_flat_state(orch, bind_se_on=None)
    _write_receipt(
        orch / "receipts", project_dir, "US-042-se.json",
        story_id="US-042", role="software-engineer", valid=False, mtime=time.time(),
    )

    result = run_hook(verify_hook, env=hook_env, stdin=_subagent_stop_stdin())
    combined = result.stderr + result.stdout
    assert result.returncode == 0
    assert "Receipt validation issues" not in combined, (
        f"an unidentified role validated someone else's receipt; combined={combined!r}"
    )


@pytest.mark.hook
def test_a_ceremony_receipt_scheme_this_module_never_heard_of_resolves(
    verify_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """#396. The previous selector carried a hard-coded ceremony id list and
    three accepted story states, which omitted Inception's
    `INCEPTION-<ROLE>-1` and scrum's `SPRINT-{N}`. Those flows wrote a valid
    receipt and were told it was missing.

    Selection now reads the identity the receipt DECLARES and checks it sits
    at the canonical path for that declaration, so an id scheme this module
    has never seen resolves without being enumerated anywhere.
    """
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(
        project_dir, quality_enforcement="strict", marker_role="project-owner"
    )
    orch = suite / ".orchestrator"
    _write_flat_state(orch, bind_se_on=None)
    _write_receipt(
        orch / "receipts", project_dir, "INCEPTION-PO-1-po.json",
        story_id="INCEPTION-PO-1", role="project-owner", mtime=time.time(),
    )

    result = run_hook(
        verify_hook, env=hook_env,
        stdin=_subagent_stop_stdin(description="project-owner"),
    )
    assert result.returncode == 0, (
        f"a valid ceremony receipt was reported missing; stderr={result.stderr!r}"
    )


@pytest.mark.hook
def test_a_receipt_written_before_the_subagent_started_is_not_its_output(
    verify_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """The causal filter, which is what closes the remaining story half.

    A receipt for the same role but an earlier story, written before this
    subagent began, cannot be what it produced. Excluding it is a statement
    about causality rather than a preference for newer files: nothing here
    ranks, it only excludes the impossible.
    """
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(
        project_dir, quality_enforcement="lenient", marker_role="quality-engineer"
    )
    orch = suite / ".orchestrator"
    _write_flat_state(orch, bind_se_on=None, state="testing")
    _write_receipt(
        orch / "receipts", project_dir, "US-041-qe.json",
        story_id="US-041", role="quality-engineer", valid=False,
        mtime=time.time() - 3600,
    )

    result = run_hook(
        verify_hook, env=hook_env,
        stdin=_subagent_stop_stdin(description="quality-engineer"),
    )
    combined = result.stderr + result.stdout
    assert "US-041-qe.json" not in combined, (
        f"a pre-existing receipt from another story was selected; "
        f"combined={combined!r}"
    )


# ── bound mode: a binding for another role or story must not answer ──────────
#
# #396. `bound_receipts` used to take only the project dir and emit the
# canonical receipt for every entry of every story's `mcp_active_dispatches`,
# and the CLI returned `status=bound` before the role was read. The role marker
# existed and this path ignored it, so the substitution the scoped path had
# just closed was still reachable whenever any binding was active.


def _write_two_story_state(orch: Path, *, se_on: str = "", qe_on: str = "") -> None:
    """A board where two stories can hold bindings at once."""
    stories = []
    for sid in ("US-042", "US-043"):
        story: dict = {"id": sid, "state": "in_progress"}
        dispatches: dict = {}
        if se_on == sid:
            dispatches["se"] = {"dispatch_id": "a" * 32, "started_at": "2026-05-20T11:00:00Z"}
        if qe_on == sid:
            dispatches["qe"] = {"dispatch_id": "b" * 32, "started_at": "2026-05-20T11:00:00Z"}
        if dispatches:
            story["mcp_active_dispatches"] = dispatches
        stories.append(story)
    (orch / "pipeline-state.json").write_text(
        json.dumps({"build_mode": "scrum", "current_stories": stories}),
        encoding="utf-8",
    )


@pytest.mark.hook
def test_a_bound_se_dispatch_does_not_answer_a_qe_stop(
    verify_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """SE holds the only binding and its receipt exists. A QE stop wrote
    nothing, so it must still report missing rather than borrow SE's."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(
        project_dir, quality_enforcement="strict", marker_role="quality-engineer"
    )
    orch = suite / ".orchestrator"
    _write_two_story_state(orch, se_on="US-042")
    _write_receipt(
        orch / "receipts", project_dir, "US-042-se.json",
        story_id="US-042", role="software-engineer", mtime=time.time(),
    )

    result = run_hook(
        verify_hook, env=hook_env,
        stdin=_subagent_stop_stdin(description="quality-engineer"),
    )
    combined = result.stderr + result.stdout
    # Asserting the RECEIPT_MISSING outcome, not the absence of a filename: a
    # receipt that is selected and validates cleanly prints no filename at
    # all, so a name-absence assertion passes whether or not the SE receipt
    # answered for QE.
    assert result.returncode == 2, (
        f"a bound SE receipt answered a QE stop that wrote nothing; "
        f"rc={result.returncode} combined={combined!r}"
    )
    assert "must produce a receipt" in combined


@pytest.mark.hook
def test_a_bound_invalid_receipt_from_another_role_does_not_block(
    verify_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """The inverse: QE's own receipt is valid, SE's is bound and invalid."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(
        project_dir, quality_enforcement="strict",
        marker_role="quality-engineer", marker_story="US-042",
        marker_dispatch="b" * 32,
    )
    orch = suite / ".orchestrator"
    _write_two_story_state(orch, se_on="US-042", qe_on="US-042")
    now = time.time()
    _write_receipt(
        orch / "receipts", project_dir, "US-042-qe.json",
        story_id="US-042", role="quality-engineer", mtime=now,
    )
    _write_receipt(
        orch / "receipts", project_dir, "US-042-se.json",
        story_id="US-042", role="software-engineer", valid=False, mtime=now,
    )

    result = run_hook(
        verify_hook, env=hook_env,
        stdin=_subagent_stop_stdin(description="quality-engineer"),
    )
    assert result.returncode == 0, (
        f"a bound invalid SE receipt blocked a valid QE stop; "
        f"stderr={result.stderr!r}"
    )


@pytest.fixture
def inject_hook(plugin_root: Path) -> Path:
    return plugin_root / "hooks" / "synaptory-inject-protocols.sh"


def _fire_subagent_start(inject_hook, hook_env, run_hook, role: str) -> None:
    """Write the marker the way production does, by running the REAL start hook.

    Fabricating a marker through the test helper is how the first parallel
    regression here proved nothing: it wrote a story and dispatch id that
    `active_dispatch_for` cannot produce when two stories hold the same role,
    so the test asserted a capability the product does not have. Anything
    claiming to cover the parallel case has to come through this.
    """
    payload = json.dumps({
        "agent_id": _TEST_AGENT_ID,
        "agent_type": "synaptory:%s" % role,
        "session_id": "test-session-396",
        "hook_event_name": "SubagentStart",
    })
    env = {**hook_env, "SYNAPTORY_AUTH_NO_GATE": "1"}
    result = run_hook(inject_hook, env=env, stdin=payload)
    assert result.returncode == 0, f"SubagentStart failed: {result.stderr}"


# ── overlapping same-role agents: the parallel shape, not a stale edge case ──
#
# #396. Role, the receipt's self-declared identity and a causal time window
# together still do not say WHICH story a stop belongs to: a receipt written by
# an overlapping agent after this one started passes all three. Both modes need
# the assignment recorded at start, and the scoped mode has to refuse when the
# board cannot name one.


def _two_in_flight(orch: Path, state: str = "testing") -> None:
    """Two stories held by the SAME role, which is the ambiguous board.

    Every caller below overlaps two QE agents, so the stories sit in
    `testing`. Two stories in DIFFERENT states are held by different roles
    and are the ordinary pipeline, not an overlap.
    """
    (orch / "pipeline-state.json").write_text(
        json.dumps({"build_mode": "scrum", "current_stories": [
            {"id": "US-042", "state": state},
            {"id": "US-099", "state": state},
        ]}),
        encoding="utf-8",
    )


@pytest.mark.hook
def test_scoped_overlap_does_not_let_another_story_answer(
    verify_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """Two QE agents overlap on different stories with no binding, which is
    the ordinary Claude shape. US-042 wrote nothing; US-099's receipt is
    newer than the US-042 marker and would pass role, canonical identity and
    the causal window. It must not answer this stop."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(
        project_dir, quality_enforcement="strict",
        marker_role="quality-engineer", marker_story="?ambiguous",
    )
    orch = suite / ".orchestrator"
    _two_in_flight(orch)
    _write_receipt(
        orch / "receipts", project_dir, "US-099-qe.json",
        story_id="US-099", role="quality-engineer", mtime=time.time(),
    )

    result = run_hook(
        verify_hook, env=hook_env,
        stdin=_subagent_stop_stdin(description="quality-engineer"),
    )
    combined = result.stderr + result.stdout
    assert result.returncode == 2, (
        f"an overlapping agent's receipt suppressed receipt_missing; "
        f"rc={result.returncode} combined={combined!r}"
    )


@pytest.mark.hook
def test_scoped_overlap_does_not_let_another_story_block(
    verify_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """The inverse. US-042's own receipt is valid and US-099's is not; both
    are newer than both markers. The invalid one belongs to the other agent
    and must not block this stop."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(
        project_dir, quality_enforcement="strict",
        marker_role="quality-engineer", marker_story="US-042",
    )
    orch = suite / ".orchestrator"
    _two_in_flight(orch)
    now = time.time()
    _write_receipt(
        orch / "receipts", project_dir, "US-042-qe.json",
        story_id="US-042", role="quality-engineer", mtime=now,
    )
    _write_receipt(
        orch / "receipts", project_dir, "US-099-qe.json",
        story_id="US-099", role="quality-engineer", valid=False, mtime=now,
    )

    result = run_hook(
        verify_hook, env=hook_env,
        stdin=_subagent_stop_stdin(description="quality-engineer"),
    )
    combined = result.stderr + result.stdout
    assert result.returncode == 0, (
        f"an overlapping agent's invalid receipt blocked this stop; "
        f"stderr={result.stderr!r}"
    )
    assert "US-099-qe.json" not in combined


@pytest.mark.hook
def test_an_ambiguous_assignment_is_recorded_at_start_not_guessed_at_stop(
    verify_hook: Path,
    hook_env: dict[str, str],
    run_hook,
):
    """`active_dispatch_for` must report ambiguity rather than pick, so the
    refusal above is reachable at all."""
    import subprocess
    import sys

    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(project_dir, marker_role="quality-engineer")
    _two_in_flight(suite / ".orchestrator")

    helper = Path(__file__).resolve().parents[2] / "hooks" / "lib" / "dispatched_receipts.py"
    out = subprocess.run(
        [sys.executable, str(helper), "--resolve-dispatch",
         str(project_dir), "quality-engineer"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert out.split("\t")[0] == "?ambiguous", (
        f"two in-flight stories must record ambiguity, got {out!r}"
    )


# ── real Start to Stop, no fabricated markers ────────────────────────────────
#
# #396. The previous parallel regressions wrote `marker_story` and
# `marker_dispatch` through the test helper, a state `active_dispatch_for`
# cannot produce when two stories hold the same role. They asserted a
# capability the product does not have. Everything below fires the real
# SubagentStart hook first, so the marker is whatever production would write.


def _two_qe_bindings(orch: Path) -> None:
    (orch / "pipeline-state.json").write_text(
        json.dumps({"build_mode": "scrum", "current_stories": [
            {"id": "US-042", "state": "in_progress", "mcp_active_dispatches": {
                "qe": {"dispatch_id": "b" * 32, "started_at": "2026-05-20T11:00:00Z"}}},
            {"id": "US-043", "state": "in_progress", "mcp_active_dispatches": {
                "qe": {"dispatch_id": "c" * 32, "started_at": "2026-05-20T11:00:00Z"}}},
        ]}),
        encoding="utf-8",
    )


@pytest.mark.hook
def test_real_start_records_ambiguity_for_two_same_role_bindings(
    inject_hook, verify_hook: Path, hook_env: dict[str, str], run_hook,
):
    """The honest capability, replacing a test that claimed a better one.

    Two live QE dispatches cannot be told apart from the role, which is all
    the host gives SubagentStart. Production therefore records ambiguity, and
    the marker's second line is NOT a story id.
    """
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(project_dir, marker_role="")
    _two_qe_bindings(suite / ".orchestrator")

    _fire_subagent_start(inject_hook, hook_env, run_hook, "quality-engineer")

    marker = suite / ".orchestrator" / "subagent-markers" / _TEST_AGENT_ID
    lines = marker.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "synaptory:quality-engineer"
    assert lines[1].split("\t")[0] == "?ambiguous", (
        f"production must record ambiguity, not a guessed story; got {lines[1]!r}"
    )


@pytest.mark.hook
def test_real_start_to_stop_bound_overlap_reports_missing(
    inject_hook, verify_hook: Path, hook_env: dict[str, str], run_hook,
):
    """Missing-current direction, end to end. Two live QE dispatches; this
    agent wrote nothing and its peer's receipt exists. The peer must not
    answer for it."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(
        project_dir, quality_enforcement="strict", marker_role=""
    )
    orch = suite / ".orchestrator"
    _two_qe_bindings(orch)
    _fire_subagent_start(inject_hook, hook_env, run_hook, "quality-engineer")
    _write_receipt(
        orch / "receipts", project_dir, "US-043-qe.json",
        story_id="US-043", role="quality-engineer", mtime=time.time(),
    )

    result = run_hook(
        verify_hook, env=hook_env,
        stdin=_subagent_stop_stdin(description="quality-engineer"),
    )
    combined = result.stderr + result.stdout
    assert result.returncode == 2, (
        f"a peer dispatch's receipt suppressed receipt_missing; "
        f"rc={result.returncode} combined={combined!r}"
    )


@pytest.mark.hook
def test_real_start_to_stop_bound_overlap_does_not_block(
    inject_hook, verify_hook: Path, hook_env: dict[str, str], run_hook,
):
    """Invalid-peer direction, end to end. The peer's receipt is invalid and
    must not block, which under ambiguity means nothing is validated at all
    rather than the wrong thing being validated."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(
        project_dir, quality_enforcement="strict", marker_role=""
    )
    orch = suite / ".orchestrator"
    _two_qe_bindings(orch)
    _fire_subagent_start(inject_hook, hook_env, run_hook, "quality-engineer")
    now = time.time()
    _write_receipt(
        orch / "receipts", project_dir, "US-042-qe.json",
        story_id="US-042", role="quality-engineer", mtime=now,
    )
    _write_receipt(
        orch / "receipts", project_dir, "US-043-qe.json",
        story_id="US-043", role="quality-engineer", valid=False, mtime=now,
    )

    result = run_hook(
        verify_hook, env=hook_env,
        stdin=_subagent_stop_stdin(description="quality-engineer"),
    )
    combined = result.stderr + result.stdout
    assert "US-043-qe.json" not in combined, (
        f"a peer dispatch's invalid receipt was validated; combined={combined!r}"
    )
    # Non-vacuous: under ambiguity neither receipt may be attributed, so the
    # stop has to refuse rather than quietly pass on the peer's valid one.
    assert result.returncode == 2, (
        f"an unattributable stop was allowed through; "
        f"rc={result.returncode} combined={combined!r}"
    )
    assert "US-042-qe.json" not in combined, (
        f"an unattributable receipt was validated anyway; combined={combined!r}"
    )


@pytest.mark.hook
def test_real_start_to_stop_unbound_overlap_reports_missing(
    inject_hook, verify_hook: Path, hook_env: dict[str, str], run_hook,
):
    """The same overlap with NO bindings, which is the ordinary Claude QE and
    CR shape, driven end to end."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(
        project_dir, quality_enforcement="strict", marker_role=""
    )
    orch = suite / ".orchestrator"
    _two_in_flight(orch)
    _fire_subagent_start(inject_hook, hook_env, run_hook, "quality-engineer")
    _write_receipt(
        orch / "receipts", project_dir, "US-099-qe.json",
        story_id="US-099", role="quality-engineer", mtime=time.time(),
    )

    result = run_hook(
        verify_hook, env=hook_env,
        stdin=_subagent_stop_stdin(description="quality-engineer"),
    )
    combined = result.stderr + result.stdout
    assert result.returncode == 2, (
        f"an overlapping unbound agent's receipt answered this stop; "
        f"rc={result.returncode} combined={combined!r}"
    )


@pytest.mark.hook
def test_real_start_to_stop_single_story_still_resolves(
    inject_hook, verify_hook: Path, hook_env: dict[str, str], run_hook,
):
    """The control. Ambiguity must cost nothing when there is none: one story
    in flight resolves end to end and its receipt is validated."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(
        project_dir, quality_enforcement="strict", marker_role=""
    )
    orch = suite / ".orchestrator"
    _write_flat_state(orch, bind_se_on=None, state="testing")
    _fire_subagent_start(inject_hook, hook_env, run_hook, "quality-engineer")
    _write_receipt(
        orch / "receipts", project_dir, "US-042-qe.json",
        story_id="US-042", role="quality-engineer", mtime=time.time(),
    )

    result = run_hook(
        verify_hook, env=hook_env,
        stdin=_subagent_stop_stdin(description="quality-engineer"),
    )
    assert result.returncode == 0, (
        f"an unambiguous single-story stop failed; stderr={result.stderr!r}"
    )


# ── transcript recovery must obey the same assignment as selection ───────────
#
# #396. Recovery reaches PAST the filesystem, into text the agent produced.
# Unconstrained, it reintroduced exactly what selection had refused: an
# ambiguous marker made selection return nothing, recovery then wrote an
# unrelated story's receipt out of the transcript, and a strict stop returned
# 0. These drive the real SubagentStart hook first, so the marker is whatever
# production would write.


def _receipt_body(project_dir: Path, story_id: str, role: str) -> dict:
    artifact_rel = "api/routes/auth.py"
    artifact_abs = project_dir / artifact_rel
    artifact_abs.parent.mkdir(parents=True, exist_ok=True)
    artifact_abs.write_text("# stub\n", encoding="utf-8")
    return {
        "story_id": story_id,
        "role": role,
        "backend": "claude",
        "model": "claude-sonnet-4-5",
        "artifacts": [artifact_rel],
        "verification_commands": [
            {"command": "echo ok", "exit_code": 0, "summary": "smoke check passes"}
        ],
        "metrics": {"tests_added": 1, "coverage_delta": 0.0},
        "completed_at": "2026-05-20T12:00:00Z",
    }


def _transcript_with(project_dir: Path, story_id: str, role: str) -> Path:
    """A parent transcript whose assistant text carries a valid receipt the
    agent never wrote to disk. This is the shape recovery exists for."""
    body = json.dumps(_receipt_body(project_dir, story_id, role), indent=2)
    line = json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Done.\n```json\n%s\n```\n" % body},
        ]},
    })
    path = project_dir / "transcript.jsonl"
    path.write_text(line + "\n", encoding="utf-8")
    return path


@pytest.mark.hook
def test_recovery_refuses_under_an_ambiguous_assignment(
    inject_hook, verify_hook: Path, hook_env: dict[str, str], run_hook,
):
    """The reviewer's reproduction, end to end. Two bound QE dispatches, no
    receipt on disk, an unrelated valid QE receipt in the transcript."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(
        project_dir, quality_enforcement="strict", marker_role=""
    )
    _two_qe_bindings(suite / ".orchestrator")
    _fire_subagent_start(inject_hook, hook_env, run_hook, "quality-engineer")
    transcript = _transcript_with(project_dir, "US-099", "quality-engineer")

    result = run_hook(
        verify_hook, env=hook_env,
        stdin=json.dumps({
            "description": "quality-engineer",
            "agent_id": _TEST_AGENT_ID,
            "session_id": "test-session-396",
            "transcript_path": str(transcript),
            "hook_event_name": "SubagentStop",
        }),
    )
    combined = result.stderr + result.stdout
    assert "US-099-qe.json" not in combined, (
        f"recovery reintroduced a receipt selection refused; combined={combined!r}"
    )
    assert not (
        suite / ".orchestrator" / "receipts" / "US-099-qe.json"
    ).exists(), "recovery wrote an unattributed receipt to disk"
    assert result.returncode == 2, (
        f"an unattributable stop passed via recovery; "
        f"rc={result.returncode} combined={combined!r}"
    )


@pytest.mark.hook
def test_recovery_refuses_a_receipt_for_another_role(
    inject_hook, verify_hook: Path, hook_env: dict[str, str], run_hook,
):
    """One story in flight, so the assignment is unambiguous, but the only
    receipt in the transcript belongs to the SE that ran beside this QE."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(
        project_dir, quality_enforcement="strict", marker_role=""
    )
    _write_flat_state(suite / ".orchestrator", bind_se_on=None, state="testing")
    _fire_subagent_start(inject_hook, hook_env, run_hook, "quality-engineer")
    transcript = _transcript_with(project_dir, "US-042", "software-engineer")

    result = run_hook(
        verify_hook, env=hook_env,
        stdin=json.dumps({
            "description": "quality-engineer",
            "agent_id": _TEST_AGENT_ID,
            "session_id": "test-session-396",
            "transcript_path": str(transcript),
            "hook_event_name": "SubagentStop",
        }),
    )
    combined = result.stderr + result.stdout
    assert not (
        suite / ".orchestrator" / "receipts" / "US-042-se.json"
    ).exists(), "a QE stop recovered the SE's receipt"
    assert result.returncode == 2, (
        f"an SE receipt answered a QE stop through recovery; "
        f"rc={result.returncode} combined={combined!r}"
    )


@pytest.mark.hook
def test_recovery_refuses_a_receipt_for_another_story(
    inject_hook, verify_hook: Path, hook_env: dict[str, str], run_hook,
):
    """Right role, wrong story. The assignment names US-042; the transcript
    carries a QE receipt for US-099."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(
        project_dir, quality_enforcement="strict", marker_role=""
    )
    _write_flat_state(suite / ".orchestrator", bind_se_on=None, state="testing")
    _fire_subagent_start(inject_hook, hook_env, run_hook, "quality-engineer")
    transcript = _transcript_with(project_dir, "US-099", "quality-engineer")

    result = run_hook(
        verify_hook, env=hook_env,
        stdin=json.dumps({
            "description": "quality-engineer",
            "agent_id": _TEST_AGENT_ID,
            "session_id": "test-session-396",
            "transcript_path": str(transcript),
            "hook_event_name": "SubagentStop",
        }),
    )
    combined = result.stderr + result.stdout
    assert not (
        suite / ".orchestrator" / "receipts" / "US-099-qe.json"
    ).exists(), "recovery wrote a receipt for a story this agent was not assigned"
    assert result.returncode == 2, (
        f"a foreign-story receipt answered this stop; "
        f"rc={result.returncode} combined={combined!r}"
    )


@pytest.mark.hook
def test_recovery_still_works_for_the_assigned_story_and_role(
    inject_hook, verify_hook: Path, hook_env: dict[str, str], run_hook,
):
    """The control. Constraining recovery must not disable it: the agent's
    OWN receipt, emitted in text and never written, is still recovered."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(
        project_dir, quality_enforcement="strict", marker_role=""
    )
    _write_flat_state(suite / ".orchestrator", bind_se_on=None, state="testing")
    _fire_subagent_start(inject_hook, hook_env, run_hook, "quality-engineer")
    transcript = _transcript_with(project_dir, "US-042", "quality-engineer")

    result = run_hook(
        verify_hook, env=hook_env,
        stdin=json.dumps({
            "description": "quality-engineer",
            "agent_id": _TEST_AGENT_ID,
            "session_id": "test-session-396",
            "transcript_path": str(transcript),
            "hook_event_name": "SubagentStop",
        }),
    )
    combined = result.stderr + result.stdout
    assert (
        suite / ".orchestrator" / "receipts" / "US-042-qe.json"
    ).exists(), f"recovery stopped working for the assigned pair; combined={combined!r}"
    assert result.returncode == 0, (
        f"a correctly recovered receipt still blocked; "
        f"rc={result.returncode} combined={combined!r}"
    )


@pytest.mark.hook
def test_recovery_refuses_a_storyless_receipt_under_ambiguity(
    inject_hook, verify_hook: Path, hook_env: dict[str, str], run_hook,
):
    """`story_id` is not part of the receipt fingerprint, so a receipt that
    omits it is still recoverable and takes its story from the assignment.
    Under ambiguity that assignment is the sentinel, which without an explicit
    refusal would be written to disk as `?ambiguous-qe.json`. This is the case
    the sentinel check alone covers: the story-mismatch check cannot see it,
    because there is no declared story to disagree with."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    suite = _make_workspace(
        project_dir, quality_enforcement="strict", marker_role=""
    )
    _two_qe_bindings(suite / ".orchestrator")
    _fire_subagent_start(inject_hook, hook_env, run_hook, "quality-engineer")

    body = _receipt_body(project_dir, "US-042", "quality-engineer")
    del body["story_id"]
    line = json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "text",
             "text": "Done.\n```json\n%s\n```\n" % json.dumps(body, indent=2)},
        ]},
    })
    transcript = project_dir / "transcript.jsonl"
    transcript.write_text(line + "\n", encoding="utf-8")

    result = run_hook(
        verify_hook, env=hook_env,
        stdin=json.dumps({
            "description": "quality-engineer",
            "agent_id": _TEST_AGENT_ID,
            "session_id": "test-session-396",
            "transcript_path": str(transcript),
            "hook_event_name": "SubagentStop",
        }),
    )
    receipts = suite / ".orchestrator" / "receipts"
    written = sorted(p.name for p in receipts.glob("*.json"))
    assert written == [], (
        f"recovery wrote a receipt under an ambiguous assignment: {written}"
    )
    assert result.returncode == 2, (
        f"an unattributable stop passed; rc={result.returncode}"
    )
