# Sprint Review Ceremony

> **Lifecycle state:** `SPRINT_REVIEW`
> **Participants:** PO, Orchestrator, TW, stakeholders (user)
> **Output:** Sprint demo, sprint-level DoD evaluation, stakeholder feedback, backlog updates

## Prerequisites

Verify lifecycle state:
```
STATE=$(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/scrum_state_machine.py" read "$(pwd)")
# Verify lifecycle_state == "SPRINT_REVIEW"
```

Read sprint summary:
```
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/scrum_state_machine.py" summary "$(pwd)"
```

---

## Step 1 — Deploy Increment to Preview (Auto-Detected)

Check project type from `.synaptory.yaml` → `project.framework`:

**Web app detected** (nextjs, express, fastapi, gin, sveltekit, nuxt, remix):
- Run preview deployment:
  ```
  Read("${CLAUDE_PLUGIN_ROOT}/skills/synaptory/modes/preview.md")
  ```
- **Asset-exhaustive smoke check (#134 GAP-9) — REQUIRED before presenting the URL.** A root-only `curl` returning 200 proves nothing about a demo: a deploy that silently dropped `.next/static` still serves 200 with every stylesheet and script 404ing (the field incident: a stakeholder saw an unstyled page past nine green receipts). Run the asset check against the preview URL **plus every completed story's route**:
  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/preview_asset_check.py" {PREVIEW_URL} \
    /route-of-US-042 /route-of-US-043 ...
  ```
  It fetches each route's HTML, extracts every same-origin asset reference (scripts, stylesheets, images, preloads, CSS `url(...)`/`@import` one level deep) and GETs each one; exit 0 = `passed: true`, exit 1 = at least one referenced asset is non-200.
- **Present the preview URL ONLY after the asset check passes.** On failure the increment is **not demoable**: route the failure to PE (deploy-artifact copy step) or SE (broken reference), fix, and re-run the check before continuing the review.
- **Ban silent-fail copies on deploy-critical artifacts.** Never `cp -r ... 2>/dev/null` (or any stderr-suppressed copy) for build outputs the preview serves — a failed copy must fail the deploy step, so check every copy's exit code explicitly.

**Non-web project** (CLI tool, library, infrastructure, mobile):
- Skip preview deployment
- Note: "Preview deployment skipped — {project_type} project"

---

## Step 2 — Generate Sprint Reports

Dispatch the Technical Writer agent to generate sprint reports.

```
TW_BACKEND=$(python3 "${CLAUDE_PLUGIN_ROOT}/skills/_shared/scripts/backend/backend_config.py" "$(pwd)" "technical-writer")
```

> **MANDATORY: Spawn this agent via the `Agent()` tool — do not write the sprint reports inline.** Inline execution skips the SubagentStop hook, so no receipt is written and the work never reaches `/cost` or `/quality`. The dispatch must look like `Agent(subagent_type="synaptory:technical-writer", description="TW sprint report", prompt=<self-contained prompt per the wrapper>)` — see `${CLAUDE_PLUGIN_ROOT}/skills/_shared/backends/${TW_BACKEND}.md`. The TW writes its receipt to `.synaptory/.orchestrator/receipts/SPRINT-{N}-tw.json` as its last action.

**TW prompt context:**
- Sprint number, goal
- Completed stories with DoD results
- Receipt data (metrics, findings, verification results)
- Git metrics (commits, files changed)

**TW output:**
- Sprint quality report → `reports/sprint-{N}-quality.md`
- Sprint progress report → `reports/sprint-{N}-progress.md`

---

## Step 3 — Demo Working Software

Present the increment organized by Sprint Goal and completed stories:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  SPRINT {N} REVIEW
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Sprint Goal: {SPRINT_GOAL}
  {PREVIEW_URL if web app}

  Completed Stories:
  ┌────────────┬──────────────────────────────┬────────┬──────────┐
  │ Story      │ Title                        │ Status │ DoD      │
  ├────────────┼──────────────────────────────┼────────┼──────────┤
  │ US-042     │ User login with MFA          │ Done   │ ✓ Pass   │
  │ US-043     │ Password reset               │ Done   │ ✓ Pass   │
  │ US-044     │ Session management           │ Done   │ ⚠ Warn   │
  │ INFRA-005  │ Add Redis cache              │ Done   │ ✓ Pass   │
  └────────────┴──────────────────────────────┴────────┴──────────┘

  Metrics:
    Stories:   {completed}/{planned} completed
    Velocity:  {velocity} stories
    Tests:     {tests_passing}/{tests_total} passing
    Coverage:  {coverage}%

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

For stories with DoD warnings, expand the detail:
```
  ⚠ US-044 DoD Warning:
    ✓ tests_pass
    ✓ build_succeeds
    ✗ no_critical_findings — 1 high-severity finding (not blocking, non-critical)
    ✓ code_reviewed
```

---

## Step 3.5 — Per-Story Acceptance Walk (signal-gated, #116)

**Trigger:** `.synaptory.yaml` sets `sprint.review.per_story_acceptance: true`. This is now the **default** (#44) — new projects ship with it on, so the PO signs off each story against its ACs. When explicitly set `false` (unattended/CI runs), skip this step and proceed to Step 4 (auto-Done flow).

When on, the SE→QE→CR pipeline routes each story to `awaiting_acceptance` instead of `done`. The PO walks every story in that queue with an Accept / Reject decision, captured as a structured `rejection_feedback` record. This is the audit-trail gate for HIPAA / SOC2 / HITRUST work — code can pass automated review and still be unfit for product reasons.

**Read the queue:**
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/story_pipeline.py" list_stories "$(pwd)" awaiting_acceptance
```

For **each** story in the queue, present the PO prompt:

```
━━━ Per-Story PO Acceptance ━━━━━━━━━━━━━━━━━━━━━━━━

  Story:    {STORY_ID} — {TITLE}
  DoD:      {DoD result table — same shape as Step 3}
  Receipts: {SE / QE / CR receipt paths}

  Decisions:
    1. Accept                        → done
    2. Reject — needs-fix            → in_progress (current sprint)
    3. Reject — redo (approach wrong)→ queued      (current sprint)
    4. Reject — defer to next sprint → queued      (carry over)
    5. Reject — cancel               → cancelled   (terminal)
```

**Accept** — story moves to `done`, tracker → DONE + `complete_task` fires:
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/scrum_state_machine.py" accept_story "$(pwd)" "{STORY_ID}" \
  --accepted-by "{PO_EMAIL_OR_ROLE}"
```

**Reject** — capture the reason class + free-form feedback + optional AC-criteria changes. Each rejection is appended to the story's `rejection_feedback` list and surfaces to the next SE dispatch:
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/scrum_state_machine.py" reject_story "$(pwd)" "{STORY_ID}" \
  --reason needs-fix|redo|defer|cancel \
  --feedback "{PO free-form text}" \
  --rejected-by "{PO_EMAIL_OR_ROLE}" \
  [--ac-change "AC-01: now requires …"] [--ac-change "AC-04: drop"]
```

Reason → next state mapping:

| Reason | Story state | Tracker column | Sprint membership |
|---|---|---|---|
| `needs-fix` | `in_progress` | In Progress | stays in current sprint |
| `redo` | `queued` | To Do | stays in current sprint |
| `defer` | `queued` (carry over) | To Do | moves to next sprint |
| `cancel` | `cancelled` | Done + `cancelled` tag | removed from velocity |

Continue until the queue is empty. After the walk, proceed to Step 4 — `aggregate_dod` now only counts accepted/done stories; cancelled stories are excluded by design.

> **Audit-trail note:** every accept/reject writes a `story_accept` or `story_reject` event to `.synaptory/.orchestrator/events.jsonl`, captured with `accepted_by` / `rejected_by`. For HIPAA / SOC2 sign-off this is the canonical record.

---

## Step 4 — Sprint-Level DoD Overlay

**First — UI delivery assertion (#44).** Before reporting any completion
percentage, assert that no UI-bearing story slipped through backend-only. For
every story marked `done`, check the `ui_acceptance` gate:

```bash
python3 - "$(pwd)" <<'PY'
import json, sys, pathlib
state = json.load(open(pathlib.Path(sys.argv[1]) / ".synaptory/.orchestrator/pipeline-state.json"))
gaps = []
for s in state.get("current_stories", []):
    if s.get("state") != "done":
        continue
    dod = s.get("dod") or {}
    if dod.get("ui_required") and dod.get("checks", {}).get("ui_acceptance", {}).get("passed") is not True:
        gaps.append(s["id"])
print("UI_GAPS=" + (",".join(gaps) if gaps else "none"))
PY
```

If `UI_GAPS` is not `none`, the sprint is **NOT** 100% — those stories have a
user-facing AC that was never verified through the UI. Do not report the Sprint
Goal as met. Reopen each listed story (dispatch SE `frontend` + QE `browser-qa`,
or have the PO explicitly accept the AC) before the sprint can close. This is
the cross-check that catches the "100% DoD with zero UI" failure mode.

Then evaluate the sprint-level overlay checks (Scrum only, not per-story):

```
━━━ Sprint DoD Overlay ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Auto checks:
  {RUN: python3 story_pipeline.py aggregate_dod "$(pwd)"}
    ☐ No regression across sprint stories — {auto_result}
    ☐ Every UI-bearing story verified through the UI — {UI_GAPS == none}

  Human checks (your input needed):
    ☐ Sprint Goal met?
    ☐ Stakeholder feedback from prior sprint addressed?
    ☐ Documentation updated for changed features?
```

Present options:
```
  Options:
  1. All checks pass — approve sprint
  2. Sprint Goal not fully met — note concerns
  3. Need more work — list what's missing
  4. Chat about this
```

Run the regression auto-check:
```
# Collect all verification commands from this sprint's receipts
# Re-run them to verify no regressions
RECEIPTS_DIR=".synaptory/.orchestrator/receipts"
for receipt in ${RECEIPTS_DIR}/*-qe.json ${RECEIPTS_DIR}/*-se.json; do
  # Extract and re-run verification_commands
done
```

---

## Step 5 — Capture Stakeholder Feedback

After the demo, prompt the user for feedback:

```
  Your feedback on Sprint {N}:
  1. Looks good — no feedback
  2. I have feedback (describe)
  3. Chat about specific stories
```

If the user provides feedback:
- Record to `.synaptory/.orchestrator/sprint-feedback.md`
- This becomes input for PO during next Sprint Planning

---

## Step 5.5 — Design: Next-Sprint Prototype (signal-gated)

**Trigger:** At least one of the following is true after Step 5:
- Stakeholder feedback mentions visual changes, new screens, layout issues, or UX uncertainty (see Design Grooming Protocol signal keywords)
- PO identifies proposed next-sprint stories with UI scope during Step 6 planning
- Sprint Goal for next sprint is primarily UI/UX focused

**Skip if:** No UI signals in feedback and next-sprint stories are backend-only. Proceed directly to Step 6.

Follow the Design Grooming Protocol at `.synaptory/.protocols/design-grooming.md`.

**Capture the canonical context inputs before prompting Claude Design:**
```
PREVIEW_URL=<the preview URL surfaced by Step 1 above — echoed back to the user here>
FEEDBACK_FILE=.synaptory/.orchestrator/sprint-feedback.md
STORY_CONTEXT=<relevant proposed next-sprint story descriptions from tracker>
CONNECTED_REPO=<grep 'connected_repo:' .synaptory/design/inception-preview.md | awk '{print $2}'>
```

**Action:**
1. Identify the top 1-3 proposed next-sprint UI stories or feedback themes
2. Prompt the team to generate Claude Design prototypes. Present the paste-ready context block to the team so they feed Claude Design correctly:
   ```
   📋 Paste into Claude Design:

     1. Web capture URL:   {PREVIEW_URL}
     2. Feedback excerpt:  {quoted lines from FEEDBACK_FILE matching the UI signal}
     3. Story context:     {full text of affected stories}
     4. Design system:     Already connected via {CONNECTED_REPO} — no re-feeding needed.
                           (If null: paste inception-preview.md → Design System Seed section.)

     Instruction: "Redesign [affected surface] based on the feedback above.
     Keep the existing design system. Generate 2-3 variants for side-by-side
     comparison."
   ```
3. Share prototype URL(s) with stakeholders for **async review before Sprint Planning** — set permission to view-only + comments (see protocol § Client Collaboration)
4. **Client comment collection loop.** Once the client reviews (async), prompt the user:
   ```
   Client feedback on the Sprint {N+1} prototype?
     1. Client approved variant {X} — capture that as the design of record
     2. Client left inline comments — paste them here
     3. No feedback yet — revisit later
     4. Chat about specific comments
   ```
   When comments arrive, append them to `.synaptory/.orchestrator/sprint-feedback.md` under a `## Design Comments` subsection AND into `sprint-{N+1}-preview.md` under `Client feedback:`. Refinement Mode picks both up.

**Output:** `.synaptory/design/sprint-{N+1}-preview.md` — prototype URLs, key screens, and `Client feedback:` (filled in once the client responds). See protocol for file format.

**PO uses this file in Step 6** (backlog update) to write stories with design-anchored acceptance criteria, and **again in Sprint Planning Step 2** to attach `design_ref` fields.

Print:
```
🎨 Design prototypes generated for Sprint {N+1}.
   Preview file:  .synaptory/design/sprint-{N+1}-preview.md
   Prototype URL: {shareable URL from Claude Design}
   Next:          Share the URL with stakeholders (view-only + comments).
                  Comments will feed Sprint {N+1} Planning via Refinement Mode.
```

---

## Step 6 — PO Updates Product Backlog

If stakeholder feedback was captured, dispatch PO to process it:

```
PO_BACKEND=$(python3 "${CLAUDE_PLUGIN_ROOT}/skills/_shared/scripts/backend/backend_config.py" "$(pwd)" "project-owner")
```

> **MANDATORY: Spawn this agent via the `Agent()` tool — do not process the feedback inline.** Inline execution skips the SubagentStop hook, so no receipt is written and the work never reaches `/cost` or `/quality`. The dispatch must look like `Agent(subagent_type="synaptory:project-owner", description="PO backlog update from sprint feedback", prompt=<self-contained prompt per the wrapper>)` — see `${CLAUDE_PLUGIN_ROOT}/skills/_shared/backends/${PO_BACKEND}.md`. The PO writes its receipt to `.synaptory/.orchestrator/receipts/SPRINT-{N}-po-feedback.json` as its last action.

**PO prompt context:**
- Stakeholder feedback from Step 5
- Current Product Backlog
- Completed sprint stories (for context)

**PO actions:**
- Create new stories from feedback
- Re-prioritize existing stories based on feedback
- Update story acceptance criteria if feedback suggests changes

---

## Step 7 — TW Report Gate (Mandatory, FSM-enforced)

**The FSM rejects `SPRINT_REVIEW → SPRINT_RETRO` unless a TW receipt exists.**

`scrum_state_machine.transition()` checks for `SPRINT-{N}-tw.json` under `.synaptory/.orchestrator/receipts/` before allowing the transition. The receipt is the canonical "TW dispatched and finished" audit signal; without it the sprint has no documented demo/metrics/feedback and downstream ceremonies have no context to feed forward. This used to be a markdown-only suggestion that the orchestrator could (and sometimes did) skip past — it is now a hard guard.

If you reach Step 7 and the receipt is missing:

```
→ Re-run Step 2 (Generate Sprint Reports). TW writes its receipt to
   .synaptory/.orchestrator/receipts/SPRINT-{N}-tw.json as its last action.
```

The receipt is verified before transition. Once it's on disk, Step 8 proceeds cleanly.

**Escape hatch (rare):** pass `force=True` to the state-machine transition (CLI: `--force`) to bypass the guard. Reserve this for operator recovery — running it skips the audit-trail guarantee that Sprint Review actually produced documentation.

---

## Step 8 — Transition to Next Ceremony

**Note: `SPRINT_REVIEW → SPRINT_CLOSE` direct transition is no longer valid.**
The state machine requires passing through `SPRINT_RETRO`. Even when retro content is
skipped (clean sprint / Sprint 1), the SPRINT_RETRO node must be visited so the
process-log is written and the retro ceremony's feed-forward file is created.

Check retro eligibility:
```
RETRO=$(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/scrum_state_machine.py" retro_check "$(pwd)")
```

Always transition to SPRINT_RETRO:
```
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/scrum_state_machine.py" transition "$(pwd)" SPRINT_RETRO
→ Load ceremonies/sprint-retro.md
```

The retro ceremony itself decides whether to run full retrospective content or a
lightweight pass (for Sprint 1 or clean sprints). The node is never skipped.

Print:
```
{IF RETRO RECOMMENDED}: "Proceeding to Sprint Retrospective..."
{IF SPRINT 1 / CLEAN}:  "Retro content skipped (Sprint 1 / clean sprint). Running lightweight retro pass..."
```
