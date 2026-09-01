"""Layer 1 — `spq_paths`: native SPQ identity and storage layout.

SPQ used to borrow Multi-Spec, with `SYNAPTORY_ACTIVE_SPEC` acting as the
workstream id and Cycle state living in a `specs.<workstream>` slot.
#303/#304/#305 require that coupling gone. These tests pin the three properties
that make the replacement safe:

1. every id is validated BEFORE it becomes a path segment, so no downstream
   join has to defend itself against traversal,
2. `SYNAPTORY_ACTIVE_SPEC` is IGNORED even when set -- a clone with a stale
   export must not silently attribute one lane's work to another, and
3. `cycle_id` carries a hash, so two clones that independently open "Cycle 7"
   produce different identities while `seq` stays the receipt-facing number.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

import spq_paths as sp


# ── id validation happens before any path join ─────────────────────────────


@pytest.mark.parametrize(
    "bad",
    ["../evil", "..", "/abs", "", "UPPER-9f2c1ab3", "7-XYZ", "7", "x" * 80,
     "seven-9f2c1ab3", "7-9f2c1ab", "7-9f2c1ab3x"],
)
def test_invalid_cycle_id_is_refused(bad):
    with pytest.raises(sp.IdentityError):
        sp.valid_cycle_id(bad)


@pytest.mark.parametrize(
    "bad", ["../evil", "/abs", "", "Upper", "-leading", "a" * 80, "with space"]
)
def test_invalid_workstream_id_is_refused(bad):
    with pytest.raises(sp.IdentityError):
        sp.valid_workstream_id(bad)


def test_every_path_stays_inside_the_project(tmp_path: Path):
    cid = sp.new_cycle_id(7)
    project = str(tmp_path)
    root = os.path.realpath(project)
    for path in (
        sp.spq_root(project),
        sp.index_path(project),
        sp.pin_path(project),
        sp.cycle_root(project, cid),
        sp.manifest_path(project, cid),
        sp.ledger_path(project, cid),
        sp.workstream_root(project, cid, "spine"),
        sp.projection_path(project, cid, "spine"),
        sp.execution_state_path(project, cid, "spine"),
        sp.receipts_dir(project, cycle_id=cid, workstream_id="spine"),
        sp.receipts_dir(project, cycle_id=cid),
        sp.tracker_data_path(project, cid, "spine"),
        sp.committed_cycle_dir(project, cid),
        sp.committed_manifest_path(project, cid),
        sp.committed_events_dir(project, cid, "spine"),
        sp.committed_sync_path(project, cid, "spine"),
    ):
        assert os.path.realpath(path).startswith(root), path


def test_a_traversing_id_cannot_reach_a_path_helper(tmp_path: Path):
    with pytest.raises(sp.IdentityError):
        sp.workstream_root(str(tmp_path), sp.new_cycle_id(1), "../../etc")


# ── cycle identity ─────────────────────────────────────────────────────────


def test_cycle_id_carries_the_seq_and_a_disambiguating_hash():
    cid = sp.new_cycle_id(7, baseline_sha="abc", goal="g", created_at="t")
    assert re.match(r"^7-[0-9a-f]{8}$", cid)
    assert sp.seq_of(cid) == 7


def test_two_clones_opening_the_same_seq_get_different_identities():
    """The whole point of the hash: `seq` collides across clones by design, so
    an id match is not an identity match. #305 pins children by hash."""
    assert sp.new_cycle_id(7) != sp.new_cycle_id(7)


def test_seq_stays_receipt_id_safe():
    """`receipt_validator` enforces ^[A-Z][A-Z0-9]*-\\d+$, so `CYCLE-7-9f2c1ab3`
    is not a legal receipt id. Receipt ids use the seq projection instead."""
    cid = sp.new_cycle_id(12)
    receipt_id = "CYCLE-%d" % sp.seq_of(cid)
    assert re.match(r"^[A-Z][A-Z0-9]*-\d+$", receipt_id)


