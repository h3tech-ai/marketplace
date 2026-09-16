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
from types import SimpleNamespace

import pytest

_HOOKS_LIB = Path(__file__).resolve().parents[2] / "hooks" / "lib"
if str(_HOOKS_LIB) not in sys.path:
    sys.path.insert(0, str(_HOOKS_LIB))

import gate_emitter as ge  # noqa: E402


# ─── Fixtures ────────────────────────────────────────────────────────────────


PROD_URL = "https://synaptory.h3t.co"


@pytest.fixture
def stub_cli(tmp_path: Path, monkeypatch, stamp_runtime):
    """Drop a fake `synaptory` shim on PATH that records its argv.

    Each invocation appends its full quoted argv to ``log_file`` and
    exits 0 (success). Tests can mutate ``exit_code_file`` to make the
    shim simulate failure.

    The fixture stamps a PRODUCTION cp-url into the runtime stamp dir and has
    the shim answer `status` with the same URL, so the emitter can see that the
    tree and the binary agree on a channel. That used to be implicit: with no
    stamp anywhere the resolver fell through to whatever was named `synaptory`,
    which is exactly the #320 defect, so the fixture now has to say which
    channel it is standing in. `status` is answered without logging, since it is
    the emitter's own probe rather than an emission under test.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    exit_code = tmp_path / "exit_code"
    exit_code.write_text("0\n")
    shim = bindir / "synaptory"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = status ]; then\n"
        f'  printf "control_plane_url:  {PROD_URL}\\n"\n'
        "  exit 0\n"
        "fi\n"
        f'printf "%q " "$@" >> {log}\n'
        f'printf "\\n" >> {log}\n'
        f'exit "$(cat {exit_code})"\n'
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    # Make sure no stale SYNAPTORY_CLI_BIN override leaks in from the
    # developer's shell.
    monkeypatch.delenv("SYNAPTORY_CLI_BIN", raising=False)
    stamp_runtime(PROD_URL)
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
def test_emit_runs_cli_from_bound_project(stub_cli, monkeypatch, tmp_path: Path):
    """MCP servers run from plugin roots; gate telemetry must bind cwd to the
    governed project so the CLI can discover its top-level project_id."""
    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):
        # The resolver probes `<cli> status` for the binary's channel identity;
        # answer it here so this test stays about cwd binding.
        if len(command) > 1 and command[1] == "status":
            return SimpleNamespace(
                returncode=0,
                stdout="control_plane_url:  %s\n" % PROD_URL,
                stderr="",
            )
        seen["command"] = command
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ge.subprocess, "run", fake_run)
    project = tmp_path / "project"
    project.mkdir()

    assert ge.emit_gate_event(
        gate_type="evidence_dod",
        target_type="spec_version",
        target_id="US-LOCAL",
        state="approved",
        project_dir=str(project),
    ) is True
    assert seen["cwd"] == str(project)


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


@pytest.mark.unit
def test_production_plugin_refuses_explicit_loopback_cli(monkeypatch, tmp_path):
    plugin = tmp_path / "plugin"
    stamp = plugin / "hooks" / "lib" / "cp-url"
    stamp.parent.mkdir(parents=True)
    stamp.write_text("https://synaptory.h3t.co\n", encoding="utf-8")
    local_cli = tmp_path / "synaptory-local"
    local_cli.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = status ]; then\n"
        "  printf 'control_plane_url:  http://localhost:8080\\n'\n"
        "fi\n",
        encoding="utf-8",
    )
    local_cli.chmod(0o755)
    monkeypatch.setenv("SYNAPTORY_PLUGIN_ROOT", str(plugin))
    monkeypatch.setenv("SYNAPTORY_CLI_BIN", str(local_cli))

    assert ge._resolve_cli() is None


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


# ─── #435 — the wire has to carry what the payload channel used to ───────────


@pytest.mark.unit
def test_a_gap_gets_its_own_token_and_a_typed_result_is_accepted(stub_cli):
    """`criteria_gap_declared` is not a boolean, so the boolean-only signature
    could not express it and it shipped as `none` -- the same token as "the
    tier did not require this check". Two different facts sharing one
    representation is the rule #521 wrote down, and it mattered here because
    the payload channel was the only one that could tell them apart, and #435
    stops crediting the payload channel.
    """
    assert ge.emit_evidence_dod_evaluated(
        "US-44",
        checks={
            "tests_pass": "pass",
            "build_succeeds": "fail",
            "coverage_no_decrease": "criteria_gap_declared",
            "code_reviewed": None,
        },
        passed=False,
        tier="mature",
    ) is True
    reason = _flags(_calls(stub_cli["log"])[0])["--reason"]
    assert "tests_pass=true" in reason, "a typed pass ships in the legacy spelling"
    assert "build_succeeds=false" in reason
    assert "coverage_no_decrease=gap" in reason
    assert "code_reviewed=none" in reason


@pytest.mark.unit
def test_the_derived_class_rides_an_additive_token(stub_cli):
    """Additive on purpose: a control plane that predates `class=` skips the
    unknown key and still parses every verdict, where fusing the class into
    the value (`tests_pass=true:replayed`) would have made its parser drop
    them and blank the Evidence gate mid-upgrade."""
    assert ge.emit_evidence_dod_evaluated(
        "US-45",
        checks={"tests_pass": True, "build_succeeds": True},
        classes={"tests_pass": "replayed"},
        passed=True,
    ) is True
    reason = _flags(_calls(stub_cli["log"])[0])["--reason"]
    assert "tests_pass=true" in reason
    assert "class=tests_pass:replayed" in reason
    assert "build_succeeds:" not in reason


@pytest.mark.unit
def test_a_class_on_an_unevaluated_or_gapped_check_is_not_shipped(stub_cli):
    """A class describes evidence that produced a verdict. A check with no
    verdict has no evidence to classify, so a class attached to one is a
    contradiction and is dropped rather than forwarded."""
    assert ge.emit_evidence_dod_evaluated(
        "US-46",
        checks={
            "tests_pass": None,
            "coverage_no_decrease": "criteria_gap_declared",
        },
        classes={"tests_pass": "replayed", "coverage_no_decrease": "attested"},
        passed=False,
    ) is True
    reason = _flags(_calls(stub_cli["log"])[0])["--reason"]
    assert "class=" not in reason


@pytest.mark.unit
def test_an_unrecognised_verdict_ships_as_no_signal(stub_cli):
    """Fail-closed at the wire: a value this emitter cannot name is no
    signal, never a pass."""
    assert ge.emit_evidence_dod_evaluated(
        "US-47",
        checks={"tests_pass": "probably", "build_succeeds": 7},
        passed=True,
    ) is True
    reason = _flags(_calls(stub_cli["log"])[0])["--reason"]
    assert "tests_pass=none" in reason
    assert "build_succeeds=none" in reason


def test_the_emitters_evidence_classes_equal_the_runtime_vocabulary():
    """The local literal in `gate_emitter` claims to be kept equal to
    `runtime_contracts.EVIDENCE_CLASSES`; this is what makes that true rather
    than aspirational. It is a local literal for the reason every other
    cross-module reach in that file is lazy: gate emission must not fail to
    render because a sibling module is missing from a partial install."""
    from runtime_contracts import EVIDENCE_CLASSES

    assert tuple(ge._EVIDENCE_CLASSES) == tuple(EVIDENCE_CLASSES)


# ── the emitter's vocabulary must equal the ORM's (#305) ───────────────────


def test_gate_and_target_types_match_the_control_plane_orm():
    """These drifted once, silently, and the schema shipped unusable.

    PR #317 widened the server side so dependency events could ride `GateEvent`
    rather than need their own table (`gate_type: dependency`,
    `target_type: work_unit`/`cycle`), complete with the idempotency index the
    ledger's content-addressed `event_id` maps onto. `gate_emitter` lagged and
    refused all three client-side, so `api` accepted a vocabulary nothing could
    produce -- the same shape of defect as `workstream_id` having columns and
    partial indexes with no field on any request model.

    Read from the ORM SOURCE rather than imported: `synaptory_api` is not on the
    plugin test path, and an `importorskip` here would be a test that never runs
    guarding the exact drift it exists to catch.
    """
    import ast

    models = (
        Path(__file__).resolve().parents[3] / "api" / "synaptory_api" / "models.py"
    )
    if not models.is_file():  # pragma: no cover - plugin-only checkout
        pytest.skip("api/synaptory_api/models.py not present in this checkout")

    tree = ast.parse(models.read_text(encoding="utf-8"))
    gate_event = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "GateEvent"
    )
    declared = {}
    for node in gate_event.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in ("GATE_TYPES", "TARGET_TYPES", "STATES"):
                declared[name] = {
                    e.value for e in node.value.elts
                    if isinstance(e, ast.Constant)
                }

    assert declared, "could not read GateEvent's enums out of models.py"
    assert set(ge._GATE_TYPES) == declared["GATE_TYPES"]
    assert set(ge._TARGET_TYPES) == declared["TARGET_TYPES"]
    assert set(ge._STATES) == declared["STATES"]


def test_a_dependency_gate_event_is_no_longer_refused_locally():
    """The validation is what refused it; ensure the widening actually landed
    rather than only the comment."""
    assert "dependency" in ge._GATE_TYPES
    assert {"work_unit", "cycle"} <= set(ge._TARGET_TYPES)


def test_a_coordination_release_still_uses_the_existing_release_gate():
    """#305 adds no new gate type. `GateEvent.target_id` is a plain String with
    no foreign key, so `RELEASE-{seq}` is accepted as-is and stays
    distinguishable from a child Cycle's own `ACCEPTANCE-{n}`."""
    import inspect

    src = inspect.getsource(ge.emit_release_approved)
    assert 'gate_type="release"' in src
    assert 'target_type="release"' in src


