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


class TestReceiptFixturesMatchTheProtocol:
    """The fixture directory is the cross-lane interface, so a fixture that
    teaches the wrong shape teaches it to every lane at once (#453).

    `receipt-protocol.md` tells agents to write `role` with the full role
    name, and `receipt_validator._DEFAULT_REQUIRED_FIELDS` requires it. These
    fixtures carried `agent` instead, which only ever read correctly because
    `story_pipeline` falls back to `agent` for #105 back-compat. Pin the key
    so the next fixture cannot reintroduce the drift.
    """

    RECEIPTS = (
        "receipt-evidence-replayed",
        "receipt-evidence-attested",
        "receipt-evidence-judged",
        "receipt-attempt-bound",
        "receipt-attempt-failed",
    )

    def test_every_receipt_fixture_names_its_role_the_way_agents_are_told_to(self):
        import json
        from pathlib import Path

        root = Path(__file__).resolve().parents[3] / "core" / "runtime-fixtures"
        for name in self.RECEIPTS:
            body = json.loads((root / (name + ".json")).read_text())
            assert "role" in body, (
                "%s carries no `role`: the protocol and the validator both "
                "require it, and `agent` is the control plane's storage key, "
                "not the on-disk one" % name
            )
            assert "agent" not in body, (
                "%s carries both keys; one shape per fixture or the next "
                "reader picks the wrong one" % name
            )
            assert "-" in body["role"], (
                "%s uses an abbreviation; the protocol says write the full "
                "role name and keep the abbreviation for the filename" % name
            )


#: The two fixtures the Go bridge unmarshals into `AttemptReceipt` and
#: re-marshals byte-for-byte (`TestFixtureRemarshalIsByteIdentical`).
_ATTEMPT_RECEIPT_FIXTURES = ("receipt-attempt-bound.json", "receipt-attempt-failed.json")


def _go_attempt_receipt_key_order() -> list:
    """The `AttemptReceipt` json tags, in declaration order, READ from the Go.

    Derived rather than listed. A hand-kept copy of the field order would be a
    second source of truth for the very thing this asserts is single, which is
    the census defect #616 closed one level up.
    """
    import re

    src = (
        Path(__file__).resolve().parents[3]
        / "cli" / "internal" / "cli" / "runtime_contract.go"
    ).read_text(encoding="utf-8")
    body = src.split("type AttemptReceipt struct {", 1)
    assert len(body) == 2, "AttemptReceipt is no longer declared as a struct literal"
    body = body[1].split("\n}", 1)[0]
    return [m.group(1) for m in re.finditer(r'json:"([^",]+)', body)]


def test_the_attempt_receipt_fixtures_match_the_go_wire_order():
    """Asserts impossible: a fixture the release gate rejects reaching `dev`.

    `TestFixtureRemarshalIsByteIdentical` in the Go suite requires these
    fixtures to re-marshal byte-for-byte, so their KEY ORDER is part of the
    contract, not formatting. That suite does not run on pull requests. It runs
    in `.github/workflows/release.yml`, so a fixture whose key order drifts
    passes every PR check and fails the release.

    That is exactly what happened: #620 added `fencing_token` to these fixtures
    beside `placement`, where it reads well, while the Go struct declares it
    after `source_revision`. Seven of seven PR checks were green and the
    release gate was deterministically red (#592 re-review at `fb54e2d6`).

    This test puts the assertion in a suite that DOES run on pull requests, and
    reads the order out of the Go source so the two cannot drift apart again.
    """
    expected = _go_attempt_receipt_key_order()
    assert expected, "no json tags parsed out of AttemptReceipt"
    for name in _ATTEMPT_RECEIPT_FIXTURES:
        actual = list(_load(name).keys())
        unknown = [k for k in actual if k not in expected]
        assert not unknown, (
            "%s carries keys AttemptReceipt does not declare: %s. The Go bridge "
            "drops them on re-marshal, so the release gate's byte-identity "
            "check fails." % (name, unknown)
        )
        ordered = [k for k in expected if k in actual]
        assert actual == ordered, (
            "%s orders its keys %s, but AttemptReceipt declares them %s. The "
            "release gate re-marshals these fixtures and compares bytes, so "
            "the order is contract. Reorder the fixture to match the struct, "
            "or move the field in `cli/internal/cli/runtime_contract.go`."
            % (name, actual, ordered)
        )
