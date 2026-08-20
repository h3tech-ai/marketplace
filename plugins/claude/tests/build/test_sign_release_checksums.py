"""Layer 2 — `infra/scripts/sign-release-checksums.sh` (#200).

Execs the real script with stubbed `python3` / `docker` / `curl` on PATH and
asserts on exit code, the `.sig` file, and — the whole point of the issue —
that a failure explains itself instead of being silently swallowed.

Exit-code contract the build depends on:
  0 → signed
  1 → not signed AND SYNAPTORY_RELEASE_REQUIRE_SIG=true (build must abort)
  2 → not signed, not required (dev-laptop skip)
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

# Any valid base64 works — nothing here verifies Ed25519 maths, only wiring.
GOOD_PUB = "fL6i0+3vPnJsY74wf8AHwS6pRHQFszvaki88LxNUgVM="
OTHER_PUB = "AAAAi0+3vPnJsY74wf8AHwS6pRHQFszvaki88LxNUgVM="
SIG = "D1vvN3FLbIRcgejzrXPkJ6FlVsSbz2V3yoiJpD/HfUvw17i+WdZmA1PjoUuTtiiu4nOM"


def _script(repo_root: Path) -> Path:
    p = repo_root / "infra" / "scripts" / "sign-release-checksums.sh"
    assert p.is_file(), f"missing {p}"
    return p


def _shim(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    p.write_text("#!/usr/bin/env bash\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def env(tmp_path: Path):
    """Sandbox: a manifest to sign plus an empty stub bin dir on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    target = tmp_path / "sha256sums.txt"
    target.write_text("abc123  synaptory-darwin-arm64\n")
    return {
        "bindir": bindir,
        "target": target,
        "sig": tmp_path / "sha256sums.txt.sig",
    }


def _run(repo_root: Path, env, **extra_env):
    e = {
        **os.environ,
        "PATH": f"{env['bindir']}:{os.environ['PATH']}",
        # Default: no CP reachable, so the public-key gate skips unless a
        # test opts in. Keeps each test to one concern.
        "SYNAPTORY_CP_URL": "",
        "SYNAPTORY_RELEASE_REQUIRE_SIG": "",
        **extra_env,
    }
    return subprocess.run(
        ["bash", str(_script(repo_root)), str(env["target"])],
        capture_output=True, text=True, timeout=60, env=e,
    )


# ─── method selection ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_local_signer_wins_when_available(repo_root: Path, env):
    """python3 sign-artifact.py succeeding is enough; docker is never called."""
    _shim(env["bindir"], "python3",
          f'if [[ "$1" == *sign-artifact.py ]]; then printf "%s\\n" "{SIG}" > "$2.sig"; exit 0; fi\n'
          'exit 1\n')
    _shim(env["bindir"], "docker", 'echo "docker MUST NOT be called" >&2; exit 99\n')

    r = _run(repo_root, env)
    assert r.returncode == 0, r.stderr
    assert env["sig"].read_text().strip() == SIG
    assert "via local signer" in r.stdout
    assert "MUST NOT" not in r.stderr


@pytest.mark.unit
def test_falls_back_to_api_container(repo_root: Path, env):
    """The prod path: no local key, so sign inside the running api container.
    The manifest goes in on stdin and never touches the container's disk."""
    _shim(env["bindir"], "python3",
          'if [[ "$1" == *sign-artifact.py ]]; then\n'
          '  echo "sign-artifact: signing failed ([Errno 30] Read-only file system: /app)" >&2\n'
          '  exit 1\n'
          'fi\nexit 1\n')
    _shim(env["bindir"], "docker",
          'if [[ "$1" == "inspect" ]]; then echo true; exit 0; fi\n'
          f'if [[ "$1" == "exec" ]]; then cat >/dev/null; printf "%s %s\\n" "{SIG}" "{GOOD_PUB}"; exit 0; fi\n'
          'exit 1\n')

    r = _run(repo_root, env)
    assert r.returncode == 0, r.stderr
    assert env["sig"].read_text().strip() == SIG
    assert "via container signer" in r.stdout


