"""Layer 1 — deleting the seal is not cheaper than honouring it (#507).

#494 made a dropped acceptance case cost what declaring a gap costs. #505 then
pinned the same asymmetry one level up, against the seal itself, as
`test_deleting_the_seal_still_downgrades_the_gate`: `rm` the manifest and the
unit fell back to the pre-#406 board rules, where a green runner exit code
clears `tests_pass` again. One `rm` beat the whole test-first mechanism, and it
was cheaper than the dishonesty #494 had just made expensive.

What the reproduction found first, and it is smaller and worse than the pinned
note guessed
-------------------------------------------------------------------------------
The seal is written to **two** places by `_seal_manifest`:

    .synaptory/.orchestrator/spq/cycles/<id>/manifest.json   (local, 0444)
    .synaptory/cycles/<id>/manifest.json                     (committed transport)

`spq_state_machine.read_manifest` already read both. The DoD gate read only the
first. So the "deletion" the residual described never deleted the seal: an
identical, hash-verifying copy sat on disk beside it and the gate downgraded
anyway. The fix is therefore **recovery before refusal**, and both halves are
asserted below:

  1. **Recovery** (section 1). `spq_manifest.read_sealed` consults every copy
     -- local, committed, `HEAD` -- so removing one is not cheaper, it is
     ineffective. No block is needed: the authored set still binds.
  2. **Detection** (section 2). When no copy reads and something else still
     says one was sealed, the authored set is `missing_manifest`: unknowable,
     not absent, which #403 renders as a criteria gap and #494 makes block.
  3. **Narrowness** (section 3). Absence with NO such record is unchanged --
     Scrum, Kanban, a pre-#406 Cycle and a pre-COMMIT SPQ project all keep the
     behaviour they had. This is why the fix is not "block when the manifest
     is absent", which would have bricked all four.
  4. **Forgeability** (section 4), including the residual that remains,
     asserted rather than described so it cannot rot quietly: `manifest_hash`
     is an UNKEYED checksum, so a counterfeit replacement document is not
     refused by any line here.

Section 4 also carries a defect found while reproducing this one, live on
`origin/dev` and inside the refusal #507 describes as working: the tamper
refusal was keyed on `story_authored_case_ids`, which reads the BOARD, so
emptying `acceptance_cases` bought the pre-#406 rules back on a Cycle whose
seal had just been tampered with.

WHAT #640 CHANGED UNDER THIS FILE, and it is why 27 arms failed at once
-------------------------------------------------------------------------------
`open_cycle` seals a `cycle_records` DECLARATION: `declaration_hash` instead of
`manifest_hash`, `admitted_units` instead of `work_units`, and a hash over that
module's own canonical bytes. Every reader here goes through `spq_manifest`,
whose retired spellings do not fail loudly against such a document -- an absent
hash reads as "does not verify" and an absent unit list as "admits nothing".

So this file's whole subject was inverted rather than moved: the recovery
ladder was intact and reported `untrusted_manifest` for every Cycle, which is
the block half of the mechanism firing on the honest case. The three arms
`_downgrade` drives were worse, because `open_cycle` and `revise_manifest` had
also stopped stamping the revision in force, so `expect_hash` was empty and
every rung stopped comparing. Both were product defects, fixed with the
migration; the arms below are unchanged in what they assert.

The lane is gone (`SPD-194`), so the board is one file per Cycle rather than
one per lane. That is a path change here and nothing more: no arm in this file
had a lane as its subject.
"""

from __future__ import annotations


import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import cycle_records as cr
import spq_manifest as mf
import spq_paths as sp
import spq_state_machine as sm
from story_pipeline import (
    create_story,
    dod_gate_block_reason,
    evaluate_story_dod,
    manifest_authored_cases,
    resolve_authored_cases,
    under_authored_regime,
)

from _spq_fixture import CYCLE_KWARGS, unit as _fx_unit

pytestmark = pytest.mark.unit


CASES = [
    _fx_unit("AC-1", statement="an unauthenticated GET returns 401"),
    _fx_unit("AC-2", statement="a member GET returns only their own rows"),
]


# ── fixtures ──────────────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=False)


