# Commit (SPQ)

> **Event:** Commit -- it **opens** a Cycle, and it is not a stage
> **Stage change:** `DISCOVERY -> CYCLE`, or `CYCLE -> CYCLE`, or `ACCEPTANCE -> CYCLE`
> **Participants:** Engagement Lead (the goal), Engineering Lead (the breakdown, and the one accountable owner of this Cycle), Quality Assurance, Solution Architect (conditional), Orchestrator
> **Output:** one **sealed declaration** -- the admitted set, closed; each unit's path scope; the source region; the trunk; the barrier criteria, published in advance
> **Human gate:** YES. The Engineering Lead approves the breakdown and every admitted unit clears readiness before build begins.

Commit is where a Cycle's set is **fixed**. Nothing joins after it: the admitted set is a property of a hash-sealed document, not of an event anybody could omit, and there is no verb that appends a unit to an open Cycle.

**Sizing here carries more weight than a sprint plan does.** A sprint ends when the clock runs out; a Cycle ends when its admitted Work Units are done, and a Cycle that cannot finish **cuts work** rather than running long. Over-admitting does not produce a late Cycle -- it produces a Cycle that reaches its barrier with work cut. Mis-sizing surfaces at the barrier, where it is expensive; it is cheap to fix here.

**The Commit event does not cause any of this.** `open_cycle` seals the declaration and records a `commit` event as a consequence. Delete the event record and the Cycle is still open; delete the seal and there is no Cycle.

---

## Step 0. Read what the last Cycle measured

A Cycle is sized against **recorded history**, not against what the team believes it can hold. Read the closed Cycles off the board and measure them:

```bash
SPQ_LIB="${PLUGIN_ROOT}/hooks/lib" python3 - <<'PY'
import json, os, sys
sys.path.insert(0, os.environ["SPQ_LIB"])
import cycle_measurement as measure, spq_state_machine as spq

board = spq.read_state(os.getcwd())
history = measure.history_report(board.get("cycles_completed") or [])
print(json.dumps(history, indent=2)[:4000])
PY
```

Read the answer literally. **`available: false` is a real answer and never a zero.** A first Cycle has no history and says so, with `problems` naming why; a Cycle whose archive is incomplete says that instead of reporting a rate nobody can reproduce. Neither is permission to invent a number.

| What history says | What this Commit may cite |
|---|---|
| `available: true` | the measured throughput and cut-rate, the Cycle ids, the sample size and the observation window |
| `available: false`, no Cycle has closed | the Discovery calibration sample plus **an explicit bootstrap assumption**, named as such |
| `available: false`, Cycles have closed, `problems` names unrecorded acceptance | neither, and the archive is NOT what is wrong. `SC-MTH-015` credits a Work Unit only once an accountable human accepted it as verified, so a Cycle that delivered and recorded no acceptance has no throughput to cite. Record the acceptance at Checkpoint (`spq/checkpoint.md` step 3, per unit, by a named human); it cannot be back-filled by an agent |
| `available: false`, Cycles have closed, any other reason | neither. Fix the archive, or state that the plan is unsupported by data -- a bootstrap while real Cycles exist is ignoring what was measured, and `assert_plan_supported` refuses it |

Then read the last Checkpoint's report for where the method chafed: a criterion routinely close to failing, a unit class that keeps getting cut, an admitted set that was consistently too large. That is process signal, and Commit is where it changes behaviour.

---

## Adaptive intensity

The DoD tier deepens as the system matures, and `next_action` reports the resolved tier on every dispatch:

| Cycles closed | Tier | Checks |
|---|---|---|
| 0-1 | `early` | tests pass, build succeeds |
| 2-3 | `growing` | + no critical findings, code reviewed |
| 4+ | `mature` | + coverage does not decrease |
| an Acceptance is in view | `release` | maximum depth |

`quality.dod_tier` in `.synaptory.yaml` overrides the computed tier. Announce the tier here so agents know the gate expectations up front:

```bash
MCP `spq_lifecycle` {"operation": "set_dod_tier", "decided_by": "<the deciding human>", "reason": "<why this tier for this Cycle>", "tier": "{TIER}"}
```

