# SPQ Mode: Lifecycle Dispatcher

Execute the SPQ delivery lifecycle. Routes to the correct stage file from the current `lifecycle_state`; each stage file owns its own transitions.

**Four stages, and three events that are not stages.**

```
DISCOVERY ──► CYCLE ──► ACCEPTANCE ──► COMPLETE
                ▲          │
                └──────────┘   a non-final go-live returns to delivery
```

| | What it is | What it may block |
|---|---|---|
| `DISCOVERY` · `CYCLE` · `ACCEPTANCE` · `COMPLETE` | the four stages | a stage change is refused when its own condition is unmet |
| **Commit** · **Sync** · **Checkpoint** | recorded method events | **nothing.** An event records that something happened; it never authorizes it |

That table is the whole shape of this lifecycle, and the second row is the part most easily got wrong. Reading Checkpoint as a gate invites a second approval path beside the one that actually authorizes release. So:

- **Commit does not open a Cycle** -- the sealed declaration does, and `open_cycle` records the event as a consequence.
- **Sync does not unblock a Work Unit** -- a satisfied dependency does. A Cycle with **no Sync event at all is the common case**, not a broken one.
- **Checkpoint does not close a Cycle** -- the barrier does. The event records what was integrated.

The words **Workstream**, **Coordination Cycle** and **Cycle of Cycles** are retired. If a request uses one, say which of the three replacements it means -- a source region, a Crew, or simply another concurrent Cycle -- and route accordingly. There is no lane, no parent Cycle and no composition step.

## Trigger Signals

"spq", "open cycle", "next cycle", "run a cycle", "cycle N", "continue cycle", "resume cycle", "commit the cycle", "record a sync", "checkpoint", "close cycle", "run the barrier"

## Prerequisites

1. The project runs the SPQ build mode. **`spq` is never a default and never inferred.** It requires an explicit `build_mode: spq` in `.synaptory.yaml`.
   ```
   STATE=$(MCP `get_state`
   ```
   If no state exists yet, initialise it:
   ```
   MCP `spq_lifecycle` {"operation": "initialize"}
   ```
   `init` puts the project at `DISCOVERY` with no Cycle and no board file. If `.synaptory.yaml` says `scrum` or `kanban`, stop: SPQ is a different lifecycle, not a setting to flip mid-flight.

2. **`.synaptory/cycles/` must not be gitignored.** A Cycle's sealed declaration, its cut record and its dependency events travel through git to every other clone. `open_cycle` refuses with `transport_ignored` when they would be invisible, because a declaration that writes cleanly here and reaches nobody is the worst of the available failures. The two lines:
   ```
   .synaptory/*
   !.synaptory/cycles/
   ```

3. **A Cycle is named, never current.** N Engineering Leads means N concurrent Cycles, so there is no project-global "current Cycle" that could select among them. Reads and mutations take the Cycle they act on (`--cycle-id`), and a verb that cannot resolve one refuses and lists what is open. A UI convenience selection is never authority.

---

## One Cycle, one Engineering Lead, one Crew, one board

| Thing | Where it lives | What owns it |
|---|---|---|
| The sealed declaration | `.synaptory/cycles/{cycle-id}/manifest.json` (committed), plus the local seal | fixed at Commit; changed only by a linked supersession |
| The board | `.synaptory/.orchestrator/spq/cycles/{cycle-id}/execution-state.json` | this Cycle, projected from its declaration |
| The mode + identity pointer | `.synaptory/.orchestrator/pipeline-state.json` | mode and Cycle identity **only** |
| Cuts | `.synaptory/cycles/{cycle-id}/cuts.json` (committed) | append-only; the barrier's second input |
| Dependency events | `.synaptory/cycles/{cycle-id}/events/` (committed) | published by a producer, pushed by a human |
| Receipts | `.synaptory/.orchestrator/spq/cycles/{cycle-id}/receipts/` | resolve it, never spell it (below) |

