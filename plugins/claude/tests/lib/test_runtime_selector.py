"""Layer 1 -- the deterministic runtime selector (#343, Epic #339).

The selector is a pure function over policy, request, registry and
availability. That is what makes the conformance row "same policy plus
availability snapshot produces the same selection and explanation" provable
rather than hopeful, so these tests lean on it hard.

Five properties are pinned:

1. determinism, asserted by repetition rather than by inspection,
2. preference never widens authority: `allowed_profiles` is the ceiling for
   every mode, so a preference or a pin outside it is refused,
3. no silent degradation: every path out is a selection with reasons, an
   explicit denial, or an inert result that says it is inert (EP-12),
4. inert on scrum and kanban, because the pilot must not depend on either,
5. capability profile is the policy key, so a prover-only runtime never wins
   a producer dispatch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import runtime_selector as rs


pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[3] / "core" / "runtime-fixtures"

POLICY = """build_mode: spq
runtimes:
  version: 1
  enabled: true
  allowed_profiles:
    - "claude-local-v1"
    - "codex-local-v1"
    - "cursor-local-readonly-v1"
  preferences:
    prover:
      - "codex-local-v1"
"""


@pytest.fixture
def profiles():
    return json.loads((FIXTURES / "profiles.json").read_text())


@pytest.fixture
def policy():
    parsed = rs.parse_policy(POLICY)
    assert parsed.enabled and not parsed.problems, parsed.problems
    return parsed


def _all_available(profiles):
    return [
        rs.Availability(profile_id=p["profile_id"], available=True) for p in profiles
    ]


def _request(role: str, **kwargs):
    kwargs.setdefault("mode", "auto")
    return rs.SelectionRequest(
        role=role,
        capability_profile=rs.capability_profile_for_role(role),
        **kwargs,
    )


class TestDeterminism:
    def test_the_same_snapshot_yields_an_identical_result_every_time(
        self, policy, profiles
    ):
        avail = _all_available(profiles)
        results = [
            rs.select_runtime(
                build_mode="spq",
                policy=policy,
                request=_request("qe"),
                profiles=profiles,
                availability=avail,
            ).to_dict()
            for _ in range(25)
        ]
        assert all(r == results[0] for r in results)

    def test_the_explanation_is_deterministic_too(self, policy, profiles):
        """The explanation is a product surface (8.1), not a debugging aid, so
        an unstable one is as much a defect as an unstable selection."""
        avail = _all_available(profiles)
        explains = {
            rs.select_runtime(
                build_mode="spq",
                policy=policy,
                request=_request("qe"),
                profiles=profiles,
                availability=avail,
            ).explain()
            for _ in range(10)
        }
        assert len(explains) == 1

    def test_registry_order_does_not_change_the_selection(self, policy, profiles):
        """Ordering by an explicit total order rather than by input order is
        what makes determinism structural."""
        avail = _all_available(profiles)
        forward = rs.select_runtime(
            build_mode="spq",
            policy=policy,
            request=_request("qe"),
            profiles=profiles,
            availability=avail,
        )
        reverse = rs.select_runtime(
            build_mode="spq",
            policy=policy,
            request=_request("qe"),
            profiles=list(reversed(profiles)),
            availability=list(reversed(avail)),
        )
        assert forward.selected == reverse.selected


class TestAuthorityIsNeverWidened:
    def test_a_pin_outside_the_allowlist_is_refused(self, policy, profiles):
        result = rs.select_runtime(
            build_mode="spq",
            policy=policy,
            request=_request(
                "qe", mode="pin", pinned_profile="claude-managed-standard-v1"
            ),
            profiles=profiles,
            availability=_all_available(profiles),
        )
        assert result.denied
        assert result.selected is None
        assert "claude-managed-standard-v1" in result.denial.detail

    def test_a_preference_outside_the_allowlist_does_not_win(self, profiles):
        policy = rs.parse_policy(
            POLICY.replace(
                '    prover:\n      - "codex-local-v1"\n',
                '    prover:\n      - "claude-managed-standard-v1"\n',
            )
        )
        result = rs.select_runtime(
            build_mode="spq",
            policy=policy,
            request=_request("qe", mode="prefer"),
            profiles=profiles,
            availability=_all_available(profiles),
        )
        assert result.selected != "claude-managed-standard-v1"


class TestNoSilentDegradation:
    def test_nothing_available_is_an_explicit_denial(self, policy, profiles):
        result = rs.select_runtime(
            build_mode="spq",
            policy=policy,
            request=_request("se"),
            profiles=profiles,
            availability=[
                rs.Availability(profile_id=p["profile_id"], available=False)
                for p in profiles
            ],
        )
        assert result.denied
        assert result.denial.code
        assert result.denial.detail

    def test_every_outcome_is_selected_denied_or_inert(self, policy, profiles):
        """EP-12: unsupported requirements deny or reroute, never degrade
        silently. There is no fourth, quieter outcome."""
        for mode in ("auto", "prefer", "pin"):
            result = rs.select_runtime(
                build_mode="spq",
                policy=policy,
                request=_request("qe", mode=mode, pinned_profile="codex-local-v1"),
                profiles=profiles,
                availability=_all_available(profiles),
            )
            assert (result.selected is not None) or result.denied or result.inert

    def test_a_denial_carries_a_reason_a_human_can_act_on(self, policy, profiles):
        result = rs.select_runtime(
            build_mode="spq",
            policy=policy,
            request=_request("qe", mode="pin", pinned_profile="does-not-exist-v1"),
            profiles=profiles,
            availability=_all_available(profiles),
        )
        assert result.denied
        assert len(result.denial.detail) > 20


class TestARuntimeThatCannotNameItsModelIsNotSelectable:
    """#689 -- selection refuses, and for the reason doctor gave.

    The CLI probe reports a runtime that states no exact model id as
    unavailable, because every kernel-minted envelope leaves the model to
    policy and the receipt would then name none. Selection reads that report
    out of the availability snapshot, so no rule about models lives here: what
    is pinned is that an unavailable profile is refused and the probe's own
    sentence survives into the explanation an operator reads.

    Without this the #357 pilot's shape repeats: selected, ten minutes of work,
    then refused for evidence the runtime could never have produced.
    """

    #: Verbatim from `runtimeSpec.modelIdentityGap` plus the doctor sentence
    #: around it, shortened. The point of copying it is that the selector must
    #: carry the probe's words rather than inventing its own.
    PROBE_REASON = (
        "codex is installed and within the pin, but codex states no exact "
        "model id anywhere in its stream. Route this capability profile to a "
        "runtime that states its model, such as claude-local-v1 (#689)"
    )

    def _availability(self, profiles):
        return [
            rs.Availability(
                profile_id=p["profile_id"],
                available=p["profile_id"] != "codex-local-v1",
                detail=(
                    self.PROBE_REASON
                    if p["profile_id"] == "codex-local-v1"
                    else ""
                ),
            )
            for p in profiles
        ]

    def test_auto_never_lands_on_it(self, policy, profiles):
        result = rs.select_runtime(
            build_mode="spq",
            policy=policy,
            request=_request("se"),
            profiles=profiles,
            availability=self._availability(profiles),
        )
        assert result.selected != "codex-local-v1", result.explain()

    def test_a_pin_is_refused_with_the_probes_own_reason(self, policy, profiles):
        result = rs.select_runtime(
            build_mode="spq",
            policy=policy,
            request=_request("se", mode="pin", pinned_profile="codex-local-v1"),
            profiles=profiles,
            availability=self._availability(profiles),
        )
        assert result.denied, result.explain()
        rejections = dict(result.rejected)["codex-local-v1"]
        codes = [r.code for r in rejections]
        assert rs.REJECT_UNAVAILABLE in codes, codes
        detail = " ".join(r.detail for r in rejections)
        assert "no exact model id" in detail, detail
        assert "#689" in detail, detail
        # And the explanation an operator actually reads carries it too.
        assert "no exact model id" in result.explain()


class TestCapabilityProfileIsThePolicyKey:
    def test_a_prover_only_runtime_never_wins_a_producer_dispatch(
        self, policy, profiles
    ):
        """cursor-local-readonly-v1 serves prover only. A producer dispatch
        that landed on it would be a read-only agent asked to build."""
        result = rs.select_runtime(
            build_mode="spq",
            policy=policy,
            request=_request("se"),
            profiles=profiles,
            availability=_all_available(profiles),
        )
        assert result.selected != "cursor-local-readonly-v1"

    def test_every_dispatchable_role_maps_to_a_capability_profile(self):
        import runtime_contracts as rc

        for role in rc.DISPATCH_CAPABILITY_PROFILE:
            assert rs.capability_profile_for_role(role) in rc.CAPABILITY_PROFILES


class TestInertOutsideSpq:
    @pytest.mark.parametrize("mode", ["scrum", "kanban"])
    def test_the_selector_is_inert_and_says_so(self, policy, profiles, mode):
        """The pilot must not depend on Scrum or Kanban, and it must not
        change how they behave either."""
        result = rs.select_runtime(
            build_mode=mode,
            policy=policy,
            request=_request("qe"),
            profiles=profiles,
            availability=_all_available(profiles),
        )
        assert result.inert
        assert result.selected is None
        assert not result.denied

    def test_an_absent_runtimes_section_is_inert_rather_than_denying(self):
        """Leaving the section out is the supported state, so a project that
        has not opted in keeps the dispatch path it has today."""
        policy = rs.parse_policy("build_mode: spq\n")
        assert not policy.enabled
        result = rs.select_runtime(
            build_mode="spq",
            policy=policy,
            request=_request("qe"),
            profiles=[],
            availability=[],
        )
        assert result.inert
        assert not result.denied

    def test_an_unknown_policy_version_fails_closed(self):
        policy = rs.parse_policy(
            "build_mode: spq\nruntimes:\n  version: 99\n  enabled: true\n"
        )
        assert not policy.enabled
        assert policy.problems


class TestPurity:
    def test_selection_touches_no_clock_and_no_filesystem(self, policy, profiles):
        """Purity is what makes the determinism row provable. If this ever
        needs to read state, the conformance claim needs revisiting."""
        import inspect

        source = inspect.getsource(rs.select_runtime)
        for forbidden in ("open(", "datetime", "time.", "random", "Path("):
            assert forbidden not in source, forbidden
