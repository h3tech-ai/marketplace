"""Every relative link in `docs/` must resolve on disk, or say why it cannot.

Forty-six links had rotted before this guard existed, in three shapes that all
look identical to a reader and none of which any test caught:

  * a directory rename the docs never followed (`hub/synaptory_api/` ->
    `api/synaptory_api/`),
  * a target deliberately deleted (`routers/me.py`, retired by ADR-019;
    `build-images.yml`, replaced by `release.yml`), and
  * a target that never existed in this repository at all — the fork inherited
    the prose but not the file (`docs/v1.0/user-portal.md`).

The middle shape is legitimate content: an ADR is a historical record, so
naming a file that has since been deleted is the point. Those references live
on only in git history, and the rule for them is **the prose must say so** —
a line that names a gone file and says it is gone is fine; a bare link to a
path that silently is not there is a defect, because a reader clicking it
learns nothing. `declares_removal` is deliberately narrow so "see also" or
"TODO" cannot buy the exemption.

Off-disk destinations (`http`, `https`, `mailto:`, `tel:`, protocol-relative,
bare `#anchor`) are never checked — there is no local path to resolve.

Scope is `docs/`, the tree this guard was cleared for. `KNOWN_BROKEN` is the
escape hatch for a link that cannot be fixed yet; it is empty today, and
`test_known_broken_has_no_stale_entries` stops it becoming a permanent
exemption list.

Layer 2 (`build` marker): repo-hygiene, alongside the CI-workflow and
version-drift guards.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import unquote

import pytest

pytestmark = pytest.mark.build

# Links that are known-broken and not yet fixed. Each entry is
# ("<repo-relative .md file>", "<link destination exactly as written>").
# Add an entry ONLY with a comment saying why it cannot be fixed now — an
# entry here is a link a reader will still click and still get nothing from.
KNOWN_BROKEN: frozenset = frozenset()

# A URI scheme (http:, https:, mailto:, tel:, ftp:, …) or a protocol-relative
# URL. Both point off-disk, so there is nothing local to resolve.
_SCHEME = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.\-]*:|//)")

# A CommonMark link destination may carry a title: `path "Title"`.
_TITLED = re.compile(r"""^(\S+)\s+(?:"[^"]*"|'[^']*'|\([^)]*\))$""")

# Phrases that make a missing target legible: the prose itself says the file
# is gone, so the name is a historical reference and not a navigation promise.
_REMOVAL_PHRASES = (
    "removed",
    "deleted",
    "retired",
    "superseded",
    "no longer exists",
    "not in this repository",
    "since replaced",
    "was replaced",
    "historical",
)


@dataclass(frozen=True)
class Link:
    """One markdown link destination found on one line."""

    lineno: int
    line: str
    dest: str
    unterminated: bool = False


def iter_links(text: str) -> Iterator[Link]:
    """Yield every `](...)` destination in `text`, one Link per occurrence.

    Paren depth is tracked rather than stopping at the first `)`, because
    CommonMark allows balanced parens in a destination — `(admin)` route-group
    paths are real links, and a checker that split on `)` would silently
    mis-read them as shorter paths that happen not to exist. A destination
    whose parens never close is reported via `unterminated` rather than
    skipped, since that one really is malformed markdown.
    """
    for lineno, line in enumerate(text.splitlines(), 1):
        cursor = 0
        while True:
            open_at = line.find("](", cursor)
            if open_at < 0:
                break
            i = open_at + 2
            depth = 1
            dest = []
            while i < len(line):
                ch = line[i]
                if ch == "\\" and i + 1 < len(line):
                    # A backslash escape contributes the escaped character
                    # only, so `\)` does not close the destination.
                    dest.append(line[i + 1])
                    i += 2
                    continue
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        break
                dest.append(ch)
                i += 1
            if depth != 0:
                yield Link(lineno, line, "".join(dest), unterminated=True)
                break
            yield Link(lineno, line, "".join(dest))
            cursor = i + 1


def local_target(dest: str) -> "str | None":
    """Normalize a destination to a relative path, or None if it is not one.

    None for absolute URLs (`http`, `https`, `mailto:`, …), protocol-relative
    URLs, and bare in-page anchors.
    """
    d = dest.strip()
    if d.startswith("<") and d.endswith(">"):
        d = d[1:-1].strip()
    titled = _TITLED.match(d)
    if titled:
        d = titled.group(1)
    if not d or d.startswith("#") or _SCHEME.match(d):
        return None
    path = unquote(d.split("#", 1)[0].split("?", 1)[0]).strip()
    return path or None


def declares_removal(line: str) -> bool:
    """True if the line's own prose says the thing it names is gone.

    This is what lets a historical record keep referring to a deleted file:
    the reference survives only in git history, and the text admits it.
    """
    lowered = line.lower()
    return any(phrase in lowered for phrase in _REMOVAL_PHRASES)


def _docs_files(repo_root: Path) -> "list[Path]":
    return sorted((repo_root / "docs").rglob("*.md"))


def _broken_links(repo_root: Path) -> "list[tuple]":
    """Every link in docs/ whose target does not resolve and is not explained.

    Returns (md_file, Link, normalized_target) triples.
    """
    found = []
    for md in _docs_files(repo_root):
        for link in iter_links(md.read_text(encoding="utf-8")):
            target = local_target(link.dest)
            if target is None:
                continue
            if (md.parent / target).resolve().exists():
                continue
            if declares_removal(link.line) and not link.unterminated:
                continue
            found.append((md, link, target))
    return found