@pytest.mark.unit
def test_skips_container_when_not_running(repo_root: Path, env):
    """A stopped api container is reported as such, not as an opaque failure."""
    _shim(env["bindir"], "python3", 'echo "no key" >&2; exit 1\n')
    _shim(env["bindir"], "docker",
          'if [[ "$1" == "inspect" ]]; then echo false; exit 0; fi\nexit 1\n')

    r = _run(repo_root, env)
    assert r.returncode == 2
    assert "is not running" in r.stderr


# ─── the actual bug in #200: silence ─────────────────────────────────────────


@pytest.mark.unit
def test_failure_surfaces_every_attempt(repo_root: Path, env):
    """The regression this issue is about. The signer's real error must reach
    the build log — previously `>/dev/null 2>&1` discarded exactly this."""
    _shim(env["bindir"], "python3",
          'echo "sign-artifact: signing failed ([Errno 30] Read-only file system: /app)" >&2\n'
          'exit 1\n')
    _shim(env["bindir"], "docker",
          'if [[ "$1" == "inspect" ]]; then echo true; exit 0; fi\n'
          'if [[ "$1" == "exec" ]]; then cat >/dev/null; echo "kv: AuthorizationFailed" >&2; exit 1; fi\n')

    r = _run(repo_root, env)
    assert r.returncode == 2
    assert "NOT signed" in r.stderr
    assert "Read-only file system" in r.stderr, "local signer's cause was swallowed"
    assert "AuthorizationFailed" in r.stderr, "container signer's cause was swallowed"
    assert not env["sig"].exists()


@pytest.mark.unit
def test_require_sig_fails_the_release(repo_root: Path, env):
    """With SYNAPTORY_RELEASE_REQUIRE_SIG=true an unsignable release must exit
    1, so the build aborts instead of publishing latest.json advertising
    signature_required with no signature to back it."""
    _shim(env["bindir"], "python3", 'echo "no key here" >&2; exit 1\n')
    _shim(env["bindir"], "docker",
          'if [[ "$1" == "inspect" ]]; then echo false; exit 0; fi\nexit 1\n')

    r = _run(repo_root, env, SYNAPTORY_RELEASE_REQUIRE_SIG="true")
    assert r.returncode == 1
    assert "failing the release" in r.stderr
    assert not env["sig"].exists()


@pytest.mark.unit
def test_soft_skip_is_the_default(repo_root: Path, env):
    """Without the flag, a dev laptop with no key still completes (exit 2) so
    `./synaptory build` keeps working offline."""
    _shim(env["bindir"], "python3", 'echo "no key" >&2; exit 1\n')
    _shim(env["bindir"], "docker",
          'if [[ "$1" == "inspect" ]]; then echo false; exit 0; fi\nexit 1\n')

    r = _run(repo_root, env)
    assert r.returncode == 2
    assert "best-effort" in r.stderr


# ─── public-key gate ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_rejects_signature_from_an_unpublished_key(repo_root: Path, env):
    """The dev-key-in-prod failure mode. A signature the CP's published key
    cannot verify is worse than none — with signature_required it would make
    CLIs reject valid downloads — so the .sig must be DELETED, not warned
    about."""
    _shim(env["bindir"], "python3", 'echo "no local key" >&2; exit 1\n')
    _shim(env["bindir"], "docker",
          'if [[ "$1" == "inspect" ]]; then echo true; exit 0; fi\n'
          f'if [[ "$1" == "exec" ]]; then cat >/dev/null; printf "%s %s\\n" "{SIG}" "{OTHER_PUB}"; exit 0; fi\n')
    _shim(env["bindir"], "curl",
          f'printf \'{{"public_key_b64":"{GOOD_PUB}"}}\'\n')

    r = _run(repo_root, env, SYNAPTORY_CP_URL="https://synaptory.h3t.co")
    assert r.returncode == 2
    assert "REFUSING the signature" in r.stderr
    assert not env["sig"].exists(), "a signature CLIs would reject was left on disk"


