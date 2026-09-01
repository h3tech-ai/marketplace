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
    # These fixtures predate the Cycle manifest and exercise criteria 2-5
    # (merge, regression, digests, journey) in isolation. Without a manifest
    # the three IDENTITY criteria are unprovable, and unprovable BLOCKS by
    # default -- so opting into the documented one-Cycle waiver is what keeps
    # this a test of the criteria it is actually about, rather than silently
    # weakening the new ones.
    allow_legacy_records: true
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


def _record(cycle_n: int, ws: str, **over) -> dict:
    rec = {
        "schema_version": "1.1",
        "cycle": cycle_n,
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

    def add_record(ws: str, cycle_n: int, record: dict | None, branch: str | None = None):
        br = branch or f"ws/{ws}"
        _git(upstream, "checkout", "-q", "-B", br, "dev")
        if record is not None:
            # Real divergent work, so the branch head is not trivially an
            # ancestor of dev and criterion 2 can tell merged from unmerged.
            (upstream / f"{ws}-work.txt").write_text(f"{ws} delivered work\n")
            _git(upstream, "add", "-A")
            _git(upstream, "commit", "-qm", f"{ws} work s{cycle_n}")
            if isinstance(record, dict) and record.get("head_sha") is None:
                record = {**record,
                          "head_sha": _git(upstream, "rev-parse", "HEAD").strip()}
            p = upstream / ".synaptory" / "sync" / f"cycle-{cycle_n}"
            p.mkdir(parents=True, exist_ok=True)
            body = record if isinstance(record, str) else json.dumps(record, indent=2)
            (p / f"{ws}.json").write_text(body if isinstance(body, str) else body)
            _git(upstream, "add", "-A")
            _git(upstream, "commit", "-qm", f"{ws} ready s{cycle_n}")
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


def _ready_board(project: Path, cycle_n: int = 3) -> None:
    """A board that satisfies declare-ready's readiness invariants."""
    orch = project / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True, exist_ok=True)
    (orch / "pipeline-state.json").write_text(json.dumps({
        "version": "2.0", "build_mode": "spq",
        "lifecycle_state": "CYCLE_EXECUTION", "current_cycle": cycle_n,
        "current_stories": [{"id": "US-1", "title": "a", "state": "done",
                             "dod": {"passed": True, "critical_passed": True, "checks": {}}}]}))


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
        "lifecycle_state": "CYCLE_EXECUTION", "current_cycle": 3,
        "current_stories": [{"id": "US-1", "title": "a", "state": "done",
                             "dod": {"passed": True, "critical_passed": True, "checks": {}}}]}))
    (orch / "events.jsonl").write_text(json.dumps({
        "event": "evidence_replay_mismatch",
        "story_id": "US-7", "check": "tests_pass",
        "command": "pytest -q",
        "attested_exit_code": 0, "replayed_exit_code": 1,
    }) + "\n")
    monkeypatch.setenv("SYNAPTORY_WORKSTREAM", "frame")

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
    monkeypatch.setenv("SYNAPTORY_WORKSTREAM", "not-a-workstream")
    with pytest.raises(sb.BarrierError, match="not in spq.workstreams"):
        sb.declare_ready(str(tmp_path), 3, run_regression=False)


def test_declare_ready_needs_a_workstream_identity(tmp_path: Path, monkeypatch):
    _write_config(tmp_path)
    _ready_board(tmp_path)
    monkeypatch.delenv("SYNAPTORY_WORKSTREAM", raising=False)
    # A stale Multi-Spec export must NOT be accepted as a workstream: a barrier
    # record attributed to the wrong lane is worse than a missing one, because
    # the quorum then looks satisfied.
    monkeypatch.setenv("SYNAPTORY_ACTIVE_SPEC", "frame")
    with pytest.raises(sb.BarrierError, match="SYNAPTORY_WORKSTREAM"):
        sb.declare_ready(str(tmp_path), 3, run_regression=False)


