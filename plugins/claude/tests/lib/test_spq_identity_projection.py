"""Layer 1 — SPQ workstream identity and manifest projection (#328, from #323).

Three defects found by driving one Cycle across three hosts, all in how a clone
decides which lane it is and which Work Units belong to it:

  G1  `SYNAPTORY_WORKSTREAM` beat an explicit `--workstream`
  G2  the clone that opened the Cycle held every lane's units and could not narrow
  G12 an unresolvable identity put a phantom `default` lane in a SEALED manifest

They share a root: `open_cycle` took no workstream argument, so every path that
needed the caller's lane re-resolved it from the environment instead.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import spq_state_machine as m
import spq_paths as _paths_mod


pytestmark = pytest.mark.unit

_LANES = (
    '    - id: "api"\n      shared_owner: true\n'
    '    - id: "web"\n'
    '    - id: "cli"\n'
)


def _repo(tmp_path: Path, workstreams: str = _LANES) -> Path:
    project = tmp_path / "proj"
    project.mkdir(parents=True)
    for args in (["init", "-q"], ["config", "user.email", "t@e.co"],
                 ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)
    (project / ".gitignore").write_text(
        ".synaptory/*\n!.synaptory/sync/\n!.synaptory/cycles/\n", encoding="utf-8")
    (project / ".synaptory.yaml").write_text(
        'build_mode: "spq"\nspq:\n  workstreams:\n' + workstreams, encoding="utf-8")
    (project / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=project, check=True,
                   capture_output=True)
    return project


def _units(*specs):
    """`("WU-API-1", "api")` -> an admitted unit owned by that lane."""
    return [{"id": uid, "title": uid, "owner_workstream": ws} for uid, ws in specs]


def _to_commit(project: Path) -> None:
    m.initialize(str(project))
    m.transition(str(project), "COMMIT", force=True)


# ─── G1: the explicit flag must beat the environment ────────────────────────


def test_explicit_workstream_beats_the_environment(tmp_path, monkeypatch):
    """`--workstream web` with `SYNAPTORY_WORKSTREAM=api` exported.

    Pre-fix the admitted set came from the flag while the PIN came from the
    env, so the clone believed it was `api` while holding `web`'s work.
    `declare-ready` without `--workstream` would then declare as the wrong
    lane and the quorum would never see the real one.
    """
    project = _repo(tmp_path)
    _to_commit(project)
    monkeypatch.setenv("SYNAPTORY_WORKSTREAM", "api")

    m.open_cycle(str(project), 1, "goal",
                 _units(("WU-WEB-1", "web")), workstream_id="web")

    assert _paths_mod.read_pin(str(project)) == "web", (
        "the environment overwrote the pin the caller asked for"
    )
    state = m.read_state(str(project))
    assert state["_workstream_id"] == "web"


def test_environment_still_answers_when_no_flag_is_given(tmp_path, monkeypatch):
    """The fix must not invert the precedence it is correcting.

    `resolve_identity` documents flag > env > pin > state. With no flag, the
    env is still the right answer.
    """
    project = _repo(tmp_path)
    _to_commit(project)
    monkeypatch.setenv("SYNAPTORY_WORKSTREAM", "cli")

    m.open_cycle(str(project), 1, "goal", _units(("WU-CLI-1", "cli")))

    assert _paths_mod.read_pin(str(project)) == "cli"


def test_hydrate_hands_its_resolved_lane_to_open_cycle(tmp_path, monkeypatch):
    """The end-to-end shape of G1, through the verb operators actually run."""
    project = _repo(tmp_path)
    _to_commit(project)
    monkeypatch.setenv("SYNAPTORY_WORKSTREAM", "api")

    m.hydrate_cycle(str(project), 1, _units(("WU-WEB-1", "web"),
                                            ("WU-WEB-2", "web")),
                    goal="goal", workstream_id="web")

    assert _paths_mod.read_pin(str(project)) == "web"
    assert m.read_state(str(project))["_workstream_id"] == "web"


# ─── G2: the opening clone must be able to narrow to its own lane ───────────


def test_hydrate_narrows_the_clone_that_opened_the_cycle(tmp_path):
    """`open_cycle` then `hydrate_cycle --workstream api` on the same clone.

    `open_cycle` leaves the opener holding every lane's units. Hydrate is how
    it narrows. Pre-fix hydrate compared only the MANIFEST HASH, which matched
    because it is the same document, so it returned `already executing` and
    left the board at `admitted=6`. `manifest_closure` then failed and the only
    recovery was deleting .orchestrator and hydrating fresh.
    """
    project = _repo(tmp_path)
    _to_commit(project)
    all_units = _units(("WU-API-1", "api"), ("WU-API-2", "api"),
                       ("WU-WEB-1", "web"), ("WU-WEB-2", "web"),
                       ("WU-CLI-1", "cli"), ("WU-CLI-2", "cli"))

    m.open_cycle(str(project), 1, "goal", all_units, workstream_id="api")
    assert len(m.read_state(str(project))["current_stories"]) == 6, (
        "precondition: the opening clone holds every lane's units"
    )

    out = m.hydrate_cycle(str(project), 1, _units(("WU-API-1", "api"),
                                                  ("WU-API-2", "api")),
                          goal="goal", workstream_id="api")

    board = [u["id"] for u in m.read_state(str(project))["current_stories"]]
    assert sorted(board) == ["WU-API-1", "WU-API-2"], (
        f"clone did not narrow to its own lane: {board} (hydrated={out.get('hydrated')})"
    )


def test_hydrate_still_short_circuits_when_the_lane_is_unchanged(tmp_path):
    """Re-running hydrate for the SAME lane must stay a cheap no-op.

    The narrowing fix must not turn every idempotent re-run into a re-projection.
    """
    project = _repo(tmp_path)
    _to_commit(project)
    units = _units(("WU-API-1", "api"), ("WU-API-2", "api"))
    m.hydrate_cycle(str(project), 1, units, goal="goal", workstream_id="api")

    out = m.hydrate_cycle(str(project), 1, units, goal="goal", workstream_id="api")
    assert out.get("hydrated") is False
    assert "already executing" in str(out.get("reason", ""))


def test_narrowing_refuses_to_strand_work_in_flight(tmp_path):
    """Re-projecting must not silently drop another lane's STARTED units.

    Dropping a queued unit is fine (it goes back to its owner). Dropping one
    that has started would orphan its receipts, so that stays a refusal.
    """
    project = _repo(tmp_path)
    _to_commit(project)
    m.open_cycle(str(project), 1, "goal",
                 _units(("WU-API-1", "api"), ("WU-WEB-1", "web")),
                 workstream_id="api")
    m.transition_story(str(project), "WU-WEB-1", "in_progress",
                       reason="started by mistake")

    with pytest.raises(m.HydrationRefusal) as exc:
        m.hydrate_cycle(str(project), 1, _units(("WU-API-1", "api")),
                        goal="goal", workstream_id="api")
    assert "WU-WEB-1" in str(exc.value)


# ─── G12: no phantom lane in a hash-sealed manifest ────────────────────────


def test_unowned_unit_refuses_instead_of_inventing_a_default_lane(tmp_path):
    """A unit with no owner and no resolvable identity is a refusal.

    Pre-fix this appended `{"id": "default"}` to the manifest, producing the
    observed `Cycle declares 5 workstreams (api, cli, default, integration,
    web)`. The manifest is hash-sealed, so the phantom was permanent and the
    unit was owned by a lane no clone would ever claim.
    """
    project = _repo(tmp_path)
    _to_commit(project)

    with pytest.raises(m.HydrationRefusal) as exc:
        m.open_cycle(str(project), 1, "goal",
                     [{"id": "WU-ORPHAN-1", "title": "no owner"}])

    message = str(exc.value)
    assert "WU-ORPHAN-1" in message, "the refusal must name the offending unit"
    assert m.SOLO_WORKSTREAM in message or "phantom" in message


def test_no_manifest_ever_declares_the_solo_sentinel_beside_real_lanes(tmp_path):
    """The property that actually matters, asserted on the sealed document."""
    project = _repo(tmp_path)
    _to_commit(project)
    m.open_cycle(str(project), 1, "goal",
                 _units(("WU-API-1", "api"), ("WU-WEB-1", "web")),
                 workstream_id="api")

    state = m.read_state(str(project))
    cycle_id = state["_cycle_id"]
    manifest = json.loads(
        Path(_paths_mod.manifest_path(str(project), cycle_id)).read_text("utf-8"))
    declared = {w["id"] for w in manifest["workstreams"]}
    assert m.SOLO_WORKSTREAM not in declared, (
        f"phantom {m.SOLO_WORKSTREAM!r} lane sealed into the manifest: {declared}"
    )
    assert declared <= {"api", "web", "cli"}, declared


def test_an_unlabelled_unit_is_owned_by_the_lane_admitting_it(tmp_path):
    """The documented fallback must survive: identity resolves, so no refusal.

    `hydrate_cycle` is documented as taking "this workstream's admitted Work
    Units", so an unlabelled unit belongs to the lane admitting it. Only the
    case where there is NO lane to fall back to is a refusal.
    """
    project = _repo(tmp_path)
    _to_commit(project)
    m.open_cycle(str(project), 1, "goal",
                 [{"id": "WU-PLAIN-1", "title": "no owner"}],
                 workstream_id="web")

    state = m.read_state(str(project))
    manifest = json.loads(
        Path(_paths_mod.manifest_path(str(project), state["_cycle_id"]))
        .read_text("utf-8"))
    owners = {u["id"]: u["owner_workstream"] for u in manifest["work_units"]}
    assert owners["WU-PLAIN-1"] == "web"


def test_a_solo_cycle_still_gets_the_sentinel(tmp_path):
    """No declared topology means a genuinely single-clone Cycle.

    `SOLO_WORKSTREAM` is right there and must keep working; the fix targets the
    sentinel appearing BESIDE real lanes, not the sentinel itself.
    """
    project = _repo(tmp_path, workstreams="")
    (project / ".synaptory.yaml").write_text('build_mode: "spq"\n', encoding="utf-8")
    _to_commit(project)

    m.open_cycle(str(project), 1, "goal", [{"id": "WU-1", "title": "solo"}])

    state = m.read_state(str(project))
    manifest = json.loads(
        Path(_paths_mod.manifest_path(str(project), state["_cycle_id"]))
        .read_text("utf-8"))
    assert [w["id"] for w in manifest["workstreams"]] == [m.SOLO_WORKSTREAM]


# ─── The harness that proves the above ──────────────────────────────────────


def test_scaffold_derives_the_repo_from_its_own_location():
    """`infra/scripts/multi-runtime-scaffold.py` must not hardcode a checkout.

    It shipped with `REPO = Path("/Users/<someone>/Working/H3Tech/synaptory")`.
    Two consequences, and the second is the dangerous one:

      1. The playbook (#324) crashes for every other developer.
      2. Run from a worktree on the SAME machine it silently exercised the main
         checkout instead, so a scaffold run could report success against code
         that did not contain the fix under test. That is exactly what happened
         while fixing #328: three separate scaffold results were read as
         evidence before the hardcoded path was noticed.

    A path guard rather than an execution test: running the scaffold takes
    minutes and needs git, but the failure mode is entirely visible in the source.
    """
    scaffold = (Path(__file__).resolve().parents[3]
                / "infra" / "scripts" / "multi-runtime-scaffold.py")
    if not scaffold.is_file():
        pytest.skip("scaffold not present in this tree")
    text = scaffold.read_text(encoding="utf-8")

    offenders = [
        line.strip() for line in text.splitlines()
        if ("Path(" in line or "= \"/" in line)
        and ("/Users/" in line or "/home/" in line)
        and not line.strip().startswith("#")
    ]
    assert not offenders, (
        "scaffold hardcodes an absolute checkout path: %s" % offenders
    )
    assert "Path(__file__)" in text, (
        "scaffold must derive REPO from its own location"
    )

