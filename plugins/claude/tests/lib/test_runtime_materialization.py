"""Layer 1 -- the managed dispatch's materialization and connector resolution
(#632, Phase D Track M of #355).

The kernel refused every `managed-laptop` placement with "nothing resolves a
project's connector configuration yet". These pin what replaced that refusal,
and the shape of the pin matters as much as the coverage: a resolver that
*guessed* a repository or a credential would be strictly worse than the refusal
it replaced, because a guess runs a runner somewhere the operator never
declared. So every negative here asserts a refusal that NAMES the field.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import runtime_contracts as rc
import runtime_materialization as rmat


pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[3] / "core" / "runtime-fixtures"

REVISION = "d" * 40


def _block(**overrides):
    block = {
        "project_id": "taskflow",
        "vcs": {
            "repo_url": "https://github.com/h3tech-ai/taskflow.git",
            "default_ref": "main",
            "auth_ref": "cred://runner/github-vcs-read",
        },
        "tracker": {"auth_ref": "cred://runner/github-tracker-write"},
        "ci": {},
        "config_sources": ["repo://.synaptory.yaml"],
    }
    block.update(overrides)
    return block


def _write(project: Path, block) -> Path:
    path = project.joinpath(*rmat.MATERIALIZATION_RELPATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(block), encoding="utf-8")
    return path


def _context(block=None, **kwargs):
    kwargs.setdefault("source_revision", REVISION)
    kwargs.setdefault("adapter_profile_id", "claude-managed-standard-v1")
    kwargs.setdefault("capability_ceiling", ["workspace.write", "receipt.v2"])
    return rmat.build_managed_context(block if block is not None else _block(), **kwargs)


class TestResolutionProducesAValidManagedEnvelope:
    def test_the_blocks_complete_an_envelope_the_frozen_contract_accepts(self):
        """The point of the ticket, asserted end to end rather than field by
        field: the resolved blocks are what turn a managed envelope from a
        refusal into one `validate_envelope` passes."""
        envelope = rc.build_envelope(
            attempt_id="att_01J6XYZ123",
            dispatch_id="f0e1d2c3b4a5968778695a4b3c2d1e0f",
            project_id="taskflow",
            cycle_id="003-9f2c41ab",
            manifest_hash="sha256:" + "b" * 64,
            workstream_id="platform",
            story_id="WU-003",
            stage="in_progress",
            role="se",
            adapter_profile_id="claude-managed-standard-v1",
            runtime_family="claude-code",
            placement="managed-laptop",
            source_revision=REVISION,
            receipt_path=".synaptory/.orchestrator/receipts/WU-003-se.json",
            expires_at="2026-09-01T14:00:00Z",
            fencing_token="1",
            capability_ceiling=["workspace.write", "process.test", "receipt.v2"],
            budget={"wall_clock_seconds": 3600},
            **_context(capability_ceiling=["workspace.write", "process.test", "receipt.v2"])
        )
        assert rc.validate_envelope(envelope) == [], envelope

    def test_the_revision_is_copied_from_the_envelope_not_resolved_again(self):
        """`validate_envelope` requires `materialization.revision ==
        source_revision`. Deriving each from its own git call is exactly how
        the two come to disagree, so the resolver takes the value it is given."""
        context = _context(source_revision="a" * 40)
        assert context["materialization"]["revision"] == "a" * 40

    def test_the_checkouts_own_branch_wins_over_the_project_default(self):
        """`materialization.ref` is the bridge's fallback when a server refuses
        a by-revision fetch, so it has to be a ref that CONTAINS the revision.
        On SPQ that is the workstream branch, never `main`."""
        context = _context(source_ref="refs/heads/cycle/003-9f2c41ab/ws/platform")
        assert context["materialization"]["ref"] == (
            "refs/heads/cycle/003-9f2c41ab/ws/platform"
        )

    def test_a_detached_checkout_falls_back_to_the_declared_default_ref(self):
        assert _context(source_ref="")["materialization"]["ref"] == "main"

    def test_workspace_write_is_derived_from_the_ceiling_never_asserted_beside_it(self):
        """A workspace flag that disagreed with the authority ceiling would be
        a second, unsigned opinion about the same permission."""
        assert _context(capability_ceiling=["workspace.write"])["workspace"]["write"] is True
        assert _context(capability_ceiling=["receipt.v2"])["workspace"]["write"] is False

    def test_the_workspace_is_disposable_for_every_managed_attempt(self):
        context = _context()
        assert context["workspace"]["mode"] == "clean-isolated"
        assert context["materialization"]["worktree_mode"] == "clean-isolated"

    def test_the_selected_profile_becomes_the_config_preset_base(self):
        refs = _context()["config_refs"]
        assert refs["preset_base"] == "claude-managed-standard-v1"
        assert refs["synaptory_yaml"] == "repo://.synaptory.yaml"

    def test_an_unset_config_sources_list_still_names_the_governance_config(self):
        """The bridge reads `config_refs.synaptory_yaml` out of the materialized
        tree and a managed attempt with none is ungoverned, so the default is
        the repository's own `.synaptory.yaml` rather than nothing."""
        block = _block()
        block.pop("config_sources")
        assert _context(block)["config_refs"]["synaptory_yaml"] == (
            "repo://.synaptory.yaml"
        )


