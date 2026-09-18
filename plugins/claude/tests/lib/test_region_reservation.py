"""Layer 1 -- two clones are kept apart by a registry, or not at all.

`SC-MTH-012` says concurrent Cycles never declare overlapping source regions,
and that is exactly what lets them run without inspecting each other's
admissions. Two clones each hold a sealed declaration the other cannot see
until somebody pushes, so NOTHING LOCAL can enforce it: the registry is the
only party that sees both in time.

The control plane grew the table, the endpoint and the refusal, and then
nothing called it -- `grep -rn "v1/cycles" cli/ core/ plugin-*/` found zero
callers, while the conformance scenario asserted only that `source_region`
was a field. A registry with no client is the shape of defect this epic keeps
finding: the check reads as implemented and runs as nothing.

WHY THERE IS EXACTLY ONE ADMISSIBLE VERDICT. `reserved` proceeds. Everything
else refuses: a `collision` because the registry said no, an `unavailable`
because it said nothing, and a project with no id because a claim cannot be
scoped without one.

The last two took two review rounds to get right, and the mistake is worth
recording. Both were treated as "there is no registry to compete in, so
nothing else can be holding this region" -- which sounds like the unstamped
single-clone case and is not. Two clones of the same unregistered repository
both lack a `project_id`, both get the same answer, and both seal overlapping
Cycles. Missing identity is an INABILITY TO ADDRESS the trusted registry, not
evidence that no peer exists, and issue #643 with
`docs/plans/spq-methodology-alignment-requirements.md:515` say an unavailable
or stale registry refuses admission and dispatch.

The consequence is deliberate and product-visible: **SPQ cannot open a Cycle
without a reachable registry and a project id.** Offline Commit is not a
supported mode, because there is no way to make it one that also keeps the
guarantee concurrency rests on.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import region_registry as registry
import spq_state_machine as sm

from _spq_fixture import CYCLE_KWARGS, unit as _fx_unit


def _fake_cli(tmp_path: Path, body: str, *, code: int = 0) -> Path:
    """A stand-in `synaptory` that answers `cycles regions` and nothing else."""
    script = tmp_path / "fake-synaptory"
    script.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "cycles" ]; then\n'
        "  cat <<'JSON'\n%s\nJSON\n"
        "  exit %d\n"
        "fi\n"
        "exit 0\n" % (body, code),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _use(monkeypatch, cli: Path | None) -> None:
    monkeypatch.setattr(registry, "_resolve_cli", lambda: (str(cli) if cli else None))


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".synaptory.yaml").write_text("build_mode: spq\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=str(proj), check=False)
    sm.initialize(str(proj))
    sm.approve_baseline(
        str(proj),
        approved_by="lead@h3t.co",
        baseline_ref="baseline-1",
        calibration={"sample_units": 1, "measured_hours": 1},
    )
    return proj


def _open(proj: Path):
    return sm.open_cycle(
        str(proj),
        goal="ship the thing",
        admitted_units=[_fx_unit("WU-1")],
        **CYCLE_KWARGS,
    )


# ══ 1. The verdicts, read from the registry rather than assumed ═════════════


def test_a_granted_reservation_admits_the_cycle(project, tmp_path, monkeypatch):
    _use(monkeypatch, _fake_cli(tmp_path, '{"ok": true, "reason": "reserved"}'))
    _open(project)
    cycle_id = sm.identity(str(project)).cycle_id
    sealed = sm.read_manifest(str(project), cycle_id)
    assert sealed["region_reservation"] == {
        "registry": "control-plane",
        "outcome": "reserved",
    }


def test_a_refused_region_refuses_the_commit(project, tmp_path, monkeypatch):
    _use(
        monkeypatch,
        _fake_cli(
            tmp_path,
            '{"ok": false, "reason": "collision", "detail": '
            '"region overlaps Cycle CY-0007"}',
            code=1,
        ),
    )
    with pytest.raises(registry.RegistryError) as caught:
        _open(project)
    assert "CY-0007" in str(caught.value)
    assert sm.identity(str(project)).cycle_id in ("", None)


def test_an_unreachable_registry_refuses_the_commit(project, tmp_path, monkeypatch):
    # The whole point: "I could not ask" is not "no collision". A Commit that
    # seals here is a Commit whose separation nobody checked.
    _use(
        monkeypatch,
        _fake_cli(
            tmp_path,
            '{"ok": false, "reason": "registry_unavailable", "detail": '
            '"connection refused"}',
            code=1,
        ),
    )
    with pytest.raises(registry.RegistryError) as caught:
        _open(project)
    message = str(caught.value)
    assert "could not be asked" in message
    assert "connection refused" in message


def test_a_malformed_answer_is_unavailable_not_a_grant(project, tmp_path, monkeypatch):
    # Exit 0 with nothing parseable must not read as `ok`. A truthy default
    # here would turn every future change to the verb's output into a silent
    # grant.
    _use(monkeypatch, _fake_cli(tmp_path, "not json at all"))
    with pytest.raises(registry.RegistryError):
        _open(project)


def test_a_tree_addressing_no_control_plane_cannot_open_a_cycle(
    project, monkeypatch
):
    """No registry, no Commit. This is the ratified requirement, not a taste.

    An earlier cut sealed `registry: "none"` here and proceeded, reasoning
    that a tree addressing no control plane has no peer reserving in one
    either. Nothing establishes that: a second clone of the same project is
    also unstamped, gets the same answer, and seals an overlapping Cycle.
    """
    _use(monkeypatch, None)
    with pytest.raises(registry.RegistryError) as caught:
        _open(project)
    assert "could not be asked" in str(caught.value)


def test_a_project_with_no_identity_cannot_open_a_cycle(
    project, tmp_path, monkeypatch
):
    """The reviewer's two-clone case, and why "no id" is not "no peer".

    Two clones of the same unregistered repository both lack a `project_id`,
    both receive `no_project_identity`, and if that converted to a proceed
    they would both seal the same region. The refusal names the remedy,
    because a project that should be admitting Cycles needs an id rather than
    a lecture.
    """
    _use(
        monkeypatch,
        _fake_cli(
            tmp_path,
            '{"ok": false, "reason": "no_project_identity", "detail": '
            '"this project names no project_id"}',
            code=1,
        ),
    )
    with pytest.raises(registry.RegistryError) as caught:
        _open(project)
    assert "project_id" in str(caught.value)


def test_the_second_clone_of_an_unregistered_project_is_refused_too(
    project, tmp_path, monkeypatch
):
    """The failure the conversion would have permitted, stated as a test.

    Not "the answer maps to a refusal" -- that is the test above -- but the
    outcome that matters: neither clone gets a sealed Cycle, so there is no
    pair of overlapping declarations for anybody to discover later.
    """
    _use(
        monkeypatch,
        _fake_cli(
            tmp_path,
            '{"ok": false, "reason": "no_project_identity", "detail": "no id"}',
            code=1,
        ),
    )
    for _clone in ("first", "second"):
        with pytest.raises(registry.RegistryError):
            _open(project)
    assert sm.identity(str(project)).cycle_id in ("", None)


def test_the_recorded_verdict_is_inside_the_declaration_hash(
    project, tmp_path, monkeypatch
):
    import cycle_records as records

    _use(monkeypatch, _fake_cli(tmp_path, '{"ok": true, "reason": "reserved"}'))
    _open(project)
    sealed = sm.read_manifest(str(project), sm.identity(str(project)).cycle_id)
    tampered = json.loads(json.dumps(sealed))
    tampered["region_reservation"] = {"registry": "none", "outcome": "none"}
    tampered.pop("declaration_hash", None)
    body = json.loads(json.dumps(sealed))
    body.pop("declaration_hash", None)
    assert records.compute_hash(tampered) != records.compute_hash(body)


def test_a_declaration_that_would_not_seal_leaves_no_reservation(
    project, tmp_path, monkeypatch
):
    # Validate before reserving, or a Commit that was never going to seal
    # leaves the region held by a Cycle that does not exist.
    calls = tmp_path / "calls"
    script = tmp_path / "counting-synaptory"
    script.write_text(
        "#!/bin/bash\necho called >> %s\necho '{\"ok\": true}'\n" % calls,
        encoding="utf-8",
    )
    script.chmod(0o755)
    _use(monkeypatch, script)
    with pytest.raises(Exception):
        sm.open_cycle(
            str(project),
            goal="ship the thing",
            admitted_units=[{"id": "WU-1"}],  # no path_scope: refused
            **CYCLE_KWARGS,
        )
    assert not calls.exists(), "a refused declaration reserved a region anyway"


# ══ 2. `PROCEEDS` is an allowlist, so a new verdict refuses by default ══════


def test_an_unknown_verdict_refuses_rather_than_proceeding(project):
    with pytest.raises(registry.RegistryError):
        registry.assert_admissible({"outcome": "probably_fine", "detail": "?"})


# ══ 3. The Checkpoint gives the region back, and cannot be blocked by it ════


def test_the_close_releases_the_region(project, tmp_path, monkeypatch):
    seen = tmp_path / "verbs"
    script = tmp_path / "recording-synaptory"
    script.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "cycles" ]; then echo "$3" >> %s; '
        "echo '{\"ok\": true, \"reason\": \"reserved\"}'; exit 0; fi\nexit 0\n"
        % seen,
        encoding="utf-8",
    )
    script.chmod(0o755)
    _use(monkeypatch, script)
    _open(project)
    verbs = seen.read_text().split()
    assert verbs == ["reserve"], verbs

    registry.release(str(project), cycle_id=sm.identity(str(project)).cycle_id)
    assert seen.read_text().split() == ["reserve", "release"]


def test_a_release_that_fails_says_how_to_recover_and_does_not_raise(
    project, tmp_path, monkeypatch, capsys
):
    _use(
        monkeypatch,
        _fake_cli(
            tmp_path,
            '{"ok": false, "reason": "registry_unavailable", "detail": "down"}',
            code=1,
        ),
    )
    verdict = registry.release(str(project), cycle_id="CY-0003")
    assert verdict["outcome"] == registry.UNAVAILABLE
    assert "cycles regions release" in capsys.readouterr().err


# ══ 4. A reservation is rechecked at dispatch, because it can be released ═══
#
# `open_cycle` refuses to seal without a grant, but the grant is not permanent:
# it can be released by hand, by a Checkpoint in another clone, or by an
# operator recovering a stuck one -- and at that moment a second Cycle can
# legally claim the region this one is still working in. Nothing local would
# notice, because the sealed declaration still says the region was granted,
# which was true when it was written.
#
# AN OUTAGE DOES NOT REFUSE HERE, and the asymmetry with Commit is the design
# rather than an inconsistency. At Commit "I could not ask" means "somebody may
# already hold this", so sealing would be a guess. At dispatch this Cycle's
# claim is already granted and no other clone can be granted an overlapping one
# while the same registry is down -- they need it to say yes just as much. An
# outage cannot create the collision this check looks for, so refusing through
# one would strand a live Cycle to protect against a state the outage prevents.


def _registry_cli(tmp_path: Path, name: str, listing: dict, *, code: int = 0) -> Path:
    script = tmp_path / name
    script.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "cycles" ] && [ "$3" = "list" ]; then\n'
        "  cat <<'JSON'\n%s\nJSON\n"
        "  exit %d\n"
        "fi\n"
        'if [ "$1" = "cycles" ]; then echo \'{"ok": true, "reason": "reserved"}\'; '
        "exit 0; fi\n"
        "exit 0\n" % (json.dumps(listing), code),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


@pytest.fixture
def reserved_cycle(project, tmp_path, monkeypatch):
    """A Cycle admitted against a registry, so a recheck has something to check."""
    _use(monkeypatch, _fake_cli(tmp_path, '{"ok": true, "reason": "reserved"}'))
    sm.open_cycle(
        str(project),
        goal="ship the thing",
        admitted_units=[_fx_unit("WU-1")],
        **CYCLE_KWARGS,
    )
    return project, sm.identity(str(project)).cycle_id


def _contradiction(project: Path, cli: Path | None, monkeypatch):
    import advance_kernel

    _use(monkeypatch, cli)
    return advance_kernel._region_contradiction(str(project))


def test_a_live_reservation_of_our_own_contradicts_nothing(
    reserved_cycle, tmp_path, monkeypatch
):
    project, cycle_id = reserved_cycle
    cli = _registry_cli(
        tmp_path, "holding", {"reservations": [{"cycle_id": cycle_id,
                                                "region": ["api/"]}]}
    )
    assert _contradiction(project, cli, monkeypatch) is None


def test_a_reservation_that_vanished_refuses_the_dispatch(
    reserved_cycle, tmp_path, monkeypatch
):
    project, _cycle_id = reserved_cycle
    cli = _registry_cli(tmp_path, "empty", {"reservations": []})
    found = _contradiction(project, cli, monkeypatch)
    assert found and found["kind"] == "released"


def test_an_overlapping_holder_refuses_the_dispatch(
    reserved_cycle, tmp_path, monkeypatch
):
    project, cycle_id = reserved_cycle
    cli = _registry_cli(
        tmp_path,
        "overlapping",
        {
            "reservations": [
                {"cycle_id": cycle_id, "region": ["api/"]},
                {"cycle_id": "CY-0099", "region": ["api/orders/"]},
            ]
        },
    )
    found = _contradiction(project, cli, monkeypatch)
    assert found and found["kind"] == "overlap"
    assert found["cycle_id"] == "CY-0099"


def test_a_disjoint_holder_is_not_a_contradiction(
    reserved_cycle, tmp_path, monkeypatch
):
    project, cycle_id = reserved_cycle
    cli = _registry_cli(
        tmp_path,
        "disjoint",
        {
            "reservations": [
                {"cycle_id": cycle_id, "region": ["api/"]},
                {"cycle_id": "CY-0099", "region": ["web/"]},
            ]
        },
    )
    assert _contradiction(project, cli, monkeypatch) is None


def test_an_unreachable_registry_refuses_a_live_dispatch(
    reserved_cycle, tmp_path, monkeypatch
):
    """A recheck that cannot be answered refuses. `#643` again.

    The first cut let a dispatch proceed through an outage, reasoning that no
    other clone can be GRANTED an overlapping claim while the same registry is
    down. That is true and insufficient: a grant made before the outage, or a
    release during it, is exactly the state the recheck exists to notice, and
    an outage is when it cannot. "I could not recheck" is not "the claim
    holds".
    """
    project, _cycle_id = reserved_cycle
    _use(monkeypatch, _registry_cli(tmp_path, "down", {}, code=1))
    found = _contradiction(project, None, monkeypatch)
    assert found and found["kind"] == "unavailable", found


def test_a_cycle_admitted_against_no_registry_cannot_exist(project, monkeypatch):
    """There is no such Cycle any more."""
    _use(monkeypatch, None)
    with pytest.raises(registry.RegistryError):
        _open(project)


def test_a_board_recording_no_grant_refuses_the_recheck(
    reserved_cycle, tmp_path, monkeypatch
):
    """A declaration sealed before the requirement was enforced.

    `open_cycle` cannot produce one now, so meeting it at dispatch means
    reading an older board -- and the honest answer is the same as a failed
    recheck rather than a pass. An earlier cut returned "no contradiction"
    here, which made "admitted without a registry" the one state that skipped
    the check entirely.
    """
    project, cycle_id = reserved_cycle
    sealed = sm.read_manifest(str(project), cycle_id)
    sealed["region_reservation"] = {"registry": "none", "outcome": "none"}
    # The LOCAL copy, which `read_manifest` consults first. Writing only the
    # committed transport would leave the local seal saying `control-plane`
    # and prove nothing about the older board this simulates.
    #
    # `chmod` first: the seal ships read-only, which is the product declining
    # to be edited in place. Simulating a board from before the requirement
    # existed means writing one, so the test does what a tamper would have to
    # do -- visibly, rather than through a product seam that permits it.
    local = Path(sm._paths.manifest_path(str(project), cycle_id))
    local.chmod(0o644)
    local.write_text(json.dumps(sealed), encoding="utf-8")

    cli = _registry_cli(
        tmp_path, "holding",
        {"reservations": [{"cycle_id": cycle_id, "region": ["api/"]}]},
    )
    found = _contradiction(project, cli, monkeypatch)
    assert found and found["kind"] == "unrecorded", found


# ══ 5. The recheck is wired into the dispatch, not merely written ═══════════
#
# Section 4 calls `_region_contradiction` directly, which proves the predicate
# and NOT that anything consults it: unwiring the call site left all six of
# those tests green. That is the exact failure this epic keeps finding -- a
# check that reads as implemented and runs as nothing -- so the refusal is
# asserted here through `evaluate_dispatch`, the boundary that authorizes work.
# Not `execute_advance`: the `queued -> in_progress` edge legitimately has no
# receipt yet, which is precisely why starting work cannot be expressed through
# it -- and the region matters when work STARTS, not when a stage moves.



def test_a_contradicted_region_refuses_a_real_dispatch(
    reserved_cycle, tmp_path, monkeypatch
):
    import advance_kernel as ak

    project, _cycle_id = reserved_cycle
    _use(monkeypatch, _registry_cli(tmp_path, "vanished", {"reservations": []}))
    policy = ak.HostPolicy(host="test", require_next_action_match=False)
    decision = ak.evaluate_dispatch(
        str(project), "WU-1", role="software-engineer", policy=policy
    )
    assert not decision.allowed
    assert decision.code == ak.REGION_CONTRADICTED, decision.reason
    assert "SC-MTH-012" in decision.reason
    assert decision.extra["region_contradiction"]["kind"] == "released"


def test_an_uncontradicted_region_does_not_block_a_dispatch(
    reserved_cycle, tmp_path, monkeypatch
):
    import advance_kernel as ak

    project, cycle_id = reserved_cycle
    _use(
        monkeypatch,
        _registry_cli(
            tmp_path, "intact",
            {"reservations": [{"cycle_id": cycle_id, "region": ["api/"]}]},
        ),
    )
    policy = ak.HostPolicy(host="test", require_next_action_match=False)
    decision = ak.evaluate_dispatch(
        str(project), "WU-1", role="software-engineer", policy=policy
    )
    assert decision.code != ak.REGION_CONTRADICTED, decision.reason
    assert decision.allowed, decision.reason



# ══ 6. The Commit ships its own seal, or a connected project cannot dispatch ═
#
# `story_pipeline.resolve_authored_cases` asks the control plane which revision
# of a Cycle is in force, and FAILS CLOSED when a connected project's authority
# cannot be read (#507) -- correctly, since the local marker is a file the
# graded principal writes. The only producer for that row was the SessionStart
# sweep, so a connected project's seal reached the control plane at the NEXT
# session: Commit, then dispatch, and every unit blocked with "no copy of this
# Cycle's sealed manifest can be read" until a session happened to sweep.
#
# It stayed invisible because nothing ran a CONNECTED SPQ project end to end --
# the e2e SPQ scenarios had no `project_id`, so the authority path never
# activated. Connecting them in CI is what surfaced it.


def test_the_commit_ships_the_seal_it_just_wrote(project, tmp_path, monkeypatch):
    shipped: list = []

    import manifest_emitter

    monkeypatch.setattr(
        manifest_emitter, "emit_cycle_manifest",
        lambda manifest, project_dir=None: shipped.append(manifest) or True,
    )
    _open(project)
    assert len(shipped) == 1, shipped
    assert shipped[0]["cycle_id"] == sm.identity(str(project)).cycle_id
    assert shipped[0]["declaration_hash"], shipped[0]


def test_a_failed_ship_does_not_fail_the_commit(project, tmp_path, monkeypatch):
    """Best-effort, deliberately: git holds the seal and the barrier reads it
    there, so telemetry must never fail a ceremony. What makes an unshipped
    seal visible is the DoD gate's own fail-closed check, not this call."""
    import manifest_emitter

    def _explode(manifest, project_dir=None):
        raise RuntimeError("control plane unreachable")

    monkeypatch.setattr(manifest_emitter, "emit_cycle_manifest", _explode)
    _open(project)
    assert sm.identity(str(project)).cycle_id


