"""Layer 1 — the SPQ cross-workstream Sync barrier (docs/spq-lifecycle-design.md §8).

Hypothesis: the barrier is only worth having if it cannot be satisfied by
accident. Three failure modes matter more than the happy path, and each has a
test here that fails loudly if the property is lost:

  1. `declare-ready` must DERIVE its quality evidence, never accept it. §8.2.1:
     a field the caller can set is decoration, not evidence. Expressed as a
     signature assertion so it survives refactoring.
  2. `collect` must resolve branches from CONFIG, never from the record it is
     about to read. §8.3 — otherwise a record could point the collector at a
     branch of its own choosing, and the ordering problem (records are only
     co-visible after the merge that is itself criterion 2) comes back.
  3. A skipped or unrunnable proof must never count as passed. That is how a
     barrier quietly stops being a barrier.

Uses real `git init` fixtures because `collect` reads blobs with `git show
<remote>/<branch>:<path>`, and mocking git would test the mock.
"""

from __future__ import annotations

import inspect
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import sync_barrier as sb


pytestmark = pytest.mark.unit

_GIT = shutil.which("git")
needs_git = pytest.mark.skipif(_GIT is None, reason="git not available")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CONFIG = """
build_mode: "spq"

spq:
  workstreams:
    - id: "frame"
      shared_owner: true
    - id: "spine"
    - id: "integration"
      integration: true
  sync:
    remote: "origin"
    branch_pattern: "ws/{id}"
    mode: "all_or_nothing"
    require_regression: false
    require_journey: false
    shared_digest_paths: ["contracts/"]
"""


def _write_config(project: Path, text: str = CONFIG) -> None:
    (project / ".synaptory.yaml").write_text(text, encoding="utf-8")


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    p = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env
    )
    assert p.returncode == 0, f"git {args}: {p.stderr}"
    return p.stdout


def _record(slice_n: int, ws: str, **over) -> dict:
    rec = {
        "schema_version": "1.1",
        "slice": slice_n,
        "workstream": ws,
        "crew_lead": "lead@h3t.co",
        "branch": f"ws/{ws}",
        "head_sha": None,   # add_record fills in the real branch head
        "work_units": {"admitted": 3, "done": 3, "cut": 0},
        "shared_digests": {"contracts/": "sha256:abc"},
        "regression": {"command": "bash scripts/x.sh", "exit_code": 0},
        "dod": {"tier": "growing", "tier_source": "planned", "checks_failed": {},
                "stories_evaluated": 3, "stories_passed": 3},
        "replay": {"commands_replayed": 9, "checks_unreplayable": 0, "mismatches": []},
        "declared_ready_at": "2026-08-14T09:12:00Z",
        "declared_by": "lead@h3t.co",
    }
    rec.update(over)
    return rec


@pytest.fixture
def integration_repo(tmp_path: Path):
    """An integration clone with `origin` pointing at a repo that has ws/* branches.

    Mirrors the real topology: the integration clone never has the workstream
    records in its own working tree, only in remote-tracking refs.
    """
    if _GIT is None:
        pytest.skip("git not available")
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "dev")
    (upstream / "README.md").write_text("x\n")
    _write_config(upstream)
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-qm", "base")

    clone = tmp_path / "integration"
    _git(tmp_path, "clone", "-q", str(upstream), "integration")

    def add_record(ws: str, slice_n: int, record: dict | None, branch: str | None = None):
        br = branch or f"ws/{ws}"
        _git(upstream, "checkout", "-q", "-B", br, "dev")
        if record is not None:
            # Real divergent work, so the branch head is not trivially an
            # ancestor of dev and criterion 2 can tell merged from unmerged.
            (upstream / f"{ws}-work.txt").write_text(f"{ws} delivered work\n")
            _git(upstream, "add", "-A")
            _git(upstream, "commit", "-qm", f"{ws} work s{slice_n}")
            if isinstance(record, dict) and record.get("head_sha") is None:
                record = {**record,
                          "head_sha": _git(upstream, "rev-parse", "HEAD").strip()}
            p = upstream / ".synaptory" / "sync" / f"slice-{slice_n}"
            p.mkdir(parents=True, exist_ok=True)
            body = record if isinstance(record, str) else json.dumps(record, indent=2)
            (p / f"{ws}.json").write_text(body if isinstance(body, str) else body)
            _git(upstream, "add", "-A")
            _git(upstream, "commit", "-qm", f"{ws} ready s{slice_n}")
        _git(upstream, "checkout", "-q", "dev")
        # Mirror the real ordering: `collect` (§8.5 step 1) fetches before the
        # human merge, so by the time `evaluate` (step 3) and `status` run, the
        # remote-tracking refs already exist. Both deliberately pass
        # fetch=False so evaluation never mutates refs underneath itself.
        _git(clone, "fetch", "-q", "origin")

    _write_config(clone)
    return clone, upstream, add_record