# ── the dependency ledger reaches the control plane (#305) ─────────────────


def test_a_dependency_event_can_be_emitted():
    """PR #317 widened `GateEvent` for exactly this and nothing ever emitted it.

    The result: a reader could see every gate around a Cycle and still not learn
    what satisfied a dependency, or when. Measured on a full 13-clone run — 0
    dependency rows before, and the cross-Cycle unblock invisible.
    """
    import inspect

    src = inspect.getsource(ge.emit_dependency_published)
    assert 'gate_type="dependency"' in src
    assert 'state="approved"' in src


def test_the_publish_path_reports_to_the_control_plane():
    """Publication writes an event another clone depends on, and it must be
    visible to the CP -- otherwise a reader sees every gate around a Cycle and
    still cannot learn what satisfied a dependency, or when.

    ONE PATH NOW; this was `test_both_publish_paths_...`. The cross-Cycle half
    lived in `coordination_cycle.py`, which `SPD-194` retires: work that cannot
    be ordered is co-admitted to ONE Cycle, so there is no second Cycle to
    publish a dependency to and no `target_type="cycle"` event to emit. The
    intra-Cycle path in `spq_ledger` is the whole surface, and the module's
    absence is asserted so the arm cannot stay dropped if it comes back.
    """
    from pathlib import Path

    core = Path(__file__).resolve().parents[3] / "core" / "lib"
    src = (core / "spq_ledger.py").read_text(encoding="utf-8")
    assert "emit_dependency_published" in src
    assert 'target_type="work_unit"' in src
    assert not (core / "coordination_cycle.py").exists(), (
        "coordination_cycle.py is back, so there is a second publish path "
        "again -- restore the cross-Cycle arm of this assertion"
    )


