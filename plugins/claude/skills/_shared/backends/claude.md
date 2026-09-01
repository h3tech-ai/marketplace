# Claude Backend Wrapper

> **Audience:** Synaptory Orchestrator only. Instructions for dispatching work to Claude subagents.

## Overview

The Claude backend dispatches work via the `Agent()` tool — the built-in Claude Code subagent mechanism. This is the default backend. Execution is always **synchronous**: the Orchestrator blocks until the subagent completes.

## Capabilities

| Capability | Support |
|-----------|---------|
| Sync dispatch | Yes |
| Async dispatch | No — Claude subagents are inline |
| Receipt generation | Self-service — subagent writes its own receipt |
| Prompt format | Full SKILL.md content via Skill() tool loading |
| Protocol injection | Automatic — `synaptory-inject-protocols.sh` fires on SubagentStart |
| Receipt validation | Automatic — `synaptory-verify-receipt.sh` fires on SubagentStop |

## Dispatch Procedure

### Step 1 — Build the Prompt

Construct a self-contained prompt for the `Agent()` call. Follow the **subagent-isolation protocol**: the subagent receives everything it needs to operate independently.

**Prompt structure:**

```
You are the {ROLE_NAME} for the {PROJECT_NAME} project.

## Your Task

{TASK_DESCRIPTION}

Story: {STORY_ID} — {STORY_TITLE}
Acceptance Criteria:
{ACCEPTANCE_CRITERIA}

## Context

Read these files before starting (in parallel):
- .synaptory.yaml
- .synaptory/.orchestrator/sprint-context.md (if exists) — the sprint
  orientation pack. Read this FIRST and INSTEAD of the BRD/ADRs/mockups;
  open a source doc only when the digest flags a gap or your story cites it.
- .synaptory/.orchestrator/settings.md (if exists)
- .synaptory/.orchestrator/codebase-context.md (if exists)
{ADDITIONAL_CONTEXT_FILES}

## Constraints

{SPRINT_CONSTRAINTS}
{PROTECTED_MODULES}
{ARCHITECTURAL_RULES}

## Evidence Contract

This story's DoD tier is {DOD.TIER}. Your work is gated on these checks:
{DOD.ACTIVE_CHECKS — paste the story's `dod.active_checks` list from the
 next_action output, one per line}

Your receipt file is:
.synaptory/.orchestrator/receipts/{STORY_ID}-{role-abbrev}.json

If the dispatch contract carries a `receipt_path`, THAT path wins over the line
above. The kernel resolves it through `advance_kernel.intended_receipts_dir`, so
it already accounts for multi-spec and SPQ layouts, where the flat path above is
not where any gate looks. Ceremony dispatches that are not kernel-driven resolve
it in their own ceremony file instead.

The SubagentStart hook injects the full Execution Envelope — treat it as
binding: it defines exactly which evidence fields your receipt must carry
for each active check.

{ROLE-SCOPED COMMANDS — verify-once discipline; see protocols/verification-discipline.md
 § Trust Chain. The orchestrator puts ONLY this role's commands here; it does NOT
 ask every role to re-run the full suite (redundant re-execution is the measured
 slow-sprint failure mode):
 - SE  → story-scoped FAST gates only: typecheck + the story's own tests + ONE build.
         NOT the full cross-sprint suite; NOT a live browser/runtime pass (QE owns that).
 - QE  → the ONE full pass: full suite + runtime verification (+ browser-qa for UI
         stories) + integration smoke, recorded as structured metrics (below).
 - CR  → READ-ONLY: reason over the diff; do NOT re-run the full suite "to be safe"
         (the SubagentStop hook replay is authoritative).}

## Instructions

Read and follow the full instructions in:
${CLAUDE_PLUGIN_ROOT}/agents/{role-name}/SKILL.md

## Output

Write your receipt to:
.synaptory/.orchestrator/receipts/{STORY_ID}-{role-abbrev}.json
(or the dispatch contract's `receipt_path`, which takes precedence: see above)

The receipt MUST include:
- role: "{ROLE_NAME}" — the FULL role name ("software-engineer" / "quality-engineer" /
  "code-reviewer"), NOT the abbreviation. The DoD evaluator maps checks to receipts by
  full role name; an abbreviated role leaves receipt_by_role empty and every role-sourced
  gate silently returns passed:null (fail-closed) — the #1 cause of "green work, blocked story".
- backend: "claude"
- model: (the model you are running on)
- story_id: "{STORY_ID}"
- verification_commands: a list of EXECUTED PROOF OBJECTS, each
  {"command": "...", "exit_code": <int>, "summary": "..."} — record the REAL exit code of
  each command you actually ran. Plain strings/argv arrays are "intent, not proof" and fail
  the tests_pass/build_succeeds gates. Include ≥1 test-term command (test/vitest/pytest) and
  ≥1 build-term command (build/tsc/typecheck).
- (QE, when runtime/UI/integration gates are active) metrics.runtime_verification
  {deployed:true, logs_inspected:true, paths_exercised:[...], findings:[...]};
  metrics.ui_verification {rendered:true, routes_tested:N, flows_failed:0} for UI stories;
  integrations:[{name, status:"live", ...}] for endpoint/service-wiring stories (only "live"
  backed by a passing smoke in verification_commands satisfies integration_verified).
- (CR) top-level status:"complete" AND story_dod.code_reviewed:true on a PASS verdict.
- All standard receipt fields per the receipt protocol
```

