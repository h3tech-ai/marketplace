"""Layer 1 -- admitting an artifact the platform did not produce (#495).

Epic #410, capability-profile-pilot.md section 5, S4. A verify-only job has no
in-platform producing stage: an outside party supplies the deliverable, the
platform checks it, a human signs off. Before this, `_story_candidate_digest`
derived the candidate two ways and both were deliberately internal, so the
strongest sign-off available on such a job was an UNBACKED one.

THE FORGERY THIS MODULE IS ORGANISED AROUND, and the honest limit of it.

`#403`'s rule is that a judged verdict must not choose its own subject, so the
digest is derived and never an argument. S4's requirement is that the candidate
is external, so its identity can only come from outside. The reconciliation is
WHEN and BY WHOM: the digest is fixed at intake, over bytes the platform read,
before any checking runs, and it is write-once through the admission API. It is
NOT immutable against the project principal, who can rewrite the record and the
artifact together: several tests below do exactly that, and the conformance
contract declares it as the reason intake is still a gap.

What that refuses, each with a test below:

  * a digest supplied at the moment of the verdict -- there is no parameter for
    one, so this is refused by construction rather than by validation;
  * a hand-written intake record CLAIMING a digest its artifact does not hash
    to (the digest is re-derived from the bytes on every read);
  * an artifact substituted between intake and sign-off;
  * a second admission changing the candidate (`O_EXCL`, so it fails at the
    syscall);
  * a sign-off binding to an artifact that was never admitted;
  * an intake record used to redirect an IN-PLATFORM unit's sign-off onto an
    unrelated file (refused at admit, and again at read if a producing receipt
    appears later).

What it does NOT refuse, stated here because the epic's fifth correction (#493)
was exactly a shape check read as an authorization check: **the submitter can
still choose which bytes their own sign-off will bind to.** `admitted_by` is a
caller-supplied string on a local verb, exactly as `accepted_by` is on
`accept_story`, and the record is a file in the project the same principal can
write. So "a different actor fixed the digest" is RECORDED and not enforced.
That is the same footing as `mcp_consumed_receipts`, the ledger this derivation
takes precedence over -- also a list in a JSON file the same principal can
edit -- so intake is neither weaker nor stronger than what it displaces.
Enforcing the separation needs a secret the submitter does not hold (#435
option 2) or a server-side fact they cannot mint (#435 option 3); both are
open, and #486 owns the acceptance edge's discipline.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

import external_intake as ei
external_intake_module = ei
import story_pipeline as sp


pytestmark = pytest.mark.unit


# ── fixtures ────────────────────────────────────────────────────────────────


def _receipts_dir(project: Path) -> Path:
    return project / ".synaptory" / ".orchestrator" / "receipts"


def _project(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".synaptory.yaml").write_text("project_id: t\n", encoding="utf-8")
    _receipts_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _artifact(project: Path, name: str, body: bytes) -> Path:
    path = project / "inbox" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _digest(body: bytes) -> str:
    return "sha256:%s" % hashlib.sha256(body).hexdigest()


def _receipt(project: Path, unit: str, abbrev: str, role: str, **fields) -> Path:
    payload = {"story_id": unit, "role": role, "agent": role}
    payload.update(fields)
    path = _receipts_dir(project) / ("%s-%s.json" % (unit, abbrev))
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


#: The attempt id these tests treat as kernel-authorized. #495 P2 requires a
#: verifying receipt to name an attempt the kernel issued, so a story reaching
#: `record_judged_verdict` has to carry the ledger the kernel would have
#: written. SEEDED THROUGH THE KERNEL'S OWN KEY AND READER
#: (`advance_kernel.AUTHORIZED_ATTEMPTS_KEY`, `attempt_history`) rather than a
#: literal, so a rename moves these tests with it instead of leaving them
#: asserting against a key nothing writes.
#:
#: That the kernel really issues one for a verify-only unit is proved where it
#: belongs, end to end against each host:
#: `conformance/test_capability_profile_contract.py::test_verify_only_job_binds_an_external_candidate`.
#: These tests are about what the verdict BINDS to.
_AUTHORIZED_ATTEMPT = "attempt-0123456789abcdef"


def _awaiting(unit: str = "US-901", *, authorized: bool = True) -> dict:
    story = {
        "id": unit,
        "title": "t",
        "state": "awaiting_acceptance",
        "pipeline_log": [
            {"state": "awaiting_acceptance", "entered_at": "t0",
             "exited_at": None}
        ],
        "receipts": [],
        "retries": {},
    }
    if authorized:
        import advance_kernel as _ak

        story[_ak.AUTHORIZED_ATTEMPTS_KEY] = {
            abbrev: [{"attempt_id": _AUTHORIZED_ATTEMPT}]
            for abbrev in ("qe", "cr", "ce")
        }
    return {"current_stories": [story]}


def _admit(
    project: Path, unit: str, body: bytes, filename: str = "", **kwargs
) -> dict:
    artifact = _artifact(project, filename or ("%s-brief.md" % unit), body)
    kwargs.setdefault("admitted_by", "intake@h3t.co")
    return ei.admit_external_candidate(str(project), unit, str(artifact), **kwargs)


def _verifier(
    project: Path, unit: str, body: bytes, abbrev: str = "qe",
    role: str = "quality-engineer", **over,
) -> Path:
    """A VALID verifying receipt bound to the admitted bytes.

    #396: an admitted candidate that nobody checked is not a verified
    candidate, and a file named `US-901-qe.json` holding two keys is not a
    check either. So this writes a receipt that satisfies the receipt contract
    as well as naming the digest, which is what the intake path requires.
    Tests about something else say so by calling this rather than by silently
    relying on a stub.
    """
    # The validator checks artifacts exist on disk, which is part of what makes
    # a receipt a record of work rather than a claim about it.
    evidence = project / "tests" / "test_candidate.py"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("def test_candidate():\n    assert True\n", encoding="utf-8")
    fields = {
        "story_id": unit,
        "backend": "claude",
        "model": "sonnet",
        "artifacts": ["tests/test_candidate.py"],
        "completed_at": "2026-01-01T04:00:00Z",
        "metrics": {"tests_run": 3},
        "verification_commands": [
            {"command": "pytest tests/ -q", "exit_code": 0, "summary": "3 passed"},
        ],
        "candidate_digest": _digest(body),
    }
    # NAMES THE AUTHORIZED ATTEMPT BY DEFAULT (#495 P2). A verifying receipt
    # that names no attempt the kernel issued is a document with the right
    # shape, and the sign-off must not credit it -- so a test that wants that
    # case says so by passing `attempt_id=None`, and every other test gets the
    # legitimate flow rather than silently relying on the weaker one.
    fields["attempt_id"] = _AUTHORIZED_ATTEMPT
    fields.update(over)
    if fields.get("attempt_id") is None:
        fields.pop("attempt_id")
    return _receipt(project, unit, abbrev, role, **fields)


def _sign_off(
    project: Path, unit: str = "US-901", by: str = "alice@h3t.co",
    *, authorized: bool = True,
) -> dict:
    """The judged VERDICT a sign-off would record, without completing the unit.

    These tests are about what the verdict binds to, not about whether the Work
    Unit may advance. They used to reach the verdict through `accept_story`,
    which was a convenience: since #592 that function refuses an acceptance
    whose verdict cannot be credited, and several tests here exist precisely to
    build an uncreditable one. Calling `record_judged_verdict` directly asks
    the question each test actually asks, and stops a fail-closed transition
    check from reading as a broken fixture.
    """
    state = _awaiting(unit, authorized=authorized)
    story = sp._find_story(state, unit)
    return sp.record_judged_verdict(
        story, unit, verdict="accepted", principal=by, project_dir=str(project)
    )


BODY = b"# Creative Brief\n\nSupplied by the client.\n"


# ── the digest is computed, never accepted ──────────────────────────────────


class TestTheDigestIsComputedHere:
    def test_admit_digests_the_bytes_it_read(self, tmp_path):
        project = _project(tmp_path)
        record = _admit(project, "US-901", BODY)
        assert record["candidate_digest"] == _digest(BODY)
        assert record["artifact_bytes"] == len(BODY)

    def test_there_is_no_parameter_for_a_claimed_digest(self, tmp_path):
        """Refused by construction. The whole point of an intake is that it
        does not move #403's forgery one step earlier."""
        project = _project(tmp_path)
        artifact = _artifact(project, "b.md", BODY)
        with pytest.raises(TypeError):
            ei.admit_external_candidate(  # type: ignore[call-arg]
                str(project), "US-901", str(artifact),
                admitted_by="intake@h3t.co",
                candidate_digest=_digest(b"something else"),
            )

    def test_a_record_claiming_a_digest_the_bytes_deny_binds_nothing(self, tmp_path):
        """THE forgery for a hand-written record: the intake file is writable
        by the same principal, so its digest field is a claim. It is re-derived
        from the artifact on every read, so a false claim binds nothing."""
        project = _project(tmp_path)
        _admit(project, "US-901", BODY)
        path = Path(ei.record_path(str(project), "US-901"))
        record = json.loads(path.read_text(encoding="utf-8"))
        record["candidate_digest"] = _digest(b"the artifact I wish I had sent")
        os.chmod(str(path), 0o644)
        path.write_text(json.dumps(record), encoding="utf-8")

        read = ei.read_intake(str(project), "US-901")
        assert read.admitted is True
        assert read.binds is False
        assert read.candidate_digest is None
        assert any("changed after intake" in p for p in read.problems), read.problems

    def test_the_record_is_write_once(self, tmp_path):
        project = _project(tmp_path)
        _admit(project, "US-901", BODY)
        with pytest.raises(ei.IntakeRefused) as caught:
            _admit(
                project, "US-901", b"a different brief\n", filename="second.md",
            )
        assert caught.value.code == "already_admitted"
        # And the first record is untouched. The verifier is here because the
        # intake path requires one (#396); this test is about the record.
        _verifier(project, "US-901", BODY)
        read = ei.read_intake(str(project), "US-901")
        assert read.candidate_digest == _digest(BODY)

    def test_the_record_is_not_group_or_world_writable(self, tmp_path):
        """Stops an accident, not a forger. Named as such in the module."""
        project = _project(tmp_path)
        _admit(project, "US-901", BODY)
        mode = os.stat(ei.record_path(str(project), "US-901")).st_mode
        assert not mode & stat.S_IWOTH
        assert not mode & stat.S_IWGRP
        assert not mode & stat.S_IWUSR


