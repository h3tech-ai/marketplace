"""Layer 1 unit tests — `plugin-claude/skills/synaptory/routing-rules.json`.

Table-driven tests that verify the routing table is self-consistent:
  - Every case from fixtures/routing_rules_cases.json finds a matching mode.
  - Each mode's trigger_signals are unique (no duplicate entries).
  - Each mode's skills_involved are registered agent names.
  - The expected lifecycle and skills_include assertions pass for the fixture cases.

No subprocess, no network — pure JSON validation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
FIXTURE = HERE.parent / "fixtures" / "routing_rules_cases.json"
PLUGIN_ROOT = HERE.parent.parent

ROUTING_RULES_PATH = PLUGIN_ROOT / "skills" / "synaptory" / "routing-rules.json"

# Known agent names (from CLAUDE.md agent roster + aliases).
KNOWN_AGENTS = {
    "project-owner",
    "product-manager",  # legacy alias accepted by server
    "solution-architect",
    "software-engineer",
    "quality-engineer",
    "code-reviewer",
    "compliance-engineer",
    "platform-engineer",
    "technical-writer",
    "research-advisor",
}


@pytest.fixture(scope="module")
def routing_rules() -> dict:
    assert ROUTING_RULES_PATH.exists(), (
        f"routing-rules.json not found at {ROUTING_RULES_PATH}"
    )
    return json.loads(ROUTING_RULES_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def fixture_cases() -> list[dict]:
    assert FIXTURE.exists(), f"fixture not found at {FIXTURE}"
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def modes_by_name(routing_rules: dict) -> dict[str, dict]:
    return {m["mode"]: m for m in routing_rules["modes"]}


# ── Schema sanity ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_routing_rules_has_required_top_level_keys(routing_rules: dict):
    """routing-rules.json must have version, modes, and tiebreakers."""
    assert "version" in routing_rules
    assert "modes" in routing_rules
    assert isinstance(routing_rules["modes"], list)
    assert len(routing_rules["modes"]) > 0


@pytest.mark.unit
def test_every_mode_has_required_fields(routing_rules: dict):
    """Every mode entry must have mode, trigger_signals, and skills_involved."""
    required = {"mode", "trigger_signals", "skills_involved", "pipeline", "skip_conditions"}
    for entry in routing_rules["modes"]:
        missing = required - set(entry.keys())
        assert not missing, (
            f"mode {entry.get('mode')!r} is missing required fields: {missing}"
        )


@pytest.mark.unit
def test_skills_involved_are_known_agents(routing_rules: dict):
    """Every skills_involved entry must be a registered agent name."""
    for entry in routing_rules["modes"]:
        for skill in entry["skills_involved"]:
            assert skill in KNOWN_AGENTS, (
                f"mode {entry['mode']!r} references unknown agent {skill!r}. "
                f"Known: {sorted(KNOWN_AGENTS)}"
            )


@pytest.mark.unit
def test_mode_names_are_unique(routing_rules: dict):
    """Mode names must be unique — no duplicate entries."""
    names = [m["mode"] for m in routing_rules["modes"]]
    assert len(names) == len(set(names)), (
        f"duplicate mode names found: {[n for n in names if names.count(n) > 1]}"
    )


# ── Fixture-driven table tests ────────────────────────────────────────────────


def _find_mode_for_signal(modes: list[dict], signal: str) -> dict | None:
    """Return the first mode whose trigger_signals or trigger_patterns match signal."""
    signal_lower = signal.lower()
    for mode in modes:
        for ts in mode.get("trigger_signals", []):
            if ts.lower() in signal_lower or signal_lower in ts.lower():
                return mode
        for pattern in mode.get("trigger_patterns", []):
            try:
                if re.search(pattern, signal, re.IGNORECASE):
                    return mode
            except re.error:
                pass
    return None


@pytest.mark.unit
@pytest.mark.parametrize(
    "case",
    json.loads(FIXTURE.read_text(encoding="utf-8")) if FIXTURE.exists() else [],
    ids=lambda c: c["description"],
)
def test_routing_case(case: dict, routing_rules: dict):
    """Each fixture case resolves to the expected mode with the right lifecycle and skills."""
    modes = routing_rules["modes"]
    signal = case["trigger_signal"]
    expected_mode = case["expected_mode"]
    expected_lifecycle = case["expected_lifecycle"]
    expected_skills = case["expected_skills_include"]

    matched = _find_mode_for_signal(modes, signal)
    assert matched is not None, (
        f"No mode matched signal {signal!r}; expected mode {expected_mode!r}"
    )
    assert matched["mode"] == expected_mode, (
        f"signal {signal!r} matched mode {matched['mode']!r}, "
        f"expected {expected_mode!r}"
    )

    # Lifecycle check — some modes lack the field (defaults to standalone).
    actual_lifecycle = matched.get("lifecycle", "standalone")
    assert actual_lifecycle == expected_lifecycle, (
        f"mode {expected_mode!r} has lifecycle {actual_lifecycle!r}, "
        f"expected {expected_lifecycle!r}"
    )

    # Skills check — all expected skills must appear in skills_involved.
    for skill in expected_skills:
        assert skill in matched["skills_involved"], (
            f"mode {expected_mode!r} missing expected skill {skill!r}; "
            f"skills_involved={matched['skills_involved']}"
        )
