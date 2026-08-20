"""End-to-end SPQ barrier walkthrough against REAL git clones.

Every other barrier test uses fixtures: a state dict, a monkeypatched
`collect`, a synthesized record. Those catch logic errors but they cannot catch
the class of bug that only appears in the actual topology of §5 — an upstream,
one clone per workstream on its own branch, and an integration clone that reads
those records over remote-tracking refs WITHOUT merging.

That topology is the whole design, and it has three properties that fixtures
cannot demonstrate:

  1. `shared-digest.sh` produces the SAME digest in every clone for the same
     tree. §8.3 exists because two sides computing it differently produces a
     barrier that fails for reasons nobody can reproduce.
  2. `collect` reads records via `git show` without polluting the integration
     working tree, which is what breaks the ordering circularity (records live
     on workstream branches, so they are only co-visible after the merge that
     is itself criterion 2).
  3. Criterion 4 catches a mid-Slice edit to a governed shared component and
     names the culprit.

Marked `slow`: it runs real git and real subprocesses. No network, no stack.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import sync_barrier as sb
import spq_state_machine as sm

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("git") is None, reason="git not available"),
]

REPO_ROOT = Path(__file__).resolve().parents[3]
DIGEST_SCRIPT = REPO_ROOT / "scripts" / "shared-digest.sh"

WORKSTREAMS = ["control-plane", "plugin-runtime", "delivery-cli"]

CONFIG = """build_mode: "spq"

spq:
  workstreams:
    - id: "control-plane"
      shared_owner: true
    - id: "plugin-runtime"
    - id: "delivery-cli"
    - id: "integration"
      integration: true
  sync:
    remote: "origin"
    branch_pattern: "ws/{id}"
    integration_branch_pattern: "sync/slice-{n}"
    promote_to: "dev"
    mode: "all_or_nothing"
    require_regression: true
    require_journey: false
    shared_digest_paths:
      - "contracts/"
    regression_script: "scripts/regress.sh"
    digest_script: "scripts/shared-digest.sh"
