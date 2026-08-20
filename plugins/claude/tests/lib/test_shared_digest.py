"""Layer 1 — `scripts/shared-digest.sh` determinism (SPQ Sync criterion 4, #209).

The SPQ Sync barrier compares each workstream's `shared_digests` against the
integrated tree (design doc §8.4 criterion 4). The digest is produced by ONE
committed script invoked twice — by `sync_barrier.py declare-ready` in the
workstream clone and by `sync_barrier.py evaluate` in the integration clone —
so any behavioural divergence between two working copies of the same tree
yields a barrier failure nobody can reproduce (§8.3).

These tests pin the four properties that divergence would come from:

  * same tree, two invocations         -> same digest
  * content change                     -> digest MOVES
  * mtime change (touch)               -> digest does NOT move
  * mode-bit change (chmod)            -> digest does NOT move
  * untracked file present             -> digest does NOT move

Fixtures are real throwaway git repos under `tmp_path` (`git init` + `git add`,
no commit needed — the file set comes from the index via `git ls-files`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent.parent
DIGEST_SCRIPT = REPO_ROOT / "scripts" / "shared-digest.sh"

# sha256 of zero bytes — what an input path with no tracked files hashes to.
EMPTY_LISTING_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available"
)


# ─── fixture helpers ──────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git in `repo` with an env that ignores the developer's own config.

    `HOME` is already redirected to tmp_path by conftest's autouse isolation
    fixture, but GIT_CONFIG_GLOBAL/SYSTEM are pinned explicitly so a global
    `core.quotePath` or `core.autocrlf` cannot leak into a fixture and make
    these assertions machine-dependent.
    """
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )


def _make_repo(root: Path, files: dict[str, str]) -> Path:
    """Init a git repo at `root`, write `files`, and `git add` all of them."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "--quiet")
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    _git(root, "add", "--all")
    return root


def _digest(repo: Path, *paths: str, expect_rc: int = 0) -> dict[str, str]:
    """Invoke the script the way the barrier does and parse `path<TAB>sha`."""
    result = subprocess.run(
        ["bash", str(DIGEST_SCRIPT), *paths],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == expect_rc, (
        f"rc={result.returncode} (want {expect_rc})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition("\t")
        assert value, f"line is not <path><TAB><sha256>: {line!r}"
        out[key] = value
    return out


def _one(repo: Path, path: str) -> str:
    d = _digest(repo, path)
    assert list(d) == [path]
    return d[path]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A small tracked tree with nested dirs and mixed-case names.

    Mixed case and a hyphen matter: `LC_ALL=C` byte order and a locale
    collation order disagree about `Zeta.md` vs `alpha.md` and about
    `a-b.md` vs `ab.md`, so this fixture would expose an unstable sort.
    """
    return _make_repo(
        tmp_path / "ws",
        {
            "shared/contract.json": '{"version": 1}\n',
            "shared/Zeta.md": "zeta\n",
            "shared/alpha.md": "alpha\n",
            "shared/a-b.md": "a-b\n",
            "shared/ab.md": "ab\n",
            "shared/nested/deep/impl.py": "X = 1\n",
            "unrelated/other.txt": "other\n",
        },
    )


# ─── the five pinned properties ───────────────────────────────────────────────


@pytest.mark.unit
def test_script_exists_and_is_syntactically_valid():
    assert DIGEST_SCRIPT.is_file(), f"missing {DIGEST_SCRIPT}"
    rc = subprocess.run(
        ["bash", "-n", str(DIGEST_SCRIPT)], capture_output=True, text=True, timeout=30
    )
    assert rc.returncode == 0, rc.stderr


@pytest.mark.unit
def test_same_tree_same_digest_across_two_invocations(tree: Path):
    """Idempotence — the barrier runs this twice and compares the two answers."""
    first = _one(tree, "shared")
    second = _one(tree, "shared")
    assert first == second
    assert len(first) == 64 and all(c in "0123456789abcdef" for c in first)


