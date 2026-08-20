"""Layer 2 — `synaptory-activity-ping.sh` CLI invocation contract.

The PostToolUse/Stop activity hook parses stdin, then fires
`synaptory telemetry activity …` in the background. Per issue #117 it must
forward Claude Code's `session_id` via `--session-id` so the CLI attributes the
event to the session's bound project (not the process cwd).

The CLI call is backgrounded (`( … ) & disown`), so the hook returns before the
stub writes its log — tests poll the telemetry log until the line lands.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


@pytest.fixture
def activity_hook(plugin_root: Path) -> Path:
    return plugin_root / "hooks" / "synaptory-activity-ping.sh"


@pytest.fixture
def telemetry_log(tmp_path: Path) -> Path:
    p = tmp_path / "telemetry.log"
    p.write_text("", encoding="utf-8")
    return p


def _poll_log(path: Path, needle: str, timeout: float = 5.0) -> str:
    """Wait up to `timeout`s for `needle` to appear in the (backgrounded) log."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        txt = path.read_text(encoding="utf-8")
        if needle in txt:
            return txt
        time.sleep(0.05)
    return path.read_text(encoding="utf-8")


def _synaptory_workspace(env: dict[str, str]) -> None:
    """The hook only fires inside a synaptory project (CLAUDE_PROJECT_DIR/.synaptory)."""
    Path(env["CLAUDE_PROJECT_DIR"], ".synaptory").mkdir(parents=True, exist_ok=True)


@pytest.mark.hook
def test_activity_ping_forwards_session_id(
    activity_hook: Path,
    hook_env: dict[str, str],
    run_hook,
    telemetry_log: Path,
):
    """The hook forwards --session-id from stdin to `telemetry activity` (#117)."""
    env = {**hook_env, "SYNAPTORY_STUB_TELEMETRY_LOG": str(telemetry_log)}
    _synaptory_workspace(env)
    stdin = json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "cd /some/other/repo"},
            "session_id": "sess-abc-123",
            "hook_event_name": "PostToolUse",
        }
    )

    result = run_hook(activity_hook, env=env, stdin=stdin)
    assert result.returncode == 0, f"hook must exit 0; stderr={result.stderr!r}"

    log = _poll_log(telemetry_log, "telemetry activity")
    assert "telemetry activity" in log, f"expected a telemetry activity call; log={log!r}"
    assert "--session-id sess-abc-123" in log, (
        f"activity ping must forward --session-id from stdin; log={log!r}"
    )


@pytest.mark.hook
def test_activity_ping_omits_session_id_when_absent(
    activity_hook: Path,
    hook_env: dict[str, str],
    run_hook,
    telemetry_log: Path,
):
    """No session_id on stdin → no --session-id flag (CLI falls back to the cache)."""
    env = {**hook_env, "SYNAPTORY_STUB_TELEMETRY_LOG": str(telemetry_log)}
    _synaptory_workspace(env)
    stdin = json.dumps(
        {"tool_name": "Read", "tool_input": {"file_path": "src/x.ts"}, "hook_event_name": "PostToolUse"}
    )

    result = run_hook(activity_hook, env=env, stdin=stdin)
    assert result.returncode == 0, f"hook must exit 0; stderr={result.stderr!r}"

    log = _poll_log(telemetry_log, "telemetry activity")
    assert "telemetry activity" in log, f"expected a telemetry activity call; log={log!r}"
    assert "--session-id" not in log, (
        f"no session_id on stdin → hook must not pass an empty --session-id; log={log!r}"
    )


@pytest.mark.hook
def test_activity_ping_noop_outside_synaptory_project(
    activity_hook: Path,
    hook_env: dict[str, str],
    run_hook,
    telemetry_log: Path,
):
    """Outside a synaptory workspace (no .synaptory/) the hook exits 0 and ships nothing."""
    env = {**hook_env, "SYNAPTORY_STUB_TELEMETRY_LOG": str(telemetry_log)}
    # Deliberately do NOT create CLAUDE_PROJECT_DIR/.synaptory.
    stdin = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})

    result = run_hook(activity_hook, env=env, stdin=stdin)
    assert result.returncode == 0
    # Give any errant background call a moment; the log must stay empty.
    time.sleep(0.2)
    assert telemetry_log.read_text(encoding="utf-8") == "", "must not ship telemetry outside a project"
