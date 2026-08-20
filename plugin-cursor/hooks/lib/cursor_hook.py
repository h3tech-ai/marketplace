"""Cursor hook I/O adapter.

Claude Copied scripts emit camelCase Claude hook JSON (via hook_io.py).
Cursor consumes snake_case (`additional_context`, `permission`,
`followup_message`, `continue`, `user_message`, `env`).

This module:
  * maps Cursor stdin → env the Claude scripts already understand
    (`CLAUDE_PLUGIN_ROOT`, `CLAUDE_PROJECT_DIR`, `CURSOR_PROJECT_DIR`)
  * runs those scripts or shared Python
  * emits Cursor JSON

subcommands map 1:1 to hooks.json entries. Prefer `type: command`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROSTER = frozenset(
    {
        "code-reviewer",
        "compliance-engineer",
        "platform-engineer",
        "project-owner",
        "quality-engineer",
        "research-advisor",
        "software-engineer",
        "solution-architect",
        "technical-writer",
    }
)

GIT_FORCE = re.compile(
    r"\bgit\s+(?:push\s+.*--force(?:-with-lease)?|--force|"
    r"reset\s+--hard|checkout\s+--force|clean\s+-[^\s]*f)",
    re.I,
)

SESSION_START_SCRIPTS = (
    "synaptory-access-token-check.sh",
    "synaptory-skills-fetch.sh",
    "synaptory-config-fetch.sh",
    "synaptory-load-rules.sh",
    "synaptory-session-start.sh",
    "session-guard.sh",
    "synaptory-otel-flush.sh",
)

SESSION_END_SCRIPTS = (
    "synaptory-session-end.sh",
    "synaptory-access-token-cleanup.sh",
    "synaptory-pipeline-snapshot.sh",
)

# Keep in sync with plugin/hooks/synaptory-inject-protocols.sh and seed.py.
REQUIRED_PROTOCOLS = (
    "receipt-protocol",
    "input-validation",
    "tool-efficiency",
    "freshness-protocol",
    "iron-laws",
    "verification-discipline",
    "socratic-gate",
    "anti-safe-harbor",
    "script-output-handling",
    "clean-code-self-check",
    "scope-challenge",
    "finding-memory",
    "tdd-discipline",
    "code-review-response",
    "subagent-isolation",
    "source-attribution",
    "open-decision-registry",
)
PROTOCOL_NAMES = REQUIRED_PROTOCOLS + (
    "ux-protocol",
    "visual-identity",
    "boundary-safety",
    "conflict-resolution",
    "coverage-ratchet",
    "ephemeral-environments",
)
# Every name we materialize is required for fail-closed cloud delivery.
REQUIRED_PROTOCOLS = PROTOCOL_NAMES


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_stdin() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _project_dir(data: dict[str, Any]) -> str:
    env = os.environ.get("CURSOR_PROJECT_DIR") or os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return env
    roots = data.get("workspace_roots") or data.get("workspaceRoots") or []
    if isinstance(roots, list) and roots:
        return str(roots[0])
    cwd = data.get("cwd") or data.get("project_dir")
    if cwd:
        return str(cwd)
    return os.getcwd()


def _export_env(data: dict[str, Any]) -> tuple[dict[str, Any], str, Path]:
    root = plugin_root()
    project = _project_dir(data)
    os.environ["PLUGIN_ROOT"] = str(root)
    os.environ["CLAUDE_PLUGIN_ROOT"] = str(root)
    os.environ["CURSOR_PROJECT_DIR"] = project
    os.environ["CLAUDE_PROJECT_DIR"] = project
    os.environ["SYNAPTORY_IDE"] = "cursor"
    if data.get("conversation_id"):
        os.environ.setdefault("CURSOR_CONVERSATION_ID", str(data["conversation_id"]))
    email = data.get("user_email") or os.environ.get("CURSOR_USER_EMAIL")
    if email:
        os.environ["CURSOR_USER_EMAIL"] = str(email)
    return data, project, root


def emit(payload: dict[str, Any] | None) -> None:
    if not payload:
        return
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _extract_json_objects(raw: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    idx = 0
    text = raw.strip()
    while idx < len(text):
        while idx < len(text) and text[idx] not in "{[":
            idx += 1
        if idx >= len(text):
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            idx += 1
            continue
        if isinstance(obj, dict):
            found.append(obj)
        idx = end
    return found


def translate_claude(obj: dict[str, Any], event: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "additionalContext" in obj and obj["additionalContext"]:
        out["additional_context"] = obj["additionalContext"]
    if obj.get("decision") == "block" and obj.get("reason"):
        if event in {"stop", "subagentStop"}:
            out["followup_message"] = obj["reason"]
        elif event == "beforeSubmitPrompt":
            out["continue"] = False
            out["user_message"] = obj["reason"]
    hso = obj.get("hookSpecificOutput") or {}
    perm = hso.get("permissionDecision")
    if perm:
        out["permission"] = "deny" if str(perm).lower() == "deny" else "allow"
        reason = hso.get("permissionDecisionReason")
        if reason:
            out["user_message"] = reason
    return out


def _run_script(root: Path, name: str, stdin: bytes, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    path = root / "hooks" / name
    merged = dict(os.environ)
    if env:
        merged.update(env)
    if not path.is_file():
        return subprocess.CompletedProcess(args=[name], returncode=0, stdout=b"", stderr=b"")
    return subprocess.run(
        ["bash", str(path)],
        input=stdin,
        env=merged,
        capture_output=True,
        check=False,
    )


def _sys_path_lib(root: Path) -> None:
    lib = str(root / "hooks" / "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)


def session_start(data: dict[str, Any], project: str, root: Path) -> dict[str, Any]:
    chunks: list[str] = []
    stdin = json.dumps(data).encode()
    auth_failed = False
    for name in SESSION_START_SCRIPTS:
        proc = _run_script(root, name, stdin)
        if name == "synaptory-access-token-check.sh" and proc.returncode != 0:
            auth_failed = True
        for obj in _extract_json_objects(proc.stdout.decode("utf-8", "replace")):
            ctx = obj.get("additionalContext")
            if ctx:
                chunks.append(str(ctx))
    if not _session_ok():
        auth_failed = True
    _materialize_protocols(project, root)
    # Cloud-VM restore is in the skill body; still inject pipeline state when
    # this hook does fire (IDE + private workers).
    state = Path(project) / ".synaptory" / ".orchestrator" / "pipeline-state.json"
    if state.is_file() and not any("pipeline-state" in c for c in chunks):
        try:
            preview = state.read_text(encoding="utf-8")[:4000]
            chunks.append("# Pipeline state (sessionStart)\n```json\n" + preview + "\n```")
        except OSError:
            pass
    env = {
        "PLUGIN_ROOT": str(root),
        "CLAUDE_PLUGIN_ROOT": str(root),
        "CLAUDE_PROJECT_DIR": project,
        "CURSOR_PROJECT_DIR": project,
        "SYNAPTORY_IDE": "cursor",
    }
    extra = os.environ.get("CURSOR_CONTROL_PLANE_URL") or os.environ.get(
        "CLAUDE_PLUGIN_OPTION_CONTROL_PLANE_URL"
    )
    # Plugin variables substitute into env as the schema name.
    var = os.environ.get("control_plane_url")
    if extra:
        env["SYNAPTORY_CONTROL_PLANE_URL"] = extra
    if var:
        env["SYNAPTORY_CONTROL_PLANE_URL"] = var
    out: dict[str, Any] = {"env": env}
    if auth_failed:
        chunks.insert(
            0,
            "🔒 BLOCKED — synaptory session missing or expired. "
            "Run `synaptory login`, then retry. beforeSubmitPrompt and "
            "subagentStart refuse work until `synaptory whoami --check` passes.",
        )
    if chunks:
        out["additional_context"] = "\n\n".join(chunks)
    return out


def _yaml_baa(project: str) -> bool:
    _sys_path_lib(plugin_root())
    try:
        from story_pipeline import healthcare_baa_enforced  # type: ignore
    except Exception:
        return False
    try:
        return bool(healthcare_baa_enforced(project))
    except Exception:
        return False


def _cli_bin() -> str | None:
    override = os.environ.get("SYNAPTORY_CLI_BIN") or ""
    if override.strip():
        path = Path(override)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    resolver = plugin_root() / "hooks" / "_resolve-cli.sh"
    if resolver.is_file():
        proc = subprocess.run(
            ["bash", str(resolver)],
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0:
            found = proc.stdout.decode("utf-8", "replace").strip()
            if found:
                return found
    if shutil_which("synaptory"):
        from shutil import which

        return which("synaptory")
    return None


def _cli_on_path() -> bool:
    return _cli_bin() is not None


def _session_ok() -> bool:
    """Fail-closed auth. Skip only when SYNAPTORY_AUTH_NO_GATE=1 (CI)."""
    if os.environ.get("SYNAPTORY_AUTH_NO_GATE") == "1":
        return True
    cli = _cli_bin()
    if not cli:
        return False
    proc = subprocess.run(
        [cli, "whoami", "--check"],
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def _auth_blocked_message() -> str:
    return (
        "synaptory: session missing or expired (`synaptory whoami --check` failed). "
        "Run `synaptory login` in a terminal, then retry. Delivery is refused "
        "while unauthenticated."
    )


_PROTOCOL_FETCH_BUDGET_S = 6.0
_PROTOCOL_FETCH_ONE_S = 4.0


def _protocol_disk_dirs(root: Path) -> list[Path]:
    return [
        root / "skills" / "_shared" / "protocols",
        root.parent / "plugin" / "skills" / "_shared" / "protocols",
    ]


def _read_protocol_disk(name: str, disk_dirs: list[Path]) -> str:
    for d in disk_dirs:
        path = d / f"{name}.md"
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
                return ""
    return ""


def _fetch_protocol_body(name: str, cli: str | None, disk_dirs: list[Path]) -> str:
    body = ""
    if cli:
        try:
            proc = subprocess.run(
                [cli, "skills", "get", f"protocols/{name}", "--host", "cursor"],
                capture_output=True,
                text=True,
                check=False,
                timeout=_PROTOCOL_FETCH_ONE_S,
            )
        except (OSError, subprocess.TimeoutExpired):
            proc = None
        if proc is not None and proc.returncode == 0 and proc.stdout.strip():
            body = proc.stdout
    if not body:
        body = _read_protocol_disk(name, disk_dirs)
    return body


def _materialize_protocols(project: str, root: Path) -> list[str]:
    """Write protocol bodies to `.synaptory/.protocols/<name>.md` in parallel.

    Returns names that are still missing (fail-closed for required set).
    Cloud agents do not run sessionStart; callers must invoke this from
    subagentStart as well. Wall-clock budget is bounded so we deny before
    the hook timeout instead of failing open.
    """
    dest = Path(project) / ".synaptory" / ".protocols"
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError:
        return list(REQUIRED_PROTOCOLS)
    cli = _cli_bin()
    disk_dirs = _protocol_disk_dirs(root)
    missing: list[str] = []
    need_fetch: list[str] = []
    for name in PROTOCOL_NAMES:
        cached = dest / f"{name}.md"
        if cached.is_file() and cached.stat().st_size > 0:
            continue
        need_fetch.append(name)
    deadline = time.monotonic() + _PROTOCOL_FETCH_BUDGET_S
    if need_fetch:
        workers = min(8, len(need_fetch))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(_fetch_protocol_body, name, cli, disk_dirs): name
                for name in need_fetch
            }
            try:
                remaining = max(0.05, deadline - time.monotonic())
                for fut in as_completed(futs, timeout=remaining):
                    name = futs[fut]
                    try:
                        body = fut.result()
                    except Exception:
                        body = ""
                    if body.strip():
                        try:
                            (dest / f"{name}.md").write_text(body, encoding="utf-8")
                        except OSError:
                            body = ""
                    if name in REQUIRED_PROTOCOLS and not (body or "").strip():
                        missing.append(name)
            except TimeoutError:
                for name in need_fetch:
                    path = dest / f"{name}.md"
                    if name in REQUIRED_PROTOCOLS and not (
                        path.is_file() and path.stat().st_size > 0
                    ):
                        if name not in missing:
                            missing.append(name)
    for name in PROTOCOL_NAMES:
        path = dest / f"{name}.md"
        if name in REQUIRED_PROTOCOLS and not (
            path.is_file() and path.stat().st_size > 0
        ):
            if name not in missing:
                missing.append(name)
    return missing


def shutil_which(name: str) -> bool:
    from shutil import which

    return which(name) is not None


def _model_id(data: dict[str, Any]) -> str:
    for key in ("model_id", "model", "subagent_model"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    params = data.get("model_params")
    if isinstance(params, dict):
        val = params.get("model") or params.get("id")
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def before_submit(data: dict[str, Any], project: str, root: Path) -> dict[str, Any] | None:
    if not _cli_on_path():
        return {
            "continue": False,
            "user_message": (
                "synaptory: CLI not found on $PATH. Install once per laptop:\n"
                "  curl -fsSL https://synaptory.h3t.co/cli/install.sh | bash\n"
                "then run `synaptory login`. /synaptory cannot start without it."
            ),
        }
    if not _session_ok():
        return {"continue": False, "user_message": _auth_blocked_message()}
    baa = _yaml_baa(project)
    if baa:
        plan = str(data.get("subscription") or data.get("plan") or data.get("cursor_plan") or "")
        plan_l = plan.lower()
        teamsish = any(x in plan_l for x in ("team", "pro", "start", "hobby")) and "enterprise" not in plan_l
        flagged = os.environ.get("SYNAPTORY_CURSOR_ENTERPRISE_BAA") == "1"
        if teamsish or not flagged:
            return {
                "continue": False,
                "user_message": (
                    "synaptory: healthcare.baa_enforced is true. Cursor BAA exists "
                    "only on Enterprise (not Teams/Pro/Start), Privacy Mode must be "
                    "locked, and Eligible Models are Trust Center only. Set "
                    "SYNAPTORY_CURSOR_ENTERPRISE_BAA=1 after those process checks, "
                    "or disable baa_enforced. See skills/synaptory/limitations.md."
                ),
            }
        model = _model_id(data)
        if not model:
            return {
                "continue": False,
                "user_message": (
                    "synaptory: healthcare.baa_enforced is true but the model id is "
                    "hidden or empty (Cursor Router). Refusing rather than inventing "
                    "a claude pin. Pick a visible Eligible Model — list is Trust "
                    "Center only ([VERIFY])."
                ),
            }
    return None


def session_end(data: dict[str, Any], project: str, root: Path) -> None:
    stdin = json.dumps(data).encode()
    for name in SESSION_END_SCRIPTS:
        _run_script(root, name, stdin)


def _nest_path(project: str) -> Path:
    p = Path(project) / ".synaptory" / ".orchestrator"
    p.mkdir(parents=True, exist_ok=True)
    return p / "cursor-nest.json"


def _read_nest(project: str) -> dict[str, Any]:
    path = _nest_path(project)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"agents": {}}
    if not isinstance(data, dict):
        return {"agents": {}}
    agents = data.get("agents")
    if not isinstance(agents, dict):
        agents = {}
    return {"agents": agents}


def _write_nest(project: str, nest: dict[str, Any]) -> None:
    _nest_path(project).write_text(json.dumps(nest), encoding="utf-8")


def _marker_dir(project: str) -> Path:
    d = Path(project) / ".synaptory" / ".orchestrator" / "subagent-markers"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_id(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", raw) or "unknown"


def _subagent_id(data: dict[str, Any]) -> str:
    for key in ("subagent_id", "agent_id", "id"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    conv = data.get("conversation_id") or data.get("generation_id") or ""
    kind = data.get("subagent_type") or data.get("agent_type") or "role"
    return f"{conv}:{kind}" if conv else kind


def _subagent_type(data: dict[str, Any]) -> str:
    for key in ("subagent_type", "agent_type", "name", "type"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _role_name(kind: str) -> str:
    raw = kind.strip()
    if raw.startswith("synaptory:"):
        raw = raw.split(":", 1)[1]
    return raw


def _parent_is_roster(data: dict[str, Any], agents: dict[str, Any]) -> bool:
    parent_agent = data.get("parent_subagent_id") or data.get("parent_agent_id")
    if isinstance(parent_agent, str) and parent_agent.strip():
        rec = agents.get(parent_agent.strip())
        if isinstance(rec, dict) and rec.get("role") in ROSTER:
            return True
    parent_conv = data.get("parent_conversation_id")
    own_conv = str(data.get("conversation_id") or "")
    if isinstance(parent_conv, str) and parent_conv.strip():
        for rec in agents.values():
            if not isinstance(rec, dict):
                continue
            if rec.get("conversation_id") == parent_conv and rec.get("role") in ROSTER:
                # Top-level parallel workers share the orchestrator conversation
                # id (`conversation_id == parent_conversation_id`). A role's
                # parallel child has a distinct conversation_id.
                if own_conv != parent_conv:
                    return True
                return False
    return False


def subagent_start(data: dict[str, Any], project: str, root: Path) -> dict[str, Any] | None:
    if not _session_ok():
        return {
            "permission": "deny",
            "user_message": _auth_blocked_message(),
        }
    kind_raw = _subagent_type(data)
    kind = _role_name(kind_raw)
    if kind in ROSTER:
        missing = _materialize_protocols(project, root)
        if missing:
            return {
                "permission": "deny",
                "user_message": (
                    "synaptory: required protocols are missing "
                    f"({', '.join(missing)}). Cursor cloud agents do not run "
                    "sessionStart — subagentStart must fetch them via "
                    "`synaptory skills get protocols/<name> --host cursor`. "
                    "Refusing this role until the cache is populated."
                ),
            }
    agent_id = _subagent_id(data)
    nest = _read_nest(project)
    agents = dict(nest.get("agents") or {})
    # Parallel top-level workers have is_parallel_worker=true and no roster
    # parent. A process-global depth counter treated the second SE as nested.
    nested = kind in ROSTER and _parent_is_roster(data, agents)
    if nested:
        return {
            "permission": "deny",
            "user_message": (
                "synaptory: role subagents cannot spawn another role "
                f"(attempted nested {kind}). Nesting is one level: "
                "orchestrator → role."
            ),
        }
    if kind in ROSTER or "synaptory:" in kind_raw:
        marker = _marker_dir(project) / _safe_id(agent_id)
        marker.write_text(kind_raw or kind, encoding="utf-8")
        agents[agent_id] = {
            "role": kind if kind in ROSTER else kind_raw,
            "conversation_id": str(data.get("conversation_id") or ""),
            "parent_id": data.get("parent_subagent_id") or data.get("parent_agent_id"),
        }
        nest["agents"] = agents
        _write_nest(project, nest)
        # Telemetry: best-effort matched to Claude inject-protocols span start.
        otel = root / "hooks" / "lib" / "otel_writer.py"
        if otel.is_file():
            subprocess.run(
                [
                    sys.executable,
                    str(otel),
                    "start",
                    "--suite-dir",
                    str(Path(project) / ".synaptory"),
                    "--session-id",
                    str(data.get("conversation_id") or ""),
                    "--agent-id",
                    agent_id,
                ],
                check=False,
                capture_output=True,
            )
    return {"permission": "allow"}


def subagent_stop(data: dict[str, Any], project: str, root: Path) -> dict[str, Any] | None:
    status = str(data.get("status") or "").lower()
    kind = _subagent_type(data)
    agent_id = _subagent_id(data)
    nest = _read_nest(project)
    agents = dict(nest.get("agents") or {})
    agents.pop(agent_id, None)
    nest["agents"] = agents
    _write_nest(project, nest)

    mapped = {
        "description": data.get("description") or data.get("task") or data.get("summary") or kind,
        "name": kind,
        "transcript_path": data.get("agent_transcript_path") or data.get("transcript_path") or "",
        "agent_id": agent_id,
        "session_id": data.get("conversation_id") or data.get("session_id") or "",
        "model": _model_id(data),
        "model_id": _model_id(data),
    }
    stdin = json.dumps(mapped).encode()
    proc = _run_script(root, "synaptory-verify-receipt.sh", stdin)
    # Cursor cannot fail-closed via exit 2. Convert a blocking Claude failure
    # into followup_message, and only when status=completed (otherwise Cursor
    # drops the field).
    if proc.returncode != 0 and status == "completed":
        err = proc.stderr.decode("utf-8", "replace").strip() or proc.stdout.decode(
            "utf-8", "replace"
        ).strip()
        return {
            "followup_message": (
                "synaptory: receipt missing or invalid — do NOT call MCP "
                f"advance / transition_story. Rewrite the receipt for {kind}.\n"
                f"{err[:2000]}"
            )
        }
    if status == "completed":
        for obj in _extract_json_objects(proc.stdout.decode("utf-8", "replace")):
            translated = translate_claude(obj, "subagentStop")
            if translated.get("followup_message"):
                return translated
    return None


def stop(data: dict[str, Any], project: str, root: Path) -> dict[str, Any] | None:
    _sys_path_lib(root)
    try:
        from loop_engine import STOP_ACTIONS, should_continue  # type: ignore
    except Exception:
        return None
    session_id = str(data.get("conversation_id") or data.get("session_id") or "")
    try:
        decision = should_continue(project, session_id)
    except Exception:
        return None
    action = ""
    try:
        # Engine may include the next_action name on the activity payload.
        activity = decision.get("activity") or {}
        action = str(activity.get("action") or "")
    except Exception:
        action = ""
    if not decision.get("continue"):
        return None
    reason = str(decision.get("reason") or "")
    # Never auto-continue through human gates even if the engine said continue
    # (it should not — belt and braces).
    lower = reason.lower()
    for stop_name in ("await_sync", "await_acceptance"):
        if stop_name in lower or action == stop_name:
            return None
    disable = os.environ.get("SYNAPTORY_LOOP", "").lower()
    if disable in {"0", "off", "disable", "disabled"}:
        return None
    if reason:
        # loop_engine reason still mentions CLAUDE_PLUGIN_ROOT / Claude Stop JSON;
        # rewrite the pointer for Cursor.
        reason = reason.replace("CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT")
        reason = reason.replace('{"decision":"block"', "followup_message")
        return {"followup_message": reason}
    return None


def _tool_name(data: dict[str, Any]) -> str:
    return str(data.get("tool_name") or data.get("tool") or data.get("name") or "")


def _tool_input(data: dict[str, Any]) -> dict[str, Any]:
    inp = data.get("tool_input") or data.get("args") or data.get("input") or {}
    return inp if isinstance(inp, dict) else {}


def boundary(data: dict[str, Any], project: str, root: Path) -> dict[str, Any] | None:
    if os.environ.get("SYNAPTORY_GUARDRAILS") == "0":
        return None
    if not (Path(project) / ".synaptory").is_dir():
        return None
    tool = _tool_name(data)
    inp = _tool_input(data)
    # Cursor beforeShellExecution uses `command`; preToolUse uses tool_input.
    command = str(
        data.get("command") or inp.get("command") or data.get("script") or ""
    )
    file_path = str(
        inp.get("file_path")
        or inp.get("path")
        or data.get("file_path")
        or data.get("file_path_after")
        or ""
    )
    if command and GIT_FORCE.search(command):
        return _deny(
            "Force-git is blocked (push --force, reset --hard, checkout --force). "
            "Iron law: no history rewrite on a shared branch."
        )
    # Reuse the Claude guard Python for tracker / pipeline-state / sync, plus Delete.
    mapped = {
        "tool_name": "Write" if tool.lower() in {"write", "delete", "edit", "stredit"} else (
            "Bash" if (tool.lower() in {"shell", "bash"} or command) else tool or "Write"
        ),
        "tool_input": {
            "file_path": file_path,
            "command": command or inp.get("command", ""),
        },
        "cwd": project,
    }
    if tool.lower() == "delete":
        mapped["tool_name"] = "Write"  # same path checks
    proc = _run_script(
        root,
        "synaptory-boundary-guard.sh",
        json.dumps(mapped).encode(),
        env={"SYNAPTORY_GUARDRAILS": os.environ.get("SYNAPTORY_GUARDRAILS", "1")},
    )
    for obj in _extract_json_objects(proc.stdout.decode("utf-8", "replace")):
        translated = translate_claude(obj, "preToolUse")
        if translated.get("permission") == "deny":
            translated.setdefault(
                "user_message",
                "Blocked by synaptory boundary guard.",
            )
            return translated
    # Nested Task to a role while already in a role.
    if tool.lower() == "task":
        nested_type = str(
            inp.get("subagent_type") or inp.get("name") or inp.get("agent") or ""
        )
        nest = _read_nest(project)
        if nested_type in ROSTER and int(nest.get("depth") or 0) >= 1:
            return _deny(
                f"Role→role Task({nested_type}) denied. Nesting is one level."
            )
    return None


def _deny(message: str) -> dict[str, Any]:
    return {"permission": "deny", "user_message": message}


def activity(data: dict[str, Any], project: str, root: Path) -> None:
    if not (Path(project) / ".synaptory").is_dir():
        return
    _run_script(root, "synaptory-activity-ping.sh", json.dumps(data).encode())
    monitor = root / "hooks" / "synaptory-monitor-events.sh"
    if monitor.is_file():
        _run_script(root, "synaptory-monitor-events.sh", json.dumps(data).encode())


def tool_failure(data: dict[str, Any], project: str, root: Path) -> None:
    _run_script(root, "synaptory-tool-failure.sh", json.dumps(data).encode())


def reanchor(data: dict[str, Any], project: str, root: Path) -> dict[str, Any] | None:
    # Cursor preCompact accepts user_message only (not additional_context).
    proc = _run_script(root, "synaptory-reanchor.sh", json.dumps(data).encode())
    chunks: list[str] = []
    for obj in _extract_json_objects(proc.stdout.decode("utf-8", "replace")):
        ctx = obj.get("additionalContext")
        if ctx:
            chunks.append(str(ctx))
    rules = root / "hooks" / "lib" / "compacted-rules.md"
    if rules.is_file() and not chunks:
        try:
            chunks.append(rules.read_text(encoding="utf-8")[:8000])
        except OSError:
            pass
    if not chunks:
        state = Path(project) / ".synaptory" / ".orchestrator" / "pipeline-state.json"
        if state.is_file():
            try:
                chunks.append(state.read_text(encoding="utf-8")[:4000])
            except OSError:
                pass
    if chunks:
        return {"user_message": "\n\n".join(chunks)[:12000]}
    return None


def after_file_edit(data: dict[str, Any], project: str, root: Path) -> dict[str, Any] | None:
    path = ""
    for key in ("file_path", "path"):
        val = data.get(key)
        if isinstance(val, str) and val:
            path = val
            break
    edits = data.get("edits")
    if not path and isinstance(edits, list) and edits and isinstance(edits[0], dict):
        maybe = edits[0].get("path")
        if isinstance(maybe, str):
            path = maybe
    interesting = (
        not path
        or "pipeline-state.json" in path
        or path.endswith(".synaptory.yaml")
        or path.endswith("/.synaptory.yaml")
    )
    if path and not interesting:
        return None
    # Always consult drift if the matcher didn't give a path.
    proc = _run_script(root, "synaptory-watch-drift.sh", json.dumps(data).encode())
    for obj in _extract_json_objects(proc.stdout.decode("utf-8", "replace")):
        ctx = obj.get("additionalContext")
        if ctx:
            return {"user_message": str(ctx)}
    return None


def before_mcp(data: dict[str, Any], project: str, root: Path) -> dict[str, Any] | None:
    name = str(data.get("tool_name") or data.get("name") or "")
    if "synaptory" not in name.lower() and not name.startswith("mcp"):
        # Still gate our server even when the matcher already scoped us.
        pass
    if _yaml_baa(project):
        # Cursor BAA does not auto-cover plugin MCP; US residency excludes MCP.
        return _deny(
            "synaptory: healthcare.baa_enforced is true — do not send PHI "
            "(or any story body) through plugin MCP. Use local Python: "
            f"{root}/hooks/lib/state_machine.py and tracker_cli.py."
        )
    return {"permission": "allow"}


def workspace_open(data: dict[str, Any], project: str, root: Path) -> dict[str, Any]:
    return {"pluginPaths": [str(root)]}


def doctor_context(data: dict[str, Any], project: str, root: Path) -> dict[str, Any] | None:
    script = root / "hooks" / "synaptory-doctor.sh"
    if not script.is_file():
        return None
    proc = _run_script(root, "synaptory-doctor.sh", b"")
    text = proc.stdout.decode("utf-8", "replace").strip()
    if text:
        return {"additional_context": text[:8000]}
    return None


HANDLERS = {
    "session-start": session_start,
    "before-submit": before_submit,
    "session-end": session_end,
    "subagent-start": subagent_start,
    "subagent-stop": subagent_stop,
    "stop": stop,
    "boundary": boundary,
    "activity": activity,
    "tool-failure": tool_failure,
    "reanchor": reanchor,
    "after-file-edit": after_file_edit,
    "before-mcp": before_mcp,
    "workspace-open": workspace_open,
    "doctor": doctor_context,
}

# Events where permission deny must also exit 2 (failClosed).
DENY_EXIT_2 = {"boundary", "before-mcp", "subagent-start"}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        sys.stderr.write("usage: cursor_hook.py <subcommand>\n")
        return 2
    name = argv[0]
    fn = HANDLERS.get(name)
    if fn is None:
        sys.stderr.write(f"unknown subcommand: {name}\n")
        return 2
    data, project, root = _export_env(_load_stdin())
    result = fn(data, project, root)
    if isinstance(result, dict):
        emit(result)
        if name in DENY_EXIT_2 and result.get("permission") == "deny":
            return 2
        if name == "before-submit" and result.get("continue") is False:
            return 0  # Cursor honors continue:false; exit 2 is for tool deny
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
