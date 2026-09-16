# Acceptance (SPQ)

> **Stage:** `ACCEPTANCE` -- one go-live, and it **repeats**
> **Participants:** Quality Assurance (the readiness decision), Engagement Lead (the commitment), QE, CE, PE, TW, CR, the client
> **Output:** one audit-ready evidence package, one release-readiness decision bound to it, and -- on the final Acceptance only -- the recorded handover
> **Human gate:** YES. Ship / do not ship, decided by a named human.
> **Next stage:** `CYCLE` for a non-final release, `COMPLETE` for the final one

Acceptance validates delivered software against the commitment. The client confirms it meets the agreed requirements; outstanding feedback, security remediation and performance tuning are closed out against the baseline.

**A long engagement ships several times, so Acceptance repeats.** Each go-live carries its own evidence package and its own readiness decision. Only the **final** Acceptance additionally ends the engagement, by handing over the codebase, the technical and operational documentation, and the knowledge the client's team needs to run what was built.

**Delivery does not stop for it.** Other Cycles may keep running: a release does not wait for an unrelated open Cycle. What it evaluates is a **pinned integrated release candidate**, not "whatever the trunk is".

**The package is compiled, not assembled.** Every record in it was produced by the work as it ran. An evidence record dated *after* this Acceptance opened is refused as retrospective -- a proof written afterwards proves the release, not the work.

---

## Prerequisites

Acceptance is reached from a closed Cycle:

```bash
MCP `spq_lifecycle` {"operation": "enter_acceptance"}
```

That transition refuses while any admitted Work Unit is neither `done` nor cut, naming each one. A Cycle that cannot finish cuts work; it does not carry it forward into a release.

Then read what this Acceptance still owes:

```bash
MCP `spq_lifecycle` {"operation": "acceptance_status"}
```

```json
{"ready": false,
 "present": ["quality-engineer"],
 "missing": ["code-reviewer", "compliance-engineer", "platform-engineer", "technical-writer"],
 "final": false,
 "next_stage": "CYCLE"}
```

**Record the moment this Acceptance opened.** The package is keyed on it, and it is what separates a record the work produced from one written for the release. Take it from the board's `lifecycle_history` entry for `ACCEPTANCE`.

`{N}` in every receipt id below is the Cycle sequence this Acceptance releases -- the `current_cycle` on the board, which `close_cycle` leaves at the last closed Cycle.

---

## The five evidence dispatches

All five are **release depth** -- the deepest DoD tier -- and all five are **gate-required**: `acceptance_status` reports `ready: false` until each receipt exists at its exact path. No skill invocation substitutes for any of them.

Resolve each path rather than spelling it:

```bash
python3 "${PLUGIN_ROOT}/hooks/lib/advance_kernel.py" bind_receipt "$(pwd)" ACCEPTANCE-{N} qe
```

`next_action` names the next missing one for you:

```bash
MCP `next_action`
```

| Role | Receipt | What it must prove |
|---|---|---|
| `qe` | `ACCEPTANCE-{N}-qe.json` | the full regression on the pinned candidate, at release depth, with a per-case outcome for every authored case in the release |
| `ce` | `ACCEPTANCE-{N}-ce.json` | the security, privacy and compliance review of the integrated surface, against the versioned threat model |
| `pe` | `ACCEPTANCE-{N}-pe.json` | production infrastructure and operational readiness, with the environment checks **executed** rather than described |
| `tw` | `ACCEPTANCE-{N}-tw.json` | the complete documentation set: what was built, how it is operated, what is known to be missing |
| `cr` | `ACCEPTANCE-{N}-cr.json` | the final read-only review of the release candidate |

> **MANDATORY: Spawn this agent via the `Agent()` tool**, for every one of the five. Inline execution skips the SubagentStop hook, so no receipt is written, `acceptance_status` stays `ready: false`, and the work never reaches `/cost` or `/quality`. Each dispatch names its installed role, for example `Agent(subagent_type="synaptory:quality-engineer", description="QE release regression", prompt=<self-contained prompt per the wrapper>)` -- see `${PLUGIN_ROOT}/skills/_shared/backends/${QE_BACKEND}.md` and its siblings. Pass the resolved absolute path from `bind_receipt` into each prompt; do not pass the literal variable.

