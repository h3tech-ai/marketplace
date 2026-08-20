"""Layer 1 — gate_emitter (#125 PR 2).

Validates the enum-pre-check + subprocess invocation contract. Stubs
the `synaptory` CLI on PATH via a tiny shell wrapper that records the
exact argv it received — no live CP, no live binary needed.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

_HOOKS_LIB = Path(__file__).resolve().parents[2] / "hooks" / "lib"
if str(_HOOKS_LIB) not in sys.path:
    sys.path.insert(0, str(_HOOKS_LIB))

import gate_emitter as ge  # noqa: E402


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def stub_cli(tmp_path: Path, monkeypatch):
    """Drop a fake `synaptory` shim on PATH that records its argv.

    Each invocation appends its full quoted argv to ``log_file`` and
    exits 0 (success). Tests can mutate ``exit_code_file`` to make the
    shim simulate failure.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    exit_code = tmp_path / "exit_code"
    exit_code.write_text("0\n")
    shim = bindir / "synaptory"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%q " "$@" >> {log}\n'
        f'printf "\\n" >> {log}\n'
        f'exit "$(cat {exit_code})"\n'
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    # Make sure no stale SYNAPTORY_CLI_BIN override leaks in from the
    # developer's shell.
    monkeypatch.delenv("SYNAPTORY_CLI_BIN", raising=False)
    return {"log": log, "exit_code": exit_code, "bin": shim}


def _flags(argv: list[str]) -> dict[str, str]:
    """Walk argv and pair `--flag value` flags into a dict.

    Value-less flags (e.g. ``--quiet``) map to ``""``. This is more
    robust than ``zip(argv[2::2], argv[3::2])`` because that pairing
    breaks the moment a value-less flag is interleaved.
    """
    out: dict[str, str] = {}
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok.startswith("--"):
            nxt = argv[i + 1] if i + 1 < len(argv) else ""
            if nxt.startswith("--") or i + 1 >= len(argv):
                out[tok] = ""
                i += 1
            else:
                out[tok] = nxt
                i += 2
        else:
            i += 1
    return out


def _calls(log: Path) -> list[list[str]]:
    """Parse the recorded argv lines back into argv lists."""
    if not log.exists():
        return []
    out = []
    for line in log.read_text().splitlines():
        # `printf %q` quotes with single-quotes around tokens that
        # contain spaces; a tokenizer that splits on whitespace and
        # strips quote pairs is enough for our test args.
        toks: list[str] = []
        buf = ""
        in_quote = False
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == "'":
                in_quote = not in_quote
                i += 1
                continue
            if ch == " " and not in_quote:
                if buf:
                    toks.append(buf)
                    buf = ""
                i += 1
                continue
            if ch == "\\" and i + 1 < len(line):
                buf += line[i + 1]
                i += 2
                continue
            buf += ch
            i += 1
        if buf:
            toks.append(buf)
        out.append(toks)
    return out


# ─── Enum validation (refuses bad input before shelling out) ─────────────────


@pytest.mark.unit
@pytest.mark.parametrize("kwargs,desc", [
    ({"gate_type": "wrong", "target_type": "spec_version",
      "target_id": "s", "state": "opened"}, "gate_type"),
    ({"gate_type": "spec_ready", "target_type": "wrong",
      "target_id": "s", "state": "opened"}, "target_type"),
    ({"gate_type": "spec_ready", "target_type": "spec_version",
      "target_id": "s", "state": "wrong"}, "state"),
    ({"gate_type": "spec_ready", "target_type": "spec_version",
      "target_id": "s", "state": "opened",
      "decision_role": "wrong"}, "decision_role"),
])
def test_emit_rejects_invalid_enum_without_shelling_out(stub_cli, kwargs, desc):
    """Bad enum input must fail fast — no CLI call, return False."""
    ok = ge.emit_gate_event(**kwargs)
    assert ok is False, f"{desc}: emit must return False on invalid enum"
    assert not stub_cli["log"].exists(), (
        f"{desc}: emit must NOT invoke the CLI on invalid input"
    )


# ─── Happy path ──────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_emit_invokes_cli_with_canonical_argv(stub_cli):
    ok = ge.emit_gate_event(
        gate_type="spec_ready",
        target_type="spec_version",
        target_id="US-042",
        state="opened",
    )
    assert ok is True
    calls = _calls(stub_cli["log"])
    assert len(calls) == 1
    argv = calls[0]
    assert argv[:3] == ["telemetry", "gate-event", "--gate-type"]
    # Spot-check the flag→value pairing.
    pairs = _flags(argv)
    assert pairs["--gate-type"] == "spec_ready"
    assert pairs["--target-type"] == "spec_version"
    assert pairs["--target-id"] == "US-042"
    assert pairs["--state"] == "opened"
    assert "--quiet" in argv


