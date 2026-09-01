# Sync. Cross-Workstream Integration Barrier

> **Lifecycle state:** `SYNC`
> **Participants:** Orchestrator (integration clone), delivery lead (human), QE, plus SE / SA / CE conditionally
> **Output:** a green barrier verdict, `SYNC-{N}-barrier.json`, and the sealed manifest's integrated `{INTEGRATION_REF}` branch that `CHECKPOINT` demos from
> **Human gate:** YES. `next_action` returns `await_sync`, which is a stop action. Never auto-continue past it.

Sync is the one SPQ state with **no scrum analogue**. Scrum has no cross-clone
rendezvous because scrum has one working copy; SPQ runs N workstream clones plus
an integration clone (design §5), so "the increment" does not exist anywhere on
disk until Sync builds it. Everything else in SPQ is an adaptation. This is new
machinery, and the ordering below is load-bearing rather than stylistic.

**Why Sync sits before Checkpoint** (§4.1): integration must precede
demonstration. A Checkpoint that demos from one un-integrated workstream branch
shows 1/N of the system and defers the integration debt to whoever finds it
next. It also relocates the full cross-workstream regression: in `scrum` that
runs at `SPRINT_CLOSE`, but the integrated tree only exists here.

---

## The procedure is ORDERED, not a set

The eight exit criteria (§8.4) look like a checklist, and reading them as one
produces a barrier that cannot be run. Readiness records live on **workstream
branches**, so they are only co-visible after a merge, and the merge is itself
criterion 4. The fix is to read the records **without merging**, via
`git show`, which breaks the circularity.

| # | Criterion | Who evaluates it |
|---|---|---|
| 1 | Every workstream has a readiness record for this Cycle | `sync_barrier.py collect` / `evaluate` |
| 2 | Every record names the sealed Cycle and manifest revision | `sync_barrier.py evaluate` (`manifest_agreement`) |
| 3 | Admitted/done/cut identities close exactly over the manifest | `sync_barrier.py evaluate` (`manifest_closure`) |
| 4 | All workstream heads are ancestors of the sealed integration ref | **Human lead merges**; `evaluate` verifies (`branches_merged`) |
| 5 | Every dependency is satisfied and every incremental unit is integrated | `sync_barrier.py evaluate` (`dependency_closure`) |
| 6 | Full cross-workstream regression green on the integrated branch | `sync_barrier.py evaluate` (`regression_script`) |
| 7 | Shared-component ownership/digests agree with the integrated tree | `sync_barrier.py evaluate` (`digest_script`) |
| 8 | The increment's end-to-end journey passes on the integrated branch | `sync_barrier.py evaluate` (`journey_script`) |

Run in this order and no other:

```
  WORKSTREAM CLONES (×N, each on its own branch)
    0. sync_barrier.py declare-ready   → writes .synaptory/sync/cycle-<N>/<id>.json, then PUSH

  INTEGRATION CLONE
    1. sync_barrier.py collect         → git fetch + git show. NO merge. Readiness inputs.
    2. HUMAN merges each ws branch into manifest.integration_ref cut from dev.
                                          Conflicts → dispatch se → re-run step 1.
    3. sync_barrier.py evaluate        → all 8 criteria, outside the hook path, no timeout.
    4. spq_state_machine.py clear_sync → green only. SYNC → CHECKPOINT.
    5. HUMAN promotes manifest.integration_ref to dev by PR. CHECKPOINT demos from dev.
```

Steps 1, 3 and 4 are deterministic and unit-tested. **Steps 2 and 5 are human by
design**, and the reason is not process preference:

> **NEVER commit or push to shared branches** (`dev`, `qa`, `uat`, `main`,
> `prod`, `staging`, `release`). All work MUST happen on feature branches.

That is rule 1 of the Git Safety Rules the plugin itself generates into the
project's `CLAUDE.md` (`skills/synaptory/modes/init.md`), and it applies to
*"all synaptory agents, Claude Code, and automated tooling"*. Rules 2 to 4
additionally require explicit human approval for any commit, push, or merge.
Merging into the manifest's Cycle-scoped `integration_ref`, which is a
**feature branch**, is what keeps the
barrier procedure compliant with **no carve-out and no spq-specific variant of
the rules block**. Promotion to `dev` stays a human PR for the same reason.
If you find yourself wanting an exception here, the design is telling you the
integration branch name is wrong, not that the rule is.

