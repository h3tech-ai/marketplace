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
    _REPO_ROOT / "plugin-claude" / "skills" / "_shared" / "scripts" / "summary" / "receipts.py"
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


# ── #847: one malformed receipt must not abort the summary ──────────────────
#
# `/synaptory status` is built from `pipeline-summary.json`. A legacy receipt
# whose `findings` was a list of plain strings raised AttributeError inside
# `extract_findings`, the whole build died, and the previous summary stayed on
# disk to be read as current. Every shape below used to raise; each must now
# build, say what it dropped in `warnings`, and keep the rest of the summary.


def _named(findings_key: dict, filename: str = "US-001-qe.json") -> dict:
    """A normalized-receipt stand-in carrying the file name warnings must cite."""
    r = {"role": "quality-engineer", "story_id": "US-001", "_filename": filename}
    r.update(findings_key)
    return r


def test_string_finding_is_counted_as_unknown_and_names_the_receipt(receipts_mod):
    """The reported shape: `findings: ["some finding text"]`."""
    warnings: list = []
    out = receipts_mod.extract_findings(
        [_named({"_findings": ["some finding text"]})], warnings
    )
    assert out["summary"]["unknown"] == 1
    assert out["summary"]["medium"] == 0, "must not be promoted into the display default"
    assert [i["title"] for i in out["items"]] == ["some finding text"]
    assert out["items"][0]["severity"] == "unknown"
    assert len(warnings) == 1 and "US-001-qe.json" in warnings[0]


def test_string_finding_is_not_marked_fixed_by_an_unrelated_fix(receipts_mod):
    """An id-less finding has an empty id, and "" is a substring of every string."""
    receipts = [
        _named({"_findings": ["legacy text finding"]}),
        _named({"_fixes": [{"finding": "SR-09", "severity": "low", "title": "other"}]},
               "US-002-se.json"),
    ]
    out = receipts_mod.extract_findings(receipts)
    legacy = [i for i in out["items"] if i["title"] == "legacy text finding"]
    assert legacy and legacy[0]["status"] == "open"


@pytest.mark.parametrize("entry", [None, 3, True, ["a"], "", "   "])
def test_non_dict_non_text_finding_entries_are_skipped_with_a_warning(receipts_mod, entry):
    warnings: list = []
    out = receipts_mod.extract_findings(
        [_named({"_findings": [entry, {"id": "F1", "severity": "high"}]})], warnings
    )
    assert out["summary"]["high"] == 1
    assert out["summary"]["unknown"] == 0
    assert [i["id"] for i in out["items"]] == ["F1"]
    assert warnings and all("US-001-qe.json" in w for w in warnings)


@pytest.mark.parametrize("severity", [None, 3, ["high"], {"a": 1}, ""])
def test_unusable_severity_is_unknown(receipts_mod, severity):
    warnings: list = []
    out = receipts_mod.extract_findings(
        [_named({"_findings": [{"id": "F1", "title": "t", "severity": severity}]})],
        warnings,
    )
    assert out["summary"]["unknown"] == 1
    assert out["items"][0]["severity"] == "unknown"
    assert any("US-001-qe.json" in w for w in warnings)


def test_missing_severity_key_keeps_the_medium_default(receipts_mod):
    """Not part of #847: changing it would shift every existing count."""
    out = receipts_mod.extract_findings(
        [_named({"_findings": [{"id": "F1", "title": "t"}]})]
    )
    assert out["summary"]["medium"] == 1
    assert out["summary"]["unknown"] == 0


def test_severity_is_case_insensitive_as_before(receipts_mod):
    out = receipts_mod.extract_findings(
        [_named({"_findings": [{"id": "F1", "severity": "HIGH"}]})]
    )
    assert out["summary"]["high"] == 1


@pytest.mark.parametrize("fid", [["a"], {"a": 1}, None, True])
def test_unhashable_or_non_text_id_is_treated_as_absent(receipts_mod, fid):
    out = receipts_mod.extract_findings(
        [_named({"_findings": [{"id": fid, "title": "t", "severity": "low"}]})]
    )
    assert out["items"][0]["id"] == ""
    assert out["items"][0]["title"] == "t"


def test_numeric_id_is_kept_as_text(receipts_mod):
    out = receipts_mod.extract_findings(
        [_named({"_findings": [{"id": 7, "title": "t", "severity": "low"}]})]
    )
    assert out["items"][0]["id"] == "7"


def test_null_title_is_treated_as_absent(receipts_mod):
    out = receipts_mod.extract_findings(
        [_named({"_findings": [{"id": "F1", "title": None, "severity": "low"}]})]
    )
    assert out["items"][0]["title"] == ""


