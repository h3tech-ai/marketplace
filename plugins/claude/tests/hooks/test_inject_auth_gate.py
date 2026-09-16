"""Layer 2 -- `synaptory-inject-protocols.sh` mid-session auth gate (#400, #396).

The gate re-runs `whoami --check` before EVERY agent dispatch, so a session
that expired and cannot be silently renewed blocks fail-closed.

There is no cache. #400 shipped one, a 300s sentinel justified as "a burst of
dispatches costs one network round-trip, not one per dispatch". That
justification was wrong: `whoami --check` goes through `clientForSession`,
which loads the keychain record and returns, contacting the control plane only
inside the five minute expiry skew when it renews. The sentinel was saving a
process spawn while opening a window in which a session that had stopped being
usable still dispatched agents. Two rounds of #396 review closed narrower
versions of that window before removing the cache closed the class.

Contract pinned here:
  * every dispatch runs `whoami --check`, with no skip path;
  * a FAILED check blocks with the BLOCKED message and exit 1;
  * a stale sentinel from the cached era is inert and gets cleaned up;
  * SYNAPTORY_AUTH_NO_GATE=1 bypasses the gate entirely (CI / offline).

What this gate does NOT prove, and no version of it ever did: that the session
is still valid SERVER-side. `whoami --check` never asks the control plane about
a healthy token, so remote revocation is invisible here and is caught at the
next authenticated call. `test_the_gate_is_not_a_revocation_check` pins that
honestly rather than leaving it implied.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest


@pytest.fixture
def inject_hook(plugin_root: Path) -> Path:
    return plugin_root / "hooks" / "synaptory-inject-protocols.sh"


def _sentinel(hook_env) -> Path:
    return (
        Path(hook_env["CLAUDE_PROJECT_DIR"])
        / ".synaptory"
        / ".orchestrator"
        / ".auth-check-ok"
    )


def _write_sentinel(hook_env, age_seconds: int, upn: str = "stub@synaptory.test") -> Path:
    """A sentinel in the format the cached era wrote. Nothing reads it now."""
    p = _sentinel(hook_env)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("%d %s" % (int(time.time()) - age_seconds, upn), encoding="utf-8")
    return p


def _whoami_calls(log: Path) -> list[str]:
    if not log.exists():
        return []
    return log.read_text(encoding="utf-8").splitlines()


def _network_checks(log: Path) -> list[str]:
    return [c for c in _whoami_calls(log) if "--check" in c]


@pytest.mark.hook
def test_every_dispatch_runs_the_check(
    inject_hook, hook_env, stub_cli, subagent_stdin, run_hook
):
    log = stub_cli.bodies_dir.parent / "whoami.log"
    env = {**hook_env, "SYNAPTORY_STUB_WHOAMI_LOG": str(log)}

    result = run_hook(inject_hook, env=env, stdin=subagent_stdin)
    assert result.returncode == 0, f"hook failed: {result.stderr}"
    assert _network_checks(log) == ["whoami --check"]


@pytest.mark.hook
def test_a_failing_check_blocks(
    inject_hook, hook_env, stub_cli, subagent_stdin, run_hook
):
    env = {**hook_env, "SYNAPTORY_STUB_WHOAMI_FAIL": "1"}

    result = run_hook(inject_hook, env=env, stdin=subagent_stdin)
    assert result.returncode == 1
    assert "BLOCKED" in result.stdout


@pytest.mark.hook
def test_a_fresh_sentinel_cannot_skip_the_check(
    inject_hook, hook_env, stub_cli, subagent_stdin, run_hook
):
    """The finding that ended the cache, in its final form.

    A TTL-fresh sentinel with the SAME UPN and a locally live session still
    must not authorize a dispatch when the check itself fails. Under the
    cached gate this proceeded with no check at all.
    """
    _write_sentinel(hook_env, age_seconds=10)
    env = {**hook_env, "SYNAPTORY_STUB_WHOAMI_FAIL_CHECK": "1"}

    result = run_hook(inject_hook, env=env, stdin=subagent_stdin)
    assert result.returncode == 1, "a fresh sentinel must not mask a failing check"
    assert "BLOCKED" in result.stdout


@pytest.mark.hook
def test_a_fresh_sentinel_does_not_suppress_the_call(
    inject_hook, hook_env, stub_cli, subagent_stdin, run_hook
):
    """Complements the above: even on the happy path the check still runs, so
    no residual sentinel-reading survives anywhere in the block."""
    _write_sentinel(hook_env, age_seconds=10)
    log = stub_cli.bodies_dir.parent / "whoami.log"
    env = {**hook_env, "SYNAPTORY_STUB_WHOAMI_LOG": str(log)}

    result = run_hook(inject_hook, env=env, stdin=subagent_stdin)
    assert result.returncode == 0, f"hook failed: {result.stderr}"
    assert _network_checks(log) == ["whoami --check"]


@pytest.mark.hook
def test_a_failing_check_removes_a_stale_sentinel(
    inject_hook, hook_env, stub_cli, subagent_stdin, run_hook
):
    """The file is inert, but leaving one on disk invites a future reader."""
    sentinel = _write_sentinel(hook_env, age_seconds=10)
    env = {**hook_env, "SYNAPTORY_STUB_WHOAMI_FAIL": "1"}

    run_hook(inject_hook, env=env, stdin=subagent_stdin)
    assert not sentinel.exists()


@pytest.mark.hook
def test_the_gate_writes_no_new_sentinel(
    inject_hook, hook_env, stub_cli, subagent_stdin, run_hook
):
    result = run_hook(inject_hook, env=hook_env, stdin=subagent_stdin)
    assert result.returncode == 0, f"hook failed: {result.stderr}"
    assert not _sentinel(hook_env).exists(), "nothing may recreate the cache"


@pytest.mark.hook
def test_the_gate_is_not_a_revocation_check(
    inject_hook, hook_env, stub_cli, subagent_stdin, run_hook
):
    """Pinned so the docstring cannot drift into a stronger claim.

    `whoami --check` returns from the local keychain record for a healthy
    token and never asks the control plane, so a session revoked server-side
    still passes this gate. That is caught at the next authenticated call, not
    here. If this ever becomes a real remote check, this test should fail and
    be rewritten deliberately.
    """
    log = stub_cli.bodies_dir.parent / "whoami.log"
    env = {**hook_env, "SYNAPTORY_STUB_WHOAMI_LOG": str(log)}

    result = run_hook(inject_hook, env=env, stdin=subagent_stdin)
    assert result.returncode == 0
    assert _network_checks(log) == ["whoami --check"], (
        "the gate asks the CLI, and the CLI answers locally"
    )


@pytest.mark.hook
def test_no_gate_env_bypasses_entirely(
    inject_hook, hook_env, stub_cli, subagent_stdin, run_hook
):
    log = stub_cli.bodies_dir.parent / "whoami.log"
    env = {
        **hook_env,
        "SYNAPTORY_AUTH_NO_GATE": "1",
        "SYNAPTORY_STUB_WHOAMI_FAIL": "1",
        "SYNAPTORY_STUB_WHOAMI_LOG": str(log),
    }

    result = run_hook(inject_hook, env=env, stdin=subagent_stdin)
    assert result.returncode == 0, "the documented CI/offline bypass must hold"
    assert _whoami_calls(log) == []
