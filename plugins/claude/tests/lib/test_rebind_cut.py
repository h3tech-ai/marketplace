"""Layer 1 -- a supersession taken after a cut has a way out (#761).

A cut is bound to the declaration it was authorized under, and that check is
load-bearing: a cut authorized against one commitment must not silently shrink
a different one. But it was applied per record with no verb able to add one,
so a `revise_manifest` taken after a `cut_work_unit` left the Cycle
unassessable — `run_barrier` raised before evaluating any criterion,
`cut_work_unit` could not re-take the cut because it calls `effective_set`
too, `close_cycle` re-derives its own verdict so there was no exception path,
and `readmits` belongs to a LATER Cycle's `open_cycle`.

A dependency event bound to a superseded revision can be re-published;
`spq_ledger.satisfied_map` expects exactly that. A cut bound the same way
could not be re-issued. The asymmetry was a missing verb rather than a bad
check, and `rebind_cut` is that verb: it APPENDS a re-affirmation under a
named principal rather than editing the cut, because a mutable cut is a
rewritable reason.

The escape the reporting project actually used — reverting the supersession —
keeps working, and is now principled rather than accidental: the original
record binds the restored declaration for the same reason it bound it before.
That escape is available only while nothing is bound to the superseding
revision, which is why the verb has to exist.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import cycle_authority as authority
import cycle_barrier as barrier
import spq_state_machine as sm

from _spq_fixture import CYCLE_KWARGS, unit as _fx_unit


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=False)


@pytest.fixture
def cut_cycle(tmp_path: Path, monkeypatch):
    """A Cycle with two admitted units, one of them cut."""
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
    sm.approve_baseline(
        str(project),
        approved_by="lead@h3t.co",
        baseline_ref="baseline-1",
        calibration={"sample_units": 2, "measured_hours": 8},
    )
    sm.open_cycle(
        str(project),
        goal="cycle 1",
        admitted_units=[_fx_unit("WU-01"), _fx_unit("WU-02")],
        **CYCLE_KWARGS,
    )
    cycle_id = sm.identity(str(project)).cycle_id
    sm.cut_work_unit(
        str(project), "WU-02", "the client deferred the export view",
        cut_by="lead@h3t.co",
    )
    return project, cycle_id


def _supersede(project: Path, cycle_id: str) -> str:
    """Correct something after the cut, the way the reporting project had to."""
    sm.revise_manifest(
        str(project), cycle_id=cycle_id,
        reason="correct a misclassification that surfaced after the cut",
        revised_by="lead@h3t.co",
        goal="cycle 1, corrected",
    )
    return str(sm.read_manifest(str(project), cycle_id).get("declaration_hash") or "")


@pytest.mark.unit
def test_rebind_cut_is_an_accountable_action_with_an_owner():
    assert authority.ACTION_OWNER["rebind-cut"] == "engineering-lead"


@pytest.mark.unit
def test_a_supersession_after_a_cut_refuses_and_names_the_route(cut_cycle):
    """The check still fires — it is load-bearing and is not relaxed here.
    What changes is that the refusal now names a verb somebody can run."""
    project, cycle_id = cut_cycle
    _supersede(project, cycle_id)

    with pytest.raises(barrier.BarrierError) as excinfo:
        barrier.effective_set(
            sm.read_manifest(str(project), cycle_id),
            sm.read_cuts(str(project), cycle_id),
        )

    message = str(excinfo.value)
    assert "rebind_cut --unit-id WU-02" in message
    assert "bound to the declaration it was authorized under" in message


@pytest.mark.unit
def test_rebind_unblocks_the_barrier_and_keeps_the_unit_cut(cut_cycle):
    project, cycle_id = cut_cycle
    new_hash = _supersede(project, cycle_id)

    result = sm.rebind_cut(str(project), "WU-02", principal="lead@h3t.co")

    assert result["rebound"] is True
    assert result["declaration_hash"] == new_hash
    assert result["effective_set"] == ["WU-01"], "the unit stays cut"


@pytest.mark.unit
def test_the_original_cut_is_not_edited(cut_cycle):
    """`link_readmission`'s rule: a mutable cut is a rewritable reason."""
    project, cycle_id = cut_cycle
    before = sm.read_cuts(str(project), cycle_id)[0]
    old_hash, old_reason, old_at = (
        before["declaration_hash"], before["reason"], before["cut_at"],
    )
    _supersede(project, cycle_id)
    sm.rebind_cut(str(project), "WU-02", principal="lead@h3t.co")

    chain = sm.read_cuts(str(project), cycle_id)
    assert len(chain) == 2, "the re-affirmation appends rather than replacing"
    assert chain[0]["declaration_hash"] == old_hash
    assert chain[0]["reason"] == old_reason
    assert chain[0]["cut_at"] == old_at
    assert chain[1]["supersedes"]["declaration_hash"] == old_hash
    assert chain[1]["reason"] == old_reason, "the reason carries forward unedited"
    assert chain[1]["decision"]["principal"] == "lead@h3t.co"


