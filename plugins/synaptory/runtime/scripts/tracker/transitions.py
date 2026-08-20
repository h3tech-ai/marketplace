"""Tracker-side workflow transition guards (#111).

The local Scrum FSM (`hooks/lib/story_pipeline.VALID_TRANSITIONS`) gates the
per-story SE→QE→CR pipeline. Multiple FSM sub-states collapse onto the same
tracker stage (e.g. both `in_progress` and `testing` show as `IN_PROGRESS`
on Teamwork/Jira/GitHub), so a 1:1 mirror is impossible. Instead, this
module defines a *tracker-side* FSM that enforces the same audit-trail
guarantees at the granularity the tracker actually sees:

    BACKLOG → TO_DO → IN_PROGRESS → IN_REVIEW → DONE

Rationale: the bug surfaced in #108's UX-gap section is "TO_DO → DONE
erases the QE+CR review gate." This module is the structural fix —
adapter `update_story_status` calls `validate_transition` before applying,
and rejects illegal jumps with `TrackerTransitionError` unless the caller
opts in with `allow_skip=True` (surfaced as `--allow-skip` in tracker_cli).
"""

from __future__ import annotations

from .base import AdapterError


# Canonical tracker stages. Every adapter normalises its native vocabulary
# (e.g. Jira "In Progress", Teamwork "IN_PROGRESS") onto these.
TRACKER_STAGES: tuple[str, ...] = (
    "BACKLOG",
    "TO_DO",
    "IN_PROGRESS",
    "IN_REVIEW",
    # Per #116: optional stage for projects with `per_story_acceptance`
    # enabled. Trackers without an "Awaiting Acceptance" column fall back
    # to IN_REVIEW + a tag (handled by each adapter).
    "AWAITING_ACCEPTANCE",
    "DONE",
)

# Allowed forward + recovery transitions per stage. The list is the audit
# gate: tickets must enter IN_REVIEW (and, when enabled, AWAITING_ACCEPTANCE)
# before they can reach DONE. Backward moves (rework / reopen) are permitted
# to one stage back. From AWAITING_ACCEPTANCE the PO can also reject all the
# way back to TO_DO (the `redo` reject reason).
VALID_TRANSITIONS: dict[str, set[str]] = {
    "BACKLOG":             {"TO_DO", "IN_PROGRESS"},
    "TO_DO":               {"IN_PROGRESS", "BACKLOG"},
    "IN_PROGRESS":         {"IN_REVIEW", "TO_DO", "BACKLOG"},
    # Trackers without AWAITING_ACCEPTANCE still get DONE directly — the
    # adapter falls back to a tag. Keeping DONE in the legal set preserves
    # back-compat with the toggle-off default.
    "IN_REVIEW":           {"AWAITING_ACCEPTANCE", "DONE", "IN_PROGRESS"},
    "AWAITING_ACCEPTANCE": {"DONE", "IN_PROGRESS", "TO_DO", "IN_REVIEW"},
    "DONE":                {"IN_PROGRESS"},  # reopen
}

# Aliases mapped to the canonical stage. Covers Jira's verbose names,
# Teamwork/GitHub case variants, and the `COMPLETED` alias for `DONE`.
# Keys are matched case-insensitively after stripping/replacing spaces.
_STATUS_ALIASES: dict[str, str] = {
    "backlog": "BACKLOG",
    "to_do": "TO_DO",
    "todo": "TO_DO",
    "to do": "TO_DO",
    "open": "TO_DO",
    "in_progress": "IN_PROGRESS",
    "inprogress": "IN_PROGRESS",
    "in progress": "IN_PROGRESS",
    "in_review": "IN_REVIEW",
    "inreview": "IN_REVIEW",
    "in review": "IN_REVIEW",
    "review": "IN_REVIEW",
    # #116 — Awaiting Acceptance variants.
    "awaiting_acceptance": "AWAITING_ACCEPTANCE",
    "awaitingacceptance": "AWAITING_ACCEPTANCE",
    "awaiting acceptance": "AWAITING_ACCEPTANCE",
    "pending acceptance": "AWAITING_ACCEPTANCE",
    "pending_acceptance": "AWAITING_ACCEPTANCE",
    "po review": "AWAITING_ACCEPTANCE",
    "done": "DONE",
    "completed": "DONE",
    "closed": "DONE",
    "resolved": "DONE",
    # BLOCKED is a label, not a stage — see validate_transition for the
    # special-case handling. Kept in the alias map so callers see a
    # canonical form when they round-trip through normalize_status.
    "blocked": "BLOCKED",
}


class TrackerTransitionError(AdapterError):
    """Raised when `update_story_status` would skip an audit-gate stage.

    Carries enough context for callers to either retry with the correct
    intermediate stage or escalate to `--allow-skip` if the bypass is
    intentional.
    """

    def __init__(self, story_id: str, current: str, target: str, valid: set[str]):
        self.story_id = story_id
        self.current = current
        self.target = target
        self.valid = valid
        valid_str = ", ".join(sorted(valid)) if valid else "(none — terminal)"
        super().__init__(
            f"Illegal tracker transition for {story_id}: "
            f"{current} → {target}. Valid targets: {valid_str}. "
            f"Pass --allow-skip (or allow_skip=True) to bypass."
        )


def normalize_status(raw: str | None) -> str:
    """Map a tracker-native status to the canonical stage.

    Returns the canonical stage if a match is found, otherwise the raw
    value (uppercased). An empty / None input maps to BACKLOG — the
    default starting stage on every supported tracker.
    """
    if not raw:
        return "BACKLOG"
    key = raw.strip().lower().replace("-", " ")
    return _STATUS_ALIASES.get(key) or raw.strip().upper()


def validate_transition(
    story_id: str,
    current: str | None,
    target: str,
    *,
    allow_skip: bool = False,
) -> None:
    """Reject illegal transitions; pass silently otherwise.

    Both `current` and `target` are normalised before comparison, so
    callers can pass tracker-native vocabulary directly.

    `allow_skip=True` bypasses validation entirely — kept narrow for
    the `--allow-skip` escape hatch in tracker_cli.
    """
    if allow_skip:
        return

    cur = normalize_status(current)
    tgt = normalize_status(target)

    # BLOCKED is a label, not a stage. Per story_pipeline.TRACKER_STATUS_MAP
    # and the Teamwork adapter, "block this ticket" applies a tag without
    # moving the workflow stage — and "unblock" needs to move from BLOCKED
    # back to whatever stage was active. Neither direction is an audit-gate
    # skip, so BLOCKED is always allowed as either endpoint.
    if tgt == "BLOCKED" or cur == "BLOCKED":
        return

    # Idempotent set: same-stage updates are a no-op the adapter still
    # serialises (status writes are cheap and the tracker may reorder).
    if cur == tgt:
        return

    # Unknown current stage: refuse to extrapolate. Caller should
    # pass --allow-skip when they really mean to overwrite an exotic
    # tracker-native status.
    if cur not in VALID_TRANSITIONS:
        raise TrackerTransitionError(story_id, cur, tgt, set())

    valid = VALID_TRANSITIONS[cur]
    if tgt not in valid:
        raise TrackerTransitionError(story_id, cur, tgt, valid)
