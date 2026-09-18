#!/usr/bin/env python3
"""One-shot migration: collapse a single-spec synaptory project into multi-spec.

Implements design §8.2 (docs/multi-spec-design.md). Idempotent.

Usage:
    python3 migrate_to_multispec.py [--project-dir DIR] \\
        --primary-spec=<id> \\
        --new-specs=<id1>[,<id2>,...] \\
        [--yes]            # skip interactive confirmation

What it does (in order):
    1. Snapshot `.synaptory/.orchestrator/` to
       `.synaptory/.migrations/<timestamp>-pre-multispec.tar.gz`.
    2. Wrap the existing flat `pipeline-state.json` under
       `state.specs.<primary-spec>` (via spec_state.upgrade_v2_to_v3).
       Initializes each new spec slot at INCEPTION / sprint=0 / empty caches.
    3. Move `.synaptory/.orchestrator/{tracker-id-map,tracker-data,
       backlog-order}.json` and `receipts/*` into
       `.synaptory/.orchestrator/specs/<primary-spec>/`.
       Empty `specs/<new-spec>/` directories are created for the new specs.
    4. Append a stub `specs:` block to `.synaptory.yaml` (commented values
       the customer fills in from their Jira side).
    5. Re-render the CLAUDE.md sentinel as a multi-spec rollup.

Re-running on an already-multi-spec project is a no-op (prints a diagnostic
and exits 0).

An SPQ project is REFUSED, and that is a decision, not an omission (#529)
------------------------------------------------------------------------
Multi-Spec and SPQ are alternative layouts, not layers. #303/#304/#305 moved
SPQ off the v3.0 envelope into its own native store, and `ADR-035` then refused
the old layout BY NAME rather than converting it: the SPQ-native migrator that
used to carry projects in that one legal direction is gone, because pilot state
carried no audit value worth a converter. Wrapping an SPQ project into v3.0
therefore produces a layout nothing reads and nothing will migrate away from,
which is not a migration.

What running it anyway did, measured on a Cycle opened by `open_cycle`:

  1. The identity pointer is wrapped to `specs.<primary>.spq`, so no Cycle
     identity resolves from it any more. `next_action`, advance and the gates
     all lose the Cycle: the lifecycle is inoperable.
  2. When an SPQ-native migrator still existed, the recovery named in that
     refusal completed with rc=0 and `warnings: []` and re-pointed the project
     at a NEW, EMPTY Cycle `0-<hash>` at seq 0, while the real Cycle, its Work
     Units and its sealed manifest stayed on disk, listed in `spq/index.json`,
     and unreachable. There is now no recovery at all, which makes the refusal
     below the only thing standing between an operator and an unrecoverable
     project.

So the board is not deleted; it is silently replaced by an empty one. That is
worse than a refusal in exactly the way this epic keeps finding: a zero that
means "nothing was recovered" wearing the representation of one that means
"nothing to recover".
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import shutil
import sys
import tarfile
from pathlib import Path


def _resolve_lib_dirs() -> tuple[Path, Path]:
    """Locate the shared lib/ and tracker/ directories.

    The lib dir sits at a different depth per layout (core/lib in the source
    tree, <pkg>/hooks/lib once composed), so probe rather than count parents.
    """
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    from _runtime_paths import find_lib_dir  # type: ignore

    return find_lib_dir(here), here / "tracker"


HOOKS_LIB, TRACKER_DIR = _resolve_lib_dirs()
sys.path.insert(0, str(HOOKS_LIB))
sys.path.insert(0, str(TRACKER_DIR.parent))  # scripts/ for the tracker package

# Imports below depend on the sys.path tweaks above.
import pipeline_board  # type: ignore  # noqa: E402
import spec_state  # type: ignore  # noqa: E402
from tracker.config import TrackerConfig  # type: ignore  # noqa: E402


CACHE_FILES = ("tracker-id-map.json", "tracker-data.json", "backlog-order.json")
ORCHESTRATOR_REL = ".synaptory/.orchestrator"

SPQ_REFUSAL = (
    "this project is build_mode 'spq', and an SPQ project is not migratable to "
    "Multi-Spec.\n"
    "\n"
    "Multi-Spec (v3.0) and SPQ are ALTERNATIVE layouts, not layers. #303/#304/"
    "#305 moved SPQ off the v3.0 envelope into its own native store, and\n"
    "`ADR-035` then refused the old layout by name rather than converting it — "
    "so the output of this migration is a layout nothing reads and nothing\n"
    "will migrate away from. Running it would wrap the identity pointer under "
    "specs.<primary>.spq, after which no Cycle identity resolves and the\n"
    "real Cycle, its Work Units and its sealed manifest are orphaned on disk.\n"
    "\n"
    "There is no --force for this, and there is no recovery script: the "
    "SPQ-native migrator was removed with `ADR-035`, because pilot state\n"
    "carried no audit value worth a converter. An SPQ project already wrapped "
    "into v3.0 has to open a fresh Cycle — `open_cycle` writes the board at\n"
    "    .synaptory/.orchestrator/spq/cycles/<cycle-id>/execution-state.json\n"
    "and leaves pipeline-state.json a mode+identity pointer.\n"
    "If you meant to change the project's lifecycle, that is a lifecycle "
    "decision and not a storage migration."
)


def _utc_stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _spq_refusal(project_dir: Path) -> str | None:
    """The refusal text when this project is SPQ, else None.

    Routed through `pipeline_board.read_board` rather than re-reading the
    layout here (#514/#529): the accessor knows every lifecycle and reports
    `build_mode` whether or not the board itself could be read — which matters,
    because an SPQ project already on the retired v3.0 layout is precisely the
    one whose board is unreadable AND the one this script must not touch. That
    unreadability is `pipeline_board.RETIRED_V3_REFUSAL`; it moved into the
    accessor when the state machine stopped refusing the old pointer itself.
    """
    board = pipeline_board.read_board(str(project_dir))
    declared = _declared_build_mode(project_dir)

    # TWO AUTHORITIES, AND EITHER ONE REFUSES (#396). The board's `build_mode`
    # DEFAULTS to scrum when there is no pointer to read it from, so a project
    # whose `.synaptory.yaml` says `build_mode: spq` and whose native Cycle
    # store still exists was classified as non-SPQ the moment its pointer went
    # missing. That is the destructive path this refusal exists to close,
    # reached through a damaged pointer rather than a healthy one: the run
    # returned 0, wrote a Scrum Multi-Spec state, and left the Cycle orphaned
    # beside it.
    #
    # Disagreement is a refusal too, not a vote. Two authorities that do not
    # agree about the lifecycle mean this project's shape is unknown, and a
    # migration that rewrites the board is the last thing to do while it is.
    if board.build_mode != "spq" and declared != "spq":
        return None
    detail = SPQ_REFUSAL
    if declared == "spq" and board.build_mode != "spq":
        detail += (
            "\n\n`.synaptory.yaml` declares `build_mode: spq` while the board "
            "reports %r. The board's build_mode defaults to scrum when there "
            "is no pointer to read, so a missing or damaged pointer is not "
            "evidence that this project is not SPQ." % board.build_mode
        )
    elif declared not in ("", "spq") and board.build_mode == "spq":
        detail += (
            "\n\nThe board reports SPQ while `.synaptory.yaml` declares "
            "`build_mode: %s`. The two authorities disagree, so the project's "
            "lifecycle is unknown and nothing here may rewrite its board."
            % declared
        )
    if not board.available:
        detail += "\n\nThe SPQ board could not be read either: %s" % (
            "; ".join(board.problems) or "unknown reason"
        )
    return detail


def _declared_build_mode(project_dir: Path) -> str:
    """`.synaptory.yaml`'s `build_mode`, or "" when it cannot be read.

    The config authority, independent of the board. "" rather than a default,
    because this caller must be able to tell "the project says scrum" from
    "the project could not be asked", and a default would erase that.
    """
    try:
        import runtime_selector
    except ImportError:  # pragma: no cover - partial install
        return ""
    try:
        if not (Path(project_dir) / ".synaptory.yaml").is_file():
            return ""
        return str(runtime_selector.read_build_mode(str(project_dir)) or "")
    except Exception:  # noqa: BLE001 - an unreadable config declares nothing
        return ""


def _is_already_multispec(state_path: Path) -> bool:
    if not state_path.exists():
        return False
    full = spec_state.read_full_state(str(state_path.parent.parent.parent))
    return spec_state.is_multispec(full)


def _confirm(prompt: str) -> bool:
    try:
        ans = input(prompt + " [y/N] ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def _print_plan(project_dir: Path, primary: str, new_specs: list[str]) -> None:
    state_path = project_dir / ORCHESTRATOR_REL / "pipeline-state.json"
    print("[migrate-to-multispec] plan:")
    print(f"  project_dir   = {project_dir}")
    print(f"  primary-spec  = {primary}")
    print(f"  new-specs     = {', '.join(new_specs) or '<none>'}")
    print()
    print("  - Snapshot .synaptory/.orchestrator/ → "
          ".synaptory/.migrations/<timestamp>-pre-multispec.tar.gz")
    if state_path.exists():
        print(f"  - Upgrade pipeline-state.json v2.0 → v3.0 (wrap under specs.{primary})")
    else:
        print("  - Initialize a fresh v3.0 pipeline-state.json")
    for s in new_specs:
        print(f"  - Seed empty INCEPTION slot for specs.{s}")
    print(f"  - Move .synaptory/.orchestrator/{{{','.join(CACHE_FILES)}}} → "
          f".synaptory/.orchestrator/specs/{primary}/")
    print(f"  - Move .synaptory/.orchestrator/receipts/* → "
          f".synaptory/.orchestrator/specs/{primary}/receipts/")
    print("  - Append stub `specs:` block to .synaptory.yaml "
          "(operator fills in Jira board_id / filter values).")
    print("  - Regenerate CLAUDE.md sentinel as a multi-spec rollup.")


def _snapshot(project_dir: Path) -> Path | None:
    orch = project_dir / ORCHESTRATOR_REL
    if not orch.exists():
        return None
    migrations_dir = project_dir / ".synaptory" / ".migrations"
    migrations_dir.mkdir(parents=True, exist_ok=True)
    tar_path = migrations_dir / f"{_utc_stamp()}-pre-multispec.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(orch, arcname=".orchestrator")
    return tar_path


def _move_caches_and_receipts(project_dir: Path, primary: str) -> None:
    orch = project_dir / ORCHESTRATOR_REL
    spec_dir = orch / "specs" / primary
    spec_dir.mkdir(parents=True, exist_ok=True)

    for name in CACHE_FILES:
        src = orch / name
        if src.exists():
            shutil.move(str(src), str(spec_dir / name))

    src_receipts = orch / "receipts"
    if src_receipts.is_dir():
        dst_receipts = spec_dir / "receipts"
        dst_receipts.mkdir(parents=True, exist_ok=True)
        for entry in src_receipts.iterdir():
            shutil.move(str(entry), str(dst_receipts / entry.name))
        # Leave the empty src directory intact — some hooks check for it.


def _seed_new_specs(project_dir: Path, new_specs: list[str]) -> None:
    """Create empty specs/<id>/ directories with empty caches so the
    JiraAdapter doesn't have to bootstrap on first use."""
    orch = project_dir / ORCHESTRATOR_REL
    for sid in new_specs:
        d = orch / "specs" / sid
        d.mkdir(parents=True, exist_ok=True)
        (d / "receipts").mkdir(exist_ok=True)


def _append_specs_stub(yaml_path: Path, primary: str, new_specs: list[str]) -> None:
    """Append a commented stub `specs:` block (operator fills it in).

    The stub shape depends on the active `tracker.backend` (local / jira /
    github / teamwork / linear). For an unknown backend, falls back to the
    Jira stub.
    """
    text = yaml_path.read_text(encoding="utf-8") if yaml_path.exists() else ""
    if "\nspecs:" in text or text.startswith("specs:"):
        return  # already present

    backend = _detect_tracker_backend(text)
    spec_template = _SPEC_STUB_TEMPLATES.get(backend, _SPEC_STUB_TEMPLATES["jira"])
    filter_types = {
        "local": "n/a (filesystem-partitioned)",
        "jira": "label | component | epic | jql",
        "github": "label | milestone",
        "teamwork": "tag | tasklist",
        "linear": "label | project",
    }.get(backend, "label | component | epic | jql")

    lines: list[str] = []
    if text and not text.endswith("\n"):
        lines.append("")
    lines.extend(
        [
            "",
            "# ── Multi-spec block (see docs/multi-spec-design.md) ─────────",
            f"# Active backend: {backend!r}. Edit each spec's connection "
            "fields and `filter.value` from your tracker setup.",
            f"# `filter.type` is one of: {filter_types}.",
            "specs:",
        ]
    )
    for sid in [primary, *new_specs]:
        lines.extend(spec_template(sid))
    yaml_path.write_text(text + "\n".join(lines) + "\n", encoding="utf-8")


def _detect_tracker_backend(yaml_text: str) -> str:
    """Best-effort: scan for `tracker.backend: <name>`. Returns 'jira' on miss."""
    import re as _re
    m = _re.search(r"^\s+backend\s*:\s*[\"']?(\w+)", yaml_text, _re.MULTILINE)
    if m and m.group(1) in ("local", "jira", "github", "teamwork", "linear"):
        return m.group(1)
    return "jira"


def _jira_stub(sid: str) -> list[str]:
    return [
        f"  - id: {sid}",
        f'    name: "{sid}"',
        "    jira:",
        '      url: ""',
        '      project_key: ""',
        "      board_id: 0",
        "      filter:",
        "        type: label",
        f"        value: {sid}",
    ]


def _github_stub(sid: str) -> list[str]:
    return [
        f"  - id: {sid}",
        f'    name: "{sid}"',
        "    github:",
        '      repo: ""',
        "      filter:",
        "        type: label",
        f"        value: {sid}",
    ]


def _teamwork_stub(sid: str) -> list[str]:
    return [
        f"  - id: {sid}",
        f'    name: "{sid}"',
        "    teamwork:",
        '      site_name: ""',
        "      project_id: 0",
        "      # If specs target different Teamwork projects, set per-spec",
        "      # workflow_stages here (stage IDs are per-project). Empty →",
        "      # falls back to top-level tracker.teamwork.workflow_stages.",
        "      # workflow_stages:",
        "      #   TO_DO: 0",
        "      #   IN_PROGRESS: 0",
        "      #   DONE: 0",
        "      filter:",
        "        type: tag",
        f"        value: {sid}",
    ]


def _linear_stub(sid: str) -> list[str]:
    return [
        f"  - id: {sid}",
        f'    name: "{sid}"',
        "    linear:",
        '      team_key: ""',
        "      # manage_cycles: false  # true lets Synaptory call cycleCreate",
        "      filter:",
        "        type: label",
        f"        value: {sid}",
    ]


def _local_stub(sid: str) -> list[str]:
    return [
        f"  - id: {sid}",
        f'    name: "{sid}"',
        "    local:",
        f"      requirements_dir: docs/{sid}/requirements",
    ]


_SPEC_STUB_TEMPLATES = {
    "local": _local_stub,
    "jira": _jira_stub,
    "github": _github_stub,
    "teamwork": _teamwork_stub,
    "linear": _linear_stub,
}


def _regenerate_sentinel(project_dir: Path) -> None:
    """Best-effort: rebuild the CLAUDE.md sentinel as a multi-spec rollup."""
    try:
        sys.path.insert(0, str(HOOKS_LIB))
        from update_claude_md import write_sentinel_rollup  # type: ignore
        write_sentinel_rollup(str(project_dir))
    except Exception as e:  # pragma: no cover — best-effort
        print(f"[migrate-to-multispec] WARN: sentinel rebuild failed: {e}",
              file=sys.stderr)


def migrate(
    project_dir: Path,
    primary_spec: str,
    new_specs: list[str],
    yes: bool = False,
) -> int:
    state_path = project_dir / ORCHESTRATOR_REL / "pipeline-state.json"

    # SPQ first, BEFORE the already-multi-spec check. An SPQ project already
    # wrapped into a v3.0 envelope satisfies `spec_state.is_multispec`, so that
    # check would return 0 with "nothing to do" — a success message for the
    # exact corrupted state this refusal exists to name.
    refusal = _spq_refusal(project_dir)
    if refusal is not None:
        print("[migrate-to-multispec] REFUSED: %s" % refusal, file=sys.stderr)
        return 2

    if _is_already_multispec(state_path):
        print("[migrate-to-multispec] state is already multi-spec — nothing to do.")
        return 0

    if not (project_dir / ".synaptory.yaml").exists():
        print(f"[migrate-to-multispec] ERROR: no .synaptory.yaml at {project_dir}",
              file=sys.stderr)
        return 2

    _print_plan(project_dir, primary_spec, new_specs)
    if not yes and not _confirm("\n  Proceed?"):
        print("[migrate-to-multispec] aborted by user.")
        return 1

    snap = _snapshot(project_dir)
    if snap:
        print(f"[migrate-to-multispec] backup: {snap}")

    # Step 2 — upgrade state schema.
    if state_path.exists():
        spec_state.upgrade_v2_to_v3(str(project_dir), primary_spec_id=primary_spec)
    else:
        spec_state.write_full_state(str(project_dir), {
            "state_schema": 3,
            "build_mode": "scrum",
            "active_spec": primary_spec,
            "specs": {},
        })

    # Step 2 cont. — seed specs in state. When no prior state existed,
    # write_full_state above created only the v3 envelope; seed the primary
    # slot as well as every new spec.
    full = spec_state.read_full_state(str(project_dir))
    for sid in [primary_spec, *new_specs]:
        if sid in (full.get("specs") or {}):
            continue
        full.setdefault("specs", {})[sid] = {
            "lifecycle_state": "INCEPTION",
            "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "lifecycle_history": [],
            "inception": None,
            "current_sprint": 0,
            "sprint_goal": None,
            "sprints_completed": [],
            "current_stories": [],
            "process_log": [],
            "agent_backends": {"default": "claude", "roles": {}},
        }
    spec_state.write_full_state(str(project_dir), full)

    # Step 3 — move caches/receipts under specs/<primary>/.
    _move_caches_and_receipts(project_dir, primary_spec)
    _seed_new_specs(project_dir, new_specs)

    # Step 4 — append stub specs: block to .synaptory.yaml (best-effort; the
    # customer must fill in real values from their Jira setup).
    try:
        _append_specs_stub(project_dir / ".synaptory.yaml", primary_spec, new_specs)
    except Exception as e:
        print(f"[migrate-to-multispec] WARN: could not patch .synaptory.yaml: {e}",
              file=sys.stderr)

    # Step 5 — regenerate CLAUDE.md sentinel.
    _regenerate_sentinel(project_dir)

    print()
    print("[migrate-to-multispec] done. Next steps:")
    print("  1. Edit .synaptory.yaml — fill in board_id / filter.value for each spec.")
    print("  2. Edit CLAUDE.md — add `### Spec: <name>` sections for each spec.")
    print("  3. Verify the new layout: "
          ".synaptory/.orchestrator/specs/{platform,...}/{caches, receipts/}.")
    print("  4. Use `/switch-spec <id>` to pick the active spec for an agent run.")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project-dir", default=os.getcwd())
    ap.add_argument("--primary-spec", required=True,
                    help="spec id that absorbs the existing single-spec state")
    ap.add_argument("--new-specs", default="",
                    help="comma-separated additional spec ids to seed at INCEPTION")
    ap.add_argument("--yes", action="store_true",
                    help="skip the interactive confirmation prompt")
    args = ap.parse_args(argv)

    new_specs = [s.strip() for s in args.new_specs.split(",") if s.strip()]
    return migrate(
        project_dir=Path(args.project_dir).resolve(),
        primary_spec=args.primary_spec,
        new_specs=new_specs,
        yes=args.yes,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