def test_declare_ready_round_trip(tmp_path: Path, monkeypatch):
    _write_config(tmp_path, CONFIG.replace(
        'shared_digest_paths: ["contracts/"]', "shared_digest_paths: []"))
    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "pipeline-state.json").write_text(json.dumps({
        "version": "2.0", "build_mode": "spq",
        "lifecycle_state": "CYCLE_EXECUTION", "current_cycle": 3,
        # A `done` Work Unit carries an evaluated DoD gate, because the
        # `reviewing -> done` edge writes one (#238). Fixtures that omitted it
        # were reproducing the #329 defect rather than the product: an
        # unevaluated board whose empty `checks_failed` reads as clean.
        "current_stories": [
            {"id": "US-1", "title": "a", "state": "done", "dod": {"passed": True, "critical_passed": True, "checks": {}}},
            {"id": "US-2", "title": "b", "state": "done", "dod": {"passed": True, "critical_passed": True, "checks": {}}},
            {"id": "US-3", "title": "c", "state": "cancelled"},
        ],
    }))
    monkeypatch.setenv("SYNAPTORY_WORKSTREAM", "frame")

    out = sb.declare_ready(
        str(tmp_path), 3, declared_by="lead@h3t.co", run_regression=False
    )
    rec = out["record"]
    assert rec["schema_version"] == sb.SCHEMA_VERSION == "2.0"
    # 2.0 is a strict SUPERSET: every field a 1.1 reader touches survives.
    assert set(rec["work_units"]) == {"admitted", "done", "cut"}
    # ...and it adds the identities #303 §6 asks for.
    assert set(rec["work_unit_ids"]) == {"admitted", "done", "cut", "integrated"}
    assert "dependency_closure" in rec
    assert rec["cycle"] == 3 and rec["workstream"] == "frame"
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
def test_collect_rejects_stale_cycle_record(integration_repo):
    """A record for cycle 2 must not satisfy cycle 3."""
    clone, _u, add_record = integration_repo
    add_record("frame", 3, _record(2, "frame"))
    add_record("spine", 3, _record(3, "spine"))
    out = sb.collect(str(clone), 3)
    assert out["records_present"] is False
    assert "cycle 2" in out["missing"][0]["reason"]


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
    _git(clone, "checkout", "-q", "-B", "sync/cycle-3")
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
    _git(clone, "checkout", "-q", "-B", "sync/cycle-3")
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
    _git(clone, "checkout", "-q", "-B", "sync/cycle-3")
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
    assert out["integration_branch"] == "sync/cycle-3"
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
    assert cfg["branch_pattern"] == "cycle/{cycle_id}/ws/{id}"
    assert cfg["integration_branch_pattern"] == "cycle/{cycle_id}/integration"
    assert cfg["mode"] == "all_or_nothing"
    assert cfg["workstreams"] == []


def test_workstream_nested_block_parses_instead_of_flattening(tmp_path: Path):
    """A nested `filter:` used to be dropped and its children flattened.

    `_parse_workstreams` kept a key only when it had a non-empty value, so the
    block header vanished and `type`/`value` landed as FLAT keys on the
    workstream. The config looked accepted while the discriminator was gone,
    which is the worst failure mode a config parser has.
    """
    _write_config(tmp_path, CONFIG.replace(
        '    - id: "spine"',
        '    - id: "spine"\n      filter:\n        type: label\n'
        '        value: "ws:spine"',
    ))
    ws = {w["id"]: w for w in sb.load_spq_config(tmp_path)["workstreams"]}
    assert ws["spine"]["filter"] == {"type": "label", "value": "ws:spine"}
    assert "type" not in ws["spine"], "nested keys must not flatten onto the item"
    assert "value" not in ws["spine"]
    # The nested block must not swallow the next list item either.
    assert "frame" in ws and ws["frame"].get("id") == "frame"
    assert "filter" not in ws["frame"]


def test_workstream_childless_block_header_is_visible(tmp_path: Path):
    """An empty header yields {} rather than being silently dropped."""
    _write_config(tmp_path, CONFIG.replace(
        '    - id: "spine"', '    - id: "spine"\n      filter:'))
    ws = {w["id"]: w for w in sb.load_spq_config(tmp_path)["workstreams"]}
    assert ws["spine"]["filter"] == {}
    assert ws["frame"].get("id") == "frame"


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


