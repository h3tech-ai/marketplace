"""Back-merge of a release from `main` into `dev` (issue #439).

The step this replaces reported success while merging nothing: it ran
`git merge` with no conflict strategy, aborted on conflict, printed a
`::warning::`, and was wrapped in `continue-on-error: true`. v1.1.5, v1.2.0 and
v1.2.1 all drifted that way behind a green run.

So the assertions that matter here are the ones about the FAILURE path: that a
conflict exits non-zero, that `dev` ends up carrying the released VERSION
anyway, and that repeating the run does not stack commits.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=check
    )
    return result.stdout


def _write(repo: Path, relative: str, content: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def backmerge_script(repo_root: Path) -> Path:
    return repo_root / "infra" / "scripts" / "backmerge-release-to-dev.sh"


@pytest.fixture
def remote_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """A bare `origin` with `main` and `dev`, plus a working clone.

    `main` carries a released v1.1.0; `dev` is still on 1.0.0 and has its own
    unreleased work, which is the state every release starts from.
    """
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "backmerge-test@example.invalid")
    _git(seed, "config", "user.name", "Backmerge Test")
    _write(seed, "VERSION", "1.0.0\n")
    _write(seed, "web/package.json", json.dumps({"name": "web", "version": "1.0.0"}, indent=2) + "\n")
    _write(seed, "core/shared.py", "BASE = 1\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "baseline 1.0.0")
    _git(seed, "branch", "dev")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "origin", "main", "dev")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(clone))
    _git(clone, "config", "user.email", "backmerge-test@example.invalid")
    _git(clone, "config", "user.name", "Backmerge Test")
    return origin, clone


def _release_on_main(clone: Path, version: str) -> None:
    _git(clone, "checkout", "main")
    _write(clone, "VERSION", f"{version}\n")
    _write(
        clone,
        "web/package.json",
        json.dumps({"name": "web", "version": version}, indent=2) + "\n",
    )
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", f"chore(release): v{version} [skip ci]")
    _git(clone, "push", "origin", "main")
    _git(clone, "checkout", "--detach")


def _work_on_dev(clone: Path, shared_body: str, message: str) -> None:
    _git(clone, "checkout", "-B", "dev", "origin/dev")
    _write(clone, "core/shared.py", shared_body)
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", message)
    _git(clone, "push", "origin", "dev")
    _git(clone, "checkout", "--detach")


def _run(script: Path, clone: Path, version: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), version],
        cwd=clone,
        capture_output=True,
        text=True,
        env={**os.environ, "BACKMERGE_SKIP_PR": "1"},
    )


def _remote_file(origin: Path, ref: str, path: str) -> str:
    return _git(origin, "show", f"{ref}:{path}")


def test_clean_back_merge_lands_on_dev(
    backmerge_script: Path, remote_and_clone: tuple[Path, Path]
):
    origin, clone = remote_and_clone
    _release_on_main(clone, "1.1.0")
    _work_on_dev(clone, "BASE = 1\nDEV_ONLY = True\n", "dev-only work")

    result = _run(backmerge_script, clone, "1.1.0")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _remote_file(origin, "dev", "VERSION").strip() == "1.1.0"
    # --no-ff means the successful path always leaves one greppable commit.
    # Investigating #439 began with exactly this grep returning nothing.
    subjects = _git(origin, "log", "dev", "--pretty=%s")
    assert "chore: back-merge v1.1.0 into dev" in subjects
    # dev's own work survives the merge.
    assert "DEV_ONLY" in _remote_file(origin, "dev", "core/shared.py")


def test_second_run_is_a_no_op(
    backmerge_script: Path, remote_and_clone: tuple[Path, Path]
):
    origin, clone = remote_and_clone
    _release_on_main(clone, "1.1.0")
    _work_on_dev(clone, "BASE = 1\nDEV_ONLY = True\n", "dev-only work")

    assert _run(backmerge_script, clone, "1.1.0").returncode == 0
    first_tip = _git(origin, "rev-parse", "dev").strip()

    second = _run(backmerge_script, clone, "1.1.0")

    assert second.returncode == 0, second.stdout + second.stderr
    assert "nothing to do" in second.stdout
    assert _git(origin, "rev-parse", "dev").strip() == first_tip


def test_conflict_fails_loudly_and_still_syncs_the_version(
    backmerge_script: Path, remote_and_clone: tuple[Path, Path]
):
    """The v1.2.0 shape. The old step printed a warning here and reported success."""
    origin, clone = remote_and_clone
    # Both branches rewrite the same line, so the merge cannot resolve itself.
    _git(clone, "checkout", "-B", "main", "origin/main")
    _write(clone, "core/shared.py", "BASE = 2  # release side\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "release-side runtime change")
    _git(clone, "push", "origin", "main")
    _git(clone, "checkout", "--detach")
    _release_on_main(clone, "1.2.0")
    _work_on_dev(clone, "BASE = 3  # dev side\n", "epic work on dev")

    result = _run(backmerge_script, clone, "1.2.0")

    assert result.returncode == 1, (
        "a conflicting back-merge must fail, not warn:\n"
        + result.stdout
        + result.stderr
    )
    assert "did not complete" in result.stdout

    # The dangerous half is fixed even though the merge is not: dev must never
    # sit below the version production serves.
    assert _remote_file(origin, "dev", "VERSION").strip() == "1.2.0"
    assert json.loads(_remote_file(origin, "dev", "web/package.json"))["version"] == "1.2.0"
    # dev's conflicting runtime is left alone for the human to reconcile.
    assert "dev side" in _remote_file(origin, "dev", "core/shared.py")

    # A branch a human can open, resolve and merge.
    branches = _git(origin, "branch", "--list", "chore/back-merge-v1.2.0-into-dev")
    assert "chore/back-merge-v1.2.0-into-dev" in branches
    assert (
        _git(origin, "rev-parse", "chore/back-merge-v1.2.0-into-dev").strip()
        == _git(origin, "rev-parse", "main").strip()
    )


def test_repeated_conflict_run_does_not_stack_commits(
    backmerge_script: Path, remote_and_clone: tuple[Path, Path]
):
    """Retry-safety on the failure path.

    A release that needed retries runs this more than once. The version sync
    must not add a second identical commit, and the reconciliation branch must
    be updated rather than duplicated.
    """
    origin, clone = remote_and_clone
    _git(clone, "checkout", "-B", "main", "origin/main")
    _write(clone, "core/shared.py", "BASE = 2  # release side\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "release-side runtime change")
    _git(clone, "push", "origin", "main")
    _git(clone, "checkout", "--detach")
    _release_on_main(clone, "1.2.0")
    _work_on_dev(clone, "BASE = 3  # dev side\n", "epic work on dev")

    assert _run(backmerge_script, clone, "1.2.0").returncode == 1
    tip_after_first = _git(origin, "rev-parse", "dev").strip()

    second = _run(backmerge_script, clone, "1.2.0")

    assert second.returncode == 1
    assert "already carries VERSION 1.2.0" in second.stdout
    assert _git(origin, "rev-parse", "dev").strip() == tip_after_first
    sync_commits = [
        line
        for line in _git(origin, "log", "dev", "--pretty=%s").splitlines()
        if line.startswith("chore: sync dev to released VERSION")
    ]
    assert len(sync_commits) == 1, sync_commits
