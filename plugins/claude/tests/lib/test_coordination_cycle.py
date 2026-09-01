"""Layer 1 — the Coordination Cycle manifest (#305).

A parent that coordinates several INDEPENDENT child Cycles on one codebase.
These tests pin the four properties that make it safe:

1. the parent and child id namespaces are disjoint, so a parent id can never
   reach the code paths that validate a Cycle id and raise;
2. children are pinned by `(cycle_id, manifest_hash, selected_sha)` and NOTHING
   keys on `cycle_seq`, which is what makes "different Cycle numbers and
   cadences" inherent rather than accommodated;
3. dropping a late child is a parent-side edit and touches no child state;
4. cross-Cycle condition strength lives in its own comparator, so the
   intra-Cycle dispatch gate is untouched.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import coordination_cycle as cc
import spq_manifest as mf
import spq_paths as sp
import spq_state_machine as m


GITIGNORE = (
    ".synaptory/*\n!.synaptory/sync/\n!.synaptory/cycles/\n"
    "!.synaptory/coordination-cycles/\n"
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    project = tmp_path / "release"
    project.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@e.co"],
                 ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)
    (project / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    (project / ".synaptory.yaml").write_text(
        'build_mode: "spq"\nspq:\n  workstreams:\n    - id: "spine"\n'
        "      shared_owner: true\n",
        encoding="utf-8",
    )
    (project / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=project, check=True,
                   capture_output=True)
    return project


# The Hano shape: three children at three DIFFERENT Cycle numbers, one of them
# fully independent and one waiting on a contract from a third.
PLATFORM = {"cycle_id": "12-aaaaaaaa", "cycle_seq": 12, "goal": "Platform",
            "manifest_hash": "sha256:" + "a" * 64, "selected_sha": "1" * 40,
            "integration_ref": "cycle/12-aaaaaaaa/integration"}
CONTRACT = {"cycle_id": "3-bbbbbbbb", "cycle_seq": 3, "goal": "Contract Mastery",
            "manifest_hash": "sha256:" + "b" * 64, "selected_sha": "2" * 40,
            "integration_ref": "cycle/3-bbbbbbbb/integration",
            "outputs": [{"kind": "contract", "id": "contracts/auth"}]}
EHR = {"cycle_id": "5-cccccccc", "cycle_seq": 5, "goal": "EHR",
       "manifest_hash": "sha256:" + "c" * 64, "selected_sha": "3" * 40,
       "integration_ref": "cycle/5-cccccccc/integration"}
EDGE = {"id": "EDGE-1", "waiter_cycle_id": "5-cccccccc",
        "waiter_unit_id": "WU-EHR-SYNC", "producer_cycle_id": "3-bbbbbbbb",
        "output": {"kind": "contract", "id": "contracts/auth"},
        "condition": "contract_published"}


def _open(project, children=(PLATFORM, CONTRACT, EHR), edges=(EDGE,), **kw):
    return m.open_coordination_cycle(
        str(project), release_goal="Q3", children=[dict(c) for c in children],
        dependency_edges=[dict(e) for e in edges], **kw
    )


def _manifest(children=(PLATFORM, CONTRACT, EHR), edges=(EDGE,), **kw):
    ccid = kw.pop("coordination_cycle_id", None) or sp.new_coordination_cycle_id(1)
    try:
        seq = sp.coordination_seq_of(ccid)
        ref = sp.coordination_integration_branch(ccid)
    except sp.IdentityError:
        # A deliberately malformed id under test. `validate` is what reports it;
        # building must not raise, or a bad manifest read off a git ref would be
        # a traceback rather than a refusal.
        seq, ref = 1, "coordination/%s/integration" % ccid
    base = dict(
        coordination_cycle_id=ccid,
        coordination_seq=seq,
        release_goal="Q3",
        baseline_sha="a" * 40,
        integration_ref=ref,
        children=[dict(c) for c in children],
        dependency_edges=[dict(e) for e in edges],
    )
    base.update(kw)
    return cc.seal(cc.build(**base))


# ── the hash rule is spq_manifest's, called not copied ─────────────────────


def test_the_hash_rule_is_the_one_definition():
    sealed = _manifest()
    assert cc.verify_hash(sealed)
    assert mf.verify_hash(sealed), "one hash rule, both documents"


def test_a_one_byte_edit_breaks_the_seal():
    sealed = _manifest()
    sealed["children"][0]["selected_sha"] = "9" * 40
    assert cc.verify_hash(sealed) is False


def test_revise_records_the_prior_hash_not_a_number():
    """A workstream holding an older revision can then be told PRECISELY that
    what it holds was superseded, rather than trusting a counter any writer
    could bump."""
    sealed = _manifest()
    prior = sealed["manifest_hash"]
    revised = cc.revise(sealed, {"release_goal": "Q4"}, revised_by="t")
    assert revised["supersedes"] == prior
    assert revised["manifest_revision"] == 2
    assert cc.verify_hash(revised)


# ── nothing keys on cycle_seq ──────────────────────────────────────────────


def test_children_at_three_different_cycle_numbers_validate():
    """The Hano case. 12, 3 and 5 in one release, opened independently."""
    assert cc.validate(_manifest(), require_baseline=False) == []


def test_two_children_at_the_SAME_cycle_number_validate():
    """The inverse, and the one that proves nothing keys on the number.

    Cycle numbers are allocated per clone, so two children can legitimately
    both be 'Cycle 12'. If any code path assumed otherwise, this is where it
    would show.
    """
    twin = dict(PLATFORM, cycle_id="12-dddddddd", goal="Reporting",
                manifest_hash="sha256:" + "d" * 64, selected_sha="4" * 40)
    manifest = _manifest(children=(PLATFORM, twin), edges=())
    assert cc.validate(manifest, require_baseline=False) == []
    assert cc.children_index(manifest).keys() == {"12-aaaaaaaa", "12-dddddddd"}


def test_duplicate_child_cycle_id_is_refused():
    problems = cc.validate(
        _manifest(children=(PLATFORM, dict(PLATFORM)), edges=()),
        require_baseline=False,
    )
    assert any("duplicate child cycle_id" in p for p in problems), problems


# ── pinning ────────────────────────────────────────────────────────────────


def test_an_included_child_without_a_manifest_hash_is_refused():
    """Pinning by number rather than by hash is what makes 'the release
    contains exactly this' unprovable."""
    problems = cc.validate(
        _manifest(children=(dict(PLATFORM, manifest_hash=""),), edges=()),
        require_baseline=False,
    )
    assert any("no sealed manifest_hash" in p for p in problems), problems