def test_new_cycle_id_refuses_a_negative_or_non_int_seq():
    for bad in (-1, "7", None, 1.5):
        with pytest.raises(sp.IdentityError):
            sp.new_cycle_id(bad)  # type: ignore[arg-type]


# ── receipt layout ─────────────────────────────────────────────────────────


def test_work_unit_receipts_are_per_workstream(tmp_path: Path):
    cid = sp.new_cycle_id(3)
    ws = sp.receipts_dir(str(tmp_path), cycle_id=cid, workstream_id="spine")
    assert ws.endswith(os.path.join("workstreams", "spine", "receipts"))


def test_cycle_level_receipts_sit_above_the_workstreams(tmp_path: Path):
    """`SYNC-{seq}-barrier` and `CHECKPOINT-{seq}-tw` belong to the Cycle, not
    to any one workstream."""
    cid = sp.new_cycle_id(3)
    cycle = sp.receipts_dir(str(tmp_path), cycle_id=cid)
    assert "workstreams" not in cycle
    assert cycle.endswith(os.path.join(cid, "receipts"))


def test_receipts_dir_requires_a_cycle(tmp_path: Path):
    with pytest.raises(sp.IdentityError):
        sp.receipts_dir(str(tmp_path))


def test_no_spq_path_contains_a_specs_segment(tmp_path: Path):
    """The Multi-Spec layout is `.orchestrator/specs/<id>/...`. Nothing SPQ
    writes may land there (#303/#304/#305)."""
    cid = sp.new_cycle_id(1)
    for path in (
        sp.manifest_path(str(tmp_path), cid),
        sp.receipts_dir(str(tmp_path), cycle_id=cid, workstream_id="spine"),
        sp.execution_state_path(str(tmp_path), cid, "spine"),
    ):
        assert "specs" not in Path(path).parts, path


# ── branch namespacing ─────────────────────────────────────────────────────


def test_branches_are_cycle_namespaced(tmp_path: Path):
    """`ws/{id}` collided across concurrent Cycles; the cycle segment fixes it."""
    cid = sp.new_cycle_id(7)
    assert sp.workstream_branch(cid, "spine") == "cycle/%s/ws/spine" % cid
    assert sp.integration_branch(cid) == "cycle/%s/integration" % cid


def test_branch_names_are_valid_git_ref_components():
    cid = sp.new_cycle_id(7)
    for ref in (sp.workstream_branch(cid, "spine"), sp.integration_branch(cid)):
        assert not re.search(r"[~^:?*\[\\ ]", ref), ref
        assert ".." not in ref
        assert not ref.endswith(".lock")


# ── identity resolution ────────────────────────────────────────────────────


def test_synaptory_active_spec_is_ignored_even_when_set(tmp_path, monkeypatch):
    """The load-bearing decoupling assertion.

    A clone with a stale `SYNAPTORY_ACTIVE_SPEC` export must not pick it up as
    a workstream: that is how one lane's work would be attributed to another,
    silently and unrecoverably.
    """
    monkeypatch.setenv("SYNAPTORY_ACTIVE_SPEC", "platform")
    monkeypatch.delenv(sp.ENV_WORKSTREAM, raising=False)
    ident = sp.resolve_identity(str(tmp_path))
    assert ident.workstream_id is None


def test_unresolvable_workstream_raises_rather_than_defaulting(tmp_path, monkeypatch):
    monkeypatch.delenv(sp.ENV_WORKSTREAM, raising=False)
    with pytest.raises(sp.IdentityError) as excinfo:
        sp.resolve_identity(str(tmp_path), require_workstream=True)
    message = str(excinfo.value)
    assert sp.ENV_WORKSTREAM in message
    assert "SYNAPTORY_ACTIVE_SPEC" in message, (
        "the error should say explicitly that the old env var is not read"
    )


