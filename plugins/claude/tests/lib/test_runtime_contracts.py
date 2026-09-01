"""Layer 1 -- `runtime_contracts`: the dispatch envelope, supervision events,
adapter profiles, and receipt attempt binding (#341, Epic #339).

These pin the properties the rest of the pilot is allowed to assume:

1. an envelope carries its SPQ binding (cycle, manifest hash, workstream), so
   an attempt cannot be validated against a Cycle it was never admitted to,
2. a managed-placement envelope is independently materializable, because a
   runner has no host checkout to fall back on,
3. no secret can travel in an envelope: connector refs are cred:// or nothing,
4. the supervision payload allowlist is CLOSED, so content the privacy table
   says never leaves the runtime has no field to travel in (SP-SEC-037),
5. attempt identity lives in the receipt payload, never in the filename.
"""

from __future__ import annotations

import pytest

import runtime_contracts as rc


pytestmark = pytest.mark.unit


def _envelope(**overrides):
    base = dict(
        attempt_id="att_01J6ABCDEF",
        dispatch_id="a" * 32,
        project_id="taskflow",
        cycle_id="003-9f2c41ab",
        manifest_hash="sha256:" + "b" * 64,
        workstream_id="api",
        story_id="WU-142",
        stage="testing",
        role="qe",
        adapter_profile_id="codex-local-v1",
        runtime_family="codex",
        placement="local",
        source_revision="c" * 40,
        receipt_path=".synaptory/.orchestrator/spq/cycles/003-9f2c41ab/workstreams/api/receipts/WU-142-qe.json",
        expires_at="2026-09-01T13:00:00Z",
        fencing_token="tok_01J6ABC",
    )
    base.update(overrides)
    return rc.build_envelope(**base)


class TestEnvelopeShape:
    def test_a_built_envelope_validates(self):
        assert rc.validate_envelope(_envelope()) == []

    def test_every_required_field_is_actually_required(self):
        for field in rc._ENVELOPE_REQUIRED_STR:
            env = _envelope()
            env[field] = ""
            problems = rc.validate_envelope(env)
            assert any(p.startswith(field + ":") for p in problems), (
                "blanking %s produced no problem naming it: %r" % (field, problems)
            )

    def test_spq_binding_is_not_optional(self):
        """An attempt with no Cycle binding cannot be checked against the
        sealed manifest, so the envelope must never be well-formed without
        one."""
        for field in ("cycle_id", "manifest_hash", "workstream_id"):
            env = _envelope()
            del env[field]
            assert any(p.startswith(field + ":") for p in rc.validate_envelope(env))

    def test_platform_overlay_defaults_from_the_role(self):
        """A caller cannot ship an envelope with the Platform vocabulary
        silently absent: both fields default from the dispatched role."""
        assert _envelope(role="se")["capability_profile"] == "producer"
        assert _envelope(role="se")["stage_profile"] == "producing"
        assert _envelope(role="qe")["capability_profile"] == "prover"
        assert _envelope(role="cr")["stage_profile"] == "verifying"

    def test_every_dispatchable_role_projects_onto_both_vocabularies(self):
        assert set(rc.DISPATCH_CAPABILITY_PROFILE) == set(rc.DISPATCH_STAGE_PROFILE)
        for role, profile in rc.DISPATCH_CAPABILITY_PROFILE.items():
            assert profile in rc.CAPABILITY_PROFILES, role
        for role, profile in rc.DISPATCH_STAGE_PROFILE.items():
            assert profile in rc.STAGE_PROFILES, role

    def test_platform_wave_placements_are_refused(self):
        """managed-server and provider-managed are Platform placements. The
        pilot must refuse them rather than forward something it cannot
        isolate (proposal 3.4)."""
        for placement in ("managed-server", "provider-managed", "kubernetes"):
            problems = rc.validate_envelope(_envelope(placement=placement))
            assert any(p.startswith("placement:") for p in problems)

    def test_attempt_and_dispatch_ids_are_shape_checked(self):
        assert any(
            p.startswith("attempt_id:")
            for p in rc.validate_envelope(_envelope(attempt_id="att_x"))
        )
        assert any(
            p.startswith("attempt_id:")
            for p in rc.validate_envelope(_envelope(attempt_id="US-142"))
        )
        assert any(
            p.startswith("dispatch_id:")
            for p in rc.validate_envelope(_envelope(dispatch_id="not-hex"))
        )


