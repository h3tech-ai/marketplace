"""Layer 1 — the Cycle manifest (#303 §2).

`open_cycle` and `hydrate_cycle` used to accept arbitrary caller-supplied Work
Unit JSON, so every clone reconstructed its own view of the Cycle from whatever
the prompt happened to pass. Two clones could disagree about what was admitted,
who owned it, and what depended on what, and nothing could detect the
disagreement until the barrier.

The properties that make the replacement worth having:

1. the hash is defined ONCE and detects any body change,
2. `validate` refuses every shape that would block a Cycle forever, and
3. a projection carries other workstreams' unit IDENTITIES without their
   mutable state -- the asymmetry that lets the #304 gate name an owner without
   reconstructing a board it cannot see.
"""

from __future__ import annotations

import json

import pytest

import spq_manifest as mf
import spq_paths as sp


def _manifest(**overrides):
    cycle_id = overrides.pop("cycle_id", None) or sp.new_cycle_id(7)
    base = dict(
        cycle_id=cycle_id,
        cycle_seq=sp.seq_of(cycle_id),
        goal="auth",
        baseline_sha="a" * 40,
        integration_ref=sp.integration_branch(cycle_id),
        workstreams=[{"id": "spine", "shared_owner": True}, {"id": "frame"}],
        work_units=[
            {
                "id": "WU-01",
                "owner_workstream": "spine",
                "outputs": [{"kind": "contract", "id": "contracts/auth/v2"}],
            },
            {
                "id": "WU-02",
                "owner_workstream": "frame",
                "depends_on": [
                    {"unit_id": "WU-01", "condition": "contract_published"}
                ],
            },
        ],
    )
    base.update(overrides)
    return mf.seal(mf.build(**base))


# ── the hash ───────────────────────────────────────────────────────────────


def test_a_valid_manifest_validates():
    assert mf.validate(_manifest()) == []


def test_default_manifest_policy_matches_the_shipped_at_sync_flow():
    assert _manifest()["integration_policy"] == {
        "mode": "at_sync",
        "merge_requires": [],
    }


def test_incremental_required_regression_cannot_be_omitted_from_policy():
    manifest = _manifest(
        verification={"require_regression": True},
        integration_policy={"mode": "incremental", "merge_requires": []},
    )
    assert any("regression_green" in problem for problem in mf.validate(manifest))


def test_incremental_policy_accepts_its_required_regression_gate():
    manifest = _manifest(
        verification={"require_regression": True},
        integration_policy={
            "mode": "incremental",
            "merge_requires": ["regression_green"],
        },
    )
    assert mf.validate(manifest) == []


def test_hash_is_stable_across_key_order():
    """Canonicalization, not dict order, decides the hash. Otherwise two
    clones serializing the same manifest could disagree about its identity."""
    sealed = _manifest()
    reordered = dict(reversed(list(sealed.items())))
    assert mf.compute_hash(sealed) == mf.compute_hash(reordered)


def test_hash_changes_on_any_body_field():
    sealed = _manifest()
    for field, value in (
        ("goal", "something else"),
        ("baseline_sha", "b" * 40),
        ("integration_ref", "cycle/other/integration"),
        ("schema_version", "0.9"),
    ):
        tampered = dict(sealed)
        tampered[field] = value
        assert not mf.verify_hash(tampered), field


def test_hash_covers_the_work_units():
    sealed = _manifest()
    tampered = json.loads(json.dumps(sealed))
    tampered["work_units"][0]["owner_workstream"] = "frame"
    assert not mf.verify_hash(tampered), "re-owning a unit must break the hash"


def test_schema_version_is_inside_the_hash():
    """No cross-version hash compatibility is owed. Pretending otherwise would
    let a v1 reader accept a v2 document it cannot fully validate."""
    sealed = _manifest()
    body = mf.canonical_bytes(sealed).decode("utf-8")
    assert '"schema_version"' in body


def test_seal_is_idempotent():
    sealed = _manifest()
    assert mf.seal(sealed) == sealed


def test_an_unsealed_manifest_does_not_verify():
    unsealed = mf.build(
        cycle_id=sp.new_cycle_id(1),
        cycle_seq=1,
        goal="g",
        baseline_sha="x",
        integration_ref="r",
        workstreams=[{"id": "a"}],
        work_units=[{"id": "U", "owner_workstream": "a"}],
    )
    assert not mf.verify_hash(unsealed)


