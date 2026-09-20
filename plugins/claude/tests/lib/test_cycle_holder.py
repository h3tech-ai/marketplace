"""Layer 1 — `cycle_holder`, the record of WHICH SESSION drives a Cycle (#791).

The claim is act-based: a session becomes the holder by dispatching into the
Cycle, never by stopping in its directory. These tests pin that asymmetry
(`claim` creates and takes over, `touch` does neither), the fail-safe reads
(unreadable record, undatable record, empty session id), and the expiry rule
that expiry only ever PERMITS continuation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import cycle_holder

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 19, 10, 0, 0, tzinfo=timezone.utc)


def _ev(project: Path, session: str, *, now: datetime = T0, ttl: int | None = None) -> dict:
    kwargs = {"now": now}
    if ttl is not None:
        kwargs["ttl_seconds"] = ttl
    return cycle_holder.evaluate(str(project), "cycle:2-bc5ba51c", session, **kwargs)


def _claim(project: Path, session: str, *, now: datetime = T0) -> dict:
    return cycle_holder.claim(str(project), "cycle:2-bc5ba51c", session, now=now)


# ── the claim ────────────────────────────────────────────────────────────────


def test_no_record_means_nobody_holds_anything(tmp_path: Path):
    assert _ev(tmp_path, "s-any")["verdict"] == cycle_holder.UNCLAIMED


def test_dispatch_makes_the_dispatching_session_the_holder(tmp_path: Path):
    out = _claim(tmp_path, "s-delivery")
    assert out["claimed"] is True and out["took_over"] is False
    assert _ev(tmp_path, "s-delivery")["verdict"] == cycle_holder.HELD_BY_SELF


def test_a_second_session_is_not_the_holder(tmp_path: Path):
    """The #791 shape: the coordinator never dispatched, so it never holds."""
    _claim(tmp_path, "s-delivery")
    verdict = _ev(tmp_path, "s-orchestration")
    assert verdict["verdict"] == cycle_holder.HELD_BY_OTHER
    assert verdict["holder"]["session_id"] == "s-delivery"


def test_an_empty_session_id_never_matches_a_holder(tmp_path: Path):
    """No identity → not the holder. The fail-safe direction is to stop."""
    _claim(tmp_path, "s-delivery")
    assert _ev(tmp_path, "")["verdict"] == cycle_holder.HELD_BY_OTHER
    assert cycle_holder.claim(str(tmp_path), "cycle:x", "")["claimed"] is False


def test_claim_takes_over_so_a_restarted_session_reclaims(tmp_path: Path):
    """A restart changes the session id. The first dispatch after it must
    reclaim, or a crashed session's record would mute its own successor."""
    _claim(tmp_path, "s-before-restart")
    out = _claim(tmp_path, "s-after-restart", now=T0 + timedelta(minutes=1))
    assert out["took_over"] is True
    assert out["previous_session_id"] == "s-before-restart"
    assert _ev(tmp_path, "s-after-restart", now=T0 + timedelta(minutes=2))[
        "verdict"
    ] == cycle_holder.HELD_BY_SELF


def test_repeated_dispatches_keep_one_claim_and_count(tmp_path: Path):
    _claim(tmp_path, "s-delivery")
    out = _claim(tmp_path, "s-delivery", now=T0 + timedelta(minutes=5))
    assert out["took_over"] is False
    record = cycle_holder.read_holders(str(tmp_path))["cycle:2-bc5ba51c"]
    assert record["dispatches"] == 2
    assert record["claimed_at"] != record["last_seen"]


# ── expiry ───────────────────────────────────────────────────────────────────


def test_a_live_holder_is_never_stale_within_ttl(tmp_path: Path):
    _claim(tmp_path, "s-delivery")
    late = T0 + timedelta(seconds=cycle_holder.DEFAULT_TTL_SECONDS - 60)
    assert _ev(tmp_path, "s-other", now=late)["verdict"] == cycle_holder.HELD_BY_OTHER


def test_an_unrefreshed_claim_stops_muting_after_ttl(tmp_path: Path):
    _claim(tmp_path, "s-delivery")
    late = T0 + timedelta(seconds=cycle_holder.DEFAULT_TTL_SECONDS + 60)
    assert _ev(tmp_path, "s-other", now=late)["verdict"] == cycle_holder.STALE


