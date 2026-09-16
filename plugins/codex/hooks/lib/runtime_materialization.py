#!/usr/bin/env python3
# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
"""A managed attempt's materialization and connector blocks (Epic #339, #632).

Source of truth: docs/proposals/governed-runtime-pilot.md 6.4, ADR-031.

A `local` placement runs inside the operator's own checkout, so an envelope may
omit every block here and the bridge uses the tree it is already standing in. A
`managed-laptop` placement has NO host environment to fall back on: the bridge
clones into a disposable workspace from the envelope alone, so the envelope has
to name the repository, the exact revision, and the scoped credentials by
reference. `runtime_contracts.validate_envelope` enforces that, and until #632
nothing in the kernel could produce it -- `_build_dispatch_envelope` refused
every managed placement outright, which is the Phase D exit blocker #355 named.

**Where the answer comes from.** The project's materialization block is control
plane state (`ProjectConnector`, `PUT /v1/runtime/projects/{ref}/materialization`).
The kernel reads a LOCAL MIRROR of that block rather than calling the control
plane on the dispatch path, for the same reason `runtime_selector` reads a probe
snapshot instead of probing: a dispatch that needs the network to establish its
own authority puts a partition between an engineer and their own runtime. The
resolution order is the one `runtime_selector.load_profiles` already uses, most
specific first:

  1. ``SYNAPTORY_RUNTIME_MATERIALIZATION``, authoritative when SET, including
     when its target does not exist. An operator who named a file and got a
     different one silently is the substitution EP-12 forbids.
  2. ``.synaptory/runtime-materialization.json`` in the project.

The accepted document is the control plane's own ``MaterializationResponse``
verbatim -- ``{project_id, vcs, tracker, ci, config_sources}`` -- so the mirror
is a copy of the API answer and never a second schema to keep in step.

**Nothing here is optional-with-a-default.** Every failure raises
`UnresolvedMaterialization` with the field that is missing and what to do about
it. A managed attempt built from guessed values would put a runner on a
repository and a credential the operator never declared, which is worse than
refusing the dispatch.

**No credential material, structurally (SP-SEC-037).** ``auth_ref`` must be a
``cred://`` reference and any secret-shaped key in a connector block is refused
at load, mirroring the control plane's own `_reject_inline_secrets`. The bridge
resolves references on the laptop (ADR-031 decision 13); what travels in the
envelope, the attempt record, and the operator's screen is the reference.

Python 3.9 compatible: this file is projected into every host package.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

import runtime_contracts as rc

#: Same override/convention pair as `runtime_selector.PROFILES_ENV` and
#: `PROFILES_RELPATH`, so an operator learns one resolution order for every
#: governed input a project pins.
MATERIALIZATION_ENV = "SYNAPTORY_RUNTIME_MATERIALIZATION"
MATERIALIZATION_RELPATH = (".synaptory", "runtime-materialization.json")

#: The connector slots the envelope carries. `llm` is listed so its absence is
#: EXPLICIT rather than merely unmentioned: managed Claude authentication
#: belongs to the runner's own subscription login, and `validate_envelope`
#: refuses a non-null value for it.
CONNECTOR_SLOTS: Tuple[str, ...] = ("vcs", "tracker", "ci")

#: Schemes the bridge can clone under a scoped credential
#: (`cli/internal/cli/runtime_workspace.go::checkRepoURL`). An ssh or scp-style
#: remote is refused because it would authenticate with whatever key the host's
#: agent happens to hold, which is the ambient credential a managed placement
#: exists to exclude. Checked here as well as there so the refusal arrives when
#: the envelope is minted and can still name the config field, rather than
#: inside a bridge that has already claimed an attempt.
CLONEABLE_SCHEMES: Tuple[str, ...] = ("https://", "http://", "file://")

#: Refused as connector keys outright, mirroring the control plane's
#: `_SECRET_KEY_HINTS`. The mirror file sits inside the project, so a key that
#: looks like material is the one shape that would put a secret in the repo.
_SECRET_KEY_HINTS: Tuple[str, ...] = (
    "token", "secret", "password", "credential", "key",
)

#: The governance config the bridge reads out of the materialized tree
#: (`readGovernanceConfig` reads exactly `config_refs.synaptory_yaml`).
DEFAULT_CONFIG_SOURCE = rc.CONFIG_REF_SCHEME + ".synaptory.yaml"

#: A managed attempt's workspace is built, used, and destroyed. The value is
#: fixed rather than configurable: `validate_envelope` accepts no other mode
#: for this placement, and a per-project override would be a way to ask for a
#: workspace that outlives the credential scope it was given.
MANAGED_WORKSPACE_MODE = "clean-isolated"


class UnresolvedMaterialization(Exception):
    """A managed dispatch's materialization or connector config is unusable.

    One exception for absent AND invalid, unlike `runtime_selector`'s split.
    That split exists because an absent runtime policy means "this project never
    opted in" and has to stay a no-op; here the project has ALREADY opted in --
    a managed profile was selected under its own policy -- so an absent block is
    not a migration case, it is a governed dispatch with no repository to run
    against. Both fail closed, so both are one type.
    """


def materialization_path(project_dir: Any) -> Optional[Path]:
    """The file this project's materialization mirror resolves to, or None.

    None only when there is no project directory to resolve against. A SET
    override resolves to its own target whether or not the target exists.
    """
    override = os.environ.get(MATERIALIZATION_ENV)
    if override is not None and override.strip():
        return Path(override.strip())
    if not project_dir:
        return None
    return Path(project_dir).joinpath(*MATERIALIZATION_RELPATH)


def load_connectors(project_dir: Any, *, project_id: str = "") -> Dict[str, Any]:
    """The project's materialization block, as the control plane stores it.

    Raises `UnresolvedMaterialization` when it is absent, unreadable, not an
    object, or names a different project than the one being dispatched. That
    last case is the copy-paste failure this check is for: a mirror pasted from
    another project would clone another project's repository under this
    project's authority.
    """
    path = materialization_path(project_dir)
    if path is None:
        raise UnresolvedMaterialization(
            "managed placement needs a project materialization block and there "
            "is no project directory to read one from"
        )
    if not path.exists():
        raise UnresolvedMaterialization(
            "managed placement needs a project materialization block and there "
            "is none at %s. Set it on the control plane (`PUT /v1/runtime/"
            "projects/<slug>/materialization` with `vcs.repo_url`, "
            "`vcs.default_ref` and a `cred://` `vcs.auth_ref`) and mirror that "
            "response to %s, or point %s at it"
            % (path, "/".join(MATERIALIZATION_RELPATH), MATERIALIZATION_ENV)
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UnresolvedMaterialization(
            "the project materialization block at %s cannot be read: %s"
            % (path, exc)
        )
    if not isinstance(raw, dict):
        raise UnresolvedMaterialization(
            "the project materialization block at %s is not an object; it is "
            "the control plane's own materialization response, verbatim" % path
        )
    declared = str(raw.get("project_id") or "").strip()
    wanted = str(project_id or "").strip()
    if declared and wanted and declared != wanted:
        raise UnresolvedMaterialization(
            "the materialization block at %s names project %r, but this "
            "dispatch belongs to %r. A block copied from another project would "
            "clone that project's repository under this one's authority"
            % (path, declared, wanted)
        )
    return raw


def build_managed_context(
    block: Mapping[str, Any],
    *,
    source_revision: str,
    source_ref: str = "",
    adapter_profile_id: str = "",
    capability_ceiling: Sequence[str] = (),
) -> Dict[str, Any]:
    """The four managed-only envelope blocks, or `UnresolvedMaterialization`.

    Returns exactly the keyword arguments `runtime_contracts.build_envelope`
    takes for them, so the kernel splats the result rather than restating the
    field names a third time.

    `source_revision` is the envelope's own value and is copied into
    `materialization.revision` rather than resolved again: the validator
    requires the two to be equal, and deriving each from its own git call is how
    they come to disagree.
    """
    revision = str(source_revision or "").strip()
    if not revision:
        raise UnresolvedMaterialization(
            "managed placement pins an exact revision, and this checkout has "
            "no resolvable HEAD to pin. A managed runner clones by revision, so "
            "there is nothing for it to check out"
        )

    vcs = _connector_block(block, "vcs")
    if not vcs:
        raise UnresolvedMaterialization(
            "materialization.vcs is empty: a managed attempt has no host "
            "checkout, so the block must name the repository it clones"
        )
    repo_url = str(vcs.get("repo_url") or "").strip()
    if not repo_url:
        raise UnresolvedMaterialization(
            "materialization.vcs.repo_url is required for managed placement: "
            "it is the repository the runner clones into its disposable "
            "workspace"
        )
    if not repo_url.lower().startswith(CLONEABLE_SCHEMES):
        raise UnresolvedMaterialization(
            "materialization.vcs.repo_url %r is not one of %s: a managed "
            "attempt clones with the scoped credential the envelope names, and "
            "an ssh or scp-style remote would instead use whatever key the "
            "host's agent happens to hold"
            % (repo_url, ", ".join(CLONEABLE_SCHEMES))
        )

    # The branch this checkout is actually on wins over the project default.
    # `default_ref` is what the project usually clones; it is not necessarily a
    # ref that CONTAINS this revision, and a server that refuses a by-SHA fetch
    # falls back to this ref. On SPQ the local branch is the workstream branch,
    # which is precisely the ref the revision lives on.
    ref = str(source_ref or "").strip() or str(vcs.get("default_ref") or "").strip()
    if not ref:
        raise UnresolvedMaterialization(
            "managed placement needs a ref to fetch: this checkout is not on a "
            "branch and materialization.vcs.default_ref is unset, so a server "
            "that refuses a by-revision fetch has nothing to fall back to"
        )

    connector_refs: Dict[str, Any] = {}
    for slot in CONNECTOR_SLOTS:
        connector_refs[slot] = _auth_ref(block, slot)
    # Explicitly null, never derived. `validate_envelope` refuses a non-null
    # `llm` and the bridge refuses to resolve one: a managed Claude runner
    # authenticates with its own subscription login, so an attempt-scoped LLM
    # token would be authority the envelope was never meant to carry.
    llm = _connector_block(block, "llm")
    if llm.get("auth_ref"):
        raise UnresolvedMaterialization(
            "materialization.llm.auth_ref is set, and a managed attempt may "
            "not carry one: authenticate the managed runner itself with "
            "`claude auth login --claudeai`"
        )
    connector_refs["llm"] = None

    return {
        "materialization": {
            "repo_url": repo_url,
            "ref": ref,
            "revision": revision,
            "worktree_mode": MANAGED_WORKSPACE_MODE,
        },
        "connector_refs": connector_refs,
        "config_refs": _config_refs(block, adapter_profile_id),
        "workspace": {
            "mode": MANAGED_WORKSPACE_MODE,
            # Derived from the ceiling rather than asserted beside it. The
            # local adapter reads `workspace.write` to decide whether the child
            # may write at all, and a workspace flag that disagreed with the
            # authority ceiling would be a second, unsigned opinion about the
            # same permission.
            "write": "workspace.write" in set(capability_ceiling or ()),
        },
    }


def managed_dispatch_context(
    project_dir: Any,
    *,
    project_id: str = "",
    source_revision: str,
    source_ref: str = "",
    adapter_profile_id: str = "",
    capability_ceiling: Sequence[str] = (),
) -> Dict[str, Any]:
    """`build_managed_context` over the project's own mirrored block."""
    return build_managed_context(
        load_connectors(project_dir, project_id=project_id),
        source_revision=source_revision,
        source_ref=source_ref,
        adapter_profile_id=adapter_profile_id,
        capability_ceiling=capability_ceiling,
    )