def _merge_all(clone: Path, *workstreams: str) -> None:
    """Perform criterion 2's human merge step, which green now requires."""
    for ws in workstreams:
        _git(clone, "merge", "-q", "--no-edit", f"origin/ws/{ws}")


def _ready_board(project: Path, slice_n: int = 3) -> None:
    """A board that satisfies declare-ready's readiness invariants."""
    orch = project / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True, exist_ok=True)
    (orch / "pipeline-state.json").write_text(json.dumps({
        "version": "2.0", "build_mode": "spq",
        "lifecycle_state": "SLICE_EXECUTION", "current_slice": slice_n,
        "current_stories": [{"id": "US-1", "title": "a", "state": "done"}]}))


# ---------------------------------------------------------------------------
# 1. declare-ready derives, never accepts (§8.2.1)
# ---------------------------------------------------------------------------

def test_declare_ready_signature_cannot_accept_evidence():
    """The design rule as an executable assertion.

    §8.2.1 requires `dod` and `replay` to be derived. The cheapest way for a
    future change to break that is to add a convenience kwarg, so the absence
    of one is asserted directly rather than trusted to review.
    """
    params = set(inspect.signature(sb.declare_ready).parameters)
    for forbidden in ("dod", "replay", "work_units", "shared_digests", "record"):
        assert forbidden not in params, (
            f"declare_ready must not accept {forbidden!r} — §8.2.1: a field the "
            "caller can set is decoration, not evidence"
        )


def test_declare_ready_refuses_on_replay_mismatch(tmp_path: Path, monkeypatch):
    """A workstream whose attested evidence did not reproduce is not done.

    Blocking here rather than at the barrier is deliberate: discovering it at
    Sync wastes the integration slot (§8.2.1).
    """
    # No shared_digest_paths: this test is about the replay refusal, and a
    # declared path with no digest script now (correctly) refuses first.
    _write_config(tmp_path, CONFIG.replace(
        'shared_digest_paths: ["contracts/"]', "shared_digest_paths: []"))
    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "pipeline-state.json").write_text(json.dumps({
        "version": "2.0", "build_mode": "spq",
        "lifecycle_state": "SLICE_EXECUTION", "current_slice": 3,
        "current_stories": [{"id": "US-1", "title": "a", "state": "done"}]}))
    (orch / "events.jsonl").write_text(json.dumps({
        "event": "evidence_replay_mismatch",
        "story_id": "US-7", "check": "tests_pass",
        "command": "pytest -q",
        "attested_exit_code": 0, "replayed_exit_code": 1,
    }) + "\n")
    monkeypatch.setenv("SYNAPTORY_ACTIVE_SPEC", "frame")

    with pytest.raises(sb.BarrierError) as e:
        sb.declare_ready(str(tmp_path), 3, run_regression=False)
    msg = str(e.value)
    assert "US-7" in msg and "tests_pass" in msg
    assert "not reproduce" in msg or "does not reproduce" in msg
    assert not (tmp_path / ".synaptory" / "sync").exists(), (
        "no record may be written when a mismatch is present"
    )


def test_declare_ready_requires_a_known_workstream(tmp_path: Path, monkeypatch):
    _write_config(tmp_path)
    _ready_board(tmp_path)
    monkeypatch.setenv("SYNAPTORY_ACTIVE_SPEC", "not-a-workstream")
    with pytest.raises(sb.BarrierError, match="not in spq.workstreams"):
        sb.declare_ready(str(tmp_path), 3, run_regression=False)


def test_declare_ready_needs_a_workstream_identity(tmp_path: Path, monkeypatch):
    _write_config(tmp_path)
    _ready_board(tmp_path)
    monkeypatch.delenv("SYNAPTORY_ACTIVE_SPEC", raising=False)
    with pytest.raises(sb.BarrierError, match="SYNAPTORY_ACTIVE_SPEC"):
        sb.declare_ready(str(tmp_path), 3, run_regression=False)