# ── validation refuses what would block a Cycle forever ────────────────────


def test_rejects_a_unit_with_no_owner():
    m = _manifest(work_units=[{"id": "WU-1"}])
    assert any("owner_workstream" in p for p in mf.validate(m))


def test_rejects_an_owner_that_is_not_a_workstream():
    m = _manifest(work_units=[{"id": "WU-1", "owner_workstream": "ghost"}])
    assert any("not in workstreams" in p for p in mf.validate(m))


def test_rejects_duplicate_unit_ids():
    m = _manifest(
        work_units=[
            {"id": "WU-1", "owner_workstream": "spine"},
            {"id": "WU-1", "owner_workstream": "frame"},
        ]
    )
    assert any("duplicate work unit" in p for p in mf.validate(m))


def test_rejects_two_shared_owners():
    """Exactly one workstream owns the shared surfaces for a Cycle (§12), or
    adjudication has no address."""
    m = _manifest(
        workstreams=[
            {"id": "spine", "shared_owner": True},
            {"id": "frame", "shared_owner": True},
        ]
    )
    assert any("shared_owner" in p for p in mf.validate(m))


def test_rejects_a_dependency_on_a_unit_that_is_not_admitted():
    """An edge that can never resolve blocks the unit forever, and #304's gate
    would report it as a typo with no way to tell that it is not."""
    m = _manifest(
        work_units=[
            {"id": "WU-1", "owner_workstream": "spine", "depends_on": ["GHOST"]}
        ]
    )
    problems = mf.validate(m)
    assert any("neither admitted" in p for p in problems)


def test_rejects_a_self_dependency():
    m = _manifest(
        work_units=[
            {"id": "WU-1", "owner_workstream": "spine", "depends_on": ["WU-1"]}
        ]
    )
    assert any("depends on itself" in p for p in mf.validate(m))


def test_rejects_a_dependency_cycle():
    """Every unit in a cycle waits for another unit in the cycle. Detecting it
    at Commit is the difference between a Cycle that cannot start and one that
    appears to start and then reports deps_blocked forever with no reason."""
    m = _manifest(
        work_units=[
            {"id": "A", "owner_workstream": "spine", "depends_on": ["B"]},
            {"id": "B", "owner_workstream": "spine", "depends_on": ["C"]},
            {"id": "C", "owner_workstream": "spine", "depends_on": ["A"]},
        ]
    )
    assert any("dependency cycle" in p for p in mf.validate(m))


def test_rejects_an_unknown_condition():
    m = _manifest(
        work_units=[
            {
                "id": "WU-1",
                "owner_workstream": "spine",
                "depends_on": [{"unit_id": "WU-1x", "condition": "vibes"}],
            },
            {"id": "WU-1x", "owner_workstream": "spine"},
        ]
    )
    assert any("unknown condition" in p for p in mf.validate(m))


def test_rejects_a_cycle_seq_that_disagrees_with_the_cycle_id():
    cycle_id = sp.new_cycle_id(7)
    m = _manifest(cycle_id=cycle_id, cycle_seq=3)
    assert any("disagrees" in p for p in mf.validate(m))


def test_rejects_an_empty_admitted_set():
    """An empty Cycle lets a workstream declare readiness having delivered
    nothing."""
    m = _manifest(work_units=[])
    assert any("work_units[] is empty" in p for p in mf.validate(m))


def test_rejects_a_shared_surface_without_a_valid_owner():
    m = _manifest(shared_surfaces=[{"path": "contracts/", "owner_workstream": "ghost"}])
    assert any("shared surface" in p for p in mf.validate(m))


def test_baseline_is_required_by_default_and_waivable_outside_git():
    """SPQ's integration model is git-based -- branches, ancestry, the
    barrier's is_ancestor check -- so a project with no repository cannot reach
    Sync regardless, and demanding a baseline it cannot have would block Cycle
    open for no protection."""
    m = _manifest(baseline_sha="")
    assert any("baseline_sha is required" in p for p in mf.validate(m))
    assert not [
        p for p in mf.validate(m, require_baseline=False) if "baseline_sha" in p
    ]


