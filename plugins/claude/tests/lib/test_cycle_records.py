"""Layer 1 — the sealed Cycle declaration and its refusals (#643, #644).

Refusal-weighted, like the path grammar, and for the same reason: a declaration
that validates correctly proves little, while one that cannot be sealed with a
missing scope, an unowned shared path or an unsequenced collision is the whole
value of sealing it.
"""

from __future__ import annotations

import pytest

import cycle_records as cr


def _unit(uid, scope, **over):
    unit = {
        "id": uid,
        "kind": "story",
        "acceptance_criteria": ["it works"],
        "path_scope": list(scope),
        "depends_on": [],
    }
    unit.update(over)
    return unit


def _declaration(**over):
    body = {
        "cycle_id": "007-9f2c41ab",
        "cycle_seq": 7,
        "repository": "h3tech-ai/synaptory-v1",
        "trunk_ref": "refs/heads/dev",
        "baseline_ref": "baseline-3",
        "goal": "authentication",
        "engineering_lead": "alice@h3t.co",
        "crew": ["agent:se-01", "agent:qe-01"],
        "source_region": ["api/"],
        "barrier_criteria": list(cr.BARRIER_CRITERIA),
        "admitted_units": [_unit("WU-01", ["api/routers/"])],
        "shared_path_owners": [],
    }
    body.update(over)
    return body


def test_a_complete_declaration_seals_and_verifies():
    sealed = cr.seal(_declaration())
    assert cr.verify_hash(sealed)
    assert sealed["kind"] == cr.KIND
    assert sealed["path_grammar"]


def test_the_grammar_version_is_inside_the_hash():
    """A document re-read under different path rules is a different document."""
    sealed = cr.seal(_declaration())
    tampered = dict(sealed)
    tampered["path_grammar"] = "99"
    assert cr.verify_hash(tampered) is False


@pytest.mark.parametrize("field", [
    "cycle_id", "repository", "trunk_ref", "baseline_ref", "goal",
    "engineering_lead",
])
def test_every_required_scalar_is_required_with_its_reason(field):
    body = _declaration()
    body[field] = ""
    found = cr.problems(body)
    assert any(field in problem for problem in found), found


def test_a_declaration_reports_every_problem_at_once():
    """Raising on the first means six declarations fixed in six round trips,
    and the sixth is when the author stops reading the messages."""
    body = _declaration(repository="", trunk_ref="", source_region=[])
    found = cr.problems(body)
    assert len(found) >= 3, found


def test_seal_raises_carrying_the_whole_list():
    with pytest.raises(cr.DeclarationError) as excinfo:
        cr.seal(_declaration(goal="", engineering_lead=""))
    assert len(excinfo.value.problems) >= 2


# ── The admitted set ─────────────────────────────────────────────────────────

def test_an_empty_admitted_set_makes_every_barrier_vacuous():
    found = cr.problems(_declaration(admitted_units=[]))
    assert any("vacuous" in p for p in found), found


def test_a_unit_admitted_twice_is_refused():
    body = _declaration(admitted_units=[
        _unit("WU-01", ["api/a/"]), _unit("WU-01", ["api/b/"])])
    assert any("admitted twice" in p for p in cr.problems(body))


@pytest.mark.parametrize("kind", ["epic", "spike", "", None, "STORY"])
def test_an_unrecognised_kind_is_rejected_rather_than_defaulted(kind):
    body = _declaration(admitted_units=[_unit("WU-01", ["api/"], kind=kind)])
    assert any("rejected rather than defaulted" in p for p in cr.problems(body))


def test_a_unit_without_acceptance_criteria_has_nothing_to_judge_it():
    body = _declaration(admitted_units=[
        _unit("WU-01", ["api/"], acceptance_criteria=[])])
    assert any("acceptance criteria" in p for p in cr.problems(body))


def test_a_unit_without_a_path_scope_is_refused_not_treated_as_disjoint():
    """The P0 finding, made mechanical: refused BEFORE any comparison."""
    body = _declaration(admitted_units=[_unit("WU-01", [])])
    found = cr.problems(body)
    assert any("no path scope" in p for p in found), found
    assert any("whole repository" in p for p in found), found


def test_an_ungrammatical_path_scope_is_refused_with_the_grammar_message():
    body = _declaration(admitted_units=[_unit("WU-01", ["*.py"])])
    assert any("grammar v" in p for p in cr.problems(body))


# ── Concurrency: M-04's reading ──────────────────────────────────────────────

def test_intersecting_scopes_need_a_distinct_execution_order():
    body = _declaration(admitted_units=[
        _unit("WU-01", ["web/"]), _unit("WU-02", ["web/app/"])])
    assert any("execution_order" in p for p in cr.problems(body))


