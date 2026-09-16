"""Layer 2 -- the dispatch envelope `execute_dispatch` actually builds, against
a realistic SPQ Cycle (#447).

`#464` gave `select_runtime` and `build_envelope` their production caller and
`#466` made a governed project that cannot build an envelope fail closed. What
neither had was a fixture with a real SPQ binding, so no test ever saw a
COMPLETE envelope: `#464`'s never called `validate_envelope`, and its fixture
had no open Cycle, so four required fields were empty and the assertions still
passed. Every case here drives `execute_dispatch` and validates what comes out
against the frozen contract.

Layer 2 rather than Layer 1 because the fixture is a real git checkout: the
envelope's `source_revision` is a commit, not a placeholder, and it is what
lets a later reader say which tree an attempt's evidence describes.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import _spq_fixture
import advance_kernel as ak
import runtime_contracts as rc
import runtime_selector as rsel


STAGE_ENTERED = "2026-01-01T00:00:00+00:00"
def _fresh_completed_at() -> str:
    """A completion stamped AFTER the stage was entered.

    `begin_dispatch` moves the story at real time, so a fixture constant is in
    the past and the receipt is refused as `stale_receipt`, which masks
    whatever the test was actually about.
    """
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()

RUNTIMES_SECTION = """runtimes:
  version: 1
  enabled: true
  allowed_profiles:
    - claude-local-v1
