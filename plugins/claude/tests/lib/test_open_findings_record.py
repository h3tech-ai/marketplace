"""Layer 1 -- a finding accepted open at promotion is a record (#823).

A receipt carries `findings`. `receipt_validator` checks the payload shape.
The DoD gate reads `metrics.findings_critical` for `no_critical_findings`. And
then nothing persisted them.

`cuts.json` exists for a unit withdrawn from a Cycle, sealed manifests for
what was admitted, a barrier ledger for promotions and closes -- and there was
no store for a defect that was found, judged non-blocking, and shipped. So the
only durable home an accepted-open finding had was free text inside
`decision.rationale`:

    "Six findings remain open and were stated before the authorization was
     given: CR-107-19/20/21/22 and QE-107-01/04, none critical, none high,
     none blocking."

Unsearchable, with no severity field, no status, no owner and no path to
closure. Across three Cycles on the reporting engagement fifteen findings were
accepted that way; six survive as bare ids whose substance nobody transcribed.

THE ASYMMETRY. `SC-MTH-009` makes a cut a recorded event the barrier reads,
precisely so nothing vanishes quietly. A finding accepted open has every
property a cut has -- raised by a named role, decided by an accountable human,
with a reason -- and had no record.

IT GATES NOTHING, and that is the issue's own instruction: the owner's
judgement at promotion is the right gate and it works. The only refusal here
is of an id no receipt raises.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import spq_paths
import spq_state_machine as sm
import story_pipeline as story

from _spq_fixture import CYCLE_KWARGS, unit as _fx_unit


pytestmark = pytest.mark.unit

FINDINGS = [
    {
        "id": "CR-002",
        "title": "three of the four new serializers are invoked by no test",
        "severity": "high",
        "file_ref": "api/wu-01/impl.py",
        "description": "dead on arrival; WU-301's CR-07 one layer along",
    },
    {
        "id": "QE-004",
        "title": "the export path has no negative case",
        "severity": "medium",
        "file_ref": "api/wu-01/impl.py",
        "description": "",
    },
]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=False)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _drive_to_done(project: Path, unit_id: str, *, findings=None) -> None:
    """One Work Unit through the receipt-gated walk, via `execute_advance`.

    Not by writing `state: done`: the barrier reads per-criterion results out
    of the DoD gate, so a unit set done by hand has a gate that never ran and
    every assertion below would be about the fixture.

    `findings` lands on the CR receipt, which is where a reviewer raises one.
    """
    import advance_kernel as ak

    policy = ak.HostPolicy(host="test", require_next_action_match=False)
    walk = (
        ("in_progress", "software-engineer", "se", 1),
        ("testing", "software-engineer", "se", 2),
        ("reviewing", "quality-engineer", "qe", 1),
        ("done", "code-reviewer", "cr", 1),
    )
    receipts = Path(story.receipts_dir_for(str(project), intended=True))
    receipts.mkdir(parents=True, exist_ok=True)
    for target, role, abbrev, nth in walk:
        artifact = project / "api" / unit_id.lower() / "impl.py"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("# %s %d\n" % (abbrev, nth), encoding="utf-8")
        payload = {
            "story_id": unit_id,
            "role": role,
            "agent": role,
            "backend": "claude",
            "model": "test-fixture",
            "task": "%s for %s" % (abbrev, unit_id),
            "artifacts": ["api/%s/impl.py" % unit_id.lower()],
            "verification_commands": [
                {"command": "pytest -q", "exit_code": 0, "summary": "ok"}
            ],
            "metrics": {"n": nth, "tests_passed": 1, "tests_failed": 0},
            "status": "complete",
            "completed_at": _now_iso(),
        }
        if findings and abbrev == "cr":
            payload["findings"] = findings
        (receipts / ("%s-%s.json" % (unit_id, abbrev))).write_text(
            json.dumps(payload, indent=1), encoding="utf-8"
        )
        decision = ak.execute_advance(str(project), unit_id, target, policy=policy)
        assert decision.allowed, (target, decision.code, decision.reason)


@pytest.fixture
def promotable(tmp_path: Path, monkeypatch):
    """A Cycle whose barrier can go green, with two findings on its review."""
    project = tmp_path / "proj"
    project.mkdir()
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "t@e.co")
    _git(project, "config", "user.name", "t")
    (project / "reg.sh").write_text(
        "#!/bin/bash\necho '412 passed'\nexit 0\n", encoding="utf-8"
    )
    (project / ".synaptory.yaml").write_text(
        "build_mode: spq\n"
        "quality:\n  dod_tier: growing\n"
        "spq:\n  regression_script: \"reg.sh\"\n",
        encoding="utf-8",
    )
    (project / ".gitignore").write_text(
        ".synaptory/*\n!.synaptory/cycles/\n", encoding="utf-8"
    )
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")
    _git(project, "branch", "-M", "dev")
    monkeypatch.delenv("SYNAPTORY_ACTIVE_SPEC", raising=False)

    sm.initialize(str(project))
    sm.approve_baseline(
        str(project), approved_by="lead@h3t.co", baseline_ref="baseline-1",
        calibration={"sample_units": 2, "measured_hours": 8},
    )
    sm.open_cycle(
        str(project), goal="cycle 1", admitted_units=[_fx_unit("WU-01")],
        **CYCLE_KWARGS,
    )
    cycle_id = sm.identity(str(project)).cycle_id
    _drive_to_done(project, "WU-01", findings=FINDINGS)
    _git(project, "checkout", "-q", "-b", "candidate")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "WU-01")
    return project, cycle_id


def _promote(project, **over):
    kwargs = {"principal": "lead@h3t.co"}
    kwargs.update(over)
    return sm.promote_cycle(str(project), **kwargs)


# ── the record exists, and carries what prose could not ───────────────────────


class TestTheFindingBecomesARecord:
    def test_a_promotion_with_open_findings_writes_them(self, promotable):
        project, cycle_id = promotable

        record = _promote(project, rationale="none critical, none blocking",
                          open_findings=[{"id": "CR-002"}, {"id": "QE-004"}])

        assert record["outcome"] == "promoted"
        assert record["open_findings_recorded"] == ["CR-002", "QE-004"]
        found = {f["id"]: f for f in sm.read_findings(str(project), cycle_id)}
        assert set(found) == {"CR-002", "QE-004"}

    def test_each_row_carries_the_fields_prose_had_no_place_for(self, promotable):
        """Six of the reporting engagement's findings survive as bare ids. The
        substance was in the receipts and was never transcribed anywhere a
        reader would find it, so it is transcribed from them here."""
        project, cycle_id = promotable

        _promote(project, rationale="accepted: not blocking this Checkpoint",
                 open_findings=[{"id": "CR-002"}])
        row = sm.read_findings(str(project), cycle_id)[0]

        assert row["severity"] == "high"          # from the receipt, not retyped
        assert "serializers" in row["title"]
        assert row["unit_id"] == "WU-01"
        assert row["raised_by"] == "code-reviewer"
        assert row["file_ref"] == "api/wu-01/impl.py"
        assert row["status"] == "open"            # and a place for closure
        assert row["accepted_by"] == "lead@h3t.co"
        assert row["accepted_at"]
        assert row["accepted_rationale"] == "accepted: not blocking this Checkpoint"

    def test_the_owner_may_re_judge_severity_at_promotion(self, promotable):
        """The receipt records what the reviewer graded it. Accepting it open
        is a second judgement and may carry a different one."""
        project, cycle_id = promotable

        _promote(project, open_findings=[{"id": "CR-002", "severity": "low"}])

        assert sm.read_findings(str(project), cycle_id)[0]["severity"] == "low"

    def test_the_record_is_committed_not_gitignored(self, promotable):
        """A later Cycle entering this region is a clone that did not make the
        decision -- the same reason the cut record is committed."""
        project, cycle_id = promotable
        _promote(project, open_findings=[{"id": "CR-002"}])

        path = Path(spq_paths.committed_findings_path(str(project), cycle_id))
        assert path.is_file()
        check = subprocess.run(
            ["git", "check-ignore", "-q", str(path.relative_to(project))],
            cwd=str(project), capture_output=True,
        )
        assert check.returncode != 0, (
            "the findings record is gitignored, so the next Cycle in this "
            "region cannot be told what is already known about it"
        )


# ── what it refuses, and what it must not ─────────────────────────────────────


class TestItCorroboratesAndDoesNotEnforce:
    def test_an_id_no_receipt_raises_is_refused(self, promotable):
        """An accepted finding nobody raised is not a judgement. This is the
        only refusal the record adds."""
        project, _ = promotable

        with pytest.raises(ValueError) as excinfo:
            _promote(project, open_findings=[{"id": "CR-999"}])

        message = str(excinfo.value)
        assert "CR-999" in message
        assert "no receipt in this Cycle raises" in message
        # It names what IS available, so the caller can see the typo.
        assert "CR-002" in message and "QE-004" in message

    def test_an_entry_with_no_id_is_refused(self, promotable):
        project, _ = promotable

        with pytest.raises(ValueError):
            _promote(project, open_findings=[{"severity": "high"}])

    def test_a_refused_list_records_nothing_and_promotes_nothing(self, promotable):
        """The corroboration runs BEFORE the promotion, so a typo cannot leave
        a Cycle promoted with a findings record it refused to write."""
        project, cycle_id = promotable

        with pytest.raises(ValueError):
            _promote(project, open_findings=[{"id": "CR-999"}])

        assert sm.read_findings(str(project), cycle_id) == []
        assert not [
            r for r in sm.read_barrier_ledger(str(project), cycle_id)
            if str(r.get("kind")) == "promotion"
        ]

    def test_open_findings_do_not_block_the_promotion(self, promotable):
        """Reporting is enough; enforcement is not wanted. A high-severity
        finding accepted open still promotes -- the owner's judgement is the
        gate and it works."""
        project, _ = promotable

        record = _promote(project, open_findings=[{"id": "CR-002"}])

        assert record["outcome"] == "promoted"

    def test_a_promotion_with_no_findings_writes_no_record(self, promotable):
        """An empty file is not the same as no findings, and a reader should
        not have to tell them apart."""
        project, cycle_id = promotable

        _promote(project)

        assert not Path(
            spq_paths.committed_findings_path(str(project), cycle_id)
        ).exists()

    def test_a_repeated_promotion_records_one_row_per_finding(self, promotable):
        """Idempotent on (id, operation_id), like the barrier ledger beside
        it: `promote` reconciles a retry, and appending again would make one
        accepted finding look like two."""
        project, cycle_id = promotable

        _promote(project, open_findings=[{"id": "CR-002"}])
        _promote(project, open_findings=[{"id": "CR-002"}])

        assert len(sm.read_findings(str(project), cycle_id)) == 1


# ── the close reports the set it is shipping ───────────────────────────────────


class TestTheCloseReportsWhatItShips:
    def test_the_close_record_carries_the_open_findings(self, promotable):
        project, _ = promotable
        _promote(project, open_findings=[{"id": "CR-002"}, {"id": "QE-004"}])
        _git(project, "checkout", "-q", "dev")
        _git(project, "merge", "-q", "--no-ff", "-m", "integrate", "candidate")

        closed = sm.close_cycle(str(project), principal="lead@h3t.co")

        assert closed["ok"] is True
        assert [f["id"] for f in closed["open_findings"]] == ["CR-002", "QE-004"]

    def test_it_is_read_from_the_record_not_taken_as_an_argument(self):
        """`close_cycle` takes no verdict for the same reason. A close handed
        its own list of known defects would be reporting a caller's claim
        about what shipped."""
        import inspect

        assert set(inspect.signature(sm.close_cycle).parameters) == {
            "project_dir", "principal", "rationale", "cycle_id",
        }

    def test_a_reconciled_close_reports_the_same_set(self, promotable):
        """A reconciled close records nothing further, by design. It still
        SHIPPED the set, so answering an empty list to a caller that retried
        would say a Cycle shipped no known defects because the retry wrote no
        new rows."""
        project, _ = promotable
        _promote(project, open_findings=[{"id": "CR-002"}])
        _git(project, "checkout", "-q", "dev")
        _git(project, "merge", "-q", "--no-ff", "-m", "integrate", "candidate")
        sm.close_cycle(str(project), principal="lead@h3t.co")

        again = sm.close_cycle(str(project), principal="lead@h3t.co")

        assert again["reconciled"] is True
        assert [f["id"] for f in again["open_findings"]] == ["CR-002"]

    def test_the_archive_carries_them_for_the_next_cycle(self, promotable):
        """So a Cycle entering this region can be told what is known without
        opening another Cycle's barrier ledger. The engagement caught the same
        defect recurring one unit along only by coincidence."""
        project, _ = promotable
        _promote(project, open_findings=[{"id": "CR-002"}])
        _git(project, "checkout", "-q", "dev")
        _git(project, "merge", "-q", "--no-ff", "-m", "integrate", "candidate")
        sm.close_cycle(str(project), principal="lead@h3t.co")

        archived = sm.read_state(str(project))["cycles_completed"][-1]

        assert [f["id"] for f in archived["open_findings"]] == ["CR-002"]


# ── reachable from a verb ─────────────────────────────────────────────────────


def test_read_findings_is_reachable_from_the_cli(promotable):
    """A store no verb can read reports to nobody, which is the state the
    prose in `barrier.json` was already in."""
    import sys

    project, cycle_id = promotable
    _promote(project, open_findings=[{"id": "CR-002"}])
    repo = Path(__file__).resolve().parents[3]

    result = subprocess.run(
        [sys.executable, str(repo / "core" / "lib" / "spq_state_machine.py"),
         "read_findings", str(project)],
        capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["cycle_id"] == cycle_id
    assert [f["id"] for f in payload["findings"]] == ["CR-002"]