---

## Prerequisites

Confirm the lifecycle state and the Cycle number. `N` is owned by the
integration clone's state and is monotonic (§8.3); the tracker cycle is a
mirror, never the source of truth.

```bash
MCP `get_state`
# require: lifecycle_state == "SYNC";  N = current_cycle
MCP `get_status`
MCP `spq_manifest_validate` {"cycle_id": "{CYCLE_ID}"}
# require: hash_verified == true; record integration_ref as {INTEGRATION_REF}
```

Confirm the resolved barrier config before spending integration time on a typo:

```bash
python3 "${PLUGIN_ROOT}/hooks/lib/sync_barrier.py" config "$(pwd)"
```

Check `workstreams[]` (an empty list cannot form a quorum), `branch_pattern`,
`remote`, `mode`, and that `regression_script`, `journey_script` and
`digest_script` all exist in the tree. A missing proof script does **not** pass:
`evaluate` reports it as `skipped`, and a skipped criterion is **unproven, not
passed**.

**Check `shared_digest_paths` came back non-empty, and do not trust the file to
tell you.** An empty list makes criterion 7 report `waived: true` and `passed:
true`, so a shared component silently drops out of the barrier. The `spq:` block
is read by a deliberately narrow yaml-lite parser (`hooks/lib` ships no PyYAML
dependency) which reads **inline** lists only:

```yaml
spq:
  sync:
    shared_digest_paths: ["contracts/", "design-system/"]   # parsed
```

The block form (`shared_digest_paths:` followed by `- "contracts/"` lines) parses
to `[]` and waives criterion 7 without warning. `sync_barrier.py config` is the
only thing that will tell you, which is why it is run here rather than after a
blocked verdict. The shipped config template uses the inline form; keep it.

---

## Step 0 (workstream clones). declare readiness

Each workstream runs this **in its own clone**, on its own branch, and then
pushes the branch. `collect` reads the record over the remote, so an unpushed
record is an absent record.

```bash
# in the hydrated workstream clone; the native spq/workstream pin identifies the lane
MCP `spq_lifecycle` {"operation": "declare_ready", "workstream": "{WORKSTREAM_ID}", "approved_by": "{LEAD_EMAIL}", "cycle": "{N}"}
```

Equivalent through the state machine (same code path, keeps the verb table in
one place):

```bash
MCP `spq_lifecycle` {"operation": "declare_ready", "workstream": "{WORKSTREAM_ID}", "cycle": "{N}"}
```

`declare-ready` executes `regression_script` locally with **no timeout**, derives
the rest from local state, and writes
`.synaptory/sync/cycle-{N}/{WORKSTREAM_ID}.json`. This is the one narrow
exception to gitignoring `.synaptory/`: commit `.synaptory/sync/` only.

### Three things about the readiness record that are easy to get wrong

**1. The record is a CLAIM, not proof (§8.2.1).** It is written in the
workstream's clone by that workstream's agents. What makes it worth having is
that it is the **only channel by which any quality evidence crosses the clone
boundary**: the integration clone cannot see a workstream's
`.synaptory/.orchestrator/events.jsonl`, and `signals.py` says outright that its
store *"never ships anywhere"*. That is why the record carries `dod` and
`replay` blocks. Without them the barrier's only quality input is one exit code
and criterion 1 degrades to "a file exists". Criteria 6 and 8 are re-derived
independently in the integration clone, so the damage from a false claim is
bounded, but `work_units` and `regression` are taken on trust.

**2. Agents must never hand-author a record.** `declare-ready` **derives** `dod`,
`replay`, `work_units` and `shared_digests`; there is deliberately no way to pass
any of them in, because a field the caller can set is decoration rather than
evidence. Boundary guard **G3** (`hooks/synaptory-boundary-guard.sh`) enforces
this at the harness layer by blocking agent write-tool access to
`.synaptory/sync/`, alongside G1 (tracker directory) and G2
(`pipeline-state.json`). If an agent reports that it cannot write the record,
the guard is working: run `declare-ready`.

