"""Layer 1 - the design-asset search tool may only advertise corpora it ships (#372).

`core.py` was forked from an upstream project that shipped 23 CSV corpora. The
fork brought the tooling and 7 of the files. Nothing noticed, because a missing
corpus is not a crash -- `search()` returns `{"error": "File not found: ..."}`
and the CLI prints it. So 17 of the 23 advertised options answered every query
with an absolute path to a file that had never existed in this repository, for
two releases, and `--stack` advertised twelve frameworks and could serve none.

The declaration drifting from the filesystem is the whole bug, so that is what
these tests pin -- in BOTH directions, over EVERY shipped copy of the tree:

  * a declared corpus with no file  -> the tool promises an answer it cannot give
  * a shipped file no one declares  -> content ships that nothing can reach

Two narrower traps are pinned alongside, because the obvious fix walks into
them. Trimming the declaration without them turns a visible error into a silent
wrong answer:

  * `search()` used to fall back to `CSV_CONFIG["style"]` for an unknown domain,
    so it returned styles.csv rows labelled with the caller's domain. Dropping
    "landing" from the config would have made design_system.py's landing search
    quietly return style rows instead of failing.
  * `detect_domain()` used to route free-text queries to removed domains, so a
    plain `search.py "landing page hero cta"` -- no flags, nothing named --
    errored. That is the path a real user hits.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from _nested_checkouts import is_in_nested_checkout

REPO = Path(__file__).resolve().parents[3]


def _design_assets_roots() -> list[Path]:
    """Every shipped copy of the design-assets tree IN THIS CHECKOUT.

    `core/` is the source of truth; `plugin-cursor/` holds a committed mirror
    that compose.py regenerates. Both are checked, so a core edit that is never
    recomposed fails here rather than shipping a mirror that declares corpora
    its own data dir does not hold.

    A nested checkout is not a shipped copy (#380). This glob starts at the
    repository ROOT, so a git worktree inside the tree -- which is where the
    documented parallel-lane workflow puts every lane -- contributed a second
    full set of roots, and five tests then failed per extra lane naming that
    lane's path. `resolve()` below does not save it: another checkout of the
    same commit is a genuinely different real path, so the de-dup keeps both.
    """
    seen: dict[Path, Path] = {}
    for p in REPO.glob("**/design_assets/scripts/core.py"):
        if "web/dist" in p.as_posix() or "__pycache__" in p.as_posix():
            continue
        if is_in_nested_checkout(REPO, p):
            continue
        root = p.parent.parent
        # plugin-claude reaches core/ through a symlink, so the same tree can be
        # found twice under different paths. Key on the real location.
        seen.setdefault(root.resolve(), root)
    assert seen, "no design_assets tree found -- the glob is wrong, not the tree"
    return sorted(seen.values())


def _load_core(root: Path):
    """Import a specific tree's core.py under a unique module name.

    `core` is far too generic a top-level name to put on sys.path, and the two
    copies would collide with each other, so each is loaded by path.
    """
    name = f"_design_assets_core_{abs(hash(root.as_posix()))}"
    spec = importlib.util.spec_from_file_location(name, root / "scripts" / "core.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ROOTS = _design_assets_roots()
IDS = [str(r.relative_to(REPO)) for r in ROOTS]

#: Read by design_system.py directly rather than through CSV_CONFIG, so it is
#: legitimately present-but-undeclared and must not trip the reverse check.
UNDECLARED_BY_DESIGN = {"ui-reasoning.csv"}


def _declared_files(core) -> set[str]:
    """Every corpus the tool advertises, as a data-dir-relative posix path."""
    return {c["file"] for c in core.CSV_CONFIG.values()} | {
        c["file"] for c in core.STACK_CONFIG.values()
    }


def _present_files(root: Path) -> set[str]:
    data = root / "data"
    return {p.relative_to(data).as_posix() for p in data.glob("**/*.csv")}


def _serving_domains(core, root: Path) -> set[str]:
    """Domains that can actually answer -- declared AND backed by a file.

    The tests below measure against this rather than against `CSV_CONFIG`,
    because the declaration is the thing under suspicion. Asserting that
    auto-detection lands inside `CSV_CONFIG` would have passed happily on the
    broken tree, where `landing` was declared and unserveable.
    """
    return {
        d
        for d, c in core.CSV_CONFIG.items()
        if (root / "data" / c["file"]).exists()
    }


# ── the guard: declaration and filesystem must agree, both ways ──────────────


@pytest.mark.parametrize("root", ROOTS, ids=IDS)
def test_every_declared_corpus_ships(root: Path):
    """The #372 direction: no advertised option may name a file we do not ship."""
    core = _load_core(root)
    missing = sorted(f for f in _declared_files(core) if not (root / "data" / f).exists())
    assert not missing, (
        f"{len(missing)} corpora are advertised but not shipped in "
        f"{root.relative_to(REPO)}: {missing}. Either ship the file or drop the "
        f"declaration -- an option that cannot answer must not be offered."
    )


@pytest.mark.parametrize("root", ROOTS, ids=IDS)
def test_every_shipped_corpus_is_declared(root: Path):
    """The reverse direction: content that ships but nothing can reach is dead weight."""
    core = _load_core(root)
    orphaned = sorted(_present_files(root) - _declared_files(core) - UNDECLARED_BY_DESIGN)
    assert not orphaned, (
        f"CSV files ship in {root.relative_to(REPO)} that no domain or stack "
        f"declares, so nothing can search them: {orphaned}."
    )


@pytest.mark.parametrize("root", ROOTS, ids=IDS)
def test_declared_and_shipped_sets_are_identical(root: Path):
    """States the invariant once, so a future reader sees the rule, not two halves."""
    core = _load_core(root)
    assert _declared_files(core) == _present_files(root) - UNDECLARED_BY_DESIGN


# ── the traps the fix must not fall into ────────────────────────────────────


@pytest.mark.parametrize("root", ROOTS, ids=IDS)
def test_unknown_domain_errors_rather_than_returning_style_rows(root: Path):
    """A domain we do not serve must say so, not answer from styles.csv.

    The old `CSV_CONFIG.get(domain, CSV_CONFIG["style"])` reported the caller's
    domain while reading styles.csv -- a wrong answer that looks like a right
    one. Trimming the config makes every removed domain take this path, so this
    has to be an error before anything is removed.
    """
    core = _load_core(root)
    result = core.search("dashboard", "landing")
    assert "error" in result, (
        "an unserved domain returned results instead of an error; it read "
        f"{result.get('file')!r} and labelled it {result.get('domain')!r}"
    )
    assert "not shipped" in result["error"].lower()


@pytest.mark.parametrize("root", ROOTS, ids=IDS)
def test_auto_detection_only_routes_to_shipped_domains(root: Path):
    """Free-text queries must never be routed into a corpus we do not have.

    No flag is passed here on purpose: this is the shape of a real query, and
    it used to auto-detect `landing` and fail with a file-not-found path.
    """
    core = _load_core(root)
    served = _serving_domains(core, root)
    for query in (
        "landing page hero cta conversion",
        "lucide icon glyph pictogram",
        "useCallback rerender bundle waterfall",
        "aria focus outline semantic form",
        "tailwind css implementation checklist",
    ):
        detected = core.detect_domain(query)
        assert detected in served, (
            f"{query!r} auto-detected to {detected!r}, which cannot answer"
        )
        # And end to end, since routing is only half of it.
        assert "error" not in core.search(query), (
            f"a plain query with no flags failed: {core.search(query)['error']}"
        )


@pytest.mark.parametrize("root", ROOTS, ids=IDS)
def test_stack_search_explains_itself_when_no_stacks_ship(root: Path):
    """`--stack` must not answer with a bare path to a file that never existed."""
    core = _load_core(root)
    if core.AVAILABLE_STACKS:
        pytest.skip("stack corpora ship in this tree; nothing to explain")
    result = core.search_stack("button", "react")
    assert "error" in result
    assert "not shipped" in result["error"].lower()


# ── the corpora that are actually served still work ─────────────────────────


@pytest.mark.parametrize("root", ROOTS, ids=IDS)
def test_every_advertised_option_returns_a_real_answer(root: Path):
    """Whatever survives the trim must answer, or the trim went too far."""
    core = _load_core(root)
    for domain in core.CSV_CONFIG:
        result = core.search("dashboard button color layout", domain)
        assert "error" not in result, f"advertised domain {domain!r}: {result['error']}"
        assert result["domain"] == domain
    for stack in core.AVAILABLE_STACKS:
        result = core.search_stack("button", stack)
        assert "error" not in result, f"advertised stack {stack!r}: {result['error']}"


@pytest.mark.parametrize("root", ROOTS, ids=IDS)
def test_design_system_only_searches_shipped_domains(root: Path):
    """design_system.py aggregates domains by name; each must be one we serve.

    Its `landing` entry was the live instance of the silent-fallback trap.
    """
    core = _load_core(root)
    text = (root / "scripts" / "design_system.py").read_text(encoding="utf-8")
    block = text.split("SEARCH_CONFIG = {", 1)[1].split("}", 1)[0]
    declared = {line.split('"')[1] for line in block.splitlines() if '"' in line}
    assert declared, "SEARCH_CONFIG did not parse -- update this test, not the guard"
    unserved = sorted(declared - _serving_domains(core, root))
    assert not unserved, (
        f"design_system.py aggregates domains that are not served: {unserved}"
    )