class TestManagedPlacementMaterialization:
    def test_managed_placement_requires_materialization_and_connectors(self):
        problems = rc.validate_envelope(
            _envelope(placement="managed-laptop", runtime_family="claude-code")
        )
        assert any(p.startswith("materialization:") for p in problems)
        assert any(p.startswith("connector_refs:") for p in problems)

    def test_managed_placement_requires_an_exact_revision(self):
        env = _envelope(
            placement="managed-laptop",
            runtime_family="claude-code",
            materialization={"repo_url": "https://example.invalid/r.git", "ref": "refs/heads/main"},
            connector_refs={"vcs": "cred://runner/vcs-read"},
        )
        assert any(p.startswith("materialization.revision:") for p in rc.validate_envelope(env))

    def test_local_placement_may_omit_both(self):
        """The local host is already inside the clone with its own
        credentials, so requiring these would be ceremony."""
        assert rc.validate_envelope(_envelope(placement="local")) == []

    def test_a_managed_envelope_with_full_materialization_validates(self):
        env = _envelope(
            placement="managed-laptop",
            runtime_family="claude-code",
            adapter_profile_id="claude-managed-standard-v1",
            materialization={
                "repo_url": "https://example.invalid/taskflow.git",
                "ref": "refs/heads/main",
                "revision": "d" * 40,
                "worktree_mode": "clean-isolated",
            },
            connector_refs={"vcs": "cred://runner/vcs-read", "ci": None},
        )
        assert rc.validate_envelope(env) == []


class TestNoSecretsInTheEnvelope:
    @pytest.mark.parametrize(
        "value",
        [
            "ghp_realtokenmaterialhere",
            "Bearer eyJhbGciOi",
            "https://user:password@example.invalid/r.git",
            "",
        ],
    )
    def test_inline_credential_material_is_refused(self, value):
        """The envelope is an authority ceiling that crosses process
        boundaries and gets logged. A ref is legal; material is not."""
        env = _envelope(connector_refs={"vcs": value})
        assert any(p.startswith("connector_refs.vcs:") for p in rc.validate_envelope(env))

    def test_a_null_connector_is_fine(self):
        assert rc.validate_envelope(_envelope(connector_refs={"ci": None})) == []


