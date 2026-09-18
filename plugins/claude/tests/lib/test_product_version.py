"""Release metadata is sourced from the executing package, never project state."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

import product_version as release
import spec_state
from state_schema import state_schema, with_state_metadata

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("host", ["source", "cursor", "claude", "codex"])
def test_each_package_layout_resolves_its_own_release(tmp_path, monkeypatch, host):
    expected = "1.4.2-rc.1+test"
    runtime = tmp_path / ("core/lib" if host == "source" else "hooks/lib")
    runtime.mkdir(parents=True)
    shutil.copy2(release.__file__, runtime / "product_version.py")
    if host in ("source", "cursor"):
        (tmp_path / "VERSION").write_text(expected + "\n")
    else:
        manifest = tmp_path / f".{host}-plugin/plugin.json"
        manifest.parent.mkdir()
        manifest.write_text(json.dumps({"version": expected}))
    monkeypatch.setenv("SYNAPTORY_PLUGIN_VERSION", "9.9.9")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/unrelated/plugin")
    result = subprocess.run([sys.executable, str(runtime / "product_version.py")],
                            cwd=tmp_path, text=True, capture_output=True, check=True)
    assert result.stdout.strip() == expected


@pytest.mark.parametrize("value", [None, "", "3.0", "dev", "v1.3.0"])
def test_missing_or_invalid_release_is_not_guessed(tmp_path, monkeypatch, value):
    monkeypatch.setattr(release, "RUNTIME_ROOT", tmp_path)
    if value is not None:
        (tmp_path / "VERSION").write_text(value)
    with pytest.raises(ValueError, match="release metadata"):
        release.product_version()


def test_source_and_new_config_match_version_file():
    expected = (ROOT / "VERSION").read_text().strip()
    assert release.product_version() == expected
    import yaml
    template = ROOT / "plugin-claude/skills/_shared/templates/synaptory.yaml.tmpl"
    resolved = release.render_config_template(template.read_text())
    assert yaml.safe_load(resolved)["version"] == expected
    assert "{{PRODUCT_VERSION}}" not in resolved


@pytest.mark.parametrize("layout", [2, 3])
def test_legacy_state_metadata_upgrade_does_not_mutate_input(layout):
    state = {"version": f"{layout}.0", "current_stories": [{"id":"preserved"}]}
    original = json.loads(json.dumps(state))
    stamped = with_state_metadata(state)
    assert state == original
    assert stamped["version"] == release.product_version()
    assert state_schema(stamped) == layout
    assert stamped["current_stories"] == state["current_stories"]


def test_spq_pointer_and_cycle_board_have_distinct_layouts(tmp_path):
    import _spq_fixture
    import spq_state_machine as spq
    import pipeline_board
    project, opened = _spq_fixture.open_project(tmp_path)
    pointer = spq.read_pointer(str(project))
    board_path = Path(spq._board_path(str(project), pointer["spq"]["cycle_id"]))
    board = json.loads(board_path.read_text())
    for state, layout in ((pointer, 2), (board, 3)):
        assert state["version"] == release.product_version()
        assert state_schema(state) == layout
    assert pipeline_board.read_board(str(project)).available
    # A pre-change board gains metadata on its next write, not its next read.
    board["version"] = "3.0"
    board.pop("state_schema")
    board_path.write_text(json.dumps(board))
    before = board_path.read_bytes()
    state = spq.read_state(str(project))
    assert board_path.read_bytes() == before
    spq._write_state(str(project), state)
    saved = json.loads(board_path.read_text())
    assert saved["version"] == release.product_version() and saved["state_schema"] == 3
    assert saved["current_stories"] == board["current_stories"]


def test_layout_migration_preserves_data_and_product_release(tmp_path):
    path = Path(spec_state.state_path(str(tmp_path)))
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version":"2.0", "build_mode":"scrum",
                               "lifecycle_state":"INCEPTION", "current_stories":[{"id":"kept"}]}))
    result = spec_state.upgrade_v2_to_v3(str(tmp_path), "primary")
    assert result["state_schema"] == 3
    assert result["version"] == release.product_version()
    assert result["specs"]["primary"]["current_stories"] == [{"id":"kept"}]
    assert not ({"version", "state_schema"} & result["specs"]["primary"].keys())
    before = path.read_bytes()
    assert spec_state.upgrade_v2_to_v3(str(tmp_path), "primary") == result
    assert path.read_bytes() == before
