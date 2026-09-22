"""Layer 1 - ADR-029's "only legal writer" and the acceptance edge (#486).

ADR-029's Decision says the advance kernel is the only legal writer of story
pipeline state. On `awaiting_acceptance -> done` that was never true, and the
bypassing path is the only path any host uses. #486 settled the boundary as a
DECLARED EXCEPTION rather than by routing acceptance through the kernel, so the
risk this file exists to manage is documentation rot: the ADR now makes a set
of specific claims about a code path no kernel check guards, and a claim nobody
re-checks becomes a claim nobody can trust.

**What every test here asserts is impossible: that the ADR's amendment and the
runtime drift apart without a test failing.** Each test binds one sentence of
the amendment to the behaviour it describes, so closing the gap, opening a new
one, or deleting the prose all break the same assertion.

Pre-change these fail on the ADR half (there was no amendment to read). They
are not characterization tests wearing a documentation hat: the runtime halves
are derived from the real tables (`TRANSITION_RECEIPT`,
`_RECEIPT_GATED_EDGES`, `VALID_TRANSITIONS`) or exercised end to end, so a
table edit moves the answer instead of leaving the document behind it.

Forgeability, in the form the epic asks for it:

  Untrusted input   `accepted_by`, the principal, a free string on a local verb.
  The forgery       a verdict that chooses its own subject, claims independence
                    it did not earn, or re-credits a candidate already judged.
  The refusing line `story_pipeline.record_judged_verdict`, which DERIVES the
                    candidate digest and computes independence, and appends a
                    repeat verdict as `evidence_class: None`.
  And the gap       that refusal governs the RECORD, not the WRITE. The
                    transition and the `evidence_dod / approved` gate event both
                    happen anyway. `test_the_replay_walk_*` and
                    `test_the_gate_event_ships_*` pin that, because a declared
                    gap that nobody can see is an undeclared one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from datetime import datetime, timedelta, timezone

import pytest

import advance_kernel as ak
import kanban_state_machine as kb
import scrum_state_machine as scrum
import spq_state_machine as spq
import story_pipeline as sp

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
ADR = REPO_ROOT / "docs" / "adrs" / "ADR-029-advance-kernel.md"
CORE_LIB = REPO_ROOT / "core" / "lib"

ACCEPTANCE_EDGE = ("awaiting_acceptance", "done")
# A receipt timestamp that POST-DATES the stage it advances out of, which is
# what the staleness gate asks for. It used to be a hard-coded year 2090/2099,
# and #801 is why it no longer is: a `completed_at` in the future passes the
# staleness gate BY CONSTRUCTION for as long as the drift lasts, so the suite
# was reaching the gate through the very hole the product now refuses. Near
# future keeps every test's intent (fresh, post-dating stage entry) while
# staying inside `receipt_validator.COMPLETED_AT_FUTURE_TOLERANCE_SECONDS`.
FUTURE = (
    datetime.now(timezone.utc) + timedelta(seconds=900)
).strftime("%Y-%m-%dT%H:%M:%SZ")
ENTERED = "2026-01-01T00:00:00+00:00"


def _adr_text() -> str:
    return ADR.read_text(encoding="utf-8")


def _amendment() -> str:
    """The #486 amendment section only, so a match elsewhere cannot pass it.

    Whitespace is collapsed: the ADR is hard-wrapped at 90 columns, so a phrase
    assertion must not depend on where the prose happens to break.
    """
    text = _adr_text()
    start = text.find("## Amendment (#486)")
    assert start != -1, (
        "ADR-029 carries no #486 amendment. Its Decision states the kernel is "
        "the only legal writer of story pipeline state, which is false on "
        "awaiting_acceptance -> done; the document and the code must agree."
    )
    nxt = text.find("\n## ", start + 1)
    body = text[start:] if nxt == -1 else text[start:nxt]
    return re.sub(r"\s+", " ", body)


# ── fixtures ─────────────────────────────────────────────────────────────────


def _project(tmp_path: Path, build_mode: str = "spq") -> Path:
    (tmp_path / ".synaptory" / ".orchestrator" / "receipts").mkdir(parents=True)
    (tmp_path / ".synaptory.yaml").write_text(
        "build_mode: %s\nsprint:\n  review:\n    per_story_acceptance: true\n"
        % build_mode,
        encoding="utf-8",
    )
    return tmp_path


def _receipt(project: Path, story_id: str, abbrev: str, role: str, stage: str) -> None:
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
            "input": 1, "output": 1, "cache_read": 0, "cache_write": 0,
            "stage": stage,
        },
        "story_dod": {
            "tests_pass": True, "build_succeeds": True,
            "no_critical_findings": True, "code_reviewed": True,
            "coverage_no_decrease": True,
        },
    }
    (
        project / ".synaptory" / ".orchestrator" / "receipts"
        / ("%s-%s.json" % (story_id, abbrev))
    ).write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _walked_unit(project: Path, story_id: str = "WU-1") -> dict:
    """A Work Unit sitting in `awaiting_acceptance` with SE/QE/CR receipts."""
    for abbrev, role, stage in (
        ("se", "software-engineer", "se-implementation"),
        ("qe", "quality-engineer", "qe-verification"),
        ("cr", "code-reviewer", "cr-review"),
    ):
        _receipt(project, story_id, abbrev, role, stage)
    return {
        "version": "2.0",
        "build_mode": "spq",
        "lifecycle_state": "CYCLE_EXECUTION",
        "current_cycle": 1,
        "current_sprint": 1,
        "cumulative_ticket_number": 1,
        "sprint_goal": "g",
        "pipeline_log": [],
        "current_stories": [
            {
                "id": story_id,
                "title": "t",
                "state": "awaiting_acceptance",
                "pipeline_log": [
                    {"state": "awaiting_acceptance",
                     "entered_at": ENTERED, "exited_at": None}
                ],
                "receipts": [],
                "retries": {},
                "rejection_feedback": [],
            }
        ],
    }


def _verdicts(state: dict, story_id: str = "WU-1") -> list:
    return sp._find_story(state, story_id).get(sp.JUDGED_VERDICTS_KEY) or []


# ── the amendment exists and says the four things it has to say ──────────────


def test_the_adr_scopes_the_absolute_to_agent_produced_transitions():
    """The Decision's unqualified sentence is the thing #486 came to fix.

    Impossible after this: reading ADR-029's Decision and concluding that every
    story-state write carries the kernel's guarantees. The Decision must carry
    a pointer, and the amendment must state the narrowed rule.
    """
    text = _adr_text()
    decision = text[text.find("## Decision"):text.find("### 1. The union")]
    assert "#486" in decision, (
        "the Decision still states the absolute with no pointer to the "
        "exception; a reader who stops there reasons wrongly"
    )
    body = _amendment()
    assert "agent-produced" in body
    assert "declared exception" in body


def test_the_amendment_names_the_edge_the_writer_and_the_discipline():
    """The exception has to be identifiable from the ADR alone."""
    body = _amendment()
    for token in (
        "awaiting_acceptance -> done",
        "accept_story",
        "record_judged_verdict",
        "TRANSITION_RECEIPT",
    ):
        assert token in body, "the amendment does not name %r" % token


def test_the_amendment_names_what_the_discipline_does_not_provide():
    """#486's first acceptance criterion, as an assertion.

    Impossible after this: an amendment that declares the exception and leaves
    a reader to assume the exception is as strong as the rule. All three named
    absences must be stated.
    """
    body = _amendment()
    lowered = body.lower()
    for absence in ("anti-replay", "dispatch binding", "next_action"):
        assert absence in lowered, (
            "the amendment does not say the acceptance edge lacks %r" % absence
        )
    assert "presence check" in lowered and "authorization check" in lowered, (
        "the amendment must say that Codex's non-empty `accepted_by` check is "
        "a presence check and not an authorization check"
    )


def test_the_kernel_docstring_carries_the_same_qualifier():
    """The module docstring made the same absolute claim as the ADR.

    Impossible after this: the ADR and the kernel's own docstring disagreeing
    about what the kernel is the only legal writer OF.
    """
    doc = (CORE_LIB / "advance_kernel.py").read_text(encoding="utf-8")
    head = doc[: doc.find('"""', doc.find('"""') + 3)]
    assert "AGENT-PRODUCED" in head or "agent-produced" in head
    assert "#486" in head
    assert "accept_story" in head


# ── the runtime half: the exception is real and shaped as described ──────────


def test_the_acceptance_verb_has_no_receipt_gated_refusal_unlike_transition():
    """The structural statement of the exception, read out of the source.

    `transition_story`'s CLI branch calls `_cli_receipt_gated_refusal` and
    refuses this edge; `accept_story`'s branch calls nothing. That asymmetry is
    the exception, and it is deliberate rather than an oversight.

    Impossible after this: the two branches silently converging (in either
    direction) without the amendment being re-read.

    SCRUM ONLY NOW, AND THAT IS A FINDING RATHER THAN A NARROWING. #644 removed
    every per-story verb from the SPQ CLI -- `transition_story`,
    `accept_story`, `reject_story` and `unblock_story` are all gone, and its
    usage line lists Cycle verbs only. There is no SPQ pair left to compare, so
    the ADR-029 #486 exception is currently unreachable on SPQ.
    `test_the_spq_cli_names_no_per_unit_acceptance_verb` asserts that absence
    directly, so this narrowing cannot quietly become the only record of it.
    """
    src = (CORE_LIB / "scrum_state_machine.py").read_text(encoding="utf-8")
    block = src[src.find('elif action == "transition_story"'):]
    block = block[: block.find('elif action == "unblock_story"')]
    assert "_cli_receipt_gated_refusal" in block, (
        "scrum_state_machine.py: the bare transition verb stopped refusing "
        "gated edges"
    )
    acc = src[src.find('elif action == "accept_story"'):]
    acc = acc[: acc.find('elif action == "reject_story"')]
    assert "_cli_receipt_gated_refusal" not in acc, (
        "scrum_state_machine.py: accept_story now routes through the "
        "gated-edge refusal, so the acceptance edge is no longer a declared "
        "exception; update ADR-029's #486 amendment before landing this"
    )


def test_the_spq_cli_names_no_per_unit_acceptance_verb():
    """The absence #644 created, and WHICH WAY the mismatch it opened went.

    This test used to record a live defect: the shipped SPQ Checkpoint walk
    instructed `spq_state_machine.py accept_story "$(pwd)" "{WU-ID}"` against a
    CLI that no longer had the verb, so the documented acceptance walk died on
    a usage error. It asserted the mismatch in both directions on purpose --
    fail if the verb came back, fail if the walk stopped naming it -- so that
    whoever resolved it had to say how.

    **It was resolved toward the method, not toward the verb.** SPQ has no
    per-Work-Unit acceptance edge to wrap: `awaiting_acceptance` is a Scrum
    sprint-review toggle that `spq_state_machine.next_action` deliberately does
    not pass, and `SPQ-R16` credits a Work Unit only once an accountable human
    has accepted it as verified -- which the P6 Checkpoint collects per unit
    and the barrier reads off the unit results, dropping any credit naming the
    automated principal. So no verb was restored, and both halves of the
    assertion invert: the CLI must stay free of the verbs, and the walk must
    stay free of them too.
    """
    src = (CORE_LIB / "spq_state_machine.py").read_text(encoding="utf-8")
    for verb in ("accept_story", "reject_story", "transition_story"):
        assert 'verb == "%s"' % verb not in src, (
            "the SPQ CLI grew a %r verb. Re-read ADR-029's #486 amendment and "
            "restore the asymmetry assertion above for this module." % verb
        )
    walk = (
        REPO_ROOT / "plugin-claude" / "skills" / "synaptory" / "spq"
        / "checkpoint.md"
    ).read_text(encoding="utf-8")
    for verb in ("accept_story", "reject_story"):
        assert verb not in walk, (
            "the Checkpoint walk names %r again, and the SPQ CLI has no such "
            "verb -- the walk would die on a usage error. Acceptance on this "
            "lifecycle is a named human recorded at Checkpoint and read by the "
            "barrier; see spq/checkpoint.md step 3." % verb
        )


def test_the_bare_transition_verb_still_refuses_the_acceptance_edge():
    """Why `TRANSITION_RECEIPT`'s row is kept rather than deleted.

    Impossible after this: someone deleting the row as "dead" and thereby
    opening `transition_story awaiting_acceptance done` to any caller.
    """
    assert ak.TRANSITION_RECEIPT[ACCEPTANCE_EDGE] == ("project-owner", "po")
    assert ACCEPTANCE_EDGE in sp._RECEIPT_GATED_EDGES
    refusal = sp._cli_receipt_gated_refusal(
        *ACCEPTANCE_EDGE, "WU-1", forced=False, reason=None
    )
    assert refusal and "receipt-gated" in refusal
    assert "kept rather than deleted" in _amendment()


def test_accept_story_writes_the_edge_without_entering_the_kernel_ledger(tmp_path):
    """The exception, exercised: `done` with no consumed-receipt digest.

    Impossible after this: acceptance quietly acquiring the kernel's ledger
    (or quietly losing the verdict) with the ADR still describing the old
    shape.
    """
    project = _project(tmp_path)
    state = _walked_unit(project)
    sp.accept_story(state, "WU-1", "po@h3t.co", project_dir=str(project))
    story = sp._find_story(state, "WU-1")
    assert story["state"] == "done"
    assert not story.get("mcp_consumed_receipts"), (
        "the acceptance edge now records a consumed-receipt digest, so it may "
        "have been routed through the kernel; re-read ADR-029's #486 amendment"
    )
    verdicts = _verdicts(state)
    assert len(verdicts) == 1
    assert verdicts[0]["evidence_class"] == "judged"
    assert verdicts[0]["check_id"] == "po_acceptance"
    assert "never reaches `evaluate_advance`" in _amendment(), (
        "the amendment no longer states the ADR's central factual claim about "
        "this edge"
    )


def test_a_second_acceptance_in_a_row_is_refused_by_the_state_guard(tmp_path):
    """Anti-replay question 1, answered by running it.

    Impossible: accepting an already-accepted Work Unit. `done` is terminal in
    `VALID_TRANSITIONS` and `accept_story` checks the current state first.
    """
    project = _project(tmp_path)
    state = _walked_unit(project)
    sp.accept_story(state, "WU-1", "po@h3t.co", project_dir=str(project))
    with pytest.raises(ValueError, match="not awaiting acceptance"):
        sp.accept_story(state, "WU-1", "attacker@h3t.co", project_dir=str(project))
    assert len(_verdicts(state)) == 1, "the refused call still recorded a verdict"
    assert sp.VALID_TRANSITIONS["done"] == []


def test_accepting_a_unit_that_is_not_awaiting_acceptance_records_nothing(tmp_path):
    """Anti-replay question 2. The refusal precedes the verdict, not follows it.

    Impossible: minting a judged verdict for a Work Unit still in flight by
    calling the acceptance verb early.
    """
    project = _project(tmp_path)
    state = _walked_unit(project)
    sp._find_story(state, "WU-1")["state"] = "in_progress"
    with pytest.raises(ValueError, match="not awaiting acceptance"):
        sp.accept_story(state, "WU-1", "po@h3t.co", project_dir=str(project))
    assert not _verdicts(state)


def test_the_replay_walk_is_reachable_and_leaves_the_verdict_uncredited(tmp_path):
    """Anti-replay question 3, and the declared gap the amendment records.

    Four public verbs, no `--force-recovery`, no new receipt, and a Work Unit
    the PO REJECTED reaches `done`. `record_judged_verdict` gets the answer
    right -- the second verdict is appended `evidence_class: None` with the
    immutability reason -- and `accept_story` transitions regardless.

    **What this asserts is impossible is the gap being closed silently.** If
    the transition ever starts refusing an uncredited verdict, this test fails
    and the ADR's "declared gap" paragraph must be rewritten as a closed one.
    It is written to fail in BOTH directions for that reason.
    """
    project = _project(tmp_path)
    state = _walked_unit(project)

    # Step 1: reject. Verdict 1, credited.
    sp.reject_story(state, "WU-1", "needs-fix", "not good", "po@h3t.co",
                    project_dir=str(project))
    assert sp._find_story(state, "WU-1")["state"] == "in_progress"
    assert _verdicts(state)[0]["evidence_class"] == "judged"

    # Steps 2 and 3: two edges that no receipt gate covers, so the public CLI
    # verb permits them. Derived from the real tables, not asserted by hand.
    for edge in (("in_progress", "blocked"), ("blocked", "awaiting_acceptance")):
        assert edge[1] in sp.VALID_TRANSITIONS[edge[0]], "%s is not a legal edge" % (edge,)
        assert edge not in sp._RECEIPT_GATED_EDGES
        assert sp._cli_receipt_gated_refusal(
            *edge, "WU-1", forced=False, reason=None
        ) is None, "%s became receipt-gated; the replay walk may be closed" % (edge,)
        sp.transition_story(state, "WU-1", edge[1], reason="r",
                            project_dir=str(project))

    # Step 4: accept, on evidence that has not changed by one byte. REFUSED
    # since the #592 re-review: crediting this verdict would let it overrule
    # the credited rejection recorded at step 1.
    with pytest.raises(ValueError, match="could not be credited"):
        sp.accept_story(state, "WU-1", "po@h3t.co", project_dir=str(project))

    verdicts = _verdicts(state)
    assert len(verdicts) == 1, (
        "the refused acceptance appended a verdict. Since #592's re-review the "
        "refusal happens BEFORE anything is written, so only the credited "
        "rejection from step 1 may be on the ledger; %r" % (verdicts,)
    )
    assert verdicts[0]["verdict"] == "rejected"
    assert verdicts[0]["evidence_class"] == "judged"
    # The reason the replay is refused, asked without writing. Asked through
    # the record path itself, because a second way of asking is what #592's
    # re-review found disagreeing with it.
    prepared = sp.record_judged_verdict(
        sp._find_story(state, "WU-1"), "WU-1", verdict="accepted",
        principal="po@h3t.co", project_dir=str(project), commit=False,
    )
    assert prepared["unbacked_code"] == "already_judged"
    assert "immutable once recorded" in prepared["unbacked_reason"]
    assert sp._find_story(state, "WU-1")["state"] == "awaiting_acceptance", (
        "the Work Unit advanced on an uncredited verdict. That reopens the gap "
        "the #592 re-review closed: `done` would again mean an uncredited "
        "verdict overruled a credited rejection."
    )
    # The refusal happens in memory, before either wrapper writes. A caller
    # that ignores the raise and writes anyway is a different defect; what this
    # pins is that `accept_story` itself does not advance the unit.
    assert "refused before any write" in _amendment(), (
        "ADR-029's amendment no longer records the transition rule this test "
        "enforces; a rule nobody can read is not a declared one"
    )


def test_a_replayed_acceptance_is_refused_and_emits_no_approval(tmp_path, monkeypatch):
    """Both halves of #592's finding 1, asserted together.

    The emission was corrected first (#599): an uncredited verdict shipped
    `evidence_dod / approved` with `decided_by` and no reason, which the control
    plane counts as a HUMAN approval, so the plugin computed "unbacked" and
    shipped "approved".

    The transition followed in the re-review. Suppressing the row alone would
    have fixed the analytics lie and left the workflow-state lie: `done` would
    still have meant that an uncredited verdict overruled a credited rejection
    while the evidence ledger said it could not.

    Impossible now, and each is asserted so neither half can regress alone:
    a replayed acceptance reaching `done`, and a replayed acceptance producing
    an approval row.

    The no-candidate case is refused too, and has been since #602. This
    docstring described the #601 narrowing that #602 reversed, three rounds
    after it stopped being true, sitting immediately above
    `test_an_acceptance_with_no_candidate_is_refused_too`.

    RUN THROUGH THE SCRUM WRAPPER SINCE #644, which removed the SPQ one. Both
    halves asserted here are WRAPPER-level -- what reaches the gate emitter,
    and what reaches disk -- so they need a wrapper, and scrum is the one that
    still exists. The refusal itself is `story_pipeline`'s and is shared, so
    nothing about the property is scrum-specific;
    `test_scrum_and_spq_share_one_acceptance_implementation` is what says the
    two cannot diverge, and
    `test_the_spq_cli_names_no_per_unit_acceptance_verb` is where the absence
    of an SPQ wrapper is recorded, and why it is now a design rather than a
    defect.
    """
    import gate_emitter

    calls: list[dict] = []
    monkeypatch.setattr(
        gate_emitter, "emit_gate_event", lambda **kw: calls.append(kw) or True
    )

    project = _project(tmp_path, build_mode="scrum")
    state = _walked_unit(project)
    state["build_mode"] = "scrum"
    state["lifecycle_state"] = "SPRINT_REVIEW"
    (project / ".synaptory" / ".orchestrator" / "pipeline-state.json").write_text(
        json.dumps(state), encoding="utf-8"
    )
    scrum.accept_story(str(project), "WU-1", "po@h3t.co")

    reopened = scrum.read_state(str(project))
    sp._find_story(reopened, "WU-1")["state"] = "awaiting_acceptance"
    (project / ".synaptory" / ".orchestrator" / "pipeline-state.json").write_text(
        json.dumps(reopened), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="could not be credited"):
        scrum.accept_story(str(project), "WU-1", "someone-else@h3t.co")

    approvals = [c for c in calls if c.get("state") == "approved"]
    assert len(approvals) == 1, (
        "only the first, credited acceptance may emit an approval; %r" % (calls,)
    )
    assert approvals[0]["decided_by"] == "po@h3t.co"
    assert not [c for c in calls if c.get("decided_by") == "someone-else@h3t.co"], (
        "the refused acceptance emitted a gate row; a refusal is not a decision"
    )

    final = scrum.read_state(str(project))
    assert sp._find_story(final, "WU-1")["state"] == "awaiting_acceptance", (
        "the refused acceptance was written to disk anyway"
    )
    assert len(_verdicts(final)) == 1, (
        "the refused attempt left a verdict behind; the refusal is supposed to "
        "discard the in-memory state with it"
    )
    assert "refused before any write" in _amendment()


def test_an_acceptance_with_no_candidate_is_refused_too(tmp_path):
    """The narrowing #601 made, REVERSED by the #592 re-review.

    #601 exempted `no_candidate` from the refusal because refusing it broke 20
    tests, one of them named `test_a_barren_unit_is_unbacked_exactly_as_before`.
    That reasoning was wrong and the reviewer said so precisely: those tests
    reach `accept_story` through a helper to OBTAIN a verdict item and assert
    only that the item is unbacked. None asserts that the Work Unit may reach
    `done`. Fixtures breaking under a fail-closed check is coupling, not
    product evidence.

    What the exemption actually left was a state and its signal disagreeing:
    the board said `done` while the gate said the acceptance was unbacked.

    Impossible now: completing a Work Unit on a verdict that names no subject.
    """
    project = _project(tmp_path)
    state = _walked_unit(project)
    story = sp._find_story(state, "WU-1")
    story.pop("mcp_consumed_receipts", None)
    for key in list(story):
        if "receipt" in key:
            story.pop(key, None)
    receipts = project / ".synaptory" / ".orchestrator" / "receipts"
    if receipts.exists():
        for f in receipts.iterdir():
            f.unlink()

    with pytest.raises(ValueError, match="could not be credited"):
        sp.accept_story(state, "WU-1", "po@h3t.co", project_dir=str(project))

    assert sp._find_story(state, "WU-1")["state"] == "awaiting_acceptance"


def test_both_uncredited_codes_are_refused_by_one_rule(tmp_path):
    """Asserts impossible: one uncredited case being exempted again.

    The refusal keys on the PRESENCE of `unbacked_code`, not on its value, so
    a third uncredited reason added later is refused by default rather than by
    someone remembering to extend a list. This test fails if the check goes
    back to naming individual codes.
    """
    import inspect

    source = inspect.getsource(sp.accept_story)
    assert "commit=False" in source, (
        "the verdict is no longer PREPARED before it is judged, so the item "
        "checked and the item stored can come from different reads"
    )
    assert "commit_judged_verdict(story, item" in source, (
        "the committed verdict is not the item that was judged"
    )
    assert 'item.get("unbacked_code")' in source
    assert '== "already_judged"' not in source, (
        "the refusal names a single code again, so a future uncredited reason "
        "would be exempt by default rather than refused by default"
    )


def test_a_refused_acceptance_writes_nothing_at_all(tmp_path):
    """Asserts impossible: a refusal leaving a trace at ANY side-effect boundary.

    #602 asked whether the verdict could be credited by RECORDING it and then
    reading the answer, so a refused acceptance had already set `accepted_by`,
    appended the verdict, and persisted a `story_judged` event carrying
    `verdict: accepted` before raising. A caller who caught the error held a
    mutated story and the durable log claimed an acceptance had been judged,
    while ADR-029 said refusal left "no state change, no verdict and no gate
    row". The document was right about the intent and wrong about the code.

    Three boundaries are checked here rather than one, because the failure was
    that only the persisted board had been considered:

    1. the caller's in-memory state dict, compared whole;
    2. the durable event log on disk;
    3. the gate emitter.
    """
    import gate_emitter

    calls: list[dict] = []
    monkeypatch_target = getattr(gate_emitter, "emit_gate_event")
    gate_emitter.emit_gate_event = lambda **kw: calls.append(kw) or True
    try:
        project = _project(tmp_path)
        state = _walked_unit(project)
        story = sp._find_story(state, "WU-1")
        story.pop("mcp_consumed_receipts", None)
        for key in list(story):
            if "receipt" in key:
                story.pop(key, None)
        receipts = project / ".synaptory" / ".orchestrator" / "receipts"
        if receipts.exists():
            for f in receipts.iterdir():
                f.unlink()

        events = project / ".synaptory" / ".orchestrator" / "events.jsonl"
        events_before = events.read_text(encoding="utf-8") if events.exists() else None
        state_before = json.dumps(state, sort_keys=True)

        with pytest.raises(ValueError, match="could not be credited"):
            sp.accept_story(state, "WU-1", "po@h3t.co", project_dir=str(project))

        assert json.dumps(state, sort_keys=True) == state_before, (
            "the refused acceptance mutated the caller's state dict"
        )
        events_after = events.read_text(encoding="utf-8") if events.exists() else None
        assert events_after == events_before, (
            "the refused acceptance appended to the durable event log"
        )
        assert calls == [], "the refused acceptance emitted a gate row"
    finally:
        gate_emitter.emit_gate_event = monkeypatch_target


def test_the_candidate_is_derived_once_and_that_snapshot_is_recorded(tmp_path):
    """Asserts impossible: the check and the record describing different moments.

    #603 asked "can this be credited" with its own read and then let
    `record_judged_verdict` take another. A candidate that changed in between
    passed the check and was stored uncredited while the unit advanced.
    Reproduced on that head: three derivations, `evidence_class: null`,
    `state: done`.

    The window is closed by removing the second read, not by locking. The
    verdict is prepared from one snapshot, judged as that exact object, and
    committed unchanged. Counting the derivations is how this test proves there
    is no window rather than asserting the outcome of one particular race.
    """
    project = _project(tmp_path)
    state = _walked_unit(project)

    real = sp._story_candidate_digest
    calls = {"n": 0}

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    sp._story_candidate_digest = counting
    try:
        sp.accept_story(state, "WU-1", "po@h3t.co", project_dir=str(project))
    finally:
        sp._story_candidate_digest = real

    assert calls["n"] == 1, (
        "the candidate was derived %d times, so a change between reads can "
        "again make the check and the record disagree" % calls["n"]
    )
    stored = _verdicts(state)[-1]
    assert stored["evidence_class"] == "judged"
    assert stored["candidate_digest"]


def test_a_candidate_that_vanishes_after_the_read_cannot_reach_done_uncredited(
    tmp_path,
):
    """The race the reviewer reproduced, asserted as a property.

    Whatever the candidate is at the single read is what gets judged AND what
    gets recorded. So there is no arrangement of a changing candidate that
    yields a stored verdict with no digest on a unit that reached `done`: the
    two cannot come from different moments any more.
    """
    project = _project(tmp_path)
    state = _walked_unit(project)

    real = sp._story_candidate_digest
    calls = {"n": 0}

    def vanishing(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return real(*a, **kw)
        return (None, "the receipts vanished between reads", None)

    sp._story_candidate_digest = vanishing
    try:
        sp.accept_story(state, "WU-1", "po@h3t.co", project_dir=str(project))
    finally:
        sp._story_candidate_digest = real

    story = sp._find_story(state, "WU-1")
    stored = _verdicts(state)[-1]
    if story["state"] == "done":
        assert stored["evidence_class"] == "judged", (
            "a unit reached `done` carrying an uncredited verdict: %r" % stored
        )
        assert stored.get("unbacked_code") is None
        assert stored["candidate_digest"], (
            "a unit reached `done` on a verdict with no candidate digest"
        )


def test_only_one_public_answer_to_whether_a_verdict_is_creditable(tmp_path):
    """Asserts impossible: a second public way to ask, which is how they drift.

    #604 added a public `uncreditable_verdict` that derived its own candidate.
    Its optional `intake` argument defaulted to `None`, which in
    `_story_candidate_digest` means "intake was already consulted and there is
    none" rather than "read it now", so it skipped an admitted external
    candidate and fell through to receipt bytes. On a SUBSTITUTED artifact it
    reported creditable while the record path refused. Two answers about one
    fact, disagreeing exactly where it mattered.

    The fix was deletion, not a better default. `record_judged_verdict(...,
    commit=False)` is the only way to ask, and it cannot disagree with the
    record path because it IS the record path.

    This fails if a second public answerer comes back.
    """
    public = [
        name
        for name in dir(sp)
        if not name.startswith("_")
        and callable(getattr(sp, name, None))
        and ("uncreditable" in name or "creditable" in name)
    ]
    assert public == [], (
        "a public creditability predicate is back: %r. Anything that answers "
        "this question by deriving its own candidate will drift from the "
        "record path; ask `record_judged_verdict(..., commit=False)` instead."
        % public
    )

    # And the one way to ask still answers, so this is not vacuous.
    project = _project(tmp_path)
    state = _walked_unit(project)
    prepared = sp.record_judged_verdict(
        sp._find_story(state, "WU-1"), "WU-1", verdict="accepted",
        principal="po@h3t.co", project_dir=str(project), commit=False,
    )
    assert prepared["evidence_class"] == "judged"
    assert prepared.get("unbacked_code") is None


# ── the forgeability line, and what it does and does not refuse ──────────────


def test_the_principal_is_the_only_caller_supplied_field(tmp_path):
    """The forgery a verdict could attempt, and the line that refuses it.

    Two verdicts over the same receipts by two different principals bind to the
    SAME candidate digest, so the untrusted field cannot move the subject. That
    is the forgery being refused, not a malformed input being rejected: both
    calls are perfectly well-formed and both are recorded.

    Impossible: a verdict choosing what it is a verdict about.
    """
    a = _project(tmp_path / "a")
    b = _project(tmp_path / "b")
    digests = []
    for project, principal in ((a, "po@h3t.co"), (b, "nobody@evil.example")):
        state = _walked_unit(project)
        sp.accept_story(state, "WU-1", principal, project_dir=str(project))
        item = _verdicts(state)[0]
        assert item["principal"] == principal, "the principal is recorded verbatim"
        digests.append(item["candidate_digest"])
    assert digests[0] == digests[1] and digests[0].startswith("sha256:")


def test_independence_fails_closed_rather_than_being_claimed(tmp_path):
    """Receipts record no human producer, so independence is unearned here.

    Impossible: `independent_of_producer: true` on a story whose producer set
    is empty. False is a valid answer and is recorded with its basis.
    """
    project = _project(tmp_path)
    state = _walked_unit(project)
    sp.accept_story(state, "WU-1", "po@h3t.co", project_dir=str(project))
    item = _verdicts(state)[0]
    assert item["independent_of_producer"] is False
    assert "recorded false rather than assumed true" in item["independence_basis"]


# ── one rule across three lifecycles ─────────────────────────────────────────


def test_scrum_and_spq_share_one_acceptance_implementation():
    """The rule is one rule because it is one function.

    Impossible: two acceptance implementations acquiring different discipline
    without this failing.

    SPQ HAS NO WRAPPER TO DELEGATE, since #644, and the assertion is inverted
    rather than dropped. "Scrum and SPQ are the same code" held because both
    wrappers held the same `story_pipeline.accept_story` object; the SPQ
    wrapper is gone, so what has to be asserted now is that its absence was
    not filled by a SECOND implementation. One function or none -- never two.
    See `test_the_spq_cli_names_no_per_unit_acceptance_verb` for why "none" is
    the design on this lifecycle: there is no per-unit acceptance edge to wrap.
    """
    assert scrum._sp_accept_story is sp.accept_story
    for name in ("accept_story", "_sp_accept_story"):
        candidate = getattr(spq, name, None)
        assert candidate in (None, sp.accept_story), (
            "spq_state_machine.%s is neither absent nor "
            "story_pipeline.accept_story, so acceptance has two "
            "implementations and the rule is no longer one rule" % name
        )
    assert "Scrum and SPQ are the same code" in _amendment()


def test_scrum_acceptance_has_the_same_guard_and_the_same_verdict(tmp_path):
    """Verified rather than assumed, since the wrappers are separate files."""
    project = _project(tmp_path, build_mode="scrum")
    state = _walked_unit(project, "US-1")
    state["build_mode"] = "scrum"
    state["lifecycle_state"] = "SPRINT_REVIEW"
    (project / ".synaptory" / ".orchestrator" / "pipeline-state.json").write_text(
        json.dumps(state), encoding="utf-8"
    )
    scrum.accept_story(str(project), "US-1", "po@h3t.co")
    after = scrum.read_state(str(project))
    assert sp._find_story(after, "US-1")["state"] == "done"
    assert _verdicts(after, "US-1")[0]["evidence_class"] == "judged"
    with pytest.raises(ValueError, match="not awaiting acceptance"):
        scrum.accept_story(str(project), "US-1", "attacker@h3t.co")


def test_kanban_has_no_acceptance_edge_at_all(tmp_path):
    """The third lifecycle is not an exception to the rule; it has no edge.

    Impossible: Kanban acquiring a PO acceptance step while the amendment still
    says it has none. Three independent signals, so removing any one of them
    does not silently pass.
    """
    assert not hasattr(kb, "accept_story")
    src = (CORE_LIB / "kanban_state_machine.py").read_text(encoding="utf-8")
    assert "accept_story" not in src
    assert "awaiting_acceptance" not in src

    project = _project(tmp_path, build_mode="kanban")
    for abbrev, role, stage in (
        ("se", "software-engineer", "se-implementation"),
        ("qe", "quality-engineer", "qe-verification"),
        ("cr", "code-reviewer", "cr-review"),
    ):
        _receipt(project, "T-1", abbrev, role, stage)
    state = {
        "version": "2.0", "build_mode": "kanban", "cumulative_ticket_number": 1,
        "current_stories": [{
            "id": "T-1", "title": "t", "state": "reviewing",
            "pipeline_log": [{"state": "reviewing", "entered_at": ENTERED,
                              "exited_at": None}],
            "receipts": [], "retries": {},
        }],
    }
    # The toggle IS on in this project, and Kanban still does not redirect.
    assert sp.is_per_story_acceptance_enabled(str(project))
    to_state, _, dod = ak.resolve_done_edge(
        str(project), json.loads(json.dumps(state)), "T-1", "done", None,
        mode="kanban",
    )
    assert (dod or {}).get("passed") is True, "the fixture did not reach the redirect"
    assert to_state == "done", (
        "Kanban now redirects to awaiting_acceptance, so it has an acceptance "
        "edge; ADR-029's #486 amendment says it has none"
    )
    assert re.search(r"\|\s*Kanban\s*\|\s*\*\*none\*\*", _amendment()), (
        "the amendment's lifecycle table no longer records Kanban as having no "
        "acceptance edge"
    )
