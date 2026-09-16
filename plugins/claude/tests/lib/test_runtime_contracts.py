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

import json
import pathlib

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

    def test_role_must_be_the_abbrev_not_the_full_agent_name(self):
        """Both spellings existed in the draft and nothing constrained the
        field, so an envelope could carry either and validate. The abbrev
        wins: it is what the kernel binds and what the canonical receipt
        filename uses. The full name lives on in the receipt's `agent`."""
        problems = rc.validate_envelope(_envelope(role="quality-engineer"))
        assert any(p.startswith("role:") for p in problems)
        assert rc.validate_envelope(_envelope(role="qe")) == []

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
            source_revision="d" * 40,
            materialization={
                "repo_url": "https://example.invalid/taskflow.git",
                "ref": "refs/heads/main",
                "revision": "d" * 40,
                "worktree_mode": "clean-isolated",
            },
            connector_refs={
                "vcs": "cred://runner/vcs-read",
                "ci": None,
            },
            workspace={"mode": "clean-isolated", "write": True},
            capability_ceiling=["workspace.write", "process.test", "receipt.v2"],
        )
        assert rc.validate_envelope(env) == []

    def test_managed_placement_refuses_borrowed_workspace_and_empty_ceiling(self):
        env = _envelope(
            placement="managed-laptop",
            runtime_family="claude-code",
            adapter_profile_id="claude-managed-standard-v1",
            materialization={
                "repo_url": "https://example.invalid/taskflow.git",
                "ref": "refs/heads/main",
                "revision": "c" * 40,
            },
            connector_refs={},
            workspace={"mode": "existing-locked-worktree", "write": True},
            capability_ceiling=[],
        )
        problems = rc.validate_envelope(env)
        assert any(p.startswith("workspace.mode:") for p in problems)
        assert any(p.startswith("capability_ceiling:") for p in problems)

    def test_managed_subscription_authentication_is_runner_owned(self):
        env = _envelope(
            placement="managed-laptop",
            runtime_family="claude-code",
            adapter_profile_id="claude-managed-standard-v1",
            source_revision="d" * 40,
            materialization={
                "repo_url": "https://example.invalid/taskflow.git",
                "ref": "refs/heads/main",
                "revision": "d" * 40,
            },
            connector_refs={"llm": "cred://runner/claude-team-oauth"},
            workspace={"mode": "clean-isolated", "write": True},
            capability_ceiling=["workspace.write", "receipt.v2"],
        )
        assert any(
            p.startswith("connector_refs.llm: must be absent or null")
            for p in rc.validate_envelope(env)
        )

    @pytest.mark.parametrize("receipt_path", ["/tmp/receipt.json", "../../receipt.json"])
    def test_managed_receipt_must_stay_inside_disposable_workspace(
        self, receipt_path
    ):
        env = _envelope(
            placement="managed-laptop",
            runtime_family="claude-code",
            receipt_path=receipt_path,
        )
        assert any(
            p.startswith("receipt_path: managed placement requires")
            for p in rc.validate_envelope(env)
        )

    def test_vendor_state_cannot_be_a_canonical_config_source(self):
        env = _envelope(
            config_refs={
                "synaptory_yaml": "file:///home/runner/.claude/projects/history.jsonl"
            }
        )
        assert any(
            p.startswith("config_refs.synaptory_yaml:")
            for p in rc.validate_envelope(env)
        )


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
    @staticmethod
    def _receipt(**overrides):
        receipt = {
            "attempt_id": "att_01J6ABCDEF",
            "dispatch_id": "e" * 32,
            "adapter_profile_id": "claude-local-v1",
            "placement": "local",
            "source_revision": "c" * 40,
            "fencing_token": "b" * 32,
        }
        receipt.update(overrides)
        return receipt

    def test_identity_is_required_in_the_payload(self):
        problems = rc.validate_receipt_attempt_binding({"task": "x"})
        assert any(p.startswith("attempt_id:") for p in problems)
        assert any(p.startswith("dispatch_id:") for p in problems)

    def test_the_lease_generation_is_part_of_that_identity(self):
        """Asserts impossible: a governed receipt that names no generation.

        Every other field in this shape makes the runner prove something about
        itself: which attempt, which dispatch, which profile, which placement,
        which source revision. The generation was the one field left optional,
        and it is the only one a SUPERSEDED runner would get wrong, so its
        absence was the cheapest way past the fence (#592).

        No format is asserted here, deliberately: the kernel mints 32 hex
        characters locally while a coordinator-issued generation is whatever
        the control plane issues, so a regex would refuse a valid one.
        """
        assert any(
            p.startswith("fencing_token:")
            for p in rc.validate_receipt_attempt_binding({"task": "x"})
        )
        assert any(
            p.startswith("fencing_token:")
            for p in rc.validate_receipt_attempt_binding(self._receipt(fencing_token=""))
        )
        assert rc.validate_receipt_attempt_binding(
            self._receipt(fencing_token="generation-7")
        ) == []

    def test_a_bound_receipt_validates(self):
        assert (
            rc.validate_receipt_attempt_binding(self._receipt())
            == []
        )

    def test_failure_class_is_a_closed_vocabulary(self):
        ok = self._receipt(failure_class="deterministic-check")
        assert rc.validate_receipt_attempt_binding(ok) == []
        bad = dict(ok, failure_class="it broke")
        assert any(p.startswith("failure_class:") for p in rc.validate_receipt_attempt_binding(bad))

    def test_unclassified_is_transient_not_a_closing_class(self):
        problems = rc.validate_receipt_attempt_binding(
            self._receipt(failure_class="unclassified")
        )
        assert any("transient only" in p for p in problems)

    @pytest.mark.parametrize(
        "field", ["adapter_profile_id", "placement", "source_revision"]
    )
    def test_runtime_identity_is_required(self, field):
        receipt = self._receipt()
        receipt.pop(field)
        assert any(
            problem.startswith(field + ":")
            for problem in rc.validate_receipt_attempt_binding(receipt)
        )

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


