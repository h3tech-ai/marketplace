#!/usr/bin/env python3
"""stdio MCP server wrapping Synaptory state machines + receipts + tracker.

Tools: doctor, get_status, get_state, next_action, advance, validate_receipt,
begin_dispatch, begin_lifecycle_dispatch, initialize, transition,
request_acceptance, accept_story, tracker_*, and the SPQ Cycle verbs.

`advance` validates the receipt first and refuses without a valid one —
that is the Cursor fail-closed gate (`subagentStop` cannot deny finished work).
Mutating tools also require doctor readiness and durable analytics delivery.

If `healthcare.baa_enforced` is true, every tool refuses: Cursor's BAA does
not auto-cover plugin MCP and US residency excludes MCP.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MCP_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = Path(os.environ.get("PLUGIN_ROOT") or MCP_DIR.parent)
os.environ.setdefault("SYNAPTORY_PLUGIN_ROOT", str(PLUGIN_ROOT))
os.environ.setdefault("SYNAPTORY_HOST", "cursor")
os.environ.setdefault("SYNAPTORY_IDE", "cursor")
LIB = PLUGIN_ROOT / "hooks" / "lib"
SCRIPTS = PLUGIN_ROOT / "skills" / "_shared" / "scripts"
# MCP_DIR first: importlib loaders (conformance) do not put the script
# directory on sys.path the way `python mcp/server.py` does in production.
for p in (MCP_DIR, LIB, SCRIPTS, SCRIPTS / "tracker"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# The shared gate. Every advance/dispatch check lives here so all three hosts
# enforce one contract; this server only supplies Cursor-specific policy.
import advance_kernel  # noqa: E402
import mcp_transport  # noqa: E402
import spq_next  # noqa: E402
from auth_status import authentication_status  # noqa: E402
from cursor_attribution import attribute_receipt_from_dispatch  # noqa: E402
from receipt_delivery import delivery_status, ship_validated_receipt  # noqa: E402

_BAA_REFUSAL = (
    "PHI over MCP is refused when healthcare.baa_enforced is true. "
    "Use local Python: hooks/lib/advance_kernel.py and "
    "skills/_shared/scripts/tracker/tracker_cli.py."
)

# Fails CLOSED: an unreadable healthcare config is treated as regulated. This
# module previously returned False on parser failure, so a broken config
# silently PERMITTED PHI work over MCP -- the mirror image of the Codex guard,
# which had always failed closed on the identical condition.
_require_not_baa = advance_kernel.baa_refusal_gate(_BAA_REFUSAL)


def _state_path(project: str | Path) -> Path:
    return Path(project) / ".synaptory" / ".orchestrator" / "pipeline-state.json"


def _shared_runtime_status() -> dict[str, Any]:
    kernel = LIB / "advance_kernel.py"
    validator = LIB / "receipt_validator.py"
    available = kernel.is_file() and validator.is_file()
    return {
        "available": available,
        "advance_kernel": str(kernel) if kernel.is_file() else None,
    }


def _execution_readiness(project: Path) -> dict[str, Any]:
    """Single readiness decision used by doctor and mutating tools."""
    state_file = _state_path(project)
    state_readable = False
    state_error: str | None = None
    if state_file.is_file():
        try:
            from state_machine import read_state  # type: ignore

            read_state(str(project))
            state_readable = True
        except Exception as exc:  # noqa: BLE001
            state_error = str(exc)
    authentication = authentication_status(project)
    receipt_delivery = delivery_status(project, authentication=authentication)
    regulated = _require_not_baa(str(project)) is not None
    shared = _shared_runtime_status()
    try:
        from state_drift import check_drift  # type: ignore

        configuration = check_drift(str(project))
    except Exception as exc:  # noqa: BLE001
        configuration = {
            "ok": False,
            "checked": False,
            "problems": ["configuration validation unavailable: %s" % exc],
        }
    overlay = (PLUGIN_ROOT / "hooks" / "lib" / "cursor_hook.py").is_file()
    ready = bool(
        authentication.get("ready")
        and receipt_delivery.get("ready")
        and state_readable
        and shared["available"]
        and overlay
        and configuration.get("ok")
        and not regulated
    )
    missing = [
        "authenticated Synaptory control-plane session"
        if not authentication.get("ready")
        else None,
        "durable Cursor receipt analytics delivery"
        if not receipt_delivery.get("ready")
        else None,
        "readable Synaptory pipeline state" if not state_readable else None,
        "composed Synaptory lifecycle and receipt runtime"
        if not shared["available"]
        else None,
        "Cursor host overlay (cursor_hook.py)" if not overlay else None,
        "PHI over MCP refused for baa_enforced projects" if regulated else None,
        "valid Synaptory state and configuration"
        if not configuration.get("ok")
        else None,
    ]
    detected_mode = None
    if state_readable:
        detected_mode = _build_mode(str(project))
    return {
        "host": "cursor",
        "runtime_generation": 2,
        "execution_scope": (
            "standard-full-spq" if detected_mode == "spq" else "uncertified-lifecycle"
        ),
        "project": {
            "detected": state_file.is_file(),
            "path": str(project),
            "regulated": regulated,
            "state_error": state_error,
        },
        "checks": {
            "authentication": authentication,
            "control_plane_receipt_delivery": receipt_delivery,
            "pipeline_state_readable": state_readable,
            "regulated_execution_allowed": not regulated,
            "shared_runtime": shared,
            "host_overlay": overlay,
            "configuration": configuration,
            "receipt_gate_enabled": shared["available"],
        },
        "status_ready": state_readable and not regulated,
        "structured_execution_ready": ready,
        "missing_requirements": [item for item in missing if item],
    }


def _require_execution_ready(project: Path) -> dict[str, Any] | None:
    readiness = _execution_readiness(project)
    if readiness["structured_execution_ready"]:
        return None
    return {
        "error": (
            "execution refused: Synaptory doctor is not ready; resolve every "
            "missing requirement before dispatch or mutation"
        ),
        "structured_execution_ready": False,
        "missing_requirements": readiness["missing_requirements"],
        "checks": readiness["checks"],
    }


def _require_authenticated(project: Path) -> dict[str, Any] | None:
    status = authentication_status(project)
    if status.get("ready"):
        return None
    return {
        "error": "mutation refused: authenticated Synaptory session is required",
        "authentication": status,
    }


def _require_spq(project: str | Path) -> dict[str, Any] | None:
    if _build_mode(str(project)) == "spq":
        return None
    return {"error": "SPQ lifecycle tools require build_mode: spq"}


def _delivery_gate(project_dir: str, receipt_path: str) -> dict[str, Any]:
    return ship_validated_receipt(Path(project_dir), Path(receipt_path))


def _kernel_policy() -> Any:
    """Cursor host policy for the shared kernel."""
    return advance_kernel.policy_for_cursor(
        baa_message=_BAA_REFUSAL,
        readiness_gate=lambda project_dir: _require_execution_ready(Path(project_dir)),
        receipt_delivery_gate=_delivery_gate,
    )


def _project(args: dict[str, Any]) -> str:
    return str(
        args.get("project_dir")
        or os.environ.get("SYNAPTORY_PROJECT_DIR")
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
    mode = str(state.get("build_mode") or "").strip()
    if mode:
        return mode
    path = Path(project_dir) / ".synaptory.yaml"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.split("#", 1)[0].strip()
            if stripped.startswith("build_mode:"):
                return stripped.split(":", 1)[1].strip().strip("\"'") or "scrum"
    except OSError:
        pass
    return "scrum"


def _lifecycle(project: str):
    mode = _build_mode(project)
    if mode == "kanban":
        import kanban_state_machine as sm  # type: ignore
    elif mode == "spq":
        import spq_state_machine as sm  # type: ignore
    else:
        import scrum_state_machine as sm  # type: ignore
    return sm


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


def tool_doctor(args: dict[str, Any]) -> dict[str, Any]:
    return _execution_readiness(Path(_project(args)))


def tool_get_status(args: dict[str, Any]) -> dict[str, Any]:
    project = _project(args)
    blocked = _require_not_baa(project)
    if blocked:
        return blocked
    from state_machine import read_state  # type: ignore

    state = read_state(project) or {}
    if not state:
        return {"detected": False, "project_dir": project}
    stories = state.get("current_stories") if isinstance(state.get("current_stories"), list) else []
    counts: dict[str, int] = {}
    for story in stories:
        if not isinstance(story, dict):
            continue
        status = str(story.get("state") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {
        "detected": True,
        "host": "cursor",
        "schema_version": state.get("version"),
        "build_mode": state.get("build_mode") or _build_mode(project),
        "lifecycle_state": state.get("lifecycle_state"),
        "current_cycle": state.get("current_cycle"),
        "current_sprint": state.get("current_sprint"),
        "stories": {"total": sum(counts.values()), "by_status": dict(sorted(counts.items()))},
        "structured_execution_ready": tool_doctor(args)["structured_execution_ready"],
    }


def tool_get_state(args: dict[str, Any]) -> dict[str, Any]:
    project = _project(args)
    blocked = _require_not_baa(project)
    if blocked:
        return blocked
    from state_machine import read_state  # type: ignore

    return {
        "host": "cursor",
        "build_mode": _build_mode(project),
        "state": read_state(project),
    }


def tool_next_action(args: dict[str, Any]) -> dict[str, Any]:
    project = _project(args)
    blocked = _require_not_baa(project)
    if blocked:
        return blocked
    mode = _build_mode(project)
    sm = _lifecycle(project)
    selected = sm.next_action(project)
    if mode == "spq":
        if selected.get("action") == "not_in_execution":
            selected = spq_next.lifecycle_next_action(Path(project))
        elif selected.get("action") == "await_sync":
            from state_machine import read_state  # type: ignore

            selected = spq_next.sync_boundary_next_action(
                Path(project), selected, read_state(project) or {}
            )
    return {"next_action": selected, "build_mode": mode}


def tool_validate_receipt(args: dict[str, Any]) -> dict[str, Any]:
    project = _project(args)
    blocked = _require_not_baa(project)
    if blocked:
        return {**blocked, "valid": False}
    path = args.get("receipt_path")
    if not path:
        return {"error": "receipt_path is required", "valid": False}
    receipt_path = Path(str(path))
    attribution = None
    try:
        attribution = attribute_receipt_from_dispatch(Path(project), receipt_path)
    except ValueError:
        attribution = None
    from receipt_validator import validate_receipt  # type: ignore

    result = validate_receipt(str(receipt_path), project)
    payload = result.to_dict()
    if attribution is not None:
        payload["cursor_attribution"] = attribution
    if result.valid:
        delivery = ship_validated_receipt(Path(project), receipt_path)
        payload["control_plane_delivery"] = delivery
        if delivery.get("handed_off") is not True:
            payload["locally_valid"] = True
            payload["valid"] = False
            errors = list(payload.get("errors") or [])
            errors.append(
                "control-plane receipt delivery failed: "
                + str(delivery.get("error") or "durable handoff unavailable")
            )
            payload["errors"] = errors
    return payload


def tool_advance(args: dict[str, Any]) -> dict[str, Any]:
    """Validate evidence and apply a transition through the shared kernel.

    Thin adapter. Every check now lives in core/lib/advance_kernel.py so Cursor,
    Codex and Claude enforce one contract. Cursor gains, by adoption rather than
    by patch: next_action binding, state-matched entered_at (this module read
    pipeline_log[-1] and skipped the check when absent), symlink and path-escape
    refusal, spec-aware receipt resolution, refusal on a corrupt consumed-receipt
    ledger (it used to be silently reset, discarding the replay guard), and a
    fail-closed BAA parse.
    """
    project = _project(args)
    story_id = str(args.get("story_id") or "")
    to_state = str(args.get("to_state") or "")
    given = args.get("receipt_path")

    decision = advance_kernel.execute_advance(
        project,
        story_id,
        to_state,
        receipt_path=str(given) if given else None,
        reason=args.get("reason"),
        policy=_kernel_policy(),
    )
    return _advance_response(decision)


def _advance_response(decision: Any) -> dict[str, Any]:
    """Map a kernel Decision onto this server's wire shape.

    Structure is shared (mcp_transport) so a refusal cannot carry a `code` on
    one host and omit it on the other; only the prose is host-specific.
    """
    if not decision.allowed and decision.code == advance_kernel.POLICY_REFUSED:
        # Keep the bare shape callers already handle, plus the machine-readable code.
        return {"error": decision.reason, "code": decision.code}
    return mcp_transport.advance_response(decision, _advance_error_text)


def _advance_error_text(decision: Any) -> str:
    """Preserve the legacy `advance refused: ...` prose."""
    if decision.code == advance_kernel.DOD_FAILED:
        return "advance refused: Definition of Done did not pass"
    return "advance refused: %s" % decision.reason


def tool_begin_dispatch(args: dict[str, Any]) -> dict[str, Any]:
    """Authorize a dispatch the deterministic next_action actually selected.

    New on Cursor: without it, dispatch role binding was unenforced and the
    model could start any agent it liked on any story.
    """
    project = _project(args)
    story_id = str(args.get("story_id") or "")
    role = args.get("role")

    decision = advance_kernel.execute_dispatch(
        project,
        story_id,
        role=str(role) if role else None,
        policy=_kernel_policy(),
    )
    if not decision.allowed:
        if decision.code == advance_kernel.POLICY_REFUSED:
            return {"error": decision.reason}
        payload: dict[str, Any] = {
            "authorized": False,
            "error": "dispatch refused: %s" % decision.reason,
            "code": decision.code,
        }
        if decision.next_action is not None:
            payload["next_action"] = decision.next_action
        return payload

    selected = decision.extra.get("dispatch_action") or decision.next_action or {}
    contract = decision.extra.get("receipt_contract", {})
    return {
        "authorized": True,
        "host": "cursor",
        "action": str(selected.get("action") or ""),
        "role": decision.role,
        "story": decision.story,
        "receipt_path": contract.get("receipt_path"),
        "receipt_contract": {
            "backend": "cursor",
            "dispatch_id": contract.get("dispatch_id"),
            "required_fields": [
                "story_id",
                "role",
                "backend",
                "model",
                "dispatch_id",
                "artifacts",
                "verification_commands",
                "metrics",
                "completed_at",
            ],
            "verification_command_shape": {
                "command": "string",
                "exit_code": "integer",
                "summary": "string",
            },
            "story_dod_shape": (
                {
                    "tests_pass": "boolean",
                    "build_succeeds": "boolean",
                    "no_critical_findings": "boolean",
                    "code_reviewed": "boolean",
                    "coverage_no_decrease": "boolean",
                }
                if decision.role in {"software-engineer", "quality-engineer"}
                else None
            ),
            "token_usage_source": (
                "Use the model id from hook stdin (model / model_id). "
                "Never invent backend: claude. Omit tokens rather than guess."
            ),
        },
        "next_action": selected,
        "archived_receipt": decision.extra.get("archived_receipt"),
        "retry_count": decision.extra.get("retry_count"),
    }


def tool_begin_lifecycle_dispatch(args: dict[str, Any]) -> dict[str, Any]:
    """Authorize a Checkpoint/Acceptance agent selected by lifecycle next_action."""
    project = Path(_project(args))
    blocked = _require_execution_ready(project) or _require_spq(project)
    if blocked:
        return {**blocked, "authorized": False}
    requested_role = advance_kernel.role_abbrev(str(args.get("role") or ""))
    requested_story = str(args.get("story_id") or "")
    from spec_state import state_transaction  # type: ignore

    with state_transaction(str(project)):
        selected = spq_next.lifecycle_next_action(project)
        role = str(selected.get("role") or "")
        if (
            not str(selected.get("action") or "").startswith("dispatch_")
            or role != requested_role
            or selected.get("story_id") != requested_story
        ):
            return {
                "authorized": False,
                "error": "lifecycle dispatch does not match deterministic next_action",
                "next_action": selected,
            }
        import spq_state_machine as module  # type: ignore

        state = module.read_state(str(project))
        path = Path(str(selected["receipt_path"]))
        receipts_root = Path(module._resolve_receipts_dir(str(project))).resolve()
        if path.is_symlink() or receipts_root.is_symlink():
            return {"authorized": False, "error": "lifecycle receipt path may not be a symlink"}
        path = path.resolve()
        try:
            path.relative_to(receipts_root)
        except ValueError:
            return {"authorized": False, "error": "lifecycle receipt path escapes receipts directory"}
        archived: str | None = None
        if path.is_file():
            archive_dir = path.parent / "archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
            archive = archive_dir / ("%s.failed-%s.json" % (path.stem, digest))
            suffix = 1
            while archive.exists():
                archive = archive_dir / ("%s.failed-%s-%s.json" % (path.stem, digest, suffix))
                suffix += 1
            shutil.move(str(path), str(archive))
            archived = str(archive)
        dispatch_id = secrets.token_hex(16)
        key = "%s:%s" % (requested_story, role)
        state.setdefault("lifecycle_dispatches", {})[key] = {
            "dispatch_id": dispatch_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "host": "cursor",
        }
        module._write_state(str(project), state)
    receipt_contract = {
        "backend": "cursor",
        "dispatch_id": dispatch_id,
        "required_fields": [
            "story_id", "role", "backend", "model", "dispatch_id",
            "artifacts", "verification_commands", "metrics", "completed_at",
        ],
        "token_usage_source": (
            "Use the model id from hook stdin. Never invent backend: claude."
        ),
    }
    return {
        "authorized": True,
        "host": "cursor",
        "action": selected["action"],
        "role": advance_kernel.ROLE_NAMES.get(role, role),
        "story_id": requested_story,
        "receipt_path": str(path),
        "receipt_contract": receipt_contract,
        "archived_receipt": archived,
        "next_action": selected,
    }


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
    project = _project(args)
    blocked = _require_execution_ready(Path(project))
    if blocked:
        return blocked
    argv = ["update-status", str(args["story_id"]), str(args["status"])]
    if args.get("allow_skip"):
        argv.append("--allow-skip")
    return _tracker(project, argv)


def tool_tracker_get_backlog(args: dict[str, Any]) -> dict[str, Any]:
    return _tracker(_project(args), ["get-backlog"])


def tool_tracker_health_check(args: dict[str, Any]) -> dict[str, Any]:
    return _tracker(_project(args), ["health-check"])


def _json_list(value: Any) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if not isinstance(parsed, list):
            raise ValueError("work_units must be a JSON array")
        return parsed
    raise ValueError("work_units must be a JSON array")


def _spq_call(
    project: str, name: str, *a: Any, skip_readiness: bool = False, **kw: Any
) -> dict[str, Any]:
    blocked = _require_not_baa(project)
    if blocked:
        return blocked
    if not skip_readiness:
        blocked = _require_execution_ready(Path(project))
        if blocked:
            return blocked
    import spq_state_machine as spq  # type: ignore
    from sync_barrier import BarrierError  # type: ignore

    try:
        return getattr(spq, name)(project, *a, **kw)
    except (ValueError, TypeError, json.JSONDecodeError, BarrierError) as exc:
        return {"error": str(exc)}


def tool_accept_story(args: dict[str, Any]) -> dict[str, Any]:
    project = _project(args)
    blocked = _require_not_baa(project) or _require_execution_ready(Path(project))
    if blocked:
        return blocked
    story_id = str(args.get("story_id") or "")
    accepted_by = str(args.get("accepted_by") or "")
    mode = _build_mode(project)
    try:
        if mode == "spq":
            from spq_state_machine import accept_story  # type: ignore
        else:
            from scrum_state_machine import accept_story  # type: ignore
        return accept_story(project, story_id, accepted_by)
    except (ValueError, TypeError) as exc:
        return {"error": str(exc)}


def tool_request_acceptance(args: dict[str, Any]) -> dict[str, Any]:
    project = _project(args)
    blocked = _require_not_baa(project) or _require_execution_ready(Path(project))
    if blocked:
        return blocked
    story_id = str(args.get("story_id") or "")
    mode = _build_mode(project)
    try:
        if mode == "spq":
            from spq_state_machine import request_acceptance  # type: ignore
        else:
            from scrum_state_machine import request_acceptance  # type: ignore
        return request_acceptance(project, story_id)
    except (ValueError, TypeError) as exc:
        return {"error": str(exc)}


def tool_initialize(args: dict[str, Any]) -> dict[str, Any]:
    project = _project(args)
    blocked = _require_not_baa(project) or _require_authenticated(Path(project))
    if blocked:
        return blocked
    if _state_path(project).exists():
        return {"error": "initialize refused: pipeline state already exists"}
    ws = str(args.get("workstream_id") or "").strip() or None
    try:
        sm = _lifecycle(project)
        if _build_mode(project) == "spq" or ws:
            import spq_state_machine as spq  # type: ignore

            return spq.initialize(project, workstream_id=ws)
        return sm.initialize(project)
    except (ValueError, TypeError) as exc:
        return {"error": str(exc)}


def tool_transition(args: dict[str, Any]) -> dict[str, Any]:
    project = _project(args)
    blocked = _require_not_baa(project) or _require_execution_ready(Path(project))
    if blocked:
        return blocked
    to_state = str(args.get("to_state") or args.get("lifecycle") or "")
    force = args.get("force")
    if isinstance(force, str):
        force = force.strip().lower() in ("1", "true", "yes")
    try:
        return _lifecycle(project).transition(project, to_state, force=bool(force))
    except (ValueError, TypeError) as exc:
        return {"error": str(exc)}


def tool_approve_baseline(args: dict[str, Any]) -> dict[str, Any]:
    return _spq_call(
        _project(args), "approve_baseline",
        approved_by=args.get("approved_by"),
    )


def tool_open_cycle(args: dict[str, Any]) -> dict[str, Any]:
    project = _project(args)
    try:
        units = _json_list(args.get("work_units"))
        cycle_n = args.get("cycle_number")
        tracker = args.get("tracker_cycle")
        return _spq_call(
            project,
            "open_cycle",
            int(cycle_n) if cycle_n not in (None, "") else None,
            str(args.get("goal") or ""),
            units,
            tracker_cycle=int(tracker) if tracker not in (None, "") else None,
        )
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return {"error": str(exc)}


def tool_hydrate_cycle(args: dict[str, Any]) -> dict[str, Any]:
    project = _project(args)
    try:
        units = _json_list(args.get("work_units"))
        tracker = args.get("tracker_cycle")
        ws = str(args.get("workstream_id") or "").strip() or None
        cycle_n = args.get("cycle_number")
        if cycle_n not in (None, "") and (
            not isinstance(cycle_n, int) or isinstance(cycle_n, bool)
        ):
            try:
                cycle_n = int(cycle_n)
            except (TypeError, ValueError):
                return {"error": "cycle_number is required for hydrate_cycle and must be an integer"}
        if not _state_path(project).is_file():
            blocked = _require_not_baa(project) or _require_authenticated(Path(project))
            if blocked:
                return blocked
            if spq_next.configured_build_mode(Path(project)) != "spq":
                return {"error": "SPQ lifecycle tools require build_mode: spq"}
            if not isinstance(units, list) or not units:
                return {"error": "work_units must be a non-empty array"}
            if cycle_n in (None, ""):
                return {"error": "cycle_number is required for hydrate_cycle and must be an integer"}
            import spq_state_machine as spq  # type: ignore

            spq.initialize(project, workstream_id=ws)
            return spq.hydrate_cycle(
                project,
                int(cycle_n),
                units,
                goal=str(args.get("goal") or ""),
                tracker_cycle=int(tracker) if tracker not in (None, "") else None,
                workstream_id=ws,
            )
        return _spq_call(
            project,
            "hydrate_cycle",
            int(cycle_n) if cycle_n not in (None, "") else None,
            units,
            goal=str(args.get("goal") or ""),
            tracker_cycle=int(tracker) if tracker not in (None, "") else None,
            workstream_id=ws,
        )
    except (ValueError, TypeError, json.JSONDecodeError, KeyError) as exc:
        return {"error": str(exc)}


def tool_declare_sync_ready(args: dict[str, Any]) -> dict[str, Any]:
    cycle_n = args.get("cycle_number")
    ws = str(args.get("workstream") or "").strip() or None
    declared_by = str(args.get("declared_by") or "").strip() or None
    return _spq_call(
        _project(args),
        "declare_sync_ready",
        cycle_n=int(cycle_n) if cycle_n not in (None, "") else None,
        workstream=ws,
        declared_by=declared_by,
    )


def tool_evaluate_sync(args: dict[str, Any]) -> dict[str, Any]:
    cycle_n = args.get("cycle_number")
    use_cache = args.get("use_cache")
    if isinstance(use_cache, str):
        use_cache = use_cache.strip().lower() in ("1", "true", "yes")
    elif use_cache is None:
        use_cache = True
    return _spq_call(
        _project(args),
        "evaluate_sync",
        cycle_n=int(cycle_n) if cycle_n not in (None, "") else None,
        use_cache=bool(use_cache),
    )


def tool_clear_sync(args: dict[str, Any]) -> dict[str, Any]:
    cycle_n = args.get("cycle_number")
    return _spq_call(
        _project(args),
        "clear_sync",
        cycle_n=int(cycle_n) if cycle_n not in (None, "") else None,
        cleared_by=args.get("cleared_by"),
    )


def tool_close_cycle(args: dict[str, Any]) -> dict[str, Any]:
    force = args.get("force")
    if isinstance(force, str):
        force = force.strip().lower() in ("1", "true", "yes")
    return _spq_call(
        _project(args),
        "close_cycle",
        proceed_to=str(args.get("proceed_to") or "COMMIT"),
        force=bool(force),
    )


# ── Coordination Cycle ceremony (#305) ───────────────────────────────────────
# A release is not a lifecycle state, but opening, revising and clearing one are
# ceremony ACTS and are gated exactly like opening a Cycle. The reads live on the
# shared `spq_*` tools, which arrive via `_spq_tools()` below.


def tool_open_coordination_cycle(args: dict[str, Any]) -> dict[str, Any]:
    children = args.get("children")
    if not isinstance(children, list) or not children:
        return {"error": "children must be a non-empty array"}
    return _spq_call(
        _project(args),
        "open_coordination_cycle",
        release_goal=str(args.get("release_goal") or ""),
        children=children,
        dependency_edges=args.get("dependency_edges") or [],
        coordination_seq=args.get("coordination_seq"),
        baseline_sha=str(args.get("baseline_sha") or ""),
        created_by=str(args.get("created_by") or ""),
    )


def tool_revise_coordination_manifest(args: dict[str, Any]) -> dict[str, Any]:
    return _spq_call(
        _project(args),
        "revise_coordination_manifest",
        coordination_cycle_id=args.get("coordination_cycle_id"),
        drop_child=str(args.get("drop_child") or ""),
        reason=str(args.get("reason") or ""),
        revised_by=str(args.get("revised_by") or ""),
    )


def tool_clear_release(args: dict[str, Any]) -> dict[str, Any]:
    return _spq_call(
        _project(args),
        "clear_release",
        coordination_cycle_id=args.get("coordination_cycle_id"),
        cleared_by=args.get("cleared_by"),
    )


# See the Codex server: shared #303 manifest/ledger surface, no ceremony
# duplication.
def _spq_tools() -> dict[str, Any]:
    try:
        import spq_mcp
    except ImportError:  # pragma: no cover
        return {}
    return spq_mcp.tools(policy_gate=_require_not_baa)


TOOLS: dict[str, Any] = {
    "doctor": {
        "fn": tool_doctor,
        "description": (
            "Read-only Cursor execution readiness: auth, receipt delivery, "
            "pipeline state, composed runtime, configuration, BAA."
        ),
        "schema": mcp_transport.PROJECT_ONLY_SCHEMA,
    },
    "get_status": {
        "fn": tool_get_status,
        "description": "Redacted board summary (ids and states, no titles or AC).",
        "schema": mcp_transport.PROJECT_ONLY_SCHEMA,
    },
    "get_state": {
        "fn": tool_get_state,
        "description": "Read pipeline-state.json for the current project.",
        "schema": mcp_transport.PROJECT_ONLY_SCHEMA,
    },
    "next_action": {
        "fn": tool_next_action,
        "description": "Deterministic next dispatch from the active lifecycle state machine.",
        "schema": mcp_transport.PROJECT_ONLY_SCHEMA,
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
        "schema": mcp_transport.ADVANCE_SCHEMA,
    },
    "begin_dispatch": {
        "fn": tool_begin_dispatch,
        "description": "Authorize the dispatch the deterministic next_action selected.",
        "schema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string"},
                "story_id": {"type": "string"},
                "role": {"type": "string"},
            },
            "required": ["story_id"],
        },
    },
    "begin_lifecycle_dispatch": {
        "fn": tool_begin_lifecycle_dispatch,
        "description": (
            "Authorize a Checkpoint/Acceptance role selected by next_action. "
            "Archives a failed receipt and re-binds a new dispatch_id."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string"},
                "story_id": {"type": "string"},
                "role": {"type": "string"},
            },
            "required": ["story_id", "role"],
        },
    },
    "tracker_update_status": {
        "fn": tool_tracker_update_status,
        "description": "Update a tracker ticket status via tracker_cli.py.",
        "schema": mcp_transport.TRACKER_UPDATE_SCHEMA,
    },
    "tracker_get_backlog": {
        "fn": tool_tracker_get_backlog,
        "description": "Get the backlog via tracker_cli.py.",
        "schema": mcp_transport.PROJECT_ONLY_SCHEMA,
    },
    "tracker_health_check": {
        "fn": tool_tracker_health_check,
        "description": "Tracker adapter health-check.",
        "schema": mcp_transport.PROJECT_ONLY_SCHEMA,
    },
    "accept_story": {
        "fn": tool_accept_story,
        "description": "Accept a story (clears await_acceptance). Scrum and SPQ.",
        "schema": mcp_transport.ACCEPT_STORY_SCHEMA,
    },
    "request_acceptance": {
        "fn": tool_request_acceptance,
        "description": "Move a story to awaiting_acceptance (per-story PO gate).",
        "schema": mcp_transport.REQUEST_ACCEPTANCE_SCHEMA,
    },
    "initialize": {
        "fn": tool_initialize,
        "description": "Seed lifecycle state. Pass workstream_id on an SPQ clone.",
        "schema": mcp_transport.INITIALIZE_SCHEMA,
    },
    "transition": {
        "fn": tool_transition,
        "description": "Transition the lifecycle (e.g. CYCLE_EXECUTION → SYNC).",
        "schema": mcp_transport.TRANSITION_SCHEMA,
    },
    "approve_baseline": {
        "fn": tool_approve_baseline,
        "description": "SPQ: approve the Discovery baseline and enter COMMIT.",
        "schema": mcp_transport.APPROVE_BASELINE_SCHEMA,
    },
    "open_cycle": {
        "fn": tool_open_cycle,
        "description": "SPQ: admit Work Units and open a Cycle from COMMIT.",
        "schema": mcp_transport.OPEN_CYCLE_SCHEMA,
    },
    "hydrate_cycle": {
        "fn": tool_hydrate_cycle,
        "description": "SPQ: bring a workstream clone into Cycle execution.",
        "schema": mcp_transport.HYDRATE_CYCLE_SCHEMA,
    },
    "declare_sync_ready": {
        "fn": tool_declare_sync_ready,
        "description": "SPQ: write this workstream's readiness record.",
        "schema": mcp_transport.DECLARE_SYNC_READY_SCHEMA,
    },
    "evaluate_sync": {
        "fn": tool_evaluate_sync,
        "description": "SPQ: evaluate the Sync barrier on the integration clone.",
        "schema": mcp_transport.EVALUATE_SYNC_SCHEMA,
    },
    "clear_sync": {
        "fn": tool_clear_sync,
        "description": "SPQ: clear the barrier and transition SYNC → CHECKPOINT.",
        "schema": mcp_transport.CLEAR_SYNC_SCHEMA,
    },
    "close_cycle": {
        "fn": tool_close_cycle,
        "description": "SPQ: close the Cycle at CHECKPOINT.",
        "schema": mcp_transport.CLOSE_CYCLE_SCHEMA,
    },
    "open_coordination_cycle": {
        "fn": tool_open_coordination_cycle,
        "description": (
            "SPQ: open a Coordination Cycle pinning exact child increments."
        ),
        "schema": mcp_transport.OPEN_COORDINATION_SCHEMA,
    },
    "revise_coordination_manifest": {
        "fn": tool_revise_coordination_manifest,
        "description": "SPQ: revise the release — today, drop a late child.",
        "schema": mcp_transport.REVISE_COORDINATION_SCHEMA,
    },
    "clear_release": {
        "fn": tool_clear_release,
        "description": "SPQ: clear a green release and emit the release gate.",
        "schema": mcp_transport.CLEAR_RELEASE_SCHEMA,
    },
}

TOOLS.update(_spq_tools())


def handle_tool(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Invoke a Cursor tool. Kept as a named function: the tests call it directly."""
    return mcp_transport.call_tool(TOOLS, name, arguments)


def main() -> int:
    # Cursor speaks Content-Length framed MCP stdio in production; the NDJSON
    # switch is honoured by the shared transport.
    return mcp_transport.serve(
        TOOLS,
        server_name="synaptory",
        server_version="1.0.0",
        default_framing=mcp_transport.FRAMED,
    )


if __name__ == "__main__":
    raise SystemExit(main())
