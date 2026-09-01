#!/usr/bin/env python3
# Copyright (c) 2024-2026 H3Tech Inc. All rights reserved. PROPRIETARY.
"""Cross-workstream Sync barrier for the SPQ lifecycle (docs/spq-lifecycle-design.md §8).

V1 has no cross-spec, cross-clone barrier primitive. `spec_state.py` nests
lifecycle state per spec, and in SPQ's hybrid topology (§5) each workstream
additionally works from its own clone with `.synaptory/.orchestrator/`
gitignored. Workstream A finishing does nothing to workstream B, and there is
no shared state file between clones. This module supplies the missing
rendezvous.

Seven verbs, split by which clone runs them:

  workstream clone
    declare-ready <N>   Build the readiness record from LOCAL state, an
                        executed regression, and digest_script. The only
                        writer of `.synaptory/sync/` (§9.1 / guard G3).

  integration clone
    collect <N>         `git fetch`, then read each workstream's record with
                        `git show <remote>/<branch>:<path>` — WITHOUT merging.
    evaluate <N>        All nine criteria on the integration branch, executing
                        the committed scripts directly: no hook, no replay
                        timeout (§9).
    evaluate-incremental <N> --unit <ID> --sha <SHA>
                        Bind one merged candidate and its regression result to
                        the exact integration HEAD before publication.
    clear <N>           Green-only. Records the verdict and emits the gate.
    status <N>          Barrier state for the tracker mirror.

WHY NOT THE SubagentStop HOOK (§9): the hook has a 30s total budget
(hooks.json) and the Evidence Contract caps replay at 60s per command /
300s total. A cross-workstream regression cannot run inside those limits, so
a hook-enforced barrier would be a formality. The orchestrator invokes
`evaluate` outside the hook path instead.

Be precise about what the hook then does to the resulting receipt, because
the design doc's earlier account was wrong: the benign "could not be
replayed" warning fires only when `_validate_command` REJECTS a command. A
TIMEOUT is a different path — `verification_runner` catches
`TimeoutExpired`, sets passed=False and appends to failures, so the hook
takes its non-zero branch and BLOCKS with exit 2 in an autonomous session
(warning only in guided mode). That is why the barrier verdict, not the
receipt, is the authority here.

THE RECORD IS A CLAIM, NOT PROOF (§8.2.1). It is written in the workstream's
clone by that workstream's agents. What makes it useful is that it is the ONLY
channel by which quality evidence crosses the clone boundary:
`evidence_replay_mismatch` is written solely to local
`.synaptory/.orchestrator/events.jsonl` and `signals.py` never ships its store
anywhere. So `declare-ready` DERIVES the `dod` and `replay` blocks rather than
accepting them — a field the caller can set is decoration, not evidence — and
refuses outright when a replay mismatch is present.

CLI:
    python3 sync_barrier.py declare-ready <project_dir> <N> [--workstream ID]
                                          [--declared-by EMAIL] [--no-regression]
    python3 sync_barrier.py collect       <project_dir> <N> [--no-fetch]
    python3 sync_barrier.py evaluate      <project_dir> <N> [--no-cache]
    python3 sync_barrier.py evaluate-incremental <project_dir> <N> --unit ID --sha SHA
    python3 sync_barrier.py clear         <project_dir> <N> [--cleared-by EMAIL]
    python3 sync_barrier.py status        <project_dir> <N>
    python3 sync_barrier.py config        <project_dir>
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import spq_paths as _paths

# 2.0 is a strict SUPERSET of 1.1: every field a 1.1 reader touches is still
# present, including the aggregate `work_units` counts that `status()` renders.
# What 2.0 adds is IDENTITY -- exactly the gap #303 names: "readiness exchanges
# aggregate counts rather than exact Work Unit identities, manifest revision,
# and integrated commit SHAs."
SCHEMA_VERSION = "2.0"
LEGACY_SCHEMA_VERSIONS = ("1.0", "1.1")

# Barrier criteria, in the order §8.4 lists them. Criterion 2 (the merge) is
# performed by a human and only RECORDED here — the shipped git rules forbid
# agents committing or pushing to shared branches (modes/init.md:372), which is
# why §8.5 merges into the sealed Cycle-scoped integration feature branch and
# promotion to `dev` stays a human PR.
CRITERIA = (
    "records_present",
    # #303 §6. Counts agreeing while identities do not is the precise failure
    # the epic describes, and the previous five criteria could not see it: three
    # workstreams each declaring "11 admitted" satisfied the barrier even if
    # they had admitted three different sets of eleven.
    "manifest_agreement",
    "manifest_closure",
    "branches_merged",
    "dependency_closure",
    "regression_green",
    "digests_match",
    "journey_green",
    # #329. The other eight criteria are all about the INTEGRATED TREE and the
    # records' agreement with each other; none of them reads `dod`, so a Cycle
    # could go green with a whole lane having evaluated nothing and `/quality`
    # under-reported it silently. `declare_ready` now refuses to publish such a
    # record, but that is the producer half: a host that writes the record
    # directly, or an older plugin, bypasses it entirely. The barrier has to be
    # able to see it too, which is the whole reason the barrier reads records
    # rather than trusting clones.
    "dod_evaluated",
)

# The criteria a 1.1 record cannot speak to. A record that predates identity
# reporting is treated as UNPROVEN for these, never as passing -- the same rule
# the module already applies to a skipped journey, and for the same reason:
# treating absence of evidence as evidence is how a barrier quietly stops being
# a barrier.
_IDENTITY_CRITERIA = ("manifest_agreement", "manifest_closure", "dependency_closure")

# A record written before #329 carries no `dod.stories_evaluated` it can be held
# to. Treated as UNPROVEN, never as passing, for the reason stated above the
# identity criteria: treating absence of evidence as evidence is how a barrier
# quietly stops being a barrier.
_DOD_CRITERION = "dod_evaluated"

_DEFAULTS: dict[str, Any] = {
    "remote": "origin",
    # Include the collision-safe Cycle identity in new projects. An existing
    # project that explicitly configures the legacy ws/{id} pattern continues
    # to resolve it unchanged.
    "branch_pattern": "cycle/{cycle_id}/ws/{id}",
    "integration_branch_pattern": "cycle/{cycle_id}/integration",
    "promote_to": "dev",
    "mode": "all_or_nothing",
    "readiness_dir": ".synaptory/sync",
    "require_regression": True,
    "require_journey": True,
    "shared_digest_paths": [],
    "regression_script": "scripts/sync-regression.sh",
    "journey_script": "scripts/sync-journey.sh",
    "digest_script": "scripts/shared-digest.sh",
    "verdict_cache": True,
    # `at_sync` (default): workstream branches meet at the human merge, so there
    # is no per-unit integration event to require. `incremental` (#303 §4) opts
    # into publishing `integrated` events during the Cycle, and then Sync
    # requires every admitted unit to carry one.
    "integration_mode": "at_sync",
    "accept_unverified_events": False,
    # One-Cycle grace for a mixed-version fleet or a Cycle opened before
    # manifests existed. Waives the three IDENTITY criteria LOUDLY (they report
    # `waived: true` with the reason) rather than passing them silently.
    "allow_legacy_records": False,
    # §8.4 criterion 4: "or the delta is explicitly accepted". Flat
    # `path=digest` entries, because the yaml-lite parser cannot express a
    # nested map. Was previously honoured by _check_digests but unreachable
    # from config, i.e. a documented escape hatch that did not exist.
    "accepted_digest_deltas": {},
    # ── Coordination Cycle (#305) ───────────────────────────────────────────
    # Parsed here rather than in a second yaml-lite reader: the plugin ships no
    # PyYAML, and a second hand-rolled parser is a second set of quoting bugs.
    "composition_regression_script": "scripts/coordination-regression.sh",
    "require_composition_regression": True,
    "allow_drop": True,
    "max_ledger_age_s": 900,
}

_VALID_MODES = ("all_or_nothing", "per_workstream")

# Keys under `spq.sync` whose value is a list. Accepted in both the inline
# form (`["a", "b"]`) and the block form (`- "a"` on following lines).
_LIST_KEYS = ("shared_digest_paths", "accepted_digest_deltas")

# `spq` subsections this parser descends into. `slice` is the pre-SPD-173 name
# of `cycle` and is accepted read-only: dropping it would silently discard a
# working `.synaptory.yaml`'s `scope_defined`, and a silently-defaulted barrier
# setting is worse than a rename that is visible in the diff.
_CFG_SECTIONS = ("sync", "cycle", "slice", "coordination")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Config
#
# hooks/lib parses .synaptory.yaml by hand rather than importing PyYAML —
# `story_pipeline.parallelism_config` and `_config_dod_tier` set that
# precedent, and the plugin ships no yaml dependency. tracker/config.py has a
# richer `_parse_yaml_lite`, but it lives under skills/_shared/scripts and is
# not importable from here without sys.path surgery, so the `spq:` block is
# parsed locally. Scope is deliberately narrow: only the keys this module and
# spq_state_machine need.
# ---------------------------------------------------------------------------

def _config_text(project_dir: str | os.PathLike) -> str:
    try:
        return (Path(project_dir) / ".synaptory.yaml").read_text(encoding="utf-8")
    except OSError:
        return ""


def _scalar(raw: str) -> Any:
    v = raw.strip().strip('"').strip("'")
    low = v.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def _strip_comment(line: str) -> str:
    """Drop a trailing comment unless the '#' sits inside a quoted scalar."""
    if "#" not in line:
        return line.rstrip()
    head = line.split("#", 1)[0]
    if head.count('"') % 2 or head.count("'") % 2:
        return line.rstrip()
    return head.rstrip()


def _block_lines(text: str, top_key: str) -> list[str]:
    """Indented lines under a column-0 `<top_key>:` header."""
    out: list[str] = []
    inside = False
    for raw in text.splitlines():
        line = _strip_comment(raw)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            if inside:
                break
            inside = bool(re.fullmatch(rf"{re.escape(top_key)}\s*:\s*", line.strip()))
            continue
        if inside:
            out.append(line)
    return out


def _parse_workstreams(block: list[str]) -> list[dict[str, Any]]:
    """Parse the `workstreams:` list of dicts (yaml-lite handles no lists).

    Nested mappings are supported. The previous version kept a key only when it
    had a non-empty value, so a block header like

        - id: spine
          filter:
            type: label
            value: ws:spine

    dropped `filter` and then landed `type` and `value` as FLAT keys on the
    workstream. That is a silent mis-parse: the config looks accepted and the
    discriminator is simply gone. A workstream carrying a tracker discriminator
    is exactly what SPQ needs once #303 makes the manifest own admission.

    A header with no indented children yields `{}` rather than being dropped,
    so an empty block is visible instead of invisible.
    """
    start = None
    base = None
    for i, line in enumerate(block):
        if re.fullmatch(r"workstreams\s*:\s*", line.strip()):
            start = i + 1
            base = len(line) - len(line.lstrip())
            break
    if start is None:
        return []

    items: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    # Each frame is [indent-of-this-dict's-keys, dict, indent-of-the-key-that-
    # opened-it]. The first element is None until the first child line reveals
    # it; the third lets a childless header be popped rather than adopting a
    # dedented sibling as its child.
    stack: list[list[Any]] = []
    for line in block[start:]:
        indent = len(line) - len(line.lstrip())
        body = line.strip()
        if indent <= (base or 0):
            break  # dedented back to a sibling of `workstreams:`
        if body.startswith("- "):
            if current:
                items.append(current)
            current = {}
            body = body[2:].strip()
            # "- " is two columns wide, so the item's keys start past it.
            stack = [[indent + 2, current, indent]]
        if current is None:
            continue
        key_indent = indent + 2 if line.strip().startswith("- ") else indent
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", body)
        if not m:
            continue
        while len(stack) > 1:
            top = stack[-1]
            if top[0] is None:
                if key_indent > top[2]:
                    top[0] = key_indent
                    break
                stack.pop()
                continue
            if key_indent < top[0]:
                stack.pop()
                continue
            break
        target = stack[-1][1]
        key, value = m.group(1), m.group(2).strip()
        if value:
            target[key] = _scalar(value)
        else:
            child: dict[str, Any] = {}
            target[key] = child
            stack.append([None, child, key_indent])
    if current:
        items.append(current)
    return [w for w in items if w.get("id")]


def load_spq_config(project_dir: str | os.PathLike) -> dict[str, Any]:
    """Resolve the `spq:` block against defaults. Never raises.

    Unknown `mode` values fall back to `all_or_nothing` rather than erroring:
    a typo in a config key must not leave the barrier in an undefined blocking
    state, and all_or_nothing is the safe direction (§8.6).
    """
    text = _config_text(project_dir)
    block = _block_lines(text, "spq")
    cfg: dict[str, Any] = dict(_DEFAULTS)
    cfg["workstreams"] = _parse_workstreams(block)

    section: str | None = None
    section_indent = 0
    pending_list: str | None = None   # a key awaiting `- item` lines
    pending_indent = 0
    for line in block:
        indent = len(line) - len(line.lstrip())
        body = line.strip()

        # Block-list continuation. Without this, the ordinary YAML form
        #     shared_digest_paths:
        #       - "contracts/"
        # parsed to [] and `_check_digests` then WAIVED criterion 4 — a
        # fail-open in a barrier whose entire job is to fail closed,
        # triggered by valid YAML. Only the inline form used to work, which
        # is why the shipped template happened to be fine.
        if body.startswith("- "):
            if pending_list and indent > pending_indent:
                cfg[pending_list].append(_scalar(body[2:]))
            continue
        pending_list = None

        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", body)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if not val:
            if (
                section in _CFG_SECTIONS
                and indent > section_indent
                and key in _LIST_KEYS
            ):
                cfg[key] = []
                pending_list, pending_indent = key, indent
                continue
            section = key
            section_indent = indent
            continue
        if section in _CFG_SECTIONS and indent > section_indent:
            if key in _LIST_KEYS:
                cfg[key] = [
                    _scalar(x) for x in val.strip("[]").split(",") if x.strip()
                ]
            elif key in _DEFAULTS or key in ("scope_defined", "number_owner"):
                cfg[key] = _scalar(val)

    if cfg.get("mode") not in _VALID_MODES:
        cfg["mode"] = "all_or_nothing"

    # `accepted_digest_deltas` is authored as flat `path=digest` entries and
    # consumed as a mapping by `_check_digests`.
    raw_deltas = cfg.get("accepted_digest_deltas")
    if isinstance(raw_deltas, list):
        parsed_deltas: dict[str, str] = {}
        for entry in raw_deltas:
            if isinstance(entry, str) and "=" in entry:
                path, digest = entry.split("=", 1)
                parsed_deltas[_norm_path(path)] = digest.strip()
        cfg["accepted_digest_deltas"] = parsed_deltas
    elif not isinstance(raw_deltas, dict):
        cfg["accepted_digest_deltas"] = {}

    # The flat `- "path=digest"` form is the only one the yaml-lite parser can
    # read, and authoring it as a nested or inline MAPPING — the shape most
    # operators reach for first — previously yielded `{}` in silence. That is
    # a config-shaped waiver that appears to be set and is not, on the one
    # knob that unblocks a Cycle. Warn loudly instead (#239).
    if not cfg.get("accepted_digest_deltas") and _looks_like_a_mapping(
        block, "accepted_digest_deltas"
    ):
        print(
            "[sync_barrier] WARNING: accepted_digest_deltas is authored as a "
            "mapping and was IGNORED. The yaml-lite parser only reads flat "
            'entries:\n    accepted_digest_deltas:\n      - "contracts=<digest>"',
            file=sys.stderr,
        )
    return cfg


def _looks_like_a_mapping(block: list[str], key: str) -> bool:
    """True when `key` was authored as a mapping rather than a flat list.

    Inline (`key: {a: b}`) or nested (`key:` then an indented `a: b` that is
    not a `- item`). Used only to warn — see the call site.
    """
    for i, line in enumerate(block):
        body = line.strip()
        if not body.startswith(f"{key}:"):
            continue
        if "{" in body[len(key) + 1:]:
            return True
        indent = len(line) - len(line.lstrip())
        for nxt in block[i + 1:]:
            if not nxt.strip():
                continue
            if len(nxt) - len(nxt.lstrip()) <= indent:
                break              # dedented — the key's block is over
            if nxt.strip().startswith("-"):
                return False       # the supported flat form
            if ":" in nxt:
                return True        # an indented `a: b` pair
            break
    return False


def quorum_workstreams(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Workstreams that must produce a readiness record.

    Entries flagged `integration: true` are excluded (Q9): the integration
    clone holds its own spec slot so barrier cost stays out of any delivery
    workstream's cost/quality rollup, but it never declares readiness — it is
    the thing readiness is declared TO.
    """
    return [w for w in cfg.get("workstreams", []) if not w.get("integration")]


