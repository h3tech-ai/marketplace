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
 next_action output, one per line. This is the set the gate will ENFORCE on
 this story, not the tier's static list: it already includes the conditional
 gates (`ui_acceptance`, `runtime_verified`, `no_critical_findings`,
 `integration_verified`) that the story's own record and `.synaptory.yaml`
 promoted. Paste it whole — a check you do not recognize from the tier is
 still a check you are graded on.}

These checks are NOT YET DETERMINED — they are unmeasured, not inactive:
{DOD.UNDETERMINED_CHECKS — paste `dod.undetermined_checks` as
 `- <check> — <why>`, one per line; omit this whole heading only when the
 list is empty. The evidence that promotes them is the evidence you are
 about to write, so a receipt that triggers one is graded on it.}

You are dispatched as **{ROLE_NAME}**, which runs under stage profile
`{PROFILE.STAGE_PROFILE}` and capability profile `{PROFILE.CAPABILITY_PROFILE}`
— paste the `profile` block from the same next_action output. The advance
kernel keys this stage on that pair, not on the role name alone (#402): stamp
the two values verbatim on your receipt and never substitute another pair. A
receipt whose pair disagrees with the stage is refused, and since #473 so is
one that omits the pair when your dispatch contract carried a
`dispatch_envelope` (copy them from there, it is the authoritative source).

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

Since #501 the envelope also carries a **Definition of Done for this story**
stanza, resolved hook-side from the story the dispatch binding names. Both it
and the `dod` block above now run the SAME computation the gate runs
(`story_pipeline.story_gate_promotions`) on the same story, so they agree by
construction and both report the CONDITIONAL gates. They are two paths to one
answer rather than a coarse one and a fine one: the hook resolves nothing when
two stories hold this role at once or during ceremony work, and the
orchestrator's block covers exactly those cases. Keep stating the tier and both
lists in the prompt. Where the two disagree, treat it as a signal that the hook
bound a different story than you dispatched, and re-check before proceeding.

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
- stage_profile / capability_profile: the pair named in the Evidence Contract above,
  copied verbatim — from your `dispatch_envelope` when the dispatch contract carries one.
  REQUIRED whenever it does (#473): omitting either is refused, exactly like a value that
  disagrees with the stage's pair (#402), so there is no cheaper answer than the true one.
  A dispatch that carries no envelope had nothing to hand over, and omitting both there is
  still a warning. Optionally
  accountable_role: "{ROLE_NAME}" — the Platform-facing accountability label riding
  alongside `role`; it must project onto the same pair.
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
| code_reviewer | opus | opus |
| quality_engineer | opus | opus |
| software_engineer | sonnet | sonnet |
| platform_engineer | opus | opus |
| technical_writer | opus | opus |

**Controlled mode** surfaces decisions and enforces human gates for the four strategic roles (PO, SA, CE, RA) — it does not upgrade any role's tier, because no role's two columns differ. Every `opus` row outside the strategic four (CR, QE, PE, TW) is therefore a routing choice made for its own recorded reason, not a controlled-mode upgrade. `software_engineer` is the one role left on `sonnet`, and that is load-bearing rather than leftover — see the two paragraphs below.

**Why the provers are not on the SE tier (#434 for CR, #483 for QE).** SE, QE and CR all resolved to `sonnet` until #434, so every verification the pipeline performed was same-model verification — a property nobody chose, just what this table happened to say. That matters in proportion to how much a check rests on judgment: `tests_pass` is decided by the machine and is indifferent to weights, while "read this diff and decide whether the logic is right" is decided almost entirely by them, and shared weights mean shared blind spots plus a familiarity effect where code the model would itself have written reads as correct even from a clean context. CR is the judgment-heaviest prover in the pipeline, so it moved first.

**Why QE moved too (#483).** Both halves of the pipeline's verification now sit off the producer's tier. Two things changed after #434 chose to scope itself to CR:

- **#406 made QE's work rest on judgment rather than on an exit code.** The authored acceptance cases sealed at COMMIT are the primary `tests_pass` evidence, and QE must report an outcome *per case id*. Deciding that a case is `blocked` or `not-applicable`, with a reason the gate accepts, is a judgment call that decides whether the story's gate passes. A green suite exit code no longer clears the check on its own. That is #434's own criterion — the layer matters in proportion to how much the check rests on judgment — applied to the role #434 left behind.
- **#519 established that the tier is the thing that actually binds.** The `Agent()` `model` parameter accepts only the four tier aliases, so moving a role between tiers changes which weights run, while re-pinning a tier does not. It also measured that `opus` resolves to `claude-opus-5`, the same generation as `claude-sonnet-5`, so [#441](https://github.com/h3tech-ai/synaptory-v1/issues/441)'s worry that the premium tier is a generation behind does not apply to the running system: moving QE to `opus` is not moving it to older weights.

What this does **not** establish: nothing here verifies that a QE dispatch ran on the tier this table names, or that its receipt's `model` string is true. The pin drift that used to sit here — both provers recording `claude-opus-5` against a `claude-opus-4-8` pin, so [`model_pin_check.py`](model_pin_check.py) refused their **honest** receipts — was closed by [#441](https://github.com/h3tech-ai/synaptory-v1/issues/441), which rolled the `opus` pin to the id every measured dispatch on that tier actually ran. Read the pinning paragraph below before quoting any of this to an auditor: the checker no longer refuses an honest receipt, and it still cannot refuse a dishonest one.

Read the scope of that honestly: this is **partial decorrelation inside one runtime family**. Different weights, different scale, different blind spots — not independent runtimes, not an independent vendor, and not a basis for claiming independently verified work to an auditor. Role-level runtime choice is pilot 1 territory: `select_runtime` / `build_envelope` exist as component shapes but have no production caller on the dispatch path (#396 finding 1), so nothing in this table can deliver runtime independence today.

Do NOT "simplify" CR or QE back onto the SE tier. Producer and prover sharing exact weights is the thing these two rows exist to prevent, and the cost is two premium dispatches per work unit rather than one (see the #434 PR for the measured per-dispatch delta). #518 added the `claude-opus-5` row to `_DEFAULT_PRICING`, so those dispatches no longer price at zero; before it landed, a dashboard reported both provers as free, which is the shape of a zero that meant "not measured" and read as "measured, and it was zero".

**Why PE and TW moved off `sonnet`, and what that reason is not.** The three executor rows were never a measured choice; they were the roster's cost default, and SE is the only one of the three that decorrelation has anything to say about. Neither PE nor TW produces or proves a work unit, so moving them does not touch the property the CR and QE rows exist to protect: SE remains alone on `sonnet`, and both provers still resolve to weights the producer does not. Read the reason as exactly what it is — a maintainer's routing decision to spend the premium tier on these two roles. No benchmark in this repository compares the tiers, so this is not a claim that their output improves, and it must not be quoted as one. What it does change is cost, and not by a fixed amount: PE fires at Inception and on every infra work unit, TW at every Sprint Review and at Release, so the delta scales with how infra-heavy and how many sprints a run is. Both now price at the premium rate in `_DEFAULT_PRICING` (#518), so the increase shows up on the Cost page rather than hiding as a zero.

The one row that must not follow them is `software_engineer`. Putting the producer on the provers' tier collapses CR and QE back into same-model verification just as surely as moving the provers down would, and [test_model_tier_decorrelation.py](../../../tests/lib/test_model_tier_decorrelation.py) fails on it from either direction.

The model tier is INDEPENDENT of backend dispatch — it only applies within the Claude backend.

**Tier → model ID pinning (HC0-F2), and what it actually binds (#519).** The pins live in [model-pins.json](model-pins.json): `opus → claude-opus-5`, `sonnet → claude-sonnet-5`, `haiku → claude-haiku-4-5`. Every pinned tier must be `baa_covered: true`, because `healthcare.baa_enforced` projects refuse an uncovered backend. Update `model-pins.json` when rolling forward, so every change is a reviewable diff rather than silent drift, and record the decision per tier in that tier's `decision` block — including the decision to KEEP a pin, so an inherited pin is distinguishable from a reviewed one.

**A tier alias is a routing label, not a capability guarantee (#441).** `opus`, `sonnet` and `haiku` name which pinned weights a role is routed to, and `cost_tier` names what they cost — a published fact. Neither asserts an ordering between the tiers on any measured axis. Nothing in this repository benchmarks one tier against another, and no such benchmark is quoted here or in the pin file. So "premium" must be read as "the more expensive tier", never as "the stronger model": the reason a role sits on a tier is the reason written in that tier's `notes`, and for CR and QE that reason is **decorrelation from the producer's weights**, which needs the tiers to differ and does not need either to be better. #441 opened on the worry that the premium tier was pinned a generation behind the standard one (`claude-4.8` against `claude-5`); #519's measurement removed the worry rather than answering it, because the pin was not what ran, and the roll to `claude-opus-5` puts both tiers on the same generation in the file as well as in the running system.

**The pin does not bind on this dispatch path, and you must not describe it as if it does.** This paragraph used to carry the instruction "Do not pass alias strings to `Agent()`", followed by "pass the resolved exact ID". That instruction is impossible to follow. #519 measured the host: the `Agent()` tool's `model` parameter accepts **only** `sonnet | opus | haiku | fable`, and an exact ID is rejected outright.

```
InputValidationError: Invalid option: expected one of "sonnet"|"opus"|"haiku"|"fable"
```

So the tier **alias** is the only value Step 3 can carry, and the alias is resolved by Anthropic's own aliasing to whatever that tier currently points at, not by this file. Measured on Claude Code 2.1.236 on 2026-09-03: `opus` runs `claude-opus-5` and `sonnet` runs `claude-sonnet-5`. Every pin now agrees with its measured alias resolution — but read *why* that is true, because the two tiers got there differently and the difference is the whole lesson. `sonnet` agreed **by coincidence** from the start, which is why nothing surfaced for the four roles on that tier until #434 moved CR to `opus` and #409's receipts started naming a model the pin file did not contain. `opus` agrees only because #441 rolled it there **after** the observation. Agreement is a snapshot, not a mechanism: the next alias move is a server-side decision that can happen on any day and nothing here will notice it until a receipt names the new id.

Read the consequence honestly rather than restating the intent:

- `model-pins.json` is a **declaration of intent shipped in the AI-BOM** (`ai_bom.model_pins` in `build-metadata.json`). It records the IDs H3Tech means each tier to use. It is **not** evidence of the IDs that ran.
- Nothing in this plugin, the hook chain, or the control plane compares an intended pin against an observed dispatch. `receipt_validator.validate_receipt` requires `model` to be a non-empty string whose family does not contradict `backend`, and nothing more.
- The `model` a receipt reports is **self-attested** by the dispatched subagent, from the `model: (the model you are running on)` line in the Output block above. The authoritative value lives in the host's own transcript and `modelUsage` accounting, which this plugin never reads. So a receipt can name a pinned model it did not run on and every check passes.
- Do **not** tell an auditor, or a regulated customer, that a dispatch is reproducible against the pinned ID. What is reproducible is the tier routing. Which weights that tier resolved to on the day is not recorded anywhere the plugin controls.

**What does bind an exact ID**, measured in the same run: a subagent's own `model:` frontmatter. `model: claude-sonnet-4-6` on a plugin subagent ran on exactly that, overriding the session model. Step 3 now dispatches the installed role so the host does consult that role definition, but it also passes the explicit tier alias from Step 2; that explicit routing input remains the product path's model decision. Switching to exact-ID frontmatter would therefore require a deliberate routing change: remove the explicit alias, keep `agents/<role>/agent.md` and `agents/<role>/SKILL.md` aligned (both register the same subagent identifier), and accept that the pin must be rolled manually or the roster silently freezes on ageing weights. Since #441 the pin names the currently observed alias resolution, so such a change would not move weights today; it would change how future alias moves are adopted. Do not make that policy change as a side effect of another fix.

**Verifying, in the meantime.** [`model_pin_check.py`](model_pin_check.py) refuses a receipt whose `model` is named by no tier in the pin file, which is the drift case #409 hit. Run it over a real receipts directory:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/_shared/backends/model_pin_check.py" \
  .synaptory/.orchestrator/receipts/*.json
```

It catches drift and mis-tiering. It cannot catch a forged model string, for the reason above. Treat its silence as "no drift detected", never as "these weights are proven".

Since #441 there is **no known outstanding drift** on any routed tier: each pin names the id its alias was measured resolving to. So a finding from this checker is now a NEW observation rather than the old one being re-reported, and the response is to re-measure the alias and record a fresh `decision` block in `model-pins.json` — not to widen the accepted set until the finding goes away.

### Step 3 — Dispatch via Agent()

```
Agent(
  prompt = composed_prompt,
  subagent_type = f"synaptory:{ROLE_NAME}",
  model = model,
  description = "{ROLE_NAME}: {STORY_ID} — {STORY_TITLE}"
)
```

`subagent_type` is the installed Synaptory role, not `general-purpose`. The
`synaptory:` namespace is the lifecycle identity consumed by SubagentStart and
SubagentStop: it binds the dispatch to its receipt contract and opens both the
control-plane subagent span and the local OTLP span. A generic worker has no
such contract and is deliberately excluded from those hooks.

`model` here is the **tier alias** from the Step 2 table (`opus` / `sonnet` / `haiku`). That is the only thing the parameter accepts, and an exact pinned ID is rejected. See the pinning paragraph above for what that means for the AI-BOM claim, and do not "improve" this call by substituting a pinned ID: it will fail with `InputValidationError`.

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
