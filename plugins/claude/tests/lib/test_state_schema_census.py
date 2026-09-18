"""Prevent another reader from using a release string to select state layout."""
import ast
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCAN_ROOTS = ("core", "cli/internal", "plugin-claude/hooks", "plugin-cursor/mcp",
              "plugin-codex/plugins/synaptory/hooks", "plugin-codex/plugins/synaptory/scripts")
SKIP = {"tests", "fixtures", "testdata", "__pycache__", "runtime", "_vendor"}
ACCESSORS = {"core/lib/state_schema.py", "cli/internal/specstate/layout.go"}


def direct_comparisons(source, suffix):
    if suffix == ".py":
        tree = ast.parse(source)
        aliases = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                if "version" in ast.unparse(node.value).lower():
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    aliases.update(target.id for target in targets if isinstance(target, ast.Name))
        return [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Compare)
                and any(isinstance(child, ast.Constant) and isinstance(child.value, str)
                        and re.fullmatch(r"[0-9]+\.[0-9]+", child.value)
                        for child in ast.walk(node))
                and ("version" in ast.unparse(node).lower()
                     or any(isinstance(child, ast.Name) and child.id in aliases
                            for child in ast.walk(node)))]
    pattern = re.compile(r'(?:version.*(?:==|!=|=).*?[\"\'][0-9]+\.[0-9]+[\"\']|[\"\'][0-9]+\.[0-9]+[\"\'].*(?:==|!=|=).*version)', re.I)
    lines = [i for i, line in enumerate(source.splitlines(), 1)
             if not line.lstrip().startswith(("//", "#")) and pattern.search(line)]
    if suffix == ".go":
        for match in re.finditer(r'switch\s+[^\n{]*[Vv]ersion[^\n{]*\{(?P<body>[^}]+)', source):
            if re.search(r'case\s+"[0-9]+\.[0-9]+"', match.group("body")):
                lines.append(source[:match.start()].count("\n") + 1)
    return lines


def test_no_direct_layout_version_comparisons():
    found = []
    for root in SCAN_ROOTS:
        for path in (ROOT / root).rglob("*"):
            if not path.is_file() or path.is_symlink() or path.suffix not in (".py", ".go", ".sh"):
                continue
            relative = path.relative_to(ROOT)
            if any(part in SKIP for part in relative.parts) or path.name.startswith("test_") or path.name.endswith("_test.go"):
                continue
            if relative.as_posix() in ACCESSORS:
                continue
            found.extend(f"{relative}:{line}" for line in direct_comparisons(path.read_text(), path.suffix))
    assert not found, "Use the state_schema accessor, not a release comparison: " + ", ".join(found)


@pytest.mark.parametrize("source,suffix", [
    ('if full.get("version") == "2.0": pass', ".py"),
    ('if full.get("version") == "4.0": pass', ".py"),
    ('if "3.0" != state["version"]: pass', ".py"),
    ('if raw.Version == "3.0" {', ".go"),
    ('v = state.get("version")\nif v == "2.0": pass', ".py"),
    ('switch raw.Version {\ncase "3.0": return true\n}', ".go"),
    ('if [ "$version" = "2.0" ]; then', ".sh"),
])
def test_census_detects_a_new_reader(source, suffix):
    assert direct_comparisons(source, suffix)