def test_an_included_child_without_a_selected_sha_is_refused():
    problems = cc.validate(
        _manifest(children=(dict(PLATFORM, selected_sha=""),), edges=()),
        require_baseline=False,
    )
    assert any("no selected_sha" in p for p in problems), problems


def test_a_dropped_child_needs_no_sha_but_does_need_a_reason():
    ok = _manifest(
        children=(PLATFORM, dict(EHR, state="dropped", selected_sha="",
                                 dropped_reason="slipped")),
        edges=(),
    )
    assert cc.validate(ok, require_baseline=False) == []

    silent = _manifest(
        children=(PLATFORM, dict(EHR, state="dropped", selected_sha="")), edges=()
    )
    problems = cc.validate(silent, require_baseline=False)
    assert any("dropped with no reason" in p for p in problems), problems


# ── edges ──────────────────────────────────────────────────────────────────


def test_an_edge_naming_a_non_child_is_refused():
    stray = dict(EDGE, producer_cycle_id="99-ffffffff")
    problems = cc.validate(_manifest(edges=(stray,)), require_baseline=False)
    assert any("not a child of this" in p for p in problems), problems


def test_a_self_edge_is_refused():
    loop = dict(EDGE, waiter_cycle_id="3-bbbbbbbb", producer_cycle_id="3-bbbbbbbb")
    problems = cc.validate(_manifest(edges=(loop,)), require_baseline=False)
    assert any("same Cycle on both ends" in p for p in problems), problems


def test_a_cross_cycle_dependency_loop_is_refused():
    """Unsatisfiable by construction, so it is caught when the release opens
    rather than discovered at the barrier after everyone built against it."""
    a = {"id": "E1", "waiter_cycle_id": "3-bbbbbbbb", "producer_cycle_id": "5-cccccccc",
         "producer_unit_id": "WU-A", "condition": "cycle_integrated"}
    b = {"id": "E2", "waiter_cycle_id": "5-cccccccc", "producer_cycle_id": "3-bbbbbbbb",
         "producer_unit_id": "WU-B", "condition": "cycle_integrated"}
    problems = cc.validate(
        _manifest(children=(CONTRACT, EHR), edges=(a, b)), require_baseline=False
    )
    assert any("dependency loop" in p for p in problems), problems


def test_an_edge_with_an_unknown_condition_is_refused():
    problems = cc.validate(
        _manifest(edges=(dict(EDGE, condition="vibes"),)), require_baseline=False
    )
    assert any("unknown condition" in p for p in problems), problems