@pytest.mark.unit
def test_same_tree_same_digest_across_two_working_copies(tree: Path, tmp_path: Path):
    """The actual §8.3 claim: two clones of one tree agree byte-for-byte.

    This is the property the whole barrier rests on. The second copy lives at a
    different absolute path and has freshly-written files, so a digest that
    leaked path prefixes or mtimes would diverge here.
    """
    other = _make_repo(
        tmp_path / "integration",
        {
            "shared/contract.json": '{"version": 1}\n',
            "shared/Zeta.md": "zeta\n",
            "shared/alpha.md": "alpha\n",
            "shared/a-b.md": "a-b\n",
            "shared/ab.md": "ab\n",
            "shared/nested/deep/impl.py": "X = 1\n",
            # A file OUTSIDE the digested path differs on purpose — it must not
            # be able to influence the shared digest.
            "unrelated/other.txt": "COMPLETELY DIFFERENT\n",
        },
    )
    assert _one(tree, "shared") == _one(other, "shared")


@pytest.mark.unit
def test_content_change_moves_the_digest(tree: Path):
    before = _one(tree, "shared")
    (tree / "shared" / "contract.json").write_text('{"version": 2}\n', encoding="utf-8")
    after = _one(tree, "shared")
    assert after != before, "a content change MUST move the digest"


@pytest.mark.unit
def test_content_change_in_a_nested_file_moves_the_digest(tree: Path):
    before = _one(tree, "shared")
    (tree / "shared" / "nested" / "deep" / "impl.py").write_text("X = 2\n", encoding="utf-8")
    assert _one(tree, "shared") != before


@pytest.mark.unit
def test_mtime_change_does_not_move_the_digest(tree: Path):
    """touch is not a content change — no modes, no timestamps (§8.3)."""
    before = _one(tree, "shared")
    for p in sorted((tree / "shared").rglob("*")):
        if p.is_file():
            os.utime(p, (0, 0))
    assert _one(tree, "shared") == before, "an mtime change must NOT move the digest"


@pytest.mark.unit
def test_chmod_does_not_move_the_digest(tree: Path):
    """Mode bits are excluded even though git itself tracks the exec bit."""
    before = _one(tree, "shared")
    for p in sorted((tree / "shared").rglob("*")):
        if p.is_file():
            p.chmod(0o755)
    assert _one(tree, "shared") == before, "a mode-bit change must NOT move the digest"


@pytest.mark.unit
def test_untracked_file_is_ignored(tree: Path):
    """Build artefacts, __pycache__ and editor droppings cannot move the digest."""
    before = _one(tree, "shared")
    (tree / "shared" / "GENERATED.md").write_text("not tracked\n", encoding="utf-8")
    (tree / "shared" / "nested" / "build.log").write_text("noise\n", encoding="utf-8")
    assert _one(tree, "shared") == before, "an untracked file must NOT move the digest"

    # ...and tracking that same file DOES move it, which proves the previous
    # assertion held because of trackedness and not because of the filename.
    _git(tree, "add", "shared/GENERATED.md")
    assert _one(tree, "shared") != before


@pytest.mark.unit
def test_deleting_a_tracked_file_moves_the_digest(tree: Path):
    before = _one(tree, "shared")
    (tree / "shared" / "alpha.md").unlink()
    _git(tree, "rm", "--quiet", "--cached", "shared/alpha.md")
    assert _one(tree, "shared") != before


# ─── invocation shape and reporting ───────────────────────────────────────────


@pytest.mark.unit
def test_trailing_slash_and_dot_prefix_are_normalised(tree: Path):
    """`shared`, `shared/` and `./shared` must be one digest, one label.

    Otherwise a config value written with a trailing slash on one side of the
    barrier reads as a mismatch against the same tree.
    """
    plain = _digest(tree, "shared")
    slashed = _digest(tree, "shared/")
    dotted = _digest(tree, "./shared")
    assert plain == slashed == dotted
    assert list(plain) == ["shared"]


