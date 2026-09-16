"""The independent version-drift guard (issue #439).

The back-merge can be broken without anyone noticing -- that is the whole
incident. This check is what fires anyway, so it has to be proven to fire.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _commit_version(repo: Path, version: str, message: str) -> None:
    (repo / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    _git(repo, "add", "VERSION")
    _git(repo, "commit", "-m", message)


@pytest.fixture
def drift_repo(tmp_path: Path) -> Path:
    """A repo with `origin/main` and `origin/dev` both at 1.0.0."""
    repo = tmp_path / "drift-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "drift-test@example.invalid")
    _git(repo, "config", "user.name", "Drift Test")
    _commit_version(repo, "1.0.0", "baseline")

    # Local refs under refs/remotes/origin/ stand in for a real remote; the
    # script only ever reads them.
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/dev", "HEAD")
    return repo


def _run_check(repo: Path, script: Path, **env_extra: str):
    env = {**os.environ, **env_extra}
    return subprocess.run(
        ["python3", str(script), "--repo", str(repo)],
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture
def drift_script(repo_root: Path) -> Path:
    return repo_root / "infra" / "scripts" / "check-version-drift.py"


def test_no_drift_when_both_branches_match(drift_repo: Path, drift_script: Path):
    result = _run_check(drift_repo, drift_script)
    assert result.returncode == 0, result.stderr
    assert "OK:" in result.stdout


def test_drift_fires_when_main_is_ahead(
    drift_repo: Path, drift_script: Path, tmp_path: Path
):
    """The v1.2.0 shape exactly: main released, dev never got the back-merge."""
    _commit_version(drift_repo, "1.2.0", "chore(release): v1.2.0 [skip ci]")
    _git(drift_repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    # dev stays where it was -- this is the drift.
    _git(drift_repo, "reset", "--hard", "refs/remotes/origin/dev")

    github_output = tmp_path / "gh-output"
    github_output.touch()
    result = _run_check(
        drift_repo, drift_script, GITHUB_OUTPUT=str(github_output)
    )

    assert result.returncode == 1, (
        "main at 1.2.0 with dev at 1.0.0 must fail the check; "
        f"got {result.returncode}\n{result.stdout}\n{result.stderr}"
    )
    assert "::error::Version drift" in result.stdout
    emitted = github_output.read_text(encoding="utf-8")
    assert "drift=true" in emitted
    assert "release_version=1.2.0" in emitted
    assert "integration_version=1.0.0" in emitted


def test_no_drift_when_dev_is_ahead(drift_repo: Path, drift_script: Path):
    """The normal state mid-sprint: dev carries unreleased work."""
    _commit_version(drift_repo, "1.1.0", "wip on dev")
    _git(drift_repo, "update-ref", "refs/remotes/origin/dev", "HEAD")

    result = _run_check(drift_repo, drift_script)
    assert result.returncode == 0, result.stdout + result.stderr


def test_unmerged_main_alone_is_not_drift(
    drift_repo: Path, drift_script: Path, tmp_path: Path
):
    """Ancestry is reported, never fatal on its own.

    A squash-merged hotfix leaves `main` un-ancestored on `dev` forever while
    the VERSION is perfectly in step. Failing on that would make the check
    permanently red, which is how a guard gets ignored.
    """
    _git(drift_repo, "checkout", "-b", "sidebranch")
    (drift_repo / "unrelated.txt").write_text("x\n", encoding="utf-8")
    _git(drift_repo, "add", "unrelated.txt")
    _git(drift_repo, "commit", "-m", "main-only commit, same VERSION")
    _git(drift_repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    github_output = tmp_path / "gh-output"
    github_output.touch()
    result = _run_check(
        drift_repo, drift_script, GITHUB_OUTPUT=str(github_output)
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "merged into origin/dev = no" in result.stdout
    assert "drift=false" in github_output.read_text(encoding="utf-8")