def _project(root: Path, mode: str = "spq") -> Path:
    project = root
    project.mkdir(parents=True, exist_ok=True)
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "t@e.co")
    _git(project, "config", "user.name", "t")
    (project / "f.txt").write_text("x", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")
    if mode == "spq":
        (project / ".synaptory.yaml").write_text(
            "build_mode: spq\n", encoding="utf-8"
        )
    else:
        (project / ".synaptory.yaml").write_text(
            "build_mode: %s\n" % mode, encoding="utf-8"
        )
    return project


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.delenv(sp.ENV_CYCLE_ID, raising=False)
    monkeypatch.delenv("SYNAPTORY_ACTIVE_SPEC", raising=False)
    return _project(tmp_path / "proj")


def _unit(uid: str = "WU-1", **over) -> dict:
    unit = {"id": uid, "title": "work", "labels": [], "kind": "story",
             "acceptance_criteria": ["it works"],
             "path_scope": ["api/" + uid.lower() + "/"]}
    unit.update(over)
    return unit


def _story(sid: str = "WU-1", state: str = "reviewing", **over) -> dict:
    story = create_story(sid, "work", acceptance_cases=CASES)
    story["state"] = state
    story["pipeline_log"] = [
        {"state": "queued", "entered_at": "2026-01-01T00:00:00Z",
         "exited_at": "2026-01-01T01:00:00Z"},
        {"state": "in_progress", "entered_at": "2026-01-01T01:00:00Z",
         "exited_at": "2026-01-01T02:00:00Z"},
        {"state": "testing", "entered_at": "2026-01-01T02:00:00Z",
         "exited_at": "2026-01-01T03:00:00Z"},
        {"state": "reviewing", "entered_at": "2026-01-01T03:00:00Z",
         "exited_at": None},
    ]
    story.update(over)
    return story


def _receipt(results, *, agent: str = "quality-engineer", exit_code: int = 0) -> dict:
    body: dict = {
        "task": "verify",
        "agent": agent,
        "backend": "claude",
        "model": "sonnet",
        "artifacts": ["tests/test_api.py"],
        "completed_at": "2026-01-01T04:00:00Z",
        "verification_commands": [
            {"command": "pytest tests/ -q", "exit_code": exit_code,
             "summary": "12 passed"},
        ],
    }
    if results is not None:
        body["metrics"] = {"authored_test_cases": {
            "source": "cycle-manifest", "total": len(results), "results": results,
        }}
    return body


def _write_receipt(project: Path, sid: str, abbrev: str, body: dict) -> None:
    d = project / ".synaptory" / ".orchestrator" / "receipts"
    d.mkdir(parents=True, exist_ok=True)
    (d / ("%s-%s.json" % (sid, abbrev))).write_text(
        json.dumps(body), encoding="utf-8"
    )


def _green_producer(project: Path, sid: str = "WU-1") -> None:
    _write_receipt(project, sid, "se", {
        "agent": "software-engineer", "completed_at": "2026-01-01T02:00:00Z",
        "verification_commands": [
            {"command": "npm run build", "exit_code": 0},
            {"command": "pytest tests/ -q", "exit_code": 0},
        ],
    })
    _write_receipt(project, sid, "cr", {
        "agent": "code-reviewer", "completed_at": "2026-01-01T03:30:00Z",
        "verification_commands": [{"command": "review", "exit_code": 0}],
    })


def _board(project: Path, story: dict) -> None:
    state = sm.read_state(str(project))
    state["current_stories"] = [story]
    sm._write_state(str(project), state)


def _sealed(repo: Path, *, cases=CASES, story=None, unit=None) -> str:
    """A Cycle at CYCLE sealing `cases` for WU-1. Returns its id."""
    sm.initialize(str(repo))
    sm.approve_baseline(str(repo), approved_by="t", baseline_ref="baseline-1",
        calibration={"sample_units": 1, "measured_hours": 1})
    sm.open_cycle(str(repo), goal="c", admitted_units=[unit if unit is not None else _unit(acceptance_cases=cases)], **CYCLE_KWARGS)
    _board(repo, story if story is not None else _story())
    _green_producer(repo)
    return sm.identity(str(repo)).cycle_id


def _gate(repo: Path, sid: str = "WU-1", tier: str = "early"):
    dod = evaluate_story_dod(str(repo), sid, tier)
    return dod, dod_gate_block_reason(dod)


def _stanza(dod: dict) -> dict:
    return dod["checks"]["tests_pass"].get("test_first") or {}


def _drop_local(repo: Path, cycle_id: str) -> None:
    Path(sp.manifest_path(str(repo), cycle_id)).unlink()


def _drop_committed(repo: Path, cycle_id: str) -> None:
    Path(sp.committed_manifest_path(str(repo), cycle_id)).unlink()


def _drop_every_copy(repo: Path, cycle_id: str) -> None:
    _drop_local(repo, cycle_id)
    _drop_committed(repo, cycle_id)


def _counterfeit(repo: Path, cycle_id: str) -> dict:
    """A whole replacement declaration, internally consistent, AC-2 dropped.

    Written to both working-tree copies. The hash comes from `cycle_records`,
    which is the sealer that produced the document being replaced: recomputing
    it with the retired `spq_manifest.compute_hash` would produce a document
    that fails its own hash, so the arm would pass for the wrong reason -- as
    a tamper refusal rather than as the counterfeit the residual is about.
    """
    path = Path(sp.manifest_path(str(repo), cycle_id))
    path.chmod(0o644)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    for unit in manifest[cr.UNITS_FIELD]:
        if unit.get("id") == "WU-1":
            unit["acceptance_cases"] = [CASES[0]]  # AC-2 quietly dropped
    manifest.pop(cr.HASH_FIELD, None)
    manifest[cr.HASH_FIELD] = cr.compute_hash(manifest)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    Path(sp.committed_manifest_path(str(repo), cycle_id)).write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return manifest


def _dropped_case_arm(repo: Path, *, board_cases=None):
    """The #494 shape: a sealed pair, one case never reported."""
    cycle_id = _sealed(
        repo, story=_story(acceptance_cases=board_cases if board_cases else [])
    )
    _write_receipt(repo, "WU-1", "qe", _receipt({"AC-1": {"status": "passed"}}))
    return cycle_id


# ══ 1. recovery: one copy of the seal is not the seal ════════════════════════


def test_the_seal_is_written_to_two_places(repo: Path):
    """The premise every recovery assertion below rests on, checked directly
    rather than assumed. If `open_cycle` ever stops writing the committed
    transport, the recovery arms would still pass through the git rung and
    nobody would notice the copy had gone."""
    cycle_id = _sealed(repo)
    assert os.path.exists(sp.manifest_path(str(repo), cycle_id))
    assert os.path.exists(sp.committed_manifest_path(str(repo), cycle_id))


def test_removing_the_local_copy_resolves_from_the_committed_one(repo: Path):
    """THE ticket, in its cheapest form. The residual #505 pinned was not a
    deleted seal, it was a gate reading one of two copies."""
    cycle_id = _dropped_case_arm(repo)
    _drop_local(repo, cycle_id)
    dod, block = _gate(repo)
    tf = _stanza(dod)
    assert tf["authored_source"] == "manifest", tf
    assert tf["authored_seal_origin"] == "committed", tf
    assert block and "AC-2" in block, block


def test_removing_the_committed_copy_resolves_from_the_local_one(repo: Path):
    """The mirror, so "reads both" is not satisfied by having swapped which
    single copy it reads."""
    cycle_id = _dropped_case_arm(repo)
    _drop_committed(repo, cycle_id)
    dod, block = _gate(repo)
    tf = _stanza(dod)
    assert tf["authored_source"] == "manifest", tf
    assert tf["authored_seal_origin"] == "local", tf
    assert block and "AC-2" in block, block


def test_a_seal_in_git_survives_the_whole_working_tree(repo: Path):
    """The rung outside the working tree, and the only one an agent cannot
    reach by writing files. With `.synaptory/cycles/` tracked -- the narrow
    un-ignore the transport refusal documents -- every on-disk copy can go and
    the authored set still binds, recovered from HEAD."""
    cycle_id = _dropped_case_arm(repo)
    _git(repo, "add", "-A", "-f")
    _git(repo, "commit", "-qm", "seal")
    _drop_every_copy(repo, cycle_id)
    shutil.rmtree(sp.committed_cycle_dir(str(repo), cycle_id))
    Path(sp.index_path(str(repo))).unlink()

    dod, block = _gate(repo)
    tf = _stanza(dod)
    assert tf["authored_source"] == "manifest", tf
    assert tf["authored_seal_origin"] == "git", tf
    assert block and "AC-2" in block, block


def test_the_state_machine_and_the_gate_read_the_same_copies(repo: Path):
    """One definition of "read the seal", which is the whole reason #507 was
    possible: `read_manifest` read local-then-committed and the gate read
    local only, so the two disagreed about whether a seal existed at all.

    The replacement inverted it -- `read_manifest` read the local copy only
    while the gate reads all three rungs -- so losing the gitignored local copy
    left the gate grading against a seal that `dep_context`, `advance_kernel`
    and `publish_event` all reported as absent. Same defect, other direction,
    and this arm is what caught it.
    """
    cycle_id = _sealed(repo)
    _drop_local(repo, cycle_id)
    assert sm.read_manifest(str(repo), cycle_id).get(cr.HASH_FIELD)
    assert mf.read_sealed(str(repo), cycle_id)[1] == mf.SEAL_COMMITTED
    cases, origin, witness = resolve_authored_cases(str(repo), "WU-1")
    assert [c["id"] for c in cases] == ["AC-1", "AC-2"]
    assert origin == mf.SEAL_COMMITTED
    assert witness == ""


# ══ 2. detection: a missing seal is unknowable, not absent ═══════════════════


def test_removing_every_copy_blocks_the_done_edge(repo: Path):
    """With no copy readable the authored set cannot be established, so the
    gate reports a criteria gap rather than the pre-#406 rules -- and #403's
    clause stops the edge, which is what makes deletion cost at least what
    honesty costs."""
    cycle_id = _dropped_case_arm(repo)
    _drop_every_copy(repo, cycle_id)
    dod, block = _gate(repo)
    assert _stanza(dod)["authored_source"] == "missing_manifest", _stanza(dod)
    results = {i["check_id"]: i["result"] for i in dod["dod_check_results"]}
    assert results["tests_pass"] == "criteria_gap_declared"
    assert block, "a deleted seal did not stop the done edge"


def test_a_green_runner_cannot_clear_a_missing_seal(repo: Path):
    """The exact mechanism the residual described: with the seal gone the unit
    fell back to rules where the runner's exit code decided `tests_pass`. Every
    authored case reported passing AND a green suite still does not clear it,
    because what is missing is the document that says which cases there were."""
    cycle_id = _sealed(repo, story=_story(acceptance_cases=[]))
    _write_receipt(repo, "WU-1", "qe", _receipt(
        {"AC-1": {"status": "passed"}, "AC-2": {"status": "passed"}}
    ))
    _drop_every_copy(repo, cycle_id)
    dod, block = _gate(repo)
    assert dod["checks"]["tests_pass"]["passed"] is None
    assert block, block


def test_the_block_names_the_deletion_and_a_recovery(repo: Path):
    """A block an operator cannot act on is a stall. It has to say that the
    document is missing rather than unsatisfied, and name the two honest ways
    out: restore the seal, or supersede it."""
    cycle_id = _dropped_case_arm(repo)
    _drop_every_copy(repo, cycle_id)
    _, block = _gate(repo)
    assert "no copy of this Cycle's sealed manifest can be read" in block, block
    assert "MISSING is not a Cycle that never had one" in block, block
    assert ".synaptory/cycles/" in block, block
    assert "revise_manifest" in block, block


def test_the_block_carries_the_witness_that_refused_it(repo: Path):
    """Which record said a seal existed is the reviewable part of this
    refusal. Without it the message asserts a deletion and shows nothing."""
    cycle_id = _dropped_case_arm(repo)
    _drop_every_copy(repo, cycle_id)
    dod, block = _gate(repo)
    witness = _stanza(dod)["authored_seal_witness"]
    assert witness, _stanza(dod)
    assert witness in block, block


@pytest.mark.parametrize("scrub", ["committed_dir", "index", "board_stamps"])
def test_each_further_deletion_still_leaves_a_witness(repo: Path, scrub: str):
    """The ladder, asserted rung by rung. Every step removes the record the
    previous step relied on, and the refusal survives each one -- which is the
    difference between "the fix reads one more file" and "the fix asks whether
    anything still says this unit was sealed"."""
    cycle_id = _dropped_case_arm(repo)
    _drop_every_copy(repo, cycle_id)
    if scrub in ("index", "board_stamps"):
        shutil.rmtree(sp.committed_cycle_dir(str(repo), cycle_id))
        Path(sp.index_path(str(repo))).unlink()
    else:
        shutil.rmtree(sp.committed_cycle_dir(str(repo), cycle_id))
    if scrub == "board_stamps":
        path = Path(sp.execution_state_path(str(repo), cycle_id))
        doc = json.loads(path.read_text(encoding="utf-8"))
        for key in ("manifest_hash", "manifest_revision", "test_first"):
            doc.pop(key, None)
        path.write_text(json.dumps(doc), encoding="utf-8")

    dod, block = _gate(repo)
    assert _stanza(dod)["authored_source"] == "missing_manifest", _stanza(dod)
    assert block, "rung %r left no witness" % scrub


def test_committing_the_removal_does_not_hide_it(repo: Path):
    """Once the transport is tracked, taking the seal out of `HEAD` needs a
    commit -- and the commit is the record. This is the one rung the subject of
    the gate cannot reach by writing files, and the honest reason the fix is a
    price increase rather than a guarantee (see `resolve_authored_cases`)."""
    cycle_id = _dropped_case_arm(repo)
    _git(repo, "add", "-A", "-f")
    _git(repo, "commit", "-qm", "seal")
    _drop_every_copy(repo, cycle_id)
    shutil.rmtree(sp.committed_cycle_dir(str(repo), cycle_id))
    Path(sp.index_path(str(repo))).unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "drop the seal")

    dod, block = _gate(repo)
    assert _stanza(dod)["authored_source"] == "missing_manifest", _stanza(dod)
    assert "git history" in _stanza(dod)["authored_seal_witness"]
    assert block, block