A project may **add** a check or raise a threshold. It may not remove one the method declares.

---

## Step 1. The goal, and one Engineering Lead

The Engagement Lead sets the Cycle goal from the specification. The Engineering Lead owns the Cycle and the Crew executing it.

**Exactly one Engineering Lead per Cycle.** N Leads means N concurrent Cycles, each on its own cadence, each closing with its own demonstration. If two people would own this one, that is two Cycles with two declarations and two non-overlapping regions -- not one Cycle with two owners.

**The Crew grants nothing.** It is a list of names: who is executing this Cycle now, re-seatable between Cycles. It is not an authorization boundary, it is not an identity anything is keyed on, and re-seating one changes no barrier admission. `crew` is a list on the declaration and the seal refuses anything else, because a table with permissions in it is the thing the separation exists to prevent.

---

## Step 2. Refine and admit the Work Units (project-owner skill)

> **SKILL INVOCATION, not a dispatch.** No gate reads a `project-owner` receipt at Commit, so the orchestrator retrieves the contract and refines in its own context. The receipt it writes MUST name `token_usage.stage: "pro-brd"`.

```
synaptory skills get project-owner
synaptory skills get project-owner/guides/refinement
```

Pull the candidates -- from the baseline for a first Cycle, from the tracker and the previous Cycle's cuts afterwards. **Units cut from an earlier Cycle returned to the backlog and are re-admitted here**, which is what a cut is for.

```bash
TRACKER_CLI="python3 ${PLUGIN_ROOT}/skills/_shared/scripts/tracker/tracker_cli.py --project-dir $(pwd)"
${TRACKER_CLI} get-backlog --status ready
```

**Admission is the readiness moment.** Each admitted Work Unit clears readiness before build begins, which is what stops a Cycle opening on work nobody has sized. The declaration is validated as a whole and **every problem is reported at once** -- an author fixing six problems in six round trips stops reading the messages by the sixth.

### What the sealed declaration requires

Read the requirements from the code rather than from this table, and read them *before* you write the JSON. This prints every problem the seal would refuse, without writing anything:

```bash
SPQ_LIB="${PLUGIN_ROOT}/hooks/lib" python3 - <<'PY'
import json, os, sys
sys.path.insert(0, os.environ["SPQ_LIB"])
import cycle_records as records

declaration = json.load(open("cycle-declaration.draft.json", encoding="utf-8"))
problems = records.problems(declaration)
print("\n".join("  - " + p for p in problems) if problems
      else "this declaration would seal; barrier criteria: "
           + ", ".join(records.BARRIER_CRITERIA))
PY
```

Draft it as `cycle-declaration.draft.json` and iterate against that command until it is clean. The Cycle-level fields:

| Field | Required because |
|---|---|
| `cycle_id` | omit it and `open_cycle` allocates one; supply one only to reproduce an id |
| `repository` | a Cycle addresses **one** repository, and a declaration naming none cannot be refused for spanning two |
| `trunk_ref` | closing integrates to one shared trunk; without a named trunk there is nothing to integrate into |
| `baseline_ref` | filled from the approved baseline. A cut must not move it, which needs it named |
| `goal` | a Cycle is demonstrated against its goal |
| `engineering_lead` | exactly one, and it is an accountability rather than a permission |
| `source_region` | two concurrent Cycles are kept apart by declaration rather than by coordination, so a Cycle declaring no region cannot be checked against another's |
| `admitted_units` | non-empty. The barrier ranges over the set Commit fixed, and an empty set makes every barrier vacuous |
| `shared_path_owners` | present even when empty. An omitted map cannot be told apart from a forgotten one, and `shared_paths_owned` would then be evaluating a field it cannot distinguish from a gap |
| `barrier_criteria` | published here so the Cycle sizes against them now instead of discovering them at close |

#### The region is reserved before the declaration is sealed

A declared region separates nothing on its own. Two clones each hold a sealed
declaration the other cannot see until somebody pushes, so `open_cycle` claims
the region from the control plane's registry **before** sealing, and there is
exactly one answer it proceeds on:

