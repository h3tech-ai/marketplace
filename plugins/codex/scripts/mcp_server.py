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
import runtime_contracts  # noqa: E402
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
    if mode == "spq":
        # ONE CALL, not a dispatcher result patched afterwards. The two `elif`
        # branches this replaces keyed on `not_in_execution` and `await_sync`:
        # the first because the host filled in every ceremony stage itself, the
        # second because Sync was a state with a human gate. Neither exists --
        # `spq_state_machine.next_action` answers for all four stages, and
        # `await_sync` is gone. Keeping the sentinel-and-patch shape would have
        # left the enrichment reachable only when the kernel happened to say
        # `not_in_execution`, which under SPQ it no longer does.
        return {
            "build_mode": mode,
            "next_action": _spq_lifecycle_next_action(project),
        }
    return {"build_mode": mode, "next_action": _next_action(project)}


def _require_spq(project: Path) -> dict[str, Any] | None:
    if _build_mode(project) == "spq":
        return None
    return {"error": "SPQ lifecycle tools require build_mode: spq"}


def _spq_lifecycle_next_action(project: Path) -> dict[str, Any]:
    """Delegate to the shared contract. Kept as a named function on purpose.

    This host's copy and Cursor's `spq_next.lifecycle_next_action` were a
    line-for-line pair, each branching on seven lifecycle states, each calling
    a `checkpoint_readiness` the rewrite removed, and each restating a
    barrier-criteria count in prose -- this one said "five" while the barrier
    published seven. Three encodings of one machine is the mechanism that
    shipped three different criteria counts in one product (`#640`), so the
    decision lives once, in `spq_mcp.lifecycle_next_action`, and both hosts ask
    it.

    `_spq_sync_boundary_next_action` IS DELETED, not ported, and every part of
    it had lost its subject: it resolved a lane from `SYNAPTORY_ACTIVE_SPEC` --
    Multi-Spec identity, which SPQ ignores -- or from the current branch;
    counted a quorum of per-lane readiness records, where the barrier is now at
    Checkpoint over the admitted set and `C-04` refuses partial admission; and
    returned `await_sync` until the count was met, which was the human gate
    `C-02` removes. Porting it would have rebuilt that gate under the new
    stages.
    """
    return importlib.import_module("spq_mcp").lifecycle_next_action(str(project))


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
        if not selected.get("receipt_path"):
            return {
                "authorized": False,
                "error": (
                    "next_action selected %s but named no receipt path, so "
                    "there is nothing to bind this dispatch to"
                    % selected.get("action")
                ),
                "next_action": selected,
            }
        path = Path(str(selected["receipt_path"]))
        # `spq_paths.receipts_dir`, because `spq_state_machine` has no
        # `_resolve_receipts_dir`: the containment check that keeps a lifecycle
        # receipt inside the Cycle's receipts directory raised `AttributeError`
        # and every Checkpoint and Acceptance dispatch on this host failed at
        # the check meant to protect it. One derivation, and it is the one
        # `spq_mcp.lifecycle_next_action` builds the path from.
        spq_paths = importlib.import_module("spq_paths")
        receipts_root = Path(
            spq_paths.receipts_dir(str(project), str(state.get("_cycle_id") or ""))
        )
        receipts_root.mkdir(parents=True, exist_ok=True)
        receipts_root = receipts_root.resolve()
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
            # `stage == "CHECKPOINT"` used to select this, and CHECKPOINT is
            # not a stage: it is a recorded event inside `CYCLE`. So the
            # audience keys off the stage that exists -- a Technical Writer
            # dispatched during a Cycle is writing for the close decision, one
            # dispatched at ACCEPTANCE is writing for the go-live.
            "release consumers, operators, and the human release approver"
            if stage == "ACCEPTANCE"
            else "human SPQ Checkpoint decision-maker"
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
    """Every per-Work-Unit board mutation, through `story_pipeline`.

    Seven operations, and four of them arrived here because they were
    addressed to `spq_state_machine`, which has never had them:
    `request_acceptance`, `accept_story`, `reject_story` and `unblock_story`
    are lifecycle-NEUTRAL board mutations shared by all three lifecycles, and
    they live on `story_pipeline`. Under SPQ each was an `AttributeError`, so a
    Work Unit could be dispatched and verified on this host and then not
    accepted on it.

    `story_pipeline` takes the state dict and leaves persistence to its caller,
    so this reads, mutates and writes through the same guarded state helpers
    the rest of the lifecycle uses. Doing it here rather than telling the
    operator to shell `story_pipeline.py` is the whole point: on Codex a prompt
    that names a shell mutation is a prompt that cannot be followed.
    """
    pipeline = importlib.import_module("story_pipeline")
    spq = importlib.import_module("spq_state_machine")

    story_id = str(arguments.get("story_id") or "").strip()
    if operation != "set_dod_tier" and not story_id:
        return {"error": f"story_id is required for {operation}"}

    # `evaluate_dod` is the one READ here: it takes the project directory and
    # its own tier, writes nothing, and needs no state round-trip.
    if operation == "evaluate_dod":
        state = spq.read_state(str(project))
        tier_info = spq.resolve_dod_tier(str(project), state) or {}
        intensity = str(
            tier_info.get("tier") or tier_info.get("intensity") or "growing"
        )
        spq_paths = importlib.import_module("spq_paths")
        try:
            return pipeline.evaluate_story_dod(
                str(project),
                story_id,
                intensity,
                receipts_dir=spq_paths.receipts_dir(
                    str(project), str(state.get("_cycle_id") or "")
                ),
            )
        except (ValueError, KeyError) as exc:
            return {"error": f"evaluate_dod refused: {exc}"}

    state = spq.read_state(str(project))

    if operation == "request_acceptance":
        args: tuple = (story_id,)
        kwargs: dict[str, Any] = {}
    elif operation == "accept_story":
        accepted_by = str(arguments.get("accepted_by") or "").strip()
        # Acceptance is a HUMAN gate. An empty attributor would record the
        # sign-off as having no author, which is the one thing an acceptance
        # record exists to carry.
        if not accepted_by:
            return {"error": "accepted_by is required to accept a Work Unit"}
        args, kwargs = (story_id, accepted_by), {}
    elif operation == "reject_story":
        rejected_by = str(arguments.get("rejected_by") or "").strip()
        if not rejected_by:
            return {"error": "rejected_by is required to reject a Work Unit"}
        raw_changes = arguments.get("ac_changes") or []
        if not isinstance(raw_changes, list):
            return {"error": "ac_changes must be an array of strings"}
        args = (
            story_id,
            str(arguments.get("reason") or "needs-fix"),
            str(arguments.get("feedback") or ""),
            rejected_by,
        )
        kwargs = {"acceptance_criteria_change": [str(c) for c in raw_changes]}
    elif operation == "unblock_story":
        args, kwargs = (story_id, str(arguments.get("reason") or "")), {}
    elif operation == "record_retry":
        role = str(arguments.get("role") or "").strip()
        if not role:
            return {"error": "story_id and role are required for record_retry"}
        args = (story_id, role, str(arguments.get("summary") or ""))
        kwargs = {}
    else:  # set_dod_tier
        tier = str(arguments.get("tier") or "").strip()
        decided_by = str(arguments.get("decided_by") or "").strip()
        if not tier:
            return {"error": "tier is required for set_dod_tier"}
        # The tier is a PLANNING DECISION with an author (#134 GAP-11).
        # Accepting it unattributed would leave `tier_source: "planned"` with
        # nobody who planned it, which reads as computed-but-lying rather than
        # as a recorded decision.
        if not decided_by:
            return {"error": "decided_by is required to set the DoD tier"}
        args = (tier, decided_by, str(arguments.get("reason") or ""))
        kwargs = {}

    try:
        result = getattr(pipeline, operation)(
            state, *args, project_dir=str(project), **kwargs
        )
    except (ValueError, TypeError, KeyError) as exc:
        return {"error": f"{operation} refused: {exc}"}
    spq._write_state(str(project), state)
    if operation == "record_retry":
        return {
            "ok": True, "story_id": story_id,
            "role": str(arguments.get("role") or ""), "retries": result,
        }
    return result if isinstance(result, dict) else {"ok": True, "result": result}