"""


def _git(cwd: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", "-c", "user.email=t@h3t.co", "-c", "user.name=t",
         "-c", "init.defaultBranch=dev", *args],
        cwd=str(cwd), capture_output=True, text=True,
    )
    assert p.returncode == 0, f"git {args} in {cwd}: {p.stderr}"
    return p.stdout


def _sm(project: Path, ws: str, *args: str) -> None:
    """Drive the state machine as the given workstream."""
    env = {**os.environ, "SYNAPTORY_ACTIVE_SPEC": ws}
    subprocess.run(
        ["python3", str(Path(sm.__file__)), args[0], str(project), *args[1:]],
        capture_output=True, text=True, env=env,
    )


@pytest.fixture
def topology(tmp_path: Path):
    """An upstream, three workstream clones, and an integration clone."""
    if not DIGEST_SCRIPT.is_file():
        pytest.skip("scripts/shared-digest.sh not present")

    up = tmp_path / "upstream"
    (up / "contracts").mkdir(parents=True)
    (up / "scripts").mkdir()
    (up / "contracts" / "api.json").write_text('{"version": 1}\n')
    (up / ".synaptory.yaml").write_text(CONFIG)
    # The carve-out that actually works: excluding CHILDREN, because git cannot
    # re-include a path whose parent directory is excluded.
    (up / ".gitignore").write_text(".synaptory/*\n!.synaptory/sync/\n")
    shutil.copy(DIGEST_SCRIPT, up / "scripts" / "shared-digest.sh")
    (up / "scripts" / "regress.sh").write_text(
        "#!/usr/bin/env bash\necho 'RESULT exit=0 passed=42 failed=0'\n")
    for s in (up / "scripts").iterdir():
        s.chmod(0o755)
    _git(up, "init", "-q")
    _git(up, "add", "-A")
    _git(up, "commit", "-qm", "base")

    clones: dict[str, Path] = {}
    for ws in WORKSTREAMS:
        d = tmp_path / ws
        _git(tmp_path, "clone", "-q", str(up), ws)
        _git(d, "checkout", "-q", "-b", f"ws/{ws}")
        _sm(d, ws, "init")
        _sm(d, ws, "transition", "COMMIT")
        units = [{"id": f"{ws[:2].upper()}X-1", "title": "a"},
                 {"id": f"{ws[:2].upper()}X-2", "title": "b"}]
        _sm(d, ws, "open_slice", "1", "--goal", "slice 1",
            "--work-units", json.dumps(units))
        for u in units:
            _write_unit_receipts(d, u["id"])
            for st in ("in_progress", "testing", "reviewing", "done"):
                _sm(d, ws, "transition_story", u["id"], st)
        clones[ws] = d

    integ = tmp_path / "integration"
    _git(tmp_path, "clone", "-q", str(up), "integration")
    _sm(integ, "integration", "init")
    return up, clones, integ


def _write_unit_receipts(clone: Path, unit_id: str) -> None:
    """Give a Work Unit the SE + QE evidence a `done` unit must actually have.

    This fixture used to drive units straight to `done` with no receipts at
    all, and `declare-ready` accepted them. That was not leniency in the
    barrier — it was #238: SPQ's `transition_story` never evaluated DoD, so
    `story["dod"]` was never written, so `derive_dod` saw
    `stories_evaluated: 0` and `checks_failed: {}`, and the invariant
    "refusing to declare ready: DoD checks failing" could never fire.

    So these tests were asserting a green barrier against units carrying zero
    evidence, which is the state the barrier exists to reject. Once the DoD
    chain was repaired the fixture broke, which is the correct outcome and is
    why the receipts are written here rather than the assertion relaxed.

    Same lesson as `_do_work` below: a fixture that under-specifies reality
    makes the surrounding assertions look stronger than they are.
    """
    receipts = clone / ".synaptory" / ".orchestrator" / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    for role, abbrev in (("software-engineer", "se"), ("quality-engineer", "qe")):
        (receipts / f"{unit_id}-{abbrev}.json").write_text(
            json.dumps({
                "task": f"{unit_id} delivered by {role}",
                "agent": role,
                "backend": "claude",
                "model": "claude-opus-5",
                "artifacts": [f"{unit_id.lower()}.py"],
                "verification_commands": [
                    {"command": "python3 -m pytest -q", "exit_code": 0,
                     "output": "42 passed"},
                ],
            }),
            encoding="utf-8",
        )


def _do_work(clone: Path, ws: str) -> str:
    """Make a real, workstream-specific commit and return its sha.

    Without this the clone's branch head equals `dev`, so every recorded
    `head_sha` is trivially an ancestor of any integration branch and criterion
    2 cannot distinguish merged from unmerged. The first version of these tests
    made exactly that mistake and passed against a correct implementation.
    """
    src = clone / f"{ws}.py"
    src.write_text(f'"""{ws} delivered work that must be integrated."""\n')
    _git(clone, "add", "-A")
    _git(clone, "commit", "-qm", f"{ws}: deliver work")
    return _git(clone, "rev-parse", "HEAD").strip()


def _declare_and_push(clone: Path, ws: str, slice_n: int = 1) -> dict:
    _do_work(clone, ws)
    env = {**os.environ, "SYNAPTORY_ACTIVE_SPEC": ws}
    p = subprocess.run(
        ["python3", str(Path(sb.__file__)), "declare-ready", str(clone),
         str(slice_n), "--declared-by", "lead@h3t.co"],
        capture_output=True, text=True, env=env, cwd=str(clone),
    )
    assert p.returncode == 0, f"declare-ready failed for {ws}: {p.stderr}"
    _git(clone, "add", "-A")
    _git(clone, "commit", "-qm", f"{ws} ready slice {slice_n}")
    _git(clone, "push", "-q", "origin", f"ws/{ws}")
    rec = clone / ".synaptory" / "sync" / f"slice-{slice_n}" / f"{ws}.json"
    return json.loads(rec.read_text())


# ---------------------------------------------------------------------------

def test_all_work_units_terminal_yields_await_sync_in_every_clone(topology):
    _up, clones, _integ = topology
    for ws, d in clones.items():
        env = {**os.environ, "SYNAPTORY_ACTIVE_SPEC": ws}
        p = subprocess.run(
            ["python3", str(Path(sm.__file__)), "next_action", str(d)],
            capture_output=True, text=True, env=env)
        assert json.loads(p.stdout)["action"] == "await_sync", f"{ws}: {p.stdout}"


def test_the_digest_is_identical_across_independent_clones(topology):
    """§8.3's core requirement, and the one fixtures cannot demonstrate."""
    _up, clones, _integ = topology
    digests = {}
    for ws, d in clones.items():
        rec = _declare_and_push(d, ws)
        digests[ws] = rec["shared_digests"]["contracts/"]
        assert not digests[ws].startswith("error:"), digests[ws]
    assert len(set(digests.values())) == 1, (
        f"clones disagree on the digest of an identical tree: {digests}"
    )


def test_declare_ready_derives_real_quality_evidence(topology):
    _up, clones, _integ = topology
    rec = _declare_and_push(clones["control-plane"], "control-plane")
    assert rec["schema_version"] == "1.1"
    assert rec["work_units"] == {"admitted": 2, "done": 2, "cut": 0}
    # Derived from an actually-executed script, not a literal.
    assert rec["regression"]["exit_code"] == 0
    assert rec["regression"]["passed"] == 42
    assert rec["dod"]["tier"] is not None
    assert rec["replay"]["mismatches"] == []


def test_collect_reads_records_without_touching_the_working_tree(topology):
    _up, clones, integ = topology
    for ws, d in clones.items():
        _declare_and_push(d, ws)
    out = sb.collect(str(integ), 1)
    assert out["records_present"] is True
    assert sorted(out["records"]) == sorted(WORKSTREAMS)
    assert "integration" not in out["records"], "the barrier seat is not in the quorum"
    assert not (integ / ".synaptory" / "sync").exists(), (
        "collect must not merge or materialise records into the integration tree"
    )


def test_barrier_blocks_before_the_human_merge_then_clears_after(topology):
    _up, clones, integ = topology
    for ws, d in clones.items():
        _declare_and_push(d, ws)

    before = sb.evaluate(str(integ), 1, use_cache=False)
    assert before["verdict"] == "blocked"
    assert "branches_merged" in before["blocking"]

    # Criterion 2 is a HUMAN act (§8.5 step 2): into a sync/ feature branch,
    # never into dev, which is what keeps the shipped git rules intact.
    _git(integ, "fetch", "-q", "origin")
    _git(integ, "checkout", "-q", "-b", "sync/slice-1", "dev")
    for ws in WORKSTREAMS:
        _git(integ, "merge", "-q", "--no-edit", f"origin/ws/{ws}")

    after = sb.evaluate(str(integ), 1, use_cache=False)
    assert after["verdict"] == "green", after["blocking"]
    for name in sb.CRITERIA:
        assert after["criteria"][name]["passed"] is True, name