def test_emission_never_blocks_a_ceremony():
    """Git is authoritative; the CP observes. A failed emit is a reporting gap,
    never a delivery one — so every call site swallows its own errors.

    `coordination_cycle.py` left the module list with the Coordination Cycle,
    and the manifest call site MOVED: `spq_state_machine.open_cycle` no longer
    emits, the `manifest_emitter` sweep does. The manifest half is therefore
    asserted on the FUNCTIONS that must not raise rather than on a text window
    around a name -- a window is positional, and the old loop skipped any
    module whose marker had stopped matching, so three retired markers would
    have passed while checking nothing.
    """
    import inspect
    from pathlib import Path

    import manifest_emitter as me

    core = Path(__file__).resolve().parents[3] / "core" / "lib"
    src = (core / "spq_ledger.py").read_text(encoding="utf-8")
    assert "emit_dependency_published" in src, (
        "spq_ledger no longer emits a dependency event; this test is scanning "
        "for something that is gone"
    )
    at = src.index("emit_dependency_published")
    assert "except Exception" in src[at - 400 : at + 700]

    # The sweep and the single ship, each swallowing its own failures. The
    # sweep's contract says so in as many words: "it never raises, for the same
    # reason the single emission never did".
    for func in (me.resend_pending, me._ship):
        body = inspect.getsource(func)
        assert "except Exception" in body, func.__name__


