"""Static contracts for the local release/E2E harness.

These checks keep the development-only identity provider and the CLI build
metadata wired into the unified build without adding the fixture service to
the production stack.
"""

from pathlib import Path

import pytest


@pytest.mark.build
def test_fixture_idp_is_built_and_local_only(repo_root: Path) -> None:
    launcher = (repo_root / "plugin" / "synaptory").read_text(encoding="utf-8")
    local_compose = (
        repo_root / "infra" / "compose" / "docker-compose.local.yml"
    ).read_text(encoding="utf-8")
    production_compose = (
        repo_root / "infra" / "compose" / "docker-compose.yml"
    ).read_text(encoding="utf-8")

    assert "fixture-idp:infra/docker/Dockerfile.fixture-idp:api" in launcher
    assert "  fixture-idp:" in local_compose
    assert "synaptory-fixture-idp:${IMAGE_TAG}" in local_compose
    assert "urllib.request.urlopen('http://localhost:9000/healthz'" in local_compose
    assert "  fixture-idp:" not in production_compose
    assert "synaptory-fixture-idp" not in production_compose


@pytest.mark.build
def test_e2e_uses_one_fixture_identity_contract(repo_root: Path) -> None:
    launcher = (repo_root / "plugin" / "synaptory").read_text(encoding="utf-8")

    expected_exports = (
        'export SYNAPTORY_CP_ENTRA_ISSUER="http://fixture-idp:9000"',
        'export SYNAPTORY_CP_ENTRA_JWKS_URL="http://fixture-idp:9000/.well-known/jwks.json"',
        'export SYNAPTORY_CP_ENTRA_USERS_GROUP="synaptory-users"',
        'export SYNAPTORY_CP_ENTRA_ADMINS_GROUP="synaptory-admins"',
        'export FIXTURE_URL="http://localhost:9000"',
    )
    for export in expected_exports:
        assert export in launcher


@pytest.mark.build
def test_local_deploy_health_gate_is_service_scoped_and_fail_closed(
    repo_root: Path,
) -> None:
    launcher = (repo_root / "plugin" / "synaptory").read_text(encoding="utf-8")

    assert 'ps api --format json' in launcher
    assert 'ps web --format json' in launcher
    assert launcher.count('grep -q \'"Health":"healthy"\'') >= 2
    assert "did not become healthy within 120 seconds" in launcher
    assert 'if [ "$waited" -ge 120 ]' in launcher


@pytest.mark.build
def test_e2e_deploy_keeps_bash_errexit_enabled(repo_root: Path) -> None:
    launcher = (repo_root / "plugin" / "synaptory").read_text(encoding="utf-8")
    e2e_command = launcher[launcher.index("cmd_e2e() {"):launcher.index("cmd_help() {")]

    assert "cmd_deploy local ||" not in e2e_command
    assert "\n  cmd_deploy local\n" in e2e_command


@pytest.mark.build
def test_cli_build_refreshes_channel_marker_with_each_binary(repo_root: Path) -> None:
    build_script = (repo_root / "cli" / "scripts" / "build-all.sh").read_text(
        encoding="utf-8"
    )

    assert 'marker="dist/.synaptory-${os}-${arch}.channel"' in build_script
    assert 'printf \'%s\\n\' "${channel:-stable}" > "$marker"' in build_script
