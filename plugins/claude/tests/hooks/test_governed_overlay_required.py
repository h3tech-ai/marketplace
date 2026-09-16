"""Layer 2 — the overlay pair is REQUIRED on a governed dispatch's receipt (#473).

The tightening #402 deferred and #447 unlocked. #342 stamped
`stage_profile` / `capability_profile` onto receipts with nothing enforcing
them; #402 refused a PRESENT pair that contradicts the transition edge but left
absence a warning, because at that point no dispatch handed an agent an
envelope to copy the pair from. Since #447/#464/#466 a governed dispatch builds
and binds one, and since #471 the bridge verifies control-plane-signed bytes
rather than the local proposal, so the agent now HAS an authoritative pair.

The three properties these tests pin, in the order they matter:

1. **Omission costs a refusal on a governed dispatch.** Requiring presence does
   not make the pair trustworthy — the agent still types it. What makes it
   load-bearing is that #402 compares a present pair against the edge, and
   before this ticket ABSENCE was the cheapest way to avoid being compared.
   `test_omitting_the_pair_costs_the_same_as_contradicting_it` is the whole
   ticket in one assertion: both roads now end in a refusal, so declaring the
   pair honestly is the cheapest remaining option.

2. **The governed condition is read from the DISPATCHER, not re-derived.** The
   signal is the runtime selection `execute_dispatch` persisted on the stage's
   dispatch binding, which is the same field `evaluate_advance` already uses to
   decide which executor was authorized. The two drift tests
   (`test_disabling_runtimes_after_the_dispatch_does_not_release_the_receipt`
   and `test_enabling_runtimes_after_an_ungoverned_dispatch_does_not_refuse_it`)
   are the discriminating pair: a validator that re-read `.synaptory.yaml`,
   the probe snapshot or the profile registry at validation time passes neither.

3. **An unconfigured project is completely unaffected.** A project with no
   `runtimes:` section returns inert before any envelope work, and so does a
   governed project nobody has probed. Neither hands over an envelope, so
   neither is held to a pair it was never given.

Layer 2 rather than Layer 1 because the governed fixture is a real git checkout
driven through `execute_dispatch`: the binding these tests read is the one the
dispatcher wrote, not one a test hand-seeded into the shape it wanted.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import advance_kernel as ak
import receipt_validator as rv
import runtime_selector as rsel
import _spq_fixture
import story_pipeline as sp


STAGE_ENTERED = "2026-01-01T00:00:00+00:00"

RUNTIMES_SECTION = """runtimes:
  version: 1
  enabled: true
  allowed_profiles:
    - claude-local-v1
