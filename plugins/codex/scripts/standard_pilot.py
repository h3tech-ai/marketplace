#!/usr/bin/env python3
"""Fail-closed operational preflight for a standard Codex staff pilot.

This checker is deliberately read-only. Installation readiness combines the
installed plugin's doctor result with unauthenticated HTTPS deployment probes.
Dispatch readiness reports runtime doctor availability and refusal reasons.
Response bodies, credentials, project receipts, and source content are never
collected or printed.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    """Keep certification bound to the operator-reviewed target URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _validated_url(raw: str, *, expected_origin: str | None = None) -> SplitResult:
    parsed = urlsplit(raw)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("staff-pilot endpoints must use HTTPS")
    if parsed.username or parsed.password:
        raise ValueError("staff-pilot endpoints must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("staff-pilot endpoints must not contain query strings or fragments")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if expected_origin is not None and origin != expected_origin:
        raise ValueError("all staff-pilot endpoints must share one reviewed origin")
    return parsed


def _probe(url: str, *, expected_statuses: set[int], timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    status: int | None = None
    error: str | None = None
    request = Request(url, headers={"User-Agent": "synaptory-codex-standard-pilot/1"})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            status = int(response.status)
    except HTTPError as exc:
        status = int(exc.code)
    except (OSError, TimeoutError, URLError) as exc:
        error = type(exc).__name__
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    return {
        "status": status,
        "latency_ms": elapsed_ms,
        "passed": status in expected_statuses,
        **({"error": error} if error else {}),
    }


def _doctor(project: Path) -> dict[str, Any]:
    from mcp_server import tool_doctor

    result = tool_doctor({"project_dir": str(project)})
    if not isinstance(result, dict):
        raise ValueError("Synaptory doctor returned a non-object")
    return result


def _dispatch_readiness(project: Path, *, timeout: float) -> dict[str, Any]:
    # _doctor has loaded the host's shared runtime and channel-aware CLI resolver.
    from mcp_server import _runtime_cli
    from staff_pilot import dispatch_readiness

    return dispatch_readiness(project, cli=_runtime_cli(), timeout=timeout)


def certification_report(
    project: Path,
    health_url: str,
    protected_urls: list[str],
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Return minimized standard-profile operational certification evidence."""

    project = project.resolve(strict=True)
    if not (project / ".synaptory.yaml").is_file():
        raise ValueError("project is missing .synaptory.yaml")
    if not protected_urls:
        raise ValueError("at least one protected endpoint is required")
    if timeout <= 0 or timeout > 30:
        raise ValueError("timeout must be greater than 0 and at most 30 seconds")

    health = _validated_url(health_url)
    origin = f"{health.scheme}://{health.netloc}"
    protected = [
        _validated_url(url, expected_origin=origin) for url in protected_urls
    ]

    doctor = _doctor(project)
    checks = doctor.get("checks") if isinstance(doctor.get("checks"), dict) else {}
    runtime = checks.get("codex_runtime") if isinstance(checks.get("codex_runtime"), dict) else {}
    authentication = (
        checks.get("authentication")
        if isinstance(checks.get("authentication"), dict)
        else {}
    )
    delivery = (
        checks.get("control_plane_receipt_delivery")
        if isinstance(checks.get("control_plane_receipt_delivery"), dict)
        else {}
    )
    profiles = (
        checks.get("custom_agent_profiles")
        if isinstance(checks.get("custom_agent_profiles"), dict)
        else {}
    )
    configuration = (
        checks.get("configuration")
        if isinstance(checks.get("configuration"), dict)
        else {}
    )
    project_status = (
        doctor.get("project") if isinstance(doctor.get("project"), dict) else {}
    )
    missing = doctor.get("missing_requirements")
    if not isinstance(missing, list):
        missing = ["doctor did not return missing_requirements"]

    doctor_passed = bool(
        doctor.get("structured_execution_ready") is True
        and doctor.get("status_ready") is True
        and project_status.get("regulated") is False
        and runtime.get("meets_minimum") is True
        and runtime.get("multi_agent_enabled") is True
        and authentication.get("ready") is True
        and authentication.get("bypassed") is not True
        and delivery.get("ready") is True
        and delivery.get("enabled") is True
        and delivery.get("data_scope") == "analytics projection only"
        and checks.get("pipeline_state_readable") is True
        and checks.get("regulated_execution_allowed") is True
        and checks.get("custom_agent_profiles_installed") is True
        and profiles.get("installed") is True
        and configuration.get("ok") is True
        and checks.get("receipt_gate_enabled") is True
        and not missing
    )

    health_probe = _probe(
        health_url, expected_statuses={200}, timeout=timeout
    )
    protected_probes = [
        {
            "path": parsed.path or "/",
            **_probe(url, expected_statuses={401, 403}, timeout=timeout),
        }
        for parsed, url in zip(protected, protected_urls)
    ]
    ready_to_install = bool(
        doctor_passed
        and health_probe["passed"]
        and all(item["passed"] for item in protected_probes)
    )

    dispatch = _dispatch_readiness(project, timeout=timeout)

    return {
        "schema_version": 2,
        "profile": "standard-non-regulated",
        "ready_to_install": ready_to_install,
        "ready_to_dispatch": dispatch["ready"],
        "dispatch": dispatch,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "doctor": {
            "passed": doctor_passed,
            "execution_scope": doctor.get("execution_scope"),
            "structured_execution_ready": doctor.get("structured_execution_ready") is True,
            "codex_version": runtime.get("version"),
            "multi_agent_enabled": runtime.get("multi_agent_enabled") is True,
            "authentication_ready": authentication.get("ready") is True,
            "authentication_bypassed": authentication.get("bypassed") is True,
            "receipt_delivery_ready": delivery.get("ready") is True,
            "receipt_delivery_mode": delivery.get("mode"),
            "receipt_data_scope": delivery.get("data_scope"),
            "managed_profiles_installed": profiles.get("installed") is True,
            "configuration_valid": configuration.get("ok") is True,
            "regulated": project_status.get("regulated") is True,
            "missing_requirements": [str(item) for item in missing],
        },
        "deployment": {
            "origin": origin,
            "health": {"path": health.path or "/", **health_probe},
            "protected": protected_probes,
        },
        "regulated_execution": {
            "certified": False,
            "policy": "fail-closed-refusal",
        },
        "data_handling": {
            "response_bodies_collected": False,
            "credentials_sent": False,
            "project_content_collected": False,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    parser.add_argument("--deployment-health-url", required=True)
    parser.add_argument(
        "--protected-url",
        action="append",
        default=[],
        help="Unauthenticated same-origin URL expected to return 401 or 403; repeatable.",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = certification_report(
            args.project_dir,
            args.deployment_health_url,
            list(args.protected_url),
            timeout=args.timeout,
        )
    except (OSError, ValueError) as exc:
        report = {
            "schema_version": 2,
            "profile": "standard-non-regulated",
            "ready_to_install": False,
            "ready_to_dispatch": False,
            "error": str(exc),
            "regulated_execution": {
                "certified": False,
                "policy": "fail-closed-refusal",
            },
        }
    except Exception as exc:  # noqa: BLE001 - report unexpected failures safely
        report = {
            "schema_version": 2,
            "profile": "standard-non-regulated",
            "ready_to_install": False,
            "ready_to_dispatch": False,
            "error": f"certification check failed: {type(exc).__name__}",
            "regulated_execution": {
                "certified": False,
                "policy": "fail-closed-refusal",
            },
        }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("ready_to_install") and report.get("ready_to_dispatch") else 1


if __name__ == "__main__":
    raise SystemExit(main())