def test_intersecting_scopes_are_admitted_when_sequenced():
    """`M-04`: the refusal is admission to a CONCURRENT set. Sequential work
    inside one Cycle may intersect."""
    body = _declaration(admitted_units=[
        _unit("WU-01", ["web/"], execution_order=1),
        _unit("WU-02", ["web/app/"], execution_order=2)])
    assert cr.problems(body) == []


def test_a_shared_execution_order_is_not_an_order():
    body = _declaration(admitted_units=[
        _unit("WU-01", ["web/"], execution_order=1),
        _unit("WU-02", ["web/app/"], execution_order=1)])
    assert any("execution_order" in p for p in cr.problems(body))


def test_disjoint_scopes_need_no_order_at_all():
    body = _declaration(admitted_units=[
        _unit("WU-01", ["api/"]), _unit("WU-02", ["web/"])])
    assert cr.problems(body) == []


# ── Dependencies, evaluated separately from paths ────────────────────────────

def test_a_dependency_outside_the_admitted_set_is_the_split_that_is_refused():
    body = _declaration(admitted_units=[
        _unit("WU-01", ["api/"], depends_on=["WU-99"])])
    found = cr.problems(body)
    assert any("did not admit" in p for p in found), found
    assert any("published version" in p for p in found), found


def test_a_mutual_pair_inside_the_cycle_is_the_atomic_case_and_is_fine():
    """`SC-MTH-012`: work that cannot be ordered cannot be split, so it belongs
    in one Cycle. A loop here is co-admitted work, not a defect."""
    body = _declaration(admitted_units=[
        _unit("WU-01", ["api/"], depends_on=["WU-02"]),
        _unit("WU-02", ["web/"], depends_on=["WU-01"])])
    assert cr.problems(body) == []


def test_dependency_freedom_is_not_path_disjointness():
    """§7.2's named mistake: two units with no edge still address one path, and
    a dependency check cannot see it. Both checks run; neither derives the other."""
    body = _declaration(admitted_units=[
        _unit("WU-01", ["web/"]), _unit("WU-02", ["web/app/"])])
    found = cr.problems(body)
    assert any("execution_order" in p for p in found), found
    assert not any("did not admit" in p for p in found), found


# ── Shared paths: never none, never two ──────────────────────────────────────

def test_a_shared_path_with_no_declared_owner_is_refused():
    body = _declaration(shared_path_owners=[{"path": "core/contract.py"}])
    found = cr.problems(body)
    assert any("no owning unit" in p for p in found), found
    assert any("merges last" in p for p in found), found


def test_a_shared_path_with_two_covering_units_is_refused():
    body = _declaration(
        admitted_units=[
            _unit("WU-01", ["core/"], execution_order=1),
            _unit("WU-02", ["core/contract.py"], execution_order=2)],
        shared_path_owners=[{"path": "core/contract.py", "owning_unit_id": "WU-01"}])
    assert any("covered by 2 units" in p for p in cr.problems(body))


def test_a_declared_owner_that_does_not_cover_the_path_is_refused():
    body = _declaration(
        admitted_units=[_unit("WU-01", ["api/"]), _unit("WU-02", ["web/"])],
        shared_path_owners=[{"path": "web/contract.ts", "owning_unit_id": "WU-01"}])
    assert any("does not declare a scope covering it" in p for p in cr.problems(body))


def test_one_owner_is_accepted():
    body = _declaration(
        admitted_units=[_unit("WU-01", ["api/"]), _unit("WU-02", ["web/"])],
        shared_path_owners=[{"path": "web/contract.ts", "owning_unit_id": "WU-02"}])
    assert cr.problems(body) == []


# ── Published criteria ───────────────────────────────────────────────────────

def test_the_barrier_criteria_are_required_in_advance():
    assert any("published" in p or "in advance" in p
               for p in cr.problems(_declaration(barrier_criteria=[])))


def test_a_project_may_add_a_criterion_but_not_remove_one():
    """§3.4: a project may add a check or raise a threshold; it may not remove
    one the method declares."""
    added = list(cr.BARRIER_CRITERIA) + ["our_own_extra_check"]
    assert cr.problems(_declaration(barrier_criteria=added)) == []
    removed = [c for c in cr.BARRIER_CRITERIA if c != "trunk_integrated"]
    assert any("omits trunk_integrated" in p
               for p in cr.problems(_declaration(barrier_criteria=removed)))


# ── Regions ──────────────────────────────────────────────────────────────────

def test_a_cycle_without_a_source_region_cannot_be_kept_apart_from_another():
    found = cr.problems(_declaration(source_region=[]))
    assert any("declaration rather than by coordination" in p for p in found), found


def test_regions_overlap_is_the_only_cross_cycle_check():
    assert cr.regions_overlap(["api/"], ["api/routers/"]) is True
    assert cr.regions_overlap(["api/"], ["web/"]) is False


def test_a_crew_is_a_list_of_names_and_not_a_table():
    assert any("grants nothing" in p
               for p in cr.problems(_declaration(crew={"alice": "admin"})))