# ══ 7. The reservation is scoped by THIS repo's project, not the CLI's guess ═
#
# #738: `open_cycle` used to call `region_registry.reserve` with no `project`
# at all, leaning on the CLI's own cwd-based default (`resolveRegionProject`).
# That default trusted a global session cache stamped for a PREVIOUS, unrelated
# project ahead of the correctly-configured `.synaptory.yaml`, so a pilot
# repository whose own project_id was right still reserved (and would have
# read reservations) under someone else's project. `resolveRegionProject` now
# guards the cache by repo root, mirroring `resolveShipProject` -- but this
# suite is the OTHER half: `open_cycle` should not depend solely on getting
# that CLI-side default right when `.synaptory.yaml` already answers the
# question directly at the call site.


def test_open_cycle_passes_this_repos_project_id_to_the_registry(
    project, tmp_path, monkeypatch
):
    """`.synaptory.yaml` names the real project; the reservation call must be
    scoped to exactly that slug, explicitly, regardless of what a stale
    session cache elsewhere on the machine might claim."""
    (project / ".synaptory.yaml").write_text(
        "build_mode: spq\nproject_id: synap-pilot-tracker\n", encoding="utf-8"
    )
    seen: dict = {}

    def _fake_reserve(_project_dir, **kwargs):
        seen.update(kwargs)
        return {"outcome": registry.RESERVED, "registry": "control-plane",
                 "detail": "ok", "cycle_id": kwargs.get("cycle_id", "")}

    monkeypatch.setattr(registry, "reserve", _fake_reserve)
    _open(project)
    assert seen.get("project") == "synap-pilot-tracker"