The subagent loads its full SKILL.md via the plugin's skill system. The `synaptory-inject-protocols.sh` hook fires on SubagentStart and injects all shared protocols automatically.

### Step 2 — Determine Model

Apply model tier routing based on engagement mode and role importance:

```
model = get_agent_model(role_name)
```

Model tier mapping:

| Agent Role | Autonomous | Controlled |
|-----------|-----------|-----------|
| project_owner | opus | opus |
| solution_architect | opus | opus |
| compliance_engineer | opus | opus |
| research_advisor | opus | opus |
| software_engineer | sonnet | sonnet |
| quality_engineer | sonnet | sonnet |
| platform_engineer | sonnet | sonnet |
| technical_writer | sonnet | sonnet |
| code_reviewer | sonnet | sonnet |

**Controlled mode** surfaces decisions and enforces human gates for the four strategic roles (PO, SA, CE, RA) — it does not upgrade executor roles (SE, QE, PE, TW, CR), which follow orchestrator direction and don't make autonomous decisions.

The model tier is INDEPENDENT of backend dispatch — it only applies within the Claude backend.

**Tier → model ID pinning (HC0-F2).** Tier aliases above (`opus`, `sonnet`, `haiku`) are resolved to **exact** model IDs via [model-pins.json](model-pins.json). Do not pass alias strings to `Agent()` — pass the resolved exact ID so regulated customers can reproduce behavior audit-to-audit. Current pins: `opus → claude-opus-4-7`, `sonnet → claude-sonnet-4-6`, `haiku → claude-haiku-4-5-20251001`. Update `model-pins.json` when rolling forward — every change is a reviewable diff rather than silent drift.

### Step 3 — Dispatch via Agent()

```
Agent(
  prompt = composed_prompt,
  subagent_type = "general-purpose",
  model = model,
  description = "{ROLE_NAME}: {STORY_ID} — {STORY_TITLE}"
)
```

The `Agent()` call blocks until the subagent completes. The Orchestrator cannot do other work during this time.

**Concurrency cap (BEA3-F1): the Orchestrator must never have more than `.synaptory.yaml → parallelism.max_concurrent_subagents` Agent() calls in flight.** Default is 3. Before dispatching, count active agent_start events in `events.jsonl` for the current chain_id that have not yet seen a matching subagent_stop; if that count is already at the cap, queue the new dispatch behind an existing one.

### Parallel story dispatch (#134 GAP-6/GAP-8)