@pytest.mark.unit
def test_emit_passes_optional_decision_fields(stub_cli):
    ge.emit_gate_event(
        gate_type="evidence_dod",
        target_type="spec_version",
        target_id="US-9",
        state="rejected",
        decided_by="po@h3t.co",
        decision_role="delivery_owner",
        reason="needs review",
    )
    argv = _calls(stub_cli["log"])[0]
    pairs = _flags(argv)
    assert pairs["--decided-by"] == "po@h3t.co"
    assert pairs["--decision-role"] == "delivery_owner"
    assert pairs["--reason"] == "needs review"


@pytest.mark.unit
def test_emit_omits_unset_optionals(stub_cli):
    """When optional fields are None, the corresponding flags must
    not appear in argv — otherwise the CLI sees an empty-string value."""
    ge.emit_gate_event(
        gate_type="spec_ready",
        target_type="spec_version",
        target_id="US-1",
        state="opened",
    )
    argv = _calls(stub_cli["log"])[0]
    assert "--decided-by" not in argv
    assert "--decision-role" not in argv
    assert "--reason" not in argv


# ─── Failure paths (never raise) ─────────────────────────────────────────────


@pytest.mark.unit
def test_emit_returns_false_on_non_zero_exit(stub_cli, capsys):
    """A failing CLI call must not raise — return False + warn."""
    stub_cli["exit_code"].write_text("1\n")
    ok = ge.emit_gate_event(
        gate_type="spec_ready", target_type="spec_version",
        target_id="x", state="opened",
    )
    assert ok is False
    err = capsys.readouterr().err
    assert "gate_emitter" in err


@pytest.mark.unit
def test_emit_returns_false_when_cli_missing(monkeypatch, tmp_path, capsys):
    """No CLI on PATH → silent skip (False), no exception."""
    monkeypatch.setenv("PATH", str(tmp_path))  # empty bindir
    monkeypatch.delenv("SYNAPTORY_CLI_BIN", raising=False)
    ok = ge.emit_gate_event(
        gate_type="spec_ready", target_type="spec_version",
        target_id="x", state="opened",
    )
    assert ok is False


# ─── Convenience wrappers ────────────────────────────────────────────────────


@pytest.mark.unit
def test_emit_spec_ready_opened_emits_one_per_spec(stub_cli):
    n = ge.emit_spec_ready_opened(["US-1", "US-2", "US-3"])
    assert n == 3
    calls = _calls(stub_cli["log"])
    assert len(calls) == 3
    targets = []
    for argv in calls:
        pairs = _flags(argv)
        assert pairs["--gate-type"] == "spec_ready"
        assert pairs["--state"] == "opened"
        targets.append(pairs["--target-id"])
    assert targets == ["US-1", "US-2", "US-3"]


@pytest.mark.unit
def test_emit_evidence_dod_accepted_carries_decision_metadata(stub_cli):
    ok = ge.emit_evidence_dod_accepted("US-7", "po@h3t.co")
    assert ok is True
    pairs = _flags(_calls(stub_cli["log"])[0])
    assert pairs["--gate-type"] == "evidence_dod"
    assert pairs["--state"] == "approved"
    assert pairs["--decided-by"] == "po@h3t.co"
    assert pairs["--decision-role"] == "delivery_owner"


@pytest.mark.unit
def test_emit_evidence_dod_rejected_carries_reason(stub_cli):
    ok = ge.emit_evidence_dod_rejected(
        "US-7", "po@h3t.co", "[needs-fix] missing input validation",
    )
    assert ok is True
    argv = _calls(stub_cli["log"])[0]
    pairs = _flags(argv)
    assert pairs["--state"] == "rejected"
    assert "needs-fix" in pairs["--reason"]


# ─── #181 — replay mismatch is observable centrally ──────────────────────────