"""


def _story(story_id: str = "WU-001") -> dict:
    return {
        "id": story_id,
        "title": "Seed unit",
        "state": "queued",
        "blocked_reason": None,
        "blocked_from": None,
        "backend": {},
        "pipeline_log": [
            {"state": "queued", "entered_at": STAGE_ENTERED, "exited_at": None}
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


def _write_snapshot(project: Path, entries) -> None:
    path = project.joinpath(*rsel.AVAILABILITY_RELPATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "probed_at": "2026-09-02T00:00:00Z",
                "reports": [
                    {"profile_id": pid, "available": ok, "reason": why}
                    for pid, ok, why in entries
                ],
            }
        ),
        encoding="utf-8",
    )


def _spq_project(tmp_path: Path, *, runtimes: bool, project_id: str = "taskflow") -> Path:
    """An SPQ checkout with one Cycle open, built BY `open_cycle`.

    This hand-wrote the pre-#303 state into `pipeline-state.json` and a
    manifest whose `manifest_hash` was `"sha256:" + "a" * 64` -- a digest of
    nothing. Against the #644 runtime none of it resolves a Cycle, so
    `next_action` answered for DISCOVERY and every envelope assertion below
    failed with `next_action_mismatch`: a fixture verdict wearing a gate's
    name (#514). The product places the board now, and the sealed declaration
    it writes is one that actually verifies.
    """
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    (project / ".synaptory" / ".orchestrator" / "receipts").mkdir(parents=True)
    # NO `spq.workstreams` BLOCK: `SPD-194` retires the lane.
    config = "build_mode: spq\nproject_id: %s\n" % project_id
    if runtimes:
        config += RUNTIMES_SECTION
    (project / ".synaptory.yaml").write_text(config, encoding="utf-8")

    # `source_revision` is a required envelope field and it is a real commit,
    # not a placeholder: it is what lets a later reader say which tree an
    # attempt's evidence describes. Initialised BEFORE `open_cycle`, which
    # refuses a gitignored transport for the seal.
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.test"],
        ["git", "config", "user.name", "T"],
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "seed"],
    ):
        subprocess.run(argv, cwd=str(project), check=True, capture_output=True)

    _spq_fixture.open_project(
        project,
        units=[{"id": "WU-001", "title": "Seed unit"}],
        goal="wire the envelope",
    )

    # The probe snapshot the kernel reads. `synaptory runtimes doctor` writes
    # it (#464); an absent snapshot is inert, never optimistic, so a test that
    # wants a selection has to have probed.
    _write_snapshot(project, [("claude-local-v1", True, "")])

    for argv in (
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "cycle"],
    ):
        subprocess.run(argv, cwd=str(project), check=True, capture_output=True)
    return project


def _board_story(project: Path, story_id: str = "WU-001") -> dict:
    """One unit off THE board, whichever layout this project is on."""
    import story_pipeline as sp  # type: ignore

    return sp.get_story(sp._read_state(str(project)), story_id) or {}


def _sealed_digest(project: Path) -> str:
    """The digest of the Cycle's sealed declaration, from the product."""
    import spq_manifest
    import spq_state_machine

    return spq_manifest.sealed_hash(
        spq_state_machine.read_manifest(str(project), _cycle_id(project))
    )


def _cycle_id(project: Path) -> str:
    """The Cycle `open_cycle` allocated. Asked for, not hardcoded: `CYCLE_ID`
    was a constant this file both wrote and read, which is only consistent
    while the fixture writes the board itself."""
    import spq_paths

    return spq_paths.resolve_identity(str(project)).cycle_id



@pytest.fixture(autouse=True)
def _no_ambient_project_id(monkeypatch):
    """`SYNAPTORY_PROJECT_ID` is the first source `_project_slug` reads, so an
    ambient one would mask the unresolved-project-id case entirely."""
    monkeypatch.delenv("SYNAPTORY_PROJECT_ID", raising=False)


def _policy() -> ak.HostPolicy:
    return ak.policy_for_claude(enforcement="enforce")


def _dispatch(project: Path):
    return ak.execute_dispatch(
        str(project), "WU-001", role="software-engineer", policy=_policy()
    )


class TestUnenrolledProjectIsUnchanged:
    def test_a_project_with_no_runtimes_section_dispatches_exactly_as_before(
        self, tmp_path
    ):
        """`load_policy` returns an inert policy with a stated reason for this
        case, and inert must stay a NO-OP rather than becoming a refusal.

        "Exactly as before" is asserted on the returned object too, not only on
        the state write: an unenrolled project must not grow a
        `runtime_selection` key, a warning, or a probe. Nothing to opt out of
        means nothing to notice.
        """
        project = _spq_project(tmp_path, runtimes=False)
        decision = _dispatch(project)
        assert decision.allowed, decision.reason
        assert "dispatch_envelope" not in decision.extra
        assert "attempt_registration" not in decision.extra
        assert decision.warnings == [], decision.warnings
        # An unenrolled project records the inert reason and nothing else: no
        # envelope, no registration, and no refusal.
        assert decision.extra["runtime_selection"]["state"] == "inert"
        # The dispatch itself is untouched: same transition, same binding.
        assert decision.story["state"] == "in_progress"
        binding = decision.story["mcp_active_dispatches"]["se"]
        assert binding["attempt_id"].startswith("att_")
        assert decision.extra["receipt_contract"]["receipt_path"]


class TestEnvelopeConstruction:
    def test_dispatch_builds_a_valid_envelope_carrying_the_spq_binding(
        self, tmp_path
    ):
        project = _spq_project(tmp_path, runtimes=True)
        decision = _dispatch(project)
        assert decision.allowed, decision.reason
        envelope = decision.extra.get("dispatch_envelope")
        assert envelope is not None, decision.warnings
        # Validated through the frozen contract, not by eye: the envelope is
        # the interface the Go bridge decodes strictly.
        assert rc.validate_envelope(envelope) == [], envelope

        binding = decision.story["mcp_active_dispatches"]["se"]
        assert envelope["attempt_id"] == binding["attempt_id"]
        assert envelope["dispatch_id"] == binding["dispatch_id"]
        assert envelope["fencing_token"] == binding["fencing_token"]
        assert envelope["expires_at"] == binding["expires_at"]
        assert envelope["cycle_id"] == _cycle_id(project) == binding["cycle_id"]
        # THE SEAL'S OWN DIGEST, asked of the declaration rather than
        # compared to a constant. `MANIFEST_HASH` used to be a stand-in this
        # file wrote into the manifest and then asserted back -- consistent,
        # and a check of nothing. `open_cycle` computes the real one, so the
        # assertion is now that the envelope carries what the Cycle is
        # actually sealed at.
        sealed_digest = _sealed_digest(project)
        assert sealed_digest, "the Cycle has no sealed declaration to bind to"
        assert envelope["manifest_hash"] == sealed_digest == binding["manifest_hash"]
        # PRESENT AND EMPTY, which is the retirement's first half. `SPD-194`
        # retires the Workstream and `_cycle_binding` writes the field blank
        # rather than dropping it, so a bridge built against the old contract
        # still decodes; dropping the key is the second half and a separate
        # release. Asserted rather than deleted, because "gone from the
        # envelope" and "carried empty" are different contracts and only one
        # of them is shipped.
        assert envelope["workstream_id"] == "" == binding.get("workstream_id", "")

    def test_the_envelope_carries_the_platform_overlay_for_this_edge(
        self, tmp_path
    ):
        project = _spq_project(tmp_path, runtimes=True)
        envelope = _dispatch(project).extra["dispatch_envelope"]
        assert envelope["role"] == "se"
        assert envelope["stage_profile"] == "producing"
        assert envelope["capability_profile"] == "producer"
        assert envelope["stage"] == "in_progress"

    def test_the_envelope_names_the_receipt_the_kernel_already_derived(
        self, tmp_path
    ):
        """One receipt path, derived once. A second derivation is how the
        envelope and the advance gate come to disagree about which file is
        the evidence."""
        project = _spq_project(tmp_path, runtimes=True)
        decision = _dispatch(project)
        envelope = decision.extra["dispatch_envelope"]
        contract = decision.extra["receipt_contract"]["receipt_path"]
        assert not Path(envelope["receipt_path"]).is_absolute()
        assert (project / envelope["receipt_path"]).resolve() == Path(contract).resolve()

    def test_the_capability_ceiling_cannot_exceed_the_certified_profile(
        self, tmp_path
    ):
        """Nothing on the dispatch path may widen authority (8.2, SP-AUT-004).
        The producing ceiling wants workspace.write; a profile that provides
        only read must not be handed one that says otherwise, so the ceiling is
        an intersection rather than a lookup."""
        project = _spq_project(tmp_path, runtimes=True)
        override = project / ".synaptory" / "runtime-profiles.json"
        override.write_text(
            json.dumps(
                [
                    {
                        "profile_id": "claude-local-v1",
                        "runtime_family": "claude-code",
                        "placement": "local",
                        "pinned_version": "PIN-AT-PHASE-B",
                        "capability_profiles": ["producer", "prover"],
                        "capabilities": ["workspace.read", "receipt.v2"],
                    }
                ]
            ),
            encoding="utf-8",
        )
        envelope = _dispatch(project).extra["dispatch_envelope"]
        assert envelope["capability_ceiling"] == ["receipt.v2"]

    def test_a_profile_declaring_nothing_grants_nothing(self, tmp_path):
        """The absence-means-unbounded error, refused. #464 read a
        `capabilities` attribute `SelectionResult` never had, so every envelope
        carried an empty ceiling; the fix must not turn empty into a wildcard
        on the way past."""
        project = _spq_project(tmp_path, runtimes=True)
        (project / ".synaptory" / "runtime-profiles.json").write_text(
            json.dumps(
                [
                    {
                        "profile_id": "claude-local-v1",
                        "runtime_family": "claude-code",
                        "placement": "local",
                        "pinned_version": "PIN-AT-PHASE-B",
                        "capability_profiles": ["producer"],
                        "capabilities": [],
                    }
                ]
            ),
            encoding="utf-8",
        )
        envelope = _dispatch(project).extra["dispatch_envelope"]
        assert envelope["capability_ceiling"] == []

    def test_a_producer_edge_declares_the_producer_evidence_obligations(
        self, tmp_path
    ):
        project = _spq_project(tmp_path, runtimes=True)
        envelope = _dispatch(project).extra["dispatch_envelope"]
        assert envelope["capability_ceiling"] == [
            "workspace.write",
            "process.test",
            "receipt.v2",
        ]
        assert envelope["required_evidence"] == [
            "build-result",
            "changed-files",
            "final-summary",
        ]

    def test_an_unresolvable_project_id_refuses_and_registers_nothing(
        self, tmp_path
    ):
        """A project id nobody declared must not be invented.

        The specific trap: `project_id:` with an EMPTY value. #464's pattern
        used a whitespace class that matches a newline, so it captured the
        next key's name
        and this project would have registered its attempts against a project
        called "spq" -- the #320 shape, a locally invented slug addressed at a
        real control plane. Under #466's fail-closed rule an unresolvable
        project id now refuses, and nothing leaves the machine.
        """
        project = _spq_project(tmp_path, runtimes=True, project_id="")
        decision = _dispatch(project)
        assert not decision.allowed
        assert decision.code == ak.PROFILE_MISMATCH
        assert "dispatch_envelope" not in decision.extra
        assert "project_id is unresolved" in decision.reason, decision.reason
        assert "spq" not in decision.reason.split("project_id is unresolved")[0]


class TestSelectionFailuresAreExplicitAndNonBlocking:
    def test_an_unavailable_runtime_refuses_rather_than_substituting(
        self, tmp_path
    ):
        """A probe that found nothing available is a real answer, and running
        the dispatch on whatever happens to be present is the silent
        degradation EP-12 forbids. #464 chose to refuse; #447 keeps that and
        moves it BEFORE the state write, which is what the board assertion
        below is for."""
        project = _spq_project(tmp_path, runtimes=True)
        _write_snapshot(project, [("claude-local-v1", False, "claude is not installed")])
        decision = _dispatch(project)
        assert not decision.allowed
        assert decision.code == ak.PROFILE_MISMATCH
        assert "dispatch_envelope" not in decision.extra
        selection = decision.extra["runtime_selection"]
        assert selection["state"] == "denied"
        # The selector's explanation, not a generic sentence: #464 read a
        # `reason` attribute `Rejection` does not have, so every denial
        # discarded which requirement excluded which profile.
        assert "claude is not installed" in selection["explanation"], selection
        assert "no-eligible-profile" in selection["reason"], selection

    def test_a_denied_selection_does_not_leave_the_story_dispatched(
        self, tmp_path
    ):
        """The ordering half. #464 ran selection AFTER `sp._write_state`, so a
        denial refused the dispatch having already transitioned the story and
        persisted its binding: the caller was told the dispatch did not happen
        while the board said it did."""
        project = _spq_project(tmp_path, runtimes=True)
        _write_snapshot(project, [("claude-local-v1", False, "not installed")])
        assert not _dispatch(project).allowed
        # THROUGH THE RESOLVER. Opening `pipeline-state.json` and expecting
        # `current_stories` is the exact defect `core/lib/pipeline_board.py`
        # exists to prevent: under `build_mode: spq` that file is a mode plus
        # identity POINTER and the board lives under the Cycle, so a direct
        # reader sees nothing and reports it as an empty board. Found four
        # separate times in the product (#505, #509, #514); a test can acquire
        # it too, and here it did.
        story = _board_story(project)
        assert story["state"] == "queued", "a refused dispatch must not move the story"
        assert not story.get("mcp_active_dispatches"), story

    def test_no_probe_at_all_stays_inert_rather_than_refusing(
        self, tmp_path
    ):
        """None and () mean different things. Nobody having probed is not an
        answer, so the existing dispatch path continues untouched; a probe that
        found nothing is an answer, and refuses (the case above)."""
        project = _spq_project(tmp_path, runtimes=True)
        (project.joinpath(*rsel.AVAILABILITY_RELPATH)).unlink()
        decision = _dispatch(project)
        assert decision.allowed, decision.reason
        assert "dispatch_envelope" not in decision.extra
        assert decision.extra["runtime_selection"]["state"] == "inert"
        assert "runtimes doctor" in decision.extra["runtime_selection"]["reason"]


# ─── the selection decides which executor runs, not just what is reported ────


def _active_binding(project: Path, abbrev: str = "se") -> dict:
    """Read through the shipped resolver, not the raw file.

    Under SPQ the board lives at `spq/cycles/<cycle-id>/execution-state.json`
    and `pipeline-state.json` is only a pointer, so the path a test guesses is
    not the one the kernel writes.
    """
    import story_pipeline as sp  # type: ignore

    state = sp._read_state(str(project))
    story = sp.get_story(state, "WU-001")
    return (story or {}).get("mcp_active_dispatches", {})[abbrev]


def test_the_selected_runtime_family_is_recorded_on_the_binding(tmp_path):
    """A selection a host can DISPLAY but nothing enforces cannot change which
    executor runs: Auto/Prefer/Pin were visible and inert, because Cursor and
    Codex surfaced the envelope and then spawned their own native agent
    anyway (#396).

    Persisting the chosen family is what lets advance refuse a receipt produced
    by a different one, which is the difference between a recommendation and a
    routing decision.
    """
    project = _spq_project(tmp_path, runtimes=True)
    _write_snapshot(project, [("claude-local-v1", True, "")])

    decision = _dispatch(project)
    assert decision.allowed is True, decision.reason
    account = decision.extra["runtime_selection"]
    assert account["state"] == "selected", account

    binding = _active_binding(project)
    assert binding.get("runtime_family") == account["runtime_family"], binding
    assert binding.get("adapter_profile_id") == account["profile_id"], binding


#: The overlay pair this fixture's edge (`in_progress -> testing`, se) runs
#: under, pinned as literals by
#: `test_the_envelope_carries_the_platform_overlay_for_this_edge` above. Since
#: #473 a governed dispatch's receipt MUST carry it -- the dispatch hands the
#: agent an envelope naming both values -- so a fixture receipt that omitted it
#: would be refused as `receipt_invalid` before reaching the rule under test.
SE_OVERLAY = {"stage_profile": "producing", "capability_profile": "producer"}


def _write_receipt(project: Path, contract: dict, binding: dict, backend: str) -> None:
    """A valid receipt for this dispatch, produced by `backend`."""
    receipt_path = Path(contract["receipt_path"])
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    artifact = project / "src" / "x.py"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("# stub\n", encoding="utf-8")
    receipt_path.write_text(json.dumps({
        "story_id": "WU-001",
        "role": "software-engineer",
        "backend": backend,
        "model": "e2e-fixture",
        "dispatch_id": binding["dispatch_id"],
        "attempt_id": binding.get("attempt_id"),
        "fencing_token": binding.get("fencing_token"),
        "artifacts": ["src/x.py"],
        "verification_commands": [
            {"command": "true", "exit_code": 0, "summary": "passed"}
        ],
        "metrics": {"files_changed": 1},
        "completed_at": _fresh_completed_at(),
        **SE_OVERLAY,
    }), encoding="utf-8")


@pytest.mark.parametrize("host_backend", ["codex", "cursor"])
def test_a_cross_family_receipt_lands_on_a_host_of_another_family(
    tmp_path, host_backend
):
    """#346: a host invoking a runtime that is NOT its own family.

    This could not land evidence at all. The host-backend rule requires
    `backend == codex` on Codex and `cursor` on Cursor, and it ran BEFORE the
    selected-family comparison, so when Codex selected claude-code the Claude
    adapter's correct `backend: claude` receipt was rejected as
    `backend_mismatch`. The enforcement worked only in the negative direction.

    A binding that carries a selection is judged against that selection now,
    and this is the positive half: the SELECTED family's receipt is accepted on
    a host whose own backend is something else.
    """
    project = _spq_project(tmp_path, runtimes=True)
    _write_snapshot(project, [("claude-local-v1", True, "")])
    decision = _dispatch(project)
    assert decision.allowed is True, decision.reason
    assert decision.extra["runtime_selection"]["runtime_family"] == "claude-code"

    binding = _active_binding(project)
    _write_receipt(project, decision.extra["receipt_contract"], binding, "claude")

    # The host-backend binding ALONE, which is the rule this finding is about.
    # Codex and Cursor also bind next_action, and dragging that in would make a
    # failure here ambiguous between two unrelated host rules.
    policy = ak.HostPolicy(
        host=host_backend,
        expected_backend=host_backend,
        require_next_action_match=False,
    )
    result = ak.evaluate_advance(str(project), "WU-001", "testing", policy=policy)
    assert result.allowed is True, (
        "a %s host could not land a receipt from the claude-code runtime it "
        "selected: %s (%s)" % (host_backend, result.reason, result.code)
    )


def test_the_host_backend_rule_still_applies_without_a_selection(tmp_path):
    """The fallback the reordering must not lose. A binding from before
    selection existed carries no family, so the host-backend rule is still what
    decides, and a foreign receipt is still refused."""
    project = _spq_project(tmp_path, runtimes=False)
    decision = _dispatch(project)
    assert decision.allowed is True, decision.reason
    binding = _active_binding(project)
    assert not binding.get("runtime_family"), binding

    _write_receipt(project, decision.extra["receipt_contract"], binding, "codex")
    # A host that BINDS a backend. `policy_for_claude` sets none, so using it
    # here would assert the fallback against a policy that has no fallback.
    policy = ak.HostPolicy(
        host="claude",
        expected_backend="claude",
        require_next_action_match=False,
    )
    result = ak.evaluate_advance(
        str(project), "WU-001", "testing", policy=policy
    )
    assert result.allowed is False, "an ungoverned dispatch lost its backend rule"
    assert result.code == ak.BACKEND_MISMATCH, result.code


def test_a_receipt_from_another_runtime_family_cannot_advance(tmp_path):
    """The enforcement half. Skipping the selected runtime and running the
    host's own agent no longer succeeds quietly: the evidence is refused."""
    project = _spq_project(tmp_path, runtimes=True)
    _write_snapshot(project, [("claude-local-v1", True, "")])
    decision = _dispatch(project)
    assert decision.allowed is True, decision.reason
    selected = decision.extra["runtime_selection"]["runtime_family"]
    assert selected == "claude-code", selected

    binding = _active_binding(project)
    contract = decision.extra["receipt_contract"]
    receipt_path = Path(contract["receipt_path"])
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    artifact = project / "src" / "x.py"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("# stub\n", encoding="utf-8")
    receipt_path.write_text(json.dumps({
        "story_id": "WU-001",
        "role": "software-engineer",
        # Produced by a runtime this dispatch did not select.
        "backend": "codex",
        "model": "e2e-fixture",
        "dispatch_id": binding["dispatch_id"],
        "attempt_id": binding.get("attempt_id"),
        "fencing_token": binding.get("fencing_token"),
        "artifacts": ["src/x.py"],
        "verification_commands": [
            {"command": "true", "exit_code": 0, "summary": "passed"}
        ],
        "metrics": {"files_changed": 1},
        "completed_at": STAGE_ENTERED,
        **SE_OVERLAY,
    }), encoding="utf-8")

    result = ak.evaluate_advance(
        str(project), "WU-001", "testing", policy=_policy()
    )
    assert result.allowed is False, (
        "a receipt from a runtime family the dispatch did not select advanced"
    )
    assert result.code == ak.RUNTIME_FAMILY_MISMATCH, result.code


# ─── Managed placement (#632, Phase D Track M of #355) ───────────────────────

MANAGED_RUNTIMES_SECTION = """runtimes:
  version: 1
  enabled: true
  allowed_profiles:
    - claude-managed-standard-v1
"""

MANAGED_BLOCK = {
    "project_id": "taskflow",
    "vcs": {
        "repo_url": "https://github.com/h3tech-ai/taskflow.git",
        "default_ref": "main",
        "auth_ref": "cred://runner/github-vcs-read",
    },
    "tracker": {"auth_ref": "cred://runner/github-tracker-write"},
    "ci": {},
    "config_sources": ["repo://.synaptory.yaml"],
}


def _managed_project(tmp_path: Path, *, block=MANAGED_BLOCK) -> Path:
    """An SPQ project whose policy admits only the managed profile.

    `block=None` writes no materialization mirror at all, which is the
    unconfigured case the negative tests drive.
    """
    project = _spq_project(tmp_path, runtimes=True)
    config = (project / ".synaptory.yaml").read_text(encoding="utf-8")
    (project / ".synaptory.yaml").write_text(
        config.replace(RUNTIMES_SECTION, MANAGED_RUNTIMES_SECTION), encoding="utf-8"
    )
    _write_snapshot(project, [("claude-managed-standard-v1", True, "")])
    if block is not None:
        import runtime_materialization as rmat

        path = project.joinpath(*rmat.MATERIALIZATION_RELPATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(block), encoding="utf-8")
    return project


class TestManagedPlacementReachesAWorker:
    """Before #632 the kernel refused every `managed-laptop` placement, which
    is the Phase D exit blocker #355 recorded as Track M NO-GO. A selected
    managed profile could never produce an authority anyone could carry, so no
    governed dispatch could reach a managed worker at all."""

    def test_a_managed_dispatch_builds_a_valid_envelope(self, tmp_path):
        project = _managed_project(tmp_path)
        decision = _dispatch(project)
        assert decision.allowed, decision.reason
        envelope = decision.extra.get("dispatch_envelope")
        assert envelope is not None, decision.extra.get("runtime_selection")
        # Validated through the frozen contract, which is what the Go bridge
        # decodes strictly and what the control plane signs.
        assert rc.validate_envelope(envelope) == [], envelope
        assert envelope["placement"] == "managed-laptop"
        assert envelope["adapter_profile_id"] == "claude-managed-standard-v1"

    def test_the_envelope_is_independently_materializable(self, tmp_path):
        """The property the placement exists for: a managed runner has no host
        checkout, so the envelope alone has to name the repository and the
        exact revision to clone."""
        project = _managed_project(tmp_path)
        envelope = _dispatch(project).extra["dispatch_envelope"]
        materialization = envelope["materialization"]
        assert materialization["repo_url"] == MANAGED_BLOCK["vcs"]["repo_url"]
        assert materialization["revision"] == envelope["source_revision"]
        assert materialization["revision"], "a managed attempt pins a real commit"
        # The branch this checkout is on, not the project default: it is the
        # ref that actually contains the revision.
        assert materialization["ref"].startswith("refs/heads/"), materialization
        assert materialization["worktree_mode"] == "clean-isolated"
        assert envelope["workspace"]["mode"] == "clean-isolated"

    def test_credentials_travel_as_references_and_the_llm_slot_stays_empty(
        self, tmp_path
    ):
        """SP-SEC-037. The envelope is an authority ceiling that crosses
        process boundaries and gets logged, so nothing in it may be material.
        `llm` is null because managed Claude authenticates with the runner's
        own subscription login, not an attempt token."""
        project = _managed_project(tmp_path)
        envelope = _dispatch(project).extra["dispatch_envelope"]
        assert envelope["connector_refs"] == {
            "vcs": "cred://runner/github-vcs-read",
            "tracker": "cred://runner/github-tracker-write",
            "ci": None,
            "llm": None,
        }
        assert "cred://runner/github-vcs-read" not in json.dumps(
            envelope["materialization"]
        )

    def test_the_governance_config_is_pinned_inside_the_materialized_tree(
        self, tmp_path
    ):
        project = _managed_project(tmp_path)
        envelope = _dispatch(project).extra["dispatch_envelope"]
        assert envelope["config_refs"]["synaptory_yaml"] == "repo://.synaptory.yaml"
        assert envelope["config_refs"]["preset_base"] == "claude-managed-standard-v1"

    def test_the_envelopes_receipt_path_is_where_the_gate_reads_the_evidence(
        self, tmp_path
    ):
        """#659 Track A, the kernel half of the delivery step.

        A managed attempt runs in a disposable workspace, so its receipt has to
        be filed into the lifecycle checkout before teardown, and the Go bridge
        derives that destination by joining `envelope["receipt_path"]` onto the
        project directory (`cli/internal/cli/runtime_receipt_delivery.go`).
        This asserts the round trip that makes that safe: the path the envelope
        names is the path `canonical_receipt_path` resolves, so a document
        filed there is the document `validate_receipt` and `advance` consume.

        Asserted rather than assumed, because the two sides derive it
        independently. `_envelope_receipt_path` relativizes what
        `canonical_receipt_path` produced and the bridge re-joins it, so a
        change of shape on either side would have a managed attempt filing real
        evidence somewhere nothing reads -- which from the board is
        indistinguishable from an attempt that produced none.
        """
        project = _managed_project(tmp_path)
        decision = _dispatch(project)
        assert decision.allowed, decision.reason
        envelope = decision.extra["dispatch_envelope"]

        rel = envelope["receipt_path"]
        assert not Path(rel).is_absolute(), (
            "a managed receipt_path must be project-relative, or the bridge has "
            "nothing to join onto the lifecycle checkout"
        )
        # THE BRIDGE'S OWN JOIN against the kernel's own resolver, on the story
        # and role this dispatch bound.
        delivered = project / rel
        canonical = ak.canonical_receipt_path(str(project), "WU-001", "se")
        assert delivered.resolve() == canonical.resolve(), (
            "the bridge would file this attempt's receipt at %s and the gate "
            "reads %s" % (delivered, canonical)
        )

        # And the gate accepts a document filed there. This is the acceptance
        # criterion's "against that filed document": a real file at the
        # delivered path, not an in-memory struct and not a digest.
        binding = _active_binding(project)
        _write_receipt(project, {"receipt_path": str(delivered)}, binding, "claude")
        result = ak.evaluate_advance(
            str(project), "WU-001", "testing", policy=_policy()
        )
        assert result.allowed, result.reason
        assert Path(result.receipt_path).resolve() == delivered.resolve(), (
            "the gate validated something other than the delivered document: %s"
            % result.receipt_path
        )

    def test_a_local_placement_still_carries_none_of_these_blocks(self, tmp_path):
        """A local attempt runs inside the checkout it was dispatched from, so
        resolving a materialization block for it would be inventing one."""
        project = _spq_project(tmp_path, runtimes=True)
        envelope = _dispatch(project).extra["dispatch_envelope"]
        assert envelope["placement"] == "local"
        assert envelope["materialization"] is None
        assert envelope["connector_refs"] is None
        assert envelope["workspace"]["mode"] == "existing-locked-worktree"


class TestManagedPlacementFailsClosed:
    def test_an_unconfigured_project_refuses_and_names_the_fix(self, tmp_path):
        """Not a silent degrade to local, and not an invented repository: the
        refusal names the endpoint and the file an operator has to populate."""
        project = _managed_project(tmp_path, block=None)
        decision = _dispatch(project)
        assert not decision.allowed
        assert decision.code == ak.PROFILE_MISMATCH
        assert "dispatch_envelope" not in decision.extra
        assert "materialization" in decision.reason, decision.reason
        assert "runtime-materialization.json" in decision.reason, decision.reason
        account = decision.extra["runtime_selection"]
        assert account["state"] == "envelope_failed", account

    def test_a_refused_managed_dispatch_does_not_move_the_story(self, tmp_path):
        project = _managed_project(tmp_path, block=None)
        assert not _dispatch(project).allowed
        # THROUGH THE RESOLVER. Opening `pipeline-state.json` and expecting
        # `current_stories` is the exact defect `core/lib/pipeline_board.py`
        # exists to prevent: under `build_mode: spq` that file is a mode plus
        # identity POINTER and the board lives under the Cycle, so a direct
        # reader sees nothing and reports it as an empty board. Found four
        # separate times in the product (#505, #509, #514); a test can acquire
        # it too, and here it did.
        story = _board_story(project)
        assert story["state"] == "queued", story
        assert not story.get("mcp_active_dispatches"), story

    def test_an_inline_credential_refuses_the_dispatch(self, tmp_path):
        block = json.loads(json.dumps(MANAGED_BLOCK))
        block["vcs"]["auth_ref"] = "ghp_thisisnotareference"
        project = _managed_project(tmp_path, block=block)
        decision = _dispatch(project)
        assert not decision.allowed
        assert "cred://" in decision.reason, decision.reason
        assert "ghp_" not in decision.reason, (
            "a refusal must not echo the material it refused"
        )

    def test_an_ssh_remote_refuses_before_an_attempt_is_minted(self, tmp_path):
        block = json.loads(json.dumps(MANAGED_BLOCK))
        block["vcs"]["repo_url"] = "git@github.com:h3tech-ai/taskflow.git"
        project = _managed_project(tmp_path, block=block)
        decision = _dispatch(project)
        assert not decision.allowed
        assert "repo_url" in decision.reason, decision.reason
