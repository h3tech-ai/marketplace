"""Layer 1 - which roles are dispatch identities, and which are skills (#405).

docs/proposals/capability-profile-pilot.md section 3.1: the non-pipeline roles
"stop being dispatch identities **where the pipeline does not require a receipt
from them**". That clause is the whole contract, and it is load-bearing in both
directions:

  - Where no gate reads a role's receipt, the orchestrator retrieves the
    role's contract as a skill and does the work in its own context. Nothing
    is weakened, because nothing was reading the receipt.
  - Where a gate DOES read it, the role stays a dispatch. Four of the five
    roles this ticket touched are in this position on at least one edge, and
    the proposal's phrase "the six non-pipeline agents" reads as if none of
    them were. `project-owner` owes a receipt on Work Unit acceptance, `pe`
    and `tw` each owe one at SPQ Acceptance, and `tw` owes the SPQ Checkpoint
    one as well. Only `sa` and `ra` are on no gated edge at all.

The forgeability question the epic requires every gating change to answer is
"does anything that used to owe a receipt now produce none, and does any gate
read that absence as a pass?". These tests answer it by construction: the gated
edges are read out of the kernel's and SPQ machine's own tables rather than
restated here, every one of them is exercised for refusal-on-absence, and the
converted ceremony prose is checked to still say DISPATCH wherever a table
says a receipt is owed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import _spq_fixture
import advance_kernel as ak
import spq_state_machine as spq
import story_pipeline as sp

pytestmark = pytest.mark.unit

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SPQ_SKILLS = PLUGIN_ROOT / "skills" / "synaptory" / "spq"
MODES = PLUGIN_ROOT / "skills" / "synaptory" / "modes"

FUTURE = "2099-01-01T00:00:00Z"
STAGE_ENTERED = "2026-01-01T00:00:00+00:00"

#: The five roles this ticket converted.
CONVERTED = (
    "project-owner",
    "solution-architect",
    "platform-engineer",
    "technical-writer",
    "research-advisor",
)

#: The abbrevs the story pipeline's own gated edges bind. Derived below from
#: the kernel rather than trusted; this is only the expectation to compare to.
EXPECTED_PIPELINE_GATED = {"se", "qe", "cr", "po"}


# ── the enumeration, read out of the real tables ─────────────────────────────


def test_the_story_pipeline_binds_exactly_se_qe_cr_and_po():
    """`TRANSITION_RECEIPT` is the pipeline's whole answer to "who owes what".

    Pinned so that adding or dropping a bound role forces this file to be
    re-read: the conversion decision for every role is a lookup in this table,
    and a silent change here would make a converted role's absence
    unobserved.
    """
    bound = {abbrev for _, abbrev in ak.TRANSITION_RECEIPT.values()}
    assert bound == EXPECTED_PIPELINE_GATED


def test_project_owner_is_bound_on_the_acceptance_edge():
    """The specific row that makes PO not a purely on-demand role."""
    assert ak.TRANSITION_RECEIPT[("awaiting_acceptance", "done")] == (
        "project-owner",
        "po",
    )
    # And a story blocked out of awaiting_acceptance is PO's too, so the
    # rejection path is a dispatch as well as the acceptance path.
    assert ak.BLOCKED_FROM_ROLE["awaiting_acceptance"] == ("project-owner", "po")


def test_sa_and_ra_are_bound_nowhere_in_the_pipeline():
    bound_roles = {role for role, _ in ak.TRANSITION_RECEIPT.values()}
    bound_roles |= {role for role, _ in ak.BLOCKED_FROM_ROLE.values()}
    assert "solution-architect" not in bound_roles
    assert "research-advisor" not in bound_roles


def test_spq_acceptance_requires_pe_and_tw():
    """`ACCEPTANCE_ROLES` is the SPQ release gate's required set."""
    assert spq.ACCEPTANCE_ROLES["pe"] == "platform-engineer"
    assert spq.ACCEPTANCE_ROLES["tw"] == "technical-writer"
    # And it does NOT require the two roles that own no gated edge, nor po.
    assert "sa" not in spq.ACCEPTANCE_ROLES
    assert "ra" not in spq.ACCEPTANCE_ROLES
    assert "po" not in spq.ACCEPTANCE_ROLES


