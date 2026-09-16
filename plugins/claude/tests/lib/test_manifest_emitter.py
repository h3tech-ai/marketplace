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

import cycle_records
import manifest_emitter as me
import spq_paths as sp


@pytest.fixture
def project(tmp_path):
    (tmp_path / ".synaptory").mkdir()
    return tmp_path


def _write_cycle_manifest(project_dir, cycle_id, digest, *, legacy=False):
    """A sealed manifest on disk, under the CURRENT digest key by default.

    `legacy=True` writes `manifest_hash`, which is what seals carried before
    #644 renamed the field to `declaration_hash`. Both shapes must ship: this
    module's job is to deliver seals ALREADY ON DISK, and a project that opened
    a Cycle under the older runtime still has one there. So which key a fixture
    writes is part of what it tests rather than an incidental spelling -- and
    writing only the legacy one is why the rest of this file kept passing while
    the sweep shipped nothing for a live Cycle.
    """
    path = Path(sp.manifest_path(str(project_dir), cycle_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    field = "manifest_hash" if legacy else cycle_records.HASH_FIELD
    path.write_text(
        json.dumps({"cycle_id": cycle_id, field: digest}), encoding="utf-8"
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
def test_the_sweep_covers_every_sealed_cycle(project, shipper):
    """Was `..._cycles_and_coordination_cycles`. There is one kind now.

    `SPD-194` retires the Coordination Cycle, so the second root the sweep
    walked is gone -- `_pending` records the removal in place rather than
    leaving the tuple looking accidentally short. What survives, and is the
    half that mattered, is that the sweep ranges over the STORE rather than
    over a queue of emissions: any sealed Cycle without a shipped marker gets
    delivered, including one whose emission site was never reached.
    """
    _write_cycle_manifest(project, sp.new_cycle_id(1), "b" * 64)
    _write_cycle_manifest(project, sp.new_cycle_id(2), "c" * 64)

    out = me.resend_pending(str(project))
    assert out["shipped"] == 2
    assert sorted(shipper["calls"]) == sorted([
        ("cycle-manifest", "b" * 64),
        ("cycle-manifest", "c" * 64),
    ])


@pytest.mark.unit
def test_a_seal_written_before_the_rename_still_ships(project, shipper):
    """The sweep's whole premise: it delivers what is on disk.

    A project that opened its Cycle before #644 has a manifest keyed
    `manifest_hash`; one that opened it after has `declaration_hash`. Reading
    only the current key would strand every older Cycle permanently, since
    nothing rewrites a sealed document -- the hash is over its own body.

    Reading only the OLD key is the defect this pins from the other side: it is
    what #644 left behind, so `_pending` saw no digest on any live Cycle,
    treated it as unsealed, and `cycle_manifests` had no producer at all.
    """
    _write_cycle_manifest(project, sp.new_cycle_id(9), "f" * 64, legacy=True)
    assert me.resend_pending(str(project))["shipped"] == 1
    # The WIRE field is unchanged whichever key the document used: the ingest
    # endpoint dedupes on `manifest_hash` and the CLI refuses a payload without
    # it, so the digest is projected rather than renamed on the way out.
    assert shipper["calls"] == [("cycle-manifest", "f" * 64)]


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


# ── reading the recorded revision back (#507) ────────────────────────────────
#
# WHO DECIDES WHETHER THIS PROJECT IS CONNECTED took two attempts. Asking
# whenever a CLI resolved sent every offline checkout to the network. Reading a
# marker directory inside the project fixed that and handed the graded
# principal a way to self-declare offline by deleting it, which is the same
# authority-boundary failure #507 rejects one level out. The CLI answers it
# now, from its own stamp and session, and neither is in the project.


def _cli(monkeypatch, payload=None, *, returncode=0, stderr=""):
    """A `cycle-authority` that answers `payload`, without a subprocess."""
    monkeypatch.setattr(me, "_resolve_cli", lambda: "/usr/bin/synaptory")

    class _Proc:
        pass

    _Proc.returncode = returncode
    _Proc.stdout = "" if payload is None else json.dumps(payload)
    _Proc.stderr = stderr
    monkeypatch.setattr(me.subprocess, "run", lambda *a, **k: _Proc())


def test_no_cli_is_silence_not_a_problem(project, monkeypatch):
    """A tree with no resolvable CLI reports nowhere, so the gate keeps the
    local marker and its declared limit rather than blocking."""
    monkeypatch.setattr(me, "_resolve_cli", lambda: None)
    assert me.read_cycle_authority(str(project), "1-abc") == (None, "")


def test_an_unconnected_answer_is_silence(project, monkeypatch):
    """`connected: false` is an ANSWER, not a failure: this install has no
    control plane or no session for one, which is the offline case."""
    _cli(monkeypatch, {"connected": False})
    assert me.read_cycle_authority(str(project), "1-abc") == (None, "")


def test_deleting_project_files_cannot_fake_being_unconnected(
    project, monkeypatch
):
    """THE FINDING that retired the marker (#396, #507).

    Connectedness used to be read from a directory the emitter writes inside
    the project, so deleting it stopped the gate asking. Nothing in the project
    is consulted now: the same call with the project emptied still returns the
    recorded revision.
    """
    _cli(monkeypatch, {"connected": True, "manifest_hash": "sha256:beef"})
    import shutil

    shutil.rmtree(str(project / ".synaptory"), ignore_errors=True)
    assert me.read_cycle_authority(str(project), "1-abc") == ("sha256:beef", "")


def test_a_connected_project_reads_the_recorded_revision(project, monkeypatch):
    _cli(monkeypatch, {"connected": True, "manifest_hash": "sha256:beef"})
    assert me.read_cycle_authority(str(project), "1-abc") == ("sha256:beef", "")


def test_a_failing_read_is_a_problem_and_names_it(project, monkeypatch):
    """A session exists and the read failed, which is the case the gate must
    fail closed on. The verb prints the SAME shape it prints on success, with
    `error` set, so this is distinguishable from a binary that never had the
    verb."""
    _cli(
        monkeypatch,
        {"connected": True, "error": "connection refused"},
        returncode=1,
        stderr="connection refused",
    )
    recorded, problem = me.read_cycle_authority(str(project), "1-abc")
    assert recorded is None
    assert "connection refused" in problem, problem


def test_a_connected_project_with_no_record_is_a_problem(project, monkeypatch):
    """A connected project whose Cycle the control plane does not hold is a
    disagreement, not a permission to proceed."""
    _cli(monkeypatch, {"connected": True, "manifest_hash": ""})
    recorded, problem = me.read_cycle_authority(str(project), "1-abc")
    assert recorded is None
    assert "holds no revision" in problem, problem


def test_a_cli_without_the_verb_is_a_problem_not_silence(project, monkeypatch):
    """VERSION SKEW IS A REASON THE AUTHORITY CANNOT BE READ (#396).

    A CLI predating this verb prints cobra's help and exits ZERO, so an exit
    code cannot tell "cannot answer" from "answered". The self-identifying
    answer solves that; what it must not do is call the absence OFFLINE. A
    stamped tree with an old or substituted binary still has an out-of-reach
    `cycle_manifests` row, so accepting silence there grades the
    agent-writable local revision on a connected project.

    I argued the other way, that refusing would block every project until
    every install was updated. Review's answer holds: skew is a reason the
    record cannot be read, not evidence that there is nothing to read. The
    refusal is ACTIONABLE instead, because the remedy is not discoverable
    from a blocked gate.
    """
    monkeypatch.setattr(me, "_resolve_cli", lambda: "/usr/bin/synaptory")

    class _Proc:
        returncode = 0
        stdout = "Ship telemetry events\n\nUsage:\n  synaptory telemetry [command]\n"
        stderr = ""

    monkeypatch.setattr(me.subprocess, "run", lambda *a, **k: _Proc())
    recorded, problem = me.read_cycle_authority(str(project), "1-abc")
    assert recorded is None
    assert "answered nothing" in problem, problem
    assert "update the CLI" in problem, "the refusal does not say what to do"


def test_a_substituted_executable_is_a_problem_too(project, monkeypatch):
    """`SYNAPTORY_CLI_BIN` naming something that answers nothing reaches the
    same branch, and must reach the same verdict: substitution is not a way to
    tell the gate this project reports nowhere."""
    monkeypatch.setattr(me, "_resolve_cli", lambda: "/bin/true")

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(me.subprocess, "run", lambda *a, **k: _Proc())
    recorded, problem = me.read_cycle_authority(str(project), "1-abc")
    assert recorded is None
    assert problem, "a silent executable passed as an offline project"


def _real_resolver(monkeypatch, tmp_path, *, stamped, cli_bin=None):
    """Run the REAL `_resolve_cli` with the stamp and the search path controlled.

    Patching the resolver's RESULT is what hid this defect: `None` carries an
    unstamped tree, a stamped tree with nothing installed, a stamped tree whose
    candidates fail the identity probe, and any exception, and only the first is
    offline. So these arms patch the resolver's INPUT (the channel signal) and
    the places it looks, and let it decide.
    """
    import host_env

    # Both, because they answer different questions from the same files: the
    # resolver asks WHICH channel, and the reader asks WHETHER one exists.
    monkeypatch.setattr(
        host_env,
        "channel_signal",
        lambda: (False, "cp-url stamp (https://cp.example)" if stamped else ""),
    )
    monkeypatch.setattr(
        host_env,
        "control_plane_stamp_state",
        lambda: (
            ("stamped", "cp-url stamp (https://cp.example)")
            if stamped
            else ("unstamped", "")
        ),
    )
    empty = tmp_path / "empty-bin"
    empty.mkdir(exist_ok=True)
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True, exist_ok=True)
    (home / "bin").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("HOME", str(home))
    if cli_bin is None:
        monkeypatch.delenv("SYNAPTORY_CLI_BIN", raising=False)
    else:
        monkeypatch.setenv("SYNAPTORY_CLI_BIN", str(cli_bin))


def test_an_unstamped_tree_with_no_cli_is_the_only_silence(
    project, monkeypatch, tmp_path
):
    """The genuine offline arm. An unstamped tree addresses no control plane,
    so there is no record to require and the DoD gate must still evaluate:
    local delivery state is canonical (§3.3)."""
    _real_resolver(monkeypatch, tmp_path, stamped=False)
    assert me.read_cycle_authority(str(project), "1-abc") == (None, "")


def test_a_stamped_tree_with_no_installed_cli_is_a_problem(
    project, monkeypatch, tmp_path
):
    """THE FINDING (#396). `_resolve_cli` answers None here too, and reading
    that as offline meant removing or renaming the matching CLI turned a
    connected governed project into the offline authority path.

    A missing CLI is a reason the record cannot be read; only a missing STAMP
    means there is no record.
    """
    _real_resolver(monkeypatch, tmp_path, stamped=True)
    recorded, problem = me.read_cycle_authority(str(project), "1-abc")
    assert recorded is None
    assert "no usable CLI resolves" in problem, problem
    assert "cp.example" in problem, "the refusal does not name the stamp"


def test_a_stamped_tree_whose_explicit_binary_is_rejected_is_a_problem(
    project, monkeypatch, tmp_path
):
    """`SYNAPTORY_CLI_BIN` naming a binary that fails the identity probe, or is
    not executable at all, reaches the resolver's same None. Substitution is
    not a way to tell a stamped tree it reports nowhere."""
    bogus = tmp_path / "not-a-cli"
    bogus.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    bogus.chmod(0o755)
    _real_resolver(monkeypatch, tmp_path, stamped=True, cli_bin=bogus)
    recorded, problem = me.read_cycle_authority(str(project), "1-abc")
    assert recorded is None
    assert problem, "a rejected explicit binary passed as an offline project"


def test_a_signal_that_cannot_be_read_is_a_problem_not_absence(
    project, monkeypatch, tmp_path
):
    """THE SAME THREE-STATE ERROR, ONE LEVEL EARLIER (#396).

    I wrote this to read as unstamped, reasoning that blocking on a question
    the install cannot ask would refuse every project on a broken tree. That is
    the fail-open again: "the signal could not be read" is unknown authority,
    not proof that no control plane exists, so a damaged install, an import
    failure, or a cp-url whose permissions deny it all granted the offline
    downgrade.

    Only an EXPLICIT no-stamp answer may take the offline path.
    """
    import host_env

    def _boom():
        raise RuntimeError("no host_env on this tree")

    monkeypatch.setattr(host_env, "control_plane_stamp_state", _boom)
    monkeypatch.setattr(me, "_resolve_cli", lambda: None)
    recorded, problem = me.read_cycle_authority(str(project), "1-abc")
    assert recorded is None
    assert "cannot report which control plane" in problem, problem
    assert "Repair the install" in problem, "the refusal does not say what to do"


def test_a_stamp_that_exists_and_cannot_be_read_is_a_problem(
    project, monkeypatch, tmp_path
):
    """The other half, from the files themselves rather than an exception.

    `_read_stamp` returns "" for a stamp that is absent, for one that is the
    placeholder, and for one whose permissions deny it, and only the first two
    mean this tree addresses nothing. `control_plane_stamp_state` separates
    them by asking whether the path exists.
    """
    import host_env

    stamp_dir = tmp_path / "hooks" / "lib"
    stamp_dir.mkdir(parents=True)
    denied = stamp_dir / "cp-url"
    denied.write_text("https://cp.example\n", encoding="utf-8")
    denied.chmod(0o000)
    try:
        state, detail = host_env._read_stamp_state(stamp_dir)
        assert state == "", state
        assert detail is True, "an unreadable stamp read as simply absent"
    finally:
        denied.chmod(0o644)


def test_a_nonzero_exit_with_the_answer_shape_still_fails_closed(
    project, monkeypatch
):
    """Non-zero WITH the vocabulary is a real failure, not an old binary."""
    _cli(monkeypatch, {"connected": True}, returncode=1, stderr="boom")
    recorded, problem = me.read_cycle_authority(str(project), "1-abc")
    assert recorded is None
    assert "boom" in problem, problem