def test_deleting_the_seal_is_not_cheaper_than_declaring_a_gap(tmp_path: Path,
                                                               monkeypatch):
    """#507's assertion, made the way #494 made its own: directly, by running
    the two arms and comparing, rather than inferred from the mechanism.

    Same sealed Cycle, same green producer. One prover honestly reports that it
    has no per-case outcomes to give (`criteria_gap_declared`, which has always
    blocked). The other deletes the document that says what the cases were and
    reports a green suite. Before this the first was blocked and the second
    completed, so the cheapest route past the gate was `rm`.
    """
    monkeypatch.delenv("SYNAPTORY_ACTIVE_SPEC", raising=False)

    honest = _project(tmp_path / "honest")
    _sealed(honest)
    _write_receipt(honest, "WU-1", "qe", _receipt(None))
    honest_dod, honest_block = _gate(honest)

    deleter = _project(tmp_path / "deleter")
    cycle_id = _sealed(deleter, story=_story(acceptance_cases=[]))
    _write_receipt(deleter, "WU-1", "qe", _receipt(
        {"AC-1": {"status": "passed"}, "AC-2": {"status": "passed"}}
    ))
    _drop_every_copy(deleter, cycle_id)
    deleter_dod, deleter_block = _gate(deleter)

    # The premise: two different paths, not one run twice.
    assert _stanza(honest_dod)["authored_source"] == "manifest"
    assert _stanza(deleter_dod)["authored_source"] == "missing_manifest"

    # The claim: the same cost.
    assert honest_block, "the honest declaration stopped costing anything"
    assert deleter_block, (
        "deleting the seal is still cheaper than declaring the gap: the "
        "declaration blocks and the deletion does not"
    )

    # And the diagnosis stays distinguishable, so equalising the cost did not
    # equalise the explanation. An operator can still tell which happened.
    assert "carries no `metrics.authored_test_cases` object" in honest_block
    assert "sealed manifest can be read" in deleter_block


def test_repointing_the_cycle_id_does_not_downgrade_the_gate(repo: Path):
    """The one-string version of the same forgery. The board pointer selects
    which Cycle the gate grades against, so pointing it at a Cycle whose seal
    this checkout has never held used to buy the pre-#406 rules for free."""
    _dropped_case_arm(repo)
    pointer = repo / ".synaptory" / ".orchestrator" / "pipeline-state.json"
    doc = json.loads(pointer.read_text(encoding="utf-8"))
    doc["spq"]["cycle_id"] = "9-deadbeef"
    pointer.write_text(json.dumps(doc), encoding="utf-8")

    dod, block = _gate(repo)
    assert _stanza(dod)["authored_source"] == "missing_manifest", _stanza(dod)
    assert block, "a repointed `_cycle_id` bought a way past the gate"


def test_repointing_and_moving_the_board_does_not_either(repo: Path):
    """The completion of the previous forgery: carry the board under the
    forged identity too, so the story is still found and the contradiction
    between the pointer and the index is the only thing left. That is enough,
    and it is why the witness set includes a record with a different writer."""
    cycle_id = _dropped_case_arm(repo)
    src = Path(sp.execution_state_path(str(repo), cycle_id))
    dst = Path(sp.execution_state_path(str(repo), "9-deadbeef"))
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(str(src), str(dst))
    pointer = repo / ".synaptory" / ".orchestrator" / "pipeline-state.json"
    doc = json.loads(pointer.read_text(encoding="utf-8"))
    doc["spq"]["cycle_id"] = "9-deadbeef"
    pointer.write_text(json.dumps(doc), encoding="utf-8")

    dod, block = _gate(repo)
    assert _stanza(dod)["authored_source"] == "missing_manifest", _stanza(dod)
    assert block, block


def test_the_missing_route_is_a_gap_and_not_the_494_clause(repo: Path):
    """Two blocks with two remedies, kept apart. A deleted seal needs a
    document restored; a dropped case needs the verifying stage re-dispatched.
    Handing an operator the second instruction for the first problem is how a
    block becomes a stall, so `missing_manifest` deliberately does not reach
    #494's clause."""
    cycle_id = _dropped_case_arm(repo)
    _drop_every_copy(repo, cycle_id)
    _, block = _gate(repo)
    assert "criteria gap" in block, block
    assert "Re-dispatch the verifying stage" not in block, block


# ══ 3. narrowness: absence with no witness is unchanged ══════════════════════


def test_a_scrum_story_is_untouched(tmp_path: Path):
    """The blast radius, stated as a test. The pilot is SPQ-only (ADR-032
    section 6) and a Scrum board has no manifest, no Cycle and no index, so
    nothing here may reach it."""
    import scrum_state_machine as scrum

    project = _project(tmp_path / "scrum", mode="scrum")
    state = scrum.initialize(str(project), "p")
    state["current_stories"] = [_story("US-1")]
    scrum._write_state(str(project), state)
    _green_producer(project, "US-1")
    _write_receipt(project, "US-1", "qe", _receipt({
        "AC-1": {"status": "passed"}, "AC-2": {"status": "failed"},
    }))
    dod = evaluate_story_dod(str(project), "US-1", "early")
    assert _stanza(dod)["authored_source"] == "board", _stanza(dod)
    assert dod["checks"]["tests_pass"]["passed"] is False
    assert dod_gate_block_reason(dod) is None