@pytest.mark.unit
def test_emit_replay_mismatch_shape(stub_cli):
    """`evidence_dod / returned` with a machine-parseable reason, and NO
    human decision metadata (a replay mismatch is a machine detection, so
    attributing it to a `decision_role` would misreport who returned it)."""
    ok = ge.emit_evidence_dod_replay_mismatch(
        "US-42",
        check_id="tests_pass",
        command="pytest -q",
        attested_exit_code=0,
        replayed_exit_code=1,
    )
    assert ok is True
    pairs = _flags(_calls(stub_cli["log"])[0])
    assert pairs["--gate-type"] == "evidence_dod"
    assert pairs["--target-type"] == "spec_version"
    assert pairs["--target-id"] == "US-42"
    assert pairs["--state"] == "returned"
    assert "--decided-by" not in pairs
    assert "--decision-role" not in pairs
    reason = pairs["--reason"]
    assert reason.startswith(ge.REPLAY_MISMATCH_REASON_PREFIX)
    assert "check=tests_pass" in reason
    assert "attested_exit=0" in reason
    assert "replayed_exit=1" in reason
    assert "command=pytest -q" in reason


@pytest.mark.unit
def test_emit_replay_mismatch_truncates_to_cp_column_width(stub_cli):
    """`gate_events.reason` is String(2048). A long command must be
    truncated client-side (and the structured keys must survive, which is
    why the command is serialized last)."""
    ok = ge.emit_evidence_dod_replay_mismatch(
        "US-42",
        check_id="tests_pass",
        command="pytest " + ("-k verylongselector " * 400),
        attested_exit_code=0,
        replayed_exit_code=2,
    )
    assert ok is True
    reason = _flags(_calls(stub_cli["log"])[0])["--reason"]
    assert len(reason) <= 2048
    assert reason.endswith("...")
    assert reason.startswith(ge.REPLAY_MISMATCH_REASON_PREFIX)
    assert "check=tests_pass" in reason
    assert "replayed_exit=2" in reason


@pytest.mark.unit
def test_emit_replay_mismatch_tolerates_missing_command(stub_cli):
    """A replay failure with no command recorded still emits; the reason
    just carries the structured keys."""
    ok = ge.emit_evidence_dod_replay_mismatch(
        "US-42", check_id="build_succeeds",
        command=None, attested_exit_code=None, replayed_exit_code=None,
    )
    assert ok is True
    reason = _flags(_calls(stub_cli["log"])[0])["--reason"]
    assert "command=" not in reason
    assert "check=build_succeeds" in reason


@pytest.mark.unit
def test_emit_replay_mismatch_returns_false_when_cli_missing(tmp_path, monkeypatch):
    """No CLI on PATH is a silent skip returning False, never a raise —
    the DoD verdict must not depend on the CP being reachable."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.delenv("SYNAPTORY_CLI_BIN", raising=False)
    assert ge.emit_evidence_dod_replay_mismatch(
        "US-42", check_id="tests_pass", command="pytest",
        attested_exit_code=0, replayed_exit_code=1,
    ) is False


# ─── #199 — the pipeline's computed DoD verdict must ship ────────────────────


@pytest.mark.unit
def test_emit_evaluated_dod_shape(stub_cli):
    """`evidence_dod` with a machine-parseable reason carrying every check, and
    no human decision metadata (null decided_by is what /gate-queue filters on
    to keep machine evaluations out of the human queue)."""
    ok = ge.emit_evidence_dod_evaluated(
        "US-42",
        checks={"tests_pass": True, "build_succeeds": False,
                "no_critical_findings": True, "code_reviewed": None,
                "coverage_no_decrease": True},
        passed=False,
        tier="early",
    )
    assert ok is True
    pairs = _flags(_calls(stub_cli["log"])[0])
    assert pairs["--gate-type"] == "evidence_dod"
    assert pairs["--target-type"] == "spec_version"
    assert pairs["--target-id"] == "US-42"
    assert pairs["--state"] == "rejected"          # passed=False
    assert "--decided-by" not in pairs
    assert "--decision-role" not in pairs
    reason = pairs["--reason"]
    assert reason.startswith(ge.EVALUATED_DOD_REASON_PREFIX)
    assert "tier=early" in reason
    assert "tests_pass=true" in reason
    assert "build_succeeds=false" in reason
    assert "code_reviewed=none" in reason, "an unevaluated check must ship as none"


@pytest.mark.unit
def test_emit_evaluated_dod_passing_story_is_approved(stub_cli):
    ok = ge.emit_evidence_dod_evaluated(
        "US-43", checks={"tests_pass": True}, passed=True,
    )
    assert ok is True
    assert _flags(_calls(stub_cli["log"])[0])["--state"] == "approved"
