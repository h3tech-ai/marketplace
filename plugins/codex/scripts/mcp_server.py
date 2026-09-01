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
import secrets
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
from codex_attribution import attribute_receipt_from_dispatch  # noqa: E402
from receipt_delivery import delivery_status, ship_validated_receipt  # noqa: E402

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

def _runtime_paths() -> tuple[Path | None, Path, Path]:
    """Resolve (root, lib, shared_scripts) for the composed package or dev tree.

    Two layouts, probed in order:
      composed package  <plugin>/hooks/lib  + <plugin>/runtime/scripts
      monorepo dev      <repo>/core/lib     + <repo>/core/scripts

    The dev branch targets the host-neutral `core/`, never a sibling host
    plugin: Codex depends on shared runtime, not on plugin-claude.
    """

    if (PLUGIN_ROOT / "hooks" / "lib" / "story_pipeline.py").is_file():
        return (
            PLUGIN_ROOT,
            PLUGIN_ROOT / "hooks" / "lib",
            PLUGIN_ROOT / "runtime" / "scripts",
        )
    for parent in PLUGIN_ROOT.parents:
        core = parent / "core"
        if (core / "lib" / "story_pipeline.py").is_file():
            return core, core / "lib", core / "scripts"
    return (
        None,
        PLUGIN_ROOT / "hooks" / "lib",
        PLUGIN_ROOT / "runtime" / "scripts",
    )


