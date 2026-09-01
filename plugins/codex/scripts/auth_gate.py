#!/usr/bin/env python3
"""Fail-closed Synaptory authentication gate shared by Codex adapters."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

CP_URL_PLACEHOLDER = "SYNAPTORY_CP_URL_PLACEHOLDER"
STAMPED_CP_URL_FILE = Path(__file__).resolve().parent.parent / "hooks" / "lib" / "cp-url"


def _control_plane_url(cp_url_file: Path | None = None) -> tuple[str | None, str]:
    """Resolve cp-url.local then the operator stamp; ignore runtime redirects."""

    stamped_file = cp_url_file or STAMPED_CP_URL_FILE
    candidates = (
        (stamped_file.with_name("cp-url.local"), "hooks/lib/cp-url.local"),
        (stamped_file, "build-stamped hooks/lib/cp-url"),
    )
    last_source = candidates[-1][1]
    for path, source in candidates:
        try:
            candidate = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        last_source = source
        if candidate and candidate != CP_URL_PLACEHOLDER:
            return candidate, source
    return None, last_source


def _is_loopback_url(raw: str | None) -> bool:
    if not raw:
        return False
    try:
        host = (urlsplit(raw).hostname or "").lower()
    except ValueError:
        return False
    return host == "localhost" or host == "::1" or host.startswith("127.")


def _path_is_local_plugin(cp_url_file: Path) -> bool:
    parts = cp_url_file.resolve().parts
    return any(
        parts[index : index + 2] == ("plugins", "local")
        for index in range(max(0, len(parts) - 1))
    )


def _candidate_paths(name: str) -> list[Path]:
    candidates: list[Path] = []
    discovered = shutil.which(name)
    if discovered:
        candidates.append(Path(discovered))
    # A local channel is intentionally installed beside the production PATH
    # binary. Probe the sibling before the fixed GUI locations so a custom
    # SYNAPTORY_CLI_PREFIX layout remains usable.
    if name == "synaptory-local":
        prod = shutil.which("synaptory")
        if prod:
            candidates.append(Path(prod).parent / name)
    candidates.extend((Path.home() / ".local" / "bin" / name, Path.home() / "bin" / name))
    out: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        value = str(candidate.expanduser())
        if value not in seen:
            out.append(Path(value))
            seen.add(value)
    return out


def _cli_identity_url(candidate: Path, *, timeout: float = 2.0) -> str | None:
    """Read a CLI's immutable URL identity without exposing auth state."""

    try:
        result = subprocess.run(
            [str(candidate), "status"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in result.stdout.splitlines():
        if line.startswith("control_plane_url:"):
            value = line.split(":", 1)[1].strip()
            return value or None
    return None


def _cli_resolution(
    cp_url_file: Path | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Return (path, failure_kind, reason) for this plugin's CLI channel."""

    stamped_file = cp_url_file or STAMPED_CP_URL_FILE
    control_plane, _source = _control_plane_url(stamped_file)
    local_plugin = _is_loopback_url(control_plane) or _path_is_local_plugin(stamped_file)
    override = os.environ.get("SYNAPTORY_CLI_BIN", "").strip()
    if override:
        path = Path(override).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            identity = _cli_identity_url(path)
            # Tiny explicit CI shims do not implement `status`; explicit means
            # the caller accepts that test boundary. Real CLIs expose an
            # identity and must still match the plugin channel.
            if identity is not None and _is_loopback_url(identity) != local_plugin:
                return (
                    None,
                    "cli_control_plane_mismatch",
                    f"explicit CLI build identity {identity} does not match this plugin",
                )
            return str(path), None, None
        return None, "cli_unavailable", "SYNAPTORY_CLI_BIN is not executable"

    name = "synaptory-local" if local_plugin else "synaptory"
    for candidate in _candidate_paths(name):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            identity = _cli_identity_url(candidate)
            if identity is None:
                return (
                    None,
                    "cli_identity_unreadable",
                    f"{name} does not expose a readable build identity",
                )
            if local_plugin and not _is_loopback_url(identity):
                return (
                    None,
                    "cli_control_plane_mismatch",
                    f"local plugin refuses production-stamped CLI {identity}",
                )
            if not local_plugin and _is_loopback_url(identity):
                return (
                    None,
                    "cli_control_plane_mismatch",
                    "production plugin refuses a loopback-stamped `synaptory` CLI "
                    f"({identity}); restore production and keep local as synaptory-local (#108)",
                )
            return str(candidate), None, None
    return (
        None,
        "cli_unavailable",
        f"{name} CLI is unavailable",
    )


def _cli_path(cp_url_file: Path | None = None) -> str | None:
    return _cli_resolution(cp_url_file)[0]


def authentication_status(
    project: Path | None = None,
    *,
    timeout: float = 2.0,
    cp_url_file: Path | None = None,
) -> dict[str, Any]:
    """Return a non-sensitive, fail-closed authentication decision.

    ``SYNAPTORY_AUTH_NO_GATE=1`` is the explicit offline/unit-test escape
    hatch used by the repository test harness. Production execution must have
    an operator-stamped control-plane URL and a successful
    ``synaptory whoami --check`` against the cached CLI session.
    """

    control_plane, control_plane_source = _control_plane_url(cp_url_file)
    cli, cli_failure_kind, cli_reason = _cli_resolution(cp_url_file)
    if os.environ.get("SYNAPTORY_AUTH_NO_GATE") == "1":
        return {
            "ready": True,
            "bypassed": True,
            "control_plane_configured": control_plane is not None,
            "control_plane_source": control_plane_source,
            "cli_available": cli is not None,
        }

    if not control_plane:
        return {
            "ready": False,
            "bypassed": False,
            "control_plane_configured": False,
            "control_plane_source": control_plane_source,
            "cli_available": cli is not None,
            "failure_kind": "control_plane_unconfigured",
            "reason": "build-stamped hooks/lib/cp-url is blank or still a placeholder",
        }

    if not cli:
        return {
            "ready": False,
            "bypassed": False,
            "control_plane_configured": True,
            "control_plane_source": control_plane_source,
            "cli_available": False,
            "failure_kind": cli_failure_kind or "cli_unavailable",
            "reason": cli_reason or "synaptory CLI is unavailable",
        }

    command_env = os.environ.copy()
    try:
        result = subprocess.run(
            [cli, "whoami", "--check"],
            cwd=str(project) if project else None,
            env=command_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "ready": False,
            "bypassed": False,
            "control_plane_configured": True,
            "control_plane_source": control_plane_source,
            "cli_available": True,
            "cli_command": Path(cli).name,
            "failure_kind": "whoami_check_failed",
            "reason": f"synaptory whoami --check failed: {type(exc).__name__}",
        }
    if result.returncode != 0:
        return {
            "ready": False,
            "bypassed": False,
            "control_plane_configured": True,
            "control_plane_source": control_plane_source,
            "cli_available": True,
            "cli_command": Path(cli).name,
            "failure_kind": "session_missing_or_expired",
            "reason": "synaptory session is missing or expired",
        }
    return {
        "ready": True,
        "bypassed": False,
        "control_plane_configured": True,
        "control_plane_source": control_plane_source,
        "cli_available": True,
        "cli_command": Path(cli).name,
    }


def authentication_block_message(status: dict[str, Any]) -> str:
    reason = str(status.get("reason") or "Synaptory authentication failed")
    failure_kind = status.get("failure_kind")
    if failure_kind == "control_plane_unconfigured":
        remediation = (
            "Install a correctly built Synaptory plugin or contact your "
            "Synaptory operator, then retry"
        )
    elif failure_kind == "cli_unavailable":
        remediation = "Install or repair the Synaptory CLI, then retry"
    elif failure_kind in {"cli_control_plane_mismatch", "cli_identity_unreadable"}:
        remediation = (
            "Restore production as `synaptory`, install local as "
            "`synaptory-local`, and retry"
        )
    elif failure_kind == "session_missing_or_expired":
        cli_command = str(status.get("cli_command") or "synaptory")
        remediation = (
            f"Run `{cli_command} login`, verify `{cli_command} whoami --check`, "
            "and retry"
        )
    else:
        remediation = (
            "Retry `synaptory whoami --check`; if it still fails, repair the "
            "Synaptory CLI or contact your Synaptory operator"
        )
    return (
        f"synaptory: {reason}. {remediation}. "
        "Delivery is refused while unauthenticated."
    )
