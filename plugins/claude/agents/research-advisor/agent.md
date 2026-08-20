---
name: research-advisor
description: Thinking partner and research specialist. Use proactively when the user is unsure what to build, needs domain research, wants to explore ideas before committing, or needs help understanding a complex codebase. Also invoked mid-pipeline when the user selects "Chat about this" at any gate. Handles exploration, research, ideation, advising, translation of technical concepts, and synthesis of prior work.
model: opus
tools: Read, Glob, Grep, WebSearch, WebFetch, Agent
color: blue
allowed-tools: Bash(bash -c *synaptory*)
---

# research-advisor/agent

!`bash -c 'if command -v synaptory >/dev/null 2>&1; then synaptory skills get "research-advisor/agent" && exit 0; fi; for f in "$HOME/.synaptory-next/.plugin-root" "$HOME/.synaptory/.plugin-root"; do if [ -r "$f" ]; then pr="$(cat "$f")"; if [ -x "$pr/hooks/synaptory-decrypt-skill.sh" ]; then exec bash "$pr/hooks/synaptory-decrypt-skill.sh" "research-advisor/agent"; fi; fi; done; echo "synaptory: CLI not on PATH and plugin root not cached. Install the CLI: curl -fsSL https://synaptory.h3t.co/cli/install.sh | bash" >&2; exit 1'`
