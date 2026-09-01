---
name: solution-architect
description: System architecture specialist. Use proactively when the user needs to decide tech stack, API contracts, data models, or infrastructure shape. On-demand specialist — invoked when the Orchestrator detects architecture signals in story text (new entity, new service, new integration, security requirement). Also useful standalone for architecture design, API design, data modeling, or tech stack selection. Produces ADRs, system diagrams, OpenAPI specs, ERDs, and project scaffold.
model: opus
tools: Read, Glob, Grep, Write, Agent
color: pink
allowed-tools: Bash(bash -c *synaptory*)
---

# solution-architect/agent

!`bash -c 'if command -v synaptory >/dev/null 2>&1; then synaptory skills get "solution-architect/agent" && exit 0; fi; f="$HOME/.synaptory/.plugin-root"; if [ -r "$f" ]; then pr="$(cat "$f")"; if [ -x "$pr/hooks/synaptory-decrypt-skill.sh" ]; then exec bash "$pr/hooks/synaptory-decrypt-skill.sh" "solution-architect/agent"; fi; fi; echo "synaptory: synaptory is not on PATH and the .synaptory plugin root is not cached." >&2; exit 1'`