def test_a_kanban_story_is_untouched(tmp_path: Path):
    """The other unsealed lifecycle, named separately in #507's acceptance
    criteria and asserted separately, because "Scrum passes" is not evidence
    about a different state machine."""
    import kanban_state_machine as kanban

    project = _project(tmp_path / "kanban", mode="kanban")
    state = kanban.initialize(str(project))
    state["current_stories"] = [_story("KB-1")]
    kanban._write_state(str(project), state)
    _green_producer(project, "KB-1")
    _write_receipt(project, "KB-1", "qe", _receipt({
        "AC-1": {"status": "passed"}, "AC-2": {"status": "failed"},
    }))
    dod = evaluate_story_dod(str(project), "KB-1", "early")
    assert _stanza(dod)["authored_source"] == "board", _stanza(dod)
    assert dod["checks"]["tests_pass"]["passed"] is False
    assert dod_gate_block_reason(dod) is None


def test_a_pre_406_cycle_is_untouched(repo: Path):
    """A Cycle sealed before the authored-case field existed carries no
    `acceptance_cases` KEY, so its seal reads fine and simply holds no position
    for this unit. That is `board`, not `missing_manifest`: the document is
    right there and it says nothing, which is a different fact from a document
    that is gone."""
    _sealed(repo, unit=_unit(), story=_story(acceptance_cases=[]))
    _write_receipt(repo, "WU-1", "qe", _receipt({
        "AC-1": {"status": "passed"}, "AC-2": {"status": "failed"},
    }))
    dod, block = _gate(repo)
    tf = _stanza(dod)
    assert tf["authored_source"] == "board", tf
    assert tf["authored_seal_origin"] == "local", tf
    assert dod["checks"]["tests_pass"]["passed"] is False
    assert block is None, block


def test_an_spq_project_with_no_cycle_has_no_witness(repo: Path):
    """Pre-COMMIT: an SPQ project that has never opened a Cycle has no seal, no
    index and no cycle directory. `_seal_witness` must find nothing, or every
    project would acquire a block on its way to its first Cycle."""
    sm.initialize(str(repo), workstream_id="spine")
    cases, origin, witness = resolve_authored_cases(str(repo), "WU-1")
    assert cases is None
    assert origin == ""
    assert witness == ""


def test_a_non_spq_project_never_reaches_the_seal(tmp_path: Path):
    """The #406 assertion, re-checked through the new resolver: no Cycle
    identity, no manifest read, no git call."""
    project = _project(tmp_path / "plain", mode="scrum")
    assert manifest_authored_cases(str(project), "US-1") is None
    assert resolve_authored_cases(str(project), "US-1") == (None, "", "")


def test_a_second_cycles_seal_does_not_answer_for_this_one(repo: Path):
    """The witness scan reads another Cycle's seal as evidence that this
    project seals manifests. It must not read it as THIS unit's authored set:
    that would grade a unit against a document that never admitted it."""
    cycle_id = _dropped_case_arm(repo)
    other = "2-abcdef01"
    manifest, _origin = mf.read_sealed(str(repo), cycle_id)
    os.makedirs(sp.cycle_root(str(repo), other), exist_ok=True)
    forged = dict(manifest)
    forged["cycle_id"] = other
    forged["cycle_seq"] = 2
    forged[cr.UNITS_FIELD] = [_fx_unit("WU-1", acceptance_cases=[])]
    Path(sp.manifest_path(str(repo), other)).write_text(
        json.dumps(cr.seal(forged)), encoding="utf-8"
    )
    _drop_every_copy(repo, cycle_id)

    dod, block = _gate(repo)
    tf = _stanza(dod)
    assert tf["authored_source"] == "missing_manifest", tf
    assert "has a readable sealed manifest" in tf["authored_seal_witness"]
    assert block, block


# ══ 4. forgeability: what refuses what, and what still does not ══════════════


def test_an_emptied_board_does_not_buy_the_pre_406_rules(repo: Path):
    """A DEFECT FOUND WHILE REPRODUCING THIS ONE, live on `origin/dev`, inside
    the refusal #507 describes as already working.

    Untrusted input: the board (`execution-state.json`), which the agent under
    the gate can write. Forgery: tamper with the sealed manifest so its hash
    fails AND empty the board's own `acceptance_cases`. `authored_cases_verdict`
    answered correctly -- "this Cycle's sealed manifest does not match its own
    hash" -- and `evaluate_tests_pass` then DISCARDED that answer, because it
    keyed the strict regime on `story_authored_case_ids`, which reads the board.
    The runner's green exit code carried the check, `_evaluate_fallback_proof`
    keyed the same way and promoted an SE receipt's `pytest` for good measure,
    and the unit completed.

    So the tamper refusal was conditional on the cooperation of the party it
    refuses, and #505's tamper test did not see it because its fixture leaves
    the board's cases in place. Refused now by `under_authored_regime`, which
    treats an unreadable seal as governing whatever the board says. That
    refuses the FORGERY and not a malformed spelling of it: there is no board
    edit that restores the pre-#406 path once the seal is unreadable.
    """
    cycle_id = _sealed(repo, story=_story(acceptance_cases=[]))
    _write_receipt(repo, "WU-1", "qe", _receipt(
        {"AC-1": {"status": "passed"}, "AC-2": {"status": "passed"}}
    ))
    path = Path(sp.manifest_path(str(repo), cycle_id))
    path.chmod(0o644)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest[cr.UNITS_FIELD][0]["acceptance_cases"] = []
    path.write_text(json.dumps(manifest), encoding="utf-8")  # hash NOT recomputed
    _drop_committed(repo, cycle_id)

    dod, block = _gate(repo)
    tf = _stanza(dod)
    assert tf["authored_source"] == "untrusted_manifest", tf
    assert dod["checks"]["tests_pass"]["passed"] is None
    assert block, "an emptied board bought the pre-#406 rules back"


def test_the_regime_predicate_reads_the_seal_not_the_board(repo: Path):
    """The predicate above, asserted on its own so the fix cannot be undone by
    a caller that goes back to asking the board directly."""
    assert under_authored_regime({"acceptance_cases": CASES}) is True
    assert under_authored_regime({"acceptance_cases": []}) is False
    assert under_authored_regime(
        {"acceptance_cases": [], "_authored_source": "untrusted_manifest"}
    ) is True
    assert under_authored_regime(
        {"acceptance_cases": [], "_authored_source": "missing_manifest"}
    ) is True
    assert under_authored_regime(None) is False


def test_the_board_cannot_stamp_its_own_seal_provenance(repo: Path):
    """`_authored_source` and `_authored_seal_origin` are DERIVED on every
    read, never adopted. Forgery: write `_authored_source: "board"` and
    `_authored_seal_origin: "local"` onto the unit record so a Cycle whose seal
    is gone reads as one that never had one. Refused by `_authoritative_story`,
    which assigns both markers in every branch after consulting the seal, so a
    board-supplied value is overwritten before the gate sees it."""
    cycle_id = _sealed(repo, story=_story(
        acceptance_cases=[], _authored_source="board",
        _authored_seal_origin="local",
    ))
    _write_receipt(repo, "WU-1", "qe", _receipt({"AC-1": {"status": "passed"}}))
    _drop_every_copy(repo, cycle_id)
    dod, block = _gate(repo)
    assert _stanza(dod)["authored_source"] == "missing_manifest", _stanza(dod)
    assert block, "a board-supplied `_authored_source` bought a way past the gate"


