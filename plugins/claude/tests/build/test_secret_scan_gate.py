# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
"""Layer 2 -- the secret-scanning gate detects what it claims to detect.

Two failure modes have already shipped in this repo, and both left the gate
GREEN, which is the property that makes them expensive:

  1. `.gitleaks.toml` carried an allowlist and no `[extend]`, so gitleaks ran
     with zero rules and reported a clean tree for four months while a live
     Entra client secret sat in `.claude/launch.json`. `secret-scan.yml`
     asserts `useDefault` before scanning; this module asserts the same thing
     from the test suite, where it is visible without reading CI logs.

  2. The first working ruleset allowlisted `signing-public-keys.d` BY PATH.
     A directory exemption is not a public-key exemption: it covers whatever
     is placed there later, including the private half of the release trust
     material the directory is named for. The scan below plants a key under
     that path and requires a finding.

An allowlist by VALUE cannot rot this way -- the rest of the file stays
scanned, so a real credential added next to a documented dummy is still
caught. That is why `.gitleaks.toml` says so and why this module enforces it.
"""
import json
import pathlib
import shutil
import subprocess

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_CONFIG = _REPO_ROOT / ".gitleaks.toml"
_GITIGNORE = _REPO_ROOT / ".gitignore"

#: Assembled rather than written out, so this file does not itself carry a
#: string the scanner is meant to fire on.
_PEM_BEGIN = "-----BEGIN " + "RSA PRIVATE" + " KEY-----"
_PEM_END = "-----END " + "RSA PRIVATE" + " KEY-----"
_PLANTED_KEY = "\n".join([_PEM_BEGIN] + ["QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2Nzg5"] * 8 + [_PEM_END]) + "\n"


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")
@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")
@pytest.mark.parametrize("relative_path", [
    "cli/internal/cli/traces_test.go",
    "cli/internal/keychain/keychain_fallback_test.go",
])
def test_session_token_fixtures_pass_but_a_real_private_key_is_detected(tmp_path, relative_path):
    """Dummy tokens pass while a generated private key in either file fails."""
    scan_root = tmp_path / "scan"
    target = scan_root / relative_path
    target.parent.mkdir(parents=True)
    shutil.copy(_REPO_ROOT / relative_path, target)
    shutil.copy(_CONFIG, scan_root / ".gitleaks.toml")
    report = tmp_path / "findings.json"
    command = [
        "gitleaks", "dir", ".", "--config", ".gitleaks.toml",
        "--redact", "--no-banner", "--report-format", "json",
        "--report-path", str(report),
    ]
    clean = subprocess.run(command, cwd=scan_root, capture_output=True, text=True)
    assert clean.returncode == 0, clean.stdout + clean.stderr

    # Generate disposable key material at runtime; never persist it in the repo.
    private_key = subprocess.run(
        ["openssl", "genrsa", "2048"],
        capture_output=True, text=True, check=True,
    ).stdout
    with target.open("a", encoding="utf-8") as handle:
        handle.write("\nvar plantedPrivateKey = `" + private_key + "`\n")
    planted = subprocess.run(command, cwd=scan_root, capture_output=True, text=True)
    assert planted.returncode == 1, planted.stdout + planted.stderr
    findings = json.loads(report.read_text(encoding="utf-8"))
    assert any(
        finding["RuleID"] == "private-key" and finding["File"] == relative_path
        for finding in findings
    ), findings


def _toml():
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.9 runners
        pytest.skip("tomllib requires Python 3.11+")
    with _CONFIG.open("rb") as handle:
        return tomllib.load(handle)


def test_the_default_ruleset_is_enabled():
    """Without this the config defines no rules and every scan passes."""
    assert _toml().get("extend", {}).get("useDefault") is True, (
        "gitleaks REPLACES its built-in rules with this config. Dropping "
        "[extend] useDefault = true makes the gate report a clean tree for "
        "any input, which is how a live Entra secret survived four months."
    )


def test_every_path_allowlist_names_a_directory_git_refuses_to_track():
    """A path exemption may only cover material that cannot be committed.

    Allowlisting by path suspends every rule for everything under it, now and
    later. The one case that survives is a gitignored directory holding local
    dev key material: `gitleaks dir` walks it because it does not read
    .gitignore, yet nothing there can reach a commit or a PR range. Anything
    else must be allowlisted by value, so the file around it stays scanned.
    """
    ignored = _GITIGNORE.read_text(encoding="utf-8").splitlines()
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=_REPO_ROOT,
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    for entry in _toml().get("allowlists", []):
        for raw in entry.get("paths", []):
            path = raw.replace("\\.", ".").rstrip("/")
            assert any(line.strip().rstrip("/") == path for line in ignored), (
                f"{path!r} is allowlisted by path but is not gitignored, so "
                f"a secret committed there would pass the gate. Allowlist the "
                f"specific non-secret VALUE instead."
            )
            assert not [f for f in tracked if f == path or f.startswith(path + "/")], (
                f"{path!r} is allowlisted by path and has tracked files under "
                f"it; the exemption covers every one of them."
            )


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")
def test_a_key_planted_under_the_signing_key_directory_is_detected(tmp_path):
    """The reproduction the path allowlist used to defeat.

    Scanned in a mirror of the repo's own relative layout, because gitleaks
    matches allowlist paths against the path relative to the scan root. A
    reinstated directory exemption makes this scan exit 0 and the test fail.
    """
    planted = tmp_path / "plugin-claude" / "hooks" / "data" / "signing-public-keys.d"
    planted.mkdir(parents=True)
    (planted / "planted.pem").write_text(_PLANTED_KEY, encoding="utf-8")
    shutil.copy(_CONFIG, tmp_path / ".gitleaks.toml")

    result = subprocess.run(
        ["gitleaks", "dir", ".", "--config", ".gitleaks.toml",
         "--redact", "--no-banner"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode != 0, (
        "a private key under signing-public-keys.d was not reported. The "
        "directory holds the PUBLIC half of the release trust material; an "
        "exemption on it hides the private half.\n"
        + result.stdout + result.stderr
    )


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")
def test_the_committed_public_key_needs_no_exemption(tmp_path):
    """The control, and the reason the exemption can simply go.

    A PUBLIC key PEM matches no default rule, so the directory it lives in
    never needed suspending. If some future committed body does trip a rule,
    the answer is its exact value, not its path.
    """
    data = tmp_path / "plugin-claude" / "hooks" / "data"
    (data / "signing-public-keys.d").mkdir(parents=True)
    for src in sorted((_REPO_ROOT / "plugin-claude" / "hooks" / "data").glob("signing-public-key*")):
        if src.is_dir():
            shutil.copytree(src, data / src.name, dirs_exist_ok=True)
        else:
            shutil.copy(src, data / src.name)
    shutil.copy(_CONFIG, tmp_path / ".gitleaks.toml")

    result = subprocess.run(
        ["gitleaks", "dir", ".", "--config", ".gitleaks.toml",
         "--redact", "--no-banner"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        "the committed signing public keys tripped a rule; allowlist the "
        "exact value rather than restoring the path exemption.\n"
        + result.stdout + result.stderr
    )
