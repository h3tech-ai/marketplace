#!/usr/bin/env python3
"""Host-observed receipt attribution for native Codex agents.

Codex normally supplies ``agent_transcript_path`` to ``SubagentStop``.  Codex
0.147 does not emit that event for every custom-agent completion, so receipt
validation also performs a bounded, dispatch-bound lookup in the local Codex
session store.  The lookup never uploads transcript content and refuses parent
sessions, other projects, oversized transcripts, and ambiguous matches.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
MAX_CANDIDATES = 256


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


def plugin_version() -> str:
    manifest = Path(__file__).resolve().parent.parent / ".codex-plugin" / "plugin.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "0.0.0"
    return str(payload.get("version") or "0.0.0")


def transcript_runtime(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract the actual model and last cumulative usage from a Codex JSONL."""

    raw_path = payload.get("agent_transcript_path") or payload.get("transcript_path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return {}
    path = Path(raw_path).expanduser()
    try:
        size = path.stat().st_size
    except OSError:
        return {}
    if path.suffix != ".jsonl" or size > MAX_TRANSCRIPT_BYTES:
        return {}
    model: str | None = None
    usage: dict[str, Any] | None = None
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                kind = record.get("type")
                body = record.get("payload")
                if not isinstance(body, dict):
                    continue
                if kind == "turn_context" and isinstance(body.get("model"), str):
                    model = body["model"]
                elif kind == "world_state":
                    state = body.get("state")
                    if isinstance(state, dict) and isinstance(state.get("model"), str):
                        model = state["model"]
                elif kind == "event_msg" and body.get("type") == "token_count":
                    info = body.get("info")
                    observed = info.get("total_token_usage") if isinstance(info, dict) else None
                    if isinstance(observed, dict):
                        usage = observed
    except OSError:
        return {}
    result: dict[str, Any] = {}
    if model:
        result["model_id"] = model
    if usage is not None:
        result["usage"] = usage
    return result


def patch_codex_receipt(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Patch only host-observed attribution fields into a Codex receipt."""

    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"receipt is unreadable: {exc}") from exc
    if not isinstance(receipt, dict):
        raise ValueError("receipt must contain a JSON object")
    if receipt.get("backend") != "codex":
        raise ValueError("governed Codex receipt must use backend: codex")
    receipt["ide"] = "codex"
    receipt["plugin_version"] = plugin_version()
    model = payload.get("model_id") or payload.get("model")
    if isinstance(receipt.get("model"), dict):
        pass  # Preserve bridge-owned typed identity; hook payloads cannot replace it.
    elif isinstance(model, str) and model.strip():
        receipt["model"] = model.strip()
    elif not str(receipt.get("model") or "").strip():
        receipt["model"] = "codex-runtime-unattributed"
    usage = payload.get("usage")
    if isinstance(usage, dict):
        existing = receipt.get("token_usage")
        token_usage = dict(existing) if isinstance(existing, dict) else {}
        aliases = {
            "input": ("input", "input_tokens"),
            "output": ("output", "output_tokens"),
            "cache_read": ("cache_read", "cached_input_tokens", "cache_read_input_tokens"),
            "cache_write": (
                "cache_write", "cache_write_input_tokens", "cache_creation_input_tokens"
            ),
        }
        for target, sources in aliases.items():
            for source in sources:
                value = usage.get(source)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    token_usage[target] = value
                    break
        stages = {
            "software-engineer": "se-implementation",
            "quality-engineer": "qe-verification",
            "code-reviewer": "cr-review",
            "compliance-engineer": "ce-compliance",
            "platform-engineer": "pe-infra",
            "solution-architect": "sa-architecture",
            "technical-writer": "tw-docs",
            "research-advisor": "orchestrator",
        }
        stage = stages.get(str(receipt.get("role") or ""))
        if stage:
            token_usage["stage"] = stage
        receipt["token_usage"] = token_usage
    _atomic_json(path, receipt)
    return receipt


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _candidate_transcripts(started_at: str | None) -> list[Path]:
    raw_home = os.environ.get("CODEX_HOME")
    codex_home = Path(raw_home).expanduser() if raw_home else Path.home() / ".codex"
    sessions = codex_home / "sessions"
    if not sessions.is_dir():
        return []
    bases = [datetime.now(timezone.utc)]
    started = _parse_time(started_at)
    if started:
        bases.append(started)
    days = {
        (base + timedelta(days=offset)).date()
        for base in bases
        for offset in (-1, 0, 1)
    }
    candidates: list[Path] = []
    for day in sorted(days, reverse=True):
        directory = sessions / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
        if directory.is_dir():
            candidates.extend(directory.glob("rollout-*.jsonl"))
    def modified(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    candidates.sort(key=modified, reverse=True)
    return candidates[:MAX_CANDIDATES]


def _is_matching_subagent(
    path: Path, project: Path, dispatch_id: str, expected_agent_role: str
) -> bool:
    try:
        if path.stat().st_size > MAX_TRANSCRIPT_BYTES:
            return False
        session_matches = False
        dispatch_matches = False
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if dispatch_id in line:
                    dispatch_matches = True
                if not session_matches:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict) or record.get("type") != "session_meta":
                        continue
                    body = record.get("payload") if isinstance(record, dict) else None
                    if not isinstance(body, dict):
                        continue
                    source = body.get("source")
                    subagent = source.get("subagent") if isinstance(source, dict) else None
                    # Codex also creates internal guardian sessions which see
                    # the parent's dispatch prompt. Only an explicitly spawned
                    # agent is eligible to author governed role evidence.
                    is_subagent = (
                        body.get("thread_source") == "subagent"
                        and isinstance(subagent, dict)
                        and isinstance(subagent.get("thread_spawn"), dict)
                    )
                    raw_cwd = body.get("cwd")
                    role_matches = body.get("agent_role") == expected_agent_role
                    if is_subagent and role_matches and isinstance(raw_cwd, str):
                        try:
                            session_matches = Path(raw_cwd).expanduser().resolve() == project.resolve()
                        except OSError:
                            session_matches = False
                if session_matches and dispatch_matches:
                    return True
    except OSError:
        return False
    return session_matches and dispatch_matches


def locate_dispatch_transcript(
    project: Path,
    dispatch_id: str,
    expected_agent_role: str,
    started_at: str | None = None,
) -> Path | None:
    """Return one exact project/subagent/dispatch match, refusing ambiguity."""

    if not dispatch_id:
        return None
    matches = [
        path
        for path in _candidate_transcripts(started_at)
        if _is_matching_subagent(path, project, dispatch_id, expected_agent_role)
    ]
    return matches[0] if len(matches) == 1 else None


def attribute_receipt_from_dispatch(
    project: Path, receipt_path: Path, started_at: str | None = None
) -> dict[str, Any]:
    """Apply local transcript attribution and return an auditable result."""

    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"attributed": False, "reason": "receipt_unreadable"}
    if not isinstance(receipt, dict) or receipt.get("backend") != "codex":
        return {"attributed": False, "reason": "not_a_codex_receipt"}
    dispatch_id = str(receipt.get("dispatch_id") or "")
    role = str(receipt.get("role") or "")
    transcript = locate_dispatch_transcript(
        project, dispatch_id, f"synaptory-{role}", started_at
    )
    if transcript is None:
        patch_codex_receipt(receipt_path, {})
        return {"attributed": False, "reason": "dispatch_transcript_not_found"}
    runtime = transcript_runtime({"agent_transcript_path": str(transcript)})
    patched = patch_codex_receipt(receipt_path, runtime)
    usage = patched.get("token_usage")
    attributed = (
        str(patched.get("model") or "") not in {"", "codex-runtime-unattributed"}
        and isinstance(usage, dict)
        and isinstance(usage.get("input"), int)
        and isinstance(usage.get("output"), int)
    )
    return {
        "attributed": attributed,
        "source": "local_dispatch_transcript",
        "transcript_id": transcript.stem.rsplit("-", 1)[-1],
    }