def test_resolution_order_is_flag_then_pin_then_env_then_state(tmp_path, monkeypatch):
    """flag > PIN > env > state.

    The pin used to rank BELOW the environment, which contradicted the reason
    the pin exists. `pin_path` and
    `test_pin_survives_a_reread_and_is_a_property_of_the_checkout` (just below)
    both say a workstream is a property of the CHECKOUT, precisely because an
    env var has to be re-exported in every shell -- so ranking a forgettable
    export above the durable file made the pin advisory.

    It was not only advisory, it was actively wrong: `read_state` re-derives
    `_workstream_id` through here on EVERY read, so a hydrated clone with a
    correct pin still reported another lane whenever `SYNAPTORY_WORKSTREAM` was
    left exported. Measured in #328 G1: `hydrate_cycle --workstream web` under
    `SYNAPTORY_WORKSTREAM=api` admitted web's units and identified as `api`, so
    `declare-ready` declared as the wrong lane and the quorum never saw the real
    one.

    The env var keeps the job it is good at -- naming a lane BEFORE a pin exists
    -- and loses only the ability to silently re-point an already-hydrated
    checkout. `--workstream` still overrides everything, so no case is
    unreachable.
    """
    project = str(tmp_path)
    state = {"spq": {"workstream_id": "from-state"}}

    monkeypatch.delenv(sp.ENV_WORKSTREAM, raising=False)
    assert sp.resolve_identity(project, state=state).workstream_id == "from-state"
    assert sp.resolve_identity(project, state=state).source["workstream_id"] == "state"

    # Env beats state: before a pin exists, an export is how a lane is named.
    monkeypatch.setenv(sp.ENV_WORKSTREAM, "from-env")
    ident = sp.resolve_identity(project, state=state)
    assert ident.workstream_id == "from-env"
    assert ident.source["workstream_id"] == "env"

    # Pin beats env: the checkout has been hydrated, so it knows what it is.
    sp.write_pin(project, "from-pin")
    ident = sp.resolve_identity(project, state=state)
    assert ident.workstream_id == "from-pin", (
        "a stray SYNAPTORY_WORKSTREAM re-identified a hydrated clone (#328 G1)"
    )
    assert ident.source["workstream_id"] == "pin"

    # Flag beats everything, including the pin.
    ident = sp.resolve_identity(project, workstream_id="from-flag", state=state)
    assert ident.workstream_id == "from-flag"
    assert ident.source["workstream_id"] == "flag"


def test_cycle_resolution_falls_back_to_the_index(tmp_path, monkeypatch):
    monkeypatch.delenv(sp.ENV_CYCLE_ID, raising=False)
    project = str(tmp_path)
    cid = sp.new_cycle_id(4)
    sp.record_cycle(project, cid, seq=4)
    ident = sp.resolve_identity(project, require_cycle=True)
    assert ident.cycle_id == cid
    assert ident.cycle_seq == 4
    assert ident.source["cycle_id"] == "index"


def test_pin_survives_a_reread_and_is_a_property_of_the_checkout(tmp_path):
    """A file rather than an env var, because an env var has to be re-exported
    in every shell -- which is exactly how SYNAPTORY_ACTIVE_SPEC came to be
    forgotten and receipts shipped unattributed."""
    project = str(tmp_path)
    sp.write_pin(project, "spine")
    assert sp.read_pin(project) == "spine"
    assert Path(sp.pin_path(project)).read_text(encoding="utf-8").strip() == "spine"


def test_write_pin_refuses_an_invalid_workstream(tmp_path):
    with pytest.raises(sp.IdentityError):
        sp.write_pin(str(tmp_path), "../escape")


def test_runner_id_is_derived_and_charset_safe(tmp_path, monkeypatch):
    monkeypatch.delenv(sp.ENV_RUNNER, raising=False)
    ident = sp.resolve_identity(str(tmp_path))
    assert sp.RUNNER_ID_RE.match(ident.runner_id), ident.runner_id
    assert ident.source["runner_id"] == "derived"
    monkeypatch.setenv(sp.ENV_RUNNER, "box-01")
    assert sp.resolve_identity(str(tmp_path)).runner_id == "box-01"


# ── index ──────────────────────────────────────────────────────────────────