def _expand_branch_pattern(
    pattern: str,
    *,
    workstream_id: str = "",
    cycle_id: str | None = None,
    cycle_n: int | None = None,
) -> str:
    """Expand supported branch tokens without ``str.format`` surprises."""
    return (
        str(pattern)
        .replace("{id}", workstream_id)
        .replace("{cycle_id}", str(cycle_id or ""))
        .replace("{n}", "" if cycle_n is None else str(cycle_n))
    )


def branch_for(
    cfg: dict[str, Any],
    ws: dict[str, Any],
    *,
    cycle_id: str | None = None,
    cycle_n: int | None = None,
) -> str:
    """Branch name from config alone.

    Deliberately NOT read from the record: `collect` needs the branch before it
    can read the file that names it (§8.3). A per-workstream `branch:` override
    wins over `branch_pattern`.
    """
    override = ws.get("branch")
    if isinstance(override, str) and override:
        return override
    pattern = str(cfg.get("branch_pattern") or _DEFAULTS["branch_pattern"])
    if pattern == _DEFAULTS["branch_pattern"] and not cycle_id:
        # Pre-manifest Cycles have no collision-safe identity to substitute.
        # Preserve their historical transport so an upgrade does not make an
        # in-flight readiness record disappear from the collector.
        return "ws/%s" % str(ws.get("id", ""))
    return _expand_branch_pattern(
        pattern,
        workstream_id=str(ws.get("id", "")),
        cycle_id=cycle_id,
        cycle_n=cycle_n,
    )


def integration_branch(
    cfg: dict[str, Any], cycle_n: int, *, cycle_id: str | None = None
) -> str:
    pattern = str(
        cfg.get("integration_branch_pattern")
        or _DEFAULTS["integration_branch_pattern"]
    )
    if pattern == _DEFAULTS["integration_branch_pattern"] and not cycle_id:
        return "sync/cycle-%s" % cycle_n
    return _expand_branch_pattern(pattern, cycle_id=cycle_id, cycle_n=cycle_n)


def record_relpath(
    cfg: dict[str, Any], cycle_n: int, ws_id: str, cycle_id: str | None = None
) -> str:
    """Where a readiness record lives.

    With a `cycle_id` the record goes under `.synaptory/cycles/<cycle-id>/sync/`
    alongside the manifest and the events, keyed on the COLLISION-SAFE identity.
    The `cycle-<N>` form keys on the per-clone Cycle NUMBER, which two clones can
    both allocate -- so two different Cycles could write the same path.
    """
    if cycle_id:
        return f".synaptory/cycles/{cycle_id}/sync/{ws_id}.json"
    root = str(cfg.get("readiness_dir") or _DEFAULTS["readiness_dir"]).strip("/")
    return f"{root}/cycle-{cycle_n}/{ws_id}.json"


def record_relpath_legacy(cfg: dict[str, Any], cycle_n: int, ws_id: str) -> str:
    """Pre-SPD-173 readiness path, when the unit was called a Slice.

    Read-only. A cutover is not atomic across clones: an upgraded integration
    clone can be collecting while a workstream clone still on v1.1.x writes
    `slice-<N>/`. Without this fallback that workstream reads as *missing* and
    the barrier blocks on a phantom, which is the failure mode most likely to
    be misdiagnosed as a lost record. Nothing writes this path.
    """
    root = str(cfg.get("readiness_dir") or _DEFAULTS["readiness_dir"]).strip("/")
    return f"{root}/slice-{cycle_n}/{ws_id}.json"


# ---------------------------------------------------------------------------
# git / subprocess
# ---------------------------------------------------------------------------

def _run(
    args: list[str],
    cwd: str | os.PathLike,
    timeout: int | None = None,
) -> tuple[int, str, str]:
    """Run a command. `timeout=None` is intentional for proof scripts (§9)."""
    try:
        p = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError as e:
        return 127, "", str(e)
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"


def _git(project_dir: str | os.PathLike, *args: str) -> tuple[int, str, str]:
    return _run(["git", *args], project_dir, timeout=120)


def head_sha(project_dir: str | os.PathLike) -> str | None:
    code, out, _ = _git(project_dir, "rev-parse", "HEAD")
    return out.strip() if code == 0 and out.strip() else None


def current_branch(project_dir: str | os.PathLike) -> str | None:
    code, out, _ = _git(project_dir, "rev-parse", "--abbrev-ref", "HEAD")
    return out.strip() if code == 0 and out.strip() else None


def commit_exists(project_dir: str | os.PathLike, sha: str) -> bool:
    """True when `sha` names an object present in this clone."""
    if not sha:
        return False
    code, out, _ = _git(project_dir, "cat-file", "-t", sha)
    return code == 0 and out.strip() == "commit"


def is_ancestor(project_dir: str | os.PathLike, sha: str, of: str = "HEAD") -> bool:
    """True when `sha` is an ancestor of `of` — i.e. it really is merged in.

    This is what actually proves criterion 2. Review finding P1 on #226: the
    criterion previously passed on the branch NAME alone, so a human could
    create `sync/cycle-N`, merge NOTHING, and take the barrier green as long as
    the other proofs happened to pass. Reproduced before fixing: verdict green
    with zero workstream heads integrated.
    """
    if not commit_exists(project_dir, sha):
        return False
    code, _, _ = _git(project_dir, "merge-base", "--is-ancestor", sha, of)
    return code == 0


# ---------------------------------------------------------------------------
# Shared digests
# ---------------------------------------------------------------------------

