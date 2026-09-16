"""Layer 1 — DoD-time evidence replay (#179 E1).

Hypothesis: with `resilience.evidence_replay: enabled`, `evaluate_story_dod`
re-runs the satisfying receipt's verification_commands through the SAME
verification_runner the SubagentStop hook uses, and an attested exit code
that does not reproduce flips the check to failed (with a self-explaining
detail + an `evidence_replay_mismatch` event). Disabled (the default) keeps
today's attested-evidence behaviour byte-for-byte; unreplayable commands
keep their attested verdict (replay narrows trust, never widens it).

Commands use `test -e <file>` — on the evidence contract's allowlist,
side-effect-free, and its exit code is controlled by creating (or not
creating) the file.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from story_pipeline import (
    _gate_remediation,
    dod_gate_block_reason,
    evaluate_story_dod,
)


def _project(tmp_path: Path, *, replay: str | None) -> Path:
    cfg = "project_id: t\n"
    if replay is not None:
        cfg += f"resilience:\n  evidence_replay: {replay}\n"
    (tmp_path / ".synaptory.yaml").write_text(cfg)
    (tmp_path / "receipts").mkdir()
    return tmp_path


def _receipt(proj: Path, sid: str, role: str, command: str, exit_code: int = 0) -> None:
    (proj / "receipts" / f"{sid}-{role}.json").write_text(json.dumps({
        "task": "t",
        "agent": {"qe": "quality-engineer", "se": "software-engineer"}[role],
        "verification_commands": [{"command": command, "exit_code": exit_code,
                                   "summary": "s"}],
        "artifacts": ["x"],
    }))


@pytest.mark.unit
def test_replay_mismatch_flips_check(tmp_path: Path):
    """An attested exit 0 that replays non-zero fails the check and the gate,
    with a detail naming the command — and emits the mismatch event."""
    proj = _project(tmp_path, replay="enabled")
    (proj / "present.txt").write_text("x")
    _receipt(proj, "US-1", "se", "test -e present.txt")   # reproduces → holds
    _receipt(proj, "US-1", "qe", "test -e missing.txt")   # does NOT reproduce

    result = evaluate_story_dod(str(proj), "US-1", "early",
                                receipts_dir=str(proj / "receipts"))

    assert result["checks"]["build_succeeds"]["passed"] is True
    tp = result["checks"]["tests_pass"]
    assert tp["passed"] is False
    assert tp["replay_mismatch"] is True
    assert "evidence replay" in tp["detail"]
    assert "test -e missing.txt" in tp["detail"]
    assert tp["replay"]["passed"] is False
    assert result["passed"] is False

    events = (proj / ".synaptory" / ".orchestrator" / "events.jsonl").read_text()
    assert "evidence_replay_mismatch" in events


@pytest.mark.unit
def test_replay_mismatch_blocks_and_routes_to_qe(tmp_path: Path):
    """E1 must BLOCK, not just record: a tests_pass replay mismatch makes
    dod_gate_block_reason return a block reason (so reviewing→done redirects
    to blocked) and _gate_remediation routes it to QE."""
    proj = _project(tmp_path, replay="enabled")
    (proj / "present.txt").write_text("x")
    _receipt(proj, "US-1", "se", "test -e present.txt")   # build reproduces
    _receipt(proj, "US-1", "qe", "test -e missing.txt")   # tests do NOT

    result = evaluate_story_dod(str(proj), "US-1", "early",
                               receipts_dir=str(proj / "receipts"))
    reason = dod_gate_block_reason(result)
    assert reason is not None
    assert reason.startswith("DoD gate:")
    assert "tests_pass" in reason
    assert _gate_remediation({"blocked_reason": reason})["role"] == "qe"


@pytest.mark.unit
def test_replay_mismatch_build_routes_to_se(tmp_path: Path):
    """A build_succeeds replay mismatch routes remediation to SE, not QE."""
    proj = _project(tmp_path, replay="enabled")
    (proj / "present.txt").write_text("x")
    _receipt(proj, "US-1", "se", "test -e missing.txt")   # build does NOT reproduce
    _receipt(proj, "US-1", "qe", "test -e present.txt")   # tests reproduce

    result = evaluate_story_dod(str(proj), "US-1", "early",
                               receipts_dir=str(proj / "receipts"))
    assert result["checks"]["build_succeeds"]["replay_mismatch"] is True
    reason = dod_gate_block_reason(result)
    assert "build_succeeds" in reason
    assert _gate_remediation({"blocked_reason": reason})["role"] == "se"


@pytest.mark.unit
def test_no_replay_mismatch_no_block(tmp_path: Path):
    """A clean replay leaves dod_gate_block_reason returning None (no
    spurious block from the new branch)."""
    proj = _project(tmp_path, replay="enabled")
    (proj / "present.txt").write_text("x")
    _receipt(proj, "US-1", "se", "test -e present.txt")
    _receipt(proj, "US-1", "qe", "test -e present.txt")

    result = evaluate_story_dod(str(proj), "US-1", "early",
                               receipts_dir=str(proj / "receipts"))
    assert dod_gate_block_reason(result) is None


@pytest.mark.unit
def test_replay_disabled_keeps_attested_verdict(tmp_path: Path):
    """Default (key absent): the gate trusts attested exit codes exactly as
    before — no replay block on the check entries."""
    proj = _project(tmp_path, replay=None)
    _receipt(proj, "US-1", "se", "test -e missing.txt")  # attested 0, would fail
    _receipt(proj, "US-1", "qe", "test -e missing.txt")

    result = evaluate_story_dod(str(proj), "US-1", "early",
                                receipts_dir=str(proj / "receipts"))

    assert result["checks"]["tests_pass"]["passed"] is True
    assert result["checks"]["build_succeeds"]["passed"] is True
    assert "replay" not in result["checks"]["tests_pass"]
    assert result["passed"] is True


@pytest.mark.unit
def test_replay_reproducing_receipt_passes(tmp_path: Path):
    """Attested evidence that reproduces keeps the gate green and records the
    replay verdict on the check."""
    proj = _project(tmp_path, replay="enabled")
    (proj / "present.txt").write_text("x")
    _receipt(proj, "US-1", "se", "test -e present.txt")
    _receipt(proj, "US-1", "qe", "test -e present.txt")

    result = evaluate_story_dod(str(proj), "US-1", "early",
                                receipts_dir=str(proj / "receipts"))

    assert result["passed"] is True
    assert result["checks"]["tests_pass"]["replay"]["passed"] is True


@pytest.mark.unit
def test_replay_attested_nonzero_reproduced_holds(tmp_path: Path):
    """The comparison is attested-vs-replayed, not replayed-vs-zero: an
    honestly-attested exit 1 (which _evaluate_check already fails) must not
    be double-punished by replay — the check fails on the ATTESTED evidence
    and replay never runs (only passing checks are replayed)."""
    proj = _project(tmp_path, replay="enabled")
    _receipt(proj, "US-1", "se", "test -e missing.txt", exit_code=1)
    _receipt(proj, "US-1", "qe", "test -e missing.txt", exit_code=1)

    result = evaluate_story_dod(str(proj), "US-1", "early",
                                receipts_dir=str(proj / "receipts"))

    assert result["checks"]["tests_pass"]["passed"] is False
    assert "replay" not in result["checks"]["tests_pass"]


@pytest.mark.unit
def test_replay_unreplayable_command_keeps_attested(tmp_path: Path):
    """A command outside the runner's allowlist is marked unreplayable
    (REQ-E-003) — the attested exit code remains the DoD evidence and the
    check stays green (replay narrows trust, never widens it)."""
    proj = _project(tmp_path, replay="enabled")
    _receipt(proj, "US-1", "se", "totally-unknown-runner --check")
    _receipt(proj, "US-1", "qe", "totally-unknown-runner --check")

    result = evaluate_story_dod(str(proj), "US-1", "early",
                                receipts_dir=str(proj / "receipts"))

    tp = result["checks"]["tests_pass"]
    assert tp["passed"] is True
    assert tp["replay"]["passed"] is True  # unreplayable ≠ failed (non-strict)


# ─── #181 — the mismatch must also leave the machine ─────────────────────────


_PROD_URL = "https://synaptory.h3t.co"


@pytest.fixture
def stub_cli(tmp_path: Path, monkeypatch, stamp_runtime):
    """Fake `synaptory` on PATH that records argv, so the gate emission
    can be asserted without a live CP or a real binary.

    Stamps a production cp-url beside the shared runtime and has the shim answer
    `status` with the same URL: an unstamped tree now resolves no CLI at all
    rather than falling through to whatever is named `synaptory` (#320), so a
    test that expects an emission has to say which channel it stands in.
    `status` is the emitter's own identity probe and is not logged.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    shim = bindir / "synaptory"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = status ]; then\n'
        f'  printf "control_plane_url:  {_PROD_URL}\\n"\n'
        "  exit 0\n"
        "fi\n"
        f'printf "%s\\n" "$*" >> {log}\n'
        "exit 0\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.delenv("SYNAPTORY_CLI_BIN", raising=False)
    stamp_runtime(_PROD_URL)
    return log


@pytest.mark.unit
def test_replay_mismatch_emits_gate_event_to_cp(tmp_path: Path, stub_cli):
    """The local events.jsonl entry never leaves the laptop, so a mismatch
    must ALSO ship as `evidence_dod / returned`. Without this, #181's
    gating precondition cannot be evaluated from production."""
    proj = _project(tmp_path, replay="enabled")
    (proj / "present.txt").write_text("x")
    _receipt(proj, "US-1", "se", "test -e present.txt")
    _receipt(proj, "US-1", "qe", "test -e missing.txt")   # does NOT reproduce

    evaluate_story_dod(str(proj), "US-1", "early",
                       receipts_dir=str(proj / "receipts"))

    calls = stub_cli.read_text() if stub_cli.exists() else ""
    assert "telemetry gate-event" in calls
    assert "--gate-type evidence_dod" in calls
    assert "--state returned" in calls
    assert "--target-id US-1" in calls
    assert "evidence_replay_mismatch" in calls
    assert "check=tests_pass" in calls


@pytest.mark.unit
def test_clean_replay_emits_no_gate_event(tmp_path: Path, stub_cli):
    """No mismatch, no emission — the signal has to stay countable, so a
    passing replay must not add `returned` rows to the gate queue."""
    proj = _project(tmp_path, replay="enabled")
    (proj / "present.txt").write_text("x")
    _receipt(proj, "US-1", "se", "test -e present.txt")
    _receipt(proj, "US-1", "qe", "test -e present.txt")

    evaluate_story_dod(str(proj), "US-1", "early",
                       receipts_dir=str(proj / "receipts"))

    assert not stub_cli.exists() or "gate-event" not in stub_cli.read_text()


@pytest.mark.unit
def test_gate_emission_failure_does_not_break_the_verdict(tmp_path: Path,
                                                          monkeypatch):
    """A DoD verdict must never depend on the CP being reachable: with the
    CLI absent entirely, the mismatch still flips the check and blocks."""
    monkeypatch.setenv("PATH", str(tmp_path / "no-such-bin"))
    monkeypatch.delenv("SYNAPTORY_CLI_BIN", raising=False)
    proj = _project(tmp_path, replay="enabled")
    (proj / "present.txt").write_text("x")
    _receipt(proj, "US-1", "se", "test -e present.txt")
    _receipt(proj, "US-1", "qe", "test -e missing.txt")

    result = evaluate_story_dod(str(proj), "US-1", "early",
                                receipts_dir=str(proj / "receipts"))

    assert result["checks"]["tests_pass"]["replay_mismatch"] is True
    assert dod_gate_block_reason(result) is not None


# ─── #199 — ship_evaluated_dod ───────────────────────────────────────────────


@pytest.mark.unit
def test_ship_evaluated_dod_sends_all_five_checks(tmp_path: Path, stub_cli):
    """The computed verdict must leave the machine. Before #199 it was written
    to story["dod"] in local pipeline state and nothing shipped it, so /quality
    scored agent self-assessment (in practice one check of five)."""
    from story_pipeline import ship_evaluated_dod

    ship_evaluated_dod("US-9", {
        "passed": False,
        "intensity": "growing",
        "checks": {
            "tests_pass": {"passed": True},
            "build_succeeds": {"passed": True},
            "no_critical_findings": {"passed": True},
            "code_reviewed": {"passed": False},
            "coverage_no_decrease": {"passed": None},
        },
    })

    calls = stub_cli.read_text() if stub_cli.exists() else ""
    assert "telemetry gate-event" in calls
    assert "--gate-type evidence_dod" in calls
    assert "--state rejected" in calls
    assert "--target-id US-9" in calls
    assert "evidence_dod_evaluated" in calls
    assert "tests_pass=true" in calls
    assert "code_reviewed=false" in calls
    assert "coverage_no_decrease=none" in calls
    assert "tier=growing" in calls


@pytest.mark.unit
def test_ship_evaluated_dod_ignores_malformed_input(tmp_path: Path, stub_cli):
    """Telemetry must never be the thing that breaks a DoD evaluation."""
    from story_pipeline import ship_evaluated_dod

    for bad in (None, {}, {"checks": "not-a-dict"}, {"passed": True}):
        ship_evaluated_dod("US-9", bad)          # must not raise
    assert not stub_cli.exists() or "gate-event" not in stub_cli.read_text()


# ─── #435 — the credited channel carries the result and the derived class ────
#
# The control plane stopped crediting `payload.dod_check_results`, because a
# receipt payload is member-supplied and written by the subject of the check.
# That left this emission as the only channel a verdict is credited from, so
# the two facts only the payload could carry had to move here: the typed
# result (`criteria_gap_declared` has no boolean, and shipping `passed` alone
# made a declared gap indistinguishable from a check the tier never required)
# and the class `backing_evidence_class` DERIVED for the check.


@pytest.mark.unit
def test_ship_evaluated_dod_distinguishes_a_gap_from_an_unevaluated_check(
    tmp_path: Path, stub_cli
):
    from story_pipeline import ship_evaluated_dod

    ship_evaluated_dod("US-9", {
        "passed": False,
        "intensity": "mature",
        "checks": {
            # Required, evaluated, and no evidence it could be evaluated
            # from: its own state, and now its own wire token.
            "coverage_no_decrease": {
                "passed": None, "result": "criteria_gap_declared",
            },
            # Not required at this tier: no result stamped, no signal.
            "code_reviewed": {"passed": None},
            "tests_pass": {"passed": True, "result": "pass"},
        },
    })

    calls = stub_cli.read_text() if stub_cli.exists() else ""
    assert "coverage_no_decrease=gap" in calls, (
        "a declared gap must not ship as `none`; before #435 the two shared "
        "a token and the gap could only live in the payload"
    )
    assert "code_reviewed=none" in calls
    assert "tests_pass=true" in calls


@pytest.mark.unit
def test_ship_evaluated_dod_ships_the_derived_class_additively(
    tmp_path: Path, stub_cli
):
    """The class is appended as its own token, not fused into the verdict.

    `tests_pass=true:replayed` would have been shorter and would have made an
    older control plane drop the verdict entirely (its parser accepts only
    `true`/`false`), blanking the Evidence gate mid-upgrade. An unknown
    `class=` key is skipped instead.
    """
    from story_pipeline import ship_evaluated_dod

    ship_evaluated_dod("US-9", {
        "passed": True,
        "intensity": "mature",
        "checks": {
            "tests_pass": {
                "passed": True, "result": "pass", "evidence_class": "replayed",
            },
            "code_reviewed": {
                "passed": True, "result": "pass", "evidence_class": "attested",
            },
            # No class the pipeline could derive: depth of zero shows as zero.
            "build_succeeds": {"passed": True, "result": "pass"},
        },
    })

    calls = stub_cli.read_text() if stub_cli.exists() else ""
    assert "tests_pass=true" in calls
    assert "class=code_reviewed:attested,tests_pass:replayed" in calls
    assert "build_succeeds:" not in calls


@pytest.mark.unit
def test_ship_evaluated_dod_refuses_a_class_it_does_not_recognise(
    tmp_path: Path, stub_cli
):
    """`checks` and `evidence_class` reach this function from the pipeline's
    own evaluation, but the emitter is the last thing before the wire, and a
    class the control plane's own vocabulary does not contain must not be
    forwarded as if it did."""
    from story_pipeline import ship_evaluated_dod

    ship_evaluated_dod("US-9", {
        "passed": True,
        "intensity": "early",
        "checks": {
            "tests_pass": {
                "passed": True, "result": "pass", "evidence_class": "vouched",
            },
        },
    })

    calls = stub_cli.read_text() if stub_cli.exists() else ""
    assert "tests_pass=true" in calls
    assert "vouched" not in calls
    assert "class=" not in calls