**3. A non-empty `replay.mismatches` blocks `declare-ready` outright.** It exits
2 with the mismatching story, check and command. Attested evidence that did not
reproduce means the Work Unit **is not done**. Fix it or re-run it, or cut it
from the Cycle (see Scope cut below). Do not push the record and hope the
barrier catches it: discovering a mismatch at Sync wastes the integration slot,
and `collect` will warn that `declare-ready` should have refused.

Record shape (`schema_version` 1.1), abridged:

```
{ "cycle": 3, "workstream": "exec", "branch": "ws/exec", "head_sha": "9f2c1ab…",
  "work_units": {"admitted": 11, "done": 10, "cut": 1},
  "shared_digests": {"contracts/": "sha256:4b81…"},
  "regression": {"command": "bash scripts/sync-regression.sh", "exit_code": 0},
  "dod":    {"tier": "growing", "stories_evaluated": 10, "stories_passed": 10},
  "replay": {"commands_replayed": 41, "checks_unreplayable": 2, "mismatches": []} }
```

`branch` inside the record is a **cross-check only**. `collect` resolves the
branch from the sealed Cycle manifest and warns on disagreement rather than
following the record, so a record can never point the collector at a branch of
its own choosing. Config expansion is only the compatibility fallback for a
pre-manifest Cycle.

---

## Step 1. Collect the readiness records (criterion 1)

```bash
MCP `spq_lifecycle` {"operation": "sync_status", "cycle": "{N}"}
```

`git fetch <remote> --prune`, then for each workstream in the quorum:
`git show <remote>/<branch>:.synaptory/sync/cycle-{N}/<id>.json`. **No merge.**
Workstreams flagged `integration: true` are excluded from the quorum: the
integration clone holds its own spec slot so barrier cost stays out of any
delivery workstream's rollups, but it never declares readiness, it is the thing
readiness is declared *to*.

Read the output before merging anything:

| Field | Meaning | Action |
|---|---|---|
| `records_present: false` | criterion 1 fails | Do not merge yet. Every entry in `missing` names the workstream, branch, path and reason |
| `missing[].reason` = `not found` | not declared, or the branch was never pushed | Ask that workstream to run `declare-ready` and push |
| `missing[].reason` = `malformed JSON` | hand-authored or truncated record | Re-run `declare-ready`; do not repair the file by hand (G3) |
| `missing[].reason` = `record is for cycle X` | stale record from a previous Cycle | Re-run `declare-ready` for `{N}` |
| `warnings[]` mentions branch disagreement | record's `branch` differs from config | Config wins. Fix the config or the override, then re-collect |
| `warnings[]` mentions replay mismatches | a record slipped through with mismatches | Treat that workstream as not ready |

A missing or malformed record fails **here**, before any integration work is
spent. That is the whole point of putting collect first.

---

## Step 2. Human merges into `{INTEGRATION_REF}` (criterion 4)

**The orchestrator does not perform this step.** Present the merge plan and stop.

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  CYCLE {N} SYNC. MERGE PLAN (human step)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Integration branch:  {INTEGRATION_REF}   (from the sealed manifest, NOT dev)
  Promote to:          dev              (human PR, after the barrier clears)

  Ready workstreams:
    ✓ frame    ws/frame     10/10 done   head 4c9e1f2
    ✓ spine    ws/spine      8/9  done   head 77ab301   (1 cut)
    ✓ exec     ws/exec      11/11 done   head 9f2c1ab

  Merge order: frame → spine → exec   (shared_owner first)

  Awaiting your merge. Nothing proceeds until {INTEGRATION_REF} exists.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

Merge the `shared_owner` workstream first: it published the contracts and design
system for this Cycle at `COMMIT`, so landing it first turns any drift into a
conflict on the drifting side rather than a silent overwrite of the governed
version.

**On a merge conflict:** dispatch `se` **in the owning workstream's context**
(see the SE dispatch block below), let that workstream resolve on its own
branch, push, and then **re-run step 1**. Do not resolve a workstream's conflict
from the integration clone: the fix belongs on the branch whose Work Unit
carries the change, or the next Cycle re-creates it.

After the merge, check out the integration branch before step 3. `evaluate`
records criterion 4 by comparing the current branch against the sealed
manifest's `integration_ref`, so a later config edit cannot redirect it. Running
from the wrong branch reports `branches_merged: false` with the exact expected
manifest ref.

