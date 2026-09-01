"""Layer 1 — integration test that the SE→QE→CR pipeline syncs every
substate to the Teamwork board (#115).

The reporter observed v2.6.3 jumping In Progress → Done on the Teamwork
board, skipping In Review. The infrastructure was already wired:
`story_pipeline.transition_story` calls `sync_tracker_status` per
substate, `TRACKER_STATUS_MAP` maps `reviewing → IN_REVIEW`, and
`TeamworkAdapter.update_story_status` calls `move_workflow_stage` for
IN_REVIEW and `complete_task` for DONE. The reported symptoms were a
consequence of #104 (the protocol body wasn't shipping, so the
orchestrator wasn't firing `transition_story` per substate).

These tests pin the wiring so the regression is caught at L1 if a
future change drops a sync call or wires the wrong stage.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make the tracker package importable.
_SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2]
    / "skills" / "_shared" / "scripts"
)
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Stub transport — records every call the adapter makes.
# ---------------------------------------------------------------------------


class _StubTeamworkTransport:
    """A minimal stand-in for TeamworkTransport that records every call.

    Returned task shape matches v3's payload subset the adapter reads. The
    adapter calls `get_task` to inspect existing tags; we always hand back
    `tags: None` to also exercise the #108 null-safe path.
    """

    def __init__(self, project_id: int = 42):
        self.calls: list[tuple] = []
        self._completed: dict[int, bool] = {}
        # Tag id allocation — first call to get_or_create_tag wins the id.
        self._tags: dict[str, int] = {}
        self._next_tag = 100
        # Attributes the adapter reads directly (for URL building).
        self.site_name = "h3tech-test"
        self.project_id = project_id

    # ── recording helpers ────────────────────────────────────────
    def _record(self, name: str, *args, **kw):
        self.calls.append((name, args, kw))

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    # ── methods the adapter touches ──────────────────────────────
    def check_api(self) -> bool:
        return True

    def get_or_create_tag(self, name: str) -> int:
        if name not in self._tags:
            self._tags[name] = self._next_tag
            self._next_tag += 1
        return self._tags[name]

    def get_task(self, task_id: int) -> dict:
        self._record("get_task", task_id)
        return {"id": task_id, "name": "Test", "tags": None,
                "status": "completed" if self._completed.get(task_id) else "new"}

    def update_task(self, task_id: int, **fields) -> dict:
        self._record("update_task", task_id, **fields)
        return {"id": task_id, **fields}

    def move_workflow_stage(self, task_id: int, stage_id: int,
                            workflow_id: int | None = None) -> dict:
        self._record("move_workflow_stage", task_id, stage_id)
        return {}

    def complete_task(self, task_id: int) -> dict:
        self._record("complete_task", task_id)
        self._completed[task_id] = True
        return {}

    def uncomplete_task(self, task_id: int) -> dict:
        self._record("uncomplete_task", task_id)
        self._completed[task_id] = False
        return {}


# ---------------------------------------------------------------------------
# Adapter fixture — bypass __init__ to skip live transport handshake.
# ---------------------------------------------------------------------------


WORKFLOW_STAGES = {
    "BACKLOG": 385920,
    "TO_DO": 385922,
    "IN_PROGRESS": 385923,
    "IN_REVIEW": 385924,
    "DONE": 385929,
}


@pytest.fixture
def adapter(tmp_path: Path):
    from tracker.teamwork_adapter import TeamworkAdapter

    project_dir = tmp_path / "proj"
    cache = project_dir / ".synaptory" / ".orchestrator" / "tracker-data.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({
        "epics": [], "sprints": [],
        "stories": [{
            "id": "US-1", "title": "Test", "feature": "", "epic": "",
            "priority": "", "status": "TO_DO", "size": "", "sprint": "1",
            "ac_count": 0, "acceptance_criteria": [], "raw_text": "",
        }],
    }))

    id_map = project_dir / ".synaptory" / ".orchestrator" / "tracker-id-map.json"
    id_map.write_text(json.dumps({
        "US-1": {"type": "story", "task_id": 1001, "teamwork_id": 1001},
    }))

    a = object.__new__(TeamworkAdapter)
    a.project_dir = project_dir
    a.transport = _StubTeamworkTransport()
    a.milestone_prefix = "Sprint"
    a._config_workflow_stages = WORKFLOW_STAGES
    a._sprint_tasklist_map = {}
    a._backlog_tasklist_id = None
    a._id_map = None
    a._id_map_path = id_map
    a._backlog_order_path = project_dir / ".synaptory" / ".orchestrator" / "backlog-order.json"
    a._tag_cache = {}
    a._local_cache_path = cache
    return a


# ---------------------------------------------------------------------------
# Tests — every substate fires the correct transport calls (#115)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_in_review_moves_to_in_review_stage(adapter):
    """reviewing → IN_REVIEW must call move_workflow_stage with IN_REVIEW id."""
    adapter.update_story_status("US-1", "IN_PROGRESS", allow_skip=True)
    adapter.update_story_status("US-1", "IN_REVIEW", allow_skip=True)
    moves = [c for c in adapter.transport.calls if c[0] == "move_workflow_stage"]
    assert any(c[1][1] == WORKFLOW_STAGES["IN_REVIEW"] for c in moves), (
        f"expected a move to IN_REVIEW stage {WORKFLOW_STAGES['IN_REVIEW']}, "
        f"got moves: {[c[1] for c in moves]}"
    )


@pytest.mark.unit
def test_done_moves_to_done_stage_and_completes_task(adapter):
    """DONE must (1) move the stage AND (2) complete the task.

    The reporter's specific complaint: `task.status` stays open even when
    the board says Done. The adapter's contract is "stage + complete";
    pin both.
    """
    for s in ("IN_PROGRESS", "IN_REVIEW", "DONE"):
        adapter.update_story_status("US-1", s, allow_skip=True)

    names = adapter.transport.names()
    assert "move_workflow_stage" in names
    assert "complete_task" in names, (
        "DONE must mark task.status=completed via Teamwork v1 "
        "/tasks/{id}/complete.json (#115)"
    )

    # Order: the final stage move must target DONE before complete_task fires.
    moves = [c for c in adapter.transport.calls if c[0] == "move_workflow_stage"]
    last_move = moves[-1]
    assert last_move[1][1] == WORKFLOW_STAGES["DONE"]


@pytest.mark.unit
def test_done_is_idempotent_even_when_complete_task_raises(adapter):
    """Re-firing DONE must not raise (#115 idempotency — sprint close may replay).

    Real Teamwork returns HTTP 422 / "already complete" on a second
    `PUT /tasks/{id}/complete.json`. Simulate that by making the stub
    raise AdapterError on the second call; the adapter's new try/except
    must swallow it.
    """
    from tracker.base import AdapterError

    call_count = {"complete_task": 0}
    orig = adapter.transport.complete_task

    def flaky_complete(task_id: int):
        call_count["complete_task"] += 1
        if call_count["complete_task"] >= 2:
            raise AdapterError("already complete")
        return orig(task_id)

    adapter.transport.complete_task = flaky_complete  # type: ignore[method-assign]

    for s in ("IN_PROGRESS", "IN_REVIEW", "DONE"):
        adapter.update_story_status("US-1", s, allow_skip=True)
    # Replay — second complete_task raises, must be swallowed.
    adapter.update_story_status("US-1", "DONE", allow_skip=True)
    assert call_count["complete_task"] >= 2


@pytest.mark.unit
def test_full_se_qe_cr_walk_hits_every_stage(adapter):
    """SE → QE → CR walk must touch every board column in order.

    The reporter saw In Progress → Done on the board. With #110 (pipeline
    follows protocol) + the existing sync_tracker_status call, the walk
    must touch IN_PROGRESS → IN_REVIEW → DONE.
    """
    pipeline = ["IN_PROGRESS", "IN_REVIEW", "DONE"]
    for s in pipeline:
        adapter.update_story_status("US-1", s, allow_skip=True)

    move_ids = [c[1][1] for c in adapter.transport.calls
                if c[0] == "move_workflow_stage"]
    expected = [WORKFLOW_STAGES[s] for s in pipeline]
    assert move_ids == expected, (
        f"board must visit each column in pipeline order; "
        f"got {move_ids}, expected {expected}"
    )


# ---------------------------------------------------------------------------
# sync_tracker_status end-to-end — drives the CLI; uses local_adapter
# so the test stays hermetic.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sync_tracker_status_visits_in_review(tmp_path: Path):
    """The FSM walk reviewing → done must produce IN_REVIEW then DONE on
    the tracker — pinning the TRACKER_STATUS_MAP contract.
    """
    # Ensure story_pipeline is importable.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks" / "lib"))
    import story_pipeline  # noqa: E402

    project = tmp_path / "proj"
    (project / ".synaptory" / ".orchestrator").mkdir(parents=True)
    (project / ".synaptory" / ".orchestrator" / "tracker-data.json").write_text(
        json.dumps({
            "epics": [], "sprints": [],
            "stories": [{
                "id": "US-7", "title": "T", "feature": "", "epic": "",
                "priority": "", "status": "TO_DO", "size": "", "sprint": "1",
                "ac_count": 0, "acceptance_criteria": [], "raw_text": "",
            }],
        })
    )
    (project / ".synaptory.yaml").write_text("tracker:\n  backend: local\n")

    for substate in ("in_progress", "testing", "reviewing", "done"):
        story_pipeline.sync_tracker_status(str(project), "US-7", substate)

    data = json.loads(
        (project / ".synaptory" / ".orchestrator" / "tracker-data.json")
        .read_text()
    )
    final = next(s for s in data["stories"] if s["id"] == "US-7")
    assert final["status"] == "DONE", (
        "the full FSM walk must end with the tracker showing DONE; "
        f"got {final['status']}"
    )


@pytest.mark.unit
def test_sync_tracker_status_allow_skip_does_not_warn_on_audit_jump(
    tmp_path: Path, capsys
):
    """Pipeline sync passes --allow-skip so SPQ/Cursor can close a story
    without walking every tracker column. The #111 TO_DO → DONE jump must
    succeed silently; the pipeline is canonical.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks" / "lib"))
    import story_pipeline  # noqa: E402

    project = tmp_path / "proj"
    (project / ".synaptory" / ".orchestrator").mkdir(parents=True)
    (project / ".synaptory" / ".orchestrator" / "tracker-data.json").write_text(
        json.dumps({
            "epics": [], "sprints": [],
            "stories": [{
                "id": "US-8", "title": "T", "feature": "", "epic": "",
                "priority": "", "status": "TO_DO", "size": "", "sprint": "1",
                "ac_count": 0, "acceptance_criteria": [], "raw_text": "",
            }],
        })
    )
    (project / ".synaptory.yaml").write_text("tracker:\n  backend: local\n")

    story_pipeline.sync_tracker_status(str(project), "US-8", "done")

    err = capsys.readouterr().err
    assert "tracker sync drift" not in err, (
        "allow-skip audit jumps must not look like tracker drift; "
        f"got: {err!r}"
    )
    data = json.loads(
        (project / ".synaptory" / ".orchestrator" / "tracker-data.json")
        .read_text()
    )
    final = next(s for s in data["stories"] if s["id"] == "US-8")
    assert final["status"] == "DONE"


