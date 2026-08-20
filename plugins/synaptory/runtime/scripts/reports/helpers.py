"""Shared utilities for all report renderers."""

import json
from datetime import datetime, timezone
from html import escape
from pathlib import Path


def css_link(depth: int = 1) -> str:
    """Return CSS link tag with correct relative path depth."""
    prefix = "../" * depth
    return f'<link rel="stylesheet" href="{prefix}assets/report-style.css">'


def get_latest_version(reports_dir: Path, report_type: str) -> int:
    """Find highest version number from existing v*.html files."""
    target = reports_dir / report_type
    if not target.exists():
        return 0
    versions = []
    for f in target.glob("v*.html"):
        stem = f.stem
        if stem.startswith("v") and "-" not in stem:
            try:
                versions.append(int(stem[1:]))
            except ValueError:
                continue
    return max(versions, default=0)


def check_immutability(reports_dir: Path, folder: str) -> bool:
    """Check if sprint reports folder is locked. Returns True if locked."""
    meta_path = reports_dir / folder / ".report-meta.json"
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return meta.get("locked", False)
    except (json.JSONDecodeError, OSError):
        return False


def write_report_meta(target_dir: Path, sprint_num: int, planned: int, completed: int):
    """Write .report-meta.json for immutability tracking."""
    meta = {
        "locked": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sprint_num": sprint_num,
        "planned_stories": planned,
        "completed_stories": completed,
    }
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / ".report-meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )


def html_doc(title: str, css_link_tag: str, body: str) -> str:
    """Wrap body content in a full HTML document shell."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(title)}</title>
{css_link_tag}
</head>
<body>
<div class="container">
{body}
</div>
</body>
</html>"""


def badge(text: str, css_class: str) -> str:
    """Render an inline badge span."""
    return f'<span class="badge {css_class}">{escape(text)}</span>'


def status_badge(status: str) -> str:
    """Render a status badge with appropriate color class."""
    mapping = {
        "TODO": "badge-type",
        "IN_PROGRESS": "badge-draft",
        "DONE": "badge-phase",
        "BLOCKED": "status-open",
        "PASS": "badge-phase",
        "FAIL": "status-open",
    }
    css = mapping.get(status, "badge-type")
    return f'<span class="badge {css}">{escape(status)}</span>'


def priority_badge(priority: str) -> str:
    """Render a priority badge."""
    mapping = {
        "Must": "status-open",
        "Should": "badge-draft",
        "Could": "badge-type",
    }
    css = mapping.get(priority, "badge-type")
    return f'<span class="badge {css}">{escape(priority)}</span>'


def size_badge(size: str) -> str:
    """Render a story size badge."""
    return f'<span class="badge badge-type">{escape(size)}</span>'


def locked_badge(locked: bool) -> str:
    """Render a LOCKED or DRAFT badge."""
    if locked:
        return '<span class="badge badge-locked">LOCKED</span>'
    return '<span class="badge badge-draft">DRAFT</span>'


def timestamp_now() -> str:
    """Return current UTC time as display string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