def test_the_converted_roles_that_stay_dispatches_are_exactly_po_pe_tw():
    """The per-role conclusion, as one assertion.

    A role stays a dispatch identity on at least one flow when any gated table
    names it. Derived, so a future table edit moves this answer rather than
    leaving the documentation behind it.

    The answer is unchanged and the derivation is now complete. `tw` used to be
    added by hand here with the comment "checkpoint_readiness, asserted
    below", because the SPQ Checkpoint gate read a `tw` receipt from a table
    this expression did not cover. #644 removed that gate -- Checkpoint is a
    recorded method event that blocks nothing -- so the hand-added row is gone
    and `tw` is gated purely by `ACCEPTANCE_ROLES`, which names it. TW stays a
    dispatch identity for one fewer reason than it had.
    """
    gated: set[str] = {role for role, _ in ak.TRANSITION_RECEIPT.values()}
    gated |= set(spq.ACCEPTANCE_ROLES.values())
    assert {role for role in CONVERTED if role in gated} == {
        "project-owner",
        "platform-engineer",
        "technical-writer",
    }
    assert {role for role in CONVERTED if role not in gated} == {
        "solution-architect",
        "research-advisor",
    }


# ── the gated edges still refuse on absence ──────────────────────────────────


def _project(tmp_path: Path, *, build_mode: str = "spq") -> Path:
    """A project whose config makes the walk below assert what it claims.

    TWO KEYS BEYOND `build_mode`, and neither is scaffolding:

    `quality.dod_tier: growing` pins the adaptive tier. Code review is an
    ADAPTIVE obligation -- `code_reviewed` enters the tier's check set at
    Sprint 2 / Cycle 2 -- so a fresh Cycle computes `early`, the kernel
    resolves no bound role for `reviewing -> awaiting_acceptance`, and it
    records `receipt waived for DoD intensity`. The walk would then reach
    `done` having never required a CR receipt, while asserting that it did.
    Pinning the tier is how the test tests the gate rather than the default.

    `growing` and not `mature`, deliberately: `mature` also promotes
    `coverage_no_decrease`, whose evidence is a `metrics.coverage_delta` no
    dispatch in this walk records. The walk's subject is WHICH IDENTITIES a
    Work Unit needs, so the tier it pins is the smallest one that makes the
    code-review identity required. A tier that pulled in an unrelated
    obligation would make this file fail for coverage reasons.

    `sprint.review.per_story_acceptance: true` is what makes
    `awaiting_acceptance` a state at all: with it off the kernel routes
    `reviewing -> done` and the two acceptance edges in `WORK_UNIT_WALK` do
    not exist. The walk's own comment has said "with per-stage acceptance on"
    since it was written; the config never said it.
    """
    (tmp_path / ".synaptory" / ".orchestrator" / "receipts").mkdir(parents=True)
    (tmp_path / ".synaptory.yaml").write_text(
        "build_mode: %s\n"
        "quality:\n"
        "  dod_tier: growing\n"
        "sprint:\n"
        "  review:\n"
        "    per_story_acceptance: true\n" % build_mode,
        encoding="utf-8",
    )
    return tmp_path


def _receipts_dir(project: Path) -> Path:
    """THE receipts home for this project, from the one resolver.

    Hardcoded as the flat `.orchestrator/receipts` until #640. That is right
    for a project with no open Cycle and wrong for one with a Cycle, whose
    receipts live under `spq/cycles/<cycle-id>/receipts` -- so once
    `_seed_work_unit` started opening a real Cycle, a hardcoded path would put
    every receipt this file writes where no gate reads it.
    """
    return Path(sp.receipts_dir_for(str(project), intended=True))


