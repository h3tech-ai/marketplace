"""Receipt normalization and findings extraction for v2 story-scoped receipts."""

import os
import re
from typing import Optional

# v2 role abbreviation mapping for story-scoped receipt naming.
# Canonical direction: abbreviation -> full role name.
ROLE_ABBREV_MAP = {
    "se": "software-engineer", "qe": "quality-engineer", "cr": "code-reviewer",
    "ce": "compliance-engineer", "po": "project-owner", "sa": "solution-architect",
    "pe": "platform-engineer", "tw": "technical-writer", "ra": "research-advisor",
}

# Reverse: full role name -> abbreviation. Legacy alias product-manager → po
# is also accepted at read time for in-flight receipts written by older plugin
# versions.
ROLE_TO_ABBREV = {v: k for k, v in ROLE_ABBREV_MAP.items()}
ROLE_TO_ABBREV["product-manager"] = "po"

# v2 lifecycle states (for reference, not ordering).
SCRUM_STATES = [
    "INCEPTION", "SPRINT_PLANNING", "SPRINT_EXECUTION",
    "SPRINT_REVIEW", "SPRINT_RETRO", "SPRINT_CLOSE",
    "RELEASE", "COMPLETE",
]
KANBAN_STATES = ["DISCOVER", "READY", "EXECUTION", "REVIEW", "RELEASE", "COMPLETE"]


def _extract_story_id_from_filename(filename: str) -> str:
    """Extract story_id from v2 filename like 'US-001-se.json'.

    Strategy: strip .json, then strip the last '-{abbrev}' segment if the
    suffix is a known role abbreviation.
    """
    base = filename.replace(".json", "")
    # Check if the last segment after '-' is a known abbreviation.
    parts = base.rsplit("-", 1)
    if len(parts) == 2 and parts[1] in ROLE_ABBREV_MAP:
        return parts[0]
    return base


def normalize_receipt(raw: dict) -> dict:
    """Normalize v2 story-scoped receipt into canonical form."""
    # Story ID: explicit field > extracted from filename
    story_id = (
        raw.get("story_id")
        or _extract_story_id_from_filename(raw.get("_filename", ""))
        or "unknown"
    )

    # Agent/role: accept role or agent
    role = raw.get("role") or raw.get("agent") or "unknown"

    # Backend + model traceability
    backend = raw.get("backend", "unknown")
    raw_model = raw.get("model", "unknown")
    identity = dict(raw_model) if isinstance(raw_model, dict) else {
        "kind": "legacy_untyped", "value": raw_model, "evidence_class": "legacy-untyped"
    }
    # Reports also read rejected/incomplete receipts. Preserve their attribution
    # object while keeping an absent or malformed display value harmless.
    model = identity.get("value") or identity.get("kind", "invalid")
    if not isinstance(model, str):
        model = "invalid"

    # Metrics
    metrics = raw.get("metrics", {})

    # Artifacts
    artifacts = raw.get("artifacts") or raw.get("outputs") or []
    if isinstance(artifacts, list):
        key_artifacts = [os.path.basename(str(a)) for a in artifacts[:5] if a]
        artifacts_count = len(artifacts)
    else:
        key_artifacts = []
        artifacts_count = 0

    # Verification commands
    # `null` or a scalar here once aborted the whole summary build (#847); a
    # receipt that cannot say what it ran contributes no commands.
    verification_commands = raw.get("verification_commands")
    if not isinstance(verification_commands, list):
        verification_commands = []

    # Story DoD (per-story Definition of Done evaluation)
    story_dod = raw.get("story_dod", {})

    # Confidence
    confidence = raw.get("confidence", {})

    # Completed timestamp
    completed_at = raw.get("completed_at", "")

    return {
        "story_id": story_id,
        "role": role,
        "backend": backend,
        "model": model,
        "model_identity": identity,
        "runtime_version": raw.get("runtime_version"),
        "model_route": raw.get("model_route"),
        "metrics": metrics,
        "artifacts_count": artifacts_count,
        "key_artifacts": key_artifacts,
        "verification_commands": verification_commands,
        "story_dod": story_dod,
        "confidence": confidence,
        "completed_at": completed_at,
        # Preserve original filename for receipt matching
        "_filename": raw.get("_filename", ""),
        # Which receipt home this one came from. Receipts are collected across
        # the flat, per-spec and per-Cycle homes (#730), so a consumer that
        # wants a narrower scope takes it from here rather than re-deriving the
        # layout — re-deriving it is how a reader goes blind in the first place.
        "_receipt_home": raw.get("_receipt_home", ""),
        "_spec_id": raw.get("_spec_id", ""),
        "_cycle_id": raw.get("_cycle_id", ""),
        # Preserve structured findings/fixes for extraction
        "_findings": raw.get("findings", {}),
        "_fixes": raw.get("fixes", []),
        "_blocking_issues": raw.get("blocking_issues", []),
        "_sr_results": raw.get("sr_results", {}),
    }


