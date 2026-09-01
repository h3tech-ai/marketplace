"""Layer 1 — validated hydration from the Cycle manifest (#303 §2).

This closes #303's problem 2 verbatim: *"each workstream can hydrate local state
from caller-supplied Work Unit JSON, allowing stale or inconsistent views of the
Cycle."*

Failing closed matters more here than anywhere else in SPQ. A workstream that
hydrates a wrong admitted set will pass its own DoD, declare readiness, and only
be caught at the barrier -- after the work is done. Every refusal below is a
case where hydrating would produce a board that disagrees with the Cycle.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import spq_manifest as mf
import spq_paths as sp
import spq_state_machine as sm
import state_store as store


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=False)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    """A real git repo with one commit, so a baseline exists."""
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
    return project


def _open_cycle(project: Path, workstream: str = "spine") -> str:
    """Open Cycle 1 with one unit per workstream. Returns the cycle_id."""
    sm.initialize(str(project), workstream_id=workstream)
    # approve_baseline records the approval AND transitions to COMMIT in one
    # write, so a separate transition would be an illegal COMMIT -> COMMIT.
    sm.approve_baseline(str(project), approved_by="t")
    sm.open_cycle(
        str(project),
        1,
        "cycle 1",
        [
            {"id": "WU-1", "title": "spine work", "labels": ["ws:spine"]},
            {"id": "WU-2", "title": "frame work", "labels": ["ws:frame"]},
        ],
    )
    return sm.identity(str(project)).cycle_id


def _tamper(project: Path, cycle_id: str, **changes) -> None:
    """Edit the sealed manifest WITHOUT re-hashing it."""
    path = Path(sp.manifest_path(str(project), cycle_id))
    path.chmod(0o644)
    body = json.loads(path.read_text(encoding="utf-8"))
    body.update(changes)
    path.write_text(json.dumps(body), encoding="utf-8")


# ── the manifest is authoritative, the caller's JSON is not ────────────────


def test_hydration_uses_the_manifest_and_discards_caller_supplied_units(repo: Path):
    """#303 problem 2. The caller's JSON is DISCARDED, not merged: merging
    would let a stale prompt re-admit a unit the Cycle cut, which is exactly
    the inconsistency the manifest exists to prevent."""
    cycle_id = _open_cycle(repo)
    sp.write_pin(str(repo), "frame")

    # A caller passing a bogus unit must not get it onto the board.
    result = sm.hydrate_cycle(
        str(repo), 1, [{"id": "NOT-ADMITTED", "title": "smuggled"}],
        workstream_id="frame",
    )
    ids = [u["id"] for u in result["current_stories"]]
    assert ids == ["WU-2"], ids
    assert "NOT-ADMITTED" not in ids


def test_hydration_admits_only_this_workstreams_units(repo: Path):
    cycle_id = _open_cycle(repo)
    result = sm.hydrate_cycle(str(repo), 1, [], workstream_id="frame")
    assert [u["id"] for u in result["current_stories"]] == ["WU-2"]


def test_hydration_writes_the_validated_projection(repo: Path):
    cycle_id = _open_cycle(repo)
    sm.hydrate_cycle(str(repo), 1, [], workstream_id="frame")
    projection = json.loads(
        Path(sp.projection_path(str(repo), cycle_id, "frame")).read_text()
    )
    assert projection["workstream_id"] == "frame"
    assert [u["id"] for u in projection["work_units"]] == ["WU-2"]
    # Identity only for the other lane -- never its mutable state.
    assert [u["id"] for u in projection["foreign_units"]] == ["WU-1"]
    assert set(projection["foreign_units"][0]) == {
        "id", "owner_workstream", "outputs"
    }


def test_manifest_uses_the_same_configured_branches_as_sync(repo: Path):
    """The manifest and Sync collector must have one branch authority.

    A manifest that records ``cycle/<id>/ws/<workstream>`` while the shipped
    Sync configuration provisions ``ws/<workstream>`` makes every remote event
    invisible unless a consumer merges the producer branch into its own tree.
    """
    (repo / ".synaptory.yaml").write_text(
        'build_mode: spq\n'
        'spq:\n'
        '  workstreams:\n'
        '    - id: "spine"\n'
        '      shared_owner: true\n'
        '    - id: "frame"\n'
        '  sync:\n'
        '    branch_pattern: "ws/{id}"\n'
        '    integration_branch_pattern: "sync/cycle-{n}"\n',
        encoding="utf-8",
    )
    cycle_id = _open_cycle(repo)
    manifest = sm.read_manifest(str(repo), cycle_id)

    assert manifest["integration_ref"] == "sync/cycle-1"
    assert {
        ws["id"]: ws["branch"] for ws in manifest["workstreams"]
    } == {"spine": "ws/spine", "frame": "ws/frame"}


# ── the refusals ───────────────────────────────────────────────────────────


def test_refuses_a_tampered_manifest(repo: Path):
    """A modified admitted set could license work the Cycle never admitted, so
    a hash mismatch must stop hydration rather than be tolerated."""
    cycle_id = _open_cycle(repo)
    _tamper(repo, cycle_id, goal="something else")
    with pytest.raises(sm.HydrationRefusal) as excinfo:
        sm.hydrate_cycle(str(repo), 1, [], workstream_id="frame")
    assert excinfo.value.code == "manifest_tampered"
    assert "Re-pull" in str(excinfo.value)


def test_refuses_a_manifest_that_no_longer_validates(repo: Path):
    cycle_id = _open_cycle(repo)
    path = Path(sp.manifest_path(str(repo), cycle_id))
    path.chmod(0o644)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["work_units"][0]["owner_workstream"] = "ghost"
    body["manifest_hash"] = mf.compute_hash(body)  # re-hash so it is not tampering
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(sm.HydrationRefusal) as excinfo:
        sm.hydrate_cycle(str(repo), 1, [], workstream_id="frame")
    assert excinfo.value.code == "manifest_invalid"


def test_refuses_a_workstream_the_manifest_does_not_declare(repo: Path):
    _open_cycle(repo)
    with pytest.raises(sm.HydrationRefusal) as excinfo:
        sm.hydrate_cycle(str(repo), 1, [], workstream_id="ghost")
    assert excinfo.value.code == "unknown_workstream"
    assert "spine" in str(excinfo.value), "name the workstreams that do exist"


def test_refuses_an_ambiguous_workstream_when_several_are_declared(repo: Path):
    """Guessing would admit another lane's Work Units into this clone."""
    _open_cycle(repo)
    Path(sp.pin_path(str(repo))).unlink()
    with pytest.raises(sm.HydrationRefusal) as excinfo:
        sm.hydrate_cycle(str(repo), 1, [])
    assert excinfo.value.code == "unknown_workstream"
    assert sp.ENV_WORKSTREAM in str(excinfo.value)


