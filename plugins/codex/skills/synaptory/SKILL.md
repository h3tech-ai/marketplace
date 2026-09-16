---
name: synaptory
description: Run governed Synaptory delivery in Codex. Certified for SPQ (`build_mode: spq`), with project setup, full SPQ ceremonies, deterministic dispatch, receipt validation, tracker access, and fail-closed advancement. Warn and recommend SPQ when a project uses Scrum or Kanban. Use when the user asks about Synaptory, SPQ, a Cycle, Sync, Checkpoint, Acceptance, a sprint, a story, pipeline state, or continuing delivery work.
---

# Synaptory for Codex

Invoke this workflow as `$synaptory`. Codex is a native Synaptory host for
standard, non-regulated SPQ execution. The Synaptory MCP state machine—not
prompt memory—selects every role and transition.

## Certified lifecycle — SPQ only

This Codex plugin is **certified for SPQ** (`build_mode: spq` in
`.synaptory.yaml`). Scrum and Kanban share the lifecycle kernel, but they have
**not** been proven end-to-end on Codex.

If the configured build mode is `scrum` or `kanban` (including an unset mode,
which currently defaults to Scrum):

1. Warn the user in the first reply, before any delivery dispatch. State that
   SPQ is the supported Codex path and that continuing Scrum/Kanban is
   uncertified.
2. Recommend switching to `build_mode: spq`. Offer the governed SPQ
   initialization or migration flow; never hand-edit pipeline state.
3. If the user explicitly chooses to continue Scrum/Kanban, keep the warning
   visible, use only MCP `next_action`/`advance` contracts, and stop at every
   human gate. Do not claim that path is certified.

Standalone Status, Doctor, Help, and Debug work remains available for every
build mode. Read `limitations.md` before structured delivery or when explaining
host gaps.

## Readiness

1. Call `doctor` with the current project directory.
2. If no Synaptory state is detected, bootstrap the managed Codex files using
   the dry-run/confirmation flow below. When the user requested SPQ, first
   initialize it with `spq_lifecycle(operation: "initialize")`, because the
   bootstrap deliberately verifies an existing governed state before writing;
   then run the bootstrap dry-run. Do not write pipeline state directly. For
   Scrum or Kanban, warn and recommend SPQ first. Use the Synaptory CLI
   initializer only after the user explicitly chooses the uncertified path.
3. If the project is regulated (`baa_enforced`), stop. The standard Codex path
   is not the certified regulated profile.
4. If managed profiles or the effective `multi_agent` feature are missing,
   call `bootstrap_project` with `apply: false`. Show the planned files and
   conflicts, then ask for explicit confirmation before calling it with
   `apply: true`. The bootstrap enables `multi_agent` in project Codex
   config without replacing other settings and installs the managed profiles.
   An explicit user `multi_agent = false` is a conflict and is never
   overwritten. A successful apply needs a new Codex task for discovery.
5. Start structured work only when `structured_execution_ready` is true. State
   initialization is the one exception because readable state is the outcome
   of that operation; it still requires authentication and a standard project.
   Confirm `checks.control_plane_receipt_delivery.ready` is also true. Normal
   authenticated execution sends only the documented analytics projection;
   authentication bypass mode keeps unit/offline tests local and never uploads.

The bootstrap owns only generated `synaptory-*` TOML files and the marked
Synaptory block in `AGENTS.md`. Never overwrite user-owned profiles or text.

## Status and planning

Use `get_status` for a compact, non-sensitive summary. Use `get_state` only
when building an authorized execution envelope; do not reproduce full story
content unnecessarily in chat. Tracker reads and mutations go through the
`tracker_*` MCP tools, never a second tracker implementation.

## Full SPQ lifecycle

The lifecycle has **four stages** —
`DISCOVERY → CYCLE → ACCEPTANCE → COMPLETE` — with `ACCEPTANCE → CYCLE` making
Acceptance repeatable. Commit, Sync
and Checkpoint are **recorded events inside those stages, never stages and
never gates**. Call `next_action` at every boundary; it answers for every
stage. Never call lifecycle Python modules directly.

- `approve_baseline` and `complete_release` are human gates. Require the user's
  explicit decision and pass the real principal as `approved_by` to
  `spq_lifecycle`.
- `approve_baseline` also requires `baseline_ref`: an approval that does not
  name the revision it approved cannot be the line a cut must not move.
- `open_cycle` seals the Cycle declaration — the goal, the repository, the
  trunk ref it will integrate to, the source region it may address, the
  accountable `engineering_lead`, and a non-empty `admitted_units` array whose
  every unit declares a `kind`, acceptance criteria and a `path_scope`. The
  declaration is immutable afterwards, and two units whose `path_scope`
  intersect are refused a concurrent execution set. Another clone joins with
  `hydrate_cycle` and its `cycle_id`; there is no per-clone Work Unit filter,
  because the admitted set is the Cycle's work.
- After every Work Unit is `done` or explicitly cut, `next_action` selects
  `close_cycle`. Run the Cycle barrier, then call `close_cycle` with the trunk
  revision it integrated (`integrated_sha`) and the barrier's own
  `barrier_verdict`. The close **records** a verdict; it does not derive one,
  and a non-green verdict cuts work rather than closing.
- If a cross-Cycle wait cleared, `record_sync` records which dependency and
  how. It unblocks nothing — a satisfied dependency does that — and its absence
  blocks nothing either.
