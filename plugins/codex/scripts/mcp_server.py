#!/usr/bin/env python3
"""Native Synaptory runtime adapter for Codex.

Codex owns host integration and agent dispatch.  Synaptory's shared lifecycle
engines own state selection, receipt validation, and legal transitions.  The
``begin_dispatch`` and ``advance`` tools form a fail-closed boundary: a model
cannot choose the next role or mutate pipeline state from prompt memory.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from project_bootstrap import bootstrap_project, inspect_bootstrap  # noqa: E402
from auth_gate import authentication_status  # noqa: E402

MIN_CODEX_VERSION = (0, 147, 0)
ROLE_NAMES = {
    "se": "software-engineer",
    "qe": "quality-engineer",
    "cr": "code-reviewer",
    "ce": "compliance-engineer",
    "pe": "platform-engineer",
    "po": "project-owner",
    "sa": "solution-architect",
    "tw": "technical-writer",
    "ra": "research-advisor",
}

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


def _runtime_root() -> Path | None:
    """Find composed runtime first, then the monorepo source during development."""

    if (PLUGIN_ROOT / "hooks" / "lib" / "story_pipeline.py").is_file():
        return PLUGIN_ROOT
    for parent in PLUGIN_ROOT.parents:
        candidate = parent / "plugin-claude"
        if (candidate / "hooks" / "lib" / "story_pipeline.py").is_file():
            return candidate
    return None


RUNTIME_ROOT = _runtime_root()
LIB = RUNTIME_ROOT / "hooks" / "lib" if RUNTIME_ROOT else PLUGIN_ROOT / "hooks" / "lib"
SHARED_SCRIPTS = (
    RUNTIME_ROOT / "runtime" / "scripts"
    if RUNTIME_ROOT and (RUNTIME_ROOT / "runtime" / "scripts").is_dir()
    else RUNTIME_ROOT / "skills" / "_shared" / "scripts"
    if RUNTIME_ROOT
    else PLUGIN_ROOT / "skills" / "_shared" / "scripts"
)
for path in (LIB, SHARED_SCRIPTS, SHARED_SCRIPTS / "tracker"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

# The shared runtime still accepts this legacy environment name when locating
# its evidence contract.  Keep the compatibility detail inside the adapter.
if RUNTIME_ROOT:
    os.environ.setdefault("CLAUDE_PLUGIN_ROOT", str(RUNTIME_ROOT))
    os.environ.setdefault("SYNAPTORY_PLUGIN_ROOT", str(RUNTIME_ROOT))
os.environ.setdefault("SYNAPTORY_HOST", "codex")
os.environ.setdefault("SYNAPTORY_IDE", "codex")


def _server_version() -> str:
    manifest = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
    try:
        value = json.loads(manifest.read_text(encoding="utf-8")).get("version")
    except (OSError, json.JSONDecodeError, AttributeError):
        return "0.0.0"
    return str(value) if value else "0.0.0"


SERVER_VERSION = _server_version()


def _project(arguments: dict[str, Any]) -> Path:
    raw = (
        arguments.get("project_dir")
        or os.environ.get("SYNAPTORY_PROJECT_DIR")
        or os.environ.get("CODEX_PROJECT_DIR")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or os.getcwd()
    )
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("project_dir must be a non-empty string")
    project = Path(raw).expanduser().resolve()
    if not project.is_dir():
        raise ValueError(f"project directory does not exist: {project}")
    return project


def _state_path(project: Path) -> Path:
    return project / ".synaptory" / ".orchestrator" / "pipeline-state.json"


def _read_state(project: Path) -> dict[str, Any]:
    path = _state_path(project)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"pipeline state is not valid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("pipeline state must contain a JSON object")
    return payload


def _regulated(project: Path) -> bool:
    try:
        from story_pipeline import healthcare_baa_enforced

        return bool(healthcare_baa_enforced(project))
    except Exception:
        # The regulated-project guard is the boundary protecting every MCP
        # operation.  A missing or broken shared parser must block execution,
        # not silently downgrade the project to the standard profile.
        return True


def _require_standard_project(project: Path) -> dict[str, Any] | None:
    if _regulated(project):
        return {
            "error": (
                "Codex execution is refused for baa_enforced projects until the "
                "Synaptory Codex regulated profile and route attestation are certified"
            )
        }
    return None


def _codex_runtime(project: Path | None = None) -> dict[str, Any]:
    executable = shutil.which("codex")
    if not executable:
        return {
            "installed": False,
            "version": None,
            "minimum": ".".join(map(str, MIN_CODEX_VERSION)),
            "meets_minimum": False,
        }
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=2,
            cwd=project,
        )
        output = f"{result.stdout}\n{result.stderr}".strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "installed": True,
            "version": None,
            "minimum": ".".join(map(str, MIN_CODEX_VERSION)),
            "meets_minimum": False,
            "error": str(exc),
        }
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", output)
    parsed = tuple(int(part) for part in match.groups()) if match else None
    feature_enabled = False
    feature_error: str | None = None
    try:
        feature_result = subprocess.run(
            [executable, "features", "list"],
            capture_output=True,
            check=False,
            text=True,
            timeout=2,
            cwd=project,
        )
        for line in feature_result.stdout.splitlines():
            match = re.match(r"^multi_agent_v2\s+\S+\s+(true|false)\s*$", line.strip())
            if match:
                feature_enabled = match.group(1) == "true"
                break
        if feature_result.returncode != 0:
            feature_error = (feature_result.stderr or feature_result.stdout).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        feature_error = str(exc)
    payload = {
        "installed": True,
        "version": ".".join(map(str, parsed)) if parsed else None,
        "minimum": ".".join(map(str, MIN_CODEX_VERSION)),
        "meets_minimum": bool(parsed and parsed >= MIN_CODEX_VERSION),
        "multi_agent_v2_enabled": feature_enabled,
    }
    if feature_error:
        payload["feature_error"] = feature_error
    return payload


def _runtime_status() -> dict[str, Any]:
    required = (
        "story_pipeline",
        "receipt_validator",
        "scrum_state_machine",
        "kanban_state_machine",
        "spq_state_machine",
    )
    missing: list[str] = []
    for name in required:
        try:
            importlib.import_module(name)
        except Exception as exc:  # report the boundary without crashing MCP startup
            missing.append(f"{name}: {type(exc).__name__}: {exc}")
    return {
        "available": not missing,
        "root": str(RUNTIME_ROOT) if RUNTIME_ROOT else None,
        "missing": missing,
    }


def _build_mode(project: Path) -> str:
    mode = str(_read_state(project).get("build_mode") or "scrum").lower()
    if mode not in {"scrum", "kanban", "spq"}:
        raise ValueError(f"unsupported Synaptory build_mode: {mode}")
    return mode


def _mode_module(project: Path) -> Any:
    return importlib.import_module(f"{_build_mode(project)}_state_machine")


def _flat_state(project: Path) -> dict[str, Any]:
    return _mode_module(project).read_state(str(project))


def _next_action(project: Path) -> dict[str, Any]:
    result = _mode_module(project).next_action(str(project))
    if not isinstance(result, dict):
        raise ValueError("state machine next_action returned a non-object")
    return result


def _select_state(state: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    if state.get("version") == "3.0" and isinstance(state.get("specs"), dict):
        active = state.get("active_spec")
        if isinstance(active, str):
            selected = state["specs"].get(active)
            if isinstance(selected, dict):
                return selected, active
        return {}, active if isinstance(active, str) else None
    return state, None


def _story_summary(selected: dict[str, Any]) -> dict[str, Any]:
    stories = selected.get("current_stories")
    if not isinstance(stories, list):
        stories = selected.get("stories")
    if not isinstance(stories, list):
        return {"total": 0, "by_status": {}}
    counts: dict[str, int] = {}
    for story in stories:
        if not isinstance(story, dict):
            continue
        status = str(story.get("state") or story.get("sub_state") or story.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {"total": sum(counts.values()), "by_status": dict(sorted(counts.items()))}


def _receipt_directory(project: Path) -> Path:
    state = _read_state(project)
    orchestrator = project / ".synaptory" / ".orchestrator"
    active = state.get("active_spec") if state.get("version") == "3.0" else None
    if isinstance(active, str) and active:
        return orchestrator / "specs" / active / "receipts"
    return orchestrator / "receipts"


def _scoped_receipt_path(project: Path, raw: str | os.PathLike[str]) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = project / candidate
    candidate = candidate.resolve()
    orchestrator = (project / ".synaptory" / ".orchestrator").resolve()
    try:
        candidate.relative_to(orchestrator)
    except ValueError as exc:
        raise ValueError("receipt_path must be inside .synaptory/.orchestrator") from exc
    return candidate


def _read_receipt(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"receipt is not readable: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"receipt is not valid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("receipt must contain a JSON object")
    return payload


def _expected_receipt_role(from_state: str, to_state: str) -> tuple[str, str] | None:
    if to_state == "blocked":
        return _BLOCKED_FROM_ROLE.get(from_state)
    return _TRANSITION_RECEIPT.get((from_state, to_state))


def _canonical_receipt_path(project: Path, story_id: str, abbreviation: str) -> Path:
    from story_pipeline import get_story_receipt_path  # type: ignore

    directory = _receipt_directory(project)
    candidate = directory / get_story_receipt_path(story_id, abbreviation)
    if directory.is_symlink() or candidate.is_symlink():
        raise ValueError("canonical receipt path may not be a symlink")
    resolved_directory = directory.resolve()
    resolved_candidate = candidate.resolve()
    try:
        resolved_candidate.relative_to(resolved_directory)
    except ValueError as exc:
        raise ValueError("canonical receipt path escapes its receipt directory") from exc
    return resolved_candidate


def _parse_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _stage_entered_at(story: dict[str, Any]) -> datetime | None:
    state = str(story.get("state") or "")
    entries = story.get("pipeline_log")
    if not isinstance(entries, list):
        return None
    for entry in reversed(entries):
        if isinstance(entry, dict) and str(entry.get("state") or "") == state:
            return _parse_timestamp(entry.get("entered_at"))
    return None


def _role_name(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return ROLE_NAMES.get(value, value)


def _get_story(project: Path, story_id: str) -> dict[str, Any] | None:
    from story_pipeline import get_story  # type: ignore

    return get_story(_flat_state(project), story_id)


def _transition_story(project: Path, story_id: str, to_state: str, reason: str) -> dict[str, Any]:
    module = _mode_module(project)
    return module.transition_story(str(project), story_id, to_state, reason)


def _unblock_story(project: Path, story_id: str, reason: str) -> None:
    from story_pipeline import unblock_story  # type: ignore

    module = _mode_module(project)
    state = module.read_state(str(project))
    state = unblock_story(state, story_id, project_dir=str(project))
    module._write_state(str(project), state)  # shared state-machine persistence seam


def _execution_readiness(project: Path) -> dict[str, Any]:
    """Return the single readiness decision used by doctor and mutating tools."""

    state_file = _state_path(project)
    state_readable = False
    state_error: str | None = None
    if state_file.is_file():
        try:
            _read_state(project)
            state_readable = True
        except ValueError as exc:
            state_error = str(exc)
    runtime = _codex_runtime(project)
    shared = _runtime_status()
    authentication = authentication_status(project)
    regulated = _regulated(project)
    agent_bootstrap = inspect_bootstrap(project)
    ready = bool(
        runtime["meets_minimum"]
        and runtime.get("multi_agent_v2_enabled") is True
        and authentication["ready"]
        and state_readable
        and shared["available"]
        and agent_bootstrap["installed"]
        and not regulated
    )
    missing = [
        "Codex CLI >=0.147.0" if not runtime["meets_minimum"] else None,
        "Codex multi_agent_v2 feature enabled" if not runtime.get("multi_agent_v2_enabled") else None,
        "authenticated Synaptory control-plane session" if not authentication["ready"] else None,
        "readable Synaptory pipeline state" if not state_readable else None,
        "all nine Synaptory custom-agent profiles" if not agent_bootstrap["installed"] else None,
        "composed Synaptory lifecycle and receipt runtime" if not shared["available"] else None,
        "certified regulated Codex profile" if regulated else None,
    ]
    return {
        "host": "codex",
        "runtime_generation": 2,
        "execution_scope": "standard-se-qe-cr",
        "project": {
            "detected": state_file.is_file(),
            "path": str(project),
            "regulated": regulated,
            "state_error": state_error,
        },
        "checks": {
            "codex_runtime": runtime,
            "authentication": authentication,
            "pipeline_state_readable": state_readable,
            "regulated_execution_allowed": not regulated,
            "custom_agent_profiles_installed": agent_bootstrap["installed"],
            "custom_agent_profiles": agent_bootstrap,
            "shared_runtime": shared,
            "receipt_gate_enabled": shared["available"],
            "provider_route_attested": os.environ.get("SYNAPTORY_PROVIDER_ROUTE_ATTESTED") == "1",
        },
        "status_ready": state_readable and not regulated,
        "structured_execution_ready": ready,
        "missing_requirements": [item for item in missing if item],
    }


def _require_execution_ready(project: Path) -> dict[str, Any] | None:
    """Fail closed before dispatch, state mutation, or tracker mutation."""

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
    if status["ready"]:
        return None
    return {
        "error": "mutation refused: authenticated Synaptory session is required",
        "authentication": status,
    }


def tool_doctor(arguments: dict[str, Any]) -> dict[str, Any]:
    return _execution_readiness(_project(arguments))


def tool_get_status(arguments: dict[str, Any]) -> dict[str, Any]:
    project = _project(arguments)
    blocked = _require_standard_project(project)
    if blocked:
        return blocked
    state = _read_state(project)
    if not state:
        return {"detected": False, "project_dir": str(project)}
    selected, active_spec = _select_state(state)
    return {
        "detected": True,
        "schema_version": state.get("version"),
        "build_mode": state.get("build_mode") or selected.get("build_mode"),
        "active_spec": active_spec,
        "lifecycle_state": selected.get("lifecycle_state"),
        "current_sprint": selected.get("current_sprint"),
        "total_sprints": selected.get("total_sprints"),
        "stories": _story_summary(selected),
        "structured_execution_ready": tool_doctor(arguments)["structured_execution_ready"],
    }


def tool_get_state(arguments: dict[str, Any]) -> dict[str, Any]:
    project = _project(arguments)
    blocked = _require_standard_project(project)
    if blocked:
        return blocked
    return {
        "host": "codex",
        "build_mode": _build_mode(project),
        "active_spec": _read_state(project).get("active_spec"),
        "state": _flat_state(project),
    }


def tool_next_action(arguments: dict[str, Any]) -> dict[str, Any]:
    project = _project(arguments)
    blocked = _require_standard_project(project)
    if blocked:
        return blocked
    return {"build_mode": _build_mode(project), "next_action": _next_action(project)}


def tool_bootstrap_project(arguments: dict[str, Any]) -> dict[str, Any]:
    project = _project(arguments)
    apply = arguments.get("apply", False)
    if not isinstance(apply, bool):
        raise ValueError("apply must be a boolean")
    if apply:
        blocked = _require_authenticated(project)
        if blocked:
            return {**blocked, "applied": False, "written": []}
    return bootstrap_project(project, apply=apply)


def tool_validate_receipt(arguments: dict[str, Any]) -> dict[str, Any]:
    project = _project(arguments)
    blocked = _require_standard_project(project)
    if blocked:
        return {**blocked, "valid": False}
    raw_path = arguments.get("receipt_path")
    if not isinstance(raw_path, str) or not raw_path:
        return {"error": "receipt_path is required", "valid": False}
    path = _scoped_receipt_path(project, raw_path)
    from receipt_validator import validate_receipt  # type: ignore

    result = validate_receipt(str(path), str(project))
    return {**result.to_dict(), "receipt_path": str(path)}


def tool_begin_dispatch(arguments: dict[str, Any]) -> dict[str, Any]:
    """Authorize exactly the dispatch selected by the deterministic engine."""

    project = _project(arguments)
    blocked = _require_standard_project(project)
    if blocked:
        return {**blocked, "authorized": False}
    blocked = _require_execution_ready(project)
    if blocked:
        return {**blocked, "authorized": False}
    selected = _next_action(project)
    story_id = str(arguments.get("story_id") or "")
    requested_role = _role_name(arguments.get("role"))
    selected_story = str(selected.get("story_id") or "")
    selected_role = _role_name(selected.get("role"))
    action = str(selected.get("action") or "")
    if not story_id or story_id != selected_story:
        return {
            "error": f"dispatch refused: next story is {selected_story or 'none'}, not {story_id or 'unspecified'}",
            "authorized": False,
            "next_action": selected,
        }
    if requested_role and requested_role != selected_role:
        return {
            "error": f"dispatch refused: next role is {selected_role}, not {requested_role}",
            "authorized": False,
            "next_action": selected,
        }
    dispatchable = action.startswith("dispatch_") or action == "recover_blocked"
    if not dispatchable:
        return {
            "error": f"dispatch refused: deterministic action is {action}",
            "authorized": False,
            "next_action": selected,
        }
    if selected.get("receipt_present") and selected.get("transition_to") and not selected.get("recovery"):
        return {
            "error": "dispatch refused: a fresh receipt exists; call advance instead",
            "authorized": False,
            "next_action": selected,
        }

    story = _get_story(project, story_id)
    if not story:
        return {"error": f"dispatch refused: story not found: {story_id}", "authorized": False}
    if action == "recover_blocked":
        _unblock_story(project, story_id, "Codex recovery dispatch authorized by next_action")
        story = _get_story(project, story_id) or story
    elif story.get("state") == "queued" and selected_role == "software-engineer":
        _transition_story(project, story_id, "in_progress", "Codex software-engineer dispatch authorized")
        story = _get_story(project, story_id) or story

    receipt_path = _receipt_directory(project) / f"{story_id}-{str(selected.get('role') or 'agent')}.json"
    return {
        "authorized": True,
        "host": "codex",
        "action": action,
        "agent_profile": f"synaptory-{selected_role}",
        "role": selected_role,
        "story": story,
        "receipt_path": str(receipt_path),
        "receipt_contract": {
            "backend": "codex",
            "required_fields": [
                "story_id",
                "role",
                "backend",
                "model",
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
        },
        "next_action": selected,
    }


def tool_advance(arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate evidence and apply only the transition selected by next_action."""

    project = _project(arguments)
    blocked = _require_standard_project(project)
    if blocked:
        return {**blocked, "advanced": False}
    blocked = _require_execution_ready(project)
    if blocked:
        return {**blocked, "advanced": False}
    story_id = str(arguments.get("story_id") or "")
    requested_state = str(arguments.get("to_state") or "")
    if not story_id or not requested_state:
        return {"error": "story_id and to_state are required", "advanced": False}
    selected = _next_action(project)
    if selected.get("story_id") != story_id or selected.get("transition_to") != requested_state:
        return {
            "error": "advance refused: requested transition does not match deterministic next_action",
            "advanced": False,
            "next_action": selected,
        }
    story = _get_story(project, story_id)
    if not story:
        return {"error": f"advance refused: story not found: {story_id}", "advanced": False}
    from_state = str(story.get("state") or "")
    expected = _expected_receipt_role(from_state, requested_state)
    if expected is None:
        return {
            "error": f"advance refused: no bound receipt role for {from_state} → {requested_state}",
            "advanced": False,
        }
    expected_role, abbreviation = expected
    try:
        path = _canonical_receipt_path(project, story_id, abbreviation)
    except ValueError as exc:
        return {"error": f"advance refused: {exc}", "advanced": False}
    given = arguments.get("receipt_path")
    if given:
        try:
            supplied = _scoped_receipt_path(project, str(given))
        except ValueError as exc:
            return {"error": f"advance refused: {exc}", "advanced": False}
        if supplied != path.resolve():
            return {
                "error": (
                    "advance refused: receipt_path must be the canonical "
                    f"{path.name} for this story and stage"
                ),
                "advanced": False,
            }
    if not path.is_file():
        return {
            "error": f"advance refused: no receipt on disk for {story_id} (expected {path.name})",
            "advanced": False,
            "next_action": selected,
        }
    receipt = _read_receipt(path)
    if receipt.get("story_id") != story_id:
        return {"error": "advance refused: receipt story_id mismatch", "advanced": False}
    if receipt.get("role") != expected_role:
        return {
            "error": f"advance refused: expected a {expected_role} receipt",
            "advanced": False,
        }
    if receipt.get("backend") != "codex":
        return {
            "error": "advance refused: receipt backend must be codex for a Codex dispatch",
            "advanced": False,
        }
    from receipt_validator import validate_receipt  # type: ignore

    result = validate_receipt(str(path), str(project))
    if not result.valid:
        return {
            "error": "advance refused: receipt invalid",
            "receipt_path": str(path),
            "errors": result.errors,
            "warnings": result.warnings,
            "advanced": False,
        }
    entered_at = _stage_entered_at(story)
    completed_at = _parse_timestamp(receipt.get("completed_at"))
    if entered_at is None:
        return {
            "error": "advance refused: current stage has no valid entered_at timestamp",
            "advanced": False,
        }
    if completed_at is None or completed_at <= entered_at:
        return {
            "error": (
                "advance refused: receipt is stale (completed_at must be after "
                "the current stage entered_at)"
            ),
            "advanced": False,
        }
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    consumed = story.get("mcp_consumed_receipts", [])
    if not isinstance(consumed, list):
        return {
            "error": "advance refused: consumed-receipt ledger is invalid",
            "advanced": False,
        }
    if digest in consumed:
        return {
            "error": "advance refused: receipt already consumed (replay)",
            "advanced": False,
        }
    reason = str(arguments.get("reason") or f"Codex receipt validated: {path.name}")
    updated_state = _transition_story(project, story_id, requested_state, reason)
    from story_pipeline import get_story  # type: ignore

    updated_story = get_story(updated_state, story_id)
    if not updated_story:
        raise ValueError(f"transitioned story disappeared from state: {story_id}")
    ledger = updated_story.setdefault("mcp_consumed_receipts", [])
    if not isinstance(ledger, list):
        raise ValueError("transitioned story has an invalid consumed-receipt ledger")
    ledger.append(digest)
    _mode_module(project)._write_state(str(project), updated_state)
    return {
        "advanced": True,
        "story": updated_story,
        "receipt_path": str(path),
        "receipt_warnings": result.warnings,
        "next_action": _next_action(project),
    }