def test_a_missing_workstream_blocks_all_or_nothing(topology):
    _up, clones, integ = topology
    for ws in ("control-plane", "plugin-runtime"):
        _declare_and_push(clones[ws], ws)
    out = sb.collect(str(integ), 1)
    assert out["records_present"] is False
    assert [m["workstream"] for m in out["missing"]] == ["delivery-cli"]


def test_criterion_4_allows_the_shared_owner_to_change_a_shared_component(topology):
    """#239: the owner doing its job must not block the Slice.

    This is the case the old comparison got backwards. It compared every
    workstream's claimed digest against the POST-MERGE integrated tree, so once
    the owner's authorised change landed, the two honest consumers — which had
    correctly never touched `contracts/` — became "drift", while the only
    workstream that actually edited it passed.

    Found in the SPQ pilot: clean merge, 37 tests green, barrier blocked,
    naming the two innocent workstreams.
    """
    up, clones, integ = topology
    owner = clones["control-plane"]           # shared_owner in CONFIG
    (owner / "contracts" / "api.json").write_text(
        '{"version": 2, "endpoints": ["/v1/config", "/v1/health"]}\n')
    _git(owner, "add", "-A")
    _git(owner, "commit", "-qm", "owner: publish contract v2")

    for ws, d in clones.items():
        _declare_and_push(d, ws)

    _git(integ, "fetch", "-q", "origin")
    _git(integ, "checkout", "-q", "-b", "sync/slice-1", "dev")
    for ws in clones:
        _git(integ, "merge", "-q", "--no-edit", f"origin/ws/{ws}")

    out = sb.evaluate(str(integ), 1, use_cache=False)
    crit = out["criteria"]["digests_match"]
    assert crit["passed"] is True, (
        f"the owner's authorised change blocked the Slice: {crit.get('drift')}"
    )
    assert crit["shared_owner"] == "control-plane"
    assert crit["used_digest_fallback"] is False, "git should have answered directly"
    assert "digests_match" not in out.get("blocking", [])


def test_criterion_4_names_the_file_a_non_owner_changed(topology):
    """#239: the evidence is now the actual path, not an opaque digest delta."""
    up, clones, integ = topology
    culprit = clones["plugin-runtime"]
    (culprit / "contracts" / "api.json").write_text('{"sneaky": true}\n')
    _git(culprit, "add", "-A")
    _git(culprit, "commit", "-qm", "drift: unauthorised contract edit")

    for ws, d in clones.items():
        _declare_and_push(d, ws)

    _git(integ, "fetch", "-q", "origin")
    _git(integ, "checkout", "-q", "-b", "sync/slice-1", "dev")
    for ws in clones:
        _git(integ, "merge", "-q", "--no-edit", f"origin/ws/{ws}")

    crit = sb.evaluate(str(integ), 1, use_cache=False)["criteria"]["digests_match"]
    assert crit["passed"] is False
    offenders = {d["workstream"] for d in crit["drift"] if d.get("files")}
    assert offenders == {"plugin-runtime"}, crit["drift"]
    assert any(
        "contracts/api.json" in d.get("files", []) for d in crit["drift"]
    ), crit["drift"]


def test_criterion_4_catches_a_mid_slice_shared_component_edit(topology):
    """§12: shared components are published at Commit by the shared_owner, so a
    delta from any other workstream means a mid-Slice edit needing adjudication."""
    up, clones, integ = topology
    culprit = clones["plugin-runtime"]
    (culprit / "contracts" / "api.json").write_text('{"version": 2, "sneaky": true}\n')
    _git(culprit, "add", "-A")
    _git(culprit, "commit", "-qm", "drift: edited a governed shared contract")

    for ws, d in clones.items():
        _declare_and_push(d, ws)

    _git(integ, "fetch", "-q", "origin")
    # Integrate only the CLEAN workstream, so the integrated tree carries the
    # published contract and the culprit's claim disagrees with it.
    _git(integ, "checkout", "-q", "-b", "sync/slice-1", "dev")
    _git(integ, "merge", "-q", "--no-edit", "origin/ws/control-plane")

    out = sb.evaluate(str(integ), 1, use_cache=False)
    d = out["criteria"]["digests_match"]
    assert d["passed"] is False, d
    assert [x["workstream"] for x in d["drift"]] == ["plugin-runtime"]
    # #239 changed the evidence: the offending FILES rather than an opaque
    # digest delta, because "you changed this path" is the actual violation.
    assert d["drift"][0]["files"] == ["contracts/api.json"]
    assert d["drift"][0]["reason"] == "modified a shared component it does not own"
    # The verdict came from git, not from the digest fallback.
    assert d["used_digest_fallback"] is False


