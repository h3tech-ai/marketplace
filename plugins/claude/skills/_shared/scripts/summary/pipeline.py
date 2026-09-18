"""Pipeline state builders — project, lifecycle, DoD summary, sprint state, context packages.

Board reads here go through `pipeline_board.read_board` (see `load_board`), not
through a direct `json.load` of `pipeline-state.json`. That file is the board
only on the flat v2 layout: under the v3.0 Multi-Spec envelope the per-spec
fields live under `specs.<id>`, and under `build_mode: spq` the file is a mode +
identity pointer. Reading it raw succeeds against the wrong shape and reports
emptiness as a measurement — an in-flight sprint rendered as `sprint 0,
0 stories, DoD pending` (#730), which reads as "no work in progress" rather than
"cannot read this state shape".
"""

import datetime
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Optional

from .helpers import _read, _load_json, _duration_str, _ts_display
from .receipts import extract_findings, SCRUM_STATES, KANBAN_STATES


# ── The canonical board accessor ─────────────────────────────────────────────
#
# `core/scripts/` and the shared lib sit at a different depth per host layout
# (core/lib in the source tree, <pkg>/hooks/lib once composed), so probe via
# `_runtime_paths.find_lib_dir` rather than counting parents — the same import
# preamble `verify_all.py` uses.

def _ensure_lib_path() -> None:
    scripts_dir = Path(__file__).resolve().parent.parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    try:
        from _runtime_paths import find_lib_dir  # type: ignore
    except ImportError:  # pragma: no cover - projection accident
        return
    lib_dir = str(find_lib_dir(scripts_dir))
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)


def load_board(project_dir: Path):
    """The board for this project, whatever layout it is on. None if unusable.

    Deliberately routed through `pipeline_board.read_board`, which knows the
    flat, Multi-Spec and SPQ layouts AND reports a board it could not read as
    `available=False` plus `problems` instead of as an empty one. That
    distinction is the whole point here: answering `{}` for an unreadable file
    is what made `/synaptory status` claim an empty pipeline (#730).

    `None` means the accessor itself could not be imported — a broken
    projection, not an empty board. `build_board_report` turns that into a
    stated problem rather than a zero.
    """
    _ensure_lib_path()
    try:
        import pipeline_board  # type: ignore
    except ImportError:  # pragma: no cover - projection accident
        return None
    return pipeline_board.read_board(str(project_dir))


def board_state(board) -> dict:
    """The flat, v2-shaped substate every builder below expects.

    Multi-Spec: the ACTIVE spec's slot (`read_board` orders substates
    active-first). A sprint number, a sprint goal and a lifecycle belong to one
    spec, so merging slots would invent a project-wide sprint that does not
    exist. Nothing is dropped silently — `build_board_report` names every spec
    with its own counts.

    `build_mode` lives at the envelope's top level in Multi-Spec and is carried
    on the Board, so it is injected here; without it `build_sprint_state` reads
    a missing mode as not-scrum and reports no sprint at all.
    """
    if board is None or not board.substates:
        return {}
    label, sub = board.substates[0]
    view = dict(sub)
    if not view.get("build_mode"):
        view["build_mode"] = board.build_mode
    if label:
        view["spec_id"] = label
    return view


def _substate_rollup(label: str, sub: dict) -> dict:
    stories = [s for s in (sub.get("current_stories") or []) if isinstance(s, dict)]
    return {
        "id": label,
        "lifecycle_state": sub.get("lifecycle_state", ""),
        "current_sprint": sub.get("current_sprint", 0),
        "stories_total": len(stories),
        "stories_done": sum(1 for s in stories if s.get("state") == "done"),
    }


