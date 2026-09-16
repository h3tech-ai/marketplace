#!/usr/bin/env python3
"""A stand-in `synaptory` that implements `cycles regions` over a JSON file.

WHY A REAL DOUBLE AND NOT A YES-MAN. `open_cycle` requires a granted
reservation and `evaluate_dispatch` rechecks that the grant is still live, so a
double that answered "reserved" to everything and listed nothing made every
dispatch refuse with `region_contradicted` -- correctly, since as far as the
kernel could tell the reservation had vanished. A double has to be consistent
with itself or it tests the wrong refusal.

So this holds state: `reserve` records the claim (refusing an overlap the way
the endpoint does), `list` reports what is held, `release` removes it. The
store is one JSON file named by `SYNAPTORY_FAKE_REGISTRY`, so two processes in
one test share a registry the way two clones share a control plane -- which is
what lets a test prove that the SECOND overlapping Cycle is refused.

Everything else exits 0 silently: the tests that use this also resolve it for
`gate_emitter` and `manifest_emitter`, and telemetry is not what it is doubling.
"""

from __future__ import annotations

import json
import os
import sys


def _store() -> str:
    return os.environ.get("SYNAPTORY_FAKE_REGISTRY") or ""


def _load() -> list:
    path = _store()
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle) or []
    except (OSError, ValueError):
        return []


def _save(rows: list) -> None:
    path = _store()
    if not path:
        return
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(rows, handle)


def _flag(argv: list, name: str) -> str:
    for i, item in enumerate(argv):
        if item == name and i + 1 < len(argv):
            return argv[i + 1]
    return ""


def _flags(argv: list, name: str) -> list:
    found = []
    for i, item in enumerate(argv):
        if item == name and i + 1 < len(argv):
            found.append(argv[i + 1])
    return found


def _project(argv: list) -> str:
    """Which project's reservations this claim competes with.

    `--project` when given, otherwise the working directory -- which is how the
    real CLI resolves it, and the reason this matters: reservations are scoped
    per project, and a double that ignored the scope made two unrelated
    temporary projects in one test collide on the same `repository` value. The
    refusal was real, the collision was the double's.
    """
    return _flag(argv, "--project") or os.getcwd()


def _overlaps(left: list, right: list) -> bool:
    """Prefix containment, which is what the shipped grammar reduces to here."""
    for a in left:
        for b in right:
            if a == b or a.startswith(b) or b.startswith(a):
                return True
    return False


def main(argv: list) -> int:
    if not argv or argv[0] != "cycles":
        return 0
    verb = argv[2] if len(argv) > 2 else ""
    rows = _load()
    project = _project(argv)
    if verb == "reserve":
        cycle_id = _flag(argv, "--cycle-id")
        region = _flags(argv, "--region")
        repository = _flag(argv, "--repository")
        for row in rows:
            if row["project"] != project:
                continue
            if row["cycle_id"] == cycle_id:
                print(json.dumps({"ok": True, "reason": "reserved",
                                  "cycle_id": cycle_id}))
                return 0
            if row["repository"] == repository and _overlaps(region, row["region"]):
                print(json.dumps({
                    "ok": False, "reason": "collision",
                    "detail": "region %s overlaps Cycle %s, which holds %s"
                              % (region, row["cycle_id"], row["region"]),
                }))
                return 1
        rows.append({
            "project": project,
            "cycle_id": cycle_id, "repository": repository, "region": region,
            "path_grammar": _flag(argv, "--path-grammar") or "v1",
            "engineering_lead": _flag(argv, "--engineering-lead"),
            "declaration_hash": _flag(argv, "--declaration-hash"),
            "reserved_at": "2026-01-01T00:00:00Z", "released_at": None,
        })
        _save(rows)
        print(json.dumps({"ok": True, "reason": "reserved", "cycle_id": cycle_id}))
        return 0
    if verb == "release":
        cycle_id = _flag(argv, "--cycle-id")
        _save([
            r for r in rows
            if not (r["project"] == project and r["cycle_id"] == cycle_id)
        ])
        print(json.dumps({"ok": True, "reason": "released", "cycle_id": cycle_id}))
        return 0
    if verb == "list":
        repository = _flag(argv, "--repository")
        shown = [
            r for r in rows
            if r["project"] == project
            and (not repository or r["repository"] == repository)
        ]
        print(json.dumps({"project_id": project,
                          "repository": repository, "reservations": shown}))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