When `parallelism.story_parallelism: enabled` and `next_action` returns a `parallel` block with `eligible: true`, dispatch the whole batch instead of just the serial action (`batch[0]` is always the serial action, so ignoring the block is always safe). The lifecycle is **orchestrator-managed** — agents never create, merge, or remove worktrees themselves:

1. **Create a worktree per batch member** (worktree isolation — the default):
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/worktree_manager.py" create \
     --project-dir "$(pwd)" --story-id {STORY_ID}
   ```
   Worktree lands at `.synaptory/.worktrees/{STORY_ID}` on branch `synaptory/{STORY_ID}`.
2. **Dispatch the batch concurrently** — one `Agent()` call per member (respecting the concurrency cap). Each SE prompt names its worktree path as the working directory ("Do all code work inside `.synaptory/.worktrees/{STORY_ID}`") and is told **not to commit** — the orchestrator owns the commit (step 3). The **receipt path stays the MAIN repo's receipts dir** (`.synaptory/.orchestrator/receipts/`), and the receipt must carry `workspace_ref: "wt://synaptory/{STORY_ID}"` so verification replays in the right tree.
3. **Commit serially after each SE receipt verifies** — the SE leaves its work uncommitted; the SubagentStop hook has already replayed verification in the (dirty) worktree, so now record one audited commit:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/worktree_manager.py" commit \
     --project-dir "$(pwd)" --story-id {STORY_ID}
   ```
   This stages everything and commits with a `Synaptory-Story: {STORY_ID}` trailer. A clean worktree commits nothing (`committed: false, reason: "clean"`) — not an error.
4. **Merge serially in completion order**:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/worktree_manager.py" merge \
     --project-dir "$(pwd)" --story-id {STORY_ID}
   ```
   `merge` is a `--no-ff` merge into the main branch and removes the worktree on success. Exit 2 = dirty worktree (you skipped step 3 — run `commit` first); exit 3 = **merge conflict, aborted cleanly**: treat it as recovery ladder tier 2 — re-dispatch the SE in the main tree with the prompt citing the conflict ("your branch conflicted with already-merged work in {files}; reapply your change on the current tree"). **Never auto-resolve a conflict.**
5. **QE/CR run post-merge on main** — never inside a worktree. QE's dev-server/browser work is serialized on the merged main workspace.

Under `isolation: shared` (no worktrees), the batch was already scope-checked by `next_action` (pairwise-disjoint `file_scope`); dispatch concurrently in the main tree and remind each SE of its declared scope boundary.

**Correlation IDs and depth guard (H9-F1, BEA4-F1).** The plugin's SubagentStart hook (`synaptory-inject-protocols.sh`) increments a per-project `active-depth` counter in `.synaptory/.orchestrator/active-depth` and blocks with exit 1 when the counter exceeds `MAX_AGENT_DEPTH=3`. SubagentStop (`synaptory-verify-receipt.sh`) decrements it. A chain_id is minted once per project and persisted in `.synaptory/.orchestrator/chain-id`; every event in `events.jsonl` is tagged with this chain_id plus the current depth. The Orchestrator does not need to set env vars — the counter file is the source of truth. If a subagent is observed to call `Agent()` itself, that is a recursive-delegation attempt and the guard will block it at depth 4.

### Step 4 — Verify Receipt

After `Agent()` returns:

1. **Check receipt exists** at the expected path:
   `.synaptory/.orchestrator/receipts/{STORY_ID}-{role-abbrev}.json`

2. **Validate receipt format**:
   - Required fields: `story_id`, `role`, `backend`, `model`, `artifacts`, `verification_commands`
   - `backend` must be `"claude"`
   - `artifacts` list must be non-empty
   - Each artifact file must exist on disk
   - `verification_commands` must contain at least one entry

3. **Hook validation**: The `synaptory-verify-receipt.sh` hook also fires automatically on SubagentStop and performs its own validation. This is a safety net — do not skip Step 4 in reliance on the hook.

### Step 5 — Return Result

Return the receipt JSON as the dispatch result. If the receipt is valid, the story pipeline can advance to the next stage.

## Error Handling

| Scenario | Action |
|----------|--------|
| Agent() hangs or times out | Claude Code platform manages subagent lifecycle — it will terminate stuck subagents |
| Missing receipt after completion | `synaptory-verify-receipt.sh` blocks the pipeline (in Autonomous mode) or warns (in Controlled mode) |
| Invalid receipt (missing fields) | Same hook handles validation; Orchestrator follows the **recovery ladder** below before blocking |
| Subagent fails to load SKILL.md | Check `${CLAUDE_PLUGIN_ROOT}` is set correctly; verify plugin is loaded |

### Recovery ladder (H3-F1)

When a dispatch fails — missing receipt, invalid receipt, or verification commands fail — the Orchestrator must escalate before marking the story blocked. The tier is computed by `story_pipeline.recommend_recovery_action(state, story_id, role_abbrev)`:

0. **Resume (tier 0 — #134 GAP-12)**: when `next_action` says `resume_candidate: true`, a prior run for this story was interrupted mid-work — partial deliverables exist (a dirty/ahead per-story worktree, or a dirty shared workspace; see `resume_evidence`). Re-dispatch the **SAME role** with a RESUME preamble prepended to the prompt:
   ```
   ## RESUME — partial work exists

   A previous run for this story was interrupted. Evidence:
   {resume_evidence summary — dirty files / commits ahead / workspace path}

   Continue from where it left off. Do NOT restart from scratch and do NOT
   revert the existing changes. Finish the remaining work and write the receipt.
   ```
   **Record NO retry for a resume** — it is not a failure, so it must not consume the ladder. The ladder starts at tier 1 only if the resume dispatch also fails. (Shared-workspace evidence is a heuristic: judge whether the dirty files belong to this story before resuming.)
1. **First failure → same-prompt retry**: call `record_retry()`, re-dispatch the identical prompt. Transient issues (tool flake, timeout, missing receipt) usually resolve here.
2. **Second failure → augmented-prompt retry**: call `record_retry()` again, re-dispatch with the prompt suffix:
   ```
   ## Previous attempt failed

   Your last dispatch produced the following failure:
   {verbatim failure reason from the hook output}

   Read the failure carefully and correct the issue. Do not repeat the same receipt.
   ```
3. **Third failure (retry_cap reached) → transition to blocked**: run `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/advance_kernel.py" advance "$(pwd)" {story_id} blocked --reason "{role} repeated failure after {retry_cap} retries: {last_reason}"`. The `blocked` state requires human unblock.

