"""Layer 1 — the integration seat can run the Sync ceremony it owns (#330, from #323).

G7: the workstream declared `integration: true` owns the barrier but could not
reach the state the barrier requires.

  hydrate_cycle   refused it (`empty_projection`: "owns no admitted Work Unit")
  therefore       it never entered CYCLE_EXECUTION, so never SYNC
  but             spq/sync.md and `clear_sync` both require SYNC

So the seat could `evaluate` and never `clear`. Completing a Cycle meant
transitioning a DELIVERY clone to SYNC and clearing from there, which is the
inverse of why the seat exists: no single delivery lane should be able to clear
its own integration.

The rest of the lifecycle already treated an empty board on this seat as
legitimate -- `transition` to SYNC, `open_cycle`, `next_action` and
`_integration_readiness_rollup` all handle it explicitly -- so this is
`hydrate_cycle` being made consistent with the design, not a new one.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import spq_state_machine as m


pytestmark = pytest.mark.unit

_LANES = (
    '    - id: "api"\n      shared_owner: true\n'
    '    - id: "web"\n'
    # Declared but admitted NOTHING into Cycle 1. This is the delivery-lane
    # control for the integration exemption: same empty projection, but the
    # refusal must still fire because the rationale still applies to it.
    '    - id: "docs"\n'
    '    - id: "integration"\n      integration: true\n'
)


def _repo(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    project.mkdir(parents=True)
    for args in (["init", "-q"], ["config", "user.email", "t@e.co"],
                 ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)
    (project / ".gitignore").write_text(
        ".synaptory/*\n!.synaptory/sync/\n!.synaptory/cycles/\n", encoding="utf-8")
    (project / ".synaptory.yaml").write_text(
        'build_mode: "spq"\nspq:\n  workstreams:\n' + _LANES, encoding="utf-8")
    (project / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=project, check=True,
                   capture_output=True)
    return project


_UNITS = [
    {"id": "WU-API-1", "title": "a", "owner_workstream": "api"},
    {"id": "WU-WEB-1", "title": "w", "owner_workstream": "web"},
]


def _opened(tmp_path: Path) -> Path:
    """A project with Cycle 1 open, sealed by the api lane."""
    project = _repo(tmp_path)
    m.initialize(str(project))
    m.transition(str(project), "COMMIT", force=True)
    m.open_cycle(str(project), 1, "goal", _UNITS, workstream_id="api")
    return project


# ─── The predicate, in isolation ────────────────────────────────────────────


def test_declared_integration_is_read_from_config_not_state(tmp_path):
    """It must answer during hydration, when state does not exist yet.

    `_is_integration_seat` resolves through the execution state, which is the
    thing hydration has not written -- so the seat could not be recognised at
    the one moment recognition mattered.
    """
    project = _repo(tmp_path)
    assert m._is_declared_integration(str(project), "integration") is True
    assert m._is_declared_integration(str(project), "api") is False
    assert m._is_declared_integration(str(project), "nonexistent") is False
    assert m._is_declared_integration(str(project), None) is False


def test_declared_integration_survives_an_unreadable_config(tmp_path):
    """An unreadable config is not an integration seat, and must not raise."""
    project = tmp_path / "bare"
    project.mkdir()
    assert m._is_declared_integration(str(project), "integration") is False


# ─── G7: the seat reaches SYNC ──────────────────────────────────────────────


def test_integration_seat_can_hydrate_with_an_empty_board(tmp_path):
    """Pre-fix this raised HydrationRefusal('empty_projection')."""
    project = _opened(tmp_path)

    m.hydrate_cycle(str(project), 1, [], goal="goal", workstream_id="integration")

    state = m.read_state(str(project))
    assert state["lifecycle_state"] == "CYCLE_EXECUTION"
    assert state.get("current_stories") in ([], None), (
        "the integration seat must hold no Work Units; they live on delivery lanes"
    )


def test_integration_seat_reaches_sync_and_can_clear(tmp_path):
    """The end-to-end shape of G7: the seat holds SYNC, so `clear_sync` is legal.

    Asserted through `transition` rather than the full barrier, because the
    defect was reaching the STATE, not evaluating the criteria. `clear_sync`
    itself is exercised only far enough to prove the lifecycle no longer blocks
    it -- the barrier verdict is `sync_barrier`'s own test surface.
    """
    project = _opened(tmp_path)
    m.hydrate_cycle(str(project), 1, [], goal="goal", workstream_id="integration")

    m.transition(str(project), "SYNC")
    assert m.read_state(str(project))["lifecycle_state"] == "SYNC", (
        "the barrier seat cannot reach the state its own ceremony requires"
    )


def test_a_delivery_lane_with_no_units_is_still_refused(tmp_path):
    """The exemption must be for the integration seat ONLY.

    The refusal's rationale still holds for a delivery lane: an empty Cycle
    would let it declare readiness having delivered nothing. Widening the
    exemption to every empty board would remove the check entirely.
    """
    project = _opened(tmp_path)

    # `docs` is declared in .synaptory.yaml but owns no admitted unit in Cycle
    # 1, so its projection is empty for the same reason the integration seat's
    # is. Passing `web` here would prove nothing: the sealed manifest supplies
    # web's real projection, so it is never empty regardless of the fix.
    with pytest.raises((m.HydrationRefusal, ValueError)) as exc:
        m.hydrate_cycle(str(project), 1, [], goal="goal", workstream_id="docs")
    message = str(exc.value)
    assert "owns no admitted Work Unit" in message, message


def test_integration_seat_next_action_is_await_sync(tmp_path):
    """It must never dispatch. An empty board plus `dispatch_se` would be worse
    than the refusal this replaces."""
    project = _opened(tmp_path)
    m.hydrate_cycle(str(project), 1, [], goal="goal", workstream_id="integration")

    action = m.next_action(str(project))
    assert action["action"] == "await_sync", action
    assert action.get("story_id") is None


# ─── The secondary claim in #330, checked rather than assumed ───────────────


def test_clear_sync_from_the_wrong_state_is_loud(tmp_path):
    """#330 reported `clear_sync` leaving a seat in COMMIT "without a loud
    refusal". At the library and CLI layers it is loud, and this pins that.

    Verified rather than fixed: `clear_sync` raises, and `main()` turns any
    exception into `{"error": ...}` on stderr with exit 1. If an operator saw
    silence, it was a ceremony or wrapper discarding stderr, not this code --
    so a speculative "fix" here would have changed nothing and hidden the real
    cause.
    """
    project = _opened(tmp_path)
    with pytest.raises(ValueError, match="expected SYNC"):
        m.clear_sync(str(project), 1, cleared_by="lead@h3t.co")

    result = subprocess.run(
        [sys.executable,
         str(Path(m.__file__).resolve()),
         "clear_sync", str(project), "1", "--cleared-by", "lead@h3t.co"],
        capture_output=True, text=True, check=False,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": ":".join(sys.path)},
    )
    assert result.returncode != 0, "a wrong-state clear_sync exited 0"
    assert "expected SYNC" in (result.stderr + result.stdout), (
        "the refusal did not reach the operator: %r" % result.stderr
    )
