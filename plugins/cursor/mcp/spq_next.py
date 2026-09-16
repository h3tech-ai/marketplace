#!/usr/bin/env python3
"""Cursor's SPQ next_action seam: one config read, one delegation.

WHAT THIS FILE USED TO BE. It encoded the lifecycle TWICE --
`sync_boundary_next_action` for the Sync boundary and `lifecycle_next_action`
branching on seven states for everything else -- against a machine that no
longer exists, and `plugin-codex`'s MCP server carried a line-for-line copy of
both. Three encodings of one machine is the mechanism that shipped three
different barrier-criteria counts in one product (`#640`), so the decision now
lives once, in `spq_mcp.lifecycle_next_action`, and both hosts ask it.

`sync_boundary_next_action` IS DELETED, not ported, and every part of it had
lost its subject. It resolved a lane from `SYNAPTORY_ACTIVE_SPEC` or the
current branch -- there are no lanes, and that variable is Multi-Spec identity
which SPQ ignores. It counted a quorum of readiness records -- the barrier is
at Checkpoint over the admitted set, and `C-04` refuses partial admission, so
there is no quorum to be short of. And it returned `await_sync` while the count
was short -- the absence of a Sync event blocks nothing now; what blocks a Work
Unit is an unsatisfied dependency, which `story_pipeline` reports with its
reason. Porting it would have rebuilt the human gate `C-02` removes.

`configured_build_mode` stays here because it is genuinely host-local: it reads
`.synaptory.yaml` from the project this Cursor server was started in, before
any lifecycle module is imported, which is what the fresh-clone `hydrate_cycle`
path needs.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any


def configured_build_mode(project: Path) -> str:
    path = project / ".synaptory.yaml"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.split("#", 1)[0].strip()
            if stripped.startswith("build_mode:"):
                return stripped.split(":", 1)[1].strip().strip("\"'") or "scrum"
    except OSError:
        pass
    return "scrum"


def lifecycle_next_action(project: Path) -> dict[str, Any]:
    """Delegate to the shared contract. Kept as a named function on purpose.

    `server.py` and its tests call this, and the composed Cursor package is
    published with those call sites in it. A thin seam is what lets the shared
    body move without every caller in the package moving with it -- and the
    reason it is thin is the whole point: anything decided here is decided
    differently on the other host.
    """
    return importlib.import_module("spq_mcp").lifecycle_next_action(str(project))