def test_declare_ready_round_trip(tmp_path: Path, monkeypatch):
    _write_config(tmp_path, CONFIG.replace(
        'shared_digest_paths: ["contracts/"]', "shared_digest_paths: []"))
    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "pipeline-state.json").write_text(json.dumps({
        "version": "2.0", "build_mode": "spq",
        "lifecycle_state": "SLICE_EXECUTION", "current_slice": 3,
        "current_stories": [
            {"id": "US-1", "title": "a", "state": "done"},
            {"id": "US-2", "title": "b", "state": "done"},
            {"id": "US-3", "title": "c", "state": "cancelled"},
        ],
    }))
    monkeypatch.setenv("SYNAPTORY_ACTIVE_SPEC", "frame")

    out = sb.declare_ready(
        str(tmp_path), 3, declared_by="lead@h3t.co", run_regression=False
    )
    rec = out["record"]
    assert rec["schema_version"] == sb.SCHEMA_VERSION == "1.1"
    assert rec["slice"] == 3 and rec["workstream"] == "frame"
    # cancelled stories are `cut`, not `admitted` — they are work the PO
    # decided not to ship, not work that failed.
    assert rec["work_units"] == {"admitted": 2, "done": 2, "cut": 1}
    assert rec["replay"]["mismatches"] == []
    assert "dod" in rec and "replay" in rec

    on_disk = json.loads((tmp_path / out["path"]).read_text())
    assert on_disk == rec, "the written record must equal the returned record"


# ---------------------------------------------------------------------------
# 2. collect: branch from config, not from the record (§8.3)
# ---------------------------------------------------------------------------

@needs_git
def test_collect_reads_records_without_merging(integration_repo):
    clone, _upstream, add_record = integration_repo
    add_record("frame", 3, _record(3, "frame"))
    add_record("spine", 3, _record(3, "spine"))

    out = sb.collect(str(clone), 3)
    assert out["records_present"] is True
    assert sorted(out["records"]) == ["frame", "spine"]
    assert out["missing"] == []
    # The whole point: nothing was merged into the working tree.
    assert not (clone / ".synaptory" / "sync").exists()


@needs_git
def test_collect_excludes_the_integration_workstream(integration_repo):
    """Q9: the integration seat holds a spec slot for cost attribution but is
    the thing readiness is declared TO, so it never joins the quorum."""
    clone, _u, add_record = integration_repo
    add_record("frame", 3, _record(3, "frame"))
    add_record("spine", 3, _record(3, "spine"))
    out = sb.collect(str(clone), 3)
    assert "integration" not in out["records"]
    assert out["records_present"] is True


@needs_git
def test_collect_uses_config_branch_not_record_claim(integration_repo):
    """A record claiming a different branch must not redirect the read."""
    clone, _u, add_record = integration_repo
    add_record("frame", 3, _record(3, "frame", branch="ws/somewhere-else"))
    add_record("spine", 3, _record(3, "spine"))
    out = sb.collect(str(clone), 3)
    assert out["records_present"] is True
    assert any("somewhere-else" in w and "using config" in w for w in out["warnings"])


@needs_git
def test_collect_reports_missing_record(integration_repo):
    clone, _u, add_record = integration_repo
    add_record("frame", 3, _record(3, "frame"))
    add_record("spine", 3, None)  # branch exists, no record on it
    out = sb.collect(str(clone), 3)
    assert out["records_present"] is False
    assert [m["workstream"] for m in out["missing"]] == ["spine"]


@needs_git
def test_collect_rejects_malformed_json(integration_repo):
    clone, _u, add_record = integration_repo
    add_record("frame", 3, "{not json")
    add_record("spine", 3, _record(3, "spine"))
    out = sb.collect(str(clone), 3)
    assert out["records_present"] is False
    assert "malformed JSON" in out["missing"][0]["reason"]


@needs_git
def test_collect_rejects_stale_slice_record(integration_repo):
    """A record for slice 2 must not satisfy slice 3."""
    clone, _u, add_record = integration_repo
    add_record("frame", 3, _record(2, "frame"))
    add_record("spine", 3, _record(3, "spine"))
    out = sb.collect(str(clone), 3)
    assert out["records_present"] is False
    assert "slice 2" in out["missing"][0]["reason"]


