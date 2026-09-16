"""Layer 1 — the Cycle dependency ledger (#303 §3/§4, migrated for #640/#644).

Without this, a `depends_on` edge declaring anything stronger than `done` can
never resolve: the #304 gate refuses it forever, which is safe but useless.
These tests pin the property that makes it useful AND still safe:

    a recorded event can unblock a dependent Work Unit, but only when the
    consumer can actually check the claim.

The last clause is the whole design. `spq.sync.accept_unverified_events`
defaults to false, so a claim-only condition fails closed. That has a real cost
-- a plain-string `depends_on` becomes usable only for board state, and the PO
must name a verifiable condition -- and it is the only way "an upstream event
safely unblocks downstream work" is true rather than aspirational.

WHAT THE WORKSTREAM'S RETIREMENT CHANGED HERE, and what it did not
-------------------------------------------------------------------------------
`SPD-194` retires the Workstream, so there is no second lane: one Cycle, one
Crew, one board, one events directory, one integration ref. Three properties in
this file had a lane as their SUBJECT and are gone with it, deleted rather than
left passing vacuously -- a lane publishing about another lane's unit, per-lane
events directories, and a lane hydrating a slice of the board.

Everything else survives with a different owner. "A stronger condition than
`done` is a ledger fact, not a board fact" used to be the cross-lane branch's
job and is now the only branch there is, which makes it MORE load-bearing rather
than less: every admitted unit is local now, so if the local branch trusted
board state the whole mechanism would be decorative.
"""

from __future__ import annotations


import json
import subprocess
from pathlib import Path

import pytest

import cycle_records as cr
import spq_ledger as lg
import spq_paths as sp
import spq_state_machine as sm
import story_pipeline as story

from _spq_fixture import CYCLE_KWARGS, unit as _fx_unit


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )


