"""#801 — a future-dated `completed_at` is refused.

The whole check was "non-empty string", unbounded in both directions. Two live
`software-engineer` receipts carried a `completed_at` ~7 hours ahead — local
time (UTC+7) labelled `Z` — and both validated.

The metrics damage is the smaller half. `completed_at` feeds the staleness
gate: a receipt is fresh when it post-dates the stage it advances out of, so a
future-dated one passes that gate BY CONSTRUCTION for as long as the drift
lasts, whatever it actually describes.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks" / "lib"))

import receipt_validator as rv  # noqa: E402

pytestmark = pytest.mark.unit


def _stamp(delta_seconds: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check(raw: str):
    result = rv.ValidationResult()
    rv._check_completed_at_bounds(result, raw)
    return result


def test_a_timezone_mislabel_is_refused():
    """The measured shape: local time written as `Z`, ~7 hours ahead."""
    result = _check(_stamp(7 * 3600))

    assert result.errors
    joined = " ".join(result.errors)
    assert "FUTURE" in joined
    # It has to name the cause, because the author believed they wrote UTC.
    assert "local time" in joined and "date -u" in joined


def test_a_past_timestamp_is_accepted():
    assert not _check(_stamp(-3600)).errors


def test_a_timestamp_inside_the_skew_tolerance_is_accepted():
    """Clock skew between a runner and this machine must not fail honest work."""
    assert not _check(_stamp(60)).errors
    assert not _check(_stamp(rv.COMPLETED_AT_FUTURE_TOLERANCE_SECONDS - 60)).errors


def test_the_tolerance_is_below_every_real_timezone_offset():
    """A tolerance of an hour or more would admit the UTC+1 version of the
    very bug this refuses. This is the bound that makes the check mean
    anything, so it is asserted rather than left to the comment."""
    assert rv.COMPLETED_AT_FUTURE_TOLERANCE_SECONDS < 3600
    assert _check(_stamp(3600)).errors, "a UTC+1 mislabel must still be refused"


def test_an_unparseable_timestamp_warns_rather_than_fails():
    """Bounding a value that parses is a separate decision from requiring it
    to parse; older receipts and fixtures carry shapes this would reject."""
    result = _check("last Tuesday")

    assert not result.errors
    assert result.warnings and "ISO 8601" in " ".join(result.warnings)


@pytest.mark.parametrize("raw", ["2099-01-01T00:00:00Z", "2090-01-01T00:00:00+00:00"])
def test_the_hard_coded_far_future_the_suite_used_is_now_refused(raw):
    """The suite itself reached the staleness gate through this hole.

    Nine test modules defined `FUTURE = "2090/2099-..."` to make a receipt
    fresh. That is the product defect used as scaffolding, so it has to fail
    now — otherwise the fix would be asserted by code that still relies on the
    behaviour it removes.
    """
    assert _check(raw).errors


def test_a_naive_timestamp_is_read_as_utc():
    assert _check((datetime.now(timezone.utc) + timedelta(seconds=7 * 3600))
                  .strftime("%Y-%m-%dT%H:%M:%S")).errors