def _connector_block(block: Mapping[str, Any], name: str) -> Dict[str, Any]:
    """One connector block, refused if it carries anything secret-shaped."""
    value = block.get(name)
    if value in (None, {}, []):
        return {}
    if not isinstance(value, dict):
        raise UnresolvedMaterialization(
            "materialization.%s must be an object, got %s"
            % (name, type(value).__name__)
        )
    for key in value:
        lowered = str(key).lower()
        if lowered == "auth_ref":
            continue
        if any(hint in lowered for hint in _SECRET_KEY_HINTS):
            raise UnresolvedMaterialization(
                "materialization.%s.%s looks like credential material; "
                "connector blocks carry cred:// references only, and the "
                "bridge resolves them on the runner (SP-SEC-037)" % (name, key)
            )
    return dict(value)


def _auth_ref(block: Mapping[str, Any], name: str) -> Optional[str]:
    """This connector's `cred://` reference, or None when it declares none.

    None is a real answer: a public repository needs no VCS credential and a
    project with no tracker connector has nothing to inject. What is refused is
    a value that is present and is not a reference, because that is the shape
    inline material would take.
    """
    value = _connector_block(block, name).get("auth_ref")
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    ref = str(value).strip()
    if not ref.startswith("cred://"):
        raise UnresolvedMaterialization(
            "materialization.%s.auth_ref must be a cred:// reference, never "
            "inline credential material (SP-SEC-037)" % name
        )
    return ref


