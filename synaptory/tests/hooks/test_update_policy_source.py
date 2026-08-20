"""Layer 2 — update-policy precedence (#96, B1).

The SessionStart hook resolves the auto-update policy as:
    SYNAPTORY_UPDATE_POLICY env  >  CP-cached admin_locked.update_policy  >  prompt

Driven through the real hook: the stub's `config value` returns the injected
"CP value" (SYNAPTORY_STUB_CONFIG_VALUE), and behaviour is observed via the
version-check side effects (background self-update fires only under `auto`; an
env `off` skips the whole check).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


@pytest.fixture
def hook(plugin_root: Path) -> Path:
    return plugin_root / "hooks" / "synaptory-access-token-check.sh"


_CLI_UPDATE = {"checked": True, "latest": "2.0.0", "critical": False,
               "cli": {"installed": "1.0.0", "update_available": True},
               "plugin": {"installed": "1.0.0", "update_available": False}}


def _env(hook_env: dict, *, cp_value: str | None = None, env_policy: str | None = None,
         update_log: Path | None = None) -> dict:
    env = {
        **hook_env,
        "SYNAPTORY_CP_ENV": "dev",
        "SYNAPTORY_CONTROL_PLANE_URL": "http://localhost:8080",
        "SYNAPTORY_STUB_VERSION_JSON": json.dumps(_CLI_UPDATE),
    }
    if cp_value is not None:
        env["SYNAPTORY_STUB_CONFIG_VALUE"] = cp_value
    if env_policy is not None:
        env["SYNAPTORY_UPDATE_POLICY"] = env_policy
    if update_log is not None:
        env["SYNAPTORY_STUB_UPDATE_LOG"] = str(update_log)
    return env


def test_cp_auto_triggers_background_update(hook, hook_env, run_hook, tmp_path):
    # No env var; CP policy = auto → the hook should fire the CLI self-update.
    log = tmp_path / "update.log"
    result = run_hook(hook, env=_env(hook_env, cp_value="auto", update_log=log))
    assert result.returncode == 0, result.stderr
    for _ in range(20):
        if log.exists():
            break
        time.sleep(0.1)
    assert log.exists() and "update --yes" in log.read_text(), (
        "CP-provided policy=auto must drive the background self-update"
    )


def test_env_overrides_cp_off_beats_auto(hook, hook_env, run_hook, tmp_path):
    # env=off must win over CP=auto → the whole version-check block is skipped,
    # so no self-update and no banner.
    log = tmp_path / "update.log"
    result = run_hook(hook, env=_env(hook_env, cp_value="auto", env_policy="off", update_log=log))
    assert result.returncode == 0, result.stderr
    time.sleep(0.4)
    assert not log.exists(), "env policy=off must suppress the update path even when CP says auto"
    assert result.stdout.strip() == "", "off policy must emit no banner"


def test_cp_prompt_notifies_without_updating(hook, hook_env, run_hook, tmp_path, parse_output):
    # CP policy = prompt (the default) → banner only, no background update.
    log = tmp_path / "update.log"
    result = run_hook(hook, env=_env(hook_env, cp_value="prompt", update_log=log))
    assert result.returncode == 0, result.stderr
    time.sleep(0.3)
    assert not log.exists(), "prompt policy must not auto-update"
    ctx = parse_output(result.stdout)["additional_context"]
    assert "run `synaptory update`" in ctx


def test_unknown_cp_value_falls_back_to_prompt(hook, hook_env, run_hook, tmp_path):
    # A garbage CP value is sanitised to prompt (not treated as auto/off).
    log = tmp_path / "update.log"
    result = run_hook(hook, env=_env(hook_env, cp_value="banana", update_log=log))
    assert result.returncode == 0, result.stderr
    time.sleep(0.3)
    assert not log.exists(), "unknown policy must sanitise to prompt (no auto-update)"
