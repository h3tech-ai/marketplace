#!/usr/bin/env python3
"""Expand content directives in a composed Claude plugin tree.

Authored bodies carry host-neutral `{{include:}}` / `{{read:}}` directives.
Claude's package is produced by rsync rather than a composer, so the expansion
runs as a post-step over the copied tree -- never over the authored source,
which must stay host-neutral.

Usage:
    python3 expand_claude_directives.py <plugin-dist-dir>
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import compose_directives as directives  # noqa: E402


def expand_tree(root: Path) -> int:
    """Expand every markdown file under `root`. Returns the count changed."""
    changed = 0
    for path in sorted(root.rglob("*.md")):
        # `{{include:}}` / `{{self:}}` resolve against the body's own authoring
        # directory, which `own_dir_for` answers the same way for every host.
        own_dir = directives.own_dir_for(path.relative_to(root).as_posix())
        text = path.read_text(encoding="utf-8")
        expanded = directives.expand(
            text,
            host="claude",
            own_dir=own_dir,
            source_dir=root / own_dir,
            strict=True,
        )
        if expanded != text:
            path.write_text(expanded, encoding="utf-8")
            changed += 1
    return changed


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    root = Path(sys.argv[1])
    if not root.is_dir():
        print("not a directory: %s" % root, file=sys.stderr)
        return 2
    try:
        changed = expand_tree(root)
    except directives.DirectiveError as exc:
        print("directive error: %s" % exc, file=sys.stderr)
        return 1
    print("  Expanded directives in %d markdown file(s)" % changed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
