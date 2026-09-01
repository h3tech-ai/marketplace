"""Layer 1 — the cross-Cycle event ledger and the dependency gate it opens (#305).

Before this, `story_dep_status` held EVERY `external` edge with `dep_external`
and the detail said the Coordination Cycle owned it. That was safe and useless:
the dependent Work Unit was blocked with no mechanism that could ever unblock it.
This module is that mechanism, and these tests are mostly about the ways it must
still refuse.

The gate keeps its central property: every unresolvable state fails CLOSED, and
each one has a distinct reason code, because the operator's next move differs
completely between "revise the parent", "chase a producer", "refresh a cache"
and "ask for a verifiable condition".
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
import story_pipeline as story
from story_pipeline import (
    DEP_CONDITION_UNVERIFIED,
    DEP_CROSS_CYCLE_UNDECLARED,
    DEP_EXTERNAL,
    DEP_LEDGER_STALE,
    story_dep_status,
)

CONTRACT_ID = "3-bbbbbbbb"
EHR_ID = "5-cccccccc"
PLATFORM_ID = "12-aaaaaaaa"
CCID = "cc-1-9f2c1ab3"

CHILDREN = [
    {"cycle_id": PLATFORM_ID, "cycle_seq": 12, "goal": "Platform",
     "manifest_hash": "sha256:" + "a" * 64, "selected_sha": "1" * 40,
     "integration_ref": "cycle/%s/integration" % PLATFORM_ID},
    {"cycle_id": CONTRACT_ID, "cycle_seq": 3, "goal": "Contract Mastery",
     "manifest_hash": "sha256:" + "b" * 64, "selected_sha": "2" * 40,
     "integration_ref": "cycle/%s/integration" % CONTRACT_ID,
     "outputs": [{"kind": "contract", "id": "contracts/auth"}]},
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


def _event(parent, condition="contract_published", verified=True, **kw):
    child = cc.children_index(parent)[kw.pop("cycle_id", CONTRACT_ID)]
    defaults = dict(
        coordination_cycle_id=parent["coordination_cycle_id"],
        coordination_manifest_hash=parent["manifest_hash"],
        cycle_id=child["cycle_id"],
        cycle_manifest_hash=child["manifest_hash"],
        condition=condition,
        output={"kind": "contract", "id": "contracts/auth",
                "digest": "sha256:deadbeef"},
        verification={"verified": verified, "detail": "test"},
        published_at="2026-08-27T10:00:00Z",
    )
    defaults.update(kw)
    return cc.build_event(**defaults)


def _snap(events, fresh=True):
    return {"coordination_cycle_id": CCID, "events": list(events),
            "fresh": fresh, "detail": "cache is current" if fresh else "old"}


# ── the address is one spelling on both sides ──────────────────────────────


def test_the_two_cross_ref_key_spellings_agree():
    """`story_pipeline` duplicates the key function to stay import-free on the
    hot path. Two spellings of one address is how a producer and a consumer stop
    matching, so they are checked against each other."""
    for cid, uid, out in (
        ("3-bbbbbbbb", "WU-01", None),
        ("3-bbbbbbbb", "", {"kind": "contract", "id": "contracts/auth"}),
        ("3-bbbbbbbb", "", None),
        ("3-bbbbbbbb", "WU-01", {"kind": "contract", "id": "contracts/auth"}),
        ("", "", {}),
    ):
        assert story.cross_ref_key(cid, uid, out) == mf.cross_ref_key(
            {"cycle_id": cid, "unit_id": uid, "output": out}
        ), (cid, uid, out)


# ── satisfied_map: five fail-closed filters ────────────────────────────────


def test_a_verified_event_at_the_declared_address_resolves():
    parent = _parent()
    got = cc.satisfied_map(_snap([_event(parent)]), parent)
    assert KEY in got
    assert got[KEY]["condition"] == "contract_published"
    assert got[KEY]["verified"] is True
    assert "contract_published" in got[KEY]["satisfies"]


def test_an_event_from_a_different_release_is_ignored():
    parent = _parent()
    alien = _event(parent, coordination_cycle_id="cc-9-deadbeef")
    assert cc.satisfied_map(_snap([alien]), parent) == {}


def test_an_event_against_a_superseded_parent_revision_is_ignored():
    """It may describe a child that has since been dropped. Letting it satisfy
    an edge in a later revision would resolve against a release that is gone."""
    parent = _parent()
    stale = _event(parent, coordination_manifest_hash="sha256:" + "0" * 64)
    assert cc.satisfied_map(_snap([stale]), parent) == {}


def test_an_event_about_a_dropped_child_is_ignored():
    dropped = [dict(c) for c in CHILDREN]
    dropped[1] = dict(dropped[1], state="dropped", dropped_reason="late",
                      selected_sha="")
    parent = _parent(children=dropped, edges=[])
    # Build the event against the ORIGINAL parent so its hash matches nothing;
    # re-stamp it onto the dropped-child revision to isolate the child filter.
    event = _event(parent, coordination_manifest_hash=parent["manifest_hash"])
    assert cc.satisfied_map(_snap([event]), parent) == {}


def test_an_event_whose_child_manifest_hash_moved_is_ignored():
    """The release pinned an exact child increment; an event about a different
    revision is about a different admitted set."""
    parent = _parent()
    moved = _event(parent, cycle_manifest_hash="sha256:" + "9" * 64)
    assert cc.satisfied_map(_snap([moved]), parent) == {}


def test_an_event_at_an_undeclared_address_is_ignored():
    """A child cannot invent a coordination the release never agreed to."""
    parent = _parent(edges=[])
    assert cc.satisfied_map(_snap([_event(parent)]), parent) == {}


def test_the_strongest_condition_wins():
    parent = _parent(edges=[dict(EDGES[0], condition="contract_published")])
    weak = _event(parent, condition="contract_published")
    strong = _event(parent, condition="cycle_integrated",
                    output={"kind": "contract", "id": "contracts/auth"})
    got = cc.satisfied_map(_snap([strong, weak]), parent)
    assert got[KEY]["condition"] == "cycle_integrated"


def test_two_children_sharing_a_unit_id_never_collide():
    """The reason cross-Cycle satisfaction is NOT merged into the intra-Cycle
    map, which is keyed on a bare unit_id. Two child Cycles in one codebase
    routinely admit the same ids."""
    edges = [
        {"id": "E1", "waiter_cycle_id": EHR_ID, "producer_cycle_id": CONTRACT_ID,
         "producer_unit_id": "WU-01", "condition": "cycle_integrated"},
        {"id": "E2", "waiter_cycle_id": EHR_ID, "producer_cycle_id": PLATFORM_ID,
         "producer_unit_id": "WU-01", "condition": "cycle_integrated"},
    ]
    parent = _parent(edges=edges)
    only_contract = _event(parent, condition="cycle_integrated",
                           unit_id="WU-01", output=None)
    got = cc.satisfied_map(_snap([only_contract]), parent)
    assert "%s|unit:WU-01" % CONTRACT_ID in got
    assert "%s|unit:WU-01" % PLATFORM_ID not in got, (
        "Contract's WU-01 must not satisfy an edge waiting on Platform's WU-01"
    )


# ── the dependency gate ────────────────────────────────────────────────────


def _ctx(parent, events=(), fresh=True, accept_unverified=False):
    snap = _snap(events, fresh=fresh)
    return {
        "cycle_id": EHR_ID, "workstream_id": "spine",
        "manifest_present": True, "manifest_hash": "sha256:" + "c" * 64,
        "owners": {"WU-EHR-SYNC": "spine"}, "admitted": ["WU-EHR-SYNC"],
        "satisfied": {}, "ledger_fresh": True,
        "accept_unverified": accept_unverified,
        "coordination_cycle_id": CCID,
        "cross_cycle": cc.satisfied_map(snap, parent),
        "declared_cross_keys": tuple(cc.declared_keys(parent)),
        "coordination_fresh": fresh,
        "coordination_detail": snap["detail"],
    }


def _story_waiting(condition="contract_published", **external):
    ext = {"cycle_id": CONTRACT_ID,
           "output": {"kind": "contract", "id": "contracts/auth"},
           "condition": condition}
    ext.update(external)
    return {"id": "WU-EHR-SYNC", "state": "queued",
            "manifest_hash": "sha256:" + "c" * 64,
            "depends_on": [{"external": ext}]}


def _state():
    return {"build_mode": "spq", "current_stories": [_story_waiting()]}


def test_a_verified_upstream_contract_unblocks_a_dependent_child():
    """The AC: an upstream contract unblocks a dependent child Cycle WITHOUT the
    whole upstream Cycle finishing."""
    parent = _parent()
    verdict = story_dep_status(
        _state(), _story_waiting(), dep_context=_ctx(parent, [_event(parent)])
    )
    assert verdict["met"] is True, verdict


def test_no_event_yet_holds_and_names_the_producer():
    parent = _parent()
    verdict = story_dep_status(_state(), _story_waiting(), dep_context=_ctx(parent))
    assert verdict["met"] is False
    assert verdict["unmet"][0]["reason_code"] == DEP_EXTERNAL
    assert CONTRACT_ID in verdict["unmet"][0]["detail"]


def test_a_stale_coordination_cache_fails_closed_with_its_own_code():
    parent = _parent()
    verdict = story_dep_status(
        _state(), _story_waiting(), dep_context=_ctx(parent, fresh=False)
    )
    assert verdict["met"] is False
    assert verdict["unmet"][0]["reason_code"] == DEP_LEDGER_STALE
    assert "refresh_coordination" in verdict["unmet"][0]["detail"]


def test_an_undeclared_edge_gets_its_own_code_naming_the_parent():
    """The operator action is revise the PARENT -- not the child, not the
    ledger, not a typo. A shared code would send them to the wrong place."""
    parent = _parent(edges=[])
    verdict = story_dep_status(_state(), _story_waiting(), dep_context=_ctx(parent))
    assert verdict["unmet"][0]["reason_code"] == DEP_CROSS_CYCLE_UNDECLARED
    assert "parent manifest" in verdict["unmet"][0]["detail"]
    assert CCID in verdict["unmet"][0]["detail"]


def test_a_weaker_event_does_not_satisfy_a_stronger_edge():
    strong = [dict(EDGES[0], condition="cycle_integrated")]
    parent = _parent(edges=strong)
    verdict = story_dep_status(
        _state(),
        _story_waiting(condition="cycle_integrated"),
        dep_context=_ctx(parent, [_event(parent, condition="contract_published")]),
    )
    assert verdict["met"] is False
    assert verdict["unmet"][0]["reason_code"] == DEP_EXTERNAL
    assert "does not meet the declared condition" in verdict["unmet"][0]["detail"]


def test_an_unverified_event_fails_closed_by_default():
    parent = _parent()
    verdict = story_dep_status(
        _state(), _story_waiting(),
        dep_context=_ctx(parent, [_event(parent, verified=False)]),
    )
    assert verdict["unmet"][0]["reason_code"] == DEP_CONDITION_UNVERIFIED
    assert "claim, not proof" in verdict["unmet"][0]["detail"]


def test_an_unverified_event_resolves_only_on_explicit_opt_in():
    parent = _parent()
    verdict = story_dep_status(
        _state(), _story_waiting(),
        dep_context=_ctx(parent, [_event(parent, verified=False)],
                         accept_unverified=True),
    )
    assert verdict["met"] is True


def test_a_cycle_with_no_parent_still_holds_every_external_edge():
    """#305 made these RESOLVABLE, not automatic.

    No `manifest_hash` on the story: with one, the stale-projection
    short-circuit fires first and correctly refuses to speak about ANY edge.
    """
    orphan = _story_waiting()
    orphan.pop("manifest_hash")
    verdict = story_dep_status(_state(), orphan, dep_context={})
    assert verdict["met"] is False
    assert verdict["unmet"][0]["reason_code"] == DEP_EXTERNAL


# ── purity ─────────────────────────────────────────────────────────────────


def test_story_dep_status_is_still_pure(tmp_path, monkeypatch):
    """The whole design rests on this boundary. All impurity lives in
    `dep_context`; the gate is a function of its arguments.

    Enforced by making every escape hatch explode: no cwd, no subprocess.
    """
    def explode(*a, **k):
        raise AssertionError("story_dep_status touched a subprocess")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.chdir(tmp_path)
    parent = _parent()
    verdict = story_dep_status(
        _state(), _story_waiting(), dep_context=_ctx(parent, [_event(parent)])
    )
    assert verdict["met"] is True


def test_story_dep_status_imports_nothing_from_coordination_cycle():
    """Condition strength arrives precomputed as a `satisfies` list, so the gate
    does membership rather than ranking."""
    import ast
    import inspect

    src = inspect.getsource(story_dep_status)
    tree = ast.parse(src.lstrip())
    imported = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
    ]
    assert imported == [], ast.dump(imported[0]) if imported else ""


# ── publish: producer-side refusals ────────────────────────────────────────


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    project = tmp_path / "release"
    project.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@e.co"],
                 ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)
    (project / ".gitignore").write_text(
        ".synaptory/*\n!.synaptory/sync/\n!.synaptory/cycles/\n"
        "!.synaptory/coordination-cycles/\n", encoding="utf-8",
    )
    (project / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=project, check=True,
                   capture_output=True)
    return project


def _digest_script(project: Path, value: str) -> None:
    scripts = project / "scripts"
    scripts.mkdir(exist_ok=True)
    script = scripts / "shared-digest.sh"
    script.write_text("#!/usr/bin/env bash\necho %s\n" % value, encoding="utf-8")
    script.chmod(0o755)


def _publish(repo, parent, **kw):
    args = dict(
        coordination_cycle_id=CCID, manifest=parent, cycle_id=CONTRACT_ID,
        condition="contract_published",
        cycle_manifest_hash="sha256:" + "b" * 64,
        output={"kind": "contract", "id": "contracts/auth",
                "digest": "sha256:deadbeef"},
    )
    args.update(kw)
    return cc.publish(str(repo), **args)


def test_publish_verifies_the_digest_and_writes_a_committed_event(repo):
    _digest_script(repo, "sha256:deadbeef")
    parent = _parent()
    result = _publish(repo, parent)
    assert result["event"]["verification"]["verified"] is True
    assert Path(result["path"]).is_file()
    assert result["key"] == KEY
    # Agents may not push; the exact command is what keeps the mandatory human
    # step from looking like a bug.
    assert "git push" in result["push_command"]


def test_publish_refuses_a_falsified_digest(repo):
    _digest_script(repo, "sha256:something-else")
    parent = _parent()
    with pytest.raises(cc.CoordinationError) as exc:
        _publish(repo, parent)
    assert "does not match the recomputed" in str(exc.value)


def test_publish_refuses_an_undeclared_address(repo):
    _digest_script(repo, "sha256:deadbeef")
    parent = _parent(edges=[])
    with pytest.raises(cc.CoordinationError) as exc:
        _publish(repo, parent)
    assert "parent-manifest revision, not an event" in str(exc.value)


def test_publish_refuses_a_dropped_child(repo):
    _digest_script(repo, "sha256:deadbeef")
    dropped = [dict(c) for c in CHILDREN]
    dropped[1] = dict(dropped[1], state="dropped", dropped_reason="late",
                      selected_sha="")
    parent = _parent(children=dropped)
    with pytest.raises(cc.CoordinationError) as exc:
        _publish(repo, parent)
    assert "dropped from this release" in str(exc.value)


def test_publish_refuses_a_child_pinned_at_another_revision(repo):
    _digest_script(repo, "sha256:deadbeef")
    parent = _parent()
    with pytest.raises(cc.CoordinationError) as exc:
        _publish(repo, parent, cycle_manifest_hash="sha256:" + "9" * 64)
    assert "different admitted set" in str(exc.value)


def test_publish_refuses_a_non_child(repo):
    parent = _parent()
    with pytest.raises(cc.CoordinationError) as exc:
        _publish(repo, parent, cycle_id="99-ffffffff")
    assert "is not a child of" in str(exc.value)


def test_publish_refuses_a_tampered_parent_manifest(repo):
    parent = _parent()
    parent["children"][0]["selected_sha"] = "9" * 40
    with pytest.raises(cc.CoordinationError) as exc:
        _publish(repo, parent)
    assert "does not match its own hash" in str(exc.value)


def test_publish_refuses_an_unknown_condition(repo):
    parent = _parent()
    with pytest.raises(cc.CoordinationError) as exc:
        _publish(repo, parent, condition="vibes")
    assert "unknown cross-Cycle condition" in str(exc.value)


def test_cycle_integrated_must_name_the_sha_the_release_pinned(repo):
    """A whole-Cycle claim has to be about the increment the release selected,
    not some other commit that happens to be on the branch."""
    parent = _parent(
        edges=[{"id": "E1", "waiter_cycle_id": EHR_ID, "producer_cycle_id": CONTRACT_ID,
                "producer_unit_id": "WU-01", "condition": "cycle_integrated"}]
    )
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "branch", "cycle/%s/integration" % CONTRACT_ID],
                   cwd=repo, check=True, capture_output=True)
    with pytest.raises(cc.CoordinationError) as exc:
        _publish(repo, parent, condition="cycle_integrated", unit_id="WU-01",
                 output=None, commit_sha=head)
    assert "the release pins" in str(exc.value)


def test_intra_cycle_publish_still_refuses_cycle_integrated():
    """Regression pin. The cross-Cycle writer is a DIFFERENT module writing to a
    DIFFERENT tree, so there was never a bypass to build here."""
    import spq_ledger

    with pytest.raises(spq_ledger.LedgerError) as exc:
        spq_ledger.publish(
            "/nonexistent", cycle_id="1-aaaaaaaa", manifest={},
            workstream_id="spine", unit_id="WU-1", condition="cycle_integrated",
        )
    assert "Coordination Cycle" in str(exc.value)


# ── an output-addressed edge need not name its producer (D2) ───────────────
#
# Found by driving a real project: EHR could not OPEN Cycle 5 while declaring a
# contract dependency, because validation demanded a `cycle_id` that only exists
# once Contract Mastery has run `open_cycle`. That is a hard ordering dependency
# between Cycles the epic calls independent, and it made the output-addressed
# form the epic itself prefers unusable.


def _story_by_output(condition="contract_published"):
    return {"id": "WU-EHR-SYNC", "state": "queued",
            "manifest_hash": "sha256:" + "c" * 64,
            "depends_on": [{"external": {
                "output": {"kind": "contract", "id": "contracts/auth"},
                "condition": condition,
            }}]}


def test_a_producer_less_edge_resolves_through_the_declared_set():
    """The release already says who supplies the output; the consumer only has
    to name what it waits on."""
    parent = _parent()
    verdict = story_dep_status(
        _state(), _story_by_output(),
        dep_context=_ctx(parent, [_event(parent)]),
    )
    assert verdict["met"] is True, verdict


def test_a_producer_less_edge_still_holds_before_the_event():
    parent = _parent()
    verdict = story_dep_status(
        _state(), _story_by_output(), dep_context=_ctx(parent)
    )
    assert verdict["met"] is False
    assert verdict["unmet"][0]["reason_code"] == DEP_EXTERNAL


def test_a_producer_less_edge_the_release_never_declared_fails_closed():
    parent = _parent(edges=[])
    verdict = story_dep_status(
        _state(), _story_by_output(), dep_context=_ctx(parent)
    )
    assert verdict["unmet"][0]["reason_code"] == DEP_CROSS_CYCLE_UNDECLARED


def test_two_producers_for_one_output_fail_closed_rather_than_guess():
    """Guessing would satisfy the edge from the wrong Cycle. Ambiguity is a
    release-manifest problem, and the hold has to say so."""
    both = [
        dict(EDGES[0]),
        {"id": "EDGE-2", "waiter_cycle_id": EHR_ID, "producer_cycle_id": PLATFORM_ID,
         "output": {"kind": "contract", "id": "contracts/auth"},
         "condition": "contract_published"},
    ]
    parent = _parent(edges=both)
    verdict = story_dep_status(
        _state(), _story_by_output(), dep_context=_ctx(parent, [_event(parent)])
    )
    assert verdict["met"] is False
    assert verdict["unmet"][0]["reason_code"] == DEP_CROSS_CYCLE_UNDECLARED
    assert "ambiguous" in verdict["unmet"][0]["detail"]


def test_a_unit_addressed_edge_still_needs_its_producer():
    """Unchanged: a bare unit id has no other way to say which Cycle it is in,
    and that is the form the local-unit smuggle takes."""
    import spq_manifest as mf_mod

    problems = mf_mod.validate(
        mf_mod.seal(mf_mod.build(
            cycle_id=EHR_ID, cycle_seq=5, goal="g", baseline_sha="a" * 40,
            integration_ref="cycle/%s/integration" % EHR_ID,
            workstreams=[{"id": "spine", "shared_owner": True}],
            work_units=[{"id": "WU-EHR", "owner_workstream": "spine",
                         "depends_on": [{"external": {"unit_id": "WU-UP"}}]}],
        )),
        require_baseline=False,
    )
    assert any("neither a cycle_id nor an output" in p for p in problems), problems


# ── gating: found by driving a real Codex agent (#305) ─────────────────────


def test_refreshing_a_cache_is_not_a_mutation():
    """The fail-closed gate must not be self-defeating.

    `dep_ledger_stale` holds a Work Unit and tells the operator to run refresh.
    With refresh behind the execution-readiness gate, refresh then refused
    because the clone had not installed nine managed agent profiles — so a clone
    could not find out whether its own dependency was satisfied without
    provisioning to dispatch agents it was never going to dispatch. A real Codex
    agent hit exactly that and correctly reported the unit blocked with no way
    forward.

    Both refresh verbs rebuild a cache their own modules describe as "never
    authoritative and can be deleted safely". They publish nothing.
    """
    import spq_mcp

    assert "spq_refresh_ledger" not in spq_mcp.MUTATING
    assert "spq_refresh_coordination" not in spq_mcp.MUTATING


def test_publishing_evidence_is_still_a_mutation():
    """The distinction is publish-vs-cache, not writes-a-file. An event others
    rely on stays behind the readiness gate."""
    import spq_mcp

    assert {"spq_publish_event", "spq_publish_cross_cycle_event",
            "spq_cut_work_unit"} <= spq_mcp.MUTATING


def test_reads_are_never_gated_on_readiness():
    """Refusing to let an operator LOOK at why their release is blocked helps
    nobody — the module's own stated rule."""
    import spq_mcp

    for read in ("spq_get_cycle", "spq_dependency_ledger", "spq_manifest_validate",
                 "spq_coordination_status", "spq_release_readiness"):
        assert read not in spq_mcp.MUTATING, read