**`pipeline-state.json` is not the board.** It carries mode and identity, and a reader that opens it looking for `current_stories` sees nothing on a healthy SPQ project and reports the emptiness as a measurement. Read a board through `pipeline_board.read_board`, or through the verbs below, which know every layout.

**A Crew grants nothing.** It is who is executing a Cycle now: an Engineering Lead and the agents they direct, re-seatable between Cycles. Re-seating one changes no barrier admission, no ownership and no permission. Do not name anything after a Crew -- an id in permanent control-plane history that moves when people move is a durable lane under a new name.

**Concurrent Cycles stay apart by declaration.** Each declares the `source_region` it may address, and two concurrent Cycles never declare overlapping regions. That is the only cross-Cycle check in the system and it runs at declaration -- which is exactly what lets concurrent Cycles stay independent, because no Cycle has to inspect what another is admitting.

---

## Receipt ids and stages, by stage

Every stage file states this for itself. The table lives here because the dispatcher is the only file that spans all of them, and a mismatch is invisible until someone opens `/cost`.

| Stage | Agents dispatched | Skill invocations | Receipt ids |
|---|---|---|---|
| `DISCOVERY` | `qe` | `ra` (conditional), `po`, `sa`, `pe` | `DISCOVERY-0-qe.json`; `DISCOVERY-0-{research,baseline,architecture,infra}.json` |
| `CYCLE`, at Commit | `qe` | `po`, `sa` | `CYCLE-{N}-qe.json`; `CYCLE-{N}-{admission,architecture}.json` |
| `CYCLE`, per Work Unit | `se` → `qe` → `cr`, plus `ce`, `pe` | | `{WORK-UNIT-ID}-{se,qe,cr,ce,pe}.json` |
| `CYCLE`, at Checkpoint | `tw` | `po` | `CHECKPOINT-{N}-tw.json`; `CHECKPOINT-{N}-feedback.json` |
| `ACCEPTANCE` | `qe`, `ce`, `pe`, `tw`, `cr` | | `ACCEPTANCE-{N}-{qe,ce,pe,tw,cr}.json` |

Three rules span every stage:

- **Never spell a receipt path.** Resolve it, so the path you name is the path the gate reads:
  ```bash
  python3 "${PLUGIN_ROOT}/hooks/lib/advance_kernel.py" bind_receipt "$(pwd)" {UNIT_OR_PSEUDO_ID} {ROLE_ABBREV}
  ```
  It prints `receipt_path` from the same resolver the gate and the DoD aggregation use. A hardcoded `.orchestrator/receipts/` is a receipt no gate sees.
- **A stage that is not a Work Unit uses a pseudo Work Unit id.** A receipt's `story_id` must match `^[A-Z][A-Z0-9]*-\d+$`, which is why the ids are `DISCOVERY-0`, `CYCLE-{N}`, `CHECKPOINT-{N}`, `ACCEPTANCE-{N}` and not stage names. A receipt the orchestrator writes itself carries `role: "orchestrator"` and a descriptive filename suffix rather than a role abbreviation.
- **`po` receipts MUST name `token_usage.stage` explicitly**: `pro-discovery` at Discovery, `pro-brd` at Commit and Checkpoint. `project-owner` is deliberately absent from the validator's role-to-stage fallback because it spans three stages. Omit it and the receipt still validates while the cost attribution silently disappears.

---

## Dispatch identities versus skill invocations