- At Checkpoint and Acceptance, a `dispatch_*` action uses
  `begin_lifecycle_dispatch`, then the named managed profile. Include the
  returned `execution_envelope` verbatim, including its pseudo story id,
  allowed paths, audience, required output and verification, exact receipt
  path, full receipt contract, and `dispatch_id` in the task. Do not infer or
  invent missing role scope. Validate the receipt and call `next_action` again.
- Dispatch Acceptance's `qe`, `ce`, `pe`, `tw`, and `cr` activities one at a
  time. Codex currently permits four total agent threads including the parent,
  so a five-agent parallel fan-out is not a valid execution plan.
- `close_cycle` refuses while `next_action` is anything other than
  `close_cycle`, so outstanding work or owed evidence blocks it. `complete`
  refuses any missing or invalid release receipt, and only the **final**
  Acceptance offers it — whether an Acceptance is final is derived from
  outstanding commitments and a recorded handover, never claimed by the
  caller.

## Governed Work Unit loop

Repeat this loop for an SPQ Cycle. The shared kernel can also return actions for
an existing Scrum sprint or Kanban execution board, but those paths are
uncertified on Codex and require the warning and explicit choice above. Re-read
`next_action` at the start of every iteration.

1. Call `next_action`.
2. If it returns an SPQ ceremony operation, follow the full lifecycle section.
   If it returns a human gate, a terminal action, or an action without a
   dispatch/transition contract, stop and report the exact reason. Never skip a
   human gate. There is no `await_sync` to wait at: what holds a Work Unit is
   an unmet dependency, and `next_action` names it and why.
3. If it returns `transition_to` with a fresh receipt already present, call
   `advance` with exactly that `story_id` and `transition_to`. Do not dispatch
   another agent first.
4. For `dispatch_*` or `recover_blocked`, call `begin_dispatch` with the exact
   selected story and role. Continue only when it returns `authorized: true`.
5. **If `begin_dispatch` returned `dispatch_envelope`, call `execute_through_runtime`
   with that envelope and the `project_dir`, and do NOT delegate to the custom
   agent.** The kernel selected a runtime for this dispatch, and a receipt from
   a different family is refused at `advance`: delegating natively there
   produces a `runtime_family_mismatch`, not a result. The `runtime` block says
   which profile was chosen and why. The bridge writes the receipt itself, so
   **skip steps 6 and 7 and continue at step 8** with the receipt it reports.
   Steps 6 and 7 are the native branch: entering them here would ask a second
   executor for a receipt this dispatch already has.
6. **Otherwise** (no `dispatch_envelope`) delegate to the custom agent named by
   `agent_profile`. The task must include
   the complete returned envelope: story, current action, DoD expectations,
   recovery information, receipt path, and receipt contract. Add only the
   repository context needed to execute the bounded work. Spawn the custom
   profile as a fresh agent with no inherited conversation history; when the
   host exposes a history/fork option, set it to `none`. Never combine a named
   custom agent profile with a full-history fork. Wait only on the concrete
   agent identifier returned by a successful spawn; a failed spawn is a
   fail-closed error, not an empty wait.
7. Still the native branch: require the agent to write its receipt last to the
   exact `receipt_path`.
   The receipt must use the full role name and `backend: codex`. Record the
   actual model identifier when available; otherwise use the honest value
   `codex-runtime-unattributed`, never a guessed model. Every verification
   command is an executed object with `command`, integer `exit_code`, and
   `summary`. Copy the returned `dispatch_id` exactly; a receipt from a previous
   or concurrent dispatch is refused.
8. After **either executor** returns, the bridge or the custom agent, call
   `validate_receipt`. If missing or invalid,
   stop without changing state and report the repair needed. A successful call
   also reports `control_plane_delivery.handed_off: true`, meaning the API
   accepted the minimized projection or the CLI durably queued it. Never treat
   local validity alone as sufficient for central analytics.
9. Call `next_action` again. Advance only when it returns a `transition_to`,
   and pass that exact value to `advance`. Then begin the next iteration.

Role mapping is deterministic: `se` is `synaptory-software-engineer`, `qe` is
`synaptory-quality-engineer`, and `cr` is `synaptory-code-reviewer`. Recovery
and conditional gates may select any of the other managed Synaptory profiles;
use the `agent_profile` returned by `begin_dispatch`, not a role chosen from
memory.

## Safety invariants

- Never edit `.synaptory/.orchestrator/pipeline-state.json` directly.
- Never call a lifecycle Python module directly; use the Synaptory MCP tools.
- Never advance or close a ceremony without valid, dispatch-bound receipts
  under `.synaptory/.orchestrator`.
- Never bypass failed control-plane receipt handoff. The upload allowlist is
  identifiers, model/token attribution, timestamps, the Cycle id, and the five
  DoD booleans. Prompts, transcripts, artifact content or paths,
  commands, summaries, and findings remain local.
- Never change the story, role, or transition returned by `next_action`.
- A failed agent, missing receipt, invalid evidence, tracker error, or MCP error
  stops the loop fail-closed.
- Preserve unrelated and concurrent user work. Code Reviewer stays read-only
  except for its governed receipt.
- Cross-host handoff is not a feature. Claude and Codex operate on the same
  repository-owned Synaptory state under normal single-writer discipline.