# ── cycle_integrated needs the child's Sync evidence (#305 review, P1 #2) ────
#
# The reviewer's repro, verbatim: "a temporary repository containing only an
# integration branch and no Sync directory still returns verified=true". That
# is ancestry standing in for greenness -- a commit being reachable from a
# branch says nothing about whether the Cycle that produced it passed its own
# barrier, so a dependent child could be unblocked by a whole-Cycle claim
# nobody verified.


def _integration_branch(repo: Path, cycle_id: str) -> str:
    """A child integration ref whose HEAD is the current commit."""
    subprocess.run(["git", "branch", "cycle/%s/integration" % cycle_id],
                   cwd=repo, check=True, capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit_readiness(repo: Path, cycle_id: str, ws: str, record: dict) -> None:
    d = repo / ".synaptory" / "cycles" / cycle_id / "sync"
    d.mkdir(parents=True, exist_ok=True)
    (d / ("%s.json" % ws)).write_text(json.dumps(record), encoding="utf-8")


def _green_record(ws: str, manifest_hash: str) -> dict:
    return {
        "workstream": ws,
        "manifest_hash": manifest_hash,
        "work_unit_ids": {"admitted": ["WU-01"], "done": ["WU-01"], "cut": []},
        "dependency_closure": {"satisfied": [], "unsatisfied": []},
        "regression": {"command": "make test", "exit_code": 0, "skipped": False},
    }


def _child(sha: str, manifest_hash: str) -> dict:
    return {
        "cycle_id": CONTRACT_ID,
        "integration_ref": "cycle/%s/integration" % CONTRACT_ID,
        "selected_sha": sha,
        "manifest_hash": manifest_hash,
    }


def test_cycle_integrated_is_not_verified_without_any_sync_evidence(repo: Path):
    """The reviewer's exact shape: an integration branch, no sync directory."""
    hash_a = "sha256:" + "a" * 64
    sha = _integration_branch(repo, CONTRACT_ID)

    got = cc.verify_cross_condition(
        str(repo),
        manifest={},
        child=_child(sha, hash_a),
        condition="cycle_integrated",
        commit_sha=sha,
    )
    assert got["verified"] is False, (
        "ancestry alone must not verify cycle_integrated: %r" % got
    )
    assert "sync" in got["detail"].lower() or "readiness" in got["detail"].lower(), got


def test_cycle_integrated_is_not_verified_when_the_child_is_not_green(repo: Path):
    hash_a = "sha256:" + "a" * 64
    _commit_readiness(repo, CONTRACT_ID, "spine", dict(
        _green_record("spine", hash_a),
        regression={"command": "make test", "exit_code": 1, "skipped": False},
    ))
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "readiness"], cwd=repo, check=True,
                   capture_output=True)
    sha = _integration_branch(repo, CONTRACT_ID)

    got = cc.verify_cross_condition(
        str(repo), manifest={}, child=_child(sha, hash_a),
        condition="cycle_integrated", commit_sha=sha,
    )
    assert got["verified"] is False
    assert "regression_green" in json.dumps(got.get("sync_evidence") or {}), got