Resolve each role's backend before writing its prompt. A dispatch prompt that does not state the backend is a dispatch that assumes one:

```bash
QE_BACKEND=$(python3 "${PLUGIN_ROOT}/skills/_shared/scripts/backend/backend_config.py" "$(pwd)" "quality-engineer")
CE_BACKEND=$(python3 "${PLUGIN_ROOT}/skills/_shared/scripts/backend/backend_config.py" "$(pwd)" "compliance-engineer")
PE_BACKEND=$(python3 "${PLUGIN_ROOT}/skills/_shared/scripts/backend/backend_config.py" "$(pwd)" "platform-engineer")
TW_BACKEND=$(python3 "${PLUGIN_ROOT}/skills/_shared/scripts/backend/backend_config.py" "$(pwd)" "technical-writer")
CR_BACKEND=$(python3 "${PLUGIN_ROOT}/skills/_shared/scripts/backend/backend_config.py" "$(pwd)" "code-reviewer")
```

**Depth is not deferred to this gate.** Every one of these checks ran per Work Unit as the work finished; what happens here is the release-depth pass over the *integrated* result. Regulated depth in particular may never be saved up for a final ceremonial gate -- if a project's supported depth cannot cover a requirement, that **reduces the supported scope** rather than passing silently.

---

## Step 1. Compile the evidence package

```bash
SPQ_LIB="${PLUGIN_ROOT}/hooks/lib" python3 - acceptance-facts.json <<'PY'
import json, os, sys
sys.path.insert(0, os.environ["SPQ_LIB"])
import acceptance_record as acceptance, spq_state_machine as spq

facts = json.load(open(sys.argv[1], encoding="utf-8"))
board = spq.read_state(os.getcwd())
closes = [c for c in board.get("cycles_completed") or []
          if str(c.get("cycle_id")) in set(facts["cycle_ids"])]
package = acceptance.compile_evidence(
    acceptance_id=facts["acceptance_id"],
    trunk_digest=facts["trunk_digest"],
    opened_at=facts["opened_at"],
    cycle_closes=closes,
    integrated_shas_on_trunk=facts["integrated_shas_on_trunk"],
    evidence=facts["evidence"],
)
json.dump(package, open("acceptance-package.json", "w", encoding="utf-8"), indent=2)
print(json.dumps({"acceptance_id": package["acceptance_id"],
                  "trunk_digest": package["trunk_digest"],
                  "cycles": [c["cycle_id"] for c in package["cycles"]],
                  "evidence": len(package["evidence"]),
                  "package_digest": package["package_digest"]}, indent=2))
PY
```

`acceptance-facts.json`:

```json
{
  "acceptance_id": "REL-2026-09-1",
  "trunk_digest": "<the pinned trunk revision this release is about>",
  "opened_at": "<when this Acceptance entered ACCEPTANCE, from lifecycle_history>",
  "cycle_ids": ["3-a91f0c2e", "4-77b1de03"],
  "integrated_shas_on_trunk": ["<every revision observed on the trunk>"],
  "evidence": [
    {"id": "ACCEPTANCE-4-qe", "kind": "verification", "digest": "sha256:…",
     "recorded_at": "2026-09-07T14:02:11Z"}
  ]
}
```

Get the observed revisions from git rather than from a claim:

```bash
git fetch origin main
git rev-parse origin/main                      # trunk_digest
git log --format=%H origin/main -n 200         # integrated_shas_on_trunk
```

Three refusals, and each is one the method names:

| Refused | Because |
|---|---|
| a Cycle close with no integrated revision | it is an **unintegrated candidate**, and a release compiled from one demonstrates a staging branch as the final result |
| a close whose integrated revision is not observed on this trunk | either that work is not in this candidate or this candidate is not the trunk. Both make the package a claim about something else |
| evidence recorded after `opened_at` | a retrospective proof. Every entry needs a `recorded_at`, because evidence that cannot be dated is indistinguishable from evidence written for the release |