#: Severities the report layer knows how to render, most severe first.
#: `unknown` is the bucket for a finding that did not state a usable one: it is
#: counted, never promoted into `medium` (the display default for a dict with
#: no `severity` key at all), so a number never shows up in a column nobody
#: wrote it under. Matches the rule `spq_state_machine.FINDING_SEVERITIES`
#: documents for the Cycle-side reader.
SEVERITIES = ("critical", "high", "medium", "low", "unknown")


def _text(value) -> str:
    """A receipt field as display text. Absent, null and structured values are "".

    Receipts are written by agents, and a field that should be text arrives as
    null, a list or a number often enough that one bad value must not decide
    whether the whole dashboard builds (#847). An int is kept (`"id": 7`);
    containers are not, since a dict or list is unhashable and has no sensible
    one-line form.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _severity(value, default: str = "unknown") -> str:
    """Lower-cased severity text, or `default` when the value is not usable text."""
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return default


def _receipt_label(r: dict) -> str:
    """Name a receipt for a warning: its file, with the home it came from."""
    name = r.get("_filename") or "<unnamed receipt>"
    home = r.get("_receipt_home")
    return os.path.join(home, name) if home else name


def shape_warnings(raw: dict) -> list:
    """What `normalize_receipt` had to tolerate in one raw receipt, as warnings."""
    out = []
    if not isinstance(raw.get("verification_commands", []), list):
        out.append(
            f"{_receipt_label(raw)}: verification_commands is "
            f"{type(raw['verification_commands']).__name__}, expected a list; ignored"
        )
    return out


def extract_findings(receipts: list, warnings: Optional[list] = None) -> dict:
    """Build structured findings from receipt data only (no markdown regex).

    Collects findings from all agent receipts (QE, CE, CR, and others).

    Tolerant by design (#847): one receipt with a malformed `findings` /
    `fixes` / `blocking_issues` / `sr_results` must not abort the summary the
    status dashboard is built from. What cannot be read is dropped or filed
    under severity `unknown`, and `warnings` (when given) names the receipt and
    the shape, so the loss is stated rather than silent.
    """
    items = []
    summary = {sev: 0 for sev in SEVERITIES}
    fixed = {sev: 0 for sev in SEVERITIES}
    if warnings is None:
        warnings = []

    def warn(r: dict, what: str) -> None:
        msg = f"{_receipt_label(r)}: {what}"
        if msg not in warnings:
            warnings.append(msg)

    for r in receipts:
        findings_data = r.get("_findings", {})
        role = r["role"]
        story_id = r["story_id"]

        # Case 1: findings is a list of structured items
        if isinstance(findings_data, list):
            for f in findings_data:
                if isinstance(f, str) and f.strip():
                    # A legacy receipt listing findings as plain strings. The
                    # text is all it states, so it becomes the title and the
                    # severity is honestly unknown.
                    warn(r, "findings entry is a plain string; counted as severity unknown")
                    f = {"title": f}
                    sev = "unknown"
                elif not isinstance(f, dict):
                    warn(r, f"findings entry of type {type(f).__name__} skipped")
                    continue
                elif "severity" not in f:
                    # No `severity` key keeps its historical `medium` default;
                    # a key that is present but unusable is `unknown`.
                    sev = "medium"
                else:
                    sev = _severity(f["severity"])
                    if sev == "unknown":
                        warn(r, "finding severity is not usable text; counted as unknown")
                items.append({
                    "id": _text(f.get("id")),
                    "title": _text(f.get("title")),
                    "severity": sev,
                    "status": _text(f.get("status")) or "open",
                    "fixed_by": _text(f.get("fixed_by")),
                    "source_role": role,
                    "source_story": story_id,
                    "file_ref": _text(f.get("file_ref")),
                    "description": _text(f.get("description")),
                })
                summary[sev] = summary.get(sev, 0) + 1

        # Case 2: findings is a summary dict with counts
        elif isinstance(findings_data, dict) and any(
            k in findings_data for k in ("critical", "high", "medium", "low", "total")
        ):
            for sev in ("critical", "high", "medium", "low"):
                raw = findings_data.get(sev, 0)
                # A receipt may express per-severity findings as a list of
                # objects (e.g. {"high": [{...}, {...}]}) rather than a scalar
                # count. Coerce to an int so aggregation never raises (#145).
                count = len(raw) if isinstance(raw, list) else (raw if isinstance(raw, int) else 0)
                summary[sev] = summary.get(sev, 0) + count

            # Extract blocking_issues as individual finding items
            blocking = r.get("_blocking_issues", [])
            if blocking is not None and not isinstance(blocking, list):
                warn(r, f"blocking_issues is {type(blocking).__name__}, expected a list; ignored")
                blocking = []
            for issue in blocking or []:
                if isinstance(issue, str):
                    # Parse "SR-01: Description" format
                    m = re.match(r"^([\w-]+):\s*(.+)", issue)
                    fid = m.group(1) if m else ""
                    title = m.group(2) if m else issue
                    items.append({
                        "id": fid,
                        "title": title,
                        "severity": "critical",
                        "status": "open",
                        "fixed_by": "",
                        "source_role": role,
                        "source_story": story_id,
                        "file_ref": "",
                        "description": "",
                    })

            # Extract SR results as findings
            sr_results = r.get("_sr_results", {})
            if sr_results is not None and not isinstance(sr_results, dict):
                warn(r, f"sr_results is {type(sr_results).__name__}, expected an object; ignored")
                sr_results = {}
            for sr_id, result in (sr_results or {}).items():
                if result in ("FAIL", "PARTIAL", "NOT_IMPLEMENTED"):
                    sev = "critical" if result == "FAIL" else "medium"
                    items.append({
                        "id": sr_id,
                        "title": f"{sr_id}: {result}",
                        "severity": sev,
                        "status": "open",
                        "fixed_by": "",
                        "source_role": role,
                        "source_story": story_id,
                        "file_ref": "",
                        "description": "",
                    })

        elif findings_data is not None and not isinstance(findings_data, dict):
            warn(r, f"findings is {type(findings_data).__name__}, expected a list or object; ignored")

    # Apply fixes from remediation receipts
    fixes_applied = []
    for r in receipts:
        fixes = r.get("_fixes", [])
        story_id = r["story_id"]
        if fixes is not None and not isinstance(fixes, list):
            warn(r, f"fixes is {type(fixes).__name__}, expected a list; ignored")
            fixes = []
        for fix in fixes or []:
            if isinstance(fix, dict):
                finding_ref = (
                    _text(fix.get("finding"))
                    or _text(fix.get("finding_id"))
                    or _text(fix.get("id"))
                )
                sev = _severity(fix.get("severity"), default="")
                fixes_applied.append({
                    "finding_ref": finding_ref,
                    "severity": sev,
                    "fixed_by": story_id,
                    "title": _text(fix.get("title")),
                    "description": _text(fix.get("description")),
                })
            else:
                warn(r, f"fixes entry of type {type(fix).__name__} skipped")

    # Mark items as fixed based on fixes
    for fix in fixes_applied:
        matched = False
        for item in items:
            # An item with no id cannot be matched by reference: "" is a
            # substring of every string, so an id-less finding (every plain
            # string finding, #847) would otherwise be marked fixed by
            # whichever fix came first.
            if (fix["finding_ref"] and item["id"] and
                (fix["finding_ref"] in item["id"] or
                 item["id"] in fix["finding_ref"])):
                item["status"] = "fixed"
                item["fixed_by"] = fix["fixed_by"]
                matched = True
                break
        # If no existing item matched, add the fix as a resolved finding
        if not matched and fix.get("title"):
            sev = fix["severity"] if fix["severity"] in summary else "high"
            items.append({
                "id": fix["finding_ref"],
                "title": fix["title"],
                "severity": sev,
                "status": "fixed",
                "fixed_by": fix["fixed_by"],
                "source_role": "remediation",
                "source_story": fix["fixed_by"],
                "file_ref": "",
                "description": fix.get("description", ""),
            })

    # Count fixed items
    for item in items:
        if item["status"] == "fixed":
            sev = item["severity"]
            fixed[sev] = fixed.get(sev, 0) + 1

    # If we have fixes but no items from verification, infer fixed counts from fix severities
    if fixes_applied and not any(i["status"] == "open" for i in items):
        for fix in fixes_applied:
            sev = fix["severity"]
            if sev in fixed:
                fixed[sev] = fixed.get(sev, 0) + 1

    # Deduplicate items by id
    seen_ids = set()
    unique_items = []
    for item in items:
        key = item["id"] or item["title"][:60]
        if key not in seen_ids:
            seen_ids.add(key)
            unique_items.append(item)

    # Sort by severity
    SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "unknown": 4}
    unique_items.sort(key=lambda x: SEV_ORDER.get(x["severity"], 5))

    return {
        "summary": summary,
        "fixed": fixed,
        "items": unique_items,
    }