| The registry says | Commit |
|---|---|
| granted | seals, recording `region_reservation: {registry: "control-plane"}` |
| a live Cycle already holds an overlapping region | **refused**, naming that Cycle |
| nothing -- unreachable, unauthenticated, timed out | **refused.** "I could not ask" is not "no collision" |
| this project has no `project_id` | **refused.** A claim cannot be scoped without one |

**So a Cycle cannot be opened offline, or from a project the control plane does
not know.** That is deliberate. The registry is the only party that sees two
clones in time, and a mode where the requirement does not apply is a mode where
concurrency has no basis -- two clones of the same unregistered repository both
lack a `project_id`, both get the same answer, and would both seal the same
region. Missing identity is an inability to *address* the registry, never
evidence that no peer exists.

If the Commit refuses with `registry_unavailable`, run `synaptory whoami`
before re-running it; `synaptory cycles regions list` shows what is held.

**A Commit that refuses after reserving gives the region back.** Re-admission
links and other post-seal checks can still fail, and a stray reservation would
have the next Commit refused for a collision with a Cycle that does not exist.

The claim is rechecked when work is **dispatched**, because a reservation can
be released afterwards and a second Cycle can then legally claim the region
this one is still working in. A dispatch is refused when the registry names
another holder, when this Cycle's grant is gone, and when the registry cannot
be reached at all -- an unanswerable recheck is not a confirmation. A Cycle
whose declaration records no grant is refused for the same reason: its
separation was never established.

The region goes back at Checkpoint. See `checkpoint.md` §6.6.

Per Work Unit:

| Field | Required because |
|---|---|
| `id` | unique in the set, and a **safe path segment** (`^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`). Ids become event and receipt filenames, so an id carrying `/` or `..` would place a Cycle's records outside its own directory |
| `kind` | one of `feature`, `story`, `bugfix`, `task`, `release`. An unrecognised kind is rejected rather than defaulted, because the kind decides which fields are required |
| `title` | what it is |
| `acceptance_criteria` | non-empty, no blank entries. Nothing can judge a unit without them, and the barrier would have nothing to evaluate |
| `path_scope` | **non-empty.** See below |
| `acceptance_cases` | the authored cases, written **before** any code exists |
| `depends_on` | only where a real edge exists |
| `execution_order` | only for an intersecting pair. See below |

### `path_scope` is not optional, and an absent one is not "no files"

Every admitted unit declares the part of the source it may address, in the grammar `spq/discovery.md` documents. An absent scope is **refused at admission** rather than treated as disjoint: compared against anything it would read as "addresses nothing", which is how a unit becomes concurrent-safe with the whole repository.

**Disjointness is evaluated separately from dependency satisfaction and never inferred from it.** Two Work Units with no edge between them may still address one file, and a dependency check cannot see it -- and the failure that prevents, two changes silently diverging on one file and reconciled by whoever merges last, is not recoverable from evidence afterwards.

**An intersecting pair is legal, sequentially.** The refusal is admission to a *concurrent* execution set, so two units whose scopes intersect need a distinct `execution_order` each:

```json
{"id": "WU-201", "path_scope": ["src/api/"], "execution_order": 1}
{"id": "WU-202", "path_scope": ["src/api/routers/"], "execution_order": 2}
```

Without distinct orders the seal refuses the pair by name. With them the pair is admitted and the **dispatch** gate keeps it sequential: a concurrent dispatch onto an intersecting scope is refused with `scope_collision`, and it compares the sealed declaration rather than the board.

Those are two refusals at two moments and neither substitutes for the other. Commit cannot know when a unit is actually running; dispatch cannot see the whole admitted set's shape.

### A shared path has exactly one owner

A path outside every declared region -- a contract, a schema, a shared library, the build configuration -- is declared with its one owning unit:

```json
"shared_path_owners": [
  {"path": "contracts/ingest.openapi.yaml", "owning_unit_id": "WU-201"}
]
```

The seal refuses a shared path with no owner, an owner whose own `path_scope` does not cover it, and a path covered by two units. **Two owners is the same unrecoverable merge as none.** State the map even when it is empty -- an empty list is a position, a missing key is not.

