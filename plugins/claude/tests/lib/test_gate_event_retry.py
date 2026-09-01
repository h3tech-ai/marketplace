"""Layer 1 — gate events survive a transient control plane (#331 G10).

One workstream emitted ZERO gate events for a whole Cycle. The failure was
`no project_id resolved`, which returns before the CLI reaches its outbox -- so
the event was dropped while the log said "CLI outbox should retry on next
session". The reassuring message was simply wrong.

Same shape as `manifest_emitter`'s spool (#334), deliberately: two emitters with
the same failure mode should not have two different recovery stories.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import gate_emitter as ge


pytestmark = pytest.mark.unit


class _Result:
    def __init__(self, code: int) -> None:
        self.returncode = code
        self.stdout = ""
        self.stderr = "no project_id resolved" if code else ""


@pytest.fixture(autouse=True)
def _installed_cli(monkeypatch):
    """A resolvable CLI. Without one `emit_gate_event` returns before it can
    build a command, which is correct but not what these tests exercise."""
    monkeypatch.setattr(ge, "_resolve_cli", lambda: "/usr/bin/true")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    (p / ".synaptory" / ".orchestrator").mkdir(parents=True)
    return p


def _spooled(project: Path) -> list[Path]:
    d = project / ".synaptory" / ".orchestrator" / "gate-events"
    return sorted(d.glob("*.json")) if d.is_dir() else []


def _emit(project: Path, target_id: str = "WU-1") -> bool:
    return ge.emit_gate_event(
        gate_type="spec_ready", target_type="work_unit", target_id=target_id,
        state="opened", project_dir=str(project),
    )


def test_a_failed_gate_event_is_spooled(project: Path, monkeypatch):
    monkeypatch.setattr(ge.subprocess, "run", lambda *a, **k: _Result(1))
    assert _emit(project) is False
    assert len(_spooled(project)) == 1


def test_a_successful_gate_event_spools_nothing(project: Path, monkeypatch):
    monkeypatch.setattr(ge.subprocess, "run", lambda *a, **k: _Result(0))
    assert _emit(project) is True
    assert _spooled(project) == []


def test_retry_sends_and_clears(project: Path, monkeypatch):
    monkeypatch.setattr(ge.subprocess, "run", lambda *a, **k: _Result(1))
    _emit(project)
    assert len(_spooled(project)) == 1

    monkeypatch.setattr(ge.subprocess, "run", lambda *a, **k: _Result(0))
    assert ge.retry_pending(str(project)) == 1
    assert _spooled(project) == []


def test_retry_keeps_the_spool_when_still_failing(project: Path, monkeypatch):
    monkeypatch.setattr(ge.subprocess, "run", lambda *a, **k: _Result(1))
    _emit(project)
    assert ge.retry_pending(str(project)) == 0
    assert len(_spooled(project)) == 1


def test_a_later_success_drains_the_backlog(project: Path, monkeypatch):
    """The spool cannot outlive its cause even if nothing calls retry_pending."""
    monkeypatch.setattr(ge.subprocess, "run", lambda *a, **k: _Result(1))
    _emit(project, "WU-OLD")
    assert len(_spooled(project)) == 1

    monkeypatch.setattr(ge.subprocess, "run", lambda *a, **k: _Result(0))
    _emit(project, "WU-NEW")
    assert _spooled(project) == []


def test_identical_events_do_not_accumulate(project: Path, monkeypatch):
    """Keyed on the command, so re-emitting the same event overwrites."""
    monkeypatch.setattr(ge.subprocess, "run", lambda *a, **k: _Result(1))
    for _ in range(5):
        _emit(project, "WU-1")
    assert len(_spooled(project)) == 1


def test_different_events_do_not_collide(project: Path, monkeypatch):
    """The key must separate distinct events, or a spool loses all but one."""
    monkeypatch.setattr(ge.subprocess, "run", lambda *a, **k: _Result(1))
    _emit(project, "WU-1")
    _emit(project, "WU-2")
    assert len(_spooled(project)) == 2


def test_spool_is_bounded(project: Path, monkeypatch):
    monkeypatch.setattr(ge.subprocess, "run", lambda *a, **k: _Result(1))
    for i in range(ge._SPOOL_MAX + 5):
        _emit(project, "WU-%d" % i)
    assert len(_spooled(project)) <= ge._SPOOL_MAX


def test_a_corrupt_entry_is_dropped(project: Path, monkeypatch):
    d = project / ".synaptory" / ".orchestrator" / "gate-events"
    d.mkdir(parents=True)
    (d / "bad.json").write_text("{not json", encoding="utf-8")

    monkeypatch.setattr(ge.subprocess, "run", lambda *a, **k: _Result(0))
    assert ge.retry_pending(str(project)) == 0
    assert _spooled(project) == []


def test_retry_is_a_noop_with_no_spool(project: Path):
    assert ge.retry_pending(str(project)) == 0


def test_retry_keeps_the_backlog_when_no_cli_is_installed(project: Path, monkeypatch):
    """Discarding the spool to "clean up" would lose the very events it
    protects. A missing CLI is a separate problem."""
    monkeypatch.setattr(ge.subprocess, "run", lambda *a, **k: _Result(1))
    _emit(project)
    assert len(_spooled(project)) == 1

    monkeypatch.setattr(ge, "_resolve_cli", lambda: None)
    assert ge.retry_pending(str(project)) == 0
    assert len(_spooled(project)) == 1


def test_the_spool_does_not_pin_an_absolute_cli_path(project: Path, monkeypatch):
    """The binary can move or be updated between sessions."""
    monkeypatch.setattr(ge.subprocess, "run", lambda *a, **k: _Result(1))
    _emit(project)
    entry = json.loads(_spooled(project)[0].read_text(encoding="utf-8"))
    assert "args" in entry and "cmd" not in entry
    assert not any(str(a).startswith("/usr/bin") for a in entry["args"]), entry


def test_an_invalid_enum_is_refused_before_it_can_spool(project: Path, monkeypatch):
    """A typo must surface as a refusal, not become a poison entry retried
    every session forever."""
    monkeypatch.setattr(ge.subprocess, "run", lambda *a, **k: _Result(1))
    assert ge.emit_gate_event(
        gate_type="nonsense", target_type="work_unit", target_id="WU-1",
        state="opened", project_dir=str(project),
    ) is False
    assert _spooled(project) == []