# ── the refusals at admit ───────────────────────────────────────────────────


class TestAdmitRefusals:
    def test_a_missing_artifact_is_refused(self, tmp_path):
        project = _project(tmp_path)
        with pytest.raises(ei.IntakeRefused) as caught:
            ei.admit_external_candidate(
                str(project), "US-901", str(project / "nope.md"),
                admitted_by="intake@h3t.co",
            )
        assert caught.value.code == "artifact_unreadable"

    def test_an_artifact_outside_the_project_is_refused(self, tmp_path):
        project = _project(tmp_path / "proj")
        outside = tmp_path / "outside.md"
        outside.write_bytes(BODY)
        with pytest.raises(ei.IntakeRefused) as caught:
            ei.admit_external_candidate(
                str(project), "US-901", str(outside),
                admitted_by="intake@h3t.co",
            )
        assert caught.value.code == "artifact_outside_project"

    def test_a_traversing_unit_id_is_refused(self, tmp_path):
        project = _project(tmp_path)
        artifact = _artifact(project, "b.md", BODY)
        with pytest.raises(ei.IntakeRefused) as caught:
            ei.admit_external_candidate(
                str(project), "../../etc/passwd", str(artifact),
                admitted_by="intake@h3t.co",
            )
        assert caught.value.code == "unit_id_rejected"

    def test_an_unnamed_principal_is_refused(self, tmp_path):
        """The record's whole purpose is to say who fixed the candidate."""
        project = _project(tmp_path)
        artifact = _artifact(project, "b.md", BODY)
        with pytest.raises(ei.IntakeRefused) as caught:
            ei.admit_external_candidate(
                str(project), "US-901", str(artifact), admitted_by="  ",
            )
        assert caught.value.code == "admitted_by_required"

    def test_a_unit_that_already_has_receipts_is_refused(self, tmp_path):
        """Intake precedes checking. On an in-platform unit this is also what
        stops an intake record redirecting the sign-off off produced work."""
        project = _project(tmp_path)
        _receipt(project, "US-901", "se", "software-engineer")
        with pytest.raises(ei.IntakeRefused) as caught:
            _admit(project, "US-901", BODY)
        assert caught.value.code == "unit_already_receipted"


