"""Layout contract and preservation checks for issue #710."""
import json
from pathlib import Path

import pytest

import kanban_state_machine as kanban
import pipeline_board
import scrum_state_machine as scrum
import spec_state
from state_schema import state_schema

ROOT = Path(__file__).resolve().parents[3]
CASES = json.loads((ROOT / "core/contracts/state-schema-cases.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_shared_contract(case):
    if case.get("error"):
        with pytest.raises(ValueError, match="state_schema"):
            state_schema(case["state"])
    else:
        assert (state_schema(case["state"]) or 0) == case["layout"]


def write_legacy(project, state):
    path = Path(spec_state.state_path(str(project)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))
    return path


@pytest.mark.parametrize("machine", [scrum, kanban])
@pytest.mark.parametrize("layout", [2, 3])
def test_legacy_board_survives_read_and_repeated_write(tmp_path, machine, layout):
    state = machine._default_state()
    state.pop("state_schema", None)
    state["version"] = "2.0"
    state["current_stories"] = [{"id": "kept", "state": "testing"}]
    if layout == 3:
        state.pop("version")
        state = {"version": "3.0", "build_mode": "scrum" if machine is scrum else "kanban",
                 "active_spec": "one", "specs": {"one": state, "two": {"lifecycle_state": "INCEPTION", "current_stories": [{"id":"other", "state":"queued"}]}}}
    path = write_legacy(tmp_path, state)
    before = path.read_bytes()
    view = machine.read_state(str(tmp_path))
    board = pipeline_board.read_board(str(tmp_path))
    assert board.available and not board.problems
    assert set(board.story_ids()) == ({"kept", "other"} if layout == 3 else {"kept"})
    assert path.read_bytes() == before
    original = json.loads(json.dumps(view))
    machine._write_state(str(tmp_path), view)
    machine._write_state(str(tmp_path), view)
    assert view == original
    saved = json.loads(path.read_text())
    assert saved["state_schema"] == layout
    if layout == 3:
        assert saved["specs"]["two"] == state["specs"]["two"]
        assert "state_schema" not in saved["specs"]["one"]
    assert pipeline_board.read_board(str(tmp_path)).story_ids() == board.story_ids()


@pytest.mark.parametrize("machine", [scrum, kanban])
def test_product_version_flat_still_refuses_multispec_init(tmp_path, machine):
    path = write_legacy(tmp_path, {"version": "1.3.1", "state_schema": 2, "lifecycle_state": "INCEPTION"})
    before = path.read_bytes()
    with pytest.raises(ValueError, match="migrate_to_multispec.py"):
        machine.initialize(str(tmp_path), spec_id="new")
    assert path.read_bytes() == before


def test_invalid_explicit_layout_is_an_unavailable_board(tmp_path):
    write_legacy(tmp_path, {"version": "2.0", "state_schema": 42, "lifecycle_state": "INCEPTION"})
    board = pipeline_board.read_board(str(tmp_path))
    assert not board.available
    assert "state_schema" in board.problems[0]
