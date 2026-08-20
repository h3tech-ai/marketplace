"""Layer 1 — `plugin-claude/hooks/lib/mode_reader.py` engagement-mode tests.

Hypothesis: every documented alias maps to one of two canonical modes
(`structured` | `interactive`); unknown input falls back to the default.
"""

from __future__ import annotations

import pytest

from hooks.lib.mode_reader import (
    CANONICAL_MODES,
    DEFAULT_MODE,
    MODE_ALIASES,
    VALID_MODES,
    normalize_mode,
)


@pytest.mark.unit
def test_canonical_modes_locked():
    """Pin the two canonical engagement modes."""
    assert CANONICAL_MODES == {"structured", "interactive"}
    assert DEFAULT_MODE == "structured"


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("structured", "structured"),
        ("autonomous", "structured"),  # legacy
        ("hands-off", "structured"),
        ("hands_off", "structured"),
        ("interactive", "interactive"),
        ("controlled", "interactive"),  # legacy
        ("hands-on", "interactive"),
        ("hands_on", "interactive"),
        ("STRUCTURED", "structured"),  # case-insensitive
        ("AutoNomous", "structured"),
    ],
)
def test_known_aliases_normalize(raw: str, expected: str):
    assert normalize_mode(raw) == expected


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["", "unknown", "yolo", "manual"])
def test_unknown_input_falls_back_to_default(raw: str):
    assert normalize_mode(raw) == DEFAULT_MODE


@pytest.mark.unit
def test_every_valid_mode_normalizes_to_a_canonical_mode():
    """Sanity: every alias in MODE_ALIASES maps into CANONICAL_MODES."""
    assert VALID_MODES == set(MODE_ALIASES.keys())
    for alias in VALID_MODES:
        assert normalize_mode(alias) in CANONICAL_MODES
