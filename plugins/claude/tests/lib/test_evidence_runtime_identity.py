"""Layer 1 -- runtime identity in the injected evidence contract (#342).

The kernel mints attempt identity at dispatch and validates it at advance
(#344). This is the third side of that triangle: telling the agent what to
stamp. Without it the kernel would be validating a field nothing produces.

The stanza is deliberately soft-required during the migration window, which
matches the kernel enforcing on mismatch rather than on absence. What an agent
loses by ignoring it is attempt-scoped evidence, which is the thing the pilot
exists to produce.
"""

from __future__ import annotations

import pytest

import evidence_contract as ec
import runtime_contracts as rc


pytestmark = pytest.mark.unit

ROLES = [
    "software-engineer",
    "quality-engineer",
    "code-reviewer",
    "technical-writer",
    "compliance-engineer",
]


@pytest.mark.parametrize("role", ROLES)
def test_every_dispatchable_role_is_told_to_stamp_identity(role):
    envelope = ec.render_envelope(role)
    assert "attempt_id" in envelope
    assert "dispatch_id" in envelope


@pytest.mark.parametrize("role", ROLES)
def test_the_existing_heading_is_untouched(role):
    """Three test files and three shipped host packages pin this string
    literally. The pilot's own object is the 'dispatch envelope'; renaming
    this one was explicitly rejected as the expensive direction."""
    envelope = ec.render_envelope(role)
    assert envelope.startswith("## Execution Envelope (evidence contract v")


def test_the_agent_is_told_to_copy_rather_than_derive():
    """A derived attempt id is worse than an absent one: it looks like
    evidence. The instruction has to say copy, in those words."""
    envelope = ec.render_envelope("software-engineer")
    assert "VERBATIM" in envelope
    assert "never derive or invent" in envelope


def test_the_spq_binding_fields_are_named():
    envelope = ec.render_envelope("quality-engineer")
    for field in ("cycle_id", "manifest_hash", "workstream_id"):
        assert field in envelope, field


def test_the_platform_overlay_fields_are_named():
    """Both vocabularies travel on every receipt so the same evidence reads
    as V1 roles or as Platform profiles (r4 amendment)."""
    envelope = ec.render_envelope("quality-engineer")
    for field in ("capability_profile", "stage_profile", "adapter_profile_id"):
        assert field in envelope, field


def test_failure_classification_is_required_on_a_failed_attempt():
    envelope = ec.render_envelope("quality-engineer")
    assert "failure_class" in envelope
    assert "unclassified" in envelope


def test_the_stanza_names_no_field_the_contract_module_rejects():
    """The instruction and the validator must not drift: anything the
    envelope tells an agent to stamp has to survive validation."""
    envelope = ec.render_envelope("software-engineer")
    receipt = {
        "attempt_id": "att_01J6ABCDEF",
        "dispatch_id": "a" * 32,
        "failure_class": "deterministic-check",
    }
    assert rc.validate_receipt_attempt_binding(receipt) == []
    for field in receipt:
        assert field in envelope, field


def test_render_never_raises_for_an_unknown_role():
    """The envelope is injected on every SubagentStart. A raise here would
    break dispatch for a role that simply has nothing to enforce."""
    assert ec.render_envelope("not-a-synaptory-agent") == ""