@needs_git
def test_collect_warns_when_record_carries_a_mismatch(integration_repo):
    """declare-ready should have refused; if a record arrives with one anyway,
    the barrier must not stay silent about it."""
    clone, _u, add_record = integration_repo
    bad = _record(3, "frame")
    bad["replay"]["mismatches"] = [{"story_id": "US-9", "check": "tests_pass"}]
    add_record("frame", 3, bad)
    add_record("spine", 3, _record(3, "spine"))
    out = sb.collect(str(clone), 3)
    assert any("replay mismatch" in w for w in out["warnings"])


def test_collect_warns_on_empty_workstream_list(tmp_path: Path):
    _write_config(tmp_path, "build_mode: \"spq\"\n")
    out = sb.collect(str(tmp_path), 3, fetch=False)
    assert out["records_present"] is False
    assert any("no workstreams configured" in w for w in out["warnings"])


# ---------------------------------------------------------------------------
# 3. Criteria: a skip is never a pass
# ---------------------------------------------------------------------------

@needs_git
def test_evaluate_blocks_on_missing_records(integration_repo):
    clone, _u, add_record = integration_repo
    add_record("frame", 3, _record(3, "frame"))
    out = sb.evaluate(str(clone), 3, use_cache=False)
    assert out["verdict"] == "blocked"
    assert "records_present" in out["blocking"]


@needs_git
def test_evaluate_blocks_when_not_on_the_integration_branch(integration_repo):
    """Criterion 2 is a human merge (§8.5 step 2), recorded not enforced — but
    evaluating on the wrong branch must still block, or the barrier would pass
    on an unintegrated tree."""
    clone, _u, add_record = integration_repo
    add_record("frame", 3, _record(3, "frame"))
    add_record("spine", 3, _record(3, "spine"))
    out = sb.evaluate(str(clone), 3, use_cache=False)
    assert "branches_merged" in out["blocking"]
    assert out["criteria"]["branches_merged"]["passed"] is False
    assert "human step" in out["criteria"]["branches_merged"]["detail"]


@needs_git
def test_evaluate_green_on_integration_branch(integration_repo):
    clone, _u, add_record = integration_repo
    add_record("frame", 3, _record(3, "frame", shared_digests={}))
    add_record("spine", 3, _record(3, "spine", shared_digests={}))
    _write_config(clone, CONFIG.replace('shared_digest_paths: ["contracts/"]',
                                        "shared_digest_paths: []"))
    _git(clone, "checkout", "-q", "-B", "sync/slice-3")
    _merge_all(clone, "frame", "spine")
    out = sb.evaluate(str(clone), 3, use_cache=False)
    assert out["verdict"] == "green", out["blocking"]
    assert out["blocking"] == []


@needs_git
def test_per_workstream_mode_relaxes_quorum_only(integration_repo):
    """Q1 ratified all_or_nothing. per_workstream stays implementable but must
    relax ONLY the quorum, never the integrated-tree criteria (§8.6)."""
    clone, _u, add_record = integration_repo
    add_record("frame", 3, _record(3, "frame", shared_digests={}))
    _write_config(clone, CONFIG
                  .replace('mode: "all_or_nothing"', 'mode: "per_workstream"')
                  .replace('shared_digest_paths: ["contracts/"]',
                           "shared_digest_paths: []"))
    out = sb.evaluate(str(clone), 3, use_cache=False)
    assert "records_present" not in out["blocking"], "quorum should be relaxed"
    # Still on the wrong branch, so the integrated-tree criterion still blocks.
    assert "branches_merged" in out["blocking"]


def test_skipped_journey_is_unproven_not_passed(tmp_path: Path, monkeypatch):
    """A journey script that skips must not satisfy criterion 5."""
    _write_config(tmp_path, CONFIG.replace(
        "require_journey: false", "require_journey: true"))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "sync-journey.sh").write_text(
        "#!/usr/bin/env bash\necho 'RESULT exit=0 status=skipped reason=no-stack'\n"
    )
    monkeypatch.setattr(sb, "collect", lambda *a, **k: {
        "records_present": True, "records": {}, "missing": [], "warnings": []})
    out = sb.evaluate(str(tmp_path), 3, use_cache=False)
    j = out["criteria"]["journey_green"]
    assert j["passed"] is False
    assert "unproven" in j["detail"]


def test_missing_proof_script_is_not_a_pass(tmp_path: Path, monkeypatch):
    _write_config(tmp_path, CONFIG.replace(
        "require_regression: false", "require_regression: true"))
    monkeypatch.setattr(sb, "collect", lambda *a, **k: {
        "records_present": True, "records": {}, "missing": [], "warnings": []})
    out = sb.evaluate(str(tmp_path), 3, use_cache=False)
    r = out["criteria"]["regression_green"]
    assert r["passed"] is False
    assert "not found" in r["detail"]