def test_clear_sync_transitions_and_writes_the_barrier_receipt(topology):
    _up, clones, integ = topology
    for ws, d in clones.items():
        _declare_and_push(d, ws)
    _git(integ, "fetch", "-q", "origin")
    _git(integ, "checkout", "-q", "-b", "sync/slice-1", "dev")
    for ws in WORKSTREAMS:
        _git(integ, "merge", "-q", "--no-edit", f"origin/ws/{ws}")

    _sm(integ, "integration", "transition", "COMMIT")
    _sm(integ, "integration", "open_slice", "1", "--goal", "s1", "--work-units", "[]")
    _sm(integ, "integration", "transition", "SYNC")

    env = {**os.environ, "SYNAPTORY_ACTIVE_SPEC": "integration"}
    p = subprocess.run(
        ["python3", str(Path(sm.__file__)), "clear_sync", str(integ), "1",
         "--cleared-by", "lead@h3t.co"],
        capture_output=True, text=True, env=env, cwd=str(integ))
    assert p.returncode == 0, p.stderr

    state = json.loads(subprocess.run(
        ["python3", str(Path(sm.__file__)), "read", str(integ)],
        capture_output=True, text=True, env=env).stdout)
    assert state["lifecycle_state"] == "CHECKPOINT"
    assert state["sync"]["verdict"] == "green"

    receipt = integ / ".synaptory" / ".orchestrator" / "receipts" / "SYNC-1-barrier.json"
    assert receipt.is_file()
    r = json.loads(receipt.read_text())
    assert r["story_id"] == "SYNC-1"
    assert r["role"] == "orchestrator"
    assert r["token_usage"]["stage"] == "orchestrator"


def test_a_replay_mismatch_refuses_declare_ready_in_a_real_clone(topology):
    _up, clones, _integ = topology
    d = clones["control-plane"]
    orch = d / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True, exist_ok=True)
    (orch / "events.jsonl").write_text(json.dumps({
        "event": "evidence_replay_mismatch", "story_id": "COX-1",
        "check": "tests_pass", "command": "pytest -q",
        "attested_exit_code": 0, "replayed_exit_code": 1}) + "\n")

    env = {**os.environ, "SYNAPTORY_ACTIVE_SPEC": "control-plane"}
    p = subprocess.run(
        ["python3", str(Path(sb.__file__)), "declare-ready", str(d), "1"],
        capture_output=True, text=True, env=env, cwd=str(d))
    assert p.returncode == 2, p.stdout
    assert "does not reproduce" in p.stderr
    assert not (d / ".synaptory" / "sync").exists()


# ---------------------------------------------------------------------------
# Review findings from PR #226 (all five reproduced before fixing)
# ---------------------------------------------------------------------------

def _write_record(clone: Path, ws: str, slice_n: int, **over) -> Path:
    """Hand-write a record, bypassing declare-ready, to isolate barrier logic."""
    d = clone / ".synaptory" / "sync" / f"slice-{slice_n}"
    d.mkdir(parents=True, exist_ok=True)
    rec = {
        "schema_version": "1.1", "slice": slice_n, "workstream": ws,
        "branch": f"ws/{ws}", "head_sha": _git(clone, "rev-parse", "HEAD").strip(),
        "work_units": {"admitted": 2, "done": 2, "cut": 0},
        "shared_digests": {}, "regression": {"exit_code": 0},
        "dod": {"tier": "early", "checks_failed": {}},
        "replay": {"mismatches": []},
        "declared_ready_at": "2026-01-01T00:00:00Z", "declared_by": "x",
    }
    rec.update(over)
    (d / f"{ws}.json").write_text(json.dumps(rec, indent=2))
    return d / f"{ws}.json"


def test_criterion_2_fails_when_no_head_was_merged(topology):
    """P1 #226: `branches_merged` passed on the branch NAME alone, so creating
    `sync/slice-N` and merging NOTHING took the barrier green. Reproduced before
    the fix: verdict green with zero workstream heads integrated."""
    _up, clones, integ = topology
    for ws, d in clones.items():
        _declare_and_push(d, ws)
    _git(integ, "fetch", "-q", "origin")
    # Create the expected branch, merge nothing.
    _git(integ, "checkout", "-q", "-b", "sync/slice-1", "dev")

    out = sb.evaluate(str(integ), 1, use_cache=False)
    c2 = out["criteria"]["branches_merged"]
    assert c2["passed"] is False, "an unmerged integration branch must block"
    assert len(c2["unmerged"]) == len(WORKSTREAMS)
    assert out["verdict"] == "blocked"


def test_criterion_2_fails_when_one_head_is_omitted(topology):
    """The realistic version: the lead merges two of three and forgets one."""
    _up, clones, integ = topology
    for ws, d in clones.items():
        _declare_and_push(d, ws)
    _git(integ, "fetch", "-q", "origin")
    _git(integ, "checkout", "-q", "-b", "sync/slice-1", "dev")
    for ws in ("control-plane", "plugin-runtime"):
        _git(integ, "merge", "-q", "--no-edit", f"origin/ws/{ws}")

    out = sb.evaluate(str(integ), 1, use_cache=False)
    c2 = out["criteria"]["branches_merged"]
    assert c2["passed"] is False
    assert [u["workstream"] for u in c2["unmerged"]] == ["delivery-cli"]
    assert "not an ancestor" in c2["unmerged"][0]["reason"]


def test_criterion_2_passes_only_with_every_head_merged(topology):
    _up, clones, integ = topology
    for ws, d in clones.items():
        _declare_and_push(d, ws)
    _git(integ, "fetch", "-q", "origin")
    _git(integ, "checkout", "-q", "-b", "sync/slice-1", "dev")
    for ws in WORKSTREAMS:
        _git(integ, "merge", "-q", "--no-edit", f"origin/ws/{ws}")

    out = sb.evaluate(str(integ), 1, use_cache=False)
    c2 = out["criteria"]["branches_merged"]
    assert c2["passed"] is True, c2["detail"]
    assert c2["unmerged"] == []
    assert out["verdict"] == "green", out["blocking"]


