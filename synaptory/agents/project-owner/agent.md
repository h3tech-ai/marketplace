---
name: project-owner
description: Requirements and backlog specialist. Use proactively when the user wants to define what to build — turns product ideas and business goals into formal requirements (BRD), user stories, and acceptance criteria. Active every sprint cycle — refines backlog during Sprint Planning, processes review feedback, updates Product Backlog continuously. Also useful standalone for "I want to build...", "new feature...", or business requirements.
model: opus
tools: Read, Glob, Grep, Write, Agent
color: purple
allowed-tools: Bash(bash -c *synaptory*)
---

# project-owner/agent

!`bash -c 'if command -v synaptory >/dev/null 2>&1; then synaptory skills get "project-owner/agent" && exit 0; fi; for f in "$HOME/.synaptory-next/.plugin-root" "$HOME/.synaptory/.plugin-root"; do if [ -r "$f" ]; then pr="$(cat "$f")"; if [ -x "$pr/hooks/synaptory-decrypt-skill.sh" ]; then exec bash "$pr/hooks/synaptory-decrypt-skill.sh" "project-owner/agent"; fi; fi; done; echo "synaptory: CLI not on PATH and plugin root not cached. Install the CLI: curl -fsSL https://synaptory.h3t.co/cli/install.sh | bash" >&2; exit 1'`
