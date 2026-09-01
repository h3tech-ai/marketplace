#!/usr/bin/env python3
"""Ship sealed SPQ manifests to the control plane. Best-effort, never blocking.

Without this the `cycle_manifests` and `coordination_cycles` tables, their
ingest endpoints, and the `/cycles` and `/coordination-cycles` pages have no
producer at all: an operator has to run `synaptory telemetry cycle-manifest` by
hand, which nothing tells them to do, so in practice the views stay empty
forever. That is the same defect #303 shipped when it added `cycle_id` columns
with no field on any request model, repeated one level up.

The control plane is an OBSERVER. Git holds the hash-sealed manifest and the
barrier reads it there, so a failure to ship must never fail a ceremony -- the
same posture `gate_emitter` takes, and for the same reason: a lifecycle
transition cannot depend on the control plane being reachable. Every path here
returns a bool and swallows its own errors.

Python 3.9 compatible: this file is projected into every host package.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

_TIMEOUT_S = 20.0

#: Per-clone record of which manifest hashes have reached the control plane.
#:
#: Local and gitignored (`.orchestrator/` is), because delivery is a property of
#: THIS clone's CLI and control plane, not of the Cycle. A committed marker would
#: let one clone's successful ship suppress every other clone's -- harmless while
#: the row exists, wrong the moment a clone is pointed at a different control
#: plane.
#:
#: Keyed on `manifest_hash` because that is exactly what the ingest endpoints
#: dedupe on, so a marker means precisely "the row this would create already
#: exists" and nothing weaker.
_SHIPPED_RELDIR = os.path.join(
    ".synaptory", ".orchestrator", "spq", ".shipped-manifests"
)


def _resolve_cli() -> Optional[str]:
    """The canonical resolver, borrowed rather than re-derived.

    `gate_emitter._resolve_cli` already picks the channel sibling matching the
    active plugin's CP stamp. A second implementation here is how the two come
    to disagree about which control plane a project reports to.
    """
    try:
        from gate_emitter import _resolve_cli as resolve

        return resolve()
    except Exception:  # noqa: BLE001
        return None


def _ship(verb: str, manifest: Dict[str, Any], project_dir: Optional[str]) -> bool:
    cli = _resolve_cli()
    if cli is None:
        return False
    try:
        options: Dict[str, Any] = {
            "input": json.dumps(manifest),
            "capture_output": True,
            "text": True,
            "timeout": _TIMEOUT_S,
        }
        # The CLI resolves project_id by walking up from cwd, so it has to run
        # inside the governed project rather than wherever the host happens to
        # have started.
        if project_dir:
            options["cwd"] = str(project_dir)
        result = subprocess.run([cli, "telemetry", verb, "--file", "-", "--quiet"],
                                **options)
    except Exception as exc:  # noqa: BLE001
        print("manifest_emitter: %s failed (%s); the CLI outbox retries on the "
              "next session." % (verb, exc), file=sys.stderr)
        return False
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        print("manifest_emitter: %s failed (%s); git remains authoritative."
              % (verb, tail[-1] if tail else "exit %d" % result.returncode),
              file=sys.stderr)
        return False
    return True


def emit_cycle_manifest(
    manifest: Dict[str, Any], *, project_dir: Optional[str] = None
) -> bool:
    """Record a sealed Cycle manifest (#303). Idempotent on its own hash."""
    if not manifest or not manifest.get("manifest_hash"):
        return False
    return _ship_and_mark("cycle-manifest", manifest, project_dir)


def emit_coordination_manifest(
    manifest: Dict[str, Any], *, project_dir: Optional[str] = None
) -> bool:
    """Record a sealed Coordination Cycle manifest (#305).

    Called on every REVISION too, not just the open: the table is append-only,
    so dropping a late child inserts a row rather than updating one, and that
    row is the only record of what the release contained before the drop.
    """
    if not manifest or not manifest.get("manifest_hash"):
        return False
    return _ship_and_mark("coordination-manifest", manifest, project_dir)


# ── retry (#334 G8) ─────────────────────────────────────────────────────────
#
# Shipping used to be one attempt inside a bare `try/except: pass` at the
# emission site, which made every transient failure permanent: on 2026-09-01 it
# failed with `no project_id resolved` and `cycle_manifests` stayed empty until
# a manifest was shipped by hand, so `/cycles` had no producer at all. The
# non-blocking property was right -- git holds the seal and the barrier reads it
# there -- but "best-effort" had been implemented as "exactly once, then forget".
#
# The retry is a SWEEP over the sealed manifests already on disk, not a queue of
# pending emissions, for three reasons:
#
#   1. The sealed manifest is already durable, hash-named and authoritative. A
#      parallel spool would be a second copy of it that can disagree.
#   2. It catches losses a queue-at-emission never would. `seal_manifest`
#      returns early on the hydration and already-sealed paths, so a hydrated
#      workstream never reached the emit call at all -- despite every workstream
#      being supposed to ship the same seal.
#   3. It removes the ordering hazard the playbook had to warn about (the
#      control-plane project must exist BEFORE `open_cycle`). It no longer must:
#      the next sweep ships what the earlier attempt could not.


def _marker_dir(project_dir: str) -> str:
    return os.path.join(str(project_dir), _SHIPPED_RELDIR)


def _marker_path(project_dir: str, manifest_hash: str) -> str:
    # basename() so a hash read from a manifest on disk can never walk out of
    # the marker directory, however malformed the document is.
    return os.path.join(
        _marker_dir(project_dir), os.path.basename(str(manifest_hash))
    )


def already_shipped(project_dir: Optional[str], manifest_hash: str) -> bool:
    """Whether this clone has a marker for `manifest_hash`."""
    if not project_dir or not manifest_hash:
        return False
    return os.path.exists(_marker_path(project_dir, manifest_hash))


def _mark_shipped(project_dir: Optional[str], manifest_hash: str) -> None:
    if not project_dir or not manifest_hash:
        return
    try:
        os.makedirs(_marker_dir(project_dir), exist_ok=True)
        with open(_marker_path(project_dir, manifest_hash), "w") as fh:
            fh.write("")
    except OSError:
        # A missing marker costs one redundant ship, which the ingest collapses.
        # Failing the caller here would trade an idempotent retry for a lost
        # manifest, which is the bug this whole section exists to fix.
        pass


def _ship_and_mark(
    verb: str, manifest: Dict[str, Any], project_dir: Optional[str]
) -> bool:
    """Ship unless this clone already has, then record that it has.

    The marker gates the DIRECT emissions as well as the sweep, which is what
    keeps its meaning single: present iff the row this document would create
    already exists. Without the guard, `seal_manifest` re-POSTed the same
    manifest on every re-open — harmless against an idempotent ingest, but it
    made the marker mean "the sweep may skip this" rather than "delivered".

    A revision seals a NEW hash, so it is never suppressed by its predecessor's
    marker. That matters: `cycle_manifests` is append-only precisely so each
    revision is its own row.

    Returns True when the manifest is delivered — including when it already was.
    """
    digest = manifest.get("manifest_hash", "")
    if already_shipped(project_dir, digest):
        return True
    if _ship(verb, manifest, project_dir):
        _mark_shipped(project_dir, digest)
        return True
    return False


def _pending(project_dir: str) -> List[Tuple[str, Dict[str, Any]]]:
    """Sealed manifests on disk that carry no shipped marker."""
    try:
        import spq_paths
    except ImportError:
        return []
    out: List[Tuple[str, Dict[str, Any]]] = []
    roots = (
        ("cycle-manifest", os.path.join(spq_paths.spq_root(project_dir), "cycles")),
        ("coordination-manifest", spq_paths.coordination_root(project_dir)),
    )
    for verb, root in roots:
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for entry in entries:
            path = os.path.join(root, entry, "manifest.json")
            try:
                with open(path, encoding="utf-8") as fh:
                    manifest = json.load(fh)
            except (OSError, ValueError):
                continue
            digest = (manifest or {}).get("manifest_hash")
            if not digest or already_shipped(project_dir, digest):
                continue
            out.append((verb, manifest))
    return out


def resend_pending(project_dir: str) -> Dict[str, int]:
    """Ship every sealed manifest this clone has not yet delivered.

    Cheap and idempotent: with nothing outstanding it lists two directories and
    returns. Safe to call on any CLI contact -- a ceremony, or the SessionStart
    hook alongside the outbox flush -- and it never raises, for the same reason
    the single emission never did.

    Returns ``{"pending": n, "shipped": n, "failed": n}``.
    """
    result = {"pending": 0, "shipped": 0, "failed": 0}
    try:
        pending = _pending(project_dir)
    except Exception:  # noqa: BLE001 - observation must never block delivery
        return result
    result["pending"] = len(pending)
    for verb, manifest in pending:
        try:
            ok = _ship_and_mark(verb, manifest, project_dir)
        except Exception:  # noqa: BLE001
            ok = False
        result["shipped" if ok else "failed"] += 1
    return result


if __name__ == "__main__":
    # Invoked detached by the SessionStart hook. Prints one line so
    # `.outbox/flush.log`-style debugging has something to read, and always
    # exits 0: a reporting sweep must not look like a failed hook.
    #
    # Run as a script, so the sibling modules this file imports lazily
    # (`spq_paths`, `gate_emitter`) are not on sys.path yet. Without this the
    # sweep would find nothing and report success -- the exact silence #334 is
    # about.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    _dir = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    _out = resend_pending(_dir)
    if _out["pending"]:
        print(
            "manifest_emitter: %d pending, %d shipped, %d failed"
            % (_out["pending"], _out["shipped"], _out["failed"])
        )
