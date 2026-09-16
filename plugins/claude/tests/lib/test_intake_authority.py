#!/usr/bin/env python3
"""`intake_authority`: the three answers, and who is allowed to say `offline`.

The point of this module is that a fact lives where its own subject cannot
rewrite it (#495 P3), so the tests that matter are the ones proving the three
verdicts stay APART. Collapsing `problem` into `not_connected` is the whole
defect class: a connected project whose store is unreachable would silently
acquire the offline guarantee, which is #507's finding.

The CLI is a shim throughout. `SYNAPTORY_INTAKE_AUTHORITY_BIN` exists for this,
and it selects WHICH BINARY answers, never WHAT it answers.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "core" / "lib"))

import intake_authority as ia  # noqa: E402


def _shim(tmp_path: Path, body: str, code: int = 0, name: str = "cli") -> Path:
    script = tmp_path / name
    script.write_text(
        "#!/bin/bash\ncat <<'JSON'\n%s\nJSON\nexit %d\n" % (body, code),
        encoding="utf-8",
    )
    script.chmod(0o755)
    # WARMED HERE, not in the test body. The first execve of a new file costs
    # around half a second on this platform, and a test that measures the cold
    # one passes for the wrong reason.
    import subprocess

    subprocess.run([str(script)], capture_output=True)
    return script


def _use(monkeypatch, cli: Path | None, stamp: str = "stamped") -> None:
    monkeypatch.setattr(ia, "_resolve_cli", lambda: (str(cli) if cli else None))
    monkeypatch.setattr(ia, "_channel_stamp", lambda: (stamp, "test"))


# ── the offline answer, and its boundary ────────────────────────────────────


def test_an_unstamped_tree_is_not_connected_rather_than_broken(monkeypatch):
    """No control plane means no out-of-reach store, which is an ANSWER.

    Refusing here would make SPQ unusable offline, which proposal 3.3 forbids
    and #659 Track D already settled as a permanent limit. Granting would be a
    lie. So it is neither, and the verdict says which world it was.
    """
    _use(monkeypatch, None, stamp="unstamped")
    for verdict in (
        ia.record_intake("/tmp", "US-901", "sha256:abc"),
        ia.read_intake_fact("/tmp", "US-901"),
    ):
        assert verdict["state"] == ia.NOT_CONNECTED, verdict
        assert verdict["authority"] == "none", verdict


def test_a_stamped_tree_with_no_cli_is_a_problem_not_offline(monkeypatch):
    """The distinction #507 cost two attempts to get right.

    A stamped project HAS a store out of the principal's reach. Not being able
    to reach it is less evidence than being offline, not more.
    """
    _use(monkeypatch, None, stamp="stamped")
    verdict = ia.read_intake_fact("/tmp", "US-901")
    assert verdict["state"] == ia.PROBLEM, verdict
    assert verdict["authority"] == "control-plane", verdict
    assert "no usable CLI" in verdict["detail"], verdict


def test_an_unreadable_stamp_is_a_problem_in_the_principals_favour_never(monkeypatch):
    """A damaged install cannot say whether a record exists.

    Answering `not_connected` there would be a guess in the graded party's
    favour, which is the direction that must never be guessed.
    """
    _use(monkeypatch, None, stamp="unreadable")
    verdict = ia.record_intake("/tmp", "US-901", "sha256:abc")
    assert verdict["state"] == ia.PROBLEM, verdict
    assert "cannot report which control plane" in verdict["detail"], verdict


def test_resolve_cli_returning_none_is_not_itself_read_as_offline(monkeypatch):
    """#396: that single None covers five states and only one is offline.

    Asserted through the stamp rather than the resolver, so a future refactor
    that starts reading `_resolve_cli() is None` as offline fails here.
    """
    _use(monkeypatch, None, stamp="stamped")
    assert ia.read_intake_fact("/tmp", "US-901")["state"] == ia.PROBLEM
    _use(monkeypatch, None, stamp="unstamped")
    assert ia.read_intake_fact("/tmp", "US-901")["state"] == ia.NOT_CONNECTED


# ── version skew is a problem, not silence ──────────────────────────────────


def test_a_cli_without_the_verb_is_skew_not_an_empty_store(monkeypatch, tmp_path):
    """An answer with no `connected` key is NO ANSWER.

    A CLI predating `cycles decisions` can print cobra output and exit in ways
    an exit code cannot distinguish from an answer, which is why the marker
    exists and why this may not read as "nothing is recorded".
    """
    _use(monkeypatch, _shim(tmp_path, '{"ok": false, "reason": "unknown command"}', 1))
    verdict = ia.read_intake_fact(str(tmp_path), "US-901")
    assert verdict["state"] == ia.PROBLEM, verdict
    assert "shape this runtime recognises" in verdict["detail"], verdict


def test_a_zero_exit_with_no_marker_is_still_skew(monkeypatch, tmp_path):
    """The case an exit code cannot catch, asserted separately on purpose."""
    _use(monkeypatch, _shim(tmp_path, "Usage:\n  synaptory cycles [command]", 0))
    verdict = ia.record_intake(str(tmp_path), "US-901", "sha256:abc")
    assert verdict["state"] == ia.PROBLEM, verdict


# ── the recorded answer ─────────────────────────────────────────────────────


def test_recording_returns_the_servers_own_principal_and_digest(monkeypatch, tmp_path):
    """What comes back is what the SERVER derived, which is the point.

    The principal is the authenticated caller and the role is derived from the
    action, so reading them off the stored row is the only way to know what was
    actually attributed.
    """
    body = json.dumps({
        "ok": True, "reason": "recorded", "connected": True,
        "decision": {
            "id": "d-1", "action": "admit-work-unit", "role": "engineering-lead",
            "principal": "lead@h3t.co", "subject": "US-901",
            "subject_digest": "sha256:aaa", "produced_by": "outside@client.example",
        },
    })
    _use(monkeypatch, _shim(tmp_path, body))
    verdict = ia.record_intake(
        str(tmp_path), "US-901", "sha256:aaa", produced_by="outside@client.example"
    )
    assert verdict["state"] == ia.RECORDED, verdict
    assert verdict["authority"] == "control-plane", verdict
    assert verdict["subject_digest"] == "sha256:aaa", verdict
    assert verdict["principal"] == "lead@h3t.co", verdict


def test_reading_takes_the_latest_admit_row_and_ignores_other_actions(
    monkeypatch, tmp_path
):
    """Same subject, three actions, two of them not an admission.

    `accept-work-unit` also carries a `subject_digest` and it means something
    else, so filtering by action is what keeps this from reading an acceptance
    as an admission. Append-only plus linked supersession is why the latest
    admit row is the current one.
    """
    body = json.dumps({
        "ok": True, "reason": "listed", "connected": True,
        "decisions": [
            {"id": "d-1", "action": "admit-work-unit", "subject_digest": "sha256:old",
             "decided_at": "2026-09-01T00:00:00Z"},
            {"id": "d-3", "action": "accept-work-unit", "subject_digest": "sha256:nope",
             "decided_at": "2026-09-03T00:00:00Z"},
            {"id": "d-2", "action": "admit-work-unit", "subject_digest": "sha256:new",
             "decided_at": "2026-09-02T00:00:00Z"},
        ],
    })
    _use(monkeypatch, _shim(tmp_path, body))
    verdict = ia.read_intake_fact(str(tmp_path), "US-901")
    assert verdict["state"] == ia.RECORDED, verdict
    assert verdict["subject_digest"] == "sha256:new", verdict
    assert verdict["decision_id"] == "d-2", verdict


def test_connected_with_nothing_recorded_is_an_answer_not_a_fallback(
    monkeypatch, tmp_path
):
    """A unit nobody admitted authoritatively must not read as `offline`.

    If it did, a connected project could get the local record's weaker
    guarantee simply by never recording anything, which is the bypass this
    whole module exists to close.
    """
    body = json.dumps({
        "ok": True, "reason": "listed", "connected": True, "decisions": [],
    })
    _use(monkeypatch, _shim(tmp_path, body))
    verdict = ia.read_intake_fact(str(tmp_path), "US-901")
    assert verdict["state"] == ia.RECORDED, verdict
    assert verdict["subject_digest"] == "", verdict
    assert verdict["authority"] == "control-plane", verdict


def test_a_server_refusal_is_a_problem_carrying_its_reason(monkeypatch, tmp_path):
    """Self-approval and membership refusals must reach the operator intact."""
    body = json.dumps({
        "ok": False, "reason": "refused", "connected": True,
        "detail": "principal produced the subject it is deciding on",
    })
    _use(monkeypatch, _shim(tmp_path, body, 1))
    verdict = ia.record_intake(str(tmp_path), "US-901", "sha256:aaa")
    assert verdict["state"] == ia.PROBLEM, verdict
    assert "produced the subject" in verdict["detail"], verdict


# ── refusals that need no control plane ─────────────────────────────────────


@pytest.mark.parametrize(
    "unit,digest", [("", "sha256:aaa"), ("US-901", ""), ("", "")]
)
def test_a_partial_fact_is_refused_before_any_subprocess(
    monkeypatch, tmp_path, unit, digest
):
    """Refused BEFORE the gate, so it cannot be mistaken for an outage.

    A record with no digest, or no subject, is not a weaker intake fact; it is
    not one at all, and recording it would put an unfalsifiable row in an
    append-only store.
    """
    called = tmp_path / "called"
    script = tmp_path / "must-not-run"
    script.write_text("#!/bin/bash\ntouch %s\n" % called, encoding="utf-8")
    script.chmod(0o755)
    _use(monkeypatch, script)
    verdict = ia.record_intake(str(tmp_path), unit, digest)
    assert verdict["state"] == ia.PROBLEM, verdict
    assert not called.exists(), "a partial fact reached the control plane"


# ── the CLI's own "no control plane" answer ─────────────────────────────────


def test_the_cli_saying_not_connected_is_honoured_over_the_stamp(
    monkeypatch, tmp_path
):
    """A stamped tree whose CLI reports nowhere is OFFLINE, not broken.

    The stamp and the session answer different questions. A committed host
    tree can carry a production stamp because that tree IS the install, while
    the project it runs against reports nowhere, and only the binary's own
    session can tell those apart. `telemetry cycle-authority` already answers
    this way and `read_cycle_authority` already honours it; refusing here
    would make two parts of the runtime disagree about what "connected" means.

    Found by the conformance suite: the Cursor arm ships a production `cp-url`,
    so without this the whole host could not admit a candidate at all.
    """
    _use(monkeypatch, _shim(tmp_path, '{"connected": false}'), stamp="stamped")
    for verdict in (
        ia.record_intake(str(tmp_path), "US-901", "sha256:aaa"),
        ia.read_intake_fact(str(tmp_path), "US-901"),
    ):
        assert verdict["state"] == ia.NOT_CONNECTED, verdict
        assert verdict["authority"] == "none", verdict


def test_connected_true_with_a_refusal_is_still_a_problem(monkeypatch, tmp_path):
    """`connected` is not a success flag, and the two must not be conflated.

    A CLI that reached the control plane and was refused reports
    `connected: true` with `ok: false`. Reading the presence of `connected` as
    "fine" would turn every server refusal into a silent pass.
    """
    _use(monkeypatch, _shim(
        tmp_path, '{"connected": true, "ok": false, "detail": "archived"}', 1
    ), stamp="stamped")
    verdict = ia.read_intake_fact(str(tmp_path), "US-901")
    assert verdict["state"] == ia.PROBLEM, verdict
    assert "archived" in verdict["detail"], verdict
