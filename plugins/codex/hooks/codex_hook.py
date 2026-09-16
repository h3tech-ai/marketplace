#!/usr/bin/env python3
"""Bounded Codex hook adapter for the native Synaptory host package.

SessionStart supplies a compact governed-execution reminder and opens the CLI
telemetry session. SubagentStart binds the host agent id to the active dispatch
and opens control-plane plus OTLP spans. SubagentStop validates the exact
receipt and closes the OTLP span, while Stop/SessionEnd perform bounded durable
delivery and local bookkeeping.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
HOOK_LIB = Path(__file__).resolve().parent / "lib"
if not (HOOK_LIB / "receipt_validator.py").is_file():
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "core" / "lib"
        if (candidate / "receipt_validator.py").is_file():
            HOOK_LIB = candidate
            break
if str(HOOK_LIB) not in sys.path:
    sys.path.insert(0, str(HOOK_LIB))

from auth_gate import (  # noqa: E402
    _cli_path,
    authentication_block_message,
    authentication_status,
)
from codex_attribution import (  # noqa: E402
    patch_codex_receipt as _shared_patch_codex_receipt,
    transcript_runtime as _shared_transcript_runtime,
)
from otel_writer import (  # noqa: E402
    append_event as _append_otel_event,
    build_end as _build_otel_end,
    build_start as _build_otel_start,
    derive_span_id as _derive_span_id,
    derive_trace_id as _derive_trace_id,
)
from receipt_delivery import project_binding, ship_validated_receipt  # noqa: E402

CONTEXT_CAP_BYTES = 4 * 1024

ROLE_ABBREVIATIONS = {
    "software-engineer": "se",
    "quality-engineer": "qe",
    "code-reviewer": "cr",
    "compliance-engineer": "ce",
    "platform-engineer": "pe",
    "project-owner": "po",
    "solution-architect": "sa",
    "technical-writer": "tw",
    "research-advisor": "ra",
}
ROLE_NAMES = {abbreviation: role for role, abbreviation in ROLE_ABBREVIATIONS.items()}


class AuthenticationRequired(RuntimeError):
    """Raised so the command adapter can block SessionStart with exit 2."""


class ReceiptRequired(RuntimeError):
    """Raised so a governed Codex agent cannot finish without valid evidence."""


def _read_input() -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _project(payload: dict[str, Any]) -> Path:
    candidates: list[Any] = [
        payload.get("cwd"),
        payload.get("project_dir"),
        payload.get("workspace_root"),
    ]
    roots = payload.get("workspace_roots")
    if isinstance(roots, list) and roots:
        candidates.append(roots[0])
    candidates.extend(
        [
            os.environ.get("SYNAPTORY_PROJECT_DIR"),
            os.environ.get("CODEX_PROJECT_DIR"),
            os.environ.get("CLAUDE_PROJECT_DIR"),
            os.getcwd(),
        ]
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            path = Path(candidate).expanduser().resolve()
            if path.is_dir():
                return path
    return Path.cwd().resolve()


def _state_path(project: Path) -> Path:
    return project / ".synaptory" / ".orchestrator" / "pipeline-state.json"


def _read_state(project: Path) -> dict[str, Any]:
    try:
        payload = json.loads(_state_path(project).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _select_state(state: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    if state.get("version") == "3.0" and isinstance(state.get("specs"), dict):
        active = state.get("active_spec")
        if isinstance(active, str):
            selected = state["specs"].get(active)
            if isinstance(selected, dict):
                return selected, active
        return {}, active if isinstance(active, str) else None
    return state, None


def _session_start(project: Path, payload: dict[str, Any]) -> dict[str, Any]:
    state = _read_state(project)
    if not state:
        return {}
    authentication = authentication_status(project)
    if not authentication["ready"]:
        raise AuthenticationRequired(authentication_block_message(authentication))
    _start_telemetry_session(project, payload)
    selected, active_spec = _select_state(state)
    mode = str(state.get("build_mode") or selected.get("build_mode") or "unknown")
    lifecycle = str(selected.get("lifecycle_state") or "unknown")
    sprint = selected.get("current_sprint")
    parts = [
        "Synaptory project state detected.",
        f"Build mode: {mode}.",
        f"Lifecycle: {lifecycle}.",
    ]
    if active_spec:
        parts.append(f"Active spec: {active_spec}.")
    if isinstance(sprint, int) and sprint > 0:
        parts.append(f"Sprint: {sprint}.")
    # SPQ has no `current_sprint`, so the line above is simply absent for an SPQ
    # project: the banner reported build mode and lifecycle but never which
    # Cycle, its goal, or whether the barrier had cleared. State reads only --
    # the SessionStart budget is 3s and a `sync_barrier evaluate` shells out to
    # git, so it must not be called here.
    cycle = selected.get("current_cycle")
    if isinstance(cycle, int) and cycle > 0:
        parts.append(f"Cycle: {cycle}.")
        goal = selected.get("cycle_goal")
        if isinstance(goal, str) and goal.strip():
            parts.append(f"Cycle goal: {goal.strip()}.")
        sync = selected.get("sync")
        if isinstance(sync, dict) and sync.get("verdict"):
            parts.append(f"Sync barrier: {sync['verdict']}.")
        elif lifecycle in ("SYNC", "CHECKPOINT"):
            parts.append("Sync barrier: not yet cleared for this Cycle.")
    parts.append(
        "Use $synaptory for readiness and governed execution. Standard story "
        "work is available only when the doctor reports structured_execution_ready."
    )
    context = " ".join(parts)
    raw = context.encode("utf-8")[:CONTEXT_CAP_BYTES]
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": raw.decode("utf-8", errors="ignore"),
        }
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
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


def _agent_role(payload: dict[str, Any]) -> str | None:
    identity = " ".join(
        str(payload.get(key) or "")
        for key in (
            "agent_type", "agent_role", "agent_name", "agent_path", "name", "description"
        )
    ).lower()
    for full_role, abbreviation in ROLE_ABBREVIATIONS.items():
        if f"synaptory-{full_role}" in identity:
            return abbreviation
    return None


def _read_board(project: Path) -> dict[str, Any]:
    """Read the active flat board through the shared multi-layout resolver."""
    try:
        from story_pipeline import _read_state as read_story_state  # type: ignore

        state = read_story_state(str(project))
    except Exception:
        state, _active_spec = _select_state(_read_state(project))
    return state if isinstance(state, dict) else {}


def _receipts_dir(project: Path) -> Path:
    try:
        from story_pipeline import receipts_dir_for  # type: ignore

        return Path(receipts_dir_for(str(project), intended=False))
    except Exception:
        _selected, active_spec = _select_state(_read_state(project))
        root = project / ".synaptory" / ".orchestrator"
        return root / "specs" / active_spec / "receipts" if active_spec else root / "receipts"


def _active_receipts(
    project: Path, role: str | None = None
) -> list[tuple[str, str, Path, str]]:
    selected = _read_board(project)
    candidates: list[tuple[str, str, dict[str, Any]]] = []
    lifecycle = selected.get("lifecycle_dispatches") or {}
    if isinstance(lifecycle, dict):
        for key, value in lifecycle.items():
            story_id, separator, abbreviation = str(key).rpartition(":")
            if (
                separator
                and isinstance(value, dict)
                and (role is None or abbreviation == role)
            ):
                candidates.append((story_id, abbreviation, value))
    for story in selected.get("current_stories") or []:
        if not isinstance(story, dict):
            continue
        active = story.get("mcp_active_dispatches") or {}
        if not isinstance(active, dict):
            continue
        for abbreviation, value in active.items():
            if isinstance(value, dict) and (role is None or abbreviation == role):
                candidates.append((str(story.get("id") or ""), abbreviation, value))
    receipts = _receipts_dir(project)
    return [
        (
            story_id,
            abbreviation,
            receipts / f"{story_id}-{abbreviation}.json",
            str(dispatch.get("dispatch_id") or ""),
        )
        for story_id, abbreviation, dispatch in candidates
    ]


def _agent_id(payload: dict[str, Any]) -> str:
    for key in ("agent_id", "subagent_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _session_id(payload: dict[str, Any]) -> str:
    for key in ("session_id", "thread_id", "conversation_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _model_id(payload: dict[str, Any]) -> str:
    for key in ("model_id", "model", "subagent_model"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return True


def _run_cli(
    project: Path,
    args: list[str],
    *,
    timeout: float = 6.0,
    offline: bool = False,
) -> subprocess.CompletedProcess[str] | None:
    """Run the role-matched CLI channel without making telemetry a hook gate."""
    cli = _cli_path()
    if not cli:
        return None
    env = os.environ.copy()
    env["SYNAPTORY_IDE"] = "codex"
    project_id, _source = project_binding(project)
    if project_id:
        env["SYNAPTORY_PROJECT_ID"] = project_id
    if offline:
        env["SYNAPTORY_OFFLINE"] = "1"
    try:
        return subprocess.run(
            [cli, *args],
            cwd=str(project),
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _start_telemetry_session(project: Path, payload: dict[str, Any]) -> None:
    project_id, _source = project_binding(project)
    if not project_id:
        return
    args = ["telemetry", "session-start", "--project-id", project_id]
    session_id = _session_id(payload)
    if _valid_uuid(session_id):
        args.extend(("--session-id", session_id))
    _run_cli(project, args)


def _marker_path(project: Path, agent_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", agent_id) or "unknown"
    return (
        project
        / ".synaptory"
        / ".orchestrator"
        / "codex-subagent-markers"
        / f"{safe}.json"
    )


def _consume_marker(project: Path, agent_id: str) -> dict[str, Any] | None:
    if not agent_id:
        return None
    path = _marker_path(project, agent_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    return payload if isinstance(payload, dict) else None


def _write_otel_start(
    project: Path,
    payload: dict[str, Any],
    *,
    role: str,
    story_id: str,
) -> None:
    agent_id = _agent_id(payload)
    if not agent_id:
        return
    session_id = _session_id(payload)
    try:
        _append_otel_event(
            project / ".synaptory",
            session_id,
            _build_otel_start(
                span_id=_derive_span_id(agent_id),
                trace_id=_derive_trace_id(session_id),
                # Reliability intentionally accepts only plugin-governed
                # spans. Preserve that namespace on OTLP while the relational
                # subagent row uses the canonical role name.
                role=f"synaptory:{role}",
                backend="codex",
                model=_model_id(payload),
                story_id=story_id,
                prompt_id=str(payload.get("prompt_id") or "") or None,
            ),
        )
    except OSError:
        pass


def _write_otel_end(
    project: Path,
    payload: dict[str, Any],
    marker: dict[str, Any] | None,
    *,
    status: str,
    message: str = "",
) -> None:
    agent_id = _agent_id(payload)
    if not agent_id:
        return
    session_id = str((marker or {}).get("session_id") or _session_id(payload))
    try:
        _append_otel_event(
            project / ".synaptory",
            session_id,
            _build_otel_end(
                span_id=_derive_span_id(agent_id),
                status=status,
                message=message[:240] or None,
            ),
        )
    except OSError:
        pass


def _subagent_start(project: Path, payload: dict[str, Any]) -> dict[str, Any]:
    role = _agent_role(payload)
    if role is None:
        return {}
    candidates = _active_receipts(project, role)
    if not candidates:
        raise ReceiptRequired(
            "synaptory: governed agent started without an active dispatch contract"
        )
    if len(candidates) != 1:
        raise ReceiptRequired(
            "synaptory: multiple active dispatches match the governed agent identity"
        )
    agent_id = _agent_id(payload)
    if not agent_id:
        raise ReceiptRequired("synaptory: governed agent start has no agent_id")
    story_id, abbreviation, _receipt, dispatch_id = candidates[0]
    role_name = ROLE_NAMES[abbreviation]
    marker = {
        "schema_version": 1,
        "agent_id": agent_id,
        "session_id": _session_id(payload),
        "story_id": story_id,
        "role": role_name,
        "role_abbreviation": abbreviation,
        "dispatch_id": dispatch_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_json(_marker_path(project, agent_id), marker)

    args = [
        "telemetry",
        "subagent-start",
        "--subagent-id",
        agent_id,
        "--role",
        role_name,
        "--backend",
        "codex",
        "--story-id",
        story_id,
    ]
    model = _model_id(payload)
    if model:
        args.extend(("--model", model))
    _run_cli(project, args)
    _write_otel_start(project, payload, role=role_name, story_id=story_id)
    return {}


def _flush_telemetry(project: Path) -> dict[str, Any]:
    otel_dir = project / ".synaptory" / ".orchestrator" / "otel"
    # Session/subagent events may already be in the durable CLI outbox. Drain
    # those first so the session row exists before its trace batch arrives.
    _run_cli(project, ["outbox", "flush"], timeout=10)
    if not otel_dir.is_dir():
        return {}
    for path in sorted(otel_dir.glob("spans-*.jsonl")):
        try:
            if path.stat().st_size == 0:
                continue
        except OSError:
            continue
        _run_cli(project, ["telemetry", "traces", "--file", str(path)], timeout=10)
    return {}


def _transcript_runtime(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract actual model/usage from Codex's SubagentStop transcript.

    Codex 0.147 supplies ``agent_transcript_path`` but not an inline ``usage``
    object. Stream the JSONL and retain only the model plus the last cumulative
    token event so receipts contain host-observed attribution, never estimates.
    """
    return _shared_transcript_runtime(payload)


