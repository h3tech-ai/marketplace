"""Layer 1 — the three method events record, and decide nothing (#644).

`C-02`'s mechanical form. Every test is one shape of "appending this changes no
outcome", because the predecessor made all three events states and the most
plausible half-fix — keeping them as optional states — would preserve the second
approval path and lose only the ordering.
"""

from __future__ import annotations

import pytest

import method_events as me


def _commit(cycle="007-aaaa1111", **over):
    fields = {"cycle_id": cycle, "declaration_hash": "sha256:" + "a" * 64,
              "admitted_unit_ids": ["WU-01", "WU-02"]}
    fields.update(over)
    return me.build("commit", **fields)


def _sync(cycle="007-aaaa1111", **over):
    fields = {"cycle_id": cycle, "waiting_unit_id": "WU-02",
              "producing_cycle_id": "008-bbbb2222", "resolution": "published"}
    fields.update(over)
    return me.build("sync", **fields)


def _checkpoint(cycle="007-aaaa1111", **over):
    fields = {"cycle_id": cycle, "integrated_sha": "c" * 40,
              "trunk_ref": "refs/heads/dev"}
    fields.update(over)
    return me.build("checkpoint", **fields)


def test_there_are_exactly_three_events():
    """A fourth would be a stage in disguise. §1.1: a term not in the method's
    vocabulary is not part of the method."""
    assert me.EVENT_KINDS == ("commit", "sync", "checkpoint")


@pytest.mark.parametrize("kind", ["cycle_execution", "review", "retro",
                                  "coordination", "release", "", None])
def test_anything_else_is_refused(kind):
    with pytest.raises(me.MethodEventError) as excinfo:
        me.build(kind, cycle_id="007-aaaa1111")
    assert "not a method event" in str(excinfo.value)


def test_this_module_cannot_decide_anything():
    """The structural claim, asserted as an absence.

    A module with no transition verb and no state file cannot gate. If any of
    these appears, the events have acquired the power `C-02` denies them.
    """
    for verb in ("transition", "advance", "gate", "approve", "close", "block",
                 "read_state", "write_state", "next_action"):
        assert not hasattr(me, verb), verb


@pytest.mark.parametrize("kind,fields", [
    ("commit", {"declaration_hash": "sha256:x", "admitted_unit_ids": ["WU-01"]}),
    ("commit", {"cycle_id": "c", "admitted_unit_ids": ["WU-01"]}),
    ("commit", {"cycle_id": "c", "declaration_hash": "sha256:x"}),
    ("sync", {"cycle_id": "c", "waiting_unit_id": "WU-02", "resolution": "published"}),
    ("checkpoint", {"cycle_id": "c", "trunk_ref": "refs/heads/dev"}),
    ("checkpoint", {"cycle_id": "c", "integrated_sha": "c" * 40}),
])
def test_an_event_that_records_less_than_its_fields_is_refused(kind, fields):
    """An event recording less than this is not evidence of anything, and the
    point of the three is that they leave a record rather than a decision."""
    with pytest.raises(me.MethodEventError):
        me.build(kind, **fields)


def test_a_sync_must_name_how_it_cleared():
    """§6: Sync is the answer to a dependency, not a ceremony with a slot. A
    Sync that named no resolution would be the ceremony."""
    with pytest.raises(me.MethodEventError) as excinfo:
        _sync(resolution="somehow")
    assert "cleared somehow" in str(excinfo.value)


@pytest.mark.parametrize("resolution", list(me.SYNC_RESOLUTIONS))
def test_every_declared_resolution_is_accepted(resolution):
    assert _sync(resolution=resolution)["resolution"] == resolution


# ── The absence of a Sync blocks nothing ─────────────────────────────────────

def test_a_cycle_with_no_sync_event_is_the_common_case():
    """The largest behavioural change in the epic. In the predecessor `SYNC`
    was the human gate and the loop engine was forbidden to drive past it;
    §1.1 says Sync happens "several times or not at all"."""
    ledger = me.append([], _commit())
    assert me.sync_count(ledger) == 0
    assert me.latest(ledger, "sync") is None


def test_latest_returns_none_as_a_real_answer():
    """A caller must treat `None` as an answer, not as a broken read: a Cycle
    with no Sync is correct."""
    assert me.latest([], "sync") is None
    assert me.latest([], "checkpoint") is None


def test_many_syncs_are_as_valid_as_none():
    ledger = me.append([], _commit())
    for unit in ("WU-02", "WU-03", "WU-04"):
        ledger = me.append(ledger, _sync(waiting_unit_id=unit))
    assert me.sync_count(ledger) == 3


# ── Append-only ──────────────────────────────────────────────────────────────

def test_the_ledger_is_append_only_and_returns_a_new_list():
    original = me.append([], _commit())
    extended = me.append(original, _sync())
    assert len(original) == 1, "the input list was mutated"
    assert len(extended) == 2


def test_a_duplicate_event_is_refused_rather_than_deduplicated():
    """Two identical events are either a replay or a caller that lost track of
    what it recorded. Silently deduplicating hides both."""
    event = _commit()
    ledger = me.append([], event)
    with pytest.raises(me.MethodEventError) as excinfo:
        me.append(ledger, event)
    assert "replay" in str(excinfo.value)


def test_an_event_without_a_digest_cannot_be_appended():
    with pytest.raises(me.MethodEventError):
        me.append([], {"kind": "commit", "cycle_id": "c"})


def test_the_digest_covers_the_record():
    a = _commit(cycle="007-aaaa1111")
    b = _commit(cycle="008-bbbb2222")
    assert a["event_digest"] != b["event_digest"]


# ── Identity, not position ───────────────────────────────────────────────────

def test_events_are_selected_by_identity_not_by_index():
    """A Cycle's second Sync is the one naming a particular dependency, not
    "index 1" — positional identity is how a replayed event silently becomes a
    different one."""
    ledger = me.append([], _commit(cycle="007-aaaa1111"))
    ledger = me.append(ledger, _commit(cycle="008-bbbb2222"))
    ledger = me.append(ledger, _sync(cycle="008-bbbb2222"))
    assert [e["cycle_id"] for e in me.of_kind(ledger, "commit")] == [
        "007-aaaa1111", "008-bbbb2222"]
    assert me.of_kind(ledger, "sync", "007-aaaa1111") == []
    assert len(me.of_kind(ledger, "sync", "008-bbbb2222")) == 1


def test_the_checkpoint_event_records_the_integration_it_did_not_permit():
    """`M-03`: the barrier is the mechanical condition, the event is the record.
    The test for whether they stayed apart: deleting this record must not change
    whether a Cycle may close."""
    event = _checkpoint()
    assert event["integrated_sha"] == "c" * 40
    assert event["trunk_ref"] == "refs/heads/dev"
    assert "outcome" not in event and "permitted" not in event
