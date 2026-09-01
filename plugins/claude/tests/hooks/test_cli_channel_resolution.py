"""The plugin selects a CLI whose immutable control-plane stamp matches it."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


def _plugin(tmp_path: Path, stamped_url: str) -> Path:
    root = tmp_path / "plugin"
    hooks = root / "hooks"
    (hooks / "lib").mkdir(parents=True)
    source_hooks = Path(__file__).resolve().parents[2] / "hooks"
    shutil.copy2(source_hooks / "_resolve-cli.sh", hooks / "_resolve-cli.sh")
    shutil.copy2(source_hooks / "_cp-url.sh", hooks / "_cp-url.sh")
    (hooks / "lib" / "cp-url").write_text(stamped_url + "\n", encoding="utf-8")
    return root


def _cli(path: Path, stamped_url: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "status" ]; then\n'
        f"  printf 'control_plane_url:  {stamped_url}\\n'\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _run(
    root: Path,
    bindir: Path,
    *,
    override: str = "",
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(root.parent / "home"),
            "PATH": f"{bindir}:/usr/bin:/bin",
            "SYNAPTORY_CLI_BIN": override,
        }
    )
    env.pop("SYNAPTORY_CP_ENV", None)
    env.pop("SYNAPTORY_CONTROL_PLANE_URL", None)
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(root / "hooks" / "_resolve-cli.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.hook
def test_loopback_plugin_selects_local_sibling(tmp_path: Path):
    root = _plugin(tmp_path, "http://localhost:8080")
    bindir = tmp_path / "bin"
    _cli(bindir / "synaptory", "https://synaptory.h3t.co")
    local = _cli(bindir / "synaptory-local", "http://localhost:8080")

    result = _run(root, bindir)

    assert result.returncode == 0, result.stderr
    assert result.stdout == str(local)


@pytest.mark.hook
def test_production_plugin_rejects_loopback_binary_named_synaptory(tmp_path: Path):
    root = _plugin(tmp_path, "https://synaptory.h3t.co")
    bindir = tmp_path / "bin"
    _cli(bindir / "synaptory", "http://localhost:8080")

    result = _run(root, bindir)

    assert result.returncode != 0
    assert "issue #108" in result.stderr
    assert "synaptory-local" in result.stderr


@pytest.mark.hook
def test_production_plugin_selects_production_binary(tmp_path: Path):
    root = _plugin(tmp_path, "https://synaptory.h3t.co")
    bindir = tmp_path / "bin"
    prod = _cli(bindir / "synaptory", "https://synaptory.h3t.co")
    _cli(bindir / "synaptory-local", "http://localhost:8080")

    result = _run(root, bindir)

    assert result.returncode == 0, result.stderr
    assert result.stdout == str(prod)


@pytest.mark.hook
def test_production_stamp_ignores_ambient_dev_redirect(tmp_path: Path):
    root = _plugin(tmp_path, "https://synaptory.h3t.co")
    bindir = tmp_path / "bin"
    prod = _cli(bindir / "synaptory", "https://synaptory.h3t.co")
    _cli(bindir / "synaptory-local", "http://localhost:8080")

    result = _run(
        root,
        bindir,
        extra_env={
            "SYNAPTORY_CP_ENV": "dev",
            "SYNAPTORY_CONTROL_PLANE_URL": "http://localhost:8080",
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == str(prod)


@pytest.mark.hook
def test_explicit_test_shim_without_status_identity_remains_supported(tmp_path: Path):
    root = _plugin(tmp_path, "http://localhost:8080")
    bindir = tmp_path / "bin"
    shim = bindir / "fixture-cli"
    shim.parent.mkdir(parents=True)
    shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    shim.chmod(0o755)

    result = _run(root, bindir, override=str(shim))

    assert result.returncode == 0, result.stderr
    assert result.stdout == str(shim)
