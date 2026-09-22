"""Layer 1 -- two open Cycles cannot both claim one shared path (#827).

`region_registry.reserve` claims a REGION, and it works. A shared path is by
definition OUTSIDE every region, so it claims nothing about one.
`_eval_shared_paths_owned` enforces exactly one owner per shared path over the
effective set -- within ONE declaration. Nothing compared two.

So two concurrent Cycles could each claim `web/src/main.tsx` (both being UI
surfaces, neither region containing the entry point that registers one), each
pass its own barrier, and both seal. Whoever merged second would overwrite the
first, and the first would discover it at a barrier where the declaration can
no longer be changed. On the reporting engagement it was one step from
happening and was caught only because a coordinating session read both sealed
manifests by hand.

WHAT THIS IS AND IS NOT. It is that hand comparison, automated at the last
moment a declaration can still change. It sees declarations COMMITTED into
this checkout. A Cycle sealed on a machine whose branch has not been fetched
is invisible to it, and no local read can change that -- the registry is the
only party that sees both in time. That is why the scope is sealed into the
declaration: the failure this product keeps finding is a guarantee credited to
a mechanism that was not providing it, and a partial check whose coverage is
not recorded is how that happens next.
"""

from __future__ import annotations

import pytest

import cycle_records as records
import spq_paths
import spq_state_machine as sm

from _spq_fixture import CYCLE_KWARGS, unit as _unit


pytestmark = pytest.mark.unit

ENTRY = "web/src/main.tsx"


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".synaptory.yaml").write_text("build_mode: spq\n", encoding="utf-8")
    monkeypatch.delenv("SYNAPTORY_ACTIVE_SPEC", raising=False)
    sm.initialize(str(root))
    sm.approve_baseline(
        str(root), approved_by="lead@h3t.co", baseline_ref="baseline-1",
        calibration={"sample_units": 2, "measured_hours": 8},
    )
    return root


def _open(project, *, unit_id, goal, region, shared=ENTRY, **over):
    kwargs = dict(CYCLE_KWARGS)
    kwargs["source_region"] = [region]
    kwargs.update(over)
    # The owning unit must scope the path it owns, and the path lies outside
    # the source region -- which is what makes it shared and is exactly why
    # the region reservation does not cover it.
    scope = [region] + ([shared] if shared else [])
    return sm.open_cycle(
        str(project), goal=goal,
        admitted_units=[_unit(unit_id, path_scope=scope)],
        shared_path_owners=(
            [{"path": shared, "owning_unit_id": unit_id}] if shared else []
        ),
        **kwargs,
    )


class TestASecondCycleCannotTakeAHeldSharedPath:
    def test_the_collision_is_refused_at_commit(self, project):
        _open(project, unit_id="WU-307", goal="surface one", region="web/a/")

        with pytest.raises(ValueError) as excinfo:
            _open(project, unit_id="WU-606", goal="surface two", region="web/b/")

        message = str(excinfo.value)
        assert ENTRY in message
        assert "WU-307" in message          # who already holds it
        assert "WU-606" in message          # who is asking for it
        assert "#827" in message
        # The consequence, because "collision" alone does not convey that it
        # is unrecoverable once both have sealed.
        assert "overwrite the first" in message
        # And three ways out, since the operator is mid-Commit.
        assert "withdraw the unit" in message
        assert "Checkpoint" in message

    def test_the_refusal_leaves_no_cycle_and_no_reservation_behind(self, project):
        first = _open(project, unit_id="WU-307", goal="surface one", region="web/a/")

        with pytest.raises(ValueError):
            _open(project, unit_id="WU-606", goal="surface two", region="web/b/")

        # Checked before the reservation, for the same reason the declaration
        # is validated before it: a refused Commit that held a region would
        # have the next Commit refused for a collision with nothing.
        assert sm.identity(str(project)).cycle_id == first["cycle_id"]

    def test_disjoint_shared_paths_are_untouched(self, project):
        _open(project, unit_id="WU-307", goal="surface one", region="web/a/")

        second = _open(
            project, unit_id="WU-606", goal="surface two", region="web/b/",
            shared="web/src/router.tsx",
        )

        assert second["ok"] is True

    def test_a_closed_cycle_holds_nothing(self, project):
        """Its work is on the trunk and its claim is spent. Skipping it is not
        leniency: without it, every path any Cycle ever shared would be
        permanently unclaimable by every later Cycle."""
        first = _open(project, unit_id="WU-307", goal="surface one", region="web/a/")
        held = sm.live_shared_path_claims(str(project))
        assert ENTRY in held

        _close_record(project, first["cycle_id"])

        assert ENTRY not in sm.live_shared_path_claims(str(project))
        assert _open(
            project, unit_id="WU-606", goal="surface two", region="web/b/"
        )["ok"] is True

    def test_a_cycle_does_not_collide_with_itself(self, project):
        """`exclude_cycle_id` is the whole of it. Without it the allocated id
        is compared against its own committed declaration on re-entry."""
        first = _open(project, unit_id="WU-307", goal="surface one", region="web/a/")

        claims = sm.live_shared_path_claims(
            str(project), exclude_cycle_id=first["cycle_id"]
        )

        assert claims == {}