def compute_shared_digests(
    project_dir: str | os.PathLike, cfg: dict[str, Any]
) -> dict[str, str]:
    """Digest each `shared_digest_paths` entry via the ONE committed script.

    §8.3: `declare-ready` (workstream clone) and `evaluate` (integration clone)
    must compute this identically, so there is one script invoked twice rather
    than two implementations. A missing script yields an explicit `error:`
    marker instead of an empty dict, so criterion 4 fails loudly rather than
    passing on absent data.
    """
    paths = [str(p) for p in (cfg.get("shared_digest_paths") or [])]
    if not paths:
        return {}
    script = str(cfg.get("digest_script") or _DEFAULTS["digest_script"])
    if not (Path(project_dir) / script).is_file():
        return {p: f"error:missing-digest-script:{script}" for p in paths}

    code, out, err = _run(["bash", script, *paths], project_dir, timeout=300)
    if code != 0:
        return {p: f"error:digest-script-exit-{code}" for p in paths}

    # Path-string normalisation. The script echoes the NORMALISED path it
    # walked, while config may say `contracts/`, `./contracts` or `contracts`
    # — §8.3's own example uses the trailing-slash form. Matching raw strings
    # would make criterion 4 report `no-digest-emitted` for a perfectly good
    # digest, so both sides are normalised for lookup while the returned dict
    # stays keyed by the ORIGINAL config string (records and comparisons must
    # be stable against config text, not against the script's echo).
    parsed: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            parsed[_norm_path(parts[0])] = parts[-1]

    resolved: dict[str, str] = {}
    for p in paths:
        digest = parsed.get(_norm_path(p))
        if digest is None:
            # A silently-dropped path would let criterion 4 pass on a shared
            # area nobody digested.
            resolved[p] = "error:no-digest-emitted"
        elif digest == _EMPTY_LISTING_SHA256:
            # The digest of an empty listing. A typo'd or deleted
            # shared_digest_path makes BOTH sides agree on "nothing", so
            # criterion 4 would pass vacuously — the failure mode is a
            # governed shared component silently dropping out of the barrier.
            resolved[p] = "error:no-tracked-files"
        else:
            resolved[p] = digest
    return resolved


# sha256 of zero bytes — what the digest script emits for a path with no
# tracked files. Never a legitimate digest for a shared component.
_EMPTY_LISTING_SHA256 = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)


def _norm_path(path: str) -> str:
    """Normalise a repo-relative path for digest-key comparison only."""
    p = str(path).strip().replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    return p.rstrip("/")


# ---------------------------------------------------------------------------
# Local evidence: DoD + replay (§8.2.1)
# ---------------------------------------------------------------------------

def _local_state(project_dir: str) -> dict[str, Any]:
    try:
        from story_pipeline import _read_state

        return _read_state(project_dir)
    except Exception:
        return {"current_stories": []}


def derive_dod(project_dir: str) -> dict[str, Any]:
    """DoD summary for THIS workstream, derived — never supplied by a caller.

    `aggregate_sprint_dod` reads one spec slot in one state file, so it sees
    exactly one workstream (§7.1 correction). That is the right scope here:
    this block is the workstream's own claim, and cross-workstream aggregation
    is the barrier's job.
    """
    out: dict[str, Any] = {
        "tier": None,
        "tier_source": None,
        "checks_failed": {},
        "stories_evaluated": 0,
        "stories_passed": 0,
    }
    state = _local_state(project_dir)
    try:
        from story_pipeline import aggregate_sprint_dod, resolve_dod_tier

        tier = resolve_dod_tier(project_dir, state)
        out["tier"] = tier.get("tier")
        out["tier_source"] = tier.get("tier_source")
        agg = aggregate_sprint_dod(state)
        out["stories_evaluated"] = agg.get("stories_evaluated", 0)
        out["stories_passed"] = agg.get("stories_passed", 0)
        failed: dict[str, list[str]] = {}
        for entry in agg.get("failed_stories", []):
            sid = entry.get("id")
            if sid:
                failed[str(sid)] = list(entry.get("failed_checks", []))
        out["checks_failed"] = failed
    except Exception as e:  # pragma: no cover — defensive
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def derive_replay(project_dir: str) -> dict[str, Any]:
    """Replay summary derived from local state + the local event log.

    WHY THE BARRIER STILL NEEDS THIS, precisely (correcting r4 §8.2.1):
    since v1.0.19 a replay mismatch IS mirrored to the control plane as
    `evidence_dod / returned` (`story_pipeline.py` calls
    `gate_emitter.emit_evidence_dod_replay_mismatch`, added under #181), so the
    blanket claim that replay outcomes never leave the machine is no longer
    true. What remains true, and is what actually matters here:

      * the barrier makes a LOCAL, BLOCKING decision and cannot depend on a CP
        round-trip — gate emission is deliberately best-effort and queues to the
        outbox when the CP is unreachable, so it is an audit channel, not a
        queryable precondition;
      * the DETAIL beyond the mismatch flag (how many commands replayed, which
        checks could not be replayed at all) exists only in local state; and
      * `signals.py` still states its store "never ships anywhere".

    So the record remains the channel the integration clone reads, and these
    counts are derived here rather than accepted from a caller.

    Two sources, deliberately:
      1. Pipeline state — `story["dod"]["checks"][cid]["replay"]` is the
         structured result `verification_runner` produced, and
         `["replay_mismatch"]` is the marker `dod_gate_block_reason` keys on.
         Authoritative for the CURRENT board.
      2. `evidence_replay_mismatch` events — durable history for stories that
         have already rolled off the board.
    Deduped on (story_id, check); state wins because it carries the detail.
    """
    out: dict[str, Any] = {
        "commands_replayed": 0,
        "checks_unreplayable": 0,
        "mismatches": [],
    }
    seen: set[tuple[str, str]] = set()

    state = _local_state(project_dir)
    for story in state.get("current_stories", []) or []:
        checks = ((story.get("dod") or {}).get("checks") or {})
        if not isinstance(checks, dict):
            continue
        for cid, entry in checks.items():
            if not isinstance(entry, dict):
                continue
            replay = entry.get("replay")
            if not isinstance(replay, dict):
                continue
            out["commands_replayed"] += int(replay.get("total") or 0)
            if replay.get("skipped"):
                out["checks_unreplayable"] += 1
            if entry.get("replay_mismatch"):
                sid = str(story.get("id", ""))
                key = (sid, str(cid))
                seen.add(key)
                failures = replay.get("failures") or []
                first = failures[0] if failures else {}
                out["mismatches"].append({
                    "story_id": sid,
                    "check": cid,
                    "command": first.get("command"),
                    "attested_exit_code": first.get("expected_exit_code"),
                    "replayed_exit_code": first.get("actual_exit_code"),
                    "source": "state",
                })

    try:
        from signals import _read_events  # same package; private by convention

        events = _read_events(project_dir)
    except Exception:
        events = []
    for ev in events:
        if ev.get("event") != "evidence_replay_mismatch":
            continue
        key = (str(ev.get("story_id", "")), str(ev.get("check", "")))
        if key in seen:
            continue
        seen.add(key)
        out["mismatches"].append({
            "story_id": ev.get("story_id"),
            "check": ev.get("check"),
            "command": ev.get("command"),
            "attested_exit_code": ev.get("attested_exit_code"),
            "replayed_exit_code": ev.get("replayed_exit_code"),
            "ts": ev.get("ts"),
            "source": "events",
        })
    return out


# ---------------------------------------------------------------------------
# declare-ready (workstream clone)
# ---------------------------------------------------------------------------

class BarrierError(RuntimeError):
    """Refusal that the caller must surface, not swallow."""


