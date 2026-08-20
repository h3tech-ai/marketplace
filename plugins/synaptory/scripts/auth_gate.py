#!/usr/bin/env python3
"""Fail-closed Synaptory authentication gate shared by Codex adapters."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

CP_URL_PLACEHOLDER = "SYNAPTORY_CP_URL_PLACEHOLDER"
STAMPED_CP_URL_FILE = Path(__file__).resolve().parent.parent / "hooks" / "lib" / "cp-url"


def _control_plane_url(cp_url_file: Path | None = None) -> tuple[str | None, str]:
    """Resolve the operator-stamped URL, with the established dev-only override."""

    stamped_file = cp_url_file or STAMPED_CP_URL_FILE
    try:
        stamped = stamped_file.read_text(encoding="utf-8").strip()
    except OSError:
        stamped = ""

    env_url = os.environ.get("SYNAPTORY_CONTROL_PLANE_URL", "").strip()
    if os.environ.get("SYNAPTORY_CP_ENV") == "dev" and env_url:
        candidate = env_url
        source = "development environment override"
    else:
        candidate = stamped
        source = "build-stamped hooks/lib/cp-url"

    if not candidate or candidate == CP_URL_PLACEHOLDER:
        return None, source
    return candidate, source


def _cli_path() -> str | None:
    override = os.environ.get("SYNAPTORY_CLI_BIN", "").strip()
    if override:
        path = Path(override).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        return None
    return shutil.which("synaptory")


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
    if os.environ.get("SYNAPTORY_AUTH_NO_GATE") == "1":
        return {
            "ready": True,
            "bypassed": True,
            "control_plane_configured": control_plane is not None,
            "control_plane_source": control_plane_source,
            "cli_available": _cli_path() is not None,
        }

    if not control_plane:
        return {
            "ready": False,
            "bypassed": False,
            "control_plane_configured": False,
            "control_plane_source": control_plane_source,
            "cli_available": _cli_path() is not None,
            "failure_kind": "control_plane_unconfigured",
            "reason": "build-stamped hooks/lib/cp-url is blank or still a placeholder",
        }

    cli = _cli_path()
    if not cli:
        return {
            "ready": False,
            "bypassed": False,
            "control_plane_configured": True,
            "control_plane_source": control_plane_source,
            "cli_available": False,
            "failure_kind": "cli_unavailable",
            "reason": "synaptory CLI is unavailable",
        }

    command_env = os.environ.copy()
    command_env["SYNAPTORY_CONTROL_PLANE_URL"] = control_plane
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
            "failure_kind": "session_missing_or_expired",
            "reason": "synaptory session is missing or expired",
        }
    return {
        "ready": True,
        "bypassed": False,
        "control_plane_configured": True,
        "control_plane_source": control_plane_source,
        "cli_available": True,
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
    elif failure_kind == "session_missing_or_expired":
        remediation = (
            "Run `synaptory login`, verify `synaptory whoami --check`, and retry"
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