# ─── #320 — which control plane a gate event is addressed to ─────────────────


def _channel_shim(path: Path, url: str) -> Path:
    """A CLI shim that reports `url` as its own control-plane identity."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = status ]; then\n'
        f'  printf "control_plane_url:  {url}\\n"\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


@pytest.mark.unit
def test_local_stamped_source_tree_resolves_the_local_cli(
    monkeypatch, tmp_path, stamp_runtime
):
    """The reported bug, in one test.

    `./synaptory deploy local` writes `hooks/lib/cp-url.local`, which through
    the plugin-claude symlink is `core/lib/cp-url.local` — right beside this
    module. The emitter only read the env vars, which a source-tree run does not
    set, so it resolved the PRODUCTION binary and addressed every gate event to
    https://synaptory.h3t.co.
    """
    bindir = tmp_path / "bin"
    _channel_shim(bindir / "synaptory", "https://synaptory.h3t.co")
    local = _channel_shim(bindir / "synaptory-local", "http://localhost:8080")
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.delenv("SYNAPTORY_CLI_BIN", raising=False)
    stamp_runtime("http://localhost:8080", local=True)

    assert ge._resolve_cli() == str(local)


@pytest.mark.unit
def test_unstamped_source_tree_refuses_to_address_production(
    monkeypatch, tmp_path, capsys, stamp_runtime
):
    """No stamp is not production. A tree that cannot say which control plane it
    reports to emits nothing, rather than guessing prod and stranding the event.
    """
    bindir = tmp_path / "bin"
    _channel_shim(bindir / "synaptory", "https://synaptory.h3t.co")
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.delenv("SYNAPTORY_CLI_BIN", raising=False)
    monkeypatch.delenv("SYNAPTORY_CHANNEL", raising=False)
    monkeypatch.setattr(ge, "_UNADDRESSED_WARNED", False)
    stamp_runtime(ge_placeholder())

    assert ge._resolve_cli() is None

    ok = ge.emit_gate_event(
        gate_type="spec_ready", target_type="spec_version",
        target_id="US-1", state="opened",
    )
    assert ok is False
    err = capsys.readouterr().err
    assert "no control-plane stamp" in err
    assert "deploy local" in err


def ge_placeholder() -> str:
    import host_env

    return host_env.CP_URL_PLACEHOLDER


@pytest.mark.unit
def test_unaddressed_warning_is_printed_once_per_process(
    monkeypatch, tmp_path, capsys, stamp_runtime
):
    """A ceremony emits one gate event per spec. Forty identical paragraphs
    would bury the ceremony's own output."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.delenv("SYNAPTORY_CLI_BIN", raising=False)
    monkeypatch.delenv("SYNAPTORY_CHANNEL", raising=False)
    monkeypatch.setattr(ge, "_UNADDRESSED_WARNED", False)
    stamp_runtime(ge_placeholder())

    ge.emit_spec_ready_opened(["US-1", "US-2", "US-3"])

    assert capsys.readouterr().err.count("no control-plane stamp") == 1


