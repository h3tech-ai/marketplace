"""Layer 1 — `plugin-claude/hooks/lib/verification_cache.py` replay-dedupe tests.

Hypothesis: verification replays are cached keyed on (command, tree state),
and ANY tree change — a dirty file or a new commit — invalidates every
entry via the key. These tests pin the contract:

  * store → lookup hit at the same tree state.
  * dirty tracked file or new commit → miss.
  * non-git dir → caching disabled (tree_state None, lookup None,
    store is a silent no-op).
  * corrupt cache file → graceful empty cache, never a crash.
  * writes are atomic — the cache file is always valid JSON.
  * prune caps entry count and drops stale entries; clear deletes the file.

Uses real temp git repos (git subprocess) — the tree-state digest IS the
behavior under test, so stubbing git would test nothing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from hooks.lib import verification_cache as vc

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


# ─── Fixtures ─────────────────────────────────────────────────────────────────


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Fresh git repo with one tracked, committed file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "test@synaptory.test")
    _run_git(repo, "config", "user.name", "Synaptory Tests")
    _run_git(repo, "config", "commit.gpgsign", "false")
    (repo / "hello.txt").write_text("hello\n", encoding="utf-8")
    _run_git(repo, "add", "hello.txt")
    _run_git(repo, "commit", "-q", "-m", "initial")
    return repo


RESULT = {"passed": True, "actual_exit_code": 0, "command": "pytest -q"}
COMMAND = "pytest -q"


# ─── tree_state ───────────────────────────────────────────────────────────────


def test_tree_state_is_stable_and_hex(git_repo: Path):
    a = vc.tree_state(str(git_repo))
    b = vc.tree_state(str(git_repo))
    assert a is not None and a == b
    int(a, 16)  # sha256 hex digest
    assert len(a) == 64


def test_tree_state_none_outside_git(tmp_path: Path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert vc.tree_state(str(plain)) is None


# ─── store / lookup ───────────────────────────────────────────────────────────


def test_store_then_lookup_hit(git_repo: Path):
    vc.store(str(git_repo), COMMAND, RESULT)
    assert vc.lookup(str(git_repo), COMMAND) == RESULT


def test_lookup_misses_other_command(git_repo: Path):
    vc.store(str(git_repo), COMMAND, RESULT)
    assert vc.lookup(str(git_repo), "pytest -q -x") is None


def test_dirty_tracked_file_invalidates(git_repo: Path):
    vc.store(str(git_repo), COMMAND, RESULT)
    (git_repo / "hello.txt").write_text("hello, dirty\n", encoding="utf-8")
    assert vc.lookup(str(git_repo), COMMAND) is None


def test_new_commit_invalidates(git_repo: Path):
    vc.store(str(git_repo), COMMAND, RESULT)
    (git_repo / "world.txt").write_text("world\n", encoding="utf-8")
    _run_git(git_repo, "add", "world.txt")
    _run_git(git_repo, "commit", "-q", "-m", "second")
    assert vc.lookup(str(git_repo), COMMAND) is None


def test_hit_returns_after_reverting_to_same_state(git_repo: Path):
    """Same tree state → same key: dirtying then restoring re-enables the hit."""
    vc.store(str(git_repo), COMMAND, RESULT)
    (git_repo / "hello.txt").write_text("hello, dirty\n", encoding="utf-8")
    assert vc.lookup(str(git_repo), COMMAND) is None
    (git_repo / "hello.txt").write_text("hello\n", encoding="utf-8")
    assert vc.lookup(str(git_repo), COMMAND) == RESULT


# ─── caching disabled outside git ─────────────────────────────────────────────


def test_non_git_dir_disables_caching(tmp_path: Path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    # store is a no-op, lookup a miss — and neither crashes.
    vc.store(str(plain), COMMAND, RESULT)
    assert vc.lookup(str(plain), COMMAND) is None
    assert not Path(vc.cache_path(str(plain))).exists()


# ─── corruption / atomicity ───────────────────────────────────────────────────


def test_corrupt_cache_file_is_graceful(git_repo: Path):
    cache_file = Path(vc.cache_path(str(git_repo)))
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text("{not json at all", encoding="utf-8")
    assert vc.lookup(str(git_repo), COMMAND) is None
    # store recovers by rebuilding an empty cache around the new entry.
    vc.store(str(git_repo), COMMAND, RESULT)
    assert vc.lookup(str(git_repo), COMMAND) == RESULT


def test_wrong_shape_cache_file_is_graceful(git_repo: Path):
    cache_file = Path(vc.cache_path(str(git_repo)))
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert vc.lookup(str(git_repo), COMMAND) is None


def test_store_writes_valid_json_atomically(git_repo: Path):
    vc.store(str(git_repo), COMMAND, RESULT)
    cache_file = Path(vc.cache_path(str(git_repo)))
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert isinstance(data["entries"], dict)
    (entry,) = data["entries"].values()
    assert entry["command"] == COMMAND
    assert entry["result"] == RESULT
    assert entry["tree_state"] == vc.tree_state(str(git_repo))
    assert "stored_at" in entry
    # No leftover tmp files from the atomic-write dance.
    leftovers = [p for p in cache_file.parent.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


# ─── prune / clear ────────────────────────────────────────────────────────────


def test_prune_caps_entries_keeping_newest(git_repo: Path):
    for i in range(10):
        vc.store(str(git_repo), f"pytest -q tests/test_{i}.py", {"passed": True, "i": i})
    vc.prune(str(git_repo), max_entries=3)
    data = json.loads(Path(vc.cache_path(str(git_repo))).read_text(encoding="utf-8"))
    entries = list(data["entries"].values())
    assert len(entries) == 3
    # Newest entries survive.
    assert sorted(e["result"]["i"] for e in entries) == [7, 8, 9]


def test_prune_drops_stale_entries(git_repo: Path):
    vc.store(str(git_repo), COMMAND, RESULT)
    cache_file = Path(vc.cache_path(str(git_repo)))
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    for entry in data["entries"].values():
        entry["stored_at"] = "2020-01-01T00:00:00+00:00"
    cache_file.write_text(json.dumps(data), encoding="utf-8")
    vc.prune(str(git_repo), max_age_days=14)
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert data["entries"] == {}


def test_store_prunes_beyond_default_cap(git_repo: Path, monkeypatch):
    monkeypatch.setattr(vc, "DEFAULT_MAX_ENTRIES", 4)
    for i in range(8):
        vc.store(str(git_repo), f"pytest -q tests/test_{i}.py", {"i": i})
    data = json.loads(Path(vc.cache_path(str(git_repo))).read_text(encoding="utf-8"))
    assert len(data["entries"]) == 4


def test_clear_removes_file_and_is_idempotent(git_repo: Path):
    vc.store(str(git_repo), COMMAND, RESULT)
    cache_file = Path(vc.cache_path(str(git_repo)))
    assert cache_file.exists()
    vc.clear(str(git_repo))
    assert not cache_file.exists()
    vc.clear(str(git_repo))  # second clear must not raise
    assert vc.lookup(str(git_repo), COMMAND) is None