**A release may compile several closed Cycles**, and nothing requires a particular count or that no other Cycle is open. The `package_digest` covers the Acceptance identity and the trunk digest together, so a package cannot be re-presented for a different go-live.

---

## Step 2. The release-readiness decision

```bash
SPQ_LIB="${PLUGIN_ROOT}/hooks/lib" python3 - "<principal>" "<outcome>" "<rationale>" <<'PY'
import json, os, sys
sys.path.insert(0, os.environ["SPQ_LIB"])
import acceptance_record as acceptance

package = json.load(open("acceptance-package.json", encoding="utf-8"))
decision = acceptance.readiness_decision(
    package=package, principal=sys.argv[1], outcome=sys.argv[2], rationale=sys.argv[3])
acceptance.assert_decision_binds(decision, package=package)
json.dump(decision, open("acceptance-decision.json", "w", encoding="utf-8"), indent=2)
print(json.dumps({k: decision.get(k) for k in
                  ("action", "role", "principal", "outcome", "risk",
                   "subject", "subject_digest", "rationale")}, indent=2))
PY
```

The outcome is one of a closed set -- `continue`, `escalate`, `ask-client`, `block`, `rework` -- and **there is deliberately no `pass` and no `approve`**: `continue` is a decision somebody is named for, and the absence of a decision is not one of the five.

Release readiness is **Quality Assurance's** action and the role is derived from the action, never chosen by the caller. `decide-release-readiness` is high risk, so it never continues automatically and it refuses the automated principal outright: a machine cannot be the accountable human here.

**`assert_decision_binds` is not optional.** It refuses an approval reused for another digest or another go-live, which is a natural failure rather than an exotic one: a release slips, the candidate is rebuilt, and last week's approval is still sitting there looking like an approval. It approved a different revision. If it refuses, rebuild the candidate and decide again.

---

## Step 3. Is this the final Acceptance?

**"Final" is not a flag anyone sets.** It is a property of the engagement's state: no outstanding commitments, and a recorded handover. A caller-set flag would let a mid-engagement release end the engagement.

```bash
SPQ_LIB="${PLUGIN_ROOT}/hooks/lib" python3 - <<'PY'
import json, os, sys
sys.path.insert(0, os.environ["SPQ_LIB"])
import acceptance_record as acceptance, spq_state_machine as spq

board = spq.read_state(os.getcwd())
outstanding = list(board.get("outstanding_commitments") or [])
handover = board.get("handover") or {}
target = acceptance.acceptance_target(outstanding_commitments=outstanding, handover=handover)
print(json.dumps({
    "outstanding_commitments": outstanding,
    "handover_recorded": acceptance.handover_recorded(handover),
    "handover_items_required": list(acceptance.HANDOVER_ITEMS),
    "next_stage": target,
}, indent=2))
PY
```

The handover is an **AND of three items**, not a count -- `codebase`, `documentation`, `operating_knowledge`. An engagement closed without the operating knowledge has handed over a codebase nobody can run.

> **There is no verb that records the handover or the outstanding commitments.** Both live on the engagement, and only `approve_baseline` writes there today, so `acceptance_status` reports `final: false` on every Acceptance and `next_stage: CYCLE`. That makes the check above a **prompt-level** guard: `transition COMPLETE` is a legal edge and does not consult it. So run `assert_close_permitted` yourself before taking that edge, and do not tell the user the lifecycle refused a premature close -- it would not have.

```bash
SPQ_LIB="${PLUGIN_ROOT}/hooks/lib" python3 - <<'PY'
import os, sys
sys.path.insert(0, os.environ["SPQ_LIB"])
import acceptance_record as acceptance, spq_state_machine as spq

board = spq.read_state(os.getcwd())
try:
    acceptance.assert_close_permitted(
        requested_target="COMPLETE",
        outstanding_commitments=list(board.get("outstanding_commitments") or []),
        handover=board.get("handover") or {},
    )
    print("this Acceptance may close the engagement")
except acceptance.AcceptanceError as exc:
    print("REFUSED:", exc)
PY
```

