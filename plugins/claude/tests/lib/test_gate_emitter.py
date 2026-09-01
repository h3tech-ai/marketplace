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


def test_both_publish_paths_report_to_the_control_plane():
    """Intra-Cycle and cross-Cycle publication each write an event that another
    clone depends on. Neither was visible to the CP."""
    from pathlib import Path

    core = Path(__file__).resolve().parents[3] / "core" / "lib"
    for module, target in (("spq_ledger.py", 'target_type="work_unit"'),
                           ("coordination_cycle.py", 'target_type="cycle"')):
        src = (core / module).read_text(encoding="utf-8")
        assert "emit_dependency_published" in src, module
        assert target in src, (module, target)


def test_emission_never_blocks_a_ceremony():
    """Git is authoritative; the CP observes. A failed emit is a reporting gap,
    never a delivery one — so every call site swallows its own errors."""
    from pathlib import Path

    core = Path(__file__).resolve().parents[3] / "core" / "lib"
    for module in ("spq_ledger.py", "coordination_cycle.py",
                   "spq_state_machine.py"):
        src = (core / module).read_text(encoding="utf-8")
        for marker in ("emit_dependency_published", "manifest_emitter"):
            if marker not in src:
                continue
            block = src[src.index(marker) - 400 : src.index(marker) + 700]
            assert "except Exception" in block, (module, marker)


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