# ─── a bridge attempt record, as a host may trust it ─────────────────────────


def _bridge_record(**over):
    rec = dict(
        attempt_id="att_01J6ABCDEF",
        dispatch_id="a" * 32,
        story_id="WU-001",
        role="se",
        state="completed",
        fencing_token="000000000002",
        cp_closed=True,
    )
    rec.update(over)
    return rec


def _bridge_envelope(**over):
    env = dict(
        attempt_id="att_01J6ABCDEF",
        dispatch_id="a" * 32,
        story_id="WU-001",
        role="se",
    )
    env.update(over)
    return env


def test_a_matching_record_has_no_disagreements():
    assert rc.attempt_record_disagreements(_bridge_envelope(), _bridge_record()) == []


@pytest.mark.parametrize(
    "field,wrong",
    [
        ("attempt_id", "att_01J6ZZZZZZ"),
        ("dispatch_id", "b" * 32),
        ("story_id", "WU-999"),
        ("role", "qe"),
    ],
)
def test_every_identifier_is_checked(field, wrong):
    """All four, because the useful lie is a partial one: a record naming the
    right story and role but a different attempt is exactly what made a
    superseded generation look current (#396)."""
    problems = rc.attempt_record_disagreements(
        _bridge_envelope(), _bridge_record(**{field: wrong})
    )
    assert problems, field
    assert any(field in p for p in problems), problems


def test_a_missing_field_is_a_disagreement_not_a_waiver():
    """Absence is not a match. A record that omits a field cannot establish
    that it names this work, the same rule the kernel applies to a binding
    carrying no attempt id."""
    record = _bridge_record()
    del record["attempt_id"]
    assert rc.attempt_record_disagreements(_bridge_envelope(), record)


def test_a_record_that_is_not_a_record_is_refused():
    assert rc.attempt_record_disagreements(_bridge_envelope(), None)
    assert rc.attempt_record_disagreements(None, _bridge_record())


def test_only_a_real_true_is_a_confirmed_close():
    """`"false"` is truthy, and a snapshot reporting a runtime unavailable
    being selected anyway is the same bug in a different file."""
    assert rc.attempt_close_confirmed(_bridge_record()) is True
    assert rc.attempt_close_confirmed(_bridge_record(cp_closed=False)) is False
    assert rc.attempt_close_confirmed(_bridge_record(cp_closed="false")) is False
    assert rc.attempt_close_confirmed(_bridge_record(cp_closed=1)) is False
    assert rc.attempt_close_confirmed({}) is False
    assert rc.attempt_close_confirmed(None) is False


# ─── SP-INT-017: a close names a cause ───────────────────────────────────────


def _failed_receipt(**over):
    rec = dict(
        attempt_id="att_01J6FAIL01",
        dispatch_id="0" * 32,
        adapter_profile_id="claude-managed-standard-v1",
        placement="managed-laptop",
        source_revision="c" * 40,
        fencing_token="b" * 32,
        failure_class="deterministic-check",
    )
    rec.update(over)
    return rec


def test_a_receipt_may_not_close_unclassified():
    """The half of SP-INT-017 that was declared and not enforced (#396).

    The coordination route already refuses an unclassified close, so a receipt
    carrying it describes a close that could not have happened, and it is the
    receipt that outlives the attempt.
    """
    assert rc.validate_receipt_attempt_binding(_failed_receipt()) == []
    problems = rc.validate_receipt_attempt_binding(
        _failed_receipt(failure_class="unclassified")
    )
    assert problems, "a failed receipt closed having explained nothing"
    assert "SP-INT-017" in " ".join(problems), problems


def test_the_vocabulary_still_refuses_a_class_it_does_not_know():
    assert rc.validate_receipt_attempt_binding(_failed_receipt(failure_class="made-up"))


