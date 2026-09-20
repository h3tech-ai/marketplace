"""#820 — a disabled continuation loop is reported, not merely obeyed.

The kill switch is right to exist. What was wrong is that nothing anywhere
said it was engaged: `next_action`'s output was identical whether the loop was
live or dead, `doctor` never looked, and `loop_engine` knew the answer and told
only telemetry. An engagement left ten Work Units `queued` for four hours
overnight with zero attempts authorized, ran three Cycles to Checkpoint in that
state, and attributed the idling to the model.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks" / "lib"))

import doctor  # noqa: E402
import loop_engine as le  # noqa: E402

pytestmark = pytest.mark.unit


def _interactive(tmp_path, name="interactive"):
    """A project whose engagement mode is NOT structured.

    The mode is read from `.synaptory/.orchestrator/settings.md`'s
    `Engagement:` line (`mode_reader.get_engagement_mode`), not from
    `.synaptory.yaml` -- a fixture that wrote the yaml key would silently
    describe a structured project and prove nothing.
    """
    project = tmp_path / name
    (project / ".synaptory" / ".orchestrator").mkdir(parents=True, exist_ok=True)
    (project / ".synaptory.yaml").write_text('build_mode: "spq"\n', encoding="utf-8")
    (project / ".synaptory" / ".orchestrator" / "settings.md").write_text(
        "Project: t\nEngagement: interactive\n", encoding="utf-8"
    )
    return project


def _structured(tmp_path, extra=""):
    project = tmp_path / "p"
    project.mkdir(exist_ok=True)
    (project / ".synaptory.yaml").write_text(
        'build_mode: "spq"\nengagement_mode: "structured"\n' + extra,
        encoding="utf-8",
    )
    return project


# ── the status itself ────────────────────────────────────────────────────────

def test_a_live_loop_reports_enabled(tmp_path, monkeypatch):
    monkeypatch.delenv(le.LOOP_DISABLE_ENV, raising=False)
    status = le.loop_status(str(_structured(tmp_path)))
    assert status["enabled"] is True
    assert status["reason"] is None


def test_the_env_kill_switch_is_named_with_its_remedy(tmp_path, monkeypatch):
    monkeypatch.setenv(le.LOOP_DISABLE_ENV, "1")
    status = le.loop_status(str(_structured(tmp_path)))

    assert status["enabled"] is False
    assert status["reason"] == "disabled_env"
    # The remedy has to name where it actually hides, which is what cost the
    # reporting engagement the time.
    assert "settings.local.json" in status["remedy"]


def test_the_config_kill_switch_is_reported_too(tmp_path, monkeypatch):
    """The report named only the env var; this one idles identically."""
    monkeypatch.delenv(le.LOOP_DISABLE_ENV, raising=False)
    project = _structured(tmp_path, "resilience:\n  loop_continuation: disabled\n")
    status = le.loop_status(str(project))

    assert status["enabled"] is False
    assert status["reason"] == "disabled_config"


def test_an_unstructured_engagement_is_reported_but_is_not_a_defect(tmp_path, monkeypatch):
    monkeypatch.delenv(le.LOOP_DISABLE_ENV, raising=False)
    status = le.loop_status(str(_interactive(tmp_path)))

    assert status["enabled"] is False
    assert status["reason"] == "not_structured"


# ── the engine and the report cannot disagree ────────────────────────────────

@pytest.mark.parametrize(
    "env,extra,expected",
    [
        ("1", "", "disabled_env"),
        (None, "resilience:\n  loop_continuation: disabled\n", "disabled_config"),
    ],
)
def test_should_continue_stops_for_exactly_what_loop_status_reports(
    tmp_path, monkeypatch, env, extra, expected
):
    """The whole point of the extraction: one predicate, two readers.

    A `doctor` that re-derived the conditions is how a report comes to say
    `enabled` about an engine that stops — the same failure, one level up.
    """
    if env is None:
        monkeypatch.delenv(le.LOOP_DISABLE_ENV, raising=False)
    else:
        monkeypatch.setenv(le.LOOP_DISABLE_ENV, env)
    project = str(_structured(tmp_path, extra))

    assert le.loop_status(project)["reason"] == expected
    assert le.should_continue(project, "sess-1")["stop_reason"] == expected


# ── doctor ───────────────────────────────────────────────────────────────────

def test_doctor_reports_a_disabled_loop_as_a_finding(tmp_path, monkeypatch, capsys):
    """`grep -c SYNAPTORY_LOOP_DISABLE doctor.py` returned 0 before #820."""
    monkeypatch.setenv(le.LOOP_DISABLE_ENV, "1")
    lines, finding = doctor._loop_lines(str(_structured(tmp_path)))
    text = "\n".join(lines)

    assert finding is True
    assert "DISABLED" in text
    assert le.LOOP_DISABLE_ENV in text
    # It must say what the operator will otherwise observe instead.
    assert "queued" in text


def test_doctor_does_not_flag_an_interactive_project(tmp_path, monkeypatch):
    """Interactive is user-in-the-loop by design; flagging it would train
    operators to ignore the finding."""
    monkeypatch.delenv(le.LOOP_DISABLE_ENV, raising=False)
    lines, finding = doctor._loop_lines(str(_interactive(tmp_path)))

    assert finding is False
    assert "not driving" in "\n".join(lines)


def test_doctor_reports_unknown_rather_than_healthy_when_it_cannot_ask(monkeypatch):
    """This check exists because silence read as health."""
    monkeypatch.setattr(
        le, "loop_status", lambda _p: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    lines, finding = doctor._loop_lines("/nonexistent")

    assert finding is False
    assert "UNKNOWN" in "\n".join(lines)


# ── the dispatch contract ────────────────────────────────────────────────────

def test_a_live_loop_attaches_no_block(tmp_path, monkeypatch):
    """A block on every action is a block nobody reads."""
    monkeypatch.delenv(le.LOOP_DISABLE_ENV, raising=False)
    action = {"action": "dispatch_se"}
    le.attach_loop_status(action, str(_structured(tmp_path)))
    assert "loop" not in action


def test_a_disabled_loop_is_named_on_the_dispatch_contract(tmp_path, monkeypatch):
    monkeypatch.setenv(le.LOOP_DISABLE_ENV, "1")
    action = {"action": "dispatch_se"}
    le.attach_loop_status(action, str(_structured(tmp_path)))

    assert action["loop"]["enabled"] is False
    assert action["loop"]["reason"] == "disabled_env"
    assert "only if a human asks" in action["loop"]["consequence"]


def test_attaching_never_breaks_the_dispatch(monkeypatch):
    monkeypatch.setattr(
        le, "loop_status", lambda _p: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    action = {"action": "dispatch_se"}
    le.attach_loop_status(action, "/nonexistent")
    assert action == {"action": "dispatch_se"}


# ── the CLI surface doctor's prose actually calls ────────────────────────────

def test_the_status_verb_answers_without_driving_the_loop(tmp_path, monkeypatch):
    """`doctor.md` shells out to this; a verb the prose names and the module
    lacks is a check that silently never runs."""
    import json
    import subprocess

    engine = Path(__file__).resolve().parents[2] / "hooks" / "lib" / "loop_engine.py"
    project = _structured(tmp_path)

    env = {**os.environ, "SYNAPTORY_LOOP_DISABLE": "1"}
    out = subprocess.run(
        [sys.executable, str(engine), "status", str(project)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout)
    assert payload["enabled"] is False
    assert payload["reason"] == "disabled_env"
    assert payload["remedy"]