def test_null_fixes_is_ignored(receipts_mod):
    out = receipts_mod.extract_findings(
        [_named({"_findings": {"high": 1}, "_fixes": None})]
    )
    assert out["summary"]["high"] == 1


def test_fix_with_null_severity_is_tolerated(receipts_mod):
    out = receipts_mod.extract_findings(
        [_named({"_fixes": [{"severity": None, "title": "patched", "finding": "F1"}]})]
    )
    # An unreadable fix severity falls to the historical `high` for a fix that
    # matched nothing, exactly as an absent one did.
    assert [i["severity"] for i in out["items"]] == ["high"]
    assert out["items"][0]["status"] == "fixed"


@pytest.mark.parametrize("ref", [5, ["F1"], {"a": 1}])
def test_fix_with_non_text_finding_ref_is_tolerated(receipts_mod, ref):
    out = receipts_mod.extract_findings(
        [
            _named({"_findings": [{"id": "F1", "severity": "high"}]}),
            _named({"_fixes": [{"finding": ref, "title": "patched"}]}, "US-002-se.json"),
        ]
    )
    assert out["summary"]["high"] == 1


def test_non_dict_fix_entry_is_skipped_with_a_warning(receipts_mod):
    warnings: list = []
    receipts_mod.extract_findings(
        [_named({"_fixes": ["F1", None]}, "US-002-se.json")], warnings
    )
    assert warnings and all("US-002-se.json" in w for w in warnings)


@pytest.mark.parametrize("blocking", [None, "SR-01: broken", 4, {"a": 1}])
def test_malformed_blocking_issues_is_ignored(receipts_mod, blocking):
    out = receipts_mod.extract_findings(
        [_named({"_findings": {"critical": 1}, "_blocking_issues": blocking})]
    )
    assert out["summary"]["critical"] == 1
    assert out["items"] == []


@pytest.mark.parametrize("sr", [None, ["SR-01"], "FAIL", 3])
def test_malformed_sr_results_is_ignored(receipts_mod, sr):
    out = receipts_mod.extract_findings(
        [_named({"_findings": {"critical": 1}, "_sr_results": sr})]
    )
    assert out["summary"]["critical"] == 1
    assert out["items"] == []


def test_blocking_issues_and_sr_results_still_extract(receipts_mod):
    """No regression for the well-formed shapes the tolerance sits beside."""
    out = receipts_mod.extract_findings(
        [_named({
            "_findings": {"critical": 1},
            "_blocking_issues": ["SR-01: broken"],
            "_sr_results": {"SR-02": "PARTIAL"},
        })]
    )
    assert {i["id"]: i["severity"] for i in out["items"]} == {
        "SR-01": "critical", "SR-02": "medium",
    }


def test_non_container_findings_is_ignored_with_a_warning(receipts_mod):
    warnings: list = []
    out = receipts_mod.extract_findings([_named({"_findings": "none"})], warnings)
    assert sum(out["summary"].values()) == 0
    assert warnings and "US-001-qe.json" in warnings[0]


def test_a_well_formed_receipt_produces_no_warnings(receipts_mod):
    warnings: list = []
    receipts_mod.extract_findings(
        [_named({"_findings": [{"id": "F1", "title": "t", "severity": "high"}]})], warnings
    )
    assert warnings == []


def test_null_verification_commands_normalizes_to_an_empty_list(receipts_mod):
    r = receipts_mod.normalize_receipt(
        {"story_id": "US-001", "role": "qe", "verification_commands": None}
    )
    assert r["verification_commands"] == []
    assert receipts_mod.shape_warnings(
        {"_filename": "US-001-qe.json", "verification_commands": None}
    )


def test_unknown_findings_are_visible_in_the_rendered_reports(receipts_mod):
    """Counted in the totals but absent from the tables was the other half of the
    problem: a bucket the renderers do not loop over is invisible."""
    reports = _REPO_ROOT / "plugin-claude" / "skills" / "_shared" / "scripts" / "reports"

    def load(name):
        spec = importlib.util.spec_from_file_location(f"rep_{name}", reports / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    findings = receipts_mod.extract_findings(
        [_named({"_findings": ["some finding text"]})]
    )
    summary = {"findings": findings}

    md = load("pipeline_md")._md_findings(summary)
    assert "UNKNOWN" in md and "some finding text" in md

    html = load("pipeline_html")._html_findings(summary)
    assert 'data-sev="unknown"' in html and "Unknown (1)" in html
    assert "some finding text" in html
