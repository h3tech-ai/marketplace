"""Layer 1 — SPQ manifest delivery and its retry (#334 G8).

The emission used to be one attempt inside a bare `try/except: pass`. On
2026-09-01 it failed with `no project_id resolved`, `cycle_manifests` stayed
empty, and `/cycles` had no producer at all until a manifest was shipped by
hand. The non-blocking property was right; "exactly once, then forget" was not.

What these tests pin:

1. a failed ship leaves no marker, so the NEXT contact retries it,
2. a succeeded ship leaves one, so the next contact does not re-ship,
3. the sweep finds manifests the old emission site never reached, and
4. nothing here can raise into a ceremony.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import manifest_emitter as me
import spq_paths as sp


@pytest.fixture
def project(tmp_path):
    (tmp_path / ".synaptory").mkdir()
    return tmp_path


def _write_cycle_manifest(project_dir, cycle_id, digest):
    path = Path(sp.manifest_path(str(project_dir), cycle_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"cycle_id": cycle_id, "manifest_hash": digest}),
        encoding="utf-8",
    )
    return path


def _write_coordination_manifest(project_dir, ccid, digest):
    path = Path(sp.coordination_manifest_path(str(project_dir), ccid))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"coordination_cycle_id": ccid, "manifest_hash": digest}),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def shipper(monkeypatch):
    """Record every (verb, hash) `_ship` was asked for; control success."""
    calls = []
    state = {"ok": True}

    def fake_ship(verb, manifest, project_dir):
        calls.append((verb, manifest.get("manifest_hash")))
        return state["ok"]

    monkeypatch.setattr(me, "_ship", fake_ship)
    return {"calls": calls, "state": state}


# ─── the retry itself ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_failed_ship_is_retried_on_the_next_sweep(project, shipper):
    """The reported bug: one attempt, then the manifest was gone from the CP's
    point of view forever."""
    cycle_id = sp.new_cycle_id(7)
    _write_cycle_manifest(project, cycle_id, "d" * 64)

    shipper["state"]["ok"] = False
    first = me.resend_pending(str(project))
    assert first == {"pending": 1, "shipped": 0, "failed": 1}

    shipper["state"]["ok"] = True
    second = me.resend_pending(str(project))
    assert second == {"pending": 1, "shipped": 1, "failed": 0}
    assert shipper["calls"] == [
        ("cycle-manifest", "d" * 64),
        ("cycle-manifest", "d" * 64),
    ]


@pytest.mark.unit
def test_a_shipped_manifest_is_not_re_shipped(project, shipper):
    """The marker is keyed on manifest_hash, which is exactly what the ingest
    dedupes on — so its presence means the row already exists, nothing weaker."""
    cycle_id = sp.new_cycle_id(3)
    _write_cycle_manifest(project, cycle_id, "a" * 64)

    assert me.resend_pending(str(project))["shipped"] == 1
    assert me.resend_pending(str(project)) == {
        "pending": 0, "shipped": 0, "failed": 0,
    }
    assert len(shipper["calls"]) == 1


@pytest.mark.unit
def test_sweep_covers_cycles_and_coordination_cycles(project, shipper):
    _write_cycle_manifest(project, sp.new_cycle_id(1), "b" * 64)
    ccid = sp.new_coordination_cycle_id(2)
    _write_coordination_manifest(project, ccid, "c" * 64)

    out = me.resend_pending(str(project))
    assert out["shipped"] == 2
    assert sorted(shipper["calls"]) == sorted([
        ("cycle-manifest", "b" * 64),
        ("coordination-manifest", "c" * 64),
    ])


@pytest.mark.unit
def test_a_revision_ships_as_its_own_row(project, shipper):
    """`cycle_manifests` is append-only so a revision is its own record. A new
    revision seals a new hash, so the marker for the prior one does not
    suppress it."""
    cycle_id = sp.new_cycle_id(4)
    _write_cycle_manifest(project, cycle_id, "1" * 64)
    me.resend_pending(str(project))

    _write_cycle_manifest(project, cycle_id, "2" * 64)  # revised in place
    assert me.resend_pending(str(project))["shipped"] == 1
    assert shipper["calls"] == [
        ("cycle-manifest", "1" * 64),
        ("cycle-manifest", "2" * 64),
    ]


@pytest.mark.unit
def test_direct_emit_marks_so_the_sweep_does_not_duplicate_it(project, shipper):
    """`emit_cycle_manifest` is still the primary path — the sweep is the safety
    net. The two must not each ship the same document."""
    cycle_id = sp.new_cycle_id(5)
    _write_cycle_manifest(project, cycle_id, "e" * 64)

    assert me.emit_cycle_manifest(
        {"cycle_id": cycle_id, "manifest_hash": "e" * 64},
        project_dir=str(project),
    ) is True
    assert me.resend_pending(str(project))["pending"] == 0
    assert len(shipper["calls"]) == 1


# ─── never blocks a ceremony ─────────────────────────────────────────────────


@pytest.mark.unit
def test_sweep_survives_an_unreadable_manifest(project, shipper):
    """A corrupt document must not stop the manifests either side of it."""
    good = sp.new_cycle_id(8)
    _write_cycle_manifest(project, good, "f" * 64)
    bad_dir = Path(
        sp.cycle_root(str(project), sp.new_cycle_id(9))
    )
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "manifest.json").write_text("{not json", encoding="utf-8")

    out = me.resend_pending(str(project))
    assert out["shipped"] == 1
    assert shipper["calls"] == [("cycle-manifest", "f" * 64)]


@pytest.mark.unit
def test_sweep_never_raises_when_the_shipper_explodes(project, monkeypatch):
    _write_cycle_manifest(project, sp.new_cycle_id(6), "9" * 64)

    def boom(*_a, **_k):
        raise RuntimeError("CLI exploded")

    monkeypatch.setattr(me, "_ship", boom)
    assert me.resend_pending(str(project)) == {
        "pending": 1, "shipped": 0, "failed": 1,
    }


@pytest.mark.unit
def test_sweep_on_a_project_with_no_manifests_is_a_no_op(project, shipper):
    assert me.resend_pending(str(project)) == {
        "pending": 0, "shipped": 0, "failed": 0,
    }
    assert shipper["calls"] == []


@pytest.mark.unit
def test_marker_cannot_escape_its_directory(project, shipper):
    """The hash is read off a document on disk, so a malformed one must not be
    able to name a path outside the marker dir."""
    me._mark_shipped(str(project), "../../escaped")
    marker_dir = Path(project) / me._SHIPPED_RELDIR
    assert (marker_dir / "escaped").exists()
    assert not (Path(project) / "escaped").exists()


@pytest.mark.unit
def test_the_marker_suppresses_a_repeat_direct_emit(project, shipper):
    """`seal_manifest` runs on every re-open, so an unguarded direct emit
    re-POSTed the same document each time and the marker only meant "the sweep
    may skip this" rather than "delivered"."""
    doc = {"cycle_id": "1-abcd1234", "manifest_hash": "7" * 64}

    assert me.emit_cycle_manifest(doc, project_dir=str(project)) is True
    assert me.emit_cycle_manifest(doc, project_dir=str(project)) is True
    assert len(shipper["calls"]) == 1


