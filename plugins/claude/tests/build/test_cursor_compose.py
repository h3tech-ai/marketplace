"""Layer 2 — plugin-cursor compose is wired into ./synaptory build."""

from __future__ import annotations

from pathlib import Path


def test_cmd_build_invokes_cursor_compose(repo_root: Path):
    text = (repo_root / "plugin-claude" / "synaptory").read_text(encoding="utf-8")
    assert "_compose_cursor_plugin" in text
    assert "_package_cursor_marketplace" in text
    assert "plugin-cursor/scripts/compose.sh" in text
    assert "--strip-ip" in text