---

## Step 3. Evaluate all nine criteria

```bash
# on {INTEGRATION_REF}, copied from the sealed manifest
MCP `spq_lifecycle` {"operation": "evaluate_sync", "cycle": "{N}"}
# exit 0 = green, exit 3 = blocked, exit 2 = refusal (config/cycle error)
```

`evaluate` runs `regression_script`, `digest_script` and `journey_script`
**directly, in the orchestrator's own process, with no hook and no replay
timeout** (§9). It writes a verdict carrying the executed-proof objects per
criterion, plus `blocking[]` naming exactly which criteria failed.

The verdict is cached against the tree state via `verification_cache`, which
already keys on HEAD plus `git status --porcelain` plus `git diff`, so re-running
`evaluate` on an unchanged tree is nearly free and `clear_sync` reuses it. Any
tree change invalidates it. Pass `--no-cache` to force a fresh full run.

Reading a blocked verdict:

| `blocking` entry | What it means | Route to |
|---|---|---|
| `records_present` | step 1 was not clean, or a record went stale | back to step 1 |
| `manifest_agreement` | a record names another Cycle or manifest revision | rehydrate and redeclare that workstream |
| `manifest_closure` | admitted/done/cut identities do not close over the manifest | fix or cut the named Work Unit, then redeclare |
| `branches_merged` | wrong branch or a recorded workstream head is absent | check out `{INTEGRATION_REF}` and merge/fetch the named head |
| `dependency_closure` | an edge is unmet or an incremental unit lacks verified integration | publish/refresh the named event or cut the unit |
| `regression_green` | the cross-workstream suite failed, or the script is missing (`skipped`, which is **unproven, not passed**) | `qe` integration pass, then `se` in the owning workstream |
| `digests_match` | a shared component drifted mid-Cycle | `sa` adjudication (block below) |
| `journey_green` | the increment's end-to-end journey failed or was skipped | `qe` integration pass |

For `digests_match`, `drift[]` names the **workstream and the shared path**, with
`claimed` versus `integrated` digests, so the report says which shared area
moved and who moved it. Shared components are governed at `COMMIT` by the
`shared_owner` (§12), so a delta means a mid-Cycle edit that needs adjudication
rather than a mechanical re-digest. `error:no-tracked-files` on a path is a
**configuration failure, not agreement**: a typo'd or deleted
`shared_digest_path` would otherwise make both sides agree on "nothing" and
criterion 7 would pass vacuously.

`shared-digest.sh` is **one script invoked twice**, by `declare-ready` in the
workstream clone and by `evaluate` here. Never write a second implementation:
two sides computing a digest differently produces a barrier that fails for
reasons nobody can reproduce.

---

## Step 4. Clear the barrier (green only)

```bash
MCP `spq_lifecycle` {"operation": "clear_sync", "approved_by": "{LEAD_EMAIL}"}
```

`clear_sync` re-evaluates (cache-served on an unchanged tree), refuses unless the
verdict is green, records the verdict and cleared sha into the integration
clone's state, emits `evidence_dod / approved` for `CYCLE-{N}`, writes
`SYNC-{N}-barrier.json`, and only then transitions `SYNC → CHECKPOINT`.

Two guards worth knowing about before you fight them:

- `sync_barrier.py clear` never touches `lifecycle_state`. There is exactly one
  writer of that, `spq_state_machine.clear_sync`, which is why the barrier stays
  unit-testable without a state machine. Calling the barrier's `clear` verb
  directly records the verdict but leaves the lifecycle in `SYNC`.
- `transition SYNC → CHECKPOINT` independently requires a green verdict recorded
  for the **current** Cycle. `--force` exists to recover a stuck barrier and
  nothing else. Forcing past it demos from an unintegrated tree, which is the
  entire failure Sync exists to prevent.

---

## Step 5. Human promotes to `dev`

```
  ✓ Cycle {N} Sync barrier GREEN
    9/9 criteria · workstreams: frame, spine, exec · head {sha}
    Receipt: {receipts_dir}/SYNC-{N}-barrier.json

  Your step: open a PR from {INTEGRATION_REF} → dev.
  CHECKPOINT demos from dev, so the demo is blocked on that merge.
```