def _patch_codex_receipt(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return _shared_patch_codex_receipt(path, payload)
    except ValueError as exc:
        raise ReceiptRequired(f"synaptory: {exc}") from exc


def _subagent_stop(project: Path, payload: dict[str, Any]) -> dict[str, Any]:
    agent_id = _agent_id(payload)
    marker = _consume_marker(project, agent_id)
    marker_role = str((marker or {}).get("role_abbreviation") or "")
    role = marker_role if marker_role in ROLE_NAMES else _agent_role(payload)
    candidates = _active_receipts(project, role)
    governed = marker is not None or role is not None or bool(candidates)
    if not governed:
        return {}
    try:
        if marker is not None:
            story_id = str(marker.get("story_id") or "")
            dispatch_id = str(marker.get("dispatch_id") or "")
            candidates = [
                candidate
                for candidate in candidates
                if candidate[0] == story_id
                and candidate[1] == role
                and (not dispatch_id or candidate[3] == dispatch_id)
            ]
        if role is not None and not candidates:
            raise ReceiptRequired(
                "synaptory: governed agent finished without an active dispatch contract"
            )
        if role is None:
            candidates = _active_receipts(project)
            if not candidates:
                return {}
            completed = [candidate for candidate in candidates if candidate[2].is_file()]
            if len(completed) == 1:
                candidates = completed
            elif len(candidates) != 1:
                raise ReceiptRequired(
                    "synaptory: governed agent identity is ambiguous across active dispatches"
                )
        if len(candidates) != 1:
            raise ReceiptRequired(
                "synaptory: multiple active dispatches match the governed agent identity"
            )
        _story_id, _resolved_role, receipt, dispatch_id = candidates[0]
        if not receipt.is_file():
            raise ReceiptRequired(
                f"synaptory: governed agent finished without receipt {receipt.name}"
            )
        enriched = {**_transcript_runtime(payload), **payload}
        parsed = _patch_codex_receipt(receipt, enriched)
        if dispatch_id and parsed.get("dispatch_id") != dispatch_id:
            raise ReceiptRequired(
                "synaptory: receipt dispatch_id does not match the active dispatch"
            )
        from receipt_validator import validate_receipt  # type: ignore

        validation = validate_receipt(str(receipt), str(project))
        if not validation.valid:
            raise ReceiptRequired(
                "synaptory: receipt validation failed: " + "; ".join(validation.errors)
            )
        delivery = ship_validated_receipt(project, receipt)
        if delivery.get("handed_off") is not True:
            raise ReceiptRequired(
                "synaptory: validated receipt could not be handed to the control plane: "
                + str(delivery.get("error") or "durable handoff unavailable")
            )
    except Exception as exc:
        _write_otel_end(
            project,
            payload,
            marker,
            status="error",
            message=str(exc),
        )
        raise
    _write_otel_end(project, payload, marker, status="ok")
    return {}


def _session_end(project: Path, payload: dict[str, Any]) -> dict[str, Any]:
    orchestrator = project / ".synaptory" / ".orchestrator"
    if not orchestrator.is_dir():
        return {}
    marker = orchestrator / "codex-session.json"
    _atomic_json(
        marker,
        {
            "host": "codex",
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "session_id": str(payload.get("session_id") or ""),
            "schema_version": 1,
        },
    )
    # SessionEnd must remain synchronous and bounded. Queue the terminal event
    # locally and let the next async flush perform network I/O.
    _run_cli(project, ["telemetry", "session-end"], timeout=1.5, offline=True)
    return {}


def handle(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    project = _project(payload)
    normalized = event.strip().lower().replace("-", "").replace("_", "")
    if normalized == "sessionstart":
        return _session_start(project, payload)
    if normalized == "sessionend":
        return _session_end(project, payload)
    if normalized == "subagentstart":
        return _subagent_start(project, payload)
    if normalized == "subagentstop":
        return _subagent_stop(project, payload)
    if normalized == "telemetryflush":
        return _flush_telemetry(project)
    if normalized == "stop":
        return {}
    return {}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: codex_hook.py <event>", file=sys.stderr)
        return 1
    try:
        result = handle(argv[1], _read_input())
    except (AuthenticationRequired, ReceiptRequired) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    normalized = argv[1].strip().lower().replace("-", "").replace("_", "")
    if result or normalized in {"subagentstop", "stop"}:
        # Codex requires JSON on stdout for both stop events even when the
        # hook has no continuation decision. An empty stdout is invalid and
        # can prevent the host from treating the hook result as successful.
        print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