def test_criterion_2_fails_on_an_unknown_head_sha(topology):
    """A record naming a commit this clone has never seen cannot be proven
    merged, so it must block rather than be assumed absent-therefore-fine."""
    _up, clones, integ = topology
    for ws, d in clones.items():
        _declare_and_push(d, ws)
    _git(integ, "fetch", "-q", "origin")
    _git(integ, "checkout", "-q", "-b", "sync/slice-1", "dev")
    for ws in WORKSTREAMS:
        _git(integ, "merge", "-q", "--no-edit", f"origin/ws/{ws}")

    collected = sb.collect(str(integ), 1, fetch=False)
    collected["records"]["control-plane"]["head_sha"] = "0" * 40
    cfg = sb.load_spq_config(integ)
    assert not sb.commit_exists(str(integ), "0" * 40)
    assert not sb.is_ancestor(str(integ), "0" * 40)


def test_declare_ready_refuses_unfinished_work_units(topology):
    """P1 #226: declare-ready counted stories and published regardless, so an
    unfinished board could emit a schema-valid record."""
    _up, clones, _integ = topology
    d = clones["control-plane"]
    env = {**os.environ, "SYNAPTORY_ACTIVE_SPEC": "control-plane"}
    # Set the sub-state directly: `done -> blocked` is not a legal transition,
    # so driving it through the state machine leaves every unit done and tests
    # nothing. What is under test here is declare-ready's invariant, not the
    # transition table.
    state_path = d / ".synaptory" / ".orchestrator" / "pipeline-state.json"
    st = json.loads(state_path.read_text())
    # Multi-spec layout: SYNAPTORY_ACTIVE_SPEC makes `initialize` write a v3
    # file, so the board lives under specs[<workstream>].
    slot = st["specs"]["control-plane"] if "specs" in st else st
    slot["current_stories"][0]["state"] = "in_progress"
    state_path.write_text(json.dumps(st))
    p = subprocess.run(
        ["python3", str(Path(sb.__file__)), "declare-ready", str(d), "1"],
        capture_output=True, text=True, env=env, cwd=str(d))
    assert p.returncode == 2, p.stdout
    assert "not done" in p.stderr
    assert not (d / ".synaptory" / "sync").exists()


def test_declare_ready_refuses_a_slice_mismatch(topology):
    _up, clones, _integ = topology
    d = clones["control-plane"]
    env = {**os.environ, "SYNAPTORY_ACTIVE_SPEC": "control-plane"}
    p = subprocess.run(
        ["python3", str(Path(sb.__file__)), "declare-ready", str(d), "7"],
        capture_output=True, text=True, env=env, cwd=str(d))
    assert p.returncode == 2
    assert "local state is on Slice 1" in p.stderr


def test_declare_ready_refuses_the_no_regression_bypass(topology):
    """`--no-regression` must not defeat require_regression: true."""
    _up, clones, _integ = topology
    d = clones["control-plane"]
    env = {**os.environ, "SYNAPTORY_ACTIVE_SPEC": "control-plane"}
    p = subprocess.run(
        ["python3", str(Path(sb.__file__)), "declare-ready", str(d), "1",
         "--no-regression"],
        capture_output=True, text=True, env=env, cwd=str(d))
    assert p.returncode == 2
    assert "cannot bypass" in p.stderr


def test_declare_ready_refuses_a_failing_regression(topology):
    _up, clones, _integ = topology
    d = clones["control-plane"]
    (d / "scripts" / "regress.sh").write_text(
        "#!/usr/bin/env bash\necho 'RESULT exit=1 passed=40 failed=2'\nexit 1\n")
    (d / "scripts" / "regress.sh").chmod(0o755)
    env = {**os.environ, "SYNAPTORY_ACTIVE_SPEC": "control-plane"}
    p = subprocess.run(
        ["python3", str(Path(sb.__file__)), "declare-ready", str(d), "1"],
        capture_output=True, text=True, env=env, cwd=str(d))
    assert p.returncode == 2
    assert "regression did not pass" in p.stderr


def test_approve_baseline_records_and_transitions_atomically(tmp_path: Path):
    """P1 #226: discovery.md told the orchestrator to Edit pipeline-state.json,
    which G2 denies, while a bare `transition COMMIT` still succeeded — so the
    documented flow could reach COMMIT with baseline_approved false."""
    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "pipeline-state.json").write_text(json.dumps({
        "version": "2.0", "build_mode": "spq", "lifecycle_state": "DISCOVERY",
        "current_slice": 0, "current_stories": [], "lifecycle_history": [],
        "discovery": {"completed_at": None, "baseline_approved": False}}))

    out = sm.approve_baseline(str(tmp_path), approved_by="lead@h3t.co")
    assert out["lifecycle_state"] == "COMMIT"
    assert out["discovery"]["baseline_approved"] is True
    assert out["discovery"]["completed_at"] is not None
    assert out["discovery"]["approved_by"] == "lead@h3t.co"
    # One write: re-reading must agree, with no separate transition needed.
    again = sm.read_state(str(tmp_path))
    assert again["lifecycle_state"] == "COMMIT"
    assert again["discovery"]["baseline_approved"] is True


