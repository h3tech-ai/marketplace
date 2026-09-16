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


def _unstamped_plugin(tmp_path: Path) -> Path:
    """A source-tree sideload: hooks present, no cp-url written."""
    root = tmp_path / "plugin"
    hooks = root / "hooks"
    (hooks / "lib").mkdir(parents=True)
    source_hooks = Path(__file__).resolve().parents[2] / "hooks"
    shutil.copy2(source_hooks / "_resolve-cli.sh", hooks / "_resolve-cli.sh")
    shutil.copy2(source_hooks / "_cp-url.sh", hooks / "_cp-url.sh")
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


@pytest.mark.hook
def test_unstamped_tree_resolves_no_cli(tmp_path: Path):
    """Issue #320: "no stamp" is not "production".

    A source-tree sideload used to fall through to the production `synaptory`
    on PATH and ship SessionStart telemetry for a project prod never heard of.
    """
    root = _unstamped_plugin(tmp_path)
    bindir = tmp_path / "bin"
    _cli(bindir / "synaptory", "https://synaptory.h3t.co")

    result = _run(root, bindir)

    assert result.returncode != 0
    assert result.stdout.strip() == ""
    assert "#320" in result.stderr
    assert "SYNAPTORY_CLI_BIN" in result.stderr


@pytest.mark.hook
def test_unstamped_tree_still_honours_explicit_cli_override(tmp_path: Path):
    """CI and e2e runs have no tree to stamp; the escape hatch must survive."""
    root = _unstamped_plugin(tmp_path)
    bindir = tmp_path / "bin"
    shim = bindir / "fixture-cli"
    shim.parent.mkdir(parents=True)
    shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    shim.chmod(0o755)

    result = _run(root, bindir, override=str(shim))

    assert result.returncode == 0, result.stderr
    assert result.stdout == str(shim)


@pytest.mark.hook
def test_an_unusable_override_refuses_rather_than_falling_through(tmp_path: Path):
    """#396: the degraded case between the two tests above.

    The unstamped guard treats any non-empty SYNAPTORY_CLI_BIN as the escape
    hatch, while the override branch only fires when the path is executable.
    A missing or stale value therefore satisfied the guard, skipped the
    override, and fell through to the ordinary production candidate search,
    which is the #320 egress the guard exists to close.

    The assertion that matters is the absence of a fallback: the production
    CLI is on PATH and must not be returned.
    """
    root = _unstamped_plugin(tmp_path)
    bindir = tmp_path / "bin"
    _cli(bindir / "synaptory", "https://synaptory.h3t.co")

    result = _run(root, bindir, override=str(tmp_path / "does" / "not" / "exist"))

    assert result.returncode != 0
    assert result.stdout.strip() == "", "no fallback candidate may be returned"
    assert "SYNAPTORY_CLI_BIN" in result.stderr


@pytest.mark.hook
def test_a_non_executable_override_refuses(tmp_path: Path):
    """The file exists and is not runnable, which a stale build or a lost
    +x bit produces and which reads as "present" to a naive check."""
    root = _unstamped_plugin(tmp_path)
    bindir = tmp_path / "bin"
    _cli(bindir / "synaptory", "https://synaptory.h3t.co")
    dud = tmp_path / "not-executable"
    dud.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    dud.chmod(0o644)

    result = _run(root, bindir, override=str(dud))

    assert result.returncode != 0
    assert result.stdout.strip() == ""


@pytest.mark.hook
def test_an_unusable_override_refuses_even_on_a_stamped_tree(tmp_path: Path):
    """Naming a binary is an instruction about WHICH CLI to use, so an
    unusable one is a stop rather than a hint, stamped or not. Without this
    the refusal would read as a property of unstamped trees."""
    root = _plugin(tmp_path, "https://synaptory.h3t.co")
    bindir = tmp_path / "bin"
    _cli(bindir / "synaptory", "https://synaptory.h3t.co")

    result = _run(root, bindir, override=str(tmp_path / "gone"))

    assert result.returncode != 0
    assert result.stdout.strip() == ""


@pytest.mark.hook
def test_local_sideload_without_stamp_keeps_local_channel(tmp_path: Path):
    """A ~/.cursor/plugins/local tree is local by path, so it still resolves."""
    root = tmp_path / "plugins" / "local" / "synaptory"
    hooks = root / "hooks"
    (hooks / "lib").mkdir(parents=True)
    source_hooks = Path(__file__).resolve().parents[2] / "hooks"
    shutil.copy2(source_hooks / "_resolve-cli.sh", hooks / "_resolve-cli.sh")
    shutil.copy2(source_hooks / "_cp-url.sh", hooks / "_cp-url.sh")
    bindir = tmp_path / "bin"
    _cli(bindir / "synaptory", "https://synaptory.h3t.co")
    local = _cli(bindir / "synaptory-local", "http://localhost:8080")

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "PATH": f"{bindir}:/usr/bin:/bin",
            "SYNAPTORY_CLI_BIN": "",
            "PLUGIN_ROOT": str(root),
        }
    )
    env.pop("SYNAPTORY_CP_ENV", None)
    env.pop("SYNAPTORY_CONTROL_PLANE_URL", None)
    result = subprocess.run(
        ["bash", str(hooks / "_resolve-cli.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == str(local)
