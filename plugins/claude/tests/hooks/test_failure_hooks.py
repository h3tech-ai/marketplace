"""Layer 2 — failure-telemetry hook contract tests (Loop Engineering P0).

The three failure hooks re-landed from #69 ship activity telemetry with
kinds the control-plane allowlist must accept (`api_error`,
`tool_failure`, `compact` — see api/synaptory_api/routers/ingest.py
`_ALLOWED_ACTIVITY_KINDS`). These tests pin the emitter side of that
contract: each hook invokes `synaptory telemetry activity --kind <kind>`
with the expected kind string, and stays silent when it should.

The hooks fire the CLI in a detached background subshell, so assertions
poll the stub telemetry log briefly instead of reading it immediately.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


@pytest.fixture
def telemetry_log(tmp_path: Path) -> Path:
    p = tmp_path / "telemetry.log"
    p.write_text("", encoding="utf-8")
    return p


@pytest.fixture
def failure_env(hook_env: dict[str, str], telemetry_log: Path) -> dict[str, str]:
    """hook_env + a .synaptory workspace (the hooks exit 0 early without
    one) + the stub telemetry sink."""
    project_dir = Path(hook_env["CLAUDE_PROJECT_DIR"])
    (project_dir / ".synaptory").mkdir(parents=True, exist_ok=True)
    return {**hook_env, "SYNAPTORY_STUB_TELEMETRY_LOG": str(telemetry_log)}


def _wait_for_log(log: Path, timeout_s: float = 3.0) -> str:
    """The hooks background their CLI call; poll for the write."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        content = log.read_text(encoding="utf-8")
        if content.strip():
            return content
        time.sleep(0.1)
    return log.read_text(encoding="utf-8")


@pytest.mark.hook
def test_stop_failure_ships_api_error_kind(
    plugin_root: Path, failure_env, run_hook, telemetry_log: Path
):
    hook = plugin_root / "hooks" / "synaptory-stop-failure.sh"
    stdin = json.dumps(
        {
            "stop_reason": "rate_limit",
            "stop_message": "429 too many requests",
            "session_id": "s1",
            "hook_event_name": "StopFailure",
        }
    )
    result = run_hook(hook, env=failure_env, stdin=stdin)
    assert result.returncode == 0, f"stderr={result.stderr!r}"

    log = _wait_for_log(telemetry_log)
    assert "--kind api_error" in log or "activity --kind api_error" in log.replace(
        "telemetry ", ""
    ), f"expected api_error telemetry, log={log!r}"
    assert "rate_limit" in log


@pytest.mark.hook
def test_stop_failure_ignores_normal_stop_reasons(
    plugin_root: Path, failure_env, run_hook, telemetry_log: Path
):
    """end_turn is a normal completion, not an API error — no telemetry."""
    hook = plugin_root / "hooks" / "synaptory-stop-failure.sh"
    stdin = json.dumps({"stop_reason": "end_turn", "hook_event_name": "StopFailure"})
    result = run_hook(hook, env=failure_env, stdin=stdin)
    assert result.returncode == 0
    time.sleep(0.5)  # give a (wrong) background write the chance to land
    assert telemetry_log.read_text(encoding="utf-8").strip() == "", (
        "end_turn must not produce api_error telemetry"
    )


@pytest.mark.hook
def test_tool_failure_ships_tool_failure_kind(
    plugin_root: Path, failure_env, run_hook, telemetry_log: Path
):
    hook = plugin_root / "hooks" / "synaptory-tool-failure.sh"
    stdin = json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "make build"},
            "error": "exit status 2",
            "hook_event_name": "PostToolUseFailure",
        }
    )
    result = run_hook(hook, env=failure_env, stdin=stdin)
    assert result.returncode == 0, f"stderr={result.stderr!r}"

    log = _wait_for_log(telemetry_log)
    assert "--kind tool_failure" in log, f"expected tool_failure telemetry, log={log!r}"
    assert "Bash" in log


@pytest.mark.hook
def test_post_compact_ships_compact_kind(
    plugin_root: Path, failure_env, run_hook, telemetry_log: Path
):
    hook = plugin_root / "hooks" / "synaptory-post-compact.sh"
    stdin = json.dumps(
        {"summary": "x" * 120, "session_id": "s1", "hook_event_name": "PostCompact"}
    )
    result = run_hook(hook, env=failure_env, stdin=stdin)
    assert result.returncode == 0, f"stderr={result.stderr!r}"

    log = _wait_for_log(telemetry_log)
    assert "--kind compact" in log, f"expected compact telemetry, log={log!r}"


@pytest.mark.hook
def test_failure_hooks_silent_outside_synaptory_project(
    plugin_root: Path, hook_env: dict[str, str], run_hook, tmp_path: Path
):
    """No .synaptory workspace → all three hooks exit 0 without output."""
    bare = tmp_path / "bare-project"
    bare.mkdir()
    env = {**hook_env, "CLAUDE_PROJECT_DIR": str(bare)}
    for name in (
        "synaptory-stop-failure.sh",
        "synaptory-tool-failure.sh",
        "synaptory-post-compact.sh",
    ):
        result = run_hook(
            plugin_root / "hooks" / name,
            env=env,
            stdin='{"stop_reason": "rate_limit", "tool_name": "Bash"}',
        )
        assert result.returncode == 0, f"{name}: stderr={result.stderr!r}"
        assert result.stdout.strip() == "", f"{name} must be silent outside a project"
