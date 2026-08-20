"""Layer 1 — assert the `/synaptory report` mode has been migrated off
the GitHub MCP path and onto the control-plane `synaptory report submit`
CLI subcommand.

This is a static-doc test: it guards the *shape* of the prompt the
orchestrator follows. If a future edit accidentally re-introduces the
old MCP call, or drops the new CLI invocation, this test catches it
before the broken mode ships.
"""

from __future__ import annotations

from pathlib import Path

import pytest

MODE_FILE = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "synaptory"
    / "modes"
    / "report.md"
)


@pytest.fixture(scope="module")
def mode_text() -> str:
    return MODE_FILE.read_text()


@pytest.mark.unit
def test_mode_file_exists(mode_text: str):
    assert mode_text, "report.md must not be empty"


@pytest.mark.unit
def test_mode_uses_cli_not_github_mcp(mode_text: str):
    # The mode must invoke the CP-backed CLI subcommand.
    assert "synaptory report submit" in mode_text, (
        "report mode must call `synaptory report submit`"
    )
    # And must NOT call mcp__github__* tools any more — that path is dead
    # for external users because the repo is private.
    assert "mcp__github__create_issue" not in mode_text
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" not in mode_text


@pytest.mark.unit
def test_mode_documents_project_slug_requirement(mode_text: str):
    # The CP endpoint validates project membership; the mode must
    # surface that the project slug is required so the orchestrator
    # includes it in the payload.
    assert "project" in mode_text.lower()
    assert "REQUIRED" in mode_text or "required" in mode_text


@pytest.mark.unit
def test_mode_describes_identity_stamping(mode_text: str):
    # Reporter identity comes from the session, server-side. Documenting
    # this in the mode prevents future contributors from re-adding a
    # client-side UPN field that the server would just overwrite.
    assert "identity" in mode_text.lower()
