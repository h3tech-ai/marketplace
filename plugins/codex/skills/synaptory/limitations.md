# Codex host limitations

Read this with `$synaptory` before structured delivery, when the configured
build mode is Scrum or Kanban, or when a user asks how Codex differs from the
other Synaptory hosts.

## Certified lifecycle — SPQ only

End-to-end certification on Codex covers **SPQ** (`build_mode: spq`): four
stages, `DISCOVERY → CYCLE → ACCEPTANCE → COMPLETE`, one Cycle per Engineering
Lead and Crew, one board, and the all-or-nothing barrier at Checkpoint
integrating to the shared trunk. Scrum and Kanban share the
same lifecycle kernel, receipt gates, and tracker adapters, but they have
**not** been proven end-to-end on this host.

For a Scrum or Kanban project, warn before any delivery dispatch and recommend
switching to SPQ. If the user explicitly continues, label the run uncertified,
use only governed MCP actions, and stop at every human gate. Do not describe
Codex as a drop-in certified host for those lifecycles.

Standalone Status, Doctor, Help, and Debug operations remain available on every
build mode.

## What is certified

- Standard, non-regulated SPQ from initialization and Discovery through Commit,
  repeated Cycle execution, Sync, Checkpoint, Acceptance, and Complete.
- Deterministic managed-role dispatch with exact execution envelopes.
- Dispatch-bound receipt validation, fail-closed advancement, and minimized
  durable analytics handoff.
- Project bootstrap for the nine managed Codex profiles and the marked
  always-on `AGENTS.md` instructions.

## Remaining boundaries

- `baa_enforced` projects are refused; SPQ certification is not regulated/HC0
  certification.
- Codex exposes a smaller hook graph than Claude Code. The package uses
  SessionStart, SessionEnd, SubagentStop, and Stop plus MCP mutation gates.
- Acceptance roles run sequentially because the certified Codex host permits
  four total agent threads including the parent.
- Cross-host handoff is unsupported. Hosts share repository state under normal
  single-writer discipline.
- Runtime installation and hook certification require Codex CLI 0.147.0 or
  newer; composition on an older CLI is not runtime certification.