class TestSupervisionEventPrivacy:
    def _event(self, **overrides):
        base = {
            "seq": 1,
            "ts": "2026-09-01T12:00:01Z",
            "kind": "tool/call",
            "attempt_id": "att_01J6ABCDEF",
            "payload": {"tool": "bash", "summary": "git status", "duration_ms": 1200},
            "privacy_safe": True,
        }
        base.update(overrides)
        return base

    def test_a_well_formed_event_validates(self):
        assert rc.validate_supervision_event(self._event()) == []

    def test_all_seven_sub_kinds_are_declared(self):
        assert len(rc.SUPERVISION_EVENT_KINDS) == 7

    def test_an_unknown_kind_is_refused(self):
        problems = rc.validate_supervision_event(self._event(kind="model/thinking"))
        assert any(p.startswith("kind:") for p in problems)

    @pytest.mark.parametrize(
        "leak",
        [
            {"tool": "bash", "stdout": "secret output"},
            {"tool": "bash", "reasoning": "I should try..."},
            {"tool": "bash", "prompt": "system prompt text"},
            {"tool": "bash", "content": "file body"},
        ],
    )
    def test_content_has_no_field_to_travel_in(self, leak):
        """SP-SEC-037 in code form: the allowlist is closed, so a leaky
        adapter fails here rather than shipping content to the CP."""
        problems = rc.validate_supervision_event(self._event(payload=leak))
        assert any(p.startswith("payload:") for p in problems)

    def test_file_write_carries_path_and_size_only(self):
        ok = self._event(kind="file/write", payload={"path": "src/a.py", "size_bytes": 12})
        assert rc.validate_supervision_event(ok) == []
        leaky = self._event(kind="file/write", payload={"path": "src/a.py", "diff": "+x"})
        assert any(p.startswith("payload:") for p in rc.validate_supervision_event(leaky))

    def test_a_summary_cannot_smuggle_a_transcript(self):
        problems = rc.validate_supervision_event(
            self._event(payload={"tool": "bash", "summary": "x" * 5000})
        )
        assert any("summary" in p for p in problems)

    def test_nested_structures_are_refused(self):
        problems = rc.validate_supervision_event(
            self._event(payload={"tool": "bash", "summary": {"nested": "object"}})
        )
        assert any(p.startswith("payload.summary:") for p in problems)

    def test_privacy_safe_must_be_asserted(self):
        assert any(
            p.startswith("privacy_safe:")
            for p in rc.validate_supervision_event(self._event(privacy_safe=False))
        )

    def test_seq_must_be_a_real_ordinal(self):
        for bad in (0, -1, "1", True, None):
            assert any(
                p.startswith("seq:")
                for p in rc.validate_supervision_event(self._event(seq=bad))
            ), bad


class TestProfileRecords:
    def _profile(self, **overrides):
        base = {
            "profile_id": "claude-managed-standard-v1",
            "runtime_family": "claude-code",
            "placement": "managed-laptop",
            "pinned_version": "2.1.0",
            "capability_profiles": ["producer", "prover"],
            "capabilities": ["workspace.write", "process.test"],
        }
        base.update(overrides)
        return base

    def test_a_well_formed_profile_validates(self):
        assert rc.validate_profile(self._profile()) == []

    def test_a_profile_must_pin_a_version(self):
        """Vendor drift is the named risk; an unpinned profile cannot be
        certified against anything."""
        assert any(
            p.startswith("pinned_version:")
            for p in rc.validate_profile(self._profile(pinned_version=""))
        )

    def test_served_capability_profiles_are_the_selector_policy_key(self):
        assert any(
            p.startswith("capability_profiles:")
            for p in rc.validate_profile(self._profile(capability_profiles=[]))
        )
        assert any(
            p.startswith("capability_profiles:")
            for p in rc.validate_profile(self._profile(capability_profiles=["wizard"]))
        )


class TestReceiptAttemptBinding:
    def test_identity_is_required_in_the_payload(self):
        problems = rc.validate_receipt_attempt_binding({"task": "x"})
        assert any(p.startswith("attempt_id:") for p in problems)
        assert any(p.startswith("dispatch_id:") for p in problems)

    def test_a_bound_receipt_validates(self):
        assert (
            rc.validate_receipt_attempt_binding(
                {"attempt_id": "att_01J6ABCDEF", "dispatch_id": "e" * 32}
            )
            == []
        )

    def test_failure_class_is_a_closed_vocabulary(self):
        ok = {
            "attempt_id": "att_01J6ABCDEF",
            "dispatch_id": "e" * 32,
            "failure_class": "deterministic-check",
        }
        assert rc.validate_receipt_attempt_binding(ok) == []
        bad = dict(ok, failure_class="it broke")
        assert any(p.startswith("failure_class:") for p in rc.validate_receipt_attempt_binding(bad))

    def test_remediation_order_classes_are_present_in_order(self):
        """SP-INT-019's preferred remediation order is the selector's
        fallback policy, so the vocabulary must carry those classes in that
        order rather than an arbitrary set."""
        order = rc.FAILURE_CLASSES[:7]
        assert order == (
            "environment",
            "deterministic-check",
            "context-knowledge",
            "skill-rule",
            "workflow-structure",
            "agent-specialization",
            "model-policy",
        )