def _tracker(project: Path, argv: list[str]) -> dict[str, Any]:
    blocked = _require_standard_project(project)
    if blocked:
        return blocked
    cli = SHARED_SCRIPTS / "tracker" / "tracker_cli.py"
    if not cli.is_file():
        return {"error": "shared tracker runtime is not installed"}
    proc = subprocess.run(
        [sys.executable, str(cli), "--project-dir", str(project), *argv],
        capture_output=True,
        text=True,
        check=False,
    )
    output: dict[str, Any] = {"returncode": proc.returncode}
    if proc.stdout.strip():
        try:
            output["result"] = json.loads(proc.stdout)
        except json.JSONDecodeError:
            output["stdout"] = proc.stdout
    if proc.stderr.strip():
        output["stderr"] = proc.stderr
    return output


def tool_tracker_update_status(arguments: dict[str, Any]) -> dict[str, Any]:
    project = _project(arguments)
    blocked = _require_execution_ready(project)
    if blocked:
        return blocked
    argv = ["update-status", str(arguments["story_id"]), str(arguments["status"])]
    if arguments.get("allow_skip"):
        argv.append("--allow-skip")
    return _tracker(project, argv)


def tool_tracker_get_backlog(arguments: dict[str, Any]) -> dict[str, Any]:
    return _tracker(_project(arguments), ["get-backlog"])