@pytest.mark.parametrize("written", ["plugin-claude/rules", "plugin-claude/rules/", "./plugin-claude/rules"])
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
        for spelling in ("plugin-claude/rules", "plugin-claude/rules/", "./plugin-claude/rules")
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

    record = repo / ".synaptory" / "sync" / "cycle-3"
    record.mkdir(parents=True)
    (record / "exec.json").write_text("{}")
    # #303: the Cycle manifest and its dependency events travel the same way.
    # git is SPQ's ONLY cross-clone channel, so an ignored manifest is a
    # manifest no other workstream can ever hydrate from.
    cycle = repo / ".synaptory" / "cycles" / "7-abc12345"
    (cycle / "events" / "spine").mkdir(parents=True)
    (cycle / "sync").mkdir(parents=True)
    (cycle / "manifest.json").write_text("{}")
    (cycle / "events" / "spine" / "0001-integrated-WU-1.json").write_text("{}")
    (cycle / "sync" / "spine.json").write_text("{}")
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

    assert not ignored(".synaptory/sync/cycle-3/exec.json"), (
        "readiness records MUST be committable — they are the only channel by "
        "which Cycle readiness reaches the integration clone (§8.2). A "
        "`.synaptory/` directory exclusion cannot be undone by a later "
        "negation; exclude `.synaptory/*` instead."
    )
    for transport in (
        ".synaptory/cycles/7-abc12345/manifest.json",
        ".synaptory/cycles/7-abc12345/events/spine/0001-integrated-WU-1.json",
        ".synaptory/cycles/7-abc12345/sync/spine.json",
    ):
        assert not ignored(transport), (
            "%s MUST be committable: git is SPQ's only cross-clone channel, so "
            "an ignored manifest or event is one no other workstream can ever "
            "hydrate from or resolve a dependency against (#303)." % transport
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
    _git(clone, "checkout", "-q", "-B", "sync/cycle-3")

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


# ---------------------------------------------------------------------------
# SPD-173 — a Slice → Cycle cutover is not atomic across workstream clones
# ---------------------------------------------------------------------------

def _add_legacy_record(upstream: Path, clone: Path, ws: str, cycle_n: int,
                       record: dict) -> None:
    """Write a readiness record at the PRE-SPD-173 path, as v1.1.x did."""
    br = f"ws/{ws}"
    _git(upstream, "checkout", "-q", "-B", br, "dev")
    (upstream / f"{ws}-work.txt").write_text(f"{ws} delivered work\n")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-qm", f"{ws} work s{cycle_n}")
    record = {**record, "head_sha": _git(upstream, "rev-parse", "HEAD").strip()}
    p = upstream / ".synaptory" / "sync" / f"slice-{cycle_n}"
    p.mkdir(parents=True, exist_ok=True)
    (p / f"{ws}.json").write_text(json.dumps(record, indent=2))
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-qm", f"{ws} ready s{cycle_n}")
    _git(upstream, "checkout", "-q", "dev")
    _git(clone, "fetch", "-q", "origin")


@needs_git
def test_collect_accepts_a_pre_rename_readiness_path(integration_repo):
    """A clone still on v1.1.x writes `slice-<N>/`; that is not a lost record.

    Without the fallback the workstream reads as *missing* and the barrier
    blocks on a phantom — the failure most likely to be misread as data loss.
    """
    clone, upstream, add_record = integration_repo
    add_record("frame", 3, _record(3, "frame"))
    _add_legacy_record(upstream, clone, "spine", 3, _record(3, "spine"))

    out = sb.collect(str(clone), 3)
    assert out["missing"] == [], "the legacy-path workstream read as missing"
    assert sorted(out["records"]) == ["frame", "spine"]
    assert any("pre-SPD-173" in w for w in out["warnings"]), \
        "a legacy path must warn so the clone actually gets upgraded"


@needs_git
def test_the_canonical_path_wins_over_the_legacy_one(integration_repo):
    """Both paths present: the new one is authoritative and no warning fires."""
    clone, _upstream, add_record = integration_repo
    add_record("frame", 3, _record(3, "frame"))
    add_record("spine", 3, _record(3, "spine"))
    out = sb.collect(str(clone), 3)
    assert not any("pre-SPD-173" in w for w in out["warnings"])


def test_nothing_writes_the_legacy_path():
    """The shim is read-only: `slice-` may appear only in the legacy reader."""
    src = inspect.getsource(sb)
    assert src.count('f"{root}/slice-') == 1, \
        "the pre-SPD-173 path is constructed somewhere other than the reader"
    assert "def record_relpath_legacy" in src


def test_pre_rename_spq_slice_config_section_still_parses(tmp_path: Path):
    """SPD-173: `spq.slice:` must not silently default `scope_defined`."""
    (tmp_path / ".synaptory.yaml").write_text(
        'build_mode: "spq"\n'
        "spq:\n"
        "  workstreams:\n"
        '    - id: "frame"\n'
        "  slice:\n"
        "    scope_defined: true\n"
    )
    cfg = sb.load_spq_config(str(tmp_path))
    assert cfg.get("scope_defined") is True, \
        "a pre-rename spq.slice section was dropped instead of read"


# ── #303 §6: closure by IDENTITY, not by count ─────────────────────────────


def _identity_records(**overrides):
    """Two 2.0 records agreeing on one manifest, unless a test perturbs them."""
    base = {
        "frame": {
            "schema_version": "2.0",
            "cycle": 1,
            "workstream": "frame",
            "manifest_hash": "sha256:aaa",
            "work_unit_ids": {
                "admitted": ["WU-1"], "done": ["WU-1"], "cut": [],
                "integrated": {"WU-1": {"sha": "abc"}},
            },
            "dependency_closure": {"satisfied": [], "unsatisfied": []},
        },
        "spine": {
            "schema_version": "2.0",
            "cycle": 1,
            "workstream": "spine",
            "manifest_hash": "sha256:aaa",
            "work_unit_ids": {
                "admitted": ["WU-2"], "done": ["WU-2"], "cut": [],
                "integrated": {"WU-2": {"sha": "def"}},
            },
            "dependency_closure": {"satisfied": [], "unsatisfied": []},
        },
    }
    for ws, patch in overrides.items():
        base[ws] = {**base[ws], **patch}
    return base


def _criteria(tmp_path, records, cfg_overrides=None):
    cfg = dict(sb._DEFAULTS)
    cfg.update(cfg_overrides or {})
    return sb._identity_criteria(str(tmp_path), 1, cfg, {"records": records})


def test_counts_agreeing_while_identities_differ_is_caught(tmp_path, monkeypatch):
    """The exact failure #303 describes.

    Two workstreams each declaring ONE admitted unit satisfied the old barrier
    even when between them they had admitted a different set than the Cycle --
    the counts matched and nothing else was compared. Asserted against a REAL
    sealed manifest rather than mocks, because the point is that the manifest is
    the thing being closed against.
    """
    import spq_paths
    import spq_state_machine as spq

    project = tmp_path / "proj"
    project.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "t@e.co"),
                 ("config", "user.name", "t")):
        _git(project, *args)
    (project / "f.txt").write_text("x", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")
    (project / ".synaptory.yaml").write_text(
        'build_mode: spq\nspq:\n  workstreams:\n    - id: "frame"\n'
        '      shared_owner: true\n    - id: "spine"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv(spq_paths.ENV_WORKSTREAM, raising=False)
    spq.initialize(str(project), workstream_id="frame")
    spq.approve_baseline(str(project), approved_by="t")
    spq.open_cycle(str(project), 1, "c", [
        {"id": "WU-1", "labels": ["ws:frame"]},
        {"id": "WU-2", "labels": ["ws:spine"]},
    ])
    manifest_hash = spq.read_manifest(
        str(project), spq.identity(str(project)).cycle_id
    )["manifest_hash"]

    # Same COUNT as the manifest (two units between them), different identities:
    # spine declares WU-99 instead of the WU-2 it actually owns.
    records = _identity_records(
        frame={"manifest_hash": manifest_hash},
        spine={"manifest_hash": manifest_hash, "work_unit_ids": {
            "admitted": ["WU-99"], "done": ["WU-99"], "cut": [], "integrated": {},
        }},
    )
    assert (len(records["frame"]["work_unit_ids"]["admitted"])
            + len(records["spine"]["work_unit_ids"]["admitted"])) == 2

    criteria = _criteria(project, records)
    closure = criteria["manifest_closure"]
    assert closure["passed"] is False
    assert "WU-2" in closure["missing"], closure
    assert "WU-99" in closure["extra"], closure


def test_a_workstream_on_a_superseded_manifest_blocks_agreement(tmp_path):
    records = _identity_records(spine={"manifest_hash": "sha256:bbb"})
    criteria = _criteria(tmp_path, records)
    agreement = criteria["manifest_agreement"]
    assert agreement["passed"] is False
    assert "different manifest revisions" in agreement["detail"]
    assert "no longer has" in agreement["detail"], "say why it matters"


def test_a_record_with_no_manifest_hash_blocks_agreement(tmp_path):
    records = _identity_records()
    records["spine"] = {**records["spine"], "manifest_hash": ""}
    criteria = _criteria(tmp_path, records)
    assert criteria["manifest_agreement"]["passed"] is False


def test_an_unsatisfied_edge_blocks_dependency_closure(tmp_path):
    records = _identity_records(frame={"dependency_closure": {
        "satisfied": [], "unsatisfied": [{"unit_id": "WU-1", "dep": "WU-2"}],
    }})
    criteria = _criteria(tmp_path, records)
    assert criteria["dependency_closure"]["passed"] is False
    assert "WU-1<-WU-2" in criteria["dependency_closure"]["detail"]


def test_a_legacy_record_blocks_rather_than_passes(tmp_path):
    """Treating absence of evidence as evidence is how a barrier quietly stops
    being a barrier -- the same rule already applied to a skipped journey."""
    records = _identity_records(spine={"schema_version": "1.1"})
    criteria = _criteria(tmp_path, records)
    for name in sb._IDENTITY_CRITERIA:
        assert criteria[name]["passed"] is False, name
        assert criteria[name]["code"] == "record_schema_too_old"
        assert "spine" in criteria[name]["legacy_workstreams"]


def test_allow_legacy_records_waives_loudly_rather_than_passing_silently(tmp_path):
    records = _identity_records(spine={"schema_version": "1.1"})
    criteria = _criteria(tmp_path, records, {"allow_legacy_records": True})
    for name in sb._IDENTITY_CRITERIA:
        assert criteria[name]["passed"] is True, name
        assert criteria[name]["waived"] is True, "a waiver must be visible"
        assert "upgrade those clones" in criteria[name]["detail"]


def test_a_cycle_with_no_manifest_is_unprovable_not_proven(tmp_path):
    records = _identity_records()
    for ws in records:
        records[ws] = {**records[ws], "manifest_hash": None}
    criteria = _criteria(tmp_path, records)
    for name in sb._IDENTITY_CRITERIA:
        assert criteria[name]["passed"] is False, name
        assert criteria[name]["code"] == "no_manifest"


def test_the_criteria_tuple_and_the_receipt_total_stay_in_step():
    """`_write_barrier_receipt` reports criteria_total from the tuple, so a
    criterion added without the receipt following is a silently under-reported
    barrier.

    8 -> 9 with `dod_evaluated` (#329). The count is pinned deliberately: the
    receipt derives `criteria_total` from the tuple and follows automatically,
    but the ceremony prose does not, so this is the tripwire that catches
    `spq/sync.md` still saying "8/8" after a criterion lands.
    """
    assert len(sb.CRITERIA) == 9
    assert sb._DOD_CRITERION in sb.CRITERIA
    for name in sb._IDENTITY_CRITERIA:
        assert name in sb.CRITERIA


def test_record_path_prefers_the_collision_safe_cycle_id():
    """`cycle-<N>` keys on the per-clone Cycle NUMBER, which two clones can both
    allocate, so two different Cycles could write the same path."""
    cfg = dict(sb._DEFAULTS)
    assert sb.record_relpath(cfg, 1, "frame", "1-abcd1234") == (
        ".synaptory/cycles/1-abcd1234/sync/frame.json"
    )
    assert sb.record_relpath(cfg, 1, "frame") == ".synaptory/sync/cycle-1/frame.json"


# ── #303 §4: incremental integration pre-merge check ────────────────────────


def _incremental_project(tmp_path, monkeypatch, *, mode="incremental",
                         merge_requires=("regression_green",),
                         require_regression=False):
    """A real Cycle whose manifest declares an integration policy."""
    import json as _json

    import spq_paths
    import spq_state_machine as spq

    project = tmp_path / "proj"
    project.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "t@e.co"),
                 ("config", "user.name", "t")):
        _git(project, *args)
    (project / "f.txt").write_text("x", encoding="utf-8")
    (project / "scripts").mkdir()
    (project / "scripts" / "regress.sh").write_text(
        "#!/usr/bin/env bash\necho 'RESULT exit=0 passed=1 failed=0'\n",
        encoding="utf-8",
    )
    (project / "scripts" / "regress.sh").chmod(0o755)
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")
    (project / ".synaptory.yaml").write_text(
        'build_mode: spq\n'
        'spq:\n'
        '  workstreams:\n'
        '    - id: "spine"\n'
        '      shared_owner: true\n'
        '  sync:\n'
        '    regression_script: "scripts/regress.sh"\n'
        f'    require_regression: {str(require_regression).lower()}\n'
        '    require_journey: false\n'
        f'    integration_mode: "{mode}"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv(spq_paths.ENV_WORKSTREAM, raising=False)
    spq.initialize(str(project), workstream_id="spine")
    spq.approve_baseline(str(project), approved_by="t")
    spq.open_cycle(str(project), 1, "c", [{"id": "WU-1", "labels": ["ws:spine"]}])
    cycle_id = spq.identity(str(project)).cycle_id

    if merge_requires is not None:
        # Specialized policy cases below patch the sealed manifest. Passing
        # None exercises the policy generated from project configuration.
        import spq_manifest
        path = Path(spq_paths.manifest_path(str(project), cycle_id))
        path.chmod(0o644)
        body = _json.loads(path.read_text(encoding="utf-8"))
        body["integration_policy"]["merge_requires"] = list(merge_requires)
        body["manifest_hash"] = spq_manifest.compute_hash(body)
        path.write_text(_json.dumps(body), encoding="utf-8")
    return project, cycle_id


def _commit_incremental_candidate(project: Path, cycle_id: str) -> str:
    """Materialise the human merge result the evaluator is meant to prove."""
    _git(project, "checkout", "-q", "-b", "cycle/%s/integration" % cycle_id)
    candidate = project / "wu-1.txt"
    candidate.write_text("candidate\n", encoding="utf-8")
    _git(project, "add", candidate.name)
    _git(project, "commit", "-qm", "integrate WU-1")
    return sb.head_sha(str(project))


@needs_git
def test_incremental_requires_running_on_the_integration_ref(tmp_path, monkeypatch):
    """The merge is a human step; this checks its RESULT, so it has to run where
    the merge landed."""
    project, cycle_id = _incremental_project(tmp_path, monkeypatch)
    out = sb.evaluate_incremental(str(project), 1, unit_id="WU-1")
    assert out["verdict"] == "blocked"
    assert "on_integration_ref" in out["blocking"]
    assert "human step" in out["checks"]["on_integration_ref"]["detail"]


@needs_git
def test_incremental_green_on_the_integration_ref(tmp_path, monkeypatch):
    project, cycle_id = _incremental_project(tmp_path, monkeypatch)
    candidate = _commit_incremental_candidate(project, cycle_id)
    out = sb.evaluate_incremental(
        str(project), 1, unit_id="WU-1", candidate_sha=candidate
    )
    assert out["verdict"] == "green", out
    assert out["attestation"]["candidate_sha"] == candidate
    assert out["attestation"]["evaluated_head"] == candidate
    assert out["checks"]["regression_green"]["passed"] is True
    # The result must tell the operator what to do next, including that the
    # merge is theirs and how it gets recorded.
    assert "publish_event" in out["detail"]
    assert "--condition integrated" in out["detail"]


@needs_git
def test_incremental_refuses_a_candidate_absent_from_the_evaluated_head(
    tmp_path, monkeypatch
):
    project, cycle_id = _incremental_project(tmp_path, monkeypatch)
    _git(project, "checkout", "-q", "-b", "candidate-only")
    (project / "side.txt").write_text("not merged\n", encoding="utf-8")
    _git(project, "add", "side.txt")
    _git(project, "commit", "-qm", "candidate outside integration")
    candidate = sb.head_sha(str(project))
    _git(project, "checkout", "-q", "-b", "cycle/%s/integration" % cycle_id, "HEAD~1")

    out = sb.evaluate_incremental(
        str(project), 1, unit_id="WU-1", candidate_sha=candidate
    )
    assert out["verdict"] == "blocked"
    assert "candidate_integrated" in out["blocking"]
    assert "attestation" not in out


@needs_git
def test_generated_incremental_policy_blocks_a_failed_required_regression(
    tmp_path, monkeypatch
):
    """The production manifest generator must carry the configured regression
    requirement; tests that manually patch merge_requires cannot prove that."""
    import spq_state_machine as spq

    project, cycle_id = _incremental_project(
        tmp_path,
        monkeypatch,
        merge_requires=None,
        require_regression=True,
    )
    manifest = spq.read_manifest(str(project), cycle_id)
    assert manifest["integration_policy"]["merge_requires"] == ["regression_green"]

    script = project / "scripts" / "regress.sh"
    script.write_text(
        "#!/usr/bin/env bash\necho 'RESULT exit=9 passed=0 failed=1'\nexit 9\n",
        encoding="utf-8",
    )
    candidate = _commit_incremental_candidate(project, cycle_id)
    out = sb.evaluate_incremental(
        str(project), 1, unit_id="WU-1", candidate_sha=candidate
    )
    assert out["verdict"] == "blocked", out
    assert "regression_green" in out["blocking"]
    assert out["checks"]["regression_green"]["required"] is True
    assert out["checks"]["regression_green"]["passed"] is False


@needs_git
def test_incremental_is_not_applicable_under_at_sync(tmp_path, monkeypatch):
    """Under `at_sync` the merge IS Sync. Reporting green would read as "this
    merge was verified" when nothing was."""
    project, cycle_id = _incremental_project(tmp_path, monkeypatch, mode="at_sync")
    out = sb.evaluate_incremental(str(project), 1, unit_id="WU-1")
    assert out["verdict"] == "not_applicable"
    assert "at_sync" in out["detail"]


@needs_git
def test_incremental_refuses_an_unadmitted_unit(tmp_path, monkeypatch):
    project, cycle_id = _incremental_project(tmp_path, monkeypatch)
    out = sb.evaluate_incremental(str(project), 1, unit_id="WU-GHOST")
    assert out["verdict"] == "blocked"
    assert "unit_admitted" in out["blocking"]


@needs_git
def test_incremental_blocks_on_a_requirement_it_cannot_evaluate(tmp_path, monkeypatch):
    """A policy naming a gate nothing implements must not silently pass."""
    project, cycle_id = _incremental_project(
        tmp_path, monkeypatch, merge_requires=("regression_green", "journey_green"),
    )
    candidate = _commit_incremental_candidate(project, cycle_id)
    out = sb.evaluate_incremental(
        str(project), 1, unit_id="WU-1", candidate_sha=candidate
    )
    assert out["verdict"] == "blocked"
    assert "journey_green" in out["blocking"]
    assert any("cannot evaluate" in w for w in out["warnings"])


@needs_git
def test_incremental_reports_the_regression_even_when_not_required(tmp_path, monkeypatch):
    """An empty merge_requires must not masquerade as a passed regression: run
    it and report it, but only block when the policy demands it."""
    project, cycle_id = _incremental_project(
        tmp_path, monkeypatch, merge_requires=(),
    )
    candidate = _commit_incremental_candidate(project, cycle_id)
    out = sb.evaluate_incremental(
        str(project), 1, unit_id="WU-1", candidate_sha=candidate
    )
    assert out["verdict"] == "green"
    assert out["checks"]["regression_green"]["required"] is False
    assert out["checks"]["regression_green"]["passed"] is True


# ── #329: "nothing failed" is not "checks ran" ──────────────────────────────


def _evaluated_state(cycle=3, *, gates=(True, True)):
    """A CYCLE_EXECUTION board whose done units carry DoD verdicts."""
    stories = []
    for i, gate in enumerate(gates, start=1):
        story = {"id": "US-%d" % i, "title": "u", "state": "done"}
        if gate is not None:
            story["dod"] = {"passed": bool(gate), "critical_passed": True,
                            "checks": {}}
        stories.append(story)
    return {
        "version": "2.0", "build_mode": "spq",
        "lifecycle_state": "CYCLE_EXECUTION", "current_cycle": cycle,
        "current_stories": stories,
    }


def _ready_tree(tmp_path: Path, state: dict):
    _write_config(tmp_path, CONFIG.replace(
        'shared_digest_paths: ["contracts/"]', "shared_digest_paths: []"))
    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True, exist_ok=True)
    (orch / "pipeline-state.json").write_text(json.dumps(state))