# ── the two questions, never one (#528's shape) ─────────────────────────────


class TestReadAnswersTwoQuestions:
    def test_no_record_reads_as_no_admission_without_problems(self, tmp_path):
        project = _project(tmp_path)
        read = ei.read_intake(str(project), "US-901")
        assert read.admitted is False
        assert read.problems == []
        assert read.why_absent and "no external candidate was admitted" in read.why_absent

    def test_an_unreadable_record_is_never_reported_as_no_admission(self, tmp_path):
        """The #528 rule. If this read as "no intake" the derivation would fall
        through to the receipt bytes and a substituted candidate would produce
        a verdict that looks backed."""
        project = _project(tmp_path)
        _admit(project, "US-901", BODY)
        path = Path(ei.record_path(str(project), "US-901"))
        os.chmod(str(path), 0o644)
        path.write_text("{ not json", encoding="utf-8")
        read = ei.read_intake(str(project), "US-901")
        assert read.admitted is True
        assert read.binds is False
        assert read.problems

    def test_a_record_for_another_unit_binds_nothing(self, tmp_path):
        """A record moved to a different unit's path would otherwise bind the
        wrong verdict to the wrong artifact."""
        project = _project(tmp_path)
        _admit(project, "US-901", BODY)
        path = Path(ei.record_path(str(project), "US-901"))
        record = json.loads(path.read_text(encoding="utf-8"))
        record["unit_id"] = "US-902"
        os.chmod(str(path), 0o644)
        path.write_text(json.dumps(record), encoding="utf-8")
        read = ei.read_intake(str(project), "US-901")
        assert read.admitted is True
        assert read.binds is False
        assert any("names unit" in p for p in read.problems), read.problems

    def test_a_record_pointing_outside_the_project_binds_nothing(self, tmp_path):
        """The record is writable by the same principal, so its path is
        re-contained on the way out as well as on the way in."""
        project = _project(tmp_path / "proj")
        outside = tmp_path / "outside.md"
        outside.write_bytes(BODY)
        _admit(project, "US-901", BODY)
        path = Path(ei.record_path(str(project), "US-901"))
        record = json.loads(path.read_text(encoding="utf-8"))
        record["artifact_path"] = "../outside.md"
        record["candidate_digest"] = _digest(BODY)
        os.chmod(str(path), 0o644)
        path.write_text(json.dumps(record), encoding="utf-8")
        read = ei.read_intake(str(project), "US-901")
        assert read.binds is False
        assert any("outside the project" in p for p in read.problems), read.problems

    def test_a_clean_admission_binds(self, tmp_path):
        project = _project(tmp_path)
        _admit(project, "US-901", BODY)
        _verifier(project, "US-901", BODY)
        read = ei.read_intake(str(project), "US-901")
        assert read.binds is True
        assert read.candidate_digest == _digest(BODY)
        assert read.why_absent is None


# ── the binding on the sign-off ─────────────────────────────────────────────


