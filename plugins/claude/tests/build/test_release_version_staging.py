"""Regression coverage for the release version index boundary."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args), cwd=cwd, capture_output=True, text=True, check=True
    )


def test_release_version_staging_indexes_every_host_output(
    repo_root: Path, tmp_path: Path
):
    repository = tmp_path / "release-repo"
    repository.mkdir()
    files = {
        "VERSION": "1.1.1\n",
        "plugin-claude/.claude-plugin/plugin.json": '{"version":"1.1.1"}\n',
        "web/package.json": '{"version":"1.1.1"}\n',
        "plugin-cursor/VERSION": "1.1.1\n",
        "plugin-cursor/.cursor-plugin/plugin.json": '{"version":"1.1.1"}\n',
        "plugin-cursor/agents/software-engineer.md": "version 1.1.1\n",
        "plugin-codex/VERSION": "1.1.1\n",
        "plugin-codex/plugins/synaptory/.codex-plugin/plugin.json": (
            '{"version":"1.1.1"}\n'
        ),
    }
    for relative, content in files.items():
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _run("git", "init", cwd=repository)
    _run("git", "config", "user.email", "release-test@example.invalid", cwd=repository)
    _run("git", "config", "user.name", "Release Test", cwd=repository)
    _run("git", "add", ".", cwd=repository)
    _run("git", "commit", "-m", "baseline", cwd=repository)

    for relative, content in files.items():
        (repository / relative).write_text(content.replace("1.1.1", "9.9.9"), encoding="utf-8")
    script = repo_root / "infra" / "scripts" / "stage-release-version.sh"
    _run("bash", str(script), cwd=repository)

    staged = set(
        _run("git", "diff", "--cached", "--name-only", cwd=repository).stdout.splitlines()
    )
    unstaged = set(_run("git", "diff", "--name-only", cwd=repository).stdout.splitlines())
    assert staged == set(files)
    assert "plugin-codex/VERSION" in staged
    assert "plugin-codex/plugins/synaptory/.codex-plugin/plugin.json" in staged
    assert not unstaged