def test_declare_ready_refuses_an_unevaluated_board(tmp_path: Path, monkeypatch):
    """The measured #329 failure: two units `done`, DoD never evaluated.

    Pre-fix the `checks_failed` guard passed vacuously -- an empty map from a
    board that evaluated nothing is indistinguishable from a clean one -- so
    this lane declared readiness exactly as loudly as one that had evaluated
    everything, and the barrier cleared.
    """
    _ready_tree(tmp_path, _evaluated_state(gates=(None, None)))
    monkeypatch.setenv("SYNAPTORY_WORKSTREAM", "frame")

    with pytest.raises(sb.BarrierError) as exc:
        sb.declare_ready(str(tmp_path), 3, declared_by="l@h3t.co",
                         run_regression=False)
    message = str(exc.value)
    assert "0 of 2" in message, message
    assert "evaluated DoD gate" in message


def test_declare_ready_refuses_a_partially_evaluated_board(tmp_path: Path, monkeypatch):
    """One evaluated, one not. A per-unit check, not a per-lane boolean."""
    _ready_tree(tmp_path, _evaluated_state(gates=(True, None)))
    monkeypatch.setenv("SYNAPTORY_WORKSTREAM", "frame")

    with pytest.raises(sb.BarrierError, match="1 of 2"):
        sb.declare_ready(str(tmp_path), 3, declared_by="l@h3t.co",
                         run_regression=False)