def declare_ready(
    project_dir: str,
    cycle_n: int,
    *,
    workstream: str | None = None,
    declared_by: str | None = None,
    run_regression: bool = True,
) -> dict[str, Any]:
    """Write this workstream's readiness record for Cycle `cycle_n`.

    NOTE the signature: there is deliberately no way to pass `dod`, `replay`,
    `work_units` or `shared_digests` in. §8.2.1 — a field the caller can set is
    decoration, not evidence. Everything is derived here or not present.

    Raises BarrierError when a replay mismatch is present: a workstream whose
    attested evidence did not reproduce has not finished its Work Units, and
    discovering that at Sync wastes the integration slot.
    """
    cfg = load_spq_config(project_dir)
    # Native identity (#303/#304/#305). `SYNAPTORY_ACTIVE_SPEC` is no longer
    # read: it remains the Scrum/Kanban Multi-Spec identity, and a clone
    # carrying a stale export must not declare readiness on another lane's
    # behalf -- a barrier record attributed to the wrong workstream is worse
    # than a missing one, because the quorum looks satisfied.
    ws_id = workstream or ""
    if not ws_id:
        try:
            ws_id = (
                _paths.resolve_identity(
                    project_dir, require_workstream=True
                ).workstream_id
                or ""
            )
        except _paths.IdentityError as exc:
            raise BarrierError(str(exc))

    known = {str(w.get("id")) for w in cfg.get("workstreams", [])}
    if known and ws_id not in known:
        raise BarrierError(
            f"workstream {ws_id!r} is not in spq.workstreams "
            f"(configured: {sorted(known)})"
        )

    state = _local_state(project_dir)

    # Local readiness invariants (review finding P1 on #226). Previously this
    # counted stories and published regardless, so an empty or stale board could
    # emit a schema-valid record and satisfy the presence quorum. The record is
    # already only a claim (§8.2.1); a claim that does not even match local
    # state is worse than none.
    local_cycle = state.get("current_cycle")
    if local_cycle is not None and int(local_cycle or 0) != int(cycle_n):
        raise BarrierError(
            f"refusing to declare ready for Cycle {cycle_n}: local state is on "
            f"Cycle {local_cycle}. Declaring for a Cycle this clone is not "
            "running would publish a record about work it never admitted."
        )

    lifecycle = state.get("lifecycle_state")
    if lifecycle and lifecycle != "CYCLE_EXECUTION":
        raise BarrierError(
            f"refusing to declare ready: lifecycle is {lifecycle}, not "
            "CYCLE_EXECUTION. Readiness is a statement about a Cycle being "
            "finished, so it is only meaningful from execution."
        )

    all_units = state.get("current_stories") or []
    cut = [s for s in all_units if s.get("state") == "cancelled"]
    admitted = [s for s in all_units if s.get("state") != "cancelled"]
    unfinished = [s for s in admitted if s.get("state") != "done"]
    if unfinished:
        raise BarrierError(
            "refusing to declare ready: "
            + ", ".join(f"{s.get('id')}={s.get('state')}" for s in unfinished[:6])
            + f" ({len(unfinished)} of {len(admitted)} Work Units not done). "
            "Finish them, or cut them from the Cycle with "
            "`spq_state_machine.py cut_work_unit` (§8.6) so the record reports "
            "the cut honestly."
        )
    if not admitted:
        raise BarrierError(
            "refusing to declare ready: no admitted Work Units on this board. "
            "An empty Cycle would satisfy the presence quorum while proving "
            "nothing was delivered."
        )

    dod = derive_dod(project_dir)
    if dod.get("checks_failed"):
        failed = "; ".join(
            f"{sid}: {', '.join(checks)}"
            for sid, checks in list(dod["checks_failed"].items())[:5]
        )
        raise BarrierError(
            f"refusing to declare ready: DoD checks failing — {failed}. A Work "
            "Unit whose DoD gate is red is not done, whatever its sub-state says."
        )

    # "Nothing failed" is NOT "checks ran". With `stories_evaluated: 0` the
    # `checks_failed` map above is empty and the guard passes vacuously, so a
    # lane that never evaluated a single Work Unit declared readiness exactly as
    # loudly as one that evaluated all of them. Measured across three hosts
    # (#329): api and cli reported `stories_evaluated: 2`, web reported 0 with
    # both units `done` and the regression green, and the barrier cleared.
    #
    # This is the CONSUMER half of #238. That fix made `transition_story`
    # auto-evaluate on the `-> done` edge, which fixes the producer -- but a
    # host that reaches `done` without going through the kernel (see #332)
    # leaves this guard as blind as it was before. `spq_state_machine.py:2669`
    # already documents this exact vacuity and this exact `stories_evaluated: 0`
    # symptom; what was missing was anything that ACTS on it.
    #
    # Every unit in `admitted` is `done` by now -- `unfinished` was refused
    # above -- so each one should have been evaluated on its way there.
    evaluated = int(dod.get("stories_evaluated") or 0)
    if evaluated < len(admitted):
        raise BarrierError(
            f"refusing to declare ready: {evaluated} of {len(admitted)} admitted "
            "Work Units have an evaluated DoD gate. The rest reached `done` "
            "without one, so this lane has no evidence to declare — and an "
            "empty `checks_failed` from an unevaluated board is indistinguishable "
            "from a clean one. Run `spq_state_machine.py evaluate_dod <id>` for "
            "each, or advance them through the kernel so the gate runs on the "
            "`reviewing -> done` edge."
        )

    replay = derive_replay(project_dir)
    if replay["mismatches"]:
        first = replay["mismatches"][0]
        raise BarrierError(
            f"refusing to declare ready: {len(replay['mismatches'])} evidence "
            f"replay mismatch(es) recorded locally, first on story "
            f"{first.get('story_id')} check {first.get('check')} "
            f"(`{first.get('command')}` attested exit "
            f"{first.get('attested_exit_code')} but replayed "
            f"{first.get('replayed_exit_code')}). Attested evidence that does "
            "not reproduce means the Work Unit is not done — fix or re-run it, "
            "or cut it from the Cycle (§8.6)."
        )

    # `--no-regression` is NOT a way around a required proof (review finding P1
    # on #226). It only suppresses a regression the config does not require.
    regression: dict[str, Any] = {"command": None, "exit_code": None, "skipped": True}
    if cfg.get("require_regression"):
        if not run_regression:
            raise BarrierError(
                "refusing to declare ready: --no-regression cannot bypass "
                "`spq.sync.require_regression: true`. Set the config to false if "
                "the Cycle genuinely has no regression to run, so the waiver is "
                "recorded where a reviewer can see it."
            )
        script = str(cfg.get("regression_script") or _DEFAULTS["regression_script"])
        regression = _run_proof_script(project_dir, script, label="regression")
        if regression.get("skipped") or regression.get("exit_code") != 0:
            raise BarrierError(
                "refusing to declare ready: required regression did not pass "
                f"({regression.get('reason') or 'exit ' + str(regression.get('exit_code'))}). "
                "Publishing a record whose own regression failed would make "
                "criterion 3 the only thing standing between a broken Cycle and "
                "a green barrier."
            )
    elif run_regression:
        script = str(cfg.get("regression_script") or _DEFAULTS["regression_script"])
        if (Path(project_dir) / script).is_file():
            regression = _run_proof_script(project_dir, script, label="regression")

    digests = compute_shared_digests(project_dir, cfg)
    bad_digests = {k: v for k, v in digests.items() if str(v).startswith("error:")}
    if bad_digests:
        raise BarrierError(
            f"refusing to declare ready: shared digests could not be computed — "
            f"{bad_digests}. A record carrying error markers would fail "
            "criterion 4 at the barrier, after the integration slot is spent."
        )

    # Identity is DERIVED here, never accepted as an argument -- the same rule
    # the aggregate evidence already follows. `declare_ready` deliberately has
    # no way to be handed a work-unit list, a manifest hash or an integrated
    # SHA, because a record whose identities the caller supplied proves nothing
    # about what this clone actually admitted and integrated.
    identity = _record_identity(project_dir, cycle_n, admitted, cut)

    record = {
        "schema_version": SCHEMA_VERSION,
        "cycle": cycle_n,
        "workstream": ws_id,
        "crew_lead": declared_by or os.environ.get("SYNAPTORY_UPN") or "",
        "branch": current_branch(project_dir),
        "head_sha": head_sha(project_dir),
        # Retained verbatim so a 1.1 reader (and `status()`) is unaffected.
        "work_units": {
            "admitted": len(admitted),
            "done": len([s for s in admitted if s.get("state") == "done"]),
            "cut": len(cut),
        },
        "shared_digests": digests,
        "regression": regression,
        "dod": dod,
        "replay": replay,
        "declared_ready_at": _now(),
        "declared_by": declared_by or os.environ.get("SYNAPTORY_UPN") or "",
    }
    record.update(identity)

    path = Path(project_dir) / record_relpath(
        cfg, cycle_n, ws_id, identity.get("cycle_id")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"record": record, "path": str(path.relative_to(Path(project_dir)))}


def resolve_cycle_id(project_dir: str, cycle_n: int) -> str | None:
    """The collision-safe Cycle identity for a Cycle NUMBER, or None.

    The integration clone discovers this exactly as a workstream clone does:
    from the committed transport, since the index and the pointer both live
    under the gitignored orchestrator tree and never travel.
    """
    try:
        import spq_state_machine as spq

        return spq._cycle_id_for_seq(project_dir, int(cycle_n))
    except Exception:  # noqa: BLE001 - no identity means the legacy path
        return None


def _identity_criteria(
    project_dir: str,
    cycle_n: int,
    cfg: dict[str, Any],
    collected: dict[str, Any],
) -> dict[str, Any]:
    """`manifest_agreement`, `manifest_closure`, `dependency_closure`.

    The gap #303 names: "readiness exchanges aggregate counts rather than exact
    Work Unit identities, manifest revision, and integrated commit SHAs." Three
    workstreams each declaring "11 admitted" satisfied the old barrier even if
    they had admitted three DIFFERENT sets of eleven.
    """
    records = collected.get("records") or {}
    allow_legacy = bool(cfg.get("allow_legacy_records"))

    legacy = sorted(
        ws for ws, rec in records.items()
        if str((rec or {}).get("schema_version") or "1.0") in LEGACY_SCHEMA_VERSIONS
    )
    # A Cycle opened before manifests existed is the same situation as a record
    # written before identity reporting: the closure is UNPROVABLE, not proven.
    # Detected as "no record carries a manifest hash", which cannot be confused
    # with a real manifest Cycle where at least one workstream declares one.
    pre_manifest = bool(records) and not any(
        (rec or {}).get("manifest_hash") for rec in records.values()
    )
    if legacy or pre_manifest:
        # A record that predates identity reporting is UNPROVEN for these three,
        # never passing. Same rule the module already applies to a skipped
        # journey: treating absence of evidence as evidence is how a barrier
        # quietly stops being a barrier. `allow_legacy_records` is a one-Cycle
        # grace period for a mixed-version fleet, and it WAIVES loudly rather
        # than passing silently.
        detail = (
            "workstream(s) %s declared on schema %s, which carries no Work Unit "
            "identities" % (", ".join(legacy), "/".join(LEGACY_SCHEMA_VERSIONS))
            if legacy
            else "this Cycle has no sealed manifest, so there is no admitted "
                 "set to close the declared Work Units against"
        )
        return {
            name: {
                "passed": bool(allow_legacy),
                "waived": bool(allow_legacy),
                "detail": detail + (
                    "; waived by spq.sync.allow_legacy_records — upgrade those "
                    "clones before the next Cycle"
                    if allow_legacy
                    else "; upgrade those clones, or set "
                         "spq.sync.allow_legacy_records for one Cycle"
                ),
                "code": "record_schema_too_old" if legacy else "no_manifest",
                "legacy_workstreams": legacy,
            }
            for name in _IDENTITY_CRITERIA
        }

    # ── manifest_agreement ──────────────────────────────────────────────
    hashes = {
        ws: str((rec or {}).get("manifest_hash") or "")
        for ws, rec in sorted(records.items())
    }
    distinct = {h for h in hashes.values() if h}
    local_hash = ""
    try:
        import spq_state_machine as spq

        discovered = resolve_cycle_id(project_dir, cycle_n)
        if discovered:
            local_hash = str(
                (spq.read_manifest(project_dir, discovered) or {}).get(
                    "manifest_hash"
                ) or ""
            )
    except Exception:  # noqa: BLE001
        local_hash = ""

    missing_hash = sorted(ws for ws, h in hashes.items() if not h)
    if local_hash:
        distinct.add(local_hash)
    agreement_passed = not missing_hash and len(distinct) <= 1
    if missing_hash:
        agreement_detail = (
            "workstream(s) %s declared no manifest hash" % ", ".join(missing_hash)
        )
    elif len(distinct) > 1:
        agreement_detail = (
            "%d different manifest revisions in play: %s. A workstream that "
            "hydrated a superseded manifest has been building against an "
            "admitted set the Cycle no longer has."
            % (len(distinct), ", ".join(sorted(h[:19] for h in distinct)))
        )
    else:
        agreement_detail = "all workstreams are on manifest %s" % (
            (next(iter(distinct)) if distinct else "")[:19] or "none"
        )
    criteria = {
        "manifest_agreement": {
            "passed": agreement_passed,
            "detail": agreement_detail,
            "hashes": hashes,
        }
    }

    # ── manifest_closure ────────────────────────────────────────────────
    admitted_by_manifest: set[str] = set()
    owners: dict[str, str] = {}
    try:
        import spq_manifest
        import spq_state_machine as spq

        discovered = resolve_cycle_id(project_dir, cycle_n)
        manifest = spq.read_manifest(project_dir, discovered) if discovered else {}
        if manifest:
            admitted_by_manifest = set(spq_manifest.admitted_ids(manifest))
            owners = spq_manifest.owners(manifest)
    except Exception:  # noqa: BLE001
        pass

    declared: set[str] = set()
    wrong_owner: list[str] = []
    for ws_id, rec in sorted(records.items()):
        ids = ((rec or {}).get("work_unit_ids") or {})
        for unit in list(ids.get("admitted") or []) + list(ids.get("cut") or []):
            declared.add(str(unit))
            expected = owners.get(str(unit))
            if expected and expected != ws_id:
                wrong_owner.append(
                    "%s declared by %s but owned by %s" % (unit, ws_id, expected)
                )

    if not admitted_by_manifest:
        closure_passed = False
        closure_detail = (
            "no sealed manifest is reachable from the integration clone, so the "
            "admitted set cannot be closed against anything"
        )
        missing_units: list[str] = []
        extra_units: list[str] = []
    else:
        missing_units = sorted(admitted_by_manifest - declared)
        extra_units = sorted(declared - admitted_by_manifest)
        closure_passed = not missing_units and not extra_units and not wrong_owner
        parts = []
        if missing_units:
            parts.append("no workstream declared: " + ", ".join(missing_units))
        if extra_units:
            parts.append("declared but not admitted: " + ", ".join(extra_units))
        if wrong_owner:
            parts.append("; ".join(wrong_owner))
        closure_detail = (
            "; ".join(parts)
            if parts
            else "the union of declared Work Units equals the manifest's "
                 "admitted set exactly (%d units)" % len(admitted_by_manifest)
        )
    criteria["manifest_closure"] = {
        "passed": closure_passed,
        "detail": closure_detail,
        "missing": missing_units,
        "extra": extra_units,
        "wrong_owner": wrong_owner,
    }

    # ── dependency_closure ──────────────────────────────────────────────
    unsatisfied: list[dict[str, Any]] = []
    unintegrated: list[str] = []
    policy_mode = ""
    try:
        import spq_state_machine as spq

        discovered = resolve_cycle_id(project_dir, cycle_n)
        manifest = spq.read_manifest(project_dir, discovered) if discovered else {}
        policy_mode = str(
            (manifest.get("integration_policy") or {}).get("mode") or ""
        )
    except Exception:  # noqa: BLE001
        pass

    for ws_id, rec in sorted(records.items()):
        for entry in ((rec or {}).get("dependency_closure") or {}).get(
            "unsatisfied"
        ) or []:
            unsatisfied.append({"workstream": ws_id, **entry})
        ids = ((rec or {}).get("work_unit_ids") or {})
        integrated = set((ids.get("integrated") or {}).keys())
        cut = set(ids.get("cut") or [])
        for unit in ids.get("admitted") or []:
            if str(unit) in cut or str(unit) in integrated:
                continue
            unintegrated.append("%s (%s)" % (unit, ws_id))

    # Integration closure is required only under an incremental policy. Under
    # all-at-once integration the merge IS Sync, so demanding per-unit
    # integration events beforehand would be demanding evidence of something
    # that has not happened yet.
    require_integration = policy_mode == "incremental"
    dep_passed = not unsatisfied and (not require_integration or not unintegrated)
    parts = []
    if unsatisfied:
        parts.append(
            "%d unsatisfied dependency edge(s): %s"
            % (
                len(unsatisfied),
                ", ".join(
                    "%s<-%s" % (e.get("unit_id"), e.get("dep")) for e in unsatisfied[:5]
                ),
            )
        )
    if require_integration and unintegrated:
        parts.append(
            "%d admitted Work Unit(s) not integrated: %s"
            % (len(unintegrated), ", ".join(unintegrated[:5]))
        )
    criteria["dependency_closure"] = {
        "passed": dep_passed,
        "detail": "; ".join(parts) if parts else "every declared edge is satisfied",
        "unsatisfied": unsatisfied,
        "unintegrated": unintegrated,
        "integration_policy": policy_mode or "unset",
    }
    return criteria


