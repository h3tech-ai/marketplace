"""Layer 1 — `plugin-claude/hooks/lib/worktree_manager.py` lifecycle tests.

Hypothesis: per-story worktrees have a deterministic, orchestrator-managed
lifecycle — create before dispatch, merge after receipt verification,
remove after merge. These tests pin the contract:

  * create → path/branch/workspace_ref shape, info/exclude updated,
    idempotent re-run, tracked files untouched.
  * commit in worktree → merge lands the file on main, worktree + branch
    are cleaned up (exit 0).
  * conflicting edits → merge aborted, main tree clean (no MERGE_HEAD),
    worktree intact (exit 3).
  * dirty worktree → merge refused, never auto-committed (exit 2).
  * remove --force clears a dirty worktree; list/--stale reports state.
  * path safety: traversal story ids are refused before any git call.

Uses real temp git repos (git subprocess) — worktree/merge mechanics ARE
the behavior under test.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from hooks.lib import worktree_manager as wm

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


# ─── Helpers / fixtures ───────────────────────────────────────────────────────


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _commit_file(repo: Path, relpath: str, content: str, message: str) -> None:
    p = repo / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    _run_git(repo, "add", relpath)
    _run_git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Fresh git repo (branch `main`) with one committed file."""
    repo = tmp_path / "project"
    repo.mkdir()
    _run_git(repo, "init", "-q", "-b", "main")
    _run_git(repo, "config", "user.email", "test@synaptory.test")
    _run_git(repo, "config", "user.name", "Synaptory Tests")
    _run_git(repo, "config", "commit.gpgsign", "false")
    _commit_file(repo, "app.txt", "line-1\n", "initial")
    return repo


def _worktree_paths(repo: Path) -> list[str]:
    out = _run_git(repo, "worktree", "list", "--porcelain").stdout
    return [ln[len("worktree "):] for ln in out.splitlines() if ln.startswith("worktree ")]


def _branch_names(repo: Path) -> set[str]:
    out = _run_git(repo, "branch", "--format=%(refname:short)").stdout
    return set(out.split())


STORY = "US-042"


# ─── create ───────────────────────────────────────────────────────────────────


def test_create_shape_and_layout(repo: Path):
    result = wm.create_worktree(str(repo), STORY)
    assert result.get("error") is None
    assert result["created"] is True
    assert result["branch"] == f"synaptory/{STORY}"
    assert result["workspace_ref"] == f"wt://synaptory/{STORY}"
    wt = Path(result["path"])
    assert wt.is_dir()
    assert wt == (repo / ".synaptory" / ".worktrees" / STORY).resolve()
    assert f"synaptory/{STORY}" in _branch_names(repo)
    # Worktree contains the committed tree.
    assert (wt / "app.txt").read_text(encoding="utf-8") == "line-1\n"


def test_create_updates_info_exclude_not_tracked_files(repo: Path):
    wm.create_worktree(str(repo), STORY)
    exclude = repo / ".git" / "info" / "exclude"
    assert ".synaptory/.worktrees/" in exclude.read_text(encoding="utf-8").splitlines()
    # No tracked file (e.g. .gitignore) was touched: main tree status is clean.
    status = _run_git(repo, "status", "--porcelain").stdout
    assert status.strip() == ""


def test_create_is_idempotent(repo: Path):
    first = wm.create_worktree(str(repo), STORY)
    second = wm.create_worktree(str(repo), STORY)
    assert second.get("error") is None
    assert second["created"] is False
    assert second["path"] == first["path"]
    assert second["branch"] == first["branch"]
    # info/exclude not duplicated.
    exclude_lines = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    assert exclude_lines.count(".synaptory/.worktrees/") == 1
    # Still exactly one extra worktree registered.
    assert len(_worktree_paths(repo)) == 2  # main tree + story worktree


def test_create_reuses_existing_branch_after_worktree_removed(repo: Path):
    created = wm.create_worktree(str(repo), STORY)
    _run_git(repo, "worktree", "remove", created["path"])
    again = wm.create_worktree(str(repo), STORY)
    assert again.get("error") is None
    assert again["created"] is True
    assert again["branch"] == f"synaptory/{STORY}"


# ─── merge (happy path) ───────────────────────────────────────────────────────


def test_merge_lands_commit_and_cleans_up(repo: Path):
    created = wm.create_worktree(str(repo), STORY)
    wt = Path(created["path"])
    _commit_file(wt, "feature.txt", "built by SE\n", f"{STORY}: add feature")

    result = wm.merge_worktree(str(repo), STORY)
    assert result["merged"] is True
    head = _run_git(repo, "rev-parse", "HEAD").stdout.strip()
    assert result["commit"] == head
    # File is on main; merge commit message is deterministic.
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "built by SE\n"
    subject = _run_git(repo, "log", "-1", "--format=%s").stdout.strip()
    assert subject == f"merge({STORY}): story worktree"
    # Worktree + branch are gone.
    assert not wt.exists()
    assert created["path"] not in _worktree_paths(repo)
    assert f"synaptory/{STORY}" not in _branch_names(repo)


