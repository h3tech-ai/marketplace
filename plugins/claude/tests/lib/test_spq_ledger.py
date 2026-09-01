"""Layer 1 — the Cycle dependency ledger (#303 §3/§4).

Without this, a cross-workstream `depends_on` edge can never resolve: the #304
gate refuses it forever, which is safe but useless. These tests pin the property
that makes it useful AND still safe:

    an upstream event can unblock a downstream Work Unit in another workstream,
    but only when the consumer can actually check the claim.

The last clause is the whole design. `spq.sync.accept_unverified_events`
defaults to false, so a claim-only condition fails closed. That has a real cost
-- a plain-string cross-workstream `depends_on` becomes unusable and the PO must
name a verifiable condition -- and it is the only way "an upstream event safely
unblocks downstream work" is true rather than aspirational.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import spq_ledger as lg
import spq_manifest as mf
import spq_paths as sp
import spq_state_machine as sm
import story_pipeline as story


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )


@pytest.fixture
def cycle(tmp_path: Path, monkeypatch):
    """A real repo with a two-workstream Cycle open and a sealed manifest."""
    project = tmp_path / "proj"
    project.mkdir()
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "t@e.co")
    _git(project, "config", "user.name", "t")
    (project / "f.txt").write_text("x", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")
    (project / ".synaptory.yaml").write_text(
        'build_mode: spq\n'
        'spq:\n'
        '  workstreams:\n'
        '    - id: "spine"\n'
        '      shared_owner: true\n'
        '    - id: "frame"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv(sp.ENV_WORKSTREAM, raising=False)
    monkeypatch.delenv("SYNAPTORY_ACTIVE_SPEC", raising=False)

    sm.initialize(str(project), workstream_id="frame")
    sm.approve_baseline(str(project), approved_by="t")
    sm.open_cycle(
        str(project), 1, "cycle 1",
        [
            {"id": "WU-UP", "title": "upstream", "labels": ["ws:spine"],
             "outputs": [{"kind": "contract", "id": "contracts/auth"}]},
            {"id": "WU-DOWN", "title": "downstream", "labels": ["ws:frame"],
             "depends_on": [{"unit_id": "WU-UP", "condition": "integrated"}]},
        ],
    )
    cycle_id = sm.identity(str(project)).cycle_id
    # The integration ref must exist: `integrated` is verified by ancestry
    # against it, and a missing ref is correctly a refusal rather than a pass.
    _git(project, "branch", sp.integration_branch(cycle_id))
    return project, cycle_id


def _as_workstream_clone(project: Path, workstream: str) -> dict:
    """Make this repo look like a FRESH workstream clone, then hydrate.

    The fixture opens the Cycle here, so the repo is simultaneously the
    integration clone -- and hydration correctly reports "already executing"
    because the board is already on the authoritative manifest. A real
    workstream clone has the committed manifest and NO execution state, which
    is what this reproduces.
    """
    import spq_paths

    ident = sm.identity(str(project))
    state_path = Path(
        spq_paths.execution_state_path(str(project), ident.cycle_id, workstream)
    )
    if state_path.exists():
        state_path.unlink()
    spq_paths.write_pin(str(project), workstream)
    return sm.hydrate_cycle(str(project), 1, [], workstream_id=workstream)


def _unit(project: Path, unit_id: str) -> dict:
    return next(
        u for u in sm.read_state(str(project))["current_stories"]
        if u["id"] == unit_id
    )


def _dep_verdict(project: Path, unit_id: str) -> dict:
    state = sm.read_state(str(project))
    unit = next(u for u in state["current_stories"] if u["id"] == unit_id)
    return story.story_dep_status(
        state, unit, dep_context=story.dep_context(str(project), state)
    )


# ── the headline property ──────────────────────────────────────────────────


def test_a_cross_workstream_edge_is_blocked_until_the_event_exists(cycle):
    """Before the ledger existed this could NEVER resolve. The refusal must
    name the owning workstream, not report an unknown id -- the operator's next
    step is completely different.

    Two distinct unsatisfied states, and the distinction is worth keeping:
    "we have never looked" (no cache) versus "we looked and there is nothing"
    (refreshed, no event). Both fail closed; only the second means chasing the
    other workstream rather than running a refresh.
    """
    project, cycle_id = cycle

    # Never refreshed: we genuinely do not know, and the detail says so.
    never_looked = _dep_verdict(project, "WU-DOWN")["unmet"][0]
    assert never_looked["reason_code"] == story.DEP_LEDGER_STALE
    assert never_looked["owner_workstream"] == "spine", "still name the owner"

    # Refreshed with nothing published: now the answer is about the workstream.
    manifest = sm.read_manifest(str(project), cycle_id)
    lg.refresh(str(project), cycle_id, manifest=manifest, fetch=False)
    looked = _dep_verdict(project, "WU-DOWN")["unmet"][0]
    assert looked["reason_code"] == story.DEP_OTHER_WORKSTREAM
    assert looked["owner_workstream"] == "spine"


def test_a_verified_integration_event_unblocks_the_downstream_unit(cycle):
    """#303's whole point: an upstream event unblocks a dependent Work Unit in
    ANOTHER workstream, without that workstream's board being visible here."""
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    head = _git(project, "rev-parse", "HEAD").stdout.strip()

    lg.publish(
        str(project), cycle_id=cycle_id, manifest=manifest,
        workstream_id="spine", unit_id="WU-UP", condition="integrated",
        commit_sha=head,
    )
    lg.refresh(str(project), cycle_id, manifest=manifest, fetch=False)

    assert _dep_verdict(project, "WU-DOWN")["met"] is True