def _record_identity(
    project_dir: str,
    cycle_n: int,
    admitted: list[dict[str, Any]],
    cut: list[dict[str, Any]],
) -> dict[str, Any]:
    """The 2.0 identity block, derived from local state and the ledger.

    Returns the empty-ish shape when no manifest is reachable, rather than
    omitting the keys: a record that says "I have no manifest" is honest and
    lets the barrier report `record_schema_too_old`-style blocking, whereas a
    record with the keys missing is indistinguishable from a 1.1 record written
    by an older plugin.
    """
    block: dict[str, Any] = {
        "cycle_id": None,
        "manifest_hash": None,
        "manifest_revision": None,
        "runner_id": None,
        "work_unit_ids": {
            "admitted": sorted(str(s.get("id")) for s in admitted),
            "done": sorted(
                str(s.get("id")) for s in admitted if s.get("state") == "done"
            ),
            "cut": sorted(str(s.get("id")) for s in cut),
            "integrated": {},
        },
        "dependency_closure": {"satisfied": [], "unsatisfied": []},
    }
    try:
        import spq_paths
        import spq_state_machine as spq
    except Exception:  # noqa: BLE001
        return block

    try:
        ident = spq.identity(project_dir)
        block["runner_id"] = ident.runner_id
        if not ident.cycle_id:
            return block
        block["cycle_id"] = ident.cycle_id
        manifest = spq.read_manifest(project_dir, ident.cycle_id)
        if manifest:
            block["manifest_hash"] = manifest.get("manifest_hash")
            block["manifest_revision"] = manifest.get("manifest_revision")
    except Exception:  # noqa: BLE001
        return block

    # Per-unit integrated SHAs come from the unit's own integration sub-record,
    # which only `publish_event --condition integrated` writes and which is
    # verified by ancestry at publish time. Reading them from the board rather
    # than asking the caller is what makes them evidence.
    for story in admitted:
        integration = story.get("integration") or {}
        if str(integration.get("status") or "") == "integrated":
            block["work_unit_ids"]["integrated"][str(story.get("id"))] = {
                "sha": integration.get("commit_sha"),
                "ref": integration.get("integration_ref"),
                "event_id": integration.get("event_id"),
            }

    try:
        import story_pipeline as sp

        state = _local_state(project_dir)
        context = sp.dep_context(project_dir, state)
        for unit in admitted:
            verdict = sp.story_dep_status(state, unit, dep_context=context)
            for dep in unit.get("depends_on") or []:
                normalized = sp.normalize_dep(dep)
                entry = {
                    "unit_id": str(unit.get("id")),
                    "dep": normalized.get("unit_id"),
                    "condition": normalized.get("condition"),
                }
                if verdict["met"]:
                    block["dependency_closure"]["satisfied"].append(entry)
                else:
                    block["dependency_closure"]["unsatisfied"].append(entry)
    except Exception:  # noqa: BLE001 - closure stays empty rather than wrong
        pass
    return block


def _run_proof_script(
    project_dir: str, script: str, *, label: str
) -> dict[str, Any]:
    """Execute a committed proof script with NO timeout.

    §9: the 60s-per-command Evidence Contract cap and the 30s SubagentStop
    budget are hook-path limits. This runs outside the hook path precisely so a
    full cross-workstream regression can finish, so imposing a timeout here
    would reintroduce the constraint the design exists to escape.
    """
    if not (Path(project_dir) / script).is_file():
        return {
            "command": f"bash {script}",
            "exit_code": None,
            "skipped": True,
            "reason": f"{label} script not found: {script}",
        }
    code, out, err = _run(["bash", script], project_dir, timeout=None)
    result: dict[str, Any] = {
        "command": f"bash {script}",
        "exit_code": code,
        "skipped": False,
    }
    tail = (out + err).strip().splitlines()
    for line in reversed(tail[-40:]):
        m = re.match(r"^RESULT\s+(.*)$", line.strip())
        if m:
            for kv in m.group(1).split():
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    result[k] = _scalar(v)
            break
    if tail:
        result["summary"] = tail[-1][:200]
    return result


# ---------------------------------------------------------------------------
# collect (integration clone)
# ---------------------------------------------------------------------------

def collect(
    project_dir: str, cycle_n: int, *, fetch: bool = True
) -> dict[str, Any]:
    """Read every workstream's record WITHOUT merging (§8.5 step 1).

    r1 presented the criteria as a flat set, which hid an ordering problem:
    records live on workstream branches, so they are only co-visible after a
    merge — which is itself criterion 2. Reading via `git show` breaks that
    circularity, and a missing or malformed record fails here, before any
    integration work is spent.
    """
    cfg = load_spq_config(project_dir)
    remote = str(cfg.get("remote") or _DEFAULTS["remote"])
    out: dict[str, Any] = {
        "cycle": cycle_n,
        "mode": cfg.get("mode"),
        "fetched": False,
        "records": {},
        "missing": [],
        "warnings": [],
    }

    quorum = quorum_workstreams(cfg)
    if not quorum:
        # `records_present` must be set on EVERY return path: `evaluate` and
        # `status` both key on it, and a KeyError here would surface as a crash
        # rather than a blocked barrier. An empty quorum is never "present".
        out["records_present"] = False
        out["warnings"].append(
            "no workstreams configured — set spq.workstreams[] (§5.3); the "
            "barrier cannot form a quorum from an empty list"
        )
        return out

    if fetch:
        code, _, err = _git(project_dir, "fetch", remote, "--prune")
        out["fetched"] = code == 0
        if code != 0:
            # BLOCKING, not a warning (review finding P1 on #226). A failed
            # fetch means the remote-tracking refs are stale, so every record
            # read below could be an older Cycle's. Degrading that to a warning
            # let the barrier evaluate confidently against data it had not
            # actually refreshed.
            out["fetch_failed"] = f"git fetch {remote} failed: {err.strip()[:200]}"
            out["warnings"].append(out["fetch_failed"])

    cycle_id = resolve_cycle_id(project_dir, cycle_n)
    out["cycle_id"] = cycle_id

    # Config constructs the transport contract at Commit. Once sealed, the
    # manifest is authoritative so a later config edit cannot redirect a
    # consumer to a different branch than the one producers were assigned.
    manifest: dict[str, Any] = {}
    if cycle_id:
        try:
            import spq_state_machine as spq

            manifest = spq.read_manifest(project_dir, cycle_id) or {}
        except Exception:  # noqa: BLE001 - legacy Cycles have no manifest
            manifest = {}
    manifest_branches = {
        str(entry.get("id") or ""): str(entry.get("branch") or "")
        for entry in (manifest.get("workstreams") or [])
        if entry.get("id") and entry.get("branch")
    }
    out["integration_ref"] = str(manifest.get("integration_ref") or "")
    out["resolved_branches"] = {}

    for ws in quorum:
        ws_id = str(ws["id"])
        branch = manifest_branches.get(ws_id) or branch_for(
            cfg, ws, cycle_id=cycle_id, cycle_n=cycle_n
        )
        out["resolved_branches"][ws_id] = branch
        rel = record_relpath(cfg, cycle_n, ws_id, cycle_id)
        ref = f"{remote}/{branch}:{rel}"
        code, blob, err = _git(project_dir, "show", ref)
        if code != 0 and cycle_id:
            # A workstream that has not adopted the cycle_id path yet still
            # writes `cycle-<N>/`. Accept it here rather than reporting the
            # record as missing, which is the failure mode most likely to be
            # misdiagnosed as a lost record.
            numbered = record_relpath(cfg, cycle_n, ws_id)
            code, blob, err = _git(
                project_dir, "show", f"{remote}/{branch}:{numbered}"
            )
            if code == 0:
                rel = numbered
        if code != 0:
            # SPD-173 fallback: the workstream clone may still be on the
            # pre-rename plugin and writing `slice-<N>/`. Accept it, and say so
            # loudly enough that the clone actually gets upgraded.
            legacy_rel = record_relpath_legacy(cfg, cycle_n, ws_id)
            lcode, lblob, _ = _git(
                project_dir, "show", f"{remote}/{branch}:{legacy_rel}"
            )
            if lcode == 0:
                out["warnings"].append(
                    f"{ws_id}: readiness record found at the pre-SPD-173 path "
                    f"{legacy_rel} — this clone is on an older plugin and must "
                    f"be upgraded; the Slice/Cycle rename makes {rel} canonical"
                )
                rel, blob = legacy_rel, lblob
            else:
                out["missing"].append(
                    {"workstream": ws_id, "branch": branch, "path": rel,
                     "reason": err.strip()[:200] or "not found"}
                )
                continue
        try:
            record = json.loads(blob)
        except json.JSONDecodeError as e:
            out["missing"].append(
                {"workstream": ws_id, "branch": branch, "path": rel,
                 "reason": f"malformed JSON: {e}"}
            )
            continue

        if record.get("cycle") != cycle_n:
            out["missing"].append(
                {"workstream": ws_id, "branch": branch, "path": rel,
                 "reason": f"record is for cycle {record.get('cycle')}, not {cycle_n}"}
            )
            continue

        # The record's own `branch` is a cross-check only (§8.3). Disagreement
        # is worth surfacing but must never redirect the read, or a record
        # could point the collector at a branch of its own choosing.
        if record.get("branch") and record["branch"] != branch:
            out["warnings"].append(
                f"{ws_id}: record claims branch {record['branch']!r} but config "
                f"resolves {branch!r}; using config"
            )
        if record.get("replay", {}).get("mismatches"):
            out["warnings"].append(
                f"{ws_id}: record carries {len(record['replay']['mismatches'])} "
                "replay mismatch(es) — declare-ready should have refused"
            )
        out["records"][ws_id] = record

    out["records_present"] = not out["missing"]
    return out


