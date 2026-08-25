# Synaptory

**Multi-agent adaptive delivery for Claude Code.** Synaptory gives your team 9 specialized agents, Scrum and Kanban delivery paths, story-scoped DoD, Claude-native dispatch with tier routing (Opus/Sonnet/Haiku), and tracker integration in one orchestrated workflow.

> **Version:** 1.1.5 · **Author:** [H3Tech Inc.](https://github.com/h3tech-ai) · **Distribution:** Proprietary

## Requirements

| Requirement | Minimum |
|-------------|---------|
| **Claude Code** | v2.1.196 — required for `prompt_id` OTel correlation, `watchPaths`/`FileChanged` hooks, `userConfig` onboarding, and `${CLAUDE_PROJECT_DIR}` in hook allowed-tools. Earlier versions load but some observability and onboarding features degrade silently. |
| **Synaptory CLI** | Matches the plugin version (installed via `install.sh`). |

## Quick Start

### 1. Install the plugin

```bash
/plugin marketplace add https://github.com/h3tech-ai/marketplace
/plugin install "synaptory@h3tech-ai"
```

Then restart Claude Code.

### 2. Install the synaptory CLI

Once per laptop. The CLI is shipped separately from the plugin (see [docs/user-guide/install-cli.md](docs/user-guide/install-cli.md) for the rationale and the full walk-through).

**macOS / Linux:**

```bash
curl -fsSL https://synaptory.h3t.co/cli/install.sh | bash
```

**Windows (PowerShell):**

```powershell
iwr -useb https://synaptory.h3t.co/cli/install.ps1 | iex
```

Both scripts detect your OS / architecture, verify SHA-256, and put `synaptory` on your `PATH`.

### 3. Sign in

```bash
synaptory login
```

A browser opens for Entra sign-in. After sign-in the CLI prints the projects you have access to. **One sign-in per laptop** — the same session works across every project you're a member of, the way `gh` and `aws` do.

To tell synaptory which project this repository belongs to, drop a one-line file at the repo root:

```yaml
# .synaptory.yaml
project_id: <your-project-slug>
```

The slug is the one your H3Tech operator created in the admin UI.

### 4. Start with a natural-language request

```text
/synaptory
Build me a SaaS for managing restaurant reservations
```

## Delivery Paths

**Scrum**

| Stage | What happens |
|-------|-------------|
| **Inception** | Product Manager defines vision and Sprint 1 stories. Solution Architect and Platform Engineer establish foundations. |
| **Sprint Loop** | Stories move through Software Engineer -> Quality Engineer -> Code Reviewer -> DoD evaluation. |
| **Release** | Full production-readiness verification and final documentation. |

**Kanban**

| Stage | What happens |
|-------|-------------|
| **Discover** | Build context packages for an existing codebase before changes begin. |
| **Ticket Loop** | Pull a ticket, implement it, verify it, review it, evaluate DoD, then move to the next ticket. |
| **Release** | Run release verification on demand. |

**Focused modes**

Story Buddy, Debug, Explore, Preview, Branch Finish, Retro, Status, and Release are available directly. The orchestrator classifies what you ask for and routes to the right workflow.

## Agent Roles

| Agent | Role |
|-------|------|
| **Product Manager** | Backlog refinement, sprint planning, story decomposition |
| **Solution Architect** | Architecture decisions, ADRs, system design |
| **Software Engineer** | Implementation across backend, frontend, AI/ML, and mobile work |
| **Quality Engineer** | Verification, tests, and coverage discipline |
| **Code Reviewer** | Review findings and architecture conformance |
| **Compliance Engineer** | Security and compliance review |
| **Platform Engineer** | CI/CD, infrastructure, environments, observability |
| **Technical Writer** | Reports, guides, and developer documentation |
| **Research Advisor** | Structured exploration and domain research |

The Claude package runs every role on Claude (Opus/Sonnet/Haiku, selected per role by tier). The Cursor host overlay lives in [`../plugin-cursor`](../plugin-cursor) and is recomposed from this tree on `./synaptory build` (do not edit GENERATED copies). The native Codex host lives in [`../plugin-codex`](../plugin-codex): its manifest, skill, MCP/auth/readiness boundary, hooks, bootstrap, and agent TOML templates are authored there, while lifecycle/receipt/verification/tracker code is copied from an explicit allowlist in this tree during the same unified build.

### Multi-host ownership and Codex gates

- Edit shared lifecycle, state, receipt, verification, and tracker logic in `plugin-claude/`; recompose both host projections and keep Claude behavior green.
- Edit Codex-only host behavior in `plugin-codex/`. Never hand-edit shared files inside a composed marketplace artifact.
- Codex SessionStart and every MCP mutation require a configured control-plane URL plus a successful `synaptory whoami --check`. Mutation readiness also requires Codex ≥0.147.0, effective `multi_agent_v2`, the composed runtime, all nine project profiles, readable state, and a non-regulated project.
- Codex advancement accepts only the canonical stage receipt, checks its `completed_at` against the stage `entered_at`, validates it, and rejects replay. Bootstrap parses TOML semantically, refuses explicit `false` in equivalent TOML forms, and validates rendered config before writing.
- The current Codex rollout is the standard-project SE→QE→CR canary. Regulated certification, broader ceremonies/modes, protected role-body delivery, expanded hook coverage, parallel multi-writer execution, and complete usage/cost attribution remain staged non-goals.

## Configuration

Create `.synaptory.yaml` in your project root:

```yaml
build_mode: scrum
engagement_mode: autonomous

agents:
  default_backend: "claude"

tracker:
  backend: local
```

Tracker backends include `local`, `github`, `jira`, `teamwork`, and `linear`.

## Runtime Artifacts

synaptory writes evidence and lifecycle state into `.synaptory/.orchestrator/`:

- `pipeline-state.json` for current lifecycle state
- `receipts/` for per-story or per-ticket evidence
- `context-packages/` for Discover outputs
- `last-session.md` for cross-session resume
- `tracker-data.json` when using the local tracker backend

## Documentation

Open **GUIDE.html** in this distribution for the full guide covering:

- plugin + CLI installation and Entra sign-in
- Scrum and Kanban delivery workflows
- focused utility modes
- AI backends and engagement modes
- tracker configuration and runtime artifacts
- hooks, enforcement, and troubleshooting

## Session And IP Terms

This software is proprietary to H3Tech Inc. See `LICENSE` and `NOTICE` files.

- each session is personal to the signed-in user and non-transferable
- runtime content is watermarked and traceable to the session holder
- unauthorized redistribution is prohibited

Copyright 2024-2026 H3Tech Inc. All rights reserved.
