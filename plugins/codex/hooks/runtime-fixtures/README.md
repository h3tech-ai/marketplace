# Runtime-federation fixtures

Canonical example objects for Epic #339. **These are the interface between the
pilot's lanes.** The kernel lane, the Go bridge lane, the control-plane lane,
and the web lane each code against these files rather than against each other's
branches, so a contract disagreement surfaces as a fixture diff and a
conversation rather than as an integration failure in Phase D.

Source of truth for the shapes is
[docs/proposals/governed-runtime-pilot.md](../../docs/proposals/governed-runtime-pilot.md)
section 4 for the pilot-1 surfaces, and
[docs/proposals/capability-profile-pilot.md](../../docs/proposals/capability-profile-pilot.md)
sections 3.1 and 3.3 for the pilot-2 vocabulary (Epic #410, keystone #399):
the role-alias projection, `accountable_role`, typed evidence classes, and
`criteria_gap_declared`. The executable definition is
[core/lib/runtime_contracts.py](../lib/runtime_contracts.py). Where a fixture
and that module disagree, the module wins, and
`plugin-claude/tests/lib/test_runtime_fixtures.py` plus
`plugin-claude/tests/lib/test_capability_profile_contracts.py` fail until
someone fixes it: every fixture here is validated on every test run, so these
cannot rot.

| File | What it is |
|---|---|
| `envelope-local-qe.json` | Dispatch envelope for a local Codex verification attempt. Local placement, so no materialization block. |
| `envelope-managed-se.json` | Dispatch envelope for a managed headless Claude Code build attempt. Carries materialization and `cred://` connector refs. |
| `materialization-managed.json` | A project's materialization block as the control plane stores it (`GET /v1/runtime/projects/<slug>/materialization`). The kernel mirrors this to `.synaptory/runtime-materialization.json` and builds `envelope-managed-se.json`'s materialization, connector and config blocks from it (#632). |
| `supervision-events.jsonl` | One event of each of the seven sub-kinds, in sequence, for one attempt. |
| `profiles.json` | The four pilot adapter profiles, with the capability profiles each may serve. |
| `receipt-attempt-bound.json` | An accepted receipt carrying attempt identity in the payload. |
| `receipt-attempt-failed.json` | A failed attempt's receipt, carrying a `failure_class`. |
| `role-profile-aliases.json` | The full 9-to-4 projection: every legacy role with its `(stage_profile, capability_profile)` pair. Asserted equal to the module's tables, both ways. |
| `receipt-evidence-replayed.json` | An SE receipt whose evidence item is `replayed` (verification_runner semantics), carrying `accountable_role`. |
| `receipt-evidence-attested.json` | A CR receipt whose evidence item is `attested`, bound to the producing attempt identity. |
| `receipt-evidence-judged.json` | A PO acceptance receipt whose evidence item is `judged`, with the full verdict discipline. |
| `judged-verdict.json` | A standalone judged verdict: a rejection with `independent_of_producer: false` recorded (recorded, not refused). |
| `dod-criteria-gap.json` | A DoD check result declaring `criteria_gap_declared`, naming the missing evidence shape. |

## Rules

1. **Attempt identity lives in the payload, never in a filename.** The receipt
   fixtures sit at the canonical SPQ path shape; the attempt id is a field.
2. **No secret ever appears here, not even a fake-looking one.** Connector
   values are `cred://` references. The validator refuses anything else, so a
   fixture that tried would fail its own test.
3. **`pinned_version` records the lifecycle's actual decision.** Local profiles
   retain their Phase B placeholders. The managed profile is concrete because
   Phase D entered on 2026-09-02 and pinned Claude Code `2.1.236`; changing it
   requires the two-platform capability report to be rerun.
4. **Adding a field means amending the proposal first.** That is the epic's
   source-of-truth rule, and it is what keeps four lanes agreeing.

## Two shapes live here, and they use the same two words differently

**Receipt fixtures name the role in `role`, with the full role name**, the way
`skills/_shared/protocols/receipt-protocol.md` tells agents to write it and the
way `receipt_validator` requires it. `agent` is the control plane's storage key,
not the on-disk one; `story_pipeline` reads it only as a #105 back-compat
fallback. `test_runtime_fixtures.py` pins this so the drift cannot return (#453).

**`role-profile-aliases.json` uses the two words the other way round**: its
`role` column holds the abbreviation (`se`) and its `agent` column holds the
full name (`software-engineer`). That is the projection table's own shape, not a
receipt, and the columns are deliberately left alone here rather than renamed
under a lane that is reading them. Read it as a lookup table, never as a receipt.

**The `receipt-evidence-*.json` trio are evidence-SHAPE fixtures, not complete
receipts.** They illustrate the three `evidence_class` disciplines and omit
`metrics`, `story_id` and real artifacts, so `receipt_validator` refuses them by
design. Anything needing a valid pipeline receipt builds its own.
