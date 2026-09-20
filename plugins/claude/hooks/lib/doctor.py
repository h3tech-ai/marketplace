#!/usr/bin/env python3
# Copyright 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
"""synaptory doctor — CLI-session diagnostic.

Auth is owned by the synaptory CLI (SAD: the plugin does not parse access
tokens). This report shells out to `synaptory whoami --check` the same way
SessionStart does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import host_env


def _python_info() -> str:
    v = sys.version_info
    return f"{v.major}.{v.minor}.{v.micro} ({sys.executable})"


def _run_cli(args: list[str]) -> tuple[int | None, str]:
    cli_name = "synaptory-local" if host_env.local_plugin_runtime() else "synaptory"
    override = os.environ.get("SYNAPTORY_CLI_BIN", "").strip()
    cli = override if override and os.path.isfile(override) and os.access(override, os.X_OK) else shutil.which(cli_name)
    if not cli:
        return None, f"{cli_name} CLI not found or build identity does not match this plugin"
    try:
        proc = subprocess.run(
            [cli, *args],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return proc.returncode, out
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def _loop_lines(project_dir: str) -> tuple[list[str], bool]:
    """The continuation loop's standing state, as report lines + a finding flag.

    `#820`. Doctor validated YAML, plugin settings, CLI auth, the control-plane
    project and workspace hygiene, and said nothing about the single setting
    that most changes how a delivery behaves. A project ran three Cycles to
    Checkpoint with autonomy switched off and attributed the idling to the
    model.

    IT ASKS `loop_engine`, it does not re-derive. A second copy of the three
    conditions here is how doctor would come to report "enabled" about an
    engine that stops -- the exact failure being fixed, one level up.

    An unimportable engine is reported as UNKNOWN rather than as healthy: this
    check exists because silence read as health.
    """
    try:
        import loop_engine  # noqa: PLC0415

        status = loop_engine.loop_status(project_dir)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        return ([f"Loop:          UNKNOWN (loop_engine unavailable: {exc})"], False)
    if status["enabled"]:
        return (["Loop:          enabled (sessions keep driving the pipeline)"], False)
    # `not_structured` is a MODE, not a misconfiguration: interactive is
    # user-in-the-loop by design. Reported either way, flagged only when the
    # loop was switched off under a project that otherwise expects autonomy.
    finding = status["reason"] != "not_structured"
    label = "DISABLED" if finding else "not driving"
    return (
        [
            f"Loop:          {label} — {status['detail']}",
            f"               ({status['reason']}) {status['remedy']}",
            "               Work Units will sit queued with no attempts "
            "authorized and nothing else will say so.",
        ],
        finding,
    )


def run(project_dir: str | None = None) -> int:
    # `host_env.project_dir`, never the Claude-specific env name directly:
    # this module is projected wholesale into the Cursor and Codex packages,
    # where that variable does not exist.
    # `test_shared_runtime_reads_no_host_specific_name_directly` enforces it.
    project_dir = project_dir or host_env.project_dir(os.getcwd())
    loop_lines, loop_finding = _loop_lines(project_dir)
    lines = [
        "",
        "synaptory doctor",
        "================",
        f"Python:        {_python_info()}",
        *loop_lines,
    ]
    rc, out = _run_cli(["whoami", "--check"])
    if rc is None:
        cli_name = "synaptory-local" if host_env.local_plugin_runtime() else "synaptory"
        lines += [
            "CLI:           NOT FOUND",
            "",
            f"Install the {cli_name} CLI, then run `{cli_name} login`.",
            "Doctor no longer reads plugin access-token files — the CLI is",
            "the only auth path.",
            "",
        ]
        print("\n".join(lines))
        return 1
    lines.append(
        f"whoami --check: {'ok' if rc == 0 else 'FAILED'} (exit {rc})"
    )
    if out:
        lines.append(out)
    if rc != 0:
        cli_name = "synaptory-local" if host_env.local_plugin_runtime() else "synaptory"
        lines += [
            "",
            f"Fix: `{cli_name} login` then re-run doctor.",
            "",
        ]
        print("\n".join(lines))
        return 1
    _, who = _run_cli(["whoami"])
    if who:
        lines.append(who)
    if loop_finding:
        # A FINDING, not a note (#820): auth being fine while autonomy is off
        # is precisely the state that read as healthy for three Cycles.
        lines += [
            "",
            "1 finding: the continuation loop is disabled (see Loop above).",
            "",
        ]
        print("\n".join(lines))
        return 1
    lines += ["", "All checks passed.", ""]
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(run())