def build_board_report(board, project_dir: Path) -> dict:
    """Whether a board was read at all, and from where.

    A summary that cannot say this cannot be trusted when it says zero.
    """
    if board is None:
        return {
            "available": False,
            "layout": "unknown",
            "build_mode": "",
            "source": "",
            "active_spec": None,
            "specs": [],
            "problems": [
                "pipeline_board is not importable from this package, so no "
                "board was read. The numbers below are not a measurement."
            ],
        }
    specs = [_substate_rollup(label, sub) for label, sub in board.substates if label]
    return {
        "available": bool(board.available),
        "layout": board.layout,
        "build_mode": board.build_mode,
        "source": board.source,
        "active_spec": board.identity.get("active_spec"),
        "specs": specs,
        "problems": list(board.problems),
    }


def _states_for(build_mode: str) -> Optional[List[str]]:
    """The lifecycle sequence for a build mode, or None when none is known.

    SPQ's list is IMPORTED from the state machine that owns it rather than
    re-typed, the rule `verify_all._states_for` already follows. `None` renders
    no stages at all: `build_mode: spq` previously fell through to
    `KANBAN_STATES` and drew six Kanban stages, all pending, for a lifecycle
    that has none of them.
    """
    if build_mode == "scrum":
        return list(SCRUM_STATES)
    if build_mode == "kanban":
        return list(KANBAN_STATES)
    if build_mode == "spq":
        _ensure_lib_path()
        try:
            from spq_state_machine import SPQ_STATES  # type: ignore
        except Exception:  # noqa: BLE001 - unimportable is unknown, not kanban
            return None
        return list(SPQ_STATES)
    return None


def build_project(project_dir: Path, settings: dict, config: dict) -> dict:
    project_name = (
        settings.get("project") or
        config.get("project.name") or
        config.get("project_name") or
        config.get("name") or
        project_dir.name
    )
    stack = (
        settings.get("stack") or
        config.get("project.stack") or
        config.get("stack") or
        ""
    )
    return {
        "name": project_name,
        "engagement": settings.get("engagement", "autonomous"),
        "stack": stack,
    }


def build_pipeline(state: dict, receipts: list, board=None) -> dict:
    lifecycle_state = state.get("lifecycle_state", "UNKNOWN")
    build_mode = state.get("build_mode", "scrum")

    # Build lifecycle history from v2 state
    lifecycle_history = state.get("lifecycle_history", [])

    # Calculate total elapsed
    started_at = state.get("started_at") or (
        lifecycle_history[0].get("entered_at") if lifecycle_history else None
    )
    total_elapsed = _duration_str(started_at) if started_at else "—"

    # Build history map for lookup
    history_map: dict[str, dict] = {}
    for entry in lifecycle_history:
        s = entry.get("state", "")
        if s not in history_map:
            history_map[s] = entry

    # Select the right state sequence based on build mode
    state_sequence = _states_for(build_mode)
    stage_problems = []
    if state_sequence is None:
        stage_problems.append(
            "no lifecycle sequence is known for build_mode %r, so no stages "
            "are drawn. An unknown mode is not Kanban." % build_mode
        )
        state_sequence = []

    # Build lifecycle stages array
    stages = []
    for stage_name in state_sequence:
        entry = history_map.get(stage_name, {})
        entered_at = entry.get("entered_at")
        exited_at = entry.get("exited_at")

        if stage_name == lifecycle_state:
            status = "active"
            duration = _duration_str(entered_at)
        elif exited_at:
            status = "complete"
            duration = _duration_str(entered_at, exited_at)
        elif entered_at:
            status = "active"
            duration = _duration_str(entered_at)
        else:
            status = "pending"
            duration = "—"

        stages.append({
            "name": stage_name,
            "status": status,
            "entered_at": entered_at or "",
            "exited_at": exited_at or "",
            "duration": duration,
        })

    # Group receipts by story for summary
    stories_by_id: dict[str, list] = {}
    for r in receipts:
        sid = r.get("story_id", "unknown")
        stories_by_id.setdefault(sid, []).append(r)

    # An unread board and an empty one must not share a representation. A
    # consumer that renders `lifecycle_state: UNKNOWN` with zero stories has to
    # be able to tell "nothing is in flight" from "nothing was read".
    problems = list(stage_problems)
    available = True
    if board is not None:
        available = bool(board.available)
        problems.extend(board.problems)
    elif not state:
        available = False
        problems.append("no board was read")

    return {
        "lifecycle_state": lifecycle_state,
        "build_mode": build_mode,
        "total_elapsed": total_elapsed,
        "stages": stages,
        "stories_with_receipts": len(stories_by_id),
        "state_available": available,
        "state_problems": problems,
    }


