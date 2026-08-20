# Sprint Mode — Scrum Ceremony Dispatcher

Execute a Scrum sprint cycle. Routes to the correct ceremony based on the current lifecycle state. Each ceremony handles its own transitions.

## Trigger Signals

"build sprint", "sprint N", "next sprint", "continue sprint", "resume sprint", "run sprint", "start sprint", "sprint planning", "sprint review", "sprint close"

## Prerequisites

1. Project must be initialized with Scrum build mode:
   ```
   STATE=$(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/scrum_state_machine.py" read "$(pwd)" 2>&1)
   ```
   If this fails with "v1 state detected" or no state file exists:
   - If `.synaptory.yaml` exists with `build_mode: scrum`: run `scrum_state_machine.py init`
   - If no config: run Init mode first (`/synaptory init`)

2. Read current lifecycle state:
   ```
   LIFECYCLE_STATE=<extract lifecycle_state from STATE>
   ```

## Ceremony Routing

Route to the correct ceremony based on `lifecycle_state`:

### `INCEPTION`

Inception is not yet complete. Inception mode handles Sprint 0 (Wave 4).
For now, transition to SPRINT_PLANNING:
```
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/scrum_state_machine.py" transition "$(pwd)" SPRINT_PLANNING
```
Then fall through to SPRINT_PLANNING below.

### `SPRINT_PLANNING`

Load and follow the Sprint Planning ceremony:
```
Read("${CLAUDE_PLUGIN_ROOT}/skills/synaptory/ceremonies/sprint-planning.md")
```

Sprint Planning handles:
- PO backlog refinement (adaptive intensity)
- SA architecture auto-detect (conditional)
- Story selection with velocity constraint
- Sprint Goal confirmation
- QE test specification
- Transition to SPRINT_EXECUTION via `start_sprint`

### `SPRINT_EXECUTION`

Execute the story pipeline for all sprint stories. Fetch and follow the story-pipeline protocol via the CLI (the protocol body is CP-delivered per ADR-016; it does not exist on local disk):
```
Bash("synaptory skills get protocols/story-pipeline")
```