def test_next_action_dispatches_the_downstream_unit_once_unblocked(cycle):
    """End to end through the shipped path, not just the resolver."""
    project, cycle_id = cycle
    # The board must be this lane's PROJECTION -- WU-DOWN only. The fixture's
    # open_cycle admitted both units, which is the integration clone's view.
    _as_workstream_clone(project, "frame")
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
        workstream_id="spine", unit_id="WU-UP", condition="integrated",
        commit_sha=head,
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
            workstream_id="spine", unit_id="WU-UP", condition="integrated",
            commit_sha="0" * 40,
        )


def test_integrated_requires_a_sha(cycle):
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    with pytest.raises(lg.LedgerError, match="requires --sha"):
        lg.publish(
            str(project), cycle_id=cycle_id, manifest=manifest,
            workstream_id="spine", unit_id="WU-UP", condition="integrated",
        )


def _incremental_attestation(project: Path, manifest: dict, candidate: str) -> dict:
    manifest["integration_policy"] = {
        "mode": "incremental",
        "merge_requires": ["regression_green"],
    }
    manifest["manifest_hash"] = mf.compute_hash(manifest)
    return lg.build_incremental_evaluation(
        cycle_id=manifest["cycle_id"],
        manifest_hash=manifest["manifest_hash"],
        unit_id="WU-UP",
        candidate_sha=candidate,
        evaluated_head=candidate,
        integration_ref=manifest["integration_ref"],
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
        workstream_id="spine", unit_id="WU-UP", condition="integrated",
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
    _git(project, "branch", "-f", manifest["integration_ref"], "HEAD")

    with pytest.raises(lg.LedgerError, match="re-run evaluate-incremental"):
        lg.publish(
            str(project), cycle_id=cycle_id, manifest=manifest,
            workstream_id="spine", unit_id="WU-UP", condition="integrated",
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
            workstream_id="spine", unit_id="WU-UP",
            condition="contract_published",
            output={"kind": "contract", "id": "contracts/auth"},
        )


def test_a_claim_only_condition_is_refused_by_default(cycle):
    """Cross-workstream `done` is unverifiable from here -- the other clone's
    board is not visible -- so it must not silently license a dispatch."""
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    lg.publish(
        str(project), cycle_id=cycle_id, manifest=manifest,
        workstream_id="spine", unit_id="WU-UP", condition="done",
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


# ── ownership ──────────────────────────────────────────────────────────────


def test_a_workstream_cannot_publish_about_another_lanes_unit(cycle):
    """Otherwise a dependency gets satisfied by someone with no knowledge of
    it -- the exact failure the owner map exists to prevent."""
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    with pytest.raises(lg.LedgerError, match="owned by workstream"):
        lg.publish(
            str(project), cycle_id=cycle_id, manifest=manifest,
            workstream_id="frame", unit_id="WU-UP", condition="done",
        )


def test_publishing_about_an_unadmitted_unit_is_refused(cycle):
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    with pytest.raises(lg.LedgerError, match="not admitted"):
        lg.publish(
            str(project), cycle_id=cycle_id, manifest=manifest,
            workstream_id="spine", unit_id="GHOST", condition="done",
        )


def test_cycle_integrated_cannot_be_published_from_inside_a_cycle(cycle):
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    with pytest.raises(lg.LedgerError, match="#305"):
        lg.publish(
            str(project), cycle_id=cycle_id, manifest=manifest,
            workstream_id="spine", unit_id="WU-UP", condition="cycle_integrated",
        )


# ── event identity and selection ───────────────────────────────────────────


def test_events_are_content_addressed_and_self_deduplicating():
    """Content addressing maps straight onto the control plane's
    client_event_id idempotency index without a second scheme."""
    kwargs = dict(cycle_id="7-abc12345", manifest_hash="sha256:x",
                  workstream_id="spine", unit_id="WU-1",
                  condition="integrated", commit_sha="dead", published_at="t")
    assert lg.build_event(**kwargs)["event_id"] == lg.build_event(**kwargs)["event_id"]
    other = dict(kwargs, commit_sha="beef")
    assert lg.build_event(**kwargs)["event_id"] != lg.build_event(**other)["event_id"]


def test_the_strongest_recorded_condition_wins():
    manifest = {"cycle_id": "7-abc12345", "manifest_hash": "sha256:x"}
    base = dict(cycle_id="7-abc12345", manifest_hash="sha256:x",
                workstream_id="spine", unit_id="WU-1", published_at="t")
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
        workstream_id="spine", unit_id="WU-UP", condition="done",
    )
    lg.refresh(str(project), cycle_id, manifest=manifest, fetch=False)
    assert _dep_verdict(project, "WU-DOWN")["met"] is False


def test_events_from_another_cycle_or_manifest_revision_are_ignored():
    """An event recorded against a superseded admitted set may describe a unit
    that no longer exists; letting it satisfy an edge resolves against a Cycle
    that is gone."""
    manifest = {"cycle_id": "7-abc12345", "manifest_hash": "sha256:x"}
    base = dict(workstream_id="spine", unit_id="WU-1", condition="integrated",
                published_at="t")
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
    quality, not safety. Blocking here would strand a workstream whose
    upstream genuinely did integrate."""
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    head = _git(project, "rev-parse", "HEAD").stdout.strip()
    lg.publish(
        str(project), cycle_id=cycle_id, manifest=manifest,
        workstream_id="spine", unit_id="WU-UP", condition="integrated",
        commit_sha=head,
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
    needs both facts: who owns the upstream, and that a refresh might change
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
    assert unmet["owner_workstream"] == "spine", "still name the owner"
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
    manifest = sm.read_manifest(str(project), cycle_id)
    head = _git(project, "rev-parse", "HEAD").stdout.strip()
    # spine is a separate CLONE in reality, so give it a hydrated board here
    # before switching to it -- re-pinning alone would point at an execution
    # state that was never written.
    sm.hydrate_cycle(str(project), 1, [], workstream_id="spine")
    state = sm.read_state(str(project))
    next(u for u in state["current_stories"] if u["id"] == "WU-UP")["state"] = "done"
    sm._write_state(str(project), state)
    sm.publish_event(
        str(project), unit_id="WU-UP", condition="integrated", commit_sha=head
    )
    state = sm.read_state(str(project))
    unit = next(u for u in state["current_stories"] if u["id"] == "WU-UP")
    assert unit["integration"]["status"] == "integrated"
    assert unit["integration"]["commit_sha"] == head


def test_publishing_integrated_refuses_a_non_done_unit(cycle):
    """An ancestor SHA proves placement, not that the Work Unit passed DoD."""
    project, _cycle_id = cycle
    head = _git(project, "rev-parse", "HEAD").stdout.strip()
    sm.hydrate_cycle(str(project), 1, [], workstream_id="spine")

    with pytest.raises(ValueError, match="must be done"):
        sm.publish_event(
            str(project), unit_id="WU-UP", condition="integrated", commit_sha=head
        )

    assert not list(Path(sp.committed_events_dir(
        str(project), sm.identity(str(project)).cycle_id, "spine"
    )).glob("*.json")), "a refused publication must not leave an event behind"


# ── operator observability ─────────────────────────────────────────────────


def test_dep_status_names_the_edge_the_owner_and_the_reason(cycle):
    """#304's AC: an unresolved dependency must be observable to the operator.
    `deps_blocked` says the loop halted; this says which edge and what to do."""
    project, _cycle_id = cycle
    report = sm.dep_status(str(project))
    down = next(u for u in report["units"] if u["unit_id"] == "WU-DOWN")
    assert down["dependencies_met"] is False
    assert down["unmet"][0]["owner_workstream"] == "spine"
    assert report["manifest_present"] is True


def test_dep_status_distinguishes_a_local_only_event_from_nothing_published(cycle):
    """Without the distinction, "I published it" and "nobody can see it" look
    identical, and the mandatory human push reads as a bug."""
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    head = _git(project, "rev-parse", "HEAD").stdout.strip()
    lg.publish(
        str(project), cycle_id=cycle_id, manifest=manifest,
        workstream_id="spine", unit_id="WU-UP", condition="integrated",
        commit_sha=head,
    )
    # Seen from the CONSUMER lane, which still owns WU-DOWN.
    report = sm.dep_status(str(project))
    down = next(u for u in report["units"] if u["unit_id"] == "WU-DOWN")
    assert down["dependencies_met"] is False

    # Seen from the PRODUCER lane, the event is written but unpushed.
    sm.hydrate_cycle(str(project), 1, [], workstream_id="spine")
    producer = sm.dep_status(str(project))
    assert "WU-UP" in producer["events_pending_push"]


def test_publish_prints_the_push_command(cycle):
    """The project's own rules forbid agents pushing, so propagation is a
    human's step. Naming the command is what stops the latency looking like a
    failure."""
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    head = _git(project, "rev-parse", "HEAD").stdout.strip()
    result = lg.publish(
        str(project), cycle_id=cycle_id, manifest=manifest,
        workstream_id="spine", unit_id="WU-UP", condition="integrated",
        commit_sha=head,
    )
    assert "git push" in result["push_command"]
    assert ".synaptory/cycles" in result["push_command"]


def test_events_land_in_per_workstream_directories(cycle):
    """One shared events file would make every mid-Cycle publish a merge
    conflict between workstreams."""
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    head = _git(project, "rev-parse", "HEAD").stdout.strip()
    result = lg.publish(
        str(project), cycle_id=cycle_id, manifest=manifest,
        workstream_id="spine", unit_id="WU-UP", condition="integrated",
        commit_sha=head,
    )
    assert Path(result["path"]).parent.name == "spine"


# ── review findings on ca8efc2, one regression test each ───────────────────


def test_a_same_id_manifest_revision_still_reprojects(cycle):
    """Review finding P1(1).

    Comparing Work Unit ID SETS treated matching ids as proof the projection was
    current. A superseding manifest that keeps the same ids while changing
    `depends_on` returned `already executing`, so the clone kept stale unit data
    AND the old manifest hash -- then declared readiness against a revision the
    Cycle had moved past. The hash is the only thing that answers "am I current".
    """
    project, cycle_id = cycle
    _as_workstream_clone(project, "frame")
    assert _unit(project, "WU-DOWN")["depends_on"], "fixture should have an edge"

    # Same ids, different dependency graph.
    revised = sm.revise_manifest(
        str(project), cycle_id=cycle_id, reason="drop the edge", revised_by="po",
    )
    for unit in revised["work_units"]:
        if unit["id"] == "WU-DOWN":
            unit["depends_on"] = []
    revised["manifest_hash"] = mf.compute_hash(
        {k: v for k, v in revised.items() if k != "manifest_hash"}
    )
    path = Path(sp.manifest_path(str(project), cycle_id))
    path.chmod(0o644)
    path.write_text(json.dumps(revised), encoding="utf-8")

    result = sm.hydrate_cycle(str(project), 1, [], workstream_id="frame")
    assert result.get("reprojected") is True, result.get("reason")
    assert sm.read_state(str(project))["manifest_hash"] == revised["manifest_hash"]
    assert _unit(project, "WU-DOWN")["depends_on"] == [], _unit(project, "WU-DOWN")


def test_reprojection_preserves_in_flight_state_and_receipts(cycle):
    """Re-projecting refreshes manifest-owned FIELDS; it must not reset a unit
    that has started, which would orphan its receipts."""
    project, cycle_id = cycle
    _as_workstream_clone(project, "frame")
    sm.transition_story(str(project), "WU-DOWN", "in_progress")

    revised = sm.revise_manifest(
        str(project), cycle_id=cycle_id, reason="retitle", revised_by="po",
    )
    for unit in revised["work_units"]:
        if unit["id"] == "WU-DOWN":
            unit["title"] = "renamed downstream"
    revised["manifest_hash"] = mf.compute_hash(
        {k: v for k, v in revised.items() if k != "manifest_hash"}
    )
    path = Path(sp.manifest_path(str(project), cycle_id))
    path.chmod(0o644)
    path.write_text(json.dumps(revised), encoding="utf-8")

    sm.hydrate_cycle(str(project), 1, [], workstream_id="frame")
    unit = _unit(project, "WU-DOWN")
    assert unit["state"] == "in_progress", "started work must not be reset"
    assert unit["title"] == "renamed downstream", "manifest fields must refresh"


def test_reprojection_refreshes_labels_and_ui_classification(cycle):
    """Manifest revisions cannot leave an old fail-open UI classification."""
    project, cycle_id = cycle
    _as_workstream_clone(project, "frame")
    assert _unit(project, "WU-DOWN")["ui_bearing"] is False

    revised = sm.revise_manifest(
        str(project), cycle_id=cycle_id, reason="add UI surface", revised_by="po",
    )
    for unit in revised["work_units"]:
        if unit["id"] == "WU-DOWN":
            unit["title"] = "Render the account dashboard"
            unit["labels"] = ["ws:frame", "surface:web"]
            unit["acceptance_criteria"] = ["The dashboard renders account data"]
    revised["manifest_hash"] = mf.compute_hash(
        {k: v for k, v in revised.items() if k != "manifest_hash"}
    )
    path = Path(sp.manifest_path(str(project), cycle_id))
    path.chmod(0o644)
    path.write_text(json.dumps(revised), encoding="utf-8")

    sm.hydrate_cycle(str(project), 1, [], workstream_id="frame")
    unit = _unit(project, "WU-DOWN")
    assert unit["labels"] == ["ws:frame", "surface:web"]
    assert unit["ui_bearing"] is True


def test_a_weaker_event_does_not_satisfy_a_locally_owned_unit(cycle):
    """Review finding P1(2).

    The locally-owned branch checked only that SOME event existed, so a `done`
    event satisfied an edge declaring `integrated`. Ownership changes who can
    publish the event, not what the event has to prove.
    """
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    # Both units on ONE board, so WU-UP is locally owned from WU-DOWN's view.
    lg.publish(
        str(project), cycle_id=cycle_id, manifest=manifest,
        workstream_id="spine", unit_id="WU-UP", condition="done",
    )
    lg.refresh(str(project), cycle_id, manifest=manifest, fetch=False)
    sm.transition_story(str(project), "WU-UP", "in_progress")
    for state in ("testing", "reviewing", "done"):
        try:
            sm.transition_story(str(project), "WU-UP", state)
        except Exception:
            break

    verdict = _dep_verdict(project, "WU-DOWN")
    assert verdict["met"] is False, "a done event must not satisfy `integrated`"


def test_an_unverified_event_does_not_satisfy_a_locally_owned_unit():
    """Same finding, isolated: verification is checked for local units too."""
    ctx = {
        "cycle_id": "7",
        "workstream_id": "frame",
        "manifest_present": True,
        "admitted": ["WU-01", "WU-02"],
        "owners": {"WU-01": "frame", "WU-02": "frame"},
        "satisfied": {"WU-01": {"condition": "integrated", "verified": False}},
    }
    state = {
        "build_mode": "spq",
        "current_stories": [
            {"id": "WU-01", "state": "done"},
            {
                "id": "WU-02", "state": "queued",
                "depends_on": [{"unit_id": "WU-01", "condition": "integrated"}],
            },
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
    manifest admits becomes a path segment. Validated at admission AND checked
    for containment at the write, because the containment check must not depend
    on validation having run -- an older plugin's manifest, or a direct caller,
    must still not place a file outside the events directory.
    """
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    poisoned = json.loads(json.dumps(manifest))
    poisoned["work_units"][0]["id"] = "../../../../escaped"
    poisoned["work_units"][0]["owner_workstream"] = "spine"

    with pytest.raises(lg.LedgerError, match="not a valid Work Unit id"):
        lg.publish(
            str(project), cycle_id=cycle_id, manifest=poisoned,
            workstream_id="spine", unit_id="../../../../escaped",
            condition="done",
        )
    assert not (Path(project).parent / "escaped.json").exists()
    assert not list(Path(project).parent.glob("*escaped*"))


def test_the_manifest_refuses_a_malformed_work_unit_id_at_admission(cycle):
    """The boundary that decides what is admitted is where this belongs."""
    project, cycle_id = cycle
    manifest = sm.read_manifest(str(project), cycle_id)
    for bad in ("../evil", "a/b", "..", ".hidden", "-lead"):
        poisoned = json.loads(json.dumps(manifest))
        poisoned["work_units"][0]["id"] = bad
        problems = mf.validate(poisoned)
        assert any("safe path segment" in p for p in problems), (bad, problems)


# ── #305: an unreadable ref is not an empty ref ────────────────────────────
#
# `refresh` used to `continue` past any `ls-tree` failure and set
# `refresh_ok = fetch_ok`, i.e. the top-level fetch alone. So a child ref this
# clone could not read produced `fresh: True` with zero events from it --
# "I could not read spine" rendered identically to "spine has published
# nothing". For one consumer asking about one upstream that was survivable,
# because no-event is already the fail-closed direction. For anything
# AGGREGATING across refs -- which is what a Coordination Cycle's release
# readiness is -- it is a green built on silence.


def test_a_branch_nobody_has_pushed_yet_is_not_unreadable(cycle):
    """The normal state at the start of every Cycle.

    No workstream has pushed, so no `origin/cycle/<id>/ws/<ws>` ref resolves.
    Counting that as unreadable would make the ledger permanently stale until
    the last lane pushes -- a fail-closed that never opens.
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
    assert [u["workstream"] for u in cache["unreadable_refs"]] == ["spine", "frame"]
    assert "unreadable refs" in cache["refresh_detail"]
    snap = lg.snapshot(str(project), cycle_id)
    assert snap["fresh"] is False, "an incomplete refresh must fail closed"