def test_a_local_tamper_is_not_papered_over_by_the_committed_copy(repo: Path):
    """Reading more copies must not make tampering cheaper. The local copy is
    consulted first and a hash failure there is definitive, so editing it is
    still a refusal rather than a silent fall-through to the pristine sibling
    -- which would have turned "the gate reads every copy" into "the gate reads
    until one agrees with it"."""
    cycle_id = _sealed(repo)
    path = Path(sp.manifest_path(str(repo), cycle_id))
    path.chmod(0o644)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest[cr.UNITS_FIELD][0]["acceptance_cases"] = []
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert os.path.exists(sp.committed_manifest_path(str(repo), cycle_id))

    _write_receipt(repo, "WU-1", "qe", _receipt(
        {"AC-1": {"status": "passed"}, "AC-2": {"status": "passed"}}
    ))
    dod, block = _gate(repo)
    assert _stanza(dod)["authored_source"] == "untrusted_manifest", _stanza(dod)
    assert block, block


def test_a_counterfeit_seal_is_not_refused_by_this_line(repo: Path):
    """THE RESIDUAL, asserted rather than described so it cannot rot quietly.

    `manifest_hash` is an UNKEYED sha256 over the document's own body. It
    proves internal consistency, not authorship. So an agent that writes a
    whole replacement manifest -- units, cases, recomputed hash -- produces a
    document every line in this file accepts, and no witness rung can refuse
    it: each of them is a file on the filesystem the agent writes to, and this
    forgery does not need to remove any of them.

    What #507 changed is therefore the PRICE and not the guarantee. One `rm`
    bought a downgrade; a downgrade now costs authoring a counterfeit sealed
    document, which is a deliberate, reviewable artifact rather than a missing
    file. Closing it needs a SIGNED seal (or the control plane's
    `cycle_manifests` row, which is genuinely out of reach and which the gate
    deliberately does not require, so an offline project can still evaluate its
    own DoD). Both are separate tickets.

    #396 RAISED THE PRICE AGAIN and the arms below say by how much. A
    counterfeit alone no longer passes: the board records which revision is in
    force, and a rewritten document hashes to something else. The forger must
    now edit the board too, which is one more write in a second file and is
    still available to the same principal. That last step is the declared
    limit, owned by #507.

    These tests pin the current behaviour and fail the day the seal is signed,
    which is the notification we want.
    """
    cycle_id = _dropped_case_arm(repo)
    manifest = _counterfeit(repo, cycle_id)

    dod, block = _gate(repo)
    assert mf.verify_sealed(manifest) is True
    # #396: the counterfeit is internally consistent and is NOT the revision
    # the board is executing, so on its own it no longer passes. The forgery
    # has to grow a second step.
    assert _stanza(dod)["authored_source"] != "manifest", _stanza(dod)
    assert block, "a counterfeit seal alone should no longer clear the gate"


def test_a_counterfeit_seal_plus_a_board_edit_is_still_not_refused(repo: Path):
    """THE LIMIT OF THE REVISION AUTHORITY, asserted rather than described.

    `sealed_manifest_hash` lives in the board, which is the same
    agent-writable file #507 analysed, so it raises the price of a downgrade
    without closing it: rewrite the manifest, recompute its hash, and write
    that hash into the board, and every rung agrees again.

    THIS CANNOT BE CLOSED OFFLINE, which is why it is a declared limit rather
    than a bug to fix here. Between `revise_manifest` and the commit that
    carries the revision, there is no record of the current revision anywhere
    outside the working tree, so any local witness is a file the same
    principal writes. Closing it needs a signed seal or the control plane's
    append-only `cycle_manifests` row, and the gate deliberately does not
    require the control plane so an offline project can still evaluate its own
    DoD. Owner: #507.

    Pinned so it fails the day an out-of-reach authority lands, rather than
    living in prose that stops being true.
    """
    cycle_id = _dropped_case_arm(repo)
    manifest = _counterfeit(repo, cycle_id)
    # The second step, which is the whole finding: the authority is a field in
    # a file this principal writes.
    state = sm.read_state(str(repo))
    state["sealed_manifest_hash"] = manifest[cr.HASH_FIELD]
    sm._write_state(str(repo), state)

    dod, block = _gate(repo)
    assert _stanza(dod)["authored_source"] == "manifest", _stanza(dod)
    assert block is None, (
        "the counterfeit-plus-board-edit residual documented here has been "
        "closed; update this test, its docstring, and #507: %s" % block
    )


# ══ 5. the reader itself ═════════════════════════════════════════════════════


def test_read_sealed_reports_which_copy_answered(repo: Path):
    """Provenance is not decoration. "The gate read the local copy" and "the
    gate had to recover the seal from HEAD" are different facts about the same
    verdict, and the epic's own rule is that two different facts must not share
    a representation."""
    cycle_id = _sealed(repo)
    assert mf.read_sealed(str(repo), cycle_id)[1] == mf.SEAL_LOCAL
    _drop_local(repo, cycle_id)
    assert mf.read_sealed(str(repo), cycle_id)[1] == mf.SEAL_COMMITTED
    _git(repo, "add", "-A", "-f")
    _git(repo, "commit", "-qm", "seal")
    _drop_committed(repo, cycle_id)
    manifest, origin = mf.read_sealed(str(repo), cycle_id)
    assert origin == mf.SEAL_GIT
    assert manifest.get("cycle_id") == cycle_id


def test_the_origins_are_a_declared_order(repo: Path):
    """The rungs are a closed, ordered vocabulary rather than strings the
    reader happens to return, so a new one cannot be added without deciding
    where it ranks.

    `stale` ranks last and is the one origin that carries NO document (#396):
    a copy answered, with a revision the board is not executing. Named for the
    condition rather than for the git rung, because every rung compares.
    """
    assert mf.SEAL_ORIGINS == (
        mf.SEAL_LOCAL, mf.SEAL_COMMITTED, mf.SEAL_GIT, mf.SEAL_STALE,
    )