def test_an_edge_naming_neither_a_unit_nor_an_output_is_refused():
    bare = {"id": "E1", "waiter_cycle_id": "5-cccccccc",
            "producer_cycle_id": "3-bbbbbbbb", "condition": "cycle_integrated"}
    problems = cc.validate(_manifest(edges=(bare,)), require_baseline=False)
    assert any("neither a producing unit nor an output" in p for p in problems), problems


# ── THE structural property: edges gate, children do not ───────────────────


def test_an_edgeless_child_is_gated_by_nothing():
    """AC: 'unrelated child Cycles are not forced through a single global
    readiness barrier.' Asserted as a property of the data, not left as an
    emergent behaviour nobody can point at."""
    manifest = _manifest()
    assert cc.gating_edges(manifest, "12-aaaaaaaa") == []
    assert cc.gating_edges(manifest, "3-bbbbbbbb") == []
    assert [e["id"] for e in cc.gating_edges(manifest, "5-cccccccc")] == ["EDGE-1"]


def test_a_release_with_no_edges_gates_nobody():
    manifest = _manifest(edges=())
    for child in manifest["children"]:
        assert cc.gating_edges(manifest, child["cycle_id"]) == []


def test_the_producer_side_is_addressable_too():
    manifest = _manifest()
    assert [e["id"] for e in cc.downstream_edges(manifest, "3-bbbbbbbb")] == ["EDGE-1"]
    assert cc.downstream_edges(manifest, "12-aaaaaaaa") == []


def test_edge_key_matches_the_producer_side_address():
    """A consumer's edge and a producer's event must spell one address."""
    manifest = _manifest()
    key = cc.edge_key(manifest["dependency_edges"][0])
    event_like = {"cycle_id": "3-bbbbbbbb", "unit_id": "",
                  "output": {"kind": "contract", "id": "contracts/auth"}}
    assert key == mf.cross_ref_key(event_like)
    assert key == "3-bbbbbbbb|out:contract:contracts/auth"


def test_declared_keys_covers_every_edge():
    keys = cc.declared_keys(_manifest())
    assert "3-bbbbbbbb|out:contract:contracts/auth" in keys


# ── the cross-Cycle comparator is separate, and agrees where it overlaps ───


def test_cross_and_intra_comparators_agree_on_shared_conditions():
    """Two comparators, deliberately. They must not DISAGREE about the
    conditions they both know, or the same event would mean different things at
    two levels of the same system."""
    shared = ("contract_published", "artifact_published", "integrated")
    for declared in shared:
        for observed in shared:
            assert cc.cross_satisfies(declared, observed) == mf.satisfies(
                declared, observed
            ), (declared, observed)


def test_cycle_integrated_satisfies_only_itself_when_declared():
    for other in cc.CROSS_CONDITIONS:
        if other == "cycle_integrated":
            continue
        assert cc.cross_satisfies("cycle_integrated", other) is False, other


def test_cycle_integrated_outranks_every_unit_level_condition_when_observed():
    """It is a statement about the WHOLE child increment."""
    for declared in ("integrated", "contract_published", "artifact_published"):
        assert cc.cross_satisfies(declared, "cycle_integrated") is True, declared


def test_environment_ready_satisfies_only_itself_in_both_directions():
    """No verifiable definition anywhere in the epic. It ships so the schema is
    forward-compatible, NOT so it can license dispatch."""
    for other in cc.CROSS_CONDITIONS:
        if other == "environment_ready":
            continue
        assert cc.cross_satisfies("environment_ready", other) is False, other
        assert cc.cross_satisfies(other, "environment_ready") is False, other


def test_a_weaker_event_never_satisfies_a_stronger_edge():
    assert cc.cross_satisfies("integrated", "contract_published") is False


def test_satisfies_set_is_precomputed_membership():
    """`story_dep_status` must do membership, not ranking, so it needs no import
    from this module and stays pure."""
    assert cc.satisfies_set("cycle_integrated") == sorted(
        c for c in cc.CROSS_CONDITIONS if cc.cross_satisfies(c, "cycle_integrated")
    )
    assert "environment_ready" not in cc.satisfies_set("cycle_integrated")


def test_an_unknown_condition_is_never_satisfied():
    assert cc.cross_satisfies("nonsense", "cycle_integrated") is False
    assert cc.cross_satisfies("cycle_integrated", "nonsense") is False


