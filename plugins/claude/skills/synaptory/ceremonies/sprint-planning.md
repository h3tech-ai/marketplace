# Sprint Planning Ceremony

> **Lifecycle state:** `SPRINT_PLANNING`
> **Participants:** PO, SA (if triggered), QE, Orchestrator
> **Output:** Sprint Backlog with goal, stories in `queued` state, QE test spec

## Adaptive Intensity

Determine planning intensity before starting:

```
STATE=$(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/scrum_state_machine.py" read "$(pwd)")
SPRINT_NUM=<extract current_sprint from STATE>
SPRINTS_COMPLETED=<extract sprints_completed array from STATE>
```

**Lightweight** (stable backlog, no new feedback from prior Sprint Review):
- PO confirms pre-refined stories
- Skip SA (unless triggers found)
- Orchestrator presents Sprint Backlog for approval
- QE generates test spec

**Full** (new feedback, scope changes, retro flagged issues):
- PO performs deep refinement
- SA architecture review if triggers found
- New stories created from prior Sprint Review feedback
- Full Sprint Backlog negotiation

**How to decide:**
- Sprint 1: always **Full** (first sprint, nothing pre-refined)
- Sprint 2+: check if prior sprint had:
  - Stakeholder feedback captured (→ Full)
  - Retro improvements applied (→ Full)
  - Carry-over stories (→ Full)
  - None of the above (→ Lightweight)

---

## Step 1 — PO Reads Context

