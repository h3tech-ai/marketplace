"""Layer 1 — `wait_for.py` bounded polling helper (epic #75 P3).

Hypothesis: the helper polls a condition to success or a hard deadline,
prints exactly one JSON verdict, and its bounds (timeout cap, interval
clamp) hold regardless of caller input. No network: conditions are shell
scriptlets; the interval floor is lowered via the SYNAPTORY_WAIT_FLOOR_S
test hook so the suite stays fast.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
WAIT_FOR = REPO / "core" / "scripts" / "wait_for.py"


@pytest.fixture(autouse=True)
def _cwd_outside_the_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run every `wait_for` child from a throwaway cwd.

    `_emit_wait_timeout` addresses its `events.jsonl` breadcrumb at
    `host_env.project_dir()`, which falls through to `os.getcwd()` when no
    project dir is named. `CLAUDE_PROJECT_DIR=""` below reads as UNSET, not as
    "no project", so "keep telemetry emit inert" is not what happened: the
    child wrote `.synaptory/.orchestrator/` into the checkout the suite was
    running from, and `.synaptory/*` is gitignored so nothing showed (#380).
    The child inherits this cwd, so the breadcrumb lands under `tmp_path`.
    """
    monkeypatch.chdir(tmp_path)


def _run(*args: str, env_extra: dict | None = None) -> tuple[int, dict, str]:
    env = {
        **os.environ,
        "SYNAPTORY_WAIT_FLOOR_S": "0.05",  # fast polling in tests
        "CLAUDE_PROJECT_DIR": "",  # reads as unset -- see the fixture above
        "PATH": "/usr/bin:/bin",  # no synaptory CLI on PATH
    }
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, str(WAIT_FOR), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    verdict = {}
    for line in proc.stdout.strip().splitlines():
        try:
            verdict = json.loads(line)
        except json.JSONDecodeError:
            continue
    return proc.returncode, verdict, proc.stderr


@pytest.mark.unit
def test_immediate_success():
    code, verdict, _ = _run("--timeout", "5", "--interval", "1", "--", "true")
    assert code == 0
    assert verdict["ok"] is True
    assert verdict["attempts"] == 1
    assert verdict["terminal"] is False


@pytest.mark.unit
def test_succeeds_on_nth_attempt(tmp_path: Path):
    """Condition fails twice, then succeeds — via a counter file."""
    counter = tmp_path / "count"
    counter.write_text("0")
    script = tmp_path / "probe.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'n=$(cat "{counter}")\n'
        f'echo $((n+1)) > "{counter}"\n'
        "[ $n -ge 2 ]\n"
    )
    script.chmod(0o755)
    code, verdict, _ = _run("--timeout", "10", "--interval", "1", "--", str(script))
    assert code == 0
    assert verdict["ok"] is True
    assert verdict["attempts"] == 3


@pytest.mark.unit
def test_timeout_returns_false_verdict():
    code, verdict, _ = _run("--timeout", "0.3", "--interval", "1", "--", "false")
    assert code == 1
    assert verdict["ok"] is False
    assert verdict["terminal"] is False
    assert verdict["attempts"] >= 1
    assert verdict["waited_s"] <= 5  # bounded, not hanging


@pytest.mark.unit
def test_timeout_capped_at_1800():
    code, verdict, _ = _run("--timeout", "999999", "--", "true")
    assert code == 0
    assert verdict["timeout_s"] == 1800.0


@pytest.mark.unit
def test_nonexecutable_command_is_terminal():
    """A command that can never run must stop immediately, not spin the
    full deadline."""
    code, verdict, stderr = _run(
        "--timeout", "30", "--interval", "1", "--", "/nonexistent/binary-xyz"
    )
    assert code == 1
    assert verdict["ok"] is False
    assert verdict["terminal"] is True
    assert verdict["attempts"] == 1
    assert "terminal failure" in stderr


