"""Doctor talks to the CLI; it must not import deleted token modules."""

from __future__ import annotations

from pathlib import Path

import doctor


def test_doctor_does_not_import_removed_auth_modules():
    src = Path(doctor.__file__).read_text(encoding="utf-8")
    assert "access_token_client" not in src
    assert "session_manager" not in src


def test_doctor_reports_missing_cli(monkeypatch, capsys):
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    assert doctor.run() == 1
    assert "NOT FOUND" in capsys.readouterr().out