The PO agent needs these inputs:
- Prior sprint's stakeholder feedback (from `.synaptory/.orchestrator/sprint-feedback.md` if exists)
- Retro insights (from `.synaptory/.orchestrator/process-log.md` if exists)
- Cross-loop signals harvested by past retros (issue #179 E3)
- Current Product Backlog state (from tracker)

```
TRACKER_CLI="python3 ${CLAUDE_PLUGIN_ROOT}/skills/_shared/scripts/tracker/tracker_cli.py --project-dir $(pwd)"

# Get current backlog
${TRACKER_CLI} get-backlog

# Get stories not yet assigned to a sprint
${TRACKER_CLI} query --status TO_DO

# Read machine-readable signals from prior loops (recurring failure classes,
# DoD gate hotspots, blocked stories, evidence-replay mismatches). Feed any
# hits into the PO prompt: a recurring failure or gate hotspot should shape
# this sprint's stories (tooling story, receipt-contract fix, AC clarity).
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/signals.py" "$(pwd)" list
```

---

## Step 2 — PO Refines Stories

Dispatch the Project Owner agent to refine stories for this sprint.

```
PO_BACKEND=$(python3 "${CLAUDE_PLUGIN_ROOT}/skills/_shared/scripts/backend/backend_config.py" "$(pwd)" "project-owner")
```

> **MANDATORY: Spawn this agent via the `Agent()` tool — do not execute the refinement inline.** Inline execution skips the SubagentStop hook, so no receipt is written and the work never reaches `/cost` or `/quality`. The dispatch must look like `Agent(subagent_type="synaptory:project-owner", description="PO sprint refinement", prompt=<self-contained prompt per the wrapper>)` — see `${CLAUDE_PLUGIN_ROOT}/skills/_shared/backends/${PO_BACKEND}.md` for the full prompt template. The PO writes its receipt to `.synaptory/.orchestrator/receipts/SPRINT-{N}-po.json` as its last action.

Read the PO backend wrapper at `${CLAUDE_PLUGIN_ROOT}/skills/_shared/backends/${PO_BACKEND}.md` and dispatch.

**PO prompt context:**
- Sprint number: {SPRINT_NUM}
- Prior sprint feedback (if any)
- Retro insights (if any)
- Current backlog (unrefined stories)
- Velocity from prior sprints (for capacity planning)

**PO output:**
- Refined stories with detailed acceptance criteria (Given/When/Then)
- Priority ordering
- Proposed Sprint Goal
- Capacity recommendation based on velocity
- Per-story `kind`, `depends_on`, and (optionally) `file_scope` — see below

**Story classification (#134 GAP-4/GAP-6/GAP-8).** Every refined story carries three extra fields that feed the pipeline:

- **`kind`** — one of `enabler | infra | backend | api | ui | mixed`. **Seed it from the story's tracker labels** (the PO sees them in the backlog); the keyword regex over titles/ACs is a last-resort fallback for unlabelled stories, not the primary source. A non-UI kind (`enabler`, `infra`, `backend`) suppresses the `ui_bearing` heuristic outright — so an enabler story like "define color tokens" is never mis-routed into full browser QA just because an AC says a component *renders* them. `kind` also keys the `quality.verification.by_kind` tier overrides.

  | Tracker label(s) | → `kind` |
  |---|---|
  | `enabler`, `tech-debt`, `spike`, `chore`, `refactor` | `enabler` |
  | `infra`, `infrastructure`, `ci`, `cd`, `devops`, `platform` | `infra` |
  | `backend`, `service`, `db`, `database`, `migration` | `backend` |
  | `api`, `endpoint`, `contract`, `integration` | `api` |
  | `ui`, `frontend`, `design`, `ux`, `screen`, `page` | `ui` |
  | UI label **and** a backend/api label (or no label matches) | `mixed` |

- **`depends_on`** — story ids that must be `done` before this story can be dispatched **at all**. This is a hard gate on both the serial and the parallel path (#304), not a parallel-batch filter: a story with an unmet edge is not dispatched, and an edge naming a story that is not on the board fails closed rather than being ignored. So record real sequencing constraints only (schema before endpoints, endpoint before screen) — an aspirational or stale edge now stalls the sprint. An empty list means independently dispatchable.

- **`file_scope`** — the glob/dir list the story is expected to touch (e.g. `["services/auth/", "libs/shared/errors/"]`). Optional under worktree isolation; **required** for every batch member under `parallelism.isolation: shared`, where batches only form from pairwise-disjoint scopes.

**Lightweight mode:** PO confirms existing stories are ready. Minimal refinement.
**Full mode:** PO creates/updates stories, processes feedback into new backlog items.

**Design-PENDING resolution (signal-gated):** For each story in the proposed sprint backlog tagged `[DESIGN-PENDING]`:

Follow the Design Grooming Protocol at `.synaptory/.protocols/design-grooming.md`.

1. Check if `sprint-{N}-preview.md` exists in `.synaptory/design/` — if yes, generate a scoped handoff bundle from the relevant prototype section
2. If no sprint preview exists, prompt the team to create a targeted prototype in Claude Design for this story (use the story's ACs as the design brief)
3. Store the handoff bundle at `.synaptory/design/{story-id}-design.md` — **this conventional path IS the `design_ref` contract.** The SE resolves it from the story id alone; there is no tracker field to write and nothing to look up. Do not attempt to set `design_ref` via `tracker_cli.py` — no backend can store an arbitrary story field (`local` would persist it, but GitHub/Jira/Teamwork/Linear silently drop unknown fields, so the round-trip is not portable).
4. **Optional, human step:** if the team wants the reference visible in the tracker UI, paste `design_ref: .synaptory/design/{story-id}-design.md` into the ticket description by hand. This is an annotation for humans only — the SE never reads it.
5. Remove `[DESIGN-PENDING]` tag from story
6. **Guard:** A story tagged `[DESIGN-PENDING]` must NOT enter `queued` state until this is resolved. If the team explicitly opts out of Design for a story, remove the tag manually.

**Skip Design resolution for:** backend-only stories, enabler stories, infra stories — those must not carry `[DESIGN-PENDING]` tags (if they do, strip the tag without generating a bundle).

---

## Step 3 — SA Architecture Review (Conditional)

Follow the SA Auto-Detect Protocol at `.synaptory/.protocols/sa-triggers.md`.

For each story in the proposed sprint backlog:
1. Scan story text for architecture trigger signals
2. If ANY triggers found → dispatch SA agent
3. If NO triggers found → skip SA entirely

Also check for periodic architecture health check:
```
Read .synaptory.yaml → architecture.health_check_interval (default: 3)
If SPRINT_NUM % health_check_interval == 0:
  Invoke SA for architecture health check regardless of triggers
```

---

## Step 4 — Team Selects Stories

Present the refined stories to the user for Sprint Backlog confirmation.

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  SPRINT {N} PLANNING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Sprint Goal: {PO_PROPOSED_GOAL}
  Velocity:    {AVG_VELOCITY} stories/sprint (from {N-1} prior sprints)
  Capacity:    {RECOMMENDED_CAPACITY} stories

  Proposed Sprint Backlog:
  ┌──────────┬──────────────────────────────┬──────────┬────────┐
  │ Priority │ Story                        │ Size     │ Design │
  ├──────────┼──────────────────────────────┼──────────┼────────┤
  │ 1        │ US-042: User login with MFA  │ M        │ 🎨     │
  │ 2        │ US-043: Password reset       │ S        │        │
  │ 3        │ US-044: Session management   │ M        │        │
  │ 4        │ INFRA-005: Add Redis cache   │ S        │        │
  └──────────┴──────────────────────────────┴──────────┴────────┘
  {SA_NOTE: "Architecture signals detected — SA reviewed: new entity (sessions table), new integration (Redis)"}
  {🎨 = Design handoff bundle available at .synaptory/design/{story-id}-design.md — open to review prototype before approving}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Options:
  1. Approve Sprint Backlog (Recommended)
  2. Add stories
  3. Remove stories
  4. Adjust Sprint Goal
  5. Chat about this
```

**User can override:** Add or remove stories from the sprint. User has final authority over sprint scope.

---

## Step 5 — Sprint Goal Confirmation

If the user approved in Step 4, the Sprint Goal is confirmed. If they adjusted, use the adjusted goal.

---

## Step 5.5 — Announce the DoD Tier (#134 GAP-11)

The DoD tier must be an **announced planning decision**, not a silent promotion the team discovers at gate time. After the backlog is approved:

1. **Resolve the tier:**
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/story_pipeline.py" dod_tier "$(pwd)"
   ```
   Prints `{tier, tier_source, active_base_checks}`. A `tier_source` of `"computed"` means the tier was never announced — exactly what this step fixes.

2. **Present it to the human:**
   ```
   ━━━ Sprint {N} DoD Tier ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
     This sprint's DoD tier is {TIER}, requiring:
       {active_base_checks, one per line}
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   ```
   **Call out the deltas explicitly** when the tier steps up from the prior sprint:
   - `growing` adds `code_reviewed` → **every story now gets a CR dispatch** (expect a cost/latency bump per story).
   - `mature` adds `coverage_no_decrease` → coverage is measured and must not regress.

3. **Let the human confirm or override** (e.g. hold a Sprint-2 project at `early` for a throwaway prototype, or promote early for regulated work), then **persist the decision**:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/story_pipeline.py" set_dod_tier "$(pwd)" {tier} \
     --decided-by "{PO_EMAIL_OR_ROLE}" [--reason "{why}"] [--sprint {N}]
   ```
   `set_dod_tier` records the decision on the pipeline state (it takes precedence over the computed tier and any `quality.dod_tier` config override) and emits a gate event so the decision is auditable. From here on, every `next_action` output carries the tier in its `dod` block with `tier_source: "planned"`.

---

## Step 6 — QE Test Specification

Dispatch the Quality Engineer agent to generate a test plan for this sprint's stories only.

```
QE_BACKEND=$(python3 "${CLAUDE_PLUGIN_ROOT}/skills/_shared/scripts/backend/backend_config.py" "$(pwd)" "quality-engineer")
```

> **MANDATORY: Spawn this agent via the `Agent()` tool — do not execute the test-spec work inline.** Inline execution skips the SubagentStop hook, so no receipt is written and the work never reaches `/cost` or `/quality`. The dispatch must look like `Agent(subagent_type="synaptory:quality-engineer", description="QE test specification", prompt=<self-contained prompt per the wrapper>)` — see `${CLAUDE_PLUGIN_ROOT}/skills/_shared/backends/${QE_BACKEND}.md`. The QE writes its receipt to `.synaptory/.orchestrator/receipts/SPRINT-{N}-qe-spec.json` as its last action.

**QE prompt context:**
- Sprint stories (IDs, titles, acceptance criteria)
- Existing test infrastructure
- Test framework from `.synaptory.yaml`

**QE output:**
- Test specification for each story's acceptance criteria
- Written to `.synaptory/.orchestrator/test-specification-sprint-{N}.md`

---

## Step 7 — Start Sprint

After approval, initialize the sprint in the state machine:

```
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/scrum_state_machine.py" start_sprint "$(pwd)" {SPRINT_NUM} \
  --goal "{SPRINT_GOAL}" \
  --stories '[
    {"id":"US-042","title":"User login with MFA",
     "acceptance_criteria":["User can open the login screen and enter credentials","On success the dashboard renders"],
     "kind":"ui","labels":["ui","auth"],"depends_on":["US-041"],"file_scope":["frontend/auth/","services/auth/"]},
    {"id":"US-043","title":"Password reset",
     "acceptance_criteria":["..."],"kind":"mixed","labels":["auth"],"depends_on":[],"file_scope":["services/auth/reset/"]},
    {"id":"INFRA-005","title":"Add Redis cache",
     "acceptance_criteria":["..."],"kind":"infra","labels":["infra"],"depends_on":[],"file_scope":["infra/"]},
    ...]'
```

**Always include each story's `acceptance_criteria`** (the same ACs the PO refined and the tracker holds) in the `--stories` JSON. They are snapshotted onto the story record and drive the `ui_acceptance` DoD gate (#44): a story whose ACs describe a user-facing screen is auto-routed to SE `frontend` + QE `browser-qa` and cannot reach `done` without UI verification. Omitting the ACs disables that protection for the story.

**Also carry the Step 2 classification fields** (`kind`, `labels`, `depends_on`, `file_scope`) onto each entry. They are snapshotted at intake: `kind` suppresses the ui_bearing heuristic for `enabler`/`infra`/`backend` stories and selects the `quality.verification.by_kind` tier; `depends_on` gates dispatch outright on both the serial and the parallel path (#304) and, together with `file_scope`, parallel-batch membership (#134 GAP-6/GAP-8). The Kanban `pull_ticket` and the `create_story`/`add_story` CLIs accept the same fields.

This creates story records in `queued` state and transitions the lifecycle to `SPRINT_EXECUTION`.

---

## Step 8 — Generate the Sprint Orientation Pack

Write `.synaptory/.orchestrator/sprint-context.md` — the **single onboarding digest every dispatched agent reads FIRST, instead of re-reading the BRD/ADRs/mockups from scratch**. The Orchestrator writes it inline (no agent dispatch), **regenerated at every Sprint Planning** so it never goes stale across sprints. Target ~2-4k tokens.

Required structure:

```markdown
<!-- generated_at: {ISO-8601} | sprint: {N} | git: {HEAD short SHA} -->
# Sprint {N} Orientation

## Sprint Goal & Stories
{goal, then a table: id | title | kind | depends_on | verification tier}

## Product & Architecture Digest
{condensed BRD/ADR/mockup digest — the decisions and constraints that bind
 THIS sprint's stories; cite the source doc path next to each item so an
 agent can open it when the digest flags a gap}

## Commands
{EXACT install / build / test / dev-server commands for this repo — copy
 from package.json / Makefile / README, verified against the tree, not guessed}

## Module & File Map
{where things live: services, frontend, shared libs, tests — annotated with
 which sprint stories touch which area}

## Conventions
{naming, error handling, test framework/patterns, styling — the rules SE/QE
 receipts get reviewed against}

## What Already Exists
{features/endpoints/screens already built in prior sprints — so agents extend
 instead of re-implementing}
```

Dispatch prompts list this file right after `.synaptory.yaml` (see `skills/_shared/backends/claude.md`); agents open a full source doc only when the digest flags a gap or their story cites it. Keep it honest — a wrong command or stale file map in the pack costs every dispatch in the sprint.

Print:
```
✓ Sprint {N} started — {M} stories queued
  Goal: {SPRINT_GOAL}
  DoD tier: {TIER} ({active checks})
  Orientation pack: .synaptory/.orchestrator/sprint-context.md
  Proceeding to Sprint Execution...
```

The Orchestrator then loads the story-pipeline protocol to begin executing stories.