def test_long_running_proof_script_is_not_timed_out(tmp_path: Path):
    """§9: the 60s Evidence-Contract cap and the 30s SubagentStop budget are
    hook-path limits. Imposing a timeout here would reintroduce the constraint
    the design exists to escape."""
    _write_config(tmp_path)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "slow.sh").write_text(
        "#!/usr/bin/env bash\nsleep 2\necho 'RESULT exit=0 passed=812 failed=0'\n"
    )
    res = sb._run_proof_script(str(tmp_path), "scripts/slow.sh", label="regression")
    assert res["exit_code"] == 0
    assert res["passed"] == 812 and res["failed"] == 0


# ---------------------------------------------------------------------------
# Digests (criterion 4)
# ---------------------------------------------------------------------------

def test_missing_digest_script_fails_loudly(tmp_path: Path):
    """Criterion 4 must fail on absent data, never pass on it."""
    _write_config(tmp_path)
    cfg = sb.load_spq_config(tmp_path)
    digests = sb.compute_shared_digests(tmp_path, cfg)
    assert digests["contracts/"].startswith("error:missing-digest-script")


def test_digest_drift_names_the_path(tmp_path: Path, monkeypatch):
    """The digest-comparison FALLBACK, used when git cannot answer (#239).

    `tmp_path` is not a git repo, so `_shared_paths_touched_by` returns None
    and criterion 4 falls back to comparing claimed against integrated.

    Uses `spine`, not `frame`. `frame` is the `shared_owner` in CONFIG, and an
    owner is now exempt because changing shared components is its role — so the
    original version of this test asserted a block against the one workstream
    allowed to do it, and only passed because the owner used to be judged like
    everyone else.
    """
    _write_config(tmp_path)
    cfg = sb.load_spq_config(tmp_path)
    monkeypatch.setattr(sb, "compute_shared_digests",
                        lambda *a, **k: {"contracts/": "sha256:INTEGRATED"})
    collected = {"records": {"spine": _record(3, "spine")}}  # claims sha256:abc
    out = sb._check_digests(str(tmp_path), cfg, collected)
    assert out["passed"] is False
    assert out["used_digest_fallback"] is True
    assert out["drift"][0]["path"] == "contracts/"
    assert out["drift"][0]["claimed"] == "sha256:abc"


def test_shared_owner_is_exempt_from_the_digest_check(tmp_path: Path, monkeypatch):
    """#239: the owner publishing a shared component is not drift."""
    _write_config(tmp_path)
    cfg = sb.load_spq_config(tmp_path)
    monkeypatch.setattr(sb, "compute_shared_digests",
                        lambda *a, **k: {"contracts/": "sha256:INTEGRATED"})
    out = sb._check_digests(
        str(tmp_path), cfg, {"records": {"frame": _record(3, "frame")}})
    assert out["passed"] is True, out["drift"]
    assert out["shared_owner"] == "frame"


def test_digest_absent_from_record_is_drift(tmp_path: Path, monkeypatch):
    _write_config(tmp_path)
    cfg = sb.load_spq_config(tmp_path)
    monkeypatch.setattr(sb, "compute_shared_digests",
                        lambda *a, **k: {"contracts/": "sha256:X"})
    rec = _record(3, "frame")
    rec["shared_digests"] = {}
    out = sb._check_digests(str(tmp_path), cfg, {"records": {"frame": rec}})
    assert out["passed"] is False
    assert out["drift"][0]["reason"] == "no digest in record"


def test_no_digest_paths_is_waived_not_failed(tmp_path: Path):
    _write_config(tmp_path, CONFIG.replace(
        'shared_digest_paths: ["contracts/"]', "shared_digest_paths: []"))
    cfg = sb.load_spq_config(tmp_path)
    out = sb._check_digests(str(tmp_path), cfg, {"records": {}})
    assert out["passed"] is True and out["waived"] is True


# ---------------------------------------------------------------------------
# clear + verdict cache (Q10)
# ---------------------------------------------------------------------------

@needs_git
def test_clear_refuses_when_blocked(integration_repo):
    clone, _u, add_record = integration_repo
    add_record("frame", 3, _record(3, "frame"))
    with pytest.raises(sb.BarrierError, match="still"):
        sb.clear(str(clone), 3)