def test_cycle_integrated_verifies_with_ancestry_and_green_sync_evidence(repo: Path):
    doc = _child_manifest(CONTRACT_ID, "", [{"id": "spine"}],
                          [{"id": "WU-01", "owner_workstream": "spine"}])
    hash_a = _seal_of(doc)
    _commit_child_manifest(repo, CONTRACT_ID, doc)
    _commit_readiness(repo, CONTRACT_ID, "spine", _green_record("spine", hash_a))
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "readiness"], cwd=repo, check=True,
                   capture_output=True)
    sha = _integration_branch(repo, CONTRACT_ID)

    got = cc.verify_cross_condition(
        str(repo), manifest={}, child=_child(sha, hash_a),
        condition="cycle_integrated", commit_sha=sha,
    )
    assert got["verified"] is True, got
    assert got["sync_evidence"]["verdict"] == "green"


def test_cycle_integrated_refuses_records_that_name_another_manifest(repo: Path):
    """Green records for a DIFFERENT manifest revision do not prove this pin."""
    _commit_readiness(repo, CONTRACT_ID, "spine",
                      _green_record("spine", "sha256:" + "b" * 64))
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "readiness"], cwd=repo, check=True,
                   capture_output=True)
    sha = _integration_branch(repo, CONTRACT_ID)

    got = cc.verify_cross_condition(
        str(repo), manifest={}, child=_child(sha, "sha256:" + "a" * 64),
        condition="cycle_integrated", commit_sha=sha,
    )
    assert got["verified"] is False
    assert "manifest_agreement" in json.dumps(got.get("sync_evidence") or {}), got