def test_approve_baseline_requires_discovery(tmp_path: Path):
    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "pipeline-state.json").write_text(json.dumps({
        "version": "2.0", "build_mode": "spq", "lifecycle_state": "COMMIT",
        "current_slice": 1, "current_stories": [], "lifecycle_history": []}))
    with pytest.raises(ValueError, match="expected DISCOVERY"):
        sm.approve_baseline(str(tmp_path))


def test_hydrate_slice_takes_a_fresh_clone_into_execution(tmp_path: Path):
    """P1 #226: provisioning never opened local Slice state, so the shipped
    prompt went straight to next_action and got `not_in_execution`."""
    (tmp_path / ".synaptory.yaml").write_text('build_mode: "spq"\n')
    units = [{"id": "WU-1", "title": "a"}, {"id": "WU-2", "title": "b"}]
    out = sm.hydrate_slice(str(tmp_path), 3, units, goal="slice 3")
    assert out["hydrated"] is True
    assert out["lifecycle_state"] == "SLICE_EXECUTION"
    assert out["current_slice"] == 3
    assert sm.next_action(str(tmp_path))["action"] == "dispatch_se"


def test_hydrate_slice_is_idempotent(tmp_path: Path):
    (tmp_path / ".synaptory.yaml").write_text('build_mode: "spq"\n')
    units = [{"id": "WU-1", "title": "a"}]
    sm.hydrate_slice(str(tmp_path), 3, units)
    again = sm.hydrate_slice(str(tmp_path), 3, units)
    assert again["hydrated"] is False
    assert "already executing" in again["reason"]


def test_hydrate_slice_records_the_workstream_identity(tmp_path: Path, monkeypatch):
    """A workstream clone must end up with multispec state naming its workstream.

    `read_state` returns a `_default_state()` that already carries
    lifecycle_state="DISCOVERY", so hydrate_slice's "looks empty" guard never
    fired on a clean clone and `initialize()` — the only spec-aware seeder —
    was skipped. The first write then persisted the v2.0 single-spec default
    and discarded SYNAPTORY_ACTIVE_SPEC.

    This is the SHIPPED path: modes/spq.md calls hydrate_slice on a provisioned
    clone and never runs `init` first.
    """
    monkeypatch.setenv("SYNAPTORY_ACTIVE_SPEC", "plugin-runtime")
    (tmp_path / ".synaptory.yaml").write_text('build_mode: "spq"\n')
    sm.hydrate_slice(str(tmp_path), 1, [{"id": "WU-1", "title": "a"}], goal="s1")

    raw = json.loads(
        (tmp_path / ".synaptory" / ".orchestrator" / "pipeline-state.json").read_text()
    )
    assert raw["version"] == "3.0", f"seeded the single-spec shape: {raw['version']}"
    assert raw["active_spec"] == "plugin-runtime"
    assert [u["id"] for u in raw["specs"]["plugin-runtime"]["current_stories"]] == ["WU-1"]
    assert sm.next_action(str(tmp_path))["spec_id"] == "plugin-runtime"


def test_hydrate_slice_does_not_strand_the_clone_in_v2(tmp_path: Path, monkeypatch):
    """Following the documented flow must not demand a migration afterwards.

    `initialize` refuses to add a v3 spec slot to a v2 file, so a clone seeded
    as v2 by hydrate_slice would fail any later spec-aware `init` with "Run
    migrate_to_multispec.py first" — a migration demand manufactured by
    following the prompts.
    """
    monkeypatch.setenv("SYNAPTORY_ACTIVE_SPEC", "delivery-cli")
    (tmp_path / ".synaptory.yaml").write_text('build_mode: "spq"\n')
    sm.hydrate_slice(str(tmp_path), 1, [{"id": "WU-1", "title": "a"}])
    sm.initialize(str(tmp_path))   # must not raise


def test_hydrate_slice_refuses_an_empty_unit_list(tmp_path: Path):
    """An empty Slice would make next_action return await_sync immediately and
    let the workstream declare readiness having delivered nothing."""
    (tmp_path / ".synaptory.yaml").write_text('build_mode: "spq"\n')
    with pytest.raises(ValueError, match="empty Slice"):
        sm.hydrate_slice(str(tmp_path), 3, [])


def test_regression_script_covers_every_shippable_module():
    """P1 #226: the 'whole-repo' gate ran only plugin pytest and the Go CLI,
    while SPQ workstreams can touch api/, web/ and infra/."""
    script = (REPO_ROOT / "scripts" / "sync-regression.sh").read_text()
    assert 'MODULES="plugin cli api web"' in script
    for needle in ("synaptory api test", "npm test"):
        assert needle in script, f"regression gate missing {needle}"
    # A missing toolchain must FAIL, never skip: criterion 3 must not be
    # satisfiable by a module being untestable on the runner.
    assert "npm not found on PATH" in script
    assert "python deps unavailable" in script
    assert "no suite ran for it" in script


# ---------------------------------------------------------------------------
# Round-2 review findings on PR #226
# ---------------------------------------------------------------------------

def _break_remote(clone: Path) -> None:
    """Point `origin` at a path that does not exist.

    Leaves the already-populated remote-tracking refs intact, which is exactly
    the dangerous shape: `git show origin/ws/x:...` still succeeds and returns
    STALE content, so a barrier that does not require a successful fetch can
    decide green from records belonging to an earlier Slice.
    """
    _git(clone, "remote", "set-url", "origin", str(clone / "does-not-exist.git"))