def test_the_git_rung_reads_the_right_file_in_a_nested_project(tmp_path: Path,
                                                               monkeypatch):
    """An SPQ project that is not the repository ROOT, which is the shape the
    worktree flow produces routinely.

    `git show HEAD:<path>` resolves against the repository root, so the
    obvious spelling reads the wrong file (git itself answers "path
    'services/api/...' exists, but not '...'"). The reader uses `HEAD:./<path>`,
    which is cwd-relative. Without this test the bug would be invisible: the
    rung would simply never answer, and the gate would report the seal as
    missing rather than recovering it -- a block instead of a wrong pass, which
    is the failure mode that gets explained away as "correct, just strict".
    """
    root = tmp_path / "monorepo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@e.co")
    _git(root, "config", "user.name", "t")
    (root / "top.txt").write_text("x", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")

    project = root / "services" / "api"
    project.mkdir(parents=True)
    (project / "f.txt").write_text("x", encoding="utf-8")
    (project / ".synaptory.yaml").write_text(
        "build_mode: spq\nspq:\n  workstreams:\n"
        '    - id: "spine"\n      shared_owner: true\n',
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "service")

    cycle_id = _dropped_case_arm(project)
    _git(root, "add", "-A", "-f")
    _git(root, "commit", "-qm", "seal")
    _drop_every_copy(project, cycle_id)

    manifest, origin = mf.read_sealed(str(project), cycle_id)
    assert origin == mf.SEAL_GIT, origin
    assert manifest.get("cycle_id") == cycle_id
    dod, block = _gate(project)
    assert _stanza(dod)["authored_seal_origin"] == "git", _stanza(dod)
    assert block and "AC-2" in block, block

    # And the history witness, whose pathspec is resolved the same way.
    _git(root, "rm", "-q", "-r", "--cached", ".synaptory/cycles")
    shutil.rmtree(sp.committed_cycle_dir(str(project), cycle_id))
    Path(sp.index_path(str(project))).unlink()
    _git(root, "commit", "-qm", "drop the seal")
    assert mf.sealed_in_history(str(project), cycle_id) is True


def test_read_sealed_can_refuse_the_subprocess(repo: Path):
    """`allow_git=False` for a caller that must not spawn a process. The rung
    is skipped, not faked: the answer is "no copy", which is what the witness
    scan then has to explain."""
    cycle_id = _sealed(repo)
    _git(repo, "add", "-A", "-f")
    _git(repo, "commit", "-qm", "seal")
    _drop_local(repo, cycle_id)
    _drop_committed(repo, cycle_id)
    assert mf.read_sealed(str(repo), cycle_id, allow_git=False) == (None, "")
    assert mf.read_sealed(str(repo), cycle_id)[1] == mf.SEAL_GIT


def test_the_reader_survives_a_project_with_no_git(tmp_path: Path, monkeypatch):
    """The git rungs must degrade to "no copy" rather than raising. A project
    with no repository at all is a supported SPQ shape (`validate`'s
    `require_baseline=False` exists for it), and a gate that raised there would
    be a worse failure than the one being fixed."""
    project = tmp_path / "nogit"
    project.mkdir()
    assert mf.read_sealed(str(project), "1-aaaaaaaa") == (None, "")
    assert mf.sealed_in_history(str(project), "1-aaaaaaaa") is False


def test_an_invalid_cycle_id_reads_nothing_rather_than_raising(repo: Path):
    """`valid_cycle_id` raises on a malformed identity, and the pointer is
    agent-writable, so the reader has to answer "no copy" for junk instead of
    turning a forged string into an exception inside the gate."""
    assert mf.read_sealed(str(repo), "../../etc/passwd") == (None, "")
    assert mf.sealed_in_history(str(repo), "not-a-cycle") is False


# ══ 4. the git rung must recover THIS revision, not any revision ═════════════
#
# #507 gave the reader a third rung so deleting both working-tree copies could
# not erase the seal. #396 found the rung answers with whatever `HEAD` holds
# and verifies only that the document hashes to itself, which is "is this a
# real document" rather than "is this the document in force". `revise_manifest`
# writes revision N+1 to both copies WITHOUT committing it, so deleting them
# leaves revision N in HEAD looking current, and the unit finishes under the
# superseded, easier authored set. Deleting the current revision was still
# cheaper than honouring it, one rung further down.


def test_head_cannot_resurrect_a_superseded_revision(repo: Path):
    """The finding. v1 in HEAD, v2 on disk only, and both copies deleted.

    The gate must not finish the unit on v1's authored set. It blocks as a
    missing seal, because what HEAD holds is not the seal in force and there
    is no honest way to tell the unit it may proceed.
    """
    cycle_id = _sealed(
        repo,
        unit=_unit(acceptance_cases=[CASES[0]]),
        story=_story(acceptance_cases=[]),
    )
    _git(repo, "add", "-A", "-f")
    _git(repo, "commit", "-qm", "seal revision 1")

    # Revision 2 adds the case a QE receipt reporting only AC-1 cannot satisfy.
    sm.revise_manifest(
        str(repo),
        cycle_id=cycle_id,
        reason="the client added a second acceptance case",
        admitted_units=[_unit(acceptance_cases=CASES)],
    )
    _write_receipt(repo, "WU-1", "qe", _receipt({"AC-1": {"status": "passed"}}))

    # Before the deletion: revision 2 is in force and the done edge is blocked.
    dod, block = _gate(repo)
    tf = _stanza(dod)
    assert tf["authored_seal_origin"] == "local", tf
    assert block and "AC-2" in block, block

    # The deletion. HEAD still holds revision 1, whose only case is AC-1.
    _drop_every_copy(repo, cycle_id)

    dod, block = _gate(repo)
    tf = _stanza(dod)
    assert tf["authored_source"] != "manifest", (
        "the gate consumed a superseded revision recovered from HEAD, so "
        "deleting the current one is still cheaper than honouring it: %r" % tf
    )
    assert tf["authored_case_ids"] != ["AC-1"], tf
    assert block, (
        "the done edge is open on an authored set the board is not executing"
    )


def test_the_git_rung_still_recovers_the_revision_that_is_committed(repo: Path):
    """The positive arm, so the fix is a distinction rather than a refusal.

    Same shape, except revision 2 IS committed before the copies go. HEAD then
    holds the seal in force, the rung answers, and the block is the honest one
    about the missing case rather than about a missing seal.
    """
    cycle_id = _sealed(
        repo,
        unit=_unit(acceptance_cases=[CASES[0]]),
        story=_story(acceptance_cases=[]),
    )
    _git(repo, "add", "-A", "-f")
    _git(repo, "commit", "-qm", "seal revision 1")
    sm.revise_manifest(
        str(repo),
        cycle_id=cycle_id,
        reason="the client added a second acceptance case",
        admitted_units=[_unit(acceptance_cases=CASES)],
    )
    _git(repo, "add", "-A", "-f")
    _git(repo, "commit", "-qm", "seal revision 2")
    _write_receipt(repo, "WU-1", "qe", _receipt({"AC-1": {"status": "passed"}}))

    _drop_every_copy(repo, cycle_id)

    dod, block = _gate(repo)
    tf = _stanza(dod)
    assert tf["authored_source"] == "manifest", tf
    assert tf["authored_seal_origin"] == "git", tf
    assert sorted(tf["authored_case_ids"]) == ["AC-1", "AC-2"], tf
    assert block and "AC-2" in block, block


def test_a_stale_copy_restored_to_disk_does_not_resurrect_it(repo: Path):
    """#396, the shorter route to the same resurrection.

    The first cut of the revision check compared only on the git rung, and the
    on-disk loop returned before reaching it. So the deletion the previous
    test needed was unnecessary: restoring revision 1 into either working-tree
    path was enough, and the gate consumed it with `origin=committed`.

    Both paths are covered, because "reads both copies" is exactly the
    property #507 added and a check on one of them is not a check.
    """
    cycle_id = _sealed(
        repo,
        unit=_unit(acceptance_cases=[CASES[0]]),
        story=_story(acceptance_cases=[]),
    )
    v1 = json.loads(
        Path(sp.manifest_path(str(repo), cycle_id)).read_text(encoding="utf-8")
    )
    _git(repo, "add", "-A", "-f")
    _git(repo, "commit", "-qm", "seal revision 1")
    sm.revise_manifest(
        str(repo),
        cycle_id=cycle_id,
        reason="the client added a second acceptance case",
        admitted_units=[_unit(acceptance_cases=CASES)],
    )
    _write_receipt(repo, "WU-1", "qe", _receipt({"AC-1": {"status": "passed"}}))

    both = (
        sp.manifest_path(str(repo), cycle_id),
        sp.committed_manifest_path(str(repo), cycle_id),
    )
    for target in both:
        for existing in both:
            Path(existing).unlink(missing_ok=True)
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(v1), encoding="utf-8")

        dod, block = _gate(repo)
        tf = _stanza(dod)
        assert tf["authored_source"] != "manifest", (
            "revision 1 restored into %s was consumed, so deleting the current "
            "revision is not even necessary: %r" % (path.name, tf)
        )
        assert block, "the done edge is open on a superseded authored set"


# ══ 6. a connected project asks the control plane which revision is in force ══
#
# #507's controlling constraint is that a marker editable by the same actor is
# not a witness, and `sealed_manifest_hash` is such a marker. No LOCAL witness
# can do better: until a revision is committed, no record of it exists outside
# the working tree. A project configured for a control plane is different,
# because `revise_manifest` already ships an append-only `cycle_manifests`
# observation, and that row is written by a different process on a different
# machine.


class _Authority:
    """A stand-in for `manifest_emitter.read_cycle_authority`.

    Patched rather than mocked at the subprocess boundary because the seam
    under test is the GATE's decision: what it does with a recorded revision,
    with silence, and with an error. The CLI verb that produces those three
    answers has its own tests in `cli/`.
    """

    def __init__(self, recorded="", problem=""):
        self.recorded, self.problem = recorded, problem
        self.calls = []

    def __call__(self, project_dir, cycle_id):
        self.calls.append((project_dir, cycle_id))
        return self.recorded, self.problem