def test_open_cycle_with_no_project_id_still_calls_the_registry(
    project, tmp_path, monkeypatch
):
    """No `project_id:` in `.synaptory.yaml` (this fixture's default) -- the
    explicit project resolves to "", the same as never passing the argument,
    so the CLI's own (now repo-guarded) default takes over rather than this
    layer inventing an identity nobody declared."""
    seen: dict = {}

    def _fake_reserve(_project_dir, **kwargs):
        seen.update(kwargs)
        return {"outcome": registry.RESERVED, "registry": "control-plane",
                 "detail": "ok", "cycle_id": kwargs.get("cycle_id", "")}

    monkeypatch.setattr(registry, "reserve", _fake_reserve)
    _open(project)
    assert seen.get("project") == ""


def test_a_compensating_release_uses_the_same_explicit_project_as_the_reserve(
    project, tmp_path, monkeypatch
):
    """A Commit that reserves and then fails to seal releases what it just
    reserved (see `test_a_declaration_that_would_not_seal_leaves_no_reservation`
    for the earlier, pre-reservation refusal). That release call must resolve
    the SAME explicit project as the reserve it is undoing -- a release
    computed from a different implicit resolution would miss the reservation
    entirely and leave it held under the real project forever."""
    (project / ".synaptory.yaml").write_text(
        "build_mode: spq\nproject_id: synap-pilot-tracker\n", encoding="utf-8"
    )
    seen_reserve: dict = {}
    seen_release: dict = {}

    def _fake_reserve(_project_dir, **kwargs):
        seen_reserve.update(kwargs)
        return {"outcome": registry.RESERVED, "registry": "control-plane",
                 "detail": "ok", "cycle_id": kwargs.get("cycle_id", "")}

    def _fake_release(_project_dir, **kwargs):
        seen_release.update(kwargs)
        return {"outcome": "released", "registry": "control-plane"}

    monkeypatch.setattr(registry, "reserve", _fake_reserve)
    monkeypatch.setattr(registry, "release", _fake_release)
    # Force the failure AFTER the reservation and BEFORE the seal completes,
    # so the compensating `except` branch runs.
    monkeypatch.setattr(
        sm._records, "seal", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    with pytest.raises(RuntimeError):
        _open(project)
    assert seen_reserve.get("project") == "synap-pilot-tracker"
    assert seen_release.get("project") == "synap-pilot-tracker"