@needs_git
def test_clear_writes_verdict_and_barrier_receipt(integration_repo):
    clone, _u, add_record = integration_repo
    add_record("frame", 3, _record(3, "frame", shared_digests={}))
    add_record("spine", 3, _record(3, "spine", shared_digests={}))
    _write_config(clone, CONFIG.replace(
        'shared_digest_paths: ["contracts/"]', "shared_digest_paths: []"))
    _git(clone, "checkout", "-q", "-B", "sync/slice-3")
    _merge_all(clone, "frame", "spine")

    out = sb.clear(str(clone), 3, cleared_by="lead@h3t.co")
    assert out["verdict"] == "green"

    receipt = clone / ".synaptory" / ".orchestrator" / "receipts" / "SYNC-3-barrier.json"
    assert receipt.is_file()
    r = json.loads(receipt.read_text())
    # Orchestrator receipts use a descriptive suffix, not a role abbrev.
    assert r["story_id"] == "SYNC-3"
    assert r["role"] == "orchestrator"
    assert r["token_usage"]["stage"] == "orchestrator"
    assert r["verification_commands"], "every receipt needs >=1 command"
    import re as _re
    assert _re.fullmatch(r"[A-Z][A-Z0-9]*-\d+", r["story_id"])


@needs_git
def test_verdict_cache_serves_and_invalidates(integration_repo):
    """Q10: a digest fix should not force a fresh full-suite run when the tree
    is provably unchanged — and any tree change must invalidate."""
    clone, _u, add_record = integration_repo
    add_record("frame", 3, _record(3, "frame", shared_digests={}))
    add_record("spine", 3, _record(3, "spine", shared_digests={}))
    _write_config(clone, CONFIG.replace(
        'shared_digest_paths: ["contracts/"]', "shared_digest_paths: []"))
    _git(clone, "checkout", "-q", "-B", "sync/slice-3")
    _merge_all(clone, "frame", "spine")

    first = sb.evaluate(str(clone), 3, use_cache=True)
    assert first["cache_served"] is False
    second = sb.evaluate(str(clone), 3, use_cache=True)
    assert second["cache_served"] is True

    (clone / "moved.txt").write_text("tree changed\n")
    third = sb.evaluate(str(clone), 3, use_cache=True)
    assert third["cache_served"] is False, "any tree change must invalidate"


# ---------------------------------------------------------------------------
# status (tracker mirror)
# ---------------------------------------------------------------------------

@needs_git
def test_status_reports_quorum_progress(integration_repo):
    clone, _u, add_record = integration_repo
    add_record("frame", 3, _record(3, "frame"))
    out = sb.status(str(clone), 3)
    assert out["quorum"] == 2  # frame + spine; integration excluded
    assert out["ready_count"] == 1
    assert out["integration_branch"] == "sync/slice-3"
    by_id = {w["workstream"]: w for w in out["workstreams"]}
    assert by_id["frame"]["ready"] is True
    assert by_id["spine"]["ready"] is False


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_unknown_mode_falls_back_to_all_or_nothing(tmp_path: Path):
    """A config typo must not leave the barrier in an undefined blocking
    state; all_or_nothing is the safe direction."""
    _write_config(tmp_path, CONFIG.replace(
        'mode: "all_or_nothing"', 'mode: "whatever"'))
    assert sb.load_spq_config(tmp_path)["mode"] == "all_or_nothing"


def test_config_defaults_without_a_yaml_file(tmp_path: Path):
    cfg = sb.load_spq_config(tmp_path)
    assert cfg["remote"] == "origin"
    assert cfg["branch_pattern"] == "ws/{id}"
    assert cfg["mode"] == "all_or_nothing"
    assert cfg["workstreams"] == []


def test_per_workstream_branch_override(tmp_path: Path):
    _write_config(tmp_path, CONFIG.replace(
        '    - id: "spine"', '    - id: "spine"\n      branch: "custom/spine"'))
    cfg = sb.load_spq_config(tmp_path)
    ws = {w["id"]: w for w in cfg["workstreams"]}
    assert sb.branch_for(cfg, ws["spine"]) == "custom/spine"
    assert sb.branch_for(cfg, ws["frame"]) == "ws/frame"