class TestSignOffBindsToTheAdmittedCandidate:
    def test_a_verify_only_sign_off_is_backed_and_bound(self, tmp_path):
        """The capability itself. Before #495 this recorded
        `evidence_class: None` with `candidate_digest: None`."""
        project = _project(tmp_path)
        _admit(project, "US-901", BODY)
        _verifier(project, "US-901", BODY)
        item = _sign_off(project)
        assert item["candidate_digest"] == _digest(BODY)
        assert item["evidence_class"] == "judged"
        assert item["candidate_source"] == sp.CANDIDATE_SOURCE_INTAKE
        assert item.get("unbacked_reason") is None

    def test_the_verdict_names_the_intake_principal_and_the_artifact(self, tmp_path):
        project = _project(tmp_path)
        _admit(project, "US-901", BODY, admitted_by="carol@h3t.co")
        _verifier(project, "US-901", BODY)
        item = _sign_off(project, by="alice@h3t.co")
        assert item["intake_principal"] == "carol@h3t.co"
        assert item["candidate_artifact"].endswith("US-901-brief.md")

    def test_an_external_producer_is_never_credited_as_independent(self, tmp_path):
        """#495's open question, decided. The in-platform producer set for a
        verify-only unit holds at most the CHECKER, so computing independence
        against it would claim independence from a producer nobody recorded."""
        project = _project(tmp_path)
        _admit(project, "US-901", BODY, admitted_by="carol@h3t.co")
        _verifier(project, "US-901", BODY, principal="dave@h3t.co")
        item = _sign_off(project, by="alice@h3t.co")
        assert item["independent_of_producer"] is False
        assert "outside this platform" in item["independence_basis"]

    def test_the_judge_admitting_their_own_candidate_is_recorded(self, tmp_path):
        """The residual, made visible rather than refused: nothing on this host
        authenticates either principal, so the same-hand case is recorded."""
        project = _project(tmp_path)
        _admit(project, "US-901", BODY, admitted_by="alice@h3t.co")
        _verifier(project, "US-901", BODY)
        item = _sign_off(project, by="alice@h3t.co")
        assert "also the judge" in item["independence_basis"]

    def test_a_substituted_artifact_leaves_the_sign_off_unbacked(self, tmp_path):
        """Negative test for AC 3. The verdict must not silently re-bind to the
        receipt bytes, which would read as backed."""
        project = _project(tmp_path)
        _admit(project, "US-901", BODY)
        _receipt(project, "US-901", "qe", "quality-engineer")
        (project / "inbox" / "US-901-brief.md").write_bytes(b"swapped\n")
        item = _sign_off(project)
        assert item["evidence_class"] is None
        assert item["candidate_digest"] is None
        assert "changed after intake" in item["unbacked_reason"]

    def test_a_never_admitted_artifact_is_not_a_candidate(self, tmp_path):
        """Negative test for AC 2's converse: dropping bytes in the project is
        not an admission, and the sign-off binds to nothing."""
        project = _project(tmp_path)
        _artifact(project, "US-901-brief.md", BODY)
        item = _sign_off(project)
        assert item["evidence_class"] is None
        assert item["candidate_digest"] is None
        assert "no receipts exist for this story" in item["unbacked_reason"]

    def test_a_unit_with_both_candidates_binds_to_neither(self, tmp_path):
        """A producing receipt appearing after intake means two candidates and
        no way to say which the verdict is about. Refusing beats guessing."""
        project = _project(tmp_path)
        _admit(project, "US-901", BODY)
        _receipt(project, "US-901", "se", "software-engineer")
        item = _sign_off(project)
        assert item["evidence_class"] is None
        assert "producing-stage receipt" in item["unbacked_reason"]

    def test_an_admitted_candidate_nobody_checked_does_not_bind(self, tmp_path):
        """#396: the second half of #495's criterion, which was missing.

        "The verifying stage runs AGAINST the admitted candidate" has two
        halves, and only the second was enforced: a receipt naming different
        bytes was refused while NO receipt at all was accepted. With nothing on
        record checking the artifact, a judged sign-off reports a verification
        that did not happen, and the conformance scenario for this capability
        was doing exactly that: admit, then sign off, with no verifying
        dispatch anywhere.

        Scoped to the intake path. This does not make `candidate_digest`
        required on receipts generally, which would be a receipt-contract
        change; it makes it required of the receipt a verify-only unit is
        signed off on, where the digest is the whole point.
        """
        project = _project(tmp_path)
        _admit(project, "US-901", BODY)
        item = _sign_off(project)
        assert item["evidence_class"] is None, item
        assert "no receipt binds a verifying stage" in item["unbacked_reason"], item

    def test_a_receipt_naming_no_subject_does_not_bind_either(self, tmp_path):
        """A receipt that exists but names nothing is not a check of these
        bytes. It was accepted before #396, which is the same hole seen from
        the side where a stage did run: it ran against something unnamed."""
        project = _project(tmp_path)
        _admit(project, "US-901", BODY)
        _receipt(project, "US-901", "qe", "quality-engineer")
        item = _sign_off(project)
        assert item["evidence_class"] is None, item
        assert "no receipt binds a verifying stage" in item["unbacked_reason"], item

    @pytest.mark.parametrize(
        "abbrev,role",
        [("tw", "technical-writer"), ("pe", "platform-engineer"),
         ("po", "project-owner"), ("sa", "solution-architect"),
         ("ra", "research-advisor")],
    )
    def test_a_non_verifying_role_does_not_verify(self, tmp_path, abbrev, role):
        """#396: the roster decides which roles verify, not this module.

        `PRODUCING_ABBREVS` was the literal `("se",)`, so every other abbrev
        could be the verifying stage. `runtime_contracts.DISPATCH_STAGE_PROFILE`
        already said otherwise for two of them: `tw` and `pe` are PRODUCING.
        A technical-writer receipt quoting the candidate's digest was therefore
        counted as the check, and an unchecked candidate reached `judged`
        through a role that verifies nothing.

        The planning and analysing roles are here too. They were never
        producing, so they never tripped the old guard either, and "not
        producing" is not the same claim as "verifies".
        """
        project = _project(tmp_path)
        _admit(project, "US-901", BODY)
        _verifier(project, "US-901", BODY, abbrev=abbrev, role=role)
        item = _sign_off(project)
        assert item["evidence_class"] is None, item
        assert item["unbacked_reason"], item

    @pytest.mark.parametrize(
        "abbrev,role",
        [("qe", "quality-engineer"), ("cr", "code-reviewer"),
         ("ce", "compliance-engineer")],
    )
    def test_every_verifying_role_can_be_the_verifier(self, tmp_path, abbrev, role):
        """The positive arm for each eligible role, so tightening the negative
        side did not leave one verifier arbitrarily unable to verify."""
        project = _project(tmp_path)
        _admit(project, "US-901", BODY)
        _verifier(project, "US-901", BODY, abbrev=abbrev, role=role)
        item = _sign_off(project)
        assert item["evidence_class"] == "judged", item
        assert item["candidate_digest"] == _digest(BODY), item

    def test_a_receipt_whose_two_role_spellings_disagree_does_not_verify(
        self, tmp_path
    ):
        """A receipt names its role twice, in the filename and in the body.

        Neither authenticates the other, since one hand writes both, but a
        DISAGREEMENT is evidence that something is wrong, and reading whichever
        spelling is convenient is how a `-tw` file claiming to be a QE would
        pass. Unreadable role, no verification.
        """
        project = _project(tmp_path)
        _admit(project, "US-901", BODY)
        _verifier(
            project, "US-901", BODY, abbrev="tw", role="quality-engineer",
        )
        item = _sign_off(project)
        assert item["evidence_class"] is None, item

    def test_a_filename_only_stub_is_not_a_verifying_receipt(self, tmp_path):
        """#396. The check asked for a `-qe.json` name and a matching digest,
        so two keys in a file were proof that a stage had run.

        Nothing else about it was a receipt: no role, no story id, no
        completion time, no artifacts, no verification commands. A verifying
        receipt has to be a RECEIPT, and `receipt_validator` already states
        what that means, so the question is asked there rather than restated.
        """
        project = _project(tmp_path)
        _admit(project, "US-901", BODY)
        path = _receipts_dir(project) / "US-901-qe.json"
        path.write_text(
            json.dumps({"candidate_digest": _digest(BODY)}), encoding="utf-8"
        )
        item = _sign_off(project)
        assert item["evidence_class"] is None, item
        assert "declares no verifying role" in item["unbacked_reason"], item

    def test_a_structurally_invalid_verifier_receipt_does_not_bind(self, tmp_path):
        """It declares the right role and names the right bytes, and it still
        records no work: the artifacts it claims do not exist, so the receipt
        contract refuses it and so does this."""
        project = _project(tmp_path)
        _admit(project, "US-901", BODY)
        _verifier(
            project, "US-901", BODY, artifacts=["tests/never_written.py"],
        )
        item = _sign_off(project)
        assert item["evidence_class"] is None, item
        assert "not a valid receipt" in item["unbacked_reason"], item

    def test_a_valid_verifier_receipt_is_not_proof_of_an_authorized_dispatch(
        self, tmp_path
    ):
        """INVERTED BY #495 P2. It used to assert the residual; now it closes it.

        The receipt below is valid, declares a verifying role, names the
        admitted digest, and was written by hand a moment ago. That is the
        whole point: matching by SHAPE cannot tell it from one a dispatched
        verifier wrote, so until P2 it was credited `judged` and this test
        asserted exactly that, as the declared limit of
        `cp_external_candidate_binding`.

        I argued on #553 that a valid receipt is the standard every DoD check
        is held to, since `tests_pass` reads a receipt too, and review answered
        it correctly: epic #339 BUILT the identity that settles it
        (`attempt_id`, the durable fenced form of `dispatch_id`), so "no check
        carries dispatch identity" described a vocabulary this epic added and
        this path did not use. It uses it now.

        THE NAME IS KEPT DELIBERATELY, and it is still true: a valid verifier
        receipt is still not proof of an authorized dispatch. What changed is
        that the platform now says so instead of crediting it.
        """
        project = _project(tmp_path)
        _admit(project, "US-901", BODY)
        # `attempt_id=None` is the hand-written case, stated rather than
        # inherited: the helper names the authorized attempt by default, so a
        # test that let it do so would prove nothing about this one.
        _verifier(project, "US-901", BODY, attempt_id=None)
        item = _sign_off(project)
        assert item["evidence_class"] is None, (
            "a hand-written receipt with no authorized dispatch behind it was "
            "credited as a verification: %r" % (item,)
        )
        assert "no attempt or dispatch this kernel authorized" in (
            item.get("unbacked_reason") or ""
        ), item

    def test_a_receipt_naming_someone_elses_attempt_does_not_bind(self, tmp_path):
        """Naming AN attempt is not naming one the kernel issued for this unit.

        The gap this closes had two shapes and only the empty one is obvious.
        A receipt carrying a plausible but unissued `attempt_id` is the forgery
        that a presence check would wave through, which is #592's finding one
        level along: absence of authority read as authorization.
        """
        project = _project(tmp_path)
        _admit(project, "US-901", BODY)
        _verifier(project, "US-901", BODY, attempt_id="attempt-not-issued-here")
        item = _sign_off(project)
        assert item["evidence_class"] is None, item
        assert "no attempt or dispatch this kernel authorized" in (
            item.get("unbacked_reason") or ""
        ), item

    def test_a_story_with_no_authorized_attempts_at_all_does_not_bind(
        self, tmp_path
    ):
        """The kernel authorized nothing, and the receipt claims nothing.

        Distinct from the two above: there the story HAD a ledger and the
        receipt missed it. Here there is no ledger at all, which is what an
        ordinary story looks like before any dispatch. It must not read as
        "nothing to check against, so pass" -- the same collapse #592 found
        between two different absences.
        """
        project = _project(tmp_path)
        _admit(project, "US-901", BODY)
        _verifier(project, "US-901", BODY, attempt_id=None)
        item = _sign_off(project, authorized=False)
        assert item["evidence_class"] is None, item

    def test_a_receipt_naming_a_different_subject_blocks_the_binding(self, tmp_path):
        """The verifying stage must have checked what was admitted."""
        project = _project(tmp_path)
        _admit(project, "US-901", BODY)
        _verifier(
            project, "US-901", BODY,
            candidate_digest=_digest(b"some other bytes"),
        )
        item = _sign_off(project)
        assert item["evidence_class"] is None
        assert "not the admitted candidate" in item["unbacked_reason"]

    def test_a_receipt_naming_the_admitted_subject_still_binds(self, tmp_path):
        project = _project(tmp_path)
        _admit(project, "US-901", BODY)
        _verifier(project, "US-901", BODY)
        item = _sign_off(project)
        assert item["candidate_digest"] == _digest(BODY)
        assert item["evidence_class"] == "judged"


