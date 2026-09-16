"""Layer 1 -- behaviour the SPQ rewrite carried out and nothing carried back.

ONE FAILURE CLASS, three instances. Replacing a 3,765-line state machine with
an 887-line one is mostly deletion, and deletion is safe when the thing deleted
had a test. These three did not:

    * an SPQ project's receipts directory fell through to a Multi-Spec spec
      slot, because the branch that forbade the fall-through was reading an
      attribute the retired lane used to carry;
    * the dispatch contract lost `#487`'s compliance declaration and `#501`'s
      per-story DoD block, because the new `next_action` forwarded fewer
      arguments than the old one and no test named any of them;
    * two MCP tools called functions with signatures that no longer existed.

None of the three is a state machine behaviour, which is why none was noticed
while the state machine's own tests went green. Each was found by a fixture
migration whose subject was something else entirely -- so the tests here exist
to make the NEXT such removal fail loudly, rather than to re-fix these three.

WHY THEY LIVE TOGETHER. They are not related by module; they are related by
being wiring. A caller's argument list, a resolver's branch order and an
adapter's call signature are all places where a behaviour can be complete on
both sides and connected on neither, and the shipped path is the only place
that shows it. Grouping them keeps that shape visible.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import spq_paths as sp
import spq_state_machine as sm
import story_pipeline as story

from _spq_fixture import CYCLE_KWARGS, unit as _fx_unit


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )


def _project(tmp_path: Path, monkeypatch, *, config: str = "build_mode: spq\n") -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "t@e.co")
    _git(project, "config", "user.name", "t")
    (project / "f.txt").write_text("x", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")
    (project / ".synaptory.yaml").write_text(config, encoding="utf-8")
    monkeypatch.delenv("SYNAPTORY_ACTIVE_SPEC", raising=False)
    return project


def _open_cycle(project: Path, **over) -> str:
    sm.initialize(str(project))
    sm.approve_baseline(
        str(project),
        approved_by="t",
        baseline_ref="baseline-1",
        calibration={"sample_units": 1, "measured_hours": 1},
    )
    kwargs = dict(CYCLE_KWARGS)
    kwargs.update(over)
    sm.open_cycle(
        str(project), goal="c1", admitted_units=[_fx_unit("WU-001")], **kwargs
    )
    return sm.identity(str(project)).cycle_id


# ══ 1. An SPQ project never resolves receipts through `active_spec` ══════════
#
# `receipts_dir_for`'s own docstring said so, and the code that enforced it
# read `ident.workstream_id`. `Identity` stopped carrying that attribute when
# the lane was retired, the `AttributeError` hit a bare `except` whose comment
# called the fall-through intentional, and every SPQ project reached the branch
# below it -- the one that resolves a directory from `SYNAPTORY_ACTIVE_SPEC`.
# The guard against the fall-through had become the fall-through.


def test_an_spq_cycles_receipts_live_under_the_cycle(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    cycle_id = _open_cycle(project)
    resolved = story.receipts_dir_for(str(project), intended=True)
    assert resolved == sp.receipts_dir(str(project), cycle_id)
    assert "specs" not in resolved


def test_an_exported_active_spec_cannot_move_an_spq_receipt(tmp_path, monkeypatch):
    """The variable is Scrum/Kanban's and SPQ ignores it. A leftover export
    from another project in the same shell is the realistic way this happens,
    and it must not silently redirect evidence."""
    project = _project(tmp_path, monkeypatch)
    cycle_id = _open_cycle(project)
    monkeypatch.setenv("SYNAPTORY_ACTIVE_SPEC", "some-other-spec")
    resolved = story.receipts_dir_for(str(project), intended=True)
    assert resolved == sp.receipts_dir(str(project), cycle_id)
    assert "some-other-spec" not in resolved


def test_an_spq_project_with_no_cycle_gets_the_flat_directory(tmp_path, monkeypatch):
    """DISCOVERY, or a clone that has not hydrated. A legitimate state with a
    legitimate answer -- and the answer is NOT a spec slot. An unresolvable
    identity is not permission to consult `active_spec`."""
    project = _project(tmp_path, monkeypatch)
    sm.initialize(str(project))
    monkeypatch.setenv("SYNAPTORY_ACTIVE_SPEC", "some-other-spec")
    resolved = story.receipts_dir_for(str(project), intended=True)
    assert resolved.endswith("/.orchestrator/receipts")
    assert "some-other-spec" not in resolved


def test_scrum_still_resolves_its_spec_slot(tmp_path, monkeypatch):
    """The SPQ branch returns rather than falling through, so it must not
    swallow the lifecycle it was never about. #303/#304 scope the decoupling
    to SPQ; a Scrum project running several label-filtered boards keeps its
    slot."""
    project = _project(tmp_path, monkeypatch, config="build_mode: scrum\n")
    monkeypatch.setenv("SYNAPTORY_ACTIVE_SPEC", "board-a")
    resolved = story.receipts_dir_for(str(project), intended=True)
    assert resolved.endswith("/specs/board-a/receipts")


# ══ 2. The dispatch contract still declares what the gate will enforce ══════


def test_the_spq_dispatch_declares_the_compliance_obligation(tmp_path, monkeypatch):
    """#487. SPQ is the ONLY lifecycle that passes `compliance` into
    `story_pipeline.next_action` -- `_attach_compliance_declaration` no-ops
    without it and Scrum/Kanban keep their compliance-engineer dispatch -- so
    dropping the argument reverted #487 for the one lifecycle it was written
    for, with every test still green on both sides of the missing wire."""
    project = _project(
        tmp_path,
        monkeypatch,
        config="build_mode: spq\nhealthcare:\n  baa_enforced: true\n",
    )
    _open_cycle(project)
    action = sm.next_action(str(project))
    compliance = (action.get("dod") or {}).get("compliance")
    assert compliance, (
        "the dispatch carries no compliance declaration, so the verifying "
        "stage learns of the obligation at the gate -- too late to have "
        "produced evidence for it"
    )
    assert compliance["baa_enforced"] is True
    # `cr`, not `ce`: SPQ dispatches no compliance-engineer on the Work Unit
    # path, so the obligation lands on the stage that already owns the
    # `reviewing -> done` edge.
    assert compliance["accountable_role"] == "cr"
    assert compliance["checks"]


def test_the_spq_dispatch_carries_the_per_story_dod_contract(tmp_path, monkeypatch):
    """#501. Without `attach_dispatch_dod_contract` the `dod` block is the
    STATIC tier list, so a dispatch is told the tier's checks rather than the
    ones its own unit will be gated on."""
    project = _project(tmp_path, monkeypatch)
    _open_cycle(project)
    action = sm.next_action(str(project))
    dod = action.get("dod") or {}
    # `verification_tier` is one of `_DISPATCH_DOD_KEYS` and reaches the block
    # ONLY through the per-story resolution, so its presence is the marker
    # that the upgrade ran. `attach_dispatch_dod_contract` is best-effort and
    # swallows its own failures in-place, which is right for a dispatch and
    # means a silent regression looks exactly like the tier-only shape.
    assert "verification_tier" in dod, (
        "the `dod` block is the STATIC tier list, so the dispatch is told the "
        "tier's checks rather than the ones its own unit will be gated on"
    )
    assert "undetermined_checks" in dod


def test_the_spq_dispatch_passes_no_parallelism_switch(tmp_path, monkeypatch):
    """The absence is the design, so it is asserted rather than left to a
    reader. `parallelism` fed an unbarriered parallel batch inside a lane --
    the second of the three units of parallel execution `C-01` reduces to one.
    Concurrency is a consequence of declared path scope (`C-07`); a switch
    here would reintroduce what `path_scope` replaced."""
    project = _project(tmp_path, monkeypatch)
    _open_cycle(project)
    action = sm.next_action(str(project))
    assert "parallel_batch" not in action
    assert "parallelism" not in action


# ══ 3. The MCP adapter calls functions that exist ═══════════════════════════
#
# Both tools below raised `AttributeError` / `TypeError` on every invocation:
# `_get_cycle` read three fields off an `Identity` that carries none of them,
# and `_hydrate` called `hydrate_cycle(project, seq, [], workstream_id=...)`
# against a signature that is now `(project_dir, *, cycle_id)`. An adapter is
# the one place a unit test of either side cannot reach.


def test_get_cycle_answers_for_an_open_cycle(tmp_path, monkeypatch):
    import spq_mcp

    project = _project(tmp_path, monkeypatch)
    cycle_id = _open_cycle(project)
    out = spq_mcp._get_cycle({"project_dir": str(project)})
    assert out["ok"] is True
    assert out["cycle_id"] == cycle_id
    assert out["manifest_valid"] is True
    assert out["admitted"] == ["WU-001"]
    # The retired lane's fields are gone from the surface, not renamed.
    assert "workstream_id" not in out
    assert "owners" not in out


def test_get_cycle_reports_shared_path_ownership_not_unit_ownership(
    tmp_path, monkeypatch
):
    """`spq_manifest.owners` answered unit -> LANE. With the lane gone a unit
    has no owner, and the ownership the method declares is of a shared PATH
    that more than one unit would otherwise touch (`C-04`'s
    `shared_paths_owned`)."""
    import spq_mcp

    project = _project(tmp_path, monkeypatch)
    # Inside WU-001's own declared scope: the declaration refuses an owner
    # that does not cover the path it claims, which is the right refusal and
    # makes the fixture's scope load-bearing.
    _open_cycle(
        project,
        shared_path_owners=[
            {"path": "api/wu-001/shared.py", "owning_unit_id": "WU-001"}
        ],
    )
    out = spq_mcp._get_cycle({"project_dir": str(project)})
    assert out["shared_path_owners"] == {"api/wu-001/shared.py": "WU-001"}


def test_hydrate_adopts_the_cycle_by_its_id(tmp_path, monkeypatch):
    import spq_mcp

    project = _project(tmp_path, monkeypatch)
    cycle_id = _open_cycle(project)
    out = spq_mcp.hydrate_cycle({"project_dir": str(project), "cycle_id": cycle_id})
    assert out.get("ok") is True, out
    assert out["work_units"] == ["WU-001"]