def test_declare_ready_accepts_a_fully_evaluated_board(tmp_path: Path, monkeypatch):
    """The fix must not block the honest case."""
    _ready_tree(tmp_path, _evaluated_state(gates=(True, True)))
    monkeypatch.setenv("SYNAPTORY_WORKSTREAM", "frame")

    out = sb.declare_ready(str(tmp_path), 3, declared_by="l@h3t.co",
                           run_regression=False)
    assert out["record"]["dod"]["stories_evaluated"] == 2


def test_cut_units_do_not_need_an_evaluated_gate(tmp_path: Path, monkeypatch):
    """A cut Work Unit is work the PO decided not to ship.

    `aggregate_sprint_dod` already excludes cancelled units, so requiring a gate
    on them would make cutting scope -- the documented release valve (§8.6) --
    impossible to declare against.
    """
    state = _evaluated_state(gates=(True,))
    state["current_stories"].append(
        {"id": "US-9", "title": "cut", "state": "cancelled"})
    _ready_tree(tmp_path, state)
    monkeypatch.setenv("SYNAPTORY_WORKSTREAM", "frame")

    out = sb.declare_ready(str(tmp_path), 3, declared_by="l@h3t.co",
                           run_regression=False)
    assert out["record"]["work_units"] == {"admitted": 1, "done": 1, "cut": 1}


