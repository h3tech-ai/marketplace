# Synaptory for Codex

This is the installable native Codex host package for Synaptory.

Invoke `$synaptory` in an existing Synaptory repository. **Codex is certified
for SPQ only** (`build_mode: spq`). Scrum and Kanban share the lifecycle kernel
but have not been proven end-to-end on this host. The skill, its limitations,
the managed always-on `AGENTS.md` block, and the plugin description warn before
delivery dispatch and recommend SPQ. An explicit user may continue an
uncertified Scrum/Kanban run through the same fail-closed MCP gates.

The plugin can inspect readiness, install all nine managed project agent
profiles, read the shared lifecycle state, execute the deterministic
SE→QE→CR story loop, validate Codex receipts, and advance state only through
the shared Synaptory lifecycle engine. Validated receipts hand an allowlisted
analytics projection to the authenticated Synaptory CLI/outbox so Activity,
Cost, and Quality can populate without uploading prompts, transcripts,
artifact content or paths, commands, summaries, or findings.

The package composes these host-neutral sources from `plugin-claude/`:

- lifecycle and story state machines;
- receipt/evidence validation and verification helpers;
- tracker scripts;
- the canonical evidence contract.

The Codex-authored layer owns the plugin manifest, `$synaptory` workflow, MCP
boundary, hooks, project bootstrap, and pure-TOML agent profiles.
Project bootstrap also explicitly enables the stable `multi_agent` feature for
fresh named-agent dispatch. It preserves unrelated config and
refuses to override an explicit user `false`.

Current certified execution scope is standard, non-regulated SPQ, covering the
complete guarded lifecycle from initialization and Discovery through Commit,
repeated Cycle execution, Sync, Checkpoint, Acceptance, and Complete. Scrum and
Kanban remain shared-kernel, uncertified paths. `baa_enforced` projects fail
closed until the regulated Codex path is certified.

Installed acceptance on Codex 0.147.0 has completed a two-Cycle SPQ release
through Work Unit delivery, Sync, Checkpoint, Acceptance, and terminal
`COMPLETE`. Fresh managed profiles produced dispatch-bound receipts; all
receipts reached Activity and Cost, and pipeline-computed DoD reached Quality.
Treat this as the SPQ standard-project canary tier. The companion read-only
`scripts/standard_pilot.py` combines installed-plugin doctor evidence with
HTTPS health, latency, and fail-closed authentication probes for a
representative deployment. Regulated/HC0 remains refused.

The runtime target is Codex CLI `0.147.0` or newer.

## Local development beside production

Production and local testing intentionally use different CLI identities:

- `synaptory` → production stamp and `~/.synaptory/`
- `synaptory-local` → loopback stamp and `~/.synaptory-local/`

The installed Codex package selects the CLI from its immutable control-plane
stamp. A local package chooses `synaptory-local`; a production package chooses
`synaptory` and fails closed if that binary is loopback-stamped. Use
`SYNAPTORY_CLI_BIN` only for an explicit CI/test override. Do not put
`SYNAPTORY_CP_ENV=dev` or a loopback URL in a global shell profile.
