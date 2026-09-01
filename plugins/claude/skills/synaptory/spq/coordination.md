# Coordination. The Release Across Independent Cycles

> **Scope:** a release composed of several **independent child SPQ Cycles** on one codebase, one git remote and one date
> **Participants:** Orchestrator (release clone), delivery lead (human), PO for scope decisions
> **Output:** a sealed Coordination Cycle manifest, cross-Cycle events, and a green release-readiness report backed by `RELEASE-{SEQ}-readiness.json`
> **Human gate:** YES. Composing the release is a merge, and agents may not merge or push.

This is **not** a lifecycle state. A Coordination Cycle is not a `build_mode`
and has no `DISCOVERY → … → COMPLETE` of its own: child Cycles run their own
lifecycles at their own cadence, and this coordinates the edges between them and
the contents of the release. Load this file when the request is about a release
spanning several Cycles, not when it is about one Cycle's own progress.

## When this applies

One master specification decomposed into several scopes that progress
independently. The shape to recognise:

- Platform on **Cycle 12**, Contract Mastery on **Cycle 3**, EHR on **Cycle 5** —
  different numbers, different cadences, no lockstep.
- EHR needs a contract Contract Mastery publishes.
- Platform needs nothing from either, and **must never wait on either**.

If every scope is in one Cycle with several workstreams, you want `spq/sync.md`
instead. That barrier is all-or-nothing across a quorum by design. This one is
not, and the difference is the whole point.

---

## The hierarchy, and who owns what

| Level | Owns | Decided by |
|---|---|---|
| Project / release | the shared outcome and the acceptance boundary | delivery lead |
| **Coordination Cycle** | which child increments ship, and the edges between them | this file |
| Child Cycle | its own admitted Work Units, its own Sync and Checkpoint | `spq/*.md` in that clone |
| Workstream | a durable lane inside one child Cycle | that Cycle's manifest |
| Work Unit | the atomic implement-and-verify unit | that workstream |

**The parent never dispatches into a child.** It coordinates declared dependency
edges and the chosen release contents, and nothing else. A parent that could
dispatch would be a second scheduler for state the child's own barrier owns.

---

## Step 1 — Choose the increments, by identity

A release pins each child by the triple `(cycle_id, manifest_hash,
selected_sha)`. **Never by Cycle number.** Numbers are allocated per clone, so
two children can legitimately both be "Cycle 12", and pinning by number makes
"the release contains exactly this" unprovable.

For each child, from its own clone or from its integration ref:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" verify_manifest "$(pwd)" \
  --cycle-id {CHILD_CYCLE_ID}
```

Record `manifest_hash`, `integration_ref`, and the exact green integration SHA
you are selecting. The SHA is a **commit, not a branch tip** — that is precisely
what lets a child open its next Cycle without disturbing a release that already
selected an increment from it.

## Step 2 — Declare the cross-Cycle edges

An edge names a consumer, a producer, an address and a condition. The
direction is DEPENDENCY, not data flow: the waiter is held until the
producer publishes.


- `waiter_cycle_id` / `waiter_unit_id` — who waits
- `producer_cycle_id` plus either `producer_unit_id` or `output` — what they wait on
- `condition` — one of `integrated`, `contract_published`, `artifact_published`,
  `cycle_integrated`, `environment_ready`

**Prefer the smallest sufficient condition.** EHR should start when the contract
it needs is published, not when every Work Unit in Contract Mastery is finished.
`contract_published` on the exact contract is that condition; `cycle_integrated`
waits for the whole child increment and is usually more than the product needs.

**`environment_ready` is claim-only** and fails closed by default. It ships so
the schema is forward-compatible, not so it can license dispatch. Anyone who
wants it to actually gate owes a verification command.

## Step 3 — Open the release

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" open_coordination_cycle "$(pwd)" \
  --goal "{RELEASE_GOAL}" \
  --children '{CHILDREN_JSON}' \
  --edges '{EDGES_JSON}'
```

This seals a hashed manifest, writes the committed copy that is the **only** way
the release reaches a child clone, records the index entry and writes the pin —
in one step, so a crash cannot leave a clone pointing at a release whose manifest
was never sealed.

