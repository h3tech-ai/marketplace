#!/usr/bin/env python3
# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
"""Bounded, token-free wait helper (Loop Engineering P3, epic #75).

Agent phase instructions used to say "deploy the stack and wait for
healthchecks" — prose the model interpreted by polling with repeated
tool calls (token cost) or by guessing (accuracy cost). This helper
moves the wait into code: one invocation polls until the condition
holds or a hard deadline passes, then prints a single JSON verdict.

Usage (one condition per invocation):

    wait_for.py [--timeout S] [--interval S] [--label TEXT] -- CMD [ARG...]
        Poll until CMD exits 0.

    wait_for.py --url http://localhost:3099/healthz [...]
        Poll until the URL answers with HTTP 2xx.

    wait_for.py --compose-healthy api [--compose-file F] [...]
        Poll until the docker-compose service reports healthy/running.

    wait_for.py --gh-run 123456 [...]
        Poll until the GitHub Actions run completes. A completed run
        with a non-success conclusion stops the wait immediately
        (terminal) — more polling can never fix a failed run.

Output: one JSON line on stdout —
    {"ok": bool, "label": str, "attempts": N, "waited_s": N,
     "timeout_s": N, "terminal": bool}
Exit 0 when the condition held, 1 otherwise.

Bounds: --timeout is hard-capped at 1800 s; --interval is clamped to
[1, 60] s. On timeout the helper best-effort emits a `wait_timeout`
event (events.jsonl via synaptory_logger + `synaptory telemetry
activity --kind wait_timeout`) so /reliability sees stuck waits;
telemetry can never affect the exit code.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _host_env():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _runtime_paths import find_lib_dir

    sys.path.insert(0, str(find_lib_dir()))
    import host_env

    return host_env


def _host_project_dir():
    return _host_env().project_dir()


def _host_plugin_root():
    return _host_env().plugin_root()


MAX_TIMEOUT_S = 1800.0
_INTERVAL_HARD_FLOOR_S = 0.05  # even the test hook can't busy-spin below this


def _interval_floor() -> float:
    """Interval floor, overridable via SYNAPTORY_WAIT_FLOOR_S (test hook).

    Parsed defensively: a malformed env value must never crash the helper at
    import — agents key phase decisions off the JSON verdict on stdout, and a
    traceback would leave them with empty stdout + a nonzero exit
    indistinguishable from a real timeout. Falls back to 1.0s, and never goes
    below the hard floor so a stray SYNAPTORY_WAIT_FLOOR_S=0 can't busy-spin
    subprocess spawns in a real environment."""
    try:
        val = float(os.environ.get("SYNAPTORY_WAIT_FLOOR_S", "1.0"))
    except (ValueError, TypeError):
        val = 1.0
    return max(_INTERVAL_HARD_FLOOR_S, val)


MIN_INTERVAL_S = _interval_floor()
MAX_INTERVAL_S = 60.0
# A single probe attempt is given the REMAINING global budget, never a fixed
# floor of seconds — a large fixed floor made a near-deadline probe overshoot
# --timeout (e.g. sleep 2 under --timeout 0.1 ran for 1.0s). This tiny floor
# only keeps the subprocess/socket timeout positive; it bounds overshoot to
# ~0.1s. For generous timeouts the budget is large, so probes are unaffected.
_PROBE_FLOOR_S = 0.1


class TerminalFailure(Exception):
    """The awaited thing failed in a way more polling can never fix."""


# ── condition checks ─────────────────────────────────────────────────────────


def _check_cmd(cmd: list[str], budget_s: float) -> bool:
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=max(budget_s, _PROBE_FLOOR_S),
        )
        return proc.returncode == 0
    except subprocess.TimeoutExpired:
        return False
    except OSError as exc:  # command not found / not executable
        raise TerminalFailure(f"cannot execute {cmd[0]}: {exc}") from exc


def _check_url(url: str, budget_s: float) -> bool:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=max(budget_s, _PROBE_FLOOR_S)) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _check_compose_healthy(service: str, compose_file: str | None, budget_s: float) -> bool:
    cmd = ["docker", "compose"]
    if compose_file:
        cmd += ["-f", compose_file]
    cmd += ["ps", "--format", "json", service]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=max(budget_s, _PROBE_FLOOR_S)
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if proc.returncode != 0 or not proc.stdout.strip():
        return False
    # `docker compose ps --format json` emits one JSON object per line
    # (newer versions) or a JSON array (older). Handle both.
    rows: list[dict] = []
    text = proc.stdout.strip()
    try:
        parsed = json.loads(text)
        rows = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        for line in text.splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    for row in rows:
        health = (row.get("Health") or "").lower()
        state = (row.get("State") or "").lower()
        if health:
            if health == "healthy":
                return True
        elif state == "running":  # no healthcheck defined → running is best signal
            return True
    return False


def _check_gh_run(run_id: str, budget_s: float) -> bool:
    try:
        proc = subprocess.run(
            ["gh", "run", "view", run_id, "--json", "status,conclusion"],
            capture_output=True,
            text=True,
            timeout=max(budget_s, _PROBE_FLOOR_S),
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if proc.returncode != 0:
        return False
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return False
    if data.get("status") != "completed":
        return False
    if data.get("conclusion") == "success":
        return True
    raise TerminalFailure(
        f"gh run {run_id} completed with conclusion={data.get('conclusion')!r}"
    )


# ── timeout telemetry (best-effort, never affects the verdict) ───────────────


def _emit_wait_timeout(label: str, waited_s: float, attempts: int) -> None:
    project_dir = _host_project_dir()
    # events.jsonl breadcrumb (only lands inside a synaptory project).
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _runtime_paths import find_lib_dir  # type: ignore

        sys.path.insert(0, str(find_lib_dir()))
        from synaptory_logger import emit as _log_emit  # type: ignore

        _log_emit(
            "wait_timeout",
            project_dir=project_dir,
            label=label,
            waited_s=round(waited_s, 1),
            attempts=attempts,
        )
    except Exception:
        pass
    # Control-plane activity event via the CLI outbox. The CLI is resolved
    # through the canonical channel-aware resolver, not `which("synaptory")` --
    # that named the PRODUCTION binary from a local or source-tree run, so the
    # activity event was addressed to prod and stranded in the outbox (#320).
    try:
        from _runtime_paths import find_lib_dir  # type: ignore

        sys.path.insert(0, str(find_lib_dir()))
        from gate_emitter import _resolve_cli  # type: ignore

        cli = _resolve_cli()
        if cli:
            subprocess.Popen(
                [
                    cli, "telemetry", "activity",
                    "--kind", "wait_timeout",
                    "--tool", "wait_for",
                    "--summary", f"{label}: no success after {waited_s:.0f}s ({attempts} attempts)",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except Exception:
        pass


# ── main ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Poll a condition until success or timeout; print one JSON verdict."
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="deadline in seconds (hard-capped at 1800). Note: when a probe "
        "hangs near the deadline, the final attempt still gets its per-check "
        "minimum budget, so the observed wait can exceed --timeout by up to "
        "that minimum (≤5s for --gh-run).",
    )
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--label", default=None)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--url")
    group.add_argument("--compose-healthy", metavar="SERVICE")
    group.add_argument("--gh-run", metavar="RUN_ID")
    parser.add_argument("--compose-file", default=None)
    parser.add_argument("cmd", nargs="*", help="command to poll (after --)")
    args = parser.parse_args(argv)

    # The condition flags are mutually exclusive with each other (argparse
    # group) AND with a positional command — otherwise a caller passing both
    # `--url … -- cmd` gets a silent winner. Make it an explicit error.
    if args.cmd and (args.url or args.compose_healthy or args.gh_run):
        parser.error(
            "pass EITHER a condition flag (--url/--compose-healthy/--gh-run) "
            "OR a -- CMD, not both"
        )

    timeout_s = min(max(args.timeout, 0.0), MAX_TIMEOUT_S)
    interval_s = min(max(args.interval, MIN_INTERVAL_S), MAX_INTERVAL_S)

    if args.url:
        label = args.label or f"url {args.url}"
        check = lambda budget: _check_url(args.url, budget)  # noqa: E731
    elif args.compose_healthy:
        label = args.label or f"compose service {args.compose_healthy} healthy"
        check = lambda budget: _check_compose_healthy(  # noqa: E731
            args.compose_healthy, args.compose_file, budget
        )
    elif args.gh_run:
        label = args.label or f"gh run {args.gh_run}"
        check = lambda budget: _check_gh_run(args.gh_run, budget)  # noqa: E731
    elif args.cmd:
        label = args.label or f"command {args.cmd[0]}"
        check = lambda budget: _check_cmd(args.cmd, budget)  # noqa: E731
    else:
        parser.error("nothing to wait for: pass --url/--compose-healthy/--gh-run or -- CMD")
        return 2  # unreachable; parser.error exits

    start = time.monotonic()
    deadline = start + timeout_s
    attempts = 0
    ok = False
    terminal = False
    while True:
        attempts += 1
        remaining = deadline - time.monotonic()
        try:
            if check(max(remaining, _PROBE_FLOOR_S)):
                ok = True
                break
        except TerminalFailure as exc:
            terminal = True
            print(f"wait_for: terminal failure — {exc}", file=sys.stderr)
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval_s, remaining))

    waited_s = time.monotonic() - start
    if not ok and not terminal:
        _emit_wait_timeout(label, waited_s, attempts)

    print(
        json.dumps(
            {
                "ok": ok,
                "label": label,
                "attempts": attempts,
                "waited_s": round(waited_s, 1),
                "timeout_s": round(timeout_s, 1),
                "terminal": terminal,
            }
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