# ── a failed committed-manifest write is not a success (#305 review, P1 #3) ──
#
# The reviewer's repro: "simulating that write failure returns the new
# coordination id, records it as active, and leaves published_exists=false".
# The committed copy is the ONLY cross-clone transport -- the code says so two
# lines above the swallow, and refuses outright when that path is merely
# gitignored. Swallowing the real write error waved the identical end state
# through by another route.


def test_a_failed_committed_write_does_not_open_the_release(repo: Path, monkeypatch):
    import state_store

    real = state_store.write_json_atomic
    published_dir = os.path.join(".synaptory", "coordination-cycles")

    def explode(path, payload, **kw):
        if published_dir in str(path):
            raise OSError(28, "No space left on device")
        return real(path, payload, **kw)

    monkeypatch.setattr(state_store, "write_json_atomic", explode)

    with pytest.raises(cc.CoordinationError) as excinfo:
        cc.open_coordination(
            str(repo),
            release_goal="ship it",
            children=[{"cycle_id": CONTRACT_ID,
                       "manifest_hash": "sha256:" + "a" * 64,
                       "selected_sha": "d" * 40,
                       "integration_ref": "cycle/%s/integration" % CONTRACT_ID}],
        )
    assert "committed release manifest" in str(excinfo.value)

    # Nothing may be left advanced: no pin, no index entry, no orphan manifest.
    assert sp.read_coordination_pin(str(repo)) in (None, ""), (
        "a release that never reached its transport must not be pinned active"
    )
    index = repo / ".synaptory" / ".orchestrator" / "spq" / "coordination-cycles"
    leftovers = sorted(p.name for p in index.glob("cc-*")) if index.exists() else []
    assert leftovers == [], (
        "the local manifest for a failed open must not survive: %s" % leftovers
    )