def test_validate_reports_every_problem_at_once():
    """An operator fixing a manifest one refusal at a time is a bad loop."""
    m = _manifest(
        workstreams=[{"id": "a", "shared_owner": True}, {"id": "b", "shared_owner": True}],
        work_units=[{"id": "U"}, {"id": "U", "owner_workstream": "ghost"}],
    )
    assert len(mf.validate(m)) >= 3


# ── the projection asymmetry ───────────────────────────────────────────────


def test_projection_carries_only_this_workstreams_full_records():
    proj = mf.project(_manifest(), "frame")
    assert [u["id"] for u in proj["work_units"]] == ["WU-02"]
    assert [u["id"] for u in proj["foreign_units"]] == ["WU-01"]


def test_foreign_units_carry_identity_and_outputs_but_no_mutable_state():
    """The load-bearing asymmetry. It lets #304's gate answer 'owned by spine,
    no contract_published event yet' instead of 'unknown id', WITHOUT
    reconstructing another workstream's board -- which #303 forbids and which
    would also be a lie, since the other clone's state is not visible here.
    """
    proj = mf.project(_manifest(), "frame")
    foreign = proj["foreign_units"][0]
    assert set(foreign) == {"id", "owner_workstream", "outputs"}
    for banned in ("state", "pipeline_log", "dod", "receipts", "retries"):
        assert banned not in foreign


def test_projection_carries_the_manifest_hash_so_staleness_is_detectable():
    sealed = _manifest()
    proj = mf.project(sealed, "spine")
    assert proj["manifest_hash"] == sealed["manifest_hash"]


def test_projection_refuses_an_unknown_workstream():
    with pytest.raises(mf.ManifestError) as excinfo:
        mf.project(_manifest(), "ghost")
    assert "ghost" in str(excinfo.value)
    assert "spine" in str(excinfo.value), "name the workstreams that do exist"


def test_owners_and_admitted_ids_feed_the_dependency_gate():
    sealed = _manifest()
    assert mf.owners(sealed) == {"WU-01": "spine", "WU-02": "frame"}
    assert sorted(mf.admitted_ids(sealed)) == ["WU-01", "WU-02"]


# ── revision ───────────────────────────────────────────────────────────────


def test_revise_bumps_the_revision_and_records_the_prior_hash():
    """`supersedes` carries the prior HASH, not the prior revision number: a
    workstream holding an older projection can then be told precisely which
    manifest superseded it, rather than trusting a number any writer could
    have bumped."""
    first = _manifest()
    second = mf.revise(first, {"goal": "auth v2"}, revised_by="po")
    assert second["manifest_revision"] == first["manifest_revision"] + 1
    assert second["supersedes"] == first["manifest_hash"]
    assert second["manifest_hash"] != first["manifest_hash"]
    assert mf.verify_hash(second)


def test_revise_refuses_an_unsealed_manifest():
    unsealed = mf.build(
        cycle_id=sp.new_cycle_id(1), cycle_seq=1, goal="g", baseline_sha="x",
        integration_ref="r", workstreams=[{"id": "a"}],
        work_units=[{"id": "U", "owner_workstream": "a"}],
    )
    with pytest.raises(mf.ManifestError):
        mf.revise(unsealed, {})


# ── condition strength ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "declared,observed,expected",
    [
        ("done", "done", True),
        ("done", "integrated", True),
        ("contract_published", "integrated", True),
        ("integrated", "done", False),
        ("integrated", "contract_published", False),
        ("contract_published", "done", False),
        ("environment_ready", "environment_ready", True),
        ("environment_ready", "integrated", False),
        ("cycle_integrated", "integrated", False),
        ("done", "vibes", False),
    ],
)
def test_condition_strength_is_explicit(declared, observed, expected):
    """The ordering is declared, not implicit. An implicit ranking is how a
    weaker event comes to satisfy a stronger requirement without anyone
    deciding that it should. `environment_ready` satisfies only itself: it is
    an out-of-band assertion nothing else implies."""
    assert mf.satisfies(declared, observed) is expected


# ── traceability is never a selector ───────────────────────────────────────


def test_source_spec_refs_round_trip_but_select_nothing():
    """#303 AC: source specification references are traceability metadata and
    do not select state, caches, receipts or commands. They are carried, and
    nothing in the module branches on them."""
    import ast
    import inspect

    sealed = _manifest(source_spec_refs=["specs/platform"])
    assert sealed["source_spec_refs"] == ["specs/platform"]

    tree = ast.parse(inspect.getsource(mf))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                body.pop(0)
    branching = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.If, ast.While))
        and "source_spec_refs" in ast.dump(n)
    ]
    assert not branching, "source_spec_refs must never be branched on"