Those three are about ONE declaration. A fourth refusal is about two: **a path an open Cycle already holds.** A shared path lies outside every source region by definition, so the region reservation does not cover it -- two Cycles could each claim `web/src/main.tsx`, each pass its own `shared_paths_owned`, and both seal, at which point the second merge overwrites the first and neither declaration can still be changed (`#827`). Commit compares this map against the declarations committed into this checkout and refuses a collision, naming both units and both Cycles. Resolve it by withdrawing the unit, handing the path to the Cycle that holds it, or waiting for that Cycle to Checkpoint, which releases the claim.

**A clean Commit is not proof there is no collision.** That comparison sees only what this checkout has fetched, and it records its own scope in the seal as `shared_path_check`, where `registry: "none"` means no registry was consulted. A Cycle sealed on a branch you have not fetched is invisible to it, so on a multi-machine engagement fetch first.

### Dependencies: what an edge may wait for

An edge names a **verifiable condition** rather than merely "done", because `done` is the producer's own claim about itself while the others are facts another unit can check.

```json
"depends_on": ["WU-201"]
"depends_on": [{"unit_id": "WU-201", "condition": "contract_published"}]
```

A bare id waits for `done`, the weakest condition. The conditions are `done`, `contract_published`, `artifact_published`, `integrated`, `environment_ready`, and anything else is refused as unverifiable. `cycle_integrated` is gone: it was the cross-Cycle condition, and there is no parent Cycle to wait on now that unorderable work is co-admitted to one.

**An edge naming a unit this Cycle did not admit is refused.** Work that cannot be ordered cannot be split: co-admit the pair to one Cycle, or consume a *published, versioned* artifact of it at this Cycle's own Commit.

**A mutual pair is not a loop to reject.** Two units that depend on each other inside one declaration are the atomic case, already co-admitted -- which is exactly what the method asks for.

**A condition stronger than `done` needs a verifier sealed with it.** `done` resolves off the board. `contract_published` and `artifact_published` are proven by a ledger event whose declared digest the resolver **recomputes**, so the declaration must name the script that computes it:

```bash
python3 core/lib/spq_state_machine.py open_cycle \
  --goal "..." --units units.json \
  --verification '{"digest_script": "scripts/digest.sh"}'
```

The script takes the output `id` as its one argument and prints the digest on stdout. It is sealed **inside the declaration hash**, so the digest that admits an edge cannot be recomputed later by a different script. A declaration carrying a digest-verified edge and no `verification.digest_script` is **refused at Commit**, as is a script path escaping the project -- because a verifier nobody can run is the same silent pass as no verifier, which is what this cost the predecessor.

Publishing a digest that does not match what the script recomputes is refused outright; the event never lands. After it does land, the consumer clears in two more steps that are deliberately human: **commit and push** `.synaptory/cycles/` (until then `dep_status` says `dep_event_local_only`, not "nothing published"), then `refresh_ledger` in the reading clone (until then it says `dep_ledger_stale`). Three distinguishable codes rather than one "blocked", because the operator's next action differs in each.

### A design-pending unit is not admitted

For each candidate tagged `[DESIGN-PENDING]`, follow the Design Grooming Protocol at `.synaptory/.protocols/design-grooming.md`:

1. If a design preview exists under `.synaptory/design/`, generate a scoped handoff bundle from the relevant prototype section.
2. Otherwise create a targeted prototype using the unit's acceptance criteria as the brief.
3. Store the bundle at `.synaptory/design/{unit-id}-design.md` and carry that path into the orientation pack (Step 8) so every dispatch sees it. `tracker_cli.py` has **no field-update verb**, so if the reference belongs on the ticket, set it in the tracker UI.
4. Remove the tag.
5. **A unit still tagged `[DESIGN-PENDING]` is not admitted.** A unit whose screen nobody has designed cannot have testable acceptance cases, and admitting it moves the design decision inside the barrier.

### Authored acceptance cases, before any code exists

Each admitted unit declares the cases the verifying stage will execute:

```json
"acceptance_cases": [
  {"id": "AC-1", "statement": "POST /ingest with a valid signature returns 202", "criterion_ref": "AC-1"},
  {"id": "AC-2", "statement": "POST /ingest with no signature returns 401", "criterion_ref": "AC-2"}
]
```