# ── the quorum must be the child's, not "at least one record" (P1 #1) ───────
#
# Reviewer's repro: a Cycle configured with workstreams `spine` and `edge`,
# with only a valid `spine.json` committed, returned verified=true and reported
# the evidence "green across spine". Both the release gate and a dependent
# child could pass while the child's own barrier would block on the missing
# `edge` record.


def _child_manifest(cycle_id: str, manifest_hash: str, workstreams, units) -> dict:
    """A GENUINELY SEALED child manifest.

    The judge recomputes the seal, so a hand-stamped `manifest_hash` is refused
    -- which is the point of the check. `manifest_hash` is honoured only when a
    test means to tamper; otherwise the computed value wins and the caller pins
    against it via `_seal_of`.
    """
    doc = mf.seal({
        "schema_version": mf.SCHEMA_VERSION,
        "kind": mf.KIND,
        "cycle_id": cycle_id,
        "workstreams": list(workstreams),
        "work_units": list(units),
    })
    return doc


def _seal_of(doc: dict) -> str:
    return str(doc.get("manifest_hash") or "")


def _commit_child_manifest(repo: Path, cycle_id: str, manifest: dict) -> None:
    d = repo / ".synaptory" / "cycles" / cycle_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _two_workstream_child(repo: Path, *, commit_edge: bool):
    """Returns `(selected_sha, real_manifest_seal)`.

    The seal is COMPUTED, not chosen: the judge recomputes it, so a fixture
    that picks its own hash would be refused for the right reason but tell us
    nothing about the quorum.
    """
    doc = _child_manifest(
        CONTRACT_ID, "",
        [{"id": "spine"}, {"id": "edge"}, {"id": "integration", "integration": True}],
        [{"id": "WU-01", "owner_workstream": "spine"},
         {"id": "WU-02", "owner_workstream": "edge"}],
    )
    seal = _seal_of(doc)
    _commit_child_manifest(repo, CONTRACT_ID, doc)
    _commit_readiness(repo, CONTRACT_ID, "spine", dict(
        _green_record("spine", seal),
        work_unit_ids={"admitted": ["WU-01"], "done": ["WU-01"], "cut": []}))
    if commit_edge:
        _commit_readiness(repo, CONTRACT_ID, "edge", dict(
            _green_record("edge", seal),
            work_unit_ids={"admitted": ["WU-02"], "done": ["WU-02"], "cut": []}))
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "child state"], cwd=repo, check=True,
                   capture_output=True)
    return _integration_branch(repo, CONTRACT_ID), seal