def _revised_v2(repo: Path) -> tuple[str, dict]:
    """v1 committed, v2 revised and left uncommitted. Returns (cycle_id, v1)."""
    cycle_id = _sealed(
        repo,
        unit=_unit(acceptance_cases=[CASES[0]]),
        story=_story(acceptance_cases=[]),
    )
    v1 = json.loads(
        Path(sp.manifest_path(str(repo), cycle_id)).read_text(encoding="utf-8")
    )
    _git(repo, "add", "-A", "-f")
    _git(repo, "commit", "-qm", "seal revision 1")
    sm.revise_manifest(
        str(repo),
        cycle_id=cycle_id,
        reason="the client added a second acceptance case",
        admitted_units=[_unit(acceptance_cases=CASES)],
    )
    _write_receipt(repo, "WU-1", "qe", _receipt({"AC-1": {"status": "passed"}}))
    return cycle_id, v1


def _downgrade(repo: Path, cycle_id: str, v1: dict) -> None:
    """The bypass: point the board at v1 and restore v1 to disk."""
    state = sm.read_state(str(repo))
    state["sealed_manifest_hash"] = v1[cr.HASH_FIELD]
    sm._write_state(str(repo), state)
    for target in (
        sp.manifest_path(str(repo), cycle_id),
        sp.committed_manifest_path(str(repo), cycle_id),
    ):
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        # The seal is written 0444, which stops an accident and not an owner.
        if path.exists():
            path.chmod(0o644)
        path.write_text(json.dumps(v1), encoding="utf-8")


def test_a_connected_project_refuses_a_rewritten_board(repo: Path, monkeypatch):
    """The finding, closed for the case where an authority exists.

    The board says v1 and the artifacts say v1, which is everything a local
    check can see. The control plane's row says v2, so the gate refuses rather
    than grading against the revision the principal chose.
    """
    import manifest_emitter

    cycle_id, v1 = _revised_v2(repo)
    v2_hash = json.loads(
        Path(sp.manifest_path(str(repo), cycle_id)).read_text(encoding="utf-8")
    )[cr.HASH_FIELD]
    _downgrade(repo, cycle_id, v1)

    authority = _Authority(recorded=v2_hash)
    monkeypatch.setattr(manifest_emitter, "read_cycle_authority", authority)

    dod, block = _gate(repo)
    tf = _stanza(dod)
    assert tf["authored_source"] != "manifest", (
        "a rewritten board downgraded a connected project, which is the one "
        "case an out-of-reach record can close: %r" % tf
    )
    assert block, "the done edge is open on a superseded authored set"
    assert authority.calls, "the gate never asked the control plane"


def test_a_connected_project_fails_closed_when_it_cannot_ask(repo: Path, monkeypatch):
    """Unreachable is not unconfigured.

    A connected project whose authority cannot be read has LESS evidence than
    an offline one, not more. Answering from the local marker here would hand
    it the offline guarantee without saying so, which is the shape of every
    silent-degradation finding on this epic.
    """
    import manifest_emitter

    cycle_id, v1 = _revised_v2(repo)
    _downgrade(repo, cycle_id, v1)
    monkeypatch.setattr(
        manifest_emitter,
        "read_cycle_authority",
        _Authority(problem="connection refused"),
    )

    dod, block = _gate(repo)
    tf = _stanza(dod)
    assert tf["authored_source"] == "missing_manifest", tf
    assert "could not be read" in (tf.get("authored_seal_witness") or ""), tf
    assert block, "the done edge is open with no way to establish the revision"


def test_deleting_project_files_cannot_suppress_the_authority_read(
    repo: Path, monkeypatch
):
    """The adversarial arm (#396, #507).

    The first connected-project cut decided whether to consult the control
    plane by reading a marker directory the emitter writes INSIDE the project,
    so the graded principal could delete it and self-declare offline: the
    out-of-reach record still existed, and only the same-principal signal
    deciding whether to consult it was removed.

    So this does the downgrade AND removes every project-local trace of ever
    having been connected, and the gate still refuses. Connectedness is the
    CLI's answer now, from its stamp and its session, and neither is here.
    """
    import shutil

    import manifest_emitter

    cycle_id, v1 = _revised_v2(repo)
    v2_hash = json.loads(
        Path(sp.manifest_path(str(repo), cycle_id)).read_text(encoding="utf-8")
    )[cr.HASH_FIELD]
    _downgrade(repo, cycle_id, v1)
    for trace in (
        Path(repo) / ".synaptory" / ".orchestrator" / "spq" / ".shipped-manifests",
        Path(repo) / ".synaptory" / ".orchestrator" / "spq" / "outbox",
    ):
        shutil.rmtree(trace, ignore_errors=True)

    authority = _Authority(recorded=v2_hash)
    monkeypatch.setattr(manifest_emitter, "read_cycle_authority", authority)

    dod, block = _gate(repo)
    tf = _stanza(dod)
    assert tf["authored_source"] != "manifest", (
        "deleting the project's own trace of being connected suppressed the "
        "authority read and reopened the downgrade: %r" % tf
    )
    assert block, "the done edge is open on a superseded authored set"
    assert authority.calls, "the gate stopped asking once the project looked bare"


#: What a maintainer has to do if the offline downgrade ever stops working.
#:
#: It is deliberately NOT an issue number. The offline limit is a PERMANENT
#: DESIGN LIMIT under proposal §12.17, which states it as "a decision rather
#: than an oversight": both closures that exist need connectivity at the
#: moment of revision, and requiring either would make the Definition of Done
#: gate depend on the control plane, which §3.3 refuses. #507 recorded the
#: finding and is closed for what it delivered -- the connected half -- so
#: pointing this tripwire at it named a closed issue as the owner of work
#: nobody owes (#659 Track D).
#:
#: So a failure here is not "the bug got fixed". It means either the offline
#: gate quietly acquired a control-plane dependency, which contradicts §3.3
#: and is a regression, or §3.3 itself changed, which is a methodology
#: decision that has to be recorded before this assertion is relaxed.
OFFLINE_LIMIT_TRIPWIRE = (
    "the offline revision-authority limit no longer holds. It is a permanent "
    "design limit, not a deferred fix: proposal §12.17 declares it and §3.3 is "
    "the constraint that makes it one (an offline project must be able to "
    "evaluate its own DoD, so the gate may not require the control plane). "
    "Either the offline gate has gained a control-plane dependency, which is a "
    "regression against §3.3, or §3.3 has changed -- record that decision in "
    "§12.17 and §3.3 and update this test together with it, and do not simply "
    "delete the assertion"
)


def test_an_offline_project_keeps_the_local_marker_and_its_limit(repo: Path, monkeypatch):
    """The explicit offline arm (proposal §12.17, §3.3).

    The CLI answers `connected: false`, which is what a machine with no
    control-plane stamp or no session reports, so there is no row to ask for
    and the gate must still evaluate: local delivery state is canonical and
    §3.3 refuses to make the DoD gate depend on the control plane. The
    downgrade therefore still works here, which is the DECLARED PERMANENT
    LIMIT rather than a defect or an open gap, and this arm is what stops the
    offline path being quietly broken by the connected one.

    The connected half of the same finding is closed and lives elsewhere:
    `read_cycle_authority` asks the recorded row and unreachable fails closed.
    See `OFFLINE_LIMIT_TRIPWIRE` for what a failure here actually means.
    """
    import manifest_emitter

    cycle_id, v1 = _revised_v2(repo)
    _downgrade(repo, cycle_id, v1)
    monkeypatch.setattr(manifest_emitter, "read_cycle_authority", _Authority())

    dod, block = _gate(repo)
    tf = _stanza(dod)
    assert tf["authored_source"] == "manifest", tf
    assert tf["authored_case_ids"] == ["AC-1"], tf
    assert block is None, "%s: %s" % (OFFLINE_LIMIT_TRIPWIRE, block)