Human PR, per the Git Safety Rules. Checkpoint reads `dev`, so a green barrier
whose branch was never promoted produces a Checkpoint that demos the previous
Cycle.

---

## Expected, not a defect: the SubagentStop replay warning

**Read this before filing a bug.** The QE integration receipt attests the
cross-workstream regression and the journey suite. When the `SubagentStop` hook
replays that receipt, those two commands are flagged. That is expected and
designed:

| Limit | Where | Effect here |
|---|---|---|
| `SubagentStop` hook budget is **30s total** | `hooks/hooks.json` | A cross-workstream suite cannot finish inside the hook |
| Replay caps are **60s per command, 300s total** | `receipt-schema/evidence-contract.json` | Same limit, independently |
| Unreplayable proof is a warning, not a block | `hooks/synaptory-verify-receipt.sh` | *"recorded exit codes accepted as evidence"* |
| `git` is **not** in the replay allowlist | the same contract | Any git work must live **inside** a committed script |

This is precisely why the barrier is evaluated by `sync_barrier.py evaluate`
outside the hook path, and why **the barrier's verdict, not the receipt's replay
result, is what gates the transition** (§9). A hook-enforced barrier would be a
formality.

Note the asymmetry, because it is the useful half:

- **`bash scripts/shared-digest.sh contracts/` runs in well under a second and IS
  genuinely replayable.** Keep it as a plain-string replay instruction in the QE
  receipt. The hook re-runs it, and a digest that does not reproduce is a real
  finding: fix it, do not waive it.
- **`bash scripts/sync-regression.sh` and `bash scripts/sync-journey.sh` are
  warning-only.** Record them as executed objects (`command` + `exit_code` +
  `summary`), which is proof, and expect the replay to flag them.

Two caveats the shipped code adds to the design's account, so nobody is
surprised:

1. The runner classes a 60s **timeout** as a verification *failure*, not as
   `replayed: false`. In a guided session that is a warning and the recorded exit
   codes stand. In an **autonomous** session `synaptory-verify-receipt.sh` exits
   2 on a verification failure, so the receipt blocks. Sync is a human gate
   (`await_sync`) and is not meant to run autonomously; if it does, the authority
   is `SYNC-{N}-barrier.json`, not the QE receipt's replay result.
2. Because the hook's own budget is 30s, the replay is often cut short before
   the first long command even reaches its 60s cap. Either way the barrier
   verdict is unaffected.

Never "fix" the warning by removing the suites from `verification_commands`, and
never wrap them to shorten the run. Both trade real proof for a quiet hook.

---

## Scope cut: the release valve (§8.6)

`all_or_nothing` is the **ratified default** (Q1). Sync does not clear until
every workstream in the quorum is ready. A workstream that cannot make the
barrier **cuts Work Units from its Cycle** rather than merging after the barrier.

```bash
MCP `spq_cut_work_unit` {"reason": "not integrable for Cycle {N}; returning to backlog", "cycle_id": "{CYCLE_ID}", "manifest_hash": "{MANIFEST_HASH}", "work_unit_id": "{WU-ID}"}
```

The Work Unit is marked `cancelled`, which `aggregate_sprint_dod` already
excludes from quality counts, so the honest `work_units.cut` number reaches the
readiness record. Return it to the backlog in the tracker as well. Then re-run
`declare-ready` in that clone so the record reflects the cut Cycle.

A Work Unit already `done` cannot be cut: cutting it would misreport the Cycle.

Why cut rather than merge late: a partially integrated increment makes the
Checkpoint demonstration dishonest, and scope-cutting is precisely what a
scope-defined Cycle is for. One late workstream pressuring three is **intended**
behaviour, because it surfaces mis-sizing at `COMMIT` where it is cheap.

`mode: per_workstream` remains implementable and relaxes **only** the readiness
quorum, never the integrated-tree criteria. It changes what the Checkpoint demo
means, so do not set it to unblock a single bad week.

---

## Agent dispatch

> Every dispatch below MUST go through the `Agent()` tool. Inline execution
> skips the `SubagentStop` hook, so **no receipt is written and the work never
> reaches `/cost` or `/quality`**. A state whose dispatch block is skipped
> produces invisible work: the barrier's own verdict is recorded, but the
> integration effort that produced it is not.

