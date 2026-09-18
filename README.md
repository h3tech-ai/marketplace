# Synaptory plugin catalog

**Multi-agent adaptive delivery for Claude Code, Cursor, and Codex.**
Published and maintained by [H3Tech Inc.](https://h3t.co)

> **Catalog version: `1.3.1`** — this repository is a **generated
> distribution tree**. It is rebuilt and force-synced by
> `./synaptory deploy prod` on every release. Do not open pull requests or
> edit files here; changes are overwritten on the next publish.

Synaptory turns one AI coding assistant into a governed delivery system: SPQ
Cycles, role dispatch, receipt-gated advancement, runtime federation as an
operator preview (bridge commands only, not yet on the normal dispatch path),
and pluggable tracker integrations (local,
GitHub, Jira, Teamwork, Linear). Scrum and Kanban remain compatibility paths
for existing projects while retirement is planned. Current host packages and
receipt wires retain the nine delivery-role names.

## Requirements

Synaptory is licensed software for H3Tech staff and customers. You need:

1. An H3Tech account with membership in at least one Synaptory project.
2. The `synaptory` CLI, installed once per laptop and shared across every
   project you belong to:

   ```bash
   # macOS / Linux
   curl -fsSL https://synaptory.h3t.co/cli/install.sh | bash

   # Windows (PowerShell)
   iwr -useb https://synaptory.h3t.co/cli/install.ps1 | iex
   ```

3. A signed-in session:

   ```bash
   synaptory login
   ```

Without a valid session the plugin installs but refuses to run — its
instruction bodies are served from the control plane, not from this
repository (see [What is in this tree](#what-is-in-this-tree)).

## Install

### Claude Code

```
/plugin marketplace add https://github.com/h3tech-ai/marketplace
/plugin install synaptory@h3tech-ai
```

Then start with a natural-language request:

```
/synaptory Use SPQ to deliver account sign-in with audit logging. Start
Discovery and show me the proposed baseline before Commit.
```

### Cursor

Import this repository as a Team marketplace (Settings → Team marketplace →
Import from Repo), then install **synaptory** from the team catalog. The
plugin body lives at `plugins/cursor/`.

```bash
synaptory skills sync --host cursor
```

Reload Cursor and use `/synaptory`.

### Codex

Requires Codex CLI `0.147.0` or newer.

```bash
codex plugin marketplace add https://github.com/h3tech-ai/marketplace
codex plugin add synaptory@synaptory-dev
```

Invoke it as `$synaptory`. Bootstrap is dry-run first and asks for explicit
confirmation before writing anything to your project. Codex certifies the full
standard, non-regulated SPQ lifecycle. Regulated (HIPAA / BAA) operation and
cross-host handoff are not in scope, and unsupported projects are refused
rather than silently downgraded.

## What is in this tree

Every host package lives at `plugins/<host>/` so the three share one
convention. Each host's catalog manifest stays where that host requires it.

| Path | Host | Contents |
|---|---|---|
| `.claude-plugin/marketplace.json` | Claude Code | Catalog `h3tech-ai`, resolves `./plugins/claude` |
| `plugins/claude/` | Claude Code | Agents, hooks, skills, plugin runtime |
| `.cursor-plugin/marketplace.json` | Cursor | Catalog `synaptory`, resolves `plugins/cursor` |
| `plugins/cursor/` | Cursor | Composed Cursor overlay — agents, `.mdc` rules, hooks, MCP |
| `.agents/plugins/marketplace.json` | Codex | Catalog `synaptory-dev`, resolves `./plugins/codex` |
| `plugins/codex/` | Codex | Codex host package — MCP server, hooks, managed role profiles |
| `plugin.json` | — | Convenience copy of the Claude Code catalog |

The plugin is named `synaptory` on every host regardless of which directory
holds it, so these paths are an implementation detail — you install by name.

All three hosts are published from the same build at the same version, so a
mixed-host team is never split across plugin releases.

**This tree ships stubs and plumbing only.** Skill bodies, agent protocols,
and rule sets are not stored here. They are delivered at runtime by the
control plane over an authenticated session, decrypted per request, and
watermarked to the fetching user. That is why a signed-in `synaptory` CLI is
a hard requirement rather than a convenience.

## Support

- In-session: ask Synaptory to report an issue from Claude Code, Codex, or Cursor.
- Email: engineering@h3t.co
- Control plane: https://synaptory.h3t.co

## License

Proprietary. Copyright © H3Tech Inc. All rights reserved. Redistribution or
use outside a valid H3Tech license agreement is prohibited.
