"""Runtime readiness is shared by the two staff-pilot host adapters."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import staff_pilot


def _profile(available=False, roles=None):
    return {
        "profile_id": "codex-local-v1",
        "available": available,
        "capability_profiles": roles if roles is not None else ["producer", "prover"],
        "reason": 'codex states no exact model id; model_route.requested="policy-default"',
    }


@pytest.mark.parametrize("host", ["codex", "cursor"])
def test_host_reads_shared_dispatch_evidence_without_writes(
    tmp_path, monkeypatch, host
):
    project = tmp_path / "project"
    project.mkdir()
    root = Path(__file__).resolve().parents[3]
    source = (
        root / "plugin-codex/plugins/synaptory/scripts/standard_pilot.py"
        if host == "codex"
        else root / "plugin-cursor/scripts/standard_pilot.py"
    )
    spec = importlib.util.spec_from_file_location("pilot_host_under_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Run a real child process that accepts only the read-only doctor command.
    cli = tmp_path / "cli"
    reports = [_profile()]
    cli.write_text(
        f"#!{sys.executable}\nimport sys, json\n"
        "assert sys.argv[1:] == ['runtimes', 'doctor', '--read-only', '--json', '--project-dir', "
        + repr(str(project))
        + "]\nprint("
        + repr(json.dumps({"reports": reports}))
        + ")\n"
    )
    cli.chmod(0o755)
    monkeypatch.setitem(
        sys.modules,
        "mcp_server" if host == "codex" else "server",
        SimpleNamespace(_runtime_cli=lambda: str(cli)),
    )
    before = {
        str(p.relative_to(project)): p.read_bytes() if p.is_file() else None
        for p in project.rglob("*")
    }
    report = module._dispatch_readiness(project, timeout=5)
    assert report == {
        "ready": False,
        "checked": True,
        "available_roles": [],
        "profiles": reports,
    }
    assert before == {
        str(p.relative_to(project)): p.read_bytes() if p.is_file() else None
        for p in project.rglob("*")
    }


def test_available_roles_do_not_hide_other_profile_refusals(tmp_path, monkeypatch):
    profiles = [
        _profile(),
        {
            "profile_id": "claude-local-v1",
            "available": True,
            "capability_profiles": ["prover", "producer"],
            "reason": "within pin",
        },
    ]
    monkeypatch.setattr(
        staff_pilot.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(
            returncode=0, stdout=json.dumps({"reports": profiles})
        ),
    )
    report = staff_pilot.dispatch_readiness(tmp_path, cli="synaptory", timeout=1)
    assert report["ready"] is True
    assert report["available_roles"] == ["producer", "prover"]
    assert report["profiles"] == profiles


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"reports": None},
        {"reports": [None]},
        {"reports": [{"profile_id": "codex"}]},
        {"reports": [_profile(available="false")]},
        {"reports": [_profile(available=True, roles=[])]},
        {"reports": [_profile(), _profile()]},
    ],
)
def test_malformed_evidence_never_passes(tmp_path, monkeypatch, payload):
    monkeypatch.setattr(
        staff_pilot.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(returncode=0, stdout=json.dumps(payload)),
    )
    report = staff_pilot.dispatch_readiness(tmp_path, cli="synaptory", timeout=1)
    assert report["ready"] is False
    assert report["checked"] is False
    assert "error" in report


@pytest.mark.parametrize(
    "failure",
    ["missing", "old-cli", "invalid-json", "timeout", "not-executable", "empty"],
)
def test_unavailable_doctor_fails_closed_without_retry(tmp_path, monkeypatch, failure):
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args[0], 1)
        if failure == "not-executable":
            raise PermissionError("private diagnostic")
        return SimpleNamespace(
            returncode=2 if failure == "old-cli" else 0,
            stdout='{"reports": []}' if failure == "empty" else "private diagnostic",
        )

    monkeypatch.setattr(staff_pilot.subprocess, "run", run)
    report = staff_pilot.dispatch_readiness(
        tmp_path, cli="" if failure == "missing" else "synaptory", timeout=1
    )
    assert report["ready"] is False
    assert report["checked"] is (failure == "empty")
    assert len(calls) == (0 if failure == "missing" else 1)
    assert "private diagnostic" not in json.dumps(report)
