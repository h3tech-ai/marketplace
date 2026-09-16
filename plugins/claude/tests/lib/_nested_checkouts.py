"""Keep a repo-wide walk inside the checkout it belongs to (issue #380).

The documented parallel-lane workflow puts each lane in a git worktree INSIDE
the repository -- `<repo>/.claude/worktrees/<agent>` -- and a worktree holds a
complete second copy of the tree. So any test that walks the checkout from its
root sees every file twice: once as itself and once as another lane's copy.

`test_design_assets_corpora.py` did exactly that and reported ten failures
naming `.claude/worktrees/<other lane>/...`. None of them were about the code
under test, and neither the count nor the names meant anything -- the shape #380
describes as "the real result is unknowable".

Excluding `.claude/worktrees` by name would not be the fix: `git worktree add`
takes any path, and a plain nested clone has the same effect. What actually
marks the boundary is the thing that makes the copy a checkout of its own -- a
`.git` entry (a directory for a clone, a file for a worktree) between the
repository root and the file being considered.

Note this is deliberately NOT the same question as "is this path ignored by
git": `.claude/worktrees/` is untracked, and so are `web/dist/` and every
`__pycache__`, but callers already skip those by name for their own reasons and
a `git check-ignore` sweep per path would be both slower and wrong (a nested
checkout can be tracked by its own git and still not belong to this walk).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


def is_in_nested_checkout(repo_root: Path, path: Path) -> bool:
    """True when `path` lives inside a checkout nested below `repo_root`.

    Walks the directories strictly between `repo_root` and `path` and answers
    yes as soon as one of them carries its own `.git`. `repo_root` itself is
    never counted, so a file in the outer checkout is always kept.
    """
    root = Path(os.path.abspath(str(repo_root)))
    here = Path(os.path.abspath(str(path)))
    try:
        rel = here.relative_to(root)
    except ValueError:
        # Outside the checkout entirely. Not this function's call to make;
        # callers scope their own walks.
        return False
    current = root
    # `rel.parts[:-1]` when `path` is a file, all parts when it is a directory.
    # Testing every intermediate directory INCLUDING the last covers both
    # without the caller having to say which it handed us.
    for part in rel.parts:
        current = current / part
        if (current / ".git").exists():
            return True
    return False


def prune_nested_checkouts(repo_root: Path, paths: Iterable[Path]) -> list[Path]:
    """`paths` with everything inside a nested checkout dropped."""
    return [p for p in paths if not is_in_nested_checkout(repo_root, p)]