@pytest.fixture
def cycle(tmp_path: Path, monkeypatch):
    """A real repo with one Cycle open, two admitted units and a sealed
    declaration. WU-DOWN waits on WU-UP for `integrated`."""
    project = tmp_path / "proj"
    project.mkdir()
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "t@e.co")
    _git(project, "config", "user.name", "t")
    (project / "f.txt").write_text("x", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")
    (project / ".synaptory.yaml").write_text("build_mode: spq\n", encoding="utf-8")
    monkeypatch.delenv("SYNAPTORY_ACTIVE_SPEC", raising=False)

    sm.initialize(str(project))
    sm.approve_baseline(str(project), approved_by="t", baseline_ref="baseline-1",
        calibration={"sample_units": 1, "measured_hours": 1})
    sm.open_cycle(str(project), goal="cycle 1", admitted_units=[
            _fx_unit("WU-UP", title="upstream", outputs=[{"kind": "contract", "id": "contracts/auth"}]),
            _fx_unit("WU-DOWN", title="downstream", depends_on=[{"unit_id": "WU-UP", "condition": "integrated"}]),
        ], **CYCLE_KWARGS)
    cycle_id = sm.identity(str(project)).cycle_id
    # The integration ref must exist: `integrated` is verified by ancestry
    # against it, and a missing ref is correctly a refusal rather than a pass.
    _git(project, "branch", sp.integration_branch(cycle_id))
    return project, cycle_id


def _unit(project: Path, unit_id: str) -> dict:
    return next(
        u for u in sm.read_state(str(project))["current_stories"]
        if u["id"] == unit_id
    )


def _set_state(project: Path, unit_id: str, value: str) -> None:
    """Move a unit on the board without going through the DoD gate.

    A fixture concern, not a shortcut under test: these tests are about what
    the LEDGER proves, and driving a full SE/QE/CR pipeline to reach `done`
    would make every one of them a test of the gate instead.
    """
    state = sm.read_state(str(project))
    next(u for u in state["current_stories"] if u["id"] == unit_id)["state"] = value
    sm._write_state(str(project), state)


def _dep_verdict(project: Path, unit_id: str) -> dict:
    state = sm.read_state(str(project))
    unit = next(u for u in state["current_stories"] if u["id"] == unit_id)
    return story.story_dep_status(
        state, unit, dep_context=story.dep_context(str(project), state)
    )


# ── the headline property ──────────────────────────────────────────────────


def test_a_conditioned_edge_is_blocked_until_the_event_exists(cycle):
    """Before the ledger existed this could NEVER resolve.

    Two distinct unsatisfied states, and the distinction is worth keeping:
    "we have never looked" (no cache) versus "we looked and there is nothing"
    (refreshed, no event). Both fail closed; only the second means chasing the
    producer rather than running a refresh.

    The refusal no longer names an owning workstream, because there is no
    second owner to name: `SPD-194` leaves a unit owned by the Cycle that
    admitted it, and `dep_context` reports an EMPTY owner map rather than an
    absent one so a resolver reads "no other owner" instead of "unknown".
    """
    project, cycle_id = cycle

    # Never refreshed: we genuinely do not know, and the detail says so.
    never_looked = _dep_verdict(project, "WU-DOWN")["unmet"][0]
    assert never_looked["reason_code"] == story.DEP_LEDGER_STALE
    assert "refresh_ledger" in never_looked["detail"]

    # Refreshed with nothing published: now the answer is about the producer.
    manifest = sm.read_manifest(str(project), cycle_id)
    lg.refresh(str(project), cycle_id, manifest=manifest, fetch=False)
    looked = _dep_verdict(project, "WU-DOWN")["unmet"][0]
    assert looked["reason_code"] == story.DEP_CONDITION_UNVERIFIED
    assert "no such event is recorded" in looked["detail"]


def test_a_verified_integration_event_unblocks_the_downstream_unit(cycle):
    """#303's whole point: a recorded, VERIFIED event unblocks a dependent Work
    Unit, where board state alone never could."""
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    head = _git(project, "rev-parse", "HEAD").stdout.strip()

    lg.publish(
        str(project), cycle_id=cycle_id, manifest=manifest,
        unit_id="WU-UP", condition="integrated", commit_sha=head,
    )
    lg.refresh(str(project), cycle_id, manifest=manifest, fetch=False)

    assert _dep_verdict(project, "WU-DOWN")["met"] is True


def test_next_action_dispatches_the_downstream_unit_once_unblocked(cycle):
    """End to end through the shipped path, not just the resolver."""
    project, cycle_id = cycle
    # WU-UP is finished on the board; the edge still holds WU-DOWN, because
    # `integrated` is not something the board can answer.
    _set_state(project, "WU-UP", "done")
    state = sm.read_state(str(project))
    context = story.dep_context(str(project), state)
    assert context["story_manifest_hash"] == state["manifest_hash"]
    assert context["manifest_hash"] == state["manifest_hash"]
    before = sm.next_action(str(project))
    assert before["action"] == "deps_blocked", before
    assert [h["story_id"] for h in before["dependencies_held"]] == ["WU-DOWN"]

    manifest = sm.read_manifest(str(project), cycle_id)
    head = _git(project, "rev-parse", "HEAD").stdout.strip()
    lg.publish(
        str(project), cycle_id=cycle_id, manifest=manifest,
        unit_id="WU-UP", condition="integrated", commit_sha=head,
    )
    lg.refresh(str(project), cycle_id, manifest=manifest, fetch=False)

    after = sm.next_action(str(project))
    assert after["action"] == "dispatch_se"
    assert after["story_id"] == "WU-DOWN"


# ── an event is a claim, not proof ─────────────────────────────────────────


def test_integrated_refuses_a_sha_that_is_not_on_the_integration_ref(cycle):
    """Publishing `integrated` for work that is not actually integrated is the
    single most damaging false claim available, so it is checked at publish
    time on the producer side where the ref is available."""
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    with pytest.raises(lg.LedgerError, match="not an ancestor"):
        lg.publish(
            str(project), cycle_id=cycle_id, manifest=manifest,
            unit_id="WU-UP", condition="integrated", commit_sha="0" * 40,
        )


def test_integrated_requires_a_sha(cycle):
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    with pytest.raises(lg.LedgerError, match="requires --sha"):
        lg.publish(
            str(project), cycle_id=cycle_id, manifest=manifest,
            unit_id="WU-UP", condition="integrated",
        )


def test_the_integration_ref_has_one_definition(cycle):
    """The ref an `integrated` claim is checked against is derived, not read
    off the declaration.

    `cycle_records` seals no `integration_ref`: the per-lane branches the
    predecessor's manifest enumerated are gone. Reading the absent key returned
    `""`, which makes `git merge-base --is-ancestor <sha> ''` fail and reads as
    "not integrated" for work that is -- a fail-closed that never opens. An
    explicit ref on the declaration still wins, so a project that pins one is
    not overridden by the default.
    """
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    assert "integration_ref" not in manifest, "the declaration seals no ref"
    assert lg.integration_ref(manifest) == sp.integration_branch(cycle_id)
    assert lg.integration_ref(dict(manifest, integration_ref="refs/heads/x")) == (
        "refs/heads/x"
    )


def _incremental_attestation(project: Path, manifest: dict, candidate: str) -> dict:
    manifest["integration_policy"] = {
        "mode": "incremental",
        "merge_requires": ["regression_green"],
    }
    body = {k: v for k, v in manifest.items() if k != cr.HASH_FIELD}
    manifest[cr.HASH_FIELD] = cr.compute_hash(body)
    return lg.build_incremental_evaluation(
        cycle_id=manifest["cycle_id"],
        manifest_hash=manifest[cr.HASH_FIELD],
        unit_id="WU-UP",
        candidate_sha=candidate,
        evaluated_head=candidate,
        integration_ref=lg.integration_ref(manifest),
        checks={"regression_green": {"passed": True, "required": True}},
        evaluated_at="2026-08-27T00:00:00Z",
    )


def test_incremental_integrated_requires_the_tree_bound_evaluation(cycle):
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    candidate = _git(project, "rev-parse", "HEAD").stdout.strip()
    attestation = _incremental_attestation(project, manifest, candidate)

    out = lg.publish(
        str(project), cycle_id=cycle_id, manifest=manifest,
        unit_id="WU-UP", condition="integrated",
        commit_sha=candidate, evaluation=attestation,
    )
    verification = out["event"]["verification"]
    assert verification["verified"] is True
    assert verification["attestation_id"] == attestation["attestation_id"]
    assert verification["evaluation"] == attestation


def test_incremental_evaluation_is_invalid_after_the_integration_ref_moves(cycle):
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    candidate = _git(project, "rev-parse", "HEAD").stdout.strip()
    attestation = _incremental_attestation(project, manifest, candidate)

    (project / "after-evaluation.txt").write_text("changed\n", encoding="utf-8")
    _git(project, "add", "after-evaluation.txt")
    _git(project, "commit", "-qm", "change integration after evaluation")
    _git(project, "branch", "-f", lg.integration_ref(manifest), "HEAD")

    with pytest.raises(lg.LedgerError, match="re-run evaluate-incremental"):
        lg.publish(
            str(project), cycle_id=cycle_id, manifest=manifest,
            unit_id="WU-UP", condition="integrated",
            commit_sha=candidate, evaluation=attestation,
        )


def test_contract_published_requires_a_digest(cycle):
    """Without one the event asserts that something was published but not
    WHAT, which cannot be checked by anyone."""
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    with pytest.raises(lg.LedgerError, match="requires an output digest"):
        lg.publish(
            str(project), cycle_id=cycle_id, manifest=manifest,
            unit_id="WU-UP", condition="contract_published",
            output={"kind": "contract", "id": "contracts/auth"},
        )


def test_a_claim_only_condition_is_refused_by_default(cycle):
    """`done` is the producer's own claim about itself, so it must not silently
    license a dispatch that declared something stronger."""
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    lg.publish(
        str(project), cycle_id=cycle_id, manifest=manifest,
        unit_id="WU-UP", condition="done",
    )
    lg.refresh(str(project), cycle_id, manifest=manifest, fetch=False)
    verdict = _dep_verdict(project, "WU-DOWN")
    assert verdict["met"] is False


def test_verify_condition_reports_claim_only_as_none_not_false(cycle):
    """None means 'cannot check', which is different from 'checked and wrong'.
    Collapsing them would make an honest unverifiable event look like a lie."""
    project, _cycle_id = cycle
    verdict = lg.verify_condition(
        str(project), manifest={}, unit_id="U", condition="environment_ready"
    )
    assert verdict["verified"] is None


# ── admission is the ownership check ───────────────────────────────────────


def test_publishing_about_an_unadmitted_unit_is_refused(cycle):
    """The surviving half of the old owner check.

    The predecessor also refused a publish about "another lane's Work Unit".
    That guard's subject is gone: `SPD-194` leaves a unit owned by the Cycle
    that admitted it, so admission IS the ownership question and a second
    check would compare an id against a lane that does not exist.

    This is also the arm that caught the re-point defect: `spq_ledger` asked
    the retired `spq_manifest.owners`, which reads `work_units` and therefore
    answered `{}` for a sealed declaration -- so every publish refused with
    "not admitted" for a unit the Cycle had admitted.
    """
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    assert lg.admitted_ids(manifest) == ["WU-UP", "WU-DOWN"]
    with pytest.raises(lg.LedgerError, match="not admitted"):
        lg.publish(
            str(project), cycle_id=cycle_id, manifest=manifest,
            unit_id="GHOST", condition="done",
        )


def test_cycle_integrated_is_no_longer_a_condition_at_all(cycle):
    """It was the CROSS-Cycle condition, owned by the Coordination Cycle.

    `SPD-194` retires that Cycle: work that cannot be ordered cannot be split,
    so it is co-admitted to one Cycle and there is no second Cycle to wait on.
    The refusal stays -- stronger than before, since the condition is now
    unknown rather than merely unpublishable -- and it names the retirement so
    a caller carrying the old vocabulary learns why from the message.
    """
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    assert "cycle_integrated" not in cr.DEP_CONDITIONS
    with pytest.raises(lg.LedgerError, match="retired with the Coordination Cycle"):
        lg.publish(
            str(project), cycle_id=cycle_id, manifest=manifest,
            unit_id="WU-UP", condition="cycle_integrated",
        )


# ── event identity and selection ───────────────────────────────────────────


def test_events_are_content_addressed_and_self_deduplicating():
    """Content addressing maps straight onto the control plane's
    client_event_id idempotency index without a second scheme."""
    kwargs = dict(cycle_id="7-abc12345", manifest_hash="sha256:x",
                  unit_id="WU-1", condition="integrated",
                  commit_sha="dead", published_at="t")
    assert lg.build_event(**kwargs)["event_id"] == lg.build_event(**kwargs)["event_id"]
    other = dict(kwargs, commit_sha="beef")
    assert lg.build_event(**kwargs)["event_id"] != lg.build_event(**other)["event_id"]


def test_the_strongest_recorded_condition_wins():
    manifest = {"cycle_id": "7-abc12345", cr.HASH_FIELD: "sha256:x"}
    base = dict(cycle_id="7-abc12345", manifest_hash="sha256:x",
                unit_id="WU-1", published_at="t")
    snap = {"events": [
        lg.build_event(condition="done", **base) | {"verification": {"verified": True}},
        lg.build_event(condition="integrated", **base) | {"verification": {"verified": True}},
    ]}
    assert lg.satisfied_map(snap, manifest)["WU-1"]["condition"] == "integrated"


def test_a_weaker_event_does_not_satisfy_a_stronger_declared_condition(cycle):
    """`done` must not satisfy an edge that declared `integrated`."""
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    lg.publish(
        str(project), cycle_id=cycle_id, manifest=manifest,
        unit_id="WU-UP", condition="done",
    )
    lg.refresh(str(project), cycle_id, manifest=manifest, fetch=False)
    assert _dep_verdict(project, "WU-DOWN")["met"] is False


def test_events_from_another_cycle_or_declaration_revision_are_ignored():
    """An event recorded against a superseded admitted set may describe a unit
    that no longer exists; letting it satisfy an edge resolves against a Cycle
    that is gone.

    The revision is compared against the declaration's OWN hash key. Reading
    the retired `manifest_hash` off a sealed declaration yields `""`, which
    skips the comparison entirely and admits an event from any revision.
    """
    manifest = {"cycle_id": "7-abc12345", cr.HASH_FIELD: "sha256:x"}
    base = dict(unit_id="WU-1", condition="integrated", published_at="t")
    snap = {"events": [
        lg.build_event(cycle_id="9-zzzzzzzz", manifest_hash="sha256:x", **base),
        lg.build_event(cycle_id="7-abc12345", manifest_hash="sha256:OTHER", **base),
    ]}
    assert lg.satisfied_map(snap, manifest) == {}


# ── staleness fails closed ─────────────────────────────────────────────────


def test_a_satisfying_event_still_satisfies_when_the_cache_is_old(cycle):
    """An event is an append-only FACT; its age does not invalidate it.

    A stale cache can only be MISSING newer events, and "not satisfied" is
    already the fail-closed direction -- so staleness affects reporting
    quality, not safety. Blocking here would strand a unit whose upstream
    genuinely did integrate."""
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    head = _git(project, "rev-parse", "HEAD").stdout.strip()
    lg.publish(
        str(project), cycle_id=cycle_id, manifest=manifest,
        unit_id="WU-UP", condition="integrated", commit_sha=head,
    )
    lg.refresh(str(project), cycle_id, manifest=manifest, fetch=False)

    # Age the cache past the ceiling.
    path = Path(sp.ledger_path(str(project), cycle_id))
    cache = json.loads(path.read_text(encoding="utf-8"))
    cache["refreshed_at"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(cache), encoding="utf-8")

    snap = lg.snapshot(str(project), cycle_id)
    assert snap["fresh"] is False
    assert "old" in snap["detail"]

    assert _dep_verdict(project, "WU-DOWN")["met"] is True


def test_a_stale_cache_with_no_event_says_so_in_the_reason(cycle):
    """When the edge is NOT satisfied and the cache is behind, the operator
    needs both facts: that nothing is recorded, and that a refresh might change
    the answer."""
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    lg.refresh(str(project), cycle_id, manifest=manifest, fetch=False)
    path = Path(sp.ledger_path(str(project), cycle_id))
    cache = json.loads(path.read_text(encoding="utf-8"))
    cache["refreshed_at"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(cache), encoding="utf-8")

    unmet = _dep_verdict(project, "WU-DOWN")["unmet"][0]
    assert unmet["reason_code"] == story.DEP_LEDGER_STALE
    assert "refresh_ledger" in unmet["detail"]


def test_a_failed_refresh_marks_the_cache_unfresh(cycle):
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    cache = lg.refresh(str(project), cycle_id, manifest=manifest, fetch=True)
    # No remote is configured, so the fetch fails and must be reported.
    assert cache["refresh_ok"] is False
    assert lg.snapshot(str(project), cycle_id)["fresh"] is False


def test_an_absent_cache_is_unfresh_rather_than_empty_and_trusted(cycle):
    project, cycle_id = cycle
    snap = lg.snapshot(str(project), cycle_id)
    assert snap["fresh"] is False
    assert snap["events"] == []


# ── integration state is an orthogonal axis, not a new enum member ────────


def test_integration_label_derives_the_three_vocabularies():
    assert sm.integration_label({"state": "in_progress"}) == "in_progress"
    assert sm.integration_label({"state": "done"}) == "integration_pending"
    assert sm.integration_label(
        {"state": "done", "integration": {"status": "integrated"}}
    ) == "integrated"


def test_story_states_and_valid_transitions_are_unchanged():
    """The guard on the design choice. Adding `integration_pending` and
    `integrated` as enum members would break every `state == "done"` consumer
    across all three lifecycles."""
    assert "integration_pending" not in story.STORY_STATES
    assert "integrated" not in story.STORY_STATES
    for targets in story.VALID_TRANSITIONS.values():
        assert "integrated" not in targets
        assert "integration_pending" not in targets


def test_publishing_integrated_stamps_the_board(cycle):
    project, cycle_id = cycle
    head = _git(project, "rev-parse", "HEAD").stdout.strip()
    _set_state(project, "WU-UP", "done")
    sm.publish_event(
        str(project), unit_id="WU-UP", condition="integrated", commit_sha=head
    )
    unit = _unit(project, "WU-UP")
    assert unit["integration"]["status"] == "integrated"
    assert unit["integration"]["commit_sha"] == head
    assert unit["integration"]["integration_ref"] == sp.integration_branch(cycle_id)


def test_publishing_integrated_refuses_a_non_done_unit(cycle):
    """An ancestor SHA proves placement, not that the Work Unit passed DoD."""
    project, cycle_id = cycle
    head = _git(project, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(ValueError, match="must be done"):
        sm.publish_event(
            str(project), unit_id="WU-UP", condition="integrated", commit_sha=head
        )

    assert not list(Path(sp.committed_events_dir(
        str(project), cycle_id
    )).glob("*.json")), "a refused publication must not leave an event behind"


# ── operator observability ─────────────────────────────────────────────────


def test_dep_status_names_the_edge_and_the_reason(cycle):
    """#304's AC: an unresolved dependency must be observable to the operator.
    `deps_blocked` says the loop halted; this says which edge and what to do.

    Also the arm that caught the delegation defect: the replacement's
    `dep_status` forwarded to `story_dep_status(project_dir, state, unit_id)`,
    which is not that function's signature, so the verb raised `TypeError` on
    every call and #304's AC had no implementation at all.
    """
    project, _cycle_id = cycle
    report = sm.dep_status(str(project))
    down = next(u for u in report["units"] if u["unit_id"] == "WU-DOWN")
    assert down["dependencies_met"] is False
    assert down["unmet"][0]["reason_code"] in (
        story.DEP_LEDGER_STALE, story.DEP_CONDITION_UNVERIFIED
    )
    assert report["manifest_present"] is True
    assert report["cycle_id"] == _cycle_id


def test_dep_status_distinguishes_a_local_only_event_from_nothing_published(cycle):
    """Without the distinction, "I published it" and "nobody can see it" look
    identical, and the mandatory human push reads as a bug."""
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    head = _git(project, "rev-parse", "HEAD").stdout.strip()

    # Nothing published: no event is pending a push.
    assert sm.dep_status(str(project))["events_pending_push"] == []

    lg.publish(
        str(project), cycle_id=cycle_id, manifest=manifest,
        unit_id="WU-UP", condition="integrated", commit_sha=head,
    )
    report = sm.dep_status(str(project))
    assert "WU-UP" in report["events_pending_push"]
    down = next(u for u in report["units"] if u["unit_id"] == "WU-DOWN")
    assert down["unmet"][0]["reason_code"] == "dep_event_local_only"


def test_publish_prints_the_push_command(cycle):
    """The project's own rules forbid agents pushing, so propagation is a
    human's step. Naming the command is what stops the latency looking like a
    failure."""
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    head = _git(project, "rev-parse", "HEAD").stdout.strip()
    result = lg.publish(
        str(project), cycle_id=cycle_id, manifest=manifest,
        unit_id="WU-UP", condition="integrated", commit_sha=head,
    )
    assert "git push" in result["push_command"]
    assert ".synaptory/cycles" in result["push_command"]


def test_each_publication_is_its_own_file_under_the_cycles_events_dir(cycle):
    """ONE shared events FILE would make every mid-Cycle publish a conflict.

    The predecessor's answer was one directory per lane, which only mattered
    because a lane was the thing publishing; `SPD-194` leaves one directory per
    Cycle. The anti-conflict property survives at the file level, which is
    where it always did the work: a publication is an append of a NEW file, so
    two clones publishing about different units never touch the same blob.
    """
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    head = _git(project, "rev-parse", "HEAD").stdout.strip()
    first = lg.publish(
        str(project), cycle_id=cycle_id, manifest=manifest,
        unit_id="WU-UP", condition="integrated", commit_sha=head,
    )
    second = lg.publish(
        str(project), cycle_id=cycle_id, manifest=manifest,
        unit_id="WU-UP", condition="contract_published",
        output={"kind": "contract", "id": "contracts/auth", "digest": "sha256:z"},
        verify=False,
    )
    events_dir = Path(sp.committed_events_dir(str(project), cycle_id))
    assert Path(first["path"]).parent == events_dir
    assert Path(second["path"]).parent == events_dir
    assert first["path"] != second["path"]
    assert len(list(events_dir.glob("*.json"))) == 2


# ── review findings on ca8efc2, one regression test each ───────────────────


def _revise(project: Path, cycle_id: str, reason: str, units: list) -> dict:
    return sm.revise_manifest(
        str(project), cycle_id=cycle_id, reason=reason, revised_by="po",
        admitted_units=units,
    )


def test_a_same_id_declaration_revision_still_reprojects(cycle):
    """Review finding P1(1).

    Comparing Work Unit ID SETS treated matching ids as proof the projection
    was current. A superseding declaration that keeps the same ids while
    changing `depends_on` returned `already executing`, so the board kept stale
    unit data AND the old hash -- then declared readiness against a revision
    the Cycle had moved past. The hash is the only thing that answers "am I
    current".

    Migration note: `revise_manifest` seals the supersession and writes NOTHING
    to the board, so without a re-projection on hydration a revision reaches no
    unit at all. That is what this arm now also covers.
    """
    project, cycle_id = cycle
    assert _unit(project, "WU-DOWN")["depends_on"], "fixture should have an edge"
    before = sm.read_state(str(project))["manifest_hash"]

    revised = _revise(project, cycle_id, "drop the edge", [
        _fx_unit("WU-UP", title="upstream"),
        _fx_unit("WU-DOWN", title="downstream", depends_on=[]),
    ])
    assert revised["declaration_hash"] != before

    result = sm.hydrate_cycle(str(project), cycle_id=cycle_id)
    assert result.get("reprojected") is True, result
    assert sm.read_state(str(project))["manifest_hash"] == revised["declaration_hash"]
    assert _unit(project, "WU-DOWN")["depends_on"] == [], _unit(project, "WU-DOWN")


def test_reprojection_preserves_in_flight_state_and_receipts(cycle):
    """Re-projecting refreshes declaration-owned FIELDS; it must not reset a
    unit that has started, which would orphan its receipts."""
    project, cycle_id = cycle
    _set_state(project, "WU-DOWN", "in_progress")
    state = sm.read_state(str(project))
    next(u for u in state["current_stories"] if u["id"] == "WU-DOWN")["receipts"] = [
        "WU-DOWN-se.json"
    ]
    sm._write_state(str(project), state)

    _revise(project, cycle_id, "retitle", [
        _fx_unit("WU-UP", title="upstream"),
        _fx_unit("WU-DOWN", title="renamed downstream",
                 depends_on=[{"unit_id": "WU-UP", "condition": "integrated"}]),
    ])
    sm.hydrate_cycle(str(project), cycle_id=cycle_id)

    unit = _unit(project, "WU-DOWN")
    assert unit["state"] == "in_progress", "started work must not be reset"
    assert unit["receipts"] == ["WU-DOWN-se.json"], "receipts must survive"
    assert unit["title"] == "renamed downstream", "declaration fields must refresh"


def test_reprojection_refreshes_labels_and_ui_classification(cycle):
    """Declaration revisions cannot leave an old fail-open UI classification."""
    project, cycle_id = cycle
    assert _unit(project, "WU-DOWN")["ui_bearing"] is False

    _revise(project, cycle_id, "add UI surface", [
        _fx_unit("WU-UP", title="upstream"),
        _fx_unit(
            "WU-DOWN",
            title="Render the account dashboard",
            labels=["surface:web"],
            acceptance_criteria=["The dashboard screen renders account data"],
            depends_on=[{"unit_id": "WU-UP", "condition": "integrated"}],
        ),
    ])
    sm.hydrate_cycle(str(project), cycle_id=cycle_id)

    unit = _unit(project, "WU-DOWN")
    assert unit["labels"] == ["surface:web"]
    assert unit["ui_bearing"] is True


def test_a_weaker_event_does_not_satisfy_a_unit_on_this_board(cycle):
    """Review finding P1(2).

    The locally-owned branch checked only that SOME event existed, so a `done`
    event satisfied an edge declaring `integrated`. Ownership changed who could
    publish the event, not what the event had to prove -- and with the
    Workstream retired this IS the only branch, so the check carries the whole
    mechanism rather than a corner of it.
    """
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    lg.publish(
        str(project), cycle_id=cycle_id, manifest=manifest,
        unit_id="WU-UP", condition="done",
    )
    lg.refresh(str(project), cycle_id, manifest=manifest, fetch=False)
    _set_state(project, "WU-UP", "done")

    verdict = _dep_verdict(project, "WU-DOWN")
    assert verdict["met"] is False, "a done event must not satisfy `integrated`"


def test_an_unverified_event_does_not_satisfy_a_unit_on_this_board():
    """Same finding, isolated: verification is checked for local units too."""
    ctx = {
        "cycle_id": "7",
        "manifest_present": True,
        "admitted": ["WU-01", "WU-02"],
        "owners": {},
        "satisfied": {"WU-01": {"condition": "integrated", "verified": False}},
    }
    state = {
        "build_mode": "spq",
        "current_stories": [
            _fx_unit("WU-01", state="done"),
            _fx_unit("WU-02", state="queued", depends_on=[{"unit_id": "WU-01", "condition": "integrated"}]),
        ],
    }
    verdict = story.story_dep_status(
        state, state["current_stories"][1], dep_context=ctx
    )
    assert verdict["met"] is False
    assert verdict["unmet"][0]["reason_code"] == story.DEP_CONDITION_UNVERIFIED


def test_a_malformed_work_unit_id_cannot_escape_the_events_directory(cycle):
    """Review finding P1(4).

    A Work Unit id is interpolated into an event FILENAME, so any id the
    declaration admits becomes a path segment. Validated at admission AND
    checked for containment at the write, because the containment check must
    not depend on validation having run -- an older plugin's declaration, or a
    direct caller, must still not place a file outside the events directory.
    """
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    poisoned = json.loads(json.dumps(manifest))
    poisoned[cr.UNITS_FIELD][0]["id"] = "../../../../escaped"

    with pytest.raises(lg.LedgerError, match="not a valid Work Unit id"):
        lg.publish(
            str(project), cycle_id=cycle_id, manifest=poisoned,
            unit_id="../../../../escaped", condition="done",
        )
    assert not (Path(project).parent / "escaped.json").exists()
    assert not list(Path(project).parent.glob("*escaped*"))


def test_the_declaration_refuses_a_malformed_work_unit_id_at_admission(cycle):
    """The boundary that decides what is admitted is where this belongs.

    The replacement's `cycle_records` validated a unit's kind, criteria and
    path scope but NOT its id shape, so the admission half of the pair was
    gone and only the write-time containment check remained. A containment
    check is defence in depth; it is not the place a traversing id is supposed
    to be rejected.
    """
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    for bad in ("../evil", "a/b", "..", ".hidden", "-lead"):
        poisoned = json.loads(json.dumps(manifest))
        poisoned[cr.UNITS_FIELD][0]["id"] = bad
        problems = cr.problems(poisoned)
        assert any("safe path segment" in p for p in problems), (bad, problems)


# ── an unreadable ref is not an empty ref ──────────────────────────────────
#
# `refresh` used to `continue` past any `ls-tree` failure and set
# `refresh_ok = fetch_ok`, i.e. the top-level fetch alone. So a ref this clone
# could not read produced `fresh: True` with zero events from it -- "I could
# not read it" rendered identically to "nothing has been published". For one
# consumer asking about one upstream that was survivable, because no-event is
# already the fail-closed direction. For anything AGGREGATING across the
# Cycle -- which release readiness is -- it is a green built on silence.


def test_a_ref_nobody_has_pushed_yet_is_not_unreadable(cycle):
    """The normal state at the start of every Cycle.

    Nobody has pushed, so `origin/cycle/<id>/integration` does not resolve.
    Counting that as unreadable would make the ledger permanently stale until
    the first push -- a fail-closed that never opens.
    """
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    cache = lg.refresh(str(project), cycle_id, manifest=manifest, fetch=False)
    assert cache["refresh_ok"] is True
    assert cache["unreadable_refs"] == []
    assert lg.snapshot(str(project), cycle_id)["fresh"] is True


def test_a_ref_that_resolves_but_cannot_be_listed_blocks(cycle, monkeypatch):
    """A resolvable ref whose tree cannot be read is a real fault.

    Simulated at the plumbing seam rather than by corrupting an object store:
    the branch under test is the one that distinguishes "absent" from
    "unreadable", and it keys on whether the ref resolves.
    """
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    real_git = lg._git

    def fake_git(project_dir, *args):
        if args and args[0] == "ls-tree":
            return (128, "", "fatal: not a tree object")
        if args and args[0] == "rev-parse":
            return (0, "b" * 40, "")          # the ref DOES resolve
        return real_git(project_dir, *args)

    monkeypatch.setattr(lg, "_git", fake_git)
    cache = lg.refresh(str(project), cycle_id, manifest=manifest, fetch=False)

    assert cache["refresh_ok"] is False
    assert [u["ref"] for u in cache["unreadable_refs"]] == [
        "origin/%s" % sp.integration_branch(cycle_id)
    ]
    assert "unreadable refs" in cache["refresh_detail"]
    snap = lg.snapshot(str(project), cycle_id)
    assert snap["fresh"] is False, "an incomplete refresh must fail closed"
