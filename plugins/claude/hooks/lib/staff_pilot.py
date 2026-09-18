"""Shared, read-only runtime readiness evidence for staff-pilot preflights."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


def dispatch_readiness(project: Path, *, cli: str, timeout: float) -> dict[str, Any]:
    """Report probe availability by role; never register or persist a snapshot.

    The adapter probe supplies the selector's availability and reason. At least
    one available capability profile means this machine can serve a role; it
    does not authorize a particular dispatch or certify a complete lifecycle.
    """
    unavailable = {
        "ready": False,
        "checked": False,
        "available_roles": [],
        "profiles": [],
    }
    if not cli:
        return {
            **unavailable,
            "error": "No Synaptory CLI matches this plugin installation",
        }
    try:
        result = subprocess.run(
            [
                cli,
                "runtimes",
                "doctor",
                "--read-only",
                "--json",
                "--project-dir",
                str(project),
            ],
            cwd=str(project),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        return {**unavailable, "error": f"Runtime doctor failed: {type(exc).__name__}"}
    if result.returncode != 0:
        # An older CLI must fail closed; never retry without --read-only.
        return {
            **unavailable,
            "error": f"Runtime doctor --read-only failed (exit {result.returncode}); check CLI version and configuration",
        }
    try:
        payload = json.loads(result.stdout)
        reports = payload["reports"]
        if not isinstance(reports, list):
            raise ValueError("reports must be an array")
        profiles = []
        seen = set()
        roles = set()
        for report in reports:
            if not isinstance(report, dict):
                raise ValueError("profile must be an object")
            profile_id = report["profile_id"]
            available = report["available"]
            reason = report.get("reason", "")
            serves = report.get("capability_profiles", [])
            if (
                not isinstance(profile_id, str)
                or not profile_id
                or profile_id in seen
                or type(available) is not bool
                or not isinstance(reason, str)
                or not isinstance(serves, list)
                or any(not isinstance(role, str) or not role for role in serves)
                or (available and not serves)
                or (not available and not reason)
            ):
                raise ValueError("invalid profile evidence")
            seen.add(profile_id)
            profiles.append(
                {
                    "profile_id": profile_id,
                    "available": available,
                    "capability_profiles": serves,
                    # Preserve this verbatim: selection quotes the same probe reason.
                    "reason": reason,
                }
            )
            if available:
                roles.update(serves)
        return {
            "ready": bool(roles),
            "checked": True,
            "available_roles": sorted(roles),
            "profiles": profiles,
        }
    except (ValueError, KeyError, TypeError):
        return {
            **unavailable,
            "error": "Runtime doctor returned invalid readiness evidence",
        }