# ── the in-platform path is untouched ───────────────────────────────────────


class TestOrdinaryJobsAreUnaffected:
    def test_receipt_bytes_still_carry_an_in_platform_unit(self, tmp_path):
        project = _project(tmp_path)
        _receipt(project, "US-901", "se", "software-engineer")
        _receipt(project, "US-901", "qe", "quality-engineer")
        expected = hashlib.sha256()
        for path in sorted(_receipts_dir(project).glob("US-901-*.json")):
            expected.update(path.name.encode("utf-8"))
            expected.update(path.read_bytes())
        item = _sign_off(project)
        assert item["candidate_digest"] == "sha256:" + expected.hexdigest()
        assert item["candidate_source"] == sp.CANDIDATE_SOURCE_RECEIPT_BYTES
        assert item["evidence_class"] == "judged"

    def test_the_kernel_ledger_still_wins_over_the_receipt_bytes(self, tmp_path):
        project = _project(tmp_path)
        _receipt(project, "US-901", "se", "software-engineer")
        digest, why, source = sp._story_candidate_digest(
            {"mcp_consumed_receipts": ["b" * 64]}, "US-901", str(project)
        )
        assert digest == "sha256:" + "b" * 64
        assert why is None
        assert source == sp.CANDIDATE_SOURCE_LEDGER

    def test_a_barren_unit_is_unbacked_exactly_as_before(self, tmp_path):
        project = _project(tmp_path)
        item = _sign_off(project)
        assert item["evidence_class"] is None
        assert item["candidate_digest"] is None
        assert item["independent_of_producer"] is False
        assert item.get("candidate_source") is None
        assert item["unbacked_reason"]


# ── the intake directory tracks the receipts resolver ───────────────────────