A case id must match `^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$` and every case needs a statement: a verifying receipt reports its per-case outcome under that exact key, so an id the two sides cannot spell identically is a case nothing can prove, and an id with no statement behind it is one a prover can trivially mark passed.

**Where cases genuinely cannot be authored yet, declare the gap.** A unit that states a position (`"acceptance_cases": []`) and declares nothing is refused: `open_cycle` refuses admission with `authored_cases_absent` before it allocates an identity or writes anything, because a Cycle that admits a unit with no criteria has already lost the test-first ordering and no later check can restore it.

```json
"criteria_gap_declared": {"reason": "the third-party contract is unpublished; QE authors the cases at verification",
                          "declared_by": "<who declared it>", "declared_at": "<iso8601>"}
```

A gap with no `reason` is not a declaration. The Cycle then records the gap on the board, on every dispatch payload and on the DoD result, so a Cycle running without test-first is *visibly* running without it rather than indistinguishable from one that is not.

---

## Step 3. Architecture review (conditional, architecture skill)

Run this when an admitted unit trips an architecture trigger -- a new entity, a new service, a new integration, a security requirement, a performance requirement -- or on the periodic health check.

> **SKILL INVOCATION, not a dispatch.** `solution-architect` is on no receipt-gated edge in this lifecycle, so no SPQ ceremony dispatches it.

```
synaptory skills get solution-architect
```

Output: the ADRs this Cycle needs, and any contract change that has to be **owned by one admitted unit** rather than edited by several.

---

## Step 4. Approve the scope (human gate)

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  COMMIT                                     Cycle {N}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Goal             {CYCLE_GOAL}
  Engineering Lead {LEAD}
  Crew             {names} -- grants nothing
  Repository       {REPO}   Trunk {TRUNK_REF}
  Source region    {paths}  · no overlap with {other open Cycles}
  Admitted         {N} Work Units, all with a declared path scope
  Intersecting     {N} pairs, each with a distinct execution_order
  Shared paths     {N}, each with exactly one owner
  Authored cases   {N}/{N} units · {N} declared gaps
  DoD tier         {tier}
  Barrier criteria {read from cycle_records.BARRIER_CRITERIA -- printed, not restated}
  Sized against    {measured history: cycles {ids}, throughput {v}, cut-rate {v}, window {w}}
                   {or: bootstrap from the Discovery calibration sample -- no Cycle has closed}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Options:
  1. Commit this scope (Recommended)
  2. Show the declaration
  3. Reduce the admitted set
  4. Chat about this