---

## Step 4. The release gate

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ACCEPTANCE                          {ACCEPTANCE_ID}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Candidate      {trunk_ref} @ {short trunk_digest}, pinned
  Cycles         {ids} · {N} accepted Work Units
  Evidence       {N}/5 role receipts · {N} compiled records
  Package        {package_digest}
  Quality        regression {…} · security {…} · infra {…} · docs {…} · review {…}
  Open items     {N} outstanding commitments
  Handover       {codebase | -} {documentation | -} {operating_knowledge | -}
  This release   {FINAL -- closes the engagement | non-final -- delivery continues}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Options:
  1. Ship it
  2. Do not ship -- {escalate | ask-client | block | rework}
  3. Show the evidence package
  4. Chat about this
```

**Print `{N}/5` from `acceptance_status`, not from memory.** Its `present` and `missing` lists are the readiness answer, and they are read from the files on disk.

---

## Step 5. On "ship it"

1. **Record the decision** (Step 2) with `outcome: continue` and a named principal. Everything below depends on it existing.

2. **Deploy**, by the mechanism the project uses. **Ask before pushing, tagging or deploying anything.** Integration to the trunk already happened at each Cycle's Checkpoint; this is the release of an already-integrated revision.

3. **Move the stage.** Two edges, and which one is legal is decided in Step 3:

   ```bash
   # a non-final go-live: delivery continues
   MCP `spq_lifecycle` {"operation": "enter_cycle"}
   ```

   ```bash
   # the FINAL go-live: the engagement is handed over and closed
   MCP `spq_lifecycle` {"operation": "complete"}
   ```

   `ACCEPTANCE -> CYCLE` is what makes Acceptance repeatable, and it is the normal edge. `COMPLETE` is terminal in both directions: nothing transitions out of it, and `open_cycle` refuses on it too, because the final Acceptance ended the engagement and there is nothing to return to.

4. **On a non-final release**, load `${PLUGIN_ROOT}/skills/synaptory/spq/commit.md` and open the next Cycle. Delivery continues from the same trunk.

---

## Step 6. On "do not ship"

Record the decision with the outcome that is true -- `escalate`, `ask-client`, `block` or `rework` -- and its rationale. **A refusal is a recorded decision, not an absence of one**, and the difference is what an auditor reads later.

Then name each blocking item's kind, exactly as at Checkpoint. The Engagement Lead owns the naming:

| Kind | Where the work goes |
|---|---|
| **Correction** | an upcoming Cycle. A critical failure -- production down, a security or compliance breach -- interrupts rather than queueing |
| **Absorb** | a coming Cycle, inside the agreed intent |
| **Swap** | exchanged for committed work of equal or smaller size, with the sizing basis recorded |
| **Re-baseline** | a new scope, timeline and cost agreed with the client **before that work begins**. Work already authorized continues while the new line is set |

Write the disposition's own fields to `change.json` and name it:

```bash
SPQ_LIB="${PLUGIN_ROOT}/hooks/lib" python3 - change.json <<'PY'
import json, os, sys
sys.path.insert(0, os.environ["SPQ_LIB"])
import acceptance_record as acceptance