def tool_tracker_health_check(arguments: dict[str, Any]) -> dict[str, Any]:
    return _tracker(_project(arguments), ["health-check"])


TOOLS: dict[str, dict[str, Any]] = {
    "doctor": {
        "fn": tool_doctor,
        "description": "Check native Codex execution readiness for a Synaptory project.",
        "schema": {"type": "object", "properties": {"project_dir": {"type": "string"}}},
    },
    "get_status": {
        "fn": tool_get_status,
        "description": "Read a redacted summary of Synaptory pipeline state.",
        "schema": {"type": "object", "properties": {"project_dir": {"type": "string"}}},
    },
    "get_state": {
        "fn": tool_get_state,
        "description": "Read the active lifecycle state required to build an execution envelope.",
        "schema": {"type": "object", "properties": {"project_dir": {"type": "string"}}},
    },
    "next_action": {
        "fn": tool_next_action,
        "description": "Return the deterministic next dispatch, transition, or human gate.",
        "schema": {"type": "object", "properties": {"project_dir": {"type": "string"}}},
    },
    "bootstrap_project": {
        "fn": tool_bootstrap_project,
        "description": "Plan or explicitly install all managed Synaptory Codex agent profiles.",
        "schema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string"},
                "apply": {"type": "boolean", "default": False},
            },
        },
    },
    "begin_dispatch": {
        "fn": tool_begin_dispatch,
        "description": "Authorize only the story and role selected by next_action and return its envelope.",
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
    "validate_receipt": {
        "fn": tool_validate_receipt,
        "description": "Validate a Codex story receipt without advancing lifecycle state.",
        "schema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string"},
                "receipt_path": {"type": "string"},
            },
            "required": ["receipt_path"],
        },
    },
    "advance": {
        "fn": tool_advance,
        "description": "Fail-closed receipt gate: apply only the transition returned by next_action.",
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
        "description": "Update a tracker item through the shared Synaptory adapter.",
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
        "description": "Read the configured tracker backlog.",
        "schema": {"type": "object", "properties": {"project_dir": {"type": "string"}}},
    },
    "tracker_health_check": {
        "fn": tool_tracker_health_check,
        "description": "Check the configured tracker adapter.",
        "schema": {"type": "object", "properties": {"project_dir": {"type": "string"}}},
    },
}


