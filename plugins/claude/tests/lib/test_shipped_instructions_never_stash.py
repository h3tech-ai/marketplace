"""Layer 1 -- nothing we ship may tell an agent to run the stash verb (#669).

`plugin-claude/agents/platform-engineer/phases/03-cicd-pipelines.md` carried
this line inside Step 5 of pre-commit hook installation, and it shipped to
customer repositories in the phase file for the role that runs during Inception
and infra stories:

    git stash -u --quiet && git stash pop --quiet 2>/dev/null || true

Two independent faults, and the combination is the expensive one:

  * `2>/dev/null || true` swallows a failed `pop`, so the line reports success
    while the user's uncommitted work stays in the stash and nothing downstream
    says so.

  * The stash stack is **repository-global**. It is the single piece of git
    state a `git worktree` does NOT isolate: one stack per repository, and
    `pop` always takes `stash@{0}` regardless of which worktree or branch
    created the entry. This project's own story pipeline creates per-story
    worktrees inside one repository (`core/lib/worktree_manager.py`), and the
    documented parallel-lane workflow puts each lane in a worktree under
    `.claude/worktrees/`, so concurrent agents in one repository are the
    expected arrangement rather than an edge case. An agent that stashes can
    therefore pop an entry another agent created, in which case the first
    agent's change vanishes from its worktree and reappears in someone else's.

So the operation that can steal work was paired with the suppression that hides
having done it.

Why this is a census and not a comment in that one file: the reason the verb is
unsafe is a property of the repository layout, not of that phase file. A prose
fix in one place does not stop the next author reaching for it, and the next
occurrence is one plausible edit away, in any of three host trees plus the
shared runtime.

**The rule is absolute: no exemption list.** A guard with exemptions turns into
a place to add one, and a verb that can silently move another agent's
uncommitted work has no safe use in shipped content. Two consequences worth
knowing before you hit this failure:

  * There is no verified-safe usage carve-out. Verifying that something runs
    does not require mutating the tree at all: invoke it against a scratch
    index (`GIT_INDEX_FILE` pointed at a path that does not exist -- git reads
    a missing index as an empty one), or exercise it on a scratch path.

  * **Prose that warns about the verb must not spell the command.** Write "the
    stash stack", "`stash pop`", or "do not stash"; the matcher keys on the
    `git`-prefixed invocation, so a warning phrased that way passes and the
    rule needs no exemption to state itself. Step 5 of the phase file above is
    the worked example.

Scope, and what it deliberately leaves out:

  * Covered: the three host trees (`plugin-claude/`, `plugin-cursor/`,
    `plugin-codex/`) and the shared runtime `core/`. That is everything
    composed into a published package, so the composed copies are covered too:
    a source fix that was never re-composed leaves the generated copy carrying
    the verb, and this census fails on the generated copy until the composer
    runs. (`plugin-cursor/roles/platform-engineer/phases/` is a composed copy
    of `plugin-claude/agents/platform-engineer/phases/`. Fix the source and
    re-compose; never hand-edit the copy.)

  * Not covered: each tree's own `tests/` directory, and the repository's
    maintainer tooling outside these trees. Tests are not shipped, and a test
    about this pattern has to be able to name it -- this file does. The
    exclusion is by directory name so it cannot be mistaken for a per-file
    opt-out.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from _nested_checkouts import is_in_nested_checkout

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Everything that gets composed into a published package.
_SHIPPED_TREES = (
    "plugin-claude",
    "plugin-cursor",
    "plugin-codex",
    "core",
)

#: Directory names never descended into. `tests` is the declared exclusion
#: (see the module docstring); the rest are caches and build output that are
#: not authored content.
_SKIP_DIRS = frozenset(
    {"tests", "__pycache__", "node_modules", "dist", ".git", ".venv", "venv"}
)

#: Extensions that cannot carry a runnable instruction as text.
_BINARY_SUFFIXES = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip",
        ".gz", ".tgz", ".woff", ".woff2", ".ttf", ".otf", ".so", ".dylib",
        ".pyc", ".wasm",
    }
)

#: The `git`-prefixed stash invocation, in the forms an instruction can take:
#: `git stash`, `git   stash`, `git -C <dir> stash`, `git --no-pager stash`.
#: Up to three intervening tokens are allowed, none of them a shell separator,
#: so `git status`, `git worktree ... the stash`, and a bare "the stash stack"
#: do not match. Matching the invocation rather than the word is what lets a
#: warning name the hazard without tripping the guard.
_GIT_STASH = re.compile(r"\bgit\b(?:[ \t]+(?!stash\b)[^\s;&|]+){0,3}[ \t]+stash\b")


def invokes_git_stash(text: str) -> bool:
    """True when `text` contains a `git`-prefixed stash invocation."""
    return _GIT_STASH.search(text) is not None


def _shipped_files() -> list[Path]:
    found: list[Path] = []
    for tree in _SHIPPED_TREES:
        base = REPO_ROOT / tree
        if not base.is_dir():
            continue
        # followlinks=False on purpose: `plugin-claude/hooks/lib` is a symlink
        # into `core/lib`, which is walked directly under its own root. Nested
        # checkouts are pruned as well, so a lane's worktree inside the
        # repository is not reported as a second copy of the tree (#380).
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            here = Path(dirpath)
            if is_in_nested_checkout(REPO_ROOT, here):
                dirnames[:] = []
                continue
            for name in sorted(filenames):
                path = here / name
                if path.suffix.lower() in _BINARY_SUFFIXES or path.is_symlink():
                    continue
                found.append(path)
    return found


@pytest.mark.unit
def test_no_shipped_instruction_invokes_git_stash():
    offenders = []
    for path in _shipped_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if invokes_git_stash(line):
                offenders.append(
                    "%s:%d: %s"
                    % (path.relative_to(REPO_ROOT).as_posix(), lineno, line.strip())
                )

    assert offenders == [], (
        "shipped content tells an agent to run the git stash verb (#669):\n"
        "  %s\n\n"
        "The stash stack is REPOSITORY-GLOBAL: one stack per repository, and "
        "`pop` always takes `stash@{0}` no matter which worktree or branch "
        "created the entry. It is the single piece of git state a worktree "
        "does not isolate, and this project's own story pipeline runs "
        "concurrent agents in per-story worktrees of one repository "
        "(`.synaptory/.worktrees/`, plus the parallel-lane worktrees under "
        "`.claude/worktrees/`). So an agent following this instruction can pop "
        "an entry another agent created: the other agent's uncommitted work "
        "disappears from its worktree and reappears in this one. Pair it with "
        "`2>/dev/null || true`, as #669 did, and the theft is silent.\n\n"
        "There is no safe form and no exemption list. To prove something runs "
        "without mutating the tree, invoke it against a scratch index "
        "(`GIT_INDEX_FILE` set to a path that does not exist, which git reads "
        "as an empty index) or exercise it on a scratch path; "
        "`plugin-claude/agents/platform-engineer/phases/03-cicd-pipelines.md` "
        "Step 5 is the worked example. To WARN about the verb in prose, write "
        "\"the stash stack\" or \"do not stash\" instead of the command: this "
        "guard matches the `git`-prefixed invocation, not the word.\n\n"
        "If the offender is a composed copy under `plugin-cursor/` or "
        "`plugin-codex/`, fix the source under `plugin-claude/` or `core/` and "
        "re-run that host's `scripts/compose.py`; never hand-edit a generated "
        "file." % "\n  ".join(offenders)
    )


@pytest.mark.unit
def test_the_census_actually_reaches_the_shipped_instructions():
    """A walk that finds nothing would pass the census while checking nothing.

    Named files rather than a count: a count rots on the next added file, and
    what matters is that both the authored phase file from #669 and its
    composed copy are inside the walk.
    """
    scanned = {p.relative_to(REPO_ROOT).as_posix() for p in _shipped_files()}
    for required in (
        "plugin-claude/agents/platform-engineer/phases/03-cicd-pipelines.md",
        "plugin-cursor/roles/platform-engineer/phases/03-cicd-pipelines.md",
        "core/lib/worktree_manager.py",
    ):
        assert required in scanned, (
            "the census did not reach %s, so it proves nothing about it" % required
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "line",
    [
        # The line #669 was filed against, verbatim.
        "git stash -u --quiet && git stash pop --quiet 2>/dev/null || true",
        "git stash",
        "git stash push -m wip",
        "git   stash   pop",
        "git -C /tmp/repo stash",
        "git --no-pager stash list",
        "$(git stash list)",
        "  - run `git stash` first",
    ],
)
def test_the_matcher_catches_the_invocation(line: str):
    """Without this the census could rot into a regex that matches nothing."""
    assert invokes_git_stash(line), line


@pytest.mark.unit
@pytest.mark.parametrize(
    "line",
    [
        # Prose that warns about the verb must stay expressible.
        "The stash stack is repository-global; `pop` takes `stash@{0}`.",
        "Do not stash: a git worktree does not isolate the stash stack.",
        "never suppress a failure with `2>/dev/null || true`",
        # Neighbouring verbs and unrelated identifiers.
        "git status --porcelain",
        "git diff --cached --name-only --diff-filter=ACM",
        "git worktree add --detach $scratch HEAD",
        "def stash_link(match: re.Match[str]) -> str:",
    ],
)
def test_the_matcher_does_not_fire_on_prose_or_neighbours(line: str):
    assert not invokes_git_stash(line), line
