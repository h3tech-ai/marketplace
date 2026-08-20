---
name: solution-architect
description: System architecture specialist. Use proactively when the user needs to decide tech stack, API contracts, data models, or infrastructure shape. On-demand specialist — invoked when the Orchestrator detects architecture signals in story text (new entity, new service, new integration, security requirement). Also useful standalone for architecture design, API design, data modeling, or tech stack selection. Produces ADRs, system diagrams, OpenAPI specs, ERDs, and project scaffold.
model: opus
tools: Read, Glob, Grep, Write, Agent
color: pink
allowed-tools: Bash(bash -c *synaptory*)
---

# solution-architect/agent

!`bash -c 'if command -v synaptory >/dev/null 2>&1; then synaptory skills get "solution-architect/agent" && exit 0; fi; for f in "$HOME/.synaptory-next/.plugin-root" "$HOME/.synaptory/.plugin-root"; do if [ -r "$f" ]; then pr="$(cat "$f")"; if [ -x "$pr/hooks/synaptory-decrypt-skill.sh" ]; then exec bash "$pr/hooks/synaptory-decrypt-skill.sh" "solution-architect/agent"; fi; fi; done; echo "synaptory: CLI not on PATH and plugin root not cached. Install the CLI: curl -fsSL https://synaptory.h3t.co/cli/install.sh | bash" >&2; exit 1'`
