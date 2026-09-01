"""Layer 1 — the story-parallelism baseline harness (benchmarks/parallelism).

Hypothesis: the harness has exactly one job that matters, and it is easy to get
subtly wrong. It must distinguish stories whose SE windows OVERLAP from stories
whose SE windows are merely ADJACENT.

That distinction is the entire measurement. §19.4 makes this baseline a
precondition for the SPQ cutover, and the number it produces will be used to
decide whether a slow first Cycle is the barrier or the inherited per-Work-Unit
serialization. A harness that reported adjacency as concurrency would certify a
serial run as parallel and make the whole exercise worse than useless.

Timestamps in `pipeline_log` are second-granularity, so a story exiting SE at
the same second the next enters is common. Hence the explicit ordering rule in
`_max_overlap`: ends before starts at equal timestamps.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
MEASURE = REPO_ROOT / "benchmarks" / "parallelism" / "measure.py"

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(not MEASURE.is_file(), reason="benchmark harness absent"),
]


def _load():
    spec = importlib.util.spec_from_file_location("measure", MEASURE)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


T0 = datetime(2026, 7, 30, 9, 0, 0, tzinfo=timezone.utc)


def _iso(minutes: int) -> str:
    return (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _story(sid: str, windows: list[tuple[str, int, int]], state: str = "done") -> dict:
    return {
        "id": sid, "title": sid, "state": state,
        "pipeline_log": [
            {"state": st, "entered_at": _iso(a), "exited_at": _iso(a + ln)}
            for st, a, ln in windows
        ],
    }


def _project(tmp_path: Path, stories: list[dict], config: str = "") -> Path:
    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True, exist_ok=True)
    (orch / "pipeline-state.json").write_text(json.dumps({
        "version": "2.0", "build_mode": "scrum",
        "lifecycle_state": "SPRINT_EXECUTION", "current_sprint": 1,
        "current_stories": stories,
    }))
    if config:
        (tmp_path / ".synaptory.yaml").write_text(config)
    return tmp_path


# ---------------------------------------------------------------------------
# The one thing that matters
# ---------------------------------------------------------------------------

def test_adjacent_se_windows_are_not_concurrent(tmp_path: Path):
    """Story B enters SE the same second A exits. That is serial."""
    m = _load()
    p = _project(tmp_path, [
        _story("BM-1", [("in_progress", 0, 20)]),
        _story("BM-2", [("in_progress", 20, 20)]),
        _story("BM-3", [("in_progress", 40, 20)]),
    ])
    out = m.analyse(str(p))
    assert out["observed_max_concurrency"] == 1
    assert out["parallelism_actually_happened"] is False


def test_overlapping_se_windows_are_concurrent(tmp_path: Path):
    m = _load()
    p = _project(tmp_path, [
        _story("BM-1", [("in_progress", 0, 20)]),
        _story("BM-2", [("in_progress", 0, 20)]),
        _story("BM-3", [("in_progress", 5, 20)]),
    ])
    out = m.analyse(str(p))
    assert out["observed_max_concurrency"] == 3
    assert out["parallelism_actually_happened"] is True
    assert sorted(out["stages"]["in_progress"]["concurrent_at_peak"]) == [
        "BM-1", "BM-2", "BM-3"]


def test_partial_overlap_reports_the_true_peak(tmp_path: Path):
    """Two overlap, the third is adjacent — the peak is 2, not 3."""
    m = _load()
    p = _project(tmp_path, [
        _story("BM-1", [("in_progress", 0, 20)]),
        _story("BM-2", [("in_progress", 10, 20)]),
        _story("BM-3", [("in_progress", 30, 20)]),
    ])
    assert _load().analyse(str(p))["observed_max_concurrency"] == 2


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def test_serial_equivalent_sums_per_story_cycle_time(tmp_path: Path):
    m = _load()
    p = _project(tmp_path, [
        _story("BM-1", [("in_progress", 0, 20), ("testing", 20, 10)]),
        _story("BM-2", [("in_progress", 30, 20), ("testing", 50, 10)]),
    ])
    out = m.analyse(str(p))
    assert out["serial_equivalent_seconds"] == 60 * 60      # 4 windows x 15m avg
    assert out["wall_clock_seconds"] == 60 * 60             # 09:00 -> 10:00
    assert out["realised_speedup"] == 1.0


def test_parallel_run_shows_a_speedup(tmp_path: Path):
    m = _load()
    p = _project(tmp_path, [
        _story("BM-1", [("in_progress", 0, 20), ("testing", 20, 10)]),
        _story("BM-2", [("in_progress", 0, 20), ("testing", 30, 10)]),
    ])
    out = m.analyse(str(p))
    assert out["realised_speedup"] > 1.0
    assert out["observed_max_concurrency"] == 2


def test_cancelled_units_are_excluded_from_quality_counts(tmp_path: Path):
    """Cut work is work the PO decided not to ship, not work that failed."""
    m = _load()
    p = _project(tmp_path, [
        _story("BM-1", [("in_progress", 0, 20)]),
        _story("BM-2", [("in_progress", 0, 20)], state="cancelled"),
    ])
    out = m.analyse(str(p))
    assert out["work_units"]["total"] == 1
    assert out["work_units"]["cut"] == 1


def test_qe_and_cr_are_marked_serial_by_design(tmp_path: Path):
    """`_parallel_batch` batches only dispatch_se. If a future change starts
    batching QE, this reading is how anyone would notice."""
    m = _load()
    p = _project(tmp_path, [_story("BM-1", [
        ("in_progress", 0, 20), ("testing", 20, 10), ("reviewing", 30, 5)])])
    stages = m.analyse(str(p))["stages"]
    assert stages["in_progress"]["batchable"] is True
    assert stages["testing"]["batchable"] is False
    assert stages["reviewing"]["batchable"] is False


def test_multispec_state_measures_the_active_slot(tmp_path: Path):
    m = _load()
    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "pipeline-state.json").write_text(json.dumps({
        "version": "3.0", "build_mode": "scrum", "active_spec": "beta",
        "specs": {
            "alpha": {"current_stories": [_story("A-1", [("in_progress", 0, 5)])]},
            "beta": {"current_stories": [
                _story("B-1", [("in_progress", 0, 20)]),
                _story("B-2", [("in_progress", 0, 20)]),
            ]},
        },
    }))
    out = m.analyse(str(tmp_path))
    assert out["work_units"]["total"] == 2
    assert out["observed_max_concurrency"] == 2


# ---------------------------------------------------------------------------
# Reporting honesty
# ---------------------------------------------------------------------------

def test_comparison_flags_an_invalid_parallel_run(tmp_path: Path):
    """A 'parallel' arm that never overlapped measured serial execution with a
    different config file. The report must refuse to hand over a speedup."""
    m = _load()
    par_cfg = ('build_mode: "scrum"\nparallelism:\n'
               "  story_parallelism: enabled\n  max_concurrent_subagents: 3\n")
    serial = _project(tmp_path / "s", [
        _story("BM-1", [("in_progress", 0, 20)]),
        _story("BM-2", [("in_progress", 20, 20)])])
    par = _project(tmp_path / "p", [
        _story("BM-1", [("in_progress", 0, 20)]),
        _story("BM-2", [("in_progress", 20, 20)])], config=par_cfg)

    report = m.compare(m.analyse(str(serial)), m.analyse(str(par)))
    # Normalise whitespace: the report is line-wrapped for the terminal, so the
    # phrase spans a newline.
    flat = " ".join(report.lower().split())
    assert "invalid comparison" in flat
    assert "do not quote a speedup" in flat


def test_control_arm_reading_of_one_is_not_reported_as_a_problem(tmp_path: Path):
    """Concurrency 1 is CORRECT for the control arm; flagging it as a defect
    would train the reader to ignore the warning that matters."""
    m = _load()
    p = _project(tmp_path, [_story("BM-1", [("in_progress", 0, 20)])],
                 config='build_mode: "scrum"\n')
    text = m.render(m.analyse(str(p)))
    assert "PROBLEM" not in text
    assert "control arm" in text


def test_enabled_but_serial_is_reported_as_a_problem(tmp_path: Path):
    m = _load()
    p = _project(tmp_path, [
        _story("BM-1", [("in_progress", 0, 20)]),
        _story("BM-2", [("in_progress", 20, 20)])],
        config=('build_mode: "scrum"\nparallelism:\n'
                "  story_parallelism: enabled\n"))
    text = m.render(m.analyse(str(p)))
    assert "PROBLEM" in text
    assert "did not happen" in text


# ---------------------------------------------------------------------------
# The backlog fixture
# ---------------------------------------------------------------------------

def test_backlog_shape_exercises_every_eligibility_rule():
    """A backlog of nine independent stories would measure the concurrency cap
    and nothing else. This one must also cover dependencies, scope overlap and
    an undeclared scope."""
    backlog = json.loads(
        (REPO_ROOT / "benchmarks" / "parallelism" / "backlog.json").read_text())
    units = {u["id"]: u for u in backlog["work_units"]}
    assert len(units) == 9

    independent = [u for u in units.values()
                   if not u.get("depends_on") and u.get("file_scope")]
    assert len(independent) >= 4, "need enough independent units to fill a batch"

    chained = [u for u in units.values() if u.get("depends_on")]
    assert len(chained) >= 2, "need a dependency chain that must serialize"

    scopes = [tuple(u.get("file_scope") or []) for u in units.values()]
    flat = [f for s in scopes for f in s]
    assert len(flat) != len(set(flat)), "need at least one overlapping file_scope"

    assert any(not u.get("file_scope") for u in units.values()), (
        "need one unit with no declared file_scope (not batchable under `shared`)"
    )


def test_every_backlog_unit_has_acceptance_criteria():
    """DoR requires them, and a unit without them stalls the pipeline rather
    than producing a measurement."""
    backlog = json.loads(
        (REPO_ROOT / "benchmarks" / "parallelism" / "backlog.json").read_text())
    for u in backlog["work_units"]:
        assert u.get("acceptance_criteria"), f"{u['id']} has no acceptance criteria"


# ---------------------------------------------------------------------------
# Fixture defects found by running the harness for real (#237)
# ---------------------------------------------------------------------------

BENCH = REPO_ROOT / "benchmarks" / "parallelism"


def test_setup_ignores_bytecode_before_the_baseline_commit():
    """A scaffolded arm must ignore `__pycache__` BEFORE `git add -A`.

    Without it the copied demo-app's .pyc files land in the baseline commit,
    and under `isolation: worktree` each concurrent unit recompiles them
    differently. The integration merge then fails on binary add/add conflicts
    before any source conflict is visible, and no single unit can fix it: a
    shared .gitignore is outside every unit's file_scope.
    """
    text = (BENCH / "setup.sh").read_text()
    assert '.gitignore' in text, "setup.sh writes no .gitignore into the arm"
    ignore_at = text.index('"$TARGET/.gitignore"')
    add_at = text.index('add -A')
    assert ignore_at < add_at, ".gitignore must be written before `git add -A`"
    for pattern in ("__pycache__/", "*.pyc"):
        assert pattern in text, f"the arm's .gitignore omits {pattern}"


def test_every_declared_file_scope_can_satisfy_its_acceptance_criteria():
    """`file_scope` is the INPUT to the eligibility rules, so a scope narrower
    than the ACs need feeds the batch decision data that cannot be built.

    Both entries below are AC text naming a member of a PRE-EXISTING module:
    `TaskStore.list` lives in todo/store.py, the `Task` record in
    todo/models.py. A unit cannot deliver either from a fresh module alone.
    """
    units = {u["id"]: u for u in
             json.loads((BENCH / "backlog.json").read_text())["work_units"]}
    required = {"BM-1": "todo/store.py", "BM-3": "todo/models.py"}
    for uid, path in required.items():
        assert path in (units[uid].get("file_scope") or []), (
            f"{uid} needs {path} to satisfy its acceptance criteria"
        )


def test_the_batchable_units_stay_pairwise_disjoint():
    """BM-1..BM-4 are the disjoint-batch case. Widening a scope is fine as long
    as they remain pairwise disjoint WITH EACH OTHER: BM-8 is meant to be the
    only overlap a batch decision has to resolve."""
    import itertools
    units = {u["id"]: set(u.get("file_scope") or []) for u in
             json.loads((BENCH / "backlog.json").read_text())["work_units"]}
    for a, b in itertools.combinations(["BM-1", "BM-2", "BM-3", "BM-4"], 2):
        assert not (units[a] & units[b]), (
            f"{a} and {b} overlap on {sorted(units[a] & units[b])}; the batch "
            "case needs them pairwise disjoint"
        )
    # The two intended overlaps, and no others.
    overlaps = {frozenset((a, b)) for a, b in itertools.combinations(units, 2)
                if units[a] & units[b]}
    assert overlaps == {frozenset(("BM-2", "BM-8")), frozenset(("BM-1", "BM-6"))}


def test_the_readme_dispatch_count_matches_the_pinned_dod_tier():
    """Both arms pin `dod_tier: early`, whose required checks exclude
    `code_reviewed`, so no CR is ever dispatched: 2 dispatches per unit, 18 per
    arm. If a future change adds `code_reviewed` to the early tier, the README's
    number silently becomes wrong, and this is where it surfaces."""
    spec = importlib.util.spec_from_file_location(
        "story_pipeline",
        REPO_ROOT / "plugin-claude" / "hooks" / "lib" / "story_pipeline.py")
    sp = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(sp)

    setup = (BENCH / "setup.sh").read_text()
    assert 'dod_tier: "early"' in setup, "the arms no longer pin the early tier"
    roles_per_unit = 3 if "code_reviewed" in sp.DOD_TIER_CHECKS["early"] else 2
    units = len(json.loads((BENCH / "backlog.json").read_text())["work_units"])

    readme = (BENCH / "README.md").read_text()
    expected = f"{roles_per_unit * units} subagent dispatches"
    assert expected in readme, f"README should quote '{expected}'"


def test_the_demo_app_does_not_suppress_its_own_test_summary():
    """QE receipts quote this command's output as tests_pass evidence, and the
    DoD gate keys only on exit_code. An `-q` in addopts silently became `-qq`
    for anyone typing the habitual `pytest -q`, which drops the summary line and
    leaves a human unable to see how many tests ran."""
    ini = (BENCH / "demo-app" / "pytest.ini").read_text()
    addopts = [ln for ln in ini.splitlines()
               if ln.strip().startswith("addopts")]
    assert not any("-q" in ln for ln in addopts), (
        "addopts must not carry -q: it compounds with an operator's own -q"
    )