# ---------------------------------------------------------------------------
# Digest interop with scripts/shared-digest.sh
#
# Found by cross-checking the real script against this module: the script echoes
# the NORMALISED path it walked, while config may write `contracts/` (which is
# exactly the form §8.3's own example uses). Keying on raw strings reported
# `no-digest-emitted` for a perfectly good digest, i.e. criterion 4 failing for
# no reason — the precise failure §8.3 exists to prevent.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
_HAS_DIGEST_SCRIPT = (REPO_ROOT / "scripts" / "shared-digest.sh").is_file()
needs_digest_script = pytest.mark.skipif(
    not _HAS_DIGEST_SCRIPT, reason="scripts/shared-digest.sh not present"
)


@pytest.mark.parametrize("written", ["plugin/rules", "plugin/rules/", "./plugin/rules"])
@needs_digest_script
def test_digest_lookup_survives_path_spelling(written: str):
    cfg = {"shared_digest_paths": [written],
           "digest_script": "scripts/shared-digest.sh"}
    out = sb.compute_shared_digests(REPO_ROOT, cfg)
    assert list(out) == [written], "keys must stay exactly as config wrote them"
    assert not out[written].startswith("error:"), out


@needs_digest_script
def test_all_spellings_agree_on_the_same_digest():
    """The two sides of the barrier may spell the path differently in config;
    they must still compare equal."""
    script = "scripts/shared-digest.sh"
    digests = {
        spelling: sb.compute_shared_digests(
            REPO_ROOT, {"shared_digest_paths": [spelling], "digest_script": script}
        )[spelling]
        for spelling in ("plugin/rules", "plugin/rules/", "./plugin/rules")
    }
    assert len(set(digests.values())) == 1, digests


@needs_digest_script
def test_path_with_no_tracked_files_is_an_error_not_a_pass():
    """Vacuous criterion 4: a typo'd or deleted shared path makes BOTH sides
    agree on the empty-listing digest, so the criterion would pass on nothing
    and a governed shared component would silently drop out of the barrier."""
    out = sb.compute_shared_digests(
        REPO_ROOT,
        {"shared_digest_paths": ["no/such/shared/dir"],
         "digest_script": "scripts/shared-digest.sh"},
    )
    assert out["no/such/shared/dir"] == "error:no-tracked-files"


def test_norm_path_is_comparison_only():
    assert sb._norm_path("contracts/") == "contracts"
    assert sb._norm_path("./contracts") == "contracts"
    assert sb._norm_path("contracts") == "contracts"


# ---------------------------------------------------------------------------
# The .gitignore carve-out
#
# §8.2 calls this "the one narrow exception to gitignoring .synaptory/", and
# implemented naively it SILENTLY DOES NOTHING: git cannot re-include a path
# whose parent directory is excluded, so `.synaptory/` + `!.synaptory/sync/`
# leaves records uncommittable and every barrier run reports "missing record"
# with no visible cause. The fix is to exclude the CHILDREN (`.synaptory/*`).
# ---------------------------------------------------------------------------

@needs_git
def test_readiness_records_are_committable_but_nothing_else_is(tmp_path: Path):
    """Guards both halves: the carve-out works AND it stays narrow."""
    gitignore = (REPO_ROOT / ".gitignore").read_text()
    repo = tmp_path / "probe"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / ".gitignore").write_text(gitignore)

    record = repo / ".synaptory" / "sync" / "slice-3"
    record.mkdir(parents=True)
    (record / "exec.json").write_text("{}")
    for leak in (".synaptory/.orchestrator/pipeline-state.json",
                 ".synaptory/signals/signals.jsonl",
                 ".synaptory/design/mockups/index.html"):
        p = repo / leak
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")

    def ignored(rel: str) -> bool:
        return subprocess.run(
            ["git", "check-ignore", "-q", rel],
            cwd=str(repo), capture_output=True,
        ).returncode == 0

    assert not ignored(".synaptory/sync/slice-3/exec.json"), (
        "readiness records MUST be committable — they are the only channel by "
        "which Slice readiness reaches the integration clone (§8.2). A "
        "`.synaptory/` directory exclusion cannot be undone by a later "
        "negation; exclude `.synaptory/*` instead."
    )
    for leak in (".synaptory/.orchestrator/pipeline-state.json",
                 ".synaptory/signals/signals.jsonl",
                 ".synaptory/design/mockups/index.html"):
        assert ignored(leak), f"the carve-out must stay narrow — {leak} leaked"


