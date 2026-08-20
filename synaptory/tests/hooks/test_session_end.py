"""Layer 2 — `plugin/hooks/synaptory-session-end.sh` final-flush ship loop.

Hypothesis: the SubagentStop ship loop only fires when a subagent finishes.
Receipts written inline by the orchestrator (notably RA, which runs as a
conversational thinking partner without an `Agent()` spawn) would never ship
without a session-end fallback.

These tests pin the contract:
  * Unshipped receipts in `.synaptory/.orchestrator/receipts/` get a final
    `telemetry receipt --file <path>` call at session end.
  * Already-shipped receipts (with a `.shipped` sentinel newer than the JSON)
    are skipped — idempotent re-runs are safe.
  * The hook still exits 0 silently when no `.synaptory/` workspace exists
    (non-synaptory project).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def session_end_hook(plugin_root: Path) -> Path:
    return plugin_root / "hooks" / "synaptory-session-end.sh"


@pytest.fixture
def telemetry_log(tmp_path: Path) -> Path:
    """Stub-CLI sink for `telemetry` invocations. Empty file = no calls."""
    p = tmp_path / "telemetry.log"
    p.write_text("", encoding="utf-8")
    return p


def _stdin_payload(session_id: str = "sess-1", reason: str = "user") -> str:
    return json.dumps({"session_id": session_id, "reason": reason})


@pytest.mark.hook
def test_silent_exit_when_no_workspace(session_end_hook, hook_env, run_hook):
    """Non-crew project (no `.synaptory/`) → exit 0, no telemetry calls."""
    result = run_hook(session_end_hook, env=hook_env, stdin=_stdin_payload())
    assert result.returncode == 0


@pytest.mark.hook
def test_ships_unshipped_ra_receipt_on_session_end(
    session_end_hook, hook_env, run_hook, telemetry_log
):
    """RA inline carve-out: a receipt written without a SubagentStop fire
    must still get shipped when the session ends."""
    project = Path(hook_env["CLAUDE_PROJECT_DIR"])
    receipts = project / ".synaptory" / ".orchestrator" / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    ra_receipt = receipts / "sess-1-ra.json"
    ra_receipt.write_text(json.dumps({"role": "research-advisor"}), encoding="utf-8")

    env = {**hook_env, "SYNAPTORY_STUB_TELEMETRY_LOG": str(telemetry_log)}
    result = run_hook(session_end_hook, env=env, stdin=_stdin_payload())
    assert result.returncode == 0, f"hook failed: {result.stderr}"

    log = telemetry_log.read_text(encoding="utf-8")
    assert "telemetry receipt --file" in log, (
        f"expected telemetry receipt call, log was: {log!r}"
    )
    assert str(ra_receipt) in log, (
        f"expected receipt path {ra_receipt} in log, got: {log!r}"
    )
    # And the .shipped sentinel must be created so re-runs don't double-ship.
    assert (receipts / "sess-1-ra.json.shipped").exists(), (
        "missing .shipped sentinel after successful ship"
    )


@pytest.mark.hook
def test_skips_already_shipped_receipt(
    session_end_hook, hook_env, run_hook, telemetry_log
):
    """Sentinel newer than receipt → no second ship attempt (idempotent)."""
    import os
    import time

    project = Path(hook_env["CLAUDE_PROJECT_DIR"])
    receipts = project / ".synaptory" / ".orchestrator" / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    receipt = receipts / "US-042-se.json"
    receipt.write_text(json.dumps({"role": "software-engineer"}), encoding="utf-8")
    sentinel = receipts / "US-042-se.json.shipped"
    sentinel.write_text("", encoding="utf-8")
    # Force sentinel mtime to be strictly newer than the receipt — some
    # filesystems (notably ext4 with second-resolution timestamps) report
    # equal mtimes for files written milliseconds apart, and `[ "$f" -nt
    # "$sentinel" ]` returns true on equality, which would make this test
    # racy. Bump sentinel +1s explicitly.
    rcpt_mtime = receipt.stat().st_mtime
    os.utime(sentinel, (rcpt_mtime + 1, rcpt_mtime + 1))

    env = {**hook_env, "SYNAPTORY_STUB_TELEMETRY_LOG": str(telemetry_log)}
    result = run_hook(session_end_hook, env=env, stdin=_stdin_payload())
    assert result.returncode == 0

    log = telemetry_log.read_text(encoding="utf-8")
    # session-end itself always calls `telemetry session-end` first — that's
    # fine. What we care about is that no `telemetry receipt --file` for
    # this receipt was emitted.
    assert f"receipt --file {receipt}" not in log, (
        f"unexpectedly re-shipped sentineled receipt: {log!r}"
    )


@pytest.mark.hook
def test_reships_when_receipt_was_modified_after_ship(
    session_end_hook, hook_env, run_hook, telemetry_log
):
    """If the receipt was rewritten after the previous ship (sentinel is
    older than the JSON), the final-flush loop should pick it up again —
    matches the SubagentStop loop's behaviour."""
    import os

    project = Path(hook_env["CLAUDE_PROJECT_DIR"])
    receipts = project / ".synaptory" / ".orchestrator" / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    receipt = receipts / "US-042-se.json"
    sentinel = receipts / "US-042-se.json.shipped"
    sentinel.write_text("", encoding="utf-8")
    receipt.write_text(json.dumps({"role": "software-engineer"}), encoding="utf-8")
    # Force receipt mtime newer than sentinel.
    sent_mtime = sentinel.stat().st_mtime
    os.utime(receipt, (sent_mtime + 1, sent_mtime + 1))

    env = {**hook_env, "SYNAPTORY_STUB_TELEMETRY_LOG": str(telemetry_log)}
    result = run_hook(session_end_hook, env=env, stdin=_stdin_payload())
    assert result.returncode == 0

    log = telemetry_log.read_text(encoding="utf-8")
    assert f"receipt --file {receipt}" in log, (
        f"expected re-ship of modified receipt, log was: {log!r}"
    )