# ── identity and seq agreement ─────────────────────────────────────────────


def test_a_seq_disagreeing_with_the_id_is_refused():
    """The seq is the receipt-facing projection (`RELEASE-{seq}`), so a mismatch
    means the receipts name a different release than the storage."""
    problems = cc.validate(
        _manifest(coordination_cycle_id="cc-1-9f2c1ab3", coordination_seq=7),
        require_baseline=False,
    )
    assert any("disagrees with coordination_cycle_id" in p for p in problems), problems


def test_a_child_cycle_id_in_the_parent_slot_is_refused():
    problems = cc.validate(
        _manifest(coordination_cycle_id="12-aaaaaaaa"), require_baseline=False
    )
    assert any("coordination_cycle_id" in p and "invalid" in p
               for p in problems), problems


def test_the_release_id_is_recorded_not_only_derived():
    manifest = _manifest(coordination_cycle_id="cc-4-9f2c1ab3", coordination_seq=4)
    assert manifest["release_id"] == "RELEASE-4"


# ── opening against a real repo ────────────────────────────────────────────


def test_open_seals_pins_and_publishes(repo):
    manifest = _open(repo)
    ccid = manifest["coordination_cycle_id"]
    assert cc.verify_hash(manifest)
    assert sp.read_coordination_pin(str(repo)) == ccid
    local = Path(sp.coordination_manifest_path(str(repo), ccid))
    published = Path(sp.committed_coordination_manifest_path(str(repo), ccid))
    assert local.is_file() and published.is_file()
    assert json.loads(published.read_text())["manifest_hash"] == manifest["manifest_hash"]


def test_the_sealed_local_manifest_is_read_only(repo):
    manifest = _open(repo)
    local = Path(sp.coordination_manifest_path(str(repo), manifest["coordination_cycle_id"]))
    assert not os.access(local, os.W_OK)


def test_open_refuses_an_ignored_transport(repo):
    """A release manifest that writes cleanly and is invisible to every child
    clone is the worst failure mode available, because the parent looks correct
    locally."""
    (repo / ".gitignore").write_text(".synaptory/*\n", encoding="utf-8")
    with pytest.raises(cc.CoordinationError) as exc:
        _open(repo)
    assert "!.synaptory/coordination-cycles/" in str(exc.value)


def test_open_refuses_an_invalid_release(repo):
    with pytest.raises(cc.CoordinationError) as exc:
        _open(repo, children=(dict(PLATFORM, selected_sha=""),), edges=())
    assert "no selected_sha" in str(exc.value)


def test_the_coordination_index_is_separate_from_the_cycle_index(repo):
    """Sharing one would put a `cc-` id one careless read away from
    `resolve_identity`'s `valid_cycle_id`, which raises -- on a path that runs
    for every `read_state`, `next_action` and hook in the clone."""
    _open(repo)
    assert Path(sp.coordination_index_path(str(repo))).is_file()
    assert sp.read_index(str(repo))["cycles"] == []
    assert sp.read_index(str(repo))["current_cycle_id"] is None


def test_resolve_coordination_id_never_reads_active_spec(repo, monkeypatch):
    _open(repo)
    monkeypatch.setenv("SYNAPTORY_ACTIVE_SPEC", "cc-9-deadbeef")
    resolved = sp.resolve_coordination_id(str(repo))
    assert resolved != "cc-9-deadbeef"


# ── dropping a late child ──────────────────────────────────────────────────


def test_dropping_a_child_is_a_parent_side_edit_only(repo):
    """The AC: 'a late child can be removed from the parent manifest ... without
    corrupting other child Cycle state.'

    Asserted by byte-comparing everything outside the parent manifest.
    """
    manifest = _open(repo)
    ccid = manifest["coordination_cycle_id"]

    # Stand in for a child clone's state living in the same tree.
    child_state = repo / "child-cycle-state.json"
    child_state.write_text(json.dumps({"cycle_id": "5-cccccccc"}), encoding="utf-8")
    before = child_state.read_bytes()
    before_mtime = child_state.stat().st_mtime_ns

    revised = m.revise_coordination_manifest(
        str(repo), coordination_cycle_id=ccid,
        drop_child="5-cccccccc", reason="slipped the window",
    )

    assert child_state.read_bytes() == before
    assert child_state.stat().st_mtime_ns == before_mtime
    assert revised["manifest_revision"] == 2
    assert revised["supersedes"] == manifest["manifest_hash"]
    dropped = cc.children_index(revised)["5-cccccccc"]
    assert dropped["state"] == "dropped"
    assert dropped["dropped_reason"] == "slipped the window"
    assert dropped["selected_sha"] == ""
    assert cc.verify_hash(revised)