def _receipt(
    project: Path,
    story_id: str,
    abbrev: str,
    role: str,
    *,
    stage: str = "qe-verification",
    **overrides,
) -> Path:
    src = project / "src" / "foo.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("x\n", encoding="utf-8")
    payload = {
        "story_id": story_id,
        "role": role,
        "backend": "claude",
        "model": "claude-opus-4-8",
        "artifacts": ["src/foo.py"],
        "verification_commands": [
            {"command": "pytest -q", "exit_code": 0, "summary": "ok"},
            {"command": "npm run build", "exit_code": 0, "summary": "ok"},
        ],
        "metrics": {"n": 1},
        "completed_at": FUTURE,
        "token_usage": {
            "input": 1,
            "output": 1,
            "cache_read": 0,
            "cache_write": 0,
            "stage": stage,
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
    receipts = _receipts_dir(project)
    receipts.mkdir(parents=True, exist_ok=True)
    path = receipts / ("%s-%s.json" % (story_id, abbrev))
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return path


def _seed_work_unit(project: Path, story_id: str = "WU-001") -> None:
    """One admitted Work Unit on a board `open_cycle` produced.

    Hand-wrote `{"build_mode": "spq", "lifecycle_state": "CYCLE_EXECUTION",
    "current_stories": [...]}` into `pipeline-state.json` until #640 -- the
    pre-#303 shape `test_board_reader_census.py` records as the reason three
    blind-reader defects survived their own suites. Against the #644 runtime
    the kernel resolves no Cycle from it and every walk below stopped at
    `story_not_found`.
    """
    _spq_fixture.open_project(
        project, units=[{"id": story_id, "title": "seed"}], goal="seed"
    )


def _policy() -> ak.HostPolicy:
    return ak.HostPolicy(host="test", require_next_action_match=False)


#: `queued -> ... -> done` with per-stage acceptance on. The role column is the
#: producing/verifying identity the kernel binds; nothing else appears. SE
#: appears twice because it owns two edges, and each needs its OWN receipt: the
#: kernel refuses the second edge with `replay` if the first edge's bytes are
#: re-presented, which is anti-replay working, not a fixture problem. The
#: `nth` column varies the receipt so the digests differ, exactly as two real
#: SE dispatches would.
WORK_UNIT_WALK = (
    ("in_progress", "software-engineer", "se", "se-implementation", 1),
    ("testing", "software-engineer", "se", "se-implementation", 2),
    ("reviewing", "quality-engineer", "qe", "qe-verification", 1),
    ("awaiting_acceptance", "code-reviewer", "cr", "cr-review", 1),
    ("done", "project-owner", "po", "pro-brd", 1),
)


# ── the seal-reader gap that stood here is closed ───────────────────────────
#
# Two tests in this file were `xfail(strict=True)` on `SEAL_READER_GAP`:
# `spq_manifest` read the pre-#644 seal keys (`manifest_hash`, `work_units`),
# so `verify_hash` answered False for every seal `open_cycle` writes, the DoD
# gate read that as an untrusted manifest and declared a `tests_pass` criteria
# gap, and no SPQ Work Unit could reach `done`.
#
# It is closed by `spq_manifest.verify_sealed` / `sealed_units`, which dispatch
# on the key the document actually carries, and the declaration is deleted with
# it -- which is what `strict` is for. Closing a gap and leaving its
# declaration standing XPASSes and fails the build, so the record cannot rot
# into a permanent excuse.
#
# WHAT REMAINED AFTER IT, and it was in the fixture rather than the product:
# with the seal readable, the walk still reached `done` without ever requiring
# a code-review receipt. `code_reviewed` is an ADAPTIVE obligation, a fresh
# Cycle computes tier `early`, and the kernel correctly recorded "receipt
# waived for DoD intensity" -- so the walk asserted a gate that the default
# config had switched off. `_project` now pins the tier. A test whose subject
# is which identities are required must not take the tier as it finds it.


def test_a_full_spq_work_unit_completes_on_producing_verifying_and_judged(tmp_path):
    """The #405 acceptance criterion, walked through the real kernel.

    Every edge is asserted to REFUSE before its receipt exists and to pass
    once it does, so the walk proves the gates ran rather than that the story
    happened to reach `done`. Then the set of receipts the unit actually
    needed is compared to the producing/verifying/acceptance set: no
    `solution-architect`, `platform-engineer`, `technical-writer` or
    `research-advisor` receipt is written anywhere, and none is asked for.
    """
    project = _project(tmp_path)
    _seed_work_unit(project)

    for target, role, abbrev, stage, nth in WORK_UNIT_WALK:
        refused = ak.evaluate_advance(str(project), "WU-001", target, policy=_policy())
        assert not refused.allowed, (
            "%s advanced with no fresh %s receipt on disk" % (target, abbrev)
        )
        # The first edge for a role has no receipt at all; a role's second
        # edge has last edge's file, which the ledger refuses as a replay.
        # Either way the edge is closed until fresh evidence arrives.
        assert refused.code in (ak.NO_RECEIPT, ak.REPLAY), (
            target,
            refused.code,
            refused.reason,
        )

        _receipt(
            project,
            "WU-001",
            abbrev,
            role,
            stage=stage,
            metrics={"n": nth, "dispatch": nth},
        )
        allowed = ak.execute_advance(
            str(project), "WU-001", target, policy=_policy()
        )
        assert allowed.allowed, "%s: %s" % (target, allowed.reason)

    story = sp.get_story(sp._read_state(str(project)), "WU-001")
    assert story["state"] == "done"
    assert len(story["mcp_consumed_receipts"]) == len(WORK_UNIT_WALK)

    written = {p.name.rsplit("-", 1)[1][:-5] for p in _receipts_dir(project).glob("*.json")}
    assert written == {"se", "qe", "cr", "po"}, (
        "a full Work Unit needed receipts beyond the producing, verifying and "
        "acceptance dispatches: %s" % sorted(written)
    )
    for absent in ("sa", "pe", "tw", "ra"):
        assert not (_receipts_dir(project) / ("WU-001-%s.json" % absent)).exists()


def test_the_acceptance_verdict_is_still_judged_evidence_bound_to_the_candidate(
    tmp_path,
):
    """#403's `judged` class survives the conversion.

    The forgeability answer for the acceptance flow lives here. The untrusted
    input is `accepted_by`, the principal. The forgery it would enable is a
    verdict that chooses its own subject, or claims independence it did not
    earn. The refusing line is `record_judged_verdict`, which DERIVES the
    candidate digest from the consumed-receipt ledger or the receipt bytes and
    computes independence against the recorded producer set, failing closed.
    Nothing about turning ceremony steps into skill invocations touches any of
    that, and this test is what says so out loud.
    """
    project = _project(tmp_path)
    _receipt(project, "WU-002", "se", "software-engineer", stage="se-implementation")
    _receipt(project, "WU-002", "qe", "quality-engineer")
    state = {
        "current_stories": [
            {
                "id": "WU-002",
                "title": "t",
                "state": "awaiting_acceptance",
                "pipeline_log": [
                    {
                        "state": "awaiting_acceptance",
                        "entered_at": STAGE_ENTERED,
                        "exited_at": None,
                    }
                ],
                "receipts": [],
                "retries": {},
            }
        ]
    }
    sp.accept_story(state, "WU-002", "po@h3t.co", project_dir=str(project))
    verdicts = sp._find_story(state, "WU-002")[sp.JUDGED_VERDICTS_KEY]
    assert len(verdicts) == 1
    item = verdicts[0]
    assert item["evidence_class"] == "judged"
    assert item["verdict"] == "accepted"
    assert item["principal"] == "po@h3t.co"
    assert item["candidate_digest"].startswith("sha256:")
    assert item["check_id"] == "po_acceptance"
    # Independence is RECORDED, not assumed, and false is a valid answer.
    assert item["independent_of_producer"] in (True, False)
    assert item["independence_basis"]


def test_the_kernel_done_edge_and_the_judged_verdict_are_two_paths(tmp_path):
    """A seam this ticket found and did NOT introduce, pinned so it is visible.

    `awaiting_acceptance -> done` has two writers, and they produce different
    evidence:

      - `advance_kernel.execute_advance(..., "done")` binds the PO receipt,
        evaluates the DoD gate, and records the consumed digest. It does NOT
        call `accept_story`, so it records no `judged` verdict.
      - `story_pipeline.accept_story` records the `judged` verdict and then
        transitions, without entering the kernel's consumed-receipt ledger.

    **And the second is the path every shipped host actually takes.** The SPQ
    Checkpoint acceptance walk runs `spq_state_machine.py accept_story`, the
    Scrum Sprint Review walk runs `scrum_state_machine.py accept_story`, and
    the Cursor and Codex MCP surfaces expose `accept_story` as a tool. None of
    them advances this edge through the kernel. So the kernel's
    `TRANSITION_RECEIPT` row for it is live only for a caller that points
    `advance` at `done` directly, and in the specified flows the evidence is
    the judged verdict rather than a PO receipt.

    That matters for how #405's acceptance criterion is stated. "Receipts from
    the producing and verifying dispatches plus judged acceptance" is exactly
    right, and the two halves have different enforcement: the receipts are
    gated by the kernel, the judged verdict is produced by the acceptance call
    and gated by nothing. It is #435's subject (verdict provenance); #403
    built the `judged` class without wiring the kernel edge to it.

    Pinned rather than fixed: `core/lib/story_pipeline.py` is #406's
    single-writer file for this window, and closing the seam means changing
    who may write that edge.
    """
    project = _project(tmp_path)
    _seed_work_unit(project, "WU-003")
    for target, role, abbrev, stage, nth in WORK_UNIT_WALK:
        _receipt(
            project,
            "WU-003",
            abbrev,
            role,
            stage=stage,
            metrics={"n": nth, "dispatch": nth},
        )
        assert ak.execute_advance(
            str(project), "WU-003", target, policy=_policy()
        ).allowed

    story = sp.get_story(sp._read_state(str(project)), "WU-003")
    assert story["state"] == "done"
    # The receipt half IS enforced, and the DoD gate did run.
    assert len(story["mcp_consumed_receipts"]) == len(WORK_UNIT_WALK)
    assert (story.get("dod") or {}).get("checks"), (
        "the done edge advanced without evaluating the DoD gate"
    )
    # The verdict half is not on this path. If this ever starts being
    # populated, the seam closed and this test should become the assertion
    # that it stays closed.
    assert not story.get(sp.JUDGED_VERDICTS_KEY), (
        "the kernel done edge now records a judged verdict; update this test "
        "to assert the verdict is present rather than absent, and drop the "
        "qualifier from the #405 acceptance claim"
    )


def test_the_specified_acceptance_walk_names_no_per_unit_verb():
    """WHICH WAY THE GAP WENT, recorded here because this test used to assert
    the opposite.

    It used to require the Checkpoint walk to name `accept_story`, which was
    the honest reading while the walk did: #644 removed every per-unit verb
    from the SPQ CLI and the shipped prose kept instructing one, so the
    documented walk died on a usage error. `test_adr029_acceptance_edge.
    test_the_spq_cli_names_no_per_unit_acceptance_verb` is the other half.

    The P6 rewrite resolved it toward the method rather than toward the
    predecessor's verb. SPQ has no per-Work-Unit acceptance edge:
    `awaiting_acceptance` is a Scrum sprint-review toggle that `spq_state_
    machine.next_action` deliberately does not pass, and acceptance under SPQ
    is a named human's judgment recorded at Checkpoint, which the barrier reads
    off the unit results and credits as throughput (`SPQ-R16`). So the walk
    names no verb, and the thing to assert is that it does not quietly grow one
    back -- in either spelling.
    """
    checkpoint = (SPQ_SKILLS / "checkpoint.md").read_text(encoding="utf-8")
    for retired in ("accept_story", "reject_story", "awaiting_acceptance"):
        assert retired not in checkpoint, (
            "the Checkpoint walk names %r again. There is no such verb on the "
            "SPQ CLI and no per-unit acceptance edge on this lifecycle; if one "
            "was restored, re-read ADR-029's #486 amendment first." % retired
        )
    # What replaced it: a named human per unit, read by the barrier.
    assert "accountable human has accepted it as verified" in checkpoint
    assert "accepted_by" in checkpoint
    # And it is still NOT a PO subagent dispatch.
    assert not _resolves_backend_for(checkpoint, "project-owner"), (
        "the SPQ acceptance walk now resolves a project-owner backend, so it "
        "became a dispatch; re-read which writer this edge uses"
    )


# `test_spq_checkpoint_still_refuses_without_the_tw_receipt` was DELETED here
# with its subject. It asserted `spq_state_machine.checkpoint_readiness`, which
# #644 removed: `C-02` makes Commit, Sync and Checkpoint method EVENTS rather
# than stages, and `method_events` "records and cannot decide". There is no
# Checkpoint gate for a receipt's absence to block, so a migrated version of
# that test could only have asserted something else under its old name.
#
# The consequence is recorded rather than hidden, because it changes an answer
# this file exists to give: `tw` was a gated dispatch identity on TWO edges and
# is now gated on one, the Acceptance receipt below. It is still a dispatch
# identity -- `test_the_converted_roles_that_stay_dispatches_are_exactly_po_pe_tw`
# derives that from `ACCEPTANCE_ROLES` -- but if that row ever goes, nothing
# gates `tw` at all and the conversion decision for it has to be re-read.
# `close_cycle` records the Checkpoint and takes a barrier verdict it did not
# compute; it reads no role's receipt.


def test_spq_acceptance_still_refuses_without_the_pe_and_tw_receipts(tmp_path):
    """Acceptance is the edge that keeps `pe` and `tw` dispatch identities.

    Rewritten against the real `acceptance_readiness(project_dir)`, which is
    keyword-free now and reads its own Cycle rather than a caller-supplied
    `state` dict: an Acceptance is per-Cycle (`SC-MTH-014`, each go-live
    compiles its own package), so the receipts it looks for are resolved from
    the Cycle on disk. A hand-passed state could name a Cycle whose receipt
    directory belonged to another one.
    """
    project = _project(tmp_path)
    _spq_fixture.open_project(
        project, units=[{"id": "WU-001", "title": "seed"}], goal="seed"
    )

    readiness = spq.acceptance_readiness(str(project))
    assert readiness["ready"] is False
    assert set(readiness["missing"]) == set(spq.ACCEPTANCE_ROLES.values()), (
        "acceptance_readiness stopped requiring some of its bound roles: %r"
        % (readiness,)
    )
    for role in ("platform-engineer", "technical-writer"):
        assert role in readiness["missing"], readiness

    # Present is the counter-case: a receipt for one role satisfies THAT role
    # and no other, so the gate reads its own bound roles rather than any
    # receipt filed for the Cycle.
    seq = int(spq.read_state(str(project))["current_cycle"])
    _receipt(
        project, "ACCEPTANCE-%d" % seq, "tw", "technical-writer", stage="tw-docs"
    )
    after = spq.acceptance_readiness(str(project))
    assert after["ready"] is False
    assert after["present"] == ["technical-writer"], after
    assert "platform-engineer" in after["missing"], after

    # And an orchestrator receipt at a descriptive suffix satisfies nothing:
    # the gate looks for `ACCEPTANCE-<seq>-<abbrev>.json` per bound role.
    _receipt(
        project, "ACCEPTANCE-%d" % seq, "feedback", "orchestrator",
        stage="pro-brd", accountable_role="project-owner",
    )
    still = spq.acceptance_readiness(str(project))
    assert "platform-engineer" in still["missing"], (
        "an orchestrator-authored ceremony receipt satisfied a bound role; the "
        "gate must read its own roles, not any receipt for the Cycle"
    )


# ── the converted ceremony prose matches the tables ──────────────────────────

#: `(ceremony file, role, expected form)` for every site this ticket touched.
#: `skill` means the prose must present it as a skill invocation and must NOT
#: still tell the orchestrator to spawn that role via `Agent()`. `dispatch`
#: means the opposite. Derived from the gated-edge tables above; a row that
#: disagrees with them is the bug this table exists to catch.
CEREMONY_SITES = (
    ("discovery.md", "research-advisor", "skill"),
    ("discovery.md", "project-owner", "skill"),
    ("discovery.md", "solution-architect", "skill"),
    ("discovery.md", "platform-engineer", "skill"),
    ("commit.md", "project-owner", "skill"),
    ("commit.md", "solution-architect", "skill"),
    ("sync.md", "solution-architect", "skill"),
    ("checkpoint.md", "project-owner", "skill"),
    ("checkpoint.md", "technical-writer", "dispatch"),
    ("acceptance.md", "platform-engineer", "dispatch"),
    ("acceptance.md", "technical-writer", "dispatch"),
)

#: The line every dispatch site carries and no skill-invocation site may.
_SPAWN_RE = re.compile(r"MANDATORY: Spawn this agent via the `Agent\(\)` tool")

#: A skill-invocation site resolves no agent backend, because there is no
#: subagent to route. A surviving `backend_config.py <role>` call is the tell
#: that a site was half-converted.
def _resolves_backend_for(text: str, role: str) -> bool:
    return ('backend_config.py}}" "$(pwd)" "%s"' % role) in text


@pytest.mark.parametrize("filename,role,form", CEREMONY_SITES)
def test_ceremony_site_matches_its_gate_status(filename: str, role: str, form: str):
    text = (SPQ_SKILLS / filename).read_text(encoding="utf-8")
    if form == "skill":
        assert not _resolves_backend_for(text, role), (
            "%s still resolves an agent backend for %s, so the site was only "
            "half converted: the prose says skill and the shell says dispatch"
            % (filename, role)
        )
        assert "SKILL INVOCATION, not a dispatch" in text, (
            "%s converted %s but never says so, so nothing tells a reader why "
            "no receipt from that role appears" % (filename, role)
        )
        assert ("synaptory skills get %s" % role) in text, (
            "%s tells the orchestrator to run %s inline without naming the "
            "skill to retrieve" % (filename, role)
        )
    else:
        assert _resolves_backend_for(text, role), (
            "%s stopped resolving the agent backend for %s, which is a "
            "gate-required dispatch" % (filename, role)
        )
        assert _SPAWN_RE.search(text), (
            "%s lost the mandatory-spawn instruction for its gate-required "
            "dispatches" % filename
        )


@pytest.mark.parametrize("role", ("solution-architect", "research-advisor"))
def test_no_ceremony_dispatches_a_role_that_owes_no_receipt(role: str):
    """The converted-in-full check: after #405, `sa` and `ra` are dispatched by
    no SPQ ceremony at all, because there is no flow where either owes one."""
    offenders = []
    for path in sorted(SPQ_SKILLS.glob("*.md")):
        if _resolves_backend_for(path.read_text(encoding="utf-8"), role):
            offenders.append(path.name)
    assert not offenders, (
        "%s is on no receipt-gated edge but is still dispatched by: %s"
        % (role, offenders)
    )


def test_the_dispatcher_states_the_rule_and_the_gated_edges():
    """`modes/spq.md` is the one file that spans every state, so the rule and
    the table of gated edges live there. Without them a later reader has no
    way to tell a deliberate skill invocation from a forgotten dispatch."""
    text = (MODES / "spq.md").read_text(encoding="utf-8")
    assert "Dispatch identities versus skill invocations" in text
    for marker in (
        "advance_kernel.TRANSITION_RECEIPT",
        "spq_state_machine.acceptance_readiness",
        "accountable_role",
    ):
        assert marker in text, "modes/spq.md does not name %s" % marker
    # `spq_state_machine.checkpoint_readiness` was a marker here until P6. The
    # function is gone -- `C-02` makes Checkpoint an event, and an event gates
    # nothing -- so requiring the dispatcher to name it required the dispatcher
    # to name something that does not exist. What must be stated instead is
    # what actually refuses the close, because "the TW receipt gates it" was
    # the wrong answer for a whole release and a reader had no way to tell.
    assert "barrier-gated" in text, (
        "modes/spq.md no longer says what refuses a Cycle close. It is the "
        "barrier, not a receipt: `close_cycle` reads no role's receipt."
    )
    assert "checkpoint_readiness" not in text, (
        "modes/spq.md names checkpoint_readiness, which #644 removed. A gate "
        "that does not exist is worse guidance than no gate named at all."
    )
    # The accounting rule that keeps /cost comparable across the two arms.
    assert "token_usage.stage` stays the ROLE's stage" in text


@pytest.mark.parametrize("role", CONVERTED)
def test_every_converted_skill_states_its_gate_status(role: str):
    """A role's own contract has to say whether it is ever gate-required.

    Otherwise the next person to read only the SKILL.md cannot tell whether
    running it inline is legitimate, and #405's whole distinction lives in
    that answer.
    """
    text = (PLUGIN_ROOT / "agents" / role / "SKILL.md").read_text(encoding="utf-8")
    if role in ("solution-architect", "research-advisor"):
        assert "no receipt-gated edge" in text, (
            "%s does not state that it is on no gated edge" % role
        )
    else:
        assert "receipt-gated edge" in text, (
            "%s does not state which edges require a dispatch from it" % role
        )
