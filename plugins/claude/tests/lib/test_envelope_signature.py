"""The dispatch envelope's signing helpers (#438, PR #396 finding 2).

The envelope is declared a signed, expiring authority ceiling
(`docs/proposals/governed-runtime-pilot.md` 4.4.1, ADR-031 §12). The signature
is applied by the CONTROL PLANE at attempt registration, not by the kernel that
assembles the envelope: a kernel-held key would sit on the same machine as
anyone who would tamper with the file, and a signature verifiable only against
the tamperer's own key authenticates nothing.

These cover the shared-runtime half: the payload that gets signed, the
verifier, and the tamper behaviour on the four authority fields the review
named. The control plane's own signing path is `api/tests/test_envelope_signing.py`
and the runner's enforcement is `cli/internal/cli/envelope_signature_test.go`.
"""
from __future__ import annotations

import copy

import pytest

from hooks.lib import runtime_contracts as rc

cryptography = pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)


class _Signer:
    """The control plane's `Signer` protocol, minimally."""

    def __init__(self, key: Ed25519PrivateKey) -> None:
        self._key = key

    def sign(self, payload: bytes) -> bytes:
        return self._key.sign(payload)


@pytest.fixture()
def keypair():
    key = Ed25519PrivateKey.generate()
    pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return _Signer(key), pem


@pytest.fixture()
def envelope():
    return rc.build_envelope(
        attempt_id="att_01J6ABCDEFGHJKMNPQRSTV",
        dispatch_id="a" * 32,
        project_id="taskflow",
        cycle_id="003-9f2c41ab",
        manifest_hash="sha256-of-sealed-manifest",
        workstream_id="ws-app",
        story_id="WU-142",
        stage="testing",
        role="qe",
        adapter_profile_id="claude-managed-standard-v1",
        runtime_family="claude-code",
        placement="managed-laptop",
        source_revision="b" * 40,
        receipt_path=".synaptory/.orchestrator/spq/receipts/WU-142-qe.json",
        expires_at="2126-08-23T13:00:00Z",
        fencing_token="1",
        capability_ceiling=["workspace.write", "process.test"],
        # An ampersand and a non-ASCII character, because #189 proved these are
        # exactly where a canonicalization disagreement between Go and Python
        # shows up, and a signature that only works on plain ASCII is a
        # signature that fails in the field.
        materialization={
            "repo_url": "https://git.example/org/app.git?a=1&b=2",
            "ref": "refs/heads/main",
            "revision": "b" * 40,
            "worktree_mode": "clean-isolated",
            "note": "café < bar",
        },
        connector_refs={"vcs": "cred://runner/github-vcs-read"},
    )


def test_a_signed_envelope_verifies(keypair, envelope) -> None:
    signer, pem = keypair
    signed = rc.sign_envelope(envelope, signer, "cp-signing-key")
    assert signed["signature"]["alg"] == "ed25519"
    assert rc.verify_envelope_signature(signed, pem) == []


@pytest.mark.parametrize(
    "field,tampered",
    [
        ("capability_ceiling", ["workspace.write", "process.test", "admin.all"]),
        ("receipt_path", ".synaptory/.orchestrator/spq/receipts/WU-999-qe.json"),
        ("source_revision", "d" * 40),
        ("connector_refs", {"vcs": "cred://runner/attacker-controlled"}),
        # #592. Both were added as kernel-minted FACTS the bridge then trusts,
        # and both were unsigned. Emptying `receipt_required_fields` disables
        # the bridge's completion check entirely; changing `role_name` changes
        # the identity it stamps onto the receipt. A fact a caller can rewrite
        # after signing is not a fact.
        ("receipt_required_fields", []),
        ("role_name", "software-engineer"),
    ],
)
def test_editing_one_authority_field_breaks_the_signature(
    keypair, envelope, field, tampered
) -> None:
    """The four fields the review named, one at a time, so a green run says
    which field is actually covered."""
    signer, pem = keypair
    signed = rc.sign_envelope(envelope, signer, "cp-signing-key")
    assert rc.verify_envelope_signature(signed, pem) == []  # control

    forged = copy.deepcopy(signed)
    forged[field] = tampered
    assert forged[field] != signed[field], "the tamper value equals the signed value"
    assert rc.verify_envelope_signature(forged, pem) != []


