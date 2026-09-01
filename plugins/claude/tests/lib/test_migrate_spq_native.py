"""Layer 1 — the one-way SPQ migration off Multi-Spec (#303 AC).

The AC is "current SPQ workstream state is migrated explicitly to native SPQ
storage". Explicitly is the load-bearing word: an implicit or lossy move would
be worse than refusing, because what is at stake includes each Work Unit's
`mcp_consumed_receipts` anti-replay ledger. Losing that silently re-arms
consumed receipts for replay.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

import spq_paths as sp

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "core" / "scripts" / "migrate_spq_native.py"

sys.path.insert(0, str(REPO_ROOT / "core" / "scripts"))
import migrate_spq_native as msn  # noqa: E402


def _unit(uid: str, state: str = "done") -> dict:
    return {
        "id": uid,
        "title": uid,
        "state": state,
        "pipeline_log": [
            {"state": state, "entered_at": "2026-01-01T00:00:00+00:00", "exited_at": None}
        ],
        "mcp_consumed_receipts": ["sha256:deadbeef"],
        "retries": {"se": 2},
        "rejection_feedback": [{"at": "2026-01-01T00:00:00+00:00", "note": "n"}],
        "dod": {"passed": True},
        "depends_on": [],
        "file_scope": [],
    }


def _v3_project(
    tmp_path: Path, *, build_mode: str = "spq", specs: dict | None = None
) -> Path:
    project = tmp_path / "proj"
    orch = project / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    (project / ".synaptory.yaml").write_text(
        "build_mode: %s\n" % build_mode, encoding="utf-8"
    )
    specs = specs if specs is not None else {
        "spine": {
            "lifecycle_state": "CYCLE_EXECUTION",
            "current_cycle": 4,
            "cycle_goal": "g4",
            "current_stories": [_unit("WU-1")],
        }
    }
    for ws in specs:
        (orch / "specs" / ws / "receipts").mkdir(parents=True, exist_ok=True)
    (orch / "pipeline-state.json").write_text(
        json.dumps(
            {
                "version": "3.0",
                "build_mode": build_mode,
                "active_spec": sorted(specs)[0],
                "specs": specs,
            }
        ),
        encoding="utf-8",
    )
    return project


# ── scope: SPQ only ────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["scrum", "kanban"])
def test_refuses_a_non_spq_project(tmp_path: Path, mode: str):
    """Multi-Spec is RETAINED for scrum and kanban. A scrum team running three
    label-filtered boards from one checkout must never be asked to change, and
    the refusal has to say so rather than looking like a bug."""
    project = _v3_project(
        tmp_path,
        build_mode=mode,
        specs={"a": {"lifecycle_state": "SPRINT_EXECUTION", "current_sprint": 1}},
    )
    with pytest.raises(msn.MigrationError) as excinfo:
        msn.plan(project)
    message = str(excinfo.value)
    assert mode in message
    assert "RETAINED" in message


def test_already_native_is_a_no_op(tmp_path: Path):
    project = tmp_path / "proj"
    orch = project / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "pipeline-state.json").write_text(
        json.dumps({"version": "2.0", "build_mode": "spq", "spq": {}}), encoding="utf-8"
    )
    assert msn.migrate(project)["status"] == "already_native"


def test_no_state_is_a_no_op(tmp_path: Path):
    assert msn.migrate(tmp_path)["status"] == "no_state"


def test_rerunning_after_a_migration_is_a_no_op(tmp_path: Path):
    project = _v3_project(tmp_path)
    assert msn.migrate(project)["status"] == "migrated"
    assert msn.migrate(project)["status"] == "already_native"


# ── nothing is lost ────────────────────────────────────────────────────────


def test_work_unit_state_survives_byte_identical(tmp_path: Path):
    """Every field matters. Dropping `mcp_consumed_receipts` would silently
    re-arm consumed receipts for replay; dropping a `pipeline_log` entered_at
    would make every existing receipt look stale."""
    project = _v3_project(tmp_path)
    before = json.loads(
        (project / ".synaptory" / ".orchestrator" / "pipeline-state.json").read_text()
    )["specs"]["spine"]["current_stories"][0]

    result = msn.migrate(project)
    cycle_id = result["workstreams"][0]["cycle_id"]
    after = json.loads(
        Path(sp.execution_state_path(str(project), cycle_id, "spine")).read_text()
    )["current_stories"][0]

    assert after == before, "the Work Unit record must be hoisted verbatim"


def test_lifecycle_and_cycle_bookkeeping_survive(tmp_path: Path):
    project = _v3_project(tmp_path)
    result = msn.migrate(project)
    cycle_id = result["workstreams"][0]["cycle_id"]
    body = json.loads(
        Path(sp.execution_state_path(str(project), cycle_id, "spine")).read_text()
    )
    assert body["lifecycle_state"] == "CYCLE_EXECUTION"
    assert body["current_cycle"] == 4
    assert body["cycle_goal"] == "g4"
    assert body["build_mode"] == "spq"
    assert "_spec_id" not in body and "_multispec" not in body


def test_snapshot_covers_the_orchestrator_and_the_config(tmp_path: Path):
    """The predecessor tarred only `.orchestrator/`. A rollback that restores
    state but not config leaves the two disagreeing."""
    project = _v3_project(tmp_path)
    result = msn.migrate(project)
    with tarfile.open(result["snapshot"]) as tar:
        names = tar.getnames()
    assert ".synaptory.yaml" in names
    assert any(n.startswith(".orchestrator") for n in names)


def test_a_move_ledger_is_written(tmp_path: Path):
    """Without it, a mis-routed receipt is undiagnosable."""
    project = _v3_project(tmp_path)
    orch = project / ".synaptory" / ".orchestrator"
    (orch / "specs" / "spine" / "receipts" / "WU-1-se.json").write_text("{}")
    result = msn.migrate(project)
    ledger = json.loads(Path(result["receipt_map"]).read_text())
    assert ledger["moves"], "every file move must be recorded"
    assert any("WU-1-se.json" in m["to"] for m in ledger["moves"])


# ── identity ───────────────────────────────────────────────────────────────


def test_workstreams_on_the_same_cycle_number_share_one_cycle_id(tmp_path: Path):
    """The barrier quorum is assembled per Cycle. Two workstreams running
    Cycle 4 ARE the same Cycle -- that is the premise of the barrier -- so
    giving them different identities would make the quorum unassemblable."""
    project = _v3_project(
        tmp_path,
        specs={
            "spine": {"lifecycle_state": "CYCLE_EXECUTION", "current_cycle": 4,
                      "current_stories": [_unit("WU-1")]},
            "frame": {"lifecycle_state": "CYCLE_EXECUTION", "current_cycle": 4,
                      "current_stories": [_unit("WU-2")]},
        },
    )
    result = msn.migrate(project)
    ids = {w["cycle_id"] for w in result["workstreams"]}
    assert len(ids) == 1, ids


def test_distinct_cycle_numbers_get_distinct_identities(tmp_path: Path):
    project = _v3_project(
        tmp_path,
        specs={
            "spine": {"lifecycle_state": "CYCLE_EXECUTION", "current_cycle": 4,
                      "current_stories": [_unit("WU-1")]},
            "frame": {"lifecycle_state": "COMMIT", "current_cycle": 5,
                      "current_stories": []},
        },
    )
    result = msn.migrate(project)
    by_ws = {w["workstream_id"]: w for w in result["workstreams"]}
    assert by_ws["spine"]["cycle_id"] != by_ws["frame"]["cycle_id"]
    assert sp.seq_of(by_ws["spine"]["cycle_id"]) == 4
    assert sp.seq_of(by_ws["frame"]["cycle_id"]) == 5


def test_pointer_becomes_the_mode_and_identity_pointer(tmp_path: Path):
    project = _v3_project(tmp_path)
    msn.migrate(project)
    pointer = json.loads(
        (project / ".synaptory" / ".orchestrator" / "pipeline-state.json").read_text()
    )
    assert pointer["version"] == "2.0"
    assert pointer["build_mode"] == "spq"
    assert "specs" not in pointer and "active_spec" not in pointer
    assert pointer["spq"]["workstream_id"] == "spine"
    assert sp.CYCLE_ID_RE.match(pointer["spq"]["cycle_id"])
    assert pointer["migrated_from_multispec"] is True


def test_single_workstream_is_pinned_without_a_warning(tmp_path: Path):
    project = _v3_project(tmp_path)
    result = msn.migrate(project)
    assert sp.read_pin(str(project)) == "spine"
    assert not result["warnings"]


def test_multi_workstream_pin_warns_about_one_clone_per_workstream(tmp_path: Path):
    """A wrong pin silently attributes one lane's work to another, so the
    ambiguous case has to be loud rather than quietly guessing."""
    project = _v3_project(
        tmp_path,
        specs={
            "spine": {"lifecycle_state": "CYCLE_EXECUTION", "current_cycle": 4,
                      "current_stories": [_unit("WU-1")]},
            "frame": {"lifecycle_state": "CYCLE_EXECUTION", "current_cycle": 4,
                      "current_stories": [_unit("WU-2")]},
        },
    )
    result = msn.migrate(project)
    assert result["warnings"]
    assert "one clone per workstream" in result["warnings"][0]


def test_a_spec_id_that_is_not_a_valid_workstream_id_refuses(tmp_path: Path):
    """A workstream id becomes a path segment and a git ref component, so it
    cannot be laundered from an arbitrary spec id."""
    project = _v3_project(
        tmp_path,
        specs={"Not A Workstream": {"lifecycle_state": "COMMIT", "current_cycle": 1,
                                    "current_stories": []}},
    )
    with pytest.raises(msn.MigrationError, match="workstream id"):
        msn.migrate(project)


# ── receipt routing ────────────────────────────────────────────────────────


def test_cycle_level_receipts_route_above_the_workstreams(tmp_path: Path):
    project = _v3_project(tmp_path)
    receipts = project / ".synaptory" / ".orchestrator" / "specs" / "spine" / "receipts"
    for name in ("SYNC-4-barrier.json", "CHECKPOINT-4-tw.json", "CYCLE-4-po.json"):
        (receipts / name).write_text("{}")
    (receipts / "WU-1-se.json").write_text("{}")

    result = msn.migrate(project)
    cycle_id = result["workstreams"][0]["cycle_id"]
    cycle_names = {p.name for p in Path(sp.receipts_dir(str(project), cycle_id=cycle_id)).iterdir()}
    unit_names = {
        p.name
        for p in Path(
            sp.receipts_dir(str(project), cycle_id=cycle_id, workstream_id="spine")
        ).iterdir()
    }
    assert cycle_names == {"SYNC-4-barrier.json", "CHECKPOINT-4-tw.json", "CYCLE-4-po.json"}
    assert unit_names == {"WU-1-se.json"}


def test_an_orphan_cycle_receipt_is_left_in_place_with_a_warning(tmp_path: Path):
    """Silently filing a receipt under the wrong Cycle would corrupt the audit
    trail; leaving it and saying so is recoverable."""
    project = _v3_project(tmp_path)
    receipts = project / ".synaptory" / ".orchestrator" / "specs" / "spine" / "receipts"
    (receipts / "SYNC-99-barrier.json").write_text("{}")
    result = msn.migrate(project)
    assert result["workstreams"][0]["receipts_left_in_place"] == 1
    assert any("SYNC-99" in w for w in result["warnings"])
    assert (receipts / "SYNC-99-barrier.json").exists()


def test_adopt_orphan_receipts_files_them_under_the_current_cycle(tmp_path: Path):
    project = _v3_project(tmp_path)
    receipts = project / ".synaptory" / ".orchestrator" / "specs" / "spine" / "receipts"
    (receipts / "SYNC-99-barrier.json").write_text("{}")
    result = msn.migrate(project, adopt_orphan_receipts=True)
    cycle_id = result["workstreams"][0]["cycle_id"]
    assert (
        Path(sp.receipts_dir(str(project), cycle_id=cycle_id)) / "SYNC-99-barrier.json"
    ).exists()


def test_tracker_data_moves_with_its_workstream(tmp_path: Path):
    project = _v3_project(tmp_path)
    src = project / ".synaptory" / ".orchestrator" / "specs" / "spine" / "tracker-data.json"
    src.write_text('{"stories": []}')
    result = msn.migrate(project)
    cycle_id = result["workstreams"][0]["cycle_id"]
    assert Path(sp.tracker_data_path(str(project), cycle_id, "spine")).is_file()
    assert not src.exists()


def test_a_collision_refuses_rather_than_clobbering(tmp_path: Path):
    """A stale destination usually means a partial earlier run. Overwriting it
    would destroy the only copy of whichever side was authoritative."""
    project = _v3_project(tmp_path)
    (project / ".synaptory" / ".orchestrator" / "specs" / "spine" / "receipts"
     / "WU-1-se.json").write_text('{"real": true}')
    # Pre-create native state so the hoist collides.
    plan_result = msn.plan(project)
    assert plan_result["status"] == "migratable"
    fake_cycle = sp.new_cycle_id(4)
    dest = Path(sp.execution_state_path(str(project), fake_cycle, "spine"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("{}")
    # A different cycle_id is allocated each run, so force the collision by
    # pointing at the very path the migration will write.
    import unittest.mock as mock

    with mock.patch.object(sp, "new_cycle_id", return_value=fake_cycle):
        with pytest.raises(msn.MigrationError, match="refusing to overwrite"):
            msn.migrate(project)


# ── the sync subtree is untouched ──────────────────────────────────────────


def test_committed_sync_records_are_not_touched(tmp_path: Path):
    """`.synaptory/sync/` is git-tracked, per-Cycle, and independent of the
    state layout. Moving it would rewrite committed history."""
    project = _v3_project(tmp_path)
    sync = project / ".synaptory" / "sync" / "cycle-4"
    sync.mkdir(parents=True)
    record = sync / "spine.json"
    record.write_text('{"schema_version": "1.1"}')
    msn.migrate(project)
    assert record.read_text() == '{"schema_version": "1.1"}'


# ── the shipped entry point ────────────────────────────────────────────────


def test_the_script_is_reachable_and_plan_only_writes_nothing(tmp_path: Path):
    project = _v3_project(tmp_path)
    before = (project / ".synaptory" / ".orchestrator" / "pipeline-state.json").read_text()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--project-dir", str(project), "--plan"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "spine" in result.stdout
    assert (
        project / ".synaptory" / ".orchestrator" / "pipeline-state.json"
    ).read_text() == before


def test_the_cli_prints_a_runnable_rollback_command(tmp_path: Path):
    project = _v3_project(tmp_path)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--project-dir", str(project), "--yes"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "tar -xzf" in result.stdout


def test_the_state_machine_refusal_names_this_script():
    """A migration demand is only useful if it names the command, and the name
    has to match the file that actually ships."""
    import spq_state_machine

    assert "migrate_spq_native.py" in spq_state_machine._V3_REFUSAL
    assert SCRIPT.is_file()