@needs_git
def test_verdict_cache_invalidates_when_a_workstream_becomes_ready(integration_repo):
    """The subtle half of Q10.

    `verification_cache` keys on LOCAL tree state, but records live on remote
    refs — so a workstream declaring ready, the single most important state
    change the barrier reacts to, leaves the local tree untouched. Without the
    record fingerprint in the key, `evaluate` would keep serving the stale
    "blocked" verdict after the workstream it was waiting for arrived.
    """
    clone, _u, add_record = integration_repo
    _write_config(clone, CONFIG.replace(
        'shared_digest_paths: ["contracts/"]', "shared_digest_paths: []"))
    _git(clone, "checkout", "-q", "-B", "sync/slice-3")

    # Merge each branch as it appears: the point of this test is that a
    # workstream becoming ready invalidates the cached verdict, so they cannot
    # all exist up front.
    add_record("frame", 3, _record(3, "frame", shared_digests={}))
    _merge_all(clone, "frame")
    first = sb.evaluate(str(clone), 3, use_cache=True)
    assert first["verdict"] == "blocked"
    assert "records_present" in first["blocking"]

    add_record("spine", 3, _record(3, "spine", shared_digests={}))
    _merge_all(clone, "spine")
    second = sb.evaluate(str(clone), 3, use_cache=True)
    assert second["cache_served"] is False, (
        "a newly-ready workstream must invalidate the cached verdict"
    )
    assert second["verdict"] == "green", second["blocking"]


# ---------------------------------------------------------------------------
# Config parsing fail-open (found by cross-checking the shipped template
# against ordinary YAML)
#
# `shared_digest_paths` in BLOCK-LIST form parsed to [], and `_check_digests`
# then WAIVED criterion 4 — a fail-open in a barrier whose whole job is to fail
# closed, triggered by valid YAML. Only the inline form worked, which is why the
# shipped template's happy path hid it.
# ---------------------------------------------------------------------------

BLOCK_LIST_CONFIG = """
build_mode: "spq"

spq:
  workstreams:
    - id: "frame"
    - id: "spine"
  sync:
    require_regression: false
    require_journey: false
    shared_digest_paths:
      - "contracts/"
      - "design-system/"
    accepted_digest_deltas:
      - "contracts/=sha256:accepted"
"""


def test_block_list_shared_digest_paths_are_parsed(tmp_path: Path):
    _write_config(tmp_path, BLOCK_LIST_CONFIG)
    cfg = sb.load_spq_config(tmp_path)
    assert cfg["shared_digest_paths"] == ["contracts/", "design-system/"]


def test_block_list_paths_do_not_waive_criterion_4(tmp_path: Path):
    """The actual failure: paths configured, criterion silently waived."""
    _write_config(tmp_path, BLOCK_LIST_CONFIG)
    cfg = sb.load_spq_config(tmp_path)
    out = sb._check_digests(str(tmp_path), cfg, {"records": {}})
    assert not out.get("waived"), (
        "criterion 4 must not waive itself when shared paths ARE configured"
    )
    assert out["passed"] is False


def test_inline_list_form_still_works(tmp_path: Path):
    _write_config(tmp_path)  # uses the inline form
    assert sb.load_spq_config(tmp_path)["shared_digest_paths"] == ["contracts/"]


def test_genuinely_absent_paths_still_waive(tmp_path: Path):
    _write_config(tmp_path, 'build_mode: "spq"\nspq:\n  sync:\n    remote: "origin"\n')
    cfg = sb.load_spq_config(tmp_path)
    out = sb._check_digests(str(tmp_path), cfg, {"records": {}})
    assert out["passed"] is True and out["waived"] is True


def test_accepted_digest_deltas_are_reachable_from_config(tmp_path: Path, monkeypatch):
    """§8.4 criterion 4 offers "or the delta is explicitly accepted". That was
    honoured by _check_digests but unreachable from config — a documented
    escape hatch that did not exist."""
    _write_config(tmp_path, BLOCK_LIST_CONFIG)
    cfg = sb.load_spq_config(tmp_path)
    assert cfg["accepted_digest_deltas"] == {"contracts": "sha256:accepted"}

    monkeypatch.setattr(sb, "compute_shared_digests", lambda *a, **k: {
        "contracts/": "sha256:INTEGRATED", "design-system/": "sha256:same"})
    rec = _record(3, "frame")
    rec["shared_digests"] = {"contracts/": "sha256:accepted",
                             "design-system/": "sha256:same"}
    out = sb._check_digests(str(tmp_path), cfg, {"records": {"frame": rec}})
    assert out["passed"] is True, out["drift"]