| Agent | Condition | Receipt |
|---|---|---|
| `qe` | always: the integration full pass | `SYNC-{N}-qe.json` |
| `se` | only on merge conflict, once per affected workstream | `{WU-ID}-se.json` (existing convention) |
| `sa` | only on a shared-digest delta | `SYNC-{N}-sa.json` |
| `ce` | only when the integrated surface changes an auth or data path | `SYNC-{N}-ce.json` |

Model tiers are unchanged: Opus for `sa` / `ce`, Sonnet for `qe` / `se`,
resolved to pinned ids through `backends/model-pins.json`. SPQ introduces no new
tier routing.

### QE, integration full pass (always)

```bash
QE_BACKEND=$(python3 "${PLUGIN_ROOT}/skills/_shared/scripts/backend/backend_config.py" "$(pwd)" "quality-engineer")
# Resolve the receipts dir; never spell it. Inside a Cycle this is the SPQ
# workstream path, not `.orchestrator/receipts/`, and the readiness gate
# resolves the same way, so a hardcoded path is a receipt no gate sees.
RECEIPTS_DIR=$(python3 "${PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" receipts_dir "$(pwd)")
```

> **MANDATORY: Spawn this agent via the `Agent()` tool, do not run the integration pass inline.** Inline execution skips the SubagentStop hook, so no receipt is written and the work never reaches `/cost` or `/quality`. The dispatch must look like `Agent(subagent_type="general-purpose", description="QE Cycle {N} integration pass", prompt=<self-contained prompt per the wrapper>)`: see `${PLUGIN_ROOT}/skills/_shared/backends/${QE_BACKEND}.md`. The QE writes its receipt to `${RECEIPTS_DIR}/SYNC-{N}-qe.json` as its last action. Pass the resolved absolute path into the prompt; do not pass the literal `${RECEIPTS_DIR}`.

**QE prompt context:**
- Cycle `{N}`, the sealed integration branch `{INTEGRATION_REF}`, and its head sha
- The readiness records collected in step 1 (per-workstream `work_units`, `dod`, `regression`)
- `regression_script`, `journey_script` and `digest_script` paths from `sync_barrier.py config`
- The `blocking[]` list from `evaluate`, when re-running after a failure
- DoD intensity: the Cycle's resolved tier

**QE obligations that are specific to this state:**
- The pass runs in the **integration clone**, under the delivery lead's
  principal, never under any workstream's `se` chain. That is what makes v2's
  independent-proof requirement structurally true here rather than aspirational
  (§5 property 3), so do not "save time" by having a workstream verify its own
  merge.
- Any `git` step belongs **inside** a committed script. `git` is not an
  allowlisted `argv[0]`, so `git diff` can never be a `verification_commands`
  entry.
- Report the two long suites as executed objects and the digest as a
  plain-string replay instruction, per the replay section above.

```json
{
  "story_id": "SYNC-3",
  "role": "quality-engineer",
  "backend": "claude",
  "model": "claude-sonnet-4-6",
  "artifacts": [
    "reports/cycle-3-integration.md",
    ".synaptory/quality-engineer/cycle-3-regression.log"
  ],
  "metrics": {
    "workstreams_integrated": 3,
    "tests_passed": 2411,
    "tests_failed": 0,
    "journeys_passed": 4
  },
  "verification_commands": [
    {"command": "bash scripts/sync-regression.sh", "exit_code": 0, "summary": "2411 passed, 0 failed across 3 workstreams"},
    {"command": "bash scripts/sync-journey.sh", "exit_code": 0, "summary": "4 increment journeys green"},
    "bash scripts/shared-digest.sh contracts/"
  ],
  "verification_summary": "Cycle 3 integration pass green on {INTEGRATION_REF}",
  "story_dod": {
    "tests_pass": true,
    "build_succeeds": true,
    "no_critical_findings": true,
    "code_reviewed": false,
    "coverage_no_decrease": true
  },
  "token_usage": {
    "input": 41200,
    "output": 7100,
    "cache_read": 26400,
    "cache_write": 3200,
    "stage": "qe-verification"
  },
  "completed_at": "2026-08-14T11:42:00Z"
}
```

