# Checkpoint (SPQ)

> **Event:** Checkpoint -- it records that a Cycle closed, and what it integrated
> **The barrier is what closes the Cycle.** The event is the record; the barrier is the condition
> **Participants:** Engineering Lead (the promotion and the close), Quality Assurance (the acceptance), Engagement Lead (the feedback disposition), Technical Writer, stakeholders
> **Output:** the demonstrated Cycle integrated to the shared trunk, a barrier verdict, a promotion record, a close record, and the Checkpoint event
> **Human gate:** YES, twice. The demonstration and acceptance are human; the trunk promotion is a human act that no agent performs.

At Checkpoint a Cycle's work is **demonstrated and accepted against the business intent**, client feedback is captured and dispositioned by kind, and **the admitted result integrates to the shared trunk**. That is why the trunk is always a working build, why a Cycle can be demonstrated the moment it closes, and why nothing downstream has to gather what integration deferred.

**Two things are separated here and must stay separated.**

| | What it is | What it does |
|---|---|---|
| **The barrier** | a mechanical transition condition over the set admitted at Commit | evaluates published criteria and **fails closed**. It is what permits the close |
| **The Checkpoint event** | a record | says the close happened, with the revision it integrated |

The test for whether an implementation kept them apart: **if deleting the event record would change whether a Cycle may close, the event has become a gate.** Reading Checkpoint as a gate is the most common way to get SPQ wrong, because it invites a second approval path beside the one that actually authorizes release.

**A failed demonstration is method signal, not a system failure.** What the platform saw was Work Units and their proofs, and those results stand.

---

## Prerequisites

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" next_action "$(pwd)" --cycle-id {CYCLE_ID}
```

`next_action` answers `close_cycle` once every admitted Work Unit is `done` or cut. Until then it names the unit still running: a Cycle does not reach its barrier with work in flight, it either finishes that work or **cuts** it.

Read the Cycle's own numbers off the board and the declaration, never from memory:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" read "$(pwd)" --cycle-id {CYCLE_ID}
```

Three counts matter and they are three different things. Print all three, labelled:

| Count | Where it comes from | Why it is not the others |
|---|---|---|
| **admitted at Commit** | the sealed declaration | the cut-rate denominator. It must not shrink after a cut |
| **cut** | `.synaptory/cycles/{id}/cuts.json` -- the committed record | the board's `cancelled` state is agent-writable; the committed record is the authority, and it is the only copy a clone that did not make the cut can read |
| **effective set** | admitted minus recorded cuts | the barrier's subject |

---

## Step 1. Cut what cannot finish (the one valve)

All-or-nothing over the admitted set is what makes a Cycle an independently demonstrable outcome rather than a partial merge. It is also what would make one late Work Unit a Cycle-wide failure -- which is why it comes with exactly one valve.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" cut_work_unit "$(pwd)" {UNIT_ID} \
  --reason "<why this unit cannot make this Cycle>" \
  --cut-by "<the Engineering Lead's identity>" \
  --cycle-id {CYCLE_ID} --manifest-hash {DECLARATION_HASH}