def test_dropping_a_producer_a_consumer_still_waits_on_is_refused(repo):
    _open(repo)
    with pytest.raises(cc.CoordinationError) as exc:
        m.revise_coordination_manifest(
            str(repo), drop_child="3-bbbbbbbb", reason="late"
        )
    assert "unsatisfiable" in str(exc.value)
    assert "drop the consumer too" in str(exc.value)


def test_dropping_the_consumer_first_then_the_producer_works(repo):
    _open(repo)
    m.revise_coordination_manifest(str(repo), drop_child="5-cccccccc", reason="late")
    revised = m.revise_coordination_manifest(
        str(repo), drop_child="3-bbbbbbbb", reason="late too"
    )
    assert revised["manifest_revision"] == 3
    assert [c["state"] for c in revised["children"]] == [
        "included", "dropped", "dropped"
    ]


def test_dropping_a_child_twice_is_idempotent(repo):
    _open(repo)
    first = m.revise_coordination_manifest(
        str(repo), drop_child="5-cccccccc", reason="late"
    )
    again = m.revise_coordination_manifest(
        str(repo), drop_child="5-cccccccc", reason="late"
    )
    assert again["manifest_hash"] == first["manifest_hash"]


def test_dropping_an_unknown_child_is_refused(repo):
    _open(repo)
    with pytest.raises(cc.CoordinationError) as exc:
        m.revise_coordination_manifest(str(repo), drop_child="99-ffffffff", reason="x")
    assert "not a child" in str(exc.value)


def test_a_drop_requires_a_reason(repo):
    _open(repo)
    with pytest.raises(cc.CoordinationError) as exc:
        m.revise_coordination_manifest(str(repo), drop_child="5-cccccccc", reason="")
    assert "--reason is required" in str(exc.value)


def test_revising_a_tampered_manifest_is_refused(repo):
    manifest = _open(repo)
    ccid = manifest["coordination_cycle_id"]
    local = Path(sp.coordination_manifest_path(str(repo), ccid))
    os.chmod(local, 0o644)
    tampered = json.loads(local.read_text())
    tampered["children"][0]["selected_sha"] = "9" * 40
    local.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(cc.CoordinationError) as exc:
        m.revise_coordination_manifest(str(repo), drop_child="5-cccccccc", reason="x")
    assert "does not match its own hash" in str(exc.value)


# ── status ─────────────────────────────────────────────────────────────────


def test_status_reports_gating_edges_per_child(repo):
    _open(repo)
    out = m.coordination_status(str(repo))
    gating = {c["cycle_id"]: c["gating_edges"] for c in out["children"]}
    assert gating == {
        "12-aaaaaaaa": [], "3-bbbbbbbb": [], "5-cccccccc": ["EDGE-1"]
    }


def test_status_on_a_clone_with_no_release_says_so(repo):
    out = m.coordination_status(str(repo))
    assert out["coordination_cycle_id"] is None
    assert "not part of a Coordination Cycle" in out["detail"]


def test_status_distinguishes_pinned_from_readable(repo):
    """A clone that pulled the pin but not the manifest is a real state, and it
    is not the same as 'no release here'."""
    manifest = _open(repo)
    ccid = manifest["coordination_cycle_id"]
    os.chmod(sp.coordination_manifest_path(str(repo), ccid), 0o644)
    os.remove(sp.coordination_manifest_path(str(repo), ccid))
    os.remove(sp.committed_coordination_manifest_path(str(repo), ccid))
    out = m.coordination_status(str(repo), coordination_cycle_id=ccid)
    assert out["manifest_present"] is False
    assert "only copy that travels" in out["detail"]


# ── receipts ───────────────────────────────────────────────────────────────


def test_a_coordination_clone_never_resolves_receipts_via_active_spec(
    repo, monkeypatch
):
    """#305 AC, verbatim: no new code 'depends on SYNAPTORY_ACTIVE_SPEC'.

    A coordination clone owns no child Cycle, so before this the SPQ branch of
    `receipts_dir_for` was skipped and control reached the Multi-Spec branch,
    which reads that variable. `RELEASE-1-po.json` would have landed in whatever
    spec slot happened to be exported.
    """
    import story_pipeline

    manifest = _open(repo)
    ccid = manifest["coordination_cycle_id"]
    monkeypatch.setenv("SYNAPTORY_ACTIVE_SPEC", "some-stale-spec")
    m.initialize(str(repo))

    resolved = story_pipeline.receipts_dir_for(str(repo), intended=True)
    assert "some-stale-spec" not in resolved
    assert resolved == sp.coordination_receipts_dir(str(repo), ccid)