# ── the barrier's own criterion (the consumer half) ────────────────────────


def _dod_records(frame_eval=1, spine_eval=1, *, drop_dod=()):
    records = {}
    for ws, evaluated in (("frame", frame_eval), ("spine", spine_eval)):
        record = {
            "schema_version": "2.0", "cycle": 1, "workstream": ws,
            "work_units": {"admitted": 1, "done": 1, "cut": 0},
            "dod": {"stories_evaluated": evaluated, "stories_passed": evaluated,
                    "checks_failed": {}},
        }
        if ws in drop_dod:
            record.pop("dod")
        records[ws] = record
    return records


def test_dod_evaluated_is_a_barrier_criterion():
    """`declare_ready` is the producer half only.

    A host that writes the record directly -- which is what the unevaluated lane
    effectively did -- never runs `declare_ready`. The barrier reads records
    precisely because it cannot trust clones, so it has to be able to see this.
    """
    assert sb._DOD_CRITERION in sb.CRITERIA


def test_criterion_blocks_a_lane_that_evaluated_nothing():
    out = sb._check_dod_evaluated({"records": _dod_records(spine_eval=0)})
    assert out["passed"] is False
    assert out["unevaluated"] == ["spine (0/1)"]
    assert "reached `done` with no DoD evaluation" in out["detail"]


def test_criterion_passes_when_every_lane_evaluated():
    out = sb._check_dod_evaluated({"records": _dod_records()})
    assert out["passed"] is True
    assert out["lanes"] == {"frame": {"admitted": 1, "evaluated": 1},
                            "spine": {"admitted": 1, "evaluated": 1}}


def test_a_record_without_a_dod_block_is_unproven_not_passing():
    """Pre-#329 records cannot speak to this.

    Same rule the module applies to a skipped journey and to 1.1 identity
    fields: treating absence of evidence as evidence is how a barrier quietly
    stops being a barrier.
    """
    out = sb._check_dod_evaluated({"records": _dod_records(drop_dod=("spine",))})
    assert out["passed"] is False
    assert out["unproven"] == ["spine"]
    assert out["lanes"]["spine"]["evaluated"] is None


def test_criterion_does_not_double_count_missing_records():
    """No records at all is `records_present`'s finding.

    Reporting it here too would put one failure in `blocking` twice and make the
    verdict read as two independent problems.
    """
    out = sb._check_dod_evaluated({"records": {}})
    assert out["passed"] is True
    assert out["lanes"] == {}
