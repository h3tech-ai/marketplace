"""Layer 2 — the per-dispatch subagent marker does not depend on a CLI (#512).

`synaptory-inject-protocols.sh` (SubagentStart) drops a marker under
`.synaptory/.orchestrator/subagent-markers/<agent_id>` for every
`synaptory:*` dispatch. Two readers consume it, and neither talks to a
control plane:

  * `synaptory-verify-receipt.sh` — which subagents owe a receipt, for which
    role, bound to which dispatch (#130, #340, #396).
  * `core/lib/skill_fetch_log.py` — which role a catalog retrieval belongs
    to, observed rather than guessed from the path (#446).

The write used to sit inside the hook's telemetry block, gated on a resolvable
CLI. An unstamped tree resolves NO CLI by design (#320, #425), so on a
source-tree sideload the marker was never written — and that is precisely the
run whose catalog retrieval takes the disk fallback and most needs
attribution. These tests pin the CLI-independence and, in both directions,
the receipt-selection behaviour the hoist changes.

What each test asserts is impossible is stated in its own docstring.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

_AGENT_ID = "cp512-agent-001"
_MARKER_REL = Path(".synaptory") / ".orchestrator" / "subagent-markers"


# ─── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def inject_hook(plugin_root: Path) -> Path:
    return plugin_root / "hooks" / "synaptory-inject-protocols.sh"


@pytest.fixture
def verify_hook(plugin_root: Path) -> Path:
    return plugin_root / "hooks" / "synaptory-verify-receipt.sh"


@pytest.fixture
def unstamped_env(plugin_root: Path, tmp_path: Path):
    """A source-tree sideload: no control-plane stamp, no CLI on $PATH.

    `hooks/lib/cp-url` in the source tree is still the build placeholder, so
    `_cp-url.sh` yields nothing and `_resolve-cli.sh` resolves no CLI. The one
    thing that can make this laptop disagree with CI is a gitignored
    `cp-url.local` written by `./synaptory deploy local`, so it is moved aside
    for the test and restored afterwards — the mirror image of what the
    `hook_env` fixture does to create a stamp.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    local_stamp = plugin_root / "hooks" / "lib" / "cp-url.local"
    parked = tmp_path / "cp-url.local.parked"
    had_stamp = local_stamp.exists()
    if had_stamp:
        shutil.move(str(local_stamp), str(parked))
    try:
        yield {
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path / "home"),
            "LANG": "C",
            "CLAUDE_PLUGIN_ROOT": str(plugin_root),
            "CLAUDE_PROJECT_DIR": str(project_dir),
            # The mid-session auth gate is CLI-driven and cannot fire without
            # one; set explicitly so the test states its own preconditions.
            "SYNAPTORY_AUTH_NO_GATE": "1",
        }
    finally:
        if had_stamp:
            shutil.move(str(parked), str(local_stamp))


# ─── helpers ─────────────────────────────────────────────────────────────────


def _start_stdin(agent_type: str, agent_id: str = _AGENT_ID) -> str:
    return json.dumps(
        {
            "agent_id": agent_id,
            "agent_type": agent_type,
            "session_id": "cp512-session",
            "transcript_path": "",
            "cwd": "/tmp",
            "hook_event_name": "SubagentStart",
        }
    )


def _stop_stdin(agent_id: str = _AGENT_ID) -> str:
    return json.dumps(
        {
            "description": "software-engineer",
            "agent_id": agent_id,
            "session_id": "cp512-session",
            "hook_event_name": "SubagentStop",
        }
    )


def _make_workspace(project_dir: Path) -> Path:
    """The minimal blocking-mode workspace `synaptory-verify-receipt.sh` reads.

    `Quality-Enforcement: strict` is what `mode_reader.py` turns into
    `autonomous=True`, the mode in which a missing receipt exits 2.
    """
    orch = project_dir / ".synaptory" / ".orchestrator"
    (orch / "receipts").mkdir(parents=True, exist_ok=True)
    (orch / "settings.md").write_text(
        "Engagement: structured\nQuality-Enforcement: strict\n", encoding="utf-8"
    )
    return orch