@pytest.mark.unit
def test_accepts_signature_matching_published_key(repo_root: Path, env):
    """The happy prod path end to end, with the key check actually running."""
    _shim(env["bindir"], "python3", 'echo "no local key" >&2; exit 1\n')
    _shim(env["bindir"], "docker",
          'if [[ "$1" == "inspect" ]]; then echo true; exit 0; fi\n'
          f'if [[ "$1" == "exec" ]]; then cat >/dev/null; printf "%s %s\\n" "{SIG}" "{GOOD_PUB}"; exit 0; fi\n')
    _shim(env["bindir"], "curl",
          f'printf \'{{"public_key_b64":"{GOOD_PUB}"}}\'\n')

    r = _run(repo_root, env, SYNAPTORY_CP_URL="https://synaptory.h3t.co")
    assert r.returncode == 0, r.stderr
    assert "public-key check OK" in r.stdout
    assert env["sig"].read_text().strip() == SIG


@pytest.mark.unit
def test_unreachable_cp_does_not_block_signing(repo_root: Path, env):
    """Reachability is best-effort; only a genuine mismatch is fatal."""
    _shim(env["bindir"], "python3", 'echo "no local key" >&2; exit 1\n')
    _shim(env["bindir"], "docker",
          'if [[ "$1" == "inspect" ]]; then echo true; exit 0; fi\n'
          f'if [[ "$1" == "exec" ]]; then cat >/dev/null; printf "%s %s\\n" "{SIG}" "{GOOD_PUB}"; exit 0; fi\n')
    _shim(env["bindir"], "curl", 'exit 7\n')  # connection refused

    r = _run(repo_root, env, SYNAPTORY_CP_URL="https://unreachable.invalid")
    assert r.returncode == 0, r.stderr
    assert "public-key check skipped" in r.stdout


@pytest.mark.unit
def test_signer_that_reports_no_public_key_is_refused(repo_root: Path, env):
    """Regression for a bug in this script's own first draft: the gate derived
    the public key in an inline python snippet that lacked sign-artifact.py's
    sys.path setup, ImportError'd, and printed 'check skipped' — letting a
    dev-key signature through the check written to stop it. A signer that does
    not report its key must be REFUSED, not skipped."""
    # Local signer succeeds but prints no `pubkey_b64=` line.
    _shim(env["bindir"], "python3",
          f'if [[ "$1" == *sign-artifact.py ]]; then printf "%s\\n" "{SIG}" > "$2.sig"; '
          'echo "sign-artifact: wrote $2.sig"; exit 0; fi\nexit 1\n')
    _shim(env["bindir"], "docker", 'exit 1\n')
    _shim(env["bindir"], "curl", f'printf \'{{"public_key_b64":"{GOOD_PUB}"}}\'\n')

    r = _run(repo_root, env, SYNAPTORY_CP_URL="https://synaptory.h3t.co")
    assert r.returncode == 2
    assert "did not report its" in r.stderr
    assert "skipped" not in r.stdout, "an unverifiable key must not be silently skipped"
    assert not env["sig"].exists()


@pytest.mark.unit
def test_local_signer_public_key_is_checked(repo_root: Path, env):
    """The local method's key must go through the same gate as the container's
    — this is the dev-key-in-prod path, verified against real keys manually."""
    _shim(env["bindir"], "python3",
          f'if [[ "$1" == *sign-artifact.py ]]; then printf "%s\\n" "{SIG}" > "$2.sig"; '
          f'echo "pubkey_b64={OTHER_PUB}"; exit 0; fi\nexit 1\n')
    _shim(env["bindir"], "docker", 'exit 1\n')
    _shim(env["bindir"], "curl", f'printf \'{{"public_key_b64":"{GOOD_PUB}"}}\'\n')

    r = _run(repo_root, env, SYNAPTORY_CP_URL="https://synaptory.h3t.co")
    assert r.returncode == 2
    assert "does not publish" in r.stderr
    assert not env["sig"].exists()
