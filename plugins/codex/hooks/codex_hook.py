#!/usr/bin/env python3
"""Bounded Codex hook adapter for the native Synaptory host package.

SessionStart supplies a compact governed-execution reminder. SubagentStop
validates the exact receipt bound to a Synaptory custom-agent dispatch, while
Stop/SessionEnd perform bounded local bookkeeping.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
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

from auth_gate import authentication_block_message, authentication_status  # noqa: E402
from codex_attribution import (  # noqa: E402
    patch_codex_receipt as _shared_patch_codex_receipt,
    transcript_runtime as _shared_transcript_runtime,
)
from receipt_delivery import ship_validated_receipt  # noqa: E402

CONTEXT_CAP_BYTES = 4 * 1024


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


def _session_start(project: Path) -> dict[str, Any]:
    state = _read_state(project)
    if not state:
        return {}
    authentication = authentication_status(project)
    if not authentication["ready"]:
        raise AuthenticationRequired(authentication_block_message(authentication))
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
    role_map = {
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
    identity = " ".join(
        str(payload.get(key) or "")
        for key in (
            "agent_type", "agent_role", "agent_name", "agent_path", "name", "description"
        )
    ).lower()
    for full_role, abbreviation in role_map.items():
        if f"synaptory-{full_role}" in identity:
            return abbreviation
    return None


def _active_receipts(
    project: Path, role: str | None = None
) -> list[tuple[str, Path, str]]:
    full = _read_state(project)
    selected, active_spec = _select_state(full)
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
    root = project / ".synaptory" / ".orchestrator"
    receipts = root / "specs" / active_spec / "receipts" if active_spec else root / "receipts"
    return [
        (
            abbreviation,
            receipts / f"{story_id}-{abbreviation}.json",
            str(dispatch.get("dispatch_id") or ""),
        )
        for story_id, abbreviation, dispatch in candidates
    ]


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
    role = _agent_role(payload)
    candidates = _active_receipts(project, role)
    if role is not None and not candidates:
        raise ReceiptRequired(
            "synaptory: governed agent finished without an active dispatch contract"
        )
    if role is None:
        candidates = _active_receipts(project)
        if not candidates:
            return {}
        completed = [candidate for candidate in candidates if candidate[1].is_file()]
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
    _resolved_role, receipt, dispatch_id = candidates[0]
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
    return {}


def handle(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    project = _project(payload)
    normalized = event.strip().lower().replace("-", "").replace("_", "")
    if normalized == "sessionstart":
        return _session_start(project)
    if normalized == "sessionend":
        return _session_end(project, payload)
    if normalized == "subagentstop":
        return _subagent_stop(project, payload)
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
