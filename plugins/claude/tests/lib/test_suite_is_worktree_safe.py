"""Layer 1 -- the suite must be safe to run from any checkout, worktree or not.

Issue #380. The documented parallel-lane workflow gives every lane a git
worktree INSIDE the repository (`<repo>/.claude/worktrees/<agent>`), and each
lane is expected to run the full suite before committing. Two things made that
unsafe, and both are invisible from the outside:

  * A repo-rooted walk sees a nested worktree as a second copy of the tree, so
    the suite reports failures that name ANOTHER LANE's files. The count and
    the names are then both meaningless, which is the state #380 describes as
    "the real result is unknowable".

  * A project event emitted with no project dir is addressed at
    `host_env.project_dir()`, which falls through to `os.getcwd()` -- so the
    suite wrote `.synaptory/.orchestrator/` into the checkout it was running
    from. `.synaptory/*` is gitignored, so nothing showed.

The second class is caught suite-wide by the session guard in `conftest.py`
(`_no_project_state_written_into_the_checkout`). What is here is the mechanism,
tested where it can be tested honestly: on synthetic trees under `tmp_path`,
and by census over the test trees, so a new repo-rooted walk cannot be added
without answering the question.

Deliberately NOT here: an assertion that running the suite deletes nothing.
Measured on this issue, nothing in the suite deletes a worktree -- every
`shutil.rmtree` / `rm -rf` / `git worktree remove` reachable from a full run
stays inside `tmp_path`, `web/dist/marketplace` or `plugin-cursor/`. Asserting
the absence of a deletion nobody has reproduced would be a test of a belief.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from _nested_checkouts import is_in_nested_checkout, prune_nested_checkouts

REPO_ROOT_FOR_CENSUS = Path(__file__).resolve().parents[3]

#: The test trees a repo-rooted walk could be added to.
_TEST_TREES = (
    "plugin-claude/tests",
    "plugin-cursor/tests",
    "plugin-codex/tests",
    "conformance",
)

#: A recursive walk rooted at a repository-root variable. Matches
#: `REPO.glob("**/…")`, `REPO_ROOT.rglob(…)`, `os.walk(REPO…)` and the
#: `repo_root` fixture spellings of the same three.
_REPO_ROOTED_WALK = re.compile(
    r"""
    (?:
        \b(?:REPO|REPO_ROOT|_REPO_ROOT|repo_root)\s*\.\s*rglob\s*\(
      | \b(?:REPO|REPO_ROOT|_REPO_ROOT|repo_root)\s*\.\s*glob\s*\(\s*["'](?:\*\*/|\*\*$)
      | \bos\.walk\s*\(\s*(?:str\s*\(\s*)?(?:REPO|REPO_ROOT|_REPO_ROOT|repo_root)\s*[,)]
    )
    """,
    re.VERBOSE,
)

#: How a file declares it handled the nested-checkout question.
_EXCLUSION_MARKERS = ("_nested_checkouts", "is_in_nested_checkout", "prune_nested_checkouts")


@pytest.fixture(autouse=True)
def _no_ambient_project_dir(monkeypatch: pytest.MonkeyPatch):
    """Clear the project-dir variables so the cwd fallback is the thing tested.

    With either variable set, `host_env.project_dir()` never reaches
    `os.getcwd()`, so the leak these tests are about cannot occur and they
    would pass without exercising anything -- green because the developer
    happened to have `CLAUDE_PROJECT_DIR` exported.
    """
    monkeypatch.delenv("SYNAPTORY_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)


# ── The mechanism ────────────────────────────────────────────────────────────


def _tree_with_a_nested_worktree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """An outer checkout holding a nested worktree, both carrying the subject.

    Built by hand rather than with real `git worktree add`: what the walk keys
    on is the `.git` ENTRY, and a worktree's is a file while a clone's is a
    directory. Both shapes are covered here, and the test does not need git.
    """
    outer = tmp_path / "checkout"
    (outer / ".git").mkdir(parents=True)  # a clone: .git is a directory
    subject = outer / "core" / "scripts" / "design_assets" / "scripts" / "core.py"
    subject.parent.mkdir(parents=True)
    subject.write_text("# outer\n", encoding="utf-8")

    lane = outer / ".claude" / "worktrees" / "agent-abc123"
    lane.mkdir(parents=True)
    (lane / ".git").write_text(  # a worktree: .git is a FILE
        "gitdir: %s\n" % (outer / ".git" / "worktrees" / "agent-abc123"),
        encoding="utf-8",
    )
    nested = lane / "core" / "scripts" / "design_assets" / "scripts" / "core.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("# lane\n", encoding="utf-8")
    return outer, subject, nested


@pytest.mark.unit
def test_a_file_in_the_outer_checkout_is_kept(tmp_path: Path):
    outer, subject, _nested = _tree_with_a_nested_worktree(tmp_path)
    assert is_in_nested_checkout(outer, subject) is False


@pytest.mark.unit
def test_a_file_inside_a_nested_worktree_is_excluded(tmp_path: Path):
    outer, _subject, nested = _tree_with_a_nested_worktree(tmp_path)
    assert is_in_nested_checkout(outer, nested) is True


@pytest.mark.unit
def test_the_boundary_is_a_git_entry_not_the_worktrees_path(tmp_path: Path):
    """`git worktree add` takes any path, so the name proves nothing.

    Excluding `.claude/worktrees` by string would pass the reproduction in the
    issue and miss a worktree placed anywhere else -- including the `../wt-x`
    convention and a plain nested clone.
    """
    outer = tmp_path / "checkout"
    (outer / ".git").mkdir(parents=True)
    elsewhere = outer / "scratch" / "lane-two"
    elsewhere.mkdir(parents=True)
    (elsewhere / ".git").write_text("gitdir: /somewhere\n", encoding="utf-8")
    hidden = elsewhere / "core" / "scripts" / "design_assets" / "scripts" / "core.py"
    hidden.parent.mkdir(parents=True)
    hidden.write_text("# lane two\n", encoding="utf-8")

    assert is_in_nested_checkout(outer, hidden) is True
    assert "worktrees" not in hidden.as_posix()


@pytest.mark.unit
def test_the_repository_root_itself_is_never_treated_as_nested(tmp_path: Path):
    """The outer `.git` must not exclude the whole tree.

    An off-by-one that counted `repo_root` itself would prune everything, and
    `_design_assets_roots`' own `assert seen` would then fail with "the glob is
    wrong" -- a true statement about the wrong thing.
    """
    outer = tmp_path / "checkout"
    (outer / ".git").mkdir(parents=True)
    top = outer / "core.py"
    top.write_text("x\n", encoding="utf-8")
    assert is_in_nested_checkout(outer, top) is False
    assert is_in_nested_checkout(outer, outer) is False


@pytest.mark.unit
def test_prune_keeps_the_outer_copy_and_drops_the_lane(tmp_path: Path):
    outer, subject, nested = _tree_with_a_nested_worktree(tmp_path)
    assert prune_nested_checkouts(outer, [subject, nested]) == [subject]


# ── The census ───────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_every_repo_rooted_walk_excludes_nested_checkouts():
    """A walk that starts at the repository root must answer the question.

    `test_design_assets_corpora.py` was the only one when #380 was fixed, and
    it is the reason this census exists rather than a comment: the next such
    walk is one glob away, and the failure it produces points at another
    developer's worktree instead of at itself.
    """
    offenders = []
    for tree in _TEST_TREES:
        base = REPO_ROOT_FOR_CENSUS / tree
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if not _REPO_ROOTED_WALK.search(text):
                continue
            if any(marker in text for marker in _EXCLUSION_MARKERS):
                continue
            offenders.append(path.relative_to(REPO_ROOT_FOR_CENSUS).as_posix())
    assert offenders == [], (
        "these files walk the checkout from its root without excluding nested "
        "checkouts, so a lane's git worktree inside the repository is scanned "
        "as a second copy of the tree and the suite fails naming that lane's "
        "files (#380): %s\n\n"
        "Filter with `_nested_checkouts.is_in_nested_checkout(REPO, path)`, or "
        "scope the walk to a subdirectory that cannot contain a worktree."
        % ", ".join(offenders)
    )


# ── The project-state class, at its source ───────────────────────────────────


@pytest.mark.unit
def test_an_unaddressed_project_event_is_dropped_not_written_to_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`create_story` with no project dir must write nowhere.

    Before the fix it emitted through `synaptory_logger.emit(project_dir=None)`,
    which resolves `host_env.project_dir()` -> `os.getcwd()`, so the event and
    the shared `chain-id` landed in whatever directory the process was in. In
    the suite that directory is the checkout under test.
    """
    import story_pipeline as sp

    cwd = tmp_path / "somewhere-else"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    record = sp.create_story("US-001", "no project named")

    assert record["id"] == "US-001"
    assert not (cwd / ".synaptory").exists(), (
        "an unaddressed story_create event was written into the cwd: %s"
        % sorted(p.name for p in (cwd / ".synaptory").rglob("*"))
    )


@pytest.mark.unit
def test_an_addressed_project_event_lands_in_that_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The other half: dropping must not be how every event now behaves."""
    import story_pipeline as sp

    cwd = tmp_path / "somewhere-else"
    cwd.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(cwd)

    sp.create_story("US-002", "addressed", project_dir=str(project))

    events = project / ".synaptory" / ".orchestrator" / "events.jsonl"
    assert events.is_file(), "the event did not reach the project it named"
    assert "US-002" in events.read_text(encoding="utf-8")
    assert not (cwd / ".synaptory").exists(), "and it must not also reach the cwd"


@pytest.mark.unit
@pytest.mark.parametrize(
    "module, verb",
    [
        ("scrum_state_machine", "add_story"),
        ("kanban_state_machine", "pull_ticket"),
    ],
)
def test_a_state_machine_names_the_project_when_it_creates_a_story(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: str, verb: str
):
    """The state machines are handed a project dir; the event must use it.

    Driven from a foreign cwd on purpose. Every host runs these with a cwd that
    is not necessarily the project -- the SPQ worktree flow guarantees it is
    not -- so "it happened to be the project" is not a property to rely on.
    """
    machine = __import__(module)

    cwd = tmp_path / "somewhere-else"
    cwd.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    machine.initialize(str(project))
    monkeypatch.chdir(cwd)

    getattr(machine, verb)(str(project), "US-003", "titled")

    events = project / ".synaptory" / ".orchestrator" / "events.jsonl"
    assert events.is_file(), "%s.%s logged nowhere in the project" % (module, verb)
    assert "US-003" in events.read_text(encoding="utf-8")
    assert not (cwd / ".synaptory").exists(), (
        "%s.%s addressed its event at the cwd instead of the project it was "
        "given" % (module, verb)
    )


# ── The end-to-end shape, cheaply ────────────────────────────────────────────


@pytest.mark.unit
def test_a_pytest_session_run_from_a_nested_worktree_leaves_it_alone(tmp_path: Path):
    """The reproduction in the issue, at a size a unit test can afford.

    A throwaway repo, a real `git worktree add` INSIDE it, and a real pytest
    session run from inside that worktree against a test that exercises the
    leaking path. Asserts the worktree still exists afterwards and that the
    session wrote no project state into it.

    Small on purpose: the full suite from a worktree is the PR's evidence, not
    a test. What this pins is that the two mechanisms above stay fixed under a
    real nested worktree rather than only under a synthetic one.
    """
    if not (git := _which_git()):
        pytest.skip("git not available")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(git, repo, "init", "-q", "-b", "main")
    _git(git, repo, "config", "user.email", "t@e.co")
    _git(git, repo, "config", "user.name", "T")
    inner = repo / "test_inner.py"
    inner.write_text(
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "import story_pipeline as sp\n"
        "def test_unaddressed_story_create_writes_nothing():\n"
        "    sp.create_story('US-001', 'x')\n"
        % str(REPO_ROOT_FOR_CENSUS / "core" / "lib"),
        encoding="utf-8",
    )
    _git(git, repo, "add", "-A")
    _git(git, repo, "commit", "-qm", "base")

    lane = repo / ".claude" / "worktrees" / "agent-abc123"
    _git(git, repo, "worktree", "add", "-q", "--detach", str(lane))
    assert lane.is_dir()

    # The project-dir variables are cleared on purpose. With either of them set
    # the cwd fallback never runs, so the child would write into that project
    # and this test would pass without exercising anything -- green because the
    # developer happened to have `CLAUDE_PROJECT_DIR` exported.
    env = {k: v for k, v in os.environ.items()
           if k not in ("SYNAPTORY_PROJECT_DIR", "CLAUDE_PROJECT_DIR")}
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "test_inner.py", "-q", "-p", "no:cacheprovider"],
        cwd=str(lane),
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )

    assert lane.is_dir(), (
        "the pytest session deleted the worktree it ran from:\n%s\n%s"
        % (done.stdout, done.stderr)
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert not (lane / ".synaptory").exists(), (
        "the session wrote project state into the worktree it ran from: %s"
        % sorted(p.name for p in (lane / ".synaptory").rglob("*"))
    )


def _which_git() -> str | None:
    import shutil

    return shutil.which("git")


def _git(git: str, cwd: Path, *args: str) -> None:
    subprocess.run(
        [git, "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
    )