def _write_valid_receipt(project_dir: Path) -> Path:
    artifact = project_dir / "api" / "auth.py"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("# stub\n", encoding="utf-8")
    receipt = (
        project_dir / ".synaptory" / ".orchestrator" / "receipts" / "US-042-se.json"
    )
    receipt.write_text(
        json.dumps(
            {
                "story_id": "US-042",
                "role": "software-engineer",
                "backend": "claude",
                "model": "claude-sonnet-4-5",
                "artifacts": ["api/auth.py"],
                "verification_commands": [
                    {"command": "echo ok", "exit_code": 0, "summary": "smoke passes"}
                ],
                "metrics": {"files_changed": 1},
                "completed_at": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return receipt


def _write_invalid_receipt(project_dir: Path) -> Path:
    """Selectable but not valid.

    `story_id` and `role` are present because `dispatched_receipts.scoped_receipts`
    matches a receipt to a stop by the pair the receipt DECLARES (#340) — strip
    those and the file is invisible to selection, which would make the hook say
    `receipt_missing` and test the wrong thing. Everything the validator
    requires beyond the pair is absent.
    """
    receipt = (
        project_dir / ".synaptory" / ".orchestrator" / "receipts" / "US-042-se.json"
    )
    receipt.write_text(
        json.dumps({"story_id": "US-042", "role": "software-engineer"}),
        encoding="utf-8",
    )
    return receipt


def _marker(project_dir: Path, agent_id: str = _AGENT_ID) -> Path:
    return project_dir / _MARKER_REL / agent_id


# ─── the marker is written without a CLI ─────────────────────────────────────


@pytest.mark.hook
def test_unstamped_synaptory_dispatch_writes_a_marker(
    inject_hook, verify_hook, unstamped_env, run_hook
):
    """Impossible after this change: a `synaptory:*` dispatch that leaves no
    marker because no CLI resolved.

    Fails before the change — the write sat inside `if [[ -n "$cli" ]]`, so an
    unstamped tree produced nothing at all.
    """
    project_dir = Path(unstamped_env["CLAUDE_PROJECT_DIR"])
    _make_workspace(project_dir)

    result = run_hook(
        inject_hook,
        env=unstamped_env,
        stdin=_start_stdin("synaptory:software-engineer"),
    )
    assert result.returncode == 0, f"hook failed: {result.stderr}"
    assert _marker(project_dir).is_file(), (
        "no marker on an unstamped tree: receipt selection and #446 role "
        "attribution are both unavailable on the run that needs them most"
    )


@pytest.mark.hook
def test_unstamped_marker_carries_the_dispatched_role(
    inject_hook, unstamped_env, run_hook
):
    """Impossible after this change: a marker on an unstamped tree that records
    existence but not the role.

    Line 1 is the full `synaptory:<role>` agent type. `skill_fetch_log`
    reads it to report an OBSERVED role; without it #446's reader falls back
    to `inferred-from-name`, and `synaptory-verify-receipt.sh` cannot bind
    selection to one (work, role) pair (#396).

    Fails before the change: there is no marker to read.
    """
    project_dir = Path(unstamped_env["CLAUDE_PROJECT_DIR"])
    _make_workspace(project_dir)

    run_hook(
        inject_hook,
        env=unstamped_env,
        stdin=_start_stdin("synaptory:quality-engineer"),
    )
    lines = _marker(project_dir).read_text(encoding="utf-8").splitlines()
    assert lines and lines[0] == "synaptory:quality-engineer", (
        f"marker line 1 must be the dispatched agent type, got {lines[:1]}"
    )


@pytest.mark.hook
def test_unstamped_non_synaptory_dispatch_writes_no_marker(
    inject_hook, unstamped_env, run_hook
):
    """COUNTER-CASE — passes before the change too, and must keep passing.

    #130's guarantee is the `synaptory:` NAMESPACE gate, not the CLI gate.
    Hoisting the marker out of the CLI gate must not widen it: a Workflow
    worker, `Explore`, or another plugin's agent still owes no synaptory
    receipt and must still leave no marker. Impossible after this change: a
    non-`synaptory:` dispatch that starts owing a receipt.
    """
    project_dir = Path(unstamped_env["CLAUDE_PROJECT_DIR"])
    _make_workspace(project_dir)

    run_hook(inject_hook, env=unstamped_env, stdin=_start_stdin("general-purpose"))
    marker_dir = project_dir / _MARKER_REL
    present = sorted(p.name for p in marker_dir.glob("*")) if marker_dir.exists() else []
    assert present == [], f"non-synaptory dispatch left markers: {present}"


# ─── receipt selection, in both directions ───────────────────────────────────


@pytest.mark.hook
def test_unstamped_synaptory_stop_blocks_on_a_missing_receipt(
    inject_hook, verify_hook, unstamped_env, run_hook
):
    """Impossible after this change: a `synaptory:*` agent finishing an
    unstamped run with no receipt and no complaint.

    This is the direction the ticket asked to establish before changing
    anything. Before the change SubagentStop found no marker, treated the
    dispatch as somebody else's subagent and exited 0 — the fail-closed
    evidence contract (#75) was simply off on every source-tree run. Exit 2
    is the same code every stamped install already returns here.
    """
    project_dir = Path(unstamped_env["CLAUDE_PROJECT_DIR"])
    _make_workspace(project_dir)

    run_hook(
        inject_hook,
        env=unstamped_env,
        stdin=_start_stdin("synaptory:software-engineer"),
    )
    stop = run_hook(verify_hook, env=unstamped_env, stdin=_stop_stdin())
    assert stop.returncode == 2, (
        f"expected the blocking exit 2, got {stop.returncode}: {stop.stderr}"
    )
    assert "without writing a receipt" in stop.stderr


@pytest.mark.hook
def test_unstamped_synaptory_stop_rejects_an_invalid_receipt(
    inject_hook, verify_hook, unstamped_env, run_hook
):
    """Impossible after this change: a receipt missing every required field
    passing an unstamped run.

    The other direction, and the one an exit code alone cannot show for a
    VALID receipt: before the change the hook exited 0 whether the receipt
    was good, bad, or absent, because it never reached the validator. A
    deliberately malformed receipt separates "validated and passed" from
    "never looked".
    """
    project_dir = Path(unstamped_env["CLAUDE_PROJECT_DIR"])
    _make_workspace(project_dir)

    run_hook(
        inject_hook,
        env=unstamped_env,
        stdin=_start_stdin("synaptory:software-engineer"),
    )
    # After the dispatch starts: `scoped_receipts` treats a receipt older than
    # the marker as somebody else's output.
    _write_invalid_receipt(project_dir)
    stop = run_hook(verify_hook, env=unstamped_env, stdin=_stop_stdin())
    assert stop.returncode == 2, (
        f"invalid receipt accepted on an unstamped tree: rc={stop.returncode}"
    )
    assert "Receipt validation failed" in stop.stderr


@pytest.mark.hook
def test_unstamped_synaptory_stop_accepts_a_valid_receipt_after_validating_it(
    inject_hook, verify_hook, unstamped_env, run_hook
):
    """Impossible after this change: an unstamped run whose valid receipt is
    waved through unread.

    Exit 0 is what the pre-change hook also produced, so the exit code is not
    the evidence — the `receipt_selection` event is. `synaptory-verify-receipt.sh`
    logs it only after selection actually ran (#340), which the pre-change hook
    never reached on an unstamped tree.
    """
    project_dir = Path(unstamped_env["CLAUDE_PROJECT_DIR"])
    orch = _make_workspace(project_dir)

    run_hook(
        inject_hook,
        env=unstamped_env,
        stdin=_start_stdin("synaptory:software-engineer"),
    )
    _write_valid_receipt(project_dir)
    stop = run_hook(verify_hook, env=unstamped_env, stdin=_stop_stdin())
    assert stop.returncode == 0, f"valid receipt rejected: {stop.stderr}"

    events = orch / "events.jsonl"
    assert events.is_file(), "no events.jsonl: the hook never got as far as selection"
    body = events.read_text(encoding="utf-8")
    assert "receipt_selection" in body, (
        "receipt_selection was never logged, so the receipt was never selected "
        "or validated — exit 0 here would mean 'skipped', not 'passed'"
    )


@pytest.mark.hook
def test_unstamped_marker_is_consumed_by_the_stop_hook(
    inject_hook, verify_hook, unstamped_env, run_hook
):
    """Impossible after this change: a marker that outlives its dispatch.

    SubagentStop removes the marker it matched. A leaked marker would make a
    later, unrelated `sole-live-marker` attribution in `skill_fetch_log` point
    at a dispatch that ended, which is a wrong observation rather than a
    missing one.
    """
    project_dir = Path(unstamped_env["CLAUDE_PROJECT_DIR"])
    _make_workspace(project_dir)

    run_hook(
        inject_hook,
        env=unstamped_env,
        stdin=_start_stdin("synaptory:software-engineer"),
    )
    _write_valid_receipt(project_dir)
    assert _marker(project_dir).is_file()
    run_hook(verify_hook, env=unstamped_env, stdin=_stop_stdin())
    assert not _marker(project_dir).exists(), "marker leaked past SubagentStop"


# ─── the execution envelope travels with the same parse ──────────────────────


@pytest.mark.hook
def test_unstamped_synaptory_dispatch_receives_the_execution_envelope(
    inject_hook, unstamped_env, run_hook, parse_output
):
    """Impossible after this change: an unstamped run that enforces the
    evidence contract on an agent without ever showing it to the agent.

    #163's envelope is rendered from `agent_type`, which was parsed only
    inside the telemetry gate. Before the change the unstamped path passed
    `--agent-type ""` and appended nothing, so SubagentStop's contract was
    enforced silently. Fails before the change.
    """
    project_dir = Path(unstamped_env["CLAUDE_PROJECT_DIR"])
    _make_workspace(project_dir)

    result = run_hook(
        inject_hook,
        env=unstamped_env,
        stdin=_start_stdin("synaptory:software-engineer"),
    )
    assert result.returncode == 0, f"hook failed: {result.stderr}"
    parsed = parse_output(result.stdout)
    assert parsed is not None, "hook emitted no additionalContext at all"
    ctx = parsed["additional_context"]
    assert "EXECUTION ENVELOPE" in ctx.upper(), (
        "no execution envelope on an unstamped dispatch; the agent is held to "
        "a contract it was never shown"
    )


# ─── the stamped path is unchanged ───────────────────────────────────────────


@pytest.mark.hook
def test_stamped_dispatch_still_emits_telemetry_and_opens_a_span(
    inject_hook, hook_env, stub_cli, run_hook, tmp_path: Path
):
    """REGRESSION GUARD — passes before the change and must keep passing.

    The hoist moves the marker out of the telemetry block; it must not move
    telemetry with it. On a stamped tree the CLI is still invoked for
    `telemetry subagent-start` and a span `start` is still opened. Impossible
    after this change: a stamped dispatch that stops shipping its span.
    """
    telemetry_log = tmp_path / "telemetry.log"
    env = {**hook_env, "SYNAPTORY_STUB_TELEMETRY_LOG": str(telemetry_log)}
    project_dir = Path(env["CLAUDE_PROJECT_DIR"])
    _make_workspace(project_dir)

    result = run_hook(
        inject_hook, env=env, stdin=_start_stdin("synaptory:software-engineer")
    )
    assert result.returncode == 0, f"hook failed: {result.stderr}"

    assert telemetry_log.is_file(), "the CLI was never invoked for telemetry"
    assert "subagent-start" in telemetry_log.read_text(encoding="utf-8")

    spans = sorted((project_dir / ".synaptory" / ".orchestrator" / "otel").glob("spans-*.jsonl"))
    assert spans, "no span file written on a stamped dispatch"
    events = [json.loads(line) for line in spans[0].read_text().splitlines() if line]
    assert any(e.get("event") == "start" for e in events), (
        f"no span start event: {events}"
    )