def test_a_missing_workstream_record_is_not_a_green_child(repo: Path):
    """The reviewer's exact shape: two workstreams declared, one record."""
    sha, seal = _two_workstream_child(repo, commit_edge=False)

    got = cc.verify_cross_condition(
        str(repo), manifest={}, child=_child(sha, seal),
        condition="cycle_integrated", commit_sha=sha,
    )
    assert got["verified"] is False, (
        "one record out of two declared workstreams is not a quorum: %r" % got
    )
    assert "records_present" in (got.get("sync_evidence") or {}).get("blocking", []), got
    assert "edge" in json.dumps(got.get("sync_evidence") or {})


def test_the_full_workstream_quorum_is_green(repo: Path):
    sha, seal = _two_workstream_child(repo, commit_edge=True)

    got = cc.verify_cross_condition(
        str(repo), manifest={}, child=_child(sha, seal),
        condition="cycle_integrated", commit_sha=sha,
    )
    assert got["verified"] is True, got
    assert got["sync_evidence"]["expected_workstreams"] == ["edge", "spine"]


def test_closure_is_measured_against_the_sealed_admitted_set(repo: Path):
    """A record cannot narrow the admitted set by reporting a smaller one."""
    doc = _child_manifest(
        CONTRACT_ID, "", [{"id": "spine"}],
        # The manifest admits two units for spine...
        [{"id": "WU-01", "owner_workstream": "spine"},
         {"id": "WU-09", "owner_workstream": "spine"}])
    hash_a = _seal_of(doc)
    _commit_child_manifest(repo, CONTRACT_ID, doc)
    # ...but the record only speaks to one, and is self-consistent about it.
    _commit_readiness(repo, CONTRACT_ID, "spine", dict(
        _green_record("spine", hash_a),
        work_unit_ids={"admitted": ["WU-01"], "done": ["WU-01"], "cut": []},
    ))
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "child state"], cwd=repo, check=True,
                   capture_output=True)
    sha = _integration_branch(repo, CONTRACT_ID)

    got = cc.verify_cross_condition(
        str(repo), manifest={}, child=_child(sha, hash_a),
        condition="cycle_integrated", commit_sha=sha,
    )
    assert got["verified"] is False
    ev = got.get("sync_evidence") or {}
    assert "manifest_closure" in ev.get("blocking", []), got
    assert "WU-09" in json.dumps(ev), got