**The iteration loop is code-driven (epic #75).** At the TOP of every iteration — before deciding anything yourself — run:
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/scrum_state_machine.py" next_action "$(pwd)"
```
and execute the returned `action`. Do not pick the next story from memory; the JSON is authoritative. Repeat (dispatch → transition → `next_action` again) until it returns `sprint_complete`, `await_acceptance`, or `all_blocked`.

> ### ⚡ Performance — the two biggest wall-clock levers (read before dispatching)
> Serial-by-default execution and per-story re-verification are the measured causes of slow sprints (a 9-story run took ~7h serial when ~3h was achievable). Both are already permitted by the framework — you must actively USE them:
>
> 1. **Parallelize independent stories — do NOT dispatch one-at-a-time by reflex.** If `next_action` returns `parallel.eligible: true`, dispatch the whole batch per `backends/claude.md` § Parallel story dispatch. If it does NOT (because `parallelism.story_parallelism` is unset — it is **off by default**), that is a config gap, not a signal to go serial: independent stories (empty `depends_on`, disjoint `file_scope`) SHOULD run concurrently. Enable it — set `parallelism.story_parallelism: enabled` (and optionally `max_concurrent_subagents`) at Sprint Planning when the backlog has independent work — or dispatch independent stories as concurrent background `Agent()` calls in worktrees yourself. A dependency chain (A→B→C) is the only reason to serialize.
> 2. **Verify ONCE, at the right layer** (`protocols/verification-discipline.md` § Trust Chain). Do NOT put the full test suite in every SE/QE/CR prompt. Scope commands per role per the backend wrapper's Evidence Contract: **SE = fast gates only** (typecheck + story-scoped tests + one build, NO full suite, NO live browser/runtime — that's QE's job); **QE = the one full pass** (full suite + runtime + browser-qa + integration smoke); **CR = read-only** (the hook replay is authoritative — do not re-run the suite "to be safe"). Full cross-sprint regression runs once, at Sprint Close.
> 3. **Cut retries at the source.** In every SE dispatch, enumerate each AC's negative/edge cases (already in `test-specification-sprint-{N}.md`) as REQUIRED tests. The measured retries come from ACs whose edge case was stated but left untested (stale-write, load-failure, illegal-transition, missing-field states).

**Every `next_action` output carries a `dod` block** — `{tier, tier_source, active_checks}` (#134 GAP-11). State the tier and its active checks in every dispatch prompt (the Evidence Contract section of the backend wrapper) so agents know the gate expectations up front. `tier_source: "computed"` means the tier was never announced at Sprint Planning — announce and persist it (`set_dod_tier`) at the next opportunity.

**Interrupted sessions.** On ANY resumed session (new conversation, post-crash, post-compact), run `next_action` FIRST — never replan from memory; the board state and receipts on disk are authoritative. Honor a `resume_candidate: true` before any re-dispatch: partial work already exists and restarting from scratch throws it away.

| `action` | What you do |
|---|---|
| `dispatch_se` | Dispatch the software-engineer for `story_id` (queued → run `transition_story … in_progress` first; in_progress → resume). If `receipt_present: true`, skip the dispatch and run `transition_story … {transition_to}` instead — the agent already finished, only the transition is missing. If the output carries `parallel.eligible: true`, dispatch the whole batch via the worktree procedure in `skills/_shared/backends/claude.md` § Parallel story dispatch (`batch[0]` is this serial action; create worktrees → dispatch concurrently → merge serially as receipts verify). |
| *(tier 0 — resume)* `dispatch_*` with `resume_candidate: true` | A prior run was interrupted mid-work (`resume_evidence` shows a dirty/ahead worktree or dirty workspace). Re-dispatch the SAME role with the RESUME preamble (see backend wrapper § Recovery ladder tier 0): continue, don't restart, don't revert. **Record NO retry** — the ladder starts at tier 1 only if the resume also fails. |
| `dispatch_qe` | Dispatch the quality-engineer for `story_id`. Same `receipt_present` rule. QE always runs post-merge on the main workspace, never in a story worktree. |
| `dispatch_cr` | Dispatch the code-reviewer for `story_id`. |
| `promote_story` | Run `scrum_state_machine.py transition_story "$(pwd)" {story_id} done` — DoD auto-fires on the transition (fail-closed to `blocked` if gates unsatisfied). |
| `request_acceptance` | Run `scrum_state_machine.py request_acceptance "$(pwd)" {story_id}` (per-story acceptance is on). |
| `recover_blocked` | Follow `recovery.tier`: **`gate_remediation`** → the story failed a conditional DoD gate; unblock and dispatch `recovery.role` (compliance-engineer for `no_critical_findings`; QE in runtime/browser-qa/integration mode for the others) following `recovery.reason` verbatim — do **NOT** re-run SE, it can't clear the gate. `retry_same_prompt` → re-dispatch `recovery.role` unchanged. `retry_augmented_prompt` → re-dispatch with the failure reason cited in the prompt. **For both retry tiers, record the retry FIRST** (it drives the ladder same-prompt → augmented → block): `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/story_pipeline.py" record_retry "$(pwd)" {story_id} {recovery.role} "{failure summary}"` (the JSON it prints echoes the updated `recovery` tier), then `... unblock "$(pwd)" {story_id}` and re-dispatch. |
| `await_acceptance` | STOP the loop — stories await the PO walk at Sprint Review. Human gate; never auto-continue past it. |
| `sprint_complete` | All stories terminal — proceed to `complete_sprint` below. |
| `all_blocked` | STOP — recovery ladder exhausted; summarize the blocked stories and ask the user. |
| `block_story` | A verification loop exhausted the retry ladder on a failed receipt. Run `transition_story "$(pwd)" {story_id} blocked --reason "{recovery.reason}"`, then continue the loop (`next_action` again) — the story is parked; move on to the next work. |

**Verification loops (strict mode + `resilience.verification_loops`, default `auto`).** `auto` (and an absent key) couples to `loop_continuation` — since continuation is on by default, verification loops are on by default too in strict mode (#179 E2); set `verification_loops: disabled` to opt out. When on, a `dispatch_se`/`dispatch_qe`/`dispatch_cr` action may carry a `recovery` object even though `receipt_present: true` — the stage's receipt was present but either its verification FAILED, or (at the mature/release DoD tiers only, #179 E6a) it carries **unverifiable** evidence (plain-string commands = intent, not proof; `recovery.verdict` is `failed` or `unverified` and the reason says which). Re-run the stage rather than advancing on it. Treat it like `recover_blocked`'s retry tiers: record the retry first (`story_pipeline.py record_retry "$(pwd)" {story_id} {recovery.role} "{failure summary}"`), then re-dispatch `recovery.role` — unchanged for `retry_same_prompt`, with the failure reason cited for `retry_augmented_prompt`. When the ladder exhausts, `next_action` returns `block_story` instead (it does NOT advance a known-failed receipt into a wasted CR/DoD cycle).

The stage semantics behind those actions:
1. **SE implements** → story transitions: queued → in_progress → testing
2. **QE tests** → story transitions: testing → reviewing
3. **CR reviews** (adaptive — `next_action` only asks for CR when the intensity tier requires `code_reviewed`) → story transitions: reviewing → done
4. **DoD evaluation** — `transition_story` auto-invokes `evaluate_story_dod` on the `→ done` transition; the result is stored on the story record. To re-run explicitly:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/scrum_state_machine.py" evaluate_dod "$(pwd)" "{STORY_ID}"
   ```

#### Conditional gates — compliance & runtime verification (#31 / #30)

`evaluate_story_dod` returns `compliance_required` and `runtime_required` for each story. These are **true** when the project sets `healthcare.baa_enforced: true` and the story's diff touches a PHI-risk path (logging/audit/URL/patient/FHIR/EHR — or any `healthcare.phi_paths` entry), or when `quality.runtime_verification: required` is set. **Static SE/QE/CR receipts cannot satisfy these gates** — `no_critical_findings` needs a compliance-engineer receipt and `runtime_verified` needs a live-deployment proof — so the story is held out of `done` until you run them. Do NOT mark such a story done on green SE/QE/CR alone.

When a story is PHI-touching (or the project requires runtime verification), insert these **before** `→ done`:

- **QE runtime verification** (`runtime_verified` gate): the quality-engineer deploys the local stack, drives the happy + edge paths, and inspects live logs / URLs / audit for leaks — recording the result as a structured `metrics.runtime_verification` object (`{deployed, logs_inspected, paths_exercised, findings}`) in the QE receipt. See `agents/quality-engineer/phases/10-runtime-verification.md`.
- **Compliance-engineer audit** (`no_critical_findings` gate): dispatch the compliance-engineer for the story (it reads `modes/healthcare.md` for PHI/HIPAA checks). It writes a `{STORY_ID}-ce.json` receipt with `metrics.findings_critical`. Critical findings (PHI in logs/URLs, missing redaction) block the story until remediated.

The gate is fail-closed: if these receipts are absent or unverifiable, `evaluate_story_dod` returns `passed=false` and the story cannot complete.

#### Conditional gates — UI acceptance & integration claims (#44)

`evaluate_story_dod` also returns `ui_required` and `integration_required` per story. These promote two more fail-closed gates, derived from the story's **own** acceptance criteria (no keyword opt-in):

- **`ui_required`** is true when the story's title/ACs describe a user-facing screen (verbs like view/open/render/display/screen/click). When true:
  - Dispatch the SE in **`frontend`** mode (unless `features.frontend: false`) so real UI is built — not just backend services.
  - Dispatch the QE in **`browser-qa`** mode (`agents/quality-engineer/modes/browser-qa.md`): it must actually exercise the screen(s) and record `metrics.ui_verification` (`{rendered: true, routes_tested: N, flows_failed: 0}`) on its receipt.
  - **A green API/unit suite does NOT satisfy `ui_acceptance`.** Do not mark a UI story `done` on backend exit codes alone — without the browser proof (or an explicit PO Accept at Sprint Review) the `reviewing → done` transition fail-closes the story to `blocked`.
- **`integration_required`** is true when the story claims an external service is wired/connected/integrated (Entra SSO, a third-party API, a webhook). When true, every claimed integration must appear in a receipt's `integrations[]` as `status: "live"` backed by an executed smoke `verification_command` (exit_code 0). A **stubbed** integration (placeholder creds) does **not** satisfy the AC — surface it at Sprint Review as "pending credentials" and treat the story as not fully delivering that AC. The `integration_verified` gate blocks `→ done` otherwise. See the receipt-protocol "Integration claims (#44)" section.

Both gates are recoverable: satisfy them (browser-qa proof / live smoke) or get PO acceptance, then `unblock`. As with #31/#30, the orchestrator drives `reviewing → done` through `scrum_state_machine.py transition_story` (never the bare `story_pipeline.py transition`) so these gates actually fire.

After `next_action` returns `sprint_complete` (or `await_acceptance` / `all_blocked` and the human gate is resolved):
```
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/scrum_state_machine.py" complete_sprint "$(pwd)"
```
This transitions to SPRINT_REVIEW.

**Infrastructure stories:** Stories tagged as infrastructure (PE) run in parallel with the app story pipeline. Detect by story title/labels containing: "infra", "CI/CD", "Docker", "Terraform", "monitoring", "pipeline".

### `SPRINT_REVIEW`

Load and follow the Sprint Review ceremony:
```
Read("${CLAUDE_PLUGIN_ROOT}/skills/synaptory/ceremonies/sprint-review.md")
```

Sprint Review handles:
- Preview deployment (auto-detected for web apps)
- TW sprint reports
- Demo organized by Sprint Goal
- Sprint-level DoD overlay (human + auto checks)
- Stakeholder feedback capture
- PO backlog updates from feedback
- Transition to SPRINT_RETRO or SPRINT_CLOSE

### `SPRINT_RETRO`

Load and follow the Sprint Retrospective ceremony:
```
Read("${CLAUDE_PLUGIN_ROOT}/skills/synaptory/ceremonies/sprint-retro.md")
```

Sprint Retro handles:
- Data collection (git, receipts, velocity, cycle time)
- Analysis (went well vs needs improvement)
- Process suggestions (concrete, actionable)
- Apply accepted improvements to config
- Feed-forward to next Sprint Planning
- Transition to SPRINT_CLOSE

### `SPRINT_CLOSE`

Load and follow the Sprint Close ceremony:
```
Read("${CLAUDE_PLUGIN_ROOT}/skills/synaptory/ceremonies/sprint-close.md")
```

Sprint Close handles:
- Carry-over policy for incomplete stories
- Sprint metrics summary
- Next decision: Continue / Release / Stop
- Transition to SPRINT_PLANNING (loop) or RELEASE

### `RELEASE`

Release mode (Wave 4). Full regression, security audit, production infrastructure, documentation.

### `COMPLETE`

Project lifecycle is finished. Report final status.

---

## Git Safety Rules (MANDATORY)

These rules apply to all synaptory agents, Claude Code, and automated tooling:

1. **NEVER commit or push to shared branches** (`dev`, `qa`, `uat`, `main`, `prod`, `staging`, `release`). All work MUST happen on feature branches.
2. **NEVER create commits without explicit user approval.** Always show the diff and ask before committing.
3. **NEVER push to any remote branch without explicit user approval.** Always ask before running `git push`.
4. **NEVER create or merge pull requests without explicit user approval.**
5. **NEVER run destructive git operations** (`git push --force`, `git reset --hard`, `git clean -f`, `git checkout .`).
6. **NEVER run database migrations against shared environments** (dev, qa, uat, prod). Migrations can only be run locally.

If the user says "just do it" or "go ahead", that applies to the current code change only — NOT to committing, pushing, or merging.

---

## Sprint Number Detection

When the user says "sprint N" or "build sprint 3":

1. Extract the sprint number from the request
2. Compare with `current_sprint` in state:
   - If N == current_sprint: resume the current sprint
   - If N == current_sprint + 1: start next sprint (must be in SPRINT_PLANNING)
   - If N > current_sprint + 1: block — "Cannot skip sprints. Current sprint is {current_sprint}."
   - If N < current_sprint: block — "Sprint {N} is already completed."

When the user says "next sprint" or "continue sprint":
- Use current_sprint + 1 if in SPRINT_CLOSE/SPRINT_PLANNING
- Resume current_sprint if in SPRINT_EXECUTION

---

## Progress Output

Print the ceremony header at the start of each ceremony:

```
━━━ Sprint {N} ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Stage: {CEREMONY_NAME}
  Goal:  {SPRINT_GOAL}
  Stories: {completed}/{total} done
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```