def build_dod_summary(state: dict) -> dict:
    """Build DoD summary from current story states.

    Aggregates per-story DoD evaluation results into an overall summary.
    """
    stories = state.get("current_stories", [])
    total = len(stories)
    evaluated = 0
    passed = 0
    checks_summary: dict[str, dict] = {}

    for story in stories:
        dod = story.get("dod")
        if not dod:
            continue
        evaluated += 1
        if dod.get("passed"):
            passed += 1

        raw_checks = dod.get("checks", {})
        if isinstance(raw_checks, dict):
            checks_iter = []
            for cid, check in raw_checks.items():
                if isinstance(check, dict):
                    checks_iter.append((cid, check))
        elif isinstance(raw_checks, list):
            checks_iter = []
            for check in raw_checks:
                if isinstance(check, dict):
                    checks_iter.append((check.get("id", "unknown"), check))
        else:
            checks_iter = []

        for cid, check in checks_iter:
            if cid not in checks_summary:
                checks_summary[cid] = {"pass": 0, "fail": 0, "skip": 0}

            # v2 state machines write boolean `passed`; older data may use
            # string `result` values such as pass/fail/skip.
            result = check.get("result")
            if result is None:
                if check.get("required") is False and check.get("passed") is None:
                    result = "skip"
                elif check.get("passed") is True:
                    result = "pass"
                elif check.get("passed") is False:
                    result = "fail"
                else:
                    result = "skip"

            if result == "pass":
                checks_summary[cid]["pass"] += 1
            elif result == "skip":
                checks_summary[cid]["skip"] += 1
            else:
                checks_summary[cid]["fail"] += 1

    # Inception gate status
    inception = state.get("inception", {})
    inception_completed = bool(inception.get("completed_at"))

    return {
        "inception_gate": "approved" if inception_completed else "pending",
        "stories_total": total,
        "stories_evaluated": evaluated,
        "stories_passed": passed,
        "pass_rate": round(passed / evaluated, 2) if evaluated else 0.0,
        "checks": checks_summary,
    }


def build_verification(receipts: list) -> dict:
    commands = []
    for r in receipts:
        for cmd in r.get("verification_commands", []):
            if isinstance(cmd, str):
                # v2 simple string format
                commands.append({
                    "story_id": r.get("story_id", ""),
                    "role": r.get("role", ""),
                    "command": cmd,
                    "exit_code": "?",
                    "summary": "",
                })
            elif isinstance(cmd, dict):
                # Legacy dict format
                commands.append({
                    "story_id": r.get("story_id", ""),
                    "role": r.get("role", ""),
                    "command": cmd.get("command", ""),
                    "exit_code": cmd.get("exit_code", "?"),
                    "summary": cmd.get("summary", ""),
                })
    return {"commands": commands}


def build_sprint_state(state: dict, receipts: list) -> Optional[dict]:
    """Extract sprint state from v2 pipeline-state.json.

    Returns None when build_mode is not scrum.
    """
    if state.get("build_mode") != "scrum":
        return None

    current = state.get("current_sprint", 0)
    total = state.get("total_sprints")
    sprint_goal = state.get("sprint_goal", "")
    completed_sprints = state.get("sprints_completed", [])
    current_stories = state.get("current_stories", [])

    # Build per-sprint summary from completed sprint records
    sprint_rows = []
    for sc in completed_sprints:
        sprint_rows.append({
            "number": sc.get("number", 0),
            "stories": f"{sc.get('stories_completed', 0)}/{sc.get('stories_planned', 0)}",
            "velocity": sc.get("velocity", 0),
            "dod_compliance": sc.get("dod_compliance", 0),
            "status": "complete",
        })

    # Add current sprint if active
    if current > 0 and current not in [s.get("number") for s in sprint_rows]:
        stories_by_state: dict[str, int] = {}
        for s in current_stories:
            st = s.get("state", "unknown")
            stories_by_state[st] = stories_by_state.get(st, 0) + 1
        done = stories_by_state.get("done", 0)
        sprint_rows.append({
            "number": current,
            "stories": f"{done}/{len(current_stories)}",
            "velocity": 0,
            "dod_compliance": 0,
            "status": "active",
            "stories_by_state": stories_by_state,
        })

    return {
        "current_sprint": current,
        "total_sprints": total,
        "sprint_goal": sprint_goal,
        "sprints_completed": len(completed_sprints),
        "sprints": sprint_rows,
    }


