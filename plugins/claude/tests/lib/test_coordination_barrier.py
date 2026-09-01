"""Layer 1 — the release barrier (#305).

The load-bearing test in this file is `test_an_edgeless_child_is_not_blocked_by_a
_blocked_sibling`. Everything else supports it.

The child-Cycle barrier is all-or-nothing across a quorum by design: a Cycle's
workstreams share one goal and one integration ref. A release is not that.
Platform, Contract Mastery and EHR share a codebase and a date and nothing else,
so gating Platform on EHR would invent a dependency the product does not have --
which is the failure the epic names in as many words.

`evaluate_edges` and `child_readiness` are PURE, which is what makes that
property a fast unit test rather than a git fixture, and `gated_children` puts it
in the verdict payload where a regression is visible rather than inferred.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import coordination_barrier as cb
import coordination_cycle as cc
import spq_paths as sp

PLATFORM_ID, CONTRACT_ID, EHR_ID = "12-aaaaaaaa", "3-bbbbbbbb", "5-cccccccc"
CCID = "cc-1-9f2c1ab3"

CHILDREN = [
    {"cycle_id": PLATFORM_ID, "cycle_seq": 12, "goal": "Platform",
     "manifest_hash": "sha256:" + "a" * 64, "selected_sha": "1" * 40,
     "integration_ref": "cycle/%s/integration" % PLATFORM_ID},
    {"cycle_id": CONTRACT_ID, "cycle_seq": 3, "goal": "Contract Mastery",
     "manifest_hash": "sha256:" + "b" * 64, "selected_sha": "2" * 40,
     "integration_ref": "cycle/%s/integration" % CONTRACT_ID},
    {"cycle_id": EHR_ID, "cycle_seq": 5, "goal": "EHR",
     "manifest_hash": "sha256:" + "c" * 64, "selected_sha": "3" * 40,
     "integration_ref": "cycle/%s/integration" % EHR_ID},
]
EDGES = [{
    "id": "EDGE-1", "waiter_cycle_id": EHR_ID, "waiter_unit_id": "WU-EHR-SYNC",
    "producer_cycle_id": CONTRACT_ID,
    "output": {"kind": "contract", "id": "contracts/auth"},
    "condition": "contract_published",
}]
KEY = "%s|out:contract:contracts/auth" % CONTRACT_ID


def _parent(children=None, edges=None, **kw):
    base = dict(
        coordination_cycle_id=CCID, coordination_seq=1, release_goal="Q3",
        baseline_sha="a" * 40,
        integration_ref=sp.coordination_integration_branch(CCID),
        children=[dict(c) for c in (children or CHILDREN)],
        dependency_edges=[dict(e) for e in (edges if edges is not None else EDGES)],
    )
    base.update(kw)
    return cc.seal(cc.build(**base))


def _collected(manifest, **overrides):
    """Every child green, unless a test says otherwise."""
    children = {}
    for child in manifest.get("children") or []:
        cid = str(child["cycle_id"])
        children[cid] = {
            "cycle_id": cid,
            "selected_sha": child.get("selected_sha"),
            "manifest_hash_verified": True,
            "selected_sha_present": True,
            "selected_sha_on_child_integration": True,
            "selected_sha_on_composition": True,
            "child_advanced_beyond_pin": False,
            "child_sync_verdict": "green",
            "child_sync_evidence": {
                "workstreams": ["spine"], "blocking": [], "detail": [],
            },
            "detail": "",
        }
    for cid, patch in (overrides.get("children") or {}).items():
        children[cid].update(patch)
    return {
        "coordination_cycle_id": CCID,
        "manifest": manifest,
        "children": children,
        "composition_head": "f" * 40,
        "current_branch": manifest.get("integration_ref"),
        "expected_branch": manifest.get("integration_ref"),
        "warnings": [],
    }


def _satisfied(condition="contract_published", verified=True):
    return {KEY: {
        "condition": condition,
        "satisfies": cc.satisfies_set(condition),
        "verified": verified,
        "sha": "2" * 40,
        "event_id": "abc123",
        "cycle_id": CONTRACT_ID,
        "coordination_cycle_id": CCID,
        "published_at": "2026-08-27T10:00:00Z",
    }}


# ── THE property ───────────────────────────────────────────────────────────


def test_an_edgeless_child_has_no_gating_edges():
    manifest = _parent()
    records = cb.evaluate_edges(_collected(manifest), manifest, {})
    for cid in (PLATFORM_ID, CONTRACT_ID):
        assert [r for r in records if r["waiter_cycle_id"] == cid] == []


def test_an_edgeless_child_is_not_blocked_by_a_blocked_sibling():
    """AC 7, stated explicitly rather than left as an emergent property.

    EHR's edge is unsatisfied. Platform and Contract must be untouched by that,
    and their `blocking` lists must contain nothing derived from EHR.
    """
    manifest = _parent()
    collected = _collected(manifest)
    records = cb.evaluate_edges(collected, manifest, {})   # no events at all

    ehr = cb.child_readiness(collected, manifest, EHR_ID, records)
    assert ehr["passed"] is False
    assert ehr["blocking"] == ["edge:EDGE-1"]

    for cid in (PLATFORM_ID, CONTRACT_ID):
        sibling = cb.child_readiness(collected, manifest, cid, records)
        assert sibling["passed"] is True, sibling
        assert sibling["blocking"] == [], sibling
        assert sibling["gating_edges"] == []


def test_no_child_blocking_list_ever_names_another_child():
    """The general form of the same rule: per-child readiness is computed from
    that child's OWN facts, so a sibling's identity cannot appear."""
    manifest = _parent()
    collected = _collected(
        manifest, children={EHR_ID: {"selected_sha_on_composition": False}}
    )
    records = cb.evaluate_edges(collected, manifest, _satisfied())
    for cid in (PLATFORM_ID, CONTRACT_ID, EHR_ID):
        readiness = cb.child_readiness(collected, manifest, cid, records)
        others = {PLATFORM_ID, CONTRACT_ID, EHR_ID} - {cid}
        for reason in readiness["blocking"]:
            for other in others:
                assert other not in reason, (cid, reason)


def test_a_release_with_no_edges_gates_nobody():
    manifest = _parent(edges=[])
    collected = _collected(manifest)
    records = cb.evaluate_edges(collected, manifest, {})
    assert records == []
    for cid in (PLATFORM_ID, CONTRACT_ID, EHR_ID):
        assert cb.child_readiness(collected, manifest, cid, records)["passed"] is True


def test_children_alone_never_create_edge_gating():
    """Three children, zero edges: `edges_satisfied` is vacuously true. Adding a
    child to a release must not, by itself, gate anything."""
    manifest = _parent(edges=[])
    records = cb.evaluate_edges(_collected(manifest), manifest, {})
    assert [r for r in records if not r["satisfied"]] == []


# ── per-edge refusals ──────────────────────────────────────────────────────


def test_a_verified_event_satisfies_its_edge():
    manifest = _parent()
    records = cb.evaluate_edges(_collected(manifest), manifest, _satisfied())
    assert records[0]["satisfied"] is True
    assert records[0]["reason_code"] == cb.EDGE_OK


def test_a_missing_event_blocks_with_its_own_code():
    manifest = _parent()
    records = cb.evaluate_edges(_collected(manifest), manifest, {})
    assert records[0]["reason_code"] == cb.EDGE_MISSING


def test_a_stale_ledger_blocks_with_its_own_code():
    manifest = _parent()
    records = cb.evaluate_edges(
        _collected(manifest), manifest, {}, ledger_fresh=False
    )
    assert records[0]["reason_code"] == cb.EDGE_STALE
    assert "refresh_coordination" in records[0]["detail"]


def test_a_weaker_event_blocks_with_its_own_code():
    manifest = _parent(edges=[dict(EDGES[0], condition="cycle_integrated")])
    records = cb.evaluate_edges(
        _collected(manifest), manifest, _satisfied("contract_published")
    )
    assert records[0]["reason_code"] == cb.EDGE_TOO_WEAK


def test_an_unverified_event_blocks_unless_the_release_opts_in():
    manifest = _parent()
    unverified = _satisfied(verified=False)
    blocked = cb.evaluate_edges(_collected(manifest), manifest, unverified)
    assert blocked[0]["reason_code"] == cb.EDGE_UNVERIFIED
    allowed = cb.evaluate_edges(
        _collected(manifest), manifest, unverified, accept_unverified=True
    )
    assert allowed[0]["satisfied"] is True


def test_an_edge_whose_producer_was_dropped_is_reported_not_silently_absent():
    dropped = [dict(c) for c in CHILDREN]
    dropped[1] = dict(dropped[1], state="dropped", dropped_reason="late",
                      selected_sha="")
    manifest = _parent(children=dropped)
    records = cb.evaluate_edges(_collected(manifest), manifest, _satisfied())
    assert records[0]["reason_code"] == cb.EDGE_CHILD_DROPPED
    assert records[0]["satisfied"] is False


def test_a_dropped_child_contributes_no_blocking():
    dropped = [dict(c) for c in CHILDREN]
    dropped[2] = dict(dropped[2], state="dropped", dropped_reason="slipped",
                      selected_sha="")
    manifest = _parent(children=dropped, edges=[])
    collected = _collected(manifest)
    readiness = cb.child_readiness(collected, manifest, EHR_ID, [])
    assert readiness["state"] == "dropped"
    assert readiness["blocking"] == []
    assert readiness["passed"] is True


# ── per-child facts ────────────────────────────────────────────────────────


def test_a_pinned_sha_not_on_the_child_integration_ref_blocks():
    manifest = _parent()
    collected = _collected(
        manifest, children={EHR_ID: {"selected_sha_on_child_integration": False}}
    )
    readiness = cb.child_readiness(collected, manifest, EHR_ID, [])
    assert "selected_sha_on_child_integration" in readiness["blocking"]


def test_an_unverifiable_child_manifest_hash_blocks():
    manifest = _parent()
    collected = _collected(
        manifest, children={PLATFORM_ID: {"manifest_hash_verified": False}}
    )
    readiness = cb.child_readiness(collected, manifest, PLATFORM_ID, [])
    assert "manifest_hash_verified" in readiness["blocking"]


def test_a_child_that_moved_past_the_pin_is_not_a_failure():
    """The parent pins a SHA, not a branch tip. That is exactly what lets a child
    open its next Cycle without disturbing the release."""
    manifest = _parent()
    collected = _collected(
        manifest, children={PLATFORM_ID: {"child_advanced_beyond_pin": True}}
    )
    readiness = cb.child_readiness(collected, manifest, PLATFORM_ID, [])
    assert readiness["child_advanced_beyond_pin"] is True
    assert readiness["passed"] is True


def test_a_sha_outside_the_composition_blocks():
    manifest = _parent()
    collected = _collected(
        manifest, children={PLATFORM_ID: {"selected_sha_on_composition": False}}
    )
    readiness = cb.child_readiness(collected, manifest, PLATFORM_ID, [])
    assert "selected_sha_on_composition" in readiness["blocking"]


# ── against a real repo ────────────────────────────────────────────────────


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A release clone with a real `origin`.

    Not a convenience: `collect` fetches, and a failed fetch is BLOCKING rather
    than a warning -- the same rule `sync_barrier` applies, because stale
    remote-tracking refs mean every child fact read could belong to an older
    increment. A fixture with no remote would only ever exercise that one path.
    """
    upstream = tmp_path / "upstream.git"
    subprocess.run(["git", "init", "-q", "--bare", str(upstream)], check=True,
                   capture_output=True)

    seed = tmp_path / "seed"
    seed.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@e.co"],
                 ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=seed, check=True, capture_output=True)
    (seed / ".gitignore").write_text(
        ".synaptory/*\n!.synaptory/sync/\n!.synaptory/cycles/\n"
        "!.synaptory/coordination-cycles/\n", encoding="utf-8",
    )
    (seed / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=seed, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=seed, check=True,
                   capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(upstream)], cwd=seed,
                   check=True, capture_output=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD:refs/heads/main"],
                   cwd=seed, check=True, capture_output=True)

    project = tmp_path / "release"
    subprocess.run(["git", "clone", "-q", str(upstream), str(project)], check=True,
                   capture_output=True)
    for args in (["config", "user.email", "t@e.co"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)
    return project


def test_no_manifest_blocks_rather_than_crashes(repo):
    verdict = cb.evaluate(str(repo), CCID, use_cache=False)
    assert verdict["verdict"] == "blocked"
    assert verdict["blocking"] == ["manifest_sealed"]
    assert "only one that travels" in verdict["detail"]


def test_a_failed_fetch_blocks_and_is_checked_before_the_cache(repo, monkeypatch):
    # `repo` has a working `origin`, so the ONLY cause of failure here is the
    # patch below -- which is what makes this a test of the fetch gate rather
    # than of a fixture that never had a remote.
    """A cached green served while the remote is unreachable would be exactly
    the staleness the fetch exists to rule out."""
    import spq_state_machine as m

    m.open_coordination_cycle(
        str(repo), release_goal="Q3", children=[dict(c) for c in CHILDREN],
        dependency_edges=[],
    )
    ccid = sp.read_coordination_pin(str(repo))
    real = cb._git

    def fake(project_dir, *args):
        if args and args[0] == "fetch":
            return (128, "", "fatal: no remote")
        return real(project_dir, *args)

    monkeypatch.setattr(cb, "_git", fake)
    verdict = cb.evaluate(str(repo), ccid, use_cache=True)
    assert verdict["verdict"] == "blocked"
    assert verdict["blocking"] == ["fetch"]
    assert verdict["cache_served"] is False


def test_clear_refuses_when_blocked(repo):
    import spq_state_machine as m

    m.open_coordination_cycle(
        str(repo), release_goal="Q3", children=[dict(c) for c in CHILDREN],
        dependency_edges=[],
    )
    ccid = sp.read_coordination_pin(str(repo))
    with pytest.raises(cb.CoordinationBarrierError) as exc:
        cb.clear(str(repo), ccid)
    assert "still blocking" in str(exc.value)
    assert "dropped from the parent manifest rather than held for" in str(exc.value)


def test_a_skipped_composition_regression_is_unproven_not_passed(repo):
    """Same rule the child barrier applies to a skipped journey: treating
    absence of evidence as evidence is how a barrier stops being one."""
    proof = cb._run_proof_script(str(repo), "scripts/nope.sh", label="x")
    assert proof["skipped"] is True
    assert proof.get("exit_code") is None


def test_the_verdict_cache_key_separates_two_releases(repo):
    a = cb._verdict_cache_key("cc-1-aaaaaaaa", {"manifest_hash": "sha256:x"},
                              {"children": {}, "composition_head": "h"})
    b = cb._verdict_cache_key("cc-1-bbbbbbbb", {"manifest_hash": "sha256:x"},
                              {"children": {}, "composition_head": "h"})
    assert a != b


def test_the_verdict_cache_key_changes_with_a_pinned_sha(repo):
    base = {"children": {PLATFORM_ID: {"selected_sha": "1" * 40}},
            "composition_head": "h"}
    moved = {"children": {PLATFORM_ID: {"selected_sha": "9" * 40}},
             "composition_head": "h"}
    manifest = {"manifest_hash": "sha256:x"}
    assert cb._verdict_cache_key(CCID, manifest, base) != cb._verdict_cache_key(
        CCID, manifest, moved
    )


# ── the report ─────────────────────────────────────────────────────────────


def test_release_readiness_names_hashes_shas_closure_and_evidence(repo):
    """AC 12, checked noun by noun."""
    import spq_state_machine as m

    m.open_coordination_cycle(
        str(repo), release_goal="Q3", children=[dict(c) for c in CHILDREN],
        dependency_edges=[dict(e) for e in EDGES],
    )
    ccid = sp.read_coordination_pin(str(repo))
    report = cb.release_readiness(str(repo), ccid, use_cache=False)

    assert report["release_id"] == "RELEASE-1"
    assert report["manifest_hash"].startswith("sha256:")
    assert {c["cycle_id"] for c in report["children"]} == {
        PLATFORM_ID, CONTRACT_ID, EHR_ID
    }
    for child in report["children"]:
        assert child["manifest_hash"].startswith("sha256:")
        assert "selected_sha" in child
    assert report["dependency_closure"]["declared"] == 1
    assert "verification_evidence" in report
    assert "composition_head_sha" in report["verification_evidence"]
    assert "authoritative" in report["note"]
    # A child with no edge is reported as gated by nothing, in the payload.
    assert report["gated_children"][PLATFORM_ID] == []
    assert report["gated_children"][EHR_ID] == ["EDGE-1"]


def test_release_readiness_lists_a_dropped_child_with_its_reason(repo):
    import spq_state_machine as m

    m.open_coordination_cycle(
        str(repo), release_goal="Q3", children=[dict(c) for c in CHILDREN],
        dependency_edges=[],
    )
    ccid = sp.read_coordination_pin(str(repo))
    m.revise_coordination_manifest(
        str(repo), coordination_cycle_id=ccid, drop_child=EHR_ID,
        reason="slipped the window",
    )
    report = cb.release_readiness(str(repo), ccid, use_cache=False)
    dropped = [c for c in report["children"] if c["cycle_id"] == EHR_ID]
    assert len(dropped) == 1
    assert dropped[0]["state"] == "dropped"
    assert dropped[0]["dropped_reason"] == "slipped the window"
    assert dropped[0]["blocking"] == []


def test_the_criteria_tuple_and_the_receipt_total_stay_in_step():
    """`criteria_total` on the release receipt is `len(CRITERIA)`. If a criterion
    is added and the receipt is not regenerated, the metric silently lies."""
    assert len(cb.CRITERIA) == 6
    assert set(cb.CRITERIA) == {
        "manifest_sealed", "children_resolved", "child_integration_green",
        "edges_satisfied", "composition_merged", "composition_regression_green",
    }


# ── the child's own Sync evidence is release evidence (#305 review, P1) ─────
#
# Before this, `_child_sync` returned the literal "declared" for any readable
# JSON and `child_readiness` never consulted it, so a child with four green SHA
# flags and NO Sync evidence at all passed. Release readiness rested on
# ancestry, which says a commit is on a branch and nothing about whether the
# Cycle that produced it went green.


def test_a_child_with_no_readable_sync_evidence_is_blocked():
    manifest = _parent()
    collected = _collected(
        manifest,
        children={PLATFORM_ID: {"child_sync_verdict": None, "child_sync_evidence": None}},
    )
    readiness = cb.child_readiness(collected, manifest, PLATFORM_ID, [])
    assert readiness["passed"] is False
    assert "child_sync_verdict" in readiness["blocking"], (
        "a child whose Sync evidence could not be read must block the release. "
        "Unreadable is not green."
    )


def test_a_child_whose_sync_evidence_is_blocked_blocks_the_release():
    manifest = _parent()
    collected = _collected(
        manifest,
        children={PLATFORM_ID: {
            "child_sync_verdict": "blocked",
            "child_sync_evidence": {"blocking": ["regression_green"]},
        }},
    )
    readiness = cb.child_readiness(collected, manifest, PLATFORM_ID, [])
    assert readiness["passed"] is False
    assert "child_sync_verdict" in readiness["blocking"]


def test_sync_evidence_blocking_stays_confined_to_its_own_child():
    """AC 7 still holds: one child's Sync failure never lands on a sibling."""
    manifest = _parent()
    collected = _collected(
        manifest, children={PLATFORM_ID: {"child_sync_verdict": "blocked"}}
    )
    sibling = cb.child_readiness(collected, manifest, CONTRACT_ID, [])
    assert sibling["passed"] is True
    assert sibling["blocking"] == []



# ── the pure evidence judge ────────────────────────────────────────────────
#
# The judge decides whether a child Cycle's committed readiness records amount
# to the green Sync evidence a release may rest on. Every fixture below builds
# a GENUINELY SEALED child manifest, because the judge recomputes the seal --
# a document that merely claims a plausible hash is rejected, which is the
# point.

import coordination_cycle as cc


def _child_mf(workstreams=("spine",), units=(("WU-01", "spine"),),
              manifest_hash=None):
    """A genuinely sealed child manifest.

    `manifest_hash` overrides the computed value ONLY for tests that mean to
    tamper with a sealed document.
    """
    import spq_manifest as mf

    doc = mf.seal({
        "schema_version": mf.SCHEMA_VERSION,
        "kind": mf.KIND,
        "cycle_id": "3-bbbbbbbb",
        "workstreams": [{"id": w} for w in workstreams],
        "work_units": [{"id": u, "owner_workstream": o} for u, o in units],
    })
    if manifest_hash is not None:
        doc["manifest_hash"] = manifest_hash
    return doc


def _record(**over):
    rec = {
        "workstream": "spine",
        "manifest_hash": "",
        "work_unit_ids": {"admitted": ["WU-01"], "done": ["WU-01"], "cut": []},
        "dependency_closure": {"satisfied": [], "unsatisfied": []},
        "regression": {"command": "make test", "exit_code": 0, "skipped": False},
    }
    rec.update(over)
    return rec


def _judge(records, doc, **kw):
    """Records are stamped with the manifest's real hash unless told otherwise."""
    pin = str(doc.get("manifest_hash") or "")
    for rec in records.values():
        if not rec.get("manifest_hash"):
            rec["manifest_hash"] = pin
    return cc.sync_evidence_verdict(
        records, child_manifest=doc, pinned_manifest_hash=kw.get("pin", pin)
    )


def test_no_records_is_blocked_not_green():
    assert cc.sync_evidence_verdict({})["verdict"] == "blocked"
    assert cc.sync_evidence_verdict({})["blocking"] == ["records_present"]


def test_records_naming_another_manifest_hash_do_not_prove_this_pin():
    doc = _child_mf()
    v = _judge({"spine": _record(manifest_hash="sha256:" + "f" * 64)}, doc)
    assert v["verdict"] == "blocked"
    assert "manifest_agreement" in v["blocking"]


def test_an_outstanding_admitted_work_unit_blocks():
    doc = _child_mf(units=(("WU-01", "spine"), ("WU-02", "spine")))
    v = _judge({"spine": _record()}, doc)
    assert v["verdict"] == "blocked"
    assert "manifest_closure" in v["blocking"]
    assert any("WU-02" in d for d in v["detail"])


def test_a_failed_regression_on_the_record_blocks():
    doc = _child_mf()
    v = _judge({"spine": _record(regression={"command": "t", "exit_code": 1,
                                             "skipped": False})}, doc)
    assert v["verdict"] == "blocked"
    assert "regression_green" in v["blocking"]


def test_an_unsatisfied_dependency_on_the_record_blocks():
    doc = _child_mf()
    v = _judge({"spine": _record(
        dependency_closure={"satisfied": [], "unsatisfied": ["WU-09"]})}, doc)
    assert v["verdict"] == "blocked"
    assert "dependency_closure" in v["blocking"]


def test_a_complete_record_set_at_the_pinned_hash_is_green():
    doc = _child_mf(workstreams=("spine", "edge"),
                    units=(("WU-01", "spine"), ("WU-02", "edge")))
    v = _judge({
        "spine": _record(),
        "edge": _record(workstream="edge",
                        work_unit_ids={"admitted": ["WU-02"], "done": ["WU-02"],
                                       "cut": []}),
    }, doc)
    assert v["verdict"] == "green", v["detail"]
    assert v["expected_workstreams"] == ["edge", "spine"]


def test_a_waived_skipped_regression_is_not_treated_as_failure():
    """`declare_ready` refuses outright when a REQUIRED regression fails, so a
    skipped one on a written record is the child's own recorded waiver."""
    doc = _child_mf()
    v = _judge({"spine": _record(regression={"command": None, "exit_code": None,
                                             "skipped": True})}, doc)
    assert v["verdict"] == "green", v["detail"]


def test_one_record_from_a_two_workstream_child_is_not_a_quorum():
    """`records_present` means the child's OWN quorum, not "at least one"."""
    doc = _child_mf(workstreams=("spine", "edge"),
                    units=(("WU-01", "spine"),))
    v = _judge({"spine": _record()}, doc)
    assert v["verdict"] == "blocked"
    assert "records_present" in v["blocking"]
    assert v["expected_workstreams"] == ["edge", "spine"]


def test_the_integration_workstream_owes_no_record():
    """Mirrors `sync_barrier`, which excludes the integration workstream."""
    import spq_manifest as mf

    doc = mf.seal({
        "schema_version": mf.SCHEMA_VERSION, "kind": mf.KIND,
        "cycle_id": "3-bbbbbbbb",
        "workstreams": [{"id": "spine"}, {"id": "integration", "integration": True}],
        "work_units": [{"id": "WU-01", "owner_workstream": "spine"}],
    })
    assert _judge({"spine": _record()}, doc)["verdict"] == "green"


def test_a_unit_settled_by_the_wrong_workstream_is_not_closed():
    """A sibling cannot sign off a unit it does not own."""
    doc = _child_mf(workstreams=("spine", "edge"),
                    units=(("WU-01", "spine"), ("WU-02", "edge")))
    v = _judge({
        "spine": _record(work_unit_ids={"admitted": ["WU-01", "WU-02"],
                                        "done": ["WU-01", "WU-02"], "cut": []}),
        "edge": _record(workstream="edge",
                        work_unit_ids={"admitted": [], "done": [], "cut": []}),
    }, doc)
    assert v["verdict"] == "blocked"
    assert "manifest_closure" in v["blocking"]
    assert any("WU-02" in d and "edge" in d for d in v["detail"])


# ── the two fail-open shapes from the re-review at 7649c6b ──────────────────


def test_a_tampered_manifest_cannot_define_the_quorum():
    """Shrink the workstream list, keep the old hash field: the seal must catch it.

    Comparing the document's stored `manifest_hash` against the pin only proves
    the file still CLAIMS that hash. Editing `edge` and its Work Unit out while
    leaving the field alone shrank the quorum to `spine` alone, and a single
    valid spine record then read as green.
    """
    sealed = _child_mf(workstreams=("spine", "edge"),
                       units=(("WU-01", "spine"), ("WU-02", "edge")))
    pin = str(sealed["manifest_hash"])

    tampered = dict(sealed)
    tampered["workstreams"] = [{"id": "spine"}]
    tampered["work_units"] = [{"id": "WU-01", "owner_workstream": "spine"}]
    tampered["manifest_hash"] = pin          # the claim survives the edit

    import spq_manifest as mf
    assert not mf.verify_hash(tampered), "fixture must actually be tampered"

    v = cc.sync_evidence_verdict(
        {"spine": _record(manifest_hash=pin)},
        child_manifest=tampered, pinned_manifest_hash=pin,
    )
    assert v["verdict"] == "blocked", v
    assert "manifest_sealed" in v["blocking"]


def test_a_manifest_with_no_delivery_workstream_is_never_green():
    """`sync_barrier.collect` refuses this outright: an empty quorum is never
    present. Treating it as vacuously satisfied let one stray record carry a
    whole release."""
    import spq_manifest as mf

    doc = mf.seal({
        "schema_version": mf.SCHEMA_VERSION, "kind": mf.KIND,
        "cycle_id": "3-bbbbbbbb",
        "workstreams": [{"id": "integration", "integration": True}],
        "work_units": [],
    })
    v = _judge({"spine": _record()}, doc)
    assert v["verdict"] == "blocked", v
    assert "records_present" in v["blocking"]
    assert v["expected_workstreams"] == []
    # Pin the REASON, not just the verdict. With a stray record present the
    # exact-equality check would also block, so asserting only "blocked" would
    # pass with the empty-quorum guard removed entirely — the test would then
    # be measuring the wrong rule.
    assert any("no delivery workstream" in d for d in v["detail"]), v["detail"]


def test_a_record_from_an_undeclared_workstream_does_not_count():
    """Exact equality, not "nothing missing": a record from a workstream the
    manifest never declared is evidence about some other Cycle."""
    doc = _child_mf(workstreams=("spine",))
    v = _judge({"spine": _record(), "ghost": _record(workstream="ghost")}, doc)
    assert v["verdict"] == "blocked", v
    assert "records_present" in v["blocking"]
    assert any("ghost" in d for d in v["detail"])


def test_an_admitted_unit_owned_by_nobody_is_unclosable():
    """A unit assigned to the integration workstream (or to no one) owes no
    record, so nothing can ever report it done or cut."""
    import spq_manifest as mf

    doc = mf.seal({
        "schema_version": mf.SCHEMA_VERSION, "kind": mf.KIND,
        "cycle_id": "3-bbbbbbbb",
        "workstreams": [{"id": "spine"}, {"id": "integration", "integration": True}],
        "work_units": [{"id": "WU-01", "owner_workstream": "spine"},
                       {"id": "WU-99", "owner_workstream": "integration"}],
    })
    v = _judge({"spine": _record()}, doc)
    assert v["verdict"] == "blocked", v
    assert "manifest_closure" in v["blocking"]
    assert any("WU-99" in d for d in v["detail"])
