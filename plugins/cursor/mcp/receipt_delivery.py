#!/usr/bin/env python3
"""Validated Cursor receipt delivery through the durable Synaptory CLI outbox.

The project receipt remains the local evidence record. The control plane gets
only the small analytics projection needed by Activity, Cost, and Quality; it
never receives transcript content, artifact paths, commands, summaries, or
findings from this adapter.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from auth_status import _cli_path, _control_plane_url, authentication_status

DELIVERY_SCHEMA_VERSION = 1
DELIVERY_PREFIX = "cursor-analytics-v1:"
HOST_BACKEND = "cursor"
EVIDENCE_GATE_KEYS = (
    "tests_pass",
    "build_succeeds",
    "no_critical_findings",
    "code_reviewed",
    "coverage_no_decrease",
)
TOKEN_KEYS = ("input", "output", "cache_read", "cache_write")
SAFE_SCALAR_FIELDS = (
    "story_id",
    "role",
    "model",
    "dispatch_id",
    "status",
    "completed_at",
    "workstream_id",
)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _delivery_id(receipt: dict[str, Any], source_digest: str) -> str:
    """Return a stable id for one governed dispatch.

    Receipt files gain host-observed attribution before validation.  A plugin
    cachebuster must not turn that harmless metadata rewrite into a second
    analytics invocation, so dispatch identity takes precedence over raw file
    bytes.  The digest fallback is retained for older receipts without a
    dispatch id.
    """

    dispatch_id = receipt.get("dispatch_id")
    if isinstance(dispatch_id, str) and dispatch_id.strip():
        identity = {
            "backend": HOST_BACKEND,
            "dispatch_id": dispatch_id.strip(),
            "role": str(receipt.get("role") or ""),
            "story_id": str(receipt.get("story_id") or ""),
        }
        seed = json.dumps(identity, separators=(",", ":"), sort_keys=True)
    else:
        seed = source_digest
    return DELIVERY_PREFIX + _sha256(
        ("synaptory-cursor-analytics-v1\0" + seed).encode("utf-8")
    )


def analytics_projection(receipt: dict[str, Any], source_digest: str) -> dict[str, Any]:
    """Return the strict, non-content analytics projection for one receipt."""

    projected: dict[str, Any] = {
        "analytics_schema_version": DELIVERY_SCHEMA_VERSION,
        "delivery_id": _delivery_id(receipt, source_digest),
        "backend": HOST_BACKEND,
    }
    for key in SAFE_SCALAR_FIELDS:
        value = receipt.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            projected[key] = value

    token_usage = receipt.get("token_usage")
    if isinstance(token_usage, dict):
        safe_tokens: dict[str, Any] = {}
        for key in TOKEN_KEYS:
            value = token_usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                safe_tokens[key] = value
        if safe_tokens:
            projected["token_usage"] = safe_tokens

    story_dod = receipt.get("story_dod")
    if isinstance(story_dod, dict):
        safe_dod = {
            key: story_dod[key]
            for key in EVIDENCE_GATE_KEYS
            if isinstance(story_dod.get(key), bool)
        }
        if safe_dod:
            projected["story_dod"] = safe_dod

    return projected


def _unquoted_yaml_scalar(raw: str) -> str:
    value = raw.split("#", 1)[0].strip().strip("\"'")
    return value if value not in {"", "{}", "[]", "null", "~"} else ""


def project_binding(project: Path) -> tuple[str | None, str]:
    """Resolve the control-plane slug without parsing nested YAML keys."""

    configured = os.environ.get("SYNAPTORY_PROJECT_ID", "").strip()
    if configured:
        return configured, "SYNAPTORY_PROJECT_ID"
    try:
        lines = (project / ".synaptory.yaml").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, "missing .synaptory.yaml"
    legacy: str | None = None
    for line in lines:
        if not line or line[0].isspace() or line.startswith("#"):
            continue
        if line.startswith("project_id:"):
            value = _unquoted_yaml_scalar(line[len("project_id:") :])
            return (value or None), "top-level project_id"
        if line.startswith("project:"):
            # Older Synaptory demos used `project: slug`. The current config
            # schema uses `project:` as an object, whose empty value must never
            # be treated as a control-plane binding.
            value = _unquoted_yaml_scalar(line[len("project:") :])
            if value:
                legacy = value
    return (legacy, "legacy top-level project") if legacy else (None, "unconfigured")


def delivery_status(
    project: Path, *, authentication: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Describe whether the authenticated CLI delivery boundary is usable."""

    auth = authentication or authentication_status(project)
    if auth.get("bypassed"):
        # The bypass exists for offline/unit tests. Never turn it into an
        # accidental external-write capability, but keep local test execution
        # available and explicit in doctor output.
        return {
            "ready": True,
            "enabled": False,
            "mode": "test-bypass",
            "data_scope": "analytics projection only",
            "reason": "authentication bypass disables external receipt delivery",
        }
    control_plane, source = _control_plane_url()
    cli = _cli_path()
    project_id, project_source = project_binding(project)
    ready = bool(auth.get("ready") and control_plane and cli and project_id)
    return {
        "ready": ready,
        "enabled": ready,
        "mode": "authenticated-cli-outbox" if ready else "unavailable",
        "data_scope": "analytics projection only",
        "control_plane_source": source,
        "project_binding_configured": project_id is not None,
        "project_binding_source": project_source,
        "reason": (
            None
            if ready
            else str(
                auth.get("reason")
                or (
                    "project_id is not configured in .synaptory.yaml"
                    if project_id is None
                    else "receipt delivery is unavailable"
                )
            )
        ),
    }