def build_context_packages(project_dir: Path) -> list:
    pkg_dir = project_dir / ".synaptory" / ".orchestrator" / "context-packages"
    expected = [
        "dependency-map", "interface-contracts", "business-rules-inventory",
        "data-dictionary", "risk-register", "health-assessment",
    ]
    packages = []
    for pkg_name in expected:
        pkg_path = pkg_dir / f"{pkg_name}.md"
        if pkg_path.exists():
            try:
                ts = os.path.getmtime(str(pkg_path))
                dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
                date = dt.strftime("%Y-%m-%d")
            except Exception:
                date = ""
            packages.append({"name": pkg_name, "status": "generated", "date": date})
        else:
            packages.append({"name": pkg_name, "status": "pending", "date": ""})
    return packages


def build_open_items(receipts: list) -> list:
    """Extract open items from receipts (open_items, open_decisions, blockers)."""
    items = []
    seen = set()
    for r in receipts:
        for field in ("open_items", "open_decisions", "blockers"):
            raw_items = r.get(field, [])
            if isinstance(raw_items, list):
                for item in raw_items:
                    if isinstance(item, str) and item not in seen:
                        seen.add(item)
                        items.append({"description": item, "source_story": r.get("story_id", "")})
                    elif isinstance(item, dict):
                        desc = item.get("description") or item.get("title", "")
                        if desc and desc not in seen:
                            seen.add(desc)
                            items.append({
                                "id": item.get("id", ""),
                                "description": desc,
                                "owner": item.get("owner", ""),
                                "blocks": item.get("blocks", ""),
                                "source_story": r.get("story_id", ""),
                            })
    return items


# ── Data loaders ─────────────────────────────────────────────────────────────

def load_settings(project_dir: Path) -> dict:
    text = _read(project_dir / ".synaptory" / ".orchestrator" / "settings.md")
    result = {}
    for line in text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            result[k.strip().lower()] = v.strip()
    return result


_CONFIG_KEY_RE = re.compile(r"^(?P<key>[\w.-]+)\s*:\s*(?P<value>.*)$")


def _config_scalar(raw: str) -> str:
    """The scalar on the right of a `key:`, with quotes and trailing comment off."""
    value = raw.strip()
    if not value or value.startswith("#"):
        # A mapping key annotated with a trailing comment — `project.health:
        # # Populated by ...` — opens a block and carries no scalar. Recording
        # the comment as its value is how prose ends up rendered as data.
        return ""
    if value[0] in ("\"", "'"):
        quote = value[0]
        end = value.find(quote, 1)
        if end != -1:
            return value[1:end]
        return value[1:]
    # An unquoted scalar ends at ` #`; a bare `#` inside a word (a colour, an
    # anchor) is not a comment.
    cut = value.find(" #")
    if cut != -1:
        value = value[:cut]
    return value.strip()


