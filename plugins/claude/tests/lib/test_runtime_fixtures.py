"""Layer 1 -- the cross-lane fixtures in `core/runtime-fixtures/` are valid.

The fixtures are the interface four lanes code against (#341). A fixture that
drifts from `runtime_contracts` would hand one lane a shape another lane
refuses, and the disagreement would not surface until Phase D integration.
This module is what stops that: every fixture is validated on every run, so a
contract change either updates the fixtures or fails here.

It also pins the two properties a reader of the fixtures is entitled to
assume: the managed envelope really is independently materializable, and no
fixture carries credential material.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import runtime_contracts as rc


pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[3] / "core" / "runtime-fixtures"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


def test_the_fixture_directory_is_where_the_readme_says() -> None:
    assert FIXTURES.is_dir(), "expected fixtures at %s" % FIXTURES
    assert (FIXTURES / "README.md").is_file()


@pytest.mark.parametrize("name", ["envelope-local-qe.json", "envelope-managed-se.json"])
def test_envelope_fixtures_validate(name: str) -> None:
    assert rc.validate_envelope(_load(name)) == []


def test_supervision_fixture_covers_every_sub_kind_in_sequence() -> None:
    lines = (FIXTURES / "supervision-events.jsonl").read_text().strip().splitlines()
    events = [json.loads(line) for line in lines]
    for event in events:
        assert rc.validate_supervision_event(event) == [], event
    assert {e["kind"] for e in events} == set(rc.SUPERVISION_EVENT_KINDS), (
        "the fixture must exercise all seven sub-kinds, so a consumer that "
        "handles only the common ones fails here rather than in the Portal"
    )
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)
    assert len({e["attempt_id"] for e in events}) == 1


def test_profile_fixtures_validate_and_cover_both_placements() -> None:
    profiles = _load("profiles.json")
    for profile in profiles:
        assert rc.validate_profile(profile) == [], profile
    assert {p["placement"] for p in profiles} == set(rc.PLACEMENTS)
    assert {p["runtime_family"] for p in profiles} == set(rc.RUNTIME_FAMILIES)


def test_receipt_fixtures_carry_attempt_identity() -> None:
    for name in ("receipt-attempt-bound.json", "receipt-attempt-failed.json"):
        assert rc.validate_receipt_attempt_binding(_load(name)) == [], name


def test_the_failed_receipt_is_classified() -> None:
    """A failed attempt that closes unclassified is exactly what SP-INT-017
    forbids, so the fixture must model the classified case."""
    failed = _load("receipt-attempt-failed.json")
    assert failed["failure_class"] in rc.FAILURE_CLASSES
    assert failed["attempt_id"] != _load("receipt-attempt-bound.json")["attempt_id"], (
        "a retry is a NEW attempt; a fixture that reused the id would teach "
        "the opposite of the model"
    )


def test_the_managed_envelope_is_independently_materializable() -> None:
    env = _load("envelope-managed-se.json")
    assert env["placement"] == "managed-laptop"
    for field in ("repo_url", "ref", "revision"):
        assert env["materialization"][field]
    assert env["source_revision"] == env["materialization"]["revision"], (
        "the runner checks out materialization.revision; if it disagreed with "
        "source_revision the receipt would attest to a different tree than the "
        "one the kernel dispatched against"
    )


def test_no_fixture_carries_credential_material() -> None:
    for path in sorted(FIXTURES.glob("*.json")) + sorted(FIXTURES.glob("*.jsonl")):
        raw = path.read_text()
        for marker in ("ghp_", "sk-", "Bearer ", "BEGIN PRIVATE KEY", "password"):
            assert marker not in raw, "%s looks like it carries a secret (%r)" % (
                path.name,
                marker,
            )


def test_every_connector_ref_is_a_reference() -> None:
    env = _load("envelope-managed-se.json")
    for name, ref in env["connector_refs"].items():
        assert ref is None or ref.startswith("cred://"), (name, ref)