def _config_refs(block: Mapping[str, Any], adapter_profile_id: str) -> Dict[str, Any]:
    """`config_refs`, from the block's `config_sources` list.

    The bridge reads exactly one governance config out of the materialized
    tree, `config_refs.synaptory_yaml`. So a list with more than one entry is
    refused rather than resolved by picking the first: silently choosing which
    of several configs governs an attempt is the silent degradation EP-12
    forbids, and an operator who listed two needs to be told which one the
    bridge would have honoured.
    """
    sources = block.get("config_sources")
    if sources in (None, ""):
        sources = []
    if not isinstance(sources, list):
        raise UnresolvedMaterialization(
            "materialization.config_sources must be a list of %s references"
            % rc.CONFIG_REF_SCHEME
        )
    entries = [str(s).strip() for s in sources if str(s).strip()]
    if len(entries) > 1:
        raise UnresolvedMaterialization(
            "materialization.config_sources names %d sources (%s), and a "
            "managed attempt reads exactly one governance config from the "
            "materialized tree. Declare the single %s.synaptory.yaml the "
            "attempt is governed by"
            % (len(entries), ", ".join(entries), rc.CONFIG_REF_SCHEME)
        )
    refs: Dict[str, Any] = {"synaptory_yaml": entries[0] if entries else DEFAULT_CONFIG_SOURCE}
    profile = str(adapter_profile_id or "").strip()
    if profile:
        refs["preset_base"] = profile
    # Validated here rather than left to the envelope validator, so the refusal
    # names `config_sources` -- the field an operator can actually edit --
    # instead of `config_refs`, which the kernel derived.
    problems = rc.config_ref_problems(refs)
    if problems:
        raise UnresolvedMaterialization(
            "materialization.config_sources: %s" % "; ".join(problems)
        )
    return refs