def test_index_refuses_a_duplicate_sequence_in_one_checkout(tmp_path):
    """Local refusal only: a clone cannot see another clone's index, which is
    exactly why cycle_id carries a hash."""
    project = str(tmp_path)
    sp.record_cycle(project, sp.new_cycle_id(7), seq=7)
    with pytest.raises(sp.IdentityError):
        sp.record_cycle(project, sp.new_cycle_id(7), seq=7)


def test_recording_the_same_cycle_twice_is_idempotent(tmp_path):
    project = str(tmp_path)
    cid = sp.new_cycle_id(7)
    sp.record_cycle(project, cid, seq=7)
    sp.record_cycle(project, cid, seq=7)
    assert [c["cycle_id"] for c in sp.list_cycles(project)] == [cid]


def test_next_seq_tracks_the_high_water_mark(tmp_path):
    project = str(tmp_path)
    assert sp.next_seq(project) == 1
    sp.record_cycle(project, sp.new_cycle_id(1), seq=1)
    sp.record_cycle(project, sp.new_cycle_id(5), seq=5)
    assert sp.next_seq(project) == 6


def test_index_on_a_fresh_project_has_the_expected_shape(tmp_path):
    index = sp.read_index(str(tmp_path))
    assert index == {"cycle_seq_high": 0, "current_cycle_id": None, "cycles": []}


# ── source_spec_refs is traceability, never a selector ─────────────────────


def test_source_spec_refs_never_reach_a_path():
    """#303 AC: 'source specification references, when present, are
    traceability metadata and do not select state, caches, receipts, or
    commands.' Asserted structurally rather than trusted, because the whole
    point of the epic is that a spec identifier stopped selecting storage.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(sp))
    # Drop docstrings: prose that NAMES the invariant must not trip the check
    # that ENFORCES it.
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            body.pop(0)

    offenders = [
        node
        for node in ast.walk(tree)
        if (isinstance(node, ast.Name) and node.id == "source_spec_refs")
        or (isinstance(node, ast.Attribute) and node.attr == "source_spec_refs")
        or (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "source_spec_refs" in node.value
        )
    ]
    assert not offenders, (
        "source_spec_refs reached executable code in spq_paths: it is "
        "traceability metadata and must never select a path, a cache key or a "
        "command (%d occurrence(s))" % len(offenders)
    )


# ── #305: the coordination namespace is provably disjoint from the Cycle one ──
#
# This is not cosmetic. `valid_cycle_id` is called on the way into
# `resolve_identity`, `seq_of`, `cycle_root`, `committed_cycle_dir`,
# `workstream_branch` and `integration_branch`. A coordination id that reached
# `index.json.current_cycle_id` or the state pointer -- both of which are written
# unvalidated and read validated -- would make every subsequent `read_state`,
# `next_action`, `dep_context` and hook in that clone raise. Not a degraded mode:
# a dead clone. So the two formats must reject each other, in both directions.


@pytest.mark.parametrize(
    "bad",
    ["../evil", "..", "/abs", "", "cc-3-XYZ", "cc-3", "cc-x-9f2c1ab3",
     "CC-3-9f2c1ab3", "cc-1-../../etc", "3-9f2c1ab3", "cc-3-9f2c1ab",
     "cc-3-9f2c1ab3x"],
)
def test_invalid_coordination_cycle_id_is_refused(bad):
    with pytest.raises(sp.IdentityError):
        sp.valid_coordination_cycle_id(bad)


def test_a_cycle_id_is_never_a_valid_coordination_id():
    """The child namespace must not be readable as a parent one."""
    with pytest.raises(sp.IdentityError):
        sp.valid_coordination_cycle_id("12-9f2c1ab3")


def test_a_coordination_id_is_never_a_valid_cycle_id():
    """And the reverse, which is the direction that would brick a clone."""
    with pytest.raises(sp.IdentityError):
        sp.valid_cycle_id("cc-1-9f2c1ab3")


def test_coordination_id_accepts_its_own_form():
    assert sp.valid_coordination_cycle_id("cc-3-9f2c1ab3") == "cc-3-9f2c1ab3"