def test_a_single_workstream_manifest_needs_no_explicit_id(repo: Path):
    """With one declared lane there is no other whose units could be admitted,
    so the ambiguity the refusal guards against cannot arise."""
    (repo / ".synaptory.yaml").write_text(
        'build_mode: spq\nspq:\n  workstreams:\n    - id: "solo"\n'
        '      shared_owner: true\n',
        encoding="utf-8",
    )
    sm.initialize(str(repo), workstream_id="solo")
    sm.approve_baseline(str(repo), approved_by="t")
    sm.open_cycle(str(repo), 1, "c", [{"id": "WU-1", "title": "t"}])
    Path(sp.pin_path(str(repo))).unlink()
    result = sm.hydrate_cycle(str(repo), 1, [])
    assert [u["id"] for u in result["current_stories"]] == ["WU-1"]


def test_refuses_an_empty_projection(repo: Path):
    """Opening an empty Cycle would make next_action return await_sync
    immediately and let the workstream declare readiness having delivered
    nothing."""
    sm.initialize(str(repo), workstream_id="spine")
    sm.approve_baseline(str(repo), approved_by="t")
    sm.open_cycle(str(repo), 1, "c", [{"id": "WU-1", "labels": ["ws:spine"]}])
    with pytest.raises(sm.HydrationRefusal) as excinfo:
        sm.hydrate_cycle(str(repo), 1, [], workstream_id="frame")
    assert excinfo.value.code == "empty_projection"


def test_refuses_an_unrelated_revision_but_accepts_a_superseding_one(repo: Path):
    """Two unrelated revisions cannot be reconciled automatically; a
    SUPERSEDING one can, because it names what it replaces."""
    cycle_id = _open_cycle(repo)
    sm.hydrate_cycle(str(repo), 1, [], workstream_id="frame")

    # A superseding revision is adopted.
    sm.revise_manifest(
        str(repo), cycle_id=cycle_id, reason="scope change", revised_by="po"
    )
    result = sm.hydrate_cycle(str(repo), 1, [], workstream_id="frame")
    assert [u["id"] for u in result["current_stories"]] == ["WU-2"]

    # An unrelated manifest (same cycle, different lineage) is refused.
    path = Path(sp.manifest_path(str(repo), cycle_id))
    path.chmod(0o644)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["supersedes"] = "sha256:" + "0" * 64
    body["goal"] = "unrelated lineage"
    body["manifest_hash"] = mf.compute_hash(body)
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(sm.HydrationRefusal) as excinfo:
        sm.hydrate_cycle(str(repo), 1, [], workstream_id="frame")
    assert excinfo.value.code == "stale_projection"