"""


def _fresh_completed_at() -> str:
    """A completion stamped AFTER the stage was entered.

    `execute_dispatch` moves the story at real time, so a fixture constant is
    in the past and the receipt is refused as `stale_receipt`, which would mask
    whatever the test is about.
    """
    return (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()



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


def _spq_project(tmp_path: Path, *, runtimes: bool, probed: bool = True) -> Path:
    """A real SPQ checkout, governed or not.

    THE BOARD COMES FROM `open_cycle`, and that is the fix rather than a
    refactor. This hand-wrote the pre-#303 state into `pipeline-state.json`
    -- `CYCLE_EXECUTION`, a `sync` block, a lane pin -- plus a manifest
    carrying `"manifest_hash": "sha256:" + "a" * 64`, a digest of nothing.
    Against the #644 runtime that resolves no Cycle: `next_action` answers for
    DISCOVERY and every dispatch below refused with `next_action_mismatch`,
    which is a fixture verdict wearing a gate's name (#514).

    Everything the governed path additionally needs is still here, because an
    incomplete fixture is how a test comes to assert against an envelope that
    was never built: the probe snapshot, and a git commit so `source_revision`
    -- a required envelope field -- resolves.
    """
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    (project / ".synaptory" / ".orchestrator" / "receipts").mkdir(parents=True)
    # NO `spq.workstreams` BLOCK: `SPD-194` retires the lane, and the sealed
    # declaration carries what the readiness checks used to read from it.
    config = "build_mode: spq\nproject_id: taskflow\n"
    if runtimes:
        config += RUNTIMES_SECTION
    (project / ".synaptory.yaml").write_text(config, encoding="utf-8")

    # BEFORE `open_cycle`, because the declaration records the trunk this
    # Cycle integrates into and `open_cycle` refuses a gitignored transport.
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.test"],
        ["git", "config", "user.name", "T"],
    ):
        subprocess.run(argv, cwd=str(project), check=True, capture_output=True)
    subprocess.run(
        ["git", "add", "-A"], cwd=str(project), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-qm", "seed"],
        cwd=str(project),
        check=True,
        capture_output=True,
    )

    _spq_fixture.open_project(
        project,
        units=[{"id": "WU-001", "title": "Seed unit"}],
        goal="require the overlay pair",
    )

    if probed:
        _write_snapshot(project, [("claude-local-v1", True, "")])

    # A SECOND COMMIT, so the Cycle's own artifacts are in the tree
    # `source_revision` names. `open_cycle` writes the committed transport
    # under `.synaptory/cycles/`, and a dispatch whose envelope points at a
    # revision predating it is describing a checkout that never held the seal.
    subprocess.run(
        ["git", "add", "-A"], cwd=str(project), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-qm", "cycle"],
        cwd=str(project),
        check=True,
        capture_output=True,
    )
    return project


def _cycle_id(project: Path) -> str:
    """The Cycle `open_cycle` allocated, asked for rather than hardcoded.

    `CYCLE_ID = "001-9f2c41ab"` used to be a constant this file both wrote and
    read, which is only consistent while the fixture writes the board itself.
    """
    import spq_paths

    return spq_paths.resolve_identity(str(project)).cycle_id


@pytest.fixture(autouse=True)
def _no_ambient_project_id(monkeypatch):
    monkeypatch.delenv("SYNAPTORY_PROJECT_ID", raising=False)


def _policy() -> ak.HostPolicy:
    return ak.policy_for_claude(enforcement="enforce")


def _dispatch(project: Path):
    return ak.execute_dispatch(
        str(project), "WU-001", role="software-engineer", policy=_policy()
    )


def _binding(project: Path, abbrev: str = "se") -> dict:
    """Read the binding through the shipped resolver, not a guessed path.

    SPQ state is workstream-scoped, so the file a test guesses is not
    necessarily the one the kernel wrote.
    """
    story = sp.get_story(sp._read_state(str(project)), "WU-001")
    return (story or {}).get("mcp_active_dispatches", {}).get(abbrev, {})


def _write_receipt(project: Path, contract: dict, binding: dict, **overrides) -> Path:
    """A receipt that is valid in every respect EXCEPT what a test overrides."""
    receipt_path = Path(contract["receipt_path"])
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    artifact = project / "src" / "x.py"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("# stub\n", encoding="utf-8")
    payload = {
        "story_id": "WU-001",
        "role": "software-engineer",
        "backend": "claude",
        "model": "e2e-fixture",
        "dispatch_id": binding.get("dispatch_id"),
        "attempt_id": binding.get("attempt_id"),
        "fencing_token": binding.get("fencing_token"),
        "artifacts": ["src/x.py"],
        "verification_commands": [
            {"command": "true", "exit_code": 0, "summary": "passed"}
        ],
        "metrics": {"files_changed": 1},
        "completed_at": _fresh_completed_at(),
    }
    payload.update(overrides)
    for key in [k for k, v in list(payload.items()) if v is _OMIT]:
        del payload[key]
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    return receipt_path


class _Omit:
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<omit>"


#: Sentinel: `_write_receipt(..., attempt_id=_OMIT)` drops the key entirely,
#: which is different from writing null and is the shape a forger would use.
_OMIT = _Omit()


def _advance(project: Path):
    return ak.evaluate_advance(str(project), "WU-001", "testing", policy=_policy())


def _errors(project: Path, receipt_path: Path) -> list:
    return rv.validate_receipt(str(receipt_path), str(project)).errors


# ── 1. a governed dispatch's receipt must carry the pair ─────────────────────


class TestGovernedDispatchRequiresThePair:
    def test_a_receipt_omitting_the_pair_is_refused(self, tmp_path):
        project = _spq_project(tmp_path, runtimes=True)
        decision = _dispatch(project)
        assert decision.allowed, decision.reason
        assert decision.extra["dispatch_envelope"], "fixture is not governed"
        _write_receipt(project, decision.extra["receipt_contract"], _binding(project))

        refused = _advance(project)
        assert not refused.allowed, "a governed receipt with no overlay advanced"
        assert refused.code == ak.RECEIPT_INVALID, refused.code

    def test_the_refusal_names_the_envelope_field_to_copy_from(self, tmp_path):
        """The message has to be actionable by the producer that hit it. Naming
        the FIELD rather than the value is deliberate: telling the agent what
        to write is telling it to type a value, and a typed value is the thing
        #402's comparison exists to catch."""
        project = _spq_project(tmp_path, runtimes=True)
        decision = _dispatch(project)
        path = _write_receipt(
            project, decision.extra["receipt_contract"], _binding(project)
        )
        errors = _errors(project, path)
        assert len(errors) == 1, errors
        message = errors[0]
        assert "dispatch_envelope.stage_profile" in message
        assert "dispatch_envelope.capability_profile" in message
        assert "VERBATIM" in message
        # The envelope's own values must NOT appear: a refusal that dictates
        # the answer turns a copy into a dictation.
        assert "producing" not in message
        assert "producer" not in message.replace("produced", "")

    def test_half_the_pair_is_refused_and_names_only_the_missing_half(self, tmp_path):
        project = _spq_project(tmp_path, runtimes=True)
        decision = _dispatch(project)
        envelope = decision.extra["dispatch_envelope"]
        path = _write_receipt(
            project,
            decision.extra["receipt_contract"],
            _binding(project),
            stage_profile=envelope["stage_profile"],
        )
        errors = _errors(project, path)
        assert len(errors) == 1, errors
        assert "'capability_profile'" in errors[0]
        assert "'stage_profile'" not in errors[0]
        assert not _advance(project).allowed

    def test_a_blank_string_is_absence_not_a_declaration(self, tmp_path):
        """`""` is what a template substitution that resolved to nothing
        leaves behind, and `advance_kernel._overlay_value` already reads it as
        absence. Both modules have to agree on what absence is, or a blank pair
        would pass here and warn there."""
        project = _spq_project(tmp_path, runtimes=True)
        decision = _dispatch(project)
        path = _write_receipt(
            project,
            decision.extra["receipt_contract"],
            _binding(project),
            stage_profile="   ",
            capability_profile="",
        )
        errors = _errors(project, path)
        assert len(errors) == 1, errors
        assert "'stage_profile'" in errors[0] and "'capability_profile'" in errors[0]

    def test_the_pair_copied_from_the_envelope_advances(self, tmp_path):
        """The positive half, so the suite cannot pass by refusing everything.
        The values come out of the envelope the dispatch produced, which is
        exactly what the agent is instructed to do."""
        project = _spq_project(tmp_path, runtimes=True)
        decision = _dispatch(project)
        envelope = decision.extra["dispatch_envelope"]
        path = _write_receipt(
            project,
            decision.extra["receipt_contract"],
            _binding(project),
            stage_profile=envelope["stage_profile"],
            capability_profile=envelope["capability_profile"],
        )
        assert _errors(project, path) == []
        allowed = _advance(project)
        assert allowed.allowed, "%s: %s" % (allowed.code, allowed.reason)