@pytest.mark.unit
def test_multiple_paths_yield_one_line_each_in_argument_order(tree: Path):
    result = subprocess.run(
        ["bash", str(DIGEST_SCRIPT), "shared/nested", "shared", "unrelated"],
        cwd=tree, capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stderr
    labels = [line.split("\t")[0] for line in result.stdout.splitlines() if line.strip()]
    assert labels == ["shared/nested", "shared", "unrelated"]


@pytest.mark.unit
def test_single_file_path_is_digestible(tree: Path):
    """`shared_digest_paths` may name a single contract file, not just a dir."""
    digest = _one(tree, "shared/contract.json")
    assert len(digest) == 64
    (tree / "shared" / "contract.json").write_text('{"version": 9}\n', encoding="utf-8")
    assert _one(tree, "shared/contract.json") != digest


@pytest.mark.unit
def test_path_with_no_tracked_files_hashes_the_empty_listing(tree: Path):
    """A typo'd shared path is deterministic AND warns — it does not crash.

    Both sides would agree on this value, so criterion 4 would pass vacuously.
    The stderr warning is the only signal, which is why it is asserted.
    """
    result = subprocess.run(
        ["bash", str(DIGEST_SCRIPT), "does-not-exist"],
        cwd=tree, capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"does-not-exist\t{EMPTY_LISTING_SHA}"
    assert "no tracked files" in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    "arg,rc",
    [
        ("/etc/hosts", 3),        # absolute path rejected
        ("../escape", 3),         # traversal rejected
        ("shared/../../etc", 3),  # traversal rejected even when embedded
    ],
)
def test_unsafe_paths_are_rejected(tree: Path, arg: str, rc: int):
    _digest(tree, arg, expect_rc=rc)


@pytest.mark.unit
def test_no_arguments_is_a_usage_error(tree: Path):
    result = subprocess.run(
        ["bash", str(DIGEST_SCRIPT)],
        cwd=tree, capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 2
    assert "Usage:" in result.stderr


@pytest.mark.unit
def test_help_exits_zero_and_documents_the_definition(tree: Path):
    result = subprocess.run(
        ["bash", str(DIGEST_SCRIPT), "--help"],
        cwd=tree, capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0
    for expected in ("Usage:", "sha256", "TRACKED", "LC_ALL=C"):
        assert expected in result.stdout, f"--help should mention {expected!r}"


@pytest.mark.unit
def test_list_flag_names_the_files_without_polluting_stdout(tree: Path):
    """`--list` is how an operator finds WHICH file moved a shared digest."""
    result = subprocess.run(
        ["bash", str(DIGEST_SCRIPT), "--list", "shared"],
        cwd=tree, capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"shared\t{_one(tree, 'shared')}"
    assert "shared/contract.json:" in result.stderr
    assert "shared/nested/deep/impl.py:" in result.stderr


@pytest.mark.unit
def test_tracked_but_missing_file_fails_loudly(tree: Path):
    """A file in the index but absent from disk means the tree is inconsistent.

    Hashing "file bytes" is undefined there, so the script must refuse rather
    than skip the file and report a digest that looks legitimate.
    """
    (tree / "shared" / "alpha.md").unlink()  # still in the index
    result = subprocess.run(
        ["bash", str(DIGEST_SCRIPT), "shared"],
        cwd=tree, capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 4
    assert "missing from working tree" in result.stderr


@pytest.mark.unit
def test_outside_a_git_work_tree_fails_loudly(tmp_path: Path):
    """Never emit a digest with no git index behind it — an empty digest that
    happened to match on both sides would be a silently vacuous criterion 4."""
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    (bare / "shared").mkdir()
    (bare / "shared" / "f.txt").write_text("x\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", str(DIGEST_SCRIPT), "shared"],
        cwd=bare,
        env={
            **os.environ,
            # Stop git from walking up into an enclosing repo (tmp_path could
            # sit inside one on some machines).
            "GIT_CEILING_DIRECTORIES": str(tmp_path),
        },
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 3
    assert "git work tree" in result.stderr


@pytest.mark.unit
def test_digest_is_invoked_repo_relative_by_the_barrier():
    """Contract check: `bash scripts/shared-digest.sh` must be replay-legal.

    The Evidence Contract requires a bash script argument to be repo-relative
    and forbids pipes/redirects/`bash -c` in the command itself
    (plugin-claude/receipt-schema/evidence-contract.json -> replayability). If the
    script ever moved out of `scripts/`, every barrier receipt would start
    failing replay validation.
    """
    assert DIGEST_SCRIPT.parent.name == "scripts"
    assert DIGEST_SCRIPT.parent.parent == REPO_ROOT
    rel = DIGEST_SCRIPT.relative_to(REPO_ROOT).as_posix()
    assert rel == "scripts/shared-digest.sh"
    assert not rel.startswith("/")
