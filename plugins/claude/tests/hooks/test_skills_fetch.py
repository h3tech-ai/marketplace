"""Layer 2 — `synaptory-skills-fetch.sh` fail-closed protocol materialization.

Dist packages have no on-disk `skills/_shared/protocols/` fallback (ADR-016).
SessionStart must refuse when `skills get` returns no bodies, not exit 0 with
an empty `.synaptory/.protocols/` cache.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from protocol_materialize import REQUIRED_PROTOCOLS


def _dist_plugin(tmp_path: Path, plugin_root: Path) -> Path:
    """A plugin tree with hooks + Python helpers but no on-disk protocols."""
    dest = tmp_path / "dist-plugin"
    hooks = dest / "hooks"
    lib = hooks / "lib"
    lib.mkdir(parents=True)
    shutil.copy2(plugin_root / "hooks" / "synaptory-skills-fetch.sh", hooks)
    shutil.copy2(plugin_root / "hooks" / "_plugin-env.sh", hooks)
    shutil.copy2(plugin_root / "hooks" / "_resolve-cli.sh", hooks)
    shutil.copy2(plugin_root / "hooks" / "_cp-url.sh", hooks)
    shutil.copy2(plugin_root / "hooks" / "lib" / "protocol_materialize.py", lib)
    shutil.copy2(plugin_root / "hooks" / "lib" / "hook_io.py", lib)
    shutil.copy2(plugin_root / "hooks" / "lib" / "resolve-python.sh", lib)
    (lib / "cp-url").write_text("https://example.test\n", encoding="utf-8")
    return dest


def _seed_all_protocols(stub_cli) -> None:
    for name in REQUIRED_PROTOCOLS:
        stub_cli.add_body("protocols/%s" % name, "# %s\n" % name)


@pytest.mark.hook
def test_cold_cache_without_bodies_fails_closed(
    plugin_root: Path, stub_cli, tmp_path: Path, run_hook
):
    dist = _dist_plugin(tmp_path, plugin_root)
    project = tmp_path / "project"
    project.mkdir()
    env = {
        **stub_cli.env,
        "CLAUDE_PLUGIN_ROOT": str(dist),
        "CLAUDE_PROJECT_DIR": str(project),
    }
    result = run_hook(dist / "hooks" / "synaptory-skills-fetch.sh", env=env, cwd=project)
    assert result.returncode != 0, result.stderr
    dest = project / ".synaptory" / ".protocols"
    assert not dest.exists() or not any(dest.glob("*.md"))


@pytest.mark.hook
def test_partial_fetch_fails_closed(
    plugin_root: Path, stub_cli, tmp_path: Path, run_hook
):
    dist = _dist_plugin(tmp_path, plugin_root)
    project = tmp_path / "project"
    project.mkdir()
    stub_cli.add_body("protocols/receipt-protocol", "# receipt-protocol\n")
    env = {
        **stub_cli.env,
        "CLAUDE_PLUGIN_ROOT": str(dist),
        "CLAUDE_PROJECT_DIR": str(project),
    }
    result = run_hook(dist / "hooks" / "synaptory-skills-fetch.sh", env=env, cwd=project)
    assert result.returncode != 0, result.stderr
    dest = project / ".synaptory" / ".protocols"
    assert not (dest / "iron-laws.md").exists() or (
        dest / "iron-laws.md"
    ).stat().st_size == 0


@pytest.mark.hook
def test_complete_fetch_writes_required_set_then_reloads(
    plugin_root: Path, stub_cli, tmp_path: Path, run_hook, parse_output
):
    dist = _dist_plugin(tmp_path, plugin_root)
    project = tmp_path / "project"
    project.mkdir()
    _seed_all_protocols(stub_cli)
    env = {
        **stub_cli.env,
        "CLAUDE_PLUGIN_ROOT": str(dist),
        "CLAUDE_PROJECT_DIR": str(project),
    }
    result = run_hook(dist / "hooks" / "synaptory-skills-fetch.sh", env=env, cwd=project)
    assert result.returncode == 0, result.stderr
    dest = project / ".synaptory" / ".protocols"
    for name in REQUIRED_PROTOCOLS:
        path = dest / ("%s.md" % name)
        assert path.is_file() and path.stat().st_size > 0, name
    parsed = parse_output(result.stdout)
    assert parsed is not None
    assert parsed.get("reloadSkills") is True or parsed.get("reload_skills") is True