```

The unit leaves the Cycle with a recorded reason and returns to the backlog; it is re-admitted at a **later** Commit, linked to this cut by the `readmission_key` the command returns. Without that link, "cut and re-admitted" and "cut and forgotten" are the same record.

Four refusals, and each was a real failure:

| Refused | Because |
|---|---|
| an empty reason | a cut nobody explained is indistinguishable from work quietly dropped |
| a finished unit | a cut removes **unfinished** work. Cutting a done unit rewrites what the Cycle delivered |
| a unit this Cycle never admitted | never admitted is backlog, not a cut, and counting it would inflate the cut-rate |
| a changed baseline digest | **a cut does not move the baseline.** It is a scheduling act inside the commitment; moving the line is a re-baseline, which the client agrees to first. The digest is asserted before and after every cut rather than trusted |

**Cutting is not one of the four change dispositions.** Correction, absorb, swap and re-baseline are the Engagement Lead's naming of a change; a cut is the Engineering Lead's scheduling act inside the baseline. Confusing them makes a slipped Cycle look like a renegotiated commitment, or worse, the reverse.

**The cut record is committed.** Push it, or a clone running the barrier computes an effective set that still contains work somebody withdrew:

```bash
git add .synaptory/cycles/{CYCLE_ID}/cuts.json
```

**Cut code must not be in the candidate.** The barrier's `admitted_set_closed` criterion reports any changed path owned exclusively by a cut unit, including partial code already produced. Remove it from the candidate before evaluating.

---

## Step 2. Demonstrate the Cycle

Demonstrate from the **candidate built on the observed trunk**, not from a Work Unit branch and not from the previous close. The customer demonstration is layered on the per-unit proofs; it does not replace them.

Show, per admitted unit, what its acceptance criteria said and what its proof recorded. Then say what the evidence actually supports and no more:

- a unit whose authored cases all reported a pass is **proven against those cases**;
- a unit admitted with a declared criteria gap is **verified at whatever depth QE authored**, and the gap belongs in the report;
- a cut unit is **not delivered**, and naming it as deferred rather than delivered is the whole point of recording the cut.

---

## Step 3. Record the acceptance, per unit, by a named human

**This step is what credits throughput, and an agent cannot do it.** A change counts as delivered only once an accountable human has accepted it as verified, so an agent reporting `done` credits nothing: the barrier drops any acceptance naming the automated principal and records the reason.

Collect, for each unit in the effective set, the human who accepted it and when. Those become the `acceptance` blocks in the facts file below, and from there the Cycle's recorded throughput.

Quality Assurance owns this judgment -- the acceptance criteria verified against, the verification metrics, the release-readiness call. It is an accountability, not a permission.

---

## Step 4. Capture the feedback, and name each item's kind

Human business feedback may create new work **without invalidating** the per-unit technical proofs already recorded, and it does not authorize shipment by itself.

> **SKILL INVOCATION, not a dispatch.** No gate reads a `project-owner` receipt at Checkpoint, so the orchestrator retrieves the contract and captures the feedback in its own context. The receipt it writes MUST name `token_usage.stage: "pro-brd"`.
>
> ```
> synaptory skills get project-owner
> ```

The Engagement Lead names which kind each item is. One person names it, because naming a re-baseline as an absorb is how a commitment quietly stops being one:

| Kind | What it is | The commitment |
|---|---|---|
| **Correction** | the work does not yet meet the bar. Routine fixes plan into an upcoming Cycle; a critical failure -- production down, a security or compliance breach -- **interrupts** rather than queueing | unchanged |
| **Absorb** | a refinement inside the agreed intent, folded into a coming Cycle | holds |
| **Swap** | new scope exchanged for committed work of **equal or smaller** size, with a recorded sizing basis | holds |
| **Re-baseline** | too large to absorb or swap. A new scope, timeline and cost are agreed with the client **before that work begins**, while work already authorized continues | **moves -- only here, and only by agreement** |

Nothing admitted to this Cycle changes as a result. Feedback is an input to the **next** Commit.

---

## Step 5. The Technical Writer report

```bash
TW_BACKEND=$(python3 "${CLAUDE_PLUGIN_ROOT}/skills/_shared/scripts/backend/backend_config.py" "$(pwd)" "technical-writer")
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/advance_kernel.py" bind_receipt "$(pwd)" CHECKPOINT-{N} tw
```

Dispatch the Technical Writer to write the Cycle report: goal, admitted set, what was demonstrated, what was cut and why, the barrier verdict, the integrated revision, and the feedback with each item's named kind.

> **MANDATORY: Spawn this agent via the `Agent()` tool.** Inline execution skips the SubagentStop hook, so no receipt is written and the work never reaches `/cost`. The dispatch looks like `Agent(subagent_type="synaptory:technical-writer", description="TW Cycle report", prompt=<self-contained prompt per the wrapper>)` -- see `${CLAUDE_PLUGIN_ROOT}/skills/_shared/backends/${TW_BACKEND}.md`. Pass the resolved absolute path from `bind_receipt` into the prompt; do not pass the literal variable.

**This receipt is not a gate, and do not tell a user it is.** The predecessor refused the close without `CHECKPOINT-{N}-tw.json`; there is no such refusal now, and inventing one would be exactly the second approval path `C-02` forbids. Write the report because a Cycle with no record of what it demonstrated has no audit trail, not because something blocks.

---

## Step 6. The barrier, and the trunk-integration transaction

Seven ordered steps. The order is the design: each one exists because skipping it produced a specific failure.

> **The barrier's verdict comes from `run_barrier`; promotion and close still drive the module.** `cycle_barrier` returns records and deliberately writes nothing -- a verdict stored where the deciding agent can edit it is a verdict the agent reached twice -- so the promote and close blocks drive the shipped module and store its records under the Cycle's committed transport. Set `SPQ_LIB="${CLAUDE_PLUGIN_ROOT}/hooks/lib"` in each. None of them writes lifecycle state; the durable board write is `close_cycle` at the end, through the verb that validates it.

### 6.1 Assemble the facts

The barrier takes **facts** and derives every criterion verdict itself. There is
no argument through which a caller can supply a criterion result, and that
absence is proved rather than documented.

**So do not assemble the facts yourself.** An earlier version of this ceremony
had you write a `barrier-facts.json` carrying `unit_results[...].
acceptance_criteria[...] = {"passed": true}` and `proofs.regression =
{"passed": true}`, transcribed from commands you had run. Those two fields ARE
criterion results and a proof, hand-authored by the principal the barrier is
judging -- the caller-supplied-verdict hole, moved one layer out of the module
and into the ceremony. The module's own guarantee cannot close a hole outside
it.

### 6.2 Evaluate

`run_barrier` derives every fact and returns the verdict. It takes none:

```bash
${CLAUDE_PLUGIN_ROOT}/hooks/lib/spq_state_machine.py run_barrier "$PWD"
```

What it derives, and where each comes from:

| Fact | Derived from |
|---|---|
| `unit_results` | `evaluate_story_dod` per admitted unit, one verdict per criterion the declaration named. A criterion the gate does not itemise passes only when the unit is `done` and every check the gate HAS passed, and is marked as derived so a reader can tell it from an itemised one |
| `cuts` | the committed cut record, so the effective set is admitted minus recorded cuts |
| `proofs.regression` | the `spq.regression_script` the project configured, **executed**. Absent, missing, skipped and errored are all unmet |
| `trunk` | `git rev-parse` / `merge-base`, including `observed_sha` so "the trunk moved" is observable |
| `changed_paths` | `git diff --name-only observed..candidate`, which reports both sides of a rename |

`changed_paths` is not only read for cut-code exclusion. `admitted_set_closed`
also requires **every changed path to be inside the declaration** -- the
Cycle's `source_region`, some effective unit's `path_scope`, or a declared
shared path. A coherent declaration is not a candidate that respected it, and
the region registry cannot catch this either: the registry keeps two *Cycles*
apart and says nothing about whether this one stayed inside the region it was
granted. The runtime's own `.synaptory/cycles/` transport is exempt -- every
Checkpoint commits it and no declaration claims it.
| `opened_at` | the Commit event |

Read the result. `green` is the verdict; `unmet` names every criterion that
did not pass and why.

**`accepted_units` is read off the board, and it is empty until a human
accepts.** `SC-MTH-015` credits a change as delivered only once an accountable
human has accepted it as verified, so a unit an agent marked `done` credits
nothing and a block naming the automated principal is dropped with the reason
recorded. The board carries `accepted_by` / `accepted_at` when acceptance was
recorded through the shipped path; `run_barrier` copies that and **synthesises
nothing** -- inventing the accountable human is the one thing the throughput
number exists to refuse.

So `accepted_units` being shorter than `effective_unit_ids` is not a barrier
failure. `acceptance_criteria_met` is what the barrier gates on; acceptance
credit is what the Cycle's throughput is measured from, and the two are
deliberately different questions.

**Report the criteria from `published_criteria`, which the sealed declaration carries. Never type the list.** Restating it in prose is the defect that shipped three different counts in one product: eight documented, nine printed, five emitted by two hosts. The verdict names every criterion it evaluated and its own result, so `{met}/{len(published_criteria)}` is a fact rather than a claim.

Three properties of the verdict are worth stating to the operator:

- **Closure is by identity, never by count.** Three lanes each declaring "11 admitted" once satisfied a barrier while having admitted three different sets of eleven. The count is never compared; the set always is.
- **A criterion with no evaluator is unmet.** A project may add a criterion; adding one nothing evaluates must not be cheaper than meeting it.
- **`criteria_all_returned` is itself a criterion**, computed over the others. Without it, "the barrier evaluated nothing" and "the barrier evaluated everything and passed" are the same verdict -- both an empty list of failures.

**Before promotion, `trunk_integrated` is legitimately the one unmet criterion.** The candidate is not on the trunk yet. Every *other* criterion must be met to promote; all of them must be met to close.

**If anything else is unmet, this Cycle does not close.** There is no partial admission and no per-unit promotion: one failing retained unit promotes nothing. The valve is Step 1 -- cut, re-evaluate, and close with the verified remainder.

### 6.3 Promote (human, atomic, once)

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" promote_cycle "$(pwd)" \
  --principal "<principal>" --rationale "<why>"
```

