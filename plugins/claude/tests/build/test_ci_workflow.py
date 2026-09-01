# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
"""Layer 2 — guards on .github/workflows/test.yml's path filtering.

The `paths-ignore` list decides whether CI runs at all. Two things about it
are load-bearing and neither is visible from the list itself:

  1. GitHub Actions does not support YAML anchors, so the list is spelled
     twice -- once under `pull_request`, once under `push`. A change applied
     to one copy and not the other silently makes merges to dev less tested
     than the PRs that fed them.

  2. `docs/user-guide/` must never be ignored. It is the one documentation
     tree with a test behind it (test_build_user_guide.py asserts DOC_GROUPS
     and the filesystem agree), and that test exists because v2.7.0/v2.7.1
     shipped a user-guide page that was never rendered into the published
     HTML. An ignore rule covering it would restore that bug and remove the
     guard in the same stroke.

This module parses the workflow with a deliberately tiny scanner rather than
PyYAML: the `test-plugin` job installs pytest and nothing else, and the repo
keeps it that way on purpose. The scanner understands exactly one shape --
`paths-ignore:` followed by single-quoted list items -- and asserts it found
both blocks, so a restructured trigger fails loudly instead of silently
matching nothing.
"""
import pathlib
import re

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "test.yml"

_TRIGGER_RE = re.compile(r"^  (pull_request|push|workflow_dispatch|schedule):\s*$")
_ITEM_RE = re.compile(r"^\s+- '([^']+)'\s*$")


def _paths_ignore_by_trigger():
    """`{trigger: [pattern, ...]}` for every trigger carrying a paths-ignore."""
    out = {}
    trigger = None
    collecting = False
    for line in _WORKFLOW.read_text(encoding="utf-8").splitlines():
        m = _TRIGGER_RE.match(line)
        if m:
            trigger, collecting = m.group(1), False
            continue
        if line.strip() == "paths-ignore:":
            assert trigger, "paths-ignore appeared before any trigger key"
            out[trigger] = []
            collecting = True
            continue
        if not collecting:
            continue
        if not line.strip() or line.strip().startswith("#"):
            continue
        item = _ITEM_RE.match(line)
        if item:
            out[trigger].append(item.group(1))
        else:
            collecting = False
    return out


@pytest.fixture(scope="module")
def ignored():
    found = _paths_ignore_by_trigger()
    assert set(found) == {"pull_request", "push"}, (
        "expected a paths-ignore block under exactly the pull_request and push "
        "triggers, found %s. If the workflow was restructured, update this "
        "scanner -- do not delete the guard." % sorted(found)
    )
    for trigger, patterns in found.items():
        assert patterns, "%s has an empty paths-ignore block" % trigger
    return found


@pytest.mark.build
def test_push_and_pull_request_ignore_the_same_paths(ignored):
    pr, push = ignored["pull_request"], ignored["push"]
    assert pr == push, (
        "the pull_request and push paths-ignore lists have drifted. Actions "
        "has no YAML anchors, so they are two copies of one decision -- a PR "
        "that skips CI must also skip it on the merge to dev, or the merge "
        "runs a suite the PR never did.\n"
        "  pull_request only: %s\n"
        "  push only:         %s"
        % (sorted(set(pr) - set(push)), sorted(set(push) - set(pr)))
    )


@pytest.mark.build
@pytest.mark.parametrize("trigger", ["pull_request", "push"])
def test_user_guide_docs_are_never_ignored(ignored, trigger):
    """docs/user-guide/ is test-covered, so it must always trigger CI."""
    for pattern in ignored[trigger]:
        assert not pattern.startswith("docs/user-guide"), (
            "%r ignores docs/user-guide/, which is covered by "
            "test_doc_groups_includes_every_user_guide_markdown. Ignoring it "
            "would let an unregistered guide page ship unrendered -- the "
            "exact bug that test was written for." % pattern
        )
        # 'docs/**' would swallow user-guide/ transitively.
        assert pattern not in ("docs/**", "docs/", "docs"), (
            "%r ignores the whole docs tree, including the test-covered "
            "docs/user-guide/. Enumerate the inert subtrees instead." % pattern
        )


@pytest.mark.build
@pytest.mark.parametrize("trigger", ["pull_request", "push"])
def test_no_source_tree_is_ignored(ignored, trigger):
    """Only prose may be ignored. Every module directory must trigger CI."""
    source_roots = {
        "core", "cli", "api", "web", "conformance", "e2e", "infra",
        "benchmarks", "plugin-claude", "plugin-cursor", "plugin-codex",
        ".github",
    }
    # Documented exceptions: subtrees under a source root that no CI job reads.
    # Each must stay narrow and stay justified — `infra/scripts/` holds operator
    # and dev tooling, while every job that needs infra reaches for
    # `infra/compose/` or `infra/docker/`, both of which still trigger.
    allowed_subtrees = {"infra/scripts/**"}
    for pattern in ignored[trigger]:
        if pattern in allowed_subtrees:
            continue
        root = pattern.split("/", 1)[0]
        assert root not in source_roots, (
            "%r ignores %s/, which carries code or CI config. paths-ignore is "
            "for prose only — add a narrow, justified entry to "
            "`allowed_subtrees` if a specific subtree genuinely has no CI "
            "consumer." % (pattern, root)
        )


@pytest.mark.build
@pytest.mark.parametrize("trigger", ["pull_request", "push"])
def test_the_infra_paths_ci_actually_uses_still_trigger(ignored, trigger):
    """`infra/scripts/**` is ignorable; the infra CI depends on is not.

    `infra/compose/` and `infra/docker/` are what the e2e job stands the stack
    up from. If either were ever added to the ignore list, e2e would stop
    running for the changes most likely to break it.
    """
    for pattern in ignored[trigger]:
        for load_bearing in ("infra/compose", "infra/docker", "infra/caddy"):
            assert not pattern.startswith(load_bearing), (
                "%r ignores %s, which the e2e job builds the stack from"
                % (pattern, load_bearing)
            )