class _StubCLI:
    """A resolved CLI whose answer the test controls, end to end.

    Patches `_resolve_cli` and `subprocess.run` inside `manifest_emitter`
    rather than `read_cycle_authority` itself, so these arms exercise the real
    parse, the real classification, and the gate's handling of all three
    outcomes.
    """

    def __init__(self, stdout="", returncode=0):
        self.stdout, self.returncode = stdout, returncode
        self.calls = []

    def install(self, monkeypatch, cli="/usr/bin/synaptory"):
        import manifest_emitter

        monkeypatch.setattr(manifest_emitter, "_resolve_cli", lambda: cli)
        outer = self

        class _Proc:
            returncode = outer.returncode
            stdout = outer.stdout
            stderr = ""

        def _run(argv, **kwargs):
            outer.calls.append(argv)
            return _Proc()

        monkeypatch.setattr(manifest_emitter.subprocess, "run", _run)
        return self


def test_an_old_cli_blocks_rather_than_grading_the_local_revision(
    repo: Path, monkeypatch
):
    """VERSION SKEW MUST NOT DOWNGRADE THE GATE (#396, #507).

    A CLI predating `telemetry cycle-authority` prints cobra's help and exits
    zero. The stamped tree still names a control plane and the row still
    exists, so treating that as offline grades the agent-writable local
    revision on a connected project. It blocks, and the reason says what to do.
    """
    cycle_id, v1 = _revised_v2(repo)
    _downgrade(repo, cycle_id, v1)
    stub = _StubCLI(
        stdout="Ship telemetry events\n\nUsage:\n  synaptory telemetry [command]\n"
    ).install(monkeypatch)

    dod, block = _gate(repo)
    tf = _stanza(dod)
    assert tf["authored_source"] != "manifest", (
        "an old CLI downgraded a connected project to its local revision: %r" % tf
    )
    assert block, "the done edge is open on a superseded authored set"
    assert "update the CLI" in (tf.get("authored_seal_witness") or ""), (
        "the refusal does not tell the operator what to do: %r" % tf
    )
    assert stub.calls, "the gate never asked"


def test_a_substituted_executable_blocks_too(repo: Path, monkeypatch):
    """`SYNAPTORY_CLI_BIN` naming something silent is not a way to tell the
    gate this project reports nowhere."""
    cycle_id, v1 = _revised_v2(repo)
    _downgrade(repo, cycle_id, v1)
    _StubCLI(stdout="").install(monkeypatch, cli="/bin/true")

    dod, block = _gate(repo)
    assert _stanza(dod)["authored_source"] != "manifest", _stanza(dod)
    assert block


def test_a_capable_cli_still_grades_the_recorded_revision(repo: Path, monkeypatch):
    """The positive arm, so the contract is a distinction and not a refusal:
    a CLI that answers is believed, and the recorded revision decides."""
    cycle_id, v1 = _revised_v2(repo)
    v2_hash = json.loads(
        Path(sp.manifest_path(str(repo), cycle_id)).read_text(encoding="utf-8")
    )[cr.HASH_FIELD]
    _downgrade(repo, cycle_id, v1)
    _StubCLI(
        stdout=json.dumps({"connected": True, "manifest_hash": v2_hash})
    ).install(monkeypatch)

    dod, block = _gate(repo)
    assert _stanza(dod)["authored_source"] != "manifest", _stanza(dod)
    assert block, "the recorded revision did not stop the downgrade"


def _real_resolver(monkeypatch, tmp_path: Path, *, stamped: bool):
    """Let the REAL `_resolve_cli` run, with the stamp and its search path
    controlled.

    Patching the resolver's RESULT is what hid #396's last finding: `None`
    carries an unstamped tree AND a stamped tree with nothing installed, and
    only the first is offline. These arms patch the INPUT and let the resolver
    decide, which is what the finding asked for.
    """
    import host_env

    # Both, because they answer different questions from the same files: the
    # resolver asks WHICH channel, and the reader asks WHETHER one exists.
    monkeypatch.setattr(
        host_env,
        "channel_signal",
        lambda: (False, "cp-url stamp (https://cp.example)" if stamped else ""),
    )
    monkeypatch.setattr(
        host_env,
        "control_plane_stamp_state",
        lambda: (
            ("stamped", "cp-url stamp (https://cp.example)")
            if stamped
            else ("unstamped", "")
        ),
    )
    empty = tmp_path / "empty-bin"
    empty.mkdir(exist_ok=True)
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True, exist_ok=True)
    (home / "bin").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SYNAPTORY_CLI_BIN", raising=False)


def test_a_stamped_tree_with_no_cli_blocks(repo: Path, monkeypatch, tmp_path):
    """THE FINDING (#396), at the gate. Removing or renaming the matching CLI
    on a stamped tree used to reach the same `(None, "")` an unstamped tree
    returns, so the connected project graded its local revision."""
    cycle_id, v1 = _revised_v2(repo)
    _downgrade(repo, cycle_id, v1)
    _real_resolver(monkeypatch, tmp_path, stamped=True)

    dod, block = _gate(repo)
    tf = _stanza(dod)
    assert tf["authored_source"] != "manifest", (
        "a stamped tree with no CLI downgraded to its local revision: %r" % tf
    )
    assert block, "the done edge is open on a superseded authored set"
    assert "no usable CLI resolves" in (tf.get("authored_seal_witness") or ""), tf


def test_an_unstamped_tree_is_still_offline_and_still_evaluates(
    repo: Path, monkeypatch, tmp_path
):
    """The real offline arm, through the same resolver. No stamp, so nothing
    addresses a control plane and there is no record to require: the gate still
    evaluates, because local delivery state is canonical (§3.3). The downgrade
    works here, which is the declared permanent limit."""
    cycle_id, v1 = _revised_v2(repo)
    _downgrade(repo, cycle_id, v1)
    _real_resolver(monkeypatch, tmp_path, stamped=False)

    dod, block = _gate(repo)
    tf = _stanza(dod)
    assert tf["authored_source"] == "manifest", tf
    assert tf["authored_case_ids"] == ["AC-1"], tf
    assert block is None, "%s: %s" % (OFFLINE_LIMIT_TRIPWIRE, block)


def test_an_unreadable_stamp_signal_blocks(repo: Path, monkeypatch, tmp_path):
    """THE SAME THREE-STATE ERROR, ONE LEVEL EARLIER (#396).

    The stamp reader caught every exception and answered "not stamped", so a
    damaged install, an import failure, or a cp-url whose permissions deny it
    all granted the offline downgrade. An install that cannot say which control
    plane it belongs to has UNKNOWN authority, and answering offline there is a
    guess in the graded party's favour.
    """
    import host_env

    cycle_id, v1 = _revised_v2(repo)
    _downgrade(repo, cycle_id, v1)

    def _boom():
        raise RuntimeError("this install is damaged")

    monkeypatch.setattr(host_env, "control_plane_stamp_state", _boom)
    _real_resolver(monkeypatch, tmp_path, stamped=False)
    monkeypatch.setattr(host_env, "control_plane_stamp_state", _boom)

    dod, block = _gate(repo)
    tf = _stanza(dod)
    assert tf["authored_source"] != "manifest", (
        "an install that cannot read its own stamp graded the local "
        "revision: %r" % tf
    )
    assert block, "the done edge is open on a superseded authored set"
    witness = tf.get("authored_seal_witness") or ""
    assert "cannot report which control plane" in witness, witness
    assert "Repair the install" in witness, "the refusal does not say what to do"


def test_a_stamp_whose_file_cannot_be_opened_blocks(
    repo: Path, monkeypatch, tmp_path
):
    """The same fact from the filesystem rather than an exception: a stamp that
    is present and unreadable is not an absent stamp."""
    import host_env

    cycle_id, v1 = _revised_v2(repo)
    _downgrade(repo, cycle_id, v1)
    _real_resolver(monkeypatch, tmp_path, stamped=False)
    monkeypatch.setattr(
        host_env,
        "control_plane_stamp_state",
        lambda: ("unreadable", "a cp-url stamp exists and could not be read"),
    )

    dod, block = _gate(repo)
    assert _stanza(dod)["authored_source"] != "manifest", _stanza(dod)
    assert block
