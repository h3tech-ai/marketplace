"""Layer 1 — `migrate_to_multispec.py` refuses an SPQ project (#529).

Every other member of the SPQ-blind-reader class is a reporting defect. This
one has **write authority** and is reachable by an operator running a
documented migration script against a project whose lifecycle the script
predates.

What running it did, measured against a Cycle opened by `open_cycle` — and it
is worse than "the pointer is destroyed":

  1. `upgrade_v2_to_v3` wraps the identity pointer under `specs.<primary>.spq`
     and exits 0. The `spq` identity block survives, nested a level deeper, and
     the Cycle directory and sealed manifest are untouched on disk — but no
     Cycle identity resolves from a pointer of that shape, so every SPQ read,
     `next_action`, advance and gate loses the Cycle. The lifecycle is
     inoperable.
  2. While an SPQ-native migrator still existed, the recovery named in that
     refusal completed with rc=0 and `warnings: []` and re-pointed the project
     at a **new, empty Cycle `0-<hash>` at seq 0**, while the real Cycle — two
     admitted Work Units, a sealed manifest, still listed in `spq/index.json` —
     was left unreachable. A zero that means "nothing was recovered", wearing
     the representation of one that means "nothing to recover".

**#640 removed that second step, which makes this refusal load-bearing rather
than merely correct.** `ADR-035` refuses the retired layout by name instead of
converting it, so the SPQ-native migrator is gone and there is no recovery at
all: this script's refusal is now the only thing between an operator and an
unrecoverable project. The tests below therefore assert that the refusal names
the decision and says no recovery exists, where they used to assert it named
the recovery script.

**The migratability decision, stated rather than answered by omission:** an SPQ
project is NOT migratable to Multi-Spec, and there is no `--force`. #303/#304/
#305 moved SPQ off the v3.0 envelope into its own native store; #453 and the
SPQ design treat the two layouts as alternatives rather than layers; and the
output of this migration is a layout nothing reads and nothing will migrate
away from. A migration whose successful output is an unreadable state is not a
migration.

Fixtures come from `spq_board` (`initialize -> approve_baseline -> open_cycle`),
never hand-written — the #509 lesson. The one exception is the un-migrated
v3.0+spq envelope, which is hand-written on purpose because it is precisely the
shape the current lifecycle can no longer produce.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import _spq_fixture

pytestmark = pytest.mark.unit


def _paths_for(repo_root: Path):
    return [str(repo_root / "core" / "lib"), str(repo_root / "core" / "scripts")]


@pytest.fixture
def spq_board(repo_root: Path, monkeypatch):
    """Local override of the conftest factory: `open_cycle` is keyword-only now.

    Same contract, and the board is still produced by the state machine rather
    than hand-written; `_spq_fixture.open_project` carries the #644 declaration
    fields and the baseline `open_cycle` now requires.
    """
    for entry in _paths_for(repo_root):
        if entry not in sys.path:
            monkeypatch.syspath_prepend(entry)
    return _spq_fixture.open_project


def _run(repo_root: Path, project: Path, primary: str = "platform",
         new_specs: str = "") -> subprocess.CompletedProcess:
    script = repo_root / "core" / "scripts" / "migrate_to_multispec.py"
    return subprocess.run(
        [sys.executable, str(script), "--project-dir", str(project),
         "--primary-spec", primary, "--new-specs", new_specs, "--yes"],
        capture_output=True, text=True,
    )


@pytest.fixture
def spq_cycle(tmp_path: Path, spq_board, repo_root: Path, monkeypatch):
    for entry in _paths_for(repo_root):
        if entry not in sys.path:
            monkeypatch.syspath_prepend(entry)
    project, _state = spq_board(
        tmp_path / "spq-cycle",
        units=[{"id": "WU-1", "title": "index writer"},
               {"id": "WU-2", "title": "query api"}],
        goal="ship the search feature",
    )
    return project


def _pointer(project: Path) -> Path:
    return project / ".synaptory" / ".orchestrator" / "pipeline-state.json"


# ── The destroying path ───────────────────────────────────────────────────────


def test_the_migrator_refuses_a_real_spq_cycle(
    spq_cycle: Path, repo_root: Path, monkeypatch
):
    """The whole defect, against a board the lifecycle actually produced.

    Refusal is asserted three ways, because "it printed an error" is not the
    same claim as "it changed nothing": the exit code, the bytes of the
    pointer, and the SPQ machine's own ability to read its board afterwards.
    """
    before = _pointer(spq_cycle).read_bytes()
    proc = _run(repo_root, spq_cycle)

    assert proc.returncode == 2, (
        "the migrator ran against an SPQ project. stdout:\n%s" % proc.stdout)
    assert "REFUSED" in proc.stderr
    assert _pointer(spq_cycle).read_bytes() == before, (
        "the identity pointer was rewritten despite the refusal")

    for entry in _paths_for(repo_root):
        if entry not in sys.path:
            monkeypatch.syspath_prepend(entry)
    import spq_state_machine as spq

    state = spq.read_state(str(spq_cycle))
    assert [u["id"] for u in state["current_stories"]] == ["WU-1", "WU-2"]
    assert state["lifecycle_state"] == "CYCLE"


def test_the_refusal_touches_nothing_on_disk(spq_cycle: Path, repo_root: Path):
    """No spec slots seeded, no `.synaptory.yaml` stub appended, and no
    migration snapshot — a refusal that still leaves a half-migration behind is
    the same trap one step earlier."""
    yaml_before = (spq_cycle / ".synaptory.yaml").read_text(encoding="utf-8")
    _run(repo_root, spq_cycle, new_specs="ehr,billing")

    orch = spq_cycle / ".synaptory" / ".orchestrator"
    assert not (orch / "specs").exists(), "spec slots were seeded anyway"
    assert not (spq_cycle / ".synaptory" / ".migrations").exists(), (
        "a pre-migration snapshot was written for a migration that never ran")
    assert (spq_cycle / ".synaptory.yaml").read_text(encoding="utf-8") == yaml_before


def test_the_refusal_states_its_reason_and_that_there_is_no_recovery(
    spq_cycle: Path, repo_root: Path
):
    """#529 asks for the reason IN the refusal. A bare "refused" leaves the
    operator to guess whether they hit a bug or a decision.

    Was `..._and_the_legal_direction` until #640. It asserted that the refusal
    named `migrate_spq_native.py`, and `ADR-035` deleted that script — so the
    old assertion was pinning a message that sent an operator to run something
    that does not exist. There is no legal direction between the two layouts
    now, and saying so is the more useful half: it tells the operator that
    ignoring this refusal is not recoverable.
    """
    err = _run(repo_root, spq_cycle).stderr
    assert "spq" in err
    assert "alternative" in err.lower() and "not layers" in err.lower()
    assert "ADR-035" in err, (
        "the refusal must name the decision it rests on; got:\n%s" % err)
    assert "no recovery script" in err.lower(), (
        "the refusal must say that no recovery exists, or an operator reads it "
        "as an inconvenience rather than a one-way door; got:\n%s" % err)
    assert "migrate_spq_native" not in err, (
        "the refusal still points at a script ADR-035 deleted; got:\n%s" % err)
    assert "no --force" in err.lower()


def test_a_pre_commit_spq_project_is_refused_too(
    tmp_path: Path, repo_root: Path, monkeypatch
):
    """A project at DISCOVERY has no Cycle yet, so `read_board` reports
    `hydrated=False` with an empty board. Keying the refusal on emptiness
    rather than on `build_mode` would let exactly this project through — and it
    is an SPQ project, whose first `open_cycle` would then land on a v3
    envelope."""
    for entry in _paths_for(repo_root):
        if entry not in sys.path:
            monkeypatch.syspath_prepend(entry)
    import spq_state_machine as spq

    project = tmp_path / "pre-commit"
    project.mkdir()
    (project / ".synaptory.yaml").write_text('build_mode: "spq"\n', encoding="utf-8")
    spq.initialize(str(project))

    proc = _run(repo_root, project)
    assert proc.returncode == 2, proc.stdout
    assert spq.read_state(str(project))["lifecycle_state"] == "DISCOVERY"


def test_an_unmigrated_v3_spq_envelope_is_refused_not_called_a_no_op(
    tmp_path: Path, repo_root: Path
):
    """The ordering that matters. An SPQ project already wrapped into v3.0
    satisfies `spec_state.is_multispec`, so the pre-existing
    `_is_already_multispec` branch answered rc=0 "nothing to do" — a success
    message for the exact corrupted state the refusal exists to name. The SPQ
    check therefore runs first.

    Hand-written on purpose: this is the shape the lifecycle stopped producing
    at #303, so `open_cycle` cannot make one.

    The "could not be read" half is the one this migration nearly lost twice.
    It held because `spq_state_machine.read_state` refused such a pointer; the
    #644 machine returns the default board instead, so the refusal moved into
    `pipeline_board` (#640) — which is where the accessor's whole contract, that
    unread and empty are different answers, already lives.
    """
    project = tmp_path / "unmigrated"
    orch = project / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    (project / ".synaptory.yaml").write_text('build_mode: "spq"\n', encoding="utf-8")
    (orch / "pipeline-state.json").write_text(json.dumps({
        "version": "3.0", "build_mode": "spq", "active_spec": "platform",
        "specs": {"platform": {"lifecycle_state": "CYCLE_EXECUTION",
                               "current_cycle": 1, "current_stories": []}},
    }), encoding="utf-8")

    proc = _run(repo_root, project)
    assert proc.returncode == 2, proc.stdout
    assert "already multi-spec" not in proc.stdout
    assert "ADR-035" in proc.stderr
    assert "could not be read" in proc.stderr, (
        "an unreadable SPQ board must be reported alongside the refusal, not "
        "swallowed; got:\n%s" % proc.stderr)


# ── The counter-cases: the migrator still does its actual job ─────────────────


def test_a_scrum_project_still_migrates(tmp_path: Path, repo_root: Path):
    """Regression guard. **Passes against `origin/dev` too** — it exists so the
    refusal cannot be over-broad and break the migration this script is for."""
    project = tmp_path / "scrum"
    orch = project / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    (project / ".synaptory.yaml").write_text(
        "build_mode: scrum\ntracker:\n  backend: local\n", encoding="utf-8")
    (orch / "pipeline-state.json").write_text(json.dumps({
        "version": "2.0", "build_mode": "scrum",
        "lifecycle_state": "SPRINT_EXECUTION", "current_sprint": 3,
        "current_stories": [], "lifecycle_history": [],
    }), encoding="utf-8")

    proc = _run(repo_root, project, primary="platform")
    assert proc.returncode == 0, proc.stderr
    full = json.loads(_pointer(project).read_text(encoding="utf-8"))
    assert full["version"] == "3.0"
    assert full["specs"]["platform"]["current_sprint"] == 3


def test_a_fresh_project_with_no_state_still_migrates(
    tmp_path: Path, repo_root: Path
):
    """The second counter-case. **Passes against `origin/dev` too.**

    `read_board` reports `build_mode` scrum by documented default when there is
    no state file at all, so the refusal must not fire on a greenfield project
    the script is explicitly designed to initialise.
    """
    project = tmp_path / "fresh"
    project.mkdir()
    (project / ".synaptory.yaml").write_text(
        "build_mode: scrum\ntracker:\n  backend: local\n", encoding="utf-8")

    proc = _run(repo_root, project, primary="venue-agent")
    assert proc.returncode == 0, proc.stderr
    full = json.loads(_pointer(project).read_text(encoding="utf-8"))
    assert set(full["specs"]) == {"venue-agent"}


# ── the config is a second authority, and a damaged pointer is not permission ──


def _spq_project_without_a_pointer(tmp_path: Path) -> Path:
    """An SPQ project by configuration, with its native Cycle store and no
    `pipeline-state.json`. This is the damaged shape, not an unusual one: the
    pointer is the file most likely to be lost, and it is the only one the
    refusal used to consult."""
    project = tmp_path / "spq-no-pointer"
    (project / ".synaptory" / ".orchestrator" / "spq" / "cycles" / "1-abc").mkdir(
        parents=True
    )
    (project / ".synaptory.yaml").write_text("build_mode: spq\n", encoding="utf-8")
    (
        project / ".synaptory" / ".orchestrator" / "spq" / "cycles" / "1-abc"
        / "manifest.json"
    ).write_text(json.dumps({"cycle_id": "1-abc", "work_units": []}), encoding="utf-8")
    return project


def test_a_missing_pointer_is_not_evidence_that_a_project_is_not_spq(
    tmp_path: Path, repo_root: Path
):
    """THE DESTRUCTIVE PATH, reached through a damaged pointer (#396).

    `read_board().build_mode` DEFAULTS to scrum when there is no pointer to
    read it from, and the refusal consulted only that. So an SPQ project whose
    pointer went missing was classified non-SPQ: the run returned 0, wrote a
    Scrum Multi-Spec state with an empty primary slot, and left the native
    Cycle orphaned beside it. That is the outcome this refusal exists to
    prevent, produced by the refusal's own blind spot.
    """
    project = _spq_project_without_a_pointer(tmp_path)
    before = sorted(p.name for p in (project / ".synaptory" / ".orchestrator").iterdir())

    proc = _run(repo_root, project)

    assert proc.returncode == 2, (
        "an SPQ project with a damaged pointer was migrated: rc=%s\n%s"
        % (proc.returncode, proc.stdout + proc.stderr)
    )
    assert "REFUSED" in (proc.stderr + proc.stdout)
    assert not _pointer(project).exists(), "the migration wrote a board anyway"
    assert sorted(
        p.name for p in (project / ".synaptory" / ".orchestrator").iterdir()
    ) == before, "the refusal changed the project"


def test_the_refusal_says_why_the_board_did_not_show_it(
    tmp_path: Path, repo_root: Path
):
    """An operator hitting this needs to know the pointer is the problem, not
    the configuration: the two authorities disagree, and the message says which
    said what."""
    project = _spq_project_without_a_pointer(tmp_path)
    out = _run(repo_root, project).stderr + _run(repo_root, project).stdout
    assert "build_mode: spq" in out, out[-600:]
    assert "defaults to scrum" in out, out[-600:]


def test_a_genuinely_scrum_project_is_still_migrated(tmp_path: Path, repo_root: Path):
    """The positive arm, so a second authority did not become a blanket
    refusal. A project that says scrum and has no SPQ store migrates."""
    project = tmp_path / "scrum"
    orch = project / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    (project / ".synaptory.yaml").write_text("build_mode: scrum\n", encoding="utf-8")
    (orch / "pipeline-state.json").write_text(
        json.dumps({"version": "2.0", "build_mode": "scrum",
                    "lifecycle_state": "SPRINT_EXECUTION"}),
        encoding="utf-8",
    )
    proc = _run(repo_root, project)
    assert proc.returncode == 0, proc.stdout + proc.stderr