It refuses, loudly, when: a child is included with no `manifest_hash` or no
`selected_sha`; an edge names a Cycle that is not a child; there is a cross-Cycle
dependency loop; or `.synaptory/coordination-cycles/` is excluded by
`.gitignore`. That last one matters most — a release manifest that writes
cleanly and is invisible to every child clone is the worst failure available,
because the parent looks correct locally.

Then **commit and push** the manifest. That is a human step.

## Step 4 — Publish cross-Cycle events as producers finish

From the producing child's clone, once its work is on its own integration ref:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" publish_cross_cycle_event "$(pwd)" \
  --cycle-id {PRODUCER_CYCLE_ID} \
  --condition {CONDITION} \
  --output '{"kind":"contract","id":"{OUTPUT_ID}","digest":"{DIGEST}"}' \
  --sha {COMMIT_SHA}
```

Verification happens **here**, on the producing side, where the refs and the
digest script exist. A digest is recomputed with the same script the child
barrier runs; `integrated` and `cycle_integrated` are checked by ancestry. An
event that was never checked here is an event nobody checks.

The command prints the exact `git add && git commit && git push`. Run it: the
event does not exist for any other clone until it is pushed, and agents may not
push.

## Step 5 — Consumers refresh and dispatch

In the consuming child's clone:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" refresh_coordination "$(pwd)"
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" dep_status "$(pwd)"
```

`refresh_coordination` reads each child's events off its integration ref with
`git show` and **never merges**. Reading a dependency must not change the tree.

Reading a held edge, by `reason_code`:

| Code | What it means | What to do |
|---|---|---|
| `dep_cross_cycle_undeclared` | the release declares no edge for this address | revise the **parent** manifest — not the child, not the ledger |
| `dep_external` | declared, but no satisfying event yet | chase the producing Cycle |
| `dep_ledger_stale` | the local cache is behind | `refresh_coordination` |
| `dep_condition_unverified` | the event is a claim nothing could check | ask for a verifiable condition |

## Step 6 — Compose and check the release

The delivery lead merges each pinned SHA onto the release integration ref. This
is a human act; `modes/init.md` forbids agents merging or pushing.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" release_readiness "$(pwd)"
```

Exit 0 green, exit 3 blocked. Six criteria: `manifest_sealed`,
`children_resolved`, `child_integration_green`, `edges_satisfied`,
`composition_merged`, `composition_regression_green`.

**`edges_satisfied` iterates EDGES, not children.** A child with no inbound edge
appears in `gated_children` as `[]` and is never blocked by a sibling. If you see
an unrelated Cycle blocked on another, that is a bug in the manifest's edges, not
in the barrier.

The report names exactly what would ship: included children with their manifest
hashes and integration SHAs, the dependency closure, and the verification
evidence. Dropped children appear with their reason.

## Step 7 — Drop a late child, if policy permits

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" revise_coordination_manifest "$(pwd)" \
  --drop-child {CYCLE_ID} --reason "{WHY}"
```

A **parent-side edit only**. The child's manifest, execution state and receipts
are untouched: it keeps running its own Cycle, it simply is not in this release.
The revision records the prior hash in `supersedes`, and the dropped child's
outbound edges are pruned with it.

Dropping a **producer** something still waits on is refused. Drop the consumer
first, or re-point its edge.

## Step 8 — Clear the release

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/lib/spq_state_machine.py" clear_release "$(pwd)" \
  --cleared-by "{UPN}"
```

Re-evaluates fail-closed, writes `RELEASE-{SEQ}-readiness.json`, and emits the
existing `release` gate with target `RELEASE-{SEQ}`. SPQ adds no new gate types.

---

## What this does NOT do

- It does not start, finish or Sync a child Cycle. Those are that clone's own
  ceremonies.
- It does not force children onto matching numbers or a shared cadence.
- It does not make the control plane authoritative. `/coordination-cycles`
  reports what the CP was told; git decides, and `release_readiness` reads git.
- It does not treat a specification document as execution state.
  `source_spec_refs[]` is traceability and selects nothing.