def test_merge_no_remove_keeps_worktree_and_branch(repo: Path):
    created = wm.create_worktree(str(repo), STORY)
    _commit_file(Path(created["path"]), "feature.txt", "x\n", "work")
    result = wm.merge_worktree(str(repo), STORY, remove=False)
    assert result["merged"] is True
    assert Path(created["path"]).is_dir()
    assert f"synaptory/{STORY}" in _branch_names(repo)


# ─── merge (conflict) ─────────────────────────────────────────────────────────


def test_merge_conflict_aborts_and_leaves_main_clean(repo: Path):
    created = wm.create_worktree(str(repo), STORY)
    wt = Path(created["path"])
    # Same line changed on main and in the worktree.
    _commit_file(wt, "app.txt", "line-1-worktree\n", "worktree edit")
    _commit_file(repo, "app.txt", "line-1-main\n", "main edit")

    result = wm.merge_worktree(str(repo), STORY)
    assert result["merged"] is False
    assert result["conflict"] is True
    assert "app.txt" in result["files"]
    # Merge was aborted: no MERGE_HEAD, main tree clean, main content intact.
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert _run_git(repo, "status", "--porcelain").stdout.strip() == ""
    assert (repo / "app.txt").read_text(encoding="utf-8") == "line-1-main\n"
    # Worktree left intact for manual resolution.
    assert wt.is_dir()
    assert f"synaptory/{STORY}" in _branch_names(repo)


# ─── commit (orchestrator-owned handoff, #170 review) ─────────────────────────


def test_commit_then_merge_lands_se_changes(repo: Path):
    """The real flow: SE leaves the worktree dirty, orchestrator commits, then
    merge lands the work on main."""
    created = wm.create_worktree(str(repo), STORY)
    wt = Path(created["path"])
    # SE produces code but does NOT commit.
    (wt / "feature.txt").write_text("built by SE\n", encoding="utf-8")

    committed = wm.commit_worktree(str(repo), STORY)
    assert committed["committed"] is True
    assert "feature.txt" in committed["files"]
    # The audited commit carries the provenance trailer.
    body = _run_git(wt, "log", "-1", "--format=%B").stdout
    assert f"Synaptory-Story: {STORY}" in body

    result = wm.merge_worktree(str(repo), STORY)
    assert result["merged"] is True
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "built by SE\n"


def test_commit_clean_worktree_is_noop(repo: Path):
    wm.create_worktree(str(repo), STORY)
    result = wm.commit_worktree(str(repo), STORY)
    assert result["committed"] is False
    assert result["reason"] == "clean"
    assert not result.get("error")


def test_commit_rejects_traversal_story_id(repo: Path):
    result = wm.commit_worktree(str(repo), "../../evil")
    assert result.get("error") is True


# ─── merge (dirty worktree refused) ───────────────────────────────────────────


def test_merge_refuses_dirty_worktree(repo: Path):
    created = wm.create_worktree(str(repo), STORY)
    wt = Path(created["path"])
    (wt / "app.txt").write_text("uncommitted change\n", encoding="utf-8")
    (wt / "untracked.txt").write_text("also dirty\n", encoding="utf-8")

    result = wm.merge_worktree(str(repo), STORY)
    assert result["merged"] is False
    assert result["reason"] == "worktree_dirty"
    assert set(result["files"]) == {"app.txt", "untracked.txt"}
    # Nothing was committed or merged.
    assert (repo / "app.txt").read_text(encoding="utf-8") == "line-1\n"
    assert wt.is_dir()


def test_merge_missing_branch_errors(repo: Path):
    result = wm.merge_worktree(str(repo), "US-999")
    assert result.get("error") is True
    assert "does not exist" in result["reason"]


# ─── remove ───────────────────────────────────────────────────────────────────


def test_remove_clean_worktree(repo: Path):
    created = wm.create_worktree(str(repo), STORY)
    result = wm.remove_worktree(str(repo), STORY)
    assert result.get("error") is None
    assert result["removed"] is True
    assert result["branch_deleted"] is True
    assert not Path(created["path"]).exists()
    assert f"synaptory/{STORY}" not in _branch_names(repo)


def test_remove_dirty_needs_force(repo: Path):
    created = wm.create_worktree(str(repo), STORY)
    wt = Path(created["path"])
    (wt / "scratch.txt").write_text("dirty\n", encoding="utf-8")

    refused = wm.remove_worktree(str(repo), STORY, force=False)
    assert refused.get("error") is True
    assert wt.is_dir()

    forced = wm.remove_worktree(str(repo), STORY, force=True)
    assert forced.get("error") is None
    assert forced["removed"] is True
    assert forced["branch_deleted"] is True
    assert not wt.exists()


# ─── list / --stale ───────────────────────────────────────────────────────────


