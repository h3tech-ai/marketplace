#!/usr/bin/env python3
"""Bounded Codex hook adapter for the native Synaptory host package.

Only SessionStart and SessionEnd are registered. SessionEnd performs bounded,
local-only work so it remains safely below Codex's three-second ceiling.
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

from auth_gate import authentication_block_message, authentication_status  # noqa: E402

CONTEXT_CAP_BYTES = 4 * 1024


class AuthenticationRequired(RuntimeError):
    """Raised so the command adapter can block SessionStart with exit 2."""


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
    return {}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: codex_hook.py <event>", file=sys.stderr)
        return 1
    try:
        result = handle(argv[1], _read_input())
    except AuthenticationRequired as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if result:
        print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
