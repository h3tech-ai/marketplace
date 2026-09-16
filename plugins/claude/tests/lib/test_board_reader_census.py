"""Layer 1 — the class-closing guard for "reader is blind to the SPQ board" (#514).

Four separate readers acquired the same defect and were found four separate
times by four separate pieces of work:

    1. `conformance/drivers/hosts.py::read_story`
    2. `benchmarks/parallelism/pilot_metrics.py`
    3. `core/lib/signals.py::harvest_pipeline_signals`
    4. `plugin-claude/hooks/synaptory-session-start.sh`

Each opened `.synaptory/.orchestrator/pipeline-state.json` and expected a
scrum-shaped board. Under `build_mode: spq` that file is a mode + identity
POINTER (`spq_state_machine._write_state` strips it at `open_cycle`, on
purpose) and the board lives at `spq/cycles/<cycle-id>/execution-state.json`,
so each read **succeeded against the wrong file** and reported emptiness as a
measurement.

This file exists to make a fifth instance impossible to add QUIETLY. It does
two things a single accessor cannot do on its own, because nothing forces a new
file to call an accessor — `json.load` is always available:

    census      Every non-generated file that reads the board must be
                CLASSIFIED here. An unregistered one fails, with the class
                explained and `pipeline_board.read_board` named. This is the
                half that catches a reader nobody thought to test.

    probes      Each registered probe runs its consumer against a board
                produced by `spq_state_machine.open_cycle` and asserts what it
                sees. `AWARE` probes must see the units. `BLIND` probes must
                still NOT see them — the conformance `declared_gaps` pattern,
                so a declaration cannot rot in either direction: fix a blind
                reader and this test tells you to delete its declaration.

What this guard cannot do, stated plainly:

  * `MIXED` entries (a module where one entry point resolves SPQ and another
    does not) are recorded, not verified. Statically distinguishing "this
    function is blind" inside a module that also contains a correct resolver is
    not something a grep can do honestly.
  * A reader written in a language or a directory outside `SCAN_ROOTS` is not
    seen at all.
  * A reader that gets the board handed to it by a blind caller is invisible
    here; the caller is the registered one.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import _spq_fixture

pytestmark = pytest.mark.unit


# ── The scan ──────────────────────────────────────────────────────────────────

#: Where non-generated board readers live. `plugin-cursor/hooks/lib`,
#: `plugin-cursor/skills/_shared/scripts`, `plugin-codex/.../runtime` and
#: `web/dist` are excluded because they are COMPOSED COPIES of `core/` — a
#: finding there is a finding in `core/` reported twice.
SCAN_ROOTS = (
    "core/lib",
    "core/scripts",
    "plugin-claude/hooks",
    "plugin-cursor/mcp",
    "plugin-codex/plugins/synaptory/hooks",
    "plugin-codex/plugins/synaptory/scripts",
    "benchmarks/parallelism",
    "conformance",
    "e2e/helpers",
    "infra/scripts",
    "cli/internal",
)

SCAN_EXTENSIONS = (".py", ".sh", ".go")

#: `lib` and `runtime` are the composed-copy directory names; `tests` and
#: `fixtures` hold assertions rather than consumers.
SKIP_DIRS = {"__pycache__", "tests", "fixtures", "node_modules", "runtime", "lib"}

#: Two triggers, because one is not enough. `current_stories` catches a board
#: read; `pipeline-state.json` catches a reader that takes `lifecycle_state` or
#: `current_sprint` off the pointer instead — which is how `update_claude_md.py`
#: and `cli/internal/specstate` acquired the same defect without ever naming
#: `current_stories`.
TRIGGERS = ("current_stories", "pipeline-state.json")


def _scan(repo_root: Path) -> set:
    found = set()
    for root in SCAN_ROOTS:
        base = repo_root / root
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(str(base), followlinks=False):
            dirnames[:] = [
                d for d in dirnames
                if d not in SKIP_DIRS
                and not os.path.islink(os.path.join(dirpath, d))
            ]
            for name in filenames:
                if not name.endswith(SCAN_EXTENSIONS):
                    continue
                if name.startswith("test_") or name.endswith("_test.go"):
                    continue
                path = Path(dirpath) / name
                if path.is_symlink():
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:  # pragma: no cover - unreadable file
                    continue
                if any(t in text for t in TRIGGERS):
                    found.add(path.relative_to(repo_root).as_posix())
    return found


# ── The registry ──────────────────────────────────────────────────────────────
#
# OWNS   defines or writes a layout. A board read here IS the definition, so
#        there is nothing to route through an accessor.
# AWARE  a consumer that resolves the board correctly on every lifecycle.
# BLIND  a consumer that is known to read a scrum-shaped board and see nothing
#        on SPQ. Each carries the ticket-worthy consequence, so the debt is
#        recorded rather than rediscovered a fifth time. NOT fixed here: #514's
#        lane is signals.py, the session-start hook and the accessor.
# MIXED  one module, both behaviours: a correct resolver next to a blind entry
#        point. Recorded with the blind symbol named.
# PATH   names `pipeline-state.json` but reads no board off it (a write guard,
#        a watch matcher, an `active_spec` lookup, an existence check).
# FIXTURE writes a board for tests. The #509 lesson lives here: a fixture that
#        writes a shape the lifecycle never produces cannot reach this defect.

OWNS = {
    "core/lib/pipeline_board.py": "the accessor itself",
    "core/lib/scrum_state_machine.py": "writes the scrum board",
    "core/lib/kanban_state_machine.py": "writes the kanban board",
    "core/lib/spq_state_machine.py": "writes the SPQ board; owns the SPQ layout",
    "core/lib/story_pipeline.py": "writes story sub-state; _read_state routes all three",
    "core/lib/spec_state.py": "the v2/v3 primitive; returns the pointer by design",
    "core/lib/state_store.py": "atomic JSON + lock primitives",
    "core/lib/spq_paths.py": "the SPQ path resolver",
    "core/scripts/migrate_to_multispec.py": (
        "writes the v3 envelope, and REFUSES an SPQ project through "
        "pipeline_board.read_board (#529). SPQ and Multi-Spec are alternative "
        "layouts, not layers -- and there is no longer a legal direction "
        "between them at all: `ADR-035` deleted the SPQ-native migrator and "
        "refuses the old layout by name (#640), which makes this refusal the "
        "only thing between an operator and an unrecoverable project. Pinned "
        "by test_multispec_migrator_refuses_spq.py"
    ),
}

AWARE = {
    "core/lib/signals.py": "reads through pipeline_board.read_board (#514)",
    "conformance/drivers/base.py": (
        "the SPQ declaration probes (#640). Cycle identity comes from "
        "pipeline_board.read_board, never from opening the pointer -- this "
        "census caught them on the commit that introduced them. The C-16 "
        "measurement probe reads the recorded per-Cycle history through "
        "Board.completed_cycles and rates it with "
        "cycle_measurement.history_report (#646). The limit previously "
        "recorded here -- `Board` exposed no completed-Cycle history, so the "
        "probe reported `available: False` with that gap as a stated problem "
        "rather than taking the forbidden direct read -- is closed by that "
        "accessor, and the probe still keeps absent distinct from zero"
    ),
    "core/scripts/verify_all.py": (
        "verify_state_machine and verify_story_receipts read through "
        "pipeline_board.read_board, and a board they cannot read reports "
        "`unverifiable` rather than `passed` (#529). Probed below and pinned by "
        "test_verifier_cannot_certify_unread.py"
    ),
    "core/lib/state_drift.py": "branches on build_mode == spq and validates the pointer",
    "core/lib/spq_mcp.py": "the SPQ MCP surface; reads spq_state_machine.read_state",
    "core/lib/dispatched_receipts.py": "story_pipeline._read_state + spq receipt homes",
    "core/lib/loop_engine.py": "dispatches to spq_state_machine",
    "core/lib/advance_kernel.py": "the single writer; knows every mode",
    "benchmarks/parallelism/pilot_metrics.py": (
        "fixed by #509; mirrors spq_paths.execution_state_path deliberately "
        "rather than importing it, so a benchmark stays standalone"
    ),
    "e2e/helpers/pipeline.py": "spq_state() shells spq_state_machine read",
    "infra/scripts/multi-runtime-scaffold.py": "shells spq_state_machine.py read",
    "cli/internal/cli/telemetry.go": "reads state[\"spq\"].cycle_id, falls back to index.json",
    "plugin-claude/hooks/synaptory-session-start.sh": (
        "renders through pipeline_board.py restore (#514)"
    ),
}

BLIND = {
    "core/scripts/build_summary.py": (
        "assemble() loads the pointer, so PIPELINE.md / PIPELINE.html / "
        "`/synaptory status` render lifecycle_state=UNKNOWN, every Kanban stage "
        "pending, and sprint=null on a live Cycle"
    ),
    "core/scripts/summary/pipeline.py": (
        "build_pipeline()/build_dod_summary()/build_sprint_state() are handed the "
        "pointer by build_summary.py; build_mode 'spq' falls through to "
        "KANBAN_STATES"
    ),
    "core/lib/update_claude_md.py": (
        "the CLAUDE.md sentinel — whose entire purpose is post-compaction "
        "pipeline awareness — anchors to lifecycle_state='' / current_sprint=0. "
        "Written by synaptory-reanchor.sh (PreCompact) and session-end"
    ),
    "benchmarks/parallelism/measure.py": (
        "the SPQ-cutover baseline benchmark (§19.4) reports work_units 0/0 and "
        "observed concurrency 0 on a Cycle with admitted units. Its sibling "
        "pilot_metrics.py was fixed by #509; this one was not"
    ),
    "plugin-claude/hooks/synaptory-pipeline-snapshot.sh": (
        "the cross-session resume snapshot records 'No sprint loop active'"
    ),
    "cli/internal/specstate/specstate.go": (
        "raw.LifecycleState is '' on an SPQ pointer so Mode resolves to "
        "ModeNone; `synaptory specs list` then hard-errors with 'not inside a "
        "synaptory project (no pipeline-state.json found)' for a project whose "
        "file exists. The only reader in the set that fails LOUDLY, and it "
        "fails with a false statement"
    ),
    "cli/internal/cli/specs.go": "consumes specstate.Mode; inherits the above",
    "plugin-codex/plugins/synaptory/hooks/codex_hook.py": (
        "_select_state returns the pointer, so the Codex SessionStart banner "
        "says 'Lifecycle: unknown' and _active_receipts finds zero dispatches"
    ),
}

MIXED = {
    "core/lib/state_machine.py": (
        "read_state() returns the raw pointer",
        ["def read_state("],
    ),
    "plugin-cursor/mcp/server.py": (
        "_lifecycle/spq_next resolve SPQ; tool_get_status and tool_get_state go "
        "through state_machine.read_state and report lifecycle_state null / 0 "
        "stories. An agent calling both sees contradictory state",
        ["def tool_get_status(", "def tool_get_state("],
    ),
    "plugin-codex/plugins/synaptory/scripts/mcp_server.py": (
        "_flat_state/_next_action dispatch to spq_state_machine correctly; "
        "tool_get_status is blind",
        ["def tool_get_status("],
    ),
}

PATH = {
    "core/lib/gate_emitter.py": "docstring reference",
    "core/lib/receipt_recovery.py": "docstring reference",
    "core/scripts/tracker/__init__.py": "active_spec fallback only",
    "plugin-claude/hooks/_plugin-env.sh": "active_spec resolution only",
    "plugin-claude/hooks/synaptory-loop-continue.sh": "active_spec resolution only",
    "plugin-claude/hooks/synaptory-verify-receipt.sh": "active_spec resolution only",
    "plugin-claude/hooks/synaptory-boundary-guard.sh": "blocks direct writes to it",
    "plugin-claude/hooks/synaptory-watch-drift.sh": "watch matcher + message",
    "plugin-codex/plugins/synaptory/scripts/project_bootstrap.py": "existence check",
}

FIXTURE = {
    "conformance/factories.py": (
        "make_spq_state() writes lifecycle_state / current_cycle / "
        "current_stories INTO pipeline-state.json — the pre-#303 shape the state "
        "machine stopped producing. A fixture that puts the board where a blind "
        "reader already looks cannot detect a blind reader, which is why the "
        "cross-host suite never surfaced any of the four instances"
    ),
    "conformance/drivers/hosts.py": (
        "read_story was fixed by #505, but seed_stories still writes the board "
        "to pipeline-state.json, so the fixture half of the same defect stands"
    ),
    "benchmarks/parallelism/stack_fixture.py": (
        "writes {'build_mode': 'scrum', 'current_stories': []} only; there is no "
        "SPQ arm to the benchmark's own fixture"
    ),
}

REGISTRY = {}
for _bucket, _kind in (
    (OWNS, "OWNS"), (AWARE, "AWARE"), (BLIND, "BLIND"),
    (PATH, "PATH"), (FIXTURE, "FIXTURE"),
):
    for _path in _bucket:
        REGISTRY[_path] = _kind
for _path in MIXED:
    REGISTRY[_path] = "MIXED"


# ── Census ────────────────────────────────────────────────────────────────────


def test_every_board_reader_is_classified(repo_root: Path):
    """A file that reads pipeline state and is not in the registry fails HERE.

    This is the class-closing assertion. It does not check that a new reader is
    correct — it cannot — it checks that adding one is a decision somebody had
    to write down.
    """
    unregistered = sorted(_scan(repo_root) - set(REGISTRY))
    assert not unregistered, (
        "These files read pipeline state but are not classified in "
        "test_board_reader_census.py:\n  "
        + "\n  ".join(unregistered)
        + "\n\nUnder `build_mode: spq`, `pipeline-state.json` is a mode+identity "
        "POINTER and the board lives in "
        "spq/cycles/<cycle-id>/execution-state.json. A reader "
        "that expects `current_stories` there sees nothing WITHOUT erroring and "
        "reports emptiness as a finding — the defect four separate readers have "
        "already had (#505, #509, #514).\n"
        "Fix: read the board through `pipeline_board.read_board(project_dir)`, "
        "which knows every layout and reports an unreadable board as unreadable "
        "instead of empty. Then classify the file in the registry above."
    )


def test_registry_carries_no_ghosts(repo_root: Path):
    """Every registry entry must still name a file that still reads the board.

    Without this the registry silently accumulates entries for files that were
    deleted or stopped reading state, and a stale declaration is how a wrong
    answer becomes durable.
    """
    live = _scan(repo_root)
    ghosts = []
    for path in sorted(REGISTRY):
        full = repo_root / path
        if not full.exists():
            ghosts.append("%s (file is gone)" % path)
        elif path not in live and path != "core/lib/advance_kernel.py":
            ghosts.append("%s (no longer reads pipeline state)" % path)
    assert not ghosts, (
        "Stale registry entries — delete them:\n  " + "\n  ".join(ghosts)
    )


def test_mixed_entries_still_name_a_real_symbol(repo_root: Path):
    """A MIXED declaration names the blind entry point; that name must exist.

    Weaker than the probes below and deliberately labelled so: this catches a
    rename or a deletion, not a fix. A MIXED module cannot be verified by
    reading it, which is exactly why each one needs its own ticket.
    """
    missing = []
    for path, (_reason, symbols) in MIXED.items():
        text = (repo_root / path).read_text(encoding="utf-8", errors="replace")
        for symbol in symbols:
            if symbol not in text:
                missing.append("%s: %r" % (path, symbol))
    assert not missing, (
        "MIXED declarations name entry points that no longer exist — re-read the "
        "module and reclassify:\n  " + "\n  ".join(missing)
    )


# ── Probes against a board the state machine actually produced ────────────────


def _paths_for(repo_root: Path):
    return [
        str(repo_root / "core" / "lib"),
        str(repo_root / "core" / "scripts"),
        str(repo_root / "benchmarks" / "parallelism"),
    ]


@pytest.fixture
def spq_board(repo_root: Path, monkeypatch):
    """Local override of the conftest factory: `open_cycle` is keyword-only now.

    Same contract -- `(project_dir, units=..., goal=...) -> (project, opened)`
    with the board produced by the state machine rather than hand-written --
    but the #644 declaration needs `repository`, `trunk_ref`, `source_region`
    and an `engineering_lead`, and a Cycle cannot open before Discovery
    approves a baseline. `_spq_fixture.open_project` supplies that shape once so
    it does not get restated per test file.
    """
    for entry in _paths_for(repo_root):
        if entry not in sys.path:
            monkeypatch.syspath_prepend(entry)
    return _spq_fixture.open_project


@pytest.fixture
def real_spq_project(tmp_path: Path, spq_board, repo_root: Path, monkeypatch):
    """A project whose board came from `open_cycle`, with two admitted units."""
    for entry in _paths_for(repo_root):
        if entry not in sys.path:
            monkeypatch.syspath_prepend(entry)
    project, _state = spq_board(
        tmp_path / "spq-project",
        units=[{"id": "WU-1", "title": "index writer"},
               {"id": "WU-2", "title": "query api"}],
        goal="ship the search feature",
    )
    return project


def test_the_accessor_sees_the_real_board(real_spq_project: Path):
    import pipeline_board

    board = pipeline_board.read_board(str(real_spq_project))
    assert board.available is True
    assert board.is_spq is True
    assert board.story_ids() == ["WU-1", "WU-2"]
    assert board.identity["cycle_seq"] == 1


def test_signals_sees_the_real_board(spq_repeated_failure, tmp_path: Path,
                                     repo_root: Path, monkeypatch):
    """MethodSignals detect a recurring failure class on a real SPQ board.

    **This test PASSES against `origin/dev` too, and that is a finding, not a
    hole in the test.** #514 reported `signals.py:213` iterating
    `state.get("current_stories")` as blind. On the default (`state=None`) path
    it was not: it called `story_pipeline._read_state`, which resolves SPQ. The
    read was TRANSITIVELY correct, through a dependency nothing in the file
    declared and no test pinned — so a refactor back to a plain `json.load`
    would have reintroduced the defect with every suite green. #514 makes the
    dependency explicit (`pipeline_board.read_board`) and this test pins it.

    See `test_signals_reports_an_unreadable_board_instead_of_no_signals` for
    the half of signals.py that WAS wrong.
    """
    for entry in _paths_for(repo_root):
        if entry not in sys.path:
            monkeypatch.syspath_prepend(entry)
    import signals

    project, unit_id, reason_hash = spq_repeated_failure(tmp_path / "signals-project")
    written = signals.harvest_pipeline_signals(str(project))
    recurring = [r for r in written if r["kind"] == "recurring_failure"]
    assert recurring, (
        "MethodSignals are SPQ machinery (SPQ design §12.2) and must detect a "
        "recurring failure class on an SPQ board; got %r" % (written,)
    )
    assert recurring[0]["data"] == {
        "story_id": unit_id, "role": "qe", "reason_hash": reason_hash, "count": 2,
    }


def test_signals_reports_an_unreadable_board_instead_of_no_signals(
    tmp_path: Path, repo_root: Path, monkeypatch
):
    """The half of signals.py that WAS wrong: a failed read read as no learning.

    An SPQ project still on the retired v3.0 layout has no board to read.
    Before #514 that was swallowed twice — `_read_state` returned the pointer,
    and `harvest_pipeline_signals` had its own bare `except Exception: state =
    {}` — so the retro harvested nothing and recorded nothing about why. "No
    signals" then meant either "nothing to learn" or "never looked at the
    board", with no way to tell.

    **This test found the refusal missing a second time (#640).** It used to
    hold because `spq_state_machine.read_state` refused a v3.0 pointer
    outright; the rewritten machine returns the default board whenever no Cycle
    identity resolves, so this project — whose pointer still names
    `CYCLE_EXECUTION` and cycle 1 — read as an empty pre-Commit Cycle and
    harvested silently again. The refusal now lives in the accessor
    (`pipeline_board.RETIRED_V3_REFUSAL`), which is the module whose contract
    is that unread and empty are different answers.
    """
    for entry in _paths_for(repo_root):
        if entry not in sys.path:
            monkeypatch.syspath_prepend(entry)
    import signals

    project = tmp_path / "unmigrated"
    orch = project / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "pipeline-state.json").write_text(json.dumps({
        "version": "3.0", "build_mode": "spq",
        "lifecycle_state": "CYCLE_EXECUTION", "current_cycle": 1,
    }), encoding="utf-8")

    written = signals.harvest_pipeline_signals(str(project))
    kinds = [r["kind"] for r in written]
    assert "board_unreadable" in kinds, (
        "a harvest that could not read the board must say so; got %r" % (written,)
    )
    record = [r for r in written if r["kind"] == "board_unreadable"][0]
    assert record["data"]["build_mode"] == "spq"
    problems = " ".join(record["data"]["problems"])
    assert "retired v3.0" in problems, problems
    assert "ADR-035" in problems, (
        "the recorded reason must name the decision, because there is no "
        "migrator left to name instead; got %r" % (problems,)
    )

    # Idempotent, like every other kind: a retro re-run must not re-append it.
    assert signals.harvest_pipeline_signals(str(project)) == []


def test_a_fresh_project_is_not_reported_as_an_unreadable_board(
    tmp_path: Path, repo_root: Path, monkeypatch
):
    """The counter-case. A project with no pipeline state has nothing to read,
    which is not the same as a read that failed — and a signal on every fresh
    project would make the new kind noise nobody looks at."""
    for entry in _paths_for(repo_root):
        if entry not in sys.path:
            monkeypatch.syspath_prepend(entry)
    import signals

    project = tmp_path / "fresh"
    project.mkdir()
    assert signals.harvest_pipeline_signals(str(project)) == []


def test_signals_harvests_every_spec_not_only_the_active_one(
    tmp_path: Path, repo_root: Path, monkeypatch
):
    """Reading only `active_spec` is the same defect one layout over.

    `story_pipeline._read_state` selects the active slot, so before #514 a retro
    on a multi-spec project harvested one spec's learnings and silently dropped
    the rest.
    """
    for entry in _paths_for(repo_root):
        if entry not in sys.path:
            monkeypatch.syspath_prepend(entry)
    import signals

    project = tmp_path / "multi"
    orch = project / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "pipeline-state.json").write_text(json.dumps({
        "version": "3.0", "build_mode": "scrum", "active_spec": "platform",
        "specs": {
            "platform": {"lifecycle_state": "SPRINT_EXECUTION",
                         "current_stories": [{"id": "US-1", "state": "blocked",
                                              "blocked_reason": "platform stall"}]},
            "ehr": {"lifecycle_state": "SPRINT_EXECUTION",
                    "current_stories": [{"id": "US-2", "state": "blocked",
                                         "blocked_reason": "ehr stall"}]},
        },
    }), encoding="utf-8")

    written = signals.harvest_pipeline_signals(str(project))
    blocked = sorted(r["data"]["story_id"] for r in written
                     if r["kind"] == "blocked_story")
    assert blocked == ["US-1", "US-2"], (
        "a retro must see every spec's board, not only the active slot; got %r"
        % (written,)
    )


def test_state_drift_accepts_the_real_board(real_spq_project: Path):
    import state_drift

    verdict = state_drift.check_drift(str(real_spq_project))
    assert verdict["problems"] == []


# The BLIND declarations, asserted as still-blind. A declared gap that cannot
# rot: if one of these starts seeing the board, this test fails and tells you to
# delete its entry from BLIND. That is the only mechanism that keeps a recorded
# debt honest once somebody fixes it in a lane of their own.
#
# It has now done that once, in both directions. #529 fixed `verify_all.py`, the
# still-blind probe failed, and its declaration had to go — see
# `test_verify_all_sees_the_real_board` below, which took its place.


def test_verify_all_sees_the_real_board(real_spq_project: Path):
    """Was `test_verify_all_is_still_blind` until #529 closed it.

    That declaration asserted `status == "passed"` with `lifecycle_state == ""`
    — a verifier certifying a board it had never read — and the mechanism
    worked exactly as intended: fixing it made the declaration fail, which is
    what forced its removal. It is replaced by the AWARE probe rather than
    deleted, so the correctness stays pinned here as well as in
    test_verifier_cannot_certify_unread.py.
    """
    import verify_all

    out = verify_all.verify_state_machine(str(real_spq_project))
    assert out["lifecycle_state"] == "CYCLE"
    assert out["status"] == "passed"
    assert out["checks_performed"], (
        "a pass that names nothing it validated is the #529 defect; got %r"
        % (out,)
    )


def test_build_summary_is_still_blind(real_spq_project: Path):
    import build_summary

    summary = build_summary.assemble(real_spq_project)
    if summary["pipeline"]["lifecycle_state"] != "UNKNOWN":
        pytest.fail(
            "build_summary.assemble now reads the SPQ board — delete its BLIND "
            "entry (and summary/pipeline.py's) in test_board_reader_census.py. "
            "Got %r" % (summary["pipeline"]["lifecycle_state"],)
        )


def test_measure_is_still_blind(real_spq_project: Path):
    import measure

    out = measure.analyse(str(real_spq_project))
    if out["work_units"]["total"]:
        pytest.fail(
            "benchmarks/parallelism/measure.py now reads the SPQ board — delete "
            "its BLIND entry in test_board_reader_census.py. Got %r"
            % (out["work_units"],)
        )
    assert out["work_units"]["total"] == 0, (
        "measure.py reports work_units on an SPQ Cycle now; reclassify it"
    )
