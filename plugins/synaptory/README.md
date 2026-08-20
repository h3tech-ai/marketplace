# Synaptory for Codex

This is the installable native Codex host package for Synaptory.

Invoke `$synaptory` in an existing Synaptory repository. The plugin can inspect
readiness, install all nine managed project agent profiles, read the shared
Scrum/Kanban/SPQ state, execute the deterministic SE→QE→CR story loop, validate
Codex receipts, and advance state only through the shared Synaptory lifecycle
engine.

The package composes these host-neutral sources from `plugin/`:

- lifecycle and story state machines;
- receipt/evidence validation and verification helpers;
- tracker scripts;
- the canonical evidence contract.

The Codex-authored layer owns the plugin manifest, `$synaptory` workflow, MCP
boundary, hooks, project bootstrap, and pure-TOML agent profiles.
Project bootstrap also enables `multi_agent_v2`, which Codex 0.147.0 requires
for reliable fresh named-agent dispatch. It preserves unrelated config and
refuses to override an explicit user `false`.

Current execution scope is standard, non-regulated projects whose lifecycle is
already in Scrum `SPRINT_EXECUTION`, Kanban `EXECUTION`, or SPQ
`SLICE_EXECUTION`. Broader ceremonies and focused modes remain part of the full
port. `baa_enforced` projects fail closed until the regulated Codex path is
certified.

Installed acceptance on Codex 0.147.0 has completed a synthetic Scrum story
through fresh SE, QE, and CR profiles with valid receipts, a passing DoD, and
terminal `sprint_complete`. Treat this as the standard-project canary tier;
cost attribution and latency optimization remain required before broad staff
rollout.

The runtime target is Codex CLI `0.147.0` or newer.