def test_evidence_without_the_child_manifest_cannot_be_green():
    """No sealed manifest means the quorum is unknowable, so not provable."""
    v = cc.sync_evidence_verdict(
        {"spine": _green_record("spine", "sha256:" + "a" * 64)},
        pinned_manifest_hash="sha256:" + "a" * 64,
    )
    assert v["verdict"] == "blocked"
    assert "records_present" in v["blocking"]


# ── revision rollback with NO prior local copy (P1 #2) ──────────────────────
#
# `read_manifest` supports a clone whose only copy is the committed transport.
# There `prior` is None, and skipping the rollback left a brand-new local
# revision beside the older committed one — and reads prefer the local copy, so
# the clone believed in a release no child could see.


def test_a_failed_revision_write_leaves_no_local_copy_when_there_was_none(
    repo: Path, monkeypatch
):
    import state_store

    ccid = cc.open_coordination(
        str(repo), release_goal="ship it",
        children=[{"cycle_id": CONTRACT_ID,
                   "manifest_hash": "sha256:" + "a" * 64,
                   "selected_sha": "d" * 40,
                   "integration_ref": "cycle/%s/integration" % CONTRACT_ID}],
    )["coordination_cycle_id"]

    # Simulate the committed-transport-only clone: drop the local manifest.
    local = sp.coordination_manifest_path(str(repo), ccid)
    os.chmod(local, 0o644)
    os.remove(local)
    assert not os.path.exists(local)

    real = state_store.write_json_atomic

    def explode(path, payload, **kw):
        if os.path.join(".synaptory", "coordination-cycles") in str(path):
            raise OSError(28, "No space left on device")
        return real(path, payload, **kw)

    monkeypatch.setattr(state_store, "write_json_atomic", explode)

    with pytest.raises(cc.CoordinationError):
        cc.drop_child(str(repo), coordination_cycle_id=ccid,
                      cycle_id=CONTRACT_ID, reason="late")

    assert not os.path.exists(local), (
        "a revision that never reached the committed transport must not leave a "
        "local copy behind: reads prefer it, so the clone would believe in a "
        "release no child can see"
    )