record = acceptance.name_change(**json.load(open(sys.argv[1], encoding="utf-8")))
print(json.dumps(record, indent=2))
PY
```

Each disposition asserts what it promises, so each needs its own fields -- a label that does not check its effect on the commitment is not a naming:

| Disposition | `change.json` carries |
|---|---|
| `correction` | `severity`: one of `production-down`, `security-breach`, `compliance-breach` (interrupting) or `defect`, `regression`, `usability`, `documentation` (routine). Guessing here decides whether a production outage waits for a Cycle |
| `absorb` | `baseline_digest_before` and `baseline_digest_after`, asserted equal -- the commitment holds |
| `swap` | `scope_added` and `scope_removed`, each a list of `{unit_id, size}` with a positive size, plus a `sizing_basis` and the unchanged baseline digests. Removed size must be **equal or larger** than added -- an unsized side makes "equal or smaller" unfalsifiable |
| `re-baseline` | `client_agreed_at` before `work_started_at`, plus `new_scope_unit_ids` and `authorized_unit_ids`. New scope may not begin before the client agreed; already-authorized work continues |

The four are a closed set and **a cut is not one of them**: a cut removes unfinished Work Units *inside* the baseline, and a re-baseline moves the baseline.

Then return to delivery with `transition CYCLE` and admit the resulting work at the next Commit. Do not admit it here: the admitted set of a closed Cycle is closed.

---

## Step 7. The handover (final Acceptance only)

Three items, all required. Compile them from what the work produced:

| Item | What it is |
|---|---|
| `codebase` | the repository, the trunk revision released, and the access the client's team needs |
| `documentation` | the technical and operational documentation -- the `tw` receipts across every Cycle are its provenance |
| `operating_knowledge` | how to run, monitor, deploy and recover what was built |

Record all three against the engagement, then take the `COMPLETE` edge. Print one line naming the released revision, the accepted Work Unit count and the handover items, so the last thing in the log is what was actually handed over.

---

## Receipt ledger for this stage

Five dispatches, five receipts, all under the pseudo Work Unit id `ACCEPTANCE-{N}` where `{N}` is the Cycle sequence this release is compiled from. Every one is gate-required: `acceptance_status` reads the files on disk and reports `ready: false` until all five exist.

```json
{
  "story_id": "ACCEPTANCE-4",
  "role": "quality-engineer",
  "backend": "claude",
  "model": "{model_id_used}",
  "artifacts": ["docs/release/REL-2026-09-1-regression.md"],
  "metrics": {"cases_executed": 214, "cases_failed": 0, "coverage_pct": 87},
  "verification_commands": [
    {"command": "make regression", "exit_code": 0, "summary": "214 passed, 0 failed at release depth"},
    {"command": "npm run test:e2e", "exit_code": 0, "summary": "18 journeys green"},
    "test -s docs/release/REL-2026-09-1-regression.md"
  ],
  "token_usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "stage": "qe-verification"},
  "completed_at": "{iso8601_utc_timestamp}"
}
```

An **executed object** (`command` + `exit_code` + `summary`) is proof: the command ran and its exit code was recorded, and only this form scores `tests_pass` or `build_succeeds`. A **plain string** is a replay instruction the SubagentStop hook re-runs, and is not proof. Both must be replayable -- no command substitution, no shell pipeline, no `python3 -c` payload -- because a command the hook cannot re-run is a claim.

`{model_id_used}` and `{iso8601_utc_timestamp}` are placeholders to substitute with real values. Never copy a literal model id out of a prompt: `/cost` would price the step at a rate nothing ran at.

**Every one of these receipts must predate this Acceptance's `opened_at`** to enter the evidence package. That is not a technicality: a proof recorded after the release opened proves the release, not the work. Run the five dispatches first, then compile.

---

## What Acceptance does not do

| Activity | Acceptance |
|---|---|
| Integrate to the trunk | No. That happened at each Cycle's Checkpoint. Acceptance releases an already-integrated revision |
| Wait for every open Cycle | No. It evaluates a pinned candidate; unrelated Cycles keep running |
| Produce the evidence | No. It **compiles** records the work already produced. A record created here for the release is refused |
| Close the engagement, by default | No. Only the final one does, and "final" is a property of the engagement's state |
| Admit new work | No. Feedback becomes work at the next Commit |
| Approve on behalf of a machine | No. Release readiness is high risk and refuses the automated principal |
| Deepen a check that was skipped earlier | No. Regulated depth cannot be deferred to a final gate; unsupported depth reduces the supported scope |