def tool_spq_lifecycle(arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute one explicit SPQ ceremony operation through guarded APIs.

    SIX OPERATIONS WERE REMOVED from this verb and from its enum (ADR-035),
    and removing them from the ENUM is the half that matters: the enum is what
    a fresh Codex install advertises in `tools/list`, so an operation left
    there is a promise the package makes to whichever model reads it.

      declare_ready, evaluate_sync, clear_sync, enter_sync, sync_status
          Sync as a lifecycle STATE, with a quorum of per-lane readiness
          records behind it. `C-02` makes Sync a recorded event that blocks
          nothing and moves the all-or-nothing barrier to Checkpoint over the
          admitted set, so none of these had an implementation left to call --
          each resolved through `getattr(spq_state_machine, ...)` against a
          name the rewrite removed, and `sync_status` additionally imported the
          deleted `sync_barrier`. `record_sync` is what survives.
      the three release-composition verbs
          That layer existed only because integration was deferred; every
          Cycle integrates to the shared trunk at its own Checkpoint now, so
          there is no parent to gather anything.
      record_method_signal
          a free-text signal with no schema. `method_events` records exactly
          three kinds and refuses a fourth, because a fourth would be a stage
          in disguise.
    """
    project = _project(arguments)
    operation = str(arguments.get("operation") or "")
    module = importlib.import_module("spq_state_machine")
    if operation == "initialize":
        blocked = _require_authenticated(project) or _require_standard_project(project)
        if blocked:
            return blocked
        if _state_path(project).exists():
            return {"error": "initialize refused: pipeline state already exists"}
        # THE LANE ARGUMENT IS GONE from the list. A clone no longer needs to
        # know which lane it is, because there is one board per Cycle -- and
        # while `initialize` still swallows unknown kwargs, keeping the
        # argument would have let a caller believe it seeded an identity
        # nothing reads.
        return module.initialize(
            str(project),
            agent_backends=arguments.get("agent_backends") or {
                "default": "codex", "roles": {}
            },
            spec_id=arguments.get("spec_id"),
        )

    # A fresh clone deliberately has no gitignored pipeline state.
    # ``hydrate_cycle`` is its guarded state-creation path, so requiring SPQ
    # state/readiness before calling it makes the documented operation
    # impossible.  Authenticate and verify the explicit repository config here;
    # after hydration, normal doctor/readiness checks apply to every dispatch.
    if operation == "hydrate_cycle":
        cycle_id = str(arguments.get("cycle_id") or "").strip()
        if not cycle_id:
            return {"error": "cycle_id is required for hydrate_cycle"}
        fresh = not _state_path(project).is_file()
        if fresh:
            blocked = _require_authenticated(project) or _require_standard_project(
                project
            )
            if blocked:
                return blocked
            if _configured_build_mode(project) != "spq":
                return {"error": "SPQ lifecycle tools require build_mode: spq"}
            module.initialize(
                str(project),
                agent_backends=arguments.get("agent_backends") or {
                    "default": "codex", "roles": {}
                },
            )
        else:
            blocked = (
                _require_standard_project(project)
                or _require_spq(project)
                or _require_execution_ready(project)
            )
            if blocked:
                return blocked
        # THROUGH `spq_mcp`, which is where the body lives so that the
        # manifest-hash refusal exists on both hosts rather than on whichever
        # was edited last. The Cursor server calls the same function. This
        # host's own copy passed a sequence number, a filtered unit list, a
        # goal, a tracker cycle and a lane id to a signature that takes only
        # `cycle_id`, so every hydration was a `TypeError` reported as a
        # lifecycle error.
        return importlib.import_module("spq_mcp").hydrate_cycle({
            "project_dir": str(project),
            "cycle_id": cycle_id,
            "manifest_hash": arguments.get("manifest_hash"),
        })

    blocked = _require_standard_project(project) or _require_spq(project)
    if blocked:
        return blocked
    if operation == "acceptance_status":
        # NO `state=`. `acceptance_readiness` takes the project and reads the
        # board itself; the `state=` this host passed was a `TypeError` on
        # every call, so the one read that tells an operator what an Acceptance
        # still owes was the read that could not be made.
        return module.acceptance_readiness(str(project))

    blocked = _require_execution_ready(project)
    if blocked:
        return blocked
    if operation == "approve_baseline":
        approved_by = str(arguments.get("approved_by") or "").strip()
        baseline_ref = str(arguments.get("baseline_ref") or "").strip()
        if not approved_by:
            return {"error": "approved_by is required for the Discovery human gate"}
        if not baseline_ref:
            # An approval that does not name the revision it approved is a
            # boolean with a signature on it: the next Cycle seals against a
            # baseline nobody can point at.
            return {
                "error": (
                    "baseline_ref is required: the approval records the exact "
                    "revision whose scope, timeline and cost were approved"
                )
            }
        calibration = arguments.get("calibration")
        if calibration is not None and not isinstance(calibration, dict):
            return {"error": "calibration must be an object (the measured sample)"}
        return module.approve_baseline(
            str(project),
            approved_by=approved_by,
            baseline_ref=baseline_ref,
            calibration=calibration,
        )
    if operation == "open_cycle":
        # THE ARGUMENT LIST IS THE DECLARATION. It replaces `(cycle,
        # work_units, goal, tracker_cycle)`, whose four values map onto exactly
        # one of the things a Cycle commits to -- and `open_cycle` has not
        # taken a cycle NUMBER since the identity became a Cycle id.
        units = arguments.get("admitted_units")
        if not isinstance(units, list) or not units:
            return {"error": "admitted_units must be a non-empty array"}
        try:
            return module.open_cycle(
                str(project),
                goal=str(arguments.get("goal") or ""),
                repository=str(arguments.get("repository") or ""),
                trunk_ref=str(arguments.get("trunk_ref") or ""),
                source_region=list(arguments.get("source_region") or []),
                admitted_units=units,
                engineering_lead=str(arguments.get("engineering_lead") or ""),
                crew=list(arguments.get("crew") or []),
                shared_path_owners=list(arguments.get("shared_path_owners") or []),
                baseline_ref=str(arguments.get("baseline_ref") or ""),
                cycle_id=str(arguments.get("cycle_id") or "") or None,
                specification_refs=list(arguments.get("specification_refs") or []),
                readmits=list(arguments.get("readmits") or []),
                verification=(arguments.get("verification") or None),
            )
        except Exception as exc:  # noqa: BLE001 - a tool refuses, never raises
            return {"error": str(exc)}
    if operation == "record_sync":
        # Read the closed set from the constant rather than restating it. A
        # copy of a closed set is the same defect that shipped three different
        # barrier-criteria counts in one product.
        allowed = tuple(importlib.import_module("method_events").SYNC_RESOLUTIONS)
        resolution = str(arguments.get("resolution") or "").strip()
        if resolution not in allowed:
            return {
                "error": (
                    "resolution must be one of %s -- \"it cleared somehow\" is "
                    "not a record anyone can act on later." % ", ".join(allowed)
                )
            }
        try:
            return module.record_sync(
                str(project),
                waiting_unit_id=str(arguments.get("waiting_unit_id") or ""),
                producing_cycle_id=str(arguments.get("producing_cycle_id") or ""),
                resolution=resolution,
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
    if operation == "promote_cycle":
        # A PROMOTION IS SOMEBODY'S DECISION, so the principal is required and
        # the verdict is not accepted. `run_barrier` evaluates the criteria,
        # observes the trunk, and records the promotion the close then has to
        # find -- none of which a caller can supply.
        principal = str(arguments.get("principal") or "")
        if not principal:
            return {
                "error": (
                    "promote_cycle needs the principal promoting: a barrier "
                    "verdict nobody signed is not a decision (`M-03`)."
                )
            }
        try:
            return module.promote_cycle(
                str(project),
                principal=principal,
                rationale=str(arguments.get("rationale") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
    if operation == "close_cycle":
        # THE CLOSE TAKES NO VERDICT AND NO SHA. It derives both: `run_barrier`
        # evaluates the criteria against the unit results, executes the
        # regression, observes the trunk itself, and `close_cycle` then
        # requires a matching successful promotion to exist.
        #
        # This host used to validate a caller-supplied `barrier_verdict` and
        # pass a caller-supplied `integrated_sha`, which reads as a strong
        # gate: not green, no criteria and a foreign Cycle were all refused.
        # A shaped JSON object satisfying those three closed a Cycle whose
        # barrier had never run -- and the shape is trivially guessable from
        # the refusals themselves. Validating a claim is not the same as
        # deriving the fact, and `M-03` asks for the second.
        #
        # `proceed_to` is gone for the same reason: whether the next stage is
        # another Cycle or Acceptance is derived from outstanding commitments
        # and a recorded handover, not chosen here.
        for retired, replacement in (
            ("barrier_verdict", "it is derived by `run_barrier`"),
            ("integrated_sha", "the trunk is observed, not asserted"),
        ):
            if arguments.get(retired) is not None:
                return {
                    "error": (
                        "close_cycle no longer accepts %s: %s. Run the barrier, "
                        "promote with a principal, merge, then close."
                        % (retired, replacement)
                    )
                }
        # THE KERNEL DECIDES WHETHER A CLOSE IS EVEN THE NEXT STEP, and this
        # host asks rather than re-deriving. `next_action` returns
        # `close_cycle` exactly when every admitted Work Unit is done or cut,
        # and a `dispatch_*` while evidence is still owed -- so "is anything
        # outstanding" is one question with one answer, and asking it here
        # cannot disagree with the loop the operator is following.
        selected = _spq_lifecycle_next_action(project)
        if str(selected.get("action") or "") != "close_cycle":
            return {
                "error": (
                    "close_cycle refused: next_action is %s, so the Cycle has "
                    "outstanding work or evidence. %s"
                    % (selected.get("action"), selected.get("reason") or "")
                ).strip(),
                "next_action": selected,
            }
        try:
            return module.close_cycle(
                str(project),
                principal=str(arguments.get("principal") or ""),
                rationale=str(arguments.get("rationale") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
    if operation in ("enter_cycle", "enter_acceptance"):
        # THROUGH `transition`, which runs `check_transition`. Writing the
        # stage directly is the hole `open_cycle` had before #644: `COMPLETE`
        # was terminal only for callers that used this verb.
        target = "CYCLE" if operation == "enter_cycle" else "ACCEPTANCE"
        try:
            return module.transition(str(project), target)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
    if operation == "complete":
        approved_by = str(arguments.get("approved_by") or "").strip()
        if not approved_by:
            return {"error": "approved_by is required for release approval"}
        readiness = module.acceptance_readiness(str(project))
        if readiness.get("ready"):
            state = module.read_state(str(project))
            seq = int(state.get("current_cycle") or 0)
            spq_paths = importlib.import_module("spq_paths")
            receipts = spq_paths.receipts_dir(
                str(project), str(state.get("_cycle_id") or "")
            )
            deliveries: dict[str, Any] = {}
            for abbreviation in sorted(module.ACCEPTANCE_ROLES):
                path = _scoped_receipt_path(
                    project,
                    os.path.join(
                        receipts, "ACCEPTANCE-%d-%s.json" % (seq, abbreviation)
                    ),
                )
                deliveries[abbreviation] = _validate_and_deliver_receipt(project, path)
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
        # NO `approved_by=` ON THE TRANSITION. `cycle_lifecycle.check_transition`
        # takes a current stage and a target and nothing else; the attributor is
        # checked above, where it can actually refuse.
        return module.transition(str(project), "COMPLETE")
    # ── Per-Work-Unit board mutations (#303 host parity) ─────────────────────
    # These exist because the SPQ ceremony prompts INSTRUCT them, and the Codex
    # ceremony composer fails the build on any prompt mutation with no MCP
    # equivalent. Before this, Acceptance and DoD-recovery were reachable on
    # Claude and Cursor but not on Codex, so composing the prompts would have
    # told a Codex operator to shell a script the host forbids.
    #
    # ALL OF THEM NOW ROUTE THROUGH `story_pipeline`, where they live. Four
    # were addressed to `spq_state_machine`, which has never had them: they are
    # lifecycle-NEUTRAL board mutations shared by all three lifecycles, so
    # every one of `request_acceptance`, `accept_story`, `reject_story` and
    # `unblock_story` was an `AttributeError` on this host -- a Work Unit could
    # be dispatched and verified here and then not accepted here.
    if operation in {
        "request_acceptance", "evaluate_dod", "accept_story", "reject_story",
        "unblock_story", "record_retry", "set_dod_tier",
    }:
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
        # #690. The superseded receipt's full digest, so an archived file can
        # be tied back to the failed attempt without reading the directory.
        "archived_receipt_digest": decision.extra.get("archived_receipt_digest"),
        "retry_count": decision.extra.get("retry_count"),
        # The governed runtime surface (#396 finding 1). The kernel selects an
        # adapter profile and mints a dispatch envelope; a host that dropped
        # them left Auto/Prefer/Pin unable to affect a real dispatch and gave
        # the operator no way to see which runtime was chosen or to hand the
        # envelope to the bridge. Absent on a project that has not opted in,
        # which is what `runtime.state == "inert"` says.
        **_runtime_surface(decision),
    }


def _runtime_surface(decision: Any) -> dict[str, Any]:
    """The selection account and the dispatch envelope, when there is one.

    Kept out of `receipt_contract`: that block describes the EVIDENCE the agent
    must produce, while these describe the authority it runs under. Folding one
    into the other would make a host that reads only the receipt shape look
    like it had honoured the envelope.
    """
    account = decision.extra.get("runtime_selection")
    if not account:
        return {}
    surface: dict[str, Any] = {"runtime": account}
    envelope = decision.extra.get("dispatch_envelope")
    if envelope:
        surface["dispatch_envelope"] = envelope
        surface["runtime_execute_hint"] = (
            "Hand this envelope to `synaptory runtime execute --envelope -` "
            "rather than running the agent directly: the control plane signs "
            "it at registration and the bridge verifies that signature before "
            "it prepares anything."
        )
    return surface


def tool_execute_through_runtime(args: dict[str, Any]) -> dict[str, Any]:
    """Run a dispatch through the SELECTED runtime, not this host's own agent.

    Surfacing an envelope was not integration (#396): the host displayed the
    selection and then spawned its native agent regardless, so Auto/Prefer/Pin
    could be shown and could not change which executor ran. This is the
    deterministic boundary. It invokes the Runtime Bridge with the
    control-plane-signed envelope and returns the attempt record, so a
    cross-family dispatch is a tool call rather than a suggestion the model may
    or may not follow.

    The kernel enforces the other half: a receipt produced by a family the
    dispatch did not select is refused at advance, so skipping this tool does
    not quietly succeed.
    """
    envelope = args.get("dispatch_envelope")
    if not isinstance(envelope, dict) or not envelope:
        return {
            "executed": False,
            "error": (
                "no dispatch envelope: call begin_dispatch first and pass the "
                "`dispatch_envelope` it returned. A dispatch with no envelope "
                "is not a governed one and runs through the host's own agent."
            ),
        }
    cli = _runtime_cli()
    if not cli:
        return {
            "executed": False,
            "error": (
                "no synaptory CLI resolved for this installation, so the "
                "selected runtime cannot be invoked. The dispatch must not "
                "fall back to this host's agent: the selection chose "
                "%s." % (envelope.get("runtime_family") or "another runtime")
            ),
        }
    # THE REQUESTED PROJECT, explicitly (#396). The tool schema takes
    # `project_dir` and this ignored it, so the bridge walked upward from the
    # MCP SERVER's working directory: outside a project it refused, and inside
    # a different checkout it ran the selected runtime there and stored the
    # attempt there, while the envelope named the intended project.
    project = str(_project(args))
    proc = subprocess.run(
        [
            cli, "runtime", "execute",
            "--envelope", "-", "--json",
            "--project-dir", project,
        ],
        input=json.dumps(envelope),
        capture_output=True,
        text=True,
        check=False,
        cwd=project,
    )
    payload: dict[str, Any] = {
        "executed": proc.returncode == 0,
        "exit_code": proc.returncode,
        "runtime_family": envelope.get("runtime_family"),
        "adapter_profile_id": envelope.get("adapter_profile_id"),
    }
    try:
        payload["attempt"] = json.loads(proc.stdout)
    except (ValueError, TypeError):
        payload["stdout"] = proc.stdout[-4000:]
    if proc.returncode != 0:
        payload["error"] = (proc.stderr or proc.stdout)[-4000:]

    # RECONCILE THE BOARD with the generation the bridge actually held (#396).
    #
    # The kernel mints a LOCAL random fencing token at dispatch; the control
    # plane issues the authoritative one at claim. Without this the two never
    # meet, and a correctly claimed receipt is refused as `fencing_token_stale`
    # against the local value. This is the caller `reconcile_dispatch` was
    # written for, and the host is where it belongs: the Go bridge has no path
    # to the Python kernel, and the host has both.
    #
    # Best effort AFTER the run: a reconciliation failure must not discard an
    # attempt that already executed, and the reason is reported rather than
    # swallowed so an operator can see why the receipt will be refused.
    attempt = payload.get("attempt")
    if isinstance(attempt, dict):
        payload["reconciled"] = _reconcile_board(project, envelope, attempt)
    return payload


def _reconcile_board(
    project: str, envelope: dict[str, Any], attempt: dict[str, Any]
) -> dict[str, Any]:
    """Put the claim's generation and the run's outcome on the dispatch binding.

    ON THE RECORD'S OWN AUTHORITY, never on the envelope's. The first version
    read `fencing_token` and `state` from the record but `attempt_id`, story
    and role from the ENVELOPE, so whatever the bridge reported was relabelled
    as the requested attempt and the kernel's exact-attempt guard could never
    fire: envelope A in, a record for B out, and B's generation landed on A's
    binding. So the two documents are matched first, and the identity passed
    down is the REPORTED one, which leaves the kernel's guard as a second
    refusal rather than a formality (#396).
    """
    problems = runtime_contracts.attempt_record_disagreements(envelope, attempt)
    if problems:
        return {"applied": False, "reason": "; ".join(problems)}
    # ONLY A CP-CONFIRMED CLOSE IS PROJECTED. A holder that was fenced out or
    # asked to cancel can still finish its adapter and describe itself as
    # completed; adopting that puts a stale generation and a state the control
    # plane never accepted onto the board, and turns evidence that says stop
    # into a receipt that can advance.
    if not runtime_contracts.attempt_close_confirmed(attempt):
        return {
            "applied": False,
            "reason": (
                "the control plane did not confirm a terminal close for this "
                "attempt, so its generation and state are this process's own "
                "opinion and must not be projected onto the board"
            ),
        }
    token = str(attempt.get("fencing_token") or "")
    state = str(attempt.get("state") or "")
    if not token and not state:
        return {"applied": False, "reason": "the bridge reported no generation or state"}
    try:
        decision = advance_kernel.reconcile_dispatch(
            project,
            str(attempt.get("story_id") or ""),
            str(attempt.get("role") or ""),
            attempt_id=str(attempt.get("attempt_id") or ""),
            fencing_token=token,
            attempt_state=state,
            # #492. The record already carries WHY the attempt ended, from the
            # control plane's own close, and until this was forwarded the
            # board learned only THAT it ended. Passed through rather than
            # normalized here: the kernel owns the closed vocabulary, and a
            # host that quietly dropped a class it did not like would make a
            # human cancellation indistinguishable from a technical failure.
            failure_class=str(attempt.get("failure_class") or ""),
        )
    except Exception as exc:  # noqa: BLE001
        return {"applied": False, "reason": "reconciliation failed: %s" % exc}
    return {
        "applied": bool(decision.allowed),
        "reason": decision.reason,
        "code": decision.code,
    }


def _runtime_cli() -> str:
    """The synaptory binary this installation is stamped for, or "".

    Reuses the shared resolver rather than searching PATH: an unstamped tree
    addresses no control plane, and inventing one here would be the #320
    fall-through in a new place.
    """
    try:
        from gate_emitter import _resolve_cli  # type: ignore

        return _resolve_cli() or ""
    except Exception:  # noqa: BLE001
        return ""


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
    "execute_through_runtime": {
        "fn": tool_execute_through_runtime,
        "description": (
            "Run a governed dispatch through the SELECTED runtime via the "
            "Runtime Bridge. Required when begin_dispatch returned a "
            "dispatch_envelope: the selection decides which executor runs, and "
            "a receipt from another family is refused at advance."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string"},
                "dispatch_envelope": {"type": "object"},
            },
            "required": ["dispatch_envelope"],
        },
    },
    "spq_lifecycle": {
        "fn": tool_spq_lifecycle,
        "description": (
            "Run one guarded SPQ ceremony operation. The lifecycle has four "
            "stages -- DISCOVERY, CYCLE, ACCEPTANCE, COMPLETE -- and Commit, "
            "Sync and Checkpoint are recorded events inside them, not stages "
            "and not gates."
        ),
        # THE ENUM IS THE DISCOVERY SURFACE, which is why nine operations were
        # removed from it and not merely from the body above. It is what a
        # fresh Codex install advertises in `tools/list`, so an operation left
        # here is a promise the package makes to whichever model reads it --
        # and all nine resolved through `getattr(spq_state_machine, ...)`
        # against names the rewrite removed, or imported the deleted
        # `sync_barrier`. Gone: `enter_sync`, `declare_ready`, `sync_status`,
        # `evaluate_sync`, `clear_sync` (Sync as a state with a per-lane
        # quorum), `record_method_signal` (a free-text signal with no schema),
        # and the three release-composition verbs, whose layer existed only
        # because integration was deferred.
        "schema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string"},
                "operation": {
                    "type": "string",
                    "enum": [
                        "initialize", "approve_baseline", "open_cycle",
                        "hydrate_cycle", "record_sync",
                        # PROMOTE, THEN MERGE, THEN CLOSE. The close requires a
                        # recorded promotion, so a host that offers only
                        # `close_cycle` cannot reach a Checkpoint at all --
                        # every attempt refuses with "no successful promotion
                        # is recorded" and nothing in the enum can produce one.
                        "promote_cycle", "close_cycle",
                        "acceptance_status", "complete",
                        # THE TWO STAGE EDGES A CYCLE NEEDS TO REACH A GO-LIVE.
                        # The enum had `complete` and neither of these, so a
                        # Codex project could open Cycles and close them and
                        # never reach ACCEPTANCE at all -- `transition` was
                        # mapped to three operations and the host implemented
                        # one. `enter_cycle` also carries `ACCEPTANCE ->
                        # CYCLE`, which is what makes repeatable Acceptance
                        # (`C-15`) reachable on this host rather than only in
                        # the kernel.
                        "enter_cycle", "enter_acceptance",
                        # Per-Work-Unit board mutations. Present because the
                        # composed SPQ prompts instruct them and the ceremony
                        # composer fails the build on an unmapped mutation.
                        "request_acceptance", "evaluate_dod", "accept_story",
                        "reject_story", "unblock_story", "record_retry",
                        "set_dod_tier",
                    ],
                },
                "approved_by": {"type": "string"},
                "spec_id": {"type": "string"},
                "agent_backends": {"type": "object"},
                # The Cycle DECLARATION. `cycle` (a sequence number),
                # `tracker_cycle`, `work_units` and the lane id are gone: the
                # identity is a Cycle id, and the four of them together could
                # not express what a Cycle commits to.
                "cycle_id": {"type": "string"},
                "manifest_hash": {"type": "string"},
                "goal": {"type": "string"},
                "repository": {"type": "string"},
                "trunk_ref": {"type": "string"},
                "baseline_ref": {"type": "string"},
                "calibration": {"type": "object"},
                "source_region": {"type": "array", "items": {"type": "string"}},
                "admitted_units": {"type": "array", "items": {"type": "object"}},
                "engineering_lead": {"type": "string"},
                "crew": {"type": "array", "items": {"type": "string"}},
                "shared_path_owners": {
                    "type": "array", "items": {"type": "object"},
                },
                "specification_refs": {"type": "array", "items": {"type": "string"}},
                "readmits": {"type": "array", "items": {"type": "string"}},
                # HOW a condition stronger than `done` is checked, sealed with
                # what it checks. Without it this host can seal a declaration
                # naming a digest-verified edge and no script to recompute the
                # digest with -- which `cycle_records` refuses, so those edges
                # would be unusable here.
                "verification": {"type": "object"},
                # The Sync record, and the close. `proceed_to` is gone with the
                # close's `force`: whether the next stage is another Cycle or
                # Acceptance is derived from outstanding commitments and a
                # recorded handover, not chosen by the caller.
                "waiting_unit_id": {"type": "string"},
                "producing_cycle_id": {"type": "string"},
                "resolution": {"type": "string"},
                # NEITHER `integrated_sha` NOR `barrier_verdict` is here any
                # more. Both were caller-supplied facts about work the caller
                # had not necessarily done: the close derives the verdict from
                # `run_barrier`, observes the trunk itself, and requires a
                # recorded promotion. `principal` and `rationale` replace them
                # because a promotion is somebody's decision (`M-03`).
                "principal": {"type": "string"},
                "rationale": {"type": "string"},
                "summary": {"type": "string"},
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