`code_reviewed: false` is deliberate and honest: CR is not a Sync participant,
the per-Work-Unit reviews happened during `CYCLE_EXECUTION`, and the release-depth
review is `ACCEPTANCE`. Report every canonical DoD key explicitly, including
`false`. An omitted key scores as unevaluated on `/quality`, not as passing.

### SE, conflict resolution (only on merge conflict)

One dispatch **per affected workstream**, carrying that workstream's context,
not the integration clone's.

```bash
SE_BACKEND=$(python3 "${PLUGIN_ROOT}/skills/_shared/scripts/backend/backend_config.py" "$(pwd)" "software-engineer")
# Resolve the receipts dir; never spell it. Inside a Cycle this is the SPQ
# workstream path, not `.orchestrator/receipts/`, and the readiness gate
# resolves the same way, so a hardcoded path is a receipt no gate sees.
RECEIPTS_DIR=$(python3 "${PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" receipts_dir "$(pwd)")
```

> **MANDATORY: Spawn this agent via the `Agent()` tool, do not resolve the conflict inline.** Inline execution skips the SubagentStop hook, so no receipt is written and the work never reaches `/cost` or `/quality`. The dispatch must look like `Agent(subagent_type="general-purpose", description="SE conflict resolution {WU-ID} for Cycle {N}", prompt=<self-contained prompt per the wrapper>)`: see `${PLUGIN_ROOT}/skills/_shared/backends/${SE_BACKEND}.md`. The SE writes its receipt to `${RECEIPTS_DIR}/{WU-ID}-se.json` as its last action. Pass the resolved absolute path into the prompt; do not pass the literal `${RECEIPTS_DIR}`.

**SE prompt context:**
- The conflicting paths and both sides of each hunk
- The owning workstream id and the Work Unit(s) whose change conflicts
- Whether either side is a `shared_digest_paths` entry, and who the
  `shared_owner` is. If the conflict is in a governed shared path, the
  `shared_owner`'s version wins and the other side's need becomes an input to
  the shared owner's **next** `COMMIT`, not a mid-Cycle edit
- The resolution lands on the **workstream branch**, then that branch is pushed
  and step 1 re-runs

Conflicts reuse the existing `{WU-ID}-se.json` convention rather than a
Sync-specific id, because the work is Work-Unit work that happens to have been
discovered at the barrier.

### SA, shared-digest adjudication (only on a digest delta)

```bash
SA_BACKEND=$(python3 "${PLUGIN_ROOT}/skills/_shared/scripts/backend/backend_config.py" "$(pwd)" "solution-architect")
# Resolve the receipts dir; never spell it. Inside a Cycle this is the SPQ
# workstream path, not `.orchestrator/receipts/`, and the readiness gate
# resolves the same way, so a hardcoded path is a receipt no gate sees.
RECEIPTS_DIR=$(python3 "${PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" receipts_dir "$(pwd)")
```

> **MANDATORY: Spawn this agent via the `Agent()` tool, do not adjudicate the drift inline.** Inline execution skips the SubagentStop hook, so no receipt is written and the work never reaches `/cost` or `/quality`. The dispatch must look like `Agent(subagent_type="general-purpose", description="SA shared-digest adjudication Cycle {N}", prompt=<self-contained prompt per the wrapper>)`: see `${PLUGIN_ROOT}/skills/_shared/backends/${SA_BACKEND}.md`. The SA writes its receipt to `${RECEIPTS_DIR}/SYNC-{N}-sa.json` as its last action. Pass the resolved absolute path into the prompt; do not pass the literal `${RECEIPTS_DIR}`.

**SA prompt context:**
- The `drift[]` array from `evaluate`: workstream, shared path, `claimed` vs `integrated`
- The version the `shared_owner` published at `COMMIT` for this Cycle
- The Work Units on each drifting workstream that touched the path

**SA decides one of three, and says which:**
1. The integrated tree is correct: the drifting workstream re-pins and re-runs `declare-ready`.
2. The drift is a genuine contract change: it becomes an input to the shared
   owner's next `COMMIT`. The Work Unit that needs it is **cut from this Cycle**.
