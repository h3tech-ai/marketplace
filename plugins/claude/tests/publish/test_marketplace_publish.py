"""Tests for the marketplace publish path in ``plugin-claude/synaptory`` (#232).

A failed ``git push`` must not report a published commit. Errexit is not in
force here: callers can reach the function through ``cmd_deploy … || { … }``,
and bash disables errexit for that call tree.

These tests drive the real ``_publish_plugin_to_git`` against a throwaway
``file://`` remote (standing in for GitHub) rather than re-implementing it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SYNAPTORY = REPO / "plugin-claude" / "synaptory"

pytestmark = pytest.mark.skipif(
    shutil.which("rsync") is None or shutil.which("git") is None,
    reason="publish path needs rsync + git",
)


def _fake_repo_root(tmp_path: Path) -> Path:
    """A minimal tree with the built marketplace output the function expects."""
    root = tmp_path / "repo"
    dist = root / "web" / "dist" / "marketplace" / "synaptory"
    dist.mkdir(parents=True)
    (dist / "README.md").write_text("published body\n", encoding="utf-8")
    hook = dist / "hooks" / "release-hook.sh"
    hook.parent.mkdir()
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)
    (root / "infra" / "git").mkdir(parents=True)
    return root


def _init_bare_remote(remote: Path) -> None:
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
        capture_output=True,
    )


def _seed_remote_with_hook_mode(
    tmp_path: Path, remote: Path, *, mode: int
) -> None:
    seed = tmp_path / f"seed-{remote.stem}"
    subprocess.run(
        ["git", "init", "-b", "main", str(seed)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(seed), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(seed), "config", "user.name", "Test"], check=True
    )
    hook = seed / "synaptory" / "hooks" / "release-hook.sh"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook.chmod(mode)
    subprocess.run(["git", "-C", str(seed), "add", "--all"], check=True)
    subprocess.run(
        ["git", "-C", str(seed), "commit", "-m", "seed"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(seed), "remote", "add", "origin", str(remote)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(seed), "push", "origin", "main"],
        check=True,
        capture_output=True,
    )


def _publish(root: Path, remote: Path) -> subprocess.CompletedProcess[str]:
    """Call the real ``_publish_plugin_to_git`` against ``remote``.

    The call goes through a wrapper invoked as ``wrapper || exit $?`` to
    reproduce the #232 errexit-disabled condition.
    """
    script = f"""
set -uo pipefail
source {SYNAPTORY!s} help >/dev/null 2>&1
REPO_ROOT={root!s}
CURRENT_VERSION="9.9.9"
_fake_deploy() {{
  _publish_plugin_to_git "file://{remote!s}" "test target"
}}
_fake_deploy || exit $?
exit 0
"""
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=str(root),
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(root),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
    )


def test_publish_fails_loudly_when_the_remote_is_missing(tmp_path: Path) -> None:
    """#232 regression: a failed push must not report a published commit."""
    root = _fake_repo_root(tmp_path)
    remote = root / "infra" / "git" / "missing-remote.git"

    result = _publish(root, remote)

    assert result.returncode != 0, (
        "publish reported success with no remote to push to:\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "Published commit" not in result.stdout, (
        "publish claimed to have published a commit after the push failed:\n"
        f"{combined}"
    )
    assert "failed" in combined.lower(), (
        f"error output does not say the push failed:\n{combined}"
    )


