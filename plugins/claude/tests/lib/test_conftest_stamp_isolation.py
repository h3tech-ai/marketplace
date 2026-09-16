"""The suite must not stamp the shared runtime tree, even when killed hard.

`hook_env` needs `${CLAUDE_PLUGIN_ROOT}/hooks/lib/cp-url.local` to exist so the
hooks it execs resolve a control plane. It used to create that file in the real
plugin tree -- which is a symlink to `core/lib/` -- and remove it in a `finally`.
A `finally` does not run when the process is killed, so a pytest timeout or an
ill-timed Ctrl-C left the source tree looking control-plane stamped. Every LATER
run then resolved the ambient installed `synaptory` instead of resolving nothing,
and `test_spq_barrier_e2e.py` failed 23 of 38 with Work Units reported `blocked`
for a reason that was not in the code (#502).

These tests hold the two halves of the fix: the stamp lives in a throwaway root,
and a stamp an older run already leaked is swept at session start.
"""

from __future__ import annotations

import os
import site
import subprocess
import sys
from pathlib import Path

import pytest


def _conftest():
    """The live `plugin-claude/tests/conftest.py` module object.

    Not `import conftest`: pytest registers it under a rootdir-derived name
    (`tests.conftest` here), and re-importing it by path would give a second
    copy whose module globals nothing patches.
    """
    target = str(Path(__file__).resolve().parents[1] / "conftest.py")
    for module in list(sys.modules.values()):
        if getattr(module, "__file__", None) == target:
            return module
    raise RuntimeError(f"conftest module not loaded from {target}")


CONFTEST = _conftest()
_FIXTURE_STAMP_URL = CONFTEST._FIXTURE_STAMP_URL
_SHARED_STAMP = CONFTEST._SHARED_STAMP

#: Set by the outer test to un-skip the inner one. The inner test is a real test
#: function rather than a file written into the repo at runtime, because a test
#: about not writing to the repo should not write to the repo.
_INNER = "SYNAPTORY_TEST_SIMULATE_HARD_KILL"
_INNER_EXIT = 70


def test_hook_env_stamps_a_throwaway_root_not_the_repository(hook_env, plugin_root):
    root = Path(hook_env["CLAUDE_PLUGIN_ROOT"])
    assert root != plugin_root, "the fixture must not hand out the real plugin tree"

    stamp = root / "hooks" / "lib" / "cp-url.local"
    assert not stamp.is_symlink(), "a symlinked stamp would write back into core/lib"
    assert stamp.read_text(encoding="utf-8").strip() == _FIXTURE_STAMP_URL

    # Real bodies are still reachable through the farm: hooks read agents,
    # rules and protocols out of the tree they are pointed at.
    assert (root / ".claude-plugin" / "plugin.json").is_file()
    assert (root / "hooks" / "_cp-url.sh").is_file()
    assert (root / "hooks" / "lib" / "host_env.py").is_file()


def test_host_env_resolves_the_fixture_url_from_that_root(hook_env, monkeypatch):
    import host_env

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", hook_env["CLAUDE_PLUGIN_ROOT"])
    assert host_env.plugin_control_plane_url() == _FIXTURE_STAMP_URL


def test_the_sweep_tells_fixture_debris_from_a_deploy_local_stamp(monkeypatch, tmp_path):
    stamp = tmp_path / "cp-url.local"
    monkeypatch.setattr(CONFTEST, "_SHARED_STAMP", stamp)
    stale = CONFTEST._stale_fixture_stamp

    assert stale() == "", "no file is not debris"

    stamp.write_text(_FIXTURE_STAMP_URL + "\n", encoding="utf-8")
    assert stale() == _FIXTURE_STAMP_URL

    # What `./synaptory deploy local` writes. Deleting it would punish a normal
    # local setup for a bug it did not cause, so the sweep must not claim it.
    stamp.write_text("http://localhost:8080\n", encoding="utf-8")
    assert stale() == ""

    stamp.write_text("https://synaptory.h3t.co\n", encoding="utf-8")
    assert stale() == ""


@pytest.mark.skipif(
    not os.environ.get(_INNER), reason="inner run, driven by the hard-kill test below"
)
def test_inner_run_that_dies_without_unwinding(hook_env):
    """Take `hook_env`, then die the way a timeout kills pytest.

    `os._exit` skips atexit handlers, fixture finalizers and the session-end
    guard alike -- the exact conditions under which the old save/restore left
    its stamp behind.
    """
    assert Path(hook_env["CLAUDE_PLUGIN_ROOT"]).is_dir()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_INNER_EXIT)


def _child_env() -> dict:
    """The inner run's environment: application HOME isolated, pytest importable.

    `conftest._isolate_synaptory_state` is autouse and points HOME at a
    tmp_path for every test, which is what keeps the suite out of the
    developer's real `~/.synaptory/`. The child inherits that HOME, and Python
    derives the user site directory FROM HOME, so on an install where pytest
    lives only in the user site the child cannot import it at all:

        /Library/Developer/CommandLineTools/usr/bin/python3: No module named pytest

    That is a test-launch failure wearing the costume of the behaviour under
    test, and it is invisible on an install where pytest sits in the
    interpreter's own site-packages, which is why CI and this developer's
    machine both stayed green while another install failed (#592 re-review).

    The parent's `site` computed USER_SITE at interpreter start, BEFORE the
    fixture moved HOME, so it still names the real one. Handing it to the child
    on PYTHONPATH restores the import without giving back the isolation: HOME
    stays inside tmp_path, so anything the plugin writes still lands there.
    """
    env = {**os.environ, _INNER: "1"}
    user_site = site.getusersitepackages()
    if isinstance(user_site, str):
        user_site = [user_site]
    existing = env.get("PYTHONPATH", "")
    parts = [p for p in (user_site or []) if p]
    if existing:
        parts.append(existing)
    if parts:
        env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def test_a_hard_killed_run_leaves_no_stamp_in_the_shared_tree(repo_root):
    if _SHARED_STAMP.exists():
        pytest.skip(
            f"{_SHARED_STAMP} already exists (a real `./synaptory deploy local` "
            "stamp?), so this test cannot assert its absence"
        )

    inner = f"{__file__}::test_inner_run_that_dies_without_unwinding"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", inner, "-q", "-p", "no:randomly", "-p", "no:cacheprovider"],
        cwd=str(repo_root),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if "No module named pytest" in result.stderr:
        raise AssertionError(
            "the inner run never started: this interpreter could not import "
            "pytest with the isolated HOME in place, so nothing was measured "
            "about hard-kill behaviour. Read this as a launch failure, not as "
            "the isolation being broken.\n"
            f"{result.stderr}"
        )
    assert result.returncode == _INNER_EXIT, (
        "the inner run was supposed to die mid-test without unwinding; got "
        f"{result.returncode}\n{result.stdout}\n{result.stderr}"
    )
    assert not _SHARED_STAMP.exists(), (
        f"a hard-killed run left {_SHARED_STAMP} behind. That file makes the whole "
        "source tree look control-plane stamped, and every later run in this tree "
        "resolves a CLI it should not (#502)."
    )
