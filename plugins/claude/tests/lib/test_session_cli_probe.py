# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
"""Layer 1 — a slow `synaptory status` is not a wrong `synaptory` (#396).

`session_manager._resolve_cli_binary` is the SECOND copy of the channel
identity probe. #568 mechanism 3 fixed the first one in `gate_emitter` and
left this one recorded in a comment, so the product path that logs into the
control plane kept the defect: a `subprocess.TimeoutExpired` was caught
alongside `OSError` and returned the same `False` as a genuine mismatch. The
candidate was dropped, `ControlPlaneBackend` got no CLI path, and the error it
raised told the reader to install a CLI that was already on PATH and correctly
stamped.

The probe now lives in `cli_probe` and both resolvers use it, so the two
cannot drift again. What is asserted here is the behaviour at THIS call site:
a timeout is retried once, survives as a distinct reason when it persists, and
reaches the operator as "could not tell" rather than "not found".

The shims are real subprocesses because the defect was about process
admission, not about the work `status` does.
"""
from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest

import cli_probe

# `session_manager` imports `access_token_client` at module level and that
# module was REMOVED from the tree, so the module cannot be imported anywhere,
# by any host, and `session_manager.py <verb>` exits on the import. Nothing in
# the shipped tree imports or invokes it (verified by grep across hooks,
# scripts and shell), so the resolver below is not on a live product path
# today. It is still worth fixing and holding: the defect is in the source, the
# probe it duplicated is now shared, and a revived module must not revive the
# bug with it. The stub is the minimum that makes the module importable, and it
# is named here rather than hidden in a conftest so this caveat travels with
# the tests.
sys.modules.setdefault("access_token_client", types.ModuleType("access_token_client"))

import session_manager as sm  # noqa: E402

PROD_URL = "https://synaptory.h3t.co"
LOCAL_URL = "http://localhost:8080"
TIGHT_CEILING = "0.5"


def _warm(path: Path) -> None:
    """Pay the first-exec scan before anything is timed.

    A freshly written executable is scanned on its first `execve` on macOS.
    Left unpaid that alone blows a sub-second ceiling, and every test here
    would pass for the wrong reason.
    """
    subprocess.run([str(path), "--warm"], capture_output=True)


