#!/usr/bin/env python3
"""Validated Codex receipt delivery through the durable Synaptory CLI outbox.

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

from auth_gate import _cli_path, _control_plane_url, authentication_status

DELIVERY_SCHEMA_VERSION = 1
DELIVERY_PREFIX = "codex-analytics-v1:"
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
    # `cycle_id` REPLACES THE RETIRED LANE IDENTITY (ADR-035). A Work Unit
    # belongs to a Cycle, and the Cycle is what the control plane can join an
    # analytics row back to. Projecting the old field instead would have left
    # every Codex row silently unattributed rather than visibly wrong, which is
    # the harder failure to notice.
    "cycle_id",
    # THE ATTEMPT THIS RECEIPT CAME FROM (#396). Both are opaque identifiers
    # the control plane already stores on the attempt row, and without them the
    # analytics row cannot be checked against the attempt at all: a
    # cross-family execution recorded under the wrong backend was invisible
    # from the control plane's own data, which is where #346's runtime identity
    # has to be visible.
    "attempt_id",
    "adapter_profile_id",
    "model_identity_version",
    "runtime_family",
    "runtime_version",
)


HOST_BACKEND = "codex"


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _within(path: Path, root: Path) -> bool:
    """Containment by `relative_to`, not `str.startswith`.

    Spelled out because this package is the one that still runs on Python 3.9
    and the obvious alternatives are each wrong in a different way: prefix
    comparison admits a sibling named `cyclesX`, and `is_relative_to` is a
    3.9 addition whose absence would surface here as an AttributeError inside
    an `except (OSError, ValueError)`.
    """
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


#: The receipt vocabulary (`core/receipt-schema/evidence-contract.json`). A
#: backend outside it is not canonicalized into the projection, because the
#: analytics row is what the control plane groups on.
KNOWN_BACKENDS = ("claude", "cursor", "codex", "gemini")


def resolved_backend(receipt: dict) -> str:
    """The backend that actually RAN this receipt's work.

    The projection hardcoded the host's own backend, so a Cursor host shipping
    a Codex-executed receipt recorded `backend: cursor` while the attempt row
    beside it said `runtime_family: codex`. The control plane then grouped a
    cross-family execution under the host that dispatched it, which is the one
    fact #346 exists to make visible (#396).

    The receipt's own backend is used when it carries an attempt binding, which
    is the same condition `_backend_admissible` admits it on: by the time this
    runs the receipt has passed `validate_receipt`, and the kernel's
    family-mismatch guard has already checked that backend against the profile
    the dispatch selected. Anything unbound, or naming a backend outside the
    receipt vocabulary, projects as the host's own rather than inventing a
    grouping key.
    """
    backend = str(receipt.get("backend") or "")
    if backend == HOST_BACKEND or backend not in KNOWN_BACKENDS:
        return HOST_BACKEND
    if receipt.get("attempt_id") and receipt.get("adapter_profile_id"):
        return backend
    return HOST_BACKEND


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
            "backend": resolved_backend(receipt),
            "dispatch_id": dispatch_id.strip(),
            "role": str(receipt.get("role") or ""),
            "story_id": str(receipt.get("story_id") or ""),
        }
        seed = json.dumps(identity, separators=(",", ":"), sort_keys=True)
    else:
        seed = source_digest
    return DELIVERY_PREFIX + _sha256(
        ("synaptory-codex-analytics-v1\0" + seed).encode("utf-8")
    )


def analytics_projection(receipt: dict[str, Any], source_digest: str) -> dict[str, Any]:
    """Return the strict, non-content analytics projection for one receipt."""

    projected: dict[str, Any] = {
        "analytics_schema_version": DELIVERY_SCHEMA_VERSION,
        "delivery_id": _delivery_id(receipt, source_digest),
        "backend": resolved_backend(receipt),
    }
    for key in SAFE_SCALAR_FIELDS:
        value = receipt.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            projected[key] = value

    # Preserve typed attribution without admitting arbitrary nested content.
    for field, keys in (("model", ("kind", "value", "source", "evidence_class")),
                        ("model_route", ("requested", "resolved_model_id"))):
        value = receipt.get(field)
        if isinstance(value, dict):
            projected[field] = {key: value[key] for key in keys if isinstance(value.get(key), str)}

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
    for delivery purposes.  Codex may subsequently refresh local attribution
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


def _normalize_token_usage(receipt_path: Path) -> bool:
    """Canonicalize `token_usage` names and backfill `stage`. True if changed.

    Delegates to the SAME `transcript_usage` the Claude and Cursor hooks call.
    A second implementation here is how three hosts come to disagree about what
    a stage is -- which is the class of defect #331 is about.
    """
    try:
        import transcript_usage  # type: ignore
    except Exception:  # noqa: BLE001 - composed runtime may predate the allowlist entry
        return False
    try:
        return bool(transcript_usage.patch_receipt(str(receipt_path)))
    except Exception:  # noqa: BLE001 - never block delivery on normalization
        return False


def _backend_admissible(receipt: dict) -> bool:
    """Whether this host may ship a receipt naming that backend.

    The host's own backend, always. And a receipt from a GOVERNED CROSS-FAMILY
    DISPATCH, which names the runtime that ran rather than the host that
    dispatched it: #346 is the capability that a host may invoke a runtime of
    another family, and a rule comparing the receipt's backend to the host's
    refused exactly the receipts that capability produces. The kernel already
    admits them, so the delivery path was the one place that still assumed a
    host only ever ships its own runtime's work (#396).

    What makes such a receipt admissible is not its backend value but its
    ATTEMPT BINDING. `attempt_id` and `adapter_profile_id` are written by the
    dispatch the kernel authorized, so a receipt carrying them is provably the
    product of one. A receipt some other tool dropped in the directory has
    neither, which is the case this rule exists to refuse, and refusing it on
    provenance is stronger than refusing it on a name it could simply have
    written differently.
    """
    backend = str(receipt.get("backend") or "")
    if backend == HOST_BACKEND:
        return True
    return bool(receipt.get("attempt_id")) and bool(receipt.get("adapter_profile_id"))


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

    # The same two roots the kernel and the MCP boundary allow (#766). This
    # one is the quietest of the three: a refusal here is reported as
    # `preflight failed: ValueError` with `handed_off: False`, so leaving it
    # behind would not have failed a test -- delivery runs in test-bypass --
    # it would have stopped every committed receipt from reaching the control
    # plane in the field, which is where receipts are the evidence a barrier
    # credits.
    _base = project / ".synaptory"
    orchestrator = (_base / ".orchestrator").resolve()
    # Only the READ side gained a root. The delivery scratch directory below
    # stays under `.orchestrator` deliberately: it is working state, and the
    # committed tree is the one a reviewer reads in a diff.
    _roots = [orchestrator, (_base / "cycles").resolve()]
    if receipt_path.is_symlink():
        return {"handed_off": False, "error": "receipt delivery refuses symlinks"}
    try:
        path = receipt_path.resolve(strict=True)
        if not any(_within(path, root) for root in _roots):
            raise ValueError("receipt path is outside the directories the runtime owns")
        raw = path.read_bytes()
        receipt = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return {
            "handed_off": False,
            "error": f"receipt delivery preflight failed: {type(exc).__name__}",
        }
    if not isinstance(receipt, dict):
        return {"handed_off": False, "error": "receipt delivery requires a JSON object"}
    if not _backend_admissible(receipt):
        return {
            "handed_off": False,
            "error": (
                "receipt delivery requires backend: %s, or a governed "
                "cross-family receipt carrying its attempt binding"
                % HOST_BACKEND
            ),
        }

    # Normalize `token_usage` before the digest is taken, so the digest covers
    # what is actually shipped. Claude and Cursor run this in their SubagentStop
    # / SessionEnd hooks; Codex ships through here instead and so ran it
    # NOWHERE, which is why every Codex receipt arrived with no
    # `token_usage.stage` and never reached /cost/by-stage (#331 G9).
    #
    # Best-effort by design, exactly as in the hooks: a normalization failure
    # must not stop a valid receipt from being delivered. An un-staged receipt
    # is worth strictly more than no receipt.
    normalized = _normalize_token_usage(path)
    if normalized:
        try:
            raw = path.read_bytes()
            receipt = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {"handed_off": False,
                    "error": "receipt became unreadable after normalization"}

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
    handle, temp_name = tempfile.mkstemp(prefix=".codex-", suffix=".upload", dir=str(temp_dir))
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