def test_the_fencing_token_may_move_without_breaking_the_signature(
    keypair, envelope
) -> None:
    """Deliberately outside the signed body: it is lease-issued, re-issued on
    every claim, and rewritten in the worker's memory. Signing it would break
    the NORMAL path, which is a worse failure than the one signing prevents.
    It is bound by indirection through `attempt_id` and the claim response."""
    signer, pem = keypair
    signed = rc.sign_envelope(envelope, signer, "cp-signing-key")
    moved = copy.deepcopy(signed)
    moved["fencing_token"] = "7"
    assert rc.verify_envelope_signature(moved, pem) == []


def test_an_added_field_breaks_the_signature(keypair, envelope) -> None:
    """A missing field is signed as ABSENT rather than as null, so an envelope
    cannot grow an authority field after signing."""
    signer, pem = keypair
    signed = rc.sign_envelope(envelope, signer, "cp-signing-key")
    grown = copy.deepcopy(signed)
    grown["config_refs"] = {"preset_base": "attacker-preset"}
    assert grown["config_refs"] != signed["config_refs"]
    assert rc.verify_envelope_signature(grown, pem) != []


def test_an_unsigned_envelope_does_not_verify(keypair, envelope) -> None:
    _, pem = keypair
    assert rc.verify_envelope_signature(envelope, pem) != []


def test_a_signature_from_another_key_does_not_verify(keypair, envelope) -> None:
    signer, _ = keypair
    other = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    signed = rc.sign_envelope(envelope, signer, "cp-signing-key")
    assert rc.verify_envelope_signature(signed, other) != []


def test_the_fencing_token_is_not_in_the_signed_set() -> None:
    """Stated as its own assertion because it is the one exclusion someone
    would 'fix' without realising it breaks every re-fenced claim."""
    assert "fencing_token" not in rc.ENVELOPE_SIGNED_FIELDS
    assert "signature" not in rc.ENVELOPE_SIGNED_FIELDS


def test_every_authority_field_the_review_named_is_signed() -> None:
    for field in (
        "capability_ceiling",
        "receipt_path",
        "source_revision",
        "connector_refs",
        "receipt_required_fields",
        "role_name",
    ):
        assert field in rc.ENVELOPE_SIGNED_FIELDS


def test_a_field_the_bridge_acts_on_is_signed() -> None:
    """Asserts impossible: a new envelope field trusted but unauthenticated.

    The rule this encodes, from #592: any field a consumer READS TO DECIDE
    something belongs in the signed set, and adding one without signing it is
    the defect rather than an omission to tidy later. `role_name` decides the
    identity the bridge stamps; `receipt_required_fields` decides whether the
    bridge may report completion at all. Both were added unsigned, and emptying
    the second silently disabled the check it exists for.

    Stated as a property over the fields the bridge acts on rather than as a
    second hand-kept list, so the next such field is caught by the rule instead
    of by whoever remembers this one.
    """
    acted_on = {
        "role_name": "the identity the bridge stamps onto a receipt",
        "receipt_required_fields": "whether the bridge may report completion",
        "receipt_path": "where the bridge files the receipt",
        "capability_ceiling": "what the runtime is permitted to do",
    }
    unsigned = sorted(
        "%s (%s)" % (field, why)
        for field, why in acted_on.items()
        if field not in rc.ENVELOPE_SIGNED_FIELDS
    )
    assert unsigned == [], (
        "these envelope fields are read to make a decision but are not "
        "authenticated, so a caller can rewrite them after signing:\n  %s"
        % "\n  ".join(unsigned)
    )
