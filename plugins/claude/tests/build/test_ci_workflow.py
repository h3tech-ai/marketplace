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
PyYAML. PyYAML is installed in the `test-plugin` job (see the bottom of this
file), but only because test_product_version.py needs it to validate a
rendered synaptory.yaml.tmpl -- a real config document, not CI trigger
config. That does not make PyYAML this scanner's business: the scanner
understands exactly one shape, `paths-ignore:` followed by single-quoted
list items, and asserts it found both blocks, so a restructured trigger
fails loudly instead of silently matching nothing. Migrating a working,
independently-tested scanner to PyYAML just because the dependency happens
to be present now would be churn for no behavior change.
"""
import fnmatch
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


#: Root-level markdown that a test reads. `- '*.md'` used to cover all three,
#: so a PR touching only one of them ran no jobs at all.
_TEST_COVERED_ROOT_MARKDOWN = {
    # test_runtime_claims_are_qualified.py reads it as a FRONT_DOORS surface.
    "README.md",
    # scripts/tests/test_identity_policy_sync.py asserts both still match
    # scripts/h3t-identity-policy.md, which fans out into them. A drifted
    # generated copy is indistinguishable from a fresh one by eye, so the
    # test is the only thing that reports it -- and it cannot report on a
    # workflow run that never happened.
    "CLAUDE.md",
    "AGENTS.md",
}


@pytest.mark.build
@pytest.mark.parametrize("trigger", ["pull_request", "push"])
def test_test_covered_root_markdown_is_never_ignored(ignored, trigger):
    """Prose may be ignored; prose with a test behind it may not.

    Matched the way GitHub matches: `*` does not cross a `/`, so `'*.md'`
    hits exactly the repository root -- which is where all three of these
    live.
    """
    for pattern in ignored[trigger]:
        for covered in sorted(_TEST_COVERED_ROOT_MARKDOWN):
            assert not fnmatch.fnmatch(covered, pattern), (
                "%r ignores %s, which a test reads. A change to it would run "
                "no CI jobs at all, so the test that guards it could not "
                "report. Enumerate the inert files instead of matching the "
                "whole root." % (pattern, covered)
            )


@pytest.mark.build
@pytest.mark.parametrize("trigger", ["pull_request", "push"])
def test_no_source_tree_is_ignored(ignored, trigger):
    """Only prose may be ignored. Every module directory must trigger CI."""
    source_roots = {
        "core", "cli", "api", "web", "conformance", "e2e", "infra",
        "benchmarks", "plugin-claude", "plugin-cursor", "plugin-codex",
        ".github",
        # scripts/ carries the identity broker and the guard shim, and
        # `./synaptory test` runs scripts/tests over both. It was never on
        # this list because it had no CI consumer -- the same premise that
        # had rotted for infra/scripts/ by #439. It has one now.
        "scripts",
    }
    # Documented exceptions: subtrees under a source root that no CI job reads.
    # Each must stay narrow and stay justified. There are none right now:
    # `infra/scripts/**` used to sit here on the premise that no CI job
    # referenced it, and #439 showed that premise had rotted: release.yml and
    # version-drift.yml both run scripts from there, and those scripts now have
    # unit tests that the ignore rule was skipping.
    allowed_subtrees: set[str] = set()
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


# ── the authoritative declared-gap owner check reaches every gate ────────────
#
# #542 made `strict` mode ask GitHub and wired it into the required
# `test-plugin` job. That closed the required-CI hole and left two others: a
# release can be dispatched manually without ever passing through `test-plugin`,
# and the operator preflight printed WARN when `gh` was missing while still
# declaring the release safe. Both are the same fail-open shape, so both are
# asserted here (#396, #517).

_RELEASE_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "release.yml"
_PREFLIGHT = _REPO_ROOT / "infra" / "scripts" / "release-preflight.sh"
_OWNER_CHECK = "conformance/gap_owners.py --check strict"


@pytest.mark.parametrize(
    "workflow", [_WORKFLOW, _RELEASE_WORKFLOW], ids=["test", "release"]
)
def test_every_gate_workflow_asks_github_about_gap_owners(workflow):
    """`./synaptory test` alone is not this evidence.

    Its conformance run is deliberately offline, so a workflow that only runs
    it asserts nothing about whether a declared gap's owner is still open. The
    release workflow is the sharper case: `workflow_dispatch` skips the merge
    that would have run the required job, so the one moment the claim matters
    most was the one path that still trusted the cache.
    """
    text = workflow.read_text(encoding="utf-8")
    assert _OWNER_CHECK in text, (
        "%s runs no authoritative declared-gap owner check, so a gap whose "
        "owner closed since the last manifest refresh ships green" % workflow.name
    )
    assert "GH_TOKEN" in text, (
        "%s invokes the owner check without a token; this repository is "
        "private, so an unauthenticated read cannot tell a closed issue from "
        "an invisible one and every row would fail for the wrong reason"
        % workflow.name
    )


def test_the_preflight_owner_check_fails_when_it_cannot_ask(tmp_path):
    """A gate that cannot reach its authority has not passed.

    The CLI-catalog probe further down warns when a public URL is unreachable,
    which is advisory and right. Copying that idiom into a gate let the script
    print "Preflight OK" without ever asking GitHub, which is the same
    fail-open that strict mode had removed one file away.

    Runs the real script with `gh` genuinely absent, so this asserts behaviour
    rather than a reading of the source. `az` is withheld too, which stops the
    run at the Azure guard: the point here is the local gate's verdict, and a
    unit test has no business reaching a production subscription to get it.
    """
    import os
    import subprocess

    shim = tmp_path / "bin"
    shim.mkdir()
    withheld = {"gh", "az"}
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        directory = pathlib.Path(entry)
        if not entry or not directory.is_dir():
            continue
        for binary in directory.iterdir():
            if binary.name in withheld or (shim / binary.name).exists():
                continue
            try:
                (shim / binary.name).symlink_to(binary)
            except OSError:
                pass

    env = dict(os.environ)
    env["PATH"] = str(shim)
    result = subprocess.run(
        ["bash", str(_PREFLIGHT)],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    combined = result.stdout + result.stderr
    assert "declared-gap owners" in combined, (
        "the owner gate did not run before the Azure guard, so an operator "
        "without a subscription is never told their declarations rotted:\n%s"
        % combined[-2000:]
    )
    assert "FAIL (no `gh` on PATH" in combined, (
        "the owner check degraded to a warning when it could not ask:\n%s"
        % combined[-2000:]
    )
    assert result.returncode != 0, (
        "the preflight exited zero without asking GitHub:\n%s" % combined[-2000:]
    )
    assert "Preflight OK" not in combined, combined[-2000:]


# ── the owner check needs a token that can actually read an issue ────────────
#
# The test above asserts GH_TOKEN is PASSED to the owner check. That is a
# presence check, and a presence check is not an authorization check: both
# gate workflows passed a token that could not read an issue at all.
#
# GITHUB_TOKEN's restricted default grants Contents, Metadata and Packages
# read and nothing else, which the runner prints verbatim under "Set up job".
# `gh issue view` against that token gets a null `repository.issue` and says
# "Could not resolve to an issue or pull request with the number of N" -- the
# same sentence it would produce for an issue that does not exist. So the
# failure named a missing owner while the real cause was a missing scope, and
# it did so on every declared-gap row at once.
#
# Two facts kept it hidden. Declaring ANY permissions block replaces the
# default rather than adding to it, so release.yml's `contents: write` +
# `packages: write` silently removed nothing and granted nothing new. And
# every CI run between the check landing and 2026-09-04 was a billing-blocked
# non-run, so the step had never once executed.

_ISSUE_READABLE = frozenset({"read", "write"})

#: What GITHUB_TOKEN carries when no `permissions:` block applies. Taken from
#: what the runner prints under "Set up job", not from a reading of the docs:
#: run 33839277019 reported exactly Contents/Metadata/Packages read. Modelled
#: here rather than treated as "nothing granted", so the contents assertion
#: below cannot fail for the wrong reason on a workflow that declares no block
#: and never needed to.
_RESTRICTED_DEFAULT = {"contents": "read", "metadata": "read", "packages": "read"}
_JOB_KEY_RE = re.compile(r"^  ([a-z][a-z0-9-]*):\s*$")
_ENTRY_RE = re.compile(r"^(\s+)([a-z-]+):\s*([a-z-]+)\s*$")


def _permissions_block(lines, start, indent):
    """`{scope: level}` for the permissions block opening at `start`.

    Comment lines inside the block are skipped rather than terminating it:
    both workflows carry the reason for the scope beside the scope.
    """
    out = {}
    for line in lines[start + 1 :]:
        if not line.strip() or line.strip().startswith("#"):
            continue
        match = _ENTRY_RE.match(line)
        if not match or len(match.group(1)) != indent:
            break
        out[match.group(2)] = match.group(3)
    return out


def _effective_permissions_for_owner_check(workflow):
    """What the owner-check step is actually granted, job block winning.

    Returns `(scope_map, source)` so a failure can say which block it read.
    """
    lines = workflow.read_text(encoding="utf-8").splitlines()

    step = next(
        (i for i, line in enumerate(lines) if _OWNER_CHECK in line), None
    )
    assert step is not None, "%s no longer runs %s" % (workflow.name, _OWNER_CHECK)

    jobs = next(
        (i for i, line in enumerate(lines) if line.rstrip() == "jobs:"), None
    )
    assert jobs is not None, "%s has no jobs: key" % workflow.name

    job = None
    for i in range(jobs + 1, step):
        if _JOB_KEY_RE.match(lines[i]):
            job = i
    assert job is not None, "%s: no job encloses the owner check" % workflow.name

    end = len(lines)
    for i in range(job + 1, len(lines)):
        if _JOB_KEY_RE.match(lines[i]):
            end = i
            break

    for i in range(job + 1, end):
        if lines[i].rstrip() == "    permissions:":
            return _permissions_block(lines, i, 6), "the %s job" % lines[job].strip(":  ")

    for i, line in enumerate(lines[:jobs]):
        if line.rstrip() == "permissions:":
            return _permissions_block(lines, i, 2), "the workflow-level block"

    return dict(_RESTRICTED_DEFAULT), "no permissions block (the restricted default)"


@pytest.mark.parametrize(
    "workflow", [_WORKFLOW, _RELEASE_WORKFLOW], ids=["test", "release"]
)
def test_the_owner_check_is_granted_issue_read(workflow):
    """A token that cannot read an issue cannot report on one."""
    granted, source = _effective_permissions_for_owner_check(workflow)
    assert granted.get("issues") in _ISSUE_READABLE, (
        "%s runs %s with GITHUB_TOKEN, but %s grants issues=%r. The restricted "
        "default is Contents+Metadata+Packages read, so `gh issue view` returns "
        "a null repository.issue and every declared-gap row fails as an "
        "unresolvable owner rather than reporting its real state."
        % (workflow.name, _OWNER_CHECK, source, granted.get("issues"))
    )


@pytest.mark.parametrize(
    "workflow", [_WORKFLOW, _RELEASE_WORKFLOW], ids=["test", "release"]
)
def test_the_owner_check_job_keeps_contents_read(workflow):
    """The fix must not cost the checkout its own permission.

    A job-level `permissions:` block replaces the default outright, so adding
    `issues: read` alone would leave actions/checkout with no contents scope.
    Asserted separately because it is the failure mode the fix itself
    introduces, and it would surface as an unrelated checkout error.
    """
    granted, source = _effective_permissions_for_owner_check(workflow)
    assert granted.get("contents") in _ISSUE_READABLE, (
        "%s: %s grants contents=%r, so actions/checkout has no permission to "
        "read the repository" % (workflow.name, source, granted.get("contents"))
    )


# ── the gate must be able to finish before it can report ────────────────────

_JOB_TIMEOUT_RE = re.compile(r"^    timeout-minutes:\s*(\d+)\s*$")

#: Measured on run 33864789037 (dev, 0e6745bf): 254s for `./synaptory test`,
#: 35s for the conformance report, ~25s of runner setup, plus the owner check
#: and the artifact upload. The job needs more than 315s, and the ceiling was
#: 300s, so it was killed after every suite had passed and after the owner
#: check had already reported -- surfacing as `cancelled`, which reads as a
#: human stop rather than a gate that never returned a verdict.
_MEASURED_SECONDS = 315
_TIMEOUT_FLOOR_MINUTES = 10


def test_the_plugin_gate_has_time_to_reach_its_own_verdict():
    """A required gate whose ceiling is below its runtime cannot pass.

    Asserted on the declared value rather than on a timed run: this suite has
    no business spending five minutes proving how long another suite takes,
    and the number that decides the outcome is the one in the file.
    """
    lines = _WORKFLOW.read_text(encoding="utf-8").splitlines()
    job = next(
        (i for i, line in enumerate(lines) if line.rstrip() == "  test-plugin:"),
        None,
    )
    assert job is not None, "test.yml no longer declares a test-plugin job"

    end = next(
        (i for i in range(job + 1, len(lines)) if _JOB_KEY_RE.match(lines[i])),
        len(lines),
    )
    declared = next(
        (
            int(_JOB_TIMEOUT_RE.match(lines[i]).group(1))
            for i in range(job + 1, end)
            if _JOB_TIMEOUT_RE.match(lines[i])
        ),
        None,
    )
    assert declared is not None, "test-plugin declares no timeout-minutes"
    assert declared >= _TIMEOUT_FLOOR_MINUTES, (
        "test-plugin allows %d minute(s), but the job was measured at more "
        "than %ds and was killed mid-run with every suite already passed. A "
        "timeout kill reports `cancelled`, so tightening this hides a gate "
        "that never reached a verdict behind a status that looks deliberate."
        % (declared, _MEASURED_SECONDS)
    )


def _setup_go(job: str) -> dict[str, str]:
    """Return the `actions/setup-go` step's `with:` map for one job.

    A tiny scanner, for the reason this module's docstring gives: it predates
    PyYAML being available in this job, and there is no behavior to gain by
    migrating it now. It understands one shape, a `uses: actions/setup-go@vN`
    line followed by a `with:` block of `key: value` pairs, and returns {}
    when the job has no such step so the caller can say which job is missing
    one.
    """
    lines = _WORKFLOW.read_text(encoding="utf-8").splitlines()
    job_re = re.compile(r"^  (\S+):\s*$")
    inside = False
    found: dict[str, str] = {}
    in_with = False
    for line in lines:
        m = job_re.match(line)
        if m:
            if inside:
                break
            inside = m.group(1) == job
            continue
        if not inside:
            continue
        if re.match(r"^\s+-?\s*uses: actions/setup-go@", line):
            in_with = "pending"
            continue
        if in_with == "pending":
            if re.match(r"^\s+with:\s*$", line):
                in_with = True
                continue
            if line.strip().startswith("- "):
                in_with = False
            continue
        if in_with is True:
            kv = re.match(r'^\s+([a-z-]+): "?([^"]*?)"?\s*$', line)
            if kv:
                found[kv.group(1)] = kv.group(2)
                continue
            if line.strip().startswith("#"):
                # A comment inside `with:` is not the end of the block. Ending
                # here made a key INVISIBLE rather than failing: the scanner
                # returned a short map and every assertion about a key below
                # the comment silently read `None`. A guard that reads less
                # than the file is the failure mode this module exists to
                # catch, one level up.
                continue
            if line.strip():
                in_with = False
    return found


def test_the_plugin_gate_declares_the_go_toolchain_it_now_needs():
    """`./synaptory test` runs Go, so the gate that runs it must install Go.

    #683 made the conformance suite shell out to `go test -overlay ... -c`, so a
    scenario exercises the real Go adapter rather than asserting a refusal. The
    job would still pass without this step, because `ubuntu-latest` ships a
    toolchain, and that is exactly what this test exists to stop: passing on the
    runner image's undeclared version while `cli/go.mod` asks for another one.

    The pin is asserted to MATCH `test-e2e`'s rather than to equal a literal, so
    bumping Go stays a one-place edit and the two gates cannot drift onto
    different toolchains.
    """
    plugin = _setup_go("test-plugin")
    assert plugin, (
        "the test-plugin job runs `./synaptory test`, which runs `go test`, and "
        "declares no Go toolchain. Add actions/setup-go with the same pin as "
        "test-e2e."
    )

    e2e = _setup_go("test-e2e")
    assert e2e, "test-e2e lost its Go setup; this test compares the pins against it"

    assert plugin.get("go-version") == e2e.get("go-version"), (
        "the two gates that run Go pin different versions: test-plugin %r vs "
        "test-e2e %r" % (plugin.get("go-version"), e2e.get("go-version"))
    )


def test_every_go_job_makes_the_same_caching_decision():
    """Caching is one decision for one runner, not a per-job preference.

    This replaces an assertion that `test-plugin` set
    `cache-dependency-path: cli/go.sum`. That was right while the premise held
    -- a cold module cache on every run -- and #713 measured the premise false
    on this runner: it is self-hosted and long-lived, `actions/checkout` cleans
    the workspace and not `$HOME`, so `~/go/pkg/mod` already survives, and the
    Actions cache lost both ways (restore reported no cache, save then failed
    to reserve). So the old assertion is stale rather than the change wrong.

    It is replaced instead of deleted, and by a PARITY check rather than a
    literal, because deleting it would have left nothing at all watching this
    and a literal would rot again at the next runner change. What must stay
    true is that the jobs agree: they share one machine, so a job that opts
    back in pays a cost its siblings measured and rejected.

    That is not hypothetical. `test-cli` (#729) was written against the old
    convention and merged in the same hour as #713, which retired it, so `dev`
    briefly carried three Go jobs on one runner with two different answers.
    Neither pull request was wrong alone and neither touched the other's
    lines; the disagreement existed only on the merged tree.
    """
    decisions = {
        job: (_setup_go(job) or {}).get("cache")
        for job in ("test-plugin", "test-e2e", "test-cli")
    }
    assert all(v is not None for v in decisions.values()), (
        "a Go job states no cache decision, so it silently inherits the "
        "action's default: %r" % (decisions,)
    )
    assert len(set(map(str, decisions.values()))) == 1, (
        "Go jobs on the same runner disagree about caching: %r" % (decisions,)
    )


# ── the Go suite must run in a CHECK, not only in a release (#729) ──────────
#
# `./synaptory cli test` used to be invoked in exactly one place in all of CI:
# release.yml's `Run tests` step, on the prod VM, during a production release.
# So a Go defect in `cli/` was invisible to every pull request and every push,
# and the first thing that noticed it was the release it aborted -- which is
# how #726 stopped v1.3.0 after sitting on dev through an entire release wave
# with every PR green. These assertions hold the new `test-cli` job open; the
# thing they are guarding against is the job being deleted or narrowed back to
# push-only, which would look exactly like the state before it existed.

_CLI_SUITE = "./synaptory cli test"


def _job_block(job: str, workflow: pathlib.Path = _WORKFLOW) -> list[str]:
    """The lines of one job in `workflow` (test.yml by default), excluding the job key itself."""
    lines = workflow.read_text(encoding="utf-8").splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.rstrip() == "  %s:" % job), None
    )
    assert start is not None, "%s no longer declares a %s job" % (workflow.name, job)
    end = next(
        (i for i in range(start + 1, len(lines)) if _JOB_KEY_RE.match(lines[i])),
        len(lines),
    )
    return lines[start + 1 : end]


def test_a_check_runs_the_go_suite():
    """Some job in test.yml must RUN the Go suite, not merely mention it.

    Matched on a `run:` line rather than anywhere in the file, deliberately.
    The loose version of this assertion passes on a job NAMED
    `Go CLI tests (./synaptory cli test)` whose only step echoes -- confirmed
    by replacing the step with `echo skipped` and watching the guard stay
    green. A check that passes on its own name is the defect, not the guard.
    """
    running = [
        line.strip()
        for line in _WORKFLOW.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
        and re.match(r"^\s+run:\s", line)
        and _CLI_SUITE in line
    ]
    assert running, (
        "no job in test.yml has a `run:` step invoking %r. That was the state "
        "#729 fixed: the Go suite ran only inside `deploy prod`, so every PR "
        "was green on a tree whose CLI had never been tested." % _CLI_SUITE
    )


def test_the_go_suite_runs_on_pull_requests():
    """A gate that only runs after the merge catches nothing before it."""
    block = _job_block("test-cli")
    gates = [line.strip() for line in block if line.strip().startswith("if:")]
    for gate in gates:
        assert "pull_request" not in gate or "!=" in gate, (
            "test-cli's %r can exclude a pull_request. The whole point of #729 "
            "is that the Go suite reports BEFORE a merge." % gate
        )
    # The one gate its siblings carry, and the only one allowed: skip a push
    # to dev, because the PR already ran this exact merge ref.
    for gate in gates:
        assert gate == (
            "if: github.event_name != 'push' || github.ref != 'refs/heads/dev'"
        ), "test-cli carries an unexpected gate: %r" % gate


def test_the_go_suite_job_does_not_hardcode_a_hosted_runner():
    """#724: a job pinned to `ubuntu-latest` failed before step 1, with zero steps.

    While hosted capacity was unavailable, `version-drift` reported a red
    check having run nothing -- an empty `runner_name` and no steps. Every
    other job in this workflow resolves through `vars.CI_RUNNER`; a new one
    that does not would reintroduce that shape on the gate that just closed a
    two-year blind spot.
    """
    block = _job_block("test-cli")
    runs_on = next(
        (line.strip() for line in block if line.strip().startswith("runs-on:")), None
    )
    assert runs_on == "runs-on: ${{ vars.CI_RUNNER || 'ubuntu-latest' }}", (
        "test-cli declares %r. It must resolve through vars.CI_RUNNER like "
        "every other job here (#724)." % runs_on
    )


def test_the_go_suite_redirects_the_keychain():
    """The belt from #726, asserted so it is not quietly dropped.

    keychain_fallback_test.go documents SYNAPTORY_KEYCHAIN_FILE as "the
    intended CI usage pattern: always set SYNAPTORY_KEYCHAIN_FILE in headless
    environments so the file backend is used unconditionally", and no workflow
    set it. This is the outer layer only: a test that needs the redirect still
    sets it with its own `t.Setenv` (#727), because a test that passes only
    under CI's environment has stopped asserting what it was written to
    assert.
    """
    block = _job_block("test-cli")
    assert any(
        line.strip().startswith("SYNAPTORY_KEYCHAIN_FILE:") for line in block
    ), (
        "test-cli does not set SYNAPTORY_KEYCHAIN_FILE, so a Go test that "
        "calls keychain.Save() before redirecting reaches the runner's real "
        "credential store (#726)."
    )


def test_the_go_gates_share_one_toolchain_pin():
    """Three jobs now run Go. They must not drift onto different versions."""
    cli = _setup_go("test-cli")
    assert cli, "test-cli runs `go test` and declares no Go toolchain"

    plugin = _setup_go("test-plugin")
    assert plugin, "test-plugin lost its Go setup; this test compares pins against it"

    assert cli.get("go-version") == plugin.get("go-version"), (
        "test-cli pins Go %r while test-plugin pins %r"
        % (cli.get("go-version"), plugin.get("go-version"))
    )


# ── the job must install what the tests it runs actually import ─────────────
#
# `pip install --quiet pytest` was the whole story until now: PRs #742 and
# #743 both failed identically with `ModuleNotFoundError: No module named
# 'yaml'` despite neither touching test_product_version.py, because that test
# (added earlier, by an unrelated change) imports PyYAML and the job never
# grew a line to install it. Every other suite under pytest_dirs passed, so
# the failure read as one flaky test on an unrelated PR rather than as a job
# with an undeclared dependency, and it repeated on every PR after because
# nothing failed the BUILD for it -- only the one test, every time.
#
# Scoped to the same trees `./synaptory test` runs inside this job
# (pytest_dirs in plugin-claude/synaptory's test(), plus conformance/ as its
# own invocation) and asserted in both directions: a dependency this narrow
# rots whichever way it drifts, and a stale install is exactly as invisible
# as a missing one.
#
# Checked against BOTH jobs that run this suite, not just test-plugin.
# release.yml's `release` job runs the identical `./synaptory test` (see the
# comment at the top of this file's Go-suite section: it used to be the ONLY
# place that ran the Go suite, and it still independently installs its own
# Python deps via its own `pip install` step -- a separate line in a separate
# workflow file, not shared with test-plugin's). #747 fixed test-plugin's
# install line and added the assertion below scoped to it alone; that left
# release.yml's copy of the same gap uncaught, and it aborted the v1.3.1
# release on the prod runner with the identical ModuleNotFoundError, after
# every pull_request check had already gone green. Parametrizing over both is
# what closes that -- a single-workflow assertion is exactly the shape that
# missed it the first time.

_YAML_IMPORT_RE = re.compile(r"^\s*import yaml\s*$", re.MULTILINE)
#: Mirrors pytest_dirs in plugin-claude/synaptory's test() plus the separate
#: conformance/ invocation -- everything that shares the test-plugin job's one
#: `pip install` step, and (via `./synaptory test`) release.yml's `release` job.
_TEST_PLUGIN_JOB_TREES = (
    "plugin-claude/tests", "plugin-cursor/tests", "plugin-codex/tests",
    "scripts/tests", "conformance",
)


@pytest.mark.parametrize(
    "workflow, job", [(_WORKFLOW, "test-plugin"), (_RELEASE_WORKFLOW, "release")],
    ids=["test-plugin", "release"],
)
def test_the_plugin_job_installs_pyyaml_to_match_what_its_tests_import(workflow, job):
    """PyYAML must be installed exactly when a suite this job runs needs it."""
    consumers = sorted(
        str(path.relative_to(_REPO_ROOT))
        for tree in _TEST_PLUGIN_JOB_TREES
        if (_REPO_ROOT / tree).is_dir()
        for path in (_REPO_ROOT / tree).rglob("*.py")
        if _YAML_IMPORT_RE.search(path.read_text(encoding="utf-8"))
    )

    block = _job_block(job, workflow)
    # `pip install`, however it is spelled. test.yml installs through
    # `uv pip install`, release.yml through plain `pip install`, and matching
    # only the latter made this guard BLIND rather than wrong: pyyaml was
    # installed, the scanner could not see it, and the assertion below fired
    # with an empty install list -- the same message it gives when pyyaml is
    # genuinely absent. A guard that cannot tell "missing" from "spelled
    # differently" is worse than one that fails, because the next person
    # reading it looks for a missing dependency that is already there.
    #
    # Still anchored to `run:`. This workflow carries comments that quote
    # `uv pip install pytest` in prose, and an unanchored search would read
    # one of those as an install step -- turning a blind guard into a
    # falsely-passing one, which is the failure direction that actually
    # lets a broken install reach a release.
    install_lines = [
        line.strip() for line in block
        if re.match(r"^\s*run:\s*(?:uv\s+|python[0-9.]*\s+-m\s+)?pip install\b", line)
    ]
    installs_pyyaml = any("pyyaml" in line.lower() for line in install_lines)

    if consumers:
        assert installs_pyyaml, (
            "%s imports yaml, but %s's %r job's pip install step (%r) does "
            "not install pyyaml. This fails as ModuleNotFoundError on every "
            "run, not just the one that adds the import -- see #742/#743 "
            "(test-plugin) and the v1.3.1 release abort (release)."
            % (consumers[0], workflow.name, job, install_lines)
        )
    else:
        assert not installs_pyyaml, (
            "no test under %s imports yaml any more, but %s's %r job still "
            "installs pyyaml (%r). Dropping the now-unused install is fine; "
            "if the consumer moved rather than disappeared, widen "
            "_TEST_PLUGIN_JOB_TREES instead of deleting this assertion."
            % (", ".join(_TEST_PLUGIN_JOB_TREES), workflow.name, job, install_lines)
        )
