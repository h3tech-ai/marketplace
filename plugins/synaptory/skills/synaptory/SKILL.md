---
name: synaptory
description: Run governed Synaptory delivery in Codex, including project readiness, deterministic story dispatch, SE→QE→CR execution, receipt validation, tracker access, and fail-closed state advancement. Use when the user asks about Synaptory, a sprint, a story, pipeline state, or continuing delivery work in an existing Synaptory repository.
---

# Synaptory for Codex

Invoke this workflow as `$synaptory`. Codex is a native Synaptory host for
standard, non-regulated project execution. The Synaptory MCP state machine—not
prompt memory—selects every role and transition.

## Readiness

1. Call `doctor` with the current project directory.
2. If no Synaptory state is detected, explain that this workflow currently
   requires an initialized Synaptory repository. Do not invent pipeline state.
3. If the project is regulated (`baa_enforced`), stop. The standard Codex path
   is not the certified regulated profile.
4. If managed profiles or the effective `multi_agent_v2` feature are missing,
   call `bootstrap_project` with `apply: false`. Show the planned files and
   conflicts, then ask for explicit confirmation before calling it with
   `apply: true`. The bootstrap enables `multi_agent_v2` in project Codex
   config without replacing other settings and installs the managed profiles.
   An explicit user `multi_agent_v2 = false` is a conflict and is never
   overwritten. A successful apply needs a new Codex task for discovery.
5. Start structured work only when `structured_execution_ready` is true.

The bootstrap owns only generated `synaptory-*` TOML files and the marked
Synaptory block in `AGENTS.md`. Never overwrite user-owned profiles or text.

## Status and planning

Use `get_status` for a compact, non-sensitive summary. Use `get_state` only
when building an authorized execution envelope; do not reproduce full story
content unnecessarily in chat. Tracker reads and mutations go through the
`tracker_*` MCP tools, never a second tracker implementation.

## Governed story loop

Repeat this loop for an existing Scrum sprint, Kanban execution board, or SPQ
slice. Re-read `next_action` at the start of every iteration.

1. Call `next_action`.
2. If it returns a human gate, terminal action, `not_in_execution`,
   `await_sync`, or an action without a dispatch/transition contract, stop and
   report the exact reason. Never skip a human gate.
3. If it returns `transition_to` with a fresh receipt already present, call
   `advance` with exactly that `story_id` and `transition_to`. Do not dispatch
   another agent first.
4. For `dispatch_*` or `recover_blocked`, call `begin_dispatch` with the exact
   selected story and role. Continue only when it returns `authorized: true`.
5. Delegate to the custom agent named by `agent_profile`. The task must include
   the complete returned envelope: story, current action, DoD expectations,
   recovery information, receipt path, and receipt contract. Add only the
   repository context needed to execute the bounded work. Spawn the custom
   profile as a fresh agent with no inherited conversation history; when the
   host exposes a history/fork option, set it to `none`. Never combine a named
   custom agent profile with a full-history fork. Wait only on the concrete
   agent identifier returned by a successful spawn; a failed spawn is a
   fail-closed error, not an empty wait.
6. Require the agent to write its receipt last to the exact `receipt_path`.
   The receipt must use the full role name and `backend: codex`. Record the
   actual model identifier when available; otherwise use the honest value
   `codex-runtime-unattributed`, never a guessed model. Every verification
   command is an executed object with `command`, integer `exit_code`, and
   `summary`.
7. After the agent returns, call `validate_receipt`. If missing or invalid,
   stop without changing state and report the repair needed.
8. Call `next_action` again. Advance only when it returns a `transition_to`,
   and pass that exact value to `advance`. Then begin the next iteration.

Role mapping is deterministic: `se` is `synaptory-software-engineer`, `qe` is
`synaptory-quality-engineer`, and `cr` is `synaptory-code-reviewer`. Recovery
and conditional gates may select any of the other managed Synaptory profiles;
use the `agent_profile` returned by `begin_dispatch`, not a role chosen from
memory.

## Safety invariants

- Never edit `.synaptory/.orchestrator/pipeline-state.json` directly.
- Never call a lifecycle Python module directly; use the Synaptory MCP tools.
- Never advance without a valid receipt under `.synaptory/.orchestrator`.
- Never change the story, role, or transition returned by `next_action`.
- A failed agent, missing receipt, invalid evidence, tracker error, or MCP error
  stops the loop fail-closed.
- Preserve unrelated and concurrent user work. Code Reviewer stays read-only
  except for its governed receipt.
- Cross-host handoff is not a feature. Claude and Codex operate on the same
  repository-owned Synaptory state under normal single-writer discipline.