def handle_tool(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    spec = TOOLS.get(name)
    if spec is None:
        return {"error": f"unknown tool: {name}"}
    try:
        fn: Callable[[dict[str, Any]], dict[str, Any]] = spec["fn"]
        return fn(arguments or {})
    except Exception as exc:
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


def _dispatch(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        requested_protocol = (request.get("params") or {}).get("protocolVersion")
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": requested_protocol or "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "synaptory", "version": SERVER_VERSION},
            },
        }
    if method in {"notifications/initialized", "initialized"}:
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": _tools_list()}
    if method == "tools/call":
        params = request.get("params") or {}
        result = handle_tool(str(params.get("name") or ""), params.get("arguments") or {})
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(result, default=str)}],
                "isError": bool(result.get("error")),
            },
        }
    if request_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _read_framed() -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        decoded = line.decode("utf-8", "replace")
        if ":" in decoded:
            key, value = decoded.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length") or 0)
    if length <= 0:
        return None
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def _write_framed(payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def main() -> int:
    # MCP stdio uses one JSON-RPC message per line.  Keep the old framed mode
    # behind an explicit compatibility switch for older host experiments; it
    # must never be the Codex default because header bytes corrupt the stdio
    # handshake before initialize completes.
    if os.environ.get("SYNAPTORY_MCP_FRAMED") == "1":
        while True:
            try:
                request = _read_framed()
            except Exception:
                return 0
            if request is None:
                return 0
            response = _dispatch(request)
            if response is not None:
                _write_framed(response)
        return 0
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            response = _dispatch(request)
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
        if response is not None:
            print(json.dumps(response, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
