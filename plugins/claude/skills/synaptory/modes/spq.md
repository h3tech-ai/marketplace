# SPQ Mode: Lifecycle Dispatcher

Execute the SPQ delivery lifecycle. Routes to the correct state file based on the current lifecycle state. Each state file handles its own transitions.

SPQ is the same delivery machinery as Scrum with one structural addition: **`SYNC`, a cross-workstream integration barrier, sits between execution and demonstration.** Integration must precede the demo. A Checkpoint that demos from one un-integrated workstream branch shows 1/N of the system and defers the integration debt to whoever merges last.

A **Cycle** is scope-defined, not time-boxed. A sprint ends when the clock runs out; a Cycle ends when its admitted Work Units are done. The practical consequence at `SYNC`: a workstream that cannot make the barrier **cuts scope from its Cycle** rather than merging late.

## Trigger Signals

"spq", "open cycle", "next cycle", "run a cycle", "cycle N", "continue cycle", "resume cycle", "sync workstreams", "run the barrier", "declare ready", "checkpoint", "close cycle"

## Prerequisites

1. Project must be initialized with the SPQ build mode. **`spq` is never a default and never inferred.** It requires an explicit `build_mode: spq` in `.synaptory.yaml`.
   ```
   STATE=$(python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" read "$(pwd)" 2>&1)
   ```
   If this fails with "not SPQ format" or no state file exists:
   - If `.synaptory.yaml` exists with `build_mode: spq`: run `spq_state_machine.py init "$(pwd)"` (or the dispatcher's `state_machine.py init_spq`)
   - If no config: run Init mode first (`/synaptory init`)
   - If the state file says `build_mode: scrum` or `kanban`, you are in the wrong lifecycle. Do **not** convert it here; load `modes/sprint.md` or `modes/kanban.md` instead.

2. Read the current lifecycle state:
   ```
   LIFECYCLE_STATE=<extract lifecycle_state from STATE>
   CYCLE_N=<extract current_cycle from STATE>
   ```

3. Confirm the `spq:` config block resolves. The barrier cannot form a quorum from an empty workstream list:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/sync_barrier.py" config "$(pwd)"
   ```
   Check `workstreams[]` is non-empty, exactly one entry carries `shared_owner: true`, and `regression_script` / `journey_script` / `digest_script` all exist on disk. A missing proof script does not fail at `SYNC` gracefully; it fails the whole barrier after the integration slot is already spent.

## Which clone am I in?

This is not optional context. **Half these states only run in one of the two clones**, and getting it wrong produces work nobody can integrate.

| Role | Working copy | Native SPQ identity | States it owns |
|---|---|---|---|
| **Workstream** (×N) | Own clone, exact branch sealed in the manifest (default `cycle/{cycle_id}/ws/{id}`) | `.synaptory/.orchestrator/spq/workstream` pin written by `hydrate_cycle` | `CYCLE_EXECUTION` only, plus `declare_sync_ready` |
| **Integration** (delivery lead) | One clone, manifest `integration_ref` (default `cycle/{cycle_id}/integration`) | Integration lifecycle state owns `cycle_id`; no delivery-workstream pin | `DISCOVERY`, `COMMIT`, `SYNC`, `CHECKPOINT`, `ACCEPTANCE` |

| **Coordination** (release lead) | One clone, release ref `coordination/{ccid}/integration` | `.synaptory/.orchestrator/spq/coordination-cycles/coordination` pin | None of the above — a release is not a lifecycle state. See `spq/coordination.md` |

A **Coordination clone** appears only when several INDEPENDENT child Cycles ship
one release (#305) — Platform on Cycle 12, Contract Mastery on Cycle 3, EHR on
Cycle 5, at their own cadences. It owns no child Cycle and dispatches no Work
Unit; it pins which child increments ship and coordinates the edges between
them. If every scope is in ONE Cycle with several workstreams, you want the
integration seat above instead.

**Hydrate every delivery clone before dispatch.** The native pin is the durable source for SPQ state, receipts and dependency events; it also prevents a shell restart from silently changing lanes. `SYNAPTORY_WORKSTREAM` is an explicit one-command override. Do not set `SYNAPTORY_ACTIVE_SPEC`: that variable selects Scrum/Kanban Multi-Spec state and SPQ deliberately ignores it.

The durable unit is the **workstream** (a product area), never the **Crew** (the people currently assigned to it). Reassigning people must not rename a workstream, move a branch, or edit `spq.workstreams[]`, because the id is already in permanent control-plane history by then.

---

## Receipt ids and stages, by state

Every state file states this for itself. The table is here because the dispatcher is the only file that spans all of them, and a mismatch is invisible until someone opens `/cost`.

| State | Agents dispatched | Receipt ids | Notes |
|---|---|---|---|
| `DISCOVERY` | `ra` → `po` → `sa` → `pe` → `qe` | `DISCOVERY-0-{ra,po,sa,pe,qe}.json` | `ra` only when the problem is genuinely unframed; `pe`/`qe` skipped when brownfield detection finds CI/test framework |
| `COMMIT` | `po`, `qe`, `sa` | `CYCLE-{N}-{po,qe,sa}.json` | `sa` on architecture triggers or the periodic health check |
| `CYCLE_EXECUTION` | `se` → `qe` → `cr`, plus `ce`, `pe` | `{WORK-UNIT-ID}-{se,qe,cr,ce,pe}.json` | The existing convention, unchanged |
| `SYNC` | `qe`, `se` (conflicts), `sa`, `ce` | `SYNC-{N}-{qe,sa,ce}.json`; conflicts reuse `{WORK-UNIT-ID}-se.json`; the barrier verdict is `SYNC-{N}-barrier.json` | |
| `CHECKPOINT` | `tw`, `po` | `CHECKPOINT-{N}-{tw,po}.json` | |
| `ACCEPTANCE` | `qe`, `ce`, `pe`, `tw`, `cr` | `ACCEPTANCE-{N}-{qe,ce,pe,tw,cr}.json` | All at release depth |

Two rules span every state:

- **Non-Work-Unit states use a pseudo Work Unit id.** Receipt `story_id` must match `^[A-Z][A-Z0-9]*-\d+$`, which is why the ids above are `DISCOVERY-0`, `CYCLE-{N}`, `SYNC-{N}`, `CHECKPOINT-{N}`, `ACCEPTANCE-{N}` rather than state names. A receipt written by the orchestrator itself carries `role: "orchestrator"` and `token_usage.stage: "orchestrator"`, and uses a **descriptive filename suffix** instead of a role abbreviation (`SYNC-{N}-barrier.json`), because `orchestrator` has no entry in the contract's role-abbreviation map.
- **`po` dispatches MUST name `token_usage.stage` explicitly**: `pro-discovery` at Discovery, `pro-brd` at Commit and Checkpoint. `project-owner` is deliberately absent from the validator's role-to-stage fallback because it spans three stages, and the prefix fallback omits `pro-` for the same reason. Omit it and the receipt still validates while the PO's cost attribution silently disappears. The other eight roles resolve through the fallback map and are safe to omit.

Model tiers are unchanged from Scrum: Opus for `po`/`sa`/`ce`/`ra`, Sonnet for `se`/`qe`/`pe`/`tw`/`cr`, resolved to exact pinned IDs through `backends/model-pins.json`. SPQ introduces no new tier routing and no new stage vocabulary.

---

## State Routing

Route to the correct state file based on `lifecycle_state`:

### Coordination Cycle (not a lifecycle state)

If the request is about a RELEASE spanning several independent child Cycles —
"pin the release", "release readiness", "drop EHR from the release", "cycle of
cycles" — load `spq/coordination.md` instead of routing on `lifecycle_state`. A
Coordination Cycle has no lifecycle of its own: the child Cycles each run theirs.

### `DISCOVERY`

Load and follow the Discovery state:
```
Read("${CLAUDE_PLUGIN_ROOT}/skills/synaptory/spq/discovery.md")
```

Discovery handles:
- RA problem framing (conditional, only when the problem is genuinely unframed)
- PO baseline: problem, users, BRD, first Work Units
- SA foundation architecture and the shared-component surface
- PE CI/CD bootstrap **plus the three committed barrier proof scripts**
- QE test framework and the increment journey
- Workstream provisioning (N clones, manifest-declared branches, native workstream pins)
- Baseline Gate (human), then transition to COMMIT

### `COMMIT`

Load and follow the Commit state:
```
Read("${CLAUDE_PLUGIN_ROOT}/skills/synaptory/spq/commit.md")
```

Commit handles:
- MethodSignal read from the prior Checkpoint
- Cycle number allocation (the integration state owns N and it is monotonic)
- Tracker cycle binding
- PO Work Unit admission, where each unit clears readiness/DoR before Build
- SA architecture review (conditional)
- Shared-component publish by the `shared_owner` workstream
- DoD tier announcement, QE Cycle test spec
- Cycle scope approval (human), then `open_cycle` → CYCLE_EXECUTION

### `CYCLE_EXECUTION`

Execute the story pipeline for the admitted Work Units. Fetch and follow the story-pipeline protocol via the CLI (the protocol body is CP-delivered per ADR-016; it does not exist on local disk):
```
Bash("synaptory skills get protocols/story-pipeline")
```

**This state runs in a workstream clone, against that workstream's admitted subset.** Pull it from the tracker rather than from a shared file, because there is no shared state between clones:
```bash
TRACKER_CLI="python3 ${CLAUDE_PLUGIN_ROOT}/skills/_shared/scripts/tracker/tracker_cli.py --project-dir $(pwd)"
${TRACKER_CLI} get-sprint-backlog {CYCLE_N}
```
Filter by this workstream's label or Linear project (the discriminator set at Commit).

**Then hydrate this clone's local Cycle state, before the loop.** A workstream
clone has its own gitignored `pipeline-state.json`; provisioning created the
branch and the discriminator but nothing opened a Cycle locally. Skip this and
`next_action` returns `not_in_execution` and dispatches nothing, which reads as
"no work" rather than "not set up".

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" \
  hydrate_cycle "$(pwd)" {CYCLE_N} \
  --goal "<the Cycle goal from Commit>" \
  --work-units '<the filtered backlog as JSON: [{"id":…,"title":…,"depends_on":[…],"file_scope":[…]}]>'
```

Pass the backlog you just fetched, filtered to THIS workstream. The command is
idempotent (re-running after a partial setup converges) and it refuses an empty
unit list on purpose: an empty Cycle would make `next_action` return
`await_sync` immediately and let the workstream declare readiness having
delivered nothing.

**On Cursor, prefer MCP over shelling these scripts.** Map:

| Verb | Cursor MCP tool |
|---|---|
| `hydrate_cycle` | `hydrate_cycle` (`cycle_number`, `work_units`, `goal`, `workstream_id`) |
| `next_action` | `next_action` |
| `begin_dispatch` | `begin_dispatch` |
| `advance` | `advance` |
| `request_acceptance` | `request_acceptance` |
| `accept_story` | `accept_story` |
| `publish_event` | `spq_publish_event` (`cycle_id`, `manifest_hash`, `work_unit_id`, `condition`, plus `commit_sha` or `output` evidence) |
| `refresh_ledger` | `spq_refresh_ledger` (`cycle_id`) |
| `declare_sync_ready` | `declare_sync_ready` (`cycle_number`, `workstream`, `declared_by`) |
| `transition SYNC` | `transition` (`to_state: SYNC`) |
| `evaluate_sync` / `clear_sync` / `close_cycle` | same-named MCP tools |

After Cycle N, this clone stays in `CYCLE_EXECUTION` (or `SYNC` after the documented transition). **Hydrate Cycle N+1 with MCP `hydrate_cycle` — never `--force`.** The verb walks a finished previous Cycle to COMMIT internally.

**The iteration loop is code-driven.** At the TOP of every iteration, before deciding anything yourself, run:
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" next_action "$(pwd)"
```
(or Cursor MCP `next_action`)
and execute the returned `action`. Do not pick the next Work Unit from memory; the JSON is authoritative. Repeat (dispatch, transition, `next_action` again) until it returns **`await_sync`**, `await_acceptance`, `deps_blocked`, or `all_blocked`.

**`sprint_complete` must never appear under `spq`.** The wrapper translates it into `await_sync`, because closing a Cycle on "all Work Units terminal" would close it *without integrating*. If you do see `sprint_complete`, you are driving the scrum state machine against SPQ state: stop and check `build_mode` and which script you invoked.

> ### ⚡ Performance: the two biggest wall-clock levers (read before dispatching)
> Serial-by-default execution and per-role re-verification are the measured causes of slow cycles. Both are already permitted by the framework, and you must actively USE them:
>
> 1. **Parallelize independent Work Units. Do NOT dispatch one-at-a-time by reflex.** If `next_action` returns `parallel.eligible: true`, dispatch the whole batch per `backends/claude.md` § Parallel story dispatch. If it does NOT (because `parallelism.story_parallelism` is unset, and it is **off by default**), that is a config gap, not a signal to go serial: independent units (empty `depends_on`, disjoint `file_scope`) SHOULD run concurrently. A dependency chain (A→B→C) is the only reason to serialize. **`depends_on` is a hard dispatch gate, not a batch filter**: a unit with an unmet edge cannot be dispatched through the serial path either, and an edge naming a unit that is not on this board fails closed. So declare real sequencing only — an aspirational or stale edge now stalls the Cycle rather than being quietly ignored.
> 2. **Verify ONCE, at the right layer** (`protocols/verification-discipline.md` § Trust Chain). **SE = fast gates only** (typecheck, unit-scoped tests, one build); **QE = the one full pass**; **CR = read-only**. Under the default `at_sync` integration policy, the full cross-workstream regression runs once at `SYNC`, not at the end of each workstream's own execution. Under opt-in `incremental`, the same regression also runs on each local integration result before its verified event is published; do not add another ad-hoc per-role pass.
> 3. **Cut retries at the source.** In every SE dispatch, enumerate each AC's negative and edge cases (already in the Cycle test specification) as REQUIRED tests.

**Every `next_action` output carries a `dod` block**: `{tier, tier_source, active_checks}`. State the tier and its active checks in every dispatch prompt (the Evidence Contract section of the backend wrapper) so agents know the gate expectations up front. `tier_source: "computed"` means the tier was never announced at Commit, so announce and persist it (`set_dod_tier`) at the next opportunity.

**Every `se` dispatch prompt must list the shared paths as ask-first.** Read `shared_digest_paths` from `sync_barrier.py config` and state in the prompt: *"these paths are owned by the `{shared_owner}` workstream for this Cycle, so do not edit them; if the Work Unit needs a change there, stop and report it as an input to the next Commit."* This protection is **prompt-level only**. No boundary guard and no `sprint.protected_modules` setting enforces it, and you must not tell the user otherwise. Drift is caught after the fact by barrier criterion 4 at `SYNC`, which is the expensive moment.

**Interrupted sessions.** On ANY resumed session (new conversation, post-crash, post-compact), run `next_action` FIRST. Never replan from memory; the board state and receipts on disk are authoritative. Honor a `resume_candidate: true` before any re-dispatch, because partial work already exists and restarting from scratch throws it away.

| `action` | What you do |
|---|---|
| `dispatch_se` | Dispatch the software-engineer for `story_id` (queued means run `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/advance_kernel.py" begin_dispatch "$(pwd)" {story_id} --role se` first, which authorizes the dispatch against `next_action` and returns the canonical receipt path to write; in_progress means resume). If `receipt_present: true`, skip the dispatch and run `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/advance_kernel.py" advance "$(pwd)" {story_id} {transition_to}` instead, because the agent already finished and only the transition is missing. If the output carries `parallel.eligible: true`, dispatch the whole batch via the worktree procedure in `skills/_shared/backends/claude.md` § Parallel story dispatch. |
| *(tier 0, resume)* `dispatch_*` with `resume_candidate: true` | A prior run was interrupted mid-work. Re-dispatch the SAME role with the RESUME preamble (backend wrapper § Recovery ladder tier 0): continue, don't restart, don't revert. **Record NO retry.** |
| `dispatch_qe` | Dispatch the quality-engineer for `story_id`. Same `receipt_present` rule. QE always runs post-merge on the workstream's main workspace, never in a Work Unit worktree. |
| `dispatch_cr` | Dispatch the code-reviewer for `story_id`. |
| `promote_story` | Run `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/advance_kernel.py" advance "$(pwd)" {story_id} done`. The kernel validates the CR receipt and fires DoD, redirecting to `blocked` if a conditional gate is unsatisfied. |
| `request_acceptance` | Run `spq_state_machine.py request_acceptance "$(pwd)" {story_id}` (or Cursor MCP `request_acceptance`). Per-Work-Unit acceptance is on. |
| `recover_blocked` | Follow `recovery.tier`. **`gate_remediation`**: the unit failed a conditional DoD gate, so unblock and dispatch `recovery.role` (compliance-engineer for `no_critical_findings`; QE in runtime/browser-qa/integration mode for the others) following `recovery.reason` verbatim. Do **NOT** re-run SE; it can't clear the gate. `retry_same_prompt`: re-dispatch `recovery.role` unchanged. `retry_augmented_prompt`: re-dispatch with the failure reason cited. **For both retry tiers, record the retry FIRST**: `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/story_pipeline.py" record_retry "$(pwd)" {story_id} {recovery.role} "{failure summary}"`, then `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/story_pipeline.py" unblock "$(pwd)" {story_id}` and re-dispatch. |
| `block_story` | A verification loop exhausted the retry ladder on a failed receipt. Run `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/advance_kernel.py" advance "$(pwd)" {story_id} blocked --reason "{recovery.reason}"`, then continue the loop. The unit is parked; move on. |
| `await_acceptance` | STOP the loop. Work Units await the PO walk at Checkpoint. Human gate; never auto-continue past it. |
| **`await_sync`** | **STOP the loop. This is the barrier, a hard human gate.** Every admitted Work Unit is terminal, but the Cycle does not close until integration is proven. Do **not** transition to CHECKPOINT, do **not** merge anything yourself, do **not** keep dispatching. Go to the `SYNC` section below. |
| `deps_blocked` | STOP. Every queued Work Unit is held by an unmet `depends_on` edge and nothing else is dispatchable. Follow the reason code before asking: for `dep_stale_manifest`, run `hydrate_cycle` again for this Cycle and workstream before any dispatch; for `dep_ledger_stale` or `dep_other_workstream_unresolved`, fetch and run `spq_state_machine.py refresh_ledger "$(pwd)"` (Cursor: `spq_refresh_ledger`) once, then run `next_action` again. If the edge is still unresolved, name its `owner_workstream` and required condition and ask the human to coordinate publication. `dep_incomplete` waits for a sibling; `dep_cancelled` requires re-admission or an explicit manifest revision; `dep_unknown` is a typo or non-admitted unit; `dep_out_of_cycle` and `dep_external` belong outside this Cycle. Never work around any case by editing `depends_on` in execution state. |
| `all_blocked` | STOP. The recovery ladder is exhausted, so summarize the blocked Work Units and ask the user. Scope-cutting one of them (`cut_work_unit`) is often the right answer under a scope-defined Cycle. |
| `not_in_execution` | The lifecycle is not `CYCLE_EXECUTION`. Re-read the state and route from the top of this file, because you are in the wrong section. |

The stage semantics behind those actions are unchanged from Scrum:
1. **SE implements**, so the unit moves `queued → in_progress → testing`
2. **QE tests**, so it moves `testing → reviewing`
3. **CR reviews** (adaptive: `next_action` only asks for CR when the DoD tier requires `code_reviewed`), so it moves `reviewing → done`
4. **DoD evaluation**: the kernel invokes `evaluate_story_dod` on the `→ done` edge. To re-run explicitly:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" evaluate_dod "$(pwd)" "{WORK_UNIT_ID}"
   ```

Drive `reviewing → done` through `advance_kernel.py advance`, the only legal writer of story state. It validates the receipt (canonical path, freshness, no replay) before firing the conditional gates (compliance, runtime verification, UI acceptance, integration claims). The bare `story_pipeline.py transition` verb refuses this edge. Those gates behave exactly as documented in `modes/sprint.md`; SPQ changes none of them.

**Integrate every admitted non-cut Work Unit under an incremental policy; a `done` board state is local to one clone.** When the sealed manifest uses `integration_policy.mode: incremental`, run this human-gated sequence for every Work Unit after it reaches `done`, whether or not another workstream currently depends on it. Sync requires an `integrated` event for the complete admitted set. A dependent edge changes the urgency of refresh, not which units must be integrated.

1. The human commits and pushes the completed workstream branch. Agents never commit, merge, or push shared branches without explicit approval.
2. In the integration clone, the human merges that branch into the manifest's `integration_ref` locally. Before pushing the result, run:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/sync_barrier.py" \
     evaluate-incremental "$(pwd)" {CYCLE_N} --unit {WORK_UNIT_ID} --sha {WORK_SHA}
   ```
   A non-green verdict blocks publication. `require_regression: true` is sealed as `merge_requires: [regression_green]`, so a failed regression cannot become an `integrated` event. Preserve the returned `attestation` object exactly; it binds `{WORK_SHA}` to the evaluated integration HEAD.
3. After a green result, the human pushes that exact integration HEAD. In the producer clone, fetch the sealed `integration_ref` and publish the verifiable event with the returned attestation:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" \
     publish_event "$(pwd)" {WORK_UNIT_ID} --condition integrated --sha {WORK_SHA} \
     --cycle-id {CYCLE_ID} --manifest-hash {MANIFEST_HASH} \
     --evaluation '{INCREMENTAL_ATTESTATION_JSON}'
   ```
   Publication re-checks the attestation id, candidate ancestry, manifest identity, and that the integration ref still points to the evaluated HEAD. If it moved, re-run `evaluate-incremental`; never reuse the stale result. On Cursor use `spq_publish_event` with the same `evaluation` object plus the sealed identities. The returned event commit/push command is a human action.
4. When another Work Unit depends on this event, immediately fetch in every dependent clone and run `spq_state_machine.py refresh_ledger "$(pwd)"` (Cursor: `spq_refresh_ledger`), then re-run `next_action`. Independent units still complete steps 1–3; they simply have no dependent clone to refresh yet. Refresh reads committed events without merging another workstream's board.

For a `contract_published` or `artifact_published` edge, publish structured digest evidence instead of an integration SHA: `--output '{"id":"contracts/auth","digest":"sha256:..."}'` (Cursor `spq_publish_event.output` is the same object). The Cycle's configured digest script recomputes it; an omitted or mismatched digest does not unblock the consumer.

**Infrastructure Work Units** (PE) run in parallel with the app pipeline, same as Scrum. Detect by title or labels containing "infra", "CI/CD", "Docker", "Terraform", "monitoring", "pipeline".

When `next_action` returns `await_sync`, the workstream declares readiness from its own clone:
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" declare_sync_ready "$(pwd)" {CYCLE_N} \
  --workstream {WORKSTREAM_ID} --declared-by "{LEAD_EMAIL}"
```
On Cursor: MCP `declare_sync_ready` with the same fields. Do not shell around a missing verb.

This runs the workstream regression and the digest script, and **derives** the `dod` and `replay` blocks from local state. You cannot supply them, by design. It refuses outright if a replay mismatch is present: a workstream whose attested evidence did not reproduce has not finished its Work Units, and discovering that at the barrier wastes the integration slot.

Then transition the lifecycle and hand off to the integration clone:
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" transition "$(pwd)" SYNC
```
On Cursor: MCP `transition` with `to_state: SYNC`. Never `--force` this step when every unit is done.

When the next Cycle starts, hydrate N+1 on this clone (MCP `hydrate_cycle`). Do not `--force` COMMIT.

### `SYNC`

Load and follow the Sync state:
```
Read("${CLAUDE_PLUGIN_ROOT}/skills/synaptory/spq/sync.md")
```

**Runs in the integration clone.** Sync handles the ordered barrier procedure: `collect` (reads every workstream's readiness record via `git show`, without merging), then a **human merge** into `sync/cycle-{N}`, then `evaluate` (regression, digests, journey on the integrated tree), then `clear_sync`, then a **human PR** promoting to `dev`. On Cursor those verbs are MCP `evaluate_sync`, `clear_sync`, and `close_cycle` (Checkpoint). Never shell `spq_state_machine.py` to work around a missing MCP tool.

Two things about this state are easy to get wrong:

- **Steps 2 and 5 are human by design.** The shipped git safety rules forbid every synaptory agent from committing or pushing to shared branches, and require explicit approval for any commit, push, or merge. That is why the barrier merges into `sync/cycle-{N}` (a feature branch cut from `dev`) and never into `dev` itself. Do not offer to do the merge for the user.
- **There is no automatic path from `CYCLE_EXECUTION` to `CHECKPOINT`.** The state machine rejects that transition outright. `SYNC` is the only route.

`CYCLE_EXECUTION → SYNC → CHECKPOINT` is also why the authoritative final cross-workstream regression lives here rather than at the end of each workstream's own run: with parallel workstreams, the complete integrated tree only exists at Sync. Incremental mode adds earlier checks of partial integration results; it does not replace this final barrier.

### `CHECKPOINT`

Load and follow the Checkpoint state:
```
Read("${CLAUDE_PLUGIN_ROOT}/skills/synaptory/spq/checkpoint.md")
```

Checkpoint handles the customer demonstration from the integrated branch, TW reporting, PO acceptance, **MethodSignal harvest** (SPQ has no retro event, so process learning is recorded as signals here and read at the next Commit), and `close_cycle`, which loops to COMMIT or proceeds to ACCEPTANCE:
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" close_cycle "$(pwd)" --proceed-to COMMIT
```

`close_cycle` refuses without a Technical Writer receipt at `CHECKPOINT-{N}-tw.json`, the same guard scrum puts on Sprint Review. The Checkpoint report is the Cycle's audit trail of what was demonstrated and accepted, so dispatch `tw` per `spq/checkpoint.md` rather than reaching for `--force`, which exists for operator backfill only.

### `ACCEPTANCE`

Load and follow the Acceptance state:
```
Read("${CLAUDE_PLUGIN_ROOT}/skills/synaptory/spq/acceptance.md")
```

Release and Operate handover at release depth: full regression, security audit, production infrastructure, documentation.

### `COMPLETE`

Project lifecycle is finished. Report final status:
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" summary "$(pwd)"
```

---

## Git Safety Rules (MANDATORY)

These rules apply to all synaptory agents, Claude Code, and automated tooling. **`SYNC` needs no carve-out from them**, which is why the barrier uses an integration feature branch:

1. **NEVER commit or push to shared branches** (`dev`, `qa`, `uat`, `main`, `prod`, `staging`, `release`). All work MUST happen on feature branches, including `sync/cycle-{N}`.
2. **NEVER create commits without explicit user approval.** Always show the diff and ask before committing.
3. **NEVER push to any remote branch without explicit user approval.** Always ask before running `git push`.
4. **NEVER create or merge pull requests without explicit user approval.** The workstream to `sync/cycle-{N}` merge and the `sync/cycle-{N}` to `dev` promotion are both human acts.
5. **NEVER run destructive git operations** (`git push --force`, `git reset --hard`, `git clean -f`, `git checkout .`).
6. **NEVER run database migrations against shared environments** (dev, qa, uat, prod). Migrations can only be run locally.
7. **NEVER hand-author a readiness record.** `.synaptory/sync/` is written only by `sync_barrier.py declare-ready`, which derives it from state and an executed regression. The boundary guard blocks write-tool access to that directory, because a record a model could write is a formality rather than a barrier.

If the user says "just do it" or "go ahead", that applies to the current code change only, NOT to committing, pushing, or merging.

---

## Cycle Number Detection

`N` has exactly one owner: the integration clone's state (`current_cycle`). The tracker's cycle is a **mirror**, bound to `N` at Commit, so a cycle renamed or renumbered in Linear cannot repoint a Cycle.

When the user says "cycle N" or "run cycle 3":

1. Extract the number from the request
2. Compare with `current_cycle` in state:
   - If N == current_cycle: resume the current Cycle
   - If N == current_cycle + 1: hydrate/open the next Cycle. On a workstream clone that is still in `CYCLE_EXECUTION` or `SYNC` from Cycle N (all units done), call `hydrate_cycle` — it walks to COMMIT. Never `--force`.
   - If N > current_cycle + 1: block with "Cannot skip Cycles. Current Cycle is {current_cycle}."
   - If N < current_cycle: block with "Cycle {N} is already closed."

When the user says "next cycle" or "continue cycle":
- Use current_cycle + 1 if in CHECKPOINT or COMMIT, or if this workstream finished Cycle N (`await_sync` / SYNC)
- Resume current_cycle if in CYCLE_EXECUTION with unfinished units

---

## Progress Output

Print the state header at the start of each state:

```
━━━ Cycle {N} ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  State:      {STATE_NAME}
  Goal:       {CYCLE_GOAL}
  Workstream: {WORKSTREAM_ID} ({clone: workstream | integration})
  Work Units: {done}/{admitted} done · {cut} cut
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

At `SYNC`, add the barrier line so the human gate is visible at a glance:

```
  Barrier:    {ready}/{quorum} workstreams ready · verdict {green | blocked | not evaluated}
```