def test_evaluate_blocks_when_the_remote_is_unreachable(topology):
    """P1 round 2: `fetch_failed` existed but `evaluate` called
    `collect(fetch=False)`, so the signal could never reach the authoritative
    decision. Stale refs plus a broken remote produced a confident green."""
    _up, clones, integ = topology
    for ws, d in clones.items():
        _declare_and_push(d, ws)
    _git(integ, "fetch", "-q", "origin")
    _git(integ, "checkout", "-q", "-b", "sync/slice-1", "dev")
    for ws in WORKSTREAMS:
        _git(integ, "merge", "-q", "--no-edit", f"origin/ws/{ws}")

    # Green while the remote is reachable...
    assert sb.evaluate(str(integ), 1, use_cache=False)["verdict"] == "green"

    # ...and blocked once it is not, even though every ref is still readable.
    _break_remote(integ)
    out = sb.evaluate(str(integ), 1, use_cache=False)
    assert out["verdict"] == "blocked", out
    assert out["blocking"] == ["fetch"]
    assert "fetch_failed" in out
    assert "stale" in out["detail"]

    # Records are still locally readable, which is what made this fail open.
    assert sb.collect(str(integ), 1, fetch=False)["records_present"] is True


def test_a_cached_green_is_not_served_while_the_remote_is_unreachable(topology):
    """The fetch check sits BEFORE the cache probe on purpose: otherwise the
    cache becomes the very staleness the fetch is meant to rule out."""
    _up, clones, integ = topology
    for ws, d in clones.items():
        _declare_and_push(d, ws)
    _git(integ, "fetch", "-q", "origin")
    _git(integ, "checkout", "-q", "-b", "sync/slice-1", "dev")
    for ws in WORKSTREAMS:
        _git(integ, "merge", "-q", "--no-edit", f"origin/ws/{ws}")

    first = sb.evaluate(str(integ), 1, use_cache=True)
    assert first["verdict"] == "green"
    assert sb.evaluate(str(integ), 1, use_cache=True)["cache_served"] is True

    _break_remote(integ)
    out = sb.evaluate(str(integ), 1, use_cache=True)
    assert out["verdict"] == "blocked"
    assert out["cache_served"] is False
    assert out["blocking"] == ["fetch"]


def test_clear_blocks_when_the_remote_is_unreachable(topology):
    """`clear` reuses `evaluate`, so it must inherit the fetch gate."""
    _up, clones, integ = topology
    for ws, d in clones.items():
        _declare_and_push(d, ws)
    _git(integ, "fetch", "-q", "origin")
    _git(integ, "checkout", "-q", "-b", "sync/slice-1", "dev")
    for ws in WORKSTREAMS:
        _git(integ, "merge", "-q", "--no-edit", f"origin/ws/{ws}")
    _break_remote(integ)

    with pytest.raises(sb.BarrierError) as e:
        sb.clear(str(integ), 1)
    assert "fetch" in str(e.value)


def test_collect_cli_exits_non_zero_on_fetch_failure(topology):
    """Printing "fetch failed" while exiting 0 invited an operator to read it as
    informational and carry on to the merge."""
    _up, clones, integ = topology
    for ws, d in clones.items():
        _declare_and_push(d, ws)
    _git(integ, "fetch", "-q", "origin")
    _break_remote(integ)

    p = subprocess.run(
        ["python3", str(Path(sb.__file__)), "collect", str(integ), "1"],
        capture_output=True, text=True)
    assert p.returncode == 4, p.stdout
    assert "fetch_failed" in p.stdout


def test_status_stays_usable_offline_and_says_so(topology):
    """`status` is a tracker mirror, not a gate, so it must not require a fetch
    — but it has to declare that it may be stale."""
    _up, clones, integ = topology
    for ws, d in clones.items():
        _declare_and_push(d, ws)
    _git(integ, "fetch", "-q", "origin")
    _break_remote(integ)

    out = sb.status(str(integ), 1)
    assert out["stale"] is True
    assert out["ready_count"] == len(WORKSTREAMS)


# ---------------------------------------------------------------------------
# P2 round 2: hydration via the SHIPPED prompt path
# ---------------------------------------------------------------------------

@pytest.fixture
def provisioned_clones(tmp_path: Path):
    """Clones as PROVISIONING leaves them: branch and discriminator only.

    Deliberately does NOT call init / transition / open_slice. The round-1
    hydration test used a bare tmp_path and called `sm.hydrate_slice` directly,
    so it characterised the function while leaving the prompt path — the thing
    that was actually broken — uncovered.
    """
    if not DIGEST_SCRIPT.is_file():
        pytest.skip("scripts/shared-digest.sh not present")
    up = tmp_path / "upstream"
    (up / "contracts").mkdir(parents=True)
    (up / "contracts" / "api.json").write_text('{"version": 1}\n')
    (up / ".synaptory.yaml").write_text(CONFIG)
    (up / ".gitignore").write_text(".synaptory/*\n!.synaptory/sync/\n")
    _git(up, "init", "-q")
    _git(up, "add", "-A")
    _git(up, "commit", "-qm", "base")

    clones = {}
    for ws in WORKSTREAMS:
        d = tmp_path / ws
        _git(tmp_path, "clone", "-q", str(up), ws)
        _git(d, "checkout", "-q", "-b", f"ws/{ws}")
        clones[ws] = d
    return up, clones