def test_an_undatable_record_is_treated_as_expired(tmp_path: Path):
    """A record we cannot date is not evidence of a live holder."""
    _claim(tmp_path, "s-delivery")
    import json

    path = cycle_holder.holder_path(str(tmp_path))
    body = json.loads(path.read_text(encoding="utf-8"))
    body["holders"]["cycle:2-bc5ba51c"]["last_seen"] = "not-a-timestamp"
    body["holders"]["cycle:2-bc5ba51c"]["claimed_at"] = ""
    path.write_text(json.dumps(body), encoding="utf-8")
    assert _ev(tmp_path, "s-other")["verdict"] == cycle_holder.STALE


def test_a_corrupt_record_reads_as_no_claim(tmp_path: Path):
    orch = tmp_path / ".synaptory" / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "cycle-holder.json").write_text("{not json", encoding="utf-8")
    assert _ev(tmp_path, "s-any")["verdict"] == cycle_holder.UNCLAIMED


# ── touch: refresh only, never create, never steal ───────────────────────────


def test_touch_refreshes_our_own_claim(tmp_path: Path):
    _claim(tmp_path, "s-delivery")
    later = T0 + timedelta(minutes=30)
    assert cycle_holder.touch(str(tmp_path), "cycle:2-bc5ba51c", "s-delivery", now=later)
    # The refresh is what keeps a looping holder alive past the TTL window.
    beyond = later + timedelta(seconds=cycle_holder.DEFAULT_TTL_SECONDS - 60)
    assert _ev(tmp_path, "s-other", now=beyond)["verdict"] == cycle_holder.HELD_BY_OTHER


def test_touch_cannot_create_a_claim(tmp_path: Path):
    """A Stop-established claim is 'first session to stop wins', which can
    mute the real Engineering Lead. `touch` must be incapable of it."""
    assert cycle_holder.touch(str(tmp_path), "cycle:2-bc5ba51c", "s-orchestration") is False
    assert _ev(tmp_path, "s-anyone")["verdict"] == cycle_holder.UNCLAIMED


def test_touch_cannot_steal_a_live_claim(tmp_path: Path):
    _claim(tmp_path, "s-delivery")
    assert cycle_holder.touch(str(tmp_path), "cycle:2-bc5ba51c", "s-orchestration") is False
    assert _ev(tmp_path, "s-delivery")["verdict"] == cycle_holder.HELD_BY_SELF


# ── scopes are independent ───────────────────────────────────────────────────


def test_two_cycles_have_two_holders(tmp_path: Path):
    cycle_holder.claim(str(tmp_path), "cycle:2-aaa", "s-one", now=T0)
    cycle_holder.claim(str(tmp_path), "cycle:3-bbb", "s-two", now=T0)
    assert cycle_holder.evaluate(str(tmp_path), "cycle:2-aaa", "s-one", now=T0)[
        "verdict"
    ] == cycle_holder.HELD_BY_SELF
    assert cycle_holder.evaluate(str(tmp_path), "cycle:3-bbb", "s-two", now=T0)[
        "verdict"
    ] == cycle_holder.HELD_BY_SELF


# ── which dispatches make a session the driver ───────────────────────────────


@pytest.mark.parametrize(
    "role",
    ["software-engineer", "quality-engineer", "code-reviewer",
     "compliance-engineer", "platform-engineer", "technical-writer",
     "synaptory:software-engineer", "Quality-Engineer"],
)
def test_pipeline_roles_drive_a_cycle(role: str):
    assert cycle_holder.drives_a_cycle(role) is True


@pytest.mark.parametrize(
    "role", ["research-advisor", "project-owner", "solution-architect"]
)
def test_advisory_roles_do_not_drive_a_cycle(role: str):
    """A coordinator asking a research-advisor a question must not take the
    Cycle — that would be #791 inverted, muting the real Engineering Lead."""
    assert cycle_holder.drives_a_cycle(role) is False


def test_an_unknown_role_still_drives():
    """A host that cannot name the role it dispatched must stay addressable."""
    assert cycle_holder.drives_a_cycle("") is True


def test_driving_roles_match_the_actions_the_loop_instructs():
    """The set is derived from two lists that live elsewhere. Pin the
    correspondence so neither can drift alone."""
    import spq_state_machine
    import story_pipeline

    dispatch_abbrevs = {
        a[len("dispatch_"):]
        for a in story_pipeline.CONTINUE_ELIGIBLE
        if a.startswith("dispatch_")
    }
    expected = {
        "se": "software-engineer",
        "qe": "quality-engineer",
        "cr": "code-reviewer",
    }
    assert dispatch_abbrevs == set(expected), dispatch_abbrevs
    assert cycle_holder.DRIVING_ROLES == set(expected.values()) | set(
        spq_state_machine.ACCEPTANCE_ROLES.values()
    )