def load_config(project_dir: Path) -> dict:
    """Flatten `.synaptory.yaml` into `{dotted.path: scalar}` — NESTING KEPT.

    Not a YAML parser, and deliberately not backed by one: nothing in `core/`,
    the hooks or `plugin-codex` imports `yaml`, and the runtime ships with no
    third-party dependency at all. What this fixes is narrower than YAML and is
    the whole of #730's third defect — the previous version matched
    `^(\\w+):` against `line.strip()`, which erased indentation, so every
    nesting level collapsed into one namespace and the LAST `name:` in the file
    won. With a `specs:` list present that is always the last spec's display
    name, and the dashboard header renamed the project every time a spec was
    appended.

    Keys are emitted at their real path: `project.name`, `tracker.backend`,
    `specs[0].id`. Top-level scalars keep their bare names, so
    `config["build_mode"]` still reads the project's build mode and not a
    nested key that happens to share the word.

    Limits, stated rather than hidden: scalars only (no block scalars, flow
    mappings, or multi-line strings), and a duplicated path is last-wins.
    """
    path = project_dir / ".synaptory.yaml"
    text = _read(path)
    result: dict = {}
    stack: list = []          # [(indent, dotted_prefix)] — open mappings
    seq_index: dict = {}      # dotted_prefix -> next sequence index

    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" \t"))
        content = raw.strip()

        if content.startswith("-"):
            # A sequence entry opens its own namespace, so `- id: platform`
            # under `specs:` becomes `specs[0].id` and can never collide with a
            # top-level `id:`.
            while stack and stack[-1][0] >= indent:
                stack.pop()
            parent = stack[-1][1] if stack else ""
            n = seq_index.get(parent, 0)
            seq_index[parent] = n + 1
            stack.append((indent, "%s[%d]" % (parent, n)))
            content = content[1:].strip()
            indent += 2
            if not content:
                continue

        while stack and stack[-1][0] >= indent:
            stack.pop()
        m = _CONFIG_KEY_RE.match(content)
        if not m:
            continue
        base = stack[-1][1] if stack else ""
        dotted = ("%s.%s" % (base, m.group("key"))) if base else m.group("key")
        value = _config_scalar(m.group("value"))
        if value:
            result[dotted] = value
        stack.append((indent, dotted))

    return result


def receipt_dirs(project_dir: Path) -> list:
    """Every directory this project may hold receipts in, on any layout.

    Three homes coexist, and which one a receipt landed in depends on when it
    was written, not on anything the reader can see:

        flat        `.orchestrator/receipts`
        Multi-Spec  `.orchestrator/specs/<spec-id>/receipts`
        SPQ         `.orchestrator/spq/cycles/<cycle-id>/receipts`

    Globbing ALL of them, rather than resolving the one the active spec or the
    open Cycle points at, is deliberate for a summary: `pipeline-summary.json`
    is the whole project's report, migration leaves the pre-migration receipts
    in the primary spec's home while new ones land beside them, and a report
    that silently drops 98 of 116 receipts (#730) truncates findings,
    verification commands and agent metrics without saying so. Each receipt
    carries the home it came from, so a consumer that wants a narrower scope can
    take one without another blind read.
    """
    orch = project_dir / ".synaptory" / ".orchestrator"
    candidates = [orch / "receipts"]
    candidates.extend(sorted((orch / "specs").glob("*/receipts")))
    candidates.extend(sorted((orch / "spq" / "cycles").glob("*/receipts")))
    seen = set()
    dirs = []
    for d in candidates:
        key = str(d)
        if key in seen or not d.is_dir():
            continue
        seen.add(key)
        dirs.append(d)
    return dirs


def load_receipts_raw(project_dir: Path) -> list:
    receipts = []
    for receipts_dir in receipt_dirs(project_dir):
        parent = receipts_dir.parent
        spec_id = parent.name if parent.parent.name == "specs" else ""
        cycle_id = parent.name if parent.parent.name == "cycles" else ""
        for f in sorted(receipts_dir.glob("*.json")):
            data = _load_json(f)
            if data:
                data["_filename"] = f.name
                data["_mtime"] = os.path.getmtime(str(f))
                data["_receipt_home"] = str(receipts_dir)
                if spec_id:
                    data["_spec_id"] = spec_id
                if cycle_id:
                    data["_cycle_id"] = cycle_id
                receipts.append(data)
    return receipts