# ─── the guard ──────────────────────────────────────────────────────────────


def test_docs_have_no_unexplained_broken_relative_links(repo_root: Path):
    failures = []
    for md, link, _target in _broken_links(repo_root):
        rel = md.relative_to(repo_root).as_posix()
        if (rel, link.dest) in KNOWN_BROKEN:
            continue
        why = (
            "destination's parens never close"
            if link.unterminated
            else "target does not exist"
        )
        failures.append(f"{rel}:{link.lineno} -> {link.dest}  ({why})")

    assert not failures, (
        f"{len(failures)} broken relative link(s) in docs/. Repoint each one, "
        "or — if the target was deliberately deleted — de-link it to a code "
        "span and say in the same line that it was removed and by which ADR:\n"
        "  " + "\n  ".join(failures)
    )


def test_known_broken_has_no_stale_entries(repo_root: Path):
    """An exemption that is no longer needed must be deleted, not left to rot."""
    live = {
        (md.relative_to(repo_root).as_posix(), link.dest)
        for md, link, _ in _broken_links(repo_root)
    }
    stale = sorted(KNOWN_BROKEN - live)
    assert not stale, (
        "KNOWN_BROKEN entries that no longer fire — delete them:\n  "
        + "\n  ".join(f"{f} -> {t}" for f, t in stale)
    )


# ─── the scanner's own behaviour ────────────────────────────────────────────
# The removal-exemption path has no live instance in docs/ by construction
# (every deleted target was de-linked), so it is proven here, not by the sweep.


@pytest.mark.parametrize(
    "dest",
    [
        "https://example.invalid/x",
        "http://example.invalid/x",
        "mailto:dev@h3t.co",
        "tel:+15550000",
        "//cdn.example.invalid/x.js",
        "#section-anchor",
    ],
)
def test_off_disk_destinations_are_not_local_targets(dest):
    assert local_target(dest) is None


def test_relative_paths_are_local_targets():
    assert local_target("../adrs/ADR-011.md") == "../adrs/ADR-011.md"
    assert local_target("ADR-011.md#context") == "ADR-011.md"
    assert local_target("<../a b.md>") == "../a b.md"
    assert local_target("../a%20b.md") == "../a b.md"
    assert local_target('../a.md "Title"') == "../a.md"


def test_balanced_parens_survive_the_scan():
    """`(admin)` route-group paths are one destination, not a truncated one."""
    (link,) = iter_links("*Source:* [x](../../web/src/app/(admin)/page.tsx).")
    assert link.dest == "../../web/src/app/(admin)/page.tsx"
    assert not link.unterminated


def test_unterminated_destination_is_reported():
    (link,) = iter_links("see [x](../a/(b/c.md")
    assert link.unterminated


def test_escaped_paren_does_not_close_the_destination():
    (link,) = iter_links(r"see [x](../a/\)b.md) done")
    assert link.dest == "../a/)b.md"
    assert not link.unterminated


def test_multiple_links_on_one_line_are_all_found():
    links = list(iter_links("[a](one.md) and [b](two.md)"))
    assert [l.dest for l in links] == ["one.md", "two.md"]


@pytest.mark.parametrize(
    "line",
    [
        "- `api/synaptory_api/routers/me.py` — removed by ADR-019.",
        "The workflow was retired in favour of release.yml.",
        "`docs/v1.0/user-portal.md` is not in this repository.",
        "Superseded by ADR-034.",
    ],
)
def test_removal_prose_is_recognized(line):
    assert declares_removal(line)


@pytest.mark.parametrize(
    "line",
    [
        "- [api/synaptory_api/tokens.py](../../api/synaptory_api/tokens.py) — mints tokens.",
        "See also the deployment runbook.",
        "TODO: write this section.",
    ],
)
def test_ordinary_prose_buys_no_exemption(line):
    assert not declares_removal(line)


def _write_docs(root: Path, name: str, body: str) -> None:
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / name).write_text(body, encoding="utf-8")


def test_sweep_flags_a_live_link_to_a_missing_file(tmp_path: Path):
    _write_docs(tmp_path, "a.md", "See [the plan](plan.md) for detail.\n")
    assert [l.dest for _, l, _ in _broken_links(tmp_path)] == ["plan.md"]


def test_sweep_allows_a_missing_target_the_text_admits_is_gone(tmp_path: Path):
    """The git-history case: the file exists only in history, and the line says so."""
    _write_docs(
        tmp_path,
        "a.md",
        "[routers/me.py](../api/routers/me.py) was removed by ADR-019.\n",
    )
    assert _broken_links(tmp_path) == []


def test_sweep_still_flags_malformed_markdown_the_text_admits_is_gone(
    tmp_path: Path,
):
    """Removal prose excuses a missing target, never a destination that
    never closes — that renders as literal text, not a link."""
    _write_docs(tmp_path, "a.md", "[me.py](../api/(routers/me.py was removed\n")
    (broken,) = _broken_links(tmp_path)
    assert broken[1].unterminated


def test_sweep_ignores_urls_and_resolvable_paths(tmp_path: Path):
    (tmp_path / "docs" / "sub").mkdir(parents=True)
    (tmp_path / "docs" / "sub" / "here.md").write_text("ok\n", encoding="utf-8")
    _write_docs(
        tmp_path,
        "a.md",
        "[web](https://example.invalid) [mail](mailto:dev@h3t.co) "
        "[here](sub/here.md) [anchor](#x)\n",
    )
    assert _broken_links(tmp_path) == []