def test_list_reports_managed_worktrees_only(repo: Path):
    wm.create_worktree(str(repo), "US-001")
    wm.create_worktree(str(repo), "US-002")
    # An unmanaged worktree elsewhere must not be reported.
    _run_git(repo, "worktree", "add", str(repo.parent / "unmanaged"), "-b", "other", "HEAD")

    result = wm.list_worktrees(str(repo))
    entries = {e["branch"]: e for e in result["worktrees"]}
    assert set(entries) == {"synaptory/US-001", "synaptory/US-002"}
    for e in entries.values():
        assert e["dir_exists"] is True
        assert e["merged"] is True  # branch tip == HEAD → contained in HEAD


def test_list_stale_filters_to_merged_or_missing(repo: Path):
    # US-001: has an unmerged commit → not stale.
    a = wm.create_worktree(str(repo), "US-001")
    _commit_file(Path(a["path"]), "a.txt", "a\n", "US-001 work")
    # US-002: branch tip == HEAD (merged) → stale.
    wm.create_worktree(str(repo), "US-002")
    # US-003: directory deleted out from under git → stale.
    c = wm.create_worktree(str(repo), "US-003")
    shutil.rmtree(c["path"])

    stale = wm.list_worktrees(str(repo), stale_only=True)
    branches = {e["branch"] for e in stale["worktrees"]}
    assert branches == {"synaptory/US-002", "synaptory/US-003"}
    everything = wm.list_worktrees(str(repo))
    assert {e["branch"] for e in everything["worktrees"]} == {
        "synaptory/US-001",
        "synaptory/US-002",
        "synaptory/US-003",
    }


# ─── path safety ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_id",
    ["../../evil", "..", "a/../..", "-rf", ".hidden", "a b", "", "US-001/../../../evil"],
)
def test_traversal_story_ids_refused(repo: Path, bad_id: str):
    for fn in (wm.create_worktree, wm.remove_worktree, wm.merge_worktree):
        result = fn(str(repo), bad_id)
        assert result.get("error") is True, (fn.__name__, bad_id)
    # Nothing escaped the repo, nothing was created.
    assert not (repo.parent / "evil").exists()
    assert len(_worktree_paths(repo)) == 1  # main tree only


def test_remove_never_targets_outside_managed_dir(repo: Path):
    """Even a valid-looking id must resolve under .synaptory/.worktrees/."""
    victim = repo.parent / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep\n", encoding="utf-8")
    result = wm.remove_worktree(str(repo), "../../victim")
    assert result.get("error") is True
    assert (victim / "keep.txt").exists()


# ─── CLI surface ──────────────────────────────────────────────────────────────


def test_cli_create_merge_exit_codes_and_json(repo: Path, capsys):
    rc = wm.main(["create", "--project-dir", str(repo), "--story-id", STORY])
    payload = json.loads(capsys.readouterr().out)
    assert rc == wm.EXIT_OK
    assert payload["workspace_ref"] == f"wt://synaptory/{STORY}"

    # Dirty worktree → exit 2.
    wt = Path(payload["path"])
    (wt / "app.txt").write_text("dirty\n", encoding="utf-8")
    rc = wm.main(["merge", "--project-dir", str(repo), "--story-id", STORY])
    dirty = json.loads(capsys.readouterr().out)
    assert rc == wm.EXIT_DIRTY
    assert dirty["reason"] == "worktree_dirty"

    # Commit + conflicting main edit → exit 3.
    _run_git(wt, "add", "app.txt")
    _run_git(wt, "commit", "-q", "-m", "worktree edit")
    _commit_file(repo, "app.txt", "main-edit\n", "main edit")
    rc = wm.main(["merge", "--project-dir", str(repo), "--story-id", STORY])
    conflict = json.loads(capsys.readouterr().out)
    assert rc == wm.EXIT_CONFLICT
    assert conflict["conflict"] is True

    # Bad story id → exit 1.
    rc = wm.main(["create", "--project-dir", str(repo), "--story-id", "../../evil"])
    err = json.loads(capsys.readouterr().out)
    assert rc == wm.EXIT_ERROR
    assert err["error"] is True


def test_cli_list_and_remove(repo: Path, capsys):
    wm.main(["create", "--project-dir", str(repo), "--story-id", STORY])
    capsys.readouterr()

    rc = wm.main(["list", "--project-dir", str(repo)])
    listing = json.loads(capsys.readouterr().out)
    assert rc == wm.EXIT_OK
    assert [e["branch"] for e in listing["worktrees"]] == [f"synaptory/{STORY}"]

    rc = wm.main(["remove", "--project-dir", str(repo), "--story-id", STORY, "--force"])
    removed = json.loads(capsys.readouterr().out)
    assert rc == wm.EXIT_OK
    assert removed["removed"] is True

    rc = wm.main(["list", "--project-dir", str(repo), "--stale"])
    empty = json.loads(capsys.readouterr().out)
    assert rc == wm.EXIT_OK
    assert empty["worktrees"] == []