def test_publish_succeeds_and_the_remote_receives_the_tree(tmp_path: Path) -> None:
    """The happy path, so the test above cannot pass by publishing never working."""
    root = _fake_repo_root(tmp_path)
    remote = root / "infra" / "git" / "marketplace-remote.git"
    _init_bare_remote(remote)

    result = _publish(root, remote)

    assert result.returncode == 0, (
        f"publish failed against a valid remote:\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "Published commit" in result.stdout, result.stdout

    listed = subprocess.run(
        ["git", "-C", str(remote), "ls-tree", "-r", "--name-only", "refs/heads/main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "synaptory/README.md" in listed, (
        f"the published tree is not reachable from refs/heads/main:\n{listed}"
    )


def test_deploy_prod_publishes_primary_and_legacy_marketplaces() -> None:
    """Existing Claude users keep their legacy source while new installs use GitHub."""
    body = SYNAPTORY.read_text(encoding="utf-8")
    assert "_publish_plugin_to_github" in body
    assert "_publish_plugin_to_legacy" in body
    assert "h3tech-ai/marketplace" in body
    deploy_prod = body.split("_deploy_prod()", 1)[1].split("_stamp_image_tag()", 1)[0]
    assert "_publish_plugin_to_github" in deploy_prod
    assert "_publish_plugin_to_legacy" in deploy_prod
    assert deploy_prod.index("_publish_plugin_to_github") < deploy_prod.index(
        "_publish_plugin_to_legacy"
    )
    assert deploy_prod.index("_publish_plugin_to_legacy") < deploy_prod.index(
        "publishing CLI"
    )
    assert "file:///mnt/pgdata/git/marketplace.git" in body


def test_marketplace_mirrors_receive_identical_trees(tmp_path: Path) -> None:
    """Both transports must expose byte-identical built marketplace content."""
    root = _fake_repo_root(tmp_path)
    primary = root / "infra" / "git" / "primary.git"
    legacy = root / "infra" / "git" / "legacy.git"
    for remote in (primary, legacy):
        _init_bare_remote(remote)

    primary_result = _publish(root, primary)
    assert primary_result.returncode == 0, primary_result.stdout + primary_result.stderr

    script = f"""
set -uo pipefail
source {SYNAPTORY!s} help >/dev/null 2>&1
REPO_ROOT={root!s}
CURRENT_VERSION="9.9.9"
SYNAPTORY_LEGACY_MARKETPLACE_ORIGIN="file://{legacy!s}"
_publish_plugin_to_legacy || exit $?
"""
    legacy_result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=str(root),
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(root),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
    )
    assert legacy_result.returncode == 0, legacy_result.stdout + legacy_result.stderr

    primary_tree = subprocess.run(
        ["git", "-C", str(primary), "rev-parse", "refs/heads/main^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    legacy_tree = subprocess.run(
        ["git", "-C", str(legacy), "rev-parse", "refs/heads/main^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert primary_tree == legacy_tree


def test_marketplace_mirrors_normalize_existing_hook_mode_drift(
    tmp_path: Path,
) -> None:
    """Publishing must repair stale modes and produce identical executable hooks."""
    root = _fake_repo_root(tmp_path)
    primary = root / "infra" / "git" / "primary-mode.git"
    legacy = root / "infra" / "git" / "legacy-mode.git"
    for remote in (primary, legacy):
        _init_bare_remote(remote)
    _seed_remote_with_hook_mode(tmp_path, primary, mode=0o755)
    _seed_remote_with_hook_mode(tmp_path, legacy, mode=0o644)

    for remote in (primary, legacy):
        result = _publish(root, remote)
        assert result.returncode == 0, result.stdout + result.stderr

    trees = [
        subprocess.run(
            ["git", "-C", str(remote), "rev-parse", "refs/heads/main^{tree}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        for remote in (primary, legacy)
    ]
    assert trees[0] == trees[1]

    for remote in (primary, legacy):
        entry = subprocess.run(
            [
                "git",
                "-C",
                str(remote),
                "ls-tree",
                "refs/heads/main",
                "synaptory/hooks/release-hook.sh",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert entry.split()[0] == "100755", entry


def test_deploy_local_does_not_publish_to_marketplace_git() -> None:
    """Local deploy must not write either production marketplace."""
    body = SYNAPTORY.read_text(encoding="utf-8")
    deploy_local = body.split("_deploy_local()", 1)[1].split("_deploy_prod()", 1)[0]
    assert "init-marketplace-repo.sh" not in deploy_local
    assert "infra/git/marketplace.git" not in deploy_local
    assert "synaptory.h3t.co/marketplace.git" not in deploy_local
    assert "web/dist/marketplace" in deploy_local


def test_deploy_prod_bootstraps_compose_roll_from_release_workspace() -> None:
    """A stale prod checkout must not select the old deploy script first."""
    body = SYNAPTORY.read_text(encoding="utf-8")
    deploy_prod = body.split("_deploy_prod()", 1)[1].split("_stamp_image_tag()", 1)[0]
    assert 'local deploy_sh="${REPO_ROOT}/infra/scripts/deploy.sh"' in deploy_prod
    assert 'REPO_DIR="${prod_repo_dir}"' in deploy_prod
    assert 'local deploy_sh="${prod_repo_dir}/infra/scripts/deploy.sh"' not in deploy_prod


def test_github_publish_does_not_embed_pat_in_remote_url() -> None:
    """#249 review: origin must stay credential-free (GIT_ASKPASS, not x-access-token URL)."""
    body = SYNAPTORY.read_text(encoding="utf-8")
    assert "x-access-token:${token}" not in body
    assert "GIT_ASKPASS" in body
    assert "_marketplace_origin_url" in body
    assert "credential.helper" in body


def test_github_publish_askpass_cleans_up_and_preserves_exit_trap(tmp_path: Path) -> None:
    """Askpass lives in a subshell: successful exit, helper gone, parent trap intact."""
    root = _fake_repo_root(tmp_path)
    marker = tmp_path / "askpass-path.txt"
    script = f"""
set -uo pipefail
source {SYNAPTORY!s} help >/dev/null 2>&1
REPO_ROOT={root!s}
trap 'echo PARENT_TRAP_STILL_SET' EXIT
_publish_plugin_to_git() {{
  test -n "${{GIT_ASKPASS:-}}"
  test -x "${{GIT_ASKPASS:-}}"
  test -n "${{SYNAPTORY_MARKETPLACE_TOKEN:-}}"
  printf '%s' "$GIT_ASKPASS" > {marker!s}
  url="$(_marketplace_origin_url)"
  case "$url" in
    *x-access-token*) echo "token in origin" >&2; return 1 ;;
  esac
  return 0
}}
export RELEASE_BOT_PAT="not-a-real-token"
_publish_plugin_to_github
echo AFTER_TOKEN="${{SYNAPTORY_MARKETPLACE_TOKEN-UNSET}}"
echo AFTER_ASKPASS="${{GIT_ASKPASS-UNSET}}"
trap -p EXIT || true
exit 0
"""
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=str(root),
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(root),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "AFTER_TOKEN=UNSET" in result.stdout
    assert "AFTER_ASKPASS=UNSET" in result.stdout
    assert "PARENT_TRAP_STILL_SET" in combined
    if marker.is_file():
        leftover = marker.read_text().strip()
        assert leftover
        assert not Path(leftover).exists()