3. The delta is explicitly accepted for this Cycle. Record the accepted digest
   and the reason in `SYNC-{N}-sa.json`, and log it as a `criterion_waived`
   MethodSignal at Checkpoint. Use this sparingly: it is the mechanism by which a
   governed shared component stops being governed.

   > `evaluate` honours an `accepted_digest_deltas` map (path → accepted digest)
   > when one is present in the resolved config, but the shipped yaml-lite parser
   > does not currently surface that key from `.synaptory.yaml`, so option 3
   > cannot be applied by editing config today. Prefer option 1 or 2 and raise
   > the parser gap with the maintainers rather than forcing a green verdict.

### CE, integrated-surface review (conditional)

Dispatch when the integrated surface changes an **auth or data path**: new or
changed authentication, authorisation, session handling, PHI or PII flow, an
encryption boundary, or a data-retention behaviour. Two workstreams can each
ship a benign change whose composition is not benign; this dispatch exists for
that composition, which no single workstream's `ce` pass could have seen.

```bash
CE_BACKEND=$(python3 "${PLUGIN_ROOT}/skills/_shared/scripts/backend/backend_config.py" "$(pwd)" "compliance-engineer")
# Resolve the receipts dir; never spell it. Inside a Cycle this is the SPQ
# workstream path, not `.orchestrator/receipts/`, and the readiness gate
# resolves the same way, so a hardcoded path is a receipt no gate sees.
RECEIPTS_DIR=$(python3 "${PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" receipts_dir "$(pwd)")
```

> **MANDATORY: Spawn this agent via the `Agent()` tool, do not run the security review inline.** Inline execution skips the SubagentStop hook, so no receipt is written and the work never reaches `/cost` or `/quality`. The dispatch must look like `Agent(subagent_type="general-purpose", description="CE integrated auth/data surface Cycle {N}", prompt=<self-contained prompt per the wrapper>)`: see `${PLUGIN_ROOT}/skills/_shared/backends/${CE_BACKEND}.md`. The CE writes its receipt to `${RECEIPTS_DIR}/SYNC-{N}-ce.json` as its last action. Pass the resolved absolute path into the prompt; do not pass the literal `${RECEIPTS_DIR}`.

**CE prompt context:**
- The integrated diff restricted to auth and data paths, per workstream
- Which workstreams contributed each change (composition is the point)
- `healthcare.baa_enforced` and any PHI-bearing paths from the project config

A critical CE finding on the integrated surface blocks the Cycle. Route the fix
to the owning workstream, or cut the Work Unit.

---

## Receipt ledger for this state

| Receipt | Author | `story_id` | `stage` |
|---|---|---|---|
| `SYNC-{N}-qe.json` | QE subagent | `SYNC-{N}` | `qe-verification` |
| `SYNC-{N}-sa.json` | SA subagent, conditional | `SYNC-{N}` | `sa-architecture` |
| `SYNC-{N}-ce.json` | CE subagent, conditional | `SYNC-{N}` | `ce-compliance` |
| `{WU-ID}-se.json` | SE subagent, per conflict | the Work Unit id | `se-implementation` |
| `SYNC-{N}-barrier.json` | **the tool**, not an agent | `SYNC-{N}` | `orchestrator` |

`SYNC-{N}-barrier.json` is written by `sync_barrier.clear` and carries
`role: "orchestrator"`. Do not hand-write it, and do not give it a `-orch`
suffix: orchestrator-authored receipts use a **descriptive filename suffix**
because `orchestrator` has no entry in the contract's `role_abbrevs` map, the
same convention as `INCEPTION-design.json`. `SYNC-{N}` satisfies the required
`^[A-Z][A-Z0-9]*-\d+$` story-id pattern.

---

## Tracker mirror

Mirror barrier status to a "Sync `{N}`" issue with one sub-item per workstream.
The tracker is a **mirror, never the source of truth**.

```bash
MCP `spq_lifecycle` {"operation": "sync_status", "cycle": "{N}"}
```

`status` is read-only and runs no proof scripts, so it is safe to poll while
waiting on workstreams. It reports `ready_count` against `quorum` plus each
workstream's `work_units` and `declared_ready_at`.

---

## Exit

On green and after `clear_sync`, the lifecycle is `CHECKPOINT`:

```
→ Read("${PLUGIN_ROOT}/skills/synaptory/spq/checkpoint.md")
```

Remind the lead that Checkpoint demos from `dev`, so step 5's PR is a
prerequisite for the demo rather than a follow-up.
