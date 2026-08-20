#!/usr/bin/env python3
"""stdio MCP server wrapping Synaptory state machines + receipts + tracker.

Tools: get_state, next_action, advance, validate_receipt, tracker_*.

`advance` validates the receipt first and refuses without a valid one —
that is the Cursor fail-closed gate (`subagentStop` cannot deny finished work).

If `healthcare.baa_enforced` is true, every tool refuses: Cursor's BAA does
not auto-cover plugin MCP and US residency excludes MCP.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(os.environ.get("PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
LIB = PLUGIN_ROOT / "hooks" / "lib"
SCRIPTS = PLUGIN_ROOT / "skills" / "_shared" / "scripts"
for p in (LIB, SCRIPTS, SCRIPTS / "tracker"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def _baa(project_dir: str) -> bool:
    try:
        from story_pipeline import healthcare_baa_enforced  # type: ignore
    except Exception:
        return False
    try:
        return bool(healthcare_baa_enforced(project_dir))
    except Exception:
        return False


def _require_not_baa(project_dir: str) -> dict[str, Any] | None:
    if _baa(project_dir):
        return {
            "error": (
                "PHI over MCP is refused when healthcare.baa_enforced is true. "
                "Use local Python: hooks/lib/state_machine.py and "
                "skills/_shared/scripts/tracker/tracker_cli.py."
            )
        }
    return None


def _project(args: dict[str, Any]) -> str:
    return str(
        args.get("project_dir")
        or os.environ.get("CURSOR_PROJECT_DIR")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or os.getcwd()
    )


def _build_mode(project_dir: str) -> str:
    try:
        from state_machine import read_state  # type: ignore

        state = read_state(project_dir) or {}
    except Exception:
        state = {}
    mode = state.get("build_mode") or "scrum"
    return str(mode)


def _redact_state(state: dict[str, Any]) -> dict[str, Any]:
    """Drop titles / AC / descriptions so we never accidentally leak PHI."""
    # Used only if we ever loosen the BAA refuse. Keep as a helper.
    out = dict(state)
    for key in ("current_stories", "stories", "backlog"):
        items = out.get(key)
        if not isinstance(items, list):
            continue
        slim = []
        for item in items:
            if not isinstance(item, dict):
                continue
            slim.append(
                {
                    k: item.get(k)
                    for k in ("id", "story_id", "state", "sub_state", "status")
                    if k in item
                }
            )
        out[key] = slim
    return out


def tool_get_state(args: dict[str, Any]) -> dict[str, Any]:
    project = _project(args)
    blocked = _require_not_baa(project)
    if blocked:
        return blocked
    from state_machine import read_state  # type: ignore

    return {"state": read_state(project)}


def tool_next_action(args: dict[str, Any]) -> dict[str, Any]:
    project = _project(args)
    blocked = _require_not_baa(project)
    if blocked:
        return blocked
    mode = _build_mode(project)
    if mode == "kanban":
        from kanban_state_machine import next_action  # type: ignore
    elif mode == "spq":
        from spq_state_machine import next_action  # type: ignore
    else:
        from scrum_state_machine import next_action  # type: ignore
    return {"next_action": next_action(project), "build_mode": mode}


def tool_validate_receipt(args: dict[str, Any]) -> dict[str, Any]:
    project = _project(args)
    path = args.get("receipt_path")
    if not path:
        return {"error": "receipt_path is required", "valid": False}
    from receipt_validator import validate_receipt  # type: ignore

    result = validate_receipt(str(path), project)
    payload = result.to_dict()
    return payload


def _parse_ts(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# Role that must have just finished for this from→to MCP advance.
_TRANSITION_RECEIPT: dict[tuple[str, str], tuple[str, str]] = {
    ("queued", "in_progress"): ("software-engineer", "se"),
    ("in_progress", "testing"): ("software-engineer", "se"),
    ("testing", "reviewing"): ("quality-engineer", "qe"),
    ("reviewing", "done"): ("code-reviewer", "cr"),
    ("reviewing", "awaiting_acceptance"): ("code-reviewer", "cr"),
    ("awaiting_acceptance", "done"): ("project-owner", "po"),
}

_BLOCKED_FROM_ROLE: dict[str, tuple[str, str]] = {
    "in_progress": ("software-engineer", "se"),
    "testing": ("quality-engineer", "qe"),
    "reviewing": ("code-reviewer", "cr"),
    "awaiting_acceptance": ("project-owner", "po"),
}


def _expected_receipt_role(from_state: str, to_state: str) -> tuple[str, str] | None:
    if to_state == "blocked":
        return _BLOCKED_FROM_ROLE.get(from_state)
    return _TRANSITION_RECEIPT.get((from_state, to_state))


def _canonical_receipt_path(project: str, story_id: str, abbrev: str) -> Path:
    from story_pipeline import get_story_receipt_path  # type: ignore

    receipts = Path(project) / ".synaptory" / ".orchestrator" / "receipts"
    return receipts / get_story_receipt_path(story_id, abbrev)


def tool_advance(args: dict[str, Any]) -> dict[str, Any]:
    project = _project(args)
    blocked = _require_not_baa(project)
    if blocked:
        return blocked
    story_id = str(args.get("story_id") or "")
    to_state = str(args.get("to_state") or "")
    if not story_id or not to_state:
        return {"error": "story_id and to_state are required"}
    from story_pipeline import (  # type: ignore
        _read_state,
        _write_state,
        evaluate_story_dod,
        get_story,
        resolve_dod_tier,
        ship_evaluated_dod,
        transition_story,
    )

    state = _read_state(project)
    story = get_story(state, story_id)
    if not story:
        return {
            "error": f"advance refused: story not found: {story_id}",
            "advanced": False,
        }
    from_state = str(story.get("state") or "")
    expected = _expected_receipt_role(from_state, to_state)
    if expected is None:
        return {
            "error": (
                f"advance refused: no bound receipt role for {from_state} → {to_state}"
            ),
            "advanced": False,
        }
    role, abbrev = expected
    canonical = _canonical_receipt_path(project, story_id, abbrev)
    given = args.get("receipt_path")
    if given:
        try:
            if Path(str(given)).resolve() != canonical.resolve():
                return {
                    "error": (
                        "advance refused: receipt_path must be the canonical "
                        f"{canonical.name} for this story and stage"
                    ),
                    "advanced": False,
                }
        except OSError:
            return {
                "error": "advance refused: receipt_path is not readable",
                "advanced": False,
            }
    if not canonical.is_file():
        return {
            "error": (
                f"advance refused: no receipt on disk for {story_id} "
                f"(expected {canonical.name}). "
                "subagentStop cannot block; rewrite the receipt before advancing."
            ),
            "advanced": False,
        }
    path = str(canonical)
    from receipt_validator import validate_receipt  # type: ignore

    result = validate_receipt(path, project)
    if not result.valid:
        return {
            "error": "advance refused: receipt invalid",
            "receipt_path": path,
            "errors": result.errors,
            "warnings": result.warnings,
            "advanced": False,
        }
    try:
        payload = json.loads(canonical.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "error": f"advance refused: cannot read receipt: {exc}",
            "advanced": False,
        }
    if str(payload.get("story_id") or "") != story_id:
        return {
            "error": (
                f"advance refused: receipt story_id {payload.get('story_id')!r} "
                f"does not match {story_id}"
            ),
            "advanced": False,
        }
    if str(payload.get("role") or "") != role:
        return {
            "error": (
                f"advance refused: receipt role {payload.get('role')!r} "
                f"does not match required {role} for {from_state} → {to_state}"
            ),
            "advanced": False,
        }
    entered = None
    log = story.get("pipeline_log") or []
    if log and isinstance(log[-1], dict):
        entered = _parse_ts(str(log[-1].get("entered_at") or ""))
    completed = _parse_ts(str(payload.get("completed_at") or ""))
    if entered is not None and (completed is None or completed <= entered):
        return {
            "error": (
                "advance refused: receipt is stale (completed_at must be after "
                "the current stage entered_at)"
            ),
            "advanced": False,
        }
    digest = hashlib.sha256(canonical.read_bytes()).hexdigest()
    consumed = story.setdefault("mcp_consumed_receipts", [])
    if not isinstance(consumed, list):
        consumed = []
        story["mcp_consumed_receipts"] = consumed
    if digest in consumed:
        return {
            "error": "advance refused: receipt already consumed (replay)",
            "advanced": False,
        }
    if to_state == "done":
        intensity = str(resolve_dod_tier(project, state, False).get("tier") or "early")
        dod = evaluate_story_dod(project, story_id, intensity)
        if not dod.get("passed"):
            return {
                "error": "advance refused: Definition of Done did not pass",
                "dod": dod,
                "advanced": False,
            }
        pending_dod = dod
    else:
        pending_dod = None

    reason = args.get("reason")
    try:
        state = transition_story(state, story_id, to_state, reason, project)
    except ValueError as exc:
        return {"error": f"advance refused: {exc}", "advanced": False}
    story = get_story(state, story_id) or {}
    used = list(story.get("mcp_consumed_receipts") or [])
    used.append(digest)
    story["mcp_consumed_receipts"] = used
    if pending_dod is not None:
        story["dod"] = pending_dod
        ship_evaluated_dod(story_id, pending_dod)
    _write_state(project, state)
    return {"advanced": True, "story": story, "receipt_path": path}


def _tracker(project: str, argv: list[str]) -> dict[str, Any]:
    blocked = _require_not_baa(project)
    if blocked:
        return blocked
    import subprocess

    cli = SCRIPTS / "tracker" / "tracker_cli.py"
    proc = subprocess.run(
        [sys.executable, str(cli), "--project-dir", project, *argv],
        capture_output=True,
        text=True,
        check=False,
    )
    out: dict[str, Any] = {"returncode": proc.returncode}
    if proc.stdout.strip():
        try:
            out["result"] = json.loads(proc.stdout)
        except json.JSONDecodeError:
            out["stdout"] = proc.stdout
    if proc.stderr.strip():
        out["stderr"] = proc.stderr
    return out


def tool_tracker_update_status(args: dict[str, Any]) -> dict[str, Any]:
    argv = ["update-status", str(args["story_id"]), str(args["status"])]
    if args.get("allow_skip"):
        argv.append("--allow-skip")
    return _tracker(_project(args), argv)


def tool_tracker_get_backlog(args: dict[str, Any]) -> dict[str, Any]:
    return _tracker(_project(args), ["get-backlog"])


def tool_tracker_health_check(args: dict[str, Any]) -> dict[str, Any]:
    return _tracker(_project(args), ["health-check"])


TOOLS: dict[str, Any] = {
    "get_state": {
        "fn": tool_get_state,
        "description": "Read pipeline-state.json for the current project.",
        "schema": {
            "type": "object",
            "properties": {"project_dir": {"type": "string"}},
        },
    },
    "next_action": {
        "fn": tool_next_action,
        "description": "Deterministic next dispatch from the active lifecycle state machine.",
        "schema": {
            "type": "object",
            "properties": {"project_dir": {"type": "string"}},
        },
    },
    "validate_receipt": {
        "fn": tool_validate_receipt,
        "description": "Validate a story receipt JSON file. Does not advance state.",
        "schema": {
            "type": "object",
            "properties": {
                "receipt_path": {"type": "string"},
                "project_dir": {"type": "string"},
            },
            "required": ["receipt_path"],
        },
    },
    "advance": {
        "fn": tool_advance,
        "description": (
            "Transition a story after validating its receipt. Refuses if the "
            "receipt is missing or invalid (Cursor fail-closed gate)."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string"},
                "story_id": {"type": "string"},
                "to_state": {"type": "string"},
                "receipt_path": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["story_id", "to_state"],
        },
    },
    "tracker_update_status": {
        "fn": tool_tracker_update_status,
        "description": "Update a tracker ticket status via tracker_cli.py.",
        "schema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string"},
                "story_id": {"type": "string"},
                "status": {"type": "string"},
                "allow_skip": {"type": "boolean"},
            },
            "required": ["story_id", "status"],
        },
    },
    "tracker_get_backlog": {
        "fn": tool_tracker_get_backlog,
        "description": "Get the backlog via tracker_cli.py.",
        "schema": {"type": "object", "properties": {"project_dir": {"type": "string"}}},
    },
    "tracker_health_check": {
        "fn": tool_tracker_health_check,
        "description": "Tracker adapter health-check.",
        "schema": {"type": "object", "properties": {"project_dir": {"type": "string"}}},
    },
}


def handle_tool(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    spec = TOOLS.get(name)
    if spec is None:
        return {"error": f"unknown tool: {name}"}
    try:
        return spec["fn"](arguments or {})
    except Exception as exc:  # never crash the MCP session
        return {"error": f"{type(exc).__name__}: {exc}"}


def _tools_list() -> dict[str, Any]:
    return {
        "tools": [
            {
                "name": name,
                "description": spec["description"],
                "inputSchema": spec["schema"],
            }
            for name, spec in TOOLS.items()
        ]
    }


def _dispatch(req: dict[str, Any]) -> dict[str, Any] | None:
    method = req.get("method")
    req_id = req.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "synaptory", "version": "1.0.0"},
            },
        }
    if method == "notifications/initialized" or method == "initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": _tools_list()}
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        result = handle_tool(name, arguments)
        is_error = bool(isinstance(result, dict) and result.get("error"))
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(result, default=str)}],
                "isError": is_error,
            },
        }
    if req_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _read_stdio_message() -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        decoded = line.decode("utf-8", "replace")
        if ":" not in decoded:
            continue
        key, value = decoded.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length") or 0)
    if length <= 0:
        return None
    body = sys.stdin.buffer.read(length)
    return json.loads(body.decode("utf-8"))


def _write_stdio_message(obj: dict[str, Any]) -> None:
    body = json.dumps(obj).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    sys.stdout.buffer.flush()


def main() -> int:
    # NDJSON test mode: SYNAPTORY_MCP_NDJSON=1 (pytest). Production is
    # Content-Length framed MCP stdio.
    ndjson = os.environ.get("SYNAPTORY_MCP_NDJSON") == "1"
    if ndjson:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            resp = _dispatch(json.loads(line))
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
        return 0
    while True:
        try:
            req = _read_stdio_message()
        except Exception:
            return 0
        if req is None:
            return 0
        resp = _dispatch(req)
        if resp is not None:
            _write_stdio_message(resp)


if __name__ == "__main__":
    raise SystemExit(main())