# ── #305: cross-Cycle edges are shape-checked, not waved through ────────────
#
# `validate` used to `continue` past every external edge: no target check, no
# self-dependency check, and `topo_order` skips them too. So
# `{"external": {"unit_id": "WU-LOCAL"}}` validated clean with nothing looked at
# at all. That was inert only while `story_dep_status` held every external edge
# unconditionally. The moment the branch becomes resolvable, an edge declared
# cross-Cycle would be satisfiable by a LOCAL event -- and a PO typo, not malice,
# is all it takes.


def _with_external(external):
    """An external edge carries its condition INSIDE `external`.

    `normalize_dep` reads `external["condition"]` and defaults to
    `cycle_integrated`; an outer `condition` on the dep is not consulted for the
    external form. Spelling it here so a test cannot pass by setting a key
    nothing reads.
    """
    dep = {"external": external}
    return _manifest(
        work_units=[
            {"id": "WU-01", "owner_workstream": "spine"},
            {"id": "WU-02", "owner_workstream": "frame", "depends_on": [dep]},
        ]
    )


def test_external_edge_naming_a_locally_admitted_unit_is_refused():
    """The smuggle. `WU-01` is admitted HERE, so it is not cross-Cycle."""
    m = _with_external(
        {"cycle_id": sp.new_cycle_id(3), "unit_id": "WU-01",
         "condition": "cycle_integrated"}
    )
    problems = mf.validate(m, require_baseline=False)
    assert any("admitted to THIS Cycle" in p for p in problems), problems


def test_a_unit_addressed_external_edge_without_a_cycle_id_is_refused():
    """A bare unit id has no other way to say which Cycle it belongs to — and
    that is the form the local-unit smuggle takes."""
    m = _with_external({"unit_id": "WU-UPSTREAM", "condition": "cycle_integrated"})
    problems = mf.validate(m, require_baseline=False)
    assert any("neither a cycle_id nor an output" in p for p in problems), problems


def test_an_output_addressed_external_edge_may_omit_the_producer():
    """The release already declares who supplies an output. Requiring the
    consumer to know forced the producing child to open its Cycle FIRST — a hard
    ordering dependency between Cycles this epic calls independent, and it made
    the output-addressed form the epic itself prefers unusable.
    """
    m = _with_external({
        "output": {"kind": "contract", "id": "contracts/auth"},
        "condition": "contract_published",
    })
    assert mf.validate(m, require_baseline=False) == []


def test_external_edge_with_a_malformed_cycle_id_is_refused():
    m = _with_external(
        {"cycle_id": "not-a-cycle", "unit_id": "WU-UP", "condition": "cycle_integrated"}
    )
    problems = mf.validate(m, require_baseline=False)
    assert any("not a valid Cycle identity" in p for p in problems), problems


def test_external_edge_naming_neither_a_unit_nor_an_output_is_refused():
    m = _with_external({"cycle_id": sp.new_cycle_id(3), "condition": "cycle_integrated"})
    problems = mf.validate(m, require_baseline=False)
    assert any("neither a unit_id nor an output" in p for p in problems), problems


def test_external_edge_declaring_a_bare_done_is_refused():
    """`done` is another clone's board state, which is not visible from here."""
    m = _with_external(
        {"cycle_id": sp.new_cycle_id(3), "unit_id": "WU-UP", "condition": "done"}
    )
    problems = mf.validate(m, require_baseline=False)
    assert any("must name a condition another Cycle can publish" in p
               for p in problems), problems


def test_output_addressed_external_edge_validates():
    """The epic's own form: address the contract, not the unit that made it."""
    m = _with_external(
        {"cycle_id": sp.new_cycle_id(3),
         "output": {"kind": "contract", "id": "contracts/auth"},
         "condition": "contract_published"}
    )
    assert mf.validate(m, require_baseline=False) == []


def test_unit_addressed_external_edge_validates():
    m = _with_external(
        {"cycle_id": sp.new_cycle_id(3), "unit_id": "WU-UPSTREAM",
         "condition": "cycle_integrated"}
    )
    assert mf.validate(m, require_baseline=False) == []


