"""Layer 1 — the declared path grammar and its refusals (#643, #644).

Weighted toward refusals on purpose. `SC-MTH-008`'s value is not that a correct
declaration compares correctly; it is that an ambiguous one cannot be admitted
at all, because the failure it prevents — two changes silently diverging on one
file, reconciled by whoever merges last — is **not recoverable from evidence
afterwards**.
"""

from __future__ import annotations

import pytest

import path_scope as ps


# ── The accepted forms ────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,canonical", [
    ("api/", "api/"),
    ("api", "api"),
    ("api/**", "api/"),
    ("api/routers/", "api/routers/"),
    ("api/routers/auth.py", "api/routers/auth.py"),
    ("  api/**  ", "api/"),
])
def test_accepted_forms_normalize_to_one_spelling(raw, canonical):
    """`api/` and `api/**` are the same declaration, so they must compare equal.

    Two spellings that survive normalisation would let two units declare the
    same region and pass a disjointness check.
    """
    assert ps.normalize(raw) == canonical


# ── The refusals ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,because", [
    ("", "empty"),
    ("api///**", "an empty path segment is a typo, not a spelling"),
    ("   ", "empty"),
    ("*.py", "suffix pattern matches in every directory"),
    ("**/fixtures", "leading globstar"),
    ("api/*/routers", "interior wildcard guesses the tree depth"),
    ("api/*", "bare trailing star is not `/**`"),
    ("../shared", "traversal leaves the repository"),
    ("api/../web", "traversal in the middle"),
    ("./api", "a no-op segment is a second spelling"),
    ("/etc/passwd", "absolute"),
    ("C:/repo/api", "absolute, other platform"),
    ("~/secrets", "home directory"),
    ("api\\routers", "backslash separator"),
    ("**", "the whole repository"),
    ("/**", "the whole repository, spelled with a root"),
    (None, "not a string"),
    (42, "not a string"),
])
def test_refused_forms_name_the_reason_and_the_alternative(raw, because):
    with pytest.raises(ps.PathScopeError) as excinfo:
        ps.normalize(raw)
    message = str(excinfo.value)
    assert "grammar v%s" % ps.GRAMMAR_VERSION in message, message
    assert "api/routers/auth.py" in message, (
        "a refusal must show what to write instead: %s" % message
    )


def test_a_declaration_refuses_on_its_first_bad_entry():
    """Partial acceptance would seal a declaration nobody wrote."""
    with pytest.raises(ps.PathScopeError):
        ps.normalize_all(["api/", "*.py", "web/"])


def test_duplicates_and_order_survive_normalisation():
    """A declaration is a record of what somebody wrote and reviewed."""
    assert ps.normalize_all(["web/", "api/", "web/**"]) == ["web/", "api/", "web/"]


# ── Intersection: the admission condition ─────────────────────────────────────

@pytest.mark.parametrize("left,right,overlaps", [
    (["api/"], ["web/"], False),
    (["api/"], ["api/"], True),
    (["api/"], ["api/routers/"], True),
    (["api/routers/"], ["api/"], True),
    (["api/routers/auth.py"], ["api/"], True),
    (["api/routers/auth.py"], ["api/routers/user.py"], False),
    (["api/", "web/"], ["cli/", "web/app/"], True),
    (["apixyz/"], ["api/"], False),
    # The broader reading, deliberately: `api` with no trailing slash is an
    # exact path AND may be a directory on disk. Strict reading would make
    # these disjoint and surface the collision at merge.
    (["api"], ["api/routers/"], True),
    (["api"], ["api/"], True),
    (["api/x.py"], ["api/y.py"], False),
])
def test_intersection_compares_declarations(left, right, overlaps):
    """`apixyz/` is not inside `api/`, and a naive prefix test says it is.

    The segment boundary is the whole point: without it, every sibling
    directory sharing a name prefix would be reported as a collision and the
    check would be ignored within a week.
    """
    assert ps.intersects(left, right) is overlaps