RUNTIME_ROOT, LIB, SHARED_SCRIPTS = _runtime_paths()
for path in (LIB, SHARED_SCRIPTS, SHARED_SCRIPTS / "tracker"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

# The shared runtime reads SYNAPTORY_PLUGIN_ROOT (host_env), so a Codex process
# no longer has to set a Claude-named variable to make core/ work (#285).
if RUNTIME_ROOT:
    os.environ.setdefault("SYNAPTORY_PLUGIN_ROOT", str(RUNTIME_ROOT))
os.environ.setdefault("SYNAPTORY_HOST", "codex")
os.environ.setdefault("SYNAPTORY_IDE", "codex")

# The shared gate. Every advance/dispatch check lives here so all three hosts
# enforce one contract; this server only supplies Codex-specific policy.
import advance_kernel  # noqa: E402
import mcp_transport  # noqa: E402


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
    feature_values: dict[str, bool] = {}
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
            match = re.match(
                r"^(multi_agent(?:_v2)?)\s+\S+\s+(true|false)\s*$",
                line.strip(),
            )
            if match:
                feature_values[match.group(1)] = match.group(2) == "true"
        if feature_result.returncode != 0:
            feature_error = (feature_result.stderr or feature_result.stdout).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        feature_error = str(exc)
    canonical_feature = feature_values.get("multi_agent")
    legacy_feature = feature_values.get("multi_agent_v2")
    effective_multi_agent = (
        canonical_feature
        if canonical_feature is not None
        else legacy_feature is True
    )
    payload = {
        "installed": True,
        "version": ".".join(map(str, parsed)) if parsed else None,
        "minimum": ".".join(map(str, MIN_CODEX_VERSION)),
        "meets_minimum": bool(parsed and parsed >= MIN_CODEX_VERSION),
        "multi_agent_enabled": effective_multi_agent,
        "multi_agent_feature_source": (
            "multi_agent"
            if canonical_feature is not None
            else ("multi_agent_v2" if legacy_feature is not None else None)
        ),
        "multi_agent_v2_enabled": legacy_feature is True,
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


def _configured_build_mode(project: Path) -> str:
    """Read the explicit top-level mode before pipeline state exists."""
    try:
        lines = (project / ".synaptory.yaml").read_text(encoding="utf-8").splitlines()
    except OSError:
        return "scrum"
    for raw in lines:
        if raw != raw.lstrip():
            continue
        match = re.match(r"^build_mode\s*:\s*([^#]+)", raw)
        if not match:
            continue
        mode = match.group(1).strip().strip('"').strip("'").lower()
        if mode not in {"scrum", "kanban", "spq"}:
            raise ValueError(f"unsupported Synaptory build_mode: {mode}")
        return mode
    return "scrum"


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
    receipt_delivery = delivery_status(project, authentication=authentication)
    regulated = _regulated(project)
    agent_bootstrap = inspect_bootstrap(project)
    multi_agent_enabled = runtime.get("multi_agent_enabled")
    if multi_agent_enabled is None:
        # Compatibility with older injected test/runtime records. Real Codex
        # probes now prefer the canonical stable `multi_agent` feature and use
        # `multi_agent_v2` only when the canonical row is absent.
        multi_agent_enabled = runtime.get("multi_agent_v2_enabled")
    try:
        from state_drift import check_drift  # type: ignore

        configuration = check_drift(project)
    except Exception as exc:
        configuration = {
            "ok": False,
            "checked": False,
            "problems": [f"configuration validation unavailable: {exc}"],
        }
    ready = bool(
        runtime["meets_minimum"]
        and multi_agent_enabled is True
        and authentication["ready"]
        and receipt_delivery["ready"]
        and state_readable
        and shared["available"]
        and agent_bootstrap["installed"]
        and configuration["ok"]
        and not regulated
    )
    missing = [
        "Codex CLI >=0.147.0" if not runtime["meets_minimum"] else None,
        "Codex multi_agent feature enabled" if not multi_agent_enabled else None,
        "authenticated Synaptory control-plane session" if not authentication["ready"] else None,
        "durable Codex receipt analytics delivery" if not receipt_delivery["ready"] else None,
        "readable Synaptory pipeline state" if not state_readable else None,
        "all nine Synaptory custom-agent profiles" if not agent_bootstrap["installed"] else None,
        "composed Synaptory lifecycle and receipt runtime" if not shared["available"] else None,
        "certified regulated Codex profile" if regulated else None,
        "valid Synaptory state and configuration" if not configuration["ok"] else None,
    ]
    detected_mode: str | None = None
    if state_readable:
        try:
            detected_mode = _build_mode(project)
        except ValueError:
            detected_mode = None
    return {
        "host": "codex",
        "runtime_generation": 2,
        "execution_scope": (
            "standard-full-spq" if detected_mode == "spq" else "standard-se-qe-cr"
        ),
        "project": {
            "detected": state_file.is_file(),
            "path": str(project),
            "regulated": regulated,
            "state_error": state_error,
        },
        "checks": {
            "codex_runtime": runtime,
            "authentication": authentication,
            "control_plane_receipt_delivery": receipt_delivery,
            "pipeline_state_readable": state_readable,
            "regulated_execution_allowed": not regulated,
            "custom_agent_profiles_installed": agent_bootstrap["installed"],
            "custom_agent_profiles": agent_bootstrap,
            "shared_runtime": shared,
            "configuration": configuration,
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
    mode = _build_mode(project)
    selected = _next_action(project)
    if mode == "spq":
        if selected.get("action") == "not_in_execution":
            selected = _spq_lifecycle_next_action(project)
        elif selected.get("action") == "await_sync":
            selected = _spq_sync_boundary_next_action(project, selected)
    return {"build_mode": mode, "next_action": selected}


def _require_spq(project: Path) -> dict[str, Any] | None:
    if _build_mode(project) == "spq":
        return None
    return {"error": "SPQ lifecycle tools require build_mode: spq"}


def _spq_sync_boundary_next_action(
    project: Path, selected: dict[str, Any]
) -> dict[str, Any]:
    """Expose readiness publication before entering Sync.

    ``declare_ready`` is valid only while the clone is still in
    CYCLE_EXECUTION. Once its record exists, committing and pushing that record
    is an explicit human gate; only then may the lifecycle enter SYNC.
    """
    barrier = importlib.import_module("sync_barrier")
    config = barrier.load_spq_config(str(project))
    workstreams = [item for item in config.get("workstreams", []) if isinstance(item, dict)]
    current = barrier.current_branch(str(project))
    state = _flat_state(project)
    workstream = str(os.environ.get("SYNAPTORY_ACTIVE_SPEC") or "").strip()
    if not workstream:
        # Native SPQ persists workstream identity in the pointer/state store so
        # the route cannot depend on one shell continuing to export an env var.
        # This is especially important for the integration seat, whose branch
        # is normally ``dev``/``sync/*`` rather than a delivery branch.
        workstream = str(state.get("_workstream_id") or "").strip()
    if not workstream and current:
        match = next(
            (str(item.get("id") or "") for item in workstreams if item.get("branch") == current),
            "",
        )
        workstream = match
    if not workstream and len(workstreams) == 1:
        workstream = str(workstreams[0].get("id") or "")
    if not workstream:
        return {
            **selected,
            "action": "sync_workstream_unknown",
            "operation": None,
            "human_gate_pending": True,
            "reason": (
                "all Work Units are terminal, but the current Sync workstream "
                "cannot be resolved from SYNAPTORY_ACTIVE_SPEC or the branch"
            ),
        }
    cycle = int(state.get("current_cycle") or 0)
    workstream_config = next(
        (item for item in workstreams if str(item.get("id") or "") == workstream),
        {},
    )
    if workstream_config.get("integration"):
        readiness = barrier.status(str(project), cycle)
        summary = {
            "workstream": workstream,
            "ready_count": int(readiness.get("ready_count") or 0),
            "quorum": int(readiness.get("quorum") or 0),
        }
        if not readiness.get("records_present"):
            return {
                **selected,
                **summary,
                "action": "await_sync",
                "operation": None,
                "human_gate_pending": True,
                "reason": (
                    f"integration seat is waiting for Cycle {cycle} delivery "
                    f"readiness ({summary['ready_count']}/{summary['quorum']})"
                ),
            }
        return {
            **selected,
            **summary,
            "action": "enter_sync",
            "operation": "enter_sync",
            "human_gate_pending": True,
            "reason": (
                "all delivery readiness records are present; after a human "
                "confirms the integration branch and workstream heads, enter "
                "the guarded Sync ceremony"
            ),
        }
    record = project / barrier.record_relpath(config, cycle, workstream)
    if not record.is_file():
        return {
            **selected,
            "action": "declare_ready",
            "operation": "declare_ready",
            "workstream": workstream,
            "human_gate_pending": False,
            "reason": (
                f"all admitted Work Units are terminal; derive the Cycle {cycle} "
                f"readiness record for workstream {workstream} through spq_lifecycle"
            ),
        }
    return {
        **selected,
        "action": "enter_sync",
        "operation": "enter_sync",
        "workstream": workstream,
        "readiness_path": str(record.relative_to(project)),
        "human_gate_pending": True,
        "reason": (
            "readiness exists; after a human commits and pushes the workstream "
            "head and readiness record, enter the guarded Sync ceremony"
        ),
    }


def _spq_lifecycle_next_action(project: Path) -> dict[str, Any]:
    """Deterministic ceremony action outside Work Unit execution."""
    module = importlib.import_module("spq_state_machine")
    state = module.read_state(str(project))
    lifecycle = str(state.get("lifecycle_state") or "DISCOVERY")
    cycle = int(state.get("current_cycle") or 0)
    base = {"lifecycle_state": lifecycle, "current_cycle": cycle, "story_id": None}
    if lifecycle == "DISCOVERY":
        return {
            **base,
            "action": "approve_baseline",
            "operation": "approve_baseline",
            "human_gate_pending": True,
            "reason": "Discovery baseline needs explicit delivery-owner approval",
        }
    if lifecycle == "COMMIT":
        return {
            **base,
            "action": "open_cycle",
            "operation": "open_cycle",
            "human_gate_pending": True,
            "reason": "admit the approved Cycle goal and Work Units",
        }
    if lifecycle == "SYNC":
        evaluation = state.get("sync_evaluation")
        current_tree_state: str | None = None
        try:
            import verification_cache

            current_tree_state = verification_cache.tree_state(str(project))
        except Exception:
            pass
        evaluation_is_current = bool(
            isinstance(evaluation, dict)
            and evaluation.get("cycle") == cycle
            and evaluation.get("tree_state")
            and evaluation.get("tree_state") == current_tree_state
        )
        if evaluation_is_current and evaluation.get("verdict") == "green":
            return {
                **base,
                "action": "clear_sync",
                "operation": "clear_sync",
                "human_gate_pending": True,
                "reason": (
                    "the current integration tree has a green five-criterion "
                    "Sync verdict; explicit human clearance is required"
                ),
            }
        previous = (
            "; blocking: " + ", ".join(evaluation.get("blocking") or [])
            if evaluation_is_current and isinstance(evaluation, dict)
            else ""
        )
        return {
            **base,
            "action": "evaluate_sync",
            "operation": "evaluate_sync",
            "human_gate_pending": False,
            "reason": (
                "collect merged workstreams and evaluate all five Sync criteria"
                f"{previous}"
            ),
        }
    if lifecycle == "CHECKPOINT":
        readiness = module.checkpoint_readiness(str(project), state=state)
        if not readiness["ready"]:
            return {
                **base,
                "action": "dispatch_tw",
                "role": "tw",
                "story_id": readiness["story_id"],
                "receipt_path": readiness["receipt_path"],
                "human_gate_pending": False,
                "reason": "; ".join(readiness["blocking"]),
            }
        return {
            **base,
            "action": "close_cycle",
            "operation": "close_cycle",
            "human_gate_pending": True,
            "reason": "Checkpoint evidence is valid; choose another Cycle or Acceptance",
        }
    if lifecycle == "ACCEPTANCE":
        readiness = module.acceptance_readiness(str(project), state=state)
        for abbrev in ("qe", "ce", "pe", "tw", "cr"):
            filename = f"{readiness['story_id']}-{abbrev}.json"
            item = readiness["receipts"][abbrev]
            role_blocked = not item["present"] or not item["valid"] or any(
                filename in reason for reason in readiness["blocking"]
            )
            if role_blocked:
                return {
                    **base,
                    "action": f"dispatch_{abbrev}",
                    "role": abbrev,
                    "story_id": readiness["story_id"],
                    "receipt_path": item["receipt_path"],
                    "human_gate_pending": False,
                    "reason": next(
                        (
                            reason
                            for reason in readiness["blocking"]
                            if filename in reason
                        ),
                        f"missing valid {filename}",
                    ),
                }
        if not readiness["ready"]:
            return {
                **base,
                "action": "acceptance_blocked",
                "human_gate_pending": True,
                "reason": "; ".join(readiness["blocking"]),
                "acceptance": readiness,
            }
        return {
            **base,
            "action": "complete_release",
            "operation": "complete",
            "human_gate_pending": True,
            "reason": "all five Acceptance receipts and Cycle barriers are green",
            "acceptance": readiness,
        }
    if lifecycle == "COMPLETE":
        return {
            **base,
            "action": "complete",
            "human_gate_pending": False,
            "reason": "SPQ lifecycle is terminal",
        }
    return {**base, "action": "not_in_execution", "human_gate_pending": False}


def tool_begin_lifecycle_dispatch(arguments: dict[str, Any]) -> dict[str, Any]:
    """Authorize a Checkpoint/Acceptance agent selected by lifecycle next_action."""
    project = _project(arguments)
    blocked = _require_execution_ready(project) or _require_spq(project)
    if blocked:
        return {**blocked, "authorized": False}
    requested_role = advance_kernel.role_abbrev(str(arguments.get("role") or ""))
    requested_story = str(arguments.get("story_id") or "")
    from spec_state import state_transaction  # type: ignore

    with state_transaction(str(project)):
        selected = _spq_lifecycle_next_action(project)
        role = str(selected.get("role") or "")
        if (
            not selected.get("action", "").startswith("dispatch_")
            or role != requested_role
            or selected.get("story_id") != requested_story
        ):
            return {
                "authorized": False,
                "error": "lifecycle dispatch does not match deterministic next_action",
                "next_action": selected,
            }
        module = importlib.import_module("spq_state_machine")
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
            archive = archive_dir / f"{path.stem}.failed-{digest}.json"
            suffix = 1
            while archive.exists():
                archive = archive_dir / f"{path.stem}.failed-{digest}-{suffix}.json"
                suffix += 1
            shutil.move(str(path), str(archive))
            archived = str(archive)
        dispatch_id = secrets.token_hex(16)
        key = f"{requested_story}:{role}"
        state.setdefault("lifecycle_dispatches", {})[key] = {
            "dispatch_id": dispatch_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "host": "codex",
        }
        module._write_state(str(project), state)
    full_role = ROLE_NAMES[role]
    receipt_contract = {
        "backend": "codex",
        "dispatch_id": dispatch_id,
        "required_fields": [
            "story_id", "role", "backend", "model", "dispatch_id",
            "artifacts", "verification_commands", "metrics", "completed_at",
        ],
        "token_usage_source": (
            "Synaptory patches host-observed Codex transcript usage at SubagentStop "
            "or receipt validation; do not guess"
        ),
    }
    stage = str(selected.get("lifecycle_state") or state.get("lifecycle_state") or "")
    role_output = {
        "quality-engineer": "release verification receipt with executed test evidence",
        "compliance-engineer": "compliance receipt with severity-counted findings",
        "platform-engineer": "operational readiness receipt with executed environment checks",
        "technical-writer": "evidence-based lifecycle report inside the governed receipt",
        "code-reviewer": "read-only release review receipt with an explicit verdict",
    }
    role_audience = {
        "quality-engineer": "human release approver",
        "compliance-engineer": "human compliance and release approver",
        "platform-engineer": "human operator and release approver",
        "technical-writer": (
            "human SPQ Checkpoint decision-maker"
            if stage == "CHECKPOINT"
            else "release consumers, operators, and the human release approver"
        ),
        "code-reviewer": "human release approver",
    }
    work_units = [
        {
            key: item.get(key)
            for key in ("id", "title", "state", "acceptance_criteria", "files")
            if item.get(key) is not None
        }
        for item in state.get("current_stories") or []
        if isinstance(item, dict)
    ]
    receipt_relative = str(path.relative_to(project))
    execution_envelope = {
        "story_id": requested_story,
        "cycle": int(state.get("current_cycle") or 0),
        "cycle_goal": str(state.get("cycle_goal") or ""),
        "current_stage": stage,
        "action": selected["action"],
        "role": full_role,
        "allowed_paths": {
            "read": ["."],
            "write": [receipt_relative],
        },
        "audience": role_audience[full_role],
        "required_output": role_output[full_role],
        "required_verification": (
            "Inspect the governed Cycle evidence and execute repository checks "
            "relevant to this role; record every command with command, integer "
            "exit_code, and summary. Do not claim unavailable infrastructure."
        ),
        "applicable_policy": (
            "AGENTS.md, .synaptory.yaml, repository instructions, and the "
            "dispatch-bound Synaptory receipt contract"
        ),
        "work_units": work_units,
        "receipt_path": str(path),
        "receipt_contract": receipt_contract,
    }
    return {
        "authorized": True,
        "host": "codex",
        "action": selected["action"],
        "agent_profile": f"synaptory-{full_role}",
        "role": full_role,
        "story_id": requested_story,
        "receipt_path": str(path),
        "receipt_contract": receipt_contract,
        "execution_envelope": execution_envelope,
        "archived_receipt": archived,
        "next_action": selected,
    }


def _story_pipeline_mutation(
    project: Path, operation: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """`record_retry` and `set_dod_tier` — the two ceremony mutations that live
    on `story_pipeline` rather than `spq_state_machine`.

    Both take the state dict and leave persistence to the caller, so this
    reads, mutates and writes through the same guarded state helpers the rest
    of the lifecycle uses. Doing it here rather than telling the operator to
    shell `story_pipeline.py` is the whole point: on Codex a prompt that names
    a shell mutation is a prompt that cannot be followed.
    """
    pipeline = importlib.import_module("story_pipeline")
    spq = importlib.import_module("spq_state_machine")
    state = spq.read_state(str(project))

    if operation == "record_retry":
        story_id = str(arguments.get("story_id") or "").strip()
        role = str(arguments.get("role") or "").strip()
        if not story_id or not role:
            return {"error": "story_id and role are required for record_retry"}
        count = pipeline.record_retry(
            state,
            story_id,
            role,
            str(arguments.get("summary") or ""),
            project_dir=str(project),
        )
        spq._write_state(str(project), state)
        return {"ok": True, "story_id": story_id, "role": role, "retries": count}

    tier = str(arguments.get("tier") or "").strip()
    decided_by = str(arguments.get("decided_by") or "").strip()
    if not tier:
        return {"error": "tier is required for set_dod_tier"}
    # The tier is a PLANNING DECISION with an author (#134 GAP-11). Accepting it
    # unattributed would leave `tier_source: "planned"` with nobody who planned
    # it, which reads as computed-but-lying rather than as a recorded decision.
    if not decided_by:
        return {"error": "decided_by is required to set the DoD tier"}
    try:
        result = pipeline.set_dod_tier(
            state,
            tier,
            decided_by,
            str(arguments.get("reason") or ""),
            project_dir=str(project),
        )
    except (ValueError, KeyError) as exc:
        return {"error": f"set_dod_tier refused: {exc}"}
    spq._write_state(str(project), state)
    return result


def tool_spq_lifecycle(arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute one explicit SPQ ceremony operation through guarded APIs."""
    project = _project(arguments)
    operation = str(arguments.get("operation") or "")
    module = importlib.import_module("spq_state_machine")
    if operation == "initialize":
        blocked = _require_authenticated(project) or _require_standard_project(project)
        if blocked:
            return blocked
        if _state_path(project).exists():
            return {"error": "initialize refused: pipeline state already exists"}
        return module.initialize(
            str(project),
            agent_backends=arguments.get("agent_backends") or {
                "default": "codex", "roles": {}
            },
            spec_id=arguments.get("spec_id"),
            workstream_id=arguments.get("workstream"),
        )

    # A fresh workstream clone deliberately has no gitignored pipeline state.
    # ``hydrate_cycle`` is its guarded state-creation path, so requiring SPQ
    # state/readiness before calling it makes the documented operation
    # impossible.  Authenticate and verify the explicit repository config here;
    # after hydration, normal doctor/readiness checks apply to every dispatch.
    if operation == "hydrate_cycle" and not _state_path(project).is_file():
        blocked = _require_authenticated(project) or _require_standard_project(project)
        if blocked:
            return blocked
        if _configured_build_mode(project) != "spq":
            return {"error": "SPQ lifecycle tools require build_mode: spq"}
        work_units = arguments.get("work_units")
        if not isinstance(work_units, list) or not work_units:
            return {"error": "work_units must be a non-empty array"}
        cycle = arguments.get("cycle")
        if not isinstance(cycle, int) or isinstance(cycle, bool):
            return {"error": "cycle is required for hydrate_cycle and must be an integer"}
        module.initialize(
            str(project),
            agent_backends=arguments.get("agent_backends") or {
                "default": "codex", "roles": {}
            },
            workstream_id=arguments.get("workstream"),
        )
        return module.hydrate_cycle(
            str(project), cycle, work_units, goal=str(arguments.get("goal") or ""),
            tracker_cycle=arguments.get("tracker_cycle"),
            workstream_id=arguments.get("workstream"),
        )

    # A RELEASE clone owns no child Cycle and dispatches no Work Unit, so
    # `_require_execution_ready` -- which checks the nine managed agent profiles
    # among other things -- is the wrong gate: it demands provisioning for
    # dispatches that will never happen. Observed with a real Codex agent, which
    # had to install nine profiles into a clone whose only job is to pin child
    # increments.
    #
    # Same treatment as `initialize` and the fresh-clone `hydrate_cycle` above,
    # and for the same stated reason: these CREATE state in a clone that has
    # none. Authentication and the compliance refusal both still apply, so a
    # regulated project is still refused.
    if operation in (
        "open_coordination_cycle",
        "revise_coordination_manifest",
        "clear_release",
    ):
        blocked = _require_authenticated(project) or _require_standard_project(
            project
        )
        if blocked:
            return blocked

    if operation == "open_coordination_cycle":
        children = arguments.get("children")
        if not isinstance(children, list) or not children:
            return {"error": "children must be a non-empty array"}
        try:
            return module.open_coordination_cycle(
                str(project),
                release_goal=str(arguments.get("release_goal") or ""),
                children=children,
                dependency_edges=arguments.get("dependency_edges") or [],
                coordination_seq=arguments.get("coordination_seq"),
                baseline_sha=str(arguments.get("baseline_sha") or ""),
                created_by=str(arguments.get("created_by") or ""),
            )
        except Exception as exc:  # noqa: BLE001 - a tool refuses, never raises
            return {"error": str(exc)}
    if operation == "revise_coordination_manifest":
        try:
            return module.revise_coordination_manifest(
                str(project),
                coordination_cycle_id=arguments.get("coordination_cycle_id"),
                drop_child=str(arguments.get("drop_child") or ""),
                reason=str(arguments.get("reason") or ""),
                revised_by=str(arguments.get("revised_by") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
    if operation == "clear_release":
        try:
            return module.clear_release(
                str(project),
                coordination_cycle_id=arguments.get("coordination_cycle_id"),
                cleared_by=arguments.get("cleared_by"),
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    blocked = _require_standard_project(project) or _require_spq(project)
    if blocked:
        return blocked
    if operation in {"sync_status", "acceptance_status"}:
        state = module.read_state(str(project))
        if operation == "sync_status":
            from sync_barrier import status  # type: ignore

            cycle = int(arguments.get("cycle") or state.get("current_cycle") or 0)
            return status(str(project), cycle)
        return module.acceptance_readiness(str(project), state=state)

    blocked = _require_execution_ready(project)
    if blocked:
        return blocked
    if operation == "approve_baseline":
        approved_by = str(arguments.get("approved_by") or "").strip()
        if not approved_by:
            return {"error": "approved_by is required for the Discovery human gate"}
        return module.approve_baseline(str(project), approved_by=approved_by)
    if operation in {"open_cycle", "hydrate_cycle"}:
        work_units = arguments.get("work_units")
        if not isinstance(work_units, list) or not work_units:
            return {"error": "work_units must be a non-empty array"}
        cycle = arguments.get("cycle")
        if cycle is not None and (not isinstance(cycle, int) or isinstance(cycle, bool)):
            return {"error": "cycle must be an integer"}
        if operation == "open_cycle":
            return module.open_cycle(
                str(project), cycle, str(arguments.get("goal") or ""), work_units,
                tracker_cycle=arguments.get("tracker_cycle"),
            )
        if cycle is None:
            return {"error": "cycle is required for hydrate_cycle"}
        return module.hydrate_cycle(
            str(project), cycle, work_units, goal=str(arguments.get("goal") or ""),
            tracker_cycle=arguments.get("tracker_cycle"),
            workstream_id=arguments.get("workstream"),
        )
    if operation == "enter_sync":
        approved_by = str(arguments.get("approved_by") or "").strip()
        if not approved_by:
            return {
                "error": (
                    "approved_by is required to confirm the workstream head and "
                    "readiness record were committed and pushed"
                )
            }
        result = module.transition(str(project), "SYNC")
        result.pop("sync_evaluation", None)
        result.setdefault("process_log", []).append({
            "at": datetime.now(timezone.utc).isoformat(),
            "event": "sync_entry_approved",
            "approved_by": approved_by,
        })
        module._write_state(str(project), result)
        return result
    if operation == "declare_ready":
        return module.declare_sync_ready(
            str(project), arguments.get("cycle"),
            workstream=arguments.get("workstream"),
            declared_by=arguments.get("approved_by"),
            run_regression=arguments.get("run_regression", True),
        )
    if operation == "evaluate_sync":
        return module.evaluate_sync(
            str(project), arguments.get("cycle"),
            use_cache=arguments.get("use_cache", True),
        )
    if operation == "clear_sync":
        approved_by = str(arguments.get("approved_by") or "").strip()
        if not approved_by:
            return {"error": "approved_by is required to clear the Sync human gate"}
        return module.clear_sync(
            str(project), arguments.get("cycle"), cleared_by=approved_by
        )
    if operation == "record_method_signal":
        return module.record_method_signal(
            str(project), str(arguments.get("kind") or ""),
            str(arguments.get("summary") or ""), arguments.get("data"),
        )
    if operation == "close_cycle":
        readiness = module.checkpoint_readiness(str(project))
        if readiness.get("ready"):
            receipt = _validate_and_deliver_receipt(
                project,
                _scoped_receipt_path(project, str(readiness["receipt_path"])),
            )
            if not receipt.get("valid"):
                return {
                    "error": "close_cycle refused: Checkpoint receipt delivery failed",
                    "receipt": receipt,
                }
        proceed_to = str(arguments.get("proceed_to") or "COMMIT").upper()
        return module.close_cycle(str(project), proceed_to=proceed_to)
    if operation == "complete":
        approved_by = str(arguments.get("approved_by") or "").strip()
        if not approved_by:
            return {"error": "approved_by is required for release approval"}
        readiness = module.acceptance_readiness(str(project))
        if readiness.get("ready"):
            deliveries: dict[str, Any] = {}
            for abbreviation, item in readiness.get("receipts", {}).items():
                path = _scoped_receipt_path(
                    project, str(item.get("receipt_path") or "")
                )
                deliveries[str(abbreviation)] = _validate_and_deliver_receipt(
                    project, path
                )
            failed = {
                role: result
                for role, result in deliveries.items()
                if not result.get("valid")
            }
            if failed:
                return {
                    "error": "complete refused: Acceptance receipt delivery failed",
                    "receipts": failed,
                }
        return module.transition(str(project), "COMPLETE", approved_by=approved_by)
    # ── Per-Work-Unit ceremony mutations (#303 host parity) ──────────────────
    # These exist because the SPQ ceremony prompts INSTRUCT them, and the Codex
    # ceremony composer fails the build on any prompt mutation with no MCP
    # equivalent. Before this, Acceptance and DoD-recovery were reachable on
    # Claude and Cursor but not on Codex, so composing the prompts would have
    # told a Codex operator to shell a script the host forbids.
    if operation == "request_acceptance":
        story_id = str(arguments.get("story_id") or "").strip()
        if not story_id:
            return {"error": "story_id is required for request_acceptance"}
        return module.request_acceptance(str(project), story_id)
    if operation == "evaluate_dod":
        story_id = str(arguments.get("story_id") or "").strip()
        if not story_id:
            return {"error": "story_id is required for evaluate_dod"}
        return module.evaluate_story_dod(str(project), story_id)
    if operation == "accept_story":
        story_id = str(arguments.get("story_id") or "").strip()
        accepted_by = str(arguments.get("accepted_by") or "").strip()
        if not story_id:
            return {"error": "story_id is required for accept_story"}
        # Acceptance is a HUMAN gate. An empty attributor would record the
        # sign-off as having no author, which is the one thing an acceptance
        # record exists to carry.
        if not accepted_by:
            return {"error": "accepted_by is required to accept a Work Unit"}
        return module.accept_story(str(project), story_id, accepted_by)
    if operation == "reject_story":
        story_id = str(arguments.get("story_id") or "").strip()
        rejected_by = str(arguments.get("rejected_by") or "").strip()
        if not story_id:
            return {"error": "story_id is required for reject_story"}
        if not rejected_by:
            return {"error": "rejected_by is required to reject a Work Unit"}
        raw_changes = arguments.get("ac_changes") or []
        if not isinstance(raw_changes, list):
            return {"error": "ac_changes must be an array of strings"}
        return module.reject_story(
            str(project),
            story_id,
            str(arguments.get("reason") or "needs-fix"),
            str(arguments.get("feedback") or ""),
            rejected_by,
            ac_changes=[str(c) for c in raw_changes],
        )
    if operation == "unblock_story":
        story_id = str(arguments.get("story_id") or "").strip()
        if not story_id:
            return {"error": "story_id is required for unblock_story"}
        return module.unblock_story(
            str(project), story_id, str(arguments.get("reason") or "")
        )
    if operation in {"record_retry", "set_dod_tier"}:
        return _story_pipeline_mutation(project, operation, arguments)
    return {"error": f"unsupported SPQ lifecycle operation: {operation}"}


def tool_bootstrap_project(arguments: dict[str, Any]) -> dict[str, Any]:
    project = _project(arguments)
    apply = arguments.get("apply", False)
    if not isinstance(apply, bool):
        raise ValueError("apply must be a boolean")
    if apply:
        blocked = _require_authenticated(project)
        if blocked:
            return {**blocked, "applied": False, "written": []}
        blocked = _require_standard_project(project)
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
    return _validate_and_deliver_receipt(project, path)


def _validate_and_deliver_receipt(project: Path, path: Path) -> dict[str, Any]:
    """Attribute, validate, and durably hand off one exact Codex receipt."""

    receipt = _read_receipt(path)
    attribution: dict[str, Any] | None = None
    if receipt.get("backend") == "codex":
        dispatch_id = str(receipt.get("dispatch_id") or "")
        attribution = attribute_receipt_from_dispatch(
            project, path, _dispatch_started_at(project, dispatch_id)
        )
    from receipt_validator import validate_receipt  # type: ignore

    result = validate_receipt(str(path), str(project))
    response = {**result.to_dict(), "receipt_path": str(path)}
    if attribution is not None:
        response["codex_attribution"] = attribution
    if result.valid:
        delivery = ship_validated_receipt(project, path)
        response["control_plane_delivery"] = delivery
        if delivery.get("handed_off") is not True:
            response["locally_valid"] = True
            response["valid"] = False
            errors = list(response.get("errors") or [])
            errors.append(
                "control-plane receipt delivery failed: "
                + str(delivery.get("error") or "durable handoff unavailable")
            )
            response["errors"] = errors
    return response


def _dispatch_started_at(project: Path, dispatch_id: str) -> str | None:
    """Resolve one dispatch start time without trusting receipt-supplied scope."""

    if not dispatch_id:
        return None
    selected, _active_spec = _select_state(_read_state(project))
    dispatches: list[Any] = list((selected.get("lifecycle_dispatches") or {}).values())
    for story in selected.get("current_stories") or []:
        if isinstance(story, dict):
            dispatches.extend((story.get("mcp_active_dispatches") or {}).values())
    matches = [
        item
        for item in dispatches
        if isinstance(item, dict) and item.get("dispatch_id") == dispatch_id
    ]
    if len(matches) != 1:
        return None
    value = matches[0].get("started_at")
    return value if isinstance(value, str) else None


def tool_begin_dispatch(arguments: dict[str, Any]) -> dict[str, Any]:
    """Authorize a dispatch the deterministic next_action actually selected."""

    project = _project(arguments)
    story_id = str(arguments.get("story_id") or "")
    requested_role = arguments.get("role")

    decision = advance_kernel.execute_dispatch(
        str(project),
        story_id,
        role=str(requested_role) if requested_role else None,
        policy=_kernel_policy(),
    )
    if not decision.allowed:
        payload: dict[str, Any] = {
            "authorized": False,
            "error": _dispatch_error_text(decision),
            "code": decision.code,
        }
        if decision.next_action is not None:
            payload["next_action"] = decision.next_action
        for key, value in decision.extra.items():
            payload.setdefault(key, value)
        return payload

    selected = decision.extra.get("dispatch_action") or decision.next_action or {}
    contract = decision.extra.get("receipt_contract", {})
    return {
        "authorized": True,
        "host": "codex",
        "action": str(selected.get("action") or ""),
        "agent_profile": "synaptory-%s" % decision.role,
        "role": decision.role,
        "story": decision.story,
        "receipt_path": contract.get("receipt_path"),
        "receipt_contract": {
            "backend": "codex",
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
                "Synaptory patches host-observed Codex transcript usage at SubagentStop "
                "or receipt validation; do not guess"
            ),
        },
        "next_action": selected,
        "recovery": selected.get("recovery"),
        "archived_receipt": decision.extra.get("archived_receipt"),
        "retry_count": decision.extra.get("retry_count"),
    }


def _dispatch_error_text(decision: Any) -> str:
    if decision.code in (advance_kernel.POLICY_REFUSED, advance_kernel.NOT_READY):
        return decision.reason
    if decision.code == advance_kernel.RECEIPT_ALREADY_PRESENT:
        return "dispatch refused: a fresh receipt exists; call advance instead"
    if decision.code == advance_kernel.DISPATCH_NOT_ELIGIBLE:
        return "dispatch refused: %s" % decision.reason
    return "dispatch refused: %s" % decision.reason


def tool_advance(arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate evidence and apply only the transition selected by next_action.

    Thin adapter: every check lives in the shared kernel so Codex, Cursor and
    Claude enforce the same contract. Codex-specific posture (regulated refusal,
    readiness probe, `backend == "codex"`) is passed as policy, not forked.
    """

    project = _project(arguments)
    story_id = str(arguments.get("story_id") or "")
    requested_state = str(arguments.get("to_state") or "")
    given = arguments.get("receipt_path")

    decision = advance_kernel.execute_advance(
        str(project),
        story_id,
        requested_state,
        receipt_path=str(given) if given else None,
        reason=arguments.get("reason"),
        policy=_kernel_policy(),
    )
    return _advance_response(decision)


def _kernel_policy() -> Any:
    """Codex host policy for the shared kernel."""
    return advance_kernel.policy_for_codex(
        policy_gate=lambda project_dir: _require_standard_project(Path(project_dir)),
        readiness_gate=lambda project_dir: _require_execution_ready(Path(project_dir)),
        receipt_delivery_gate=lambda project_dir, receipt_path: ship_validated_receipt(
            Path(project_dir), Path(receipt_path)
        ),
    )


def _advance_response(decision: Any) -> dict[str, Any]:
    """Map a kernel Decision onto this server's wire shape.

    Structure is shared (mcp_transport) so a refusal cannot carry a `code` on
    one host and omit it on the other; only the prose is host-specific.
    """
    return mcp_transport.advance_response(decision, _advance_error_text)


def _advance_error_text(decision: Any) -> str:
    """Preserve the legacy `advance refused: ...` prose the tools expect."""
    if decision.code in (advance_kernel.POLICY_REFUSED, advance_kernel.NOT_READY):
        return decision.reason
    return "advance refused: %s" % decision.reason


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


# The #303 manifest and ledger surface, shared with Cursor via `spq_mcp` so the
# two hosts cannot drift. Deliberately does NOT include the ceremony operations
# -- `spq_lifecycle` below already owns those, and two paths to one operation
# means two gates to keep in step and a model free to pick whichever refuses
# less. Every mutation here inherits both Codex gates.
def _spq_tools() -> dict[str, dict[str, Any]]:
    try:
        import spq_mcp
    except ImportError:  # pragma: no cover - shared runtime always ships it
        return {}
    return spq_mcp.tools(
        policy_gate=lambda project: _require_standard_project(Path(project)),
        readiness_gate=lambda project: _require_execution_ready(Path(project)),
    )


TOOLS: dict[str, dict[str, Any]] = {
    "doctor": {
        "fn": tool_doctor,
        "description": "Check native Codex execution readiness for a Synaptory project.",
        "schema": mcp_transport.PROJECT_ONLY_SCHEMA,
    },
    "get_status": {
        "fn": tool_get_status,
        "description": "Read a redacted summary of Synaptory pipeline state.",
        "schema": mcp_transport.PROJECT_ONLY_SCHEMA,
    },
    "get_state": {
        "fn": tool_get_state,
        "description": "Read the active lifecycle state required to build an execution envelope.",
        "schema": mcp_transport.PROJECT_ONLY_SCHEMA,
    },
    "next_action": {
        "fn": tool_next_action,
        "description": "Return the deterministic next dispatch, transition, or human gate.",
        "schema": mcp_transport.PROJECT_ONLY_SCHEMA,
    },
    "spq_lifecycle": {
        "fn": tool_spq_lifecycle,
        "description": (
            "Run one guarded SPQ ceremony operation across Discovery, Commit, "
            "Sync, Checkpoint, Acceptance, or Complete."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string"},
                "operation": {
                    "type": "string",
                    "enum": [
                        "initialize", "approve_baseline", "open_cycle",
                        "hydrate_cycle", "enter_sync", "declare_ready",
                        "sync_status", "evaluate_sync", "clear_sync",
                        "record_method_signal", "close_cycle",
                        "acceptance_status", "complete",
                        # Per-Work-Unit ceremony mutations. Present because the
                        # composed SPQ prompts instruct them and the ceremony
                        # composer fails the build on an unmapped mutation.
                        "request_acceptance", "evaluate_dod", "accept_story",
                        "reject_story", "unblock_story", "record_retry",
                        "set_dod_tier",
                        # Coordination Cycle mutations (#305). A release is not
                        # a lifecycle state, but opening, revising and clearing
                        # one are ceremony acts, gated exactly like opening a
                        # Cycle. The READS live on the shared `spq_*` tools.
                        "open_coordination_cycle",
                        "revise_coordination_manifest",
                        "clear_release",
                    ],
                },
                "approved_by": {"type": "string"},
                "spec_id": {"type": "string"},
                "release_goal": {"type": "string"},
                "children": {"type": "array", "items": {"type": "object"}},
                "dependency_edges": {"type": "array", "items": {"type": "object"}},
                "coordination_seq": {"type": "integer"},
                "coordination_cycle_id": {"type": "string"},
                "baseline_sha": {"type": "string"},
                "created_by": {"type": "string"},
                "drop_child": {"type": "string"},
                "revised_by": {"type": "string"},
                "cleared_by": {"type": "string"},
                "agent_backends": {"type": "object"},
                "cycle": {"type": "integer", "minimum": 1},
                "tracker_cycle": {"type": "integer"},
                "goal": {"type": "string"},
                "work_units": {"type": "array", "items": {"type": "object"}},
                "workstream": {"type": "string"},
                "run_regression": {"type": "boolean", "default": True},
                "use_cache": {"type": "boolean", "default": True},
                "kind": {"type": "string"},
                "summary": {"type": "string"},
                "data": {"type": "object"},
                "proceed_to": {"type": "string", "enum": ["COMMIT", "ACCEPTANCE"]},
                "story_id": {"type": "string"},
                "accepted_by": {"type": "string"},
                "rejected_by": {"type": "string"},
                "reason": {"type": "string"},
                "feedback": {"type": "string"},
                "ac_changes": {"type": "array", "items": {"type": "string"}},
                "role": {"type": "string"},
                "tier": {"type": "string"},
                "decided_by": {"type": "string"},
            },
            "required": ["operation"],
        },
    },
    "begin_lifecycle_dispatch": {
        "fn": tool_begin_lifecycle_dispatch,
        "description": (
            "Authorize the Checkpoint or Acceptance role selected by next_action "
            "and issue a receipt-bound Codex dispatch contract."
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
        "schema": mcp_transport.VALIDATE_RECEIPT_SCHEMA,
    },
    "advance": {
        "fn": tool_advance,
        "description": "Fail-closed receipt gate: apply only the transition returned by next_action.",
        "schema": mcp_transport.ADVANCE_SCHEMA,
    },
    "tracker_update_status": {
        "fn": tool_tracker_update_status,
        "description": "Update a tracker item through the shared Synaptory adapter.",
        "schema": mcp_transport.TRACKER_UPDATE_SCHEMA,
    },
    "tracker_get_backlog": {
        "fn": tool_tracker_get_backlog,
        "description": "Read the configured tracker backlog.",
        "schema": mcp_transport.PROJECT_ONLY_SCHEMA,
    },
    "tracker_health_check": {
        "fn": tool_tracker_health_check,
        "description": "Check the configured tracker adapter.",
        "schema": mcp_transport.PROJECT_ONLY_SCHEMA,
    },
}

TOOLS.update(_spq_tools())


def handle_tool(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Invoke a Codex tool. Kept as a named function: the tests call it directly."""
    return mcp_transport.call_tool(TOOLS, name, arguments)


def main() -> int:
    # NDJSON is the Codex default and must stay so: header bytes corrupt the
    # stdio handshake before initialize completes. The framed compatibility
    # switch is honoured by the shared transport.
    return mcp_transport.serve(
        TOOLS,
        server_name="synaptory",
        server_version=SERVER_VERSION,
        default_framing=mcp_transport.NDJSON,
    )


if __name__ == "__main__":
    raise SystemExit(main())