def test_a_failed_revision_write_restores_the_prior_local_revision(
    repo: Path, monkeypatch
):
    """The other half of the rollback: a clone that DOES hold a local copy.

    The local-absent path is covered above. Here `prior` is not None, so the
    rollback must put the previous revision's BYTES back — not merely leave a
    file in place. Asserting only that the file exists would pass against a
    rollback that wrote nothing, which is the failure worth catching.
    """
    import state_store

    opened = cc.open_coordination(
        str(repo), release_goal="ship it",
        children=[{"cycle_id": CONTRACT_ID,
                   "manifest_hash": "sha256:" + "a" * 64,
                   "selected_sha": "d" * 40,
                   "integration_ref": "cycle/%s/integration" % CONTRACT_ID},
                  {"cycle_id": PLATFORM_ID,
                   "manifest_hash": "sha256:" + "b" * 64,
                   "selected_sha": "e" * 40,
                   "integration_ref": "cycle/%s/integration" % PLATFORM_ID}],
    )
    ccid = opened["coordination_cycle_id"]
    local = sp.coordination_manifest_path(str(repo), ccid)
    before = json.loads(Path(local).read_text(encoding="utf-8"))
    assert before["manifest_revision"] == 1
    assert {c["cycle_id"] for c in before["children"]} == {CONTRACT_ID, PLATFORM_ID}

    real = state_store.write_json_atomic

    def explode(path, payload, **kw):
        if os.path.join(".synaptory", "coordination-cycles") in str(path):
            raise OSError(28, "No space left on device")
        return real(path, payload, **kw)

    monkeypatch.setattr(state_store, "write_json_atomic", explode)

    with pytest.raises(cc.CoordinationError) as excinfo:
        cc.drop_child(str(repo), coordination_cycle_id=ccid,
                      cycle_id=CONTRACT_ID, reason="late")
    assert "rolled back" in str(excinfo.value)

    after = json.loads(Path(local).read_text(encoding="utf-8"))
    assert after == before, (
        "the local manifest must be byte-identical to the revision that was "
        "there before the failed revise; a clone whose local copy advanced past "
        "the committed transport believes in a release no child can see"
    )
    assert after["manifest_revision"] == 1
    assert {c["cycle_id"] for c in after["children"]} == {CONTRACT_ID, PLATFORM_ID}
    # And the sealed copy must still be readable — a rollback that left the file
    # mode wrong would break every later read just as badly.
    assert cc.verify_hash(after)