def test_runtime_death_is_the_honest_narrowing():
    """`unclassified` is the ABSENCE of a cause; `runtime-death` is one. A
    runner that does not know narrows to the second rather than closing on the
    first."""
    assert rc.validate_receipt_attempt_binding(
        _failed_receipt(failure_class="runtime-death")
    ) == []


# ─── vendor state cannot enter through config_refs ───────────────────────────


def _with_config_refs(value):
    return _envelope(config_refs=value)


def test_a_repo_relative_reference_is_the_accepted_shape():
    assert rc.validate_envelope(
        _with_config_refs({"synaptory_yaml": "repo://.synaptory.yaml"})
    ) == []


def test_an_opaque_identifier_is_still_allowed():
    """`preset_base` names something the control plane resolves, not a place on
    a disk, so it carries no scheme and no separator."""
    assert rc.validate_envelope(
        _with_config_refs({"preset_base": "claude-managed-standard-v1"})
    ) == []


@pytest.mark.parametrize(
    "ref",
    [
        # The exact leak the conformance scenario names: a host runtime's own
        # conversation store, sourced as configuration.
        "file:///home/runner/.claude/projects/taskflow/history.jsonl",
        "file:///Users/x/.codex/sessions/latest.json",
        "http://internal/config.yaml",
        "cp://project/synaptory.yaml",
        # A bare path is read on the runner's filesystem, which the dispatch
        # does not pin, so it is not an identifier either.
        "/etc/synaptory.yaml",
        "~/.cursor/mcp.json",
        ".claude/settings.json",
        # Inside the scheme, but not inside the tree.
        "repo:///etc/passwd",
        "repo://../../.claude/history.jsonl",
        "repo://a/../../b",
        "repo://",
    ],
)
def test_config_refs_may_not_reach_outside_the_materialized_tree(ref):
    problems = rc.validate_envelope(_with_config_refs({"synaptory_yaml": ref}))
    assert problems, "%r was accepted as a configuration source" % ref


def test_config_refs_must_be_an_object():
    # Set on the built envelope rather than through the builder, which coerces.
    not_an_object = _envelope()
    not_an_object["config_refs"] = "repo://.synaptory.yaml"
    assert rc.validate_envelope(not_an_object)
    assert rc.validate_envelope(_with_config_refs({"synaptory_yaml": ""}))
    # Absent is fine: a local dispatch uses the host's own checkout.
    assert rc.validate_envelope(_with_config_refs(None)) == []


# ─── containment of the RESOLVED config file, not the lexical name ───────────

_CONTAINMENT = json.loads(
    (
        pathlib.Path(__file__).resolve().parents[3]
        / "core" / "runtime-fixtures" / "config-ref-containment.json"
    ).read_text(encoding="utf-8")
)


def _build_case(case, tmp_path):
    """Materialize one case's tree and return the checkout root."""
    outside = tmp_path / "outside"
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    for entry in case.get("outside", []):
        target = outside / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(entry.get("content", ""), encoding="utf-8")
    for entry in case["layout"]:
        path = checkout / entry["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        if entry["kind"] == "file":
            path.write_text(entry.get("content", ""), encoding="utf-8")
        elif entry["kind"] == "dir":
            path.mkdir(parents=True, exist_ok=True)
        elif entry["kind"] == "symlink":
            if "outside_target" in entry:
                path.symlink_to(outside / entry["outside_target"])
            else:
                path.symlink_to(checkout / entry["inside_target"])
        else:  # pragma: no cover - the table is closed
            raise AssertionError("unknown layout kind %r" % entry["kind"])
    return checkout


@pytest.mark.parametrize(
    "case", _CONTAINMENT["cases"], ids=[c["name"] for c in _CONTAINMENT["cases"]]
)
def test_config_ref_containment(case, tmp_path):
    """A signed revision pins a symlink's TEXT, not its target's content, so the
    lexical `repo://` rule is not containment: after checkout, an ordinary open
    follows a symlink straight out of the tree (#396).

    The table is shared with the Go bridge's own test, because the reader that
    actually opens the file lives there and two rules that disagree means which
    one you hit depends on which side of the bridge you are on.
    """
    checkout = _build_case(case, tmp_path)
    problem = rc.config_ref_containment_problem(str(checkout), case["ref"])
    if case["contained"]:
        assert problem == "", (case["name"], problem)
    else:
        assert problem, case["name"]


def test_a_symlinked_checkout_root_is_still_contained(tmp_path):
    """The workspace itself may sit under a symlinked temp dir, which is the
    normal case on macOS (`/var` -> `/private/var`). Resolving the file without
    resolving the root would report every legitimate config as an escape."""
    real = tmp_path / "real-checkout"
    real.mkdir()
    (real / ".synaptory.yaml").write_text("project_id: taskflow\n", encoding="utf-8")
    link = tmp_path / "checkout"
    link.symlink_to(real)
    assert rc.config_ref_containment_problem(str(link), "repo://.synaptory.yaml") == ""