def _shim(path: Path, url: str, *, delay: float = 0.0, slow_calls: int = 0) -> Path:
    """A CLI shim whose first `slow_calls` `status` probes sleep past the ceiling.

    The counter lives in a sibling file so it survives the separate process
    each probe spawns. `#!/bin/sh` with an explicit absolute PATH: these tests
    point PATH at a scratch bindir, where `env` and `sleep` are not found, and
    that failure is silent enough to mimic the thing under test.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    counter = path.parent / (path.name + ".probes")
    path.write_text(
        "#!/bin/sh\n"
        "PATH=/usr/bin:/bin\n"
        'if [ "$1" = status ]; then\n'
        f"  n=$(cat {counter})\n"
        f"  echo $((n + 1)) > {counter}\n"
        f'  if [ "$n" -lt {slow_calls} ]; then sleep {delay}; fi\n'
        f'  printf "control_plane_url:  {url}\\n"\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    _warm(path)
    counter.write_text("0", encoding="utf-8")
    return path


@pytest.fixture
def bindir(tmp_path, monkeypatch):
    """A scratch PATH holding only the shims a test writes."""
    d = tmp_path / "bin"
    d.mkdir()
    monkeypatch.setenv("PATH", str(d))
    monkeypatch.delenv("SYNAPTORY_CLI_BIN", raising=False)
    # `_resolve_cli_binary` also probes ~/.local/bin and ~/bin. Point HOME at
    # the scratch tree so a real installed CLI cannot answer for the shim.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return d


@pytest.fixture
def tight_probe(monkeypatch):
    monkeypatch.setenv("SYNAPTORY_CLI_PROBE_TIMEOUT", TIGHT_CEILING)


@pytest.mark.unit
def test_a_timeout_verdict_is_not_a_mismatch_verdict(bindir, tight_probe):
    """The distinction the fix rests on, asserted on the probe itself."""
    slow = _shim(bindir / "slow", PROD_URL, delay=3.0, slow_calls=99)
    prod = _shim(bindir / "synaptory", PROD_URL)

    assert cli_probe.probe_channel_identity(str(slow), False, 0.5) == cli_probe.TIMEOUT
    assert cli_probe.probe_channel_identity(str(prod), False, 10.0) == cli_probe.MATCH
    assert cli_probe.probe_channel_identity(str(prod), True, 10.0) == cli_probe.MISMATCH


@pytest.mark.unit
def test_one_slow_probe_still_resolves_the_cli(bindir, tight_probe):
    """The transient spike, which used to reject the only candidate there was.

    It now costs a second probe rather than the whole resolution, because a
    timeout is a statement about the machine and machines recover.
    """
    _shim(bindir / "synaptory", PROD_URL, delay=3.0, slow_calls=1)

    path, reason = sm.resolve_cli_binary(PROD_URL)

    assert path == str(bindir / "synaptory"), (
        "a single slow probe rejected a correctly stamped binary; that is the "
        "defect, one level in from where #568 fixed it"
    )
    assert reason == ""


@pytest.mark.unit
def test_a_persistently_slow_probe_reports_a_timeout_not_a_missing_cli(
    bindir, tight_probe
):
    """The persistent arm. The answer is "could not tell", and it is carried."""
    _shim(bindir / "synaptory", PROD_URL, delay=3.0, slow_calls=99)

    path, reason = sm.resolve_cli_binary(PROD_URL)

    assert path is None
    assert reason == sm.UNRESOLVED_TIMEOUT, (
        "a persistent timeout collapsed into the same empty reason as 'there "
        "is no CLI', which is what made the operator message wrong"
    )


@pytest.mark.unit
def test_a_genuine_mismatch_still_reports_no_reason(bindir, tight_probe):
    """The refusal #320 exists for must not be weakened into a timeout.

    A local-stamped binary answering a production runtime is an ANSWER. It is
    rejected, and the reason stays empty because there is nothing here that
    could not be told.
    """
    _shim(bindir / "synaptory", LOCAL_URL)

    path, reason = sm.resolve_cli_binary(PROD_URL)

    assert path is None
    assert reason == ""


@pytest.mark.unit
def test_a_second_candidate_that_answers_still_resolves(bindir, tight_probe):
    """One unprobeable candidate does not end the search.

    `synaptory-local` on PATH is tried first for a loopback URL, and the
    sibling beside the prod binary is tried next. A timeout on the first must
    not discard the second, which is why the reason is remembered rather than
    raised out of the loop.
    """
    _shim(bindir / "synaptory-local", LOCAL_URL, delay=3.0, slow_calls=99)
    home_bin = Path.home() / ".local" / "bin"
    _shim(home_bin / "synaptory-local", LOCAL_URL)

    path, reason = sm.resolve_cli_binary(LOCAL_URL)

    assert path == str(home_bin / "synaptory-local")
    assert reason == ""


@pytest.mark.unit
def test_the_backend_says_could_not_tell_rather_than_not_found(bindir, tight_probe):
    """The operator-facing half, which is the whole point of carrying it.

    `ControlPlaneBackend` still falls back to local, which is safe. What it
    must not do is send the reader to install a CLI that is on PATH and
    correctly stamped.
    """
    _shim(bindir / "synaptory", PROD_URL, delay=3.0, slow_calls=99)

    backend = sm.ControlPlaneBackend(PROD_URL)
    assert backend.cli_path is None
    assert backend.cli_unresolved == sm.UNRESOLVED_TIMEOUT

    with pytest.raises(sm.SessionError) as raised:
        backend._run_cli(["whoami"])
    message = str(raised.value)
    assert "not found on PATH" not in message, message
    assert "could not confirm" in message and "could not tell" in message, message
    assert "SYNAPTORY_CLI_PROBE_TIMEOUT" in message, message


@pytest.mark.unit
def test_a_real_missing_cli_still_says_so(bindir):
    """The control. Accurate diagnosis in one arm is worth nothing if the
    other arm inherits it."""
    backend = sm.ControlPlaneBackend(PROD_URL)
    assert backend.cli_path is None
    assert backend.cli_unresolved == ""

    with pytest.raises(sm.SessionError) as raised:
        backend._run_cli(["whoami"])
    assert "not found on PATH" in str(raised.value)


@pytest.mark.unit
def test_url_classification_parses_the_host_rather_than_matching_a_substring():
    """`localhost.example.com` is not loopback, and used to read as one.

    The old `_cli_url_is_local` said True for anything CONTAINING "localhost"
    or "127.", so a production host with either in its name selected the local
    CLI name and then rejected the prod binary as mismatched.
    """
    assert sm._is_local_url("http://localhost:8080") is True
    assert sm._is_local_url("http://127.0.0.1:3000") is True
    assert sm._is_local_url("https://synaptory.h3t.co") is False
    assert sm._is_local_url("https://localhost.example.com") is False
    assert sm._is_local_url("https://cp.example.com/127.0.0.1") is False


@pytest.mark.unit
def test_both_resolvers_share_one_probe():
    """The reason this cannot drift a third time.

    `gate_emitter` re-binds the names for its own readers and tests; what
    matters is that the function object is the one `cli_probe` defines.
    """
    import gate_emitter as ge

    assert ge._probe_channel_identity is cli_probe.probe_channel_identity
    assert ge._TIMEOUT is cli_probe.TIMEOUT
    assert ge._Unprobeable is cli_probe.Unprobeable
