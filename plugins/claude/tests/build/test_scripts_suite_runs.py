# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
"""Layer 2 — the identity-broker suite is actually reached by automation.

`scripts/tests/` covers the identity broker (`scripts/h3t-github`), the guard
shim (`scripts/h3t-tool-guard`), the identity audit and the policy fan-out.
For most of its life nothing ran it: it was not in `./synaptory test`'s
`pytest_dirs` and no file under `.github/workflows/` named it, so it executed
only when someone typed the path by hand. Both guard defects to date (#656,
#667) were found by hand, and the suite that would have caught them was
already written and already green.

A test suite nobody runs is indistinguishable from one that passes, so the
wiring needs a guard of its own -- otherwise dropping `scripts/tests` from the
array reads as a tidy-up and reports nothing. These tests assert the two ends
of the chain:

  * `./synaptory test` names `scripts/tests`, which is what puts it on the
    required `test-plugin` job as well as on a maintainer's laptop; and
  * every test file in the directory is inside that run, so adding a fifth
    file cannot land somewhere the array does not reach.
"""
import pathlib
import re

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_BUILD_CLI = _REPO_ROOT / "plugin-claude" / "synaptory"
_SCRIPTS_TESTS = _REPO_ROOT / "scripts" / "tests"

pytestmark = pytest.mark.build


def _pytest_dirs() -> set[str]:
    """The directories `./synaptory test` hands to pytest.

    Parsed rather than executed: running the real `test` verb from here would
    recurse into this very suite. The shape is a bash array literal plus the
    `pytest_dirs+=(...)` appends that guard optional trees.
    """
    body = _BUILD_CLI.read_text(encoding="utf-8")
    literal = re.search(r"local pytest_dirs=\(([^)]*)\)", body)
    assert literal, (
        "could not find `local pytest_dirs=(...)` in plugin-claude/synaptory. "
        "If the test verb was restructured, update this parser rather than "
        "deleting the guard."
    )
    dirs = set(literal.group(1).split())
    dirs.update(re.findall(r"pytest_dirs\+=\(([^)]+)\)", body))
    return dirs


def test_synaptory_test_runs_the_scripts_suite():
    """The guard shim and the broker are covered by the required gate."""
    dirs = _pytest_dirs()
    assert "scripts/tests" in dirs, (
        "`./synaptory test` no longer runs scripts/tests. That is the only "
        "automation over scripts/h3t-github and scripts/h3t-tool-guard: no "
        "workflow names the directory directly, so removing it here leaves "
        "the suite green and unrun, which is how #656 and #667 both reached "
        "a laptop before anything reported.\n"
        "  currently: %s" % sorted(dirs)
    )


def test_every_scripts_test_file_is_inside_that_run():
    """A fifth test file cannot land outside the wired directory."""
    assert _SCRIPTS_TESTS.is_dir(), "scripts/tests/ is missing"
    found = sorted(p.name for p in _SCRIPTS_TESTS.glob("test_*.py"))
    assert found, "scripts/tests/ holds no test_*.py files"

    stray = sorted(
        p.relative_to(_REPO_ROOT).as_posix()
        for p in (_REPO_ROOT / "scripts").glob("test_*.py")
    )
    assert not stray, (
        "%s sits under scripts/ but outside scripts/tests/, so `./synaptory "
        "test` does not collect it. Move it into scripts/tests/." % stray
    )