# ── the stringified-mapping bug ────────────────────────────────────────────


def test_an_output_dict_never_becomes_the_unit_id():
    """`str(external["output"])` produced "{'kind': 'contract', ...}" as an id.

    It never crashed. It produced an unaddressable edge, put a stringified
    mapping in the operator's "why is this blocked" message, and compared that
    string against the admitted set in `validate`.
    """
    dep = mf.normalize_dep(
        {"external": {"cycle_id": "3-aabbccdd",
                      "output": {"kind": "contract", "id": "contracts/auth"},
                      "condition": "contract_published"}}
    )
    assert dep["unit_id"] == ""
    assert dep["output"] == {"kind": "contract", "id": "contracts/auth"}
    assert "{" not in dep["unit_id"]


def test_normalize_dep_matches_story_pipeline():
    """One dep form, two modules, one reading. They mirror by contract."""
    import story_pipeline as sp_mod

    for entry in (
        "WU-7",
        {"unit_id": "WU-7", "condition": "integrated"},
        {"external": {"cycle_id": "3-aabbccdd", "unit_id": "WU-UP",
                      "condition": "cycle_integrated"}},
        {"external": {"cycle_id": "3-aabbccdd",
                      "output": {"kind": "contract", "id": "c/auth"},
                      "condition": "contract_published"}},
    ):
        a, b = mf.normalize_dep(entry), sp_mod.normalize_dep(entry)
        assert a["unit_id"] == b["unit_id"], entry
        assert a["condition"] == b["condition"], entry
        assert a["external"] == b["external"], entry


def test_normalized_external_does_not_alias_the_caller():
    """`unmet[].dep` is surfaced to callers; it must not alias board state."""
    entry = {"external": {"cycle_id": "3-aabbccdd", "unit_id": "WU-UP"}}
    dep = mf.normalize_dep(entry)
    dep["external"]["unit_id"] = "TAMPERED"
    assert entry["external"]["unit_id"] == "WU-UP"


# ── cross_ref_key: one address, both sides ─────────────────────────────────


def test_cross_ref_key_is_stable_across_a_spec_and_an_event():
    """A producer's event and a consumer's edge must spell one address."""
    edge = {"cycle_id": "3-aabbccdd",
            "output": {"kind": "contract", "id": "contracts/auth"}}
    event = {"cycle_id": "3-aabbccdd", "unit_id": "",
             "output": {"kind": "contract", "id": "contracts/auth"},
             "condition": "contract_published"}
    assert mf.cross_ref_key(edge) == mf.cross_ref_key(event)
    assert mf.cross_ref_key(edge) == "3-aabbccdd|out:contract:contracts/auth"


def test_cross_ref_key_prefers_the_narrower_unit_claim():
    key = mf.cross_ref_key(
        {"cycle_id": "3-aabbccdd", "unit_id": "WU-UP",
         "output": {"kind": "contract", "id": "contracts/auth"}}
    )
    assert key == "3-aabbccdd|unit:WU-UP"


def test_cross_ref_key_falls_back_to_the_whole_cycle():
    assert mf.cross_ref_key({"cycle_id": "3-aabbccdd"}) == "3-aabbccdd|cycle"


def test_cross_ref_keys_of_two_cycles_never_collide():
    """Two child Cycles in one codebase routinely share Work Unit ids."""
    a = mf.cross_ref_key({"cycle_id": "12-aaaaaaaa", "unit_id": "WU-01"})
    b = mf.cross_ref_key({"cycle_id": "5-bbbbbbbb", "unit_id": "WU-01"})
    assert a != b


# ── coordination_cycle_id: hashed, and no longer shape-checked ─────────────
#
# `test_a_malformed_coordination_cycle_id_is_refused` and its well-formed twin
# lived here. Their subject was the shape validator: `coordination_cycle_id`
# became a PATH SEGMENT the moment a parent resolved it, so #303's unchecked
# hashed field was a traversal waiting to happen and `validate` delegated to
# `spq_paths.valid_coordination_cycle_id`.
#
# `SPD-194` retires the Coordination Cycle (#644), and `spq_paths` no longer
# offers that validator. Nothing resolves the field into a path any more, so
# the traversal is unreachable and refusing `../../etc/passwd` is a guarantee
# about a concept that does not exist. Deleted rather than relaxed.
#
# What is NOT deleted is the field: `build` still accepts it and
# `canonical_bytes` still hashes it, which the two tests below pin. The
# in-between state -- an inert `validate` branch that reads as a shape check --
# is pinned explicitly, because a validator that silently validates nothing is
# worse than one that is gone, and prose in a docstring rots where an assertion
# does not.