@pytest.mark.unit
def test_rebinding_twice_does_not_grow_the_chain(cut_cycle):
    project, cycle_id = cut_cycle
    _supersede(project, cycle_id)
    sm.rebind_cut(str(project), "WU-02", principal="lead@h3t.co")
    second = sm.rebind_cut(str(project), "WU-02", principal="lead@h3t.co")

    assert second["rebound"] is False
    assert len(sm.read_cuts(str(project), cycle_id)) == 2


@pytest.mark.unit
def test_the_cut_rate_numerator_still_counts_one_withdrawal(cut_cycle):
    """Two records, one cut. `cut_work_unit`'s idempotency comment names this
    hazard for retries; a re-affirmation must not reintroduce it."""
    project, cycle_id = cut_cycle
    _supersede(project, cycle_id)
    sm.rebind_cut(str(project), "WU-02", principal="lead@h3t.co")

    cuts = sm.read_cuts(str(project), cycle_id)
    assert len(cuts) == 2
    assert sorted({str(c["unit_id"]) for c in cuts}) == ["WU-02"]


@pytest.mark.unit
def test_revising_back_does_not_restore_the_binding(cut_cycle):
    """Which is why the verb has to exist rather than the operator "just
    revising it back". A revision that restores the previous CONTENT is still
    a new declaration — it carries `supersedes` — so it gets a new hash and
    the cut is bound to neither. Superseding again only deepens the hole."""
    project, cycle_id = cut_cycle
    original = str(sm.read_cuts(str(project), cycle_id)[0]["declaration_hash"])
    _supersede(project, cycle_id)
    sm.revise_manifest(
        str(project), cycle_id=cycle_id, reason="revert the correction",
        revised_by="lead@h3t.co", goal="cycle 1",
    )
    restored = sm.read_manifest(str(project), cycle_id)

    assert str(restored.get("declaration_hash")) != original
    with pytest.raises(barrier.BarrierError, match="rebind_cut --unit-id WU-02"):
        barrier.effective_set(restored, sm.read_cuts(str(project), cycle_id))


@pytest.mark.unit
def test_restoring_the_exact_bytes_binds_again(cut_cycle):
    """The escape the reporting project actually used — a git-level restore of
    the sealed document, not a revision. It keeps working, and now for a
    stated reason rather than by accident: the original record binds the
    restored declaration because it is the same declaration.

    It is available only while nothing has been bound to the superseding
    revision, which is exactly the window `rebind_cut` exists to survive."""
    import spq_paths

    project, cycle_id = cut_cycle
    sealed_before = Path(spq_paths.manifest_path(str(project), cycle_id)).read_bytes()
    committed = Path(spq_paths.committed_manifest_path(str(project), cycle_id))
    original = str(sm.read_cuts(str(project), cycle_id)[0]["declaration_hash"])
    _supersede(project, cycle_id)

    for path in (Path(spq_paths.manifest_path(str(project), cycle_id)), committed):
        path.chmod(0o644)
        path.write_bytes(sealed_before)

    restored = sm.read_manifest(str(project), cycle_id)
    assert str(restored.get("declaration_hash")) == original
    assert barrier.effective_set(
        restored, sm.read_cuts(str(project), cycle_id)
    ) == ["WU-01"]


@pytest.mark.unit
def test_rebind_is_not_a_way_to_cut_something(cut_cycle):
    project, _ = cut_cycle
    with pytest.raises(ValueError, match="not a way to cut"):
        sm.rebind_cut(str(project), "WU-01", principal="lead@h3t.co")


@pytest.mark.unit
def test_rebind_refuses_a_declaration_that_never_admitted_the_unit(cut_cycle):
    project, cycle_id = cut_cycle
    cut = sm.read_cuts(str(project), cycle_id)[0]
    foreign = dict(sm.read_manifest(str(project), cycle_id))
    foreign["admitted_units"] = [u for u in foreign["admitted_units"]
                                 if str(u.get("id")) != "WU-02"]
    foreign["declaration_hash"] = "sha256:" + "f" * 60

    with pytest.raises(barrier.BarrierError, match="does not admit WU-02"):
        barrier.rebind_cut(
            cut=cut, declaration=foreign, principal="lead@h3t.co",
        )


@pytest.mark.unit
def test_an_uncut_cycle_and_a_same_revision_cut_are_unaffected(cut_cycle):
    """No supersession: the original binding holds and nothing new is needed."""
    project, cycle_id = cut_cycle
    assert barrier.effective_set(
        sm.read_manifest(str(project), cycle_id),
        sm.read_cuts(str(project), cycle_id),
    ) == ["WU-01"]
