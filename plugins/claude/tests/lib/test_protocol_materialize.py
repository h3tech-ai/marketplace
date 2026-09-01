"""Layer 1 — fail-closed protocol materialization (ADR-016).

Dist packages ship no on-disk `skills/_shared/protocols/` fallback. An empty
`skills get` must leave the required set missing so SessionStart / subagentStart
can refuse, not silently start with zero protocol files.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from protocol_materialize import REQUIRED_PROTOCOLS, materialize_protocols

_SHIPPED_PROTOCOL_REF = re.compile(r"\.synaptory/\.protocols/([a-z0-9-]+)\.md")
_SHIPPED_ROOTS = (
    Path(__file__).resolve().parents[2] / "agents",
    Path(__file__).resolve().parents[2] / "skills",
    Path(__file__).resolve().parents[3] / "plugin-cursor" / "roles",
    Path(__file__).resolve().parents[3] / "plugin-cursor" / "skills",
    Path(__file__).resolve().parents[3] / "plugin-cursor" / "agents",
)
_SKIP_DIR_NAMES = {"__pycache__", "tests", ".pytest_cache"}


def _shipped_protocol_refs() -> set[str]:
    """Names referenced as `.synaptory/.protocols/<name>.md` in shipped bodies.

    Independent of REQUIRED_PROTOCOLS so a stale required-set cannot define
    its own completeness.
    """
    names: set[str] = set()
    for root in _SHIPPED_ROOTS:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in _SKIP_DIR_NAMES for part in path.parts):
                continue
            if path.suffix.lower() not in {".md", ".tmpl", ".txt"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            names.update(_SHIPPED_PROTOCOL_REF.findall(text))
    return names


@pytest.mark.unit
def test_cold_cache_without_cli_or_disk_is_missing(tmp_path: Path):
    empty_plugin = tmp_path / "empty-plugin"
    empty_plugin.mkdir()
    project = tmp_path / "project"
    missing = materialize_protocols(
        str(project),
        plugin_root=empty_plugin,
        cli=None,
        host="claude",
    )
    assert "receipt-protocol" in missing
    assert "ephemeral-environments" in missing
    assert "local-deploy-verification" in missing
    assert "design-grooming" in missing
    assert "sa-triggers" in missing
    dest = project / ".synaptory" / ".protocols"
    assert not (dest / "receipt-protocol.md").exists() or (
        dest / "receipt-protocol.md"
    ).stat().st_size == 0


@pytest.mark.unit
def test_partial_fetch_reports_the_missing_names(tmp_path: Path):
    empty_plugin = tmp_path / "empty-plugin"
    empty_plugin.mkdir()
    project = tmp_path / "project"

    def fetch(name: str) -> str:
        if name == "receipt-protocol":
            return "# receipt-protocol\n"
        return ""

    missing = materialize_protocols(
        str(project),
        plugin_root=empty_plugin,
        fetch_one=fetch,
    )
    assert "receipt-protocol" not in missing
    assert "iron-laws" in missing
    assert (project / ".synaptory" / ".protocols" / "receipt-protocol.md").is_file()


@pytest.mark.unit
def test_complete_fetch_writes_every_required_body(tmp_path: Path):
    empty_plugin = tmp_path / "empty-plugin"
    empty_plugin.mkdir()
    project = tmp_path / "project"
    missing = materialize_protocols(
        str(project),
        plugin_root=empty_plugin,
        fetch_one=lambda name: "# %s\n" % name,
    )
    assert missing == []
    dest = project / ".synaptory" / ".protocols"
    for name in REQUIRED_PROTOCOLS:
        path = dest / ("%s.md" % name)
        assert path.is_file() and path.stat().st_size > 0


@pytest.mark.unit
def test_required_set_covers_shipped_protocol_references():
    """REQUIRED_PROTOCOLS must not be its own completeness definition.

    Shipped workflows read `.synaptory/.protocols/<name>.md`. If a live name
    is missing from the required set, SessionStart can exit 0 in a dist
    install while those files stay absent.
    """
    referenced = _shipped_protocol_refs()
    assert "design-grooming" in referenced
    assert "sa-triggers" in referenced
    missing = sorted(referenced - set(REQUIRED_PROTOCOLS))
    assert missing == [], (
        "shipped .synaptory/.protocols/*.md references not in REQUIRED_PROTOCOLS: %s"
        % ", ".join(missing)
    )