class TestTheSealRecordsWhatWasCompared:
    def test_the_scope_is_sealed_so_silence_is_not_read_as_proof(self, project):
        """A check that is partial by construction must say so where an
        auditor reads it. `registry: none` is the load-bearing field: it says
        a clone's committed declarations were compared and a registry was
        not."""
        first = _open(project, unit_id="WU-307", goal="surface one", region="web/a/")

        sealed = sm.read_manifest(str(project), first["cycle_id"])
        check = sealed["shared_path_check"]

        assert check["scope"] == "local-committed-declarations"
        assert check["registry"] == "none"
        assert check["paths_claimed"] == [ENTRY]
        assert check["cycles_compared"] == []

    def test_it_names_the_cycles_it_actually_compared(self, project):
        first = _open(project, unit_id="WU-307", goal="surface one", region="web/a/")
        second = _open(
            project, unit_id="WU-606", goal="surface two", region="web/b/",
            shared="web/src/router.tsx",
        )

        sealed = sm.read_manifest(str(project), second["cycle_id"])

        assert sealed["shared_path_check"]["cycles_compared"] == [first["cycle_id"]]

    def test_the_scope_is_inside_the_hash(self, project):
        """Like `region_reservation` beside it. A record of how separation was
        established that a caller could edit after the fact establishes
        nothing."""
        first = _open(project, unit_id="WU-307", goal="surface one", region="web/a/")
        sealed = sm.read_manifest(str(project), first["cycle_id"])

        assert records.verify_hash(sealed)
        tampered = dict(sealed)
        tampered["shared_path_check"] = dict(
            sealed["shared_path_check"], registry="control-plane"
        )
        assert not records.verify_hash(tampered)


class TestTheReaderIsHonestAboutWhatItCanSee:
    def test_a_project_with_no_committed_cycles_claims_nothing(self, tmp_path):
        assert sm.live_shared_path_claims(str(tmp_path)) == {}

    def test_a_stray_directory_is_not_a_cycle(self, project):
        _open(project, unit_id="WU-307", goal="surface one", region="web/a/")
        stray = (
            project / spq_paths.COMMITTED_RELDIR / "not-a-cycle-id"
        )
        stray.mkdir(parents=True, exist_ok=True)
        (stray / "manifest.json").write_text("{}", encoding="utf-8")

        claims = sm.live_shared_path_claims(str(project))

        assert list(claims) == [ENTRY]


def _close_record(project, cycle_id: str) -> None:
    """The one fact the reader uses to decide a Cycle is spent."""
    import json
    path = spq_paths.committed_barrier_path(str(project), cycle_id)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump([{"kind": "close", "cycle_id": cycle_id}], handle)