Cap is `.synaptory.yaml → resilience.story_retry_cap` (default 2). Set to 0 to disable retries.

On successful completion, call `story_pipeline.reset_retries(state, story_id, role_abbrev)` so subsequent failures restart from tier 1.

## Verification trust chain

Each stage verifies its **own** slice once; nothing is re-run on trust alone:

- **SE** runs story-scoped fast gates (lint/typecheck on changed files, story-scoped tests) plus **ONE** build — not the full cross-sprint suite.
- **QE** writes and runs NEW AC tests and the tier-appropriate render/runtime checks. **QE never re-runs SE's commands** — the SubagentStop hook replay already re-executed them.
- **The hook replay is authoritative**: `synaptory-verify-receipt.sh` re-runs every receipt's executed `verification_commands` (cached per tree-state via the verification cache). A receipt's claims count only after the replay confirms them — the orchestrator does not need a second agent to "double-check".

## Limitations

- **No async dispatch**: Claude subagents are inline. The Orchestrator blocks during execution.
- **Context budget**: Subagents share the conversation context budget. Very large SKILL.md files or many concurrent subagents can exhaust context.
- **Parallelism bounded by the host**: concurrent `Agent()` dispatch follows the orchestrator-managed worktree procedure above (`worktree_manager.py`); the cap is `parallelism.max_concurrent_subagents` (default 3).

> **Cross-provider note.** plugin-claude is Claude-only. Other host plugins (plugin-cursor, plugin-codex, plugin-antigravity) run agents on their own native model family and ship their own dispatch wrappers.