@pytest.mark.unit
def test_no_condition_errors_out():
    proc = subprocess.run(
        [sys.executable, str(WAIT_FOR), "--timeout", "5"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 2  # argparse error
    assert "nothing to wait for" in proc.stderr


@pytest.mark.unit
def test_label_flows_into_verdict():
    code, verdict, _ = _run("--timeout", "2", "--label", "api healthy", "--", "true")
    assert verdict["label"] == "api healthy"


@pytest.mark.unit
def test_single_json_line_on_stdout():
    """Agents parse stdout — the verdict must be the only stdout content."""
    env = {
        **os.environ,
        "SYNAPTORY_WAIT_FLOOR_S": "0.05",
        "CLAUDE_PROJECT_DIR": "",
        "PATH": "/usr/bin:/bin",
    }
    proc = subprocess.run(
        [sys.executable, str(WAIT_FOR), "--timeout", "2", "--", "true"],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    assert len(lines) == 1
    json.loads(lines[0])  # parses cleanly


@pytest.mark.unit
def test_interval_clamped_to_floor():
    """interval 0 must not busy-spin: clamped to the floor, the failing
    probe still yields a bounded number of attempts within the deadline."""
    code, verdict, _ = _run("--timeout", "0.3", "--interval", "0", "--", "false")
    assert code == 1
    # floor 0.05s over a 0.3s window → ~7 attempts max, not thousands.
    assert verdict["attempts"] < 20


@pytest.mark.unit
def test_timeout_emits_events_jsonl_breadcrumb(tmp_path: Path):
    """Inside a synaptory project, a timeout writes a wait_timeout event."""
    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    code, verdict, _ = _run(
        "--timeout", "0.2", "--interval", "1", "--", "false",
        env_extra={"CLAUDE_PROJECT_DIR": str(tmp_path)},
    )
    assert code == 1
    events = orch / "events.jsonl"
    assert events.exists(), "wait_timeout breadcrumb missing from events.jsonl"
    rows = [json.loads(l) for l in events.read_text().splitlines() if l.strip()]
    assert any(r.get("event") == "wait_timeout" for r in rows)


# ── review-driven coverage (PR #85) ──────────────────────────────────────────


@pytest.mark.unit
def test_malformed_floor_env_does_not_crash():
    """P2 regression guard: a bad SYNAPTORY_WAIT_FLOOR_S must not crash the
    helper at import — agents read the JSON verdict on stdout, and a
    traceback would leave them with empty stdout + a nonzero exit
    indistinguishable from a real timeout."""
    code, verdict, stderr = _run(
        "--timeout", "5", "--", "true",
        env_extra={"SYNAPTORY_WAIT_FLOOR_S": "not-a-float"},
    )
    assert code == 0, f"stderr={stderr!r}"
    assert verdict.get("ok") is True, f"expected a verdict, got {verdict!r} stderr={stderr!r}"


@pytest.mark.unit
def test_gh_run_terminal_failure_stops_immediately(tmp_path: Path):
    """A completed+failed GH run is terminal — stop at once, don't poll to
    the deadline (the headline --gh-run behavior, previously untested)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'echo \'{"status":"completed","conclusion":"failure"}\'\n'
    )
    gh.chmod(0o755)
    code, verdict, stderr = _run(
        "--gh-run", "123", "--timeout", "30", "--interval", "1",
        env_extra={"PATH": f"{bindir}:/usr/bin:/bin"},
    )
    assert code == 1
    assert verdict["terminal"] is True
    assert verdict["ok"] is False
    assert verdict["attempts"] == 1  # stopped on first poll, not looped to 30s
    assert verdict["waited_s"] < 5


@pytest.mark.unit
def test_gh_run_success(tmp_path: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'echo \'{"status":"completed","conclusion":"success"}\'\n'
    )
    gh.chmod(0o755)
    code, verdict, _ = _run(
        "--gh-run", "123", "--timeout", "30",
        env_extra={"PATH": f"{bindir}:/usr/bin:/bin"},
    )
    assert code == 0
    assert verdict["ok"] is True


@pytest.mark.unit
def test_hanging_probe_respects_deadline():
    """A probe that hangs must not blow past --timeout. Re-review repro:
    `--timeout 1 -- sleep 30` previously waited to the per-check floor; now
    the per-attempt subprocess timeout is the remaining budget, so the wait
    stays within ~the deadline + the tiny probe floor."""
    code, verdict, _ = _run("--timeout", "1", "--interval", "1", "--", "sleep", "30")
    assert code == 1
    assert verdict["ok"] is False
    assert verdict["waited_s"] < 1.5, f"overshot the deadline: {verdict}"


@pytest.mark.unit
def test_subsecond_hanging_probe_honors_deadline():
    """The exact re-review failure: a sub-second timeout on a hanging command
    probe must not overshoot to the old 1.0s floor."""
    code, verdict, _ = _run("--timeout", "0.2", "--interval", "1", "--", "sleep", "10")
    assert code == 1
    assert verdict["ok"] is False
    assert verdict["waited_s"] < 0.75, f"overshot the sub-second deadline: {verdict}"


@pytest.mark.unit
def test_url_and_cmd_conflict_is_error():
    """Passing both a condition flag and a -- CMD is a usage error, not a
    silent winner."""
    proc = subprocess.run(
        [sys.executable, str(WAIT_FOR), "--url", "http://x", "--", "true"],
        capture_output=True, text=True, timeout=10,
        env={**os.environ, "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 2
    assert "not both" in proc.stderr
