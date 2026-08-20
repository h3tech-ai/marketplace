"""Layer 1 — `summary/receipts.py` findings aggregation.

Regression coverage for #145: `extract_findings` crashed with
`TypeError: unsupported operand type(s) for +: 'int' and 'list'` when a
receipt expressed per-severity findings as a *list* of objects
(e.g. `{"high": [{...}, {...}]}`) instead of a scalar count. The Case-2
"summary dict with counts" branch assumed every value was an int and
aborted the whole summary build (blocking Status refresh + report
generation).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MODULE_PATH = (
    _REPO_ROOT / "plugin" / "skills" / "_shared" / "scripts" / "summary" / "receipts.py"
)


@pytest.fixture(scope="module")
def receipts_mod():
    spec = importlib.util.spec_from_file_location("summary_receipts", _MODULE_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _receipt(findings) -> dict:
    """Minimal Case-2 receipt carrying a summary-dict `_findings`."""
    return {"role": "quality-engineer", "story_id": "US-001", "_findings": findings}


def test_list_valued_severity_is_counted_not_crashing(receipts_mod):
    """#145: list-valued severities are coerced to their length, not added raw."""
    receipts = [_receipt({"critical": [{"id": "F1"}], "high": [{"id": "F2"}, {"id": "F3"}]})]
    out = receipts_mod.extract_findings(receipts)
    assert out["summary"]["critical"] == 1
    assert out["summary"]["high"] == 2
    assert out["summary"]["medium"] == 0


def test_scalar_valued_severity_still_works(receipts_mod):
    """No regression for the original scalar-count shape."""
    out = receipts_mod.extract_findings([_receipt({"high": 3, "low": 1})])
    assert out["summary"]["high"] == 3
    assert out["summary"]["low"] == 1


def test_mixed_and_garbage_values_coerce_safely(receipts_mod):
    """Non-int / non-list values degrade to 0 rather than raising."""
    receipts = [
        _receipt({"critical": None, "high": "oops", "medium": [{}, {}, {}], "low": 2})
    ]
    out = receipts_mod.extract_findings(receipts)
    assert out["summary"]["critical"] == 0
    assert out["summary"]["high"] == 0
    assert out["summary"]["medium"] == 3
    assert out["summary"]["low"] == 2


def test_multiple_receipts_accumulate(receipts_mod):
    """Counts sum across receipts, mixing list- and scalar-valued shapes."""
    receipts = [
        _receipt({"high": [{"id": "A"}, {"id": "B"}]}),
        _receipt({"high": 1, "critical": 2}),
    ]
    out = receipts_mod.extract_findings(receipts)
    assert out["summary"]["high"] == 3
    assert out["summary"]["critical"] == 2
