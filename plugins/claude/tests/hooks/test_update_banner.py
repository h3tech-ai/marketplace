"""Layer 2 — SessionStart update-banner behaviour (auto-update Phase A, #93-95).

Execs the real `synaptory-access-token-check.sh` against the stub CLI, whose
`version --check --json` payload is injected via SYNAPTORY_STUB_VERSION_JSON.
Covers:
  * A1 (#93) — the throttle stamp reflects success vs failure (short backoff on
    a failed/unreachable check, full interval on success) so a failed fetch
    doesn't silence updates for ~6h.
  * A2 (#94) — the plugin banner steers to "enable auto-update once" not just
    the per-release manual command.
  * A3 (#95) — a `critical` release forces the background CLI self-update even
    under the default `prompt` policy; a non-critical update does not.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


@pytest.fixture
def hook(plugin_root: Path) -> Path:
    return plugin_root / "hooks" / "synaptory-access-token-check.sh"


def _env(hook_env: dict, *, version_json: dict | None = None,
         policy: str | None = None, update_log: Path | None = None) -> dict:
    env = {
        **hook_env,
        # Force cp-url resolution regardless of the (possibly placeholder)
        # build-stamped hooks/lib/cp-url in the source tree.
        "SYNAPTORY_CP_ENV": "dev",
        "SYNAPTORY_CONTROL_PLANE_URL": "http://localhost:8080",
    }
    if version_json is not None:
        env["SYNAPTORY_STUB_VERSION_JSON"] = json.dumps(version_json)
    if policy is not None:
        env["SYNAPTORY_UPDATE_POLICY"] = policy
    if update_log is not None:
        env["SYNAPTORY_STUB_UPDATE_LOG"] = str(update_log)
    return env


def _stamp(env: dict) -> Path:
    return Path(env["HOME"]) / ".synaptory" / ".version-check-stamp"


_UP_TO_DATE = {"checked": True, "latest": "1.0.0",
               "cli": {"installed": "1.0.0", "update_available": False},
               "plugin": {"installed": "1.0.0", "update_available": False}}


# ── A1: stamp reflects success vs failure ────────────────────────────────────


@pytest.mark.hook
def test_successful_check_stamps_full_interval(hook, hook_env, run_hook):
    env = _env(hook_env, version_json=_UP_TO_DATE)
    before = int(time.time())
    result = run_hook(hook, env=env)
    assert result.returncode == 0, result.stderr
    stamp = _stamp(env)
    assert stamp.is_file(), "successful check must write a stamp"
    val = int(stamp.read_text().strip())
    # A recent timestamp (≈ now), not a backoff far in the past.
    assert val >= before - 5, f"expected ~now, got {val} (before={before})"


@pytest.mark.hook
def test_failed_check_writes_short_backoff(hook, hook_env, run_hook):
    # checked:false = reachable-but-errored → treated as failure → short backoff
    # so the next session retries in ~10 min, not ~6h.
    env = _env(hook_env, version_json={"checked": False})
    now = int(time.time())
    result = run_hook(hook, env=env)
    assert result.returncode == 0, result.stderr
    val = int(_stamp(env).read_text().strip())
    # Backoff = now - interval(21600) + 600 ≈ now - 21000 → well in the past,
    # but not the full-interval "now" a success would write.
    assert val < now - 3600, f"failed check must write a backoff, got {val} (now={now})"


# ── A2: plugin banner steers to enable-once ──────────────────────────────────


@pytest.mark.hook
def test_plugin_update_banner_promotes_enable_once(hook, hook_env, run_hook, parse_output):
    vj = {**_UP_TO_DATE, "latest": "2.0.0",
          "plugin": {"installed": "1.0.0", "update_available": True}}
    result = run_hook(hook, env=_env(hook_env, version_json=vj))
    assert result.returncode == 0, result.stderr
    ctx = parse_output(result.stdout)["additional_context"]
    assert "Auto-update" in ctx and "/plugin" in ctx, ctx
    assert "once" in ctx.lower(), "banner should tell the user to enable it once"


# ── A3: critical forces auto-update under prompt policy ──────────────────────


@pytest.mark.hook
def test_critical_forces_cli_autoupdate_under_prompt(hook, hook_env, run_hook, tmp_path, parse_output):
    log = tmp_path / "update.log"
    vj = {"checked": True, "latest": "2.0.0", "critical": True,
          "cli": {"installed": "1.0.0", "update_available": True},
          "plugin": {"installed": "1.0.0", "update_available": False}}
    # policy defaults to prompt (not set) — critical must still force it.
    result = run_hook(hook, env=_env(hook_env, version_json=vj, update_log=log))
    assert result.returncode == 0, result.stderr
    # Give the detached Popen a beat to exec the stub.
    for _ in range(20):
        if log.exists():
            break
        time.sleep(0.1)
    assert log.exists() and "update --yes" in log.read_text(), (
        "critical release must fire the background CLI self-update even under prompt policy"
    )
    ctx = parse_output(result.stdout)["additional_context"]
    assert "critical" in ctx.lower()


@pytest.mark.hook
def test_noncritical_prompt_does_not_autoupdate(hook, hook_env, run_hook, tmp_path, parse_output):
    log = tmp_path / "update.log"
    vj = {"checked": True, "latest": "2.0.0", "critical": False,
          "cli": {"installed": "1.0.0", "update_available": True},
          "plugin": {"installed": "1.0.0", "update_available": False}}
    result = run_hook(hook, env=_env(hook_env, version_json=vj, update_log=log))
    assert result.returncode == 0, result.stderr
    time.sleep(0.5)
    assert not log.exists(), "non-critical + prompt policy must NOT auto-update the CLI"
    ctx = parse_output(result.stdout)["additional_context"]
    assert "run `synaptory update`" in ctx
