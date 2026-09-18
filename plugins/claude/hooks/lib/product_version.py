"""Resolve the executing Synaptory product release, never a project schema.

All host composers keep this module at hooks/lib/product_version.py. In a
source checkout its real path is core/lib/product_version.py. In both cases
parents[2] is the release root. Ignore host/project environment overrides so
an unrelated installation cannot relabel state written by this runtime.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

RUNTIME_ROOT = Path(__file__).resolve().parents[2]
_RELEASE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\Z")


def product_version() -> str:
    """Read VERSION in source/Cursor, or the installed Claude/Codex manifest."""
    version_file = RUNTIME_ROOT / "VERSION"
    if version_file.is_file():
        value = version_file.read_text(encoding="utf-8").strip()
    else:
        value = None
        for host in (".claude-plugin", ".codex-plugin", ".cursor-plugin"):
            manifest = RUNTIME_ROOT / host / "plugin.json"
            if manifest.is_file():
                value = json.loads(manifest.read_text(encoding="utf-8")).get("version")
                break
    if not isinstance(value, str) or not _RELEASE.fullmatch(value):
        raise ValueError(f"Missing or invalid Synaptory product release metadata at {RUNTIME_ROOT}")
    return value


def render_config_template(template: str) -> str:
    """Resolve the product placeholder before filling project-specific answers."""
    return template.replace("{{PRODUCT_VERSION}}", product_version())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, help="Print a config template with its release resolved")
    args = parser.parse_args()
    if args.template:
        print(render_config_template(args.template.read_text(encoding="utf-8")), end="")
    else:
        print(product_version())