**The rule (#405, ADR-032).** A role is a dispatch identity on the flows where the lifecycle **requires a receipt from it**, and a fetchable skill everywhere else. Where no gate reads a role's receipt, spawning an isolated subagent buys nothing and costs a 15k-to-25k-word persona payload per step.

These are the receipt-gated edges. On them the role stays a dispatch and no skill invocation substitutes:

| Edge | Required receipt | Refusing line |
|---|---|---|
| `queued -> in_progress`, `in_progress -> testing` | `{WU}-se.json` | `advance_kernel.TRANSITION_RECEIPT` |
| `testing -> reviewing` | `{WU}-qe.json` | `advance_kernel.TRANSITION_RECEIPT` |
| `reviewing -> done` | `{WU}-cr.json` | `advance_kernel.TRANSITION_RECEIPT` |
| `ACCEPTANCE` readiness | `ACCEPTANCE-{N}-{qe,ce,pe,tw,cr}.json` | `spq_state_machine.acceptance_readiness` |

**There is no per-Work-Unit acceptance edge on this lifecycle, and no `accept_story` verb.** `awaiting_acceptance` is a Scrum sprint-review toggle; SPQ accepts against an evidence package at Acceptance, and credits throughput from a named human's acceptance recorded at Checkpoint. A prompt reaching for `accept_story` is reading a retired surface: the verb is gone, and a test asserts its absence in both directions.

**The Cycle close is not receipt-gated either.** It is barrier-gated, which is a stronger thing and a different one -- see `spq/checkpoint.md`. Do not tell a user the close refuses without a Technical Writer receipt: it does not, and the barrier is what refuses.

**How to run a skill invocation.** Retrieve the role's intent contract and only the guides the step needs, then do the work here:

```
synaptory skills get <role>
synaptory skills get <role>/guides/<playbook>
```

**Then write ONE receipt for the step.** Inline work skips SubagentStop, so nothing writes it for you, and an unwritten receipt is work that never reaches `/cost` or `/audit`. Shape:

```json
{
  "story_id": "DISCOVERY-0",
  "role": "orchestrator",
  "accountable_role": "solution-architect",
  "backend": "claude",
  "model": "{model_id_used}",
  "artifacts": ["docs/architecture/SAD.md", "docs/architecture/ERD.md"],
  "metrics": {"adrs_written": 0, "api_endpoints": 0},
  "verification_commands": ["test -s docs/architecture/SAD.md"],
  "token_usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "stage": "sa-architecture"},
  "completed_at": "{iso8601_utc_timestamp}"
}
```

`role: "orchestrator"` is who ran the step; `accountable_role` names the role whose contract governed it, which is an accountability and never a permission; and `token_usage.stage` stays the ROLE's stage -- `sa-architecture`, `pe-infra`, `pro-discovery`, `pro-brd` -- so `/cost` by-stage stays comparable. The kind of act did not change when the runner did. Writing `orchestrator` in the stage field moves the spend into top-level routing and destroys that comparison.

Model tiers are unchanged from Scrum **for dispatches**: Opus for `po`/`sa`/`ce`/`ra`/`cr`, Sonnet for `se`/`qe`/`pe`/`tw`, routed by tier alias. `backends/model-pins.json` records what each alias is INTENDED to mean and is not read on the dispatch path (#519, #596). A **skill invocation selects no tier**: it runs on whatever model this session is on, and the receipt records that model. Record it accurately rather than copying the role's pin, or `/cost` prices the step at a rate nothing ran at.

---

## Stage routing

Route on `lifecycle_state` from `read`. Load the stage file and follow it.

### `DISCOVERY`

```
Read("${PLUGIN_ROOT}/skills/synaptory/spq/discovery.md")
```

Frames the problem, resolves the solution, declares the initial source regions, and ends in an **approved baseline** -- scope, non-scope, timeline, cost, and a *measured* calibration sample. Nothing opens a Cycle before that baseline exists.

### `CYCLE`

One stage that repeats. Plan → build → prove happens continuously inside it, and the three events punctuate it. Route within the stage by what the Cycle needs next:

| Situation | File |
|---|---|
| No open Cycle, or the last one closed and the next is being sized | `Read("${PLUGIN_ROOT}/skills/synaptory/spq/commit.md")` |
| Work Units are admitted and running | the per-unit loop below |
| A Work Unit waits on something another Cycle has not published | `Read("${PLUGIN_ROOT}/skills/synaptory/spq/sync.md")` |
| Every admitted unit is done or cut | `Read("${PLUGIN_ROOT}/skills/synaptory/spq/checkpoint.md")` |

The per-unit pipeline itself is unchanged: SE -> QE -> CR per Work Unit, with the sub-states `queued -> in_progress -> testing -> reviewing -> done` (or `blocked`). Fetch and follow its protocol -- the body is control-plane delivered per ADR-016 and does not exist on local disk:

```
Bash("synaptory skills get protocols/story-pipeline")
```

**The per-unit loop is code-driven.** At the top of every iteration, before deciding anything yourself:

```bash
MCP `next_action` {"cycle_id": "{CYCLE_ID}"}
```

Execute the returned `action`. Do not pick the next Work Unit from memory; the JSON is authoritative about what the board says.

**`next_action` recommends; the sealed declaration authorizes.** Under SPQ it is advisory, and that is a deliberate change. A second dispatch is legal when the three checks that precede the binding all pass -- the unit is admitted to the sealed declaration, its dependencies are met, and its declared `path_scope` is disjoint from every live dispatch. A unit clearing all three is legal work by the method's own rule, so refusing it because `next_action` named a different unit would be the retired parallelism switch under another name.

Authorize each dispatch through the kernel, which runs exactly those checks:

```bash
MCP `begin_lifecycle_dispatch` {"role": "{ROLE_ABBREV}", "story_id": "{UNIT_ID}"}
MCP `advance` {"receipt_path": "{RECEIPT_PATH}", "story_id": "{UNIT_ID}", "to_state": "{TO_STATE}"}
```

`begin_dispatch` reports its checks by name (`deps_met`, `path_scopes_disjoint`, `next_action_match`). A refusal coded **`scope_collision`** means this unit's declared scope intersects one already holding a live attempt: that pair is sequential work inside one Cycle, so finish the running one first. The scopes it compares come from the sealed declaration, never from the board -- the board is agent-writable, and a unit able to narrow its own recorded scope could dispatch straight into another unit's files.

**Concurrency needs no enablement.** There is no `story_parallelism` switch on this lifecycle and no `isolation` mode; `concurrency_policy` answers `max_concurrent` plus `governed_by: "declared path scope (C-07)"`. `parallelism.max_concurrent` is a resource ceiling and may never make colliding work legal.

**Every `next_action` output carries a `dod` block** -- `{tier, tier_source, active_checks, undetermined_checks, compliance}`. State the tier and BOTH lists verbatim in every dispatch prompt. `active_checks` is resolved per unit by the same code the gate calls, so it includes conditional gates that appear in no tier list; each `undetermined_checks` entry carries a `why` naming what would activate it, so a check absent from `active_checks` may be unmeasured rather than inactive. `compliance.required: false` with `provisional: true` means the PHI signal has not tripped **yet**.

**Every `se`/`qe`/`cr` dispatch output also carries an `authored_cases` block** -- `{authored_at_commit, count, cases: [{id, statement, criterion_ref}], criteria_gap_declared}`. **You MUST state it verbatim in the dispatch prompt.** It is not context, it is the target:

- `dispatch_se` -- "these are the acceptance cases this Cycle authored at Commit, before any code existed. They are your target; the verifying stage will execute exactly these. Do not edit them."
- `dispatch_qe` -- "execute these authored cases and record a per-case outcome for EVERY id in `metrics.authored_test_cases.results`. An omitted id is scored as a dropped case and fails `tests_pass`; a green suite exit code cannot clear it."
- `dispatch_cr` -- "review the diff against these declared criteria, not against itself."

When `authored_at_commit: false`, state `criteria_gap_declared.reason` instead. `declared: false` means nobody declared the gap -- the unit predates the requirement, and that belongs in the Checkpoint report rather than carried forever.

**A clone that did not open the Cycle must hydrate onto it** before dispatching. The declaration travels through git; the board does not:

```bash
MCP `spq_lifecycle` {"operation": "hydrate_cycle", "cycle_id": "{CYCLE_ID}"}
```

It verifies the seal first and refuses `manifest_absent` or `manifest_tampered` rather than adopting an unverified admitted set. It is also how a **superseded** declaration reaches the board: a revision writes the seal and nothing else, so re-hydrating is what re-projects it. `reprojected: true` in the result means the board moved to a new revision.

**Interrupted sessions.** On any resumed session -- new conversation, post-crash, post-compact -- run `next_action` FIRST. Never replan from memory; the board and the receipts on disk are authoritative. Honour a `resume_candidate: true` before any re-dispatch, because partial work already exists.

### `ACCEPTANCE`

```
Read("${PLUGIN_ROOT}/skills/synaptory/spq/acceptance.md")
```

One go-live. **Acceptance repeats** -- each one compiles its own evidence package and its own readiness decision, and only the final one closes the engagement. Delivery may continue in other Cycles while it runs: a release does not wait for an unrelated open Cycle.

### `COMPLETE`

The engagement is closed: the final Acceptance handed over the codebase, the documentation and the operating knowledge. `COMPLETE` is terminal and there is no edge back -- `open_cycle` refuses on it too, not only `transition`. Report the final state:

```bash
MCP `get_state`
```

---

## There is no override

`transition` takes no `force`, and neither does any other verb -- `cycle_lifecycle.assert_no_force_parameter` proves the absence rather than asserting it, because the predecessor's `force=True` moved a project from `DISCOVERY` to a close in one call, from any raw module invocation. A transition that cannot be taken is refused, and recovery is an accountable decision recorded where the deciding agent cannot write it.

So when a verb refuses, read the refusal. Every one names what is legal instead, and reaching for a flag that would skip it is reaching for something removed on purpose.

---

## Git safety rules (MANDATORY)

1. **NEVER commit or push to shared branches** (`dev`, `qa`, `uat`, `main`, `prod`, `staging`, `release`). All work happens on feature branches, including a Cycle's own integration branch.
2. **NEVER create commits without explicit user approval.** Show the diff and ask.
3. **NEVER push to any remote branch without explicit user approval.**
4. **NEVER create or merge pull requests without explicit user approval.** The trunk promotion at Checkpoint is a human act; the barrier's job is to bind that authorization to one exact candidate, not to perform the merge.
5. **NEVER run destructive git operations** (`git push --force`, `git reset --hard`, `git clean -f`, `git checkout .`).
6. **NEVER run database migrations against shared environments.**
7. **NEVER hand-author a sealed declaration, a cut record, a dependency event or a barrier verdict.** Each is written by the code that validates it. A record a model can author is a formality rather than a guardrail.

If the user says "just do it" or "go ahead", that applies to the current code change only, NOT to committing, pushing, or merging.

---

## Which Cycle does the user mean?

A Cycle id is `{seq}-{hash}` -- the sequence for reading, the hash for identity, so two clones opening sequence 7 at the same moment get different Cycles. When the user says "cycle 3":

1. Read what is open. `read` reports the resolved `_cycle_id`, and a verb given no `--cycle-id` refuses with the open ids listed.
2. If exactly one open Cycle carries that sequence, act on it.
3. If several do, **ask**. Do not guess: acting on the wrong Cycle applies a change to a Cycle nobody named, which is why the mutations refuse a mismatched identity in the first place.
4. "Next cycle" after a close means a new Commit, not a renumbering. `CYCLE -> CYCLE` is a legal self-transition: closing one Cycle and opening the next is delivery continuing, and modelling it as a departure and a return would make the trunk look momentarily undelivered.

---

## Progress output

Print the header at the start of each stage:

```
━━━ Cycle {CYCLE_ID} ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Stage:      {STAGE}
  Goal:       {CYCLE_GOAL}
  Lead:       {ENGINEERING_LEAD}
  Work Units: {done}/{admitted} done · {cut} cut · effective {n}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

At Checkpoint add the barrier line, so the state of the transaction is visible at a glance. Take the counts from the verdict rather than restating a criteria list from memory: the criteria are published in the sealed declaration, and the verdict names every one it evaluated.

```
  Barrier:    {met}/{published} criteria met · {green | unmet: …} · candidate {sha}
```