# ---------------------------------------------------------------------------
# evaluate (integration clone)
# ---------------------------------------------------------------------------

def _verdict_cache_key(
    cycle_n: int, branch: str | None, collected: dict[str, Any]
) -> str:
    """Cache key covering the integration tree AND the readiness records.

    `verification_cache` invalidates on local tree state (HEAD + status +
    diff), which is necessary but NOT sufficient here: the records live on
    REMOTE refs, so a workstream declaring ready — the single most important
    state change the barrier reacts to — leaves the local tree untouched. Left
    on tree state alone, `evaluate` would keep serving a stale "blocked"
    verdict after the workstream it was waiting for became ready.

    So the records' own identity goes into the key: which workstreams reported,
    at which head_sha, and when. A new, changed, or withdrawn record produces a
    different key and forces a fresh evaluation.
    """
    fingerprint = ";".join(
        f"{ws}@{(rec or {}).get('head_sha', '?')}"
        f"#{(rec or {}).get('declared_ready_at', '?')}"
        for ws, rec in sorted((collected.get("records") or {}).items())
    )
    missing = ",".join(sorted(m["workstream"] for m in collected.get("missing", [])))
    digest = hashlib.sha256(
        f"{fingerprint}|missing={missing}".encode("utf-8")
    ).hexdigest()[:16]
    return f"sync_barrier:evaluate:cycle-{cycle_n}:{branch or '?'}:{digest}"


def evaluate_incremental(
    project_dir: str,
    cycle_n: int,
    *,
    unit_id: str | None = None,
    candidate_sha: str | None = None,
    remote: str = "",
) -> dict[str, Any]:
    """Did the merged Work Unit make the Cycle integration ref safe? (§303 §4)

    Incremental integration exists so the Cycle does not discover every conflict
    at once, at the end, when the only remedy left is cutting scope. This is the
    per-merge check: run the Cycle's OWN regression against the integration ref
    and report whether the policy's `merge_requires` are met. The caller names
    the candidate commit, and a green result carries a content-addressed
    attestation binding that candidate to the exact evaluated integration HEAD.

    Three deliberate boundaries.

    The HUMAN performs the merge. `modes/init.md` forbids agents committing or
    pushing to shared branches, and §8.5 step 2 keeps the merge a human act for
    exactly that reason. This function verifies the human merge result; it
    never performs one. `publish_event --condition integrated` must carry the
    returned attestation and refuses it if the integration ref moved later.

    It reuses the manifest's own `regression_script`, not a second one. Two
    spellings of "the regression" is how a gate quietly stops gating: the check
    that runs per merge must be the same check that runs at Sync, or a unit can
    pass here and fail there.

    It is NOT cached. `evaluate` caches its verdict against tree state because
    the barrier may be re-run repeatedly on an unchanged tree; here the tree has
    just changed by definition -- that is why someone is asking.
    """
    cfg = load_spq_config(project_dir)
    remote = remote or str(cfg.get("remote") or _DEFAULTS["remote"])
    cycle_id = resolve_cycle_id(project_dir, cycle_n)

    out: dict[str, Any] = {
        "cycle": cycle_n,
        "cycle_id": cycle_id,
        "unit_id": unit_id,
        "candidate_sha": candidate_sha,
        "verdict": "blocked",
        "blocking": [],
        "checks": {},
        "warnings": [],
    }

    manifest: dict[str, Any] = {}
    if cycle_id:
        try:
            import spq_state_machine as spq

            manifest = spq.read_manifest(project_dir, cycle_id) or {}
        except Exception as exc:  # noqa: BLE001
            out["warnings"].append("manifest unreadable: %s" % exc)

    if not manifest:
        out["blocking"].append("manifest")
        out["detail"] = (
            "no sealed manifest for Cycle %s, so there is no integration ref to "
            "check and no policy to check it against" % (cycle_id or cycle_n)
        )
        return out

    policy = manifest.get("integration_policy") or {}
    mode = str(policy.get("mode") or "at_sync")
    out["integration_policy"] = mode
    if mode != "incremental":
        # Under `at_sync` the merge IS Sync, so a per-unit pre-merge check has
        # nothing to gate. Reporting `not_applicable` rather than green matters:
        # a green here would read as "this merge was verified" when nothing was.
        out["verdict"] = "not_applicable"
        out["detail"] = (
            "integration_policy.mode is %r, so workstream branches meet at the "
            "Sync merge and there is no incremental merge to gate. Set it to "
            "'incremental' at Commit to enable per-unit integration." % mode
        )
        return out

    if not unit_id:
        out["blocking"].append("unit_required")
        out["detail"] = (
            "incremental evaluation requires --unit so its result can be bound "
            "to one admitted Work Unit"
        )
        return out

    # The unit must be admitted, and owned by whoever is asking. An event about
    # another lane's unit is the failure `spq_ledger.publish` refuses; refusing
    # the pre-check too means the operator learns it here, before the merge,
    # rather than after.
    try:
        import spq_manifest

        owners = spq_manifest.owners(manifest)
    except Exception:  # noqa: BLE001
        owners = {}
    if unit_id not in owners:
        out["blocking"].append("unit_admitted")
        out["detail"] = "%s is not admitted to Cycle %s" % (unit_id, cycle_id)
        return out
    out["owner_workstream"] = owners.get(unit_id)

    expected_ref = str(manifest.get("integration_ref") or "")
    branch = current_branch(project_dir)
    on_expected = bool(expected_ref) and branch == expected_ref
    out["checks"]["on_integration_ref"] = {
        "passed": on_expected,
        "detail": (
            "on %s" % branch if on_expected
            else "expected to run on %s, currently on %s. The merge is a human "
                 "step (§8.5 step 2) and this check runs on its result."
                 % (expected_ref or "<unset>", branch)
        ),
    }
    if not on_expected:
        out["blocking"].append("on_integration_ref")

    integration_head = head_sha(project_dir) or ""
    candidate = str(candidate_sha or "")
    candidate_present = bool(candidate) and commit_exists(project_dir, candidate)
    candidate_integrated = candidate_present and is_ancestor(
        project_dir, candidate, integration_head
    )
    out["checks"]["candidate_integrated"] = {
        "passed": candidate_integrated,
        "candidate_sha": candidate,
        "evaluated_head": integration_head,
        "detail": (
            "%s is an ancestor of evaluated HEAD %s"
            % (candidate[:12], integration_head[:12])
            if candidate_integrated
            else (
                "--sha is required and must name the Work Unit commit already "
                "merged into evaluated HEAD %s" % (integration_head[:12] or "<none>")
            )
        ),
    }
    if not candidate_integrated:
        out["blocking"].append("candidate_integrated")

    verification = manifest.get("verification") or {}
    script = str(
        verification.get("regression_script") or cfg.get("regression_script")
        or _DEFAULTS["regression_script"]
    )
    requires = [str(r) for r in (policy.get("merge_requires") or [])]
    out["merge_requires"] = requires

    # `merge_requires: []` means the policy asks for nothing, and an empty
    # requirement list must not masquerade as a passed regression. Run it anyway
    # and REPORT it, but only block when the policy actually demands it --
    # otherwise the operator gets a green with no evidence behind it.
    result = _run_proof_script(project_dir, script, label="regression")
    green = result.get("exit_code") == 0 and not result.get("skipped")
    out["checks"]["regression_green"] = {
        "passed": green,
        "required": "regression_green" in requires,
        "detail": result.get("reason") or result.get("summary") or "",
        "command": result.get("command"),
        "exit_code": result.get("exit_code"),
        "skipped": result.get("skipped"),
    }
    if "regression_green" in requires and not green:
        out["blocking"].append("regression_green")

    unknown = [r for r in requires if r != "regression_green"]
    if unknown:
        # Fail closed on a requirement nothing implements. Ignoring it would let
        # a policy declare a gate that silently never ran.
        out["blocking"].extend(unknown)
        out["warnings"].append(
            "merge_requires names %s, which this check cannot evaluate; "
            "blocking rather than assuming they pass" % ", ".join(sorted(unknown))
        )

    out["verdict"] = "green" if not out["blocking"] else "blocked"
    if out["verdict"] == "green":
        import spq_ledger

        evaluated_at = _now()
        out["evaluated_at"] = evaluated_at
        out["attestation"] = spq_ledger.build_incremental_evaluation(
            cycle_id=str(cycle_id),
            manifest_hash=str(manifest.get("manifest_hash") or ""),
            unit_id=unit_id,
            candidate_sha=candidate,
            evaluated_head=integration_head,
            integration_ref=expected_ref,
            checks=out["checks"],
            evaluated_at=evaluated_at,
        )
        out["detail"] = (
            "%s is present in regression-green HEAD %s on %s. Push that exact "
            "ref, then pass this result's attestation to `publish_event %s "
            "--condition integrated --sha %s --evaluation <json>`. Publication "
            "refuses the attestation if the ref moves."
            % (unit_id, integration_head[:12], expected_ref, unit_id, candidate)
        )
    else:
        out["detail"] = "blocking: %s" % ", ".join(out["blocking"])
    return out