@pytest.mark.unit
def test_sync_tracker_status_warns_on_failure(
    tmp_path: Path, capsys, monkeypatch
):
    """When the tracker CLI exits non-zero (offline remote, adapter crash),
    sync_tracker_status must surface a stderr warning so the agent
    transcript shows the drift instead of board state silently lagging.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks" / "lib"))
    import story_pipeline  # noqa: E402

    project = tmp_path / "proj"
    (project / ".synaptory" / ".orchestrator").mkdir(parents=True)
    (project / ".synaptory" / ".orchestrator" / "tracker-data.json").write_text(
        json.dumps({
            "epics": [], "sprints": [],
            "stories": [{
                "id": "US-8", "title": "T", "feature": "", "epic": "",
                "priority": "", "status": "TO_DO", "size": "", "sprint": "1",
                "ac_count": 0, "acceptance_criteria": [], "raw_text": "",
            }],
        })
    )
    (project / ".synaptory.yaml").write_text("tracker:\n  backend: local\n")

    class _Fail:
        returncode = 1
        stderr = "connection refused"
        stdout = ""

    monkeypatch.setattr(
        story_pipeline.subprocess, "run", lambda *a, **k: _Fail()
    )

    story_pipeline.sync_tracker_status(str(project), "US-8", "done")

    err = capsys.readouterr().err
    assert "tracker sync drift" in err, (
        "sync drift must produce a visible stderr warning; "
        f"got: {err!r}"
    )
    assert "US-8" in err
    assert "connection refused" in err
