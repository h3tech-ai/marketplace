"""Pytest fixtures for the plugin module test suite.

Three layers live under plugin-claude/tests/:

  * lib/    — Layer 1 unit tests for plugin Python (no subprocess, no network).
  * hooks/  — Layer 2 integration tests that exec real .sh hooks against a
              stub `synaptory` CLI (this module's `stub_cli` fixture).
  * build/  — Layer 2 tests that exec the build script against a tmp output dir.

Layer 3 (full e2e against a live control plane) lives in e2e/scenarios/
and is run by `./synaptory e2e`, not pytest from here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pytest

HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parent
REPO_ROOT = PLUGIN_ROOT.parent

# Make the plugin's Python helpers importable from L1 tests.
# Two paths are added because modules under hooks/lib/ use sibling-import
# style (e.g. `from story_pipeline import …`) — they're typically loaded
# as scripts. Tests mirror that import style.
for _p in (PLUGIN_ROOT, PLUGIN_ROOT / "hooks" / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# ─── Session-scoped roots ─────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def plugin_root() -> Path:
    return PLUGIN_ROOT


@pytest.fixture(scope="session")
def stub_cli_path() -> Path:
    """Absolute path to the bash-shim stub `synaptory` binary."""
    p = HERE / "fixtures" / "stub_cli" / "synaptory"
    assert p.is_file(), f"stub CLI missing at {p}"
    return p


# ─── Per-test stub-CLI scaffolding ────────────────────────────────────────────


@dataclass
class StubCli:
    """Snapshot of the stub-CLI environment a test has set up.

    The hook process must see `synaptory` first on `$PATH`. We build a
    per-test bin dir containing a symlink to the shim, plus a per-test
    `bodies_dir` that the shim reads for canned `skills get` responses.
    Tests pass `env` to subprocess.run when invoking hooks.
    """

    bin_dir: Path
    bodies_dir: Path
    env: dict[str, str]

    def add_body(self, name: str, body: str) -> Path:
        """Write a canned body the stub will return for `skills get <name>`.

        `name` follows the seeder's naming (`protocols/receipt-protocol`,
        `rules/synaptory-ux`, `software-engineer/agent`, etc.).
        """
        body_path = self.bodies_dir / f"{name}.md"
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_text(body, encoding="utf-8")
        return body_path

    def fail_names(self, names: Iterable[str]) -> None:
        """Force the stub to exit 1 for any of these names (partial-failure tests)."""
        self.env["SYNAPTORY_STUB_FAIL_NAMES"] = " ".join(names)


@pytest.fixture
def stub_cli(tmp_path: Path, stub_cli_path: Path) -> StubCli:
    bin_dir = tmp_path / "bin"
    bodies_dir = tmp_path / "bodies"
    bin_dir.mkdir()
    bodies_dir.mkdir()
    # Symlink keeps the shebang intact and means `command -v synaptory`
    # resolves to a real executable.
    (bin_dir / "synaptory").symlink_to(stub_cli_path)
    (bin_dir / "synaptory-local").symlink_to(stub_cli_path)

    env = {
        # Minimal sanitised env — hooks shouldn't need anything else.
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "LANG": "C",
        "SYNAPTORY_STUB_BODIES_DIR": str(bodies_dir),
    }
    return StubCli(bin_dir=bin_dir, bodies_dir=bodies_dir, env=env)


@pytest.fixture
def stub_cli_unavailable(tmp_path: Path) -> dict[str, str]:
    """Env with NO stub on $PATH — tests the on-disk fallback path."""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "LANG": "C",
    }


# ─── Hook invocation helper ───────────────────────────────────────────────────


def _run_hook(
    hook_path: Path,
    *,
    env: dict[str, str],
    stdin: str = "",
    cwd: Path | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    """Run a hook script and return the full CompletedProcess.

    Tests that want the parsed `additional_context` should call
    `parse_hook_output(result.stdout)` on the returned `.stdout`.
    """
    return subprocess.run(
        ["bash", str(hook_path)],
        input=stdin,
        env=env,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def parse_hook_output(stdout: str) -> dict | None:
    """Parse a hook's JSON output payload.

    Returns None when stdout is empty (hooks may legitimately exit silently
    when there's nothing to inject).

    Normalises the camelCase ``additionalContext`` key introduced in the Phase 1
    modernisation back to the legacy ``additional_context`` key so that existing
    test assertions continue to pass during the transition.  Both keys are
    present in the returned dict when the hook emits either form.
    """
    s = stdout.strip()
    if not s:
        return None
    d = json.loads(s)
    # Accept both the new camelCase key and the legacy snake_case key.
    if "additionalContext" in d and "additional_context" not in d:
        d["additional_context"] = d["additionalContext"]
    elif "additional_context" in d and "additionalContext" not in d:
        d["additionalContext"] = d["additional_context"]
    return d


@pytest.fixture
def run_hook():
    """Convenience wrapper for invoking a hook script."""
    return _run_hook


@pytest.fixture
def parse_output():
    return parse_hook_output


# ─── Hook env builder ─────────────────────────────────────────────────────────


@pytest.fixture
def hook_env(plugin_root: Path, stub_cli: StubCli, tmp_path: Path):
    """Default hook env: stub CLI on PATH + plugin root + tmp project dir.

    Writes gitignored hooks/lib/cp-url.local so hooks resolve a URL without
    honouring SYNAPTORY_CONTROL_PLANE_URL. Restored after the test.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    local_url = plugin_root / "hooks" / "lib" / "cp-url.local"
    local_url.parent.mkdir(parents=True, exist_ok=True)
    previous = local_url.read_text(encoding="utf-8") if local_url.exists() else None
    local_url.write_text("https://cp.test\n", encoding="utf-8")
    try:
        yield {
            **stub_cli.env,
            "CLAUDE_PLUGIN_ROOT": str(plugin_root),
            "CLAUDE_PROJECT_DIR": str(project_dir),
        }
    finally:
        if previous is None:
            local_url.unlink(missing_ok=True)
        else:
            local_url.write_text(previous, encoding="utf-8")


@pytest.fixture
def subagent_stdin() -> str:
    """JSON payload Claude Code sends to a SubagentStart hook on stdin."""
    return json.dumps(
        {
            "agent_id": "test-agent-001",
            "agent_type": "general-purpose",
            "session_id": "test-session-001",
            "transcript_path": "/tmp/transcript.jsonl",
            "cwd": "/tmp",
            "hook_event_name": "SubagentStart",
        }
    )


# ─── Test-isolation safety ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_synaptory_state(monkeypatch, tmp_path):
    """Make sure tests don't leak into the developer's real ~/.synaptory/.

    Every test gets a fresh $HOME inside tmp_path.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    yield