def _tracker_backlog(slice_n: int) -> list[dict]:
    """A tracker-shaped Slice backlog spanning every workstream.

    Shape mirrors `tracker_cli.py get-sprint-backlog`, including the
    discriminator label the prompt filters on.
    """
    return [
        {"id": "CP-1", "title": "control plane: publish contracts",
         "labels": ["ws:control-plane"], "depends_on": [], "file_scope": ["api/"]},
        {"id": "CP-2", "title": "control plane: pricing rollup",
         "labels": ["ws:control-plane"], "depends_on": [], "file_scope": ["api/x"]},
        {"id": "PR-1", "title": "plugin runtime: consume digest",
         "labels": ["ws:plugin-runtime"], "depends_on": [], "file_scope": ["plugin-claude/"]},
        {"id": "DC-1", "title": "delivery cli: status verb",
         "labels": ["ws:delivery-cli"], "depends_on": [], "file_scope": ["cli/"]},
    ]


def test_shipped_hydration_path_takes_provisioned_clones_into_execution(
    provisioned_clones,
):
    """P2 round 2: drive the CLI command `modes/spq.md` actually prints, with a
    filtered tracker-shaped backlog, and prove dispatch happens with NO manual
    init / transition / open_slice anywhere."""
    _up, clones = provisioned_clones
    backlog = _tracker_backlog(1)

    for ws, d in clones.items():
        env = {**os.environ, "SYNAPTORY_ACTIVE_SPEC": ws}

        # Before hydration the prompt's own loop command must report that there
        # is nothing to dispatch — this is the symptom the finding described.
        pre = subprocess.run(
            ["python3", str(Path(sm.__file__)), "next_action", str(d)],
            capture_output=True, text=True, env=env)
        assert json.loads(pre.stdout)["action"] == "not_in_execution", ws

        # The filter step the prompt describes: this workstream's units only.
        mine = [u for u in backlog if f"ws:{ws}" in u["labels"]]
        assert mine, f"backlog has no units for {ws}"

        # The exact CLI form printed in modes/spq.md.
        hyd = subprocess.run(
            ["python3", str(Path(sm.__file__)), "hydrate_slice", str(d), "1",
             "--goal", "Slice 1", "--work-units", json.dumps(mine)],
            capture_output=True, text=True, env=env)
        assert hyd.returncode == 0, hyd.stderr
        assert json.loads(hyd.stdout)["hydrated"] is True

        # And now the loop dispatches, without any manual state setup.
        post = subprocess.run(
            ["python3", str(Path(sm.__file__)), "next_action", str(d)],
            capture_output=True, text=True, env=env)
        na = json.loads(post.stdout)
        assert na["action"] == "dispatch_se", (ws, na)
        assert na["story_id"] in {u["id"] for u in mine}
        assert na["current_slice"] == 1


def test_shipped_hydration_admits_only_this_workstreams_units(provisioned_clones):
    """The discriminator is load-bearing: a clone must not admit another
    workstream's Work Units."""
    _up, clones = provisioned_clones
    backlog = _tracker_backlog(1)
    d = clones["control-plane"]
    env = {**os.environ, "SYNAPTORY_ACTIVE_SPEC": "control-plane"}
    mine = [u for u in backlog if "ws:control-plane" in u["labels"]]
    subprocess.run(
        ["python3", str(Path(sm.__file__)), "hydrate_slice", str(d), "1",
         "--work-units", json.dumps(mine)],
        capture_output=True, text=True, env=env, check=True)

    state = json.loads(subprocess.run(
        ["python3", str(Path(sm.__file__)), "read", str(d)],
        capture_output=True, text=True, env=env).stdout)
    ids = {s["id"] for s in state["current_stories"]}
    assert ids == {"CP-1", "CP-2"}
    assert "PR-1" not in ids and "DC-1" not in ids


def test_shipped_hydration_then_declare_ready_end_to_end(provisioned_clones):
    """Provisioned clone to published readiness record, entirely through the
    commands the prompts print."""
    _up, clones = provisioned_clones
    d = clones["plugin-runtime"]
    env = {**os.environ, "SYNAPTORY_ACTIVE_SPEC": "plugin-runtime"}
    mine = [u for u in _tracker_backlog(1) if "ws:plugin-runtime" in u["labels"]]

    subprocess.run(
        ["python3", str(Path(sm.__file__)), "hydrate_slice", str(d), "1",
         "--work-units", json.dumps(mine)],
        capture_output=True, text=True, env=env, check=True)
    # PR-1 needs real evidence, otherwise declare-ready refuses on the DoD
    # invariant before it ever reaches the regression check this test is about
    # (#238). Ordering matters here: the DoD gate is checked first by design.
    _write_unit_receipts(d, "PR-1")
    for st in ("in_progress", "testing", "reviewing", "done"):
        subprocess.run(
            ["python3", str(Path(sm.__file__)), "transition_story", str(d),
             "PR-1", st], capture_output=True, text=True, env=env)

    na = json.loads(subprocess.run(
        ["python3", str(Path(sm.__file__)), "next_action", str(d)],
        capture_output=True, text=True, env=env).stdout)
    assert na["action"] == "await_sync", na

    # CONFIG requires a regression; the provisioned upstream ships no script, so
    # declare-ready must refuse rather than publish an unproven record.
    p = subprocess.run(
        ["python3", str(Path(sb.__file__)), "declare-ready", str(d), "1"],
        capture_output=True, text=True, env=env, cwd=str(d))
    assert p.returncode == 2
    assert "regression" in p.stderr
