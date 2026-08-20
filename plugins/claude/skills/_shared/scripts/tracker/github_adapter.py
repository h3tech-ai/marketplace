"""GitHub Issues adapter — maps synaptory artifacts to GitHub Issues.

Mapping:
- Epic     -> Issue Type: Epic (or Feature fallback), label EPIC-001
- Story    -> Issue Type: Story (or Feature fallback), label US-037
- Task     -> Issue Type: Task
- Bug      -> Issue Type: Bug
- Sprint   -> Milestone
- Status   -> Open/Closed state. Only `blocked` label for BLOCKED status.
- Priority -> Labels (P1-critical, P2-high, P3-medium, P4-low)
- Points   -> Labels (points:1, points:3, points:5, points:8)

Issue Types:
  GitHub supports custom Issue Types (Bug, Feature, Task are defaults).
  synaptory defines additional types (Epic, Story) during initialization.
  Creating types requires admin:org scope — if unavailable, the adapter
  prints setup instructions for an org admin and falls back to labels.
  Assigning types to issues only requires repo scope (no elevation needed).

ID convention:
  The Synaptory ID (e.g. US-037) is the canonical cross-adapter identifier.
  It is stored as a `US-037` label on each issue for searchability.
  Issue titles use the clean human-readable name only — no ID prefix.
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Optional

from .base import (
    ArtifactAdapter, Epic, Story, SprintInfo, BacklogItem,
    AcceptanceCriterion, SprintMetrics, QueryFilter,
    AdapterError, AdapterAuthError, AdapterOfflineError,
)
from .transport.github_transport import GitHubTransport


# ── Issue Type Definitions ───────────────────────────────────────────────

# Types synaptory expects. Defaults (Bug, Feature, Task) exist on every repo.
# Epic and Story are custom types created during initialization.
REQUIRED_ISSUE_TYPES = {
    "Epic":    {"color": "PURPLE", "description": "Epic — groups related features and stories"},
    "Story":   {"color": "BLUE",   "description": "User story — deliverable unit of work"},
    # These are GitHub defaults — no need to create:
    # "Bug":   exists
    # "Feature": exists
    # "Task":  exists
}

# Synaptory entity -> GitHub Issue Type name
ENTITY_TYPE_MAP = {
    "epic":        "Epic",
    "story":       "Story",
    "task":        "Task",
    "bug":         "Bug",
    "enhancement": "Feature",
}

_ENTITY_TYPE_PREFIXES = {
    "B-": "bug", "BUG-": "bug",
    "EPIC-": "epic",
    "T-": "task", "TASK-": "task",
}


def _detect_entity_type(entity_id: str) -> str:
    """Detect entity type from ID prefix. Returns 'story' as default."""
    upper = entity_id.upper()
    for prefix, etype in _ENTITY_TYPE_PREFIXES.items():
        if upper.startswith(prefix):
            return etype
    return "story"


# ── Labels (only what GitHub doesn't handle natively) ────────────────────

PRIORITY_LABELS = {
    "Must":   {"name": "P1-critical", "color": "b60205", "description": "Must have — required for MVP"},
    "Should": {"name": "P2-high",     "color": "d93f0b", "description": "Should have — important"},
    "Could":  {"name": "P3-medium",   "color": "fbca04", "description": "Could have — nice to have"},
    "Won't":  {"name": "P4-low",      "color": "c5def5", "description": "Won't have — out of scope"},
}

SIZE_TO_POINTS = {"S": 1, "M": 3, "L": 5, "XL": 8}

# GitHub status model: open/closed state + label-based status indicators.
# Only statuses that need a label are listed — open/closed handles the rest.
STATUS_LABELS = {
    "BLOCKED":              "blocked",
    "IN_REVIEW":            "in-review",
    # #116 — PO-acceptance lifecycle labels. Adapter still uses
    # open/closed for the BACKLOG/IN_PROGRESS/DONE axis; these are
    # discriminator labels on the open side.
    "AWAITING_ACCEPTANCE":  "awaiting-acceptance",
    "CANCELLED":            "cancelled",
}

# Reverse: label name → synaptory status (for _issue_to_story)
STATUS_REVERSE = {
    "blocked":              "BLOCKED",
    "in-review":            "IN_REVIEW",
    "awaiting-acceptance":  "AWAITING_ACCEPTANCE",
    "cancelled":            "CANCELLED",
}


def _clean_title(raw_title: str) -> str:
    """Strip the ID prefix from a story/epic title.

    'US-037 — CMS-Specific Activation Export' -> 'CMS-Specific Activation Export'
    'EPIC-001 — Authentication, Session Management & RBAC' -> 'Authentication, Session Management & RBAC'
    """
    # Strip leading ID + separator:  US-037 — , US-037 - , EPIC-001 —
    m = re.match(r'^[A-Z]+-\w+\s*[—–\-:]\s*', raw_title)
    if m:
        return raw_title[m.end():].strip()
    return raw_title


# ── Issue Body Templates ────────────────────────────────────────────────


def _build_epic_body(epic: Epic, raw_text: str = "") -> str:
    """Build rich GitHub issue body for an epic."""
    parts = [f"> synaptory: `{epic.id}`"]
    if raw_text:
        # Extract key sections from the epic Markdown file
        sections_to_include = [
            "Objective", "User Impact Statement", "Technical Context",
            "Data Model", "NFRs", "Feature List",
        ]
        for section_name in sections_to_include:
            section = _extract_md_section(raw_text, section_name)
            if section:
                parts.append(f"\n## {section_name}\n\n{section}")
    else:
        parts.append(f"\nFeature count: {epic.feature_count}")
    return "\n".join(parts)


def _build_story_body(story: Story, raw_text: str = "") -> str:
    """Build rich GitHub issue body for a story."""
    parts = [f"> synaptory: `{story.id}` | Feature: `{story.feature}`"]

    if raw_text:
        # Extract the user story narrative
        story_section = _extract_md_section(raw_text, "Story")
        if story_section:
            parts.append(f"\n## User Story\n\n{story_section}")

        # ACs with full Given/When/Then from the source file
        ac_section = _extract_md_section(raw_text, "Acceptance Criteria")
        if ac_section:
            # Convert ACs to checklist format preserving GWT detail
            parts.append("\n## Acceptance Criteria\n")
            current_ac = None
            for line in ac_section.splitlines():
                stripped = line.strip()
                ac_match = re.match(r'\*\*AC-(\w+)\s*[—–\-]*\s*([^*]*)\*\*:?\s*(.*)', stripped)
                if ac_match:
                    ac_id = ac_match.group(1)
                    ac_title = ac_match.group(2).strip().rstrip(":").strip()
                    after = ac_match.group(3).strip()
                    label = ac_title or after or f"AC-{ac_id}"
                    parts.append(f"- [ ] **AC-{ac_id}: {label}**")
                    current_ac = ac_id
                elif stripped.startswith("- Given") or stripped.startswith("- When") or stripped.startswith("- Then"):
                    parts.append(f"  {stripped}")
                elif current_ac and stripped.startswith("-"):
                    parts.append(f"  {stripped}")
        elif story.acceptance_criteria:
            # Fallback: use parsed ACs
            parts.append("\n## Acceptance Criteria\n")
            for ac in story.acceptance_criteria:
                check = "x" if ac.met else " "
                parts.append(f"- [{check}] **{ac.id}: {ac.text}**")

        # Business Rules
        br_section = _extract_md_section(raw_text, "Business Rules")
        if br_section:
            parts.append(f"\n## Business Rules\n\n{br_section}")

        # Testing Notes
        test_section = _extract_md_section(raw_text, "Testing Notes")
        if test_section:
            parts.append(f"\n## Testing Notes\n\n{test_section}")
    else:
        # Minimal body when no source text available
        if story.acceptance_criteria:
            parts.append("\n## Acceptance Criteria\n")
            for ac in story.acceptance_criteria:
                check = "x" if ac.met else " "
                parts.append(f"- [{check}] **{ac.id}: {ac.text}**")

    return "\n".join(parts)


def _extract_md_section(text: str, heading: str) -> str:
    """Extract content under a markdown heading until next same-level heading."""
    lines = text.splitlines()
    in_section = False
    section_lines = []
    heading_level = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            hashes = len(stripped) - len(stripped.lstrip("#"))
            title = stripped.lstrip("#").strip()
            if title.lower() == heading.lower():
                in_section = True
                heading_level = hashes
                continue
            elif in_section and hashes <= heading_level:
                break
        if in_section:
            section_lines.append(line)
    return "\n".join(section_lines).strip()


class GitHubAdapter(ArtifactAdapter):
    """Adapter using GitHub Issues for ticket tracking."""

    def __init__(self, project_dir: Path, config, spec=None):
        """Construct the adapter.

        spec — optional SpecConfig binding the adapter to one spec in a
        multi-spec GitHub-Issues-backed project (docs/multi-spec-design.md).
        When set, every read filters by the spec's label or milestone, every
        write applies the corresponding label/milestone side-effect, and
        cache files route to `.synaptory/.orchestrator/specs/{spec.id}/`.
        When None (legacy), behavior is unchanged — flat cache paths, no
        read filter beyond what the caller passes.
        """
        super().__init__(project_dir, config)
        self.spec = spec

        # Per-spec repo override (rare — multi-spec normally stays in one repo).
        repo = (spec.github.repo if (spec is not None and spec.github and spec.github.repo)
                else config.github.repo)
        if not repo:
            repo = self._detect_repo()
        self.transport = GitHubTransport(repo)
        self.prefix = config.github.label_prefix
        self.points_prefix = config.github.points_label_prefix
        # Per-spec milestone prefix override allows spec teams to namespace
        # their sprints (e.g. "Platform Sprint ").
        if spec is not None and spec.github and spec.github.sprint_milestone_prefix:
            self.milestone_prefix = spec.github.sprint_milestone_prefix
        else:
            self.milestone_prefix = config.github.sprint_milestone_prefix
        # Per-spec issue_type / status_map overrides fall back to the
        # top-level maps when empty (same pattern as Teamwork's
        # workflow_stages). Empty ⇒ ENTITY_TYPE_MAP / STATUS_LABELS defaults
        # ⇒ unchanged behavior.
        spec_types = (dict(getattr(spec.github, "issue_type_overrides", {}) or {})
                      if (spec is not None and spec.github) else {})
        self._issue_type_overrides = spec_types or dict(
            getattr(config.github, "issue_type_overrides", {}) or {})
        spec_status = (dict(getattr(spec.github, "status_map", {}) or {})
                       if (spec is not None and spec.github) else {})
        self._status_map_override = spec_status or dict(
            getattr(config.github, "status_map", {}) or {})
        self._id_map: Optional[dict] = None

        if spec is not None:
            spec_dir = project_dir / ".synaptory" / ".orchestrator" / "specs" / spec.id
            self._id_map_path = spec_dir / "tracker-id-map.json"
            self._backlog_order_path = spec_dir / "backlog-order.json"
            self._local_cache_path = spec_dir / "tracker-data.json"
        else:
            self._id_map_path = project_dir / ".synaptory" / ".orchestrator" / "tracker-id-map.json"
            self._backlog_order_path = project_dir / ".synaptory" / ".orchestrator" / "backlog-order.json"
            self._local_cache_path = project_dir / ".synaptory" / ".orchestrator" / "tracker-data.json"

        self._type_ids: Optional[dict[str, str]] = None  # lazy-loaded: {"Epic": "IT_...", "Story": "IT_..."}

    # ── Override resolution ──────────────────────────────────

    def _gh_issue_type(self, entity_type: str, default: str = "Story") -> str:
        """Resolve the GitHub Issue Type name for a synaptory entity kind.

        Per-entity override (from the `issue_type` config) wins; otherwise
        the built-in ENTITY_TYPE_MAP default applies.
        """
        overrides = getattr(self, "_issue_type_overrides", None) or {}
        return (overrides.get(entity_type)
                or ENTITY_TYPE_MAP.get(entity_type, default))

    def _status_label(self, canonical: str) -> str:
        """Resolve the status *label* name for a label-backed canonical state
        (BLOCKED, IN_REVIEW, AWAITING_ACCEPTANCE, CANCELLED). Per-repo
        `status_map` override wins; otherwise the STATUS_LABELS default.
        """
        override = getattr(self, "_status_map_override", None) or {}
        return (override.get(canonical)
                or STATUS_LABELS.get(canonical, ""))

    # ── Spec filter helpers ──────────────────────────────────

    def _spec_search_fragment(self) -> str:
        """GitHub Issues search query fragment scoping reads to the active spec.

        Returns "" when no spec is configured. Use in `search_issues()` queries
        by appending after the existing terms (space-separated, GitHub search syntax).

          label    → `label:"<value>"`
          milestone→ `milestone:"<value>"`
        """
        if self.spec is None:
            return ""
        ftype = self.spec.filter.type
        value = self.spec.filter.value
        if ftype == "label":
            return f'label:"{value}"'
        if ftype == "milestone":
            return f'milestone:"{value}"'
        return ""

    def _spec_label_for_list_query(self) -> Optional[str]:
        """When the spec is label-bound, return the label string to pass via
        the `labels=` kwarg of transport.list_issues() (the non-search code path).
        Returns None for milestone-bound or unscoped adapters."""
        if self.spec is None:
            return None
        if self.spec.filter.type == "label":
            return self.spec.filter.value
        return None

    def _spec_milestone_for_list_query(self) -> Optional[str]:
        """When the spec is milestone-bound, return the milestone title to
        pass via the `milestone=` kwarg of transport.list_issues(). Returns
        None for label-bound or unscoped adapters."""
        if self.spec is None:
            return None
        if self.spec.filter.type == "milestone":
            return self.spec.filter.value
        return None

    def _ensure_spec_writeable(self, op: str) -> None:
        """Refuse writes when the spec uses a read-only filter type."""
        if self.spec is None:
            return
        if self.spec.filter.type in ("jql",):
            raise AdapterError(
                f"Spec {self.spec.id!r} uses filter.type={self.spec.filter.type!r} "
                f"which is read-only; refusing {op}."
            )

    def _apply_spec_write_side_effect(
        self, *, labels: list, milestone_title: Optional[str] = None,
    ) -> tuple[list, Optional[str]]:
        """Apply the spec's write side-effect to issue-creation parameters.

        Returns (labels, milestone_title) — both usable in the transport
        `create_issue(..., labels=..., milestone_title=...)` call.

        - label    → append the spec label to `labels` (deduped).
        - milestone→ set `milestone_title` to the spec value if not already set.
        """
        if self.spec is None:
            return labels, milestone_title
        ftype = self.spec.filter.type
        value = self.spec.filter.value
        if ftype == "label":
            if value and value not in labels:
                labels.append(value)
                # Make sure the label exists in the repo before assignment.
                try:
                    self._ensure_ref_label(value)
                except Exception:
                    pass
        elif ftype == "milestone":
            if not milestone_title:
                milestone_title = value
        return labels, milestone_title

    # ── Lifecycle ─────────────────────────────────────────────

    def initialize(self) -> None:
        """Verify auth, ensure issue types, create labels."""
        if not self.transport.check_cli():
            raise AdapterAuthError(
                "GitHub CLI (gh) not available.\n"
                "Install: brew install gh && gh auth login\n"
                "Or set GITHUB_TOKEN env var."
            )
        self._ensure_issue_types()
        self._ensure_labels()
        self._load_id_map()

    def health_check(self) -> dict:
        h = self.transport.health_check()
        h["backend"] = "github"
        h["id_map_loaded"] = self._id_map is not None
        return h

    # ── Local Cache Write-Through ────────────────────────────

    def _update_local_cache_status(self, story_id: str, status: str) -> None:
        """Write-through: keep tracker-data.json in sync after GitHub updates."""
        if not self._local_cache_path.exists():
            return
        try:
            data = json.loads(self._local_cache_path.read_text(encoding="utf-8"))
            for s in data.get("stories", []):
                if s["id"] == story_id:
                    s["status"] = status
                    break
            self._local_cache_path.write_text(
                json.dumps(data, indent=2, default=str), encoding="utf-8",
            )
        except (json.JSONDecodeError, OSError):
            pass  # cache is best-effort; don't fail the primary operation

    def sync_to_local_cache(self, sprint_num: Optional[int] = None) -> dict:
        """Pull current status from GitHub and update tracker-data.json.

        Args:
            sprint_num: If given, sync only stories in that sprint.
                        If None, sync all stories.

        Returns:
            dict with sync stats: {"synced": N, "added": N, "errors": [...]}
        """
        stats: dict = {"synced": 0, "added": 0, "errors": []}
        if not self._local_cache_path.exists():
            # Bootstrap an empty cache so the first sync in a fresh workspace
            # works (#108). `migrate` is the wrong direction for new projects —
            # there's nothing local to push; the user wants to pull remote.
            self._local_cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._local_cache_path.write_text(
                json.dumps({"epics": [], "sprints": [], "stories": []}, indent=2),
                encoding="utf-8",
            )

        try:
            data = json.loads(self._local_cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            stats["errors"].append(f"Failed to read local cache: {e}")
            return stats

        local_stories = {s["id"]: s for s in data.get("stories", [])}

        if sprint_num is not None:
            live_stories = self.get_sprint_backlog(sprint_num)
        else:
            live_stories = self.list_stories()

        for story in live_stories:
            if story.id in local_stories:
                local_stories[story.id]["status"] = story.status
                stats["synced"] += 1
            else:
                data["stories"].append({
                    "id": story.id, "title": story.title,
                    "feature": story.feature, "epic": story.epic,
                    "priority": story.priority, "status": story.status,
                    "size": story.size, "sprint": story.sprint,
                    "ac_count": story.ac_count, "acceptance_criteria": [],
                    "raw_text": "",
                })
                stats["added"] += 1

        try:
            self._local_cache_path.write_text(
                json.dumps(data, indent=2, default=str), encoding="utf-8",
            )
        except OSError as e:
            stats["errors"].append(f"Failed to write local cache: {e}")

        return stats

    # ── Epic Operations ──────────────────────────────────────

    def list_epics(self) -> list[Epic]:
        # Search by the resolved issue type if available, fall back to label.
        epic_type = self._gh_issue_type("epic", default="Epic")
        base = (f'repo:{self.transport.repo} type:"{epic_type}"'
                if self._get_type_ids().get(epic_type)
                else f'repo:{self.transport.repo} label:epic')
        frag = self._spec_search_fragment()
        query = f'{base} {frag}'.strip() if frag else base
        issues = self.transport.search_issues(query)
        return [self._issue_to_epic(i) for i in issues]

    def get_epic(self, epic_id: str) -> Optional[Epic]:
        gh_num = self._resolve_id(epic_id)
        if gh_num is None:
            return None
        try:
            issue = self.transport.get_issue(gh_num)
            return self._issue_to_epic(issue)
        except AdapterError:
            return None

    def create_epic(self, epic: Epic, raw_text: str = "") -> Epic:
        """Create an epic issue with synaptory ID label, issue type, and rich body."""
        self._ensure_spec_writeable("create_epic")
        id_label = epic.id
        self._ensure_ref_label(id_label)
        labels = [id_label]
        # Apply spec write side-effect (label append / milestone set).
        labels, milestone_title = self._apply_spec_write_side_effect(labels=labels)
        body = _build_epic_body(epic, raw_text or epic.raw_text)
        title = _clean_title(epic.title)
        issue = self.transport.create_issue(
            title=title,
            body=body,
            labels=labels,
            milestone_title=milestone_title,
        )
        epic.tracker_id = str(issue["number"])
        epic.tracker_url = issue.get("url", "")
        self._set_issue_type(issue["number"], self._gh_issue_type("epic", default="Epic"))
        self._save_id_mapping(epic.id, issue["number"], issue.get("url", ""))
        return epic

    def update_epic(self, epic_id: str, **fields) -> Epic:
        gh_num = self._resolve_id(epic_id)
        if gh_num is None:
            raise AdapterError(f"Epic not found in ID map: {epic_id}")
        update = {}
        if "title" in fields:
            update["title"] = _clean_title(fields["title"])
        self.transport.update_issue(gh_num, **update)
        return self.get_epic(epic_id) or Epic(id=epic_id, title=fields.get("title", ""))

    # ── Story Operations ─────────────────────────────────────

    def list_stories(self, epic_id: Optional[str] = None,
                     sprint: Optional[int] = None) -> list[Story]:
        # Search by issue type if available, fall back to label
        milestone_name = None
        if sprint is not None:
            milestone_name = f"{self.milestone_prefix}{sprint}"
        # Multi-spec: milestone-bound spec narrows to its milestone unless the
        # caller already specified one (sprint takes precedence).
        spec_milestone = self._spec_milestone_for_list_query()
        if milestone_name is None and spec_milestone:
            milestone_name = spec_milestone
        story_type = self._gh_issue_type("story")
        if self._get_type_ids().get(story_type):
            query = f'repo:{self.transport.repo} type:"{story_type}"'
            if milestone_name:
                query += f' milestone:"{milestone_name}"'
            frag = self._spec_search_fragment()
            if frag and not query.endswith(frag):
                # Only append the spec fragment for label-bound specs — the
                # milestone case was already injected above.
                if self.spec is None or self.spec.filter.type == "label":
                    query = f'{query} {frag}'.strip()
            issues = self.transport.search_issues(query)
        else:
            labels = ["story"]
            spec_label = self._spec_label_for_list_query()
            if spec_label and spec_label not in labels:
                labels.append(spec_label)
            issues = self.transport.list_issues(labels=labels, milestone=milestone_name)
        stories = [self._issue_to_story(i) for i in issues]
        if epic_id:
            stories = [s for s in stories if s.epic == epic_id]
        return stories

    def get_story(self, story_id: str) -> Optional[Story]:
        gh_num = self._resolve_id(story_id)
        if gh_num is None:
            return None
        try:
            issue = self.transport.get_issue(gh_num)
            return self._issue_to_story(issue)
        except AdapterError:
            return None

    def create_ticket(self, story: Story, raw_text: str = "") -> Story:
        """Create a ticket issue with synaptory ID label, priority, points, milestone, and rich body."""
        self._ensure_spec_writeable("create_ticket")
        id_label = story.id
        self._ensure_ref_label(id_label)
        labels = [id_label]

        # Priority label
        if story.priority in PRIORITY_LABELS:
            labels.append(PRIORITY_LABELS[story.priority]["name"])
        # Blocked label (only status that needs a label — open/closed handles the rest)
        if story.status == "BLOCKED":
            labels.append("blocked")
        # Points label from size
        if story.size in SIZE_TO_POINTS:
            labels.append(f"{self.points_prefix}{SIZE_TO_POINTS[story.size]}")

        # Clean title (strip ID prefix)
        title = _clean_title(story.title)

        # Rich body
        body = _build_story_body(story, raw_text or story.raw_text)

        # Milestone (sprint assignment) — gh issue create takes milestone title
        milestone_title = None
        if story.sprint:
            try:
                milestone_title = f"{self.milestone_prefix}{int(story.sprint)}"
            except (ValueError, TypeError):
                pass

        # Spec write side-effect — append spec label (label-bound spec) or
        # set milestone (milestone-bound spec, only if caller didn't already).
        labels, milestone_title = self._apply_spec_write_side_effect(
            labels=labels, milestone_title=milestone_title,
        )

        issue = self.transport.create_issue(
            title=title,
            body=body,
            labels=labels,
            milestone_title=milestone_title,
        )
        story.tracker_id = str(issue["number"])
        story.tracker_url = issue.get("url", "")
        # Set correct issue type based on entity ID prefix
        entity_type = _detect_entity_type(story.id)
        self._set_issue_type(issue["number"], self._gh_issue_type(entity_type))
        self._save_id_mapping(story.id, issue["number"], issue.get("url", ""))

        # Link as sub-issue of epic
        if story.epic:
            epic_num = self._resolve_id(story.epic)
            if epic_num:
                try:
                    self.transport.add_sub_issue(epic_num, issue["number"])
                except AdapterError:
                    pass  # Sub-issue API may not be available

        return story

    def update_story_status(self, story_id: str, status: str, *,
                            allow_skip: bool = False) -> Story:
        self._validate_status_transition(story_id, status, allow_skip)
        gh_num = self._resolve_id(story_id)
        if gh_num is None:
            raise AdapterError(f"Story not found in ID map: {story_id}")

        # Status model: open/closed + label-based status indicators.
        # Label names resolve through the per-repo status_map override
        # (empty ⇒ the STATUS_LABELS defaults, unchanged behavior).
        blocked_lbl = self._status_label("BLOCKED")
        in_review_lbl = self._status_label("IN_REVIEW")
        awaiting_lbl = self._status_label("AWAITING_ACCEPTANCE")
        cancelled_lbl = self._status_label("CANCELLED")
        all_status_labels = [blocked_lbl, in_review_lbl, awaiting_lbl, cancelled_lbl]

        if status == "BLOCKED":
            self.transport.update_issue(
                gh_num, add_labels=[blocked_lbl],
                remove_labels=[in_review_lbl, awaiting_lbl, cancelled_lbl],
            )
            try:
                self.transport.reopen_issue(gh_num)
            except AdapterError:
                pass
        elif status == "IN_REVIEW":
            self.transport.update_issue(
                gh_num, add_labels=[in_review_lbl],
                remove_labels=[blocked_lbl, awaiting_lbl, cancelled_lbl],
            )
            try:
                self.transport.reopen_issue(gh_num)
            except AdapterError:
                pass
        elif status == "AWAITING_ACCEPTANCE":
            # #116: open + `awaiting-acceptance` label so the PO can scan
            # the queue with a single GitHub label filter at Sprint Review.
            self.transport.update_issue(
                gh_num, add_labels=[awaiting_lbl],
                remove_labels=[blocked_lbl, in_review_lbl, cancelled_lbl],
            )
            try:
                self.transport.reopen_issue(gh_num)
            except AdapterError:
                pass
        elif status == "CANCELLED":
            # PO cancel: closed, with `cancelled` label preserving the
            # audit trail (distinct from a regular DONE close).
            self.transport.update_issue(
                gh_num, add_labels=[cancelled_lbl],
                remove_labels=[blocked_lbl, in_review_lbl, awaiting_lbl],
            )
            try:
                self.transport.close_issue(gh_num)
            except AdapterError:
                pass
        elif status in ("DONE", "COMPLETED"):
            self.transport.update_issue(gh_num, remove_labels=all_status_labels)
            self.transport.close_issue(gh_num)
        else:
            # BACKLOG, TO_DO, IN_PROGRESS — open, no status labels
            self.transport.update_issue(gh_num, remove_labels=all_status_labels)
            try:
                self.transport.reopen_issue(gh_num)
            except AdapterError:
                pass

        # Write-through to local cache
        self._update_local_cache_status(story_id, status)

        story = self.get_story(story_id)
        if story:
            return story
        return Story(id=story_id, title=story_id, status=status)

    def update_story(self, story_id: str, **fields) -> Story:
        if "status" in fields:
            return self.update_story_status(story_id, fields["status"])
        gh_num = self._resolve_id(story_id)
        if gh_num is None:
            raise AdapterError(f"Story not found: {story_id}")
        update = {}
        if "title" in fields:
            update["title"] = _clean_title(fields["title"])
        if update:
            self.transport.update_issue(gh_num, **update)
        return self.get_story(story_id) or Story(id=story_id, title="")

    def get_acceptance_criteria(self, story_id: str) -> list[AcceptanceCriterion]:
        gh_num = self._resolve_id(story_id)
        if gh_num is None:
            return []
        issue = self.transport.get_issue(gh_num)
        return self._parse_checklist_acs(issue.get("body", ""))

    def update_acceptance_criteria(self, story_id: str, ac_id: str,
                                   met: bool) -> AcceptanceCriterion:
        gh_num = self._resolve_id(story_id)
        if gh_num is None:
            raise AdapterError(f"Story not found: {story_id}")
        issue = self.transport.get_issue(gh_num)
        body = issue.get("body", "")
        pattern = rf"- \[[ x]\] \*\*{re.escape(ac_id)}:"
        replacement = f"- [{'x' if met else ' '}] **{ac_id}:"
        new_body = re.sub(pattern, replacement, body)
        if new_body != body:
            self.transport.update_issue(gh_num, body=new_body)
        return AcceptanceCriterion(id=ac_id, text="", met=met)

    # ── Task & Bug Operations ────────────────────────────────

    def create_aux_ticket(self, ticket_type: str, title: str,
                          parent_id: Optional[str] = None, **fields) -> dict:
        self._ensure_spec_writeable("create_aux_ticket")
        labels, milestone_title = self._apply_spec_write_side_effect(labels=[])
        issue = self.transport.create_issue(
            title=title, body=fields.get("body", ""), labels=labels,
            milestone_title=milestone_title,
        )
        # Set issue type (Bug, Task, Feature)
        self._set_issue_type(issue["number"], self._gh_issue_type(ticket_type, default="Task"))
        if parent_id:
            parent_num = self._resolve_id(parent_id)
            if parent_num:
                try:
                    self.transport.add_sub_issue(parent_num, issue["number"])
                except AdapterError:
                    pass
        return {"id": str(issue["number"]), "title": title, "type": ticket_type,
                "url": issue.get("url", "")}

    def close_ticket(self, ticket_id: str, resolution: str = "") -> dict:
        gh_num = self._resolve_id(ticket_id)
        if gh_num is None:
            try:
                gh_num = int(ticket_id)
            except ValueError:
                raise AdapterError(f"Ticket not found: {ticket_id}")
        self.transport.close_issue(gh_num)
        return {"id": ticket_id, "status": "DONE", "resolution": resolution}

    # ── Sprint Operations ────────────────────────────────────

    def list_sprints(self) -> list[SprintInfo]:
        milestones = self.transport.list_milestones(state="all")
        result = []
        for ms in milestones:
            title = ms.get("title", "")
            if not title.startswith(self.milestone_prefix):
                continue
            num_str = title[len(self.milestone_prefix):]
            try:
                num = int(num_str)
            except ValueError:
                continue
            issues = self.transport.list_issues(milestone=title)
            story_ids = self._extract_synaptory_ids(issues)
            result.append(SprintInfo(
                number=num,
                goal=ms.get("description", title),
                story_ids=story_ids,
                story_count=len(story_ids),
                tracker_id=str(ms["number"]),
                tracker_url=ms.get("html_url", ""),
            ))
        return sorted(result, key=lambda s: s.number)

    def get_sprint(self, sprint_num: int) -> Optional[SprintInfo]:
        for s in self.list_sprints():
            if s.number == sprint_num:
                return s
        return None

    def create_sprint(self, sprint: SprintInfo) -> SprintInfo:
        title = f"{self.milestone_prefix}{sprint.number}"
        ms = self.transport.create_milestone(
            title=title,
            description=sprint.goal,
        )
        sprint.tracker_id = str(ms.get("number", ""))
        sprint.tracker_url = ms.get("html_url", "")
        self._save_id_mapping(f"SPRINT-{sprint.number}", ms.get("number", 0), ms.get("html_url", ""))
        return sprint

    def get_sprint_backlog(self, sprint_num: int) -> list[Story]:
        milestone_name = f"{self.milestone_prefix}{sprint_num}"
        story_type = self._gh_issue_type("story")
        if self._get_type_ids().get(story_type):
            query = f'repo:{self.transport.repo} type:"{story_type}" milestone:"{milestone_name}"'
            # Append spec label fragment if applicable (milestone-bound specs
            # would conflict with the sprint milestone — they're already
            # filtered narrowly enough by the sprint scope).
            if self.spec is not None and self.spec.filter.type == "label":
                query = f'{query} {self._spec_search_fragment()}'.strip()
            issues = self.transport.search_issues(query)
        else:
            labels = ["story"]
            spec_label = self._spec_label_for_list_query()
            if spec_label and spec_label not in labels:
                labels.append(spec_label)
            issues = self.transport.list_issues(labels=labels, milestone=milestone_name)
        return [self._issue_to_story(i) for i in issues]

    def assign_to_sprint(self, story_id: str, sprint_num: int) -> Story:
        gh_num = self._resolve_id(story_id)
        if gh_num is None:
            raise AdapterError(f"Story not found: {story_id}")
        ms_num = self._get_milestone_number(sprint_num)
        if ms_num is None:
            raise AdapterError(f"Sprint milestone not found: {sprint_num}")
        self.transport.update_issue(gh_num, milestone=ms_num)
        # Story stays in BACKLOG — transitions to TO_DO when sprint starts
        return self.get_story(story_id) or Story(id=story_id, title="")

    def remove_from_sprint(self, story_id: str, sprint_num: int) -> Story:
        gh_num = self._resolve_id(story_id)
        if gh_num is None:
            raise AdapterError(f"Story not found: {story_id}")
        self.transport._run_api(
            "PATCH", f"repos/{self.transport.repo}/issues/{gh_num}",
            body={"milestone": None},
        )
        return self.get_story(story_id) or Story(id=story_id, title="")

    def close_sprint(self, sprint_num: int) -> SprintInfo:
        ms_num = self._get_milestone_number(sprint_num)
        if ms_num is None:
            raise AdapterError(f"Sprint milestone not found: {sprint_num}")
        self.transport.close_milestone(ms_num)
        return self.get_sprint(sprint_num) or SprintInfo(number=sprint_num, goal="")

    def move_incomplete_to_next(self, from_sprint: int, to_sprint: int) -> list[Story]:
        stories = self.get_sprint_backlog(from_sprint)
        # CANCELLED is terminal (PO cancel, #116) — never carried forward.
        incomplete = [s for s in stories
                      if s.status not in ("DONE", "COMPLETED", "CANCELLED")]
        for s in incomplete:
            self.assign_to_sprint(s.id, to_sprint)
        return incomplete

    def get_sprint_metrics(self, sprint_num: int) -> SprintMetrics:
        stories = self.get_sprint_backlog(sprint_num)
        planned = len(stories)
        completed = sum(1 for s in stories if s.status in ("DONE", "COMPLETED"))
        in_progress = sum(1 for s in stories if s.status in ("IN_PROGRESS", "IN_REVIEW"))
        blocked = sum(1 for s in stories if s.status == "BLOCKED")
        return SprintMetrics(
            planned=planned, completed=completed,
            in_progress=in_progress, blocked=blocked,
            velocity=float(completed),
        )

    # ── Backlog Operations ───────────────────────────────────

    def get_backlog(self) -> list[BacklogItem]:
        story_type = self._gh_issue_type("story")
        if self._get_type_ids().get(story_type):
            query = f'repo:{self.transport.repo} type:"{story_type}"'
            frag = self._spec_search_fragment()
            if frag:
                query = f'{query} {frag}'.strip()
            issues = self.transport.search_issues(query)
        else:
            labels = ["story"]
            spec_label = self._spec_label_for_list_query()
            if spec_label and spec_label not in labels:
                labels.append(spec_label)
            milestone = self._spec_milestone_for_list_query()
            issues = self.transport.list_issues(labels=labels, milestone=milestone)
        items = [self._issue_to_backlog_item(i) for i in issues]
        order = self._load_backlog_order()
        if order:
            order_map = {sid: pos for pos, sid in enumerate(order)}
            items.sort(key=lambda i: order_map.get(i.id, 9999))
        return items

    def query_tickets(self, query_filter: QueryFilter) -> list[BacklogItem]:
        items = self.get_backlog()
        if query_filter.status:
            items = [i for i in items if i.status in query_filter.status]
        if query_filter.priority:
            items = [i for i in items if i.priority in query_filter.priority]
        if query_filter.sprint is not None:
            items = [i for i in items if i.sprint == str(query_filter.sprint)]
        if query_filter.text:
            q = query_filter.text.lower()
            items = [i for i in items if q in i.title.lower() or q in i.id.lower()]
        if query_filter.assignee:
            target = query_filter.assignee
            # #187 — `currentUser()` is a Jira-only JQL sentinel. On GitHub it
            # must be resolved to the authenticated login, else it matches no
            # assignee and `--mine` silently returns nothing.
            if target == "currentUser()":
                target = self.transport.current_user()
            items = [i for i in items if i.assignee == target]
        return items

    # ── Reporting Operations ─────────────────────────────────

    def get_velocity_data(self, num_sprints: int = 0) -> list[dict]:
        sprints = self.list_sprints()
        data = []
        for s in sprints:
            metrics = self.get_sprint_metrics(s.number)
            data.append({"sprint": s.number, "planned": metrics.planned, "completed": metrics.completed})
        if num_sprints > 0:
            data = data[-num_sprints:]
        return data

    def get_sprint_report_data(self, sprint_num: int) -> dict:
        metrics = self.get_sprint_metrics(sprint_num)
        sprint = self.get_sprint(sprint_num)
        stories = self.get_sprint_backlog(sprint_num)
        return {
            "sprint_num": sprint_num,
            "sprint_goal": sprint.goal if sprint else f"Sprint {sprint_num}",
            "stories": {
                "planned": metrics.planned, "completed": metrics.completed,
                "in_progress": metrics.in_progress, "blocked": metrics.blocked,
                "completion_pct": round(
                    (metrics.completed / metrics.planned * 100) if metrics.planned > 0 else 0
                ),
                "status_by_story": [
                    {"id": s.id, "title": s.title, "status": s.status,
                     "priority": s.priority, "size": s.size, "feature": s.feature}
                    for s in stories
                ],
            },
        }

    def get_burndown_data(self, sprint_num: int) -> list[dict]:
        return []

    # ── Private Helpers ──────────────────────────────────────

    def _detect_repo(self) -> str:
        """Detect owner/repo from git remote origin."""
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=5,
                cwd=str(self.project_dir),
            )
            url = result.stdout.strip()
            if url.endswith(".git"):
                url = url[:-4]
            if "github.com/" in url:
                return url.split("github.com/")[1]
            if "github.com:" in url:
                return url.split("github.com:")[1]
        except Exception:
            pass
        raise AdapterError(
            "Could not detect GitHub repo. Set tracker.github.repo in .synaptory.yaml"
        )

    def _get_type_ids(self) -> dict[str, str]:
        """Lazy-load issue type IDs on first access."""
        if self._type_ids is None:
            available = self.transport.list_issue_types()
            self._type_ids = {t["name"]: t["id"] for t in available}
        return self._type_ids

    def _required_issue_types(self) -> dict:
        """Effective issue-type specs the adapter needs to exist.

        With no overrides this is exactly REQUIRED_ISSUE_TYPES. Overridden
        entity kinds swap in the custom name (with a generic spec when it
        isn't one of the built-in defaults); default names displaced by an
        override are dropped so we don't create types the adapter never sets.
        """
        if not (getattr(self, "_issue_type_overrides", None) or {}):
            return dict(REQUIRED_ISSUE_TYPES)
        required: dict = {}
        for entity in ENTITY_TYPE_MAP:
            name = self._gh_issue_type(entity)
            if name in REQUIRED_ISSUE_TYPES:
                required[name] = REQUIRED_ISSUE_TYPES[name]
            elif name not in ENTITY_TYPE_MAP.values():
                # Custom override name — GitHub defaults (Bug, Feature, Task)
                # exist on every repo and need no creation.
                required.setdefault(name, {
                    "color": "GRAY",
                    "description": "synaptory custom issue type",
                })
        return required

    def _ensure_issue_types(self) -> None:
        """Detect available issue types and create missing ones if possible.

        - Querying types requires repo scope (always available).
        - Creating types requires admin:org scope (org admin only).
        If creation fails, prints setup instructions and falls back to labels.
        """
        available = self.transport.list_issue_types()
        available_by_name = {t["name"]: t["id"] for t in available}
        self._type_ids = {name: available_by_name[name]
                          for name in available_by_name}

        required_types = self._required_issue_types()
        missing = [name for name in required_types
                   if name not in available_by_name]
        if not missing:
            return

        # Try to create missing types (needs admin:org)
        owner_id = self.transport.get_owner_id()
        created = []
        failed = []
        for name in missing:
            spec = required_types[name]
            try:
                result = self.transport.create_issue_type(
                    owner_id=owner_id, name=name,
                    description=spec["description"], color=spec["color"],
                )
                if result.get("id"):
                    self._type_ids[name] = result["id"]
                    created.append(name)
                else:
                    failed.append(name)
            except AdapterAuthError:
                failed.append(name)
            except AdapterError:
                failed.append(name)

        if failed:
            # Print guidance — don't fail, just degrade to labels
            print(
                f"\n  ⚠ Could not create GitHub Issue Types: {', '.join(failed)}\n"
                f"  Creating custom types requires admin:org scope.\n\n"
                f"  Ask an org admin to create them:\n"
                f"    Repository → Settings → Issue Types → New type\n\n"
                f"  Required types:\n"
                + "".join(f"    - {name} ({required_types[name]['color'].lower()}): "
                          f"{required_types[name]['description']}\n" for name in failed)
                + f"\n  Until created, the adapter will use labels as fallback.\n"
            )

    def _set_issue_type(self, issue_number: int, type_name: str) -> None:
        """Set the issue type on an issue. No-op if type not available."""
        type_id = self._get_type_ids().get(type_name)
        if type_id:
            try:
                self.transport.set_issue_type(issue_number, type_id)
            except AdapterError:
                pass  # Degrade silently — labels still identify the issue

    def _ensure_ref_label(self, label_name: str) -> None:
        """Create a synaptory ID label if it doesn't exist."""
        try:
            self.transport.create_label(
                name=label_name, color="c5def5",
                description=f"synaptory: {label_name}",
            )
        except AdapterError:
            pass  # Already exists (--force handles this)

    def _ensure_labels(self) -> None:
        """Create required labels if they don't exist.

        Only labels for things GitHub doesn't handle natively:
        - Priority labels (P1-critical, P2-high, etc.)
        - Points labels (points:1, points:3, etc.)
        - blocked (the one status not captured by open/closed)
        Issue types (Epic, Story, Bug, Task) and status (open/closed)
        are handled by GitHub natively — no labels needed.
        """
        existing = {l["name"] for l in self.transport.list_labels()}
        all_labels: dict[str, dict] = {}
        all_labels.update({v["name"]: v for v in PRIORITY_LABELS.values()})
        all_labels["blocked"] = {"name": "blocked", "color": "d73a4a", "description": "Blocked — waiting on dependency"}
        all_labels["in-review"] = {"name": "in-review", "color": "0e8a16", "description": "In review — awaiting human approval"}
        for pts in [1, 2, 3, 5, 8, 13]:
            name = f"{self.points_prefix}{pts}"
            all_labels[name] = {"name": name, "color": "c2e0c6", "description": f"{pts} story points"}

        for name, label in all_labels.items():
            if name not in existing:
                self.transport.create_label(
                    name=label["name"],
                    color=label.get("color", ""),
                    description=label.get("description", ""),
                )

    def _load_id_map(self) -> dict:
        if self._id_map is not None:
            return self._id_map
        if self._id_map_path.exists():
            try:
                self._id_map = json.loads(self._id_map_path.read_text(encoding="utf-8"))
            except Exception:
                self._id_map = {}
        else:
            self._id_map = {}
        return self._id_map

    def _save_id_mapping(self, synaptory_id: str, gh_number: int, gh_url: str = "") -> None:
        id_map = self._load_id_map()
        id_map[synaptory_id] = {"github_number": gh_number, "github_url": gh_url}
        self._id_map_path.parent.mkdir(parents=True, exist_ok=True)
        self._id_map_path.write_text(json.dumps(id_map, indent=2), encoding="utf-8")

    def _resolve_id(self, synaptory_id: str) -> Optional[int]:
        """Resolve a synaptory ID to a GitHub issue number."""
        id_map = self._load_id_map()
        entry = id_map.get(synaptory_id)
        if entry:
            return entry.get("github_number")
        return None

    def _get_milestone_number(self, sprint_num: int) -> Optional[int]:
        entry = self._load_id_map().get(f"SPRINT-{sprint_num}")
        if entry:
            return entry.get("github_number")
        title = f"{self.milestone_prefix}{sprint_num}"
        for ms in self.transport.list_milestones(state="all"):
            if ms.get("title") == title:
                return ms["number"]
        return None

    def _issue_to_epic(self, issue: dict) -> Epic:
        synaptory_id = self._extract_synaptory_id(issue)
        return Epic(
            id=synaptory_id or f"GH-{issue['number']}",
            title=issue.get("title", ""),
            tracker_id=str(issue["number"]),
            tracker_url=issue.get("url", ""),
        )

    def _issue_to_story(self, issue: dict) -> Story:
        synaptory_id = self._extract_synaptory_id(issue)
        labels = [l.get("name", "") if isinstance(l, dict) else l
                  for l in issue.get("labels", [])]

        # Status from open/closed state + status labels. Label names resolve
        # through the status_map override so custom label names read back to
        # their canonical states. All four label-backed states round-trip:
        # closed splits into CANCELLED (PO cancel, #116) vs DONE; open
        # discriminates BLOCKED / IN_REVIEW / AWAITING_ACCEPTANCE.
        if issue.get("state") in ("closed", "CLOSED"):
            if self._status_label("CANCELLED") in labels:
                status = "CANCELLED"
            else:
                status = "DONE"
        elif self._status_label("BLOCKED") in labels:
            status = "BLOCKED"
        elif self._status_label("IN_REVIEW") in labels:
            status = "IN_REVIEW"
        elif self._status_label("AWAITING_ACCEPTANCE") in labels:
            status = "AWAITING_ACCEPTANCE"
        else:
            status = "TO_DO"

        # Priority from labels
        priority = ""
        for p, info in PRIORITY_LABELS.items():
            if info["name"] in labels:
                priority = p
                break

        # Size from points label
        size = ""
        points_to_size = {v: k for k, v in SIZE_TO_POINTS.items()}
        for label in labels:
            if label.startswith(self.points_prefix):
                pts_str = label[len(self.points_prefix):]
                try:
                    size = points_to_size.get(int(pts_str), pts_str)
                except ValueError:
                    pass

        # Sprint from milestone
        sprint = ""
        ms = issue.get("milestone")
        if ms:
            ms_title = ms.get("title", "") if isinstance(ms, dict) else ""
            if ms_title.startswith(self.milestone_prefix):
                sprint = ms_title[len(self.milestone_prefix):]

        # Feature from body ref line
        feature = ""
        body = issue.get("body", "")
        feat_m = re.search(r'Feature:\s*`?(\S+?)`?(?:\s|$)', body)
        if feat_m:
            feature = feat_m.group(1)

        acs = self._parse_checklist_acs(body)

        # Assignee
        assignees = issue.get("assignees", [])
        assignee_value = None
        if assignees:
            first = assignees[0]
            assignee_value = first.get("login", "") if isinstance(first, dict) else str(first)

        return Story(
            id=synaptory_id or f"GH-{issue['number']}",
            title=issue.get("title", ""),
            feature=feature,
            status=status,
            priority=priority,
            size=size,
            sprint=sprint,
            ac_count=len(acs),
            acceptance_criteria=acs,
            assignee=assignee_value,
            tracker_id=str(issue["number"]),
            tracker_url=issue.get("url", ""),
        )

    def _issue_to_backlog_item(self, issue: dict) -> BacklogItem:
        story = self._issue_to_story(issue)
        return BacklogItem(
            id=story.id, title=story.title, feature=story.feature,
            priority=story.priority, status=story.status,
            size=story.size, sprint=story.sprint, assignee=story.assignee,
        )

    @staticmethod
    def _extract_synaptory_id(issue: dict) -> str:
        """Extract synaptory ID from label or body metadata.

        Looks for labels matching synaptory ID patterns (US-NNN, EPIC-NNN, etc.)
        directly — no prefix needed.
        """
        # Primary: direct ID label (cross-adapter convention)
        labels = issue.get("labels", [])
        for l in labels:
            name = l.get("name", "") if isinstance(l, dict) else l
            # Match synaptory ID patterns: US-001, EPIC-001, S1-01, etc.
            if re.match(r'^(US|EPIC|FEAT|BUG|ENH|TASK|S\d+)-\w+$', name):
                return name

        # Fallback: body metadata line
        body = issue.get("body", "")
        m = re.search(r'synaptory:\s*`?(\S+?)`?(?:\s|$)', body)
        if m:
            return m.group(1)

        # Legacy fallback: ref: label or [US-001] title prefix
        for l in labels:
            name = l.get("name", "") if isinstance(l, dict) else l
            if name.startswith("ref:"):
                return name[4:]
        title = issue.get("title", "")
        m = re.match(r"\[([A-Z]+-\w+)\]", title)
        if m:
            return m.group(1)

        return ""

    @staticmethod
    def _extract_synaptory_ids(issues: list[dict]) -> list[str]:
        return [hid for i in issues if (hid := GitHubAdapter._extract_synaptory_id(i))]

    @staticmethod
    def _parse_checklist_acs(body: str) -> list[AcceptanceCriterion]:
        """Parse acceptance criteria from GitHub issue body checklist."""
        acs = []
        if not body:
            return acs
        for line in body.splitlines():
            m = re.match(r"- \[([xX ])\] \*\*(AC-\w+):\s*(.*?)\*\*", line.strip())
            if m:
                acs.append(AcceptanceCriterion(
                    id=m.group(2), text=m.group(3).strip(), met=m.group(1).lower() == "x",
                ))
        return acs

    def _load_backlog_order(self) -> list[str]:
        if self._backlog_order_path.exists():
            try:
                return json.loads(self._backlog_order_path.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []

    def _save_backlog_order(self, order: list[str]) -> None:
        self._backlog_order_path.parent.mkdir(parents=True, exist_ok=True)
        self._backlog_order_path.write_text(json.dumps(order, indent=2), encoding="utf-8")