def evaluate(
    project_dir: str, cycle_n: int, *, use_cache: bool = True
) -> dict[str, Any]:
    """Criteria 3, 4 and 5 on the integration branch (§8.5 step 3).

    Q10: the verdict is cached against the tree state via
    `verification_cache`, which already keys on HEAD + `status --porcelain` +
    `diff`. That is structurally the right invalidation, so this reuses it
    rather than writing new caching. A failed criterion 4 followed by a digest
    fix should not force a fresh full-suite run when the tree is provably
    unchanged.
    """
    cfg = load_spq_config(project_dir)
    branch = current_branch(project_dir)

    # `collect` runs BEFORE the cache probe, deliberately: it is cheap (git
    # show, no scripts) and its output is part of the cache key, so paying it
    # every time is what makes the cache safe. The expensive things — the
    # regression and journey scripts — are what the cache actually protects.
    #
    # AND IT FETCHES. Round-2 review finding on #226: the previous version
    # passed fetch=False here "so evaluation never mutates refs underneath
    # itself", which meant the `fetch_failed` signal added in round 1 could
    # never reach the authoritative decision. With populated-but-stale
    # remote-tracking refs and an unreachable remote, `evaluate` would happily
    # decide GREEN from records belonging to an older Cycle. Deciding on
    # unrefreshed data is a worse failure than re-pointing refs mid-run, so the
    # fetch is now part of the decision rather than an earlier courtesy.
    collected = collect(project_dir, cycle_n, fetch=True)

    # Checked BEFORE the cache probe on purpose: a cached green must not be
    # served while the remote is unreachable, or the cache becomes the very
    # staleness the fetch is meant to rule out.
    if collected.get("fetch_failed"):
        return {
            "cycle": cycle_n,
            "verdict": "blocked",
            "mode": cfg.get("mode"),
            "branch": branch,
            "expected_branch": (
                collected.get("integration_ref")
                or integration_branch(
                    cfg, cycle_n, cycle_id=collected.get("cycle_id")
                )
            ),
            "criteria": {},
            "blocking": ["fetch"],
            "workstreams": sorted(collected.get("records", {}).keys()),
            "warnings": collected.get("warnings", []),
            "fetch_failed": collected["fetch_failed"],
            "detail": (
                "cannot evaluate the barrier: "
                f"{collected['fetch_failed']}. Remote-tracking refs may be "
                "stale, so every readiness record read could belong to an "
                "earlier Cycle. Restore access to the remote and re-run; do "
                "not clear the barrier on unrefreshed data."
            ),
            "evaluated_at": _now(),
            "cache_served": False,
        }

    cache_key = _verdict_cache_key(cycle_n, branch, collected)

    if use_cache and cfg.get("verdict_cache"):
        try:
            import verification_cache

            hit = verification_cache.lookup(project_dir, cache_key)
            if hit:
                hit = dict(hit)
                hit["cache_served"] = True
                return hit
        except Exception:
            pass

    criteria: dict[str, Any] = {}

    criteria["records_present"] = {
        "passed": bool(collected.get("records_present")),
        "detail": (
            "all workstream records present"
            if collected.get("records_present")
            else f"missing: {[m['workstream'] for m in collected['missing']]}"
        ),
    }

    # ── #303 §6: closure by IDENTITY, not by count ───────────────────────
    criteria.update(
        _identity_criteria(project_dir, cycle_n, cfg, collected)
    )

    # Criterion 2. The MERGE is a human act (§8.5 step 2, §10.3), but whether it
    # happened is a fact this must verify rather than assume: every recorded
    # `head_sha` has to be an ancestor of the integration HEAD.
    expected_branch = (
        collected.get("integration_ref")
        or integration_branch(cfg, cycle_n, cycle_id=collected.get("cycle_id"))
    )
    on_expected = branch == expected_branch
    integration_head = head_sha(project_dir)
    unmerged: list[dict[str, Any]] = []
    for ws_id, rec in sorted((collected.get("records") or {}).items()):
        sha = (rec or {}).get("head_sha") or ""
        if not sha:
            unmerged.append({"workstream": ws_id, "reason": "record has no head_sha"})
        elif not commit_exists(project_dir, sha):
            unmerged.append({"workstream": ws_id, "head_sha": sha,
                             "reason": "commit not present in this clone; fetch it"})
        elif not is_ancestor(project_dir, sha):
            unmerged.append({"workstream": ws_id, "head_sha": sha,
                             "reason": "not an ancestor of the integration HEAD"})

    fetch_failed = collected.get("fetch_failed")
    c2_passed = on_expected and not unmerged and not fetch_failed
    if fetch_failed:
        c2_detail = f"blocking: {fetch_failed}"
    elif not on_expected:
        c2_detail = (
            f"expected to run on {expected_branch}, currently on {branch}. The "
            "merge is a human step (§8.5 step 2) — agents must not commit to "
            "shared branches."
        )
    elif unmerged:
        c2_detail = (
            f"{len(unmerged)} workstream head(s) not integrated: "
            + ", ".join(f"{u['workstream']} ({u['reason']})" for u in unmerged)
        )
    else:
        c2_detail = (
            f"on {branch}; all {len(collected.get('records') or {})} recorded "
            "head(s) are ancestors of HEAD"
        )
    criteria["branches_merged"] = {
        "passed": c2_passed,
        "detail": c2_detail,
        "head_sha": integration_head,
        "unmerged": unmerged,
    }

    if cfg.get("require_regression"):
        r = _run_proof_script(
            project_dir,
            str(cfg.get("regression_script") or _DEFAULTS["regression_script"]),
            label="regression",
        )
        criteria["regression_green"] = {
            "passed": r.get("exit_code") == 0 and not r.get("skipped"),
            "detail": r.get("reason") or r.get("summary") or "",
            "proof": r,
        }
    else:
        criteria["regression_green"] = {
            "passed": True, "detail": "require_regression: false", "waived": True,
        }

    criteria["digests_match"] = _check_digests(project_dir, cfg, collected)
    criteria["dod_evaluated"] = _check_dod_evaluated(collected)

    if cfg.get("require_journey"):
        j = _run_proof_script(
            project_dir,
            str(cfg.get("journey_script") or _DEFAULTS["journey_script"]),
            label="journey",
        )
        # A skipped journey is UNPROVEN, not passed. Treating skip as pass is
        # how a barrier quietly stops being a barrier.
        skipped = bool(j.get("skipped")) or j.get("status") == "skipped"
        criteria["journey_green"] = {
            "passed": j.get("exit_code") == 0 and not skipped,
            "detail": ("journey skipped — unproven, not passed" if skipped
                       else j.get("summary") or ""),
            "proof": j,
        }
    else:
        criteria["journey_green"] = {
            "passed": True, "detail": "require_journey: false", "waived": True,
        }

    mode = cfg.get("mode")
    blocking = [c for c in CRITERIA if not criteria.get(c, {}).get("passed")]
    if mode == "per_workstream":
        # Q1 ratified all_or_nothing as the default; per_workstream stays
        # implementable but changes what the Checkpoint demo means (§8.6), so
        # it relaxes ONLY the quorum, never the integrated-tree criteria.
        blocking = [c for c in blocking if c != "records_present"]

    verdict = {
        "cycle": cycle_n,
        "verdict": "green" if not blocking else "blocked",
        "mode": mode,
        "branch": branch,
        "expected_branch": expected_branch,
        "criteria": criteria,
        "blocking": blocking,
        "workstreams": sorted(collected.get("records", {}).keys()),
        "warnings": collected.get("warnings", []),
        "evaluated_at": _now(),
        "cache_served": False,
    }

    if use_cache and cfg.get("verdict_cache"):
        try:
            import verification_cache

            verification_cache.store(project_dir, cache_key, verdict)
        except Exception:
            pass
    return verdict


def _check_dod_evaluated(collected: dict[str, Any]) -> dict[str, Any]:
    """Did every reporting workstream actually EVALUATE its admitted units?

    The barrier's other eight criteria answer "does the integrated tree work"
    and "do the records agree". None answers "was the evidence gate ever run",
    which is how a Cycle went green with one lane at `stories_evaluated: 0`
    (#329).

    Deliberately NOT "did the gates pass". A failing gate is `declare_ready`'s
    job and blocks the record at source; this asks the weaker, unfalsifiable-by-
    omission question, because an empty `checks_failed` from an unevaluated
    board looks exactly like a clean one.

    A record with no `dod.stories_evaluated` is UNPROVEN. Pre-#329 records
    genuinely cannot speak to this, and treating "the key is missing" as "it
    passed" would make the criterion decorative on precisely the records most
    likely to need it.
    """
    lanes: dict[str, Any] = {}
    unevaluated: list[str] = []
    unproven: list[str] = []

    for ws_id, record in sorted((collected.get("records") or {}).items()):
        units = record.get("work_units") or {}
        admitted = int(units.get("admitted") or 0)
        dod = record.get("dod")
        if not isinstance(dod, dict) or "stories_evaluated" not in dod:
            unproven.append(ws_id)
            lanes[ws_id] = {"admitted": admitted, "evaluated": None}
            continue
        evaluated = int(dod.get("stories_evaluated") or 0)
        lanes[ws_id] = {"admitted": admitted, "evaluated": evaluated}
        if evaluated < admitted:
            unevaluated.append("%s (%d/%d)" % (ws_id, evaluated, admitted))

    if not lanes:
        # No records at all is `records_present`'s finding, not this one.
        # Reporting it twice would double-count one failure in `blocking`.
        return {"passed": True, "detail": "no records to evaluate", "lanes": {}}

    detail = ""
    if unevaluated:
        detail = ("Work Units reached `done` with no DoD evaluation: "
                  + ", ".join(unevaluated))
    elif unproven:
        detail = ("records predate the evidence-gate criterion and cannot "
                  "prove it: " + ", ".join(unproven))

    return {
        "passed": not unevaluated and not unproven,
        "detail": detail or "every admitted Work Unit has an evaluated DoD gate",
        "lanes": lanes,
        "unevaluated": unevaluated,
        "unproven": unproven,
    }


def _shared_owner_id(cfg: dict[str, Any]) -> str | None:
    """The workstream authorised to change shared components (§12)."""
    for w in cfg.get("workstreams", []) or []:
        if w.get("shared_owner"):
            return str(w.get("id"))
    return None