# ── 2. omission no longer dodges the #402 comparison ─────────────────────────


class TestOmissionIsNoLongerTheCheapRoute:
    def test_omitting_the_pair_costs_the_same_as_contradicting_it(self, tmp_path):
        """The point of the ticket. Before this change a producing stage had
        two options: declare `capability_profile: prover` and be refused by
        #402, or declare nothing and be waved through with a warning. The
        second was strictly cheaper, which is #494's inverted incentive in
        another field — silence cheaper than honesty. Both are refusals now."""
        project = _spq_project(tmp_path, runtimes=True)
        decision = _dispatch(project)
        contract = decision.extra["receipt_contract"]
        binding = _binding(project)

        _write_receipt(
            project, contract, binding,
            stage_profile="verifying", capability_profile="prover",
        )
        contradicted = _advance(project)
        assert not contradicted.allowed
        assert contradicted.code == ak.PROFILE_MISMATCH, contradicted.code

        _write_receipt(project, contract, binding)
        omitted = _advance(project)
        assert not omitted.allowed
        assert omitted.code == ak.RECEIPT_INVALID, omitted.code

    def test_a_contradicting_pair_is_still_refused_so_402_does_not_regress(
        self, tmp_path
    ):
        """A present pair still goes to `profile_overlay_problems`: the new
        requirement returns early when both values are readable, so it cannot
        shadow the comparison it exists to make unavoidable."""
        project = _spq_project(tmp_path, runtimes=True)
        decision = _dispatch(project)
        _write_receipt(
            project,
            decision.extra["receipt_contract"],
            _binding(project),
            stage_profile="verifying",
            capability_profile="prover",
        )
        refused = _advance(project)
        assert refused.code == ak.PROFILE_MISMATCH
        assert "capability_profile" in refused.reason

    def test_a_non_string_pair_stays_a_shape_refusal(self, tmp_path):
        """`{"capability_profile": {"name": "prover"}}` is an unreadable claim,
        not a missing one. The new check must not relabel it as "missing" and
        take the kernel's precise refusal away."""
        project = _spq_project(tmp_path, runtimes=True)
        decision = _dispatch(project)
        envelope = decision.extra["dispatch_envelope"]
        path = _write_receipt(
            project,
            decision.extra["receipt_contract"],
            _binding(project),
            stage_profile=envelope["stage_profile"],
            capability_profile={"name": "prover"},
        )
        assert _errors(project, path) == []
        refused = _advance(project)
        assert refused.code == ak.PROFILE_MISMATCH
        assert "must be a string" in refused.reason

    def test_dropping_the_attempt_identity_does_not_release_the_requirement(
        self, tmp_path
    ):
        """The second-order dodge, and the reason the governed signal may never
        come from the receipt. If "was this governed?" were answered by any
        field the agent writes — `attempt_id`, an embedded envelope, a
        `governed` flag — then deleting that field would delete the
        requirement. Here the receipt claims nothing about its dispatch and is
        still refused, because the answer comes from the binding."""
        project = _spq_project(tmp_path, runtimes=True)
        decision = _dispatch(project)
        path = _write_receipt(
            project,
            decision.extra["receipt_contract"],
            _binding(project),
            attempt_id=_OMIT,
            dispatch_id=_OMIT,
            fencing_token=_OMIT,
        )
        errors = _errors(project, path)
        assert len(errors) == 1, errors
        assert "governed dispatch" in errors[0]


