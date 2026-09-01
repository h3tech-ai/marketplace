"""Layer 1 - tracker CLI resolution across every installed layout.

A wrong guess here fails SILENTLY: `sync_tracker_status` returns early, so the
pipeline advances while the tracker never hears about it. That is exactly what
happened to Codex — the composed package puts the shared scripts at
`runtime/scripts/`, which the old hard-coded `skills/_shared/scripts` path never
matched, and the source tree moved to `core/scripts/` on top of that.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import story_pipeline as sp

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("SYNAPTORY_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)


def _make_layout(root: Path, *parts: str) -> Path:
    target = root.joinpath(*parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# stub tracker\n", encoding="utf-8")
    return target


@pytest.mark.parametrize(
    "parts",
    [
        ("scripts", "tracker", "tracker_cli.py"),               # core source tree
        ("skills", "_shared", "scripts", "tracker", "tracker_cli.py"),  # Claude / Cursor
        ("runtime", "scripts", "tracker", "tracker_cli.py"),    # Codex package
    ],
    ids=["core_source", "claude_cursor_package", "codex_package"],
)
def test_every_layout_resolves(tmp_path, monkeypatch, parts):
    expected = _make_layout(tmp_path, *parts)
    monkeypatch.setenv("SYNAPTORY_PLUGIN_ROOT", str(tmp_path))
    assert sp._resolve_tracker_cli() == str(expected)


def test_unknown_layout_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNAPTORY_PLUGIN_ROOT", str(tmp_path))
    monkeypatch.setattr(sp, "__file__", str(tmp_path / "lib" / "story_pipeline.py"))
    assert sp._resolve_tracker_cli() is None


def test_repo_tree_resolves_without_env():
    """The module-relative fallback must land on the real tracker CLI.

    Which spelling it finds depends on how story_pipeline was imported: through
    plugin-claude's symlink it resolves via skills/_shared/scripts, directly
    from core/ it resolves via core/scripts. Both are the same file on disk, so
    assert the identity rather than the spelling.
    """
    resolved = sp._resolve_tracker_cli()
    assert resolved is not None
    assert Path(resolved).is_file()
    assert (
        Path(resolved).resolve()
        == (REPO / "core" / "scripts" / "tracker" / "tracker_cli.py").resolve()
    )


def test_codex_source_tree_adapter_syncs_the_tracker(tmp_path, monkeypatch):
    """The regression the review caught: a source-tree Codex advance must sync.

    mcp_server exports its dev RUNTIME_ROOT (<repo>/core) as the plugin root.
    story_pipeline then had to build a path under it, and the only path it knew
    was `<root>/skills/_shared/scripts/...`, which does not exist under core/.
    The result was a silent no-op: state advanced, tracker never notified.
    """
    import importlib.util

    server_path = (
        REPO / "plugin-codex" / "plugins" / "synaptory" / "scripts" / "mcp_server.py"
    )
    spec = importlib.util.spec_from_file_location("codex_server_tracker", server_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Dev mode resolves to the repo's core/, never to a sibling host plugin.
    assert module.RUNTIME_ROOT == REPO / "core"

    monkeypatch.setenv("SYNAPTORY_PLUGIN_ROOT", str(module.RUNTIME_ROOT))
    resolved = sp._resolve_tracker_cli()
    assert resolved is not None, (
        "Codex source-tree advance would skip tracker sync silently"
    )
    assert Path(resolved).is_file()

    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    monkeypatch.setattr(sp.subprocess, "run", _fake_run)
    sp.sync_tracker_status(str(tmp_path), "US-001", "done")

    assert calls, "sync_tracker_status did not invoke the tracker CLI"
    assert str(resolved) in calls[0]
    assert "update-status" in calls[0]