class TestNoCredentialMaterialCanTravel:
    """SP-SEC-037, enforced structurally rather than by review."""

    def test_every_connector_arrives_as_a_reference_or_as_nothing(self):
        refs = _context()["connector_refs"]
        assert refs == {
            "vcs": "cred://runner/github-vcs-read",
            "tracker": "cred://runner/github-tracker-write",
            "ci": None,
            "llm": None,
        }

    def test_an_inline_auth_value_is_refused_by_shape(self):
        block = _block(vcs={
            "repo_url": "https://example.test/x.git",
            "default_ref": "main",
            "auth_ref": "ghp_notarealtokenbutstillnotareference",
        })
        with pytest.raises(rmat.UnresolvedMaterialization) as exc:
            _context(block)
        assert "vcs.auth_ref" in str(exc.value)
        assert "cred://" in str(exc.value)

    def test_a_secret_shaped_key_is_refused_even_beside_a_valid_ref(self):
        """The mirror file sits INSIDE the project, so a key that looks like
        material is the one shape that would put a secret in the repo."""
        block = _block(tracker={
            "auth_ref": "cred://runner/github-tracker-write",
            "api_token": "definitely-not-allowed",
        })
        with pytest.raises(rmat.UnresolvedMaterialization) as exc:
            _context(block)
        assert "tracker.api_token" in str(exc.value)

    def test_an_llm_credential_is_refused_rather_than_forwarded(self):
        """Managed Claude authenticates with the RUNNER's own subscription
        login. `validate_envelope` refuses a non-null `connector_refs.llm`, so
        forwarding one would only move the refusal later."""
        block = _block(llm={"auth_ref": "cred://runner/anthropic"})
        with pytest.raises(rmat.UnresolvedMaterialization) as exc:
            _context(block)
        assert "claude auth login" in str(exc.value)

    def test_the_fixture_block_carries_no_material_of_its_own(self):
        raw = (FIXTURES / "materialization-managed.json").read_text()
        for marker in ("ghp_", "sk-", "Bearer ", "BEGIN PRIVATE KEY", "password"):
            assert marker not in raw, marker


