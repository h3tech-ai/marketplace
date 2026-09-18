"""Versioned report data assembly — requirements, technical, sprint report data."""

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

from .helpers import (
    _read, _load_json, _now_iso, _ts_display,
    _parse_markdown_table, _extract_heading, _extract_section, _count_files,
)
from .receipts import normalize_receipt, extract_findings
from .pipeline import receipt_dirs, load_receipts_raw

# Ensure tracker package is importable
_scripts_dir = str(Path(__file__).resolve().parent.parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from tracker import get_adapter


def build_requirements_summary(project_dir: Path, version: int = 1) -> dict:
    """Assemble requirements report data from PM artifacts."""
    adapter = get_adapter(project_dir)
    req_dir = adapter.req_dir if hasattr(adapter, "req_dir") else project_dir / ".requirements"
    brd_dir = req_dir / "BRD"

    # BRD metadata (documentation artifact — stays as direct file read)
    brd_text = _read(brd_dir / "brd.md")
    brd_title = _extract_heading(brd_text, 1) or "Untitled Project"
    nfr_section = _extract_section(brd_text, "Non-Functional Requirements") or \
                  _extract_section(brd_text, "NFR Grid") or ""

    # Epics — from adapter
    epics_raw = adapter.list_epics()
    epics = [
        {
            "id": e.id,
            "title": e.title,
            "file": e.file_path or "",
            "feature_count": e.feature_count,
        }
        for e in epics_raw
    ]

    # Stories — from adapter (includes backlog enrichment)
    stories_raw = adapter.list_stories()
    stories = []
    stories_by_feature: dict[str, list] = {}
    for s in stories_raw:
        story_dict = {
            "id": s.id,
            "feature": s.feature,
            "title": s.title,
            "file": s.file_path or "",
            "ac_count": s.ac_count,
            "priority": s.priority,
            "sprint": s.sprint,
            "status": s.status,
            "size": s.size,
            "review_hours": s.review_hours,
        }
        stories.append(story_dict)
        stories_by_feature.setdefault(s.feature, []).append(story_dict)

    # Backlog rows (raw dicts for backward compat with changelog diffing)
    backlog_items = adapter.get_backlog()
    backlog_rows = [
        {
            "ID": b.id, "Title": b.title, "Feature": b.feature,
            "Priority": b.priority, "Status": b.status, "Size": b.size,
            "Sprint": b.sprint, "Review Hours": b.review_hours,
            "Blocked By": b.blocked_by,
        }
        for b in backlog_items
    ]

    # Priority distribution
    priority_dist = {"Must": 0, "Should": 0, "Could": 0}
    for s in stories:
        p = s.get("priority", "")
        if p in priority_dist:
            priority_dist[p] += 1

    # Roadmap (documentation artifact — stays as direct file read)
    roadmap_text = _read(req_dir / "ROADMAP.md")
    roadmap_params = _extract_section(roadmap_text, "Planning Parameters")
    roadmap_timeline = _extract_section(roadmap_text, "Timeline") or \
                       _extract_section(roadmap_text, "Phase")

    # Sprints — from adapter
    sprints_raw = adapter.list_sprints()
    sprints = [
        {
            "number": str(s.number),
            "file": s.file_path or "",
            "goal": s.goal,
            "story_ids": s.story_ids,
            "story_count": s.story_count,
        }
        for s in sprints_raw
    ]

    # Triage / feedback files (pipeline internal — stays as direct file read)
    feedback_dir = req_dir / "FEEDBACK"
    triage_files = []
    if feedback_dir.exists():
        for tf in sorted(feedback_dir.glob("triage-sprint-*.md")):
            triage_files.append({
                "file": str(tf.relative_to(project_dir)),
                "sprint": tf.stem.replace("triage-sprint-", ""),
            })

    return {
        "version": version,
        "generated_at": _now_iso(),
        "generated_at_display": _ts_display(_now_iso()),
        "project": {"title": brd_title},
        "epics": epics,
        "epic_count": len(epics),
        "stories": stories,
        "story_count": len(stories),
        "stories_by_feature": {k: len(v) for k, v in stories_by_feature.items()},
        "priority_distribution": priority_dist,
        "backlog_rows": backlog_rows,
        "roadmap": {
            "parameters": roadmap_params,
            "timeline": roadmap_timeline,
            "full_text": roadmap_text[:5000],  # Truncate for safety
        },
        "sprints": sprints,
        "nfr_section": nfr_section[:2000],
        "triage_files": triage_files,
    }


# ── Technical Summary ─────────────────────────────────────────────────────


def _find_receipt_by_filename(project_dir: Path, filename: str) -> dict:
    """The first receipt named `filename`, searched across every receipt home.

    A receipt keeps its filename when `migrate_to_multispec.py` moves it to
    `.orchestrator/specs/<id>/receipts/`, or when `build_mode: spq` writes it
    under `.orchestrator/spq/cycles/<id>/receipts/` — only the directory
    changes. A literal path built against the flat pre-migration home goes
    blind the same way an unscoped `receipts_dir.glob()` does (#733, sibling
    of #730's `load_receipts_raw`), so this resolves through `receipt_dirs`
    (the same accessor `load_receipts_raw` uses) instead of a hard-coded path.
    """
    for d in receipt_dirs(project_dir):
        candidate = d / filename
        if candidate.exists():
            data = _load_json(candidate)
            if data:
                return data
    return {}


def build_technical_summary(project_dir: Path, version: int = 1) -> dict:
    """Assemble technical report data from architecture artifacts."""
    # Architecture overview — read SAD from docs/architecture/ (single source of truth)
    sad_path = project_dir / "docs" / "architecture" / "SAD.md"
    design_text = _read(sad_path) if sad_path.exists() else ""
    arch_pattern = _extract_heading(design_text, 1) or "Not documented"
    tech_stack_section = _extract_section(design_text, "Technology Stack") or \
                         _extract_section(design_text, "Tech Stack") or ""

    # ADRs
    adrs = []
    adr_dir = project_dir / "docs" / "architecture" / "adr"
    if not adr_dir.exists():
        adr_dir = project_dir / "docs" / "architecture" / "adrs"
    if adr_dir.exists():
        for f in sorted(adr_dir.glob("*.md")):
            text = _read(f)
            title = _extract_heading(text, 1) or f.stem
            status_line = ""
            for line in text.splitlines():
                if line.strip().lower().startswith("status:"):
                    status_line = line.split(":", 1)[1].strip()
                    break
            decision = _extract_section(text, "Decision")
            adrs.append({
                "id": f.stem,
                "title": title,
                "status": status_line or "Accepted",
                "decision_summary": (decision[:200] + "...") if len(decision) > 200 else decision,
                "file": str(f.relative_to(project_dir)),
            })

    # API specs
    api_specs = []
    for api_dir_name in ["api/openapi", "api"]:
        api_dir = project_dir / api_dir_name
        if api_dir.exists():
            for f in api_dir.glob("*.yaml"):
                text = _read(f)
                # Count paths (rough endpoint count)
                endpoint_count = text.count("  /")
                api_specs.append({
                    "name": f.stem,
                    "file": str(f.relative_to(project_dir)),
                    "endpoint_count": endpoint_count,
                })
            for f in api_dir.glob("*.yml"):
                text = _read(f)
                endpoint_count = text.count("  /")
                api_specs.append({
                    "name": f.stem,
                    "file": str(f.relative_to(project_dir)),
                    "endpoint_count": endpoint_count,
                })
            break  # Use first found

    # gRPC protos
    grpc_dir = project_dir / "api" / "grpc"
    if grpc_dir.exists():
        for f in grpc_dir.glob("*.proto"):
            text = _read(f)
            rpc_count = text.count("rpc ")
            api_specs.append({
                "name": f.stem + " (gRPC)",
                "file": str(f.relative_to(project_dir)),
                "endpoint_count": rpc_count,
            })

    # Data models
    schemas_dir = project_dir / "schemas"
    erd_text = ""
    migration_count = 0
    if schemas_dir.exists():
        erd_text = _read(schemas_dir / "erd.md")
        migration_count = _count_files(schemas_dir / "migrations", "*.sql")

    # Services inventory
    services = []
    services_dir = project_dir / "services"
    if services_dir.exists():
        for d in sorted(services_dir.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                services.append({
                    "name": d.name,
                    "path": str(d.relative_to(project_dir)),
                })

    # Infrastructure
    dockerfile_count = _count_files(project_dir, "**/Dockerfile")
    ci_count = _count_files(project_dir / ".github" / "workflows", "*.yml") + \
               _count_files(project_dir / ".github" / "workflows", "*.yaml")

    # SA receipt metrics
    sa_receipt = _find_receipt_by_filename(project_dir, "T2-solution-architect.json")
    sa_metrics = sa_receipt.get("metrics", {})

    return {
        "version": version,
        "generated_at": _now_iso(),
        "generated_at_display": _ts_display(_now_iso()),
        "architecture": {
            "pattern": arch_pattern,
            "tech_stack": tech_stack_section[:1000],
            "design_summary": design_text[:3000],
        },
        "adrs": adrs,
        "adr_count": len(adrs),
        "api_specs": api_specs,
        "total_endpoints": sum(s["endpoint_count"] for s in api_specs),
        "data_models": {
            "erd_summary": erd_text[:2000],
            "migration_count": migration_count,
        },
        "services": services,
        "service_count": len(services),
        "infrastructure": {
            "dockerfile_count": dockerfile_count,
            "ci_pipeline_count": ci_count,
        },
        "sa_metrics": sa_metrics,
    }


# ── Sprint Report Data ───────────────────────────────────────────────────


def build_sprint_report_data(project_dir: Path, sprint_num: int,
                              sprint_type: str = "dev") -> dict:
    """Assemble sprint report data (quality + progress) for a single sprint."""
    adapter = get_adapter(project_dir)
    req_dir = adapter.req_dir if hasattr(adapter, "req_dir") else project_dir / ".requirements"
    synaptory_dir = project_dir / ".synaptory"

    # Sprint goal — handle special sprint types that have non-numeric files
    sprint_goal = f"Sprint {sprint_num}"
    sprint_text = ""
    if sprint_type == "verification":
        sprint_file = req_dir / "SPRINTS" / "SPRINT_VERIFICATION.md"
        if not sprint_file.exists():
            sprint_file = req_dir / "SPRINTS" / f"SPRINT_{sprint_num}.md"
        sprint_text = _read(sprint_file)
        sprint_goal = _extract_heading(sprint_text, 1) or _extract_heading(sprint_text, 2) or sprint_goal
    elif sprint_type == "uat":
        sprint_file = req_dir / "SPRINTS" / "SPRINT_UAT.md"
        if not sprint_file.exists():
            sprint_file = req_dir / "SPRINTS" / f"SPRINT_{sprint_num}.md"
        sprint_text = _read(sprint_file)
        sprint_goal = _extract_heading(sprint_text, 1) or _extract_heading(sprint_text, 2) or sprint_goal
    else:
        sprint_info = adapter.get_sprint(sprint_num)
        if sprint_info:
            sprint_goal = sprint_info.goal
        # Still read sprint file for risks/sections below
        sprint_file = req_dir / "SPRINTS" / f"SPRINT_{sprint_num}.md"
        sprint_text = _read(sprint_file)

    # Stories and statuses — from adapter
    sprint_stories = adapter.get_sprint_backlog(sprint_num)
    sprint_story_ids = [s.id for s in sprint_stories]

    # Also get full backlog for health metrics below
    backlog_items = adapter.get_backlog()
    backlog_rows = [
        {
            "ID": b.id, "Status": b.status, "Priority": b.priority,
            "Title": b.title, "Feature": b.feature, "Size": b.size,
            "MoSCoW": b.priority,
        }
        for b in backlog_items
    ]
    backlog_by_id = {b.id: b for b in backlog_items}

    stories_status = []
    done_count = 0
    blocked_count = 0
    in_progress_count = 0
    for s in sprint_stories:
        stories_status.append({
            "id": s.id,
            "title": s.title,
            "feature": s.feature,
            "size": s.size,
            "priority": s.priority,
            "status": s.status,
        })
        if s.status == "DONE":
            done_count += 1
        elif s.status == "BLOCKED":
            blocked_count += 1
        elif s.status == "IN_PROGRESS":
            in_progress_count += 1

    planned_count = len(sprint_story_ids)
    completion_pct = round((done_count / planned_count * 100) if planned_count > 0 else 0)

    # Story-map (files changed per story)
    story_map_text = _read(synaptory_dir / "software-engineer" / "story-map.md")
    story_files = {}
    for line in story_map_text.splitlines():
        if "→" in line or "->" in line:
            sep = "→" if "→" in line else "->"
            parts = line.split(sep, 1)
            if len(parts) == 2:
                sid = parts[0].strip()
                files = parts[1].strip()
                story_files[sid] = files

    for s in stories_status:
        s["files_changed"] = story_files.get(s["id"], "")

    # QE test report
    qe_report_path = synaptory_dir / "quality-engineer" / f"sprint-{sprint_num}-test-report.md"
    if sprint_type == "hardening":
        alt_path = synaptory_dir / "quality-engineer" / "hardening-report.md"
        if alt_path.exists():
            qe_report_path = alt_path
    qe_report_text = _read(qe_report_path)

    # Receipts — v2 story-scoped naming: {story_id}-{role_abbrev}.json
    # Load all receipts across every home (flat / per-spec / per-Cycle) and
    # filter by role abbreviation. A flat `.orchestrator/receipts` glob only
    # saw the pre-migration home and silently under-counted a client-facing,
    # sometimes-immutable sprint report (#733, sibling of #730's
    # `load_receipts_raw`); `load_receipts_raw` is the one shared accessor.
    all_receipts_raw = load_receipts_raw(project_dir)

    # Find role-specific receipts by role field or filename suffix
    def _find_receipt(role_abbrev: str) -> dict:
        for r in all_receipts_raw:
            fname = r.get("_filename", "")
            role = r.get("role", "")
            if fname.endswith(f"-{role_abbrev}.json") or role_abbrev in role:
                return r
        return {}

    qe_receipt = _find_receipt("qe")
    cr_receipt = _find_receipt("cr")
    ce_receipt = _find_receipt("ce")

    # Test metrics from QE receipt
    qe_metrics = qe_receipt.get("metrics", {})
    test_total = qe_metrics.get("tests_written", qe_metrics.get("total_tests", 0))
    test_passed = qe_metrics.get("tests_passing", qe_metrics.get("passing", 0))
    test_failed = qe_metrics.get("tests_failing", qe_metrics.get("failing", 0))

    # Findings from review agent receipts
    review_receipts = []
    for r_raw in [qe_receipt, cr_receipt, ce_receipt]:
        if r_raw:
            review_receipts.append(normalize_receipt(r_raw))
    findings = extract_findings(review_receipts)

    # Remediation stats from SE receipts (if any contain fix data)
    findings_fixed = 0
    findings_remaining = 0
    for r_raw in all_receipts_raw:
        m = r_raw.get("metrics", {})
        findings_fixed += m.get("findings_fixed", 0)
        if m.get("findings_remaining"):
            findings_remaining = m["findings_remaining"]

    # Velocity: from adapter + current sprint
    burndown_data = adapter.get_velocity_data()
    # Add/update current sprint in burn-down
    current_in_data = False
    for bd in burndown_data:
        if bd["sprint"] == sprint_num:
            bd["planned"] = planned_count
            bd["completed"] = done_count
            current_in_data = True
            break
    if not current_in_data:
        burndown_data.append({
            "sprint": sprint_num,
            "planned": planned_count,
            "completed": done_count,
        })

    # Calculate velocity
    total_completed = sum(b["completed"] for b in burndown_data)
    avg_velocity = round(total_completed / len(burndown_data), 1) if burndown_data else 0

    # Backlog health (overall remaining)
    total_stories = len(backlog_rows)
    total_done = sum(1 for r in backlog_rows
                     if r.get("Status", r.get("status", "")) == "DONE")
    total_todo = sum(1 for r in backlog_rows
                     if r.get("Status", r.get("status", "")) == "TODO")
    total_blocked = sum(1 for r in backlog_rows
                        if r.get("Status", r.get("status", "")) == "BLOCKED")
    must_remaining = sum(1 for r in backlog_rows
                         if r.get("Priority", r.get("MoSCoW", "")) == "Must"
                         and r.get("Status", r.get("status", "")) != "DONE")

    # Next sprint plan
    next_sprint = {}
    if sprint_type == "dev":
        next_info = adapter.get_sprint(sprint_num + 1)
        if next_info:
            next_stories = []
            for sid in next_info.story_ids:
                bl = backlog_by_id.get(sid)
                next_stories.append({
                    "id": sid,
                    "title": bl.title if bl else sid,
                    "size": bl.size if bl else "",
                    "priority": bl.priority if bl else "",
                    "blocked_by": bl.blocked_by if bl else "",
                })
            next_sprint = {
                "number": sprint_num + 1,
                "goal": next_info.goal,
                "stories": next_stories,
                "story_count": len(next_stories),
            }
        else:
            # Last dev sprint — next is hardening
            next_sprint = {
                "number": "hardening",
                "goal": "Hardening Sprint — Regression, NFR, Bug Fixes",
                "stories": [],
                "story_count": 0,
                "type": "hardening",
            }
    elif sprint_type == "hardening":
        next_sprint = {
            "number": "uat",
            "goal": "UAT Sprint — Client Acceptance Testing",
            "stories": [],
            "story_count": 0,
            "type": "uat",
        }
    elif sprint_type == "uat":
        next_sprint = {
            "number": "complete",
            "goal": "Production Deployment",
            "stories": [],
            "story_count": 0,
            "type": "complete",
        }

    # Sprint summary (raw markdown from VERIFY)
    sprint_summary_path = req_dir / "REPORTS" / f"sprint-{sprint_num}-summary.md"
    sprint_summary_text = _read(sprint_summary_path)

    # Risks from sprint file
    risks_section = _extract_section(sprint_text, "Risks") or \
                    _extract_section(sprint_text, "Risks/Blockers") or ""

    # Human feedback
    triage_path = req_dir / "FEEDBACK" / f"triage-sprint-{sprint_num}.md"
    triage_text = _read(triage_path)

    return {
        "sprint_num": sprint_num,
        "sprint_type": sprint_type,
        "generated_at": _now_iso(),
        "generated_at_display": _ts_display(_now_iso()),
        "sprint_goal": sprint_goal,
        "stories": {
            "planned": planned_count,
            "completed": done_count,
            "in_progress": in_progress_count,
            "blocked": blocked_count,
            "completion_pct": completion_pct,
            "status_by_story": stories_status,
        },
        "tests": {
            "total": test_total,
            "passed": test_passed,
            "failed": test_failed,
            "qe_report_text": qe_report_text[:3000],
        },
        "findings": findings,
        "remediation": {
            "fixed": findings_fixed,
            "remaining": findings_remaining,
        },
        "velocity": {
            "this_sprint": done_count,
            "cumulative": total_completed,
            "average": avg_velocity,
        },
        "burndown_data": burndown_data,
        "backlog_health": {
            "total": total_stories,
            "done": total_done,
            "todo": total_todo,
            "blocked": total_blocked,
            "must_remaining": must_remaining,
        },
        "next_sprint": next_sprint,
        "risks": risks_section,
        "human_feedback": triage_text[:2000] if triage_text else "",
        "sprint_summary_raw": sprint_summary_text[:3000],
    }


# ── Changelog Builder ─────────────────────────────────────────────────────


def build_changelog(current_data: dict, previous_data: dict,
                    report_type: str) -> dict:
    """Build changelog by diffing current vs previous report data."""
    items = []

    if report_type == "requirements":
        # Diff stories
        prev_ids = {s["id"] for s in previous_data.get("stories", [])}
        curr_ids = {s["id"] for s in current_data.get("stories", [])}
        added_ids = curr_ids - prev_ids
        removed_ids = prev_ids - curr_ids

        for sid in sorted(added_ids):
            story = next((s for s in current_data["stories"] if s["id"] == sid), {})
            items.append({
                "type": "added", "category": "Story",
                "description": f"{sid}: {story.get('title', '')}",
                "detail": f"Priority: {story.get('priority', '?')}, Sprint: {story.get('sprint', '?')}",
            })
        for sid in sorted(removed_ids):
            story = next((s for s in previous_data["stories"] if s["id"] == sid), {})
            items.append({
                "type": "removed", "category": "Story",
                "description": f"{sid}: {story.get('title', '')}",
                "detail": "Removed or deferred",
            })

        # Diff story statuses/priorities
        for story in current_data.get("stories", []):
            prev = next((s for s in previous_data.get("stories", [])
                         if s["id"] == story["id"]), None)
            if prev and story["id"] not in added_ids:
                changes = []
                if prev.get("priority") != story.get("priority"):
                    changes.append(f"Priority: {prev.get('priority')} -> {story.get('priority')}")
                if prev.get("sprint") != story.get("sprint"):
                    changes.append(f"Sprint: {prev.get('sprint')} -> {story.get('sprint')}")
                if prev.get("status") != story.get("status"):
                    changes.append(f"Status: {prev.get('status')} -> {story.get('status')}")
                if changes:
                    items.append({
                        "type": "modified", "category": "Story",
                        "description": f"{story['id']}: {story.get('title', '')}",
                        "detail": "; ".join(changes),
                    })

        # Diff epics
        prev_epic_ids = {e["id"] for e in previous_data.get("epics", [])}
        curr_epic_ids = {e["id"] for e in current_data.get("epics", [])}
        for eid in sorted(curr_epic_ids - prev_epic_ids):
            epic = next((e for e in current_data["epics"] if e["id"] == eid), {})
            items.append({
                "type": "added", "category": "Epic",
                "description": f"{eid}: {epic.get('title', '')}",
                "detail": "",
            })

        # Diff sprint plans
        prev_sprints = {s["number"] for s in previous_data.get("sprints", [])}
        curr_sprints = {s["number"] for s in current_data.get("sprints", [])}
        for sn in sorted(curr_sprints - prev_sprints):
            sp = next((s for s in current_data["sprints"] if s["number"] == sn), {})
            items.append({
                "type": "added", "category": "Sprint Plan",
                "description": f"Sprint {sn}: {sp.get('goal', '')}",
                "detail": f"{sp.get('story_count', 0)} stories",
            })

    elif report_type == "technical":
        # Diff ADRs
        prev_adr_ids = {a["id"] for a in previous_data.get("adrs", [])}
        curr_adr_ids = {a["id"] for a in current_data.get("adrs", [])}
        for aid in sorted(curr_adr_ids - prev_adr_ids):
            adr = next((a for a in current_data["adrs"] if a["id"] == aid), {})
            items.append({
                "type": "added", "category": "ADR",
                "description": f"{aid}: {adr.get('title', '')}",
                "detail": adr.get("decision_summary", "")[:100],
            })

        # Diff API specs
        prev_specs = {s["name"] for s in previous_data.get("api_specs", [])}
        curr_specs = {s["name"] for s in current_data.get("api_specs", [])}
        for sn in sorted(curr_specs - prev_specs):
            spec = next((s for s in current_data["api_specs"] if s["name"] == sn), {})
            items.append({
                "type": "added", "category": "API Spec",
                "description": sn,
                "detail": f"{spec.get('endpoint_count', 0)} endpoints",
            })
        # Check endpoint count changes in existing specs
        for spec in current_data.get("api_specs", []):
            prev = next((s for s in previous_data.get("api_specs", [])
                         if s["name"] == spec["name"]), None)
            if prev and prev["endpoint_count"] != spec["endpoint_count"]:
                diff = spec["endpoint_count"] - prev["endpoint_count"]
                items.append({
                    "type": "modified", "category": "API Spec",
                    "description": spec["name"],
                    "detail": f"Endpoints: {prev['endpoint_count']} -> {spec['endpoint_count']} ({'+' if diff > 0 else ''}{diff})",
                })

        # Diff services
        prev_svc = {s["name"] for s in previous_data.get("services", [])}
        curr_svc = {s["name"] for s in current_data.get("services", [])}
        for sn in sorted(curr_svc - prev_svc):
            items.append({
                "type": "added", "category": "Service",
                "description": sn, "detail": "",
            })

        # Diff migrations
        prev_mig = previous_data.get("data_models", {}).get("migration_count", 0)
        curr_mig = current_data.get("data_models", {}).get("migration_count", 0)
        if curr_mig > prev_mig:
            items.append({
                "type": "added", "category": "Schema Migration",
                "description": f"{curr_mig - prev_mig} new migration(s)",
                "detail": f"Total: {curr_mig}",
            })

    added = sum(1 for i in items if i["type"] == "added")
    modified = sum(1 for i in items if i["type"] == "modified")
    removed = sum(1 for i in items if i["type"] == "removed")

    return {
        "items": items,
        "summary": {"added": added, "modified": modified, "removed": removed},
        "total_changes": added + modified + removed,
    }


# ── Generate sprint report JSON to disk ───────────────────────────────────


def generate_sprint_data(project_dir: Path, sprint_num: int,
                          sprint_type: str = "dev",
                          output_path: Optional[Path] = None) -> Path:
    """Generate sprint-summary-{N}.json for the report renderer."""
    data = build_sprint_report_data(project_dir, sprint_num, sprint_type)
    if sprint_type == "hardening":
        folder_name = "hardening"
    elif sprint_type == "uat":
        folder_name = "uat"
    else:
        folder_name = f"sprint-{sprint_num}"

    if output_path is None:
        output_path = project_dir / "reports" / folder_name / f"sprint-data.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return output_path


def generate_requirements_data(project_dir: Path,
                                output_path: Optional[Path] = None) -> Path:
    """Generate requirements report data JSON."""
    reports_dir = project_dir / "reports" / "requirements"
    reports_dir.mkdir(parents=True, exist_ok=True)

    # Determine version
    existing = sorted(reports_dir.glob("v*.data.json"))
    version = len(existing) + 1

    # Load previous version for changelog
    prev_data = {}
    if existing:
        prev_data = _load_json(existing[-1])

    data = build_requirements_summary(project_dir, version)

    # Build changelog if v2+
    changelog = {}
    if prev_data:
        changelog = build_changelog(data, prev_data, "requirements")
    data["changelog"] = changelog

    if output_path is None:
        output_path = reports_dir / f"v{version}.data.json"
    output_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return output_path


def generate_technical_data(project_dir: Path,
                             output_path: Optional[Path] = None) -> Path:
    """Generate technical report data JSON."""
    reports_dir = project_dir / "reports" / "technical"
    reports_dir.mkdir(parents=True, exist_ok=True)

    # Determine version
    existing = sorted(reports_dir.glob("v*.data.json"))
    version = len(existing) + 1

    # Load previous version for changelog
    prev_data = {}
    if existing:
        prev_data = _load_json(existing[-1])

    data = build_technical_summary(project_dir, version)

    # Build changelog if v2+
    changelog = {}
    if prev_data:
        changelog = build_changelog(data, prev_data, "technical")
    data["changelog"] = changelog

    if output_path is None:
        output_path = reports_dir / f"v{version}.data.json"
    output_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return output_path