One verb, not a heredoc assembling a ledger: the promotion reads the latest
verdict from the barrier ledger, prints `reconcile` first, appends the
promotion record, and returns the operation identity. A ceremony that had the
operator load the ledger and pick the verdict out of it made the *choice of
verdict* the operator's, which is the choice the operation identity exists to
remove.

This **authorizes** one promotion of one exact candidate. It does not merge: the merge is the authorized human-controlled mechanism, and the git rules forbid an agent performing it. What is enforced is that no other candidate, no moved trunk and no second promotion can hide behind that authorization.

- **A moved trunk is refused.** If the candidate was built on `X` and the trunk is now at `Y`, rebuild the candidate on `Y` and re-evaluate. A verdict bound to `X` is not evidence about `Y`.
- **The principal is a named human.** `integrate-to-trunk` is a high-risk action and never continues automatically; the accountable role is derived from the action, not chosen by the caller.
- **A retry of the same operation reconciles rather than promoting twice.** The operation identity is *derived* from the Cycle, the declaration, the candidate and the observed trunk -- a freshly generated id would make every retry a new operation by construction, which is how a candidate gets merged twice.

`reconcile` is printed first on purpose. After a crash, run it and follow its `action` -- `promote`, `close`, `complete` or `rebuild` -- rather than resuming from what this session believes happened. The process that crashed is exactly the one whose idea of its own progress cannot be trusted.