class TestMissingOrInvalidConfigFailsClosed:
    def test_an_absent_block_names_the_endpoint_and_the_file(self, tmp_path):
        with pytest.raises(rmat.UnresolvedMaterialization) as exc:
            rmat.load_connectors(tmp_path)
        message = str(exc.value)
        assert "materialization" in message
        assert "runtime-materialization.json" in message

    def test_an_unreadable_block_is_not_treated_as_an_absent_one(self, tmp_path):
        path = tmp_path.joinpath(*rmat.MATERIALIZATION_RELPATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(rmat.UnresolvedMaterialization) as exc:
            rmat.load_connectors(tmp_path)
        assert "cannot be read" in str(exc.value)

    def test_a_block_from_another_project_is_refused(self, tmp_path):
        """The copy-paste failure: a mirror pasted from another project would
        clone that project's repository under this project's authority."""
        _write(tmp_path, _block(project_id="other-project"))
        with pytest.raises(rmat.UnresolvedMaterialization) as exc:
            rmat.load_connectors(tmp_path, project_id="taskflow")
        assert "other-project" in str(exc.value)

    def test_a_missing_repo_url_names_the_field(self):
        with pytest.raises(rmat.UnresolvedMaterialization) as exc:
            _context(_block(vcs={"default_ref": "main"}))
        assert "vcs.repo_url" in str(exc.value)

    def test_an_empty_vcs_block_is_refused(self):
        with pytest.raises(rmat.UnresolvedMaterialization) as exc:
            _context(_block(vcs={}))
        assert "materialization.vcs" in str(exc.value)

    def test_an_ssh_remote_is_refused_at_mint_time(self):
        """`checkRepoURL` in the Go bridge refuses this too. Refusing here as
        well means the operator is told while the message can still name a
        config field, instead of inside a bridge that already claimed an
        attempt."""
        with pytest.raises(rmat.UnresolvedMaterialization) as exc:
            _context(_block(vcs={
                "repo_url": "git@github.com:h3tech-ai/taskflow.git",
                "default_ref": "main",
            }))
        assert "repo_url" in str(exc.value)

    def test_a_checkout_with_no_resolvable_revision_is_refused(self):
        with pytest.raises(rmat.UnresolvedMaterialization) as exc:
            _context(source_revision="")
        assert "revision" in str(exc.value)

    def test_a_detached_checkout_with_no_default_ref_is_refused(self):
        with pytest.raises(rmat.UnresolvedMaterialization) as exc:
            _context(_block(vcs={"repo_url": "https://example.test/x.git"}), source_ref="")
        assert "default_ref" in str(exc.value)

    def test_two_config_sources_are_refused_rather_than_silently_narrowed(self):
        """The bridge reads exactly one governance config. Picking the first of
        several is the silent degradation EP-12 forbids."""
        block = _block(config_sources=["repo://.synaptory.yaml", "repo://other.yaml"])
        with pytest.raises(rmat.UnresolvedMaterialization) as exc:
            _context(block)
        assert "config_sources" in str(exc.value)

    def test_a_config_source_outside_the_materialized_tree_is_refused(self):
        """Refused against the same rule `validate_envelope` applies, but named
        against `config_sources` -- the field an operator can actually edit."""
        block = _block(config_sources=["file:///home/runner/.claude/history.jsonl"])
        with pytest.raises(rmat.UnresolvedMaterialization) as exc:
            _context(block)
        assert "config_sources" in str(exc.value)


class TestResolutionOrder:
    def test_the_project_file_is_the_conventional_source(self, tmp_path):
        _write(tmp_path, _block())
        assert rmat.load_connectors(tmp_path)["project_id"] == "taskflow"

    def test_a_set_override_is_authoritative_including_when_it_is_missing(
        self, tmp_path, monkeypatch
    ):
        """An operator who named a registry and silently got a different one is
        the substitution EP-12 forbids, so a missing override refuses rather
        than falling through to the project file."""
        _write(tmp_path, _block())
        monkeypatch.setenv(rmat.MATERIALIZATION_ENV, str(tmp_path / "nope.json"))
        with pytest.raises(rmat.UnresolvedMaterialization) as exc:
            rmat.load_connectors(tmp_path)
        assert "nope.json" in str(exc.value)

    def test_an_override_that_exists_is_read_instead_of_the_project_file(
        self, tmp_path, monkeypatch
    ):
        _write(tmp_path, _block())
        elsewhere = tmp_path / "elsewhere.json"
        elsewhere.write_text(json.dumps(_block(project_id="taskflow", ci={})), encoding="utf-8")
        monkeypatch.setenv(rmat.MATERIALIZATION_ENV, str(elsewhere))
        assert rmat.materialization_path(tmp_path) == elsewhere
        assert rmat.load_connectors(tmp_path)["project_id"] == "taskflow"


def test_the_fixture_block_builds_the_fixture_envelopes_blocks():
    """The cross-lane fixtures agree with the resolver that produces them.

    `materialization-managed.json` is what the control plane stores;
    `envelope-managed-se.json` is what the kernel mints from it. A drift between
    the two is exactly the disagreement the fixture directory exists to catch.
    """
    block = json.loads((FIXTURES / "materialization-managed.json").read_text())
    envelope = json.loads((FIXTURES / "envelope-managed-se.json").read_text())
    context = rmat.build_managed_context(
        block,
        source_revision=envelope["source_revision"],
        source_ref=envelope["materialization"]["ref"],
        adapter_profile_id=envelope["adapter_profile_id"],
        capability_ceiling=envelope["capability_ceiling"],
    )
    assert context["materialization"] == envelope["materialization"]
    assert context["connector_refs"] == envelope["connector_refs"]
    assert context["config_refs"] == envelope["config_refs"]
    assert context["workspace"]["mode"] == envelope["workspace"]["mode"]
    assert context["workspace"]["write"] == envelope["workspace"]["write"]
