"""Layer 1 — #134 pipeline changes (WP1/WP3/WP4/WP5 story_pipeline surface).

Covers: verification-tier resolution + kind suppression (GAP-3/4), the
evidence-based DoD check fallback + code_reviewed any_of (GAP-1b/GAP-10),
explicit DoD-tier resolution (GAP-11), the next_action `dod` block +
parallel batch (GAP-6/8), and interrupted-dispatch detection (GAP-12).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_HOOKS_LIB = _PLUGIN_ROOT / "hooks" / "lib"
if str(_HOOKS_LIB) not in sys.path:
    sys.path.insert(0, str(_HOOKS_LIB))

import story_pipeline as sp  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers (mirror test_ui_acceptance_gate conventions)
# ---------------------------------------------------------------------------


def _project(tmp_path: Path, config: str = "") -> Path:
    (tmp_path / ".synaptory" / ".orchestrator" / "receipts").mkdir(
        parents=True, exist_ok=True
    )
    if config:
        (tmp_path / ".synaptory.yaml").write_text(config, encoding="utf-8")
    return tmp_path


def _write_receipt(project: Path, story_id: str, role: str, **fields) -> None:
    receipts = project / ".synaptory" / ".orchestrator" / "receipts"
    abbrev = sp._role_to_abbrev(role)
    (receipts / f"{story_id}-{abbrev}.json").write_text(
        json.dumps({"agent": role, **fields}), encoding="utf-8"
    )


def _seed_story(project: Path, story_id: str, title: str = "", acs=None, **kw) -> None:
    story = sp.create_story(story_id, title, acceptance_criteria=acs, **kw)
    state = {
        "current_stories": [story],
        "current_sprint": 1,
        "lifecycle_state": "SPRINT_EXECUTION",
    }
    sp._write_state(str(project), state)


def _dod(project: Path, story_id: str, intensity: str = "early") -> dict:
    receipts = str(project / ".synaptory" / ".orchestrator" / "receipts")
    return sp.evaluate_story_dod(
        str(project), story_id, intensity, receipts_dir=receipts
    )


# ---------------------------------------------------------------------------
# GAP-3 — verification-tier resolution
# ---------------------------------------------------------------------------


VERIFICATION_CFG = """\
quality:
  verification:
    default: standard
    by_kind:
      enabler: minimal
      ui: full
    stories:
      US-9: full