class TestIntakeIsScopedLikeReceipts:
    def test_intake_follows_the_receipts_directory_into_a_spec_slot(
        self, tmp_path, monkeypatch
    ):
        """Not a hardcoded path. `receipts_dir_for` exists because a unit id is
        not unique across cycles, so an intake record keyed only by unit id
        under a project-level directory would collide exactly where receipts do
        not. The derivation is a leaf swap on the one authoritative resolver,
        which is what makes the SPQ and multi-spec layouts work without this
        module knowing either of them."""
        project = _project(tmp_path)
        spec = project / ".synaptory" / ".orchestrator" / "specs" / "SPEC-1"
        (spec / "receipts").mkdir(parents=True, exist_ok=True)
        (project / ".synaptory" / ".orchestrator" / "pipeline-state.json").write_text(
            json.dumps({"build_mode": "scrum", "specs": {"SPEC-1": {}}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("SYNAPTORY_ACTIVE_SPEC", "SPEC-1")
        assert Path(ei.intake_dir(str(project))) == spec / "intake"
        assert Path(sp.receipts_dir_for(str(project), intended=True)).parent == (
            Path(ei.intake_dir(str(project))).parent
        )


# ── the module CLI, the only reachable entrypoint today ─────────────────────


class TestModuleCli:
    def test_admit_then_read_round_trips(self, tmp_path, capsys):
        project = _project(tmp_path)
        artifact = _artifact(project, "brief.md", BODY)
        rc = ei.main([
            "admit", str(project), "US-901", str(artifact),
            "--admitted-by", "carol@h3t.co", "--source-note", "emailed 2026-09-01",
        ])
        assert rc == 0
        record = json.loads(capsys.readouterr().out)
        assert record["candidate_digest"] == _digest(BODY)
        assert record["source_note"] == "emailed 2026-09-01"

        _verifier(project, "US-901", BODY)
        assert ei.main(["read", str(project), "US-901"]) == 0
        read = json.loads(capsys.readouterr().out)
        assert read["binds"] is True

    def test_a_refusal_exits_nonzero_with_its_code(self, tmp_path, capsys):
        project = _project(tmp_path)
        rc = ei.main([
            "admit", str(project), "US-901", str(project / "nope.md"),
            "--admitted-by", "carol@h3t.co",
        ])
        assert rc == 1
        assert json.loads(capsys.readouterr().out)["refused"] == "artifact_unreadable"

    def test_read_exits_nonzero_when_nothing_binds(self, tmp_path, capsys):
        project = _project(tmp_path)
        assert ei.main(["read", str(project), "US-901"]) == 1
        assert json.loads(capsys.readouterr().out)["admitted"] is False


# ── `role` is authoritative, and a contradictory legacy `agent` is refused ────
#
# Found by the #592 review of Epic #410 (finding 5). `_declared_abbrev` and
# `_receipt_abbrev` both read `receipt.get("agent") or receipt.get("role")`, so
# the OPTIONAL legacy field overrode the REQUIRED canonical one and nothing
# compared them. `_declared_abbrev` decides eligibility to VERIFY a candidate,
# so a technical-writer receipt carrying `agent: quality-engineer` earned QE
# credit on a field `receipt_validator` does not treat as canonical.


def test_a_legacy_agent_cannot_override_the_canonical_role():
    """Asserts impossible: earning a role's eligibility from `agent` alone.

    The canonical `role` says technical-writer. Before this change
    `_declared_abbrev` returned `qe` here, which is the whole defect: intake
    would treat the document as a verifying stage that never ran.
    """
    receipt = {
        "role": "technical-writer",
        "agent": "quality-engineer",
        "candidate_digest": "d" * 64,
    }
    assert ei._declared_abbrev(receipt) == ""


def test_the_two_role_fields_agreeing_is_still_read():
    """The ordinary case: both fields present and saying the same thing."""
    receipt = {"role": "quality-engineer", "agent": "quality-engineer"}
    assert ei._declared_abbrev(receipt) == "qe"


def test_a_legacy_only_receipt_is_still_read():
    """REGRESSION GUARD, passes before the change too.

    Older receipts carry only `agent`. Making `role` authoritative must not
    stop reading them, or the fix would refuse historical evidence rather than
    contradictory evidence.
    """
    assert ei._declared_abbrev({"agent": "quality-engineer"}) == "qe"


def test_a_canonical_only_receipt_is_read():
    """REGRESSION GUARD, passes before the change too: `role` with no `agent`."""
    assert ei._declared_abbrev({"role": "quality-engineer"}) == "qe"


def test_the_conflict_is_refused_case_insensitively():
    """Asserts impossible: escaping the conflict check with capitalisation."""
    receipt = {"role": "Technical-Writer", "agent": "QUALITY-ENGINEER"}
    assert ei._declared_abbrev(receipt) == ""


def test_receipt_abbrev_also_refuses_the_two_field_conflict():
    """Asserts impossible: the producing-stage path believing `agent` instead.

    `_receipt_abbrev` already refused a filename/body disagreement. It read the
    body through the same `agent or role` expression, so a `-qe` file whose
    canonical role was technical-writer agreed with the filename via the legacy
    field and passed. The conflict is now caught before the filename is
    consulted at all.
    """
    receipt = {"role": "technical-writer", "agent": "quality-engineer"}
    assert ei._receipt_abbrev("US-901-qe.json", receipt) == ""


# ── the immutability claim cannot come back ──────────────────────────────────
#
# The #592 re-review found the module asserting two contradictory things twenty
# lines apart: that the digest "is immutable afterwards", and that the project
# principal can rewrite the record and the artifact together. The second is the
# true one, it is why `cp_external_candidate_intake` is a declared gap, and the
# first had already been copied into the proposal (#598) and this file's own
# docstring. Nothing guarded the class, so it drifted in three places at once.


def _intake_prose() -> str:
    """Every line of prose a reader could take the claim from."""
    import inspect

    return "\n".join(
        [
            inspect.getdoc(external_intake_module) or "",
            inspect.getsource(external_intake_module.admit_external_candidate),
            __doc__ or "",
        ]
    )


def test_no_prose_claims_the_intake_record_is_simply_immutable():
    """Asserts impossible: the absolute claim returning anywhere a reader looks.

    `immutable` is allowed only in a sentence that denies it, because the
    honest statement has to use the word to deny it. What is refused is the
    word used to ASSERT the property: write-once through the admission API is
    what `admit` provides, and immutability against the project principal is
    what it does not.
    """
    import re

    prose = _intake_prose()
    offenders = [
        line.strip()
        for line in prose.splitlines()
        if "immutable" in line.lower()
        and not re.search(r"\bnot immutable\b", line, re.I)
    ]
    assert offenders == [], (
        "prose asserts immutability the module does not provide: %r. The "
        "record is write-once through the admission API; the project "
        "principal can still rewrite it, which is the declared gap." % offenders
    )


# ── an admitted candidate that no longer verifies STOPS, it does not fall back ─
#
# Found by the #592 re-review. `uncreditable_verdict` took an optional `intake`
# defaulting to `None`, which in `_story_candidate_digest` means "already
# consulted, there is none" rather than "read it now", so the public helper
# skipped an admitted record and digested the receipts instead. On a
# SUBSTITUTED artifact it therefore reported creditable while the record path
# refused. The helper is gone; this pins the property it got wrong.


def test_a_substituted_artifact_stops_rather_than_falling_back_to_receipts(
    tmp_path,
):
    """Asserts impossible: a substituted candidate being rescued by receipts.

    The module docstring already states the rule, that an admission which
    exists but yields no candidate STOPS rather than falling through to the
    ledger or the receipt bytes, "which would read as backed". This is that
    rule asserted end to end on the path a verdict actually takes, with a valid
    verifying receipt present so the fallback would succeed if it were reached.
    """
    import story_pipeline as sp

    project = _project(tmp_path)
    _admit(project, "US-901", BODY)
    # A verifying receipt that DOES bind the admitted candidate, so intake
    # resolves cleanly before the substitution and receipt-byte derivation is
    # also available. Both routes to a digest exist if the stop fails.
    _verifier(project, "US-901", BODY)
    assert ei.read_intake(str(project), "US-901").binds is True, (
        "the fixture does not bind before substitution, so this test would "
        "pass for the wrong reason"
    )

    # The admitted artifact is replaced, after admission and after checking.
    record = json.loads(
        Path(ei.record_path(str(project), "US-901")).read_text(encoding="utf-8")
    )
    Path(project / record["artifact_path"]).write_bytes(
        b"# A different brief entirely\n"
    )

    story = {"id": "US-901", "state": "awaiting_acceptance"}
    prepared = sp.record_judged_verdict(
        story, "US-901", verdict="accepted", principal="po@h3t.co",
        project_dir=str(project), commit=False,
    )
    assert prepared["evidence_class"] is None
    assert prepared["unbacked_code"] == "no_candidate"
    assert prepared["candidate_digest"] is None, (
        "the verdict bound to a candidate anyway; if it bound to the receipt "
        "bytes the substitution would read as backed evidence"
    )
    assert "external candidate" in prepared["unbacked_reason"], (
        "the reason does not name the intake, so a reader cannot tell a "
        "substituted artifact from a unit that simply produced nothing"
    )


# ── the intake fact lives out of the admitting principal's reach (#495 P3) ──
#
# Everything above this line compares the record against the artifact, and both
# are files the admitting principal owns, so a matched pair proves internal
# consistency and nothing else: rewrite the digest, swap the bytes, and the
# pair matches again. That was why #495 was reopened twice.
#
# These drive the control plane through a shim, which is what
# `SYNAPTORY_INTAKE_AUTHORITY_BIN` is for. It selects WHICH BINARY answers,
# never WHAT it answers: every assertion below still depends on the recorded
# fact coming back and being compared.


def _cp_shim(tmp_path: Path, digest: str, *, rows: str = "") -> Path:
    """A control plane that records `digest` and reports it back."""
    listed = rows or (
        '{"action":"admit-work-unit","subject_digest":"%s",'
        '"decided_at":"2026-09-10T00:00:00Z","id":"d-1"}' % digest
    )
    script = tmp_path / "cp-cli"
    script.write_text(
        "#!/bin/bash\n"
        'if [ "$3" = "record" ]; then\n'
        "  echo '{\"ok\":true,\"connected\":true,\"decision\":"
        '{"id":"d-1","subject_digest":"%s","principal":"lead@h3t.co"}}\'\n'
        "else\n"
        "  echo '{\"ok\":true,\"connected\":true,\"decisions\":[%s]}'\n"
        "fi\n" % (digest, listed),
        encoding="utf-8",
    )
    script.chmod(0o755)
    import subprocess

    subprocess.run([str(script)], capture_output=True)  # warm the first execve
    return script


def _connected(monkeypatch, shim: Path) -> None:
    import intake_authority

    monkeypatch.setenv("SYNAPTORY_INTAKE_AUTHORITY_BIN", str(shim))
    monkeypatch.setattr(intake_authority, "_channel_stamp", lambda: ("stamped", "test"))


def _admitted(tmp_path: Path, body: bytes) -> "tuple[Path, Path, str]":
    project = tmp_path / "project"
    (project / "inbox").mkdir(parents=True, exist_ok=True)
    artifact = project / "inbox" / "brief.md"
    artifact.write_bytes(body)
    return project, artifact, "sha256:" + hashlib.sha256(body).hexdigest()


class TestTheIntakeFactIsOutOfReach:
    def test_a_connected_admission_records_the_fact_and_says_so(
        self, tmp_path, monkeypatch
    ):
        """`authority` on the record is how a reader tells the two worlds apart.

        Without it, a local record and an authoritative one are the same
        document and a reader has to guess which guarantee it carries.
        """
        project, artifact, digest = _admitted(tmp_path, b"external bytes\n")
        _connected(monkeypatch, _cp_shim(tmp_path, digest))
        record = ei.admit_external_candidate(
            str(project), "US-901", str(artifact), admitted_by="intake@h3t.co"
        )
        assert record["authority"] == "control-plane", record
        assert record["authority_decision_id"] == "d-1", record

    def test_rewriting_the_record_and_the_artifact_together_is_refused(
        self, tmp_path, monkeypatch
    ):
        """THE ATTACK #495 WAS REOPENED FOR, and the one local checks cannot see.

        Admit A, replace the artifact with B after the checking point, rewrite
        the record's digest and byte count to match B. Every local comparison
        passes: the record and the artifact agree. The control plane still says
        A, and disagreement is the LOCAL record being wrong.
        """
        project, artifact, digest = _admitted(tmp_path, b"the admitted bytes\n")
        _connected(monkeypatch, _cp_shim(tmp_path, digest))
        ei.admit_external_candidate(
            str(project), "US-901", str(artifact), admitted_by="intake@h3t.co"
        )

        substitute = b"swapped in after checking\n"
        record_file = Path(ei.record_path(str(project), "US-901"))
        os.chmod(record_file, 0o644)
        record = json.loads(record_file.read_text(encoding="utf-8"))
        artifact.write_bytes(substitute)
        record["candidate_digest"] = (
            "sha256:" + hashlib.sha256(substitute).hexdigest()
        )
        record["artifact_bytes"] = len(substitute)
        record_file.write_text(json.dumps(record), encoding="utf-8")

        read = ei.read_intake(str(project), "US-901")
        assert read.candidate_digest is None, read
        assert any("control plane is authoritative" in p for p in read.problems), read

    def test_a_connected_project_with_no_recorded_fact_is_not_evidence(
        self, tmp_path, monkeypatch
    ):
        """A local record nobody recorded upstream proves nothing.

        Otherwise the bypass is trivial: write the file by hand on a connected
        project and inherit the offline guarantee.
        """
        project, artifact, digest = _admitted(tmp_path, b"never recorded\n")
        _connected(monkeypatch, _cp_shim(tmp_path, digest))
        ei.admit_external_candidate(
            str(project), "US-901", str(artifact), admitted_by="intake@h3t.co"
        )
        # The store forgets it, which is indistinguishable from never having
        # recorded it -- and both mean the local file is not evidence.
        _connected(monkeypatch, _cp_shim(tmp_path, digest, rows=""))
        empty = _cp_shim(tmp_path, digest, rows="")
        empty.write_text(
            "#!/bin/bash\necho '{\"ok\":true,\"connected\":true,\"decisions\":[]}'\n",
            encoding="utf-8",
        )
        empty.chmod(0o755)
        _connected(monkeypatch, empty)
        read = ei.read_intake(str(project), "US-901")
        assert read.candidate_digest is None, read
        assert any("holds no admitted candidate" in p for p in read.problems), read

    def test_a_connected_project_whose_store_is_unreachable_refuses(
        self, tmp_path, monkeypatch
    ):
        """Unreachable is LESS evidence than offline, never more (#507).

        Treating it as unconfigured is how a connected project would silently
        acquire the offline guarantee, which is the direction that must never
        be guessed.
        """
        project, artifact, digest = _admitted(tmp_path, b"bytes\n")
        _connected(monkeypatch, _cp_shim(tmp_path, digest))
        ei.admit_external_candidate(
            str(project), "US-901", str(artifact), admitted_by="intake@h3t.co"
        )
        broken = tmp_path / "broken-cli"
        broken.write_text("#!/bin/bash\nexit 7\n", encoding="utf-8")
        broken.chmod(0o755)
        _connected(monkeypatch, broken)
        read = ei.read_intake(str(project), "US-901")
        assert read.candidate_digest is None, read
        assert any("could not be read" in p for p in read.problems), read

    def test_admission_is_refused_when_the_fact_cannot_be_recorded(
        self, tmp_path, monkeypatch
    ):
        """Fail closed at intake rather than leaving a record claiming nothing.

        A local record written after a failed upstream record would be
        indistinguishable from a rewritten one on the next read, so there is no
        honest way to keep it.
        """
        project, artifact, _ = _admitted(tmp_path, b"bytes\n")
        broken = tmp_path / "broken-cli"
        broken.write_text("#!/bin/bash\nexit 7\n", encoding="utf-8")
        broken.chmod(0o755)
        _connected(monkeypatch, broken)
        with pytest.raises(ei.IntakeRefused) as caught:
            ei.admit_external_candidate(
                str(project), "US-901", str(artifact), admitted_by="intake@h3t.co"
            )
        assert caught.value.code == "authority_unavailable", caught.value.code
        assert not Path(ei.record_path(str(project), "US-901")).exists(), (
            "a refused admission left a local record behind"
        )

    def test_an_offline_project_keeps_working_with_its_limit_recorded(
        self, tmp_path, monkeypatch
    ):
        """Proposal 3.3, and #659 Track D's permanent-limit declaration.

        Requiring connectivity to evaluate a DoD gate is what that rule
        refuses, so offline keeps today's behaviour. What changes is that the
        record now SAYS it was local, so no reader mistakes the two.
        """
        import intake_authority

        project, artifact, _ = _admitted(tmp_path, b"offline bytes\n")
        monkeypatch.delenv("SYNAPTORY_INTAKE_AUTHORITY_BIN", raising=False)
        monkeypatch.setattr(intake_authority, "_resolve_cli", lambda: None)
        monkeypatch.setattr(
            intake_authority, "_channel_stamp", lambda: ("unstamped", "test")
        )
        record = ei.admit_external_candidate(
            str(project), "US-901", str(artifact), admitted_by="intake@h3t.co"
        )
        assert record["authority"] == "none", record
        assert "authority_decision_id" not in record, record
        read = ei.read_intake(str(project), "US-901")
        assert read.admitted is True, read
        # The verifying-receipt requirement is the only thing missing offline,
        # which is the pre-P3 behaviour and not an authority refusal.
        assert all("control plane" not in p for p in read.problems), read


class TestVerifyOnlySelection:
    """`next_action` selects the VERIFYING stage for a verify-only unit.

    Without this the verifier could never be dispatched, so it could never
    carry an attempt identity, so the binding #495 P2 requires would refuse a
    legitimate job as loudly as a forged one. Measured before the change:
    `next_action` answered `dispatch_se` for a queued unit with an admitted
    external candidate, and `evaluate_dispatch` refused `qe` as
    `next_action_mismatch` on all three host policies.
    """

    def _queued(self, unit: str = "US-901") -> dict:
        return {"current_stories": [{
            "id": unit, "title": "t", "state": "queued",
            "pipeline_log": [{"state": "queued", "entered_at": "t0",
                              "exited_at": None}],
            "receipts": [], "retries": {},
        }]}

    def test_an_admitted_unit_selects_the_verifier(self, tmp_path):
        import scrum_state_machine as ssm

        project = _project(tmp_path)
        _admit(project, "US-901", BODY)
        assert ssm.verify_only_units(str(project), self._queued()) == frozenset(
            {"US-901"}
        )

    def test_a_unit_with_no_intake_record_is_untouched(self, tmp_path):
        """The gate on the whole change: nothing that exists today moves."""
        import scrum_state_machine as ssm

        project = _project(tmp_path)
        assert ssm.verify_only_units(str(project), self._queued()) == frozenset()

    def test_a_unit_past_queued_is_not_reselected(self, tmp_path):
        """A started unit has a producing stage behind it whatever intake says.

        Re-selecting a verifier there would contradict the board.
        """
        import scrum_state_machine as ssm

        project = _project(tmp_path)
        _admit(project, "US-901", BODY)
        state = self._queued()
        state["current_stories"][0]["state"] = "in_progress"
        assert ssm.verify_only_units(str(project), state) == frozenset()

    def test_a_substituted_candidate_is_not_verify_only(self, tmp_path):
        """A unit whose subject cannot be established gets no verifier.

        Dispatching one would invite a check against bytes the platform has
        already refused to name. It falls back to `dispatch_se` and its
        sign-off stays unbacked with intake's own reason.
        """
        import scrum_state_machine as ssm

        project = _project(tmp_path)
        _admit(project, "US-901", BODY)
        (project / "inbox" / "US-901-brief.md").write_bytes(b"swapped\n")
        assert ssm.verify_only_units(str(project), self._queued()) == frozenset()

    def test_kanban_asks_the_same_question(self, tmp_path):
        """One definition, both lifecycles. A second copy is the drift."""
        import kanban_state_machine as ksm
        import scrum_state_machine as ssm

        project = _project(tmp_path)
        _admit(project, "US-901", BODY)
        state = self._queued()
        assert ksm.verify_only_units(str(project), state) == ssm.verify_only_units(
            str(project), state
        )