def _shared_paths_touched_by(
    project_dir: str,
    cfg: dict[str, Any],
    ws_id: str,
    paths: list[str],
    *,
    branch: str = "",
) -> list[str] | None:
    """Files under `paths` that this workstream's branch actually changed.

    The fork point is taken against the PROMOTION base (`promote_to`, e.g.
    `dev`), never against the integration branch's HEAD. That distinction is
    load-bearing and cost a wrong first implementation: `evaluate` runs after
    the human merge, and once a branch is merged into HEAD, `merge-base HEAD
    <branch>` IS that branch's own tip, so the diff is empty and every
    violation reads as clean. Forking against `promote_to` is stable no matter
    what has already been merged.

    Returns None when git cannot answer (no repo, missing refs) so the caller
    falls back rather than guessing.
    """
    remote = str(cfg.get("remote") or "origin")
    resolved = branch or branch_for(cfg, {"id": ws_id})
    ref = f"{remote}/{resolved}"
    promote_to = str(cfg.get("promote_to") or "dev")

    base = ""
    for mainline in (f"{remote}/{promote_to}", promote_to):
        code, out, _ = _git(project_dir, "merge-base", mainline, ref)
        if code == 0 and out.strip():
            base = out.strip()
            break
    if not base:
        return None
    code, out, _ = _git(project_dir, "diff", "--name-only", base, ref, "--", *paths)
    if code != 0:
        return None
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _check_digests(
    project_dir: str, cfg: dict[str, Any], collected: dict[str, Any]
) -> dict[str, Any]:
    """Criterion 4: no NON-OWNER workstream changed a shared component (#239).

    This used to compare every workstream's claimed digest against the
    post-merge integrated tree, and that produced exactly the wrong answer
    whenever the `shared_owner` did its job. A non-owner records the digest of
    the shared component as it stood when it declared readiness — the
    pre-change value, precisely because it correctly never touched it. Once the
    owner's authorised change is merged, the integrated digest moves and every
    honest non-owner's claim becomes "drift".

    Measured in the SPQ pilot: the owner changed `contracts/events.json`, the
    two consumers changed nothing (`git diff` against the merge-base: 0 files
    each), the merge was clean and 37 tests passed — and the barrier blocked,
    naming the two innocent workstreams while the only actual editor passed.
    The comparison could not distinguish an illegal edit from an honest
    declaration against the earlier state, and only the second is reachable in
    a Cycle where the owner changes anything.

    The question is now asked directly: did this workstream's branch modify the
    shared paths, relative to the merge-base it branched from? That is
    independent of what the owner did, independent of declaration ordering, and
    needs no operator bookkeeping. The owner is exempt because changing shared
    components is its role; its diff is governed by review, which is where that
    authority already lives.

    When git cannot answer, the old digest comparison still runs, so a caller
    with no repository keeps the previous behaviour rather than silently
    passing.
    """
    paths = [str(p) for p in (cfg.get("shared_digest_paths") or [])]
    if not paths:
        return {
            "passed": True,
            "detail": "no shared_digest_paths configured",
            "waived": True,
        }

    integrated = compute_shared_digests(project_dir, cfg)
    bad = {p: d for p, d in integrated.items() if str(d).startswith("error:")}
    if bad:
        return {
            "passed": False,
            "detail": f"could not digest the integrated tree: {bad}",
            "integrated": integrated,
        }

    accepted = cfg.get("accepted_digest_deltas") or {}
    owner = _shared_owner_id(cfg)
    drift: list[dict[str, Any]] = []
    fell_back = False

    for ws_id, record in (collected.get("records") or {}).items():
        # §8.2.1 requires the field, so its absence is a record-completeness
        # problem regardless of which mechanism judges the value.
        for path in paths:
            if (record.get("shared_digests") or {}).get(path) is None:
                drift.append({"workstream": ws_id, "path": path,
                              "reason": "no digest in record"})

        if ws_id == owner:
            continue  # authorised to change shared components (§12)

        touched = _shared_paths_touched_by(
            project_dir,
            cfg,
            ws_id,
            paths,
            branch=str((collected.get("resolved_branches") or {}).get(ws_id) or ""),
        )
        if touched is None:
            fell_back = True
            for path in paths:
                claimed = (record.get("shared_digests") or {}).get(path)
                if claimed is None or claimed == integrated.get(path):
                    continue
                if accepted.get(_norm_path(path)) == claimed:
                    continue
                drift.append({
                    "workstream": ws_id, "path": path,
                    "claimed": claimed, "integrated": integrated.get(path),
                    "reason": "digest mismatch (git unavailable for this clone)",
                })
        elif touched:
            drift.append({
                "workstream": ws_id, "files": touched,
                "reason": "modified a shared component it does not own",
            })

    if not drift:
        detail = (
            "no non-owner workstream modified a shared component"
            + ("; owner: " + owner if owner else "; no shared_owner configured")
        )
    else:
        detail = (
            f"{len(drift)} violation(s); shared components are governed at "
            "COMMIT by the shared_owner (§12). A non-owner that changed one "
            "needs adjudication, or record the delta as accepted with a flat "
            'entry: accepted_digest_deltas:\n  - "<path>=<digest>"'
        )
    return {
        "passed": not drift,
        "detail": detail,
        "drift": drift,
        "integrated": integrated,
        "shared_owner": owner,
        "used_digest_fallback": fell_back,
    }


# ---------------------------------------------------------------------------
# clear (integration clone)
# ---------------------------------------------------------------------------

def clear(
    project_dir: str, cycle_n: int, *, cleared_by: str | None = None
) -> dict[str, Any]:
    """Record a green verdict and emit the gate (§8.5 step 4).

    Does NOT transition the lifecycle itself — `spq_state_machine.clear_sync`
    owns that, so the barrier stays unit-testable without a state machine and
    there is exactly one writer of lifecycle_state.
    """
    verdict = evaluate(project_dir, cycle_n)
    if verdict["verdict"] != "green":
        raise BarrierError(
            f"cannot clear Cycle {cycle_n}: {verdict['blocking']} still "
            f"blocking. The barrier is all-or-nothing by design (§8.6) — a "
            "workstream that cannot make it cuts scope rather than merging "
            "late."
        )

    who = cleared_by or os.environ.get("SYNAPTORY_UPN") or ""
    try:
        import spq_state_machine as _spq

        state = _spq.read_state(project_dir)
        state["sync"] = {
            "cycle": cycle_n,
            "verdict": "green",
            "cleared_at": _now(),
            "cleared_by": who,
            "head_sha": verdict["criteria"]["branches_merged"].get("head_sha"),
            "workstreams": verdict["workstreams"],
        }
        _spq._write_state(project_dir, state)
    except Exception as e:  # pragma: no cover — defensive
        verdict.setdefault("warnings", []).append(f"state write failed: {e}")

    # §13.2: SPQ emits no new gate types. `evidence_dod` scoped to CYCLE-{N}
    # is accepted as-is because GateEvent.target_id is a plain String(128) with
    # no FK. Accepted cost: on /overview the Cycle barrier is indistinguishable
    # from per-Work-Unit DoD gates in the counts.
    try:
        from gate_emitter import emit_evidence_dod_accepted

        emit_evidence_dod_accepted(
            f"CYCLE-{cycle_n}", who, project_dir=project_dir
        )
    except Exception:
        pass

    _write_barrier_receipt(project_dir, cycle_n, verdict)
    return verdict


def _write_barrier_receipt(
    project_dir: str, cycle_n: int, verdict: dict[str, Any]
) -> str | None:
    """Write `SYNC-{N}-barrier.json`.

    Orchestrator-authored receipts use a descriptive filename suffix rather
    than a role abbreviation (`orchestrator` has no entry in the contract's
    role_abbrevs map) — same convention as inception's `INCEPTION-design.json`.
    `story_id` is `SYNC-{N}`, which satisfies `^[A-Z][A-Z0-9]*-\\d+$`.
    """
    try:
        from story_pipeline import _resolve_receipts_dir

        receipts = Path(_resolve_receipts_dir(project_dir))
    except Exception:
        receipts = Path(project_dir) / ".synaptory" / ".orchestrator" / "receipts"

    commands: list[Any] = []
    for name in ("regression_green", "journey_green"):
        proof = (verdict.get("criteria", {}).get(name) or {}).get("proof")
        if proof and not proof.get("skipped") and proof.get("exit_code") is not None:
            commands.append({
                "command": proof["command"],
                "exit_code": proof["exit_code"],
                "summary": proof.get("summary", "")[:200],
            })
    if not commands:
        # Every receipt must carry at least one verification command or the
        # SubagentStop hook errors in autonomous mode.
        commands.append("test -f docs/spq-lifecycle-design.md")

    host = os.environ.get("SYNAPTORY_HOST", "claude").strip().lower()
    backend = host if host in {"claude", "codex", "cursor"} else "claude"
    receipt = {
        "story_id": f"SYNC-{cycle_n}",
        "role": "orchestrator",
        "backend": backend,
        "model": os.environ.get(
            "SYNAPTORY_MODEL", f"{backend}-runtime-unattributed"
        ),
        "artifacts": [],
        "metrics": {
            "workstreams": len(verdict.get("workstreams", [])),
            "criteria_passed": sum(
                1 for c in verdict.get("criteria", {}).values() if c.get("passed")
            ),
            "criteria_total": len(CRITERIA),
        },
        "verification_commands": commands,
        "verification_summary": (
            f"Cycle {cycle_n} Sync barrier {verdict['verdict']}"
        ),
        "token_usage": {
            "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
            "stage": "orchestrator",
        },
        "completed_at": _now(),
    }
    try:
        receipts.mkdir(parents=True, exist_ok=True)
        path = receipts / f"SYNC-{cycle_n}-barrier.json"
        path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        return str(path)
    except OSError:
        return None


def status(project_dir: str, cycle_n: int) -> dict[str, Any]:
    """Barrier state for the tracker mirror (§8.4).

    The tracker is a mirror, never the source of truth, so this is read-only
    and runs no proof scripts.
    """
    cfg = load_spq_config(project_dir)
    # fetch=False here is deliberate and NOT the bug fixed in evaluate: status
    # is a read-only mirror for the tracker, it gates nothing, and it must stay
    # usable offline. It reports `stale: true` so a reader knows the difference.
    collected = collect(project_dir, cycle_n, fetch=False)
    per_ws = []
    for ws in quorum_workstreams(cfg):
        ws_id = str(ws["id"])
        rec = collected.get("records", {}).get(ws_id)
        per_ws.append({
            "workstream": ws_id,
            "branch": str(
                (collected.get("resolved_branches") or {}).get(ws_id)
                or branch_for(
                    cfg,
                    ws,
                    cycle_id=collected.get("cycle_id"),
                    cycle_n=cycle_n,
                )
            ),
            "ready": rec is not None,
            "work_units": (rec or {}).get("work_units"),
            "declared_ready_at": (rec or {}).get("declared_ready_at"),
        })
    return {
        "cycle": cycle_n,
        "mode": cfg.get("mode"),
        "integration_branch": (
            collected.get("integration_ref")
            or integration_branch(
                cfg, cycle_n, cycle_id=collected.get("cycle_id")
            )
        ),
        "workstreams": per_ws,
        "ready_count": sum(1 for w in per_ws if w["ready"]),
        "quorum": len(per_ws),
        "records_present": collected.get("records_present", False),
        "warnings": collected.get("warnings", []),
        "stale": True,  # no fetch: a mirror, never a gate
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _flag(args: list[str], name: str) -> str | None:
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 1
    action, project_dir = argv[1], argv[2]
    rest = argv[3:]

    def _cycle() -> int:
        for a in rest:
            if re.fullmatch(r"\d+", a):
                return int(a)
        raise BarrierError("a cycle number is required")

    try:
        if action == "config":
            print(json.dumps(load_spq_config(project_dir), indent=2, sort_keys=True))
        elif action == "declare-ready":
            out = declare_ready(
                project_dir, _cycle(),
                workstream=_flag(rest, "--workstream"),
                declared_by=_flag(rest, "--declared-by"),
                run_regression="--no-regression" not in rest,
            )
            print(json.dumps(out, indent=2, sort_keys=True))
        elif action == "collect":
            out = collect(project_dir, _cycle(), fetch="--no-fetch" not in rest)
            print(json.dumps(out, indent=2, sort_keys=True))
            # Non-zero on fetch failure or a missing record. Exiting 0 while
            # printing "fetch failed" invited an operator to read the output as
            # informational and carry on to the merge (round-2 review on #226).
            if out.get("fetch_failed"):
                return 4
            if not out.get("records_present"):
                return 3
        elif action == "evaluate":
            out = evaluate(project_dir, _cycle(), use_cache="--no-cache" not in rest)
            print(json.dumps(out, indent=2, sort_keys=True))
            return 0 if out["verdict"] == "green" else 3
        elif action == "evaluate-incremental":
            out = evaluate_incremental(
                project_dir,
                _cycle(),
                unit_id=_flag(rest, "--unit"),
                candidate_sha=_flag(rest, "--sha"),
            )
            print(json.dumps(out, indent=2, sort_keys=True))
            # `not_applicable` exits 0: the policy asks for nothing here, and a
            # non-zero would read as a failure the operator must fix.
            return 0 if out["verdict"] in ("green", "not_applicable") else 3
        elif action == "clear":
            print(json.dumps(
                clear(project_dir, _cycle(), cleared_by=_flag(rest, "--cleared-by")),
                indent=2, sort_keys=True))
        elif action == "status":
            print(json.dumps(status(project_dir, _cycle()), indent=2, sort_keys=True))
        else:
            print(f"Unknown action: {action}", file=sys.stderr)
            return 1
    except BarrierError as e:
        print(f"[sync_barrier] {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