**Then the human merges the candidate into the trunk**, by the mechanism this repository uses. Ask before opening or merging anything.

### 6.4 Observe the trunk update, and re-evaluate

The promotion authorized a merge; only the trunk says whether it landed.
**Run 6.2 again** -- `run_barrier` re-observes git, so `trunk_integrated`
becomes met on the observation rather than on an edit you made. There is
nothing to update by hand, which is the point: a `candidate_is_ancestor_of_
trunk` you can set to `true` is an integration you can claim.

If the candidate is *not* an ancestor of the trunk, the promotion did not land: `reconcile` answers `rebuild`, and reusing this verdict would report an integration that never happened.

### 6.5 Record the close

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" close_cycle "$(pwd)" \
  --principal "<principal>" --rationale "<why>"
```

**The close takes no verdict and no trunk revision.** It derives both: it
re-runs the barrier over *admitted minus recorded cuts*, executes the
regression, observes git for the trunk state, and then requires a **successful
promotion under that verdict's own operation identity** to exist. There is no
argument through which a caller can supply any of it.

That is a deliberate replacement of a check that read as strong and was not.
The verb used to take `--barrier-verdict` and `--integrated-sha` and refuse a
verdict that was not green, named no criteria, or came from another Cycle -- so
a shaped JSON object satisfying those three closed a Cycle whose barrier had
never run, and the shape was guessable from the refusals themselves. Validating
a claim is not deriving the fact, and `M-03` asks for the second.

| Refused | Because |
|---|---|
| no recorded promotion | a close records an integration somebody authorized. Without one, a staging branch becomes the demonstrated final result -- the one thing a staging branch may never be |
| a barrier that is not green | a Cycle that cannot meet its barrier cuts work; it does not close on a red one |
| a candidate that is not an ancestor of the trunk | the promotion did not land. `reconcile` answers `rebuild`, and closing here reports an integration that never happened |
| a promotion under a different declaration revision | a revision changes the set the barrier ranges over, so an older promotion is green about a different commitment |
| `--barrier-verdict` or `--integrated-sha` | refused **by name** rather than ignored: a caller passing them is following the retired sequence, and silently succeeding would leave them believing they had supplied something |

A second close of the same Cycle **reconciles** rather than raising, and writes
nothing twice: a crash between the merge and the local record is the case the
operation identity exists for, and a retry that refused would leave a promoted
Cycle permanently unclosable.

`integrated_at` is taken from the promotion record, not from this moment, so
lead time ends at the observed trunk integration rather than absorbing the
bookkeeping gap.

### 6.6 The region goes back

The close releases this Cycle's source-region reservation, so the next Cycle
can claim it. A release that fails does **not** fail the close -- the work is
already integrated, and refusing here would strand the trunk behind a registry
outage. This is the one place in the Cycle where an unanswered registry is not
a refusal: Commit and dispatch both refuse, because there the missing answer is
about whether anyone else holds the region. The failure mode is a stale reservation that refuses the next
overlapping Commit *by name*, which the operator clears by hand:

```bash
synaptory cycles regions release --cycle-id {CYCLE_ID}
```

A release names a Cycle and nothing else: `(project, cycle)` identifies the
one live row. It is restricted to the **holder**, the **Engineering Lead named
on the declaration**, or a **project admin** -- releasing frees the region for
another Cycle to claim, so it is the other half of the same guardrail. If
somebody else closed the Cycle, hand them this command.

`synaptory cycles regions list` shows what is held right now.

### 6.7 Push the transport

```bash
git add .synaptory/cycles/{CYCLE_ID}/
git commit -m "spq: checkpoint Cycle {CYCLE_ID} -- integrated {SHORT_SHA} to {TRUNK_REF}"
```

Ask before committing and before pushing. **Nothing integrates behind a closed barrier**: no closed Cycle publishes additional code later, and a promotion after the close would put work on the trunk that no barrier ever ranged over.

---

## Step 7. Where the Cycle goes next

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" next_action "$(pwd)" --cycle-id {CYCLE_ID}
```