# ── 3. the governed signal is the dispatcher's, and cannot drift ─────────────


class TestTheGovernedSignalComesFromTheDispatcher:
    def test_disabling_runtimes_after_the_dispatch_does_not_release_the_receipt(
        self, tmp_path
    ):
        """Discriminating. The dispatch was governed and handed over an
        envelope; the config then says otherwise. A validator that re-read
        `.synaptory.yaml` would accept this receipt, because `load_policy`
        now returns a disabled policy. The binding does not move, so the
        requirement does not."""
        project = _spq_project(tmp_path, runtimes=True)
        decision = _dispatch(project)
        assert decision.extra["dispatch_envelope"]
        _write_receipt(project, decision.extra["receipt_contract"], _binding(project))

        config = project / ".synaptory.yaml"
        config.write_text(
            config.read_text(encoding="utf-8").replace(RUNTIMES_SECTION, ""),
            encoding="utf-8",
        )
        assert not rsel.load_policy(str(project)).enabled, "fixture did not disable"

        refused = _advance(project)
        assert not refused.allowed, (
            "editing the config after the dispatch released a governed receipt"
        )
        assert refused.code == ak.RECEIPT_INVALID

    def test_deleting_the_probe_snapshot_after_the_dispatch_does_not_release_it(
        self, tmp_path
    ):
        """The same drift through the other input. `load_availability` returning
        None is what makes selection inert, so a validator deriving governance
        from the config plus the snapshot would accept this receipt too."""
        project = _spq_project(tmp_path, runtimes=True)
        decision = _dispatch(project)
        _write_receipt(project, decision.extra["receipt_contract"], _binding(project))
        project.joinpath(*rsel.AVAILABILITY_RELPATH).unlink()
        assert rsel.load_availability(str(project)) is None

        refused = _advance(project)
        assert not refused.allowed
        assert refused.code == ak.RECEIPT_INVALID

    def test_enabling_runtimes_after_an_ungoverned_dispatch_does_not_refuse_it(
        self, tmp_path
    ):
        """The other direction, which is the one that would refuse HONEST work.
        This dispatch was inert and handed over no envelope, so its agent had
        nothing to copy. Opting the project in afterwards must not retroactively
        require a pair nobody was given."""
        project = _spq_project(tmp_path, runtimes=False)
        decision = _dispatch(project)
        assert decision.extra["runtime_selection"]["state"] == "inert"
        assert "dispatch_envelope" not in decision.extra
        path = _write_receipt(
            project, decision.extra["receipt_contract"], _binding(project)
        )

        config = project / ".synaptory.yaml"
        config.write_text(
            config.read_text(encoding="utf-8") + RUNTIMES_SECTION, encoding="utf-8"
        )
        _write_snapshot(project, [("claude-local-v1", True, "")])
        assert rsel.load_policy(str(project)).enabled, "fixture did not enable"

        assert _errors(project, path) == []
        allowed = _advance(project)
        assert allowed.allowed, "%s: %s" % (allowed.code, allowed.reason)