@pytest.mark.parametrize("left,right", [
    ([], ["api/"]),
    (["api/"], []),
    ([], []),
])
def test_an_absent_scope_is_refused_rather_than_called_disjoint(left, right):
    """The predecessor's hole, closed and pinned.

    `story_pipeline._scopes_disjoint([], ["api/**"])` returns True — disjoint
    from everything — while its own docstring says empties fail closed. A
    boolean has nowhere to put "you did not declare one", so this raises.
    """
    with pytest.raises(ps.PathScopeError) as excinfo:
        ps.intersects(left, right)
    assert "absent path scope" in str(excinfo.value)
    assert "SC-MTH-008" in str(excinfo.value)


def test_a_blank_entry_inside_a_declaration_is_still_refused():
    """The half the predecessor did get right, kept."""
    with pytest.raises(ps.PathScopeError):
        ps.intersects(["api/", ""], ["web/"])


# ── Validating the change that actually shipped ───────────────────────────────

def test_uncovered_names_every_path_outside_the_declaration():
    """Accepting a declaration at admission is worth nothing if the change that
    ships is never compared against it."""
    scope = ["api/", "docs/spec.md"]
    changed = ["api/routers/auth.py", "docs/spec.md", "web/app/page.tsx", "cli/main.go"]
    assert ps.uncovered(scope, changed) == ["web/app/page.tsx", "cli/main.go"]


def test_a_rename_is_checked_at_both_ends():
    """A unit whose scope covers only the destination has still modified the
    source. A check that sees one of the two lets a rename move a file out of
    another Cycle's region unremarked."""
    scope = ["web/"]
    assert ps.uncovered(scope, ["web/new.ts"]) == []
    assert ps.uncovered(scope, ["api/old.ts", "web/new.ts"]) == ["api/old.ts"]


def test_covers_keeps_the_strict_reading_that_intersects_widens():
    """The two questions have opposite safe directions, so they differ.

    For a COLLISION, a bare `api` might be a directory and must collide with
    `api/routers/`. For validating what SHIPPED, a scope of `api` covers the
    path `api` and not `api/routers/auth.py` -- crediting a change to a
    declaration that does not contain it is how an out-of-scope change passes
    a promotion check.
    """
    assert ps.intersects(["api"], ["api/routers/"]) is True
    assert ps.covers(["api"], "api/routers/auth.py") is False
    assert ps.covers(["api/"], "api/routers/auth.py") is True


def test_creating_a_new_file_cannot_escape_an_accepted_scope():
    """Declarations are compared, not the filesystem: the file below does not
    exist anywhere and is still inside `api/`."""
    assert ps.covers(["api/"], "api/does/not/exist/yet.py") is True


# ── Case, stated rather than folded ───────────────────────────────────────────

def test_case_is_not_folded_and_the_difference_is_reportable():
    """macOS folds case and Linux does not, so folding would make one
    declaration mean two things depending on the machine. Refusing to fold
    reports a macOS-only collision everywhere, which is the safe direction."""
    assert ps.intersects(["Api/"], ["api/"]) is False
    assert ps.same_path_different_case("Api/", "api/") is True
    assert ps.same_path_different_case("api/", "api/") is False
    assert ps.same_path_different_case("api/", "web/") is False


# ── Shared-path ownership: never none, never two ──────────────────────────────

def test_owners_of_returns_the_set_so_none_and_two_are_different_refusals():
    """`SC-MTH-008` wants exactly one owner. A boolean cannot tell a barrier
    which of the two refusals it hit, and the messages differ."""
    owners = [("WU-01", ["api/"]), ("WU-02", ["web/"]), ("WU-03", ["api/routers/"])]
    assert ps.owners_of("api/routers/auth.py", owners) == {"WU-01", "WU-03"}
    assert ps.owners_of("web/app/page.tsx", owners) == {"WU-02"}
    assert ps.owners_of("infra/compose.yml", owners) == set()