@pytest.mark.unit
def test_explicit_cli_bin_still_overrides_an_unstamped_tree(
    monkeypatch, tmp_path, stamp_runtime
):
    """CI and e2e have no plugin tree to stamp. `SYNAPTORY_CLI_BIN` is the
    documented escape hatch and must keep working when nothing else resolves."""
    shim = tmp_path / "bin" / "shim"
    shim.parent.mkdir(parents=True)
    shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    shim.chmod(0o755)
    monkeypatch.setenv("SYNAPTORY_CLI_BIN", str(shim))
    monkeypatch.delenv("SYNAPTORY_CHANNEL", raising=False)
    stamp_runtime(ge_placeholder())

    assert ge._resolve_cli() == str(shim)


#: #334 G8: the SPQ Cycle manifest is emitted once at `open_cycle` with no
#: retry, so a misaddressed emission loses the manifest outright rather than
#: merely delaying it — `cycle_manifests` stayed empty and `/cycles` had no
#: producer. `manifest_emitter` borrows this resolver instead of re-deriving
#: one, so the addressing fix reaches it; the two tests below pin that in both
#: directions so a future second implementation there is caught.


@pytest.mark.unit
def test_manifest_emitter_resolves_the_local_cli(monkeypatch, tmp_path, stamp_runtime):
    import manifest_emitter as me

    bindir = tmp_path / "bin"
    _channel_shim(bindir / "synaptory", "https://synaptory.h3t.co")
    local = _channel_shim(bindir / "synaptory-local", "http://localhost:8080")
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.delenv("SYNAPTORY_CLI_BIN", raising=False)
    stamp_runtime("http://localhost:8080", local=True)

    assert me._resolve_cli() == str(local)


@pytest.mark.unit
def test_manifest_emitter_refuses_an_unstamped_tree(
    monkeypatch, tmp_path, stamp_runtime
):
    import manifest_emitter as me

    bindir = tmp_path / "bin"
    _channel_shim(bindir / "synaptory", "https://synaptory.h3t.co")
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.delenv("SYNAPTORY_CLI_BIN", raising=False)
    monkeypatch.delenv("SYNAPTORY_CHANNEL", raising=False)
    monkeypatch.setattr(ge, "_UNADDRESSED_WARNED", False)
    stamp_runtime(ge_placeholder())

    assert me._resolve_cli() is None
    assert me.emit_cycle_manifest({"manifest_hash": "abc", "cycle_id": "C-1"}) is False


# ─── #568 mechanism 3 — a slow probe is not a mismatched binary ──────────────
#
# `_resolve_cli` probes each candidate with `synaptory status` to check that the
# binary and the runtime tree agree on a control plane (#320). That probe used to
# have a 2.0 s ceiling whose `TimeoutExpired` landed in the same
# `except (OSError, subprocess.SubprocessError, StopIteration)` arm as every
# other failure and returned `False` — the value that also means "this binary
# belongs to the other channel". So a probe that merely ran SLOWLY resolved no
# CLI, emitted nothing, and said nothing.
#
# It was caught by `test_emit_omits_unset_optionals` above, which failed on one
# full-suite run and passed on the next two: its `stub_cli` deliberately stamps a
# production cp-url so tree and binary agree on a channel, which FORCES the probe
# to run against a bash shim on every call. Under full-suite load that exceeded
# 2.0 s and the test's `_calls(...)[0]` raised IndexError on an empty log.
#
# These reproduce it deliberately rather than waiting for load.

import subprocess  # noqa: E402