# ── 4. an unconfigured project is completely unaffected ──────────────────────


class TestUngovernedProjectsAreUnaffected:
    def test_a_project_with_no_runtimes_section_advances_and_warns(self, tmp_path):
        project = _spq_project(tmp_path, runtimes=False)
        decision = _dispatch(project)
        assert "dispatch_envelope" not in decision.extra
        path = _write_receipt(
            project, decision.extra["receipt_contract"], _binding(project)
        )

        result = rv.validate_receipt(str(path), str(project))
        assert result.valid, result.errors
        assert any(
            "soft-required" in w and "stage_profile" in w for w in result.warnings
        ), result.warnings

        allowed = _advance(project)
        assert allowed.allowed, "%s: %s" % (allowed.code, allowed.reason)
        assert any("profile_overlay_absent" in w for w in allowed.warnings)

    def test_a_governed_project_nobody_probed_is_unaffected(self, tmp_path):
        """`runtimes.enabled` is TRUE here and selection is still inert, because
        an absent probe snapshot is inert rather than optimistic (#464). This is
        the case a config-only governance test gets wrong in the direction that
        refuses honest receipts."""
        project = _spq_project(tmp_path, runtimes=True, probed=False)
        decision = _dispatch(project)
        assert decision.allowed, decision.reason
        assert decision.extra["runtime_selection"]["state"] == "inert"
        path = _write_receipt(
            project, decision.extra["receipt_contract"], _binding(project)
        )
        assert _errors(project, path) == []
        assert _advance(project).allowed

    def test_accountable_role_stays_soft_required_on_a_governed_dispatch(
        self, tmp_path
    ):
        """The #399 decision, deliberately not bundled into this ticket. The
        overlay pair is handed over on the envelope; `accountable_role` is not
        a field of the envelope at all, so requiring it would be requiring the
        agent to derive something, which is the opposite of the copy rule."""
        project = _spq_project(tmp_path, runtimes=True)
        decision = _dispatch(project)
        envelope = decision.extra["dispatch_envelope"]
        assert "accountable_role" not in envelope
        _write_receipt(
            project,
            decision.extra["receipt_contract"],
            _binding(project),
            stage_profile=envelope["stage_profile"],
            capability_profile=envelope["capability_profile"],
        )
        allowed = _advance(project)
        assert allowed.allowed, "%s: %s" % (allowed.code, allowed.reason)
        assert any("accountable_role_absent" in w for w in allowed.warnings)


# ── 5. the writer-facing surface refuses before the gate does ────────────────


def test_the_subagentstop_validator_cli_refuses_a_governed_missing_pair(tmp_path):
    """The producer reads this refusal, not the advance gate's. The SubagentStop
    hook shells out to `receipt_validator.py <receipt> <project_dir>`, so the
    exit code and the printed error are the surface that actually reaches an
    agent that has just finished."""
    project = _spq_project(tmp_path, runtimes=True)
    decision = _dispatch(project)
    path = _write_receipt(
        project, decision.extra["receipt_contract"], _binding(project)
    )
    proc = subprocess.run(
        [sys.executable, rv.__file__, str(path), str(project)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1, proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["valid"] is False
    assert "dispatch_envelope.stage_profile" in " ".join(payload["errors"])