@pytest.mark.unit
def test_a_failed_direct_emit_leaves_the_retry_open(project, shipper):
    doc = {"cycle_id": "1-abcd1234", "manifest_hash": "8" * 64}
    shipper["state"]["ok"] = False
    assert me.emit_cycle_manifest(doc, project_dir=str(project)) is False
    assert me.already_shipped(str(project), "8" * 64) is False

    shipper["state"]["ok"] = True
    assert me.emit_cycle_manifest(doc, project_dir=str(project)) is True
    assert len(shipper["calls"]) == 2


# ─── the issue's own verification, without a live stack ──────────────────────


@pytest.mark.unit
def test_control_plane_down_then_up_lands_the_manifest_with_no_manual_ship(
    project, monkeypatch
):
    """#334's stated check: kill the CP, open a Cycle, bring it back, run any
    CLI contact, and assert the row exists with no manual ship in the path.

    Stands in for the live stack by failing `_ship` while the CP is "down".
    What is being verified is that nothing between the two states drops the
    manifest — before this, the single attempt happened while it was down and
    there was no second one, ever.
    """
    import spq_paths as paths

    cycle_id = paths.new_cycle_id(11)
    _write_cycle_manifest(project, cycle_id, "c" * 64)

    shipped = []
    down = {"value": True}

    def flaky_ship(verb, manifest, project_dir):
        if down["value"]:
            return False
        shipped.append((verb, manifest.get("manifest_hash")))
        return True

    monkeypatch.setattr(me, "_ship", flaky_ship)

    # CP down: the ceremony completes, nothing lands, nothing raises.
    assert me.resend_pending(str(project))["failed"] == 1
    assert shipped == []

    # CP up. The next contact is the SessionStart sweep; no operator action.
    down["value"] = False
    assert me.resend_pending(str(project))["shipped"] == 1
    assert shipped == [("cycle-manifest", "c" * 64)]

    # And it does not land twice on the contact after that.
    assert me.resend_pending(str(project))["pending"] == 0
    assert len(shipped) == 1