#: Probe ceiling for the tests that time a real subprocess. Two orders of
#: magnitude above a warmed shim's ~10 ms spawn, so a pass means the mechanism
#: worked rather than that the machine was quick.
TIGHT_CEILING = "0.5"


def _warm(path: Path) -> None:
    """Pay macOS's first-exec cost before anything is timed.

    A freshly written executable is scanned on its first `execve` (Gatekeeper /
    XProtect): measured on this repo at **473 ms cold against 10 ms warm** for a
    four-line `/bin/sh` script. Left unpaid, that alone blows a sub-second
    ceiling and every test below passes for the wrong reason. It is also a neat
    corroboration of the defect: the probe's budget is spent on process
    admission, not on the work `status` does.
    """
    subprocess.run([str(path), "--warm"], capture_output=True)


def _slow_status_shim(path: Path, url: str, *, delay: float, slow_calls: int) -> Path:
    """A CLI shim whose first `slow_calls` `status` probes sleep past the ceiling.

    `slow_calls=1` is the observed shape: one transient spike, then a machine
    that answers fine. The counter lives in a sibling file so it survives the
    separate process each probe spawns.

    `#!/bin/sh` and an explicit absolute PATH, both deliberate: these tests point
    PATH at a scratch bindir, so `#!/usr/bin/env bash` finds no interpreter and
    `sleep` is not on PATH either. That failure is silent and mimics the thing
    under test — the shim exits fast and unreadably instead of timing out.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    counter = path.parent / (path.name + ".probes")
    path.write_text(
        "#!/bin/sh\n"
        "PATH=/usr/bin:/bin\n"
        'if [ "$1" = status ]; then\n'
        f"  n=$(cat {counter})\n"
        f"  echo $((n + 1)) > {counter}\n"
        f'  if [ "$n" -lt {slow_calls} ]; then sleep {delay}; fi\n'
        f'  printf "control_plane_url:  {url}\\n"\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    _warm(path)
    counter.write_text("0", encoding="utf-8")
    return path


@pytest.fixture
def tight_probe(monkeypatch):
    """Shrink the probe ceiling so a sleeping shim is deterministically late."""
    monkeypatch.setenv("SYNAPTORY_CLI_PROBE_TIMEOUT", TIGHT_CEILING)
    monkeypatch.setattr(ge, "_UNPROBEABLE_WARNED", False)
    monkeypatch.setattr(ge, "_UNADDRESSED_WARNED", False)


@pytest.fixture
def always_times_out(monkeypatch):
    """Every probe reports a timeout, without any of them costing wall clock.

    The tests that assert on the *reporting* do not need a real slow subprocess,
    and paying two real ceilings per emission would put a Layer 1 file into the
    tens of seconds. The tests that assert on the *mechanism* below use real
    subprocesses.
    """
    calls: list = []

    def _fake(candidate, local, timeout):
        calls.append((candidate, local, timeout))
        return ge._TIMEOUT

    monkeypatch.setattr(ge, "_probe_channel_identity", _fake)
    return calls


@pytest.mark.unit
def test_a_timeout_verdict_is_not_a_mismatch_verdict(tmp_path):
    """The distinction the fix rests on, asserted on the probe itself.

    A mismatch means *do not emit*. A timeout means *we could not tell*. Folding
    them together is what made a loaded machine indistinguishable from a wrongly
    installed CLI.
    """
    bindir = tmp_path / "bin"
    slow = _slow_status_shim(bindir / "slow", PROD_URL, delay=5.0, slow_calls=99)
    prod = _channel_shim(bindir / "prod", PROD_URL)
    _warm(prod)

    assert ge._probe_channel_identity(str(slow), False, 0.5) == ge._TIMEOUT
    assert ge._probe_channel_identity(str(prod), True, 10.0) == ge._MISMATCH
    assert ge._probe_channel_identity(str(prod), False, 10.0) == ge._MATCH
    assert len({ge._TIMEOUT, ge._MISMATCH, ge._UNREADABLE, ge._MATCH}) == 4


@pytest.mark.unit
def test_a_probe_that_times_out_once_still_resolves_the_cli(
    monkeypatch, tmp_path, stamp_runtime, tight_probe
):
    """The observed failure, made deterministic.

    One slow probe used to reject the only candidate there was. It now costs a
    second probe rather than the whole resolution, because a timeout is transient
    by construction where a mismatch never is.
    """
    bindir = tmp_path / "bin"
    shim = _slow_status_shim(bindir / "synaptory", PROD_URL, delay=5.0, slow_calls=1)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.delenv("SYNAPTORY_CLI_BIN", raising=False)
    stamp_runtime(PROD_URL)

    assert ge._resolve_cli() == str(shim)


@pytest.mark.unit
def test_a_slow_probe_no_longer_swallows_the_emission(
    monkeypatch, tmp_path, stamp_runtime, tight_probe
):
    """`test_emit_omits_unset_optionals`'s failure, reproduced on purpose.

    Same shape as `stub_cli` — a production stamp so the probe is forced to run —
    except the shim's first `status` is slow. On the old resolver the log stayed
    empty and `_calls(...)[0]` raised IndexError with nothing on stderr saying why.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    counter = bindir / "probes"
    shim = bindir / "synaptory"
    shim.write_text(
        "#!/bin/bash\n"
        "PATH=/usr/bin:/bin\n"
        'if [ "$1" = status ]; then\n'
        f"  n=$(cat {counter})\n"
        f"  echo $((n + 1)) > {counter}\n"
        '  if [ "$n" -lt 1 ]; then sleep 5; fi\n'
        f'  printf "control_plane_url:  {PROD_URL}\\n"\n'
        "  exit 0\n"
        "fi\n"
        f'printf "%q " "$@" >> {log}\n'
        f'printf "\\n" >> {log}\n'
        "exit 0\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    _warm(shim)
    counter.write_text("0", encoding="utf-8")
    log.unlink(missing_ok=True)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.delenv("SYNAPTORY_CLI_BIN", raising=False)
    stamp_runtime(PROD_URL)

    ok = ge.emit_gate_event(
        gate_type="spec_ready", target_type="spec_version",
        target_id="US-42", state="opened",
    )

    assert ok is True
    argv = _calls(log)[0]
    assert argv[:2] == ["telemetry", "gate-event"]


@pytest.mark.unit
def test_an_unanswerable_probe_is_reported_rather_than_silent(
    monkeypatch, tmp_path, capsys, stamp_runtime, tight_probe, always_times_out
):
    """When we genuinely cannot tell, say so — and say which of the three it is.

    Silence was the expensive part: "the machine was too busy to answer" read
    exactly like "no CLI is installed" and like "the installed CLI belongs to the
    other channel", and those have three different operator responses.
    """
    bindir = tmp_path / "bin"
    _channel_shim(bindir / "synaptory", PROD_URL)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.delenv("SYNAPTORY_CLI_BIN", raising=False)
    stamp_runtime(PROD_URL)

    assert ge._resolve_cli() is None

    err = capsys.readouterr().err
    assert "did not answer" in err
    assert "could not tell" in err
    assert "NOT 'the binary does not match'" in err
    assert "no control-plane stamp" not in err  # that is a different diagnosis


@pytest.mark.unit
def test_one_retry_is_spent_per_resolution_not_per_candidate(
    monkeypatch, tmp_path, stamp_runtime, tight_probe, always_times_out
):
    """The bound that keeps the retry affordable inside a ceremony.

    A timeout is a statement about the machine, so every later candidate would
    observe the same thing. Retrying each of them turns a 4 × 2.0 s worst case
    into 8 × the (now larger) ceiling, inside a loop that runs once per emitted
    gate event. Two probes total, then give up and say so.
    """
    bindir = tmp_path / "bin"
    _channel_shim(bindir / "synaptory", PROD_URL)
    _channel_shim(Path(tmp_path) / ".local" / "bin" / "synaptory", PROD_URL)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.delenv("SYNAPTORY_CLI_BIN", raising=False)
    stamp_runtime(PROD_URL)

    assert ge._resolve_cli() is None
    assert len(always_times_out) == 2


@pytest.mark.unit
def test_the_unprobeable_warning_is_printed_once_per_process(
    monkeypatch, tmp_path, capsys, stamp_runtime, tight_probe, always_times_out
):
    """Same reason as the unaddressed warning: one gate event per spec."""
    bindir = tmp_path / "bin"
    _channel_shim(bindir / "synaptory", PROD_URL)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.delenv("SYNAPTORY_CLI_BIN", raising=False)
    stamp_runtime(PROD_URL)

    ge.emit_spec_ready_opened(["US-1", "US-2", "US-3"])

    assert capsys.readouterr().err.count("did not answer") == 1


@pytest.mark.unit
def test_a_mismatched_binary_is_rejected_without_paying_for_a_retry(
    monkeypatch, tmp_path, stamp_runtime, tight_probe
):
    """The retry is for timeouts only.

    A mismatch is a permanent property of the installed binary, so re-asking is
    pure latency inside a ceremony. Counted rather than asserted structurally,
    because an edit widening the retry to every failure would still pass a shape
    check.
    """
    bindir = tmp_path / "bin"
    counter = bindir / "synaptory-local.probes"
    # A LOCAL-stamped tree whose `synaptory-local` answers with the PRODUCTION
    # URL — a real mismatch, reached and probed rather than skipped.
    _slow_status_shim(bindir / "synaptory-local", PROD_URL, delay=0.0, slow_calls=0)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.delenv("SYNAPTORY_CLI_BIN", raising=False)
    stamp_runtime("http://localhost:8080", local=True)

    assert ge._resolve_cli() is None
    assert counter.read_text(encoding="utf-8").strip() == "1"


@pytest.mark.unit
def test_an_explicit_cli_bin_keeps_the_benefit_of_the_doubt_when_slow(
    monkeypatch, tmp_path, stamp_runtime, tight_probe, always_times_out
):
    """`SYNAPTORY_CLI_BIN` is the documented CI/e2e hatch.

    It is already accepted when it answers nothing at all, so a slow answer —
    strictly weaker evidence — must not be the thing that rejects it. Otherwise
    the fix would break the one path that has no plugin tree to stamp.
    """
    shim = _channel_shim(tmp_path / "bin" / "shim", PROD_URL)
    monkeypatch.setenv("SYNAPTORY_CLI_BIN", str(shim))
    stamp_runtime(PROD_URL)

    assert ge._resolve_cli() == str(shim)


@pytest.mark.unit
def test_the_probe_ceiling_is_overridable(monkeypatch):
    """CI on a small runner may need more than the default headroom."""
    monkeypatch.delenv("SYNAPTORY_CLI_PROBE_TIMEOUT", raising=False)
    assert ge._probe_timeout() == ge._PROBE_TIMEOUT_DEFAULT
    monkeypatch.setenv("SYNAPTORY_CLI_PROBE_TIMEOUT", "12")
    assert ge._probe_timeout() == 12.0
    for junk in ("", "   ", "nonsense", "0", "-3"):
        monkeypatch.setenv("SYNAPTORY_CLI_PROBE_TIMEOUT", junk)
        assert ge._probe_timeout() == ge._PROBE_TIMEOUT_DEFAULT, junk


@pytest.mark.unit
def test_the_ceiling_left_the_value_that_was_measured_too_tight():
    """2.0 s was reachable on a machine running the suite (#568 mechanism 3).

    Pinned so the headroom is not quietly given back. A bump is the weakest of
    the three levers on its own — the retry and the distinct verdict are the fix
    — which is exactly why it needs a test rather than a comment.
    """
    assert ge._PROBE_TIMEOUT_DEFAULT > 2.0