def test_the_coordination_cycle_id_shape_check_is_inert_and_says_so():
    """`validate`'s `coordination_cycle_id` branch checks nothing now.

    It calls `spq_paths.valid_coordination_cycle_id` inside
    `except (ImportError, AttributeError): pass`, and `SPD-194` removed that
    function -- so the branch swallows the `AttributeError` and every value
    passes, including a traversal. Asserted rather than described so the next
    reader of that branch learns it is dead from a failing test if anyone
    "fixes" the field back into service without restoring the validator.
    """
    import spq_paths

    assert not hasattr(spq_paths, "valid_coordination_cycle_id")
    problems = mf.validate(
        _manifest(coordination_cycle_id="../../etc/passwd"),
        require_baseline=False,
    )
    assert problems == [], (
        "a shape check came back to life; re-assert the refusal above rather "
        "than this absence"
    )


def test_absent_coordination_cycle_id_is_not_a_problem():
    assert mf.validate(_manifest(), require_baseline=False) == []


def test_child_manifest_hash_is_unchanged_when_no_coordination_cycle():
    """The highest-risk regression in the epic.

    `coordination_cycle_id` is INSIDE `canonical_bytes`. If supplying it as
    `None` differed in any way from the pre-#305 document, every clone in a
    running fleet would compute a different hash and `manifest_agreement` would
    refuse the Cycle -- reading exactly like a barrier bug.
    """
    cycle_id = sp.new_cycle_id(7)
    common = dict(
        cycle_id=cycle_id, cycle_seq=sp.seq_of(cycle_id), goal="auth",
        baseline_sha="a" * 40, integration_ref=sp.integration_branch(cycle_id),
        workstreams=[{"id": "spine", "shared_owner": True}],
        work_units=[{"id": "WU-01", "owner_workstream": "spine"}],
        created_at="2026-08-27T00:00:00Z",
    )
    omitted = mf.seal(mf.build(**common))
    explicit_none = mf.seal(mf.build(coordination_cycle_id=None, **common))
    assert omitted["manifest_hash"] == explicit_none["manifest_hash"]
    assert omitted["coordination_cycle_id"] is None

    named = mf.seal(mf.build(coordination_cycle_id="cc-1-9f2c1ab3", **common))
    assert named["manifest_hash"] != omitted["manifest_hash"], (
        "binding a parent MUST change the hash -- that is what makes the "
        "binding tamper-evident"
    )


def test_satisfies_is_unchanged_by_the_coordination_layer():
    """`satisfies` is a shared attractor. Pin its exact truth table.

    #305 needs `cycle_integrated` to be satisfiable, and the tempting fix is to
    relax this function. It is on the intra-Cycle dispatch path for every Cycle
    already running, and the only knob that would make a claim-only condition
    resolve -- `accept_unverified_events` -- is one manifest-wide boolean that
    would simultaneously let every bare `done` claim satisfy an edge.

    So cross-Cycle strength lives in its own comparator in
    `coordination_cycle.py`, and this asserts the leak never happens inward.
    """
    # `cycle_integrated` and `environment_ready` satisfy ONLY themselves.
    for special in ("cycle_integrated", "environment_ready"):
        assert mf.satisfies(special, special) is True
        for other in mf.DEP_CONDITIONS:
            if other == special:
                continue
            assert mf.satisfies(special, other) is False, (special, other)
            assert mf.satisfies(other, special) is False, (other, special)

    # The ranked conditions: stronger observed satisfies weaker declared.
    assert mf.satisfies("done", "integrated") is True
    assert mf.satisfies("contract_published", "integrated") is True
    assert mf.satisfies("integrated", "contract_published") is False
    assert mf.satisfies("done", "contract_published") is True
    assert mf.satisfies("contract_published", "artifact_published") is True

    # Unknown on either side is never satisfied.
    assert mf.satisfies("done", "nonsense") is False
    assert mf.satisfies("nonsense", "integrated") is False