Two legal moves, and closing the Cycle chose neither of them for you:

| Next | Command | When |
|---|---|---|
| another Cycle | `Read("${CLAUDE_PLUGIN_ROOT}/skills/synaptory/spq/commit.md")` | delivery continues. `CYCLE -> CYCLE` is a legal self-transition -- closing one Cycle and opening the next is delivery continuing, not a stage change |
| a go-live | `transition "$(pwd)" ACCEPTANCE` | this integrated result is being released |

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" transition "$(pwd)" ACCEPTANCE
```

`ACCEPTANCE` is refused while any admitted unit is neither done nor cut, naming each one: a Cycle that cannot finish cuts work, it does not carry it forward silently.

---

## The Checkpoint summary

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  CHECKPOINT                        Cycle {CYCLE_ID}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Goal          {CYCLE_GOAL}
  Admitted      {N} at Commit        Cut {N}        Effective {N}
  Barrier       {met}/{published} criteria met · {green | unmet: …}
  Accepted      {N} units, by {named humans}
  Candidate     {short sha} → {trunk_ref} at {integrated_at}
  Feedback      {N} items · {corrections} / {absorbs} / {swaps} / {re-baselines}
  Sync events   {N} (zero is normal)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

Every number here comes from a record: the counts from the declaration, the cut file and the verdict; the acceptance count from the verdict's `accepted_units`; the Sync count from `method_events`. **Report zero as zero and unavailable as unavailable.** A measurement nobody could take is not a zero, and the archive is written so that the two have different shapes.

---

## Receipt ledger for this event

`tw` is a dispatch and writes its own receipt; the feedback capture is a skill invocation whose receipt the orchestrator writes inline. Both use the pseudo Work Unit id `CHECKPOINT-{N}`, where `{N}` is the Cycle sequence.

```json
{
  "story_id": "CHECKPOINT-3",
  "role": "technical-writer",
  "backend": "claude",
  "model": "{model_id_used}",
  "artifacts": ["docs/cycles/cycle-3-report.md"],
  "metrics": {"work_units_reported": 5, "work_units_cut": 1, "criteria_met": 7},
  "verification_commands": [
    {"command": "make regression", "exit_code": 0, "summary": "412 passed, 0 failed on the candidate"},
    "test -s docs/cycles/cycle-3-report.md"
  ],
  "token_usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "stage": "tw-docs"},
  "completed_at": "{iso8601_utc_timestamp}"
}
```

An **executed object** (`command` + `exit_code` + `summary`) is proof: the command ran and its exit code was recorded, and only this form scores `tests_pass` or `build_succeeds`. A **plain string** is a replay instruction the SubagentStop hook re-runs, and is not proof. Both must be replayable -- no command substitution, no shell pipeline, no `python3 -c` payload -- because a command the hook cannot re-run is a claim rather than evidence.

`{model_id_used}` and `{iso8601_utc_timestamp}` are placeholders to substitute with real values. Never copy a literal model id out of a prompt: `/cost` would price the step at a rate nothing ran at.

---

## What Checkpoint does not do

| Activity | Checkpoint |
|---|---|
| Gate the release | No. It integrates and records; the authority that lets something ship resolves at Acceptance, and nowhere else |
| Admit late work | No. The set was closed at Commit, and nothing integrates behind a closed barrier |
| Promote part of the set | No. All-or-nothing over the effective set, at promotion and at close |
| Merge | No. The promotion is authorized here and performed by a human |
| Move the baseline | No. A cut leaves the commitment where it was; a re-baseline is a separate accountable action |
| Run a retro | There is no retro event. Process learning goes into the Technical Writer report and is read at the next Commit |