def test_every_refusal_carries_a_machine_readable_code(repo: Path):
    """Prose alone would force callers to match on message text, which is how
    a refusal silently stops being handled when the wording improves."""
    cycle_id = _open_cycle(repo)
    _tamper(repo, cycle_id, goal="x")
    try:
        sm.hydrate_cycle(str(repo), 1, [], workstream_id="frame")
    except sm.HydrationRefusal as exc:
        assert isinstance(exc.code, str) and exc.code
        assert isinstance(exc, ValueError), "callers catching ValueError still work"


# ── no manifest: the shipped single-clone flow still works ────────────────


def test_without_a_manifest_the_callers_units_are_still_accepted(tmp_path: Path):
    """A project that has never opened a Cycle has nothing to project from, and
    refusing there would break the shipped flow for the majority case that has
    no cross-workstream risk at all."""
    project = tmp_path / "solo"
    project.mkdir()
    (project / ".synaptory.yaml").write_text("build_mode: spq\n", encoding="utf-8")
    result = sm.hydrate_cycle(
        str(project), 1, [{"id": "WU-9", "title": "t"}], workstream_id="solo"
    )
    assert [u["id"] for u in result["current_stories"]] == ["WU-9"]


# ── revise_manifest ────────────────────────────────────────────────────────


def test_revise_records_what_it_supersedes(repo: Path):
    cycle_id = _open_cycle(repo)
    before = sm.read_manifest(str(repo), cycle_id)
    after = sm.revise_manifest(
        str(repo), cycle_id=cycle_id, reason="drop a unit",
        drop_units=["WU-2"], revised_by="po",
    )
    assert after["supersedes"] == before["manifest_hash"]
    assert after["manifest_revision"] == before["manifest_revision"] + 1
    assert [u["id"] for u in after["work_units"]] == ["WU-1"]
    assert mf.verify_hash(after)


def test_revise_requires_a_reason(repo: Path):
    """A Cycle's admitted set changing mid-flight is a decision, and the audit
    trail should say whose."""
    cycle_id = _open_cycle(repo)
    with pytest.raises(ValueError, match="requires --reason"):
        sm.revise_manifest(str(repo), cycle_id=cycle_id, reason="")


def test_revise_refuses_when_the_current_manifest_is_tampered(repo: Path):
    """What would be superseded is unknown, so `supersedes` would be a lie."""
    cycle_id = _open_cycle(repo)
    _tamper(repo, cycle_id, goal="x")
    with pytest.raises(ValueError, match="does not match its own hash"):
        sm.revise_manifest(str(repo), cycle_id=cycle_id, reason="r")


def test_revise_refuses_a_result_that_would_be_invalid(repo: Path):
    cycle_id = _open_cycle(repo)
    with pytest.raises(ValueError, match="invalid"):
        sm.revise_manifest(
            str(repo), cycle_id=cycle_id, reason="drop everything",
            drop_units=["WU-1", "WU-2"],
        )


def test_open_cycle_refuses_to_silently_replace_a_sealed_manifest(repo: Path):
    """A silent replacement would leave every hydrated workstream on a manifest
    that no longer exists, with no way to discover that."""
    cycle_id = _open_cycle(repo)
    state = sm.read_state(str(repo))
    state["lifecycle_state"] = "COMMIT"
    sm._write_state(str(repo), state)
    with pytest.raises(ValueError, match="revise_manifest"):
        sm.seal_manifest(
            str(repo), cycle_id=cycle_id, cycle_seq=1, goal="different",
            work_units=[{"id": "WU-9", "labels": ["ws:spine"]}],
        )


# ── the CLI surface ────────────────────────────────────────────────────────


def test_verify_manifest_verb_reports_hash_and_admitted_set(repo: Path):
    cycle_id = _open_cycle(repo)
    result = subprocess.run(
        [sys.executable, sm.__file__, "verify_manifest", str(repo),
         "--cycle-id", cycle_id],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["hash_verified"] is True
    assert payload["problems"] == []
    assert sorted(payload["admitted"]) == ["WU-1", "WU-2"]
    assert payload["owners"] == {"WU-1": "spine", "WU-2": "frame"}


def test_verify_manifest_reports_a_tampered_manifest_rather_than_crashing(repo: Path):
    """`/doctor` needs a verdict, not a traceback."""
    cycle_id = _open_cycle(repo)
    _tamper(repo, cycle_id, goal="x")
    result = subprocess.run(
        [sys.executable, sm.__file__, "verify_manifest", str(repo),
         "--cycle-id", cycle_id],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["hash_verified"] is False