def test_the_release_receipt_id_is_legal(repo):
    from receipt_validator import STORY_ID_PATTERN

    manifest = _open(repo)
    rid = sp.release_id(manifest["coordination_cycle_id"])
    assert STORY_ID_PATTERN.match(rid), rid
    assert rid == "RELEASE-1"


# ── a CHILD discovers the release it was included in (D1) ──────────────────
#
# Found by driving a real project end to end, not by a test: EHR held
# `dep_external` saying "this Cycle is not part of a Coordination Cycle" AFTER a
# verified event, WHILE the sealed release manifest sat in its own tree.
#
# A child opens its Cycle before the release exists, so its sealed manifest's
# `coordination_cycle_id` is written as None and stays None. It has no
# coordination pin and no index entry either — it did not open the release. The
# committed transport is the only place the binding reaches it, and nothing read
# it. Every cross-Cycle edge was unresolvable in the shipped flow.
#
# Exactly the failure `_cycle_id_for_seq` already solves one level down.


def _child_clone(tmp_path, release_manifest, cycle_id):
    """A child that has the committed release manifest and nothing else."""
    child = tmp_path / ("child-%s" % cycle_id)
    committed = child / ".synaptory" / "coordination-cycles" / release_manifest[
        "coordination_cycle_id"
    ]
    committed.mkdir(parents=True)
    (committed / "manifest.json").write_text(
        json.dumps(release_manifest), encoding="utf-8"
    )
    return child


def test_a_child_discovers_its_release_from_the_committed_transport(tmp_path):
    manifest = _manifest()
    child = _child_clone(tmp_path, manifest, PLATFORM["cycle_id"])
    assert sp.read_coordination_pin(str(child)) is None
    assert sp.read_coordination_index(str(child))[
        "current_coordination_cycle_id"
    ] is None
    resolved = sp.resolve_coordination_id(
        str(child), manifest={"cycle_id": PLATFORM["cycle_id"]}
    )
    assert resolved == manifest["coordination_cycle_id"]


def test_a_child_the_release_never_named_resolves_nothing(tmp_path):
    manifest = _manifest()
    child = _child_clone(tmp_path, manifest, "99-ffffffff")
    assert sp.resolve_coordination_id(
        str(child), manifest={"cycle_id": "99-ffffffff"}
    ) is None


def test_a_DROPPED_child_no_longer_resolves_the_release(tmp_path):
    """It is not in the release any more, so it must not resolve cross-Cycle
    edges through it."""
    dropped = [dict(c) for c in (PLATFORM, CONTRACT, EHR)]
    dropped[2] = dict(dropped[2], state="dropped", dropped_reason="late",
                      selected_sha="")
    manifest = _manifest(children=dropped, edges=())
    child = _child_clone(tmp_path, manifest, EHR["cycle_id"])
    assert sp.resolve_coordination_id(
        str(child), manifest={"cycle_id": EHR["cycle_id"]}
    ) is None


def test_two_releases_claiming_one_child_refuse_rather_than_guess(tmp_path):
    """Guessing would resolve a dependency against the wrong release. Same
    posture as the duplicate-sequence refusal in `_cycle_id_for_seq`."""
    first = _manifest(coordination_cycle_id="cc-1-aaaaaaaa", coordination_seq=1)
    second = _manifest(coordination_cycle_id="cc-2-bbbbbbbb", coordination_seq=2)
    child = _child_clone(tmp_path, first, PLATFORM["cycle_id"])
    other = (child / ".synaptory" / "coordination-cycles"
             / second["coordination_cycle_id"])
    other.mkdir(parents=True)
    (other / "manifest.json").write_text(json.dumps(second), encoding="utf-8")

    with pytest.raises(sp.IdentityError) as exc:
        sp.resolve_coordination_id(
            str(child), manifest={"cycle_id": PLATFORM["cycle_id"]}
        )
    assert "--coordination-cycle-id" in str(exc.value)


def test_an_explicit_id_still_wins_over_discovery(tmp_path):
    manifest = _manifest()
    child = _child_clone(tmp_path, manifest, PLATFORM["cycle_id"])
    assert sp.resolve_coordination_id(
        str(child), coordination_cycle_id="cc-9-deadbeef"
    ) == "cc-9-deadbeef"