```

**Print the barrier criteria; do not type them.** They come from one constant, and restating a list in prose is how three different criteria counts came to ship in one product -- eight documented, nine printed, five emitted by two hosts. The check in Step 2 prints them, and after Commit the sealed declaration carries them.

**The Cycle sizes against those criteria here.** Every one has to be met at the close, together, over the whole admitted set. If the Cycle cannot plausibly clear all of them with this set, the set is too large -- and reducing it now costs a conversation, while discovering it at the barrier costs the close.

---

## Step 5. Bind the tracker

The tracker mirrors the Cycle; it is never its source. A cycle renamed or renumbered in the tracker cannot repoint a Cycle, because identity lives in the sealed declaration.

**TWO NUMBERS, AND THEY ARE NOT THE SAME NUMBER (#793).** `cycle_seq` is Synaptory's allocation counter. `tracker_ref` is this Cycle's name in the tracker's namespace, sealed at Commit. Use each for its own purpose:

| Use | Which number | Why |
|---|---|---|
| Tracker calls -- `create-sprint`, `get-sprint-backlog` | **`tracker_ref`** | The tracker numbers a BODY OF WORK |
| Pseudo Work Unit id for receipts -- `CYCLE-{CYCLE_SEQ}` | **`cycle_seq`** | Synaptory's own identity; changing it moves every receipt path |

They coincide only until they do not, and three ordinary things separate them **permanently**: a body of work re-delivered (one tracker cycle, two sequences), Cycles opened concurrently (sequences land in Commit order, not tracker order), and a Cycle abandoned before close (which still spends a sequence). No offset corrects the gap.

Read the sealed value rather than assuming it:

```bash
TRACKER_REF=$(MCP `get_state`

${TRACKER_CLI} health-check
${TRACKER_CLI} list-sprints
echo '{"number":'"${TRACKER_REF}"',"goal":"{CYCLE_GOAL}"}' | ${TRACKER_CLI} create-sprint
```

SPQ adds no tracker adapter surface: a Cycle maps onto the existing sprint interface, and `get-sprint-backlog ${TRACKER_REF}` is how a clone reads it back.

**`create-sprint` is adopt-or-create.** An existing empty milestone with this number is adopted (that is what a re-delivery looks like); one that already holds Work Units is **refused**, because adopting it would merge two Cycles' admitted sets into one backlog.

**If `tracker_ref` is empty** the Cycle predates `#793` -- `open_cycle` now refuses to seal without it on `github`, `jira`, `teamwork` and `linear`. Do **not** substitute `CYCLE_SEQ` silently: `get-sprint-backlog` with the wrong number SUCCEEDS and returns another Cycle's Work Units, with no error and a well-formed result. Confirm the number against the plan with the operator first.

**There is no verb that assigns a unit to a cycle.** `tracker_cli.py` exposes no field-update verb at all, and the remote adapters restrict `update_story()` to a fixed field allow-list that would silently drop the rest -- so moving tickets into the cycle is a tracker-UI action. Do not invent a verb for it; the Cycle's own authority is the sealed declaration, and the tracker is a mirror of it.

---

## Step 6. Open the Cycle

One command seals the declaration, projects the board, and records the Commit event.

```bash
MCP `spq_lifecycle` {"operation": "open_cycle", "goal": "{CYCLE_GOAL}", "repository": "{OWNER/REPO}", "trunk_ref": "{TRUNK_REF}", "engineering_lead": "{LEAD}", "crew": ["se", "qe", "cr"], "source_region": ["src/ingest/", "src/api/routers/ingest.py"], "shared_path_owners": [{"path": "contracts/ingest.openapi.yaml", "owning_unit_id": "WU-201"}], "admitted_units": [{"id": "WU-201", "kind": "feature", "title": "Ingest accepts a signed envelope", "acceptance_criteria": ["AC-1 a signed envelope is accepted", "AC-2 an unsigned envelope is refused with 401"], "path_scope": ["src/ingest/", "contracts/ingest.openapi.yaml"], "acceptance_cases": [{"id": "AC-1", "statement": "POST /ingest with a valid signature returns 202", "criterion_ref": "AC-1"}, {"id": "AC-2", "statement": "POST /ingest with no signature returns 401", "criterion_ref": "AC-2"}]}]}
```

`--units` and `--source-region` are JSON. `--goal`, `--repository`, `--trunk-ref` and `--engineering-lead` are strings. `barrier_criteria` and `baseline_ref` are **not** arguments: the criteria come from the one constant, and the baseline comes from the approved Discovery record -- neither is a value a Commit may choose.

The result names what was sealed:

```json
{"ok": true, "cycle_id": "3-a91f0c2e", "cycle_seq": 3,
 "declaration_hash": "sha256:…", "admitted_unit_ids": ["WU-201", "WU-202"]}
```

**Record the `cycle_id` and the `declaration_hash`.** Every later mutation names them, and a mutation whose declared identity is not the current one is refused rather than applied to whichever Cycle happens to be open.

### What `open_cycle` refuses, and what each refusal means

| Refusal | Meaning |
|---|---|
| a baseline is not approved | Discovery has not ended. The baseline is the line a cut must not move |
| `DISCOVERY -> CYCLE` is not legal from here | the engagement is `COMPLETE`. The final Acceptance handed it over, and there is nothing to reopen |
| `authored_cases_absent` | a unit stated `acceptance_cases: []` and declared no gap. Author the cases or declare the gap with a reason |
| the declaration cannot be sealed, with a list | every problem at once. Fix them together and re-run |
| `transport_ignored` | `.synaptory/cycles/` is gitignored, so this declaration would be invisible to every other clone while writing cleanly here |

**A refusal leaves nothing on disk.** Admission is checked before the identity is allocated and before anything is written, so a refused Commit is re-runnable rather than a half-opened Cycle to recover.

---

## Step 7. Commit and push the transport

The sealed declaration reaches other clones through git, and nothing else does:

```bash
git add .synaptory/cycles/{CYCLE_ID}/manifest.json
git commit -m "spq: commit Cycle {CYCLE_ID} -- {N} Work Units admitted"
```

**Ask before committing and before pushing.** Then every other clone joins the Cycle by hydrating onto it, which verifies the seal before adopting anything:

```bash
MCP `spq_lifecycle` {"operation": "hydrate_cycle", "cycle_id": "{CYCLE_ID}"}
```

---

## Step 8. The orientation pack

Write one file the Crew reads instead of re-deriving the Cycle:

```markdown
# Cycle {CYCLE_ID}

## Goal
{CYCLE_GOAL}   Engineering Lead: {LEAD}   Trunk: {TRUNK_REF}

## Admitted Work Units
| id | kind | path scope | depends on | order | authored cases |

## Source region
{paths}. Outside it: shared paths and their one owner each.

## The barrier this Cycle sized against
{the criteria, printed from the sealed declaration}

## Commands
next_action · begin_dispatch · advance · bind_receipt · dep_status · cut_work_unit

## Conventions
{test command, lint command, the regression named in the baseline}
```

**State the shared paths as ask-first in every `se` dispatch prompt**: *"`{paths}` are owned by `{unit}` for this Cycle. Do not edit them. If your Work Unit needs a change there, stop and report it as an input to the next Commit."*

**That protection is prompt-level, and you must not overstate it.** No boundary guard enforces it during execution. What *is* enforced is the barrier's `shared_paths_owned` criterion at the close, and the dispatch gate's `scope_collision` refusal when two units' declared scopes intersect -- both of which are cheaper than a merge and more expensive than asking.

---

## Receipt ledger for this event

The QE Cycle test specification is a dispatch and writes its own receipt; the refinement and architecture steps are skill invocations whose receipt the orchestrator writes inline. Resolve every path with `bind_receipt` under the pseudo Work Unit id `CYCLE-{CYCLE_SEQ}`.

```json
{
  "story_id": "CYCLE-3",
  "role": "orchestrator",
  "accountable_role": "project-owner",
  "backend": "claude",
  "model": "{model_id_used}",
  "artifacts": [".synaptory/cycles/3-a91f0c2e/manifest.json", "docs/cycles/cycle-3.md"],
  "metrics": {"units_admitted": 5, "units_readmitted": 1, "authored_cases": 14},
  "verification_commands": [
    {"command": "python3 -m json.tool .synaptory/cycles/3-a91f0c2e/manifest.json", "exit_code": 0, "summary": "sealed declaration parses"},
    "test -s .synaptory/cycles/3-a91f0c2e/manifest.json"
  ],
  "token_usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "stage": "pro-brd"},
  "completed_at": "{iso8601_utc_timestamp}"
}
```

An **executed object** (`command` + `exit_code` + `summary`) is proof; a **plain string** is a replay instruction the SubagentStop hook re-runs, and is not proof. Both must be replayable -- no command substitution, no shell pipeline, no `python3 -c` payload -- because a command the hook cannot re-run is a claim.

`{model_id_used}` and `{iso8601_utc_timestamp}` are placeholders to substitute with real values. Never copy a literal model id out of a prompt: `/cost` would price the step at a rate nothing ran at.

---

## What Commit does not do

| Activity | Commit |
|---|---|
| Add a unit to an open Cycle | No. The set is closed at Commit, and there is no verb that appends one |
| Grow the set by revising the declaration | No. A revision may narrow, correct a scope or record a cut; a revision that admitted a unit would be late admission with a reason attached |
| Assign units to a lane | No. There are no lanes. A unit belongs to the Cycle that admitted it |
| Enable parallelism | No. Concurrency is a consequence of the declared scopes, not a switch |
| Choose the barrier criteria | No. They come from one constant. A project may add one or raise a threshold; removing one the method declares is refused at the seal |
| Move the baseline | No. That is a re-baseline, an accountable action the client agrees to first |
