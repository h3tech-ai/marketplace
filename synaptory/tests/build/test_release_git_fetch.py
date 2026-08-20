"""Release checkout authentication and bootstrap regressions."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _fake_git(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "fetch.txt"
    git = bin_dir / "git"
    git.write_text(
        """#!/bin/bash
set -eu
if [ "${1:-}" = remote ]; then
  printf '%s\\n' "${FAKE_ORIGIN_URL:?}"
  exit 0
fi
printf 'argv=%s\\n' "$*" > "${FAKE_CAPTURE:?}"
printf 'askpass=%s\\n' "${GIT_ASKPASS:-}" >> "${FAKE_CAPTURE:?}"
printf 'prompt=%s\\n' "${GIT_TERMINAL_PROMPT:-}" >> "${FAKE_CAPTURE:?}"
if [ -n "${GIT_ASKPASS:-}" ]; then
  printf 'username=%s\\n' "$("$GIT_ASKPASS" Username)" >> "${FAKE_CAPTURE:?}"
  printf 'password=%s\\n' "$("$GIT_ASKPASS" Password)" >> "${FAKE_CAPTURE:?}"
  printf 'helper=%s\\n' "$GIT_ASKPASS" >> "${FAKE_CAPTURE:?}"
fi
""",
        encoding="utf-8",
    )
    git.chmod(0o755)
    return bin_dir, capture


def _run(
    repo_root: Path,
    tmp_path: Path,
    *,
    origin: str,
    token: str = "",
    repo_token: str = "",
):
    bin_dir, capture = _fake_git(tmp_path)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "FAKE_ORIGIN_URL": origin,
        "FAKE_CAPTURE": str(capture),
    }
    if token:
        env["RELEASE_BOT_PAT"] = token
    if repo_token:
        env["SYNAPTORY_REPO_TOKEN"] = repo_token
    script = repo_root / "infra" / "scripts" / "git-fetch-authenticated.sh"
    result = subprocess.run(
        ["bash", str(script), "origin", "main"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )
    return result, capture.read_text(encoding="utf-8")


def test_https_github_fetch_uses_ephemeral_askpass(repo_root: Path, tmp_path: Path):
    secret = "release-test-secret"
    result, capture = _run(
        repo_root,
        tmp_path,
        origin="https://github.com/h3tech-ai/synaptory.git",
        token=secret,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "argv=-c credential.helper= fetch --prune origin main" in capture
    assert "prompt=0" in capture
    assert "username=x-access-token" in capture
    assert f"password={secret}" in capture
    assert secret not in result.stdout + result.stderr
    helper = Path(capture.split("helper=", 1)[1].splitlines()[0])
    assert not helper.exists()


def test_fetch_without_token_uses_normal_git_behavior(repo_root: Path, tmp_path: Path):
    result, capture = _run(
        repo_root,
        tmp_path,
        origin="https://github.com/h3tech-ai/synaptory.git",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "argv=fetch --prune origin main" in capture
    assert "askpass=" in capture
    assert "username=" not in capture


def test_current_repo_token_wins_over_marketplace_release_pat(
    repo_root: Path, tmp_path: Path
):
    repo_secret = "current-repo-token"
    marketplace_secret = "marketplace-release-token"
    result, capture = _run(
        repo_root,
        tmp_path,
        origin="https://github.com/h3tech-ai/synaptory.git",
        token=marketplace_secret,
        repo_token=repo_secret,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"password={repo_secret}" in capture
    assert marketplace_secret not in capture + result.stdout + result.stderr


def test_release_workflow_passes_scoped_repo_token(repo_root: Path):
    workflow = (repo_root / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert 'SYNAPTORY_REPO_TOKEN="${{ github.token }}"' in workflow


def test_non_github_remote_never_receives_pat(repo_root: Path, tmp_path: Path):
    secret = "release-test-secret"
    result, capture = _run(
        repo_root,
        tmp_path,
        origin="file:///mnt/pgdata/git/synaptory.git",
        token=secret,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "argv=fetch --prune origin main" in capture
    assert secret not in capture + result.stdout + result.stderr