"""


@pytest.mark.unit
def test_tier_precedence_story_beats_kind_beats_default(tmp_path):
    project = _project(tmp_path, VERIFICATION_CFG)
    assert sp.resolve_verification_tier(project, {"id": "US-9", "kind": "enabler"}) == "full"
    assert sp.resolve_verification_tier(project, {"id": "US-1", "kind": "enabler"}) == "minimal"
    assert sp.resolve_verification_tier(project, {"id": "US-1", "kind": "mixed"}) == "standard"


@pytest.mark.unit
def test_tier_legacy_runtime_verification_maps_to_full(tmp_path):
    project = _project(tmp_path, "quality:\n  runtime_verification: required\n")
    assert sp.resolve_verification_tier(project, {"id": "US-1"}) == "full"


@pytest.mark.unit
def test_tier_default_is_standard_without_config(tmp_path):
    project = _project(tmp_path)
    assert sp.resolve_verification_tier(project, {"id": "US-1"}) == "standard"
    assert sp.resolve_verification_tier(project, None) == "standard"


@pytest.mark.unit
def test_tier_unknown_values_ignored(tmp_path):
    project = _project(
        tmp_path, "quality:\n  verification:\n    default: bananas\n"
    )
    assert sp.resolve_verification_tier(project, {"id": "US-1"}) == "standard"


@pytest.mark.unit
def test_full_tier_promotes_runtime_gate(tmp_path):
    project = _project(tmp_path, "quality:\n  verification:\n    default: full\n")
    _seed_story(project, "US-1", "Persist orders to Postgres")
    _write_receipt(
        project, "US-1", "software-engineer",
        verification_commands=[{"command": "npm run build", "exit_code": 0}],
    )
    result = _dod(project, "US-1")
    assert result["verification_tier"] == "full"
    assert result["runtime_required"] is True
    assert result["checks"]["runtime_verified"]["required"] is True


@pytest.mark.unit
def test_minimal_tier_suppresses_ui_gate(tmp_path):
    project = _project(
        tmp_path,
        "quality:\n  verification:\n    by_kind:\n      enabler: minimal\n",
    )
    # UI-verby ACs, but the PO tagged it an enabler at minimal tier.
    _seed_story(
        project, "US-2", "Define color tokens",
        acs=["The component renders tokens on every page"],
        kind="enabler",
    )
    result = _dod(project, "US-2")
    assert result["verification_tier"] == "minimal"
    assert result["ui_required"] is False
    assert result["runtime_required"] is False


@pytest.mark.unit
def test_standard_tier_accepts_render_assertion(tmp_path):
    project = _project(tmp_path)  # default standard
    _seed_story(
        project, "US-3", "Landing page",
        acs=["The page renders the hero section"],
    )
    _write_receipt(
        project, "US-3", "quality-engineer",
        verification_commands=[{"command": "npm test", "exit_code": 0}],
        # Render assertion only: no full flows exercised.
        metrics={"ui_verification": {"rendered": True, "routes_tested": 1}},
    )
    result = _dod(project, "US-3")
    assert result["verification_tier"] == "standard"
    assert result["ui_required"] is True
    assert result["checks"]["ui_acceptance"]["passed"] is True


@pytest.mark.unit
def test_standard_tier_render_assertion_needs_a_route(tmp_path):
    project = _project(tmp_path)
    _seed_story(project, "US-3", "Landing page", acs=["The page renders"])
    _write_receipt(
        project, "US-3", "quality-engineer",
        verification_commands=[{"command": "npm test", "exit_code": 0}],
        metrics={"ui_verification": {"rendered": True, "routes_tested": 0}},
    )
    result = _dod(project, "US-3")
    assert result["checks"]["ui_acceptance"]["passed"] is False


# ---------------------------------------------------------------------------
# GAP-4 — kind suppression of the ui_bearing heuristic
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["enabler", "infra", "backend"])
def test_non_ui_kind_suppresses_ui_bearing_at_create(kind):
    story = sp.create_story(
        "US-4", "Define color tokens",
        acceptance_criteria=["Component renders tokens", "Page displays palette"],
        kind=kind,
    )
    assert story["ui_bearing"] is False
    assert story["kind"] == kind


@pytest.mark.unit
def test_ui_kind_keeps_heuristic():
    story = sp.create_story(
        "US-5", "Checkout screen", acceptance_criteria=["The form renders"],
        kind="ui",
    )
    assert story["ui_bearing"] is True


@pytest.mark.unit
def test_kind_suppression_wins_over_stale_snapshot(tmp_path):
    project = _project(tmp_path)
    _seed_story(project, "US-6", "Tokens", acs=["renders"], kind="")
    # Simulate a kind assigned after creation with a stale ui_bearing snapshot.
    state = sp._read_state(str(project))
    state["current_stories"][0]["ui_bearing"] = True
    state["current_stories"][0]["kind"] = "enabler"
    sp._write_state(str(project), state)
    assert sp._story_is_ui_bearing_in_state(str(project), "US-6") is False


# ---------------------------------------------------------------------------
# GAP-1b / GAP-10 — code_reviewed any_of + evidence-based fallback
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_code_reviewed_accepts_story_dod_boolean():
    receipt = {"story_dod": {"code_reviewed": True}}
    assert sp._evaluate_check("code_reviewed", receipt) is True


@pytest.mark.unit
def test_code_reviewed_accepts_top_level_status():
    assert sp._evaluate_check("code_reviewed", {"status": "complete"}) is True
    assert sp._evaluate_check("code_reviewed", {"status": "partial"}) is False


@pytest.mark.unit
def test_tests_pass_falls_back_to_se_test_proof(tmp_path):
    """US-013 shape: no QE receipt, but the SE receipt carries executed
    test proof — the gate sources it instead of failing unsourced."""
    project = _project(tmp_path)
    _seed_story(project, "US-7", "Manual audit")
    _write_receipt(
        project, "US-7", "software-engineer",
        verification_commands=[
            {"command": "pnpm build", "exit_code": 0},
            {"command": "pnpm test", "exit_code": 0},
        ],
    )
    result = _dod(project, "US-7")
    assert result["checks"]["tests_pass"]["passed"] is True
    assert result["checks"]["tests_pass"]["source"].endswith("-se.json")


@pytest.mark.unit
def test_build_succeeds_falls_back_to_pe_receipt(tmp_path):
    """US-012 shape: platform-engineer authored the build proof."""
    project = _project(tmp_path)
    _seed_story(project, "US-8", "a11y CI job")
    _write_receipt(
        project, "US-8", "platform-engineer",
        verification_commands=[{"command": "pnpm build", "exit_code": 0}],
    )
    result = _dod(project, "US-8")
    assert result["checks"]["build_succeeds"]["passed"] is True
    assert result["checks"]["build_succeeds"]["source"].endswith("-pe.json")


@pytest.mark.unit
def test_fallback_requires_matching_proof_not_any_green_command(tmp_path):
    """A QE receipt with only pytest proof must NOT satisfy build_succeeds."""
    project = _project(tmp_path)
    _seed_story(project, "US-9b", "Backend story")
    _write_receipt(
        project, "US-9b", "quality-engineer",
        verification_commands=[{"command": "pytest -q", "exit_code": 0}],
    )
    result = _dod(project, "US-9b")
    assert result["checks"]["build_succeeds"]["passed"] is not True


@pytest.mark.unit
def test_code_reviewed_has_no_fallback(tmp_path):
    """An SE receipt claiming review must never satisfy code_reviewed."""
    project = _project(tmp_path)
    _seed_story(project, "US-10", "Story")
    _write_receipt(
        project, "US-10", "software-engineer",
        status="complete",
        story_dod={"code_reviewed": True},
        verification_commands=[{"command": "pnpm build", "exit_code": 0}],
    )
    result = _dod(project, "US-10", intensity="growing")
    assert result["checks"]["code_reviewed"]["passed"] is None
    assert result["passed"] is False


@pytest.mark.unit
def test_unverified_tests_get_explaining_detail(tmp_path):
    project = _project(tmp_path)
    _seed_story(project, "US-11", "Story")
    _write_receipt(
        project, "US-11", "quality-engineer",
        verification_commands=["npm test -- --bail 2>&1 | tail -10"],
    )
    result = _dod(project, "US-11")
    check = result["checks"]["tests_pass"]
    assert check["passed"] is None
    assert "exit_code" in check.get("detail", "")


@pytest.mark.unit
def test_fallback_proof_no_vacuous_pass():
    # Strict matchers: missing metric fields never satisfy via fallback.
    assert sp._evaluate_fallback_proof("no_critical_findings", {"metrics": {}}) is None
    assert sp._evaluate_fallback_proof("coverage_no_decrease", {"metrics": {}}) is None
    assert sp._evaluate_fallback_proof("tests_pass", {"verification_commands": []}) is None


# ---------------------------------------------------------------------------
# GAP-11 — explicit DoD tier
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_dod_tier_precedence(tmp_path):
    project = str(_project(tmp_path))
    state = {"current_sprint": 2}
    # computed: sprint 2 → growing
    info = sp.resolve_dod_tier(project, state)
    assert info["tier"] == "growing" and info["tier_source"] == "computed"
    # config override
    (Path(project) / ".synaptory.yaml").write_text(
        "quality:\n  dod_tier: early\n", encoding="utf-8"
    )
    info = sp.resolve_dod_tier(project, state)
    assert info["tier"] == "early" and info["tier_source"] == "config"
    # planned decision beats config
    sp.set_dod_tier(state, "mature", "po@example.com", "release hardening")
    info = sp.resolve_dod_tier(project, state)
    assert info["tier"] == "mature" and info["tier_source"] == "planned"
    assert info["decided_by"] == "po@example.com"
    assert "coverage_no_decrease" in info["active_base_checks"]


@pytest.mark.unit
def test_set_dod_tier_rejects_unknown_tier():
    with pytest.raises(ValueError):
        sp.set_dod_tier({}, "bananas", "po@example.com")


@pytest.mark.unit
def test_next_action_carries_dod_block(tmp_path):
    state = {
        "current_stories": [
            {"id": "US-1", "title": "T", "state": "queued", "pipeline_log": []},
        ],
        "current_sprint": 2,
    }
    out = sp.next_action(state)
    assert out["dod"]["tier"] == "growing"
    assert out["dod"]["tier_source"] == "computed"
    assert "code_reviewed" in out["dod"]["active_checks"]
    out = sp.next_action(
        state, dod_tier_info={"tier": "early", "tier_source": "planned"}
    )
    assert out["dod"]["tier"] == "early"
    assert out["dod"]["tier_source"] == "planned"
    assert "code_reviewed" not in out["dod"]["active_checks"]


@pytest.mark.unit
def test_planned_tier_drives_cr_adaptivity(tmp_path):
    """A planned `early` tier in sprint 2 must NOT demand a CR dispatch."""
    story = {
        "id": "US-1", "title": "T", "state": "reviewing",
        "pipeline_log": [{"state": "reviewing", "entered_at": "2026-01-01T00:00:00Z"}],
    }
    state = {"current_stories": [story], "current_sprint": 2}
    out = sp.next_action(
        state, dod_tier_info={"tier": "early", "tier_source": "planned"}
    )
    assert out["action"] == "promote_story"


# ---------------------------------------------------------------------------
# GAP-6/GAP-8 — parallel batch
# ---------------------------------------------------------------------------


def _board(*stories) -> dict:
    return {"current_stories": list(stories), "current_sprint": 1}


def _queued(sid: str, **kw) -> dict:
    return {"id": sid, "title": sid, "state": "queued", "pipeline_log": [], **kw}


PAR = {"story_parallelism": True, "max_concurrent": 3, "isolation": "worktree"}


@pytest.mark.unit
def test_parallel_batch_lead_is_serial_action():
    state = _board(_queued("US-1"), _queued("US-2"), _queued("US-3"))
    out = sp.next_action(state, parallelism=PAR)
    assert out["action"] == "dispatch_se" and out["story_id"] == "US-1"
    par = out["parallel"]
    assert par["eligible"] is True
    assert par["batch"][0] == {"story_id": "US-1", "action": "dispatch_se", "role": "se"}
    assert [b["story_id"] for b in par["batch"]] == ["US-1", "US-2", "US-3"]


@pytest.mark.unit
def test_no_batch_when_serial_action_is_qe():
    """#170 review: a `testing` story (→ dispatch_qe) plus a queued story must
    NOT produce a batch — QE runs serially post-merge, never concurrently with
    an SE dispatch."""
    testing = {
        "id": "US-1", "title": "T", "state": "testing",
        "pipeline_log": [{"state": "testing", "entered_at": "2026-01-01T00:00:00Z"}],
    }
    state = _board(testing, _queued("US-2"))
    out = sp.next_action(state, parallelism=PAR)
    assert out["action"] == "dispatch_qe" and out["story_id"] == "US-1"
    assert "parallel" not in out or not out["parallel"]["eligible"]
    if "parallel" in out:
        assert all(b["action"] == "dispatch_se" for b in out["parallel"]["batch"][1:])


@pytest.mark.unit
def test_parallel_absent_without_config():
    state = _board(_queued("US-1"), _queued("US-2"))
    out = sp.next_action(state)
    assert "parallel" not in out
    out = sp.next_action(
        state, parallelism={"story_parallelism": False, "max_concurrent": 3}
    )
    assert "parallel" not in out


@pytest.mark.unit
def test_parallel_batch_respects_depends_on():
    state = _board(
        _queued("US-1"),
        _queued("US-2", depends_on=["US-1"]),          # dep not done
        _queued("US-3", depends_on=["US-0"]),          # unknown dep → fail closed
    )
    out = sp.next_action(state, parallelism=PAR)
    assert [b["story_id"] for b in out["parallel"]["batch"]] == ["US-1"]
    assert out["parallel"]["eligible"] is False


@pytest.mark.unit
def test_parallel_batch_dep_done_allows_join():
    done = {"id": "US-0", "title": "", "state": "done", "pipeline_log": []}
    state = _board(done, _queued("US-1"), _queued("US-2", depends_on=["US-0"]))
    out = sp.next_action(state, parallelism=PAR)
    assert [b["story_id"] for b in out["parallel"]["batch"]] == ["US-1", "US-2"]


@pytest.mark.unit
def test_parallel_batch_caps_at_max_concurrent():
    state = _board(*[_queued(f"US-{i}") for i in range(1, 6)])
    out = sp.next_action(state, parallelism={**PAR, "max_concurrent": 2})
    assert len(out["parallel"]["batch"]) == 2


@pytest.mark.unit
def test_shared_isolation_requires_disjoint_declared_scopes():
    state = _board(
        _queued("US-1", file_scope=["app/api/"]),
        _queued("US-2", file_scope=["components/hero/"]),
        _queued("US-3", file_scope=["app/api/orders/"]),   # overlaps US-1
        _queued("US-4"),                                    # undeclared
    )
    out = sp.next_action(state, parallelism={**PAR, "isolation": "shared"})
    ids = [b["story_id"] for b in out["parallel"]["batch"]]
    assert ids == ["US-1", "US-2"]
    assert "US-3" not in ids and "US-4" not in ids


@pytest.mark.unit
def test_parallel_never_attaches_to_recovery_paths():
    blocked = {
        "id": "US-1", "title": "", "state": "blocked",
        "blocked_reason": "DoD gate: ui_acceptance ...", "pipeline_log": [],
        "retries": {},
    }
    state = _board(blocked, _queued("US-2"))
    out = sp.next_action(state, parallelism=PAR)
    # Queued story wins first; but force the recovery path by having only blocked:
    state2 = _board(blocked)
    out2 = sp.next_action(state2, parallelism=PAR)
    assert out2["action"] == "recover_blocked"
    assert "parallel" not in out2 or out2.get("parallel") is None or not out2["parallel"]["eligible"] or out2["parallel"]["batch"][0]["action"].startswith("dispatch")
    # The clean assertion: recovery actions never carry an eligible batch.
    if "parallel" in out2:
        assert out2["parallel"] is None or out2["parallel"]["eligible"] is False


# ---------------------------------------------------------------------------
# parallelism_config parsing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parallelism_config_defaults_off(tmp_path):
    project = _project(tmp_path)
    cfg = sp.parallelism_config(project)
    assert cfg["story_parallelism"] is False
    assert cfg["max_concurrent"] == 3
    assert cfg["isolation"] == "worktree"


@pytest.mark.unit
def test_parallelism_config_parses_block(tmp_path):
    project = _project(
        tmp_path,
        "parallelism:\n  max_concurrent_subagents: 5\n"
        "  story_parallelism: enabled\n  isolation: shared\n",
    )
    cfg = sp.parallelism_config(project)
    assert cfg == {
        "max_concurrent": 5, "story_parallelism": True, "isolation": "shared",
    }


# ---------------------------------------------------------------------------
# GAP-12 — interrupted-dispatch detection
# ---------------------------------------------------------------------------


def _git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=path, check=True,
    )


@pytest.mark.unit
def test_interrupted_dispatch_detects_dirty_workspace(tmp_path):
    project = _project(tmp_path)
    _git_repo(project)
    (project / "partial.py").write_text("wip = True\n", encoding="utf-8")
    evidence = sp.detect_interrupted_dispatch(str(project), "US-1")
    assert evidence is not None
    assert "workspace" in evidence
    assert any("partial.py" in f for f in evidence["workspace"]["dirty_files"])


@pytest.mark.unit
def test_interrupted_dispatch_clean_repo_is_none(tmp_path):
    project = _project(tmp_path)
    _git_repo(project)
    # Empty .synaptory dirs are untracked-but-invisible to porcelain status,
    # so the fresh repo is already clean.
    assert sp.detect_interrupted_dispatch(str(project), "US-1") is None


@pytest.mark.unit
def test_interrupted_dispatch_non_git_is_none(tmp_path):
    project = _project(tmp_path)
    assert sp.detect_interrupted_dispatch(str(project), "US-1") is None