def _sentinel_path(receipt_path: Path) -> Path:
    return Path(str(receipt_path) + ".shipped")


def _handed_off_marker(
    receipt_path: Path, source_digest: str, delivery_id: str
) -> dict[str, Any] | None:
    """Return an authoritative handoff marker, including older formats.

    Once a governed receipt path has been durably handed off it is immutable
    for delivery purposes.  Cursor may subsequently refresh local attribution
    metadata (for example a plugin cachebuster); that must not re-upload the
    same dispatch.  Corrections require a new governed dispatch/receipt.
    """

    sentinel = _sentinel_path(receipt_path)
    if not sentinel.is_file():
        return None
    try:
        marker = json.loads(sentinel.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Compatibility with Claude/Cursor's zero-byte mtime sentinel.
        try:
            if sentinel.stat().st_mtime_ns >= receipt_path.stat().st_mtime_ns:
                return {"delivery_id": delivery_id, "legacy": True}
            return None
        except OSError:
            return None
    if not isinstance(marker, dict):
        return None
    marker_delivery_id = marker.get("delivery_id")
    if marker.get("source_sha256") == source_digest:
        return marker
    if (
        isinstance(marker_delivery_id, str)
        and marker_delivery_id.startswith(DELIVERY_PREFIX)
        and isinstance(marker.get("handed_off_at"), str)
    ):
        return marker
    return None


def _atomic_marker(path: Path, payload: dict[str, Any]) -> None:
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def ship_validated_receipt(
    project: Path,
    receipt_path: Path,
    *,
    timeout: float = 6.0,
) -> dict[str, Any]:
    """Hand one validated receipt to the CLI or its durable outbox.

    A successful CLI exit means either the API accepted the receipt or the CLI
    durably queued it. Only then is the shared ``.shipped`` sentinel written.
    """

    status = delivery_status(project)
    if status["mode"] == "test-bypass":
        return {
            "handed_off": True,
            "skipped": True,
            "mode": status["mode"],
            "data_scope": status["data_scope"],
        }
    if not status["ready"]:
        return {
            "handed_off": False,
            "error": status["reason"],
            "mode": status["mode"],
            "data_scope": status["data_scope"],
        }

    orchestrator = (project / ".synaptory" / ".orchestrator").resolve()
    if receipt_path.is_symlink():
        return {"handed_off": False, "error": "receipt delivery refuses symlinks"}
    try:
        path = receipt_path.resolve(strict=True)
        path.relative_to(orchestrator)
        raw = path.read_bytes()
        receipt = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return {
            "handed_off": False,
            "error": f"receipt delivery preflight failed: {type(exc).__name__}",
        }
    if not isinstance(receipt, dict):
        return {"handed_off": False, "error": "receipt delivery requires a JSON object"}
    if receipt.get("backend") != HOST_BACKEND:
        return {"handed_off": False, "error": "receipt delivery requires backend: cursor"}

    source_digest = _sha256(raw)
    delivery_id = _delivery_id(receipt, source_digest)
    marker = _handed_off_marker(path, source_digest, delivery_id)
    if marker is not None:
        return {
            "handed_off": True,
            "already_handed_off": True,
            "delivery_id": marker.get("delivery_id") or delivery_id,
            "mode": status["mode"],
            "data_scope": status["data_scope"],
        }

    projected = analytics_projection(receipt, source_digest)
    encoded = json.dumps(projected, separators=(",", ":"), sort_keys=True).encode("utf-8")
    temp_dir = orchestrator / ".delivery-tmp"
    if temp_dir.exists() and temp_dir.is_symlink():
        return {"handed_off": False, "error": "receipt delivery temp directory is a symlink"}
    temp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        temp_dir.chmod(0o700)
    except OSError:
        pass
    handle, temp_name = tempfile.mkstemp(prefix=".cursor-", suffix=".upload", dir=str(temp_dir))
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "wb") as stream:
            stream.write(encoded)
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())

        cli = _cli_path()
        control_plane, _source = _control_plane_url()
        if not cli or not control_plane:
            return {"handed_off": False, "error": "receipt delivery boundary disappeared"}
        command_env = os.environ.copy()
        project_id, _project_source = project_binding(project)
        if not project_id:
            return {"handed_off": False, "error": "receipt project binding disappeared"}
        command_env["SYNAPTORY_PROJECT_ID"] = project_id
        try:
            result = subprocess.run(
                [cli, "telemetry", "receipt", "--file", temp_name, "--quiet"],
                cwd=str(project),
                env=command_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                "handed_off": False,
                "error": f"synaptory CLI receipt handoff failed: {type(exc).__name__}",
            }
        if result.returncode != 0:
            detail = ""
            if result.stderr:
                lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
                if lines:
                    detail = lines[-1].replace(str(project), "<project>")[:240]
            return {
                "handed_off": False,
                "error": (
                    f"synaptory CLI receipt handoff exited {result.returncode}"
                    + (f": {detail}" if detail else "")
                ),
            }
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass

    _atomic_marker(
        _sentinel_path(path),
        {
            "schema_version": DELIVERY_SCHEMA_VERSION,
            "source_sha256": source_digest,
            "payload_sha256": _sha256(encoded),
            "delivery_id": delivery_id,
            "handed_off_at": datetime.now(timezone.utc).isoformat(),
            "data_scope": "analytics projection only",
        },
    )
    return {
        "handed_off": True,
        "already_handed_off": False,
        "delivery_id": delivery_id,
        "mode": status["mode"],
        "data_scope": status["data_scope"],
        "fields": sorted(projected),
    }
